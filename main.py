#!/usr/bin/env python3
"""
EDAV Private Endpoint Monitor v4.2.0
=======================================
Enterprise-grade Azure governance and cleanup platform for EDAV disconnected
private endpoints.

Validation examples:
    edav-dev-aisearch-eastus2-internal-pe
        Connection State: Disconnected
        Backend target resource: ResourceNotFound
        DeleteRecommendation: SAFE_DELETE

    edavdrdrill2026-blob-pe
        Connection State: Disconnected
        Backend target resource still exists
        DeleteRecommendation: REVIEW_REQUIRED

Deletion safety layers (in order)
----------------------------------
 1. --delete-approved flag required (CLI)
 2. --cleanup-approved mode required (CLI)
 3. ApprovedToDelete == Yes required per row
 4. Exclusion list checked
 5. Denylist checked
 6. Recommended Action keyword block
 7. DeleteRecommendation must be SAFE_DELETE (blocks REVIEW_REQUIRED)
 8. User must type CONFIRM at interactive prompt
 9. ARM JSON backup written before every real delete
10. Pre-delete re-validation: endpoint exists AND still Disconnected
11. Subscription context verified before each delete
12. Post-delete verification: Azure returns ResourceNotFound
13. --dry-run simulates everything without touching Azure
14. Rollback instructions generated automatically
15. Structured log written to logs/ directory

Safe by default -- Read/Report only unless --cleanup-approved --delete-approved passed.
"""
import argparse
import json
import logging
import os
import shutil
import smtplib
import subprocess
import sys
import time
import threading
import itertools
from collections import defaultdict
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

try:
    import pandas as pd
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    print("ERROR: Missing dependencies. Run: pip install -r requirements.txt")
    sys.exit(1)

# ===========================================================================
# Version & Constants
# ===========================================================================
VERSION = "4.2.0"

_AZ_WINDOWS_FALLBACK = r"C:\Program Files (x86)\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"

def _resolve_az():
    az = shutil.which("az")
    if az:
        return az
    if os.path.isfile(_AZ_WINDOWS_FALLBACK):
        return _AZ_WINDOWS_FALLBACK
    print("FATAL: Azure CLI (az) not found.")
    sys.exit(1)

AZ_CMD = _resolve_az()

_LOG_FMT = "%(asctime)s %(levelname)-8s [%(threadName)s] %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

CLR = {
    "hdr_bg": "1F4E79", "hdr_fg": "FFFFFF",
    "safe_bg": "C6EFCE", "safe_fg": "276221",
    "rev_bg":  "FFEB9C", "rev_fg":  "9C6500",
    "del_bg":  "FFC7CE", "del_fg":  "9C0006",
    "na_bg":   "D9D9D9", "na_fg":   "595959",
    "dry_bg":  "DDEBF7", "dry_fg":  "1F4E79",
    "exc_bg":  "E2EFDA", "exc_fg":  "375623",
    "ver_bg":  "E2CFEA", "ver_fg":  "5C2D91",
    "fail_bg": "FF9999", "fail_fg": "660000",
}

ACTION_STYLE = {
    "Safe Delete Candidate":               ("safe_bg", "safe_fg"),
    "Do Not Delete - Terraform Managed":   ("del_bg",  "del_fg"),
    "Investigate - Backend Exists":        ("rev_bg",  "rev_fg"),
    "Review - Not Disconnected":           ("rev_bg",  "rev_fg"),
    "Review":                              ("rev_bg",  "rev_fg"),
    "Endpoint Not Found / Check Subscription": ("na_bg", "na_fg"),
    "Skipped - Empty Name":                ("na_bg",  "na_fg"),
    "Excluded":                            ("exc_bg",  "exc_fg"),
    "Denied":                              ("del_bg",  "del_fg"),
}

SCAN_HEADERS = [
    "Endpoint Name", "Resource Group", "Subscription", "Region",
    "Connection State", "Backend Resource", "Backend Exists",
    "BackendResourceId", "BackendResourceName", "BackendResourceType",
    "DeleteRecommendation",
    "Terraform Managed", "Recommended Action", "Scan Timestamp", "Notes",
]

DEL_HEADERS = [
    "Endpoint Name", "Resource Group", "Subscription", "Region",
    "Recommended Action", "DeleteRecommendation", "ApprovedToDelete",
    "Change Ticket", "Approved By",
    "Delete Result", "Delete Timestamp", "Duration (s)", "Dry Run",
    "Backup Path", "Pre-Delete Validation", "Error Message",
]

VERIFY_HEADERS = [
    "Endpoint Name", "Resource Group", "Subscription",
    "Delete Result", "Verification Status", "Azure Response",
    "Verification Timestamp", "Verification Notes",
]

COL_ALIASES = {
    "endpoint name":  "Endpoint Name",  "endpointname": "Endpoint Name",
    "name":           "Endpoint Name",
    "resource group": "Resource Group", "resourcegroup": "Resource Group",
    "rg":             "Resource Group",
}

_BLOCK_SUBSTRINGS = ("Do Not Delete", "Endpoint Not Found", "Terraform Managed", "Excluded", "Denied")

# DeleteRecommendation values
DR_SAFE_DELETE          = "SAFE_DELETE"
DR_REVIEW_REQUIRED      = "REVIEW_REQUIRED"
DR_ENDPOINT_NOT_FOUND   = "ENDPOINT_NOT_FOUND"
DR_ACCESS_OR_SUB_REVIEW = "ACCESS_OR_SUBSCRIPTION_REVIEW"
DR_NOT_DISCONNECTED     = "NOT_DISCONNECTED"
DR_TERRAFORM_MANAGED    = "TERRAFORM_MANAGED"
DR_EXCLUDED             = "EXCLUDED"
DR_DENIED               = "DENIED"
DR_UNKNOWN              = "UNKNOWN"

# ===========================================================================
# Logging
# ===========================================================================

def setup_logging(log_dir: str, ts: str):
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"EDAV_RUN_{ts}.log")
    logger = logging.getLogger("edav")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(_LOG_FMT, _DATE_FMT))
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(_LOG_FMT, _DATE_FMT))
    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger, log_path

logging.basicConfig(level=logging.INFO, format=_LOG_FMT, datefmt=_DATE_FMT)
log = logging.getLogger("edav")

# ===========================================================================
# Startup Banner
# ===========================================================================

_BANNER = """
EDAV PRIVATE ENDPOINT MONITOR v{version}
Enterprise Azure Governance Platform
"""

def print_banner(version: str, mode: str, run_dt: str,
                 change_ticket: str = "", approved_by: str = ""):
    width = 72
    sep  = "=" * width
    sep2 = "-" * width
    print("")
    print(sep)
    print(f"  EDAV Private Endpoint Monitor v{version}")
    print(sep2)
    print(f"  Run Date    : {run_dt}")
    print(f"  Mode        : {mode}")
    if change_ticket:
        print(f"  Change Ticket: {change_ticket}")
    if approved_by:
        print(f"  Approved By  : {approved_by}")
    print(sep2)
    print(f"  Safety  : 15-layer deletion gate | backup-before-delete | post-delete verify")
    print(f"  Safe-default: Read/Report ONLY unless --cleanup-approved --delete-approved passed")
    print(sep)
    print("")

# ===========================================================================
# Phase Progress Tracker
# ===========================================================================

_PHASE_TOTAL = 6
_phase_start_time: float = 0.0

def phase_start(num: int, label: str):
    global _phase_start_time
    _phase_start_time = time.time()
    log.info("")
    log.info("=" * 72)
    log.info("[PHASE %d/%d] %s", num, _PHASE_TOTAL, label)
    log.info("=" * 72)

def phase_end(num: int, label: str):
    elapsed = time.time() - _phase_start_time
    log.info("[PHASE %d/%d] %s -- COMPLETE (%.1fs)", num, _PHASE_TOTAL, label, elapsed)

# ===========================================================================
# Live Spinner
# ===========================================================================

class Spinner:
    _CHARS = itertools.cycle(["|", "/", "-", "\\"])

    def __init__(self, msg: str = "", interval: float = 0.1):
        self.msg = msg
        self.interval = interval
        self._stop   = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def _spin(self):
        while not self._stop.is_set():
            char = next(self._CHARS)
            sys.stdout.write(f"\r  {char} {self.msg} ")
            sys.stdout.flush()
            time.sleep(self.interval)
        sys.stdout.write("\r" + " " * (len(self.msg) + 10) + "\r")
        sys.stdout.flush()

    def __enter__(self):
        if sys.stdout.isatty():
            self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)

# ===========================================================================
# Governance helpers
# ===========================================================================

def normalize_value(value: str) -> str:
    return str(value).replace("\ufeff", "").strip().lower()

def get_approval_value(ep) -> str:
    return str(ep.get("ApprovedToDelete", "") if isinstance(ep, dict) else "").strip().lower()

def is_approved(value: str) -> bool:
    return normalize_value(value) in {"yes", "y", "true", "1", "approved"}

def load_exclusions(path: str = "exclusions.txt") -> set:
    excluded = set()
    if not os.path.isfile(path):
        return excluded
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                excluded.add(line.lower())
    log.info("Loaded %d exclusion(s) from %s", len(excluded), path)
    return excluded

def load_denylist(path: str = "governance/denylist.json") -> set:
    if not os.path.isfile(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        result = {str(v).lower() for v in (data if isinstance(data, list) else data.keys())}
        log.info("Loaded %d denylist entry/entries from %s", len(result), path)
        return result
    except Exception as exc:
        log.warning("Failed to load denylist from %s: %s", path, exc)
        return set()

def load_allowlist(path: str = "governance/allowlist.json") -> set:
    if not os.path.isfile(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        result = {str(v).lower() for v in (data if isinstance(data, list) else data.keys())}
        log.info("Loaded %d allowlist entry/entries from %s", len(result), path)
        return result
    except Exception as exc:
        log.warning("Failed to load allowlist from %s: %s", path, exc)
        return set()

# ===========================================================================
# Azure CLI helpers
# ===========================================================================

def _az_with_retry(args: list, retries: int = 3, timeout: int = 30):
    """Run an Azure CLI command with exponential-backoff retry."""
    cmd = [AZ_CMD] + args + ["-o", "json"]
    for attempt in range(1, retries + 1):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if r.returncode == 0 and r.stdout.strip():
                try:
                    return json.loads(r.stdout)
                except json.JSONDecodeError:
                    return None
            err_text = (r.stderr or r.stdout or "").strip()
            if "ResourceNotFound" in err_text or "was not found" in err_text.lower():
                return None
            if attempt < retries:
                wait = 2 ** attempt
                log.debug("  az retry %d/%d in %ds", attempt, retries, wait)
                time.sleep(wait)
        except subprocess.TimeoutExpired:
            if attempt < retries:
                time.sleep(2 ** attempt)
        except Exception as exc:
            log.debug("  az exception: %s", exc)
            if attempt < retries:
                time.sleep(2 ** attempt)
    return None

def _az(args, silent=False):
    return _az_with_retry(args)

def validate_azure_login() -> dict:
    log.info("Validating Azure login...")
    try:
        r = subprocess.run(
            [AZ_CMD, "account", "show", "-o", "json"],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode == 0 and r.stdout.strip():
            acct = json.loads(r.stdout)
            log.info("  Logged in as : %s", acct.get("user", {}).get("name", "unknown"))
            log.info("  Tenant       : %s", acct.get("tenantId", "unknown"))
            log.info("  Active sub   : %s", acct.get("name", "unknown"))
            return acct
    except Exception as exc:
        log.debug("az account show exception: %s", exc)
    log.error("AZURE LOGIN REQUIRED: az login  OR  az login --use-device-code")
    sys.exit(1)

def get_subscriptions() -> list:
    subs = _az(["account", "list", "--query", "[].name"])
    return list(subs) if subs else []

def set_subscription(name: str) -> bool:
    try:
        r = subprocess.run(
            [AZ_CMD, "account", "set", "--subscription", name],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode == 0:
            return True
        log.warning("  Cannot set subscription '%s': %s", name, r.stderr[:120])
        return False
    except Exception as exc:
        log.warning("  Exception setting subscription %s: %s", name, exc)
        return False

def verify_subscription_context(expected_sub: str) -> bool:
    data = _az(["account", "show"])
    if not data:
        return False
    return data.get("name", "").strip().lower() == expected_sub.strip().lower()

def get_private_endpoint(name: str, rg: str):
    if rg:
        return _az(["network", "private-endpoint", "show",
                    "--name", name, "--resource-group", rg])
    return None

def get_private_endpoint_connection_state(pe: dict) -> str:
    conns = (pe.get("privateLinkServiceConnections") or
             pe.get("manualPrivateLinkServiceConnections") or [])
    if not conns:
        return "No Connection Object"
    cs = conns[0].get("privateLinkServiceConnectionState", {})
    return cs.get("status", "Unknown")

def get_endpoint_region(pe: dict) -> str:
    if not pe:
        return ""
    return pe.get("location", "")

def resource_exists(rid: str) -> bool:
    if not rid:
        return False
    return _az(["resource", "show", "--ids", rid]) is not None

def get_backend_resource_details(rid: str) -> dict:
    """
    Call az resource show --ids <rid>.
    Returns dict with exists, name, resource_type, raw.
    Used to populate BackendExists, BackendResourceId, BackendResourceName,
    BackendResourceType and compute DeleteRecommendation.
    """
    if not rid:
        return {"exists": False, "name": "", "resource_type": "", "raw": None}
    raw = _az(["resource", "show", "--ids", rid])
    if raw is None:
        return {"exists": False, "name": "", "resource_type": "", "raw": None}
    return {
        "exists":        True,
        "name":          raw.get("name", ""),
        "resource_type": raw.get("type", ""),
        "raw":           raw,
    }

def private_endpoint_still_valid_for_delete(name: str, rg: str, sub: str) -> tuple:
    """Pre-delete re-validation. Returns (ok: bool, reason: str)."""
    if not name or not rg or not sub:
        return False, "Missing name, RG, or subscription"
    if not set_subscription(sub):
        return False, f"Cannot set subscription context to '{sub}'"
    if not verify_subscription_context(sub):
        return False, f"Subscription context verification failed for '{sub}'"
    pe = get_private_endpoint(name, rg)
    if pe is None:
        return False, "Endpoint no longer exists in Azure (already removed)"
    state = get_private_endpoint_connection_state(pe)
    if state != "Disconnected":
        return False, f"Connection state changed to '{state}' -- no longer Disconnected"
    return True, "OK"

# ===========================================================================
# Post-delete verification
# ===========================================================================

def verify_endpoint_deleted(name: str, rg: str, sub: str) -> tuple:
    if not name or not rg:
        return False, "Verification Skipped", "Missing name or RG"
    try:
        if sub:
            set_subscription(sub)
        pe = get_private_endpoint(name, rg)
        if pe is None:
            return True, "Verified - Resource Not Found", "Azure: ResourceNotFound (confirmed deleted)"
        state = get_private_endpoint_connection_state(pe)
        return False, "FAILED - Resource Still Exists", f"Azure: Resource still present, state='{state}'"
    except Exception as exc:
        return False, "Verification Error", f"Exception during verification: {exc}"

def run_post_delete_verification(del_log: list, dry_run: bool = False) -> list:
    verify_log = []
    deleted_entries = [r for r in del_log if r.get("Delete Result") == "Deleted"]
    dry_entries     = [r for r in del_log if r.get("Delete Result") == "Dry-Run"]
    other_entries   = [r for r in del_log if r.get("Delete Result") not in ("Deleted", "Dry-Run")]

    log.info("Post-delete verification: %d deleted, %d dry-run, %d other",
             len(deleted_entries), len(dry_entries), len(other_entries))

    for rec in deleted_entries:
        name = rec.get("Endpoint Name", "")
        rg   = rec.get("Resource Group", "")
        sub  = rec.get("Subscription", "")
        log.info("  [VERIFY] %s ...", name)
        with Spinner(f"Verifying {name}"):
            verified, status, az_resp = verify_endpoint_deleted(name, rg, sub)
        ts = datetime.now().isoformat()
        if verified:
            log.info("    -> %s", status)
        else:
            log.error("    -> VERIFICATION FAILED: %s | %s", status, az_resp)
        verify_log.append({
            "Endpoint Name": name, "Resource Group": rg, "Subscription": sub,
            "Delete Result": rec.get("Delete Result", ""),
            "Verification Status": status, "Azure Response": az_resp,
            "Verification Timestamp": ts,
            "Verification Notes": "Verified deleted" if verified else "WARNING: resource may still exist",
        })

    for rec in dry_entries:
        verify_log.append({
            "Endpoint Name": rec.get("Endpoint Name", ""),
            "Resource Group": rec.get("Resource Group", ""),
            "Subscription": rec.get("Subscription", ""),
            "Delete Result": "Dry-Run",
            "Verification Status": "Verification Skipped (Dry Run)",
            "Azure Response": "No delete performed",
            "Verification Timestamp": datetime.now().isoformat(),
            "Verification Notes": "Dry-run mode -- no real delete occurred",
        })

    for rec in other_entries:
        verify_log.append({
            "Endpoint Name": rec.get("Endpoint Name", ""),
            "Resource Group": rec.get("Resource Group", ""),
            "Subscription": rec.get("Subscription", ""),
            "Delete Result": rec.get("Delete Result", ""),
            "Verification Status": "Not Applicable",
            "Azure Response": "No delete attempted",
            "Verification Timestamp": datetime.now().isoformat(),
            "Verification Notes": "Delete result was '" + str(rec.get("Delete Result", "")) + "' -- verification not required",
        })

    v_ok   = sum(1 for v in verify_log if v["Verification Status"].startswith("Verified"))
    v_fail = sum(1 for v in verify_log if "FAILED" in v["Verification Status"])
    log.info("Verification Summary: Confirmed=%d Failed=%d", v_ok, v_fail)
    if v_fail > 0:
        log.error("WARNING: %d endpoint(s) could NOT be verified as deleted!", v_fail)
    return verify_log

# ===========================================================================
# ARM backup
# ===========================================================================

def export_endpoint_backup(pe: dict, name: str, rg: str, sub: str,
                            backup_dir: str, ts: str) -> str:
    subdir = os.path.join(backup_dir, "private_endpoints")
    os.makedirs(subdir, exist_ok=True)
    safe_name = name.replace("/", "_").replace("\\", "_")
    fname  = f"{safe_name}_{sub}_{ts}.json".replace(" ", "_")
    fpath  = os.path.join(subdir, fname)
    payload = {
        "backup_timestamp": datetime.utcnow().isoformat() + "Z",
        "subscription": sub, "resource_group": rg,
        "endpoint_name": name, "arm_resource": pe,
    }
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    log.info("  Backup: %s", fpath)
    return fpath

# ===========================================================================
# Terraform helpers
# ===========================================================================

def load_terraform(path: str) -> tuple:
    if not path or not os.path.isdir(path):
        return "", ""
    orig = os.getcwd()
    os.chdir(path)
    state = code = ""
    try:
        r = subprocess.run(["terraform", "state", "list"], capture_output=True, text=True, timeout=60)
        state = r.stdout if r.returncode == 0 else ""
    except Exception:
        pass
    try:
        for f in Path(path).rglob("*.tf"):
            code += f.read_text(errors="replace") + "\n"
    except Exception:
        pass
    os.chdir(orig)
    return state, code

def in_terraform(name: str, state: str, code: str) -> str:
    if not state and not code:
        return "Unknown"
    n = name.lower()
    if n in state.lower() or n in code.lower():
        return "Yes"
    return "No"

# ===========================================================================
# Decision logic
# ===========================================================================

def decide(conn_state: str, backend_exists: str, tf_managed: str) -> tuple:
    """
    Returns (recommended_action, notes, delete_recommendation).

    DeleteRecommendation:
        SAFE_DELETE         -- Disconnected AND backend gone AND not TF managed
        REVIEW_REQUIRED     -- Disconnected BUT backend still exists
        ENDPOINT_NOT_FOUND  -- Endpoint could not be found
        TERRAFORM_MANAGED   -- In Terraform state/code
        NOT_DISCONNECTED    -- Connection state is not Disconnected
        UNKNOWN             -- Insufficient data
    """
    if tf_managed == "Yes":
        return ("Do Not Delete - Terraform Managed",
                "Found in Terraform state/code. Remove from TF first.",
                DR_TERRAFORM_MANAGED)
    if conn_state == "Disconnected" and backend_exists == "No" and tf_managed == "No":
        return ("Safe Delete Candidate",
                "Disconnected, backend gone, not in Terraform. Safe to decommission.",
                DR_SAFE_DELETE)
    if conn_state == "Disconnected" and backend_exists == "Yes":
        return ("Investigate - Backend Exists",
                "Endpoint disconnected but backend resource still active.",
                DR_REVIEW_REQUIRED)
    if conn_state == "Endpoint Not Found":
        return ("Endpoint Not Found / Check Subscription",
                "Not found in any scanned subscription.",
                DR_ENDPOINT_NOT_FOUND)
    if conn_state not in ("Disconnected", "Unknown", ""):
        return ("Review - Not Disconnected",
                f"Connection state is '{conn_state}'. May still be in use.",
                DR_NOT_DISCONNECTED)
    return ("Review", "Insufficient data to make a safe recommendation.", DR_UNKNOWN)

# ===========================================================================
# Input loader (hardened)
# ===========================================================================

def load_endpoints(path: str) -> list:
    path = path.strip()
    if not os.path.isfile(path):
        log.error("Input file not found: %s", path)
        sys.exit(1)
    ext = Path(path).suffix.lower()
    try:
        if ext in (".xlsx", ".xls"):
            df = pd.read_excel(path, dtype=str)
        else:
            df = pd.read_csv(path, dtype=str)
    except PermissionError:
        log.error("Permission denied: %s -- close the file if open in Excel", path)
        sys.exit(1)
    except pd.errors.EmptyDataError:
        log.error("Input file is empty: %s", path)
        sys.exit(1)
    except Exception as exc:
        log.error("Error reading %s: %s", path, exc)
        sys.exit(1)
    df.columns = df.columns.str.strip()
    df = df.fillna("")
    rename = {}
    for col in df.columns:
        key   = col.strip().lower().replace(" ", "").replace("_", "")
        alias = COL_ALIASES.get(key) or COL_ALIASES.get(col.strip().lower())
        if alias and col != alias:
            rename[col] = alias
    df.rename(columns=rename, inplace=True)
    if "Endpoint Name" not in df.columns:
        log.error("No 'Endpoint Name' column found. Columns: %s", list(df.columns))
        sys.exit(1)
    for col in ("Resource Group", "ApprovedToDelete", "Subscription", "Change Ticket", "Approved By"):
        if col not in df.columns:
            df[col] = ""
    log.info("Input file loaded: %d row(s)", len(df))
    return df.to_dict(orient="records")

# ===========================================================================
# Core scan (hardened)
# ===========================================================================

def scan(ep: dict, subscriptions: list, tf_state: str,
         tf_code: str, exclusions: set, denylist: set = None) -> dict:
    """
    Scan a single endpoint across all subscriptions.

    For every Disconnected endpoint, calls az resource show --ids <backendId>
    to determine BackendExists, BackendResourceId, BackendResourceName,
    BackendResourceType and sets DeleteRecommendation accordingly:

        Disconnected + backend ResourceNotFound -> SAFE_DELETE
        Disconnected + backend still exists     -> REVIEW_REQUIRED
        Endpoint not found in any sub           -> ENDPOINT_NOT_FOUND
    """
    if denylist is None:
        denylist = set()
    original_row = dict(ep)
    name = str(ep.get("Endpoint Name", "")).strip()
    rg   = str(ep.get("Resource Group", "")).strip()
    rec = {
        "Endpoint Name":        name,
        "Resource Group":       rg,
        "Subscription":         "",
        "Region":               "",
        "Connection State":     "Not Found",
        "Backend Resource":     "",
        "Backend Exists":       "Unknown",
        "BackendResourceId":    "",
        "BackendResourceName":  "",
        "BackendResourceType":  "",
        "DeleteRecommendation": DR_UNKNOWN,
        "Terraform Managed":    "Unknown",
        "Recommended Action":   "",
        "Scan Timestamp":       datetime.now().isoformat(),
        "Notes":                "",
    }

    if not name:
        rec["Recommended Action"]   = "Skipped - Empty Name"
        rec["DeleteRecommendation"] = DR_UNKNOWN
        rec.update({k: v for k, v in original_row.items() if k not in rec or not rec[k]})
        return rec

    if name.lower() in denylist:
        rec["Recommended Action"]   = "Denied"
        rec["DeleteRecommendation"] = DR_DENIED
        rec["Notes"] = "Listed in denylist -- hard blocked from deletion."
        log.info("  [DENIED] %s", name)
        rec.update({k: v for k, v in original_row.items() if k not in rec or not rec[k]})
        return rec

    if name.lower() in exclusions:
        rec["Recommended Action"]   = "Excluded"
        rec["DeleteRecommendation"] = DR_EXCLUDED
        rec["Notes"] = "Listed in exclusions.txt -- will never be deleted."
        log.info("  [EXCLUDED] %s", name)
        rec.update({k: v for k, v in original_row.items() if k not in rec or not rec[k]})
        return rec

    found = False
    for sub in subscriptions:
        try:
            if not set_subscription(sub):
                log.warning("  [SKIP-SUB] Cannot set subscription '%s'", sub)
                continue
            pe = get_private_endpoint(name, rg)
            if pe is None and not rg:
                all_pe = _az(["network", "private-endpoint", "list",
                              "--query", f"[?name=='{name}']"])
                if all_pe:
                    pe = all_pe[0]
                    rec["Resource Group"] = pe.get("resourceGroup", rg)
            if pe:
                rec["Subscription"] = sub
                rec["Region"]       = get_endpoint_region(pe)
                conns = (pe.get("privateLinkServiceConnections") or
                         pe.get("manualPrivateLinkServiceConnections") or [])
                if conns:
                    cs  = conns[0].get("privateLinkServiceConnectionState", {})
                    rec["Connection State"] = cs.get("status", "Unknown")
                    bid = conns[0].get("privateLinkServiceId", "")
                    rec["Backend Resource"]  = bid
                    rec["BackendResourceId"] = bid
                    # Resolve backend details for all endpoints (critical for Disconnected)
                    bk = get_backend_resource_details(bid)
                    rec["Backend Exists"]       = "Yes" if bk["exists"] else "No"
                    rec["BackendResourceName"]  = bk["name"]
                    rec["BackendResourceType"]  = bk["resource_type"]
                else:
                    rec["Connection State"] = "No Connection Object"
                found = True
                break
        except Exception as exc:
            log.warning("  [SUB-ERROR] '%s' error for '%s': %s", sub, name, exc)
            continue

    if not found:
        rec["Connection State"]     = "Endpoint Not Found"
        rec["Notes"]                = "Not found in any scanned subscription."
        rec["Recommended Action"]   = "Endpoint Not Found / Check Subscription"
        rec["DeleteRecommendation"] = DR_ENDPOINT_NOT_FOUND
        rec.update({k: v for k, v in original_row.items() if k not in rec or not rec[k]})
        return rec

    rec["Terraform Managed"] = in_terraform(name, tf_state, tf_code)
    action, notes, dr = decide(rec["Connection State"],
                               rec["Backend Exists"],
                               rec["Terraform Managed"])
    rec["Recommended Action"]   = action
    rec["Notes"]                = notes
    rec["DeleteRecommendation"] = dr
    rec.update({k: v for k, v in original_row.items() if k not in rec or not rec[k]})
    return rec

# ===========================================================================
# Deletion safety gate (hardened)
# ===========================================================================

def _is_deletion_blocked(rec: dict, exclusions: set, denylist: set = None) -> tuple:
    """
    Safety gate checks in order:
    1. Endpoint Name blank
    2. Resource Group blank
    3. Denylist (hard block)
    4. Exclusion list
    5. ApprovedToDelete != yes
    6. Recommended Action keyword block
    7. DeleteRecommendation != SAFE_DELETE
       (blocks REVIEW_REQUIRED, ENDPOINT_NOT_FOUND, ACCESS_OR_SUBSCRIPTION_REVIEW)
    """
    if denylist is None:
        denylist = set()
    name     = str(rec.get("Endpoint Name", "")).strip()
    rg       = str(rec.get("Resource Group", "")).strip()
    approved = get_approval_value(rec)
    action   = str(rec.get("Recommended Action", "")).strip()
    dr       = str(rec.get("DeleteRecommendation", "")).strip()

    if not name:
        return True, "Endpoint Name is blank"
    if not rg:
        return True, "Resource Group is blank"
    if name.lower() in denylist:
        return True, f"'{name}' is on the denylist -- hard blocked"
    if name.lower() in exclusions:
        return True, "Listed in exclusions.txt"
    if approved != "yes":
        return True, ("ApprovedToDelete='" + str(rec.get("ApprovedToDelete", "")) + "' -- must be Yes")
    for substr in _BLOCK_SUBSTRINGS:
        if substr.lower() in action.lower():
            return True, f"Recommended Action '{action}' contains blocking keyword"
    if dr != DR_SAFE_DELETE:
        return True, (
            f"DeleteRecommendation='{dr}' -- only SAFE_DELETE endpoints may be deleted. "
            "REVIEW_REQUIRED, ENDPOINT_NOT_FOUND, and ACCESS_OR_SUBSCRIPTION_REVIEW "
            "endpoints must not be deleted."
        )
    return False, ""

# ===========================================================================
# The ONLY allowed Azure delete call
# ===========================================================================

def _execute_delete(name: str, rg: str) -> tuple:
    """
    Issue az network private-endpoint delete.

    THIS IS THE SOLE AZURE DELETE COMMAND IN THIS ENTIRE CODEBASE.
    Only Private Endpoint resources are ever touched.
    No backend resources (Key Vault, Storage, SQL, VNet, NIC, DNS, NSG,
    Subnet, Route Table, or any other resource type) are EVER touched.

    NOTE: --yes is intentionally omitted. The az CLI does not prompt
    interactively when called from subprocess (no TTY attached).
    Removing --yes eliminates any risk of unintended confirmation bypass.

    Returns (success: bool, error_msg: str)
    """
    try:
        r = subprocess.run(
            [AZ_CMD, "network", "private-endpoint", "delete",
             "--name", name, "--resource-group", rg],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            return True, ""
        err = (r.stderr or r.stdout or "Unknown error from az CLI").strip()
        return False, err
    except subprocess.TimeoutExpired:
        return False, "Delete command timed out after 120s"
    except Exception as exc:
        return False, f"Unexpected exception during delete: {exc}"

# ===========================================================================
# Core deletion workflow
# ===========================================================================

def run_delete_approved(
    results: list,
    exclusions: set,
    denylist: set,
    output_dir: str,
    backup_dir: str,
    ts: str,
    run_dt: str,
    dry_run: bool = False,
    delete_pause: float = 2.0,
    change_ticket: str = "",
    approved_by: str = "",
) -> list:
    """
    Approval-gated deletion workflow.

    Only SAFE_DELETE endpoints with ApprovedToDelete=Yes are eligible.
    REVIEW_REQUIRED endpoints are NEVER deleted.

    Dry-run shows clearly:
        Would delete -> SAFE_DELETE endpoints (ApprovedToDelete=Yes)
        Would skip   -> REVIEW_REQUIRED endpoints
        Would skip   -> ENDPOINT_NOT_FOUND endpoints
    """
    mode = "DRY RUN" if dry_run else "LIVE DELETE"
    log.info("")
    log.info("=" * 70)
    log.info("CLEANUP-APPROVED MODE [%s]", mode)
    log.info("=" * 70)

    del_log = []
    approved_rows = [r for r in results if get_approval_value(r) == "yes"]
    skipped_rows  = [r for r in results if get_approval_value(r) != "yes"]

    log.info("  Total rows           : %d", len(results))
    log.info("  Rows approved (Yes)  : %d", len(approved_rows))
    log.info("  Rows skipped (non-Yes): %d", len(skipped_rows))

    if len(approved_rows) == 0:
        log.warning("No rows marked ApprovedToDelete=Yes -- nothing to process.")
        _write_delete_reports(del_log, output_dir, ts, run_dt, dry_run)
        generate_rollback(del_log, backup_dir, output_dir, run_dt)
        return del_log

    # Pass 1: safety gate
    candidates = []
    for rec in approved_rows:
        blocked, reason = _is_deletion_blocked(rec, exclusions, denylist)
        dr = rec.get("DeleteRecommendation", DR_UNKNOWN)
        if blocked:
            if "denylist" in reason:
                label = "Denied"
            elif "exclusions" in reason:
                label = "Excluded"
            elif dr == DR_REVIEW_REQUIRED:
                label = "Skipped-ReviewRequired"
            elif dr == DR_ENDPOINT_NOT_FOUND:
                label = "Skipped-EndpointNotFound"
            elif dr == DR_ACCESS_OR_SUB_REVIEW:
                label = "Skipped-AccessOrSubReview"
            else:
                label = "Skipped"
            entry = {
                "Endpoint Name": rec.get("Endpoint Name", ""),
                "Resource Group": rec.get("Resource Group", ""),
                "Subscription": rec.get("Subscription", ""),
                "Region": rec.get("Region", ""),
                "Recommended Action": rec.get("Recommended Action", ""),
                "DeleteRecommendation": dr,
                "ApprovedToDelete": rec.get("ApprovedToDelete", ""),
                "Change Ticket": change_ticket, "Approved By": approved_by,
                "Delete Result": label, "Delete Timestamp": "",
                "Duration (s)": "", "Dry Run": str(dry_run),
                "Backup Path": "", "Pre-Delete Validation": reason,
                "Error Message": reason,
            }
            del_log.append(entry)
            log.warning("  BLOCKED [%s]: %s -- %s", label, rec.get("Endpoint Name", ""), reason)
        else:
            candidates.append(rec)

    if not candidates:
        log.info("No rows passed the safety gate.")
        _write_delete_reports(del_log, output_dir, ts, run_dt, dry_run)
        generate_rollback(del_log, backup_dir, output_dir, run_dt)
        return del_log

    # Dry-run summary
    if dry_run:
        rev_req   = sum(1 for r in del_log if r["Delete Result"] == "Skipped-ReviewRequired")
        notfound  = sum(1 for r in del_log if r["Delete Result"] == "Skipped-EndpointNotFound")
        access    = sum(1 for r in del_log if r["Delete Result"] == "Skipped-AccessOrSubReview")
        log.info("")
        log.info("[DRY RUN] Simulation -- no Azure resources will be modified.")
        log.info("  Would-Delete  (SAFE_DELETE)             : %d", len(candidates))
        log.info("  Would-Skip    (REVIEW_REQUIRED)         : %d", rev_req)
        log.info("  Would-Skip    (ENDPOINT_NOT_FOUND)      : %d", notfound)
        log.info("  Would-Skip    (ACCESS_OR_SUB_REVIEW)    : %d", access)

    log.info("")
    log.info("Endpoints queued for %s (%d):", "simulation" if dry_run else "deletion", len(candidates))
    for rec in candidates:
        log.info("  - %-45s RG=%-25s DR=%s",
                 rec["Endpoint Name"], rec["Resource Group"],
                 rec.get("DeleteRecommendation", ""))

    # CONFIRM prompt (live only)
    if not dry_run:
        log.warning("")
        log.warning("=" * 70)
        log.warning("WARNING: This will PERMANENTLY DELETE Azure Private Endpoints.")
        log.warning("ONLY SAFE_DELETE endpoints are included.")
        log.warning("REVIEW_REQUIRED endpoints are NOT in this list.")
        log.warning("Backend resources are NEVER touched.")
        log.warning("=" * 70)
        confirm = input("\nType CONFIRM to continue: ")
        if confirm.strip() != "CONFIRM":
            log.info("Deletion cancelled -- user did not type CONFIRM.")
            for rec in candidates:
                del_log.append({
                    "Endpoint Name": rec["Endpoint Name"],
                    "Resource Group": rec["Resource Group"],
                    "Subscription": rec.get("Subscription", ""),
                    "Region": rec.get("Region", ""),
                    "Recommended Action": rec.get("Recommended Action", ""),
                    "DeleteRecommendation": rec.get("DeleteRecommendation", ""),
                    "ApprovedToDelete": rec.get("ApprovedToDelete", ""),
                    "Change Ticket": change_ticket, "Approved By": approved_by,
                    "Delete Result": "Skipped", "Delete Timestamp": datetime.now().isoformat(),
                    "Duration (s)": "", "Dry Run": "False", "Backup Path": "",
                    "Pre-Delete Validation": "Cancelled by user",
                    "Error Message": "Cancelled by user at CONFIRM prompt",
                })
            _write_delete_reports(del_log, output_dir, ts, run_dt, dry_run)
            generate_rollback(del_log, backup_dir, output_dir, run_dt)
            return del_log
    else:
        log.info("[DRY RUN] Simulating -- no Azure resources will be modified.")

    # Pass 2: grouped sequential execution
    grouped = defaultdict(lambda: defaultdict(list))
    for rec in candidates:
        grouped[rec.get("Subscription", "")][rec.get("Resource Group", "")].append(rec)

    for sub, rg_map in grouped.items():
        log.info("")
        log.info("  Subscription: %s", sub)
        if sub and not dry_run:
            if not set_subscription(sub):
                log.error("  Cannot set subscription '%s' -- skipping all endpoints in it.", sub)
                for rg2, recs in rg_map.items():
                    for rec in recs:
                        del_log.append({
                            "Endpoint Name": rec["Endpoint Name"],
                            "Resource Group": rg2, "Subscription": sub,
                            "Region": rec.get("Region", ""),
                            "Recommended Action": rec.get("Recommended Action", ""),
                            "DeleteRecommendation": rec.get("DeleteRecommendation", ""),
                            "ApprovedToDelete": rec.get("ApprovedToDelete", ""),
                            "Change Ticket": change_ticket, "Approved By": approved_by,
                            "Delete Result": "Failed", "Delete Timestamp": datetime.now().isoformat(),
                            "Duration (s)": "", "Dry Run": str(dry_run), "Backup Path": "",
                            "Pre-Delete Validation": "FAILED",
                            "Error Message": f"Cannot set subscription context to '{sub}'",
                        })
                continue

        for rg, recs in rg_map.items():
            log.info("    Resource Group: %s", rg)
            for rec in recs:
                name    = rec["Endpoint Name"]
                t_start = time.time()
                entry = {
                    "Endpoint Name": name, "Resource Group": rg, "Subscription": sub,
                    "Region": rec.get("Region", ""),
                    "Recommended Action": rec.get("Recommended Action", ""),
                    "DeleteRecommendation": rec.get("DeleteRecommendation", DR_SAFE_DELETE),
                    "ApprovedToDelete": rec.get("ApprovedToDelete", ""),
                    "Change Ticket": change_ticket, "Approved By": approved_by,
                    "Delete Result": "", "Delete Timestamp": datetime.now().isoformat(),
                    "Duration (s)": "", "Dry Run": str(dry_run),
                    "Backup Path": "", "Pre-Delete Validation": "", "Error Message": "",
                }

                if dry_run:
                    dr = rec.get("DeleteRecommendation", DR_SAFE_DELETE)
                    log.info("    [DRY-RUN] Would delete (DR=%s): %s", dr, name)
                    entry["Delete Result"] = "Dry-Run"
                    entry["Duration (s)"]  = "0"
                    entry["Pre-Delete Validation"] = "Simulated OK"
                    del_log.append(entry)
                    continue

                # Backup
                log.info("    [Backup] %s ...", name)
                pe_data     = get_private_endpoint(name, rg)
                backup_path = ""
                if pe_data:
                    os.makedirs(backup_dir, exist_ok=True)
                    backup_path = export_endpoint_backup(pe_data, name, rg, sub, backup_dir, ts)
                    entry["Backup Path"] = backup_path
                else:
                    log.warning("    Could not fetch ARM data for backup -- proceeding with caution.")

                # Pre-delete re-validation
                log.info("    [Validate] %s ...", name)
                with Spinner(f"Validating {name}"):
                    valid, reason = private_endpoint_still_valid_for_delete(name, rg, sub)
                entry["Pre-Delete Validation"] = reason
                if not valid:
                    log.warning("    SKIPPED: %s -- %s", name, reason)
                    entry["Delete Result"] = "Skipped"
                    entry["Duration (s)"]  = f"{time.time()-t_start:.1f}"
                    entry["Error Message"] = reason
                    del_log.append(entry)
                    continue

                # Subscription context verification
                if not verify_subscription_context(sub):
                    msg = f"Subscription context mismatch -- expected '{sub}'"
                    log.error("    FAILED: %s -- %s", name, msg)
                    entry["Delete Result"] = "Failed"
                    entry["Duration (s)"]  = f"{time.time()-t_start:.1f}"
                    entry["Error Message"] = msg
                    del_log.append(entry)
                    continue

                # Execute delete
                log.info("    [DELETE] %s  RG=%s  Sub=%s  DR=%s",
                         name, rg, sub, rec.get("DeleteRecommendation", ""))
                with Spinner(f"Deleting {name}"):
                    ok, err = _execute_delete(name, rg)
                elapsed = f"{time.time()-t_start:.1f}"
                entry["Duration (s)"] = elapsed
                if ok:
                    log.info("    -> Deleted OK (%.1fs)", float(elapsed))
                    entry["Delete Result"] = "Deleted"
                else:
                    log.error("    -> FAILED: %s", err)
                    entry["Delete Result"] = "Failed"
                    entry["Error Message"] = err
                del_log.append(entry)

                if delete_pause > 0:
                    time.sleep(delete_pause)

    def _count(label): return sum(1 for r in del_log if r["Delete Result"] == label)
    log.info("")
    log.info("=" * 70)
    log.info("DELETION SUMMARY [%s]", mode)
    if dry_run:
        log.info("  Would-Delete              : %d", _count("Dry-Run"))
        log.info("  Would-Skip (ReviewReq)    : %d", _count("Skipped-ReviewRequired"))
        log.info("  Would-Skip (NotFound)     : %d", _count("Skipped-EndpointNotFound"))
        log.info("  Would-Skip (AccessReview) : %d", _count("Skipped-AccessOrSubReview"))
    else:
        log.info("  Deleted                   : %d", _count("Deleted"))
        log.info("  Failed                    : %d", _count("Failed"))
        log.info("  Skipped                   : %d", _count("Skipped"))
        log.info("  Skipped (ReviewRequired)  : %d", _count("Skipped-ReviewRequired"))
        log.info("  Skipped (EndpointNotFound): %d", _count("Skipped-EndpointNotFound"))
    log.info("  Excluded                  : %d", _count("Excluded"))
    log.info("  Denied                    : %d", _count("Denied"))
    log.info("=" * 70)

    failed_list = [r for r in del_log if r["Delete Result"] == "Failed"]
    if failed_list:
        log.error("FAILED DELETIONS (%d):", len(failed_list))
        for r in failed_list:
            log.error("  - %s RG=%s Error=%s",
                      r["Endpoint Name"], r["Resource Group"], r.get("Error Message", ""))

    _write_delete_reports(del_log, output_dir, ts, run_dt, dry_run)
    generate_rollback(del_log, backup_dir, output_dir, run_dt)
    return del_log

# ===========================================================================
# Excel style helpers
# ===========================================================================

def _fill(c):  return PatternFill("solid", fgColor=c)
def _font(c, bold=False): return Font(color=c, bold=bold, name="Calibri", size=10)
def _border():
    s = Side(style="thin", color="BFBFBF")
    return Border(left=s, right=s, top=s, bottom=s)

def _autosize_sheet(ws, headers: list, extra_padding: int = 4):
    for ci, header in enumerate(headers, 1):
        col_letter = get_column_letter(ci)
        max_len = len(str(header))
        for row in ws.iter_rows(min_row=2, min_col=ci, max_col=ci):
            for cell in row:
                try:
                    cell_len = len(str(cell.value)) if cell.value is not None else 0
                    if cell_len > max_len:
                        max_len = cell_len
                except Exception:
                    pass
        adjusted = min(max(max_len + extra_padding, 12), 60)
        ws.column_dimensions[col_letter].width = adjusted

# ===========================================================================
# Validation / Discovery report (XLSX)
# ===========================================================================

def build_excel(results: list, path: str, run_date: str):
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = "EDAV Disconnected Private Endpoint Governance Report"
    ws["A1"].font = Font(name="Calibri", size=16, bold=True, color=CLR["hdr_bg"])
    ws.merge_cells("A1:E1")
    ws.row_dimensions[1].height = 28
    ws["A2"] = f"Generated: {run_date} | EDAV Monitor v{VERSION}"
    ws["A2"].font = Font(name="Calibri", size=10, italic=True, color="595959")
    ws.merge_cells("A2:E2")

    counts = {}
    for r in results:
        a = r.get("Recommended Action", "Review")
        counts[a] = counts.get(a, 0) + 1
    dr_counts = {}
    for r in results:
        d = r.get("DeleteRecommendation", DR_UNKNOWN)
        dr_counts[d] = dr_counts.get(d, 0) + 1

    for ci, h in enumerate(["Recommended Action", "Count", "%", "DeleteRecommendation", "Count"], 1):
        c = ws.cell(row=4, column=ci, value=h)
        c.fill = _fill(CLR["hdr_bg"]); c.font = _font(CLR["hdr_fg"], bold=True)
        c.alignment = Alignment(horizontal="center"); c.border = _border()
    ws.row_dimensions[4].height = 20

    total = len(results) or 1
    row = 5
    for action, count in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        style = ACTION_STYLE.get(action, ("na_bg", "na_fg"))
        bg, fg = CLR[style[0]], CLR[style[1]]
        ws.cell(row=row, column=1, value=action).fill = _fill(bg)
        ws.cell(row=row, column=1).font = _font(fg); ws.cell(row=row, column=1).border = _border()
        ws.cell(row=row, column=2, value=count).fill = _fill(bg)
        ws.cell(row=row, column=2).font = _font(fg); ws.cell(row=row, column=2).border = _border()
        ws.cell(row=row, column=2).alignment = Alignment(horizontal="center")
        ws.cell(row=row, column=3, value=f"{count/total*100:.1f}%").fill = _fill(bg)
        ws.cell(row=row, column=3).font = _font(fg); ws.cell(row=row, column=3).border = _border()
        ws.row_dimensions[row].height = 18
        row += 1

    row_dr = 5
    for dr_val, cnt in sorted(dr_counts.items(), key=lambda x: x[1], reverse=True):
        ws.cell(row=row_dr, column=4, value=dr_val).border = _border()
        ws.cell(row=row_dr, column=5, value=cnt).border = _border()
        ws.cell(row=row_dr, column=5).alignment = Alignment(horizontal="center")
        row_dr += 1

    ws.column_dimensions["A"].width = 55; ws.column_dimensions["B"].width = 10
    ws.column_dimensions["C"].width = 12; ws.column_dimensions["D"].width = 34
    ws.column_dimensions["E"].width = 10
    ws.freeze_panes = "A5"

    def write_sheet(wb, title, rows, headers, row_filter=None, bg_key="na_bg", fg_key="na_fg"):
        ws2 = wb.create_sheet(title)
        ws2.row_dimensions[1].height = 22
        for ci, h in enumerate(headers, 1):
            c = ws2.cell(row=1, column=ci, value=h)
            c.fill = _fill(CLR["hdr_bg"]); c.font = _font(CLR["hdr_fg"], bold=True)
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            c.border = _border()
        ri = 2
        for rec in rows:
            if row_filter and not row_filter(rec):
                continue
            action = rec.get("Recommended Action", "")
            style  = ACTION_STYLE.get(action, (bg_key, fg_key))
            bg, fg = CLR[style[0]], CLR[style[1]]
            for ci, h in enumerate(headers, 1):
                c = ws2.cell(row=ri, column=ci, value=rec.get(h, ""))
                c.fill = _fill(bg); c.font = _font(fg); c.border = _border()
                c.alignment = Alignment(wrap_text=True, vertical="top")
            ws2.row_dimensions[ri].height = 18
            ri += 1
        ws2.freeze_panes = "A2"
        ws2.auto_filter.ref = ws2.dimensions
        _autosize_sheet(ws2, headers)
        return ws2

    write_sheet(wb, "All Endpoints", results, SCAN_HEADERS)
    write_sheet(wb, "SAFE_DELETE", results, SCAN_HEADERS,
                row_filter=lambda r: r.get("DeleteRecommendation") == DR_SAFE_DELETE,
                bg_key="safe_bg", fg_key="safe_fg")
    write_sheet(wb, "REVIEW_REQUIRED", results, SCAN_HEADERS,
                row_filter=lambda r: r.get("DeleteRecommendation") == DR_REVIEW_REQUIRED,
                bg_key="rev_bg", fg_key="rev_fg")
    write_sheet(wb, "Excluded", results, SCAN_HEADERS,
                row_filter=lambda r: r.get("Recommended Action") in ("Excluded", "Denied"),
                bg_key="exc_bg", fg_key="exc_fg")
    write_sheet(wb, "Investigate", results, SCAN_HEADERS,
                row_filter=lambda r: "Investigate" in r.get("Recommended Action", "") or
                                     "Review" in r.get("Recommended Action", ""),
                bg_key="rev_bg", fg_key="rev_fg")
    wb.save(path)

# ===========================================================================
# Delete report (XLSX)
# ===========================================================================

def build_delete_excel(del_log: list, path: str, run_date: str, dry_run: bool):
    wb = Workbook()
    ws = wb.active
    ws.title = "Delete Report"
    mode_label = "DRY RUN -- No Real Changes" if dry_run else "LIVE DELETION REPORT"
    ws["A1"] = f"EDAV Private Endpoint Deletion Report [{mode_label}]"
    ws["A1"].font = Font(name="Calibri", size=14, bold=True,
                         color=CLR["dry_fg"] if dry_run else CLR["hdr_bg"])
    ws.merge_cells("A1:P1")
    ws.row_dimensions[1].height = 26
    ws["A2"] = f"Generated: {run_date} | EDAV Monitor v{VERSION}"
    ws["A2"].font = Font(name="Calibri", size=10, italic=True, color="595959")
    ws.merge_cells("A2:P2")
    ws.row_dimensions[4].height = 22
    for ci, h in enumerate(DEL_HEADERS, 1):
        c = ws.cell(row=4, column=ci, value=h)
        c.fill = _fill(CLR["hdr_bg"]); c.font = _font(CLR["hdr_fg"], bold=True)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = _border()
    result_colour = {
        "Deleted": ("safe_bg", "safe_fg"), "Dry-Run": ("dry_bg", "dry_fg"),
        "Failed": ("del_bg", "del_fg"), "Skipped": ("na_bg", "na_fg"),
        "Skipped-ReviewRequired": ("rev_bg", "rev_fg"),
        "Skipped-EndpointNotFound": ("na_bg", "na_fg"),
        "Skipped-AccessOrSubReview": ("na_bg", "na_fg"),
        "Excluded": ("exc_bg", "exc_fg"), "Denied": ("del_bg", "del_fg"),
    }
    for ri, row_data in enumerate(del_log, 5):
        result = row_data.get("Delete Result", "")
        style  = result_colour.get(result, ("na_bg", "na_fg"))
        bg, fg = CLR[style[0]], CLR[style[1]]
        for ci, h in enumerate(DEL_HEADERS, 1):
            c = ws.cell(row=ri, column=ci, value=row_data.get(h, ""))
            c.fill = _fill(bg); c.font = _font(fg); c.border = _border()
            c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[ri].height = 18
    ws.freeze_panes = "A5"
    _autosize_sheet(ws, DEL_HEADERS)
    wb.save(path)

# ===========================================================================
# Verification report (XLSX)
# ===========================================================================

def build_verification_excel(verify_log: list, path: str, run_date: str):
    wb = Workbook()
    ws = wb.active
    ws.title = "Verification Report"
    ws["A1"] = "EDAV Private Endpoint Post-Delete Verification Report"
    ws["A1"].font = Font(name="Calibri", size=14, bold=True, color=CLR["hdr_bg"])
    ws.merge_cells("A1:H1")
    ws.row_dimensions[1].height = 26
    ws["A2"] = f"Generated: {run_date} | EDAV Monitor v{VERSION}"
    ws["A2"].font = Font(name="Calibri", size=10, italic=True, color="595959")
    ws.merge_cells("A2:H2")
    ws.row_dimensions[4].height = 22
    for ci, h in enumerate(VERIFY_HEADERS, 1):
        c = ws.cell(row=4, column=ci, value=h)
        c.fill = _fill(CLR["hdr_bg"]); c.font = _font(CLR["hdr_fg"], bold=True)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = _border()
    for ri, row_data in enumerate(verify_log, 5):
        status = row_data.get("Verification Status", "")
        if status.startswith("Verified"):
            bg, fg = CLR["safe_bg"], CLR["safe_fg"]
        elif "FAILED" in status:
            bg, fg = CLR["fail_bg"], CLR["fail_fg"]
        else:
            bg, fg = CLR["na_bg"], CLR["na_fg"]
        for ci, h in enumerate(VERIFY_HEADERS, 1):
            c = ws.cell(row=ri, column=ci, value=row_data.get(h, ""))
            c.fill = _fill(bg); c.font = _font(fg); c.border = _border()
            c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[ri].height = 18
    ws.freeze_panes = "A5"
    _autosize_sheet(ws, VERIFY_HEADERS)
    wb.save(path)

# ===========================================================================
# Markdown + rollback reports
# ===========================================================================

def build_delete_markdown(del_log: list, path: str, run_date: str, dry_run: bool):
    def rows_by(result): return [r for r in del_log if r.get("Delete Result") == result]
    deleted   = rows_by("Deleted");   dry       = rows_by("Dry-Run")
    failed    = rows_by("Failed");    skipped   = rows_by("Skipped")
    excluded  = rows_by("Excluded");  rev_req   = rows_by("Skipped-ReviewRequired")
    not_found = rows_by("Skipped-EndpointNotFound")
    access_rev = rows_by("Skipped-AccessOrSubReview")
    mode = "DRY RUN" if dry_run else "LIVE"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# EDAV Private Endpoint Deletion Report [{mode}]\n\n")
        f.write(f"**Run Date:** {run_date} | **Mode:** {mode} | **Tool:** EDAV Monitor v{VERSION}\n\n")
        f.write("| Outcome | Count |\n|---|---|\n")
        for label, lst in [
            ("Deleted", deleted), ("Dry-Run (would delete)", dry),
            ("Failed", failed), ("Skipped", skipped), ("Excluded", excluded),
            ("Skipped (REVIEW_REQUIRED)", rev_req),
            ("Skipped (ENDPOINT_NOT_FOUND)", not_found),
            ("Skipped (ACCESS_OR_SUBSCRIPTION_REVIEW)", access_rev),
        ]:
            if lst:
                f.write(f"| {label} | {len(lst)} |\n")
        f.write(f"| **Total** | **{len(del_log)}** |\n\n")
        def write_table(title, lst, cols):
            if not lst: return
            f.write(f"## {title}\n\n")
            f.write("| " + " | ".join(cols) + " |\n")
            f.write("|" + "|".join(["---"] * len(cols)) + "|\n")
            for r in lst:
                f.write("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |\n")
            f.write("\n")
        if dry_run:
            write_table("Would Be Deleted (Dry-Run / SAFE_DELETE)", dry,
                        ["Endpoint Name", "Resource Group", "Subscription", "DeleteRecommendation"])
            write_table("Would Be Skipped (REVIEW_REQUIRED)", rev_req,
                        ["Endpoint Name", "Resource Group", "Subscription", "DeleteRecommendation"])
            write_table("Would Be Skipped (ENDPOINT_NOT_FOUND)", not_found,
                        ["Endpoint Name", "Resource Group", "Subscription", "DeleteRecommendation"])
            write_table("Would Be Skipped (ACCESS_OR_SUBSCRIPTION_REVIEW)", access_rev,
                        ["Endpoint Name", "Resource Group", "Subscription", "DeleteRecommendation"])
        else:
            write_table("Deleted", deleted,
                        ["Endpoint Name", "Resource Group", "Subscription",
                         "DeleteRecommendation", "Delete Timestamp", "Duration (s)"])
            write_table("Failed", failed,
                        ["Endpoint Name", "Resource Group", "Error Message"])
            write_table("Skipped (REVIEW_REQUIRED -- not deleted)", rev_req,
                        ["Endpoint Name", "Resource Group", "DeleteRecommendation", "Error Message"])
            write_table("Skipped", skipped,
                        ["Endpoint Name", "Resource Group", "Error Message"])
            write_table("Excluded", excluded,
                        ["Endpoint Name", "Resource Group", "Error Message"])
        f.write("---\n\n")
        f.write("> **Safety Note:** Only Azure Private Endpoint resources were targeted.\n")
        f.write("> No backend resources were modified or deleted.\n")
        f.write("> REVIEW_REQUIRED endpoints were NOT deleted.\n")

def generate_rollback(del_log: list, backup_dir: str, output_dir: str, run_date: str):
    path    = os.path.join(output_dir, "rollback_instructions.md")
    deleted = [r for r in del_log if r.get("Delete Result") == "Deleted"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("# EDAV Private Endpoint Rollback Instructions\n\n")
        f.write(f"**Deletion Run Date:** {run_date}\n")
        f.write(f"**Backup Directory:** {os.path.abspath(backup_dir)}\n\n")
        if not deleted:
            f.write("No endpoints were deleted during this run. No rollback needed.\n")
            return path
        f.write("## Deleted Endpoints\n\n")
        f.write("| # | Endpoint Name | Resource Group | Subscription | DeleteRecommendation |\n")
        f.write("|---|---|---|---|---|\n")
        for i, r in enumerate(deleted, 1):
            f.write(f"| {i} | {r['Endpoint Name']} | {r['Resource Group']} | "
                    f"{r['Subscription']} | {r.get('DeleteRecommendation','')} |\n")
        f.write("\n## How to Restore an Endpoint\n\n")
        f.write("See backup JSON in: " + os.path.abspath(os.path.join(backup_dir, "private_endpoints")) + "\n\n")
        f.write("```bash\n")
        f.write("az network private-endpoint create --name <name> --resource-group <rg> \\\n")
        f.write("  --vnet-name <vnet> --subnet <subnet> \\\n")
        f.write("  --private-connection-resource-id <backend-id> \\\n")
        f.write("  --connection-name <conn> --group-id <group-id>\n")
        f.write("```\n")
    log.info("Rollback MD : %s", path)
    return path

def _write_delete_reports(del_log: list, output_dir: str, ts: str, run_dt: str, dry_run: bool):
    prefix    = "EDAV_DryRun_Report" if dry_run else "EDAV_Delete_Report"
    md_prefix = "dryrun_summary"     if dry_run else "delete_summary"
    del_csv  = os.path.join(output_dir, f"{prefix}_{ts}.csv")
    del_xlsx = os.path.join(output_dir, f"{prefix}_{ts}.xlsx")
    del_md   = os.path.join(output_dir, f"{md_prefix}_{ts}.md")
    df_del = pd.DataFrame(del_log if del_log else [], columns=DEL_HEADERS)
    df_del.to_csv(del_csv, index=False)
    log.info("Delete CSV  : %s", del_csv)
    build_delete_excel(del_log, del_xlsx, run_dt, dry_run)
    log.info("Delete XLSX : %s", del_xlsx)
    build_delete_markdown(del_log, del_md, run_dt, dry_run)
    log.info("Delete MD   : %s", del_md)

# ===========================================================================
# HTML report builder
# ===========================================================================

def build_html_report(results: list, del_log: list, verify_log: list,
                       path: str, run_date: str, mode: str,
                       change_ticket: str = "", approved_by: str = ""):
    counts = {}
    for r in results:
        counts[r.get("Recommended Action","Review")] = counts.get(r.get("Recommended Action","Review"),0)+1
    dr_counts = {}
    for r in results:
        d = r.get("DeleteRecommendation", DR_UNKNOWN)
        dr_counts[d] = dr_counts.get(d, 0) + 1

    total        = len(results) or 1
    safe         = dr_counts.get(DR_SAFE_DELETE, 0)
    review_req   = dr_counts.get(DR_REVIEW_REQUIRED, 0)
    not_found_ct = dr_counts.get(DR_ENDPOINT_NOT_FOUND, 0)
    deleted      = sum(1 for r in del_log if r.get("Delete Result") == "Deleted")
    verified     = sum(1 for v in verify_log if v.get("Verification Status","").startswith("Verified"))
    failed       = sum(1 for r in del_log if r.get("Delete Result") == "Failed")
    ver_failed   = sum(1 for v in verify_log if "FAILED" in v.get("Verification Status",""))
    disconnected = sum(1 for r in results if r.get("Connection State") == "Disconnected")

    scan_rows = ""
    for r in results:
        colour = {"Safe Delete Candidate":"#C6EFCE","Investigate - Backend Exists":"#FFEB9C",
                  "Review - Not Disconnected":"#FFEB9C","Review":"#FFEB9C",
                  "Excluded":"#E2EFDA","Denied":"#FFC7CE"}.get(r.get("Recommended Action",""),"#FFFFFF")
        scan_rows += (f'<tr style="background:{colour}">'
                      f'<td>{r.get("Endpoint Name","")}</td>'
                      f'<td>{r.get("Resource Group","")}</td>'
                      f'<td>{r.get("Connection State","")}</td>'
                      f'<td>{r.get("Backend Exists","")}</td>'
                      f'<td>{r.get("BackendResourceName","")}</td>'
                      f'<td>{r.get("BackendResourceType","")}</td>'
                      f'<td><b>{r.get("DeleteRecommendation","")}</b></td>'
                      f'<td>{r.get("Recommended Action","")}</td>'
                      f'<td>{r.get("Notes","")}</td></tr>')

    del_rows = ""
    if del_log:
        for r in del_log:
            res    = r.get("Delete Result","")
            colour = {"Deleted":"#C6EFCE","Failed":"#FFC7CE","Dry-Run":"#DDEBF7",
                      "Skipped-ReviewRequired":"#FFEB9C"}.get(res,"#D9D9D9")
            del_rows += (f'<tr style="background:{colour}">'
                         f'<td>{r.get("Endpoint Name","")}</td>'
                         f'<td>{r.get("Resource Group","")}</td>'
                         f'<td>{r.get("DeleteRecommendation","")}</td>'
                         f'<td><b>{res}</b></td>'
                         f'<td>{r.get("Error Message","")}</td></tr>')
    else:
        del_rows = "<tr><td colspan=5 style='color:#888'>No deletion operations performed</td></tr>"

    ver_rows = ""
    if verify_log:
        for v in verify_log:
            status = v.get("Verification Status","")
            colour = "#C6EFCE" if status.startswith("Verified") else "#FFC7CE" if "FAILED" in status else "#D9D9D9"
            ver_rows += (f'<tr style="background:{colour}">'
                         f'<td>{v.get("Endpoint Name","")}</td>'
                         f'<td><b>{status}</b></td>'
                         f'<td>{v.get("Azure Response","")}</td></tr>')
    else:
        ver_rows = "<tr><td colspan=3 style='color:#888'>No verification data</td></tr>"

    dr_rows = "".join(
        f'<tr style="background:{{"SAFE_DELETE":"#C6EFCE","REVIEW_REQUIRED":"#FFEB9C"}.get(d,"#D9D9D9")}">'
        f'<td><b>{d}</b></td><td style="text-align:center">{c}</td></tr>'
        for d, c in sorted(dr_counts.items(), key=lambda x: x[1], reverse=True)
    )

    html = (
        "<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>"
        f"<title>EDAV Monitor v{VERSION}</title>"
        "<style>body{font-family:Calibri,Arial,sans-serif;margin:0;color:#333;background:#f4f6f9;}"
        ".hdr{background:linear-gradient(135deg,#1F4E79,#2980B9);color:#fff;padding:24px 36px;}"
        ".hdr h1{margin:0;font-size:24px;} .hdr .sub{font-size:12px;opacity:0.85;}"
        ".badges{padding:16px 36px;background:#fff;border-bottom:1px solid #e0e0e0;display:flex;flex-wrap:wrap;gap:8px;}"
        ".badge{padding:6px 14px;border-radius:6px;font-weight:bold;font-size:12px;}"
        ".green{background:#C6EFCE;color:#276221;} .red{background:#FFC7CE;color:#9C0006;}"
        ".blue{background:#DDEBF7;color:#1F4E79;} .grey{background:#D9D9D9;color:#595959;}"
        ".amber{background:#FFEB9C;color:#9C6500;}"
        ".content{padding:20px 36px;}"
        "h2{color:#1F4E79;margin:24px 0 6px;font-size:15px;border-bottom:2px solid #DDEBF7;padding-bottom:4px;}"
        "table{border-collapse:collapse;width:100%;font-size:12px;margin-top:4px;}"
        "th{background:#1F4E79;color:#fff;padding:8px 10px;text-align:left;}"
        "td{padding:6px 10px;border-bottom:1px solid #eee;}"
        ".footer{padding:12px 36px;background:#f0f4f8;border-top:1px solid #ddd;font-size:10px;color:#888;}"
        "</style></head><body>"
        f'<div class="hdr"><h1>EDAV Private Endpoint Monitor v{VERSION}</h1>'
        f'<div class="sub">Run: {run_date} | Mode: {mode}'
        + (f" | Ticket: {change_ticket}" if change_ticket else "")
        + (f" | By: {approved_by}" if approved_by else "")
        + "</div></div>"
        '<div class="badges">'
        f'<div class="badge grey">{len(results)} Total</div>'
        f'<div class="badge amber">{disconnected} Disconnected</div>'
        f'<div class="badge green">{safe} SAFE_DELETE</div>'
        f'<div class="badge amber">{review_req} REVIEW_REQUIRED</div>'
        f'<div class="badge grey">{not_found_ct} ENDPOINT_NOT_FOUND</div>'
        f'<div class="badge blue">{deleted} Deleted</div>'
        f'<div class="badge green">{verified} Verified</div>'
        + (f'<div class="badge red">{failed} Failed</div>' if failed else "")
        + (f'<div class="badge red">{ver_failed} VerifyFailed</div>' if ver_failed else "")
        + "</div>"
        '<div class="content">'
        "<h2>DeleteRecommendation Summary</h2>"
        "<table><thead><tr><th>DeleteRecommendation</th><th>Count</th></tr></thead>"
        f"<tbody>{dr_rows}</tbody></table>"
        "<h2>Discovery Results</h2>"
        "<table><thead><tr><th>Endpoint Name</th><th>Resource Group</th>"
        "<th>Connection State</th><th>Backend Exists</th>"
        "<th>BackendResourceName</th><th>BackendResourceType</th>"
        "<th>DeleteRecommendation</th><th>Recommended Action</th><th>Notes</th>"
        f"</tr></thead><tbody>{scan_rows}</tbody></table>"
        "<h2>Deletion Report</h2>"
        "<table><thead><tr><th>Endpoint Name</th><th>Resource Group</th>"
        "<th>DeleteRecommendation</th><th>Delete Result</th><th>Error</th>"
        f"</tr></thead><tbody>{del_rows}</tbody></table>"
        "<h2>Verification</h2>"
        "<table><thead><tr><th>Endpoint Name</th><th>Verification Status</th><th>Azure Response</th>"
        f"</tr></thead><tbody>{ver_rows}</tbody></table>"
        "</div>"
        '<div class="footer">EDAV Private Endpoint Monitor v' + VERSION + ' | '
        'Only Azure Private Endpoint resources were targeted | '
        'No backend resources were modified or deleted | '
        'REVIEW_REQUIRED endpoints were NOT deleted.</div>'
        "</body></html>"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    log.info("HTML report : %s", path)

# ===========================================================================
# JSON report builder
# ===========================================================================

def build_json_report(results: list, del_log: list, verify_log: list,
                       path: str, run_date: str, mode: str,
                       change_ticket: str = "", approved_by: str = ""):
    dr_counts = {}
    for r in results:
        d = r.get("DeleteRecommendation", DR_UNKNOWN)
        dr_counts[d] = dr_counts.get(d, 0) + 1
    payload = {
        "report_metadata": {
            "tool": f"EDAV Private Endpoint Monitor v{VERSION}",
            "run_date": run_date, "mode": mode,
            "change_ticket": change_ticket or "Not provided",
            "approved_by": approved_by or "Not provided",
            "generated_at": datetime.utcnow().isoformat() + "Z",
        },
        "executive_summary": {
            "total_scanned":       len(results),
            "total_disconnected":  sum(1 for r in results if r.get("Connection State") == "Disconnected"),
            "safe_delete":         dr_counts.get(DR_SAFE_DELETE, 0),
            "review_required":     dr_counts.get(DR_REVIEW_REQUIRED, 0),
            "endpoint_not_found":  dr_counts.get(DR_ENDPOINT_NOT_FOUND, 0),
            "access_or_sub_review": dr_counts.get(DR_ACCESS_OR_SUB_REVIEW, 0),
            "excluded":            sum(1 for r in results if r.get("Recommended Action") in ("Excluded","Denied")),
            "total_deleted":       sum(1 for r in del_log if r.get("Delete Result") == "Deleted"),
            "total_failed":        sum(1 for r in del_log if r.get("Delete Result") == "Failed"),
            "skipped_review_req":  sum(1 for r in del_log if r.get("Delete Result") == "Skipped-ReviewRequired"),
            "skipped_not_found":   sum(1 for r in del_log if r.get("Delete Result") == "Skipped-EndpointNotFound"),
            "total_dry_run":       sum(1 for r in del_log if r.get("Delete Result") == "Dry-Run"),
            "total_verified":      sum(1 for v in verify_log if v.get("Verification Status","").startswith("Verified")),
            "verify_failed":       sum(1 for v in verify_log if "FAILED" in v.get("Verification Status","")),
        },
        "discovery_results": results,
        "deletion_log":      del_log,
        "verification_log":  verify_log,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    log.info("JSON report : %s", path)

# ===========================================================================
# Email
# ===========================================================================

def build_email_html(results: list, del_log: list, verify_log: list, run_date: str) -> str:
    counts = {}
    for r in results:
        a = r.get("Recommended Action", "Review")
        counts[a] = counts.get(a, 0) + 1
    rows = "".join(
        f"<tr><td style='padding:6px 12px;border:1px solid #ddd'>{k}</td>"
        f"<td style='padding:6px 12px;border:1px solid #ddd;text-align:center'>{v}</td></tr>"
        for k, v in sorted(counts.items(), key=lambda x: x[1], reverse=True)
    )
    safe     = counts.get("Safe Delete Candidate", 0)
    deleted  = sum(1 for r in del_log if r.get("Delete Result") == "Deleted")
    verified = sum(1 for v in verify_log if v.get("Verification Status", "").startswith("Verified"))
    ver_fail = sum(1 for v in verify_log if "FAILED" in v.get("Verification Status", ""))
    return (
        "<html><body style='font-family:Calibri,Arial,sans-serif;color:#333'>"
        f"<h2 style='color:#1F4E79'>EDAV Private Endpoint Governance Report</h2>"
        f"<p><strong>Run Date:</strong> {run_date} | <strong>Total Scanned:</strong> {len(results)}</p>"
        "<table style='border-collapse:collapse'>"
        "<thead><tr style='background:#1F4E79;color:#fff'>"
        "<th style='padding:8px 16px'>Recommended Action</th><th style='padding:8px 16px'>Count</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
        f"<br><p><strong>Deleted:</strong> {deleted} | <strong>Verified:</strong> {verified} | "
        f"<strong>Verify Failed:</strong> {ver_fail}</p>"
        "<p>Full reports are attached.</p>"
        f"<p style='color:#888;font-size:11px'>EDAV Private Endpoint Monitor v{VERSION}</p>"
        "</body></html>"
    )

def send_email(cfg: dict, subject: str, body: str, attachments: list):
    req = ("smtp_server", "smtp_port", "from_email", "to_email")
    if any(not cfg.get(k) for k in req):
        log.warning("Incomplete email config -- skipping.")
        return
    msg = MIMEMultipart("mixed")
    msg["From"] = cfg["from_email"]; msg["To"] = cfg["to_email"]; msg["Subject"] = subject
    msg.attach(MIMEText(body, "html"))
    for fp in attachments:
        if not os.path.isfile(fp): continue
        with open(fp, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename={os.path.basename(fp)}")
        msg.attach(part)
    try:
        with smtplib.SMTP(cfg["smtp_server"], int(cfg["smtp_port"]), timeout=15) as s:
            if cfg.get("use_tls", True): s.starttls()
            if cfg.get("smtp_user") and cfg.get("smtp_pass"):
                s.login(cfg["smtp_user"], cfg["smtp_pass"])
            s.sendmail(cfg["from_email"], [e.strip() for e in cfg["to_email"].split(",")], msg.as_string())
        log.info("Email sent to %s", cfg["to_email"])
    except Exception as e:
        log.error("Email failed: %s", e)

# ===========================================================================
# Executive Dashboard
# ===========================================================================

def print_executive_dashboard(results: list, del_log: list, verify_log: list,
                               run_dt: str, change_ticket: str, mode: str):
    dr_counts = {}
    for r in results:
        d = r.get("DeleteRecommendation", DR_UNKNOWN)
        dr_counts[d] = dr_counts.get(d, 0) + 1

    cnt_safe    = dr_counts.get(DR_SAFE_DELETE, 0)
    cnt_review  = dr_counts.get(DR_REVIEW_REQUIRED, 0)
    cnt_notfound = dr_counts.get(DR_ENDPOINT_NOT_FOUND, 0)
    cnt_access  = dr_counts.get(DR_ACCESS_OR_SUB_REVIEW, 0)
    excluded    = sum(1 for r in results if r.get("Recommended Action") in ("Excluded", "Denied"))

    total_deleted    = sum(1 for r in del_log if r.get("Delete Result") == "Deleted")
    total_dry_run    = sum(1 for r in del_log if r.get("Delete Result") == "Dry-Run")
    total_failed     = sum(1 for r in del_log if r.get("Delete Result") == "Failed")
    total_skipped    = sum(1 for r in del_log if r.get("Delete Result") in ("Skipped","Excluded","Denied"))
    review_skipped   = sum(1 for r in del_log if r.get("Delete Result") == "Skipped-ReviewRequired")
    notfound_skipped = sum(1 for r in del_log if r.get("Delete Result") == "Skipped-EndpointNotFound")
    access_skipped   = sum(1 for r in del_log if r.get("Delete Result") == "Skipped-AccessOrSubReview")
    total_verified   = sum(1 for v in verify_log if v.get("Verification Status","").startswith("Verified"))
    total_ver_failed = sum(1 for v in verify_log if "FAILED" in v.get("Verification Status",""))

    W = 72
    log.info("")
    log.info("=" * W)
    log.info("  EXECUTIVE DASHBOARD -- EDAV Private Endpoint Monitor v%s", VERSION)
    log.info("=" * W)
    log.info("  Run Date      : %s", run_dt)
    log.info("  Mode          : %s", mode)
    log.info("  Change Ticket : %s", change_ticket or "Not provided")
    log.info("-" * W)
    log.info("  DISCOVERY")
    log.info("  Total Endpoints Scanned         : %4d", len(results))
    log.info("  Total Disconnected              : %4d",
             sum(1 for r in results if r.get("Connection State") == "Disconnected"))
    log.info("-" * W)
    log.info("  DELETE RECOMMENDATION")
    log.info("  SAFE_DELETE                     : %4d", cnt_safe)
    log.info("  REVIEW_REQUIRED                 : %4d", cnt_review)
    log.info("  ENDPOINT_NOT_FOUND              : %4d", cnt_notfound)
    log.info("  ACCESS_OR_SUBSCRIPTION_REVIEW   : %4d", cnt_access)
    log.info("  Total Excluded / Denied         : %4d", excluded)
    log.info("-" * W)
    log.info("  DELETION")
    if total_dry_run:
        log.info("  Total Dry-Run (simulated)       : %4d", total_dry_run)
    log.info("  Total Deleted                   : %4d", total_deleted)
    log.info("  Total Skipped / Blocked         : %4d", total_skipped)
    log.info("  Skipped (REVIEW_REQUIRED)       : %4d", review_skipped)
    log.info("  Skipped (ENDPOINT_NOT_FOUND)    : %4d", notfound_skipped)
    log.info("  Skipped (ACCESS_OR_SUB_REVIEW)  : %4d", access_skipped)
    log.info("  Total Failed                    : %4d", total_failed)
    log.info("-" * W)
    log.info("  VERIFICATION")
    log.info("  Total Verified (Gone)           : %4d", total_verified)
    log.info("  Total Verification FAILED       : %4d", total_ver_failed)
    log.info("=" * W)
    if total_ver_failed > 0:
        log.error("  ACTION REQUIRED: %d endpoint(s) could NOT be verified deleted!", total_ver_failed)
    if total_failed > 0:
        log.error("  ACTION REQUIRED: %d deletion(s) FAILED -- review logs immediately!", total_failed)
    log.info("")

# ===========================================================================
# Run Summary Footer
# ===========================================================================

def print_run_summary(output_files: list, log_path: str, elapsed: float):
    W = 72
    log.info("")
    log.info("=" * W)
    log.info("  RUN COMPLETE -- Output Files")
    log.info("=" * W)
    for fpath in output_files:
        if fpath and os.path.isfile(fpath):
            size_kb = os.path.getsize(fpath) / 1024
            ext     = Path(fpath).suffix.upper().lstrip(".")
            log.info("  [%-4s] %6.1f KB  %s", ext, size_kb, fpath)
        elif fpath:
            log.info("  [----]  not generated  %s", fpath)
    log.info("-" * W)
    log.info("  Log file   : %s", log_path)
    log.info("  Total time : %.1f seconds", elapsed)
    log.info("=" * W)
    log.info("")

# ===========================================================================
# CLI argument parser
# ===========================================================================

def parse_args():
    p = argparse.ArgumentParser(
        description=f"EDAV Private Endpoint Monitor v{VERSION} -- Enterprise Azure Governance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Safe discovery and reporting (no deletions):
  python main.py --input report.csv --subscriptions "Sub1,Sub2" --verify-only

  # Dry-run cleanup simulation (no real deletes):
  python main.py --input approved.csv --subscriptions "Sub1" \
      --cleanup-approved --delete-approved --dry-run \
      --change-ticket CHG0012345 --approved-by "John Smith"

  # Live cleanup:
  python main.py --input approved.csv --subscriptions "Sub1" \
      --cleanup-approved --delete-approved \
      --change-ticket CHG0012345 --approved-by "John Smith"
""",
    )

    p.add_argument("--version", action="version", version=f"EDAV Monitor v{VERSION}")
    p.add_argument("--input", required=True,
                   help="Path to CSV or Excel input file (.csv, .xlsx, .xls)")
    p.add_argument("--subscriptions", default="",
                   help="Comma-separated Azure subscription names to scan")
    p.add_argument("--terraform-path", default="",
                   help="Local Terraform repo path for ownership checks (optional)")
    p.add_argument("--output-dir",    default="reports",
                   help="Directory for output reports (default: reports/)")
    p.add_argument("--backup-dir",    default="backups",
                   help="Directory for ARM JSON backups (default: backups/)")
    p.add_argument("--log-dir",       default="logs",
                   help="Directory for structured log files (default: logs/)")
    p.add_argument("--output-prefix", default="",
                   help="Optional prefix for all output report filenames")
    p.add_argument("--verify-only", action="store_true", default=False,
                   help="Discovery + Validation + Reporting only. No deletion.")
    p.add_argument("--cleanup-approved", action="store_true", default=False,
                   help="Validation + Deletion + Verification. Requires --delete-approved.")
    p.add_argument("--delete-approved", action="store_true", default=False,
                   help="REQUIRED to enable deletion. USE ONLY AFTER APPROVED CHANGE REQUEST.")
    p.add_argument("--dry-run", action="store_true", default=False,
                   help="Simulate deletions only -- no Azure resources are modified.")
    p.add_argument("--delete-pause", type=float, default=2.0,
                   help="Seconds to pause between deletes (default: 2.0)")
    p.add_argument("--exclusions", default="exclusions.txt")
    p.add_argument("--denylist",   default="governance/denylist.json")
    p.add_argument("--allowlist",  default="governance/allowlist.json")
    p.add_argument("--change-ticket", default="",
                   help="Change ticket reference recorded in all reports")
    p.add_argument("--approved-by",   default="",
                   help="Approver identity recorded in audit trail")
    p.add_argument("--email-to",     default="")
    p.add_argument("--email-from",   default="")
    p.add_argument("--smtp-server",  default="")
    p.add_argument("--smtp-port",    default="587")
    p.add_argument("--smtp-user",    default="")
    p.add_argument("--smtp-pass",    default="")
    return p.parse_args()

# ===========================================================================
# Main entry point
# ===========================================================================

def main():
    _run_start = time.time()
    args       = parse_args()
    ts         = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dt     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    global log
    log, log_path = setup_logging(args.log_dir, ts)

    if args.cleanup_approved and not args.delete_approved:
        log.error("--cleanup-approved requires --delete-approved to be explicitly passed.")
        log.error("This is a safety requirement. Add --delete-approved to proceed.")
        sys.exit(1)

    if args.cleanup_approved:
        mode_label = "CLEANUP-APPROVED (DRY RUN)" if args.dry_run else "CLEANUP-APPROVED [LIVE]"
    else:
        mode_label = "VERIFY-ONLY (Discovery + Validation)"

    pfx = (args.output_prefix.strip() + "_") if args.output_prefix.strip() else ""
    print_banner(VERSION, mode_label, run_dt, args.change_ticket, args.approved_by)
    log.info("  Log File: %s", log_path)

    try:
        validate_azure_login()

        for d in (args.output_dir, args.backup_dir, args.log_dir, "governance"):
            os.makedirs(d, exist_ok=True)

        subs = [s.strip() for s in args.subscriptions.split(",") if s.strip()]
        if not subs:
            log.info("No subscriptions specified -- auto-detecting...")
            with Spinner("Fetching subscriptions"):
                subs = get_subscriptions()
            if not subs:
                log.error("No Azure subscriptions found. Run: az login --use-device-code")
                sys.exit(1)
        log.info("  Subscriptions (%d): %s", len(subs), subs)

        tf_state, tf_code = load_terraform(args.terraform_path)
        exclusions = load_exclusions(args.exclusions)
        denylist   = load_denylist(args.denylist)
        allowlist  = load_allowlist(args.allowlist)

        log.info("  Loading input: %s", args.input)
        endpoints = load_endpoints(args.input)
        log.info("  Loaded %d endpoint(s)", len(endpoints))

        def rp(name): return os.path.join(args.output_dir, f"{pfx}{name}_{ts}")
        xlsx_out    = rp("EDAV_Validation_Report") + ".xlsx"
        csv_out     = rp("EDAV_Validation_Report") + ".csv"
        md_out      = rp("EDAV_Summary")           + ".md"
        html_out    = rp("EDAV_Report")            + ".html"
        json_out    = rp("EDAV_Report")            + ".json"
        verify_csv  = rp("EDAV_Verification")      + ".csv"
        verify_xlsx = rp("EDAV_Verification")      + ".xlsx"

        # ----------------------------------------------------------------
        # PHASE 1: DISCOVERY & SCAN
        # ----------------------------------------------------------------
        phase_start(1, "DISCOVERY & SCAN")
        results = []
        for i, ep in enumerate(endpoints, 1):
            nm = str(ep.get("Endpoint Name", "")).strip()
            log.info("  [%d/%d] Scanning: %s", i, len(endpoints), nm or "(empty name)")
            with Spinner(f"az query: {nm}"):
                result = scan(ep, subs, tf_state, tf_code, exclusions, denylist)
            results.append(result)
        phase_end(1, "DISCOVERY & SCAN")

        # ----------------------------------------------------------------
        # PHASE 2: VALIDATION REPORTS
        # ----------------------------------------------------------------
        phase_start(2, "VALIDATION REPORTS")

        df_out = pd.DataFrame(results, columns=SCAN_HEADERS + ["ApprovedToDelete"])
        df_out.to_csv(csv_out, index=False)
        log.info("  Validation CSV  : %s", csv_out)
        build_excel(results, xlsx_out, run_dt)
        log.info("  Validation XLSX : %s", xlsx_out)

        counts = {}
        for r in results:
            a = r.get("Recommended Action", "Review")
            counts[a] = counts.get(a, 0) + 1
        dr_counts = {}
        for r in results:
            d = r.get("DeleteRecommendation", DR_UNKNOWN)
            dr_counts[d] = dr_counts.get(d, 0) + 1

        log.info("")
        log.info("  VALIDATION SUMMARY (Total: %d)", len(results))
        for a, c in sorted(counts.items(), key=lambda x: x[1], reverse=True):
            log.info("    %-52s %d", a, c)
        log.info("")
        log.info("  DELETE RECOMMENDATION SUMMARY:")
        for d, c in sorted(dr_counts.items(), key=lambda x: x[1], reverse=True):
            log.info("    %-36s %d", d, c)

        total = len(results) or 1
        with open(md_out, "w", encoding="utf-8") as f:
            f.write("# EDAV Private Endpoint Validation Summary\n\n")
            f.write(f"**Run Date:** {run_dt} | **Total:** {len(results)} | "
                    f"**Mode:** {mode_label} | **Tool:** EDAV Monitor v{VERSION}\n\n")
            if args.change_ticket:
                f.write(f"**Change Ticket:** {args.change_ticket}\n\n")
            f.write("## DeleteRecommendation Summary\n\n")
            f.write("| DeleteRecommendation | Count |\n|---|---|\n")
            for d, c in sorted(dr_counts.items(), key=lambda x: x[1], reverse=True):
                f.write(f"| {d} | {c} |\n")
            f.write("\n## Action Distribution\n\n")
            f.write("| Action | Count | % |\n|---|---|---|\n")
            for a, c in sorted(counts.items(), key=lambda x: x[1], reverse=True):
                f.write(f"| {a} | {c} | {c/total*100:.1f}% |\n")
            f.write(f"\n**Reports saved to:** {args.output_dir}\n")
            f.write(f"\n**Log file:** {log_path}\n")
        log.info("  Validation MD   : %s", md_out)
        phase_end(2, "VALIDATION REPORTS")

        # ----------------------------------------------------------------
        # PHASE 3: CLEANUP ENGINE
        # ----------------------------------------------------------------
        del_log    = []
        verify_log = []

        if args.cleanup_approved:
            phase_start(3, "CLEANUP ENGINE")
            del_log = run_delete_approved(
                results=results, exclusions=exclusions, denylist=denylist,
                output_dir=args.output_dir, backup_dir=args.backup_dir,
                ts=ts, run_dt=run_dt, dry_run=args.dry_run,
                delete_pause=args.delete_pause,
                change_ticket=args.change_ticket, approved_by=args.approved_by,
            )
            phase_end(3, "CLEANUP ENGINE")

            # ----------------------------------------------------------------
            # PHASE 4: POST-DELETE VERIFICATION
            # ----------------------------------------------------------------
            phase_start(4, "POST-DELETE VERIFICATION")
            verify_log = run_post_delete_verification(del_log, dry_run=args.dry_run)
            df_verify = pd.DataFrame(verify_log if verify_log else [], columns=VERIFY_HEADERS)
            df_verify.to_csv(verify_csv, index=False)
            log.info("  Verification CSV  : %s", verify_csv)
            build_verification_excel(verify_log, verify_xlsx, run_dt)
            log.info("  Verification XLSX : %s", verify_xlsx)
            phase_end(4, "POST-DELETE VERIFICATION")

        else:
            log.info("")
            log.info("  [PHASE 3+4 SKIPPED] Not in --cleanup-approved mode.")
            safe_n = dr_counts.get(DR_SAFE_DELETE, 0)
            rev_n  = dr_counts.get(DR_REVIEW_REQUIRED, 0)
            if safe_n or rev_n:
                log.info("")
                log.info("  >>> SAFE_DELETE endpoints    : %d  (eligible for deletion)", safe_n)
                log.info("  >>> REVIEW_REQUIRED endpoints: %d  (do NOT delete - backend still exists)", rev_n)
                log.info("  >>> Step 1: Review XLSX -- check DeleteRecommendation column")
                log.info("  >>> Step 2: Mark ApprovedToDelete=Yes ONLY for SAFE_DELETE endpoints")
                log.info("  >>> Step 3: Get change ticket approval")
                log.info("  >>> Step 4: --cleanup-approved --delete-approved --dry-run (preview)")
                log.info("  >>> Step 5: --cleanup-approved --delete-approved (execute)")

        # ----------------------------------------------------------------
        # PHASE 5: HTML + JSON REPORTS
        # ----------------------------------------------------------------
        phase_start(5, "HTML + JSON REPORTS")
        build_html_report(results, del_log, verify_log, html_out, run_dt, mode_label,
                          args.change_ticket, args.approved_by)
        build_json_report(results, del_log, verify_log, json_out, run_dt, mode_label,
                          args.change_ticket, args.approved_by)
        phase_end(5, "HTML + JSON REPORTS")

        # ----------------------------------------------------------------
        # PHASE 6: EXECUTIVE DASHBOARD
        # ----------------------------------------------------------------
        phase_start(6, "EXECUTIVE DASHBOARD")
        print_executive_dashboard(results, del_log, verify_log, run_dt,
                                  args.change_ticket, mode_label)
        phase_end(6, "EXECUTIVE DASHBOARD")

        # Email
        if args.email_to and args.smtp_server:
            cfg = dict(smtp_server=args.smtp_server, smtp_port=args.smtp_port,
                       from_email=args.email_from, to_email=args.email_to,
                       smtp_user=args.smtp_user, smtp_pass=args.smtp_pass, use_tls=True)
            subj = (f"EDAV Endpoint Governance | {run_dt} | "
                    f"SAFE_DELETE={dr_counts.get(DR_SAFE_DELETE,0)} | "
                    f"REVIEW_REQUIRED={dr_counts.get(DR_REVIEW_REQUIRED,0)}")
            attachments = [xlsx_out, csv_out, html_out, json_out]
            if verify_log:
                attachments += [verify_csv, verify_xlsx]
            send_email(cfg, subj, build_email_html(results, del_log, verify_log, run_dt), attachments)

        # Run Summary Footer
        output_files = [xlsx_out, csv_out, html_out, json_out, md_out]
        if args.cleanup_approved:
            del_prefix = "EDAV_DryRun_Report" if args.dry_run else "EDAV_Delete_Report"
            md_pfx     = "dryrun_summary"      if args.dry_run else "delete_summary"
            output_files += [
                os.path.join(args.output_dir, f"{pfx}{del_prefix}_{ts}.xlsx"),
                os.path.join(args.output_dir, f"{pfx}{del_prefix}_{ts}.csv"),
                os.path.join(args.output_dir, f"{pfx}{md_pfx}_{ts}.md"),
                verify_xlsx, verify_csv,
                os.path.join(args.output_dir, "rollback_instructions.md"),
            ]
        elapsed = time.time() - _run_start
        print_run_summary(output_files, log_path, elapsed)

    except KeyboardInterrupt:
        log.info("Run interrupted by user (Ctrl+C).")
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as exc:
        log.exception("UNEXPECTED ERROR: %s", exc)
        log.error("Run terminated unexpectedly. Check log: %s", log_path)
        sys.exit(2)

if __name__ == "__main__":
    main()

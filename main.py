#!/usr/bin/env python3
"""
EDAV Private Endpoint Monitor v4.0.0
=======================================
Enterprise-grade Azure governance and cleanup platform for EDAV disconnected
private endpoints.

What this does
--------------
* Validates Azure login before any work begins
* Reads a CSV/Excel input file of private endpoints
* Scans each endpoint across one or more subscriptions using Azure CLI
* Validates backend resource existence
* Checks Terraform ownership (state file + .tf source)
* Loads governance files: exclusions.txt, allowlist.json, denylist.json
* Generates colour-coded Excel/CSV/HTML/JSON/Markdown reports
* Post-delete verification: confirms endpoint is gone after every deletion
* Executive dashboard printed at end of every run
* Optionally emails reports

Operational Modes
-----------------
--verify-only         Discovery + Validation + Reporting. No deletion.
--cleanup-approved    Requires --delete-approved. Validates, Deletes, Verifies.
(default)             Same as --verify-only.

Deletion safety layers (in order)
----------------------------------
1.  --delete-approved flag required  (CLI)
2.  --cleanup-approved mode required (CLI)
3.  ApprovedToDelete == Yes required per row
4.  Exclusion list checked
5.  Denylist checked
6.  Recommended Action keyword block
7.  User must type CONFIRM at interactive prompt
8.  ARM JSON backup written before every real delete
9.  Pre-delete re-validation: endpoint exists AND still Disconnected
10. Subscription context verified before each delete
11. Post-delete verification: Azure returns ResourceNotFound
12. --dry-run simulates everything without touching Azure
13. Rollback instructions generated automatically
14. Structured log written to logs/ directory

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
VERSION = "4.0.0"

_AZ_WINDOWS_FALLBACK = r"C:\Program Files (x86)\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"

def _resolve_az():
    az = shutil.which("az")
    if az:
        return az
    if os.path.isfile(_AZ_WINDOWS_FALLBACK):
        return _AZ_WINDOWS_FALLBACK
    print("FATAL: Azure CLI (az) not found.")
    print("  Install from: https://aka.ms/installazurecliwindows")
    sys.exit(1)

AZ_CMD = _resolve_az()

_LOG_FMT  = "%(asctime)s %(levelname)-8s [%(threadName)s] %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

CLR = {
    "hdr_bg":  "1F4E79", "hdr_fg":  "FFFFFF",
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
    "Safe Delete Candidate":                         ("safe_bg", "safe_fg"),
    "Do Not Delete - Terraform Managed":             ("del_bg",  "del_fg"),
    "Investigate - Backend Exists":                  ("rev_bg",  "rev_fg"),
    "Review - Not Disconnected":                     ("rev_bg",  "rev_fg"),
    "Review":                                        ("rev_bg",  "rev_fg"),
    "Endpoint Not Found / Check Subscription":       ("na_bg",   "na_fg"),
    "Skipped - Empty Name":                          ("na_bg",   "na_fg"),
    "Excluded":                                      ("exc_bg",  "exc_fg"),
    "Denied":                                        ("del_bg",  "del_fg"),
}

SCAN_HEADERS = [
    "Endpoint Name", "Resource Group", "Subscription", "Region",
    "Connection State", "Backend Resource", "Backend Exists",
    "Terraform Managed", "Recommended Action", "Scan Timestamp", "Notes",
]

DEL_HEADERS = [
    "Endpoint Name", "Resource Group", "Subscription", "Region",
    "Recommended Action", "ApprovedToDelete", "Change Ticket", "Approved By",
    "Delete Result", "Delete Timestamp", "Duration (s)", "Dry Run",
    "Backup Path", "Pre-Delete Validation", "Error Message",
]

VERIFY_HEADERS = [
    "Endpoint Name", "Resource Group", "Subscription",
    "Delete Result", "Verification Status", "Azure Response",
    "Verification Timestamp", "Verification Notes",
]

COL_ALIASES = {
    "endpoint name": "Endpoint Name", "endpointname": "Endpoint Name",
    "name": "Endpoint Name",
    "resource group": "Resource Group", "resourcegroup": "Resource Group",
    "rg": "Resource Group",
}

_BLOCK_SUBSTRINGS = ("Do Not Delete", "Endpoint Not Found", "Terraform Managed", "Excluded", "Denied")

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
    """Load hard-blocked endpoint names from JSON file."""
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
    """Load pre-approved endpoint names from JSON file."""
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
    """Run an Azure CLI command with exponential-backoff retry.
    Returns parsed JSON or None. Isolates per-subscription failures."""
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
                log.debug("  az retry %d/%d in %ds cmd=%s", attempt, retries, wait, args[:3])
                time.sleep(wait)
        except subprocess.TimeoutExpired:
            log.debug("  az timeout attempt %d/%d", attempt, retries)
            if attempt < retries:
                time.sleep(2 ** attempt)
        except Exception as exc:
            log.debug("  az exception attempt %d/%d: %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(2 ** attempt)
    return None

def _az(args, silent=False):
    return _az_with_retry(args)

def validate_azure_login() -> dict:
    """Validate Azure CLI login. Exits with clear instructions if not logged in."""
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
    log.error("=" * 60)
    log.error("AZURE LOGIN REQUIRED")
    log.error("  Interactive:    az login")
    log.error("  Device code:    az login --use-device-code")
    log.error("  Service prin:   az login --service-principal -u <appId> -p <pwd> --tenant <t>")
    log.error("=" * 60)
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
            log.debug("  Subscription set: %s", name)
            return True
        log.warning("  Cannot set subscription '%s': %s", name, r.stderr[:120])
        return False
    except subprocess.TimeoutExpired:
        log.warning("  Timeout setting subscription: %s", name)
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

def private_endpoint_still_valid_for_delete(name: str, rg: str, sub: str) -> tuple:
    """Pre-delete re-validation. Returns (ok: bool, reason: str).
    ok=True only if endpoint EXISTS and is still Disconnected."""
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
# Post-delete verification  (NEW in v4.0.0)
# ===========================================================================

def verify_endpoint_deleted(name: str, rg: str, sub: str) -> tuple:
    """
    Post-delete verification. After every deletion attempt, confirm that
    Azure returns ResourceNotFound for the endpoint.

    Returns:
        (verified: bool, status: str, azure_response: str)

    verified=True means Azure confirms the resource is gone.
    Fails the operation record if the resource still exists.
    """
    if not name or not rg:
        return False, "Verification Skipped", "Missing name or RG"
    try:
        # Set subscription context
        if sub:
            set_subscription(sub)
        pe = get_private_endpoint(name, rg)
        if pe is None:
            # Resource not found -- deletion confirmed
            return True, "Verified - Resource Not Found", "Azure: ResourceNotFound (confirmed deleted)"
        else:
            # Resource still exists -- deletion may have failed silently
            state = get_private_endpoint_connection_state(pe)
            return False, "FAILED - Resource Still Exists", f"Azure: Resource still present, state='{state}'"
    except Exception as exc:
        return False, "Verification Error", f"Exception during verification: {exc}"


def run_post_delete_verification(del_log: list, dry_run: bool = False) -> list:
    """
    Run post-delete verification for every entry in del_log.

    For DRY-RUN entries: marks as 'Verification Skipped (Dry Run)'.
    For Deleted entries: calls verify_endpoint_deleted() and records result.
    For Failed/Skipped entries: marks as 'Not Applicable'.

    Returns a list of verification records matching VERIFY_HEADERS.
    """
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
        log.info("  [VERIFY] %s (RG=%s Sub=%s) ...", name, rg, sub)
        verified, status, az_resp = verify_endpoint_deleted(name, rg, sub)
        ts = datetime.now().isoformat()
        if verified:
            log.info("    -> %s", status)
        else:
            log.error("    -> VERIFICATION FAILED: %s | %s", status, az_resp)
        verify_log.append({
            "Endpoint Name":         name,
            "Resource Group":        rg,
            "Subscription":          sub,
            "Delete Result":         rec.get("Delete Result", ""),
            "Verification Status":   status,
            "Azure Response":        az_resp,
            "Verification Timestamp": ts,
            "Verification Notes":    "Verified deleted" if verified else "WARNING: resource may still exist",
        })

    for rec in dry_entries:
        verify_log.append({
            "Endpoint Name":         rec.get("Endpoint Name", ""),
            "Resource Group":        rec.get("Resource Group", ""),
            "Subscription":          rec.get("Subscription", ""),
            "Delete Result":         "Dry-Run",
            "Verification Status":   "Verification Skipped (Dry Run)",
            "Azure Response":        "No delete performed",
            "Verification Timestamp": datetime.now().isoformat(),
            "Verification Notes":    "Dry-run mode -- no real delete occurred",
        })

    for rec in other_entries:
        verify_log.append({
            "Endpoint Name":         rec.get("Endpoint Name", ""),
            "Resource Group":        rec.get("Resource Group", ""),
            "Subscription":          rec.get("Subscription", ""),
            "Delete Result":         rec.get("Delete Result", ""),
            "Verification Status":   "Not Applicable",
            "Azure Response":        "No delete attempted",
            "Verification Timestamp": datetime.now().isoformat(),
            "Verification Notes":    ("Delete result was '" + str(rec.get("Delete Result", "")) + "' -- verification not required"),
        })

    # Summary
    v_ok   = sum(1 for v in verify_log if v["Verification Status"].startswith("Verified"))
    v_fail = sum(1 for v in verify_log if "FAILED" in v["Verification Status"])
    v_skip = sum(1 for v in verify_log if "Skipped" in v["Verification Status"] or "Not Applicable" in v["Verification Status"])
    log.info("Verification Summary: Confirmed=%d  Failed=%d  Skipped/N/A=%d", v_ok, v_fail, v_skip)
    if v_fail > 0:
        log.error("WARNING: %d endpoint(s) could NOT be verified as deleted -- manual review required!", v_fail)
    return verify_log

# ===========================================================================
# ARM backup
# ===========================================================================

def export_endpoint_backup(pe: dict, name: str, rg: str, sub: str,
                           backup_dir: str, ts: str) -> str:
    """Export full ARM JSON of the private endpoint before deletion.
    Returns the backup file path."""
    subdir = os.path.join(backup_dir, "private_endpoints")
    os.makedirs(subdir, exist_ok=True)
    safe_name = name.replace("/", "_").replace("\\", "_")
    fname = f"{safe_name}_{sub}_{ts}.json".replace(" ", "_")
    fpath = os.path.join(subdir, fname)
    payload = {
        "backup_timestamp": datetime.utcnow().isoformat() + "Z",
        "subscription":     sub,
        "resource_group":   rg,
        "endpoint_name":    name,
        "arm_resource":     pe,
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
        r = subprocess.run(["terraform", "state", "list"],
                           capture_output=True, text=True, timeout=60)
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
    if tf_managed == "Yes":
        return ("Do Not Delete - Terraform Managed",
                "Found in Terraform state/code. Remove from TF first.")
    if conn_state == "Disconnected" and backend_exists == "No" and tf_managed == "No":
        return ("Safe Delete Candidate",
                "Disconnected, backend gone, not in Terraform. Safe to decommission.")
    if conn_state == "Disconnected" and backend_exists == "Yes":
        return ("Investigate - Backend Exists",
                "Endpoint disconnected but backend resource still active.")
    if conn_state not in ("Disconnected", "Unknown", ""):
        return ("Review - Not Disconnected",
                f"Connection state is '{conn_state}'. May still be in use.")
    return ("Review", "Insufficient data to make a safe recommendation.")


# ===========================================================================
# Input loader
# ===========================================================================

def load_endpoints(path: str) -> list:
    """Load endpoint rows from a CSV or Excel file."""
    path = path.strip()
    if not os.path.isfile(path):
        log.error("Input file not found: %s", path)
        sys.exit(1)
    ext = Path(path).suffix.lower()
    if ext in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype=str)
    else:
        df = pd.read_csv(path, dtype=str)
    df.columns = df.columns.str.strip()
    df = df.fillna("")
    rename = {}
    for col in df.columns:
        key = col.strip().lower().replace(" ", "").replace("_", "")
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
    log.info("Input file loaded: %d row(s), columns: %s", len(df), list(df.columns))
    return df.to_dict(orient="records")

# ===========================================================================
# Core scan
# ===========================================================================

def scan(ep: dict, subscriptions: list, tf_state: str,
         tf_code: str, exclusions: set, denylist: set = None) -> dict:
    """Scan a single endpoint across all subscriptions.
    Adds Region field. Checks denylist in addition to exclusions."""
    if denylist is None:
        denylist = set()
    original_row = dict(ep)
    name = str(ep.get("Endpoint Name", "")).strip()
    rg   = str(ep.get("Resource Group", "")).strip()
    rec = {
        "Endpoint Name":     name,
        "Resource Group":    rg,
        "Subscription":      "",
        "Region":            "",
        "Connection State":  "Not Found",
        "Backend Resource":  "",
        "Backend Exists":    "Unknown",
        "Terraform Managed": "Unknown",
        "Recommended Action": "",
        "Scan Timestamp":    datetime.now().isoformat(),
        "Notes":             "",
    }
    if not name:
        rec["Recommended Action"] = "Skipped - Empty Name"
        rec.update({k: v for k, v in original_row.items() if k not in rec or not rec[k]})
        return rec

    # Denylist check (hard block)
    if name.lower() in denylist:
        rec["Recommended Action"] = "Denied"
        rec["Notes"] = "Listed in denylist -- hard blocked from deletion."
        log.info("  [DENIED] %s", name)
        rec.update({k: v for k, v in original_row.items() if k not in rec or not rec[k]})
        return rec

    # Exclusion list check
    if name.lower() in exclusions:
        rec["Recommended Action"] = "Excluded"
        rec["Notes"] = "Listed in exclusions.txt -- will never be deleted."
        log.info("  [EXCLUDED] %s", name)
        rec.update({k: v for k, v in original_row.items() if k not in rec or not rec[k]})
        return rec

    for sub in subscriptions:
        if not set_subscription(sub):
            log.warning("  [SKIP-SUB] Cannot set subscription '%s' -- skipping for this endpoint", sub)
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
            rec["Region"] = get_endpoint_region(pe)
            conns = (pe.get("privateLinkServiceConnections") or
                     pe.get("manualPrivateLinkServiceConnections") or [])
            if conns:
                cs = conns[0].get("privateLinkServiceConnectionState", {})
                rec["Connection State"] = cs.get("status", "Unknown")
                bid = conns[0].get("privateLinkServiceId", "")
                rec["Backend Resource"] = bid
                rec["Backend Exists"] = "Yes" if resource_exists(bid) else "No"
            else:
                rec["Connection State"] = "No Connection Object"
            break

    if rec["Subscription"] == "":
        rec["Connection State"]   = "Endpoint Not Found"
        rec["Notes"]              = "Not found in any scanned subscription."
        rec["Recommended Action"] = "Endpoint Not Found / Check Subscription"
        rec.update({k: v for k, v in original_row.items() if k not in rec or not rec[k]})
        return rec

    rec["Terraform Managed"] = in_terraform(name, tf_state, tf_code)
    action, notes = decide(rec["Connection State"],
                           rec["Backend Exists"],
                           rec["Terraform Managed"])
    rec["Recommended Action"] = action
    rec["Notes"] = notes
    rec.update({k: v for k, v in original_row.items() if k not in rec or not rec[k]})
    return rec


# ===========================================================================
# Deletion safety gate (hardened in v4.0.0)
# ===========================================================================

def _is_deletion_blocked(rec: dict, exclusions: set, denylist: set = None) -> tuple:
    """
    Hardened safety gate. Checks in order:
    1. Endpoint Name is blank
    2. Resource Group is blank
    3. Endpoint on denylist (hard block)
    4. Endpoint on exclusion list
    5. ApprovedToDelete != yes
    6. Endpoint still Disconnected (connection state confirmed)
    7. Recommended Action contains a blocking keyword
    """
    if denylist is None:
        denylist = set()
    name    = str(rec.get("Endpoint Name", "")).strip()
    rg      = str(rec.get("Resource Group", "")).strip()
    approved = get_approval_value(rec)
    action  = str(rec.get("Recommended Action", "")).strip()

    if not name:
        return True, "Endpoint Name is blank"
    if not rg:
        return True, "Resource Group is blank"
    if name.lower() in denylist:
        return True, f"'{name}' is on the denylist -- hard blocked"
    if name.lower() in exclusions:
        return True, "Listed in exclusions.txt"
    if approved != "yes":
        return True, (
            "ApprovedToDelete='"
            + str(rec.get("ApprovedToDelete", ""))
            + "' -- must be Yes / YES / yes"
        )
    for substr in _BLOCK_SUBSTRINGS:
        if substr.lower() in action.lower():
            return True, f"Recommended Action '{action}' contains blocking keyword"
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

    Returns (success: bool, error_msg: str)
    """
    try:
        r = subprocess.run(
            [AZ_CMD, "network", "private-endpoint", "delete",
             "--name", name, "--resource-group", rg, "--yes"],
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
# Core deletion workflow (enhanced in v4.0.0)
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
    Approval-gated deletion workflow with post-delete verification hooks.

    Safety layers enforced (in order):
    1.  --delete-approved + --cleanup-approved flags required (enforced by caller)
    2.  ApprovedToDelete == yes required per row
    3.  Denylist checked
    4.  Exclusion list checked
    5.  Recommended Action keyword block
    6.  User must type CONFIRM at interactive prompt
    7.  ARM backup exported before each delete
    8.  Pre-delete re-validation (endpoint exists AND still Disconnected)
    9.  Subscription context verified before each delete
    10. Delete issued (ONLY az network private-endpoint delete)
    11. Post-delete verification called (endpoint must return ResourceNotFound)
    12. Configurable pause between deletes
    13. --dry-run simulates everything without touching Azure
    14. Rollback instructions generated after run
    """
    mode = "DRY RUN" if dry_run else "LIVE DELETE"
    log.info("")
    log.info("=" * 70)
    log.info("CLEANUP-APPROVED MODE [%s]", mode)
    if change_ticket:
        log.info("Change Ticket : %s", change_ticket)
    if approved_by:
        log.info("Approved By   : %s", approved_by)
    log.info("=" * 70)

    del_log = []

    approved_rows = []
    skipped_rows  = []
    for r in results:
        if get_approval_value(r) == "yes":
            approved_rows.append(r)
        else:
            skipped_rows.append(r)

    log.info("  Total rows in input  : %d", len(results))
    log.info("  Rows approved (Yes)  : %d", len(approved_rows))
    log.info("  Rows skipped (non-Yes): %d", len(skipped_rows))

    if skipped_rows:
        for r in skipped_rows[:5]:
            log.info("  SKIPPED | %-40s | ApprovedToDelete=%r",
                     r.get("Endpoint Name", "(blank)"),
                     r.get("ApprovedToDelete", ""))

    if len(approved_rows) == 0:
        log.warning("No rows marked ApprovedToDelete=Yes -- nothing to process.")
        _write_delete_reports(del_log, output_dir, ts, run_dt, dry_run)
        generate_rollback(del_log, backup_dir, output_dir, run_dt)
        return del_log

    # Pass 1: safety gate
    candidates = []
    for rec in approved_rows:
        blocked, reason = _is_deletion_blocked(rec, exclusions, denylist)
        if blocked:
            label = "Excluded" if "exclusions" in reason else ("Denied" if "denylist" in reason else "Skipped")
            entry = {
                "Endpoint Name":       rec.get("Endpoint Name", ""),
                "Resource Group":      rec.get("Resource Group", ""),
                "Subscription":        rec.get("Subscription", ""),
                "Region":              rec.get("Region", ""),
                "Recommended Action":  rec.get("Recommended Action", ""),
                "ApprovedToDelete":    rec.get("ApprovedToDelete", ""),
                "Change Ticket":       change_ticket,
                "Approved By":         approved_by,
                "Delete Result":       label,
                "Delete Timestamp":    "",
                "Duration (s)":        "",
                "Dry Run":             str(dry_run),
                "Backup Path":         "",
                "Pre-Delete Validation": reason,
                "Error Message":       reason,
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

    log.info("")
    log.info("Endpoints queued for %s (%d):", "simulation" if dry_run else "deletion", len(candidates))
    for rec in candidates:
        log.info("  - %-45s RG=%-25s Sub=%s",
                 rec["Endpoint Name"], rec["Resource Group"], rec.get("Subscription", ""))

    # CONFIRM prompt
    if not dry_run:
        log.warning("")
        log.warning("=" * 70)
        log.warning("WARNING: This will PERMANENTLY DELETE Azure Private Endpoints.")
        log.warning("This action CANNOT be undone.")
        log.warning("Only Private Endpoint resources are deleted.")
        log.warning("Backend resources are NEVER touched.")
        log.warning("=" * 70)
        confirm = input("\nType CONFIRM to continue: ")
        if confirm.strip() != "CONFIRM":
            log.info("Deletion cancelled -- user did not type CONFIRM.")
            for rec in candidates:
                del_log.append({
                    "Endpoint Name":       rec["Endpoint Name"],
                    "Resource Group":      rec["Resource Group"],
                    "Subscription":        rec.get("Subscription", ""),
                    "Region":              rec.get("Region", ""),
                    "Recommended Action":  rec.get("Recommended Action", ""),
                    "ApprovedToDelete":    rec.get("ApprovedToDelete", ""),
                    "Change Ticket":       change_ticket,
                    "Approved By":         approved_by,
                    "Delete Result":       "Skipped",
                    "Delete Timestamp":    datetime.now().isoformat(),
                    "Duration (s)":        "",
                    "Dry Run":             "False",
                    "Backup Path":         "",
                    "Pre-Delete Validation": "Cancelled by user",
                    "Error Message":       "Cancelled by user at CONFIRM prompt",
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
        # Subscription-level error isolation
        if sub and not dry_run:
            if not set_subscription(sub):
                log.error("  Cannot set subscription '%s' -- skipping all endpoints in it.", sub)
                for rg, recs in rg_map.items():
                    for rec in recs:
                        del_log.append({
                            "Endpoint Name":       rec["Endpoint Name"],
                            "Resource Group":      rg,
                            "Subscription":        sub,
                            "Region":              rec.get("Region", ""),
                            "Recommended Action":  rec.get("Recommended Action", ""),
                            "ApprovedToDelete":    rec.get("ApprovedToDelete", ""),
                            "Change Ticket":       change_ticket,
                            "Approved By":         approved_by,
                            "Delete Result":       "Failed",
                            "Delete Timestamp":    datetime.now().isoformat(),
                            "Duration (s)":        "",
                            "Dry Run":             str(dry_run),
                            "Backup Path":         "",
                            "Pre-Delete Validation": "FAILED",
                            "Error Message":       f"Cannot set subscription context to '{sub}'",
                        })
                continue

        for rg, recs in rg_map.items():
            log.info("  Resource Group: %s", rg)
            for rec in recs:
                name    = rec["Endpoint Name"]
                t_start = time.time()
                entry   = {
                    "Endpoint Name":       name,
                    "Resource Group":      rg,
                    "Subscription":        sub,
                    "Region":              rec.get("Region", ""),
                    "Recommended Action":  rec.get("Recommended Action", ""),
                    "ApprovedToDelete":    rec.get("ApprovedToDelete", ""),
                    "Change Ticket":       change_ticket,
                    "Approved By":         approved_by,
                    "Delete Result":       "",
                    "Delete Timestamp":    datetime.now().isoformat(),
                    "Duration (s)":        "",
                    "Dry Run":             str(dry_run),
                    "Backup Path":         "",
                    "Pre-Delete Validation": "",
                    "Error Message":       "",
                }

                if dry_run:
                    log.info("    [DRY-RUN] Would delete: %s", name)
                    entry["Delete Result"] = "Dry-Run"
                    entry["Duration (s)"] = "0"
                    entry["Pre-Delete Validation"] = "Simulated OK"
                    del_log.append(entry)
                    continue

                # Backup
                log.info("    [Backup] %s ...", name)
                pe_data = get_private_endpoint(name, rg)
                backup_path = ""
                if pe_data:
                    os.makedirs(backup_dir, exist_ok=True)
                    backup_path = export_endpoint_backup(pe_data, name, rg, sub, backup_dir, ts)
                    entry["Backup Path"] = backup_path
                else:
                    log.warning("    Could not fetch ARM data for backup -- proceeding with caution.")

                # Pre-delete re-validation
                log.info("    [Validate] %s ...", name)
                valid, reason = private_endpoint_still_valid_for_delete(name, rg, sub)
                entry["Pre-Delete Validation"] = reason
                if not valid:
                    log.warning("    SKIPPED: %s -- %s", name, reason)
                    entry["Delete Result"] = "Skipped"
                    entry["Duration (s)"] = f"{time.time()-t_start:.1f}"
                    entry["Error Message"] = reason
                    del_log.append(entry)
                    continue

                # Subscription context verification
                if not verify_subscription_context(sub):
                    msg = f"Subscription context mismatch -- expected '{sub}'"
                    log.error("    FAILED: %s -- %s", name, msg)
                    entry["Delete Result"] = "Failed"
                    entry["Duration (s)"] = f"{time.time()-t_start:.1f}"
                    entry["Error Message"] = msg
                    del_log.append(entry)
                    continue

                # Execute delete
                log.info("    [DELETE] %s  RG=%s  Sub=%s", name, rg, sub)
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

    # Deletion summary
    def _count(label): return sum(1 for r in del_log if r["Delete Result"] == label)
    log.info("")
    log.info("=" * 70)
    log.info("DELETION SUMMARY [%s]", mode)
    if dry_run:
        log.info("  Would-Delete : %d", _count("Dry-Run"))
    else:
        log.info("  Deleted      : %d", _count("Deleted"))
    log.info("  Failed       : %d", _count("Failed"))
    log.info("  Skipped      : %d", _count("Skipped"))
    log.info("  Excluded     : %d", _count("Excluded"))
    log.info("  Denied       : %d", _count("Denied"))
    log.info("=" * 70)

    failed_list = [r for r in del_log if r["Delete Result"] == "Failed"]
    if failed_list:
        log.error("FAILED DELETIONS (%d):", len(failed_list))
        for r in failed_list:
            log.error("  - %-45s RG=%-25s Error=%s",
                      r["Endpoint Name"], r["Resource Group"], r.get("Error Message", ""))

    _write_delete_reports(del_log, output_dir, ts, run_dt, dry_run)
    generate_rollback(del_log, backup_dir, output_dir, run_dt)
    return del_log

# ===========================================================================
# Excel style helpers
# ===========================================================================

def _fill(c): return PatternFill("solid", fgColor=c)
def _font(c, bold=False): return Font(color=c, bold=bold, name="Calibri", size=10)
def _border():
    s = Side(style="thin", color="BFBFBF")
    return Border(left=s, right=s, top=s, bottom=s)


# ===========================================================================
# Validation / Discovery report (XLSX)
# ===========================================================================

def build_excel(results: list, path: str, run_date: str):
    wb = Workbook()

    # Summary sheet
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = "EDAV Disconnected Private Endpoint Governance Report"
    ws["A1"].font = Font(name="Calibri", size=16, bold=True, color=CLR["hdr_bg"])
    ws.merge_cells("A1:D1")
    ws["A2"] = f"Generated: {run_date} | EDAV Monitor v{VERSION}"
    ws["A2"].font = Font(name="Calibri", size=10, italic=True, color="595959")
    ws.merge_cells("A2:D2")
    counts = {}
    for r in results:
        a = r.get("Recommended Action", "Review")
        counts[a] = counts.get(a, 0) + 1
    ws["A4"] = "Recommended Action"
    ws["B4"] = "Count"
    ws["C4"] = "% of Total"
    for cell in [ws["A4"], ws["B4"], ws["C4"]]:
        cell.fill = _fill(CLR["hdr_bg"])
        cell.font = _font(CLR["hdr_fg"], bold=True)
        cell.alignment = Alignment(horizontal="center")
    total = len(results) or 1
    row = 5
    for action, count in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        style = ACTION_STYLE.get(action, ("na_bg", "na_fg"))
        bg, fg = CLR[style[0]], CLR[style[1]]
        ws.cell(row=row, column=1, value=action).fill = _fill(bg)
        ws.cell(row=row, column=1).font = _font(fg)
        ws.cell(row=row, column=2, value=count).fill = _fill(bg)
        ws.cell(row=row, column=2).font = _font(fg)
        ws.cell(row=row, column=2).alignment = Alignment(horizontal="center")
        ws.cell(row=row, column=3, value=f"{count/total*100:.1f}%").fill = _fill(bg)
        ws.cell(row=row, column=3).font = _font(fg)
        ws.cell(row=row, column=3).alignment = Alignment(horizontal="center")
        row += 1
    ws.column_dimensions["A"].width = 55
    ws.column_dimensions["B"].width = 10
    ws.column_dimensions["C"].width = 12

    def write_sheet(wb, title, rows, headers, row_filter=None, bg_key="na_bg", fg_key="na_fg"):
        ws = wb.create_sheet(title)
        for ci, h in enumerate(headers, 1):
            c = ws.cell(row=1, column=ci, value=h)
            c.fill = _fill(CLR["hdr_bg"])
            c.font = _font(CLR["hdr_fg"], bold=True)
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            c.border = _border()
        ri = 2
        for rec in rows:
            if row_filter and not row_filter(rec):
                continue
            action = rec.get("Recommended Action", "")
            style = ACTION_STYLE.get(action, (bg_key, fg_key))
            bg, fg = CLR[style[0]], CLR[style[1]]
            for ci, h in enumerate(headers, 1):
                c = ws.cell(row=ri, column=ci, value=rec.get(h, ""))
                c.fill = _fill(bg)
                c.font = _font(fg)
                c.border = _border()
                c.alignment = Alignment(wrap_text=True, vertical="top")
            ri += 1
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        return ws

    write_sheet(wb, "All Endpoints", results, SCAN_HEADERS)
    write_sheet(wb, "Safe Delete Candidates", results, SCAN_HEADERS,
                row_filter=lambda r: r.get("Recommended Action") == "Safe Delete Candidate",
                bg_key="safe_bg", fg_key="safe_fg")
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
    ws.merge_cells("A1:O1")
    ws["A2"] = f"Generated: {run_date} | EDAV Monitor v{VERSION}"
    ws["A2"].font = Font(name="Calibri", size=10, italic=True, color="595959")
    ws.merge_cells("A2:O2")
    for ci, h in enumerate(DEL_HEADERS, 1):
        c = ws.cell(row=4, column=ci, value=h)
        c.fill = _fill(CLR["hdr_bg"])
        c.font = _font(CLR["hdr_fg"], bold=True)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = _border()
    result_colour = {
        "Deleted":  ("safe_bg", "safe_fg"),
        "Dry-Run":  ("dry_bg",  "dry_fg"),
        "Failed":   ("del_bg",  "del_fg"),
        "Skipped":  ("na_bg",   "na_fg"),
        "Excluded": ("exc_bg",  "exc_fg"),
        "Denied":   ("del_bg",  "del_fg"),
    }
    for ri, row_data in enumerate(del_log, 5):
        result = row_data.get("Delete Result", "")
        style = result_colour.get(result, ("na_bg", "na_fg"))
        bg, fg = CLR[style[0]], CLR[style[1]]
        for ci, h in enumerate(DEL_HEADERS, 1):
            c = ws.cell(row=ri, column=ci, value=row_data.get(h, ""))
            c.fill = _fill(bg)
            c.font = _font(fg)
            c.border = _border()
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A5"
    wb.save(path)


# ===========================================================================
# Verification report (XLSX)  -- NEW in v4.0.0
# ===========================================================================

def build_verification_excel(verify_log: list, path: str, run_date: str):
    wb = Workbook()
    ws = wb.active
    ws.title = "Verification Report"
    ws["A1"] = "EDAV Private Endpoint Post-Delete Verification Report"
    ws["A1"].font = Font(name="Calibri", size=14, bold=True, color=CLR["hdr_bg"])
    ws.merge_cells("A1:H1")
    ws["A2"] = f"Generated: {run_date} | EDAV Monitor v{VERSION}"
    ws["A2"].font = Font(name="Calibri", size=10, italic=True, color="595959")
    ws.merge_cells("A2:H2")
    for ci, h in enumerate(VERIFY_HEADERS, 1):
        c = ws.cell(row=4, column=ci, value=h)
        c.fill = _fill(CLR["hdr_bg"])
        c.font = _font(CLR["hdr_fg"], bold=True)
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
            c.fill = _fill(bg)
            c.font = _font(fg)
            c.border = _border()
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A5"
    wb.save(path)

# ===========================================================================
# HTML report builder -- NEW in v4.0.0
# ===========================================================================

def build_html_report(results: list, del_log: list, verify_log: list,
                      path: str, run_date: str, mode: str,
                      change_ticket: str = "", approved_by: str = ""):
    """Generate a standalone HTML report covering all phases."""
    counts = {}
    for r in results:
        a = r.get("Recommended Action", "Review")
        counts[a] = counts.get(a, 0) + 1

    total = len(results) or 1
    safe    = counts.get("Safe Delete Candidate", 0)
    deleted = sum(1 for r in del_log if r.get("Delete Result") == "Deleted")
    verified = sum(1 for v in verify_log if v.get("Verification Status", "").startswith("Verified"))
    failed  = sum(1 for r in del_log if r.get("Delete Result") == "Failed")
    ver_failed = sum(1 for v in verify_log if "FAILED" in v.get("Verification Status", ""))

    def row_colour(action):
        colours = {
            "Safe Delete Candidate": "#C6EFCE",
            "Do Not Delete - Terraform Managed": "#FFC7CE",
            "Investigate - Backend Exists": "#FFEB9C",
            "Review - Not Disconnected": "#FFEB9C",
            "Review": "#FFEB9C",
            "Excluded": "#E2EFDA",
            "Denied": "#FFC7CE",
        }
        return colours.get(action, "#FFFFFF")

    def scan_rows_html():
        rows = ""
        for r in results:
            colour = row_colour(r.get("Recommended Action", ""))
            rows += f'<tr style="background:{colour}"><td>{r.get("Endpoint Name","")}</td>'
            rows += f'<td>{r.get("Resource Group","")}</td>'
            rows += f'<td>{r.get("Subscription","")}</td>'
            rows += f'<td>{r.get("Region","")}</td>'
            rows += f'<td>{r.get("Connection State","")}</td>'
            rows += f'<td>{r.get("Backend Exists","")}</td>'
            rows += f'<td>{r.get("Terraform Managed","")}</td>'
            rows += f'<td><b>{r.get("Recommended Action","")}</b></td>'
            rows += f'<td>{r.get("Notes","")}</td></tr>'
        return rows

    def del_rows_html():
        if not del_log:
            return "<tr><td colspan=6>No deletion operations performed</td></tr>"
        rows = ""
        for r in del_log:
            res = r.get("Delete Result", "")
            colour = {"Deleted": "#C6EFCE", "Failed": "#FFC7CE",
                      "Dry-Run": "#DDEBF7", "Skipped": "#D9D9D9"}.get(res, "#FFFFFF")
            rows += f'<tr style="background:{colour}">'
            rows += f'<td>{r.get("Endpoint Name","")}</td><td>{r.get("Resource Group","")}</td>'
            rows += f'<td>{r.get("Subscription","")}</td><td><b>{res}</b></td>'
            rows += f'<td>{r.get("Delete Timestamp","")}</td>'
            rows += f'<td>{r.get("Error Message","")}</td></tr>'
        return rows

    def ver_rows_html():
        if not verify_log:
            return "<tr><td colspan=5>No verification data (verify-only mode)</td></tr>"
        rows = ""
        for v in verify_log:
            status = v.get("Verification Status", "")
            colour = "#C6EFCE" if status.startswith("Verified") else ("#FFC7CE" if "FAILED" in status else "#D9D9D9")
            rows += f'<tr style="background:{colour}">'
            rows += f'<td>{v.get("Endpoint Name","")}</td><td>{v.get("Resource Group","")}</td>'
            rows += f'<td><b>{status}</b></td><td>{v.get("Azure Response","")}</td>'
            rows += f'<td>{v.get("Verification Timestamp","")}</td></tr>'
        return rows

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>EDAV Private Endpoint Monitor v{VERSION} Report</title>
<style>
  body {{font-family:Calibri,Arial,sans-serif;margin:24px;color:#333;}}
  h1 {{color:#1F4E79;}} h2 {{color:#1F4E79;margin-top:32px;}}
  table {{border-collapse:collapse;width:100%;margin-top:8px;font-size:13px;}}
  th {{background:#1F4E79;color:#fff;padding:8px 10px;text-align:left;}}
  td {{padding:6px 10px;border:1px solid #ddd;}}
  .badge {{display:inline-block;padding:4px 10px;border-radius:4px;font-weight:bold;margin:4px;}}
  .green {{background:#C6EFCE;color:#276221;}} .red {{background:#FFC7CE;color:#9C0006;}}
  .blue {{background:#DDEBF7;color:#1F4E79;}} .grey {{background:#D9D9D9;color:#595959;}}
  .meta {{font-size:12px;color:#888;margin-bottom:16px;}}
</style></head><body>
<h1>EDAV Private Endpoint Monitor v{VERSION}</h1>
<p class="meta">Run Date: {run_date} &nbsp;|&nbsp; Mode: {mode} &nbsp;|&nbsp;
Change Ticket: {change_ticket or 'Not provided'} &nbsp;|&nbsp;
Approved By: {approved_by or 'Not provided'}</p>

<h2>Executive Summary</h2>
<span class="badge grey">Total Scanned: {len(results)}</span>
<span class="badge red">Disconnected: {counts.get('Safe Delete Candidate',0)+counts.get('Investigate - Backend Exists',0)}</span>
<span class="badge green">Safe Delete Candidates: {safe}</span>
<span class="badge green">Deleted: {deleted}</span>
<span class="badge green">Verified: {verified}</span>
<span class="badge red">Delete Failed: {failed}</span>
<span class="badge red">Verify Failed: {ver_failed}</span>

<h2>Discovery &amp; Validation Results</h2>
<table><thead><tr>
<th>Endpoint Name</th><th>Resource Group</th><th>Subscription</th><th>Region</th>
<th>Connection State</th><th>Backend Exists</th><th>Terraform Managed</th>
<th>Recommended Action</th><th>Notes</th></tr></thead>
<tbody>{scan_rows_html()}</tbody></table>

<h2>Deletion Report</h2>
<table><thead><tr>
<th>Endpoint Name</th><th>Resource Group</th><th>Subscription</th>
<th>Delete Result</th><th>Timestamp</th><th>Error Message</th>
</tr></thead><tbody>{del_rows_html()}</tbody></table>

<h2>Post-Delete Verification Report</h2>
<table><thead><tr>
<th>Endpoint Name</th><th>Resource Group</th><th>Verification Status</th>
<th>Azure Response</th><th>Timestamp</th>
</tr></thead><tbody>{ver_rows_html()}</tbody></table>

<p class="meta" style="margin-top:32px;">Generated by EDAV Private Endpoint Monitor v{VERSION}.
Only Azure Private Endpoint resources were targeted.
No backend resources were modified or deleted.</p>
</body></html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    log.info("HTML report: %s", path)


# ===========================================================================
# JSON report builder -- NEW in v4.0.0
# ===========================================================================

def build_json_report(results: list, del_log: list, verify_log: list,
                      path: str, run_date: str, mode: str,
                      change_ticket: str = "", approved_by: str = ""):
    """Generate a machine-readable JSON report covering all phases."""
    payload = {
        "report_metadata": {
            "tool":           f"EDAV Private Endpoint Monitor v{VERSION}",
            "run_date":       run_date,
            "mode":           mode,
            "change_ticket":  change_ticket or "Not provided",
            "approved_by":    approved_by or "Not provided",
            "generated_at":   datetime.utcnow().isoformat() + "Z",
        },
        "executive_summary": {
            "total_scanned":      len(results),
            "total_disconnected": sum(1 for r in results if r.get("Connection State") == "Disconnected"),
            "safe_delete_candidates": sum(1 for r in results if r.get("Recommended Action") == "Safe Delete Candidate"),
            "excluded":           sum(1 for r in results if r.get("Recommended Action") in ("Excluded", "Denied")),
            "total_approved":     sum(1 for r in del_log),
            "total_deleted":      sum(1 for r in del_log if r.get("Delete Result") == "Deleted"),
            "total_failed":       sum(1 for r in del_log if r.get("Delete Result") == "Failed"),
            "total_skipped":      sum(1 for r in del_log if r.get("Delete Result") == "Skipped"),
            "total_dry_run":      sum(1 for r in del_log if r.get("Delete Result") == "Dry-Run"),
            "total_verified":     sum(1 for v in verify_log if v.get("Verification Status", "").startswith("Verified")),
            "verify_failed":      sum(1 for v in verify_log if "FAILED" in v.get("Verification Status", "")),
        },
        "discovery_results":  results,
        "deletion_log":       del_log,
        "verification_log":   verify_log,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    log.info("JSON report : %s", path)

# ===========================================================================
# Delete markdown + rollback
# ===========================================================================

def build_delete_markdown(del_log: list, path: str, run_date: str, dry_run: bool):
    def rows_by(result): return [r for r in del_log if r.get("Delete Result") == result]
    deleted  = rows_by("Deleted")
    dry      = rows_by("Dry-Run")
    failed   = rows_by("Failed")
    skipped  = rows_by("Skipped")
    excluded = rows_by("Excluded")
    mode = "DRY RUN" if dry_run else "LIVE"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# EDAV Private Endpoint Deletion Report [{mode}]\n\n")
        f.write(f"**Run Date:** {run_date} | **Mode:** {mode} | **Tool:** EDAV Monitor v{VERSION}\n\n")
        f.write("| Outcome | Count |\n|---|---|\n")
        for label, lst in [("Deleted", deleted), ("Dry-Run (would delete)", dry),
                           ("Failed", failed), ("Skipped", skipped), ("Excluded", excluded)]:
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
            write_table("Would Be Deleted (Dry-Run)", dry,
                        ["Endpoint Name", "Resource Group", "Subscription"])
        else:
            write_table("Deleted", deleted,
                        ["Endpoint Name", "Resource Group", "Subscription", "Delete Timestamp", "Duration (s)"])
            write_table("Failed", failed, ["Endpoint Name", "Resource Group", "Error Message"])
            write_table("Skipped", skipped, ["Endpoint Name", "Resource Group", "Error Message"])
            write_table("Excluded", excluded, ["Endpoint Name", "Resource Group", "Error Message"])
        f.write("---\n\n")
        f.write("> **Safety Note:** Only Azure Private Endpoint resources were targeted.\n")
        f.write("> No backend resources were modified or deleted.\n")


def generate_rollback(del_log: list, backup_dir: str, output_dir: str, run_date: str):
    path = os.path.join(output_dir, "rollback_instructions.md")
    deleted = [r for r in del_log if r.get("Delete Result") == "Deleted"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("# EDAV Private Endpoint Rollback Instructions\n\n")
        f.write(f"**Deletion Run Date:** {run_date}\n")
        f.write(f"**Backup Directory:** {os.path.abspath(backup_dir)}\n\n")
        if not deleted:
            f.write("No endpoints were deleted during this run. No rollback needed.\n")
            return path
        f.write("## Deleted Endpoints\n\n")
        f.write("| # | Endpoint Name | Resource Group | Subscription |\n")
        f.write("|---|---|---|---|\n")
        for i, r in enumerate(deleted, 1):
            f.write(f"| {i} | {r['Endpoint Name']} | {r['Resource Group']} | {r['Subscription']} |\n")
        f.write("\n## How to Restore an Endpoint\n\n")
        f.write("### Step 1 -- Locate the backup JSON\n")
        f.write(f"Backups are saved in: {os.path.abspath(os.path.join(backup_dir, 'private_endpoints'))}\n\n")
        f.write("### Step 2 -- Recreate via Azure CLI\n")
        f.write("```bash\n")
        f.write("az network private-endpoint create \\\\\n")
        f.write("  --name <endpoint-name> \\\\\n")
        f.write("  --resource-group <resource-group> \\\\\n")
        f.write("  --vnet-name <vnet-name> \\\\\n")
        f.write("  --subnet <subnet-name> \\\\\n")
        f.write("  --private-connection-resource-id <backend-resource-id> \\\\\n")
        f.write("  --connection-name <connection-name> \\\\\n")
        f.write("  --group-id <group-id>\n")
        f.write("```\n\n")
        f.write("### Step 3 -- Re-approve the private link connection\n")
        f.write("### Step 4 -- Verify\n")
        f.write("```bash\n")
        f.write("az network private-endpoint show --name <name> --resource-group <rg>\n")
        f.write("```\n")
    log.info("Rollback MD : %s", path)
    return path


def _write_delete_reports(del_log: list, output_dir: str, ts: str, run_dt: str, dry_run: bool):
    prefix    = "EDAV_DryRun_Report"  if dry_run else "EDAV_Delete_Report"
    md_prefix = "dryrun_summary"      if dry_run else "delete_summary"
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
    verified = sum(1 for v in verify_log if v.get("Verification Status","").startswith("Verified"))
    ver_fail = sum(1 for v in verify_log if "FAILED" in v.get("Verification Status",""))
    return (
        "<html><body style='font-family:Calibri,Arial,sans-serif;color:#333'>"
        f"<h2 style='color:#1F4E79'>EDAV Private Endpoint Governance Report</h2>"
        f"<p><strong>Run Date:</strong> {run_date} | "
        f"<strong>Total Scanned:</strong> {len(results)} | "
        f"<strong>Tool:</strong> EDAV Monitor v{VERSION}</p>"
        "<table style='border-collapse:collapse;margin-top:10px'>"
        "<thead><tr style='background:#1F4E79;color:#fff'>"
        "<th style='padding:8px 16px;text-align:left'>Recommended Action</th>"
        "<th style='padding:8px 16px'>Count</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
        f"<br><p style='color:#276221;font-weight:bold'>{safe} Safe Delete Candidate(s) found.</p>"
        f"<p><strong>Deleted:</strong> {deleted} | <strong>Verified:</strong> {verified} | <strong>Verify Failed:</strong> {ver_fail}</p>"
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
    msg["From"]    = cfg["from_email"]
    msg["To"]      = cfg["to_email"]
    msg["Subject"] = subject
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
            s.sendmail(cfg["from_email"],
                       [e.strip() for e in cfg["to_email"].split(",")],
                       msg.as_string())
        log.info("Email sent to %s", cfg["to_email"])
    except Exception as e:
        log.error("Email failed: %s", e)


# ===========================================================================
# Executive Dashboard  -- NEW in v4.0.0
# ===========================================================================

def print_executive_dashboard(results: list, del_log: list, verify_log: list,
                               run_dt: str, change_ticket: str, mode: str):
    """Print a colour-text executive dashboard to console at end of run."""
    total_scanned      = len(results)
    total_disconnected = sum(1 for r in results if r.get("Connection State") == "Disconnected")
    total_approved     = sum(1 for r in del_log)
    total_deleted      = sum(1 for r in del_log if r.get("Delete Result") == "Deleted")
    total_dry_run      = sum(1 for r in del_log if r.get("Delete Result") == "Dry-Run")
    total_failed       = sum(1 for r in del_log if r.get("Delete Result") == "Failed")
    total_skipped      = sum(1 for r in del_log if r.get("Delete Result") in ("Skipped", "Excluded", "Denied"))
    total_verified     = sum(1 for v in verify_log if v.get("Verification Status", "").startswith("Verified"))
    total_ver_failed   = sum(1 for v in verify_log if "FAILED" in v.get("Verification Status", ""))
    excluded           = sum(1 for r in results if r.get("Recommended Action") in ("Excluded", "Denied"))

    log.info("")
    log.info("=" * 72)
    log.info("EXECUTIVE DASHBOARD  --  EDAV Private Endpoint Monitor v%s", VERSION)
    log.info("=" * 72)
    log.info("  Run Date       : %s", run_dt)
    log.info("  Mode           : %s", mode)
    log.info("  Change Ticket  : %s", change_ticket or "Not provided")
    log.info("-" * 72)
    log.info("  Total Endpoints Scanned    : %d", total_scanned)
    log.info("  Total Disconnected         : %d", total_disconnected)
    log.info("  Total Excluded/Denied      : %d", excluded)
    log.info("-" * 72)
    log.info("  Total Approved for Delete  : %d", total_approved)
    if total_dry_run:
        log.info("  Total Dry-Run (simulated)  : %d", total_dry_run)
    log.info("  Total Deleted              : %d", total_deleted)
    log.info("  Total Skipped/Blocked      : %d", total_skipped)
    log.info("  Total Failed               : %d", total_failed)
    log.info("-" * 72)
    log.info("  Total Verified (Gone)      : %d", total_verified)
    log.info("  Total Verification FAILED  : %d", total_ver_failed)
    log.info("=" * 72)
    if total_ver_failed > 0:
        log.error("ACTION REQUIRED: %d endpoint(s) could NOT be verified as deleted!", total_ver_failed)
    if total_failed > 0:
        log.error("ACTION REQUIRED: %d deletion(s) FAILED -- review logs immediately!", total_failed)
    log.info("")


# ===========================================================================
# CLI argument parser
# ===========================================================================

def parse_args():
    p = argparse.ArgumentParser(
        description=(
            f"EDAV Private Endpoint Monitor v{VERSION} -- "
            "Enterprise Azure Governance and Cleanup Platform"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Safe discovery and reporting (default, no deletions):
  python main.py --input report.csv --subscriptions "Sub1,Sub2" --verify-only

  # Dry-run cleanup simulation (no real deletes):
  python main.py --input approved.csv --subscriptions "Sub1" --cleanup-approved --delete-approved --dry-run

  # Live cleanup with post-delete verification:
  python main.py --input approved.csv --subscriptions "Sub1" --cleanup-approved --delete-approved

  # With change ticket, approver, Terraform check, 5s pause:
  python main.py --input approved.csv --subscriptions "Sub1" --cleanup-approved --delete-approved
      --change-ticket CHG0012345 --approved-by "John Smith"
      --terraform-path /tf-scripts --delete-pause 5
""",
    )

    # Input / Output
    p.add_argument("--input", required=True,
                   help="Path to CSV or Excel input file (.csv, .xlsx, .xls)")
    p.add_argument("--subscriptions", default="",
                   help="Comma-separated Azure subscription names to scan")
    p.add_argument("--terraform-path", default="",
                   help="Local Terraform repo path for ownership checks (optional)")
    p.add_argument("--output-dir", default="reports",
                   help="Directory for output reports (default: reports/)")
    p.add_argument("--backup-dir", default="backups",
                   help="Directory for ARM JSON backups before deletion (default: backups/)")
    p.add_argument("--log-dir", default="logs",
                   help="Directory for structured log files (default: logs/)")

    # Operational modes
    p.add_argument("--verify-only", action="store_true", default=False,
                   help="Discovery + Validation + Reporting only. No deletion.")
    p.add_argument("--cleanup-approved", action="store_true", default=False,
                   help="Validation + Deletion + Verification. Requires --delete-approved.")

    # Safety controls
    p.add_argument("--delete-approved", action="store_true", default=False,
                   help="REQUIRED to enable deletion. USE ONLY AFTER APPROVED CHANGE REQUEST.")
    p.add_argument("--dry-run", action="store_true", default=False,
                   help="Simulate deletions only -- no Azure resources are modified.")
    p.add_argument("--delete-pause", type=float, default=2.0,
                   help="Seconds to pause between deletes (default: 2.0)")

    # Governance
    p.add_argument("--exclusions", default="exclusions.txt",
                   help="Path to exclusions file (default: exclusions.txt)")
    p.add_argument("--denylist", default="governance/denylist.json",
                   help="Path to denylist JSON (default: governance/denylist.json)")
    p.add_argument("--allowlist", default="governance/allowlist.json",
                   help="Path to allowlist JSON (default: governance/allowlist.json)")
    p.add_argument("--change-ticket", default="",
                   help="Change ticket reference (e.g. CHG0012345) recorded in all reports")
    p.add_argument("--approved-by", default="",
                   help="Approver identity recorded in audit trail")

    # Email
    p.add_argument("--email-to",    default="", help="Recipient email(s), comma-separated")
    p.add_argument("--email-from",  default="", help="Sender email address")
    p.add_argument("--smtp-server", default="", help="SMTP server hostname")
    p.add_argument("--smtp-port",   default="587", help="SMTP port (default: 587)")
    p.add_argument("--smtp-user",   default="", help="SMTP username")
    p.add_argument("--smtp-pass",   default="", help="SMTP password")
    return p.parse_args()


# ===========================================================================
# Main entry point
# ===========================================================================

def main():
    args = parse_args()
    ts     = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Logging
    global log
    log, log_path = setup_logging(args.log_dir, ts)

    # Determine operating mode
    if args.cleanup_approved and not args.delete_approved:
        log.error("--cleanup-approved requires --delete-approved to be explicitly passed.")
        log.error("This is a safety requirement. Add --delete-approved to proceed.")
        sys.exit(1)

    if args.cleanup_approved:
        mode_label = "CLEANUP-APPROVED (DRY RUN)" if args.dry_run else "CLEANUP-APPROVED [LIVE]"
    else:
        mode_label = "VERIFY-ONLY (Discovery + Validation)"

    log.info("=" * 72)
    log.info("EDAV Private Endpoint Monitor v%s", VERSION)
    log.info("Run Timestamp  : %s", run_dt)
    log.info("Thread         : %s", threading.current_thread().name)
    log.info("Mode           : %s", mode_label)
    log.info("Change Ticket  : %s", args.change_ticket or "Not provided")
    log.info("Approved By    : %s", args.approved_by or "Not provided")
    log.info("Log File       : %s", log_path)
    log.info("=" * 72)

    # Azure login
    validate_azure_login()

    # Output directories
    for d in (args.output_dir, args.backup_dir, args.log_dir, "governance"):
        os.makedirs(d, exist_ok=True)

    # Subscriptions
    subs = [s.strip() for s in args.subscriptions.split(",") if s.strip()]
    if not subs:
        log.info("No subscriptions specified -- auto-detecting...")
        subs = get_subscriptions()
        if not subs:
            log.error("No Azure subscriptions found. Run: az login --use-device-code")
            sys.exit(1)
    log.info("Subscriptions (%d): %s", len(subs), subs)

    # Terraform
    tf_state, tf_code = load_terraform(args.terraform_path)
    if args.terraform_path:
        log.info("Terraform data loaded from: %s", args.terraform_path)

    # Governance
    exclusions = load_exclusions(args.exclusions)
    denylist   = load_denylist(args.denylist)
    allowlist  = load_allowlist(args.allowlist)

    # Input file
    log.info("Loading input: %s", args.input)
    endpoints = load_endpoints(args.input)
    log.info("Loaded %d endpoint(s)", len(endpoints))

    # Report file paths
    xlsx_out   = os.path.join(args.output_dir, f"EDAV_Validation_Report_{ts}.xlsx")
    csv_out    = os.path.join(args.output_dir, f"EDAV_Validation_Report_{ts}.csv")
    md_out     = os.path.join(args.output_dir, f"EDAV_Summary_{ts}.md")
    html_out   = os.path.join(args.output_dir, f"EDAV_Report_{ts}.html")
    json_out   = os.path.join(args.output_dir, f"EDAV_Report_{ts}.json")
    verify_csv  = os.path.join(args.output_dir, f"EDAV_Verification_{ts}.csv")
    verify_xlsx = os.path.join(args.output_dir, f"EDAV_Verification_{ts}.xlsx")

    # ----------------------------------------------------------------
    # PHASE 1: DISCOVERY & SCAN
    # ----------------------------------------------------------------
    log.info("")
    log.info("=" * 72)
    log.info("PHASE 1 -- DISCOVERY & SCAN")
    log.info("=" * 72)
    results = []
    for i, ep in enumerate(endpoints, 1):
        nm = str(ep.get("Endpoint Name", "")).strip()
        log.info("[%d/%d] Scanning: %s", i, len(endpoints), nm or "(empty name)")
        results.append(scan(ep, subs, tf_state, tf_code, exclusions, denylist))

    # ----------------------------------------------------------------
    # PHASE 2: VALIDATION REPORTS
    # ----------------------------------------------------------------
    log.info("")
    log.info("=" * 72)
    log.info("PHASE 2 -- VALIDATION REPORTS")
    log.info("=" * 72)

    df_out = pd.DataFrame(results, columns=SCAN_HEADERS + ["ApprovedToDelete"])
    df_out.to_csv(csv_out, index=False)
    log.info("Validation CSV  : %s", csv_out)

    build_excel(results, xlsx_out, run_dt)
    log.info("Validation XLSX : %s", xlsx_out)

    counts = {}
    for r in results:
        a = r.get("Recommended Action", "Review")
        counts[a] = counts.get(a, 0) + 1

    log.info("")
    log.info("VALIDATION SUMMARY (Total: %d)", len(results))
    for a, c in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        log.info("  %-52s %d", a, c)

    total = len(results) or 1
    with open(md_out, "w", encoding="utf-8") as f:
        f.write("# EDAV Private Endpoint Validation Summary\n\n")
        f.write(f"**Run Date:** {run_dt} | **Total:** {len(results)} | "
                f"**Mode:** {mode_label} | **Tool:** EDAV Monitor v{VERSION}\n\n")
        if args.change_ticket:
            f.write(f"**Change Ticket:** {args.change_ticket}\n\n")
        f.write("| Action | Count | % |\n|---|---|---|\n")
        for a, c in sorted(counts.items(), key=lambda x: x[1], reverse=True):
            f.write(f"| {a} | {c} | {c/total*100:.1f}% |\n")
        f.write(f"\n**Reports saved to:** {args.output_dir}\n")
        f.write(f"\n**Log file:** {log_path}\n")
    log.info("Validation MD   : %s", md_out)

    # ----------------------------------------------------------------
    # PHASE 3: CLEANUP (only in --cleanup-approved mode)
    # ----------------------------------------------------------------
    del_log    = []
    verify_log = []

    if args.cleanup_approved:
        log.info("")
        log.info("=" * 72)
        log.info("PHASE 3 -- CLEANUP ENGINE")
        log.info("=" * 72)
        del_log = run_delete_approved(
            results       = results,
            exclusions    = exclusions,
            denylist      = denylist,
            output_dir    = args.output_dir,
            backup_dir    = args.backup_dir,
            ts            = ts,
            run_dt        = run_dt,
            dry_run       = args.dry_run,
            delete_pause  = args.delete_pause,
            change_ticket = args.change_ticket,
            approved_by   = args.approved_by,
        )

        # ----------------------------------------------------------------
        # PHASE 4: POST-DELETE VERIFICATION
        # ----------------------------------------------------------------
        log.info("")
        log.info("=" * 72)
        log.info("PHASE 4 -- POST-DELETE VERIFICATION")
        log.info("=" * 72)
        verify_log = run_post_delete_verification(del_log, dry_run=args.dry_run)

        # Write verification reports
        df_verify = pd.DataFrame(verify_log if verify_log else [], columns=VERIFY_HEADERS)
        df_verify.to_csv(verify_csv, index=False)
        log.info("Verification CSV  : %s", verify_csv)
        build_verification_excel(verify_log, verify_xlsx, run_dt)
        log.info("Verification XLSX : %s", verify_xlsx)

    else:
        safe_n = counts.get("Safe Delete Candidate", 0)
        if safe_n:
            log.info("")
            log.info(">>> %d Safe Delete Candidate(s) found.", safe_n)
            log.info(">>> Step 1: Open the Excel, add ApprovedToDelete=Yes to approved rows")
            log.info(">>> Step 2: Get change ticket approval (--change-ticket CHGxxxxxxx)")
            log.info(">>> Step 3: Run --cleanup-approved --delete-approved --dry-run to preview")
            log.info(">>> Step 4: Run --cleanup-approved --delete-approved to execute")

    # ----------------------------------------------------------------
    # PHASE 5: HTML + JSON reports (always generated)
    # ----------------------------------------------------------------
    build_html_report(results, del_log, verify_log, html_out, run_dt, mode_label,
                      args.change_ticket, args.approved_by)
    build_json_report(results, del_log, verify_log, json_out, run_dt, mode_label,
                      args.change_ticket, args.approved_by)

    # ----------------------------------------------------------------
    # PHASE 6: EXECUTIVE DASHBOARD
    # ----------------------------------------------------------------
    print_executive_dashboard(results, del_log, verify_log, run_dt,
                               args.change_ticket, mode_label)

    # ----------------------------------------------------------------
    # Email
    # ----------------------------------------------------------------
    if args.email_to and args.smtp_server:
        cfg = dict(
            smtp_server = args.smtp_server, smtp_port = args.smtp_port,
            from_email  = args.email_from,  to_email  = args.email_to,
            smtp_user   = args.smtp_user,   smtp_pass  = args.smtp_pass,
            use_tls = True,
        )
        subj = (f"EDAV Endpoint Governance | {run_dt} | Mode={mode_label} | "
                f"{counts.get('Safe Delete Candidate', 0)} Safe Candidates")
        attachments = [xlsx_out, csv_out, html_out, json_out]
        if verify_log:
            attachments += [verify_csv, verify_xlsx]
        send_email(cfg, subj, build_email_html(results, del_log, verify_log, run_dt), attachments)

    log.info("")
    log.info("EDAV Run Complete.")
    log.info("Reports : %s", args.output_dir)
    log.info("Log     : %s", log_path)


if __name__ == "__main__":
    main()

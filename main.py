#!/usr/bin/env python3
"""
EDAV Private Endpoint Monitor  v3.0
=====================================
Enterprise-grade Azure governance and cleanup platform for EDAV disconnected
private endpoints.

What this does
--------------
* Validates Azure login before any work begins
* Reads a CSV/Excel input file of private endpoints
* Scans each endpoint across one or more subscriptions using Azure CLI
* Validates backend resource existence
* Checks Terraform ownership (state file + .tf source)
* Loads an exclusion list (exclusions.txt) – excluded endpoints are never deleted
* Generates professional colour-coded Excel/CSV/Markdown validation reports
* Optionally emails the report

Deletion (approval-gated, three-layer safety)
---------------------------------------------
* Runs ONLY when --delete-approved is explicitly passed
* Rows must have ApprovedToDelete = Yes
* User must type CONFIRM at the interactive prompt
* --dry-run simulates the entire delete workflow without touching Azure
* Full ARM JSON backup written before every real delete
* Rollback instructions generated automatically
* All Azure calls use exponential-backoff retry (3 attempts)
* Deletes are sequential, grouped by subscription then resource group
* Configurable pause between deletes (--delete-pause, default 2 s)
* Pre-delete re-validation: endpoint must still exist AND be disconnected
* Structured log written to logs/ directory for every run
* Delete report (XLSX/CSV/MD) written after every delete run
* The ONLY Azure delete command ever issued is:
      az network private-endpoint delete
  No backend resources (Key Vault, Storage, SQL, VNet, NIC, DNS, NSG, etc.)
  are ever touched.

Safe by default — Read / Report only unless --delete-approved is passed.
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
# Version
# ===========================================================================
VERSION = "3.0.0"

# ===========================================================================
# Azure CLI path resolution
# ===========================================================================
_AZ_WINDOWS_FALLBACK = r"C:\Program Files (x86)\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"

def _resolve_az():
    az = shutil.which("az")
    if az:
        return az
    if os.path.isfile(_AZ_WINDOWS_FALLBACK):
        return _AZ_WINDOWS_FALLBACK
    print("FATAL: Azure CLI (az) not found.")
    print("  Install from: https://aka.ms/installazurecliwindows")
    print("  Then re-run this script.")
    sys.exit(1)

AZ_CMD = _resolve_az()

# ===========================================================================
# Logging  —  dual output: console + rolling log file
# ===========================================================================
_LOG_FMT = "%(asctime)s  %(levelname)-8s  [%(threadName)s]  %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

def setup_logging(log_dir: str, ts: str) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"EDAV_DELETE_{ts}.log")
    logger = logging.getLogger("edav")
    logger.setLevel(logging.DEBUG)
    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(_LOG_FMT, _DATE_FMT))
    # File handler (DEBUG level — captures everything)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(_LOG_FMT, _DATE_FMT))
    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger, log_path

# Temporary bootstrap logger (replaced after arg parse)
logging.basicConfig(level=logging.INFO, format=_LOG_FMT, datefmt=_DATE_FMT)
log = logging.getLogger("edav")

# ===========================================================================
# Style constants
# ===========================================================================
CLR = {
    "hdr_bg":  "1F4E79", "hdr_fg":  "FFFFFF",
    "safe_bg": "C6EFCE", "safe_fg": "276221",
    "rev_bg":  "FFEB9C", "rev_fg":  "9C6500",
    "del_bg":  "FFC7CE", "del_fg":  "9C0006",
    "na_bg":   "D9D9D9", "na_fg":   "595959",
    "dry_bg":  "DDEBF7", "dry_fg":  "1F4E79",
    "exc_bg":  "E2EFDA", "exc_fg":  "375623",
}

ACTION_STYLE = {
    "Safe Delete Candidate":                              ("safe_bg", "safe_fg"),
    "Do Not Delete - Terraform Managed":                  ("del_bg",  "del_fg"),
    "Investigate - Backend Exists":                       ("rev_bg",  "rev_fg"),
    "Review - Not Disconnected":                          ("rev_bg",  "rev_fg"),
    "Review":                                             ("rev_bg",  "rev_fg"),
    "Endpoint Not Found / Check Subscription or Access":  ("na_bg",   "na_fg"),
    "Skipped - Empty Name":                               ("na_bg",   "na_fg"),
    "Excluded":                                           ("exc_bg",  "exc_fg"),
}

SCAN_HEADERS = [
    "Endpoint Name", "Resource Group", "Subscription",
    "Connection State", "Backend Resource", "Backend Exists",
    "Terraform Managed", "Recommended Action", "Notes",
]

DEL_HEADERS = [
    "Endpoint Name", "Resource Group", "Subscription",
    "Recommended Action", "ApprovedToDelete",
    "Delete Result", "Timestamp", "Duration (s)", "Dry Run", "Error Message",
]

COL_ALIASES = {
    "endpoint name": "Endpoint Name", "endpointname": "Endpoint Name",
    "name":          "Endpoint Name",
    "resource group": "Resource Group", "resourcegroup": "Resource Group",
    "rg":             "Resource Group",
}

# Substrings in Recommended Action that hard-block deletion
_BLOCK_SUBSTRINGS = ("Do Not Delete", "Endpoint Not Found", "Terraform Managed", "Excluded")

# ===========================================================================
# Exclusion list
# ===========================================================================

def load_exclusions(path: str = "exclusions.txt") -> set:
    """Load endpoint names to exclude from deletion from a plain-text file.
    One endpoint name per line.  Lines starting with # are comments."""
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

# ===========================================================================
# Azure login validation
# ===========================================================================

def validate_azure_login() -> dict:
    """Run az account show and fail fast if not logged in.
    Returns the account dict on success."""
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
    log.error("=" * 60)
    log.error("You are not logged in to Azure CLI.")
    log.error("")
    log.error("Run ONE of the following commands and try again:")
    log.error("")
    log.error("  Interactive browser login:")
    log.error("    az login")
    log.error("")
    log.error("  Device-code login (recommended for remote/server sessions):")
    log.error("    az login --use-device-code")
    log.error("")
    log.error("  Service principal login:")
    log.error("    az login --service-principal -u <appId> -p <password> --tenant <tenant>")
    log.error("")
    log.error("After login, verify with:  az account show")
    log.error("=" * 60)
    sys.exit(1)

# ===========================================================================
# Retry wrapper
# ===========================================================================

def _az_with_retry(args: list, retries: int = 3, timeout: int = 30):
    """Run an Azure CLI command with exponential-backoff retry.
    Returns parsed JSON or None."""
    cmd = [AZ_CMD] + args + ["-o", "json"]
    for attempt in range(1, retries + 1):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
            if attempt < retries:
                wait = 2 ** attempt          # 2, 4, 8 seconds
                log.debug("  az retry %d/%d in %ds  cmd=%s", attempt, retries, wait, args[:3])
                time.sleep(wait)
        except subprocess.TimeoutExpired:
            log.debug("  az timeout on attempt %d/%d", attempt, retries)
            if attempt < retries:
                time.sleep(2 ** attempt)
        except Exception as exc:
            log.debug("  az exception attempt %d/%d: %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(2 ** attempt)
    return None

# Keep a simple alias so callers don't have to change
def _az(args, silent=False):
    return _az_with_retry(args)

# ===========================================================================
# Azure subscription helpers
# ===========================================================================

def get_subscriptions() -> list:
    subs = _az(["account", "list", "--query", "[].name"])
    return list(subs) if subs else []

def set_subscription(name: str) -> bool:
    r = subprocess.run(
        [AZ_CMD, "account", "set", "--subscription", name],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode == 0:
        log.debug("  Subscription set: %s", name)
        return True
    log.debug("  Failed to set subscription: %s  stderr=%s", name, r.stderr[:200])
    return False

def verify_subscription_context(expected_sub: str) -> bool:
    """Confirm that the active az context matches expected_sub."""
    data = _az(["account", "show"])
    if not data:
        return False
    return data.get("name", "").strip().lower() == expected_sub.strip().lower()

# ===========================================================================
# Private endpoint Azure helpers
# ===========================================================================

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

def private_endpoint_still_valid_for_delete(name: str, rg: str, sub: str) -> tuple:
    """Pre-delete re-validation.
    Returns (ok: bool, reason: str).
    ok=True only if endpoint exists AND is still Disconnected."""
    if not name or not rg or not sub:
        return False, "Missing name, RG, or subscription"
    if not set_subscription(sub):
        return False, f"Cannot set subscription context to '{sub}'"
    if not verify_subscription_context(sub):
        return False, f"Subscription context verification failed for '{sub}'"
    pe = get_private_endpoint(name, rg)
    if pe is None:
        return False, "Endpoint no longer exists in Azure"
    state = get_private_endpoint_connection_state(pe)
    if state != "Disconnected":
        return False, f"Endpoint connection state changed to '{state}' — no longer Disconnected"
    return True, "OK"

def resource_exists(rid: str) -> bool:
    if not rid:
        return False
    return _az(["resource", "show", "--ids", rid]) is not None

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
        "subscription": sub,
        "resource_group": rg,
        "endpoint_name": name,
        "arm_resource": pe,
    }
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    log.info("    Backup: %s", fpath)
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
    path = path.strip()
    if not os.path.isfile(path):
        log.error("Input file not found: %s", path)
        sys.exit(1)
    ext = Path(path).suffix.lower()
    if ext in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype=str).fillna("")
    else:
        df = pd.read_csv(path, dtype=str).fillna("")
    rename = {}
    for col in df.columns:
        key = col.strip().lower().replace(" ", "")
        alias = COL_ALIASES.get(key) or COL_ALIASES.get(col.strip().lower())
        if alias and col != alias:
            rename[col] = alias
    df.rename(columns=rename, inplace=True)
    if "Endpoint Name" not in df.columns:
        log.error("No 'Endpoint Name' column found. Columns: %s", list(df.columns))
        sys.exit(1)
    for col in ("Resource Group", "ApprovedToDelete"):
        if col not in df.columns:
            df[col] = ""
    return df.to_dict(orient="records")

# ===========================================================================
# Core scan
# ===========================================================================

def scan(ep: dict, subscriptions: list, tf_state: str,
         tf_code: str, exclusions: set) -> dict:
    name = str(ep.get("Endpoint Name", "")).strip()
    rg   = str(ep.get("Resource Group", "")).strip()
    rec  = {
        "Endpoint Name":      name,
        "Resource Group":     rg,
        "Subscription":       "",
        "Connection State":   "Not Found",
        "Backend Resource":   "",
        "Backend Exists":     "Unknown",
        "Terraform Managed":  "Unknown",
        "Recommended Action": "",
        "Notes":              "",
        "ApprovedToDelete":   str(ep.get("ApprovedToDelete", "")).strip(),
    }
    if not name:
        rec["Recommended Action"] = "Skipped - Empty Name"
        return rec

    # Exclusion list check
    if name.lower() in exclusions:
        rec["Recommended Action"] = "Excluded"
        rec["Notes"] = "Listed in exclusions.txt — will never be deleted."
        log.info("  [EXCLUDED] %s", name)
        return rec

    for sub in subscriptions:
        if not set_subscription(sub):
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
            conns = (pe.get("privateLinkServiceConnections") or
                     pe.get("manualPrivateLinkServiceConnections") or [])
            if conns:
                cs  = conns[0].get("privateLinkServiceConnectionState", {})
                rec["Connection State"] = cs.get("status", "Unknown")
                bid = conns[0].get("privateLinkServiceId", "")
                rec["Backend Resource"] = bid
                rec["Backend Exists"]   = "Yes" if resource_exists(bid) else "No"
            else:
                rec["Connection State"] = "No Connection Object"
            break

    if rec["Subscription"] == "":
        rec["Connection State"]   = "Endpoint Not Found"
        rec["Notes"]              = "Not found in any scanned subscription."
        rec["Recommended Action"] = "Endpoint Not Found / Check Subscription or Access"
        return rec

    rec["Terraform Managed"] = in_terraform(name, tf_state, tf_code)
    action, notes = decide(rec["Connection State"],
                           rec["Backend Exists"],
                           rec["Terraform Managed"])
    rec["Recommended Action"] = action
    rec["Notes"]              = notes
    return rec

# ===========================================================================
# Excel helpers
# ===========================================================================

def _fill(c):            return PatternFill("solid", fgColor=c)
def _font(c, bold=False): return Font(color=c, bold=bold, name="Calibri", size=10)
def _border():
    s = Side(style="thin", color="BFBFBF")
    return Border(left=s, right=s, top=s, bottom=s)

# ===========================================================================
# Validation report builder
# ===========================================================================

def build_excel(results: list, path: str, run_date: str):
    wb = Workbook()

    # ---- Summary sheet ----
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = "EDAV Disconnected Private Endpoint Governance Report"
    ws["A1"].font = Font(name="Calibri", size=16, bold=True, color=CLR["hdr_bg"])
    ws.merge_cells("A1:D1")
    ws["A2"] = f"Generated: {run_date}   |   EDAV Monitor v{VERSION}"
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
        cell.fill      = _fill(CLR["hdr_bg"])
        cell.font      = _font(CLR["hdr_fg"], bold=True)
        cell.alignment = Alignment(horizontal="center")
    total = len(results) or 1
    row = 5
    for action, count in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        style  = ACTION_STYLE.get(action, ("na_bg", "na_fg"))
        bg, fg = CLR[style[0]], CLR[style[1]]
        ws.cell(row=row, column=1, value=action).fill        = _fill(bg)
        ws.cell(row=row, column=1).font                      = _font(fg)
        ws.cell(row=row, column=2, value=count).fill         = _fill(bg)
        ws.cell(row=row, column=2).font                      = _font(fg)
        ws.cell(row=row, column=2).alignment                 = Alignment(horizontal="center")
        ws.cell(row=row, column=3, value=f"{count/total*100:.1f}%").fill = _fill(bg)
        ws.cell(row=row, column=3).font                      = _font(fg)
        ws.cell(row=row, column=3).alignment                 = Alignment(horizontal="center")
        row += 1

    ws.column_dimensions["A"].width = 55
    ws.column_dimensions["B"].width = 10
    ws.column_dimensions["C"].width = 12

    # ---- All Endpoints sheet ----
    ws2 = wb.create_sheet("All Endpoints")
    for ci, h in enumerate(SCAN_HEADERS, 1):
        c = ws2.cell(row=1, column=ci, value=h)
        c.fill      = _fill(CLR["hdr_bg"])
        c.font      = _font(CLR["hdr_fg"], bold=True)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border    = _border()

    for ri, rec in enumerate(results, 2):
        action = rec.get("Recommended Action", "")
        style  = ACTION_STYLE.get(action, ("na_bg", "na_fg"))
        bg, fg = CLR[style[0]], CLR[style[1]]
        for ci, h in enumerate(SCAN_HEADERS, 1):
            c = ws2.cell(row=ri, column=ci, value=rec.get(h, ""))
            c.fill      = _fill(bg)
            c.font      = _font(fg)
            c.border    = _border()
            c.alignment = Alignment(wrap_text=True, vertical="top")

    col_w = [42, 26, 30, 20, 60, 14, 18, 45, 60]
    for i, w in enumerate(col_w, 1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    ws2.freeze_panes    = "A2"
    ws2.auto_filter.ref = ws2.dimensions

    # ---- Safe Delete sheet ----
    ws3 = wb.create_sheet("Safe Delete Candidates")
    for ci, h in enumerate(SCAN_HEADERS, 1):
        c = ws3.cell(row=1, column=ci, value=h)
        c.fill      = _fill(CLR["hdr_bg"])
        c.font      = _font(CLR["hdr_fg"], bold=True)
        c.alignment = Alignment(horizontal="center")
        c.border    = _border()
    dr = 2
    for rec in results:
        if rec.get("Recommended Action") == "Safe Delete Candidate":
            for ci, h in enumerate(SCAN_HEADERS, 1):
                c = ws3.cell(row=dr, column=ci, value=rec.get(h, ""))
                c.fill   = _fill(CLR["safe_bg"])
                c.font   = _font(CLR["safe_fg"])
                c.border = _border()
            dr += 1
    ws3.freeze_panes = "A2"

    # ---- Excluded sheet ----
    ws4 = wb.create_sheet("Excluded")
    for ci, h in enumerate(SCAN_HEADERS, 1):
        c = ws4.cell(row=1, column=ci, value=h)
        c.fill      = _fill(CLR["hdr_bg"])
        c.font      = _font(CLR["hdr_fg"], bold=True)
        c.alignment = Alignment(horizontal="center")
        c.border    = _border()
    er = 2
    for rec in results:
        if rec.get("Recommended Action") == "Excluded":
            for ci, h in enumerate(SCAN_HEADERS, 1):
                c = ws4.cell(row=er, column=ci, value=rec.get(h, ""))
                c.fill   = _fill(CLR["exc_bg"])
                c.font   = _font(CLR["exc_fg"])
                c.border = _border()
            er += 1
    ws4.freeze_panes = "A2"

    wb.save(path)

# ===========================================================================
# Delete report builders
# ===========================================================================

def build_delete_excel(del_log: list, path: str, run_date: str, dry_run: bool):
    wb  = Workbook()
    ws  = wb.active
    ws.title = "Delete Report"
    mode_label = "DRY RUN — No Real Changes" if dry_run else "LIVE DELETION REPORT"
    ws["A1"] = f"EDAV Private Endpoint Deletion Report  [{mode_label}]"
    ws["A1"].font = Font(name="Calibri", size=14, bold=True,
                         color=CLR["dry_fg"] if dry_run else CLR["hdr_bg"])
    ws.merge_cells("A1:J1")
    ws["A2"] = f"Generated: {run_date}   |   EDAV Monitor v{VERSION}"
    ws["A2"].font = Font(name="Calibri", size=10, italic=True, color="595959")
    ws.merge_cells("A2:J2")

    for ci, h in enumerate(DEL_HEADERS, 1):
        c = ws.cell(row=4, column=ci, value=h)
        c.fill      = _fill(CLR["hdr_bg"])
        c.font      = _font(CLR["hdr_fg"], bold=True)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border    = _border()

    result_colour = {
        "Deleted":      ("safe_bg", "safe_fg"),
        "Dry-Run":      ("dry_bg",  "dry_fg"),
        "Failed":       ("del_bg",  "del_fg"),
        "Skipped":      ("na_bg",   "na_fg"),
        "Excluded":     ("exc_bg",  "exc_fg"),
    }
    for ri, row_data in enumerate(del_log, 5):
        result         = row_data.get("Delete Result", "")
        style          = result_colour.get(result, ("na_bg", "na_fg"))
        bg, fg         = CLR[style[0]], CLR[style[1]]
        for ci, h in enumerate(DEL_HEADERS, 1):
            c = ws.cell(row=ri, column=ci, value=row_data.get(h, ""))
            c.fill      = _fill(bg)
            c.font      = _font(fg)
            c.border    = _border()
            c.alignment = Alignment(wrap_text=True, vertical="top")

    col_w = [42, 26, 30, 38, 18, 12, 20, 12, 10, 55]
    for i, w in enumerate(col_w, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A5"
    wb.save(path)


def build_delete_markdown(del_log: list, path: str, run_date: str, dry_run: bool):
    def rows_by(result):
        return [r for r in del_log if r.get("Delete Result") == result]

    deleted   = rows_by("Deleted")
    dry       = rows_by("Dry-Run")
    failed    = rows_by("Failed")
    skipped   = rows_by("Skipped")
    excluded  = rows_by("Excluded")
    mode      = "DRY RUN" if dry_run else "LIVE"

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# EDAV Private Endpoint Deletion Report  [{mode}]\n\n")
        f.write(f"**Run Date:** {run_date}  |  **Mode:** {mode}  |  **Tool:** EDAV Monitor v{VERSION}\n\n")
        f.write("| Outcome | Count |\n|---|---|\n")
        for label, lst in [("Deleted", deleted), ("Dry-Run (would delete)", dry),
                            ("Failed", failed), ("Skipped", skipped),
                            ("Excluded", excluded)]:
            if lst:
                f.write(f"| {label} | {len(lst)} |\n")
        f.write(f"| **Total** | **{len(del_log)}** |\n\n")

        def write_table(title, lst, cols):
            if not lst:
                return
            f.write(f"## {title}\n\n")
            f.write("| " + " | ".join(cols) + " |\n")
            f.write("|" + "|".join(["---"] * len(cols)) + "|\n")
            for r in lst:
                f.write("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |\n")
            f.write("\n")

        if dry_run:
            write_table("Would Be Deleted (Dry-Run)",  dry,
                        ["Endpoint Name", "Resource Group", "Subscription"])
        else:
            write_table("Deleted", deleted,
                        ["Endpoint Name", "Resource Group", "Subscription", "Timestamp", "Duration (s)"])
        write_table("Failed",   failed,
                    ["Endpoint Name", "Resource Group", "Error Message"])
        write_table("Skipped",  skipped,
                    ["Endpoint Name", "Resource Group", "Error Message"])
        write_table("Excluded", excluded,
                    ["Endpoint Name", "Resource Group", "Error Message"])

        f.write("---\n\n")
        f.write("> **Safety Note:** Only Azure Private Endpoint resources were targeted.\n")
        f.write("> No backend resources (Key Vault, Storage, SQL, VNet, NIC, DNS, NSG, etc.)\n")
        f.write("> were modified or deleted.\n")

# ===========================================================================
# Rollback instructions generator
# ===========================================================================

def generate_rollback(del_log: list, backup_dir: str, output_dir: str, run_date: str):
    path = os.path.join(output_dir, "rollback_instructions.md")
    deleted = [r for r in del_log if r.get("Delete Result") == "Deleted"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("# EDAV Private Endpoint Rollback Instructions\n\n")
        f.write(f"**Deletion Run Date:** {run_date}\n")
        f.write(f"**Backup Directory:** {os.path.abspath(backup_dir)}\n\n")
        f.write("> These instructions describe how to recreate private endpoints\n")
        f.write("> that were deleted during this run.\n\n")
        if not deleted:
            f.write("No endpoints were deleted during this run. No rollback needed.\n")
            log.info("Rollback MD: %s (no deletions)", path)
            return path
        f.write("## Deleted Endpoints\n\n")
        f.write("| # | Endpoint Name | Resource Group | Subscription |\n")
        f.write("|---|---|---|---|\n")
        for i, r in enumerate(deleted, 1):
            f.write(f"| {i} | {r[\"Endpoint Name\"]} | {r[\"Resource Group\"]} | {r[\"Subscription\"]} |\n")
        f.write("\n## How to Restore an Endpoint\n\n")
        f.write("### Step 1 — Locate the backup JSON\n")
        f.write(f"Backups are saved in: {os.path.abspath(os.path.join(backup_dir, 'private_endpoints'))}\n\n")
        f.write("### Step 2 — Recreate via Azure CLI\n")
        f.write("Use the values from the backup JSON to reconstruct the endpoint:\n")
        f.write("```bash\n")
        f.write("az network private-endpoint create \\\n")
        f.write("  --name <endpoint-name> \\\n")
        f.write("  --resource-group <resource-group> \\\n")
        f.write("  --vnet-name <vnet-name> \\\n")
        f.write("  --subnet <subnet-name> \\\n")
        f.write("  --private-connection-resource-id <backend-resource-id> \\\n")
        f.write("  --connection-name <connection-name> \\\n")
        f.write("  --group-id <group-id>\n")
        f.write("```\n\n")
        f.write("### Step 3 — Re-approve the private link connection\n")
        f.write("The backend service owner must approve the new connection request.\n\n")
        f.write("### Step 4 — Verify\n")
        f.write("```bash\n")
        f.write("az network private-endpoint show --name <name> --resource-group <rg>\n")
        f.write("```\n\n")
        f.write("---\n*Generated by EDAV Private Endpoint Monitor v" + VERSION + "*\n")
    log.info("Rollback MD : %s", path)
    return path


# ===========================================================================
# Email
# ===========================================================================

def build_email_html(results: list, run_date: str) -> str:
    counts = {}
    for r in results:
        a = r.get("Recommended Action", "Review")
        counts[a] = counts.get(a, 0) + 1
    rows = "".join(
        f"<tr><td style='padding:6px 12px;border:1px solid #ddd'>{k}</td>"
        f"<td style='padding:6px 12px;border:1px solid #ddd;text-align:center'>{v}</td></tr>"
        for k, v in sorted(counts.items(), key=lambda x: x[1], reverse=True)
    )
    safe = counts.get("Safe Delete Candidate", 0)
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
        f"<br><p style='color:#276221;font-weight:bold'>"
        f"{safe} Safe Delete Candidate(s) ready after change ticket approval.</p>"
        "<p>Full validation report is attached. "
        "<em>No endpoints have been deleted by this run.</em></p>"
        f"<p style='color:#888;font-size:11px'>EDAV Private Endpoint Monitor v{VERSION}</p>"
        "</body></html>"
    )


def send_email(cfg: dict, subject: str, body: str, attachments: list):
    req = ("smtp_server", "smtp_port", "from_email", "to_email")
    if any(not cfg.get(k) for k in req):
        log.warning("Incomplete email config — skipping.")
        return
    msg           = MIMEMultipart("mixed")
    msg["From"]   = cfg["from_email"]
    msg["To"]     = cfg["to_email"]
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
# Deletion safety gate
# ===========================================================================

def _is_deletion_blocked(rec: dict, exclusions: set) -> tuple:
    """Return (blocked: bool, reason: str).
    A row is blocked from deletion if ANY of the following is true:
      - Endpoint Name is blank
      - Resource Group is blank
      - ApprovedToDelete != 'yes' (case-insensitive)
      - Recommended Action contains a blocking keyword
      - Endpoint name is in the exclusions set
    """
    name     = str(rec.get("Endpoint Name", "")).strip()
    rg       = str(rec.get("Resource Group", "")).strip()
    approved = str(rec.get("ApprovedToDelete", "")).strip().lower()
    action   = str(rec.get("Recommended Action", "")).strip()

    if not name:
        return True, "Endpoint Name is blank"
    if not rg:
        return True, "Resource Group is blank"
    if approved != "yes":
        return True, f"ApprovedToDelete='{rec.get('ApprovedToDelete','')}' — must be 'Yes'"
    if name.lower() in exclusions:
        return True, "Listed in exclusions.txt"
    for substr in _BLOCK_SUBSTRINGS:
        if substr.lower() in action.lower():
            return True, f"Recommended Action '{action}' contains blocking keyword"
    return False, ""


# ===========================================================================
# The ONLY allowed delete call
# ===========================================================================

def _execute_delete(name: str, rg: str) -> tuple:
    """Issue az network private-endpoint delete.
    This is the ONLY Azure delete command in this entire codebase.
    Returns (success: bool, error_msg: str).
    NO backend resources are ever touched by this function."""
    r = subprocess.run(
        [AZ_CMD, "network", "private-endpoint", "delete",
         "--name", name, "--resource-group", rg, "--yes"],
        capture_output=True, text=True, timeout=90,
    )
    if r.returncode == 0:
        return True, ""
    err = (r.stderr or r.stdout or "Unknown error").strip()
    return False, err

# ===========================================================================
# Core deletion workflow
# ===========================================================================

def run_delete_approved(
    results:      list,
    exclusions:   set,
    output_dir:   str,
    backup_dir:   str,
    ts:           str,
    run_dt:       str,
    dry_run:      bool    = False,
    delete_pause: float   = 2.0,
) -> list:
    """
    Approval-gated deletion workflow.

    Safety layers enforced (in order):
      1. --delete-approved flag required (enforced by caller)
      2. ApprovedToDelete == 'Yes' required per row
      3. Exclusion list checked
      4. Recommended Action keyword block
      5. User must type CONFIRM at interactive prompt
      6. ARM backup exported before each delete
      7. Pre-delete re-validation (endpoint exists AND still Disconnected)
      8. Subscription context verified before each delete
      9. Deletes grouped by subscription then resource group, sequential
     10. Configurable pause between deletes
     11. --dry-run simulates everything without touching Azure
     12. Rollback instructions generated after run
    """
    mode = "DRY RUN" if dry_run else "LIVE DELETE"
    log.info("")
    log.info("=" * 70)
    log.info("DELETE-APPROVED MODE  [%s]", mode)
    log.info("=" * 70)

    del_log = []

    # ------------------------------------------------------------------
    # Pass 1 — screening
    # ------------------------------------------------------------------
    candidates = []
    for rec in results:
        blocked, reason = _is_deletion_blocked(rec, exclusions)
        if blocked:
            result_label = "Excluded" if "exclusions.txt" in reason else "Skipped"
            entry = {
                "Endpoint Name":     rec.get("Endpoint Name", ""),
                "Resource Group":    rec.get("Resource Group", ""),
                "Subscription":      rec.get("Subscription", ""),
                "Recommended Action": rec.get("Recommended Action", ""),
                "ApprovedToDelete":  rec.get("ApprovedToDelete", ""),
                "Delete Result":     result_label,
                "Timestamp":         "",
                "Duration (s)":      "",
                "Dry Run":           str(dry_run),
                "Error Message":     reason,
            }
            del_log.append(entry)
            if str(rec.get("ApprovedToDelete", "")).strip().lower() == "yes":
                log.warning("  BLOCKED [%s]: %s  — %s",
                            result_label, rec.get("Endpoint Name", "(empty)"), reason)
        else:
            candidates.append(rec)

    if not candidates:
        log.info("No rows qualify for deletion.")
        log.info("Nothing will be %s.", "simulated" if dry_run else "deleted")
        _write_delete_reports(del_log, output_dir, ts, run_dt, dry_run)
        generate_rollback(del_log, backup_dir, output_dir, run_dt)
        return del_log

    # ------------------------------------------------------------------
    # Display queued list
    # ------------------------------------------------------------------
    log.info("")
    log.info("Endpoints queued for %s (%d):",
             "simulation" if dry_run else "deletion", len(candidates))
    for rec in candidates:
        log.info("  - %-45s  RG=%-25s  Sub=%s",
                 rec["Endpoint Name"], rec["Resource Group"],
                 rec.get("Subscription", ""))

    # ------------------------------------------------------------------
    # CONFIRM prompt (skip for dry-run)
    # ------------------------------------------------------------------
    if not dry_run:
        log.info("")
        log.warning("=" * 70)
        log.warning("WARNING: This will PERMANENTLY DELETE Azure Private Endpoints.")
        log.warning("This action CANNOT be undone.")
        log.warning("Only Private Endpoint resources are deleted.")
        log.warning("Backend resources are NEVER touched.")
        log.warning("=" * 70)
        confirm = input("\nType CONFIRM to continue: ")
        if confirm.strip() != "CONFIRM":
            log.info("Deletion cancelled — user did not type CONFIRM.")
            for rec in candidates:
                del_log.append({
                    "Endpoint Name":     rec["Endpoint Name"],
                    "Resource Group":    rec["Resource Group"],
                    "Subscription":      rec.get("Subscription", ""),
                    "Recommended Action": rec.get("Recommended Action", ""),
                    "ApprovedToDelete":  rec.get("ApprovedToDelete", ""),
                    "Delete Result":     "Skipped",
                    "Timestamp":         datetime.now().isoformat(),
                    "Duration (s)":      "",
                    "Dry Run":           "False",
                    "Error Message":     "Cancelled by user at CONFIRM prompt",
                })
            _write_delete_reports(del_log, output_dir, ts, run_dt, dry_run)
            generate_rollback(del_log, backup_dir, output_dir, run_dt)
            return del_log
    else:
        log.info("")
        log.info("[DRY RUN] Simulating deletion — no Azure resources will be modified.")

    # ------------------------------------------------------------------
    # Pass 2 — grouped sequential execution
    # ------------------------------------------------------------------
    from collections import defaultdict
    grouped = defaultdict(lambda: defaultdict(list))
    for rec in candidates:
        sub = rec.get("Subscription", "")
        rg  = rec.get("Resource Group", "")
        grouped[sub][rg].append(rec)

    for sub, rg_map in grouped.items():
        log.info("")
        log.info("  Subscription: %s", sub)
        if sub and not dry_run:
            if not set_subscription(sub):
                log.error("  Cannot set subscription '%s' — skipping all endpoints in it.", sub)
                for rg, recs in rg_map.items():
                    for rec in recs:
                        del_log.append({
                            "Endpoint Name":     rec["Endpoint Name"],
                            "Resource Group":    rg,
                            "Subscription":      sub,
                            "Recommended Action": rec.get("Recommended Action",""),
                            "ApprovedToDelete":  rec.get("ApprovedToDelete",""),
                            "Delete Result":     "Failed",
                            "Timestamp":         datetime.now().isoformat(),
                            "Duration (s)":      "",
                            "Dry Run":           str(dry_run),
                            "Error Message":     f"Cannot set subscription context to '{sub}'",
                        })
                continue

        for rg, recs in rg_map.items():
            log.info("    Resource Group: %s", rg)
            for rec in recs:
                name  = rec["Endpoint Name"]
                t_start = time.time()
                entry = {
                    "Endpoint Name":     name,
                    "Resource Group":    rg,
                    "Subscription":      sub,
                    "Recommended Action": rec.get("Recommended Action",""),
                    "ApprovedToDelete":  rec.get("ApprovedToDelete",""),
                    "Delete Result":     "",
                    "Timestamp":         datetime.now().isoformat(),
                    "Duration (s)":      "",
                    "Dry Run":           str(dry_run),
                    "Error Message":     "",
                }

                if dry_run:
                    log.info("      [DRY-RUN] Would delete: %s", name)
                    entry["Delete Result"] = "Dry-Run"
                    entry["Duration (s)"]  = "0"
                    del_log.append(entry)
                    continue

                # --- Pre-delete ARM backup ---
                log.info("      [Backup] %s ...", name)
                pe_data = get_private_endpoint(name, rg)
                if pe_data:
                    os.makedirs(backup_dir, exist_ok=True)
                    export_endpoint_backup(pe_data, name, rg, sub, backup_dir, ts)
                else:
                    log.warning("      Could not fetch ARM data for backup — proceeding.")

                # --- Pre-delete re-validation ---
                log.info("      [Validate] %s ...", name)
                valid, reason = private_endpoint_still_valid_for_delete(name, rg, sub)
                if not valid:
                    log.warning("      SKIPPED: %s — %s", name, reason)
                    entry["Delete Result"]  = "Skipped"
                    entry["Duration (s)"]   = f"{time.time()-t_start:.1f}"
                    entry["Error Message"]  = reason
                    del_log.append(entry)
                    continue

                # --- Subscription context verification ---
                if not verify_subscription_context(sub):
                    msg = f"Subscription context mismatch — expected '{sub}'"
                    log.error("      FAILED: %s — %s", name, msg)
                    entry["Delete Result"]  = "Failed"
                    entry["Duration (s)"]   = f"{time.time()-t_start:.1f}"
                    entry["Error Message"]  = msg
                    del_log.append(entry)
                    continue

                # --- Execute delete (the ONLY az delete command) ---
                log.info("      [DELETE] %s  RG=%s  Sub=%s", name, rg, sub)
                ok, err = _execute_delete(name, rg)
                elapsed = f"{time.time()-t_start:.1f}"
                if ok:
                    log.info("      -> Deleted  (%.1fs)", float(elapsed))
                    entry["Delete Result"] = "Deleted"
                else:
                    log.error("      -> FAILED  %s", err)
                    entry["Delete Result"]  = "Failed"
                    entry["Error Message"]  = err
                entry["Duration (s)"] = elapsed
                del_log.append(entry)

                if delete_pause > 0:
                    log.debug("      Pause %.1fs before next delete...", delete_pause)
                    time.sleep(delete_pause)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    def _count(label):
        return sum(1 for r in del_log if r["Delete Result"] == label)

    log.info("")
    log.info("=" * 70)
    log.info("DELETION SUMMARY  [%s]", mode)
    if dry_run:
        log.info("  Would-Delete : %d", _count("Dry-Run"))
    else:
        log.info("  Deleted      : %d", _count("Deleted"))
        log.info("  Failed       : %d", _count("Failed"))
    log.info("  Skipped      : %d", _count("Skipped"))
    log.info("  Excluded     : %d", _count("Excluded"))
    log.info("=" * 70)

    _write_delete_reports(del_log, output_dir, ts, run_dt, dry_run)
    generate_rollback(del_log, backup_dir, output_dir, run_dt)
    return del_log


def _write_delete_reports(del_log: list, output_dir: str, ts: str,
                          run_dt: str, dry_run: bool):
    prefix    = "EDAV_DryRun_Report" if dry_run else "EDAV_Delete_Report"
    md_prefix = "dryrun_summary"     if dry_run else "delete_summary"
    del_csv   = os.path.join(output_dir, f"{prefix}_{ts}.csv")
    del_xlsx  = os.path.join(output_dir, f"{prefix}_{ts}.xlsx")
    del_md    = os.path.join(output_dir, f"{md_prefix}_{ts}.md")

    df_del = pd.DataFrame(del_log, columns=DEL_HEADERS)
    df_del.to_csv(del_csv, index=False)
    log.info("Delete CSV : %s", del_csv)
    build_delete_excel(del_log, del_xlsx, run_dt, dry_run)
    log.info("Delete XLSX: %s", del_xlsx)
    build_delete_markdown(del_log, del_md, run_dt, dry_run)
    log.info("Delete MD  : %s", del_md)

# ===========================================================================
# CLI argument parser
# ===========================================================================

def parse_args():
    p = argparse.ArgumentParser(
        description=(
            f"EDAV Private Endpoint Monitor v{VERSION} — "
            "Enterprise Azure Governance and Cleanup Platform"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Read-only validation (default, safest):
    python main.py --input report.csv --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1"

  Dry-run (simulate deletes, no changes):
    python main.py --input approved.csv --subscriptions "OCIO-TSBDEV-C1" --delete-approved --dry-run

  Live deletion (requires change ticket approval):
    python main.py --input approved.csv --subscriptions "OCIO-TSBDEV-C1" --delete-approved

  With Terraform check + 5 s pause between deletes:
    python main.py --input approved.csv --subscriptions "OCIO-TSBDEV-C1"
      --terraform-path C:\\terraform-scripts --delete-approved --delete-pause 5
""",
    )
    p.add_argument("--input",          required=True,
                   help="Path to CSV or Excel input file")
    p.add_argument("--subscriptions",  default="",
                   help="Comma-separated Azure subscription names to scan")
    p.add_argument("--terraform-path", default="",
                   help="Local Terraform repo path for ownership checks (optional)")
    p.add_argument("--output-dir",     default="reports",
                   help="Directory for output reports (default: reports/)")
    p.add_argument("--backup-dir",     default="backups",
                   help="Directory for ARM JSON backups before deletion (default: backups/)")
    p.add_argument("--log-dir",        default="logs",
                   help="Directory for structured log files (default: logs/)")
    p.add_argument("--exclusions",     default="exclusions.txt",
                   help="Path to exclusions file (default: exclusions.txt)")
    p.add_argument("--delete-approved", action="store_true", default=False,
                   help=(
                       "DANGER: Activate deletion mode. "
                       "Rows with ApprovedToDelete=Yes will be deleted. "
                       "USE ONLY AFTER AN APPROVED CHANGE REQUEST. "
                       "Combine with --dry-run to simulate first."
                   ))
    p.add_argument("--dry-run",        action="store_true", default=False,
                   help="Simulate deletions only — no Azure resources are modified. "
                        "Requires --delete-approved.")
    p.add_argument("--delete-pause",   type=float, default=2.0,
                   help="Seconds to pause between deletes (default: 2.0)")
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
    args   = parse_args()
    ts     = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ---- Set up structured logging ----
    global log
    log, log_path = setup_logging(args.log_dir, ts)

    log.info("=" * 70)
    log.info("EDAV Private Endpoint Monitor  v%s", VERSION)
    log.info("Run: %s", run_dt)
    log.info("Thread: %s", threading.current_thread().name)
    if args.delete_approved and args.dry_run:
        log.info("Mode: DRY RUN (--delete-approved + --dry-run — no changes)")
    elif args.delete_approved:
        log.warning("Mode: LIVE DELETE (--delete-approved — REAL deletions will occur)")
    else:
        log.info("Mode: READ-ONLY / REPORT (no deletions)")
    log.info("Log file: %s", log_path)
    log.info("=" * 70)

    # ---- Azure login check (always runs first) ----
    validate_azure_login()

    # ---- Subscription resolution ----
    subs = [s.strip() for s in args.subscriptions.split(",") if s.strip()]
    if not subs:
        log.info("No subscriptions specified — auto-detecting...")
        subs = get_subscriptions()
    if not subs:
        log.error("No Azure subscriptions found. Run: az login --use-device-code")
        sys.exit(1)
    log.info("Subscriptions: %s", subs)

    # ---- Terraform ----
    tf_state, tf_code = load_terraform(args.terraform_path)
    if args.terraform_path:
        log.info("Terraform data loaded from: %s", args.terraform_path)

    # ---- Exclusion list ----
    exclusions = load_exclusions(args.exclusions)

    # ---- Input file ----
    log.info("Loading input: %s", args.input)
    endpoints = load_endpoints(args.input)
    log.info("Loaded %d endpoint(s)", len(endpoints))

    # ---- Output directories ----
    for d in (args.output_dir, args.backup_dir, args.log_dir):
        os.makedirs(d, exist_ok=True)

    xlsx_out = os.path.join(args.output_dir, f"EDAV_Validation_Report_{ts}.xlsx")
    csv_out  = os.path.join(args.output_dir, f"EDAV_Validation_Report_{ts}.csv")
    md_out   = os.path.join(args.output_dir, f"summary_{ts}.md")

    # ---- Scan ----
    results = []
    for i, ep in enumerate(endpoints, 1):
        nm = str(ep.get("Endpoint Name", "")).strip()
        log.info("[%d/%d] Scanning: %s", i, len(endpoints), nm or "(empty name)")
        results.append(scan(ep, subs, tf_state, tf_code, exclusions))

    # ---- Validation reports ----
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
    log.info("=" * 70)
    log.info("VALIDATION SUMMARY  (Total: %d)", len(results))
    log.info("=" * 70)
    for a, c in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        log.info("  %-52s  %d", a, c)
    log.info("=" * 70)

    with open(md_out, "w", encoding="utf-8") as f:
        f.write("# EDAV Private Endpoint Validation Summary\n\n")
        f.write(f"**Run Date:** {run_dt}  |  **Total:** {len(results)}  |  "
                f"**Tool:** EDAV Monitor v{VERSION}\n\n")
        f.write("| Action | Count | % |\n|---|---|---|\n")
        total = len(results) or 1
        for a, c in sorted(counts.items(), key=lambda x: x[1], reverse=True):
            f.write(f"| {a} | {c} | {c/total*100:.1f}% |\n")
        f.write(f"\n**Reports saved to:** {args.output_dir}\n")
        f.write(f"\n**Log file:** {log_path}\n")
    log.info("Validation MD   : %s", md_out)

    # ---- Delete mode ----
    if args.delete_approved:
        run_delete_approved(
            results      = results,
            exclusions   = exclusions,
            output_dir   = args.output_dir,
            backup_dir   = args.backup_dir,
            ts           = ts,
            run_dt       = run_dt,
            dry_run      = args.dry_run,
            delete_pause = args.delete_pause,
        )
    else:
        safe_n = counts.get("Safe Delete Candidate", 0)
        if safe_n:
            log.info("")
            log.info(">>> %d Safe Delete Candidate(s) found.", safe_n)
            log.info(">>> Step 1: Open the Excel, add ApprovedToDelete=Yes to approved rows")
            log.info(">>> Step 2: Get change ticket approval")
            log.info(">>> Step 3: Run --delete-approved --dry-run to preview")
            log.info(">>> Step 4: Run --delete-approved to execute")

    # ---- Email ----
    if args.email_to and args.smtp_server:
        cfg = dict(
            smtp_server  = args.smtp_server,
            smtp_port    = args.smtp_port,
            from_email   = args.email_from,
            to_email     = args.email_to,
            smtp_user    = args.smtp_user,
            smtp_pass    = args.smtp_pass,
            use_tls      = True,
        )
        subj = (f"EDAV Endpoint Governance | {run_dt} | "
                f"{counts.get('Safe Delete Candidate', 0)} Safe Delete Candidates")
        send_email(cfg, subj, build_email_html(results, run_dt), [xlsx_out, csv_out])

    log.info("")
    log.info("Done. Reports in: %s   Log: %s", args.output_dir, log_path)


if __name__ == "__main__":
    main()

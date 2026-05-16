#!/usr/bin/env python3
"""
EDAV Private Endpoint Monitor v2.1
=====================================
Scans Azure disconnected private endpoints from a CSV/Excel input file,
validates backend resources, checks Terraform ownership, and generates a
professional colour-coded Excel report. Optionally emails the report.
Deletion is approval-gated: nothing is removed unless --delete-approved
flag is used AND the row has an approval column set to an approved value.

Safe by default - Read / Report only.

v2.1 Changes:
- Flexible approval column detection (ApprovedToDelete, Approved To Delete,
  approved_to_delete, approved, delete approved, DeleteApproved, etc.)
- Flexible approval value detection (Yes, YES, yes, Y, y, True, true, 1, Approved, approved)
- Debug logging when --delete-approved is passed
- Fail-safe with clear message when no approved rows are found
"""

import argparse
import json
import logging
import os
import smtplib
import subprocess
import sys
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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("edav-monitor")

# ---------------------------------------------------------------------------
# Style constants
# ---------------------------------------------------------------------------
CLR = {
    "hdr_bg": "1F4E79", "hdr_fg": "FFFFFF",
    "safe_bg": "C6EFCE", "safe_fg": "276221",
    "rev_bg": "FFEB9C", "rev_fg": "9C6500",
    "del_bg": "FFC7CE", "del_fg": "9C0006",
    "na_bg": "D9D9D9", "na_fg": "595959",
}

ACTION_STYLE = {
    "Safe Delete Candidate": ("safe_bg", "safe_fg"),
    "Do Not Delete - Terraform Managed": ("del_bg", "del_fg"),
    "Investigate - Backend Exists": ("rev_bg", "rev_fg"),
    "Review - Not Disconnected": ("rev_bg", "rev_fg"),
    "Review": ("rev_bg", "rev_fg"),
    "Endpoint Not Found / Check Subscription or Access": ("na_bg", "na_fg"),
    "Skipped - Empty Name": ("na_bg", "na_fg"),
}

HEADERS = [
    "Endpoint Name", "Resource Group", "Subscription",
    "Connection State", "Backend Resource", "Backend Exists",
    "Terraform Managed", "Recommended Action", "Notes",
]

COL_ALIASES = {
    "endpoint name": "Endpoint Name", "endpointname": "Endpoint Name",
    "name": "Endpoint Name",
    "resource group": "Resource Group", "resourcegroup": "Resource Group",
    "rg": "Resource Group",
}

# ---------------------------------------------------------------------------
# Approval normalisation helpers
# ---------------------------------------------------------------------------

def normalize_key(value):
    """Normalise a column header for flexible matching."""
    return str(value).replace("\ufeff", "").strip().lower().replace(" ", "").replace("_", "")


def normalize_value(value):
    """Normalise an approval cell value for flexible matching."""
    return str(value).replace("\ufeff", "").strip().lower()


def is_approved(value):
    """Return True if the normalised value represents an approval."""
    return normalize_value(value) in {"yes", "y", "true", "1", "approved"}


def get_approval_value(row):
    """
    Search a CSV row for an approval column using flexible key matching.

    Accepted column names (after normalisation):
        approvedtodelete, approved, deleteapproved, approveddelete, deleteapproval
    """
    approval_keys = [
        "approvedtodelete",
        "approved",
        "deleteapproved",
        "approveddelete",
        "deleteapproval",
    ]
    normalized_row = {normalize_key(k): v for k, v in row.items()}
    for key in approval_keys:
        if key in normalized_row:
            return normalized_row[key]
    return ""


# ---------------------------------------------------------------------------
# Azure CLI helpers
# ---------------------------------------------------------------------------

def _az(args, silent=False):
    cmd = ["az"] + args + ["-o", "json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


def get_subscriptions():
    subs = _az(["account", "list", "--query", "[].name"])
    return list(subs) if subs else []


def set_subscription(name):
    r = subprocess.run(
        ["az", "account", "set", "--subscription", name],
        capture_output=True, text=True, timeout=15)
    return r.returncode == 0


def get_private_endpoint(name, rg):
    if rg:
        return _az(["network", "private-endpoint", "show",
                    "--name", name, "--resource-group", rg], silent=True)
    return None


def resource_exists(rid):
    if not rid:
        return False
    return _az(["resource", "show", "--ids", rid], silent=True) is not None


# ---------------------------------------------------------------------------
# Terraform helpers
# ---------------------------------------------------------------------------

def load_terraform(path):
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


def in_terraform(name, state, code):
    if not state and not code:
        return "Unknown"
    n = name.lower()
    if n in state.lower() or n in code.lower():
        return "Yes"
    return "No"


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

def decide(conn_state, backend_exists, tf_managed):
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


# ---------------------------------------------------------------------------
# Input reader
# ---------------------------------------------------------------------------

def load_endpoints(path):
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
        alias = (COL_ALIASES.get(col.strip().lower().replace(" ", "")) or
                 COL_ALIASES.get(col.strip().lower()))
        if alias and col != alias:
            rename[col] = alias
    df.rename(columns=rename, inplace=True)
    if "Endpoint Name" not in df.columns:
        log.error("No Endpoint Name column. Found: %s", list(df.columns))
        sys.exit(1)
    if "Resource Group" not in df.columns:
        df["Resource Group"] = ""
    return df.to_dict(orient="records")


# ---------------------------------------------------------------------------
# Core scan
# ---------------------------------------------------------------------------

def scan(ep, subscriptions, tf_state, tf_code):
    name = str(ep.get("Endpoint Name", "")).strip()
    rg = str(ep.get("Resource Group", "")).strip()

    # Preserve the raw approval value from whatever column it came from
    raw_approval = get_approval_value(ep)

    rec = {
        "Endpoint Name": name,
        "Resource Group": rg,
        "Subscription": "",
        "Connection State": "Not Found",
        "Backend Resource": "",
        "Backend Exists": "Unknown",
        "Terraform Managed": "Unknown",
        "Recommended Action": "",
        "Notes": "",
        "ApprovedToDelete": raw_approval,
    }
    if not name:
        rec["Recommended Action"] = "Skipped - Empty Name"
        return rec

    for sub in subscriptions:
        if not set_subscription(sub):
            continue
        pe = get_private_endpoint(name, rg)
        if pe is None and not rg:
            all_pe = _az(["network", "private-endpoint", "list",
                          "--query", f"[?name=='{name}']"], silent=True)
            if all_pe:
                pe = all_pe[0]
                rec["Resource Group"] = pe.get("resourceGroup", rg)
        if pe:
            rec["Subscription"] = sub
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
        rec["Connection State"] = "Endpoint Not Found"
        rec["Notes"] = "Not found in any scanned subscription."
        rec["Recommended Action"] = "Endpoint Not Found / Check Subscription or Access"
        return rec

    rec["Terraform Managed"] = in_terraform(name, tf_state, tf_code)
    action, notes = decide(rec["Connection State"],
                           rec["Backend Exists"],
                           rec["Terraform Managed"])
    rec["Recommended Action"] = action
    rec["Notes"] = notes
    return rec


# ---------------------------------------------------------------------------
# Excel report builder
# ---------------------------------------------------------------------------

def _fill(c): return PatternFill("solid", fgColor=c)
def _font(c, bold=False): return Font(color=c, bold=bold, name="Calibri", size=10)
def _border():
    s = Side(style="thin", color="BFBFBF")
    return Border(left=s, right=s, top=s, bottom=s)


def build_excel(results, path, run_date):
    wb = Workbook()

    # --- Summary sheet ---
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = "EDAV Disconnected Private Endpoint Cleanup Report"
    ws["A1"].font = Font(name="Calibri", size=16, bold=True, color=CLR["hdr_bg"])
    ws.merge_cells("A1:C1")
    ws["A2"] = f"Generated: {run_date}"
    ws["A2"].font = Font(name="Calibri", size=10, italic=True, color="595959")
    ws.merge_cells("A2:C2")

    counts = {}
    for r in results:
        a = r.get("Recommended Action", "Review")
        counts[a] = counts.get(a, 0) + 1

    ws["A4"] = "Recommended Action"
    ws["B4"] = "Count"
    for cell in [ws["A4"], ws["B4"]]:
        cell.fill = _fill(CLR["hdr_bg"])
        cell.font = _font(CLR["hdr_fg"], bold=True)

    row = 5
    for action, count in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        style = ACTION_STYLE.get(action, ("na_bg", "na_fg"))
        bg, fg = CLR[style[0]], CLR[style[1]]
        ws.cell(row=row, column=1, value=action).fill = _fill(bg)
        ws.cell(row=row, column=1).font = _font(fg)
        ws.cell(row=row, column=2, value=count).fill = _fill(bg)
        ws.cell(row=row, column=2).font = _font(fg)
        row += 1

    ws.column_dimensions["A"].width = 52
    ws.column_dimensions["B"].width = 10

    # --- All Endpoints sheet ---
    ws2 = wb.create_sheet("All Endpoints")
    for ci, h in enumerate(HEADERS, 1):
        c = ws2.cell(row=1, column=ci, value=h)
        c.fill = _fill(CLR["hdr_bg"])
        c.font = _font(CLR["hdr_fg"], bold=True)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = _border()

    for ri, rec in enumerate(results, 2):
        action = rec.get("Recommended Action", "")
        style = ACTION_STYLE.get(action, ("na_bg", "na_fg"))
        bg, fg = CLR[style[0]], CLR[style[1]]
        for ci, h in enumerate(HEADERS, 1):
            c = ws2.cell(row=ri, column=ci, value=rec.get(h, ""))
            c.fill = _fill(bg)
            c.font = _font(fg)
            c.border = _border()
            c.alignment = Alignment(wrap_text=True, vertical="top")

    col_w = [42, 26, 30, 20, 60, 14, 18, 45, 60]
    for i, w in enumerate(col_w, 1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    ws2.freeze_panes = "A2"
    ws2.auto_filter.ref = ws2.dimensions

    # --- Safe Delete sheet ---
    ws3 = wb.create_sheet("Safe Delete Candidates")
    for ci, h in enumerate(HEADERS, 1):
        c = ws3.cell(row=1, column=ci, value=h)
        c.fill = _fill(CLR["hdr_bg"])
        c.font = _font(CLR["hdr_fg"], bold=True)
        c.alignment = Alignment(horizontal="center")
        c.border = _border()
    dr = 2
    for rec in results:
        if rec.get("Recommended Action") == "Safe Delete Candidate":
            for ci, h in enumerate(HEADERS, 1):
                c = ws3.cell(row=dr, column=ci, value=rec.get(h, ""))
                c.fill = _fill(CLR["safe_bg"])
                c.font = _font(CLR["safe_fg"])
                c.border = _border()
            dr += 1
    ws3.freeze_panes = "A2"

    wb.save(path)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def build_email_html(results, run_date):
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
        "<h2 style='color:#1F4E79'>EDAV Private Endpoint Cleanup Report</h2>"
        f"<p><strong>Run Date:</strong> {run_date} | "
        f"<strong>Total Scanned:</strong> {len(results)}</p>"
        "<table style='border-collapse:collapse;margin-top:10px'>"
        "<thead><tr style='background:#1F4E79;color:#fff'>"
        "<th style='padding:8px 16px;text-align:left'>Recommended Action</th>"
        "<th style='padding:8px 16px'>Count</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
        f"<br><p style='color:#276221;font-weight:bold'>"
        f"Safe Delete Candidates ready for decommission: {safe}</p>"
        "<p>Full validation report is attached. "
        "<em>No endpoints have been deleted by this run.</em></p>"
        "<p style='color:#888;font-size:11px'>EDAV Private Endpoint Monitor v2.1</p>"
        "</body></html>"
    )


def send_email(cfg, subject, body, attachments):
    req = ("smtp_server", "smtp_port", "from_email", "to_email")
    if any(not cfg.get(k) for k in req):
        log.warning("Incomplete email config - skipping.")
        return
    msg = MIMEMultipart("mixed")
    msg["From"] = cfg["from_email"]
    msg["To"] = cfg["to_email"]
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html"))
    for fp in attachments:
        if not os.path.isfile(fp):
            continue
        with open(fp, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition",
                        f"attachment; filename={os.path.basename(fp)}")
        msg.attach(part)
    try:
        with smtplib.SMTP(cfg["smtp_server"], int(cfg["smtp_port"]), timeout=15) as s:
            if cfg.get("use_tls", True):
                s.starttls()
            if cfg.get("smtp_user") and cfg.get("smtp_pass"):
                s.login(cfg["smtp_user"], cfg["smtp_pass"])
            s.sendmail(cfg["from_email"],
                       [e.strip() for e in cfg["to_email"].split(",")],
                       msg.as_string())
        log.info("Email sent to %s", cfg["to_email"])
    except Exception as e:
        log.error("Email failed: %s", e)


# ---------------------------------------------------------------------------
# Delete (approval-gated)
# ---------------------------------------------------------------------------

def delete_endpoint(name, rg, sub):
    set_subscription(sub)
    r = subprocess.run(
        ["az", "network", "private-endpoint", "delete",
         "--name", name, "--resource-group", rg, "--yes"],
        capture_output=True, text=True, timeout=60)
    return r.returncode == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="EDAV Private Endpoint Monitor v2.1 -- Scan, Validate, Report")
    p.add_argument("--input", required=True,
                   help="Path to CSV or Excel input file")
    p.add_argument("--subscriptions", default="",
                   help="Comma-separated Azure subscription names")
    p.add_argument("--terraform-path", default="",
                   help="Path to local Terraform repo (optional)")
    p.add_argument("--output-dir", default="reports",
                   help="Output directory for reports (default: reports/)")
    p.add_argument("--delete-approved", action="store_true", default=False,
                   help="Delete rows approved for deletion. USE ONLY AFTER CHANGE TICKET APPROVAL.")
    p.add_argument("--email-to", default="", help="Recipient email(s), comma-separated")
    p.add_argument("--email-from", default="", help="Sender email address")
    p.add_argument("--smtp-server", default="", help="SMTP server hostname")
    p.add_argument("--smtp-port", default="587", help="SMTP port (default 587)")
    p.add_argument("--smtp-user", default="", help="SMTP username (if auth required)")
    p.add_argument("--smtp-pass", default="", help="SMTP password (if auth required)")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    run_dt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.info("=" * 60)
    log.info("EDAV Private Endpoint Monitor v2.1")
    log.info("Run: %s", run_dt)
    log.info("=" * 60)

    subs = [s.strip() for s in args.subscriptions.split(",") if s.strip()]
    if not subs:
        log.info("No subscriptions specified - auto-detecting...")
        subs = get_subscriptions()
        if not subs:
            log.error("No subscriptions found. Run: az login --use-device-code")
            sys.exit(1)
    log.info("Subscriptions: %s", subs)

    tf_state, tf_code = load_terraform(args.terraform_path)
    if args.terraform_path:
        log.info("Terraform data loaded from: %s", args.terraform_path)

    log.info("Loading input: %s", args.input)
    endpoints = load_endpoints(args.input)
    log.info("Loaded %d endpoints", len(endpoints))

    os.makedirs(args.output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    xlsx_out = os.path.join(args.output_dir, f"EDAV_Validation_Report_{ts}.xlsx")
    csv_out  = os.path.join(args.output_dir, f"EDAV_Validation_Report_{ts}.csv")
    md_out   = os.path.join(args.output_dir, f"summary_{ts}.md")

    results = []
    for i, ep in enumerate(endpoints, 1):
        nm = str(ep.get("Endpoint Name", "")).strip()
        log.info("[%d/%d] %s", i, len(endpoints), nm or "(empty)")
        results.append(scan(ep, subs, tf_state, tf_code))

    # Save outputs
    df_out = pd.DataFrame(results, columns=HEADERS + ["ApprovedToDelete"])
    df_out.to_csv(csv_out, index=False)
    log.info("CSV : %s", csv_out)
    build_excel(results, xlsx_out, run_dt)
    log.info("Excel: %s", xlsx_out)

    counts = {}
    for r in results:
        a = r.get("Recommended Action", "Review")
        counts[a] = counts.get(a, 0) + 1

    log.info("")
    log.info("=" * 60)
    log.info("SUMMARY (Total: %d)", len(results))
    log.info("=" * 60)
    for a, c in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        log.info("  %-50s %d", a, c)
    log.info("=" * 60)

    with open(md_out, "w") as f:
        f.write("# EDAV Private Endpoint Validation Summary\n\n")
        f.write(f"**Run Date:** {run_dt} | **Total:** {len(results)}\n\n")
        f.write("| Action | Count |\n|---|---|\n")
        for a, c in sorted(counts.items(), key=lambda x: x[1], reverse=True):
            f.write(f"| {a} | {c} |\n")
        f.write(f"\n**Reports saved to:** {args.output_dir}\n")
    log.info("MD  : %s", md_out)

    # ------------------------------------------------------------------
    # Deletion (approval-gated)
    # ------------------------------------------------------------------
    if args.delete_approved:
        # --- Debug: show what approval values were detected ---
        all_approval_values = [get_approval_value(r) for r in results]
        approved_rows = [r for r in results
                         if r.get("Recommended Action") == "Safe Delete Candidate"
                         and is_approved(get_approval_value(r))]
        skipped_rows  = [r for r in results
                         if r.get("Recommended Action") == "Safe Delete Candidate"
                         and not is_approved(get_approval_value(r))]

        # Detect which approval column name was actually found in the CSV
        detected_col = "not detected"
        if endpoints:
            sample_row = endpoints[0]
            approval_keys_norm = [
                "approvedtodelete", "approved", "deleteapproved",
                "approveddelete", "deleteapproval",
            ]
            norm_sample = {normalize_key(k): k for k in sample_row.keys()}
            for ak in approval_keys_norm:
                if ak in norm_sample:
                    detected_col = norm_sample[ak]
                    break

        log.info("")
        log.info("=" * 60)
        log.info("DELETE MODE - Approval Debug")
        log.info("=" * 60)
        log.info("  Detected approval column : %s", detected_col)
        log.info("  Total approved rows      : %d", len(approved_rows))
        log.info("  Total skipped rows       : %d", len(skipped_rows))
        log.info("  First 5 approval values  : %s",
                 [str(v) for v in all_approval_values[:5]])
        log.info("=" * 60)

        if not approved_rows:
            log.error("")
            log.error("No approved rows found. Check approval column/value formatting.")
            log.error("")
            log.error("CSV columns found in input:")
            if endpoints:
                for col in endpoints[0].keys():
                    log.error("  -> %r  (normalised: %r)", col, normalize_key(col))
            log.error("")
            log.error("First 5 approval-related values detected:")
            for val in all_approval_values[:5]:
                log.error("  raw=%r  normalised=%r  is_approved=%s",
                          val, normalize_value(val), is_approved(val))
            log.error("")
            log.error("Accepted approval values: yes, y, true, 1, approved")
            log.error("Accepted column names   : ApprovedToDelete, Approved To Delete,")
            log.error("                          approved_to_delete, approved,")
            log.error("                          delete approved, DeleteApproved")
            sys.exit(1)

        log.warning(">>> DELETION MODE: %d endpoint(s) queued.", len(approved_rows))
        confirm = input(f"\nType CONFIRM to delete {len(approved_rows)} endpoint(s): ")
        if confirm.strip() != "CONFIRM":
            log.info("Deletion cancelled.")
        else:
            for rec in approved_rows:
                nm  = rec["Endpoint Name"]
                rg  = rec["Resource Group"]
                sub = rec["Subscription"]
                log.info("Deleting %s ...", nm)
                ok = delete_endpoint(nm, rg, sub)
                log.info("  -> %s", "SUCCESS" if ok else "FAILED")
    else:
        safe_n = counts.get("Safe Delete Candidate", 0)
        if safe_n:
            log.info("")
            log.info(">>> %d Safe Delete Candidate(s) found.", safe_n)
            log.info(">>> Open the Excel, set ApprovedToDelete=Yes on approved rows,")
            log.info(">>> then re-run with --delete-approved after change ticket approval.")

    # Email
    if args.email_to and args.smtp_server:
        cfg = dict(
            smtp_server=args.smtp_server, smtp_port=args.smtp_port,
            from_email=args.email_from, to_email=args.email_to,
            smtp_user=args.smtp_user, smtp_pass=args.smtp_pass,
            use_tls=True,
        )
        subj = (f"EDAV Endpoint Cleanup | {run_dt} | "
                f"{counts.get('Safe Delete Candidate', 0)} Safe Delete Candidates")
        send_email(cfg, subj, build_email_html(results, run_dt), [xlsx_out, csv_out])

    log.info("Done. Open the Excel report for full details.")


if __name__ == "__main__":
    main()

# EDAV Private Endpoint Monitor  v3.0

> **Enterprise Azure Governance and Cleanup Platform** — Identifies disconnected private
> endpoints, validates backend resources and Terraform ownership, generates colour-coded
> Excel/CSV/Markdown reports, creates ARM JSON backups, produces rollback instructions,
> and safely decommissions approved endpoints with a full audit trail.
>
> **Nothing is deleted without explicit approval, a change request, and a typed CONFIRM.**

---

## Table of Contents

1. [What This Tool Does](#what-this-tool-does)
2. [Why This Is Safe](#why-this-is-safe)
3. [Architecture Overview](#architecture-overview)
4. [Prerequisites](#prerequisites)
5. [Setup](#setup)
6. [Azure Login and Least-Privilege RBAC](#azure-login-and-least-privilege-rbac)
7. [Exclusion List](#exclusion-list)
8. [How to Run — Read-Only Validation](#how-to-run)
9. [How to Run — Dry-Run Mode](#how-to-run--dry-run-mode)
10. [How to Prepare the Approved CSV](#how-to-prepare-the-approved-csv)
11. [How to Run — Live Deletion](#how-to-run--live-deletion)
12. [Delete Workflow Step-by-Step](#delete-workflow-step-by-step)
13. [Output Files Reference](#output-files-reference)
14. [Rollback Procedure](#rollback-procedure)
15. [Understanding the Excel Report](#understanding-the-excel-report)
16. [Governance and Approval Process](#governance-and-approval-process)
17. [Scheduling and Automation](#scheduling-and-automation)
18. [Troubleshooting](#troubleshooting)
19. [File Structure](#file-structure)
20. [What To Say to Your Boss](#what-to-say-to-your-boss)

---

## What This Tool Does

Your EDAV Resource Monitor (Critical > Network findings) is showing a large number of
**disconnected private endpoints** costing money every month. This tool:

1. **Validates Azure login** before doing anything — fails immediately with clear instructions if not logged in
2. Reads your CSV/Excel list of endpoints from EDAV
3. Scans each endpoint across one or more subscriptions using Azure CLI
4. Checks whether the **backend resource** (Key Vault, Storage, SQL, etc.) still exists
5. Checks whether the endpoint is **Terraform-managed** to prevent accidental deletion
6. Checks an **exclusion list** (`exclusions.txt`) — listed endpoints are never deleted
7. Produces a professional **colour-coded Excel report** with four tabs:
   - Summary (counts + percentages by action)
   - All Endpoints (full detail with auto-filters)
   - Safe Delete Candidates (filtered clean view)
   - Excluded (endpoints protected by exclusions.txt)
8. Writes a **structured log file** to `logs/` for every run
9. Optionally **emails** the report
10. In delete mode: exports **full ARM JSON backups** before every deletion
11. In delete mode: generates **rollback instructions** automatically
12. Allows deletion **only** after: change ticket approval + `ApprovedToDelete=Yes` in CSV + `--delete-approved` flag + typed `CONFIRM` prompt

---

## Why This Is Safe

| Safety Control | How It Works |
|---|---|
| Read-only by default | Nothing is modified unless `--delete-approved` is explicitly passed |
| Azure login check | Runs `az account show` first — exits immediately with instructions if not logged in |
| Approval gate | Rows must have `ApprovedToDelete=Yes` — any other value is skipped |
| CONFIRM prompt | User must type exactly `CONFIRM` — any other input cancels everything |
| Dry-run mode | `--dry-run` simulates the entire workflow, generates a report, touches nothing in Azure |
| Exclusion list | Any endpoint in `exclusions.txt` is permanently excluded from deletion |
| Terraform protection | Terraform-managed endpoints are automatically blocked |
| Pre-delete re-validation | Verifies endpoint still exists AND is still Disconnected immediately before delete |
| Subscription verification | Confirms correct subscription context before each delete |
| ARM backup | Full ARM JSON exported to `backups/private_endpoints/` before every real delete |
| Rollback instructions | `rollback_instructions.md` generated automatically after every delete run |
| Retry protection | All Azure CLI calls use 3-attempt exponential-backoff retry (2s, 4s, 8s) |
| Sequential execution | Deletes run one at a time, grouped by subscription then RG, with configurable pause |
| Only one delete command | The ONLY Azure delete command ever issued is `az network private-endpoint delete` |
| Structured logging | Every action logged to `logs/EDAV_DELETE_YYYYMMDD_HHMMSS.log` |

> **WARNING: A Change Request must be approved BEFORE running `--delete-approved`.**
> Deletions are permanent and cannot be undone automatically.

---

## Architecture Overview

```
main.py
|
├── validate_azure_login()          <- Always first. Fails fast if not logged in.
├── load_exclusions()               <- Reads exclusions.txt
├── load_endpoints()                <- Reads CSV/Excel input
|
├── scan() [per endpoint]           <- Read-only Azure queries
|   ├── get_private_endpoint()
|   ├── resource_exists()
|   └── in_terraform()
|
├── build_excel()                   <- Validation report (4-tab XLSX)
|
└── run_delete_approved()           <- Only if --delete-approved
    ├── _is_deletion_blocked()      <- Multi-rule safety gate
    ├── CONFIRM prompt              <- Interactive human gate
    ├── export_endpoint_backup()    <- ARM JSON to backups/
    ├── private_endpoint_still_valid_for_delete()  <- Pre-delete re-check
    ├── verify_subscription_context()
    ├── _execute_delete()           <- THE ONLY delete command
    ├── _write_delete_reports()
    └── generate_rollback()
```

All Azure CLI calls pass through `_az_with_retry()` — 3 attempts with exponential backoff (2s, 4s, 8s).

---

## Prerequisites

### 1. Azure CLI
```
https://aka.ms/installazurecliwindows
az --version
```

### 2. Python 3.10+
```
https://www.python.org/downloads/
python --version
```

### 3. Git (optional)
```
https://git-scm.com/download/win
```

---

## Setup

```powershell
git clone https://github.com/ausjones84/edav-private-endpoint-monitor.git
cd edav-private-endpoint-monitor
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

---

## Azure Login and Least-Privilege RBAC

### Login
```bash
# Interactive browser
az login

# Device-code (recommended for remote/RDP)
az login --use-device-code

# Service principal (for automation/pipelines)
az login --service-principal -u <appId> -p <password> --tenant <tenant>

# Verify
az account show
```

### Least-Privilege RBAC

| Mode | Required Azure Role | Why |
|---|---|---|
| Read-only scan | `Reader` on each subscription | List/show private endpoints and backend resources |
| Delete mode | `Network Contributor` on each subscription | Delete private endpoints only |
| Terraform check | Local filesystem only | No Azure permissions needed |

> **Best Practice:** Use a dedicated service principal with `Reader` for scanning
> and a separate one with `Network Contributor` for deletion. Never use Owner or
> Contributor for routine scanning.

```bash
# Assign Reader for scanning
az role assignment create \
  --assignee <your-object-id> \
  --role "Reader" \
  --scope /subscriptions/<subscription-id>

# Assign Network Contributor for deletion
az role assignment create \
  --assignee <your-object-id> \
  --role "Network Contributor" \
  --scope /subscriptions/<subscription-id>
```

---

## Exclusion List

Create `exclusions.txt` in the same directory as `main.py`. One endpoint name per line. Lines starting with `#` are comments.

```
# exclusions.txt — endpoints that must NEVER be deleted
pe-keyvault-prod-001
pe-storage-dmz-critical
pe-sql-finance-prod
# DMZ boundary endpoints
pe-dmz-boundary-01
```

Excluded endpoints appear on the **Excluded** tab of the Excel report (green) and are logged as "Excluded" in the deletion report.

---

## How to Run

### Read-Only Validation (default, safest)

```powershell
python main.py \
  --input "C:\Users\bh55\Downloads\EDAV_Disconnected_Private_Endpoints_Full_Report.csv" \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1,OCIO-EDAV-DMZ-C1,OCIO-DMZ-C1" \
  --terraform-path "C:\Users\bh55\terraform-scripts"
```

Optional flags:
```powershell
  --output-dir "my-reports"
  --log-dir "my-logs"
  --exclusions "my-exclusions.txt"
  --email-to "boss@cdc.gov" --smtp-server "smtp.cdc.gov"
```

---

## How to Run — Dry-Run Mode

Simulates deletions without touching Azure. **Always run this before a live deletion.**

```powershell
python main.py \
  --input "C:\Users\bh55\Downloads\EDAV_Approved_Private_Endpoint_Deletions.csv" \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1,OCIO-EDAV-DMZ-C1,OCIO-DMZ-C1" \
  --terraform-path "C:\Users\bh55\terraform-scripts" \
  --delete-approved \
  --dry-run
```

Dry-run produces `EDAV_DryRun_Report_*.xlsx/csv` and `dryrun_summary_*.md`. No CONFIRM prompt needed.

---

## How to Prepare the Approved CSV

> **STOP: Get your Change Request approved BEFORE proceeding.**

1. Run read-only scan to generate the validation report
2. Open `reports/EDAV_Validation_Report_YYYYMMDD.xlsx`
3. Review the **Safe Delete Candidates** tab
4. For each endpoint approved in your change ticket, set `ApprovedToDelete = Yes`
5. Leave blank or `No` for everything else
6. Save as CSV: `EDAV_Approved_Private_Endpoint_Deletions.csv`
7. Run dry-run, review output, then run live deletion

**CSV column requirements:**

| Column | Required | Rules |
|---|---|---|
| Endpoint Name | Yes | Exact Azure resource name |
| Resource Group | Yes | Exact Azure resource group name |
| Subscription | Recommended | Subscription name for context switching |
| ApprovedToDelete | Yes | Must be exactly `Yes` (capital Y) |

**Rows automatically skipped:**
- `ApprovedToDelete` blank or `No`
- `Endpoint Name` or `Resource Group` blank
- Recommended Action contains blocking keyword
- Endpoint is in `exclusions.txt`
- Pre-delete: endpoint no longer exists
- Pre-delete: endpoint no longer Disconnected

---

## How to Run — Live Deletion

> **REQUIRES AN APPROVED CHANGE REQUEST.**
> **Run dry-run first.**

```powershell
python main.py \
  --input "C:\Users\bh55\Downloads\EDAV_Approved_Private_Endpoint_Deletions.csv" \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1,OCIO-EDAV-DMZ-C1,OCIO-DMZ-C1" \
  --terraform-path "C:\Users\bh55\terraform-scripts" \
  --delete-approved
```

Optional delete flags:
```powershell
  --delete-pause 5          # Seconds between deletes (default: 2)
  --backup-dir "my-backups" # Custom backup dir (default: backups/)
```

---

## Delete Workflow Step-by-Step

When `--delete-approved` is passed (without `--dry-run`):

```
1.  Validate Azure login
2.  Load input file
3.  Scan all endpoints (real Azure queries — read-only)
4.  Generate validation report (XLSX + CSV + MD)
5.  Screen all rows through safety gate
6.  Display queued endpoints
7.  Prompt: "Type CONFIRM to continue"
    Any other input -> cancel, write reports, exit
8.  For each approved endpoint (grouped sub -> RG, sequential):
    a. Export full ARM JSON to backups/private_endpoints/
    b. Re-validate: endpoint still exists in Azure?
    c. Re-validate: endpoint still Disconnected?
    d. Re-verify subscription context
    e. Run: az network private-endpoint delete --name <n> --resource-group <rg> --yes
    f. Log result (Deleted / Failed / Skipped)
    g. Pause --delete-pause seconds
9.  Write deletion report (XLSX + CSV + MD)
10. Generate rollback_instructions.md
11. Print final summary
```

The **only** Azure delete command ever issued:
```bash
az network private-endpoint delete --name <endpoint-name> --resource-group <rg> --yes
```

**No backend resources are ever touched.**

---

## Output Files Reference

### Validation reports (always generated)

| File | Description |
|---|---|
| `reports/EDAV_Validation_Report_YYYYMMDD_HHMMSS.xlsx` | 4-tab colour-coded Excel |
| `reports/EDAV_Validation_Report_YYYYMMDD_HHMMSS.csv` | Flat CSV |
| `reports/summary_YYYYMMDD_HHMMSS.md` | Markdown summary with percentages |

### Deletion reports (when `--delete-approved` is used)

| File | Description |
|---|---|
| `reports/EDAV_Delete_Report_YYYYMMDD_HHMMSS.xlsx` | Deletion result Excel |
| `reports/EDAV_Delete_Report_YYYYMMDD_HHMMSS.csv` | Deletion result CSV |
| `reports/delete_summary_YYYYMMDD_HHMMSS.md` | Deletion markdown report |
| `reports/rollback_instructions.md` | Step-by-step restore guide |

### Dry-run reports (when `--delete-approved --dry-run`)

| File | Description |
|---|---|
| `reports/EDAV_DryRun_Report_YYYYMMDD_HHMMSS.xlsx` | Simulated result Excel |
| `reports/EDAV_DryRun_Report_YYYYMMDD_HHMMSS.csv` | Simulated result CSV |
| `reports/dryrun_summary_YYYYMMDD_HHMMSS.md` | Dry-run markdown summary |

### Backups

| Path | Description |
|---|---|
| `backups/private_endpoints/<name>_<sub>_<ts>.json` | Full ARM JSON before deletion |

### Logs

| File | Description |
|---|---|
| `logs/EDAV_DELETE_YYYYMMDD_HHMMSS.log` | Structured log, DEBUG to file / INFO to console |

---

## Rollback Procedure

The `rollback_instructions.md` file contains detailed restore steps. Summary:

```bash
# Step 1: Find backup
dir backups\private_endpoints\<endpoint-name>*

# Step 2: Review ARM JSON backup (open in any text editor)

# Step 3: Recreate endpoint
az network private-endpoint create \
  --name <name> --resource-group <rg> \
  --vnet-name <vnet> --subnet <subnet> \
  --private-connection-resource-id <backend-id> \
  --connection-name <conn-name> --group-id <group-id>

# Step 4: Approve connection (backend service owner)
az network private-endpoint-connection approve \
  --resource-name <backend> --resource-group <backend-rg> \
  --name <conn-name> --type Microsoft.KeyVault/vaults

# Step 5: Verify
az network private-endpoint show --name <name> --resource-group <rg> -o table
```

---

## Understanding the Excel Report

### Validation report tabs

| Tab | Contents |
|---|---|
| Summary | Action counts and % with colour coding |
| All Endpoints | Full detail for every endpoint, filterable |
| Safe Delete Candidates | Only Safe Delete Candidate rows |
| Excluded | Endpoints protected by exclusions.txt |

### Colour codes

| Colour | Recommended Action | What To Do |
|---|---|---|
| GREEN | Safe Delete Candidate | Approved for decommission after change ticket |
| RED | Do Not Delete - Terraform Managed | Must be removed from TF code first |
| YELLOW | Investigate - Backend Exists | Backend still active — manual review needed |
| GREY | Endpoint Not Found | Not in scanned subscription — check access |
| BLUE | Excluded | Protected by exclusions.txt — never deleted |

### Deletion report columns

| Column | Description |
|---|---|
| Endpoint Name | Azure private endpoint resource name |
| Resource Group | Resource group |
| Subscription | Azure subscription name |
| Recommended Action | Value from validation scan |
| ApprovedToDelete | Value from input CSV |
| Delete Result | Deleted / Dry-Run / Failed / Skipped / Excluded |
| Timestamp | ISO 8601 timestamp of the attempt |
| Duration (s) | How long the delete operation took |
| Dry Run | True / False |
| Error Message | Reason for failure or skip |

---

## Governance and Approval Process

```
Week 1: Discovery
  Run read-only scan -> Review XLSX with team

Week 2: Review
  Engineer reviews each Safe Delete Candidate
  Add retained endpoints to exclusions.txt permanently

Week 3: Approval
  Submit change request ticket with validation report as evidence
  Get CAB or manager approval
  Set ApprovedToDelete=Yes for approved rows

Week 4: Execution
  Run --delete-approved --dry-run -> review dry-run report
  Confirm dry-run output matches change ticket scope
  During change window: run --delete-approved (LIVE)
  Type CONFIRM
  Attach delete report + rollback_instructions.md to change ticket
```

---

## Scheduling and Automation

### Weekly validation scan (Task Scheduler)

```batch
@echo off
cd /d C:\tools\edav-private-endpoint-monitor
call .venv\Scripts\activate
python main.py ^
  --input "C:\edav-data\latest_endpoints.csv" ^
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1,OCIO-EDAV-DMZ-C1,OCIO-DMZ-C1" ^
  --terraform-path "C:\terraform-scripts" ^
  --email-to "network-team@cdc.gov" ^
  --email-from "automation@cdc.gov" ^
  --smtp-server "smtp.cdc.gov"
```

Save as `run_weekly_scan.bat` and schedule in Task Scheduler every Monday at 08:00.

### Azure DevOps / GitHub Actions

```yaml
- name: EDAV Endpoint Scan
  run: |
    python main.py \
      --input endpoints.csv \
      --subscriptions "${{ vars.AZURE_SUBSCRIPTIONS }}" \
      --output-dir reports
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `AZURE LOGIN REQUIRED` | Run `az login --use-device-code` |
| `az: command not found` | Install Azure CLI from aka.ms/installazurecliwindows |
| `ModuleNotFoundError: pandas` | Run `pip install -r requirements.txt` |
| All endpoints say `Endpoint Not Found` | Wrong subscription — run `az account list -o table` |
| `Terraform not found` | Skip `--terraform-path` — checks show Unknown |
| Nothing deleted | Check `ApprovedToDelete` is exactly `Yes` (capital Y) |
| Endpoint skipped in delete run | Check `logs/` file for the skip reason |
| Cannot set subscription context | Verify you have Reader on that subscription |
| Zscaler / VPN issues | Make sure Zscaler Client Connector is ON with CDC creds |

---

## File Structure

```
edav-private-endpoint-monitor/
|-- main.py              <- Main script (v3.0)
|-- requirements.txt     <- Python dependencies
|-- sample_input.csv     <- Example input format
|-- exclusions.txt       <- Endpoints to never delete (create this)
|-- .gitignore
|-- README.md
|
|-- reports/             <- All output reports (auto-created)
|   |-- EDAV_Validation_Report_*.xlsx
|   |-- EDAV_Validation_Report_*.csv
|   |-- summary_*.md
|   |-- EDAV_Delete_Report_*.*     <- Delete mode
|   |-- EDAV_DryRun_Report_*.*    <- Dry-run mode
|   |-- rollback_instructions.md
|
|-- backups/             <- ARM JSON backups (auto-created)
|   |-- private_endpoints/<endpoint>_<sub>_<ts>.json
|
|-- logs/                <- Structured logs (auto-created)
    |-- EDAV_DELETE_YYYYMMDD_HHMMSS.log
```

---

## What To Say to Your Boss

```
I built an enterprise-grade Azure governance platform for the EDAV disconnected
private endpoint cleanup project.

The tool validates Azure login, performs real-time scanning, checks Terraform
ownership to prevent recreation loops, and generates colour-coded reports showing
exactly what is safe to decommission.

Deletion is gated behind three independent controls: change request approval,
a CSV flag, and a typed CONFIRM prompt. A --dry-run mode lets us simulate the
entire operation before committing.

Before every real deletion, it exports a full ARM JSON backup, then immediately
re-validates the endpoint before issuing the delete command. After the run, it
generates rollback instructions for any team member to follow.

Every run produces a structured log, an Excel deletion report, and a Markdown
summary for the change ticket. The only Azure command it ever issues is
az network private-endpoint delete -- it never touches backend resources.

I can schedule the scan to run weekly and email the report automatically.
```

---

## Author
Built by Austin Jones for EDAV infrastructure cost cleanup at CDC/OCIO.

**Version:** 3.0.0  
**Safe:** Read-only by default. Three-layer approval-gated deletion.  
**Only deletes:** Azure Private Endpoint resources (`az network private-endpoint delete`)  
**Never deletes:** Key Vaults, Storage Accounts, SQL, VNets, NICs, NSGs, DNS zones, or any backend resource

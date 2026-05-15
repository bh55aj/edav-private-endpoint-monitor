# EDAV Private Endpoint Monitor

> **Azure cost-saving automation** — Identifies disconnected private endpoints, validates
> backend resources and Terraform ownership, generates colour-coded Excel reports, and
> optionally emails a summary. **Nothing is deleted without explicit approval.**

---

## What This Tool Does

Your EDAV Resource Monitor (Critical > Network findings) is showing a large number of
**disconnected private endpoints** that are costing money every month. This tool:

1. Reads your CSV/Excel list of endpoints from EDAV
2. Logs into Azure using your SU account (no credentials stored)
3. Scans each endpoint across one or more subscriptions
4. Checks whether the **backend resource** (Key Vault, Storage, SQL, etc.) still exists
5. Checks whether the endpoint is **managed by Terraform** (so you dont accidentally delete it)
6. Produces a professional **colour-coded Excel report** with three tabs:
   - Summary (counts by action)
   - All Endpoints (full detail)
   - Safe Delete Candidates (filtered view)
7. Optionally **emails the report** to you and your boss
8. Allows deletion **only** after you manually mark rows `ApprovedToDelete=Yes`, pass the `--delete-approved` flag, **and** type `CONFIRM` at the prompt

---

## Why This Is Safe

- **Read-only by default** — no Azure resources are modified on a normal run
- **Three layers of deletion protection:** the `--delete-approved` flag AND `ApprovedToDelete=Yes` in the CSV AND a typed `CONFIRM` prompt
- **Only Azure Private Endpoint resources are ever deleted** — backend resources such as Key Vaults, Storage Accounts, SQL servers, Databricks, App Services, VNets, NICs, private DNS zones, and all other resources are **never touched**
- No credentials are stored anywhere — uses the active `az login` session
- Terraform-managed endpoints are automatically blocked from deletion
- Endpoints with live backend resources are flagged INVESTIGATE, not deleted
- A pre-deletion re-check confirms each endpoint still exists in Azure before the delete command is issued

> **⚠ WARNING: A Change Request must be approved before running --delete-approved.**
> Do not use the delete mode without an approved change ticket. Deletions cannot be undone.

---

## Prerequisites (Install These First)

### 1. Azure CLI
Download and install from: https://aka.ms/installazurecliwindows

Verify it works:
```
az --version
```

### 2. Python 3.10+
Download from: https://www.python.org/downloads/

Verify:
```
python --version
```

### 3. Git (optional, for cloning)
Download from: https://git-scm.com/download/win

---

## Setup — Do This Once

### Step 1: Get the code

Option A — Clone (if you have Git):
```
git clone https://github.com/ausjones84/edav-private-endpoint-monitor.git
cd edav-private-endpoint-monitor
```

Option B — Download ZIP from GitHub, unzip it, open a PowerShell window inside the folder.

### Step 2: Create a virtual environment
```
python -m venv .venv
.venv\Scripts\activate
```

You should see `(.venv)` appear at the start of your prompt.

### Step 3: Install dependencies
```
pip install -r requirements.txt
```

### Step 4: Login to Azure with your SU account
```
az login --use-device-code
```
It will give you a code. Go to https://microsoft.com/devicelogin, enter the code,
and sign in with your CDC SU account.

Then set the subscription you want to scan:
```
az account set --subscription "OCIO-TSBDEV-C1"
```

Verify you are in:
```
az account show
```

---

## How To Run

### Read-only validation (safest option — default mode)
```
python main.py --input "C:\Users\bh55\Downloads\EDAV_Disconnected_Private_Endpoints_Full_Report.csv" --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1,OCIO-EDAV-DMZ-C1,OCIO-DMZ-C1" --terraform-path "C:\Users\bh55\terraform-scripts"
```

### Delete approved endpoints (requires --delete-approved flag + CONFIRM prompt)
```
python main.py --input "C:\Users\bh55\Downloads\EDAV_Approved_Private_Endpoint_Deletions.csv" --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1,OCIO-EDAV-DMZ-C1,OCIO-DMZ-C1" --terraform-path "C:\Users\bh55\terraform-scripts" --delete-approved
```

---

## How To Prepare the Approved CSV for Deletion

> ⚠ **A Change Request must be approved before proceeding.**

1. Run the tool in read-only mode first to generate the validation report.
2. Open `reports/EDAV_Validation_Report_YYYYMMDD_HHMMSS.csv` in Excel.
3. Review the **Safe Delete Candidates** rows.
4. For each endpoint approved in your change ticket, add `Yes` in the `ApprovedToDelete` column.
5. Leave **blank** or set to `No` for any endpoint you do not want to delete.
6. Save the file as a CSV (e.g. `EDAV_Approved_Private_Endpoint_Deletions.csv`).
7. Pass this file as the `--input` argument when running with `--delete-approved`.

**Column requirements for the deletion CSV:**

| Column | Required | Description |
|---|---|---|
| Endpoint Name | ✅ Yes | Exact name of the Azure private endpoint |
| Resource Group | ✅ Yes | Resource group containing the endpoint |
| Subscription | Recommended | Subscription name (used to set az context) |
| ApprovedToDelete | ✅ Yes | Must be exactly `Yes` to qualify for deletion |
| Recommended Action | Auto-set | Must NOT contain "Do Not Delete", "Endpoint Not Found", or "Terraform Managed" |

**Rows are automatically skipped (not deleted) if:**
- `ApprovedToDelete` is blank or `No`
- `Endpoint Name` is blank
- `Resource Group` is blank
- `Recommended Action` contains `Do Not Delete`, `Endpoint Not Found`, or `Terraform Managed`
- The endpoint is not found during the pre-deletion existence check

---

## What Happens During --delete-approved

1. The script scans the input file for rows where `ApprovedToDelete=Yes`
2. It pre-screens each row against the blocking rules above
3. It prints the list of endpoints queued for deletion
4. It asks you to type `CONFIRM` — **if you do not type it exactly, nothing is deleted**
5. For each approved endpoint, it re-checks that the endpoint still exists in Azure
6. It runs **only** this command per endpoint:
   ```
   az network private-endpoint delete --name <endpoint-name> --resource-group <resource-group> --yes
   ```
7. **No other resources are touched.** Backend resources (Key Vaults, Storage Accounts, SQL, Databricks, App Services, VNets, NICs, private DNS zones) are never modified or deleted.
8. A full deletion report is saved to the `reports/` folder

---

## Output Files

### Validation report (always generated)
- `reports/EDAV_Validation_Report_YYYYMMDD_HHMMSS.xlsx` — colour-coded Excel
- `reports/EDAV_Validation_Report_YYYYMMDD_HHMMSS.csv` — plain CSV
- `reports/summary_YYYYMMDD_HHMMSS.md` — markdown summary

### Deletion report (only generated when --delete-approved is used)
- `reports/EDAV_Delete_Report_YYYYMMDD_HHMMSS.xlsx` — colour-coded delete result Excel
- `reports/EDAV_Delete_Report_YYYYMMDD_HHMMSS.csv` — deletion result CSV
- `reports/delete_summary_YYYYMMDD_HHMMSS.md` — deletion markdown summary

The deletion report includes: Endpoint Name, Resource Group, Subscription, Recommended Action, ApprovedToDelete, Delete Result (Deleted / Failed / Skipped), and Error Message.

---

## Understanding the Validation Excel Report

| Colour | Recommended Action | What To Do |
|---|---|---|
| GREEN | Safe Delete Candidate | Safe to decommission after change ticket approval |
| RED | Do Not Delete - Terraform Managed | Do NOT touch — must be removed from Terraform code first |
| YELLOW | Investigate - Backend Exists | Endpoint is disconnected but backend resource still exists — review |
| GREY | Endpoint Not Found | Not found in scanned subscription — try another subscription |

---

## What To Say to Your Boss / In Standup

```
I built a repeatable validation pipeline for the EDAV disconnected private endpoint cleanup.
The tool reads the EDAV CSV, scans Azure using my SU account, validates each endpoint
against its backend resource, checks Terraform ownership to prevent recreation issues,
and generates a colour-coded report showing what is safe to decommission.
Deletion is gated behind change ticket approval - nothing is removed automatically.
Only the Azure Private Endpoint resource is deleted - no backend resources are touched.
I can schedule this to run weekly and email the report automatically.
```

---

## Windows Azure CLI Note

The script automatically finds `az` using the following logic:
1. Checks your system PATH with `shutil.which("az")`
2. If not found, falls back to the Windows MSI install location:
   `C:\Program Files (x86)\Microsoft SDKs\Azure\CLI2\wbin\az.cmd`
3. If still not found, exits with a clear error message

This means it works whether you installed Azure CLI via winget, the MSI installer, or added it to PATH manually.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `az: command not found` | Install Azure CLI from aka.ms/installazurecliwindows |
| `Not logged in` | Run `az login --use-device-code` |
| `ModuleNotFoundError: pandas` | Run `pip install -r requirements.txt` |
| All endpoints say `Endpoint Not Found` | Wrong subscription. Run `az account list -o table` and switch |
| `Terraform not found` | Skip `--terraform-path` — Terraform checks show Unknown |
| Cannot access EDAV dashboard | Make sure Zscaler Client Connector is ON with CDC creds |
| Nothing deleted after --delete-approved | Check that `ApprovedToDelete` is exactly `Yes` (capital Y) in the CSV |

---

## File Structure

```
edav-private-endpoint-monitor/
|-- main.py              <- The main script (run this)
|-- requirements.txt     <- Python packages to install
|-- sample_input.csv     <- Example input file format
|-- .gitignore           <- Keeps reports and data files out of git
|-- README.md            <- This file
|-- reports/             <- Output folder (reports saved here)
```

---

## Author
Built by Austin Jones for EDAV infrastructure cost cleanup at CDC/OCIO.

**Version:** 2.1.0
**Safe:** Read-only by default. Approval-gated deletion only.
**Only deletes:** Azure Private Endpoint resources (`az network private-endpoint delete`).
**Never deletes:** Key Vaults, Storage Accounts, SQL, VNets, NICs, DNS zones, or any backend resource.

# EDAV Private Endpoint Cleanup Process

**Version:** v7.0.0 | EDAV Platform Team | June 2026

This document describes the end-to-end process for safely discovering, validating,
approving, and removing disconnected private endpoints identified in the EDAV
Resource Monitor.

---

## Overview

The EDAV Resource Monitor has identified approximately **93 disconnected private
endpoints** across four subscriptions. These endpoints have no active backend
connection and are generating unnecessary Azure costs (~$7.30/endpoint/month,
totalling ~$679/month or ~$8,151/year).

The cleanup tool automates the full workflow:

```
Resource Monitor Export (DisconnectedPEs.xlsx)
         |
         v
   [1] Import  --> Read all disconnected PE findings
         |
         v
   [2] Validate --> Check each endpoint against live Azure
         |         (exists? connected? locked? backend alive?)
         v
   [3] Classify --> SAFE_DELETE | REVIEW_REQUIRED | ALREADY_REMOVED | KEEP
         |
         v
   [4] Preview  --> Generate deletion plan (HTML + Markdown)
         |         NO DELETIONS at this stage
         v
   [5] Approve  --> Collect ApprovalTicket + ApprovedBy
         |
         v
   [6] Dry-Run  --> Simulate all deletions
         |
         v
   [7] Delete   --> Delete SAFE_DELETE endpoints (SU account)
         |         Individual backup -> delete -> verify per endpoint
         v
   [8] Report   --> Excel, HTML, Markdown, JSON, CSV
```

---

## Prerequisites

- Python 3.9+
- Azure CLI (`az` >= 2.84.0)
- Azure CLI authenticated as SU account (`bh55-su@cdc.gov`) for deletion
- `pip install openpyxl pandas` (for Excel reports)
- Approved ITSM change ticket (e.g., CHG0001234)
- Approval from Linda Johnson or designated approver

---

## Step 1: Export from EDAV Resource Monitor

Export the Resource Monitor findings to Excel:

1. Open the EDAV Resource Monitor dashboard
2. Filter by **Check = disconnected_private_endpoints**
3. Export to Excel: `DisconnectedPEs.xlsx`
4. Save to your working directory

The tool reads this file as the **authoritative source** of endpoints to review.
All subsequent steps are driven by this export.

---

## Step 2: Preview Cleanup (Recommended First Step)

Before doing anything else, run the preview to see exactly what will be deleted:

```bash
python main.py --cleanup-private-endpoints \
  --import-resource-monitor DisconnectedPEs.xlsx \
  --preview-cleanup \
  --output-dir reports/cleanup/
```

**This generates:**
- `reports/cleanup/PE_Preview_Cleanup_<timestamp>.md` — Markdown plan
- `reports/cleanup/PE_Preview_Cleanup_<timestamp>.html` — HTML dashboard

**The preview report shows:**
- Which endpoints will be deleted and why
- The owning team for each endpoint
- Estimated monthly and yearly cost savings
- Any blockers (locks, dependencies, missing approvals)
- Endpoints that require additional review

> Share this report with Linda and Brock for sign-off before proceeding.

---

## Step 3: Validate Only (Optional Deep Check)

Run full Azure validation without deleting:

```bash
python main.py --cleanup-private-endpoints \
  --import-resource-monitor DisconnectedPEs.xlsx \
  --validate-only \
  --output-dir reports/cleanup/
```

This checks every endpoint against live Azure:
- Does it still exist?
- What is its live connection state?
- Does the backend resource exist?
- Is there a resource lock?

Generates full Excel + HTML + Markdown + JSON + CSV reports.

---

## Step 4: Collect Approvals

For endpoints classified as `SAFE_DELETE`:

1. Open the generated Excel file (`SAFE_DELETE` tab)
2. Fill in the approval columns:
   - `ApprovedToDelete` = `Yes`
   - `ApprovalTicket` = `CHG0001234` (your ITSM ticket)
   - `ApprovedBy` = `Linda Johnson` (or designated approver)
3. Save as `DisconnectedPEs_Approved.xlsx`

Or pass the ticket and approver directly via CLI:

```bash
python main.py --cleanup-private-endpoints \
  --import-resource-monitor DisconnectedPEs_Approved.xlsx \
  --delete-approved \
  --change-ticket CHG0001234 \
  --approved-by "Linda Johnson"
```

---

## Step 5: Dry Run

Always dry-run before live deletion:

```bash
python main.py --cleanup-private-endpoints \
  --import-resource-monitor DisconnectedPEs_Approved.xlsx \
  --dry-run \
  --delete-approved \
  --change-ticket CHG0001234 \
  --approved-by "Linda Johnson" \
  --output-dir reports/cleanup/
```

Review the dry-run output carefully before proceeding.

---

## Step 6: SU Account Login

Live deletion requires the SU account:

```bash
# Log out standard account
az logout

# Log in with SU account (device code for CDC VDI)
az login --use-device-code

# Verify identity
az account show --query user -o table
# Must show: bh55-su@cdc.gov
```

---

## Step 7: Live Delete

```bash
python main.py --cleanup-private-endpoints \
  --import-resource-monitor DisconnectedPEs_Approved.xlsx \
  --delete-approved \
  --change-ticket CHG0001234 \
  --approved-by "Linda Johnson" \
  --required-user bh55-su@cdc.gov \
  --output-dir reports/cleanup/
```

**The tool will:**
1. Import all findings from the Excel file
2. Validate each endpoint against Azure
3. Show the deletion plan
4. Prompt: `Type YES to proceed with deletion`
5. For each approved endpoint:
   - Back up ARM JSON to `backups/`
   - Delete via Azure CLI (`az network private-endpoint delete`)
   - Verify deletion (ResourceNotFound confirmation)
   - Log result
6. Generate final reports

---

## Step 8: Post-Delete Verification

Run the Resource Graph verification query:

```bash
az graph query -q "
Resources
| where type =~ 'microsoft.network/privateendpoints'
| mv-expand connections = properties.privateLinkServiceConnections
| extend connectionState = tostring(connections.properties.privateLinkServiceConnectionState.status)
| where isnull(connectionState) or connectionState !in~ ('Approved','Connected')
| project name, resourceGroup, subscriptionId, connectionState
| order by resourceGroup asc
" --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1,OCIO-EDAV-DMZ-DEV-C1,OCIO-EDAV-DMZ-PRD-C1"
```

The count should be lower than the pre-cleanup baseline.

---

## Reports Generated

| File | Description |
|------|-------------|
| `PE_Preview_Cleanup_*.md` | Deletion plan (pre-cleanup) |
| `PE_Preview_Cleanup_*.html` | Interactive HTML dashboard |
| `PE_Cleanup_validation_*.xlsx` | Excel (SAFE_DELETE / REVIEW / KEEP tabs) |
| `PE_Cleanup_validation_*.html` | Validation dashboard |
| `PE_Cleanup_post_delete_*.xlsx` | Final cleanup results |
| `PE_Cleanup_post_delete_*.html` | Post-cleanup dashboard |
| `PE_Cleanup_post_delete_*.md` | Executive summary |
| `PE_Cleanup_post_delete_*.json` | Full audit trail |
| `backups/*.json` | ARM JSON backup per deleted endpoint |

---

## Classification Reference

| Classification | Meaning | Action |
|----------------|---------|--------|
| `SAFE_DELETE` | Disconnected, no backend, no lock, no deps | Delete after approval |
| `REVIEW_REQUIRED` | Has blockers (lock / dependency / backend alive) | Manual review |
| `ALREADY_REMOVED` | Not found in Azure (already deleted) | No action |
| `KEEP` | Approved/Connected - endpoint is in use | Do not delete |
| `UNKNOWN` | Azure validation failed | Investigate |

---

## Safety Gates

Deletion ONLY proceeds when ALL of the following are true:

1. `--delete-approved` flag is present
2. Azure CLI user matches `--required-user` (default: `bh55-su@cdc.gov`)
3. `ApprovedToDelete = Yes` in the input file
4. `ApprovalTicket` is populated
5. `ApprovedBy` is populated
6. Classification is `SAFE_DELETE`
7. No Azure resource lock on the endpoint
8. No active dependencies
9. User types `YES` at the confirmation prompt

---

## Rollback

Every endpoint is backed up as an ARM JSON file in `backups/` before deletion.
To restore a deleted endpoint:

```bash
# View backup
cat backups/<endpoint-name>_<timestamp>.json

# Re-create using ARM template (manual step)
# The backup file contains the full resource configuration needed
# to re-create the private endpoint in Azure Portal or via az CLI.
```

---

*EDAV Resource Governance Platform v7.0.0 | Built for the EDAV Platform Team at CDC*

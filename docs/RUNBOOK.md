# EDAV Azure Resource Monitor — RUNBOOK

**Version:** Phase 1  
**Tool:** EDAV Azure Resource Monitor and Cost Optimization Platform  
**Last Updated:** June 2026

---

## Overview

This runbook covers the complete Phase 1 operational workflow for the EDAV Azure Resource Monitor.  
It covers private endpoint cleanup, storage account discovery, Terraform drift detection, and team reporting.

---

## Pre-Run Checklist

- [ ] Azure CLI installed: `az --version`
- [ ] Logged in to Azure: `az login`
- [ ] Correct tenant verified: `az account show`
- [ ] Python 3.8+: `python --version`
- [ ] Dependencies installed: `pip install -r requirements.txt`
- [ ] `config/owners.yml` reviewed and updated with current team contacts
- [ ] `config/subscriptions.yml` correct for target environment
- [ ] Working directory: root of the `edav-private-endpoint-monitor` repo

---

## Step 1: Run Validation (Report Mode)

### Private Endpoints - Basic Report

```bash
python main.py \
  --mode report \
  --resource-type private-endpoints \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
  --output-dir reports/
```

### With Terraform Drift Detection

```bash
python main.py \
  --mode report \
  --resource-type private-endpoints \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
  --terraform-path "C:\\Users\\bh55\\terraform-scripts" \
  --owners-file config/owners.yml \
  --output-dir reports/
```

### Storage Accounts (Discovery)

```bash
python main.py \
  --mode report \
  --resource-type storage \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
  --output-dir reports/
```

### All Resource Types

```bash
python main.py \
  --mode report \
  --resource-type all \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
  --terraform-path "C:\\Users\\bh55\\terraform-scripts" \
  --output-dir reports/
```

---

## Step 2: Review Generated Reports

After running report mode, check the output directory:

```
reports/
  EDAV_Findings_Report_<timestamp>.xlsx   # Full findings
  EDAV_Findings_Report_<timestamp>.csv    # CSV export
  EDAV_Summary_<timestamp>.md             # Markdown summary
  EDAV_Report_<timestamp>.html            # HTML report
  executive_summary.md                    # Executive summary

team_reports/
  networking_private_endpoints.xlsx
  databricks_private_endpoints.xlsx
  internal_test_private_endpoints.xlsx
  storage_private_endpoints.xlsx

logs/
  run_<timestamp>.log
```

Key things to review in the report:
- Total resources scanned
- SAFE_DELETE count and reasons
- REVIEW_REQUIRED count broken down by team
- DO_NOT_DELETE count (should be high for production)
- Terraform Drift findings
- Unknown ownership (needs follow-up)

---

## Step 3: Generate Team Reports

Team reports are generated automatically during report mode.  
They are stored in `team_reports/` directory.

Send team reports to the appropriate contacts listed in `config/owners.yml`:
- Networking Team: network-team@cdc.gov
- EDAV Platform: edav-platform@cdc.gov
- Analytics/Databricks: edav-analytics@cdc.gov

Template email subject:  
`EDAV Resource Monitor - [Team] Private Endpoint Review - [Month Year]`

---

## Step 4: Collect Approvals

After teams review their reports:

1. Teams mark resources `ApprovedToDelete: Yes` in the Excel
2. They add an ITSM change ticket number to `ApprovalTicket` column
3. They add their name to `ApprovedBy` column
4. Teams return the approved file (e.g., `approvals_networking_june2026.xlsx`)

Save approved files as: `examples/approved_YYYY-MM.xlsx`

---

## Step 5: Dry Run Deletion (Simulate)

Before any real deletion, always run dry-run first:

```bash
python main.py \
  --mode delete \
  --resource-type private-endpoints \
  --input examples/approved_YYYY-MM.xlsx \
  --delete-approved \
  --dry-run \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1"
```

Verify:
- Count of SAFE_DELETE resources matches approvals
- No production resources included
- Terraform-managed resources are blocked

---

## Step 6: Run Delete Mode (Live)

**Prerequisites — all must be true:**
- `--delete-approved` flag passed
- `ApprovedToDelete = Yes` in every row
- `ApprovalTicket` populated  
- `ApprovedBy` populated
- `--dry-run` is NOT passed
- Resource type is private-endpoint
- You type `CONFIRM` at the prompt

```bash
python main.py \
  --mode delete \
  --resource-type private-endpoints \
  --input examples/approved_YYYY-MM.xlsx \
  --delete-approved \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1"
```

When prompted, type: `CONFIRM`

---

## Step 7: Validate After Deletion

```bash
# Run report mode again to confirm resources are gone
python main.py \
  --mode report \
  --resource-type private-endpoints \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
  --output-dir reports/post-delete-verify/
```

Also check the delete log: `logs/delete_<timestamp>.log`

---

## Step 8: Update the Ticket

After successful deletion:
1. Open the ITSM change ticket
2. Attach the delete report: `reports/EDAV_Delete_Report_<ts>.xlsx`
3. Attach the verification run report
4. Note count of resources deleted and estimated monthly savings
5. Close the change ticket

---

## Rollback Procedure

If a resource needs to be restored after deletion:

1. Find the ARM backup:
   ```bash
   ls backups/privateEndpoints/
   ```
2. Open the ARM JSON file for the deleted resource
3. Use the `rollback_instructions` field in the report or ARM backup
4. Reconstruct using az CLI:
   ```bash
   az network private-endpoint create \
     --name <name> \
     --resource-group <rg> \
     --private-connection-resource-id <backend-id> \
     --connection-name <name>-conn
   ```
5. Reconnect private link service connections if needed
6. Notify the service owner team

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `AZURE LOGIN REQUIRED` | Run `az login` or `az login --use-device-code` |
| Token expired mid-run | Re-run `az login`, restart the tool |
| Subscription not found | Run `az account list` and check name spelling |
| `resource_rules.yaml not found` | Run `ls config/` — ensure all config files present |
| Missing pandas/openpyxl | Run `pip install -r requirements.txt` |
| Terraform state unavailable | Check `--terraform-path` points to a valid TF workspace |
| Resource locked | Check Azure Portal > Locks on the resource group |

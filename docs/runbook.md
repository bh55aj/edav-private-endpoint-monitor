# EDAV Resource Monitor Cleanup Platform - Monthly Runbook

## Cadence

Run this tool **monthly or twice-monthly** to keep the EDAV Resource Monitor dashboard findings under control and reduce Azure costs progressively.

Recommended schedule:
- **Run 1 (1st of month)**: Audit-only pass. Review SAFE_DELETE candidates. Share report with Linda.
- **Run 2 (3rd-5th of month)**: Live cleanup of approved resources after Linda approves change ticket.

---

## Pre-Run Checklist

- [ ] Azure CLI installed and up to date: `az --version`
- [ ] Logged in to Azure: `az account show`
- [ ] Correct tenant: verify Tenant ID matches EDAV tenant
- [ ] Python 3.8+ installed: `python --version`
- [ ] Dependencies installed: `pip install -r requirements.txt`
- [ ] Input CSV exported from EDAV Resource Monitor dashboard
- [ ] config/ files are current (review exclusions.txt and denylist.json)
- [ ] Working in the correct git branch (use main for production runs)

---

## Monthly Audit Run (Week 1)

### 1. Export from Dashboard

```
URL: https://internal-resource-monitor.edav.cdc.gov/dashboard
Path: Login > Findings > Filter by Severity=HIGH > Export CSV
Save as: inputs/findings_YYYY-MM.csv
```

### 2. Run Audit

```bash
# Full audit - all subscriptions
python main.py \
  --input inputs/findings_YYYY-MM.csv \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1,OCIO-TSBTST-C1" \
  --audit-only \
  --output-dir reports/YYYY-MM/

# With Terraform check (if you have access to TF repo)
python main.py \
  --input inputs/findings_YYYY-MM.csv \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
  --audit-only \
  --terraform-path /path/to/terraform/repo \
  --output-dir reports/YYYY-MM/
```

### 3. Review Reports

Open: `reports/YYYY-MM/EDAV_Findings_Report_<ts>.xlsx`

Key metrics to capture:
- Total findings reviewed
- SAFE_DELETE count and estimated monthly savings
- REVIEW_REQUIRED count (breakdown by team)
- DO_NOT_DELETE count
- UNKNOWN count (these need investigation)

### 4. Prepare Executive Summary for Linda

From the HTML or Markdown report, prepare a brief summary:
- X findings reviewed (from dashboard)
- X confirmed SAFE_DELETE (estimated $Y/month savings)
- X resources need team review (by team)
- X already gone or Terraform-managed

---

## Monthly Cleanup Run (Week 1-2)

### 1. Get Approval

1. Share SAFE_DELETE report with Linda
2. Linda reviews and approves specific resources
3. Create ServiceNow change ticket: CHGxxxxxxx
4. Get change ticket approved

### 2. Prepare Approved Input File

Copy approved resources from the findings report into `inputs/approved_YYYY-MM.csv`:

```csv
ResourceName,ResourceGroup,Subscription,ResourceType,ApprovedToDelete,ApprovalTicket,ApprovedBy,Notes
resource-name-01,rg-name,OCIO-TSBDEV-C1,Microsoft.Network/privateEndpoints,Yes,CHG0012345,Linda Johnson,Validated disconnected no backend
```

### 3. Dry Run

```bash
python main.py \
  --input inputs/approved_YYYY-MM.csv \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
  --cleanup-approved \
  --dry-run \
  --change-ticket CHG0012345 \
  --approved-by "Linda Johnson" \
  --output-dir reports/YYYY-MM/
```

Review dry-run output. Confirm count matches expected approvals.

### 4. Live Cleanup

```bash
python main.py \
  --input inputs/approved_YYYY-MM.csv \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
  --cleanup-approved \
  --change-ticket CHG0012345 \
  --approved-by "Linda Johnson" \
  --delete-pause 3 \
  --output-dir reports/YYYY-MM/
```

When prompted, type `CONFIRM` and press Enter.

### 5. Post-Cleanup Verification

```bash
# Verify previously deleted resources are gone
python main.py \
  --input inputs/approved_YYYY-MM.csv \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
  --verify-only \
  --output-dir reports/YYYY-MM/
```

### 6. Final Dashboard Check

1. Log back into https://internal-resource-monitor.edav.cdc.gov/dashboard
2. Check Findings page
3. Confirm finding count has decreased
4. Screenshot before/after for the report

---

## Troubleshooting

### Authentication Issues

**Error:** `AZURE LOGIN REQUIRED` or `AADSTS500173`
```bash
az login
# OR for remote sessions:
az login --use-device-code
az account show
```

**Error:** Token expired mid-run
The tool detects token expiry and stops automatically. Re-run az login and restart.

### Subscription Context Issues

**Error:** `Cannot set subscription context to 'SubName'`
```bash
az account list --output table
az account set --subscription "OCIO-TSBDEV-C1"
az account show
```

### Resource Validation Failures

**Error:** Resource classified as UNKNOWN or ACCESS_OR_SUBSCRIPTION_REVIEW
- Check if you have Reader access to the resource's subscription
- Try: `az resource show --ids <resource_id>`
- If access denied, escalate to platform team

### File Not Found

**Error:** config/resource_rules.yaml not found
```bash
ls config/
# Ensure all config files are present
```

### Missing Python Dependencies

```bash
pip install -r requirements.txt
# Or with specific version:
pip install pandas>=2.0.0 openpyxl>=3.1.0 pyyaml>=6.0.0 jinja2>=3.1.0 colorama>=0.4.6
```

---

## Rollback / Recovery

If a resource was deleted but needs to be restored:

1. Find the ARM backup: `ls backups/`
2. Open the ARM JSON for the deleted resource
3. Review `reports/YYYY-MM/rollback_instructions.md`
4. Reconstruct the resource using the ARM JSON values
5. The service owner must re-approve any private link connections

**Important:** ARM backups capture resource configuration, not data. For storage accounts, databases, or similar, data recovery requires separate backup processes.

---

## Post-Run Report Archive

Keep reports organized:
```
reports/
  2026-01/
    EDAV_Findings_Report_<ts>.xlsx
    EDAV_Delete_Report_<ts>.xlsx
    EDAV_Verification_<ts>.xlsx
    executive_summary_<ts>.md
  2026-02/
    ...
```

Retain reports for 12 months for audit purposes.

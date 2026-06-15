# EDAV Resource Monitor Cleanup Platform - Detailed Workflow

## End-to-End Workflow

### Step 1: Access the Dashboard

1. Open: https://internal-resource-monitor.edav.cdc.gov/dashboard
2. Log in with your EDAV/CDC account
3. Navigate to **Findings**
4. Apply filters as needed:
   - Severity: HIGH, MEDIUM
   - Status: Active
   - Check Name: DisconnectedPrivateEndpoint, UnattachedNIC, UnattachedDisk, etc.
5. Export findings as CSV or Excel

### Step 2: Run Audit Mode

```bash
python main.py \
  --input examples/sample_dashboard_findings.csv \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
  --audit-only \
  --output-dir reports/
```

This generates all discovery and validation reports with no deletions.

### Step 3: Review Reports

Open the generated Excel report: `reports/EDAV_Findings_Report_<timestamp>.xlsx`

Key sheets to review:
- **Executive Summary** - counts by classification, owner, cost
- **SAFE_DELETE Candidates** - resources confirmed safe to remove
- **REVIEW_REQUIRED** - resources needing team confirmation
- **DO_NOT_DELETE** - resources the tool will never touch
- **Owner Report** - findings grouped by team

### Step 4: Validate SAFE_DELETE Candidates

For each resource marked SAFE_DELETE:
1. Confirm the resource name and resource group are correct
2. Verify the classification reason makes sense (e.g., Disconnected + backend gone)
3. Check the owner/team field - if UNKNOWN, reach out to the platform team
4. Confirm no one on your team is actively using it

### Step 5: Get Approval

1. Share the Executive Summary and SAFE_DELETE report with Linda / team lead
2. Create a change ticket in ServiceNow: CHGxxxxxxx
3. Get the change ticket approved by the required approvers
4. Document the approver name

### Step 6: Update Input File with Approvals

Edit the input CSV to add approval information for each approved resource:

```csv
ResourceName,ResourceGroup,Subscription,ResourceType,ApprovedToDelete,ApprovalTicket,ApprovedBy
edavdrdrill2026-dfs-pe,ocio-network,OCIO-TSBDEV-C1,Microsoft.Network/privateEndpoints,Yes,CHG0012345,Linda Johnson
stale-disk-01,ocio-edav-dev,OCIO-TSBDEV-C1,Microsoft.Compute/disks,Yes,CHG0012345,Linda Johnson
```

### Step 7: Run Dry-Run

Always run dry-run BEFORE live cleanup:

```bash
python main.py \
  --input examples/sample_approved_cleanup.csv \
  --subscriptions "OCIO-TSBDEV-C1" \
  --cleanup-approved \
  --dry-run \
  --change-ticket CHG0012345 \
  --approved-by "Linda Johnson"
```

Review the dry-run output to confirm:
- The right resources are queued for deletion
- No unexpected resources appear
- Classification shows SAFE_DELETE for all queued resources
- Counts match what you approved

### Step 8: Run Live Cleanup

After reviewing the dry-run output:

```bash
python main.py \
  --input examples/sample_approved_cleanup.csv \
  --subscriptions "OCIO-TSBDEV-C1" \
  --cleanup-approved \
  --change-ticket CHG0012345 \
  --approved-by "Linda Johnson" \
  --delete-pause 3
```

The tool will:
1. Re-validate each resource against Azure
2. Present you with a summary of what will be deleted
3. Prompt: "Type CONFIRM to proceed with deletion of N resources"
4. Type `CONFIRM` and press Enter
5. Delete each resource, backing up ARM JSON first
6. Verify each deletion by confirming Azure returns ResourceNotFound

### Step 9: Review Post-Cleanup Reports

Check:
- `reports/EDAV_Delete_Report_<timestamp>.xlsx` - What was deleted
- `reports/EDAV_Verification_<timestamp>.xlsx` - Deletion confirmations
- `reports/rollback_instructions.md` - How to restore if needed
- `backups/` - ARM JSON backups for each deleted resource

### Step 10: Verify on Dashboard

1. Log back into the EDAV Resource Monitor dashboard
2. Go to Findings
3. Confirm the finding count has decreased
4. Capture before/after finding counts for the executive report

### Step 11: Send Final Report to Linda

Share:
- Executive Summary (before vs after finding counts)
- Number of resources deleted
- Estimated monthly cost savings
- Remaining REVIEW_REQUIRED items and what team owns them
- Link to detailed reports

## Input File Column Reference

| Column | Required | Description |
|--------|----------|-------------|
| ResourceName | YES | Azure resource name (not display name) |
| ResourceGroup | YES | Azure resource group name |
| Subscription | NO | Azure subscription name (auto-detected if blank) |
| ResourceType | NO | Azure resource type (e.g., Microsoft.Compute/disks) |
| ApprovedToDelete | NO | Set to "Yes" to enable deletion |
| ApprovalTicket | NO | Change ticket reference (e.g., CHG0012345) |
| ApprovedBy | NO | Name of approver |
| Notes | NO | Any additional notes |
| FindingID | NO | From dashboard export |
| Severity | NO | HIGH/MEDIUM/LOW from dashboard |
| Owner | NO | Owner name if known |
| Team | NO | Team name if known |
| MonthlyCost | NO | Monthly cost from dashboard |
| Environment | NO | Dev/Test/Production/etc. |

### Column Aliases

The parser accepts these alternative column names:

| Standard Column | Accepted Aliases |
|----------------|-----------------|
| ResourceName | name, resource_name, endpointname, endpoint_name |
| ResourceGroup | rg, resource_group, resourcegroup |
| Subscription | sub, subscription_name |
| ResourceType | type, resource_type |
| ApprovedToDelete | approved, approved_to_delete, delete_approved |
| ApprovalTicket | ticket, change_ticket, itsmticket |
| ApprovedBy | approver, approved_by_name |

## Classification Logic Quick Reference

### SAFE_DELETE

A resource is classified SAFE_DELETE when ALL of these are true:
- Not on denylist or exclusions list
- Not Terraform-managed
- No Azure lock
- Resource exists in Azure
- Resource type supports auto-delete
- Specific type criteria met (e.g., disconnected PE with no backend)
- ApprovedToDelete = Yes
- ApprovalTicket is populated
- ApprovedBy is populated

### REVIEW_REQUIRED

A resource is classified REVIEW_REQUIRED when:
- Meets some but not all SAFE_DELETE criteria
- Has dependencies that need verification
- Ownership is unclear
- Resource type does not support auto-delete
- Approval information is missing

### DO_NOT_DELETE

A resource is classified DO_NOT_DELETE when:
- Resource is active/in-use
- Resource is Terraform-managed
- Azure lock exists on resource
- Resource is in production environment
- Resource is on the denylist
- Resource is on the exclusions list

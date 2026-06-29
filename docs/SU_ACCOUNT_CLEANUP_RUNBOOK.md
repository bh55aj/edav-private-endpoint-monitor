# SU Account Cleanup Runbook

**EDAV Azure Resource Monitor - Private Endpoint Cleanup**
**Version:** v6.1.0 | June 2026

---

## Background

This runbook documents the SU (Service User / Privileged) account requirement for
running Azure private endpoint deletions from the command line using Azure CLI.

---

## Why Previous Cleanup Failed

Private endpoint deletions previously failed for two reasons:

1. **Unsupported `--yes` flag**: The Azure CLI command
   `az network private-endpoint delete` does **not** support the `--yes` flag.
   The cleanup script was passing `--yes`, which caused the command to fail with
   an unrecognized argument error.

2. **Wrong Azure CLI account**: The script was running under the standard account
   `bh55@cdc.gov`, which does not have `Microsoft.Network/privateEndpoints/delete`
   RBAC permission on the target subscriptions.

Manual deletion succeeded in the Azure Portal after logging in as `bh55-su@cdc.gov`,
which confirms the SU account has the required Contributor/Network Contributor role.

---

## Standard Account vs SU Account

| Property | Standard Account | SU Account |
|----------|-----------------|------------|
| UPN | bh55@cdc.gov | bh55-su@cdc.gov |
| Role | Reader/Limited | Contributor or Network Contributor |
| Can delete resources | No | Yes |
| Use for | Day-to-day browsing | Privileged operations only |
| Portal login | Always | Only when needed |
| CLI login | `az login` | `az login --use-device-code` |

---

## How to Login with SU Account

### Step 1: Logout of Current Session

```bash
az logout
```

### Step 2: Login with Device Code (Required for CDC MFA)

```bash
az login --use-device-code
```

A device code will be printed. Open https://microsoft.com/devicelogin in your browser,
enter the code, and sign in as `bh55-su@cdc.gov`.

### Step 3: Verify the Active Account

```bash
az account show --query user -o table
```

Expected output:
```
Name                  Type
--------------------  --------
bh55-su@cdc.gov       user
```

### Step 4: Set Subscription

```bash
az account set --subscription "OCIO-TSBDEV-C1"
az account show --query "{Subscription:name,User:user.name}" -o table
```

---

## How to Verify Azure CLI User at Any Time

```bash
az account show --query user.name -o tsv
```

This should return: `bh55-su@cdc.gov`

---

## Running the Cleanup Script

### Report Mode (No Deletions - Safe to Run as Standard Account)

```bash
python main.py \
  --mode report \
  --resource-type private-endpoints \
  --input inputs/findings_2026-06.csv \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
  --output-dir reports/2026-06/
```

Report mode does NOT require the SU account.

---

### Test-Delete Mode (Single Endpoint - Proves CLI Works)

Run this FIRST to prove the CLI deletion works before bulk cleanup:

```bash
python main.py \
  --mode test-delete \
  --resource-type private-endpoints \
  --name testwebbseries-pe \
  --resource-group ocio-network \
  --subscription OCIO-TSBDEV-C1 \
  --required-user bh55-su@cdc.gov
```

This will:
1. Confirm the SU account
2. Set the subscription
3. Show the endpoint connection state
4. Ask you to type CONFIRM
5. Delete the endpoint (no --yes flag)
6. Verify ResourceNotFound
7. Write test_delete_report_<timestamp>.md

---

### Dry-Run Delete Mode (Simulation - Safe to Review)

```bash
python main.py \
  --mode delete \
  --resource-type private-endpoints \
  --input "C:\Users\bh55\Desktop\EDAV_Approved_Private_Endpoint_Deletions.xlsx" \
  --delete-approved \
  --dry-run \
  --required-user "bh55-su@cdc.gov" \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1"
```

---

### Live Delete Mode (Actual Deletion - Requires SU Account)

**Prerequisites:**
- [ ] Logged in as `bh55-su@cdc.gov` (verified with `az account show --query user.name -o tsv`)
- [ ] Input Excel has ApprovedToDelete=Yes, ApprovalTicket, ApprovedBy for each row
- [ ] Dry-run completed and reviewed
- [ ] ITSM change ticket created

```bash
python main.py \
  --mode delete \
  --resource-type private-endpoints \
  --input "C:\Users\bh55\Desktop\EDAV_Approved_Private_Endpoint_Deletions.xlsx" \
  --delete-approved \
  --required-user "bh55-su@cdc.gov" \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
  --change-ticket CHG0012345 \
  --approved-by "Linda Johnson"
```

The script will:
1. Block if current user != bh55-su@cdc.gov
2. Show preflight context (user, subscription, input, mode)
3. Show bulk validation summary
4. Ask you to type `CONFIRM`
5. For each resource: set subscription, verify, backup, delete, verify-gone
6. Write reports: CSV, Excel, Markdown, JSON, HTML
7. Print Resource Graph reminder

---

## How to Verify Resource Graph After Cleanup

In the Azure Portal:

1. Navigate to **Azure Resource Graph Explorer**
2. Run the query from `docs/RESOURCE_GRAPH_QUERIES.md`:

```kql
Resources
| where type =~ 'microsoft.network/privateendpoints'
| mv-expand connections = properties.privateLinkServiceConnections
| extend connectionState = tostring(connections.properties.privateLinkServiceConnectionState.status)
| extend privateLinkServiceId = tostring(connections.properties.privateLinkServiceId)
| extend rg = resourceGroup
| where isnull(connectionState) or connectionState !in~ ('Approved','Connected')
| project name, resourceGroup=rg, subscriptionId, connectionState, privateLinkServiceId
| order by resourceGroup asc, name asc
```

If an endpoint you deleted still appears, wait 1-2 minutes and re-run the query.

---

## How to Verify a Specific Endpoint is Gone

```bash
az network private-endpoint show \
  --name <endpoint-name> \
  --resource-group <resource-group> \
  2>&1 | grep -i "resourcenotfound"
```

Or use Resource Graph:

```kql
Resources
| where type =~ 'microsoft.network/privateendpoints'
| where name =~ '<endpoint_name>'
```

If the query returns 0 rows, the endpoint is confirmed deleted.

---

## How to Update the Change Ticket

After deletion:

1. Copy the deletion report path from the script output:
   `reports/EDAV_Delete_Report_<timestamp>.csv`
2. Open the ITSM change ticket (e.g., CHG0012345)
3. Add implementation notes:
   - Number of endpoints deleted
   - Subscriptions affected
   - Report file path
   - Resource Graph query result (0 remaining disconnected endpoints)
4. Close the ticket as **Completed**

---

## Rollback

If a deletion needs to be reversed, use the ARM JSON backup:

```bash
ls backups/privateEndpoints/

az network private-endpoint create \
  --name <name> \
  --resource-group <rg> \
  --private-connection-resource-id <backend-id-from-backup> \
  --connection-name <name>-conn \
  --vnet-name <vnet> \
  --subnet <subnet>
```

The backup file contains:
- `resource_name`
- `resource_group`
- `subscription`
- `azure_data` (full ARM JSON)
- `azure_tags`
- `approval_ticket`
- `backup_timestamp`

---

## Error Reference

| Error | Cause | Fix |
|-------|-------|-----|
| DELETE BLOCKED: ... | Wrong Azure CLI account | az logout then az login --use-device-code as bh55-su@cdc.gov |
| RBAC BLOCKED: ... | Account lacks delete permission | Verify SU account has Contributor on the subscription |
| ResourceNotFound | Endpoint already deleted | No action needed - already gone |
| Cannot set subscription | Account lacks Reader on subscription | Check subscription access in Azure Portal |
| --yes not supported | Old script version | Updated in v6.1.0 - upgrade to latest |

---

*EDAV Azure Resource Governance Platform v6.1.0*
*Built for the EDAV Platform Team at CDC*

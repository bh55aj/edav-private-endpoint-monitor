# Azure Resource Graph Queries

**EDAV Resource Governance Platform - Complete Query Library**
**Version:** v6.1.0 | June 2026

Use these queries in the **Azure Resource Graph Explorer** in the Azure Portal,
or via Azure CLI with: `az graph query -q "..." --subscriptions "SUB1,SUB2"`

---

## How to Run via Azure CLI

```bash
# Install the graph extension (one-time)
az extension add --name resource-graph

# Run any query
az graph query -q "PASTE_QUERY_HERE" \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1,OCIO-EDAV-DMZ-DEV-C1,OCIO-EDAV-DMZ-PRD-C1" \
  --output table
```

---

## Query 1: Disconnected Private Endpoints

Finds all private endpoints NOT in Approved or Connected state.
This is the primary private endpoint cleanup discovery query.

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

**Run after cleanup to confirm remaining disconnected endpoints.**

---

## Query 2: Unattached Network Security Groups

NSGs with no NIC or subnet associations.

```kql
Resources
| where type =~ 'microsoft.network/networksecuritygroups'
| extend nics = properties.networkInterfaces
| extend subnets = properties.subnets
| where isempty(nics) and isempty(subnets)
| project name, resourceGroup, subscriptionId, location
| order by resourceGroup asc
```

> **Note:** Unattached NSG does not automatically mean safe to delete.
> Verify there are no active security rules and confirm with the network team first.

---

## Query 3: Unattached Managed Disks

Managed disks where `managedBy` is null (no VM or other resource is using them).

```kql
Resources
| where type =~ 'microsoft.compute/disks'
| where isempty(managedBy)
| project name, resourceGroup, subscriptionId, location,
          diskState=tostring(properties.diskState),
          sku=tostring(sku.name),
          diskSizeGB=tostring(properties.diskSizeGB)
| order by resourceGroup asc
```

> **Note:** Check for Databricks disks (names containing "databricks" or "dbfs")
> and backup disks before deleting. These should be REVIEW_REQUIRED.

---

## Query 4: Unattached Public IP Addresses

Public IPs with no `ipConfiguration` (not attached to any NIC, Load Balancer, or App Gateway).

```kql
Resources
| where type =~ 'microsoft.network/publicipaddresses'
| where isempty(properties.ipConfiguration)
| project name, resourceGroup, subscriptionId, location,
          sku=tostring(sku.name),
          allocationMethod=tostring(properties.publicIPAllocationMethod)
| order by resourceGroup asc
```

---

## Query 5: Unattached Network Interfaces

NICs with no VM attached. **READ THE NIC SAFETY NOTE BELOW.**

```kql
Resources
| where type =~ 'microsoft.network/networkinterfaces'
| where isempty(properties.virtualMachine)
| project name, resourceGroup, subscriptionId, location,
          privateIPAddress=tostring(properties.ipConfigurations[0].properties.privateIPAddress)
| order by resourceGroup asc
```

### ⚠️ NIC Safety Note — CRITICAL

**An unattached NIC does NOT mean it is orphaned or safe to delete.**

Many NICs are Azure-managed and must be classified as **KEEP**:

| Pattern | Service | Rule |
|---------|---------|------|
| Name ends with `-pe-nic` | Private Endpoints | KEEP — PE-managed NIC |
| Name contains `.nic.` | Private Endpoints | KEEP — PE-managed NIC |
| Name contains `kube-apiserver` | AKS | KEEP — AKS control plane NIC |
| Name contains `aksnode` / `aks-` | AKS nodes | KEEP — AKS node NIC |
| Name contains `databricks` | Databricks | KEEP — Databricks cluster NIC |
| Name contains `appgw` / `agw` | App Gateway | KEEP — AppGW NIC |
| Resource group starts with `mc_` | AKS managed RG | KEEP — all NICs in this RG |
| Resource group starts with `databricks-rg-` | Databricks RG | KEEP — all NICs in this RG |
| Name contains `privateNIC` / `publicNIC` | Private/Public NICs | KEEP |

**The governance scanner automatically applies these patterns.**
Never manually delete a NIC without verifying it is not Azure-managed.

---

## Query 6: Stopped / Deallocated VMs

```kql
Resources
| where type =~ 'microsoft.compute/virtualmachines'
| extend powerState = tostring(properties.extended.instanceView.powerState.displayStatus)
| where powerState in~ ('VM stopped', 'VM deallocated', 'Stopped', 'Deallocated')
       or isnull(powerState) or powerState == ''
| project name, resourceGroup, subscriptionId, location, powerState, tags
| order by resourceGroup asc
```

> **Note:** Stopped VMs still incur disk costs. Deallocated VMs do not incur compute costs
> but still use storage. Confirm with owner before deallocation or deletion.

---

## Query 7: Event Grid Topics / System Topics with No Subscriptions

```kql
Resources
| where type =~ 'microsoft.eventgrid/topics'
  or type =~ 'microsoft.eventgrid/systemtopics'
| extend subCount = iif(isnotnull(properties.eventSubscriptionCount),
                        toint(properties.eventSubscriptionCount), 0)
| where subCount == 0 or isnull(subCount)
| project name, resourceGroup, subscriptionId, location, type, subCount, tags
| order by resourceGroup asc
```

---

## Query 8: Storage Accounts Needing Review

```kql
Resources
| where type =~ 'microsoft.storage/storageaccounts'
| extend allowPublicAccess = tostring(properties.allowBlobPublicAccess)
| extend httpsOnly = tostring(properties.supportsHttpsTrafficOnly)
| extend accessTier = tostring(properties.accessTier)
| extend sku = tostring(sku.name)
| project name, resourceGroup, subscriptionId, location,
          sku, accessTier, allowPublicAccess, httpsOnly, tags
| order by resourceGroup asc
```

---

## Query 9: AKS / Databricks Managed Resources

Resources in AKS or Databricks managed resource groups (always KEEP):

```kql
Resources
| where resourceGroup startswith 'mc_'
       or resourceGroup startswith 'databricks-rg-'
       or resourceGroup startswith 'databricks_rg_'
| project name, type, resourceGroup, subscriptionId, location, tags
| order by resourceGroup asc, type asc
```

---

## Query 10: Verify Specific Endpoint is Gone

Replace `<endpoint_name>` with the endpoint you deleted.

```kql
Resources
| where type =~ 'microsoft.network/privateendpoints'
| where name =~ '<endpoint_name>'
```

**Expected result after successful deletion: 0 rows.**

---

## Query 11: Private Endpoints by Subscription (Count Summary)

```kql
Resources
| where type =~ 'microsoft.network/privateendpoints'
| mv-expand connections = properties.privateLinkServiceConnections
| extend connectionState = tostring(connections.properties.privateLinkServiceConnectionState.status)
| where isnull(connectionState) or connectionState !in~ ('Approved','Connected')
| summarize DisconnectedCount=count() by subscriptionId
| order by DisconnectedCount desc
```

---

## Query 12: All Private Endpoints (Any State — Full Inventory)

```kql
Resources
| where type =~ 'microsoft.network/privateendpoints'
| mv-expand connections = properties.privateLinkServiceConnections
| extend connectionState = tostring(connections.properties.privateLinkServiceConnectionState.status)
| extend privateLinkServiceId = tostring(connections.properties.privateLinkServiceId)
| project name, resourceGroup, subscriptionId, location,
          connectionState, privateLinkServiceId, tags
| order by connectionState asc, resourceGroup asc, name asc
```

---

## Classification Reference

| connectionState / State | Classification | Action |
|------------------------|----------------|--------|
| Approved / Connected | KEEP | No action - endpoint is in use |
| Disconnected / Rejected | SAFE_DELETE (non-prod) / REVIEW (prod) | Cleanup candidate |
| null / empty | REVIEW_REQUIRED | Investigate |
| NIC in mc_* RG | KEEP | Azure-managed (AKS) |
| NIC name: -pe-nic | KEEP | Azure-managed (PE) |
| Databricks disk | REVIEW_REQUIRED | Do not delete without team approval |
| VM stopped (prod) | REVIEW_REQUIRED | Confirm with owner |
| NSG unattached | REVIEW_REQUIRED | Confirm no active rules |

---

## Running the Governance Scanner

The governance scanner runs ALL queries above automatically:

```bash
python main.py --scan-governance \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1,OCIO-EDAV-DMZ-DEV-C1,OCIO-EDAV-DMZ-PRD-C1" \
  --output-dir reports/governance/
```

This generates:
- `EDAV_Governance_Full_Findings_<ts>.csv`
- `EDAV_Governance_Findings_<ts>.xlsx` (tabs: SAFE_DELETE, REVIEW_REQUIRED, KEEP, per-category, Summary)
- `EDAV_Governance_Executive_Summary_<ts>.md`
- `EDAV_Governance_Dashboard_<ts>.html`
- `EDAV_Governance_Full_Report_<ts>.json`

---

## Post-Cleanup Verification Workflow

1. Run **governance scan** before cleanup to get baseline counts
2. Run the cleanup script (delete mode with SU account)
3. Run **governance scan** again — counts should be lower
4. For each deleted endpoint, run **Query 10** — should return 0 rows
5. Document results in the ITSM change ticket

---

*EDAV Azure Resource Governance Platform v6.1.0*
*Built for the EDAV Platform Team at CDC*

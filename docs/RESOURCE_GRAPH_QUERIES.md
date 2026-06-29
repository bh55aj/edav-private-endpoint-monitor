# Azure Resource Graph Queries

**EDAV Azure Resource Monitor - Private Endpoint Queries**

Use these queries in the **Azure Resource Graph Explorer** in the Azure Portal,
or via Azure CLI: `az graph query -q "..."`

---

## Query 1: All Disconnected Private Endpoints

Finds all private endpoints that are NOT in Approved or Connected state.
This is the primary cleanup discovery query.

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

**Use after cleanup to confirm remaining disconnected endpoints.**

---

## Query 2: Verify Specific Endpoint is Gone

Replace `<endpoint_name>` with the endpoint you deleted.

```kql
Resources
| where type =~ 'microsoft.network/privateendpoints'
| where name =~ '<endpoint_name>'
```

**Expected result after successful deletion: 0 rows.**

---

## Query 3: All Private Endpoints (Any State)

Lists all private endpoints with their connection state, for a full inventory.

```kql
Resources
| where type =~ 'microsoft.network/privateendpoints'
| mv-expand connections = properties.privateLinkServiceConnections
| extend connectionState = tostring(connections.properties.privateLinkServiceConnectionState.status)
| extend privateLinkServiceId = tostring(connections.properties.privateLinkServiceId)
| project name, resourceGroup, subscriptionId, location,
          connectionState, privateLinkServiceId,
          tags
| order by connectionState asc, resourceGroup asc, name asc
```

---

## Query 4: Private Endpoints by Subscription

Count disconnected endpoints per subscription.

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

## Query 5: Recently Created Private Endpoints

Endpoints created in the last 90 days that are disconnected.

```kql
Resources
| where type =~ 'microsoft.network/privateendpoints'
| mv-expand connections = properties.privateLinkServiceConnections
| extend connectionState = tostring(connections.properties.privateLinkServiceConnectionState.status)
| extend privateLinkServiceId = tostring(connections.properties.privateLinkServiceId)
| where isnull(connectionState) or connectionState !in~ ('Approved','Connected')
| where todatetime(properties.timeCreated) > ago(90d)
| project name, resourceGroup, subscriptionId, connectionState,
          privateLinkServiceId,
          createdTime=tostring(properties.timeCreated)
| order by createdTime desc
```

---

## How to Run via Azure CLI

```bash
# Install the graph extension if not already installed
az extension add --name resource-graph

# Run the disconnected PE query
az graph query -q "
Resources
| where type =~ 'microsoft.network/privateendpoints'
| mv-expand connections = properties.privateLinkServiceConnections
| extend connectionState = tostring(connections.properties.privateLinkServiceConnectionState.status)
| extend privateLinkServiceId = tostring(connections.properties.privateLinkServiceId)
| extend rg = resourceGroup
| where isnull(connectionState) or connectionState !in~ ('Approved','Connected')
| project name, resourceGroup=rg, subscriptionId, connectionState, privateLinkServiceId
| order by resourceGroup asc, name asc
" --output table

# Verify a specific endpoint is gone
az graph query -q "
Resources
| where type =~ 'microsoft.network/privateendpoints'
| where name =~ 'your-endpoint-name'
" --output table
```

---

## Interpreting Results

| connectionState | Meaning | Action |
|-----------------|---------|--------|
| Approved | Connected and working | Keep |
| Connected | Connected and working | Keep |
| Disconnected | Backend resource gone or removed | Cleanup candidate |
| Pending | Awaiting backend approval | Review with owner |
| Rejected | Rejected by backend | Cleanup candidate |
| null / empty | No connection (orphaned) | Cleanup candidate |

---

## Post-Cleanup Verification Workflow

1. Run **Query 1** before cleanup to get baseline count
2. Run the cleanup script
3. Run **Query 1** again - count should be lower
4. For each deleted endpoint, run **Query 2** - should return 0 rows
5. Document results in the ITSM change ticket

---

*EDAV Azure Resource Governance Platform v6.1.0*
*Built for the EDAV Platform Team at CDC*

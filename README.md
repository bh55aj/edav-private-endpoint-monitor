# EDAV Azure Resource Governance and Cost Optimization Platform

**v6.0.0** — Enterprise Azure governance, cost-reduction, ownership tracking, and safe cleanup platform for the EDAV Platform Team at CDC.

Dashboard: https://internal-resource-monitor.edav.cdc.gov/dashboard

**Nothing is deleted without validation, approval, a change ticket, an approver, and a typed CONFIRM.**

---

## What This Platform Does

Azure environments accumulate orphaned, idle, disconnected, and unowned resources over time. These resources waste money, create compliance risk, and clutter dashboards. This platform automates the entire governance lifecycle across 14+ Azure services.

```
Azure Subscriptions
        |
  Resource Graph + Azure CLI
        |
  Service Monitors (14 Azure services)
        |
  Ownership Engine  -  Who owns it?
  Cost Optimizer    -  What does it cost?
  Terraform Drift   -  Is it in Terraform?
        |
  Classification: SAFE_DELETE / REVIEW_REQUIRED / DO_NOT_DELETE
        |
  Reports: CSV, XLSX, Markdown, Executive Summary
        |
  Console Dashboard
        |
  Approval-gated cleanup with ARM backup + post-delete verification
```

---

## Architecture

### Layer 1 - Service Monitors (monitors/)

Each Azure service has its own monitor module inheriting from `BaseMonitor`.

| Monitor | Service | Key Checks |
|---|---|---|
| base_monitor.py | Abstract base | Shared classification, az CLI wrapper, tag extraction |
| storage_monitor.py | Storage Accounts | Empty containers, idle 90+ days, public access |
| aks_monitor.py | AKS | Stopped clusters, zero nodes, failed state |
| keyvault_monitor.py | Key Vaults | Empty vaults, no access policies, soft-delete disabled |
| sql_monitor.py | SQL / SQL MI | Empty servers, paused DBs, stopped MIs |
| eventhub_monitor.py | Event Hubs | Empty namespaces, disabled state |
| azureml_monitor.py | Azure ML | No compute, failed workspaces |
| databricks_monitor.py | Databricks | Failed state, no tags |
| appservice_monitor.py | App Services + Function Apps | Empty plans, stopped sites |
| redis_monitor.py | Redis Cache | Failed state, non-SSL port |
| ai_monitors.py | AI Search, AI Foundry, OpenAI, Event Grid | Degraded/empty/undeployed |

### Layer 2 - Engines (engines/)

| Engine | Purpose |
|---|---|
| cost_optimizer.py | Aggregates findings, produces monthly savings estimates by team/service |
| ownership_engine.py | Enriches resources with owner/team/cost-center from tags and config patterns |
| terraform_drift.py | Compares Azure resources against Terraform state and .tf source files |

### Layer 3 - Platform Core

| File | Purpose |
|---|---|
| main.py | Core engine: 15-gate safety model, ARM backup, deletion, verification |
| dashboard.py | Console dashboard: real-time scan summary |

---

## Supported Azure Services

| Service | Resource Type |
|---|---|
| Private Endpoints | Microsoft.Network/privateEndpoints |
| Network Interfaces | Microsoft.Network/networkInterfaces |
| Public IPs | Microsoft.Network/publicIPAddresses |
| NSGs | Microsoft.Network/networkSecurityGroups |
| Managed Disks | Microsoft.Compute/disks |
| Virtual Machines | Microsoft.Compute/virtualMachines |
| **Storage Accounts** | Microsoft.Storage/storageAccounts |
| **AKS** | Microsoft.ContainerService/managedClusters |
| **Azure ML** | Microsoft.MachineLearningServices/workspaces |
| **Databricks** | Microsoft.Databricks/workspaces |
| **SQL / SQL MI** | Microsoft.Sql/servers, Microsoft.Sql/managedInstances |
| **Key Vaults** | Microsoft.KeyVault/vaults |
| **Event Grid** | Microsoft.EventGrid/topics, domains |
| **App Services** | Microsoft.Web/sites, serverfarms |
| **Function Apps** | Microsoft.Web/sites (kind=functionapp) |
| **Event Hubs** | Microsoft.EventHub/namespaces |
| **Redis** | Microsoft.Cache/Redis |
| **AI Search** | Microsoft.Search/searchServices |
| **AI Foundry** | Microsoft.MachineLearningServices/workspaces (kind=hub) |
| **Azure OpenAI** | Microsoft.CognitiveServices/accounts |

---

## Quick Start

```bash
git clone https://github.com/ausjones84/edav-private-endpoint-monitor
cd edav-private-endpoint-monitor
pip install -r requirements.txt
az login
az account set --subscription "OCIO-TSBDEV-C1"

python main.py \
  --input inputs/findings_2026-06.csv \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
  --audit-only \
  --output-dir reports/2026-06/

python dashboard.py --report-dir reports/2026-06/
```

---

## Installation

**Prerequisites:** Python 3.8+, Azure CLI 2.40+, Reader access (Contributor for cleanup).

```bash
pip install -r requirements.txt
az login
python main.py --self-test
```

---

## Usage Examples

**Full audit:**
```bash
python main.py --input inputs/findings.csv --subscriptions "SUB1,SUB2" --audit-only --output-dir reports/
```

**With Terraform drift detection:**
```bash
python main.py --input inputs/findings.csv --subscriptions "SUB1" --terraform-path /path/to/tf-repo --audit-only --output-dir reports/
```

**Ownership report:**
```bash
python main.py --input inputs/findings.csv --subscriptions "SUB1,SUB2" --generate-owner-report --output-dir reports/
```

**Cost optimization report:**
```bash
python main.py --input inputs/findings.csv --subscriptions "SUB1,SUB2" --cost-report --output-dir reports/
```

**Dry run (preview cleanup):**
```bash
python main.py --input inputs/approved.csv --subscriptions "SUB1" --cleanup-approved --dry-run --change-ticket CHG0012345 --approved-by "Linda Johnson"
```

**Live cleanup:**
```bash
python main.py --input inputs/approved.csv --subscriptions "SUB1" --cleanup-approved --change-ticket CHG0012345 --approved-by "Linda Johnson" --delete-pause 3
```

---

## Monthly Workflow

1. Export dashboard findings to `inputs/findings_YYYY-MM.csv`
2. 2. Run `--audit-only` to classify all resources
   3. 3. Review XLSX report: SAFE_DELETE tab, REVIEW_REQUIRED tab, Cost Opportunities, Terraform Drift, Ownership
      4. 4. Share Executive Summary with leadership; get CHGxxxxxxx change ticket
         5. 5. Add `ApprovedToDelete=Yes`, `ApprovalTicket=CHGxxxxxxx`, `ApprovedBy=Name` to CSV
            6. 6. Run `--cleanup-approved --dry-run` to preview
               7. 7. Run `--cleanup-approved` (type CONFIRM when prompted)
                  8. 8. Run `--verify-only` to confirm deletions
                    
                     9. ---
                    
                     10. ## Reporting
                    
                     11. Every run produces these reports in `--output-dir`:
                    
                     12. | Report | Formats | Contents |
                     13. |---|---|---|
                     14. | Findings Report | CSV, XLSX, HTML, JSON, Markdown | All scanned resources with classification, owner, cost |
                     15. | Executive Summary | Markdown, HTML | Counts, savings estimate, top findings |
                     16. | Cost Optimization | CSV, XLSX, Markdown | Opportunities ranked by savings, by team/service |
                     17. | Ownership Report | CSV, XLSX, Markdown | Resource-to-owner mapping, unowned resources |
                     18. | Terraform Drift | CSV, XLSX, Markdown | Manual deployments, stale state, config drift |
                     19. | SAFE_DELETE | XLSX tab | Confirmed cleanup candidates |
                     20. | REVIEW_REQUIRED | XLSX tab | Resources requiring human review |
                     21. | DO_NOT_DELETE | XLSX tab | Protected resources and reasons |
                     22. | Deletion Report | CSV, XLSX | Audit log: what deleted, when, by whom |
                     23. | Verification Report | CSV | Post-delete Azure confirmation |
                     24. | Rollback Instructions | Markdown | Step-by-step restore guide |
                    
                     25. ---
                    
                     26. ## Ownership Discovery
                    
                     27. Ownership is detected automatically in this priority order:
                    
                     28. 1. **Azure tags** - owner, Owner, EDAV_Business_POC, EDAV_Created_By, team, CostCenter, etc.
                         2. 2. **Resource Group patterns** - configured in `config/ownership_map.yaml`
                            3. 3. **Subscription patterns** - configured in `config/ownership_map.yaml`
                               4. 4. **Resource name patterns** - configured in `config/ownership_map.yaml`
                                 
                                  5. **Configure ownership_map.yaml:**
                                  6. ```yaml
                                     resource_group_patterns:
                                       "ocio-dav-dev-.*":
                                         owner: "EDAV Platform Team"
                                         team: "EDAV Platform - Dev"
                                     subscription_patterns:
                                       "OCIO-TSBDEV-.*":
                                         owner: "EDAV Platform Team"
                                         team: "EDAV Platform - Dev"
                                     ```

                                     ---

                                     ## Cost Optimization

                                     The `CostOptimizer` engine identifies 5 opportunity types:

                                     | Type | Description |
                                     |---|---|
                                     | EMPTY | Resource with no workloads or contents (empty storage, empty App Service Plan) |
                                     | IDLE | Resource running but with no recent activity (stopped AKS, paused SQL DB) |
                                     | FAILED | Resource in error, failed, or stopped state (failed SQL MI, stopped cluster) |
                                     | ORPHANED | Resource with no known consumer (disconnected private endpoint) |
                                     | UNOWNED | Resource with no ownership tags |

                                     Produces monthly and annual savings estimates by team, service, subscription, and resource group.

                                     ---

                                     ## Terraform Drift Detection

                                     The `TerraformDriftDetector` compares Azure against Terraform:

                                     ```bash
                                     python main.py --input inputs/findings.csv --terraform-path /path/to/tf-repo --audit-only --output-dir reports/
                                     ```

                                     **Drift types detected:**

                                     - **Not in Terraform** - manual deployments and shadow IT (includes `terraform import` instructions)
                                     - - **Not in Azure** - stale Terraform state entries (includes `terraform state rm` instructions)
                                       - - **Configuration Drift** - resources that exist in both but have diverged
                                        
                                         - ---

                                         ## Dashboard

                                         The console dashboard renders after every scan:

                                         ```
                                         ========================================================================
                                                     EDAV Azure Resource Governance Platform
                                                                Scan Dashboard
                                         ========================================================================
                                           Resources scanned:                          1,247
                                           Cleanup candidates (SAFE_DELETE):              89
                                           Owner review required:                        156
                                           Est. monthly savings:                   $12,450.00
                                           Est. annual savings:                   $149,400.00
                                           Terraform - not in Terraform:                  34
                                           Terraform - stale state entries:                7
                                           Unowned resources:                             42
                                         ========================================================================
                                           Nothing is deleted without validation, approval, a ticket, and CONFIRM.
                                         ========================================================================
                                         ```

                                         ---

                                         ## Cleanup Workflows

                                         ### 15-Gate Safety Model

                                         All 15 must pass before any deletion:

                                         1. --cleanup-approved flag passed
                                         2. 2. ApprovedToDelete = Yes in input
                                            3. 3. ApprovalTicket populated (CHGxxxxxxx)
                                               4. 4. ApprovedBy populated
                                                  5. 5. Classification = SAFE_DELETE
                                                     6. 6. Not on config/exclusions.txt
                                                        7. 7. Not on config/denylist.json
                                                           8. 8. Resource still exists in Azure (live re-check)
                                                              9. 9. No Azure resource lock
                                                                 10. 10. Not Terraform-managed
                                                                     11. 11. Not production/high environment
                                                                         12. 12. Resource type supports auto-delete
                                                                             13. 13. ARM JSON backup created successfully
                                                                                 14. 14. Subscription context verified
                                                                                     15. 15. CONFIRM typed at interactive prompt
                                                                                        
                                                                                         16. ---
                                                                                        
                                                                                         17. ## Rollback Procedures
                                                                                        
                                                                                         18. Every deleted resource has an ARM JSON backup in `backups/`:
                                                                                        
                                                                                         19. ```bash
                                                                                             ls backups/<resource-name>_*.json
                                                                                             cat reports/rollback_instructions.md

                                                                                             az resource create \
                                                                                               --properties @backups/<resource-name>_<timestamp>.json \
                                                                                               --resource-group <rg-name> \
                                                                                               --resource-type <resource-type>
                                                                                             ```

                                                                                             ---

                                                                                             ## Security Considerations

                                                                                             - Read-only by default - nothing deleted without `--cleanup-approved`
                                                                                             - - No hardcoded credentials - uses `az login` Azure CLI tokens
                                                                                               - - ARM backups before every deletion
                                                                                                 - - All 15 safety gates enforced in code
                                                                                                   - - Timestamped audit logs for every run
                                                                                                     - - Production resource auto-detection prevents accidental deletion
                                                                                                       - - Terraform protection - TF-managed resources are never deleted
                                                                                                        
                                                                                                         - ---
                                                                                                         
                                                                                                         ## Configuration Reference
                                                                                                         
                                                                                                         | File | Purpose |
                                                                                                         |---|---|
                                                                                                         | config/resource_rules.yaml | Per-resource-type classification rules and auto-delete flags |
                                                                                                         | config/ownership_map.yaml | Owner/team mapping by RG pattern, subscription, resource name |
                                                                                                         | config/exclusions.txt | Resources NEVER deleted (one name per line) |
                                                                                                         | config/denylist.json | Hard-blocked names, groups, and patterns |
                                                                                                         | config/allowlist.json | Pre-approved safe candidates |
                                                                                                         
                                                                                                         ---
                                                                                                         
                                                                                                         ## Repository Structure
                                                                                                         
                                                                                                         ```
                                                                                                         edav-private-endpoint-monitor/
                                                                                                         ├── main.py                    # Core platform engine (v6.0.0)
                                                                                                         ├── dashboard.py               # Console governance dashboard
                                                                                                         ├── README.md
                                                                                                         ├── requirements.txt
                                                                                                         ├── sample_input.csv
                                                                                                         ├── monitors/
                                                                                                         │   ├── __init__.py
                                                                                                         │   ├── base_monitor.py        # Abstract base class
                                                                                                         │   ├── storage_monitor.py
                                                                                                         │   ├── aks_monitor.py
                                                                                                         │   ├── keyvault_monitor.py
                                                                                                         │   ├── sql_monitor.py
                                                                                                         │   ├── eventhub_monitor.py
                                                                                                         │   ├── azureml_monitor.py
                                                                                                         │   ├── databricks_monitor.py
                                                                                                         │   ├── appservice_monitor.py
                                                                                                         │   ├── redis_monitor.py
                                                                                                         │   └── ai_monitors.py         # AI Search, AI Foundry, OpenAI, Event Grid
                                                                                                         ├── engines/
                                                                                                         │   ├── cost_optimizer.py
                                                                                                         │   ├── ownership_engine.py
                                                                                                         │   └── terraform_drift.py
                                                                                                         ├── config/
                                                                                                         │   ├── resource_rules.yaml
                                                                                                         │   ├── ownership_map.yaml
                                                                                                         │   ├── exclusions.txt
                                                                                                         │   ├── denylist.json
                                                                                                         │   └── allowlist.json
                                                                                                         ├── examples/
                                                                                                         ├── docs/
                                                                                                         ├── reports/                   # Auto-generated (git-ignored)
                                                                                                         ├── backups/                   # ARM JSON backups (git-ignored)
                                                                                                         └── logs/                      # Run logs (git-ignored)
                                                                                                         ```
                                                                                                         
                                                                                                         ---
                                                                                                         
                                                                                                         ## Version History
                                                                                                         
                                                                                                         | Version | Date | Changes |
                                                                                                         |---|---|---|
                                                                                                         | v6.0.0 | June 2026 | Full platform expansion: 14 service monitors, Cost Optimizer, Ownership Engine, Terraform Drift Detector, console Dashboard, Azure SDK support |
                                                                                                         | v5.0.0 | June 2026 | Multi-resource type support, 15-gate safety model, ARM backups, ownership detection, executive reports |
                                                                                                         | v4.0.0 | April 2026 | Private endpoint monitor with post-delete verification |
                                                                                                         | v3.x | February 2026 | Enterprise governance features |
                                                                                                         
                                                                                                         ---
                                                                                                         
                                                                                                         *EDAV Azure Resource Governance and Cost Optimization Platform v6.0.0*
                                                                                                         *Built for the EDAV Platform Team at CDC*
                                                                                                         *Nothing is deleted without validation, approval, a change ticket, an approver, and a typed CONFIRM.*
                                                                                                         

# EDAV Azure Resource Governance & Cost Optimization Platform

> **v6.0.0** — The primary EDAV Azure governance, resource cleanup, ownership tracking, and cost optimization platform for the EDAV Platform Team at CDC.
>
> [![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://python.org)
> [![Azure CLI](https://img.shields.io/badge/azure--cli-2.40%2B-blue)](https://docs.microsoft.com/cli/azure/install-azure-cli)
> [![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
>
> **Dashboard:** https://internal-resource-monitor.edav.cdc.gov/dashboard
>
> > Nothing is deleted without validation, approval, a change ticket, an approver, and a typed `CONFIRM`.
> >
> > ---
> >
> > ## Table of Contents
> >
> > 1. [What This Platform Does](#what-this-platform-does)
> > 2. 2. [Architecture](#architecture)
> >    3. 3. [Supported Azure Services](#supported-azure-services)
> >       4. 4. [Quick Start](#quick-start)
> >          5. 5. [Installation](#installation)
> >             6. 6. [Usage Examples](#usage-examples)
> >                7. 7. [Monthly Workflow](#monthly-workflow)
> >                   8. 8. [Reporting](#reporting)
> >                      9. 9. [Ownership Discovery](#ownership-discovery)
> >                         10. 10. [Cost Optimization](#cost-optimization)
> >                             11. 11. [Terraform Drift Detection](#terraform-drift-detection)
> >                                 12. 12. [Dashboard](#dashboard)
> >                                     13. 13. [Cleanup Workflows](#cleanup-workflows)
> >                                         14. 14. [Rollback Procedures](#rollback-procedures)
> >                                             15. 15. [Security Considerations](#security-considerations)
> >                                                 16. 16. [Configuration Reference](#configuration-reference)
> >                                                     17. 17. [Repository Structure](#repository-structure)
> >                                                         18. 18. [Known Limitations](#known-limitations)
> >                                                             19. 19. [Version History](#version-history)
> >                                                                
> >                                                                 20. ---
> >                                                                
> >                                                                 21. ## What This Platform Does
> >                                                                
> >                                                                 22. Azure environments accumulate orphaned, idle, disconnected, and unowned resources over time. These resources waste money, create compliance risk, and clutter dashboards.
> >
> > This platform automates the entire governance lifecycle:
> >
> > ```
> > Azure Subscriptions
> >         |
> >         v
> >   Resource Graph + Azure CLI
> >         |
> >         v
> >   Service Monitors (14 Azure services)
> >         |
> >         v
> >   Ownership Engine  ──► Who owns it?
> >   Cost Optimizer    ──► What does it cost?
> >   Terraform Drift   ──► Is it in Terraform?
> >         |
> >         v
> >   Classification: SAFE_DELETE / REVIEW_REQUIRED / DO_NOT_DELETE
> >         |
> >         v
> >   Reports: CSV, XLSX, Markdown, Executive Summary
> >         |
> >         v
> >   Dashboard (console)
> >         |
> >         v
> >   Approval-gated cleanup with ARM backup + verification
> > ```
> >
> > ---
> >
> > ## Architecture
> >
> > The platform is organized into three layers:
> >
> > ### Layer 1 — Service Monitors (`monitors/`)
> >
> > Each Azure service has its own monitor module inheriting from `BaseMonitor`. Monitors use Azure Resource Graph queries and Azure CLI calls to discover and classify resources.
> >
> > | Monitor Module | Service | Key Checks |
> > |---|---|---|
> > | `base_monitor.py` | Abstract base | Shared classification, az CLI wrapper, tag extraction |
> > | `storage_monitor.py` | Storage Accounts | Empty containers, idle >90d, public access |
> > | `aks_monitor.py` | AKS | Stopped clusters, zero nodes, failed state |
> > | `keyvault_monitor.py` | Key Vaults | Empty vaults, no access policies, soft-delete disabled |
> > | `sql_monitor.py` | SQL / SQL MI | Empty servers, paused DBs, stopped MIs |
> > | `eventhub_monitor.py` | Event Hubs | Empty namespaces, disabled state |
> > | `azureml_monitor.py` | Azure ML | No compute, failed workspaces |
> > | `databricks_monitor.py` | Databricks | Failed state, no tags |
> > | `appservice_monitor.py` | App Services & Function Apps | Empty plans, stopped sites |
> > | `redis_monitor.py` | Redis Cache | Failed state, non-SSL port |
> > | `ai_monitors.py` | AI Search, AI Foundry, OpenAI, Event Grid | Degraded/empty/undeployed resources |
> >
> > Private Endpoint, NIC, NSG, Public IP, Managed Disk, and VM monitoring continues through the existing `main.py` engine.
> >
> > ### Layer 2 — Engines (`engines/`)
> >
> > | Engine | Purpose |
> > |---|---|
> > | `cost_optimizer.py` | Aggregates findings → monthly savings estimates, team/service breakdowns |
> > | `ownership_engine.py` | Enriches resources with owner/team/cost-center from tags, RG patterns, sub patterns |
> > | `terraform_drift.py` | Compares Azure resources against Terraform state and source files |
> >
> > ### Layer 3 — Platform Core
> >
> > | File | Purpose |
> > |---|---|
> > | `main.py` | Core engine: 15-gate safety model, ARM backup, deletion, verification |
> > | `dashboard.py` | Console dashboard: real-time scan summary |
> > | `requirements.txt` | Python dependencies |
> >
> > ---
> >
> > ## Supported Azure Services
> >
> > | Service | Resource Type | Monitor |
> > |---|---|---|
> > | Private Endpoints | `Microsoft.Network/privateEndpoints` | main.py |
> > | Network Interfaces | `Microsoft.Network/networkInterfaces` | main.py |
> > | Public IPs | `Microsoft.Network/publicIPAddresses` | main.py |
> > | NSGs | `Microsoft.Network/networkSecurityGroups` | main.py |
> > | Managed Disks | `Microsoft.Compute/disks` | main.py |
> > | Virtual Machines | `Microsoft.Compute/virtualMachines` | main.py |
> > | **Storage Accounts** | `Microsoft.Storage/storageAccounts` | storage_monitor.py |
> > | **AKS** | `Microsoft.ContainerService/managedClusters` | aks_monitor.py |
> > | **Azure ML** | `Microsoft.MachineLearningServices/workspaces` | azureml_monitor.py |
> > | **Databricks** | `Microsoft.Databricks/workspaces` | databricks_monitor.py |
> > | **SQL / SQL MI** | `Microsoft.Sql/servers`, `Microsoft.Sql/managedInstances` | sql_monitor.py |
> > | **Key Vaults** | `Microsoft.KeyVault/vaults` | keyvault_monitor.py |
> > | **Event Grid** | `Microsoft.EventGrid/topics`, `domains` | ai_monitors.py |
> > | **App Services** | `Microsoft.Web/sites`, `serverfarms` | appservice_monitor.py |
> > | **Function Apps** | `Microsoft.Web/sites` (kind=functionapp) | appservice_monitor.py |
> > | **Event Hubs** | `Microsoft.EventHub/namespaces` | eventhub_monitor.py |
> > | **Redis** | `Microsoft.Cache/Redis` | redis_monitor.py |
> > | **AI Search** | `Microsoft.Search/searchServices` | ai_monitors.py |
> > | **AI Foundry** | `Microsoft.MachineLearningServices/workspaces` (kind=hub) | ai_monitors.py |
> > | **Azure OpenAI** | `Microsoft.CognitiveServices/accounts` | ai_monitors.py |
> >
> > ---
> >
> > ## Quick Start
> >
> > ```bash
> > # 1. Clone and install
> > git clone https://github.com/ausjones84/edav-private-endpoint-monitor
> > cd edav-private-endpoint-monitor
> > pip install -r requirements.txt
> >
> > # 2. Login to Azure
> > az login
> > az account set --subscription "OCIO-TSBDEV-C1"
> >
> > # 3. Run a full audit (read-only)
> > python main.py \
> >   --input inputs/findings_2026-06.csv \
> >   --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
> >   --audit-only \
> >   --output-dir reports/2026-06/
> >
> > # 4. View the dashboard
> > python dashboard.py --report-dir reports/2026-06/
> > ```
> >
> > ---
> >
> > ## Installation
> >
> > ### Prerequisites
> >
> > | Requirement | Version | Notes |
> > |---|---|---|
> > | Python | 3.8+ | [python.org](https://python.org) |
> > | Azure CLI | 2.40+ | [Install guide](https://aka.ms/install-azure-cli) |
> > | Azure access | Reader | For audit runs |
> > | Azure access | Contributor | For live cleanup only |
> >
> > ### Install Steps
> >
> > ```bash
> > # Clone
> > git clone https://github.com/ausjones84/edav-private-endpoint-monitor
> > cd edav-private-endpoint-monitor
> >
> > # Install all dependencies (including Azure SDK)
> > pip install -r requirements.txt
> >
> > # Login
> > az login
> > # For headless/remote sessions:
> > az login --use-device-code
> >
> > # Verify
> > az account show
> > python main.py --self-test
> > ```
> >
> > ---
> >
> > ## Usage Examples
> >
> > ### Full Multi-Service Audit
> >
> > ```bash
> > python main.py \
> >   --input inputs/findings_2026-06.csv \
> >   --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
> >   --audit-only \
> >   --output-dir reports/2026-06/
> > ```
> >
> > ### Audit with Terraform Drift Detection
> >
> > ```bash
> > python main.py \
> >   --input inputs/findings_2026-06.csv \
> >   --subscriptions "OCIO-TSBDEV-C1" \
> >   --terraform-path /path/to/terraform-repo \
> >   --audit-only \
> >   --output-dir reports/2026-06/
> > ```
> >
> > ### Generate Ownership Report
> >
> > ```bash
> > python main.py \
> >   --input inputs/findings_2026-06.csv \
> >   --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
> >   --generate-owner-report \
> >   --output-dir reports/2026-06/
> > ```
> >
> > ### Generate Cost Optimization Report
> >
> > ```bash
> > python main.py \
> >   --input inputs/findings_2026-06.csv \
> >   --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
> >   --cost-report \
> >   --output-dir reports/2026-06/
> > ```
> >
> > ### Dry Run (Preview Cleanup)
> >
> > ```bash
> > python main.py \
> >   --input inputs/approved_2026-06.csv \
> >   --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
> >   --cleanup-approved \
> >   --dry-run \
> >   --change-ticket CHG0012345 \
> >   --approved-by "Linda Johnson"
> > ```
> >
> > ### Live Cleanup
> >
> > ```bash
> > python main.py \
> >   --input inputs/approved_2026-06.csv \
> >   --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
> >   --cleanup-approved \
> >   --change-ticket CHG0012345 \
> >   --approved-by "Linda Johnson" \
> >   --delete-pause 3
> > ```
> >
> > ---
> >
> > ## Monthly Workflow
> >
> > Follow these steps in order each month. **Do not skip steps.**
> >
> > ### Step 1 — Export Dashboard Findings
> >
> > 1. Log in to https://internal-resource-monitor.edav.cdc.gov/dashboard
> > 2. 2. Go to **Findings** → Filter: Severity = HIGH, Status = Active
> >    3. 3. Export as CSV → save as `inputs/findings_YYYY-MM.csv`
> >      
> >       4. ### Step 2 — Run Full Audit
> >      
> >       5. ```bash
> >          python main.py \
> >            --input inputs/findings_2026-06.csv \
> >            --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
> >            --audit-only \
> >            --terraform-path /path/to/tf-repo \
> >            --output-dir reports/2026-06/
> >          ```
> >
> > ### Step 3 — Review Reports
> >
> > Open `reports/2026-06/EDAV_Findings_Report_<timestamp>.xlsx`:
> >
> > | Tab | What To Do |
> > |---|---|
> > | SAFE_DELETE | Review — these are cleanup candidates |
> > | REVIEW_REQUIRED | Human review needed before any action |
> > | DO_NOT_DELETE | Leave alone — active, locked, or Terraform-managed |
> > | Executive Summary | Share with leadership |
> > | Cost Opportunities | Review estimated savings by service and team |
> > | Terraform Drift | Review manual deployments and stale state |
> > | Ownership | Review unowned resources and assign owners |
> >
> > ### Step 4 — Get Approval
> >
> > Share the Executive Summary with Linda. Get a change ticket number (CHGxxxxxxx).
> >
> > Add to input CSV for approved resources:
> >
> > | Column | Value |
> > |---|---|
> > | ApprovedToDelete | Yes |
> > | ApprovalTicket | e.g. CHG0012345 |
> > | ApprovedBy | e.g. Linda Johnson |
> >
> > ### Step 5 — Dry Run
> >
> > ```bash
> > python main.py \
> >   --input inputs/approved_2026-06.csv \
> >   --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
> >   --cleanup-approved \
> >   --dry-run \
> >   --change-ticket CHG0012345 \
> >   --approved-by "Linda Johnson"
> > ```
> >
> > ### Step 6 — Live Cleanup
> >
> > ```bash
> > python main.py \
> >   --input inputs/approved_2026-06.csv \
> >   --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
> >   --cleanup-approved \
> >   --change-ticket CHG0012345 \
> >   --approved-by "Linda Johnson" \
> >   --delete-pause 3
> > ```
> >
> > You will be prompted to type `CONFIRM` before any deletions.
> >
> > ### Step 7 — Verify
> >
> > ```bash
> > python main.py \
> >   --input inputs/approved_2026-06.csv \
> >   --verify-only \
> >   --output-dir reports/2026-06/
> > ```
> >
> > ---
> >
> > ## Reporting
> >
> > Every run produces a full set of reports in `--output-dir`:
> >
> > | Report | Formats | Contents |
> > |---|---|---|
> > | Findings Report | CSV, XLSX, HTML, JSON, Markdown | All scanned resources with classification, owner, cost |
> > | Executive Summary | Markdown, HTML | Counts, estimated cost savings, top findings |
> > | Cost Optimization | CSV, XLSX, Markdown | Opportunities ranked by savings, by team/service |
> > | Ownership Report | CSV, XLSX, Markdown | Resource→owner mapping, unowned resources |
> > | Terraform Drift | CSV, XLSX, Markdown | Manual deployments, stale state, config drift |
> > | SAFE_DELETE Sheet | XLSX tab | Confirmed cleanup candidates |
> > | REVIEW_REQUIRED Sheet | XLSX tab | Resources requiring human review |
> > | DO_NOT_DELETE Sheet | XLSX tab | Protected resources and reasons |
> > | Deletion Report | CSV, XLSX | Audit log of what was deleted, when, by whom |
> > | Verification Report | CSV | Post-delete Azure confirmation |
> > | Rollback Instructions | Markdown | Step-by-step restore guide using ARM backups |
> >
> > ### Executive Summary Example
> >
> > ```markdown
> > # EDAV Resource Monitor — Cost Optimization Executive Summary
> > Generated: 2026-06-16T14:23:00Z
> >
> > ## Top-Line Numbers
> > - Total resources scanned: 1,247
> > - Cost optimization opportunities: 89
> > - Estimated monthly savings: $12,450.00
> > - Estimated annual savings: $149,400.00
> >
> > ## Savings by Service
> >   - managedClusters: $4,200.00/month
> >   - managedInstances: $3,500.00/month
> >   - storageAccounts: $2,100.00/month
> >   ...
> > ```
> >
> > ---
> >
> > ## Ownership Discovery
> >
> > The platform automatically discovers resource ownership using multiple methods:
> >
> > ### Discovery Priority
> >
> > 1. **Azure Tags** — checks `owner`, `Owner`, `EDAV_Business_POC`, `EDAV_Created_By`, `EDAV_Project_Name`, `EDAV_Center_Name`, `team`, `cost_center`, `CostCenter`, and more
> > 2. 2. **Resource Group Patterns** — `config/ownership_map.yaml` maps RG name patterns to owners
> >    3. 3. **Subscription Patterns** — `config/ownership_map.yaml` maps subscription names to teams
> >       4. 4. **Resource Name Patterns** — `config/ownership_map.yaml` maps name prefixes to owners
> >         
> >          5. ### Configuring Ownership Map
> >         
> >          6. Edit `config/ownership_map.yaml`:
> >         
> >          7. ```yaml
> > resource_group_patterns:
> >   "ocio-dav-dev-.*":
> >     owner: "EDAV Platform Team"
> >     team: "EDAV Platform - Dev"
> >   "ocio-dav-prd-.*":
> >     owner: "EDAV Platform Team"
> >     team: "EDAV Platform - Production"
> >
> > subscription_patterns:
> >   "OCIO-TSBDEV-.*":
> >     owner: "EDAV Platform Team"
> >     team: "EDAV Platform - Dev"
> >
> > name_patterns:
> >   "edav-prd-.*":
> >     owner: "EDAV Platform Team"
> >     team: "EDAV Platform - Production"
> > ```
> >
> > ### Team Ownership Reports
> >
> > The `OwnershipEngine` generates:
> >
> > - `ownership_report.csv` — every resource with detected owner
> > - `ownership_report.xlsx` — with separate tabs for All Resources, Unowned Resources, Team Summary
> > - `ownership_summary.md` — ownership coverage metrics and top unowned resources
> >
> > ---
> >
> > ## Cost Optimization
> >
> > The `CostOptimizer` engine analyzes all findings and generates:
> >
> > ### Opportunity Types
> >
> > | Type | Description | Example |
> > |---|---|---|
> > | `EMPTY` | Resource with no active workloads/contents | Storage account with no blobs |
> > | `IDLE` | Resource running but with no recent activity | AKS cluster with 0 nodes |
> > | `FAILED` | Resource in error/failed/stopped state | SQL MI in STOPPED state |
> > | `ORPHANED` | Resource with no known owner or consumer | Disconnected private endpoint |
> > | `UNOWNED` | Resource with no ownership tags | Databricks workspace with no tags |
> >
> > ### Cost Report Contents
> >
> > - Total monthly savings estimate (all opportunities)
> > - Estimated annual savings
> > - Breakdown by: Service, Team, Subscription, Resource Group, Opportunity Type
> > - Top 10 individual opportunities by savings
> > - Per-resource action recommendations
> >
> > ---
> >
> > ## Terraform Drift Detection
> >
> > The `TerraformDriftDetector` compares Azure resources against Terraform:
> >
> > ### How It Works
> >
> > 1. Runs `terraform state list` in your Terraform repo directory
> > 2. Scans all `.tf` source files for resource name strings
> > 3. Compares Azure resource names against the combined Terraform inventory
> > 4. Flags: resources NOT in Terraform, resources in Terraform but NOT in Azure
> >
> > ### Running with Terraform Detection
> >
> > ```bash
> > python main.py \
> >   --input inputs/findings_2026-06.csv \
> >   --terraform-path /path/to/terraform-repo \
> >   --audit-only \
> >   --output-dir reports/2026-06/
> > ```
> >
> > ### Drift Report Contents
> >
> > - **Not in Terraform** — manual deployments and shadow IT (with `terraform import` instructions)
> > - - **Not in Azure** — stale Terraform state entries (with `terraform state rm` instructions)
> >   - - **Configuration Drift** — resources that exist in both but have diverged
> >    
> >     - ---
> >
> > ## Dashboard
> >
> > The console dashboard displays a real-time summary after every scan:
> >
> > ```
> > ========================================================================
> >             EDAV Azure Resource Governance Platform
> >                        Scan Dashboard
> >               2026-06-16T14:23:00.000000+00:00
> > ========================================================================
> >
> >   TOP-LINE METRICS
> >   -----------------------------------------------------------------------
> >   Resources scanned:                          1,247
> >   Subscriptions scanned:                          2
> >   Cleanup candidates (SAFE_DELETE):              89
> >   Owner review required:                        156
> >   Protected (DO_NOT_DELETE):                    982
> >   Unknown state:                                 20
> >   Est. monthly savings:                   $12,450.00
> >   Est. annual savings:                   $149,400.00
> >
> >   RESOURCES BY SERVICE
> >   -----------------------------------------------------------------------
> >   Service                        Total  Safe Del   Review  Protected
> >   Storage Accounts               234       12       45        177
> >   AKS                             18        3        5         10
> >   Key Vaults                     156        8       23        125
> >   ...
> >
> >   TERRAFORM DRIFT
> >   -----------------------------------------------------------------------
> >   Not in Terraform (manual deployments):         34
> >   In Terraform but not in Azure:                  7
> >
> >   OWNERSHIP STATUS
> >   -----------------------------------------------------------------------
> >   Unowned resources:                             42
> >   Owner review required:                         42
> >
> > ========================================================================
> >   Nothing is deleted without validation, approval, a ticket, and CONFIRM.
> > ========================================================================
> > ```
> >
> > ---
> >
> > ## Cleanup Workflows
> >
> > ### Standard Cleanup Workflow
> >
> > 1. Run `--audit-only` → review SAFE_DELETE tab
> > 2. 2. Add `ApprovedToDelete=Yes`, `ApprovalTicket=CHGxxxxxxx`, `ApprovedBy=Name` to CSV
> >    3. 3. Run `--cleanup-approved --dry-run` → verify output
> >       4. 4. Run `--cleanup-approved` → type `CONFIRM` at prompt
> >          5. 5. Run `--verify-only` → confirm resources are gone
> >            
> >             6. ### 15-Gate Safety Model
> >            
> >             7. All 15 must pass before any deletion:
> >            
> >             8. | Gate | Check |
> > |---|---|
> > | 1 | `--cleanup-approved` flag passed |
> > | 2 | `ApprovedToDelete=Yes` in input |
> > | 3 | `ApprovalTicket` populated |
> > | 4 | `ApprovedBy` populated |
> > | 5 | Classification = SAFE_DELETE |
> > | 6 | Not on `config/exclusions.txt` |
> > | 7 | Not on `config/denylist.json` |
> > | 8 | Resource still exists in Azure (live check) |
> > | 9 | No Azure resource lock |
> > | 10 | Not Terraform-managed |
> > | 11 | Not production/high environment |
> > | 12 | Resource type supports auto-delete |
> > | 13 | ARM JSON backup created successfully |
> > | 14 | Subscription context verified |
> > | 15 | `CONFIRM` typed at interactive prompt |
> >
> > ---
> >
> > ## Rollback Procedures
> >
> > Every deleted resource has an ARM JSON backup in `backups/`:
> >
> > ```bash
> > # Find the backup
> > ls backups/<resource-name>_*.json
> >
> > # Review rollback instructions
> > cat reports/rollback_instructions.md
> >
> > # Restore using ARM JSON (Azure Portal or CLI)
> > az resource create \
> >   --properties @backups/<resource-name>_<timestamp>.json \
> >   --resource-group <rg-name> \
> >   --resource-type <resource-type>
> > ```
> >
> > > **Note:** Service owners must re-approve private link connections after restoration.
> > >
> > > ---
> > >
> > > ## Security Considerations
> > >
> > > - **Read-only by default.** All runs are read-only unless `--cleanup-approved` is explicitly passed.
> > > - - **No hardcoded credentials.** Uses `az login` / Azure CLI token. No API keys or service principal secrets in code.
> > >   - - **ARM backups before deletion.** Every resource is backed up to JSON before any delete operation.
> > >     - - **Immutable safety gates.** All 15 gates are enforced in code and cannot be bypassed via config.
> > >       - - **Audit logs.** Every run produces a timestamped log in `logs/`.
> > >         - - **Exclusions and denylist.** Critical resources can be permanently protected via `config/exclusions.txt` and `config/denylist.json`.
> > >           - - **Production detection.** Resources matching production patterns are automatically classified `DO_NOT_DELETE`.
> > >             - - **Terraform protection.** Any resource detected in Terraform state or source is never deleted.
> > >              
> > >               - ---
> > >
> > > ## Configuration Reference
> > >
> > > All configuration lives in `config/`:
> > >
> > > | File | Purpose |
> > > |---|---|
> > > | `config/resource_rules.yaml` | Per-resource-type classification rules and auto-delete flags |
> > > | `config/ownership_map.yaml` | Owner/team mapping by RG pattern, subscription, resource name |
> > > | `config/exclusions.txt` | Resources that are NEVER deleted (one name per line) |
> > > | `config/denylist.json` | Hard-blocked names, groups, and patterns |
> > > | `config/allowlist.json` | Pre-approved safe candidates |
> > >
> > > ### Adding a Resource to Exclusions
> > >
> > > Edit `config/exclusions.txt`:
> > > ```
> > > my-critical-keyvault
> > > prod-storage-account-01
> > > edav-prd-aisearch-eastus
> > > ```
> > >
> > > ---
> > >
> > > ## Repository Structure
> > >
> > > ```
> > > edav-private-endpoint-monitor/
> > > ├── main.py                    # Core platform engine (v6.0.0)
> > > ├── dashboard.py               # Console governance dashboard
> > > ├── README.md                  # This file
> > > ├── requirements.txt           # Python dependencies
> > > ├── sample_input.csv           # Example input file
> > > ├── .gitignore
> > > │
> > > ├── monitors/                  # Service monitor modules
> > > │   ├── __init__.py
> > > │   ├── base_monitor.py        # Abstract base class + shared data classes
> > > │   ├── storage_monitor.py     # Storage Accounts
> > > │   ├── aks_monitor.py         # AKS clusters
> > > │   ├── keyvault_monitor.py    # Key Vaults
> > > │   ├── sql_monitor.py         # SQL Server, DB, Managed Instance
> > > │   ├── eventhub_monitor.py    # Event Hub namespaces
> > > │   ├── azureml_monitor.py     # Azure ML workspaces
> > > │   ├── databricks_monitor.py  # Databricks workspaces
> > > │   ├── appservice_monitor.py  # App Services + Function Apps
> > > │   ├── redis_monitor.py       # Redis Cache
> > > │   └── ai_monitors.py         # AI Search, AI Foundry, OpenAI, Event Grid
> > > │
> > > ├── engines/                   # Governance engines
> > > │   ├── cost_optimizer.py      # Cost optimization report generator
> > > │   ├── ownership_engine.py    # Ownership discovery and enrichment
> > > │   └── terraform_drift.py     # Terraform drift detection
> > > │
> > > ├── config/
> > > │   ├── resource_rules.yaml    # Per-resource classification rules
> > > │   ├── ownership_map.yaml     # Team/owner mapping
> > > │   ├── exclusions.txt         # Never-delete list
> > > │   ├── denylist.json          # Hard-blocked resources
> > > │   └── allowlist.json         # Pre-approved candidates
> > > │
> > > ├── examples/
> > > │   ├── sample_dashboard_findings.csv
> > > │   └── sample_approved_cleanup.csv
> > > │
> > > ├── docs/
> > > │   ├── architecture.md
> > > │   ├── workflow.md
> > > │   ├── runbook.md
> > > │   └── mermaid-diagram.md
> > > │
> > > ├── reports/                   # Auto-generated (git-ignored)
> > > ├── backups/                   # ARM JSON backups (git-ignored)
> > > └── logs/                      # Run logs (git-ignored)
> > > ```
> > >
> > > ---
> > >
> > > ## Known Limitations
> > >
> > > - **No direct dashboard API.** Reads CSV/Excel exports. No live API connection to the EDAV dashboard.
> > > - - **Azure CLI required.** Uses `az` CLI for most operations. Azure SDK is available as an optional enhancement.
> > >   - - **Sequential processing.** Resources are validated one at a time. Parallel mode planned.
> > >     - - **NSG auto-delete disabled.** NSG deletion always requires manual confirmation.
> > >       - - **Terraform path required.** Terraform checks require `--terraform-path` pointing to a local checkout.
> > >         - - **Service monitors are advisory.** New monitors (Storage, AKS, etc.) classify but do not auto-delete without `auto_delete_supported=True` in `config/resource_rules.yaml`.
> > >          
> > >           - ---
> > >
> > > ## Version History
> > >
> > > | Version | Date | Changes |
> > > |---|---|---|
> > > | v6.0.0 | June 2026 | **Full platform expansion**: 14 service monitors, Cost Optimizer engine, Ownership Discovery engine, Terraform Drift Detector, console Dashboard, Azure SDK support, expanded README |
> > > | v5.0.0 | June 2026 | Multi-resource type support, 15-gate safety model, ARM backups, ownership detection, executive reports |
> > > | v4.0.0 | April 2026 | Private endpoint monitor with post-delete verification |
> > > | v3.x | February 2026 | Enterprise governance features |
> > >
> > > ---
> > >
> > > *EDAV Azure Resource Governance & Cost Optimization Platform v6.0.0*
> > > *Built for the EDAV Platform Team · CDC · Azure*
> > >
> > > > Nothing is deleted without validation, approval, a change ticket, an approver, and a typed `CONFIRM`.
> > > > 

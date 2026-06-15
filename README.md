# EDAV Azure Resource Governance Platform

> **Enterprise Azure governance, cost-reduction, and safe cleanup platform for the EDAV environment.**
> Built for the EDAV Platform Team at CDC — transforms dashboard findings into fully audited, approval-gated resource cleanup actions.

[![Platform](https://img.shields.io/badge/Platform-Azure-0078D4?logo=microsoftazure&logoColor=white)](https://azure.microsoft.com)
[![Python](https://img.shields.io/badge/Python-3.8%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![Azure CLI](https://img.shields.io/badge/Azure_CLI-2.40%2B-0078D4)](https://aka.ms/install-azure-cli)
[![Safety Gates](https://img.shields.io/badge/Safety_Gates-15-brightgreen)](docs/architecture.md)
[![Default Mode](https://img.shields.io/badge/Default_Mode-Audit_Only-blue)](docs/workflow.md)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

**Dashboard:** https://internal-resource-monitor.edav.cdc.gov/dashboard

> **Nothing is deleted without validation, approval, a change ticket, an approver, and a typed `CONFIRM`.**

---

## Table of Contents

- [What Problem Does This Solve?](#what-problem-does-this-solve)
- [How It Works — Plain English](#how-it-works--plain-english)
- [The Safety Model — 15 Gates](#the-safety-model--15-gates)
- [Quick Start](#quick-start)
- [Monthly Workflow](#monthly-workflow--step-by-step)
- [Supported Resource Types](#supported-resource-types)
- [Resource Classifications](#resource-classifications)
- [Reports Generated](#reports-generated)
- [Execution Modes](#execution-modes)
- [Input File Format](#input-file-format)
- [Configuration Files](#configuration-files)
- [Ownership Detection](#ownership-detection)
- [Terraform Detection](#terraform-detection)
- [Rollback and Recovery](#rollback-and-recovery)
- [Installation](#installation)
- [Troubleshooting](#troubleshooting)
- [Repository Structure](#repository-structure)
- [Where This Fits in the Platform Suite](#where-this-fits-in-the-platform-suite)
- [Known Limitations](#known-limitations)
- [Version History](#version-history)

---

## What Problem Does This Solve?

Azure environments accumulate **orphaned and disconnected resources** over time — private endpoints with no backend, unattached disks, dangling NICs, unused public IPs. These resources:

- **Cost money every month** even though nothing uses them
- **Create compliance risk** because they are undocumented and unowned
- **Clutter dashboards** and make real issues harder to find
- **Take hours to clean up manually** when done one resource at a time

Before this platform, the cleanup process was entirely manual:

1. Export findings from the EDAV dashboard
2. Check each resource in the Azure Portal one at a time
3. Email owners to ask if something is safe to delete
4. Submit change tickets and wait for approval
5. Delete resources manually and hope nothing breaks
6. Update spreadsheets by hand

**This platform automates every step of that process** while adding safety checks the manual process never had.

---

## How It Works — Plain English

Think of this platform as a **smart, cautious assistant** that reviews Azure cleanup work and makes sure nothing is missed or accidentally deleted.

```
EDAV Dashboard
     |
     | (you export a CSV of findings)
     v
This Platform
     |
     |-- Checks each resource still exists in Azure (live validation)
     |-- Checks if it is managed by Terraform (never delete those)
     |-- Checks if it has an Azure resource lock (never delete those)
     |-- Checks if it is a production resource (never delete those)
     |-- Detects who owns it from tags and naming patterns
     |-- Classifies it: SAFE_DELETE / REVIEW_REQUIRED / DO_NOT_DELETE
     |
     v
Executive Report (Excel, HTML, Markdown, JSON, CSV)
     |
     | (you review, get approval, get change ticket)
     v
Dry Run — simulates deletions, nothing actually deleted
     |
     | (you confirm the dry run looks correct)
     v
Live Cleanup — with typed CONFIRM prompt
     |
     v
Verification — confirms deleted resources no longer exist in Azure
```

At every step the platform protects you. If a resource is in Terraform, it will not be deleted. If it has a lock, it will not be deleted. If you have not provided a change ticket and approver name, it will not run. If you do not type `CONFIRM`, nothing happens.

---

## The Safety Model — 15 Gates

Before any resource is deleted, **all 15 conditions must be true**. If even one fails, the resource is skipped and logged — never silently deleted.

| Gate | What Is Checked |
|------|-----------------|
| 1 | `--cleanup-approved` flag was passed explicitly |
| 2 | `ApprovedToDelete` column is `Yes` in the input file |
| 3 | `ApprovalTicket` column is populated (e.g. `CHG0012345`) |
| 4 | `ApprovedBy` column is populated (e.g. `Linda Johnson`) |
| 5 | Resource classification is `SAFE_DELETE` |
| 6 | Resource is NOT on the exclusions list (`config/exclusions.txt`) |
| 7 | Resource is NOT on the denylist (`config/denylist.json`) |
| 8 | Resource still exists in Azure (re-validated live just before deletion) |
| 9 | Resource has no Azure resource lock |
| 10 | Resource is NOT managed by Terraform |
| 11 | Resource is NOT in a production or high environment |
| 12 | Resource type supports auto-delete for this platform |
| 13 | ARM JSON backup was created successfully |
| 14 | Subscription context is verified and correct |
| 15 | You typed `CONFIRM` at the interactive prompt |

> If you are running with `--dry-run`, all 15 gates are evaluated but nothing is deleted. This is the recommended way to preview a cleanup run before going live.

---

## Quick Start

### Prerequisites

- Python 3.8 or higher
- Azure CLI 2.40 or higher — [Install guide](https://aka.ms/install-azure-cli)
- Azure account with **Reader** access (Contributor needed only for live cleanup runs)

### Setup

```bash
# 1. Clone the repository
git clone https://github.com/ausjones84/edav-private-endpoint-monitor
cd edav-private-endpoint-monitor

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Log in to Azure
az login

# If running in a remote or headless session:
az login --use-device-code

# 4. Verify your login worked
az account show

# 5. Run the built-in self-test to confirm everything is set up correctly
python main.py --self-test
```

### Your First Audit Run

```bash
python main.py \
  --input inputs/findings_2026-06.csv \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
  --audit-only \
  --output-dir reports/2026-06/
```

Open the generated Excel file in `reports/2026-06/` and review the **SAFE_DELETE** tab.

---

## Monthly Workflow — Step by Step

Follow these steps in order each month. Do not skip steps.

### Step 1 — Export Findings from the Dashboard

1. Log in to https://internal-resource-monitor.edav.cdc.gov/dashboard
2. Go to **Findings**
3. Filter: **Severity = HIGH**, **Status = Active**
4. Export as CSV and save as `inputs/findings_YYYY-MM.csv`

### Step 2 — Run the Audit (Read-Only)

This step validates resources against Azure and generates a report. **Nothing is deleted.**

```bash
python main.py \
  --input inputs/findings_2026-06.csv \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
  --audit-only \
  --output-dir reports/2026-06/
```

### Step 3 — Review the Report

Open `reports/2026-06/EDAV_Findings_Report_<timestamp>.xlsx`

| Tab | What To Do |
|-----|-----------|
| **SAFE_DELETE** | Review carefully — these are candidates for cleanup |
| **REVIEW_REQUIRED** | Human review needed — do not delete without checking |
| **DO_NOT_DELETE** | Leave these alone — active, locked, or Terraform-managed |
| **Executive Summary** | Share this with leadership for approval |

### Step 4 — Get Approval

Share the Executive Summary with Linda. Get a change ticket number (`CHGxxxxxxx`).

Add these three columns to your input CSV for approved resources:

| Column | Value |
|--------|-------|
| `ApprovedToDelete` | `Yes` |
| `ApprovalTicket` | e.g. `CHG0012345` |
| `ApprovedBy` | e.g. `Linda Johnson` |

Save as `inputs/approved_2026-06.csv`.

### Step 5 — Dry Run

Simulates the cleanup exactly — but **deletes nothing**. Always do this before a live run.

```bash
python main.py \
  --input inputs/approved_2026-06.csv \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
  --cleanup-approved \
  --dry-run \
  --change-ticket CHG0012345 \
  --approved-by "Linda Johnson"
```

Review the output carefully. If anything looks wrong, stop here and investigate.

### Step 6 — Live Cleanup

Only run this after the dry run looks correct and approval is confirmed.

```bash
python main.py \
  --input inputs/approved_2026-06.csv \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
  --cleanup-approved \
  --change-ticket CHG0012345 \
  --approved-by "Linda Johnson" \
  --delete-pause 3
```

You will be shown a summary and prompted to type `CONFIRM`. Nothing is deleted until you do.

### Step 7 — Verify

```bash
python main.py \
  --input inputs/approved_2026-06.csv \
  --verify-only \
  --output-dir reports/2026-06/
```

Then reopen the dashboard and confirm the finding count has decreased.

---

## Supported Resource Types

| Resource Type | Display Name | Auto-Delete Supported | Default Classification |
|--------------|-------------|-----------------------|----------------------|
| `Microsoft.Network/privateEndpoints` | Private Endpoint | Yes | REVIEW_REQUIRED |
| `Microsoft.Network/networkInterfaces` | Network Interface | Yes | REVIEW_REQUIRED |
| `Microsoft.Network/networkSecurityGroups` | Network Security Group | No | REVIEW_REQUIRED |
| `Microsoft.Network/publicIPAddresses` | Public IP Address | Yes | REVIEW_REQUIRED |
| `Microsoft.Compute/disks` | Managed Disk | Yes | REVIEW_REQUIRED |
| `Microsoft.Compute/virtualMachines` | Virtual Machine | No | REVIEW_REQUIRED |
| `Microsoft.Storage/storageAccounts` | Storage Account | No | REVIEW_REQUIRED |
| `Microsoft.KeyVault/vaults` | Key Vault | No | REVIEW_REQUIRED |
| `Microsoft.ContainerRegistry/registries` | Container Registry | No | REVIEW_REQUIRED |
| `Microsoft.EventGrid/topics` | Event Grid Topic | No | REVIEW_REQUIRED |
| `Microsoft.EventGrid/systemTopics` | Event Grid System Topic | No | REVIEW_REQUIRED |
| `Microsoft.EventHub/namespaces` | Event Hub Namespace | No | REVIEW_REQUIRED |
| `Microsoft.Sql/managedInstances` | SQL Managed Instance | No | REVIEW_REQUIRED |
| `Microsoft.MachineLearningServices/workspaces` | ML Workspace | No | REVIEW_REQUIRED |
| Any other type | — | No | UNKNOWN |

> Resources with **Auto-Delete = No** require manual review and confirmation even when classified as SAFE_DELETE. NSG deletion is always manual regardless of state.

---

## Resource Classifications

Every resource receives exactly one of these six classifications:

| Classification | What It Means | What To Do |
|---------------|--------------|------------|
| `SAFE_DELETE` | Confirmed unused, no active connections, no dependencies, approved | Can be deleted after all 15 safety gates pass |
| `REVIEW_REQUIRED` | Uncertain state, has dependencies, or needs team sign-off | Do not delete — contact the resource owner |
| `DO_NOT_DELETE` | Active resource, Terraform-managed, locked, or production | Never delete — leave it alone |
| `UNKNOWN` | Platform could not determine state (missing data or access issue) | Investigate — check subscription access and resource type |
| `RESOURCE_NOT_FOUND` | Resource no longer exists in Azure | Already gone — no action needed |
| `ACCESS_OR_SUBSCRIPTION_REVIEW` | Auth or subscription issue blocked the validation check | Fix authentication and re-run |

---

## Reports Generated

Every run produces a full set of reports in your `--output-dir`:

| Report | Formats | What Is In It |
|--------|---------|--------------|
| **Findings Report** | CSV, Excel, HTML, JSON, Markdown | All scanned resources with classification, owner, cost, and status |
| **Executive Summary** | Markdown, HTML | Counts by classification, resource type, owner, and estimated cost |
| **SAFE_DELETE Sheet** | Excel tab | Confirmed safe candidates — ready for approval |
| **REVIEW_REQUIRED Sheet** | Excel tab | Resources needing team review before any action |
| **DO_NOT_DELETE Sheet** | Excel tab | Protected resources and the reason each is protected |
| **Deletion Report** | CSV, Excel | Full audit log of what was deleted, when, and by whom |
| **Verification Report** | CSV | Post-delete Azure confirmation (ResourceNotFound confirmed) |
| **Rollback Instructions** | Markdown | Step-by-step guide to restore any deleted resource from its ARM backup |
| **Owner/Team Report** | CSV | All findings grouped by team — can be sent directly to resource owners |

---

## Execution Modes

| Mode | Flag(s) | Deletes Resources? | When To Use |
|------|---------|-------------------|-------------|
| **Audit Only** (default) | `--audit-only` | No | First step every month — always start here |
| **Dry Run** | `--cleanup-approved --dry-run` | No (simulated only) | Before any live cleanup to preview what would happen |
| **Live Cleanup** | `--cleanup-approved` | Yes — approved only, all 15 gates | After dry run is reviewed and approval is confirmed |
| **Verify Only** | `--verify-only` | No | After a live cleanup run to confirm resources are gone |
| **Owner Report** | `--generate-owner-report` | No | To send findings to resource owners for follow-up |
| **Self-Test** | `--self-test` | No | To confirm the platform is installed and configured correctly |

---

## Input File Format

The platform reads CSV or Excel files exported directly from the EDAV Resource Monitor dashboard.

### Required Columns

| Column | Description |
|--------|-------------|
| `ResourceName` | The Azure resource name |
| `ResourceGroup` | The Azure resource group name |

### Optional but Recommended Columns

| Column | Description |
|--------|-------------|
| `Subscription` | Subscription name or ID |
| `ResourceType` | Full Azure resource type (e.g. `Microsoft.Network/privateEndpoints`) |
| `ApprovedToDelete` | Set to `Yes` for approved resources |
| `ApprovalTicket` | Change ticket number (e.g. `CHG0012345`) |
| `ApprovedBy` | Full name of the approver (e.g. `Linda Johnson`) |
| `Severity` | Finding severity from the dashboard |
| `Owner` | Resource owner name or email |
| `Team` | Team name |
| `MonthlyCost` | Estimated monthly cost in USD |
| `Environment` | e.g. `dev`, `prod`, `high` |

> The platform accepts many column name aliases. For example, `resource_name`, `Resource`, and `name` all map to `ResourceName`. See `docs/workflow.md` for the full alias table.

---

## Configuration Files

All configuration lives in the `config/` folder. Edit these files to customize behavior for your environment without touching any code.

| File | What It Controls |
|------|-----------------|
| `config/resource_rules.yaml` | Per-resource-type validation rules and classification logic |
| `config/ownership_map.yaml` | Team and owner mapping by resource group, name pattern, and subscription |
| `config/exclusions.txt` | Resources that are **never** deleted — one resource name per line |
| `config/denylist.json` | Hard-blocked resource names, groups, and name patterns |
| `config/allowlist.json` | Pre-approved safe candidates — accelerates classification for known orphans |

### Example — Adding a Resource to the Exclusions List

Open `config/exclusions.txt` and add the resource name, one per line:

```
my-critical-keyvault
prod-storage-account-01
edav-prd-aisearch-eastus
```

Any resource on this list will always be classified `DO_NOT_DELETE`, regardless of what the dashboard reports.

---

## Ownership Detection

The platform automatically detects who owns each resource using multiple methods, in this priority order:

1. **Azure tags** — looks for `owner`, `Owner`, `EDAV_Created_By`, `EDAV_Business_POC`, `EDAV_Project_Name`, `EDAV_Center_Name`, `EDAV_Division_Name`, `application`, `team`
2. **Resource name patterns** — defined in `config/ownership_map.yaml`
3. **Resource group patterns** — e.g. a resource in `ocio-dav-dev-*` maps to the EDAV Platform team
4. **Subscription patterns** — e.g. `OCIO-TSBDEV-C1` maps to EDAV Platform - Dev
5. **CSV input columns** — the `Owner` and `Team` columns from your dashboard export

Edit `config/ownership_map.yaml` to add your team's naming patterns. The more complete this file is, the more accurate the owner reports will be.

---

## Terraform Detection

If a resource is managed by Terraform, it will **never** be deleted by this platform regardless of its classification.

Pass `--terraform-path` pointing to your local Terraform repository to enable this check:

```bash
python main.py \
  --input inputs/findings_2026-06.csv \
  --terraform-path /path/to/your/terraform-repo \
  --audit-only
```

The platform checks two sources:

- **`terraform state list` output** — if the resource name appears here, it is Terraform-managed
- **`.tf` source files** — if the resource name appears in any `.tf` file, it is Terraform-managed

Any resource found in either source is immediately classified `DO_NOT_DELETE`.

> **Note:** Terraform detection requires a local checkout of your Terraform repo. Without `--terraform-path`, Terraform checks are skipped and a warning is displayed.

---

## Rollback and Recovery

Every resource deleted by this platform has a full ARM JSON backup created **before** deletion. If a resource needs to be restored:

1. Find the ARM backup in `backups/<resource-name>_<timestamp>.json`
2. Open `reports/rollback_instructions.md` for step-by-step restore guidance
3. Use the ARM JSON values to reconstruct the resource in Azure
4. Note: Service owners must re-approve any private link connections after restoration

---

## Installation

### Prerequisites

| Requirement | Version | Notes |
|------------|---------|-------|
| Python | 3.8+ | [python.org](https://python.org) |
| Azure CLI | 2.40+ | [aka.ms/install-azure-cli](https://aka.ms/install-azure-cli) |
| Azure access | Reader | For audit runs |
| Azure access | Contributor | For live cleanup runs only |

### Install Steps

```bash
# Clone
git clone https://github.com/ausjones84/edav-private-endpoint-monitor
cd edav-private-endpoint-monitor

# Install Python dependencies
pip install -r requirements.txt

# Log in to Azure
az login

# For remote or headless sessions (copy the code shown in the terminal)
az login --use-device-code

# Confirm you are in the right subscription
az account show

# Confirm the platform is working
python main.py --self-test
```

### Switching Subscriptions

```bash
# List all available subscriptions
az account list --output table

# Switch to a specific subscription
az account set --subscription "OCIO-TSBDEV-C1"

# Confirm the switch
az account show
```

---

## Troubleshooting

| Error or Symptom | What It Means | How to Fix |
|-----------------|--------------|-----------|
| `AZURE LOGIN REQUIRED` | Not logged in to Azure CLI | Run `az login` |
| `Cannot set subscription context` | Subscription name not found | Run `az account list` and check the exact name |
| `AADSTS500173` | Token expired | Run `az login` again |
| `Missing pandas/openpyxl` | Python dependencies not installed | Run `pip install -r requirements.txt` |
| `Classification = UNKNOWN` | Platform cannot see the resource | Check subscription access and confirm resource type is supported |
| `All resources = ACCESS_OR_SUBSCRIPTION_REVIEW` | Auth issue blocking all validation | Re-authenticate with `az login` and verify subscription access |
| `Terraform detection skipped` | `--terraform-path` not provided | Pass `--terraform-path /path/to/tf-repo` to enable Terraform checks |

**Report is empty after audit run**
Check that your CSV has at least a `ResourceName` and `ResourceGroup` column. Run with `-v` for verbose output to see what the parser detected.

**Resources I know are unused show as REVIEW_REQUIRED instead of SAFE_DELETE**
This is by design. REVIEW_REQUIRED means the platform found something it cannot automatically confirm — a dependency, uncertain connection state, or missing data. Review those resources manually before approving them.

**Dry run shows 0 resources would be deleted**
Check that `ApprovedToDelete` is set to `Yes`, `ApprovalTicket` is filled in, and `ApprovedBy` is filled in for the resources you want to clean up. All three are required.

---

## Repository Structure

```
edav-private-endpoint-monitor/
├── main.py                              # Core platform engine (v5.0.0)
├── README.md                            # This file
├── requirements.txt                     # Python dependencies
├── sample_input.csv                     # Example input file to get started
├── .gitignore
│
├── config/
│   ├── resource_rules.yaml              # Per-resource-type validation rules
│   ├── ownership_map.yaml               # Team/owner mapping by RG, name, subscription
│   ├── exclusions.txt                   # Resources that are NEVER deleted
│   ├── denylist.json                    # Hard-blocked names, groups, and patterns
│   └── allowlist.json                   # Pre-approved safe candidates
│
├── examples/
│   ├── sample_dashboard_findings.csv    # Example dashboard export
│   └── sample_approved_cleanup.csv      # Example approved cleanup input
│
├── docs/
│   ├── architecture.md                  # Component architecture overview
│   ├── workflow.md                      # Full step-by-step workflow guide
│   ├── runbook.md                       # Monthly runbook
│   └── mermaid-diagram.md               # Architecture diagrams (Mermaid)
│
├── reports/                             # Auto-generated reports (git-ignored)
├── backups/                             # ARM JSON backups before deletion (git-ignored)
└── logs/                                # Run logs (git-ignored)
```

---

## Where This Fits in the Platform Suite

This repository is one part of a broader EDAV Azure Governance Platform. All four tools share the same `az login` authentication model, default to read-only or dry-run modes, and require explicit confirmation before any write operation.

```
EDAV Azure Governance Platform
│
├── edav-private-endpoint-monitor              ← YOU ARE HERE
│     Finds, classifies, and safely removes orphaned dashboard findings
│
├── azure-terraform-dr-discovery-tool
│     Discovers Azure resources and compares them against Terraform state
│     Generates drift reports and Azure DevOps ticket drafts
│
├── azure-terraform-module-builder
│     Reads DR discovery output and generates Terraform module scaffolds
│     for resources that exist in Azure but are not in Terraform yet
│
└── ssl-certificate-lifecycle-platform
      Manages the full SSL certificate lifecycle end-to-end
      Discovery, CSR, chain building, Key Vault import, App Service validation
```

---

## Known Limitations

- **No direct dashboard API** — the platform reads CSV/Excel exports. There is no live API connection to the EDAV dashboard.
- **Azure CLI required** — uses `az` CLI commands, not the Azure Python SDK. SDK integration is planned for a future version.
- **Sequential processing** — resources are validated one at a time. Parallel mode is planned.
- **NSG auto-delete disabled** — NSG deletion requires manual confirmation even when unattached.
- **Terraform path required** — Terraform checks only work if you pass `--terraform-path` with a local checkout of your Terraform repo.

---

## Version History

| Version | Date | What Changed |
|---------|------|-------------|
| v5.0.0 | June 2026 | Complete rewrite — multi-resource type support, dashboard integration, ownership detection, 15-gate safety model, full classification system, owner/team reports, ARM backups, post-delete verification |
| v4.0.0 | April 2026 | Private endpoint monitor with post-delete verification |
| v3.x | February 2026 | Enterprise governance features |

---

> **EDAV Azure Resource Governance Platform v5.0.0**
> Built for the EDAV Platform Team · CDC · Azure
>
> *Nothing is deleted without validation, approval, a change ticket, an approver, and a typed `CONFIRM`.*

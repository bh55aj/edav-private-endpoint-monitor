# EDAV Resource Monitor Cleanup Platform - Architecture

## Overview

The EDAV Resource Monitor Cleanup Platform is a Python-based, safety-first enterprise cleanup tool designed to help the EDAV team reduce Azure cloud costs and eliminate dashboard findings from the [EDAV Resource Monitor](https://internal-resource-monitor.edav.cdc.gov/dashboard).

It supports multiple Azure resource types, enforces a multi-layered approval and safety model, and produces audit-quality reports suitable for sharing with Linda and the broader EDAV platform team.

## Design Principles

**Safety First**: Nothing is deleted automatically. Every deletion requires validation, classification, approval, ticket reference, approver identity, and an interactive typed CONFIRM. Fifteen safety gates must all pass before any resource is deleted.

**Dashboard-Driven**: The tool is designed to consume exports from the EDAV Resource Monitor dashboard Findings page, not to scan broadly. You tell it what the dashboard found; the tool validates, classifies, and safely handles them.

**Multi-Resource**: Supports 15+ Azure resource types. New types are added by extending resource_rules.yaml — no code changes required for basic support.

**Audit-Quality Reports**: Every run produces CSV, Excel, HTML, JSON, and Markdown reports. Designed to produce a clear executive summary for Linda and a detailed technical report for the platform team.

**Cost-Aware**: Where cost data is available in the input file, the platform summarizes estimated monthly savings from safe deletions.

**Owner-Aware**: Ownership is detected from Azure tags, resource group patterns, subscription names, and the ownership_map.yaml config. Owner-specific reports are generated for targeted follow-up.

## Repository Structure

```
edav-private-endpoint-monitor/
├── main.py                          # Core platform engine (v5.0.0)
├── README.md                        # Full documentation
├── requirements.txt                 # Python dependencies
├── .gitignore                       # Git ignore patterns
│
├── config/                          # Configuration files
│   ├── resource_rules.yaml          # Per-resource-type validation rules
│   ├── ownership_map.yaml           # Resource group/name -> team mapping
│   ├── exclusions.txt               # Resources never deleted
│   ├── denylist.json               # Hard-blocked resource names/patterns
│   └── allowlist.json              # Pre-approved safe candidates
│
├── examples/                        # Sample input files
│   ├── sample_dashboard_findings.csv  # Example dashboard export
│   └── sample_approved_cleanup.csv    # Example approved cleanup input
│
├── docs/                            # Documentation
│   ├── architecture.md              # This file
│   ├── workflow.md                  # Detailed workflow guide
│   ├── runbook.md                   # Monthly runbook
│   └── mermaid-diagram.md           # Architecture diagrams
│
├── reports/                         # Auto-generated reports (git-ignored output)
├── backups/                         # ARM JSON backups (git-ignored output)
└── logs/                            # Run logs (git-ignored output)
```

## Core Components

### main.py

The single-file platform engine. Contains:

- **PreflightChecker**: Validates Azure CLI, login, tenant, subscription access, token validity
- **ConfigLoader**: Loads all config files (rules, ownership map, exclusions, denylist, allowlist)
- **InputParser**: Parses CSV/Excel input from dashboard, normalizes column aliases, validates required fields
- **AzureValidator**: Per-resource-type validation logic using Azure CLI commands
- **OwnershipDetector**: Maps resources to teams using tags, RG patterns, subscription patterns
- **TerraformChecker**: Checks if resource appears in terraform state or .tf source files
- **ResourceClassifier**: Applies classification logic based on validation results
- **SafetyGate**: Enforces all 15 pre-deletion safety checks
- **CleanupEngine**: Handles ARM backup, deletion, and post-delete verification
- **ReportGenerator**: Produces all output reports in all formats
- **ExecutiveDashboard**: Prints console summary at run end

### Execution Modes

| Mode | Flag | Deletes? | Use Case |
|------|------|----------|----------|
| Audit Only | --audit-only | No | Monthly discovery run |
| Dry Run | --dry-run | No (simulated) | Preview what would be deleted |
| Cleanup Approved | --cleanup-approved | Yes (approved only) | Monthly cleanup run |
| Verify Only | --verify-only | No | Check previous deletions |
| Owner Report | --generate-owner-report | No | Generate team-specific reports |

### Classification System

| Classification | Meaning | Can Delete? |
|---|---|---|
| SAFE_DELETE | Confirmed unused/orphaned, approved, no deps | YES - if all gates pass |
| REVIEW_REQUIRED | Uncertain, has deps, needs team confirmation | NO |
| DO_NOT_DELETE | Active, Terraform-managed, locked, production | NO |
| UNKNOWN | Insufficient data, access issue | NO |
| RESOURCE_NOT_FOUND | Resource no longer exists in Azure | N/A |
| ACCESS_OR_SUBSCRIPTION_REVIEW | Auth/subscription issue blocked validation | NO |

### Safety Gate Model

15 ordered safety gates must all pass before any deletion:

1. --cleanup-approved mode required
2. ApprovedToDelete = Yes in input file
3. ApprovalTicket populated
4. ApprovedBy populated
5. Classification = SAFE_DELETE
6. Resource not on denylist
7. Resource not on exclusions list
8. Resource not Terraform-managed
9. No Azure lock on resource
10. Resource not tagged as production/protected
11. ARM JSON backup written to backups/ directory
12. Pre-delete re-validation: resource still exists AND still meets safe criteria
13. Subscription context verified before delete
14. Interactive CONFIRM prompt (user must type "CONFIRM")
15. Resource type supports auto-deletion (per resource_rules.yaml)

### Supported Resource Types

| Resource Type | Auto-Delete | Default Classification |
|---|---|---|
| Microsoft.Network/privateEndpoints | Yes | REVIEW_REQUIRED |
| Microsoft.Network/networkInterfaces | Yes | REVIEW_REQUIRED |
| Microsoft.Network/networkSecurityGroups | No | REVIEW_REQUIRED |
| Microsoft.Network/publicIPAddresses | Yes | REVIEW_REQUIRED |
| Microsoft.Compute/disks | Yes | REVIEW_REQUIRED |
| Microsoft.Compute/virtualMachines | No | REVIEW_REQUIRED |
| Microsoft.Storage/storageAccounts | No | REVIEW_REQUIRED |
| Microsoft.KeyVault/vaults | No | REVIEW_REQUIRED |
| Microsoft.ContainerRegistry/registries | No | REVIEW_REQUIRED |
| Microsoft.EventGrid/topics | No | REVIEW_REQUIRED |
| Microsoft.EventGrid/systemTopics | No | REVIEW_REQUIRED |
| Microsoft.EventHub/namespaces | No | REVIEW_REQUIRED |
| Microsoft.Sql/managedInstances | No | REVIEW_REQUIRED |
| Microsoft.MachineLearningServices/workspaces | No | REVIEW_REQUIRED |
| Any other type | No | UNKNOWN |

## Dependencies

- Python 3.8+
- Azure CLI 2.40+
- pandas >= 2.0.0
- openpyxl >= 3.1.0
- pyyaml >= 6.0.0
- jinja2 >= 3.1.0 (HTML report generation)
- colorama >= 0.4.6 (colored console output)

## Security Model

- No service principal or managed identity credentials are stored in this tool
- All Azure operations use the active az login session of the running user
- Token expiry is detected mid-run and raises an immediate stop with re-auth instructions
- No resources are accessed via hard-coded credentials
- ARM backups are stored locally only - never transmitted

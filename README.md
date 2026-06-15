# EDAV Resource Monitor Cleanup Platform v5.0.0

> **Enterprise Azure governance, cost-reduction, and safe cleanup platform for the EDAV Resource Monitor dashboard.**

[![Platform](https://img.shields.io/badge/Platform-Azure-blue)](https://azure.microsoft.com)
[![Python](https://img.shields.io/badge/Python-3.8%2B-green)](https://python.org)
[![Safety](https://img.shields.io/badge/Safety-15_Gate_Model-brightgreen)](docs/architecture.md)

**Dashboard:** [https://internal-resource-monitor.edav.cdc.gov/dashboard](https://internal-resource-monitor.edav.cdc.gov/dashboard)

Nothing is deleted without validation, approval, a change ticket, an approver, and a typed `CONFIRM`.

---

## What This Tool Does

The EDAV Resource Monitor Cleanup Platform transforms dashboard findings into safe, documented, owner-tracked Azure resource cleanup actions. It:

- Ingests findings exported from the EDAV Resource Monitor dashboard (CSV or Excel)
- Validates each resource against Azure to confirm current state
- Classifies every resource as SAFE_DELETE, REVIEW_REQUIRED, DO_NOT_DELETE, UNKNOWN, or RESOURCE_NOT_FOUND
- Detects resource ownership from Azure tags, resource group patterns, and subscription names
- Generates executive-quality reports (CSV, Excel, HTML, JSON, Markdown)
- Enforces 15 safety gates before any deletion
- Creates ARM JSON backups before every deletion
- Verifies deletions by confirming Azure returns ResourceNotFound
- Produces owner-specific reports for team follow-up

---

## Repository Structure

```
edav-private-endpoint-monitor/
├── main.py                          # Core platform engine (v5.0.0)
├── README.md                        # This file
├── requirements.txt                 # Python dependencies
├── .gitignore
│
├── config/
│   ├── resource_rules.yaml          # Per-resource-type validation rules
│   ├── ownership_map.yaml           # Team/owner mapping by RG, name, subscription
│   ├── exclusions.txt               # Resources that are NEVER deleted
│   ├── denylist.json               # Hard-blocked names, groups, and patterns
│   └── allowlist.json              # Pre-approved safe candidates
│
├── examples/
│   ├── sample_dashboard_findings.csv  # Example dashboard export
│   └── sample_approved_cleanup.csv    # Example approved cleanup input
│
├── docs/
│   ├── architecture.md              # Architecture overview
│   ├── workflow.md                  # Step-by-step workflow guide
│   ├── runbook.md                   # Monthly runbook
│   └── mermaid-diagram.md           # Architecture diagrams (Mermaid)
│
├── reports/                         # Auto-generated reports (git-ignored)
├── backups/                         # ARM JSON backups (git-ignored)
└── logs/                            # Run logs (git-ignored)
```

---

## Supported Azure Resource Types

| Resource Type | Auto-Delete | Default |
|---|---|---|
| Microsoft.Network/privateEndpoints | ✅ Yes | REVIEW_REQUIRED |
| Microsoft.Network/networkInterfaces | ✅ Yes | REVIEW_REQUIRED |
| Microsoft.Network/networkSecurityGroups | ❌ No | REVIEW_REQUIRED |
| Microsoft.Network/publicIPAddresses | ✅ Yes | REVIEW_REQUIRED |
| Microsoft.Compute/disks | ✅ Yes | REVIEW_REQUIRED |
| Microsoft.Compute/virtualMachines | ❌ No | REVIEW_REQUIRED |
| Microsoft.Storage/storageAccounts | ❌ No | REVIEW_REQUIRED |
| Microsoft.KeyVault/vaults | ❌ No | REVIEW_REQUIRED |
| Microsoft.ContainerRegistry/registries | ❌ No | REVIEW_REQUIRED |
| Microsoft.EventGrid/topics | ❌ No | REVIEW_REQUIRED |
| Microsoft.EventGrid/systemTopics | ❌ No | REVIEW_REQUIRED |
| Microsoft.EventHub/namespaces | ❌ No | REVIEW_REQUIRED |
| Microsoft.Sql/managedInstances | ❌ No | REVIEW_REQUIRED |
| Microsoft.MachineLearningServices/workspaces | ❌ No | REVIEW_REQUIRED |
| Any other type | ❌ No | UNKNOWN |

---

## Classification System

| Classification | Meaning | Action |
|---|---|---|
| **SAFE_DELETE** | Confirmed unused, approved, no dependencies | Delete (if all 15 safety gates pass) |
| **REVIEW_REQUIRED** | Uncertain, has deps, or needs team confirmation | Do not delete - reach out to team |
| **DO_NOT_DELETE** | Active, TF-managed, locked, or production | Never delete |
| **UNKNOWN** | Cannot determine state - missing data or access issue | Investigate |
| **RESOURCE_NOT_FOUND** | Resource no longer exists in Azure | Already gone |
| **ACCESS_OR_SUBSCRIPTION_REVIEW** | Auth or subscription context issue blocked validation | Fix auth and re-run |

---

## Dashboard Workflow

### Step 1: Export from Dashboard

1. Login: [https://internal-resource-monitor.edav.cdc.gov/dashboard](https://internal-resource-monitor.edav.cdc.gov/dashboard)
2. Navigate to **Findings**
3. Filter: Severity = HIGH, Status = Active
4. Export CSV → save as `inputs/findings_YYYY-MM.csv`

### Step 2: Audit Run

```bash
python main.py \
  --input inputs/findings_YYYY-MM.csv \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
  --audit-only \
  --output-dir reports/YYYY-MM/
```

### Step 3: Review Reports

Open `reports/YYYY-MM/EDAV_Findings_Report_<ts>.xlsx` and review the SAFE_DELETE sheet.

### Step 4: Get Approval

Share the executive summary with Linda. Get change ticket approved (CHGxxxxxxx).

### Step 5: Dry Run

```bash
python main.py \
  --input inputs/approved_YYYY-MM.csv \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
  --cleanup-approved \
  --dry-run \
  --change-ticket CHG0012345 \
  --approved-by "Linda Johnson"
```

### Step 6: Live Cleanup

```bash
python main.py \
  --input inputs/approved_YYYY-MM.csv \
  --subscriptions "OCIO-TSBDEV-C1,OCIO-TSBPRD-C1" \
  --cleanup-approved \
  --change-ticket CHG0012345 \
  --approved-by "Linda Johnson" \
  --delete-pause 3
```

Type `CONFIRM` when prompted.

### Step 7: Verify

Re-open the dashboard and confirm finding count has decreased.

---

## Approval Workflow

Deletion requires ALL of these in the input file:

| Field | Required Value |
|-------|---------------|
| ApprovedToDelete | `Yes` |
| ApprovalTicket | e.g., `CHG0012345` |
| ApprovedBy | e.g., `Linda Johnson` |

Classification must be **SAFE_DELETE** and all 15 safety gates must pass.

---

## Input File Format

Required columns: **ResourceName**, **ResourceGroup**

Optional but recommended: Subscription, ResourceType, ApprovedToDelete, ApprovalTicket, ApprovedBy, Severity, Owner, Team, MonthlyCost, Environment

The parser accepts many column name aliases. See [docs/workflow.md](docs/workflow.md) for the full alias table.

---

## Safety Model - 15 Gates

Before any resource is deleted, all 15 gates must pass:

1. `--cleanup-approved` mode required
2. `ApprovedToDelete = Yes`
3. ApprovalTicket populated
4. ApprovedBy populated
5. Classification = SAFE_DELETE
6. Not on exclusions list (`config/exclusions.txt`)
7. Not on denylist (`config/denylist.json`)
8. Resource exists in Azure (re-validated just before deletion)
9. No Azure resource lock
10. Not Terraform-managed
11. Not production/high environment
12. Resource type supports auto-delete
13. ARM JSON backup created successfully
14. Subscription context verified
15. User types `CONFIRM` at interactive prompt

---

## Execution Modes

| Mode | Flag | Deletes? |
|------|------|----------|
| Audit Only (default) | `--audit-only` | No |
| Dry Run | `--cleanup-approved --dry-run` | No (simulated) |
| Live Cleanup | `--cleanup-approved` | Yes (approved only) |
| Verify Only | `--verify-only` | No |
| Owner Report | `--generate-owner-report` | No |
| Self-Test | `--self-test` | No |

---

## Reports Generated

| Report | Format | Contents |
|--------|--------|----------|
| Findings Report | CSV, Excel, HTML, JSON, Markdown | All resources with classification |
| Executive Summary | Markdown, HTML | Counts by classification/type/owner/cost |
| SAFE_DELETE Sheet | Excel tab | Confirmed safe candidates |
| REVIEW_REQUIRED Sheet | Excel tab | Resources needing team review |
| DO_NOT_DELETE Sheet | Excel tab | Protected resources |
| Deletion Report | CSV, Excel | What was deleted and when |
| Verification Report | CSV | Post-delete Azure confirmation |
| Rollback Instructions | Markdown | How to restore deleted resources |
| Owner/Team Report | CSV | Findings grouped by team |

---

## Installation

### Prerequisites

- Python 3.8+
- Azure CLI 2.40+: [https://aka.ms/install-azure-cli](https://aka.ms/install-azure-cli)
- Azure account with Reader access (Contributor for deletions)

### Setup

```bash
# Clone
git clone https://github.com/ausjones84/edav-private-endpoint-monitor
cd edav-private-endpoint-monitor

# Install dependencies
pip install -r requirements.txt

# Login to Azure
az login
# OR for remote sessions:
az login --use-device-code

# Verify login
az account show

# Run preflight check
python main.py --self-test
```

---

## Ownership Detection

Owner and team are automatically detected from:

1. **Azure tags**: `owner`, `Owner`, `EDAV_Created_By`, `EDAV_Business_POC`, `EDAV_Project_Name`, `EDAV_Center_Name`, `EDAV_Division_Name`, `application`, `team`
2. **Resource name patterns**: defined in `config/ownership_map.yaml`
3. **Resource group patterns**: e.g., `ocio-dav-dev` → EDAV Platform
4. **Subscription patterns**: e.g., `OCIO-TSBDEV-C1` → EDAV Platform - Dev
5. **CSV input fields**: Owner and Team columns from dashboard export

Edit `config/ownership_map.yaml` to add your team's patterns.

---

## Terraform Detection

Pass `--terraform-path /path/to/your/tf/repo` to check:
- `terraform state list` output
- `.tf` source files for resource name references

If a resource is found in Terraform, it is classified **DO_NOT_DELETE**.

---

## Rollback / Recovery

If a resource needs to be restored:
1. Check `backups/` for the ARM JSON backup
2. Open `reports/rollback_instructions.md`
3. Reconstruct using the ARM JSON values
4. Service owners must re-approve any private link connections

---

## Troubleshooting

| Error | Fix |
|-------|-----|
| `AZURE LOGIN REQUIRED` | Run `az login` |
| `Cannot set subscription context` | Run `az account list` and verify subscription name |
| `AADSTS500173` | Token expired - run `az login` |
| `Missing pandas/openpyxl` | Run `pip install -r requirements.txt` |
| Classification = UNKNOWN | Check subscription access and resource type |
| All resources = ACCESS_OR_SUBSCRIPTION_REVIEW | Re-authenticate with `az login` |

---

## Known Limitations

- **No direct dashboard API**: Tool consumes CSV/Excel exports - no live dashboard connection
- **Azure CLI required**: Uses `az` CLI, not Azure SDK (SDK upgrade planned)
- **Sequential processing**: Resources validated one at a time (parallel mode planned)
- **NSG auto-delete disabled**: NSG deletion requires manual confirmation even when unattached
- **Terraform path required**: Terraform check only works if you have the TF repo checked out locally

---

## How to Use Monthly

1. **Week 1**: Export from dashboard → run audit → share report with Linda
2. **Week 1-2**: Get change ticket approved → run dry-run → review
3. **Week 2**: Run live cleanup → verify on dashboard → send final report

See [docs/runbook.md](docs/runbook.md) for the complete monthly runbook.

---

## Architecture

See [docs/architecture.md](docs/architecture.md) for component details and [docs/mermaid-diagram.md](docs/mermaid-diagram.md) for visual flow diagrams.

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| v5.0.0 | 2026-06 | Complete rewrite: multi-resource support, dashboard integration, ownership detection, 15-gate safety model, classification system, owner reports |
| v4.0.0 | 2026-04 | Private endpoint monitor with post-delete verification |
| v3.x | 2026-02 | Enterprise governance features |

---

*EDAV Resource Monitor Cleanup Platform v5.0.0 — Built for the EDAV Platform Team*

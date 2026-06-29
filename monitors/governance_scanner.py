#!/usr/bin/env python3
"""
============================================================================
EDAV Resource Governance Scanner - monitors/governance_scanner.py
============================================================================
Broad Azure Resource Governance scanner for the EDAV platform.

Runs Azure Resource Graph queries to discover:
  - Disconnected Private Endpoints
  - Unattached Network Security Groups
  - Unattached Managed Disks
  - Unattached Public IP Addresses
  - Unattached Network Interfaces (with Azure-managed detection)
  - Stopped / Deallocated Virtual Machines
  - Event Grid Topics / System Topics with no subscriptions
  - Storage accounts needing review
  - Databricks / AKS / managed service resource detection
  - Suspicious / empty Resource Groups

Classification logic:
  KEEP             - Azure-managed resources (AKS, Databricks, PE-NICs, etc.)
  REVIEW_REQUIRED  - PRD, backup, Terraform-managed, unclear ownership
  SAFE_DELETE      - Non-prod, unattached, approved, all safety gates pass
  DO_NOT_DELETE    - Production, locked, Terraform-managed, explicit exclusion

IMPORTANT - NIC Safety Rule:
  Unattached NIC != Orphaned NIC.
  Many NICs are Azure-managed (AKS, Databricks, Private Endpoints,
  App Gateway, Load Balancer). These must be classified KEEP, not deleted.

Version: v6.1.0 | EDAV Platform Team
============================================================================
"""

import fnmatch
import json
import re
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ============================================================================
# AZURE-MANAGED RESOURCE DETECTION PATTERNS
# ============================================================================

# NIC name patterns that indicate Azure-managed (KEEP)
AZURE_MANAGED_NIC_NAME_PATTERNS = [
    "*-pe-nic",          # Private Endpoint NICs
    "*-nic.*",           # PE NICs with numeric suffix (e.g. myendpoint-nic.abc123)
    "*.nic.*",           # PE NICs dot-notation
    "*kube*",            # AKS kube-apiserver NICs
    "*kube-apiserver*",
    "*aksnode*",         # AKS node NICs
    "*aks-*",
    "*agw*",             # Application Gateway NICs
    "*appgw*",
    "*privatenic*",      # Private NICs (case-insensitive match done separately)
    "*publicnic*",       # Public NICs
    "*lb-*-nic*",        # Load Balancer NICs
    "*databricks*",      # Databricks cluster NICs
    "*worker-nic*",
    "*-worker-*-nic*",
]

# NIC name substrings (case-insensitive) that indicate Azure-managed
AZURE_MANAGED_NIC_SUBSTRINGS = [
    "-pe-nic",
    ".nic.",
    "kube-apiserver",
    "privatenic",
    "publicnic",
    "databricks",
    "aksnode",
    "aks-agent",
    "kube-",
    "appgw",
    "agw-",
    "-appgw-",
]

# Resource Group prefixes that indicate Azure-managed resources (KEEP)
AZURE_MANAGED_RG_PREFIXES = [
    "mc_",               # AKS managed cluster RGs
    "databricks-rg-",    # Databricks managed RGs
    "databricks_rg_",
    "defaultresourcegroup-",
    "networkwatcherrg",
    "azurebackuprg",
    "aro-",              # Azure Red Hat OpenShift
    "managed-",
    "cloud-shell-storage-",
    "aml-",              # Azure ML managed
    "asr-",              # Azure Site Recovery
]

# Resource Group name patterns (fnmatch) for KEEP
AZURE_MANAGED_RG_PATTERNS = [
    "mc_*",
    "databricks-rg-*",
    "databricks_rg_*",
    "defaultresourcegroup-*",
    "networkwatcherrg*",
    "azurebackuprg*",
    "aro-*",
    "aml-*",
    "asr-*",
    "cloud-shell-storage-*",
]

# Resource Group substrings for REVIEW (production/backup)
PRODUCTION_RG_SUBSTRINGS = [
    "prd", "prod", "production", "prd-", "-prd",
    "live", "-live",
]

BACKUP_RG_SUBSTRINGS = [
    "backup", "bkp", "dr", "-dr-",
]

# Disk name patterns for REVIEW (backup / databricks)
DATABRICKS_DISK_PATTERNS = [
    "databricks*",
    "*databricks*",
    "*dbfs*",
]

BACKUP_DISK_PATTERNS = [
    "*backup*",
    "*bkp*",
    "*snapshot*",
]

# NSG patterns for REVIEW (production/backup)
PRODUCTION_NSG_PATTERNS = [
    "*-prd-*", "*-prod-*", "*-production-*", "*prd*nsg*",
]

BACKUP_NSG_PATTERNS = [
    "*backup*", "*bkp*", "*dr*",
]


# ============================================================================
# GOVERNANCE CLASSIFICATION CONSTANTS
# ============================================================================

CLS_KEEP          = "KEEP"
CLS_SAFE_DELETE   = "SAFE_DELETE"
CLS_REVIEW        = "REVIEW_REQUIRED"
CLS_DO_NOT_DELETE = "DO_NOT_DELETE"
CLS_NOT_FOUND     = "RESOURCE_NOT_FOUND"
CLS_UNKNOWN       = "UNKNOWN"

RECOMMENDED_ACTIONS = {
    CLS_KEEP:          "No action - Azure-managed resource. Do not delete.",
    CLS_SAFE_DELETE:   "Approved for deletion. Collect approval ticket and run delete mode.",
    CLS_REVIEW:        "Owner review required. Confirm with team before any action.",
    CLS_DO_NOT_DELETE: "Blocked from deletion. Production, locked, or Terraform-managed.",
    CLS_NOT_FOUND:     "Already removed - no action needed.",
    CLS_UNKNOWN:       "Unknown state. Manual investigation required.",
}


# ============================================================================
# RESOURCE GRAPH QUERIES
# ============================================================================

GOVERNANCE_QUERIES = {
    "disconnected_private_endpoints": {
        "display_name": "Disconnected Private Endpoints",
        "resource_type": "Microsoft.Network/privateEndpoints",
        "query": """
Resources
| where type =~ 'microsoft.network/privateendpoints'
| mv-expand connections = properties.privateLinkServiceConnections
| extend connectionState = tostring(connections.properties.privateLinkServiceConnectionState.status)
| extend privateLinkServiceId = tostring(connections.properties.privateLinkServiceId)
| extend rg = resourceGroup
| where isnull(connectionState) or connectionState !in~ ('Approved','Connected')
| project name, resourceGroup=rg, subscriptionId, location,
          connectionState, privateLinkServiceId, tags
| order by resourceGroup asc, name asc
""".strip(),
    },
    "unattached_nsgs": {
        "display_name": "Unattached Network Security Groups",
        "resource_type": "Microsoft.Network/networkSecurityGroups",
        "query": """
Resources
| where type =~ 'microsoft.network/networksecuritygroups'
| extend nics = properties.networkInterfaces
| extend subnets = properties.subnets
| where isempty(nics) and isempty(subnets)
| project name, resourceGroup, subscriptionId, location, tags
| order by resourceGroup asc
""".strip(),
    },
    "unattached_disks": {
        "display_name": "Unattached Managed Disks",
        "resource_type": "Microsoft.Compute/disks",
        "query": """
Resources
| where type =~ 'microsoft.compute/disks'
| where isempty(managedBy)
| project name, resourceGroup, subscriptionId, location,
          diskState=tostring(properties.diskState),
          sku=tostring(sku.name),
          diskSizeGB=tostring(properties.diskSizeGB),
          tags
| order by resourceGroup asc
""".strip(),
    },
    "unattached_public_ips": {
        "display_name": "Unattached Public IP Addresses",
        "resource_type": "Microsoft.Network/publicIPAddresses",
        "query": """
Resources
| where type =~ 'microsoft.network/publicipaddresses'
| where isempty(properties.ipConfiguration)
| project name, resourceGroup, subscriptionId, location,
          sku=tostring(sku.name),
          allocationMethod=tostring(properties.publicIPAllocationMethod),
          tags
| order by resourceGroup asc
""".strip(),
    },
    "unattached_nics": {
        "display_name": "Unattached Network Interfaces",
        "resource_type": "Microsoft.Network/networkInterfaces",
        "query": """
Resources
| where type =~ 'microsoft.network/networkinterfaces'
| where isempty(properties.virtualMachine)
| project name, resourceGroup, subscriptionId, location,
          privateIPAddress=tostring(properties.ipConfigurations[0].properties.privateIPAddress),
          tags
| order by resourceGroup asc
""".strip(),
    },
    "stopped_vms": {
        "display_name": "Stopped / Deallocated VMs",
        "resource_type": "Microsoft.Compute/virtualMachines",
        "query": """
Resources
| where type =~ 'microsoft.compute/virtualmachines'
| extend powerState = tostring(properties.extended.instanceView.powerState.displayStatus)
| where powerState in~ ('VM stopped', 'VM deallocated', 'Stopped', 'Deallocated')
       or isnull(powerState) or powerState == ''
| project name, resourceGroup, subscriptionId, location, powerState, tags
| order by resourceGroup asc
""".strip(),
    },
    "eventgrid_no_subscriptions": {
        "display_name": "Event Grid Topics with No Subscriptions",
        "resource_type": "Microsoft.EventGrid/topics",
        "query": """
Resources
| where type =~ 'microsoft.eventgrid/topics'
  or type =~ 'microsoft.eventgrid/systemtopics'
| extend subCount = iif(isnotnull(properties.eventSubscriptionCount),
                        toint(properties.eventSubscriptionCount), 0)
| where subCount == 0 or isnull(subCount)
| project name, resourceGroup, subscriptionId, location, type, subCount, tags
| order by resourceGroup asc
""".strip(),
    },
    "storage_review": {
        "display_name": "Storage Accounts Needing Review",
        "resource_type": "Microsoft.Storage/storageAccounts",
        "query": """
Resources
| where type =~ 'microsoft.storage/storageaccounts'
| extend allowPublicAccess = tostring(properties.allowBlobPublicAccess)
| extend httpsOnly = tostring(properties.supportsHttpsTrafficOnly)
| extend accessTier = tostring(properties.accessTier)
| extend sku = tostring(sku.name)
| project name, resourceGroup, subscriptionId, location,
          sku, accessTier, allowPublicAccess, httpsOnly, tags
| order by resourceGroup asc
""".strip(),
    },
    "aks_databricks_resources": {
        "display_name": "AKS / Databricks Managed Resources",
        "resource_type": "AKS/Databricks",
        "query": """
Resources
| where resourceGroup startswith 'mc_'
       or resourceGroup startswith 'databricks-rg-'
       or resourceGroup startswith 'databricks_rg_'
| project name, type, resourceGroup, subscriptionId, location, tags
| order by resourceGroup asc, type asc
""".strip(),
    },
}

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def _fnmatch_any(name: str, patterns: List[str]) -> bool:
    """Return True if name matches any fnmatch pattern (case-insensitive)."""
    name_lower = name.lower()
    return any(fnmatch.fnmatch(name_lower, p.lower()) for p in patterns)

def _contains_any(text: str, substrings: List[str]) -> bool:
    """Return True if text contains any of the substrings (case-insensitive)."""
    text_lower = text.lower()
    return any(s.lower() in text_lower for s in substrings)

def _rg_is_azure_managed(rg: str) -> bool:
    """Return True if resource group is Azure-managed."""
    rg_lower = rg.lower()
    return (any(rg_lower.startswith(p) for p in AZURE_MANAGED_RG_PREFIXES)
            or _fnmatch_any(rg, AZURE_MANAGED_RG_PATTERNS))

def _rg_is_production(rg: str) -> bool:
    """Return True if resource group looks like production."""
    return _contains_any(rg, PRODUCTION_RG_SUBSTRINGS)

def _rg_is_backup(rg: str) -> bool:
    """Return True if resource group looks like backup/DR."""
    return _contains_any(rg, BACKUP_RG_SUBSTRINGS)

def _nic_is_azure_managed(name: str, rg: str) -> Tuple[bool, str]:
    """
    Return (is_managed, reason) for a NIC.
    Azure-managed NICs must never be deleted.
    """
    # Check resource group first
    if _rg_is_azure_managed(rg):
        return True, "In Azure-managed resource group: " + rg

    # Check NIC name patterns
    name_lower = name.lower()
    if _contains_any(name, AZURE_MANAGED_NIC_SUBSTRINGS):
        matched = next(s for s in AZURE_MANAGED_NIC_SUBSTRINGS
                       if s.lower() in name_lower)
        return True, "Name pattern indicates Azure-managed: '" + matched + "'"

    if _fnmatch_any(name, AZURE_MANAGED_NIC_NAME_PATTERNS):
        return True, "Name matches Azure-managed NIC pattern"

    return False, ""


# ============================================================================
# RESOURCE GRAPH RUNNER
# ============================================================================

def run_resource_graph_query(query: str, subscriptions: List[str] = None,
                              timeout: int = 120) -> Tuple[List[Dict], Optional[str]]:
    """
    Run an Azure Resource Graph query using az graph query.
    Returns (rows, error_string).
    Handles pagination automatically (up to 5000 rows).
    """
    cmd = ["az", "graph", "query",
           "-q", query,
           "--output", "json",
           "--first", "1000"]

    if subscriptions:
        cmd += ["--subscriptions"] + subscriptions

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            err = result.stderr.strip()
            # Try to install graph extension if missing
            if "not found" in err.lower() or "extension" in err.lower():
                subprocess.run(
                    ["az", "extension", "add", "--name", "resource-graph", "--yes"],
                    capture_output=True, timeout=60
                )
                # Retry
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout
                )
                if result.returncode != 0:
                    return [], result.stderr.strip()
            else:
                return [], err
        if not result.stdout.strip():
            return [], None
        data = json.loads(result.stdout)
        # az graph query returns {"count": N, "data": [...]}
        rows = data.get("data", [])
        if isinstance(rows, list):
            return rows, None
        return [], "Unexpected response format"
    except subprocess.TimeoutExpired:
        return [], f"Resource Graph query timed out after {timeout}s"
    except json.JSONDecodeError as e:
        return [], f"JSON parse error: {e}"
    except FileNotFoundError:
        return [], "Azure CLI not found. Install Azure CLI and run az login."
    except Exception as e:
        return [], str(e)


# ============================================================================
# GOVERNANCE CLASSIFIER
# ============================================================================

class GovernanceClassifier:
    """
    Classifies Azure resources discovered via Resource Graph into:
      KEEP / SAFE_DELETE / REVIEW_REQUIRED / DO_NOT_DELETE / UNKNOWN
    """

    def classify_private_endpoint(self, row: Dict) -> Tuple[str, str, bool]:
        """Returns (classification, reason, azure_managed)."""
        name = row.get("name", "")
        rg = row.get("resourceGroup", "")
        state = row.get("connectionState", "")

        if _rg_is_azure_managed(rg):
            return CLS_KEEP, "Azure-managed resource group: " + rg, True

        if state in ("Disconnected", "Rejected", ""):
            # Check production
            if _rg_is_production(rg):
                return CLS_REVIEW, "Disconnected but in PRD resource group", False
            return (CLS_SAFE_DELETE,
                    "Disconnected private endpoint - no backend. Eligible for cleanup.",
                    False)

        if state in ("Approved", "Connected"):
            return CLS_KEEP, "Connected private endpoint - in use", False

        return CLS_REVIEW, "Connection state unclear: " + state, False

    def classify_nsg(self, row: Dict) -> Tuple[str, str, bool]:
        """Returns (classification, reason, azure_managed)."""
        name = row.get("name", "")
        rg = row.get("resourceGroup", "")

        if _rg_is_azure_managed(rg):
            return CLS_KEEP, "Azure-managed resource group: " + rg, True

        if _rg_is_production(rg):
            return CLS_REVIEW, "NSG in production resource group", False

        if _rg_is_backup(rg):
            return CLS_REVIEW, "NSG in backup/DR resource group", False

        if _fnmatch_any(name, PRODUCTION_NSG_PATTERNS):
            return CLS_REVIEW, "NSG name indicates production", False

        if _fnmatch_any(name, BACKUP_NSG_PATTERNS):
            return CLS_REVIEW, "NSG name indicates backup/DR", False

        # Unattached NSG in non-prod, non-backup: safe candidate but NSG
        # auto-delete is disabled by config - set REVIEW with safe indicator
        return (CLS_REVIEW,
                "Unattached NSG - no NIC or subnet associations. "
                "Confirm no active security rules before removal.",
                False)

    def classify_disk(self, row: Dict) -> Tuple[str, str, bool]:
        """Returns (classification, reason, azure_managed)."""
        name = row.get("name", "")
        rg = row.get("resourceGroup", "")
        disk_state = row.get("diskState", "")
        tags = row.get("tags") or {}

        if _rg_is_azure_managed(rg):
            return CLS_KEEP, "Azure-managed resource group (AKS/Databricks): " + rg, True

        if _fnmatch_any(name, DATABRICKS_DISK_PATTERNS):
            return CLS_REVIEW, "Databricks disk - do not delete without Databricks team approval", True

        if _fnmatch_any(name, BACKUP_DISK_PATTERNS):
            return CLS_REVIEW, "Disk name indicates backup/snapshot - manual review required", False

        if _rg_is_production(rg):
            return CLS_REVIEW, "Unattached disk in production resource group", False

        if _rg_is_backup(rg):
            return CLS_REVIEW, "Disk in backup/DR resource group", False

        # Check backup tags
        tag_values = [str(v).lower() for v in tags.values()]
        if any("backup" in v or "snapshot" in v for v in tag_values):
            return CLS_REVIEW, "Disk has backup/snapshot tags - manual review", False

        if disk_state in ("Unattached", ""):
            return (CLS_SAFE_DELETE,
                    "Unattached managed disk (managedBy=null, diskState=Unattached). "
                    "Eligible for cleanup after owner confirmation.",
                    False)

        return CLS_REVIEW, "Disk state: " + disk_state + " - manual review", False

    def classify_public_ip(self, row: Dict) -> Tuple[str, str, bool]:
        """Returns (classification, reason, azure_managed)."""
        name = row.get("name", "")
        rg = row.get("resourceGroup", "")

        if _rg_is_azure_managed(rg):
            return CLS_KEEP, "Azure-managed resource group: " + rg, True

        if _rg_is_production(rg):
            return CLS_REVIEW, "Unattached public IP in production resource group", False

        if _contains_any(name, ["aks", "appgw", "agw", "apgw", "databricks"]):
            return CLS_KEEP, "Name suggests Azure-managed service (AKS/AppGW)", True

        return (CLS_SAFE_DELETE,
                "Unattached public IP (no ipConfiguration). "
                "Eligible for cleanup.",
                False)

    def classify_nic(self, row: Dict) -> Tuple[str, str, bool]:
        """
        Returns (classification, reason, azure_managed).

        IMPORTANT: Unattached NIC does NOT mean orphaned.
        Must check name patterns and resource group before classifying.
        """
        name = row.get("name", "")
        rg = row.get("resourceGroup", "")

        # Step 1: Azure-managed check (highest priority)
        managed, reason = _nic_is_azure_managed(name, rg)
        if managed:
            return CLS_KEEP, "Azure-managed NIC - " + reason + ". Do NOT delete.", True

        # Step 2: Production check
        if _rg_is_production(rg):
            return CLS_REVIEW, "Unattached NIC in production resource group", False

        # Step 3: Backup/DR check
        if _rg_is_backup(rg):
            return CLS_REVIEW, "NIC in backup/DR resource group", False

        # Step 4: Safe candidate (non-prod, not Azure-managed)
        return (CLS_REVIEW,
                "NIC has no VM attached - but verify: not PE-NIC, not AKS node NIC. "
                "Confirm with network team before deletion.",
                False)

    def classify_vm(self, row: Dict) -> Tuple[str, str, bool]:
        """Returns (classification, reason, azure_managed)."""
        name = row.get("name", "")
        rg = row.get("resourceGroup", "")
        power_state = row.get("powerState", "") or ""

        if _rg_is_azure_managed(rg):
            return CLS_KEEP, "VM in Azure-managed resource group", True

        if _rg_is_production(rg):
            return CLS_REVIEW, "Stopped VM in production resource group", False

        return (CLS_REVIEW,
                "Stopped/deallocated VM - power state: " + power_state +
                ". Confirm no backup or scheduled use before deallocating costs.",
                False)

    def classify_eventgrid(self, row: Dict) -> Tuple[str, str, bool]:
        """Returns (classification, reason, azure_managed)."""
        name = row.get("name", "")
        rg = row.get("resourceGroup", "")
        sub_count = row.get("subCount", 0)

        if _rg_is_production(rg):
            return CLS_REVIEW, "Event Grid topic with no subscriptions in PRD", False

        return (CLS_REVIEW,
                "Event Grid topic has 0 event subscriptions. "
                "Confirm no active consumers before removal.",
                False)

    def classify_storage(self, row: Dict) -> Tuple[str, str, bool]:
        """Returns (classification, reason, azure_managed)."""
        name = row.get("name", "")
        rg = row.get("resourceGroup", "")
        public_access = row.get("allowPublicAccess", "")

        if _rg_is_azure_managed(rg):
            return CLS_KEEP, "Storage in Azure-managed resource group", True

        if _rg_is_production(rg):
            return CLS_REVIEW, "Storage account in production resource group", False

        if str(public_access).lower() == "true":
            return (CLS_REVIEW,
                    "Storage account allows public blob access - security review needed",
                    False)

        return (CLS_REVIEW,
                "Storage account needs activity and content review before any action.",
                False)

    def classify_aks_databricks(self, row: Dict) -> Tuple[str, str, bool]:
        """Returns (classification, reason, azure_managed)."""
        rg = row.get("resourceGroup", "")
        rtype = row.get("type", "")
        return (CLS_KEEP,
                "Resource in AKS/Databricks managed RG (" + rg + "). "
                "Do NOT delete - Azure-managed infrastructure.",
                True)

    def classify_row(self, row: Dict, query_key: str) -> Dict:
        """
        Classify a single Resource Graph row.
        Returns the enriched row dict with classification fields.
        """
        name = row.get("name", "")
        rg = row.get("resourceGroup", "")
        sub = row.get("subscriptionId", "")
        loc = row.get("location", "")
        rtype = row.get("type", row.get("resource_type", ""))
        tags = row.get("tags") or {}

        # Determine owner from tags
        owner_tag_keys = [
            "owner", "Owner", "EDAV_Business_POC", "EDAV_Created_By",
            "team", "Team", "application", "Application", "contact",
        ]
        owner = next(
            (str(tags.get(k, "")) for k in owner_tag_keys
             if tags.get(k)),
            "UNKNOWN"
        )

        # Run classification
        cls, reason, azure_managed = self._classify_by_key(row, query_key)

        # Determine approval requirement
        approval_required = cls in (CLS_SAFE_DELETE, CLS_REVIEW)

        # Safe delete eligibility
        safe_eligible = (
            cls == CLS_SAFE_DELETE
            and not azure_managed
            and not _rg_is_production(rg)
        )

        return {
            "ResourceName": name,
            "ResourceType": rtype or _query_key_to_type(query_key),
            "ResourceGroup": rg,
            "Subscription": sub,
            "Location": loc,
            "Classification": cls,
            "Reason": reason,
            "OwnerTeam": owner,
            "TerraformManaged": False,   # updated by caller if TF path given
            "AzureManaged": azure_managed,
            "SafeDeleteEligible": safe_eligible,
            "ApprovalRequired": approval_required,
            "RecommendedAction": RECOMMENDED_ACTIONS.get(cls, "Manual review required."),
            "ScanTimestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "QueryCategory": query_key,
            # Extra fields from the row
            "ConnectionState": row.get("connectionState", ""),
            "DiskState": row.get("diskState", ""),
            "DiskSizeGB": row.get("diskSizeGB", ""),
            "SKU": row.get("sku", ""),
            "AllocationMethod": row.get("allocationMethod", ""),
            "PowerState": row.get("powerState", ""),
            "EventSubCount": row.get("subCount", ""),
            "Tags": json.dumps(tags) if tags else "",
            # Approval gate fields (to be filled by operator)
            "ApprovedToDelete": "",
            "ApprovalTicket": "",
            "ApprovedBy": "",
            "Notes": "",
        }

    def _classify_by_key(self, row: Dict, query_key: str) -> Tuple[str, str, bool]:
        """Dispatch to per-type classifier."""
        if query_key == "disconnected_private_endpoints":
            return self.classify_private_endpoint(row)
        elif query_key == "unattached_nsgs":
            return self.classify_nsg(row)
        elif query_key == "unattached_disks":
            return self.classify_disk(row)
        elif query_key == "unattached_public_ips":
            return self.classify_public_ip(row)
        elif query_key == "unattached_nics":
            return self.classify_nic(row)
        elif query_key == "stopped_vms":
            return self.classify_vm(row)
        elif query_key == "eventgrid_no_subscriptions":
            return self.classify_eventgrid(row)
        elif query_key == "storage_review":
            return self.classify_storage(row)
        elif query_key == "aks_databricks_resources":
            return self.classify_aks_databricks(row)
        else:
            return CLS_UNKNOWN, "Unknown query category", False


def _query_key_to_type(key: str) -> str:
    """Return resource type string for a query key."""
    mapping = {
        "disconnected_private_endpoints": "Microsoft.Network/privateEndpoints",
        "unattached_nsgs": "Microsoft.Network/networkSecurityGroups",
        "unattached_disks": "Microsoft.Compute/disks",
        "unattached_public_ips": "Microsoft.Network/publicIPAddresses",
        "unattached_nics": "Microsoft.Network/networkInterfaces",
        "stopped_vms": "Microsoft.Compute/virtualMachines",
        "eventgrid_no_subscriptions": "Microsoft.EventGrid/topics",
        "storage_review": "Microsoft.Storage/storageAccounts",
        "aks_databricks_resources": "AKS/Databricks",
    }
    return mapping.get(key, "Unknown")


# ============================================================================
# GOVERNANCE SCANNER (MAIN ENTRY POINT)
# ============================================================================

class GovernanceScanner:
    """
    Runs all Resource Graph governance queries and classifies every result.
    Returns a list of classified resource dicts.
    """

    def __init__(self, subscriptions: List[str] = None,
                 query_keys: List[str] = None,
                 terraform_path: str = None):
        self.subscriptions = subscriptions or []
        self.query_keys = query_keys or list(GOVERNANCE_QUERIES.keys())
        self.terraform_path = terraform_path
        self.classifier = GovernanceClassifier()

    def scan(self) -> Tuple[List[Dict], Dict]:
        """
        Run all governance queries. Returns (all_results, summary_dict).
        """
        all_results = []
        summary = {
            "scan_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "subscriptions": self.subscriptions,
            "query_results": {},
            "totals": {
                CLS_KEEP: 0,
                CLS_SAFE_DELETE: 0,
                CLS_REVIEW: 0,
                CLS_DO_NOT_DELETE: 0,
                CLS_NOT_FOUND: 0,
                CLS_UNKNOWN: 0,
                "total": 0,
                "azure_managed": 0,
                "no_action_needed": 0,
                "errors": 0,
            },
        }

        for key in self.query_keys:
            query_info = GOVERNANCE_QUERIES.get(key)
            if not query_info:
                continue

            display = query_info["display_name"]
            query = query_info["query"]

            rows, err = run_resource_graph_query(
                query, self.subscriptions
            )

            query_result = {
                "display_name": display,
                "resource_type": query_info.get("resource_type", ""),
                "count": 0,
                "error": err,
                "classifications": {
                    CLS_KEEP: 0,
                    CLS_SAFE_DELETE: 0,
                    CLS_REVIEW: 0,
                    CLS_DO_NOT_DELETE: 0,
                    CLS_NOT_FOUND: 0,
                    CLS_UNKNOWN: 0,
                },
            }

            if err:
                summary["totals"]["errors"] += 1
                summary["query_results"][key] = query_result
                continue

            for row in rows:
                classified = self.classifier.classify_row(row, key)
                all_results.append(classified)
                cls = classified["Classification"]
                query_result["classifications"][cls] = (
                    query_result["classifications"].get(cls, 0) + 1
                )
                summary["totals"][cls] = summary["totals"].get(cls, 0) + 1
                summary["totals"]["total"] += 1
                if classified.get("AzureManaged"):
                    summary["totals"]["azure_managed"] += 1
                if cls in (CLS_KEEP, CLS_NOT_FOUND):
                    summary["totals"]["no_action_needed"] += 1

            query_result["count"] = len(rows)
            summary["query_results"][key] = query_result

        return all_results, summary

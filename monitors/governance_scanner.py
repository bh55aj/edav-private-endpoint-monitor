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

Query execution modes (--query-mode):
  auto       Try CLI graph first, fall back to az rest, then report manual CSV needed
  cli        Use az graph query (requires resource-graph extension)
  rest       Use az rest POST to management.azure.com/providers/Microsoft.ResourceGraph
  csv        Use manually exported CSV files from Azure Portal Resource Graph

Query sources are tagged on every result row:
  CLI_GRAPH  - Result came from az graph query
  AZ_REST    - Result came from az rest fallback
  MANUAL_CSV - Result came from a manually exported CSV file

Classification logic:
KEEP           - Azure-managed resources (AKS, Databricks, PE-NICs, etc.)
REVIEW_REQUIRED - PRD, backup, Terraform-managed, unclear ownership
SAFE_DELETE    - Non-prod, unattached, approved, all safety gates pass
DO_NOT_DELETE  - Production, locked, Terraform-managed, explicit exclusion

IMPORTANT - NIC Safety Rule:
Unattached NIC != Orphaned NIC.
Many NICs are Azure-managed (AKS, Databricks, Private Endpoints,
App Gateway, Load Balancer). These must be classified KEEP, not deleted.

Version: v6.2.0 | EDAV Platform Team
============================================================================
"""

import csv
import fnmatch
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ============================================================================
# QUERY MODE CONSTANTS
# ============================================================================

QUERY_MODE_AUTO = "auto"
QUERY_MODE_CLI = "cli"
QUERY_MODE_REST = "rest"
QUERY_MODE_CSV = "csv"

QUERY_SOURCE_CLI = "CLI_GRAPH"
QUERY_SOURCE_REST = "AZ_REST"
QUERY_SOURCE_CSV = "MANUAL_CSV"
QUERY_SOURCE_NONE = "NO_DATA"

# CSV column name mapping for manual exports from Azure Portal
# Azure Portal Resource Graph CSV columns vary slightly by query
MANUAL_CSV_QUERY_MAP = {
    "disconnected_private_endpoints": "manual_private_endpoints",
    "unattached_nsgs":                "manual_nsg",
    "unattached_disks":               "manual_disks",
    "unattached_public_ips":          "manual_publicips",
    "unattached_nics":                "manual_nics",
}

# ============================================================================
# AZURE-MANAGED RESOURCE DETECTION PATTERNS
# ============================================================================

AZURE_MANAGED_NIC_NAME_PATTERNS = [
    "*-pe-nic",
    "*-nic.*",
    "*.nic.*",
    "*kube*",
    "*kube-apiserver*",
    "*aksnode*",
    "*aks-*",
    "*agw*",
    "*appgw*",
    "*privatenic*",
    "*publicnic*",
    "*lb-*-nic*",
    "*databricks*",
    "*worker-nic*",
    "*-worker-*-nic*",
]

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

AZURE_MANAGED_RG_PREFIXES = [
    "mc_",
    "databricks-rg-",
    "databricks_rg_",
    "defaultresourcegroup-",
    "networkwatcherrg",
    "azurebackuprg",
    "aro-",
    "managed-",
    "cloud-shell-storage-",
    "aml-",
    "asr-",
]

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

PRODUCTION_RG_SUBSTRINGS = [
    "prd", "prod", "production", "prd-", "-prd",
    "live", "-live",
]

BACKUP_RG_SUBSTRINGS = [
    "backup", "bkp", "dr", "-dr-",
]

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

PRODUCTION_NSG_PATTERNS = [
    "*-prd-*", "*-prod-*", "*-production-*", "*prd*nsg*",
]

BACKUP_NSG_PATTERNS = [
    "*backup*", "*bkp*", "*dr*",
]

# ============================================================================
# GOVERNANCE CLASSIFICATION CONSTANTS
# ============================================================================

CLS_KEEP = "KEEP"
CLS_SAFE_DELETE = "SAFE_DELETE"
CLS_REVIEW = "REVIEW_REQUIRED"
CLS_DO_NOT_DELETE = "DO_NOT_DELETE"
CLS_NOT_FOUND = "RESOURCE_NOT_FOUND"
CLS_UNKNOWN = "UNKNOWN"

RECOMMENDED_ACTIONS = {
    CLS_KEEP: "No action - Azure-managed resource. Do not delete.",
    CLS_SAFE_DELETE: "Approved for deletion. Collect approval ticket and run delete mode.",
    CLS_REVIEW: "Owner review required. Confirm with team before any action.",
    CLS_DO_NOT_DELETE: "Blocked from deletion. Production, locked, or Terraform-managed.",
    CLS_NOT_FOUND: "Already removed - no action needed.",
    CLS_UNKNOWN: "Unknown state. Manual investigation required.",
}

# ============================================================================
# RESOURCE GRAPH QUERIES
# ============================================================================

GOVERNANCE_QUERIES = {
    "disconnected_private_endpoints": {
        "display_name": "Disconnected Private Endpoints",
        "resource_type": "Microsoft.Network/privateEndpoints",
        "query": (
            "Resources"
            "| where type =~ 'microsoft.network/privateendpoints'"
            "| mv-expand connections = properties.privateLinkServiceConnections"
            "| extend connectionState = tostring(connections.properties.privateLinkServiceConnectionState.status)"
            "| extend privateLinkServiceId = tostring(connections.properties.privateLinkServiceId)"
            "| extend rg = resourceGroup"
            "| where isnull(connectionState) or connectionState !in~ ('Approved','Connected')"
            "| project name, resourceGroup=rg, subscriptionId, location,"
            "  connectionState, privateLinkServiceId, tags"
            "| order by resourceGroup asc, name asc"
        ),
    },
    "unattached_nsgs": {
        "display_name": "Unattached Network Security Groups",
        "resource_type": "Microsoft.Network/networkSecurityGroups",
        "query": (
            "Resources"
            "| where type =~ 'microsoft.network/networksecuritygroups'"
            "| extend nics = properties.networkInterfaces"
            "| extend subnets = properties.subnets"
            "| where isempty(nics) and isempty(subnets)"
            "| project name, resourceGroup, subscriptionId, location, tags"
            "| order by resourceGroup asc"
        ),
    },
    "unattached_disks": {
        "display_name": "Unattached Managed Disks",
        "resource_type": "Microsoft.Compute/disks",
        "query": (
            "Resources"
            "| where type =~ 'microsoft.compute/disks'"
            "| where isempty(managedBy)"
            "| project name, resourceGroup, subscriptionId, location,"
            "  diskState=tostring(properties.diskState),"
            "  sku=tostring(sku.name),"
            "  diskSizeGB=tostring(properties.diskSizeGB),"
            "  tags"
            "| order by resourceGroup asc"
        ),
    },
    "unattached_public_ips": {
        "display_name": "Unattached Public IP Addresses",
        "resource_type": "Microsoft.Network/publicIPAddresses",
        "query": (
            "Resources"
            "| where type =~ 'microsoft.network/publicipaddresses'"
            "| where isempty(properties.ipConfiguration)"
            "| project name, resourceGroup, subscriptionId, location,"
            "  sku=tostring(sku.name),"
            "  allocationMethod=tostring(properties.publicIPAllocationMethod),"
            "  tags"
            "| order by resourceGroup asc"
        ),
    },
    "unattached_nics": {
        "display_name": "Unattached Network Interfaces",
        "resource_type": "Microsoft.Network/networkInterfaces",
        "query": (
            "Resources"
            "| where type =~ 'microsoft.network/networkinterfaces'"
            "| where isempty(properties.virtualMachine)"
            "| project name, resourceGroup, subscriptionId, location,"
            "  privateIPAddress=tostring(properties.ipConfigurations[0].properties.privateIPAddress),"
            "  tags"
            "| order by resourceGroup asc"
        ),
    },
    "stopped_vms": {
        "display_name": "Stopped / Deallocated VMs",
        "resource_type": "Microsoft.Compute/virtualMachines",
        "query": (
            "Resources"
            "| where type =~ 'microsoft.compute/virtualmachines'"
            "| extend powerState = tostring(properties.extended.instanceView.powerState.displayStatus)"
            "| where powerState in~ ('VM stopped', 'VM deallocated', 'Stopped', 'Deallocated')"
            "  or isnull(powerState) or powerState == ''"
            "| project name, resourceGroup, subscriptionId, location, powerState, tags"
            "| order by resourceGroup asc"
        ),
    },
    "eventgrid_no_subscriptions": {
        "display_name": "Event Grid Topics with No Subscriptions",
        "resource_type": "Microsoft.EventGrid/topics",
        "query": (
            "Resources"
            "| where type =~ 'microsoft.eventgrid/topics'"
            "  or type =~ 'microsoft.eventgrid/systemtopics'"
            "| extend subCount = iif(isnotnull(properties.eventSubscriptionCount),"
            "    toint(properties.eventSubscriptionCount), 0)"
            "| where subCount == 0 or isnull(subCount)"
            "| project name, resourceGroup, subscriptionId, location, type, subCount, tags"
            "| order by resourceGroup asc"
        ),
    },
    "storage_review": {
        "display_name": "Storage Accounts Needing Review",
        "resource_type": "Microsoft.Storage/storageAccounts",
        "query": (
            "Resources"
            "| where type =~ 'microsoft.storage/storageaccounts'"
            "| extend allowPublicAccess = tostring(properties.allowBlobPublicAccess)"
            "| extend httpsOnly = tostring(properties.supportsHttpsTrafficOnly)"
            "| extend accessTier = tostring(properties.accessTier)"
            "| extend sku = tostring(sku.name)"
            "| project name, resourceGroup, subscriptionId, location,"
            "  sku, accessTier, allowPublicAccess, httpsOnly, tags"
            "| order by resourceGroup asc"
        ),
    },
    "aks_databricks_resources": {
        "display_name": "AKS / Databricks Managed Resources",
        "resource_type": "AKS/Databricks",
        "query": (
            "Resources"
            "| where resourceGroup startswith 'mc_'"
            "  or resourceGroup startswith 'databricks-rg-'"
            "  or resourceGroup startswith 'databricks_rg_'"
            "| project name, type, resourceGroup, subscriptionId, location, tags"
            "| order by resourceGroup asc, type asc"
        ),
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
    if _rg_is_azure_managed(rg):
        return True, "In Azure-managed resource group: " + rg
    name_lower = name.lower()
    if _contains_any(name, AZURE_MANAGED_NIC_SUBSTRINGS):
        matched = next(s for s in AZURE_MANAGED_NIC_SUBSTRINGS
                       if s.lower() in name_lower)
        return True, "Name pattern indicates Azure-managed: '" + matched + "'"
    if _fnmatch_any(name, AZURE_MANAGED_NIC_NAME_PATTERNS):
        return True, "Name matches Azure-managed NIC pattern"
    return False, ""

# ============================================================================
# QUERY EXECUTION - CLI GRAPH
# ============================================================================

def _run_cli_graph_query(query: str, subscriptions: List[str] = None,
                         timeout: int = 120) -> Tuple[List[Dict], Optional[str]]:
    """
    Run an Azure Resource Graph query using az graph query.
    Returns (rows, error_string).
    Source tag: CLI_GRAPH
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
            return [], err
        if not result.stdout.strip():
            return [], None
        data = json.loads(result.stdout)
        rows = data.get("data", [])
        if isinstance(rows, list):
            return rows, None
        return [], "Unexpected response format"
    except subprocess.TimeoutExpired:
        return [], "az graph query timed out after " + str(timeout) + "s"
    except json.JSONDecodeError as e:
        return [], "JSON parse error: " + str(e)
    except FileNotFoundError:
        return [], "Azure CLI not found. Install Azure CLI and run az login."
    except Exception as e:
        return [], str(e)

def _check_az_graph_available() -> bool:
    """
    Check if az graph extension is available.
    Returns True if az graph query works.
    NOTE: On restricted CDC VDIs, az extension add may fail due to SSL
    certificate errors. We do NOT attempt auto-install here; instead we
    fall back to az rest automatically.
    """
    try:
        result = subprocess.run(
            ["az", "graph", "query", "--help"],
            capture_output=True, text=True, timeout=15
        )
        return result.returncode == 0
    except Exception:
        return False

# ============================================================================
# QUERY EXECUTION - AZ REST FALLBACK
# ============================================================================

_REST_URL = (
    "https://management.azure.com/providers/Microsoft.ResourceGraph"
    "/resources?api-version=2021-03-01"
)

def _run_rest_graph_query(query: str, subscriptions: List[str] = None,
                          timeout: int = 120) -> Tuple[List[Dict], Optional[str]]:
    """
    Run a Resource Graph query via az rest POST.
    Does NOT require the resource-graph extension.
    Works with standard Azure CLI authentication.
    Source tag: AZ_REST
    """
    body: Dict[str, Any] = {"query": query}
    if subscriptions:
        body["subscriptions"] = subscriptions
    # az rest accepts a JSON body string
    body_str = json.dumps(body)
    cmd = [
        "az", "rest",
        "--method", "post",
        "--url", _REST_URL,
        "--body", body_str,
        "--output", "json",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            err = result.stderr.strip()
            return [], "az rest error: " + err
        if not result.stdout.strip():
            return [], None
        data = json.loads(result.stdout)
        # az rest returns ResourceGraph response: {"data": [...], "count": N}
        rows = data.get("data", [])
        if isinstance(rows, list):
            return rows, None
        return [], "Unexpected REST response format"
    except subprocess.TimeoutExpired:
        return [], "az rest query timed out after " + str(timeout) + "s"
    except json.JSONDecodeError as e:
        return [], "REST JSON parse error: " + str(e)
    except FileNotFoundError:
        return [], "Azure CLI not found. Install Azure CLI and run az login."
    except Exception as e:
        return [], "az rest exception: " + str(e)

# ============================================================================
# QUERY EXECUTION - MANUAL CSV FALLBACK
# ============================================================================

def _load_manual_csv(csv_path: str, query_key: str) -> Tuple[List[Dict], Optional[str]]:
    """
    Load resource data from a manually exported CSV file.
    The CSV should be exported from Azure Portal > Resource Graph Explorer > Download.

    Expected columns (flexible - maps common Azure Portal export column names):
      name, resourceGroup, subscriptionId, location, type
      Plus query-specific fields (connectionState, diskState, etc.)

    Source tag: MANUAL_CSV
    """
    path = Path(csv_path)
    if not path.exists():
        return [], "CSV file not found: " + csv_path
    rows = []
    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = [h.lower().strip() for h in (reader.fieldnames or [])]
            # Build a case-insensitive header map
            header_map = {}
            for orig in (reader.fieldnames or []):
                header_map[orig.lower().strip()] = orig
            for row in reader:
                normalized = {}
                for k, v in row.items():
                    normalized[k.lower().strip()] = v
                # Map to standard field names
                mapped = _map_csv_row_to_graph_row(normalized, query_key)
                rows.append(mapped)
        return rows, None
    except Exception as e:
        return [], "CSV load error (" + csv_path + "): " + str(e)

def _map_csv_row_to_graph_row(row: Dict, query_key: str) -> Dict:
    """
    Map Azure Portal CSV export column names to the standard Resource Graph
    field names used by our classifiers.
    Azure Portal exports may use: NAME, Resource Group, Subscription Id, etc.
    """
    def _get(keys: List[str]) -> str:
        for k in keys:
            v = row.get(k, row.get(k.replace(" ", ""), row.get(k.replace("-", ""), "")))
            if v:
                return str(v).strip()
        return ""

    mapped = {
        "name": _get(["name", "resource name", "resourcename"]),
        "resourceGroup": _get(["resourcegroup", "resource group", "resourcegroupname"]),
        "subscriptionId": _get(["subscriptionid", "subscription id", "subscription"]),
        "location": _get(["location", "region"]),
        "type": _get(["type", "resource type", "resourcetype"]),
        "tags": _get(["tags"]),
    }
    # Query-specific fields
    if query_key == "disconnected_private_endpoints":
        mapped["connectionState"] = _get(["connectionstate", "connection state"])
        mapped["privateLinkServiceId"] = _get(["privatelinkserviceid", "private link service id"])
    elif query_key == "unattached_disks":
        mapped["diskState"] = _get(["diskstate", "disk state"])
        mapped["sku"] = _get(["sku", "sku name"])
        mapped["diskSizeGB"] = _get(["disksizegb", "disk size gb", "disk size (gb)"])
    elif query_key == "unattached_public_ips":
        mapped["sku"] = _get(["sku", "sku name"])
        mapped["allocationMethod"] = _get(["allocationmethod", "allocation method",
                                           "publicipallocationmethod"])
    elif query_key == "stopped_vms":
        mapped["powerState"] = _get(["powerstate", "power state"])
    elif query_key == "eventgrid_no_subscriptions":
        mapped["subCount"] = _get(["subcount", "subscription count",
                                   "eventsubscriptioncount"])
    elif query_key == "storage_review":
        mapped["allowPublicAccess"] = _get(["allowpublicaccess", "allow public access"])
        mapped["httpsOnly"] = _get(["httpsonly", "https only"])
        mapped["accessTier"] = _get(["accesstier", "access tier"])
        mapped["sku"] = _get(["sku", "sku name"])
    return mapped

# ============================================================================
# UNIFIED QUERY RUNNER (auto / cli / rest / csv)
# ============================================================================

def run_resource_graph_query(
        query: str,
        subscriptions: List[str] = None,
        query_mode: str = QUERY_MODE_AUTO,
        timeout: int = 120,
        manual_csv_path: str = None,
        query_key: str = "",
) -> Tuple[List[Dict], Optional[str], str]:
    """
    Unified Resource Graph query runner with fallback support.

    Args:
        query:          KQL query string
        subscriptions:  list of subscription names/IDs to scope
        query_mode:     "auto" | "cli" | "rest" | "csv"
        timeout:        seconds before timeout
        manual_csv_path: path to manually exported CSV (for csv mode)
        query_key:      query category key (for CSV column mapping)

    Returns:
        (rows, error_string, query_source)
        query_source is one of: CLI_GRAPH, AZ_REST, MANUAL_CSV, NO_DATA
    """
    # --- CSV mode: only use manual CSV ---
    if query_mode == QUERY_MODE_CSV:
        if not manual_csv_path:
            return [], ("No CSV file provided for query_key=" + query_key +
                        ". Use --manual-" + query_key.replace("_", "-") + "-csv <path>"),
                   QUERY_SOURCE_NONE
        rows, err = _load_manual_csv(manual_csv_path, query_key)
        if err:
            return [], err, QUERY_SOURCE_NONE
        return rows, None, QUERY_SOURCE_CSV

    # --- CLI mode: only use az graph ---
    if query_mode == QUERY_MODE_CLI:
        rows, err = _run_cli_graph_query(query, subscriptions, timeout)
        if err:
            return [], err, QUERY_SOURCE_NONE
        return rows, None, QUERY_SOURCE_CLI

    # --- REST mode: only use az rest ---
    if query_mode == QUERY_MODE_REST:
        rows, err = _run_rest_graph_query(query, subscriptions, timeout)
        if err:
            return [], err, QUERY_SOURCE_NONE
        return rows, None, QUERY_SOURCE_REST

    # --- AUTO mode: try CLI, fall back to REST ---
    if query_mode == QUERY_MODE_AUTO:
        # Step 1: check if az graph is available (fast check)
        cli_available = _check_az_graph_available()
        if cli_available:
            rows, err = _run_cli_graph_query(query, subscriptions, timeout)
            if err is None:
                return rows, None, QUERY_SOURCE_CLI
            # CLI failed - check if it is a graph-specific error
            err_lower = err.lower()
            if any(x in err_lower for x in [
                "not found", "extension", "could not find",
                "no module", "the command", "unrecognized"
            ]):
                # Graph extension unavailable - go to REST
                pass
            else:
                # Real error (auth, timeout, etc.) - return it
                return [], err, QUERY_SOURCE_NONE
        # Step 2: fall back to az rest
        rows, err = _run_rest_graph_query(query, subscriptions, timeout)
        if err is None:
            return rows, None, QUERY_SOURCE_REST
        # Step 3: both failed - check for manual CSV
        if manual_csv_path:
            rows, csv_err = _load_manual_csv(manual_csv_path, query_key)
            if csv_err is None:
                return rows, None, QUERY_SOURCE_CSV
            return [], csv_err, QUERY_SOURCE_NONE
        # All paths failed
        return [], (
            "All query methods failed for " + query_key + ". "
            "az graph error: " + (err or "unknown") + ". "
            "To use manual CSV: export from Azure Portal Resource Graph Explorer "
            "and pass --manual-" + query_key.replace("_", "-") + "-csv <path>"
        ), QUERY_SOURCE_NONE

    return [], "Unknown query_mode: " + query_mode, QUERY_SOURCE_NONE

# ============================================================================
# GOVERNANCE CLASSIFIER
# ============================================================================

class GovernanceClassifier:
    """
    Classifies Azure resources discovered via Resource Graph into:
    KEEP / SAFE_DELETE / REVIEW_REQUIRED / DO_NOT_DELETE / UNKNOWN
    """

    def classify_private_endpoint(self, row: Dict) -> Tuple[str, str, bool]:
        name = row.get("name", "")
        rg = row.get("resourceGroup", "")
        state = row.get("connectionState", "")
        if _rg_is_azure_managed(rg):
            return CLS_KEEP, "Azure-managed resource group: " + rg, True
        if state in ("Disconnected", "Rejected", ""):
            if _rg_is_production(rg):
                return CLS_REVIEW, "Disconnected but in PRD resource group", False
            return (CLS_SAFE_DELETE,
                    "Disconnected private endpoint - no backend. Eligible for cleanup.",
                    False)
        if state in ("Approved", "Connected"):
            return CLS_KEEP, "Connected private endpoint - in use", False
        return CLS_REVIEW, "Connection state unclear: " + state, False

    def classify_nsg(self, row: Dict) -> Tuple[str, str, bool]:
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
        return (CLS_REVIEW,
                "Unattached NSG - no NIC or subnet associations. "
                "Confirm no active security rules before removal.",
                False)

    def classify_disk(self, row: Dict) -> Tuple[str, str, bool]:
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
        if isinstance(tags, dict):
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
        name = row.get("name", "")
        rg = row.get("resourceGroup", "")
        if _rg_is_azure_managed(rg):
            return CLS_KEEP, "Azure-managed resource group: " + rg, True
        if _rg_is_production(rg):
            return CLS_REVIEW, "Unattached public IP in production resource group", False
        if _contains_any(name, ["aks", "appgw", "agw", "apgw", "databricks"]):
            return CLS_KEEP, "Name suggests Azure-managed service (AKS/AppGW)", True
        return (CLS_SAFE_DELETE,
                "Unattached public IP (no ipConfiguration). Eligible for cleanup.",
                False)

    def classify_nic(self, row: Dict) -> Tuple[str, str, bool]:
        """
        IMPORTANT: Unattached NIC does NOT mean orphaned.
        Must check name patterns and resource group before classifying.
        """
        name = row.get("name", "")
        rg = row.get("resourceGroup", "")
        managed, reason = _nic_is_azure_managed(name, rg)
        if managed:
            return CLS_KEEP, "Azure-managed NIC - " + reason + ". Do NOT delete.", True
        if _rg_is_production(rg):
            return CLS_REVIEW, "Unattached NIC in production resource group", False
        if _rg_is_backup(rg):
            return CLS_REVIEW, "NIC in backup/DR resource group", False
        return (CLS_REVIEW,
                "NIC has no VM attached - but verify: not PE-NIC, not AKS node NIC. "
                "Confirm with network team before deletion.",
                False)

    def classify_vm(self, row: Dict) -> Tuple[str, str, bool]:
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
        rg = row.get("resourceGroup", "")
        if _rg_is_production(rg):
            return CLS_REVIEW, "Event Grid topic with no subscriptions in PRD", False
        return (CLS_REVIEW,
                "Event Grid topic has 0 event subscriptions. "
                "Confirm no active consumers before removal.",
                False)

    def classify_storage(self, row: Dict) -> Tuple[str, str, bool]:
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
        rg = row.get("resourceGroup", "")
        return (CLS_KEEP,
                "Resource in AKS/Databricks managed RG (" + rg + "). "
                "Do NOT delete - Azure-managed infrastructure.",
                True)

    def classify_row(self, row: Dict, query_key: str) -> Dict:
        """Classify a single Resource Graph row. Returns enriched row dict."""
        name = row.get("name", "")
        rg = row.get("resourceGroup", "")
        sub = row.get("subscriptionId", "")
        loc = row.get("location", "")
        rtype = row.get("type", row.get("resource_type", ""))
        tags = row.get("tags") or {}

        owner_tag_keys = [
            "owner", "Owner", "EDAV_Business_POC", "EDAV_Created_By",
            "team", "Team", "application", "Application", "contact",
        ]
        if isinstance(tags, dict):
            owner = next(
                (str(tags.get(k, "")) for k in owner_tag_keys if tags.get(k)),
                "UNKNOWN"
            )
        else:
            owner = "UNKNOWN"

        cls, reason, azure_managed = self._classify_by_key(row, query_key)
        approval_required = cls in (CLS_SAFE_DELETE, CLS_REVIEW)
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
            "TerraformManaged": False,
            "AzureManaged": azure_managed,
            "SafeDeleteEligible": safe_eligible,
            "ApprovalRequired": approval_required,
            "RecommendedAction": RECOMMENDED_ACTIONS.get(cls, "Manual review required."),
            "ScanTimestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "QueryCategory": query_key,
            "QuerySource": "",   # filled by GovernanceScanner.scan()
            "ConnectionState": row.get("connectionState", ""),
            "DiskState": row.get("diskState", ""),
            "DiskSizeGB": row.get("diskSizeGB", ""),
            "SKU": row.get("sku", ""),
            "AllocationMethod": row.get("allocationMethod", ""),
            "PowerState": row.get("powerState", ""),
            "EventSubCount": row.get("subCount", ""),
            "Tags": json.dumps(tags) if isinstance(tags, dict) else str(tags),
            "ApprovedToDelete": "",
            "ApprovalTicket": "",
            "ApprovedBy": "",
            "Notes": "",
        }

    def _classify_by_key(self, row: Dict, query_key: str) -> Tuple[str, str, bool]:
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

    query_mode controls the execution path:
      auto  - try az graph, fall back to az rest, then report CSV needed
      cli   - az graph query only (requires resource-graph extension)
      rest  - az rest POST only (no extension required)
      csv   - manually exported CSV files only

    manual_csv_paths maps query_key -> file path for csv/auto mode:
      {
        "disconnected_private_endpoints": "/path/to/pe.csv",
        "unattached_nics": "/path/to/nics.csv",
        ...
      }
    """

    def __init__(self, subscriptions: List[str] = None,
                 query_keys: List[str] = None,
                 terraform_path: str = None,
                 query_mode: str = QUERY_MODE_AUTO,
                 manual_csv_paths: Dict[str, str] = None):
        self.subscriptions = subscriptions or []
        self.query_keys = query_keys or list(GOVERNANCE_QUERIES.keys())
        self.terraform_path = terraform_path
        self.query_mode = query_mode
        self.manual_csv_paths = manual_csv_paths or {}
        self.classifier = GovernanceClassifier()

    def scan(self) -> Tuple[List[Dict], Dict]:
        """
        Run all governance queries. Returns (all_results, summary_dict).
        Each result row includes a QuerySource field:
          CLI_GRAPH  - came from az graph query
          AZ_REST    - came from az rest fallback
          MANUAL_CSV - came from manually exported CSV
          NO_DATA    - query failed / no data available
        """
        all_results = []
        summary = {
            "scan_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "subscriptions": self.subscriptions,
            "query_mode": self.query_mode,
            "query_results": {},
            "query_sources": {},
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
            manual_csv = self.manual_csv_paths.get(key)

            rows, err, source = run_resource_graph_query(
                query=query,
                subscriptions=self.subscriptions,
                query_mode=self.query_mode,
                manual_csv_path=manual_csv,
                query_key=key,
            )

            summary["query_sources"][key] = source

            query_result = {
                "display_name": display,
                "resource_type": query_info.get("resource_type", ""),
                "count": 0,
                "error": err,
                "query_source": source,
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
                classified["QuerySource"] = source
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


# ============================================================================
# MANUAL CSV INSTRUCTIONS HELPER
# ============================================================================

def get_manual_csv_instructions(query_key: str = None) -> str:
    """
    Return instructions for manually exporting Resource Graph results to CSV.
    Used when both az graph and az rest fail (e.g., SSL issues on CDC VDI).
    """
    if query_key:
        qinfo = GOVERNANCE_QUERIES.get(query_key, {})
        qname = qinfo.get("display_name", query_key)
        query_text = qinfo.get("query", "")
        return (
            "Manual CSV Export Instructions for: " + qname + "\n"
            "========================================\n"
            "1. Open Azure Portal: portal.azure.com\n"
            "2. Search for: Resource Graph Explorer\n"
            "3. Paste this KQL query:\n\n"
            + query_text + "\n\n"
            "4. Click: Run query\n"
            "5. Click: Download as CSV\n"
            "6. Save the file\n"
            "7. Pass to scanner: --manual-" + query_key.replace("_", "-") + "-csv <path>\n"
        )
    # All queries
    lines_out = [
        "Manual CSV Export - All Queries",
        "================================",
        "If az graph and az rest both fail, export results manually from Azure Portal.",
        "",
        "Steps:",
        "1. Open: portal.azure.com > Resource Graph Explorer",
        "2. For each query below, run it and download CSV",
        "3. Pass the CSV paths to the scanner:",
        "",
        "python main.py --scan-governance --query-mode csv \\",
        "  --manual-private-endpoints-csv pe_results.csv \\",
        "  --manual-nsg-csv nsg_results.csv \\",
        "  --manual-disks-csv disk_results.csv \\",
        "  --manual-publicips-csv pip_results.csv \\",
        "  --manual-nics-csv nic_results.csv",
        "",
    ]
    for key, qinfo in GOVERNANCE_QUERIES.items():
        lines_out.append("Query: " + qinfo.get("display_name", key))
        lines_out.append("Flag:  --manual-" + key.replace("_", "-") + "-csv")
        lines_out.append("")
    return "\n".join(lines_out)

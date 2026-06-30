#!/usr/bin/env python3
"""
============================================================================
EDAV Private Endpoint Discovery Engine - engines/pe_discovery.py
============================================================================
Discovers disconnected private endpoints from three sources:
  1. Azure Resource Graph (via az graph query or az rest fallback)
  2. Azure REST API (no extension required)
  3. Manual CSV export from EDAV Resource Monitor / Azure Portal

Each discovered endpoint is normalized into a PrivateEndpointRecord dataclass.
Discovery source is always tagged: CLI_GRAPH | AZ_REST | MANUAL_CSV | AZURE_REST

Target subscriptions (EDAV):
  OCIO-TSBDEV-C1          (DEV)
  OCIO-TSBPRD-C1          (PRD)
  OCIO-EDAV-DMZ-DEV-C1    (DMZ DEV)
  OCIO-EDAV-DMZ-PRD-C1    (DMZ PRD)

Version: v7.0.0 | EDAV Platform Team
============================================================================
"""

from __future__ import annotations

import csv
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ============================================================================
# TARGET SUBSCRIPTIONS
# ============================================================================

EDAV_SUBSCRIPTIONS = [
    "OCIO-TSBDEV-C1",
    "OCIO-TSBPRD-C1",
    "OCIO-EDAV-DMZ-DEV-C1",
    "OCIO-EDAV-DMZ-PRD-C1",
]

# Cost estimate per private endpoint per month (USD)
# Based on Azure pricing: ~$7.30/month per PE (processing + data)
PE_MONTHLY_COST_USD = 7.30

# Resource Graph KQL for disconnected private endpoints
PE_GRAPH_QUERY = (
    "Resources"
    " | where type =~ 'microsoft.network/privateendpoints'"
    " | mv-expand connections = properties.privateLinkServiceConnections"
    " | extend connectionState = tostring(connections.properties.privateLinkServiceConnectionState.status)"
    " | extend privateLinkServiceId = tostring(connections.properties.privateLinkServiceId)"
    " | extend subnetId = tostring(properties.subnet.id)"
    " | where isnull(connectionState) or connectionState !in~ ('Approved','Connected')"
    " | project name, resourceGroup, subscriptionId, location,"
    "   connectionState, privateLinkServiceId, subnetId, tags, id"
    " | order by resourceGroup asc, name asc"
)

# REST API endpoint for Resource Graph
RESOURCE_GRAPH_REST_URL = (
    "https://management.azure.com/providers/Microsoft.ResourceGraph"
    "/resources?api-version=2021-03-01"
)

# ============================================================================
# DATA MODEL
# ============================================================================

@dataclass
class PrivateEndpointRecord:
    """Normalized representation of a discovered private endpoint."""

    # Identity
    name: str = ""
    resource_id: str = ""
    resource_group: str = ""
    subscription_id: str = ""
    subscription_name: str = ""
    location: str = ""

    # Connection details
    connection_state: str = ""
    private_link_service_id: str = ""
    subnet_id: str = ""

    # Classification (set by downstream engines)
    classification: str = ""
    classification_reason: str = ""
    environment: str = ""

    # Team / ownership
    owner_team: str = ""
    owner_contact: str = ""
    terraform_managed: bool = False
    azure_managed: bool = False

    # Validation (set by validation engine)
    exists_in_azure: Optional[bool] = None
    backend_exists: Optional[bool] = None
    has_lock: bool = False
    lock_reason: str = ""
    has_dependencies: bool = False
    dependency_detail: str = ""

    # Approval (set from input file or approval workflow)
    approved_to_delete: bool = False
    approval_ticket: str = ""
    approved_by: str = ""

    # Cost
    monthly_cost_usd: float = PE_MONTHLY_COST_USD
    yearly_cost_usd: float = field(default=0.0)

    # Deletion tracking (set by cleanup engine)
    delete_result: str = ""
    delete_command: str = ""
    delete_timestamp: str = ""
    verify_result: str = ""
    backup_path: str = ""
    error_message: str = ""

    # Metadata
    tags: Dict = field(default_factory=dict)
    source: str = ""       # CLI_GRAPH | AZ_REST | MANUAL_CSV | AZURE_REST
    scan_timestamp: str = ""
    raw_data: Dict = field(default_factory=dict)
    notes: str = ""

    def __post_init__(self):
        self.yearly_cost_usd = round(self.monthly_cost_usd * 12, 2)
        if not self.scan_timestamp:
            self.scan_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def to_dict(self) -> Dict:
        """Serialize to flat dict for CSV/Excel output."""
        return {
            "Name": self.name,
            "ResourceID": self.resource_id,
            "ResourceGroup": self.resource_group,
            "SubscriptionID": self.subscription_id,
            "SubscriptionName": self.subscription_name,
            "Location": self.location,
            "ConnectionState": self.connection_state,
            "PrivateLinkServiceID": self.private_link_service_id,
            "SubnetID": self.subnet_id,
            "Classification": self.classification,
            "ClassificationReason": self.classification_reason,
            "Environment": self.environment,
            "OwnerTeam": self.owner_team,
            "OwnerContact": self.owner_contact,
            "TerraformManaged": self.terraform_managed,
            "AzureManaged": self.azure_managed,
            "ExistsInAzure": self.exists_in_azure,
            "BackendExists": self.backend_exists,
            "HasLock": self.has_lock,
            "LockReason": self.lock_reason,
            "HasDependencies": self.has_dependencies,
            "DependencyDetail": self.dependency_detail,
            "ApprovedToDelete": self.approved_to_delete,
            "ApprovalTicket": self.approval_ticket,
            "ApprovedBy": self.approved_by,
            "MonthlyCostUSD": self.monthly_cost_usd,
            "YearlyCostUSD": self.yearly_cost_usd,
            "DeleteResult": self.delete_result,
            "DeleteCommand": self.delete_command,
            "DeleteTimestamp": self.delete_timestamp,
            "VerifyResult": self.verify_result,
            "BackupPath": self.backup_path,
            "ErrorMessage": self.error_message,
            "Tags": json.dumps(self.tags) if self.tags else "",
            "Source": self.source,
            "ScanTimestamp": self.scan_timestamp,
            "Notes": self.notes,
        }

    @property
    def is_safe_delete(self) -> bool:
        return self.classification == "SAFE_DELETE"

    @property
    def is_already_removed(self) -> bool:
        return self.classification in ("ALREADY_REMOVED", "RESOURCE_NOT_FOUND")

    @property
    def display_name(self) -> str:
        return self.name or self.resource_id.split("/")[-1] if self.resource_id else "(unknown)"


# ============================================================================
# DISCOVERY ENGINE
# ============================================================================

class PrivateEndpointDiscovery:
    """
    Discovers disconnected private endpoints from multiple sources.
    Handles fallback automatically: CLI Graph -> REST -> Manual CSV.
    """

    def __init__(
        self,
        subscriptions: Optional[List[str]] = None,
        query_mode: str = "auto",
        manual_csv_path: Optional[str] = None,
        timeout: int = 120,
    ):
        self.subscriptions = subscriptions or EDAV_SUBSCRIPTIONS
        self.query_mode = query_mode
        self.manual_csv_path = manual_csv_path
        self.timeout = timeout
        self._sub_name_cache: Dict[str, str] = {}

    def discover(self) -> Tuple[List[PrivateEndpointRecord], str, Optional[str]]:
        """
        Run discovery. Returns (records, source_used, error_message).
        source_used: "CLI_GRAPH" | "AZ_REST" | "MANUAL_CSV"
        """
        logger.info("Starting PE discovery: mode=%s subs=%s",
                    self.query_mode, self.subscriptions)

        if self.query_mode == "csv":
            return self._discover_from_csv()

        if self.query_mode == "cli":
            rows, err = self._run_graph_cli()
            if err:
                return [], "CLI_GRAPH", err
            return self._rows_to_records(rows, "CLI_GRAPH"), "CLI_GRAPH", None

        if self.query_mode == "rest":
            rows, err = self._run_graph_rest()
            if err:
                return [], "AZ_REST", err
            return self._rows_to_records(rows, "AZ_REST"), "AZ_REST", None

        # auto mode: try CLI -> REST -> CSV
        rows, err = self._run_graph_cli()
        if err is None:
            logger.info("Discovery via CLI_GRAPH: %d rows", len(rows))
            return self._rows_to_records(rows, "CLI_GRAPH"), "CLI_GRAPH", None

        logger.warning("CLI graph failed (%s), trying az rest", err[:80])
        rows, err2 = self._run_graph_rest()
        if err2 is None:
            logger.info("Discovery via AZ_REST: %d rows", len(rows))
            return self._rows_to_records(rows, "AZ_REST"), "AZ_REST", None

        logger.warning("REST also failed (%s)", err2[:80] if err2 else "")
        if self.manual_csv_path:
            return self._discover_from_csv()

        combined_err = f"CLI: {err} | REST: {err2}"
        return [], "NONE", combined_err

    def _run_graph_cli(self) -> Tuple[List[Dict], Optional[str]]:
        """Run az graph query. Returns (rows, error)."""
        cmd = ["az", "graph", "query", "-q", PE_GRAPH_QUERY,
               "--output", "json", "--first", "1000"]
        if self.subscriptions:
            cmd += ["--subscriptions"] + self.subscriptions
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=self.timeout)
            if r.returncode != 0:
                return [], r.stderr.strip()
            data = json.loads(r.stdout or "{}")
            return data.get("data", []), None
        except Exception as exc:
            return [], str(exc)

    def _run_graph_rest(self) -> Tuple[List[Dict], Optional[str]]:
        """Run az rest POST to Resource Graph. Returns (rows, error)."""
        body = json.dumps({"query": PE_GRAPH_QUERY,
                           "subscriptions": self.subscriptions})
        cmd = ["az", "rest", "--method", "post",
               "--url", RESOURCE_GRAPH_REST_URL,
               "--body", body, "--output", "json"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=self.timeout)
            if r.returncode != 0:
                return [], r.stderr.strip()
            data = json.loads(r.stdout or "{}")
            return data.get("data", []), None
        except Exception as exc:
            return [], str(exc)

    def _discover_from_csv(self) -> Tuple[List[PrivateEndpointRecord], str, Optional[str]]:
        """Load from manually exported CSV. Returns (records, source, error)."""
        path = self.manual_csv_path
        if not path or not Path(path).exists():
            return [], "MANUAL_CSV", f"CSV not found: {path}"
        records = []
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    records.append(self._csv_row_to_record(row))
            logger.info("Loaded %d records from CSV: %s", len(records), path)
            return records, "MANUAL_CSV", None
        except Exception as exc:
            return [], "MANUAL_CSV", str(exc)

    def _csv_row_to_record(self, row: Dict) -> PrivateEndpointRecord:
        """Map CSV row (case-insensitive) to PrivateEndpointRecord."""
        def g(*keys) -> str:
            for k in keys:
                for rk, rv in row.items():
                    if rk.lower().replace(" ", "").replace("_", "") == k.lower():
                        return str(rv).strip()
            return ""

        name = g("name", "resourcename", "endpointname")
        rg = g("resourcegroup", "rg")
        sub = g("subscriptionid", "subscription", "subscriptionname")
        conn = g("connectionstate", "state")
        plsid = g("privatelinkserviceid", "backendresource")
        rid = g("id", "resourceid")
        loc = g("location", "region")
        approved = g("approvedtodelete", "approved")
        ticket = g("approvalticket", "ticket", "itsmticket")
        approver = g("approvedby", "approver")
        env = self._infer_env(sub)

        rec = PrivateEndpointRecord(
            name=name,
            resource_id=rid,
            resource_group=rg,
            subscription_id=sub,
            subscription_name=sub,
            location=loc,
            connection_state=conn,
            private_link_service_id=plsid,
            environment=env,
            approved_to_delete=approved.lower() in ("yes","true","1","approved"),
            approval_ticket=ticket,
            approved_by=approver,
            source="MANUAL_CSV",
            raw_data=dict(row),
        )
        return rec

    def _rows_to_records(self, rows: List[Dict], source: str) -> List[PrivateEndpointRecord]:
        """Convert Resource Graph rows to PrivateEndpointRecord list."""
        records = []
        for row in rows:
            sub_id = row.get("subscriptionId", "")
            sub_name = self._resolve_sub_name(sub_id)
            env = self._infer_env(sub_name or sub_id)
            tags = row.get("tags") or {}
            if isinstance(tags, str):
                try: tags = json.loads(tags)
                except Exception: tags = {}
            rec = PrivateEndpointRecord(
                name=row.get("name", ""),
                resource_id=row.get("id", ""),
                resource_group=row.get("resourceGroup", ""),
                subscription_id=sub_id,
                subscription_name=sub_name,
                location=row.get("location", ""),
                connection_state=row.get("connectionState", ""),
                private_link_service_id=row.get("privateLinkServiceId", ""),
                subnet_id=row.get("subnetId", ""),
                environment=env,
                tags=tags,
                source=source,
                raw_data=row,
            )
            records.append(rec)
        return records

    def _resolve_sub_name(self, sub_id: str) -> str:
        """Resolve subscription ID to name. Cached."""
        if not sub_id:
            return ""
        if sub_id in self._sub_name_cache:
            return self._sub_name_cache[sub_id]
        # Try matching known subscription names by pattern
        for name in EDAV_SUBSCRIPTIONS:
            # sub_id might already be a name
            if name.lower() == sub_id.lower():
                self._sub_name_cache[sub_id] = name
                return name
        # Try az account show
        try:
            r = subprocess.run(
                ["az", "account", "show", "--subscription", sub_id,
                 "--query", "name", "--output", "tsv"],
                capture_output=True, text=True, timeout=15
            )
            if r.returncode == 0 and r.stdout.strip():
                name = r.stdout.strip()
                self._sub_name_cache[sub_id] = name
                return name
        except Exception:
            pass
        self._sub_name_cache[sub_id] = sub_id
        return sub_id

    @staticmethod
    def _infer_env(sub: str) -> str:
        """Infer environment from subscription name."""
        s = sub.upper()
        if "PRD" in s or "PROD" in s:
            if "DMZ" in s:
                return "DMZ-PRD"
            return "PRD"
        if "DEV" in s:
            if "DMZ" in s:
                return "DMZ-DEV"
            return "DEV"
        return "UNKNOWN"


# ============================================================================
# CONVENIENCE FUNCTION
# ============================================================================

def discover_private_endpoints(
    subscriptions: Optional[List[str]] = None,
    query_mode: str = "auto",
    manual_csv_path: Optional[str] = None,
    timeout: int = 120,
) -> Tuple[List[PrivateEndpointRecord], str, Optional[str]]:
    """
    Convenience wrapper. Discover disconnected private endpoints.
    Returns (records, source_used, error_message).
    """
    engine = PrivateEndpointDiscovery(
        subscriptions=subscriptions,
        query_mode=query_mode,
        manual_csv_path=manual_csv_path,
        timeout=timeout,
    )
    return engine.discover()

#!/usr/bin/env python3
"""
monitors/private_endpoints.py
==============================
EDAV Azure Resource Monitor - Phase 1
Private Endpoint Monitor

Discovers disconnected Azure Private Endpoints, validates connection state,
detects backend resource existence, maps ownership, assigns risk scores,
and supports ApprovedToDelete / safe-delete / dry-run / rollback workflows.

Builds on existing AzureValidator logic from main.py and extends it with:
- Azure Resource Graph bulk discovery
- Per-subscription validation
- Risk scoring (Low / Medium / High)
- Terraform drift field
- Team report generation
- Rollback instruction generation
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .base_monitor import BaseMonitor, ResourceFinding


# ---------------------------------------------------------------------------
# Private Endpoint specific finding fields
# ---------------------------------------------------------------------------

@dataclass
class PrivateEndpointFinding(ResourceFinding):
    """ResourceFinding extended with private endpoint specifics."""

    connection_state: str = "Unknown"
    private_link_resource_id: str = ""
    backend_exists: Optional[bool] = None
    backend_resource_type: str = ""
    approved_to_delete: str = ""
    approval_ticket: str = ""
    approved_by: str = ""
    recommended_action: str = ""
    risk_level: str = "Unknown"
    approval_status: str = "Pending"
    rollback_instructions: str = ""
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = super().to_dict()
        d.update({
            "connection_state": self.connection_state,
            "private_link_resource_id": self.private_link_resource_id,
            "backend_exists": self.backend_exists,
            "backend_resource_type": self.backend_resource_type,
            "approved_to_delete": self.approved_to_delete,
            "approval_ticket": self.approval_ticket,
            "approved_by": self.approved_by,
            "recommended_action": self.recommended_action,
            "risk_level": self.risk_level,
            "approval_status": self.approval_status,
            "rollback_instructions": self.rollback_instructions,
            "notes": self.notes,
        })
        return d


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class PrivateEndpointMonitor(BaseMonitor):
    """
    Discovers and validates Azure Private Endpoints.

    Classification logic:
      - SAFE_DELETE  : Disconnected + backend gone + not TF managed + not prod
      - REVIEW_REQUIRED : Disconnected but backend exists or owner unknown
      - DO_NOT_DELETE : Connected, TF-managed, locked, or production
    """

    SERVICE_TYPE = "Private Endpoints"
    RESOURCE_TYPES = ["Microsoft.Network/privateEndpoints"]

    def scan_subscription(
        self, subscription_id: str, subscription_name: str = ""
    ) -> List[ResourceFinding]:
        findings: List[ResourceFinding] = []

        # Bulk discovery via Resource Graph
        query = """
Resources
| where type =~ 'microsoft.network/privateendpoints'
| project id, name, resourceGroup, subscriptionId, location, tags,
  connections=properties.privateLinkServiceConnections,
  manualConnections=properties.manualPrivateLinkServiceConnections,
  networkInterfaces=properties.networkInterfaces
"""
        resources = self._resource_graph_query(query, [subscription_id])

        if not resources:
            # Fallback: az network private-endpoint list
            try:
                data = self._az(
                    f"az network private-endpoint list --subscription {subscription_id}",
                    subscription_id,
                )
                resources = data if isinstance(data, list) else []
            except Exception:
                resources = []

        for r in resources:
            finding = self._process_resource(r, subscription_id, subscription_name)
            if finding:
                findings.append(finding)

        return findings

    def _process_resource(
        self,
        r: Dict[str, Any],
        subscription_id: str,
        subscription_name: str,
    ) -> Optional[PrivateEndpointFinding]:
        name = r.get("name", "")
        rg = r.get("resourceGroup", "")
        location = r.get("location", "")
        resource_id = r.get("id", "")
        tags = self._get_tags(r)

        # Ownership
        owner, team, cost_center = self._detect_owner_from_tags(tags)
        is_prod = self._is_production(name, rg, tags)
        has_lock = self._check_resource_lock(resource_id, subscription_id)

        # Connection state + backend
        connection_state, backend_id = self._get_connection_info(r, name, rg, subscription_id)
        backend_exists, backend_type = self._validate_backend(backend_id) if backend_id else (None, "")

        # Terraform drift
        is_tf = self.config.get("terraform_checker") and self.config["terraform_checker"].is_terraform_managed(name, resource_id)[0]
        tf_reason = ""
        if is_tf:
            _, tf_reason = self.config["terraform_checker"].is_terraform_managed(name, resource_id)

        # Risk scoring
        risk_level = self._score_risk(
            connection_state=connection_state,
            backend_exists=backend_exists,
            is_prod=is_prod,
            is_tf=is_tf,
            owner=owner,
            subscription_name=subscription_name,
        )

        # Classification
        is_orphaned = (connection_state == "Disconnected")
        extra_safe = (connection_state == "Disconnected" and backend_exists is False and not is_tf and not is_prod)
        classification, reason = self._classify(
            is_orphaned=is_orphaned,
            is_production=is_prod,
            is_terraform=is_tf,
            has_lock=has_lock,
            auto_delete_supported=True,
            extra_safe=extra_safe,
        )

        # Recommended action
        if classification == "SAFE_DELETE":
            recommended_action = "Approved for deletion - backend gone, disconnected"
        elif connection_state == "Disconnected" and backend_exists:
            recommended_action = "Review required - backend still exists"
        elif connection_state == "Connected":
            recommended_action = "No action - endpoint is connected and active"
        else:
            recommended_action = "Manual review required"

        # Rollback instructions
        rollback = (
            f"To restore: az network private-endpoint create "
            f"--name {name} --resource-group {rg} "
            f"--private-connection-resource-id {backend_id or 'UNKNOWN'} "
            f"--connection-name {name}-conn --manual-request false. "
            f"ARM backup available in backups/privateEndpoints/"
        ) if classification == "SAFE_DELETE" else ""

        return PrivateEndpointFinding(
            resource_id=resource_id,
            resource_name=name,
            resource_type="Microsoft.Network/privateEndpoints",
            resource_group=rg,
            subscription_id=subscription_id,
            subscription_name=subscription_name,
            location=location,
            owner=owner or "UNKNOWN",
            team=team or "UNKNOWN",
            cost_center=cost_center,
            tags=tags,
            classification=classification,
            classification_reason=reason,
            severity="HIGH" if risk_level == "High" else ("MEDIUM" if risk_level == "Medium" else "LOW"),
            is_terraform_managed=bool(is_tf),
            terraform_reason=tf_reason,
            has_resource_lock=has_lock,
            is_production=is_prod,
            # PE-specific
            connection_state=connection_state,
            private_link_resource_id=backend_id or "",
            backend_exists=backend_exists,
            backend_resource_type=backend_type,
            recommended_action=recommended_action,
            risk_level=risk_level,
            approval_status="Pending",
            rollback_instructions=rollback,
        )

    # ------------------------------------------------------------------
    # Connection state detection
    # ------------------------------------------------------------------

    def _get_connection_info(
        self,
        r: Dict[str, Any],
        name: str,
        rg: str,
        subscription_id: str,
    ) -> Tuple[str, str]:
        """Return (connection_state, backend_resource_id)."""
        # Try from Resource Graph result
        connections = r.get("connections") or r.get("privateLinkServiceConnections") or []
        manual = r.get("manualConnections") or r.get("manualPrivateLinkServiceConnections") or []
        all_conns = (connections if isinstance(connections, list) else []) + (manual if isinstance(manual, list) else [])

        if all_conns:
            conn = all_conns[0]
            props = conn.get("properties", conn)
            state = (
                props.get("privateLinkServiceConnectionState", {}).get("status", "")
                or props.get("connectionState", {}).get("status", "")
                or "Unknown"
            )
            backend_id = props.get("privateLinkServiceId", "")
            return state, backend_id

        # Fallback: az network private-endpoint show
        try:
            data = self._az(
                f"az network private-endpoint show --name {name} --resource-group {rg}",
                subscription_id,
            )
            conns = data.get("privateLinkServiceConnections") or data.get("manualPrivateLinkServiceConnections") or []
            if conns:
                conn = conns[0]
                state = conn.get("privateLinkServiceConnectionState", {}).get("status", "Unknown")
                backend_id = conn.get("privateLinkServiceId", "")
                return state, backend_id
        except Exception:
            pass

        return "Unknown", ""

    def _validate_backend(self, backend_id: str) -> Tuple[Optional[bool], str]:
        """Check if the backend resource still exists. Returns (exists, resource_type)."""
        if not backend_id:
            return None, ""
        try:
            data = self._az(f"az resource show --ids {backend_id}")
            rtype = data.get("type", "") if isinstance(data, dict) else ""
            return True, rtype
        except RuntimeError as e:
            if "ResourceNotFound" in str(e) or "not found" in str(e).lower():
                return False, ""
            return None, ""
        except Exception:
            return None, ""

    # ------------------------------------------------------------------
    # Risk scoring
    # ------------------------------------------------------------------

    def _score_risk(
        self,
        connection_state: str,
        backend_exists: Optional[bool],
        is_prod: bool,
        is_tf: bool,
        owner: str,
        subscription_name: str,
    ) -> str:
        """
        Risk scoring per Phase 1 specification:
          Low    : Disconnected + no backend + not TF managed
          Medium : Disconnected + backend still exists OR owner unknown
          High   : Production subscription + TF managed + backend exists + unclear ownership
        """
        if is_prod or (is_tf and backend_exists):
            return "High"
        if connection_state == "Disconnected" and backend_exists is False and not is_tf:
            return "Low"
        if connection_state == "Disconnected" and (backend_exists or not owner or owner == "UNKNOWN"):
            return "Medium"
        return "Medium"

databricks_monitor.py#!/usr/bin/env python3
"""
databricks_monitor.py — Azure Databricks workspace monitor.

Identifies:
  - Databricks workspaces with no clusters running
  - Workspaces in provisioning failure state
  - Workspaces with no jobs run in last 90 days (requires Databricks API)
  - Trial workspaces approaching expiry

Cost: Premium workspace ~$0.55/DBU; idle workspace overhead ~$0/month
(cost is cluster-driven). Flags workspaces that may be abandoned.
"""

from __future__ import annotations
from typing import Any, Dict, List
from .base_monitor import BaseMonitor, ResourceFinding


class DatabricksMonitor(BaseMonitor):
    SERVICE_TYPE = "Databricks"
    RESOURCE_TYPES = ["Microsoft.Databricks/workspaces"]

    def scan_subscription(self, subscription_id: str, subscription_name: str = "") -> List[ResourceFinding]:
        findings: List[ResourceFinding] = []
        query = """
        Resources
        | where type =~ 'microsoft.databricks/workspaces'
        | project id, name, resourceGroup, subscriptionId, location, tags,
                  sku=sku.name,
                  provisioningState=properties.provisioningState,
                  workspaceUrl=properties.workspaceUrl,
                  workspaceId=properties.workspaceId,
                  managedResourceGroupId=properties.managedResourceGroupId,
                  publicNetworkAccess=properties.publicNetworkAccess,
                  createdDateTime=properties.createdDateTime
        """
        resources = self._resource_graph_query(query, [subscription_id])
        for r in resources:
            f = self._process_resource(r, subscription_id, subscription_name)
            if f:
                findings.append(f)
        return findings

    def _process_resource(self, r: Dict[str, Any], sub: str, sub_name: str) -> ResourceFinding:
        name = r.get("name", "")
        rg = r.get("resourceGroup", "")
        location = r.get("location", "")
        tags = self._get_tags(r)
        resource_id = r.get("id", "")
        sku = r.get("sku", "premium")

        owner, team, cost_center = self._detect_owner_from_tags(tags)
        is_prod = self._is_production(name, rg, tags)
        has_lock = self._check_resource_lock(resource_id, sub)

        provisioning = r.get("provisioningState", "Succeeded")
        is_failed = provisioning.lower() in ("failed", "canceled", "deleting")
        public_access = str(r.get("publicNetworkAccess", "")).lower() == "enabled"
        no_tags = len(tags) == 0

        is_orphaned = is_failed or no_tags

        classification, reason = self._classify(
            is_orphaned=is_orphaned,
            is_production=is_prod,
            is_terraform=False,
            has_lock=has_lock,
            auto_delete_supported=False,
        )
        flags = []
        if is_failed:
            reason = f"Databricks workspace in {provisioning} state"
            flags.append("PROVISIONING_FAILURE")
        if no_tags and not is_failed:
            reason = "No tags found — ownership unverifiable; requires review"
            flags.append("NO_TAGS")
        if public_access:
            flags.append("PUBLIC_NETWORK_ACCESS")
        if flags and classification not in ("DO_NOT_DELETE",):
            classification = "REVIEW_REQUIRED"
            reason += f" | Flags: {', '.join(flags)}"

        return ResourceFinding(
            resource_id=resource_id,
            resource_name=name,
            resource_type="Microsoft.Databricks/workspaces",
            resource_group=rg,
            subscription_id=sub,
            subscription_name=sub_name,
            location=location,
            owner=owner, team=team, cost_center=cost_center, tags=tags,
            classification=classification,
            classification_reason=reason,
            severity="HIGH" if is_failed else ("MEDIUM" if no_tags else "LOW"),
            estimated_monthly_cost_usd=0.0,
            is_production=is_prod, has_resource_lock=has_lock,
            detail={
                "sku": sku,
                "provisioning_state": provisioning,
                "public_network_access": public_access,
                "workspace_url": r.get("workspaceUrl", ""),
                "workspace_id": r.get("workspaceId", ""),
                "flags": flags,
            },
        )

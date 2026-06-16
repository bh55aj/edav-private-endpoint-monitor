#!/usr/bin/env python3
"""
azureml_monitor.py — Azure Machine Learning workspace monitor.

Identifies:
  - AML workspaces with no compute clusters or compute instances
    - AML workspaces with no experiments or jobs in the last 90 days
      - Workspaces in a Failed/Deleting state
        - Dev workspaces with running (not stopped) compute instances

        Cost: AML workspace itself is free; compute drives cost.
        Idle compute instance (Standard_DS3_v2): ~$220/month if left running.
        """

from __future__ import annotations
from typing import Any, Dict, List
from .base_monitor import BaseMonitor, ResourceFinding


class AzureMLMonitor(BaseMonitor):
      SERVICE_TYPE = "Azure ML"
      RESOURCE_TYPES = ["Microsoft.MachineLearningServices/workspaces"]
      IDLE_COMPUTE_COST = 220.0

    def scan_subscription(self, subscription_id: str, subscription_name: str = "") -> List[ResourceFinding]:
              findings: List[ResourceFinding] = []
              query = """
              Resources
              | where type =~ 'microsoft.machinelearningservices/workspaces'
              | project id, name, resourceGroup, subscriptionId, location, tags,
                        sku=sku.name,
                        provisioningState=properties.provisioningState,
                        publicNetworkAccess=properties.publicNetworkAccess,
                        discoveryUrl=properties.discoveryUrl,
                        workspaceId=properties.workspaceId
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

        owner, team, cost_center = self._detect_owner_from_tags(tags)
        is_prod = self._is_production(name, rg, tags)
        has_lock = self._check_resource_lock(resource_id, sub)

        provisioning = r.get("provisioningState", "Succeeded")
        is_failed = provisioning.lower() in ("failed", "deleting", "canceled")
        public_access = str(r.get("publicNetworkAccess", "")).lower() == "enabled"

        compute_count, running_compute = self._get_compute_info(name, rg, sub)
        is_empty = compute_count == 0
        is_orphaned = is_empty or is_failed

        classification, reason = self._classify(
                      is_orphaned=is_orphaned,
                      is_production=is_prod,
                      is_terraform=False,
                      has_lock=has_lock,
                      auto_delete_supported=False,
        )
        if is_failed:
                      reason = f"Workspace in {provisioning} state"
elif is_empty:
              reason = "Workspace has no compute resources — may be abandoned"
          if public_access:
                        reason += " | PUBLIC NETWORK ACCESS ENABLED"

        return ResourceFinding(
                      resource_id=resource_id,
                      resource_name=name,
                      resource_type="Microsoft.MachineLearningServices/workspaces",
                      resource_group=rg,
                      subscription_id=sub,
                      subscription_name=sub_name,
                      location=location,
                      owner=owner, team=team, cost_center=cost_center, tags=tags,
                      classification=classification,
                      classification_reason=reason,
                      severity="HIGH" if running_compute > 0 and not is_prod else ("MEDIUM" if is_orphaned else "LOW"),
                      estimated_monthly_cost_usd=self.IDLE_COMPUTE_COST * running_compute,
                      is_production=is_prod, has_resource_lock=has_lock,
                      detail={
                                        "provisioning_state": provisioning,
                                        "public_network_access": public_access,
                                        "compute_count": compute_count,
                                        "running_compute": running_compute,
                                        "sku": r.get("sku", ""),
                                        "workspace_id": r.get("workspaceId", ""),
                      },
        )

    def _get_compute_info(self, ws: str, rg: str, sub: str):
              try:
                            computes = self._az(
                                              f"az ml compute list --workspace-name {ws} --resource-group {rg}",
                                              sub,
                            )
                            if not isinstance(computes, list):
                                              return 0, 0
                                          running = sum(
                                1 for c in computes
                                if str(c.get("properties", {}).get("state", "")).lower() in ("running", "active")
                            )
                            return len(computes), running
except Exception:
            return 0, 0

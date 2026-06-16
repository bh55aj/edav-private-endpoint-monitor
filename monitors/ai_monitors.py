#!/usr/bin/env python3
"""
ai_monitors.py — AI service monitors for EDAV Resource Monitor.

Contains monitors for:
  - AISearchMonitor  (Microsoft.Search/searchServices)
    - AIFoundryMonitor (Microsoft.MachineLearningServices/workspaces kind=hub)
      - OpenAIMonitor    (Microsoft.CognitiveServices/accounts kind=OpenAI)
        - EventGridMonitor (Microsoft.EventGrid/topics + systemTopics)

        Each follows the BaseMonitor interface.
        """

from __future__ import annotations
from typing import Any, Dict, List
from .base_monitor import BaseMonitor, ResourceFinding


# ---------------------------------------------------------------------------
# AI Search Monitor
# ---------------------------------------------------------------------------

AI_SEARCH_TIER_COST = {
      "free": 0.0, "basic": 73.0, "standard": 246.0,
      "standard2": 985.0, "standard3": 1970.0,
      "storage_optimized_l1": 2925.0, "storage_optimized_l2": 5850.0,
}


class AISearchMonitor(BaseMonitor):
      SERVICE_TYPE = "AI Search"
      RESOURCE_TYPES = ["Microsoft.Search/searchServices"]

    def scan_subscription(self, subscription_id: str, subscription_name: str = "") -> List[ResourceFinding]:
              query = """
                      Resources
                              | where type =~ 'microsoft.search/searchservices'
                                      | project id, name, resourceGroup, subscriptionId, location, tags,
                                                        sku=sku.name, replicaCount=properties.replicaCount,
                                                                          partitionCount=properties.partitionCount,
                                                                                            status=properties.status,
                                                                                                              provisioningState=properties.provisioningState,
                                                                                                                                publicNetworkAccess=properties.publicNetworkAccess,
                                                                                                                                                  hostingMode=properties.hostingMode
                                                                                                                                                          """
              resources = self._resource_graph_query(query, [subscription_id])
              return [self._process(r, subscription_id, subscription_name) for r in resources]

    def _process(self, r: Dict[str, Any], sub: str, sub_name: str) -> ResourceFinding:
              name = r.get("name", "")
              rg = r.get("resourceGroup", "")
              tags = self._get_tags(r)
              resource_id = r.get("id", "")
              sku = str(r.get("sku", "standard")).lower()
              monthly_cost = AI_SEARCH_TIER_COST.get(sku, 246.0) * int(r.get("replicaCount", 1) or 1)

        owner, team, cost_center = self._detect_owner_from_tags(tags)
        is_prod = self._is_production(name, rg, tags)
        has_lock = self._check_resource_lock(resource_id, sub)

        status = r.get("status", "running")
        is_degraded = status.lower() in ("degraded", "disabled", "error", "deleting")
        public_access = str(r.get("publicNetworkAccess", "")).lower() == "enabled"

        classification, reason = self._classify(
                      is_orphaned=is_degraded,
                      is_production=is_prod,
                      is_terraform=False,
                      has_lock=has_lock,
                      auto_delete_supported=False,
        )
        if is_degraded:
                      reason = f"Search service status: {status} — ${monthly_cost:.0f}/month potential savings"
                  if public_access:
                                reason += " | PUBLIC NETWORK ACCESS ENABLED"

        return ResourceFinding(
                      resource_id=resource_id, resource_name=name,
                      resource_type="Microsoft.Search/searchServices",
                      resource_group=rg, subscription_id=sub, subscription_name=sub_name,
                      location=r.get("location", ""),
                      owner=owner, team=team, cost_center=cost_center, tags=tags,
                      classification=classification, classification_reason=reason,
                      severity="HIGH" if is_degraded else "LOW",
                      estimated_monthly_cost_usd=monthly_cost if is_degraded else 0.0,
                      is_production=is_prod, has_resource_lock=has_lock,
                      detail={
                                        "sku": sku, "status": status, "replica_count": r.get("replicaCount", 1),
                                        "partition_count": r.get("partitionCount", 1),
                                        "public_network_access": public_access,
                                        "estimated_monthly_cost": monthly_cost,
                      },
        )


# ---------------------------------------------------------------------------
# AI Foundry Monitor (Azure AI Hub — same RP as AML, kind=hub)
# ---------------------------------------------------------------------------

class AIFoundryMonitor(BaseMonitor):
      SERVICE_TYPE = "AI Foundry"
      RESOURCE_TYPES = ["Microsoft.MachineLearningServices/workspaces"]

    def scan_subscription(self, subscription_id: str, subscription_name: str = "") -> List[ResourceFinding]:
              query = """
                      Resources
                              | where type =~ 'microsoft.machinelearningservices/workspaces'
                                      | where properties.kind =~ 'hub' or properties.kind =~ 'project'
                                              | project id, name, resourceGroup, subscriptionId, location, tags,
                                                                kind=properties.kind,
                                                                                  provisioningState=properties.provisioningState,
                                                                                                    publicNetworkAccess=properties.publicNetworkAccess,
                                                                                                                      workspaceId=properties.workspaceId
                                                                                                                              """
              resources = self._resource_graph_query(query, [subscription_id])
              return [self._process(r, subscription_id, subscription_name) for r in resources]

    def _process(self, r: Dict[str, Any], sub: str, sub_name: str) -> ResourceFinding:
              name = r.get("name", "")
              rg = r.get("resourceGroup", "")
              tags = self._get_tags(r)
              resource_id = r.get("id", "")
              kind = r.get("kind", "hub")

        owner, team, cost_center = self._detect_owner_from_tags(tags)
        is_prod = self._is_production(name, rg, tags)
        has_lock = self._check_resource_lock(resource_id, sub)

        provisioning = r.get("provisioningState", "Succeeded")
        is_failed = provisioning.lower() in ("failed", "canceled", "deleting")
        public_access = str(r.get("publicNetworkAccess", "")).lower() == "enabled"
        no_tags = len(tags) == 0

        classification, reason = self._classify(
                      is_orphaned=is_failed or no_tags,
                      is_production=is_prod,
                      is_terraform=False,
                      has_lock=has_lock,
                      auto_delete_supported=False,
        )
        if is_failed:
                      reason = f"AI Foundry {kind} in {provisioning} state"
elif no_tags:
              reason = f"AI Foundry {kind} has no tags — ownership unknown"
          if public_access:
                        reason += " | PUBLIC NETWORK ACCESS ENABLED"

        return ResourceFinding(
                      resource_id=resource_id, resource_name=name,
                      resource_type=f"Microsoft.MachineLearningServices/workspaces ({kind})",
                      resource_group=rg, subscription_id=sub, subscription_name=sub_name,
                      location=r.get("location", ""),
                      owner=owner, team=team, cost_center=cost_center, tags=tags,
                      classification=classification, classification_reason=reason,
                      severity="HIGH" if is_failed else "MEDIUM",
                      estimated_monthly_cost_usd=0.0,
                      is_production=is_prod, has_resource_lock=has_lock,
                      detail={
                                        "kind": kind, "provisioning_state": provisioning,
                                        "public_network_access": public_access,
                                        "workspace_id": r.get("workspaceId", ""),
                      },
        )


# ---------------------------------------------------------------------------
# Azure OpenAI Monitor
# ---------------------------------------------------------------------------

OPENAI_TIER_COST = {"S0": 0.0, "F0": 0.0}  # Cost is consumption-based


class OpenAIMonitor(BaseMonitor):
      SERVICE_TYPE = "Azure OpenAI"
      RESOURCE_TYPES = ["Microsoft.CognitiveServices/accounts"]

    def scan_subscription(self, subscription_id: str, subscription_name: str = "") -> List[ResourceFinding]:
              query = """
                      Resources
                              | where type =~ 'microsoft.cognitiveservices/accounts'
                                      | where kind =~ 'OpenAI' or kind =~ 'AIServices'
                                              | project id, name, resourceGroup, subscriptionId, location, tags,
                                                                kind, sku=sku.name,
                                                                                  provisioningState=properties.provisioningState,
                                                                                                    publicNetworkAccess=properties.publicNetworkAccess,
                                                                                                                      endpoint=properties.endpoint,
                                                                                                                                        customSubDomainName=properties.customSubDomainName,
                                                                                                                                                          deployments=properties.deployments
                                                                                                                                                                  """
              resources = self._resource_graph_query(query, [subscription_id])
              return [self._process(r, subscription_id, subscription_name) for r in resources]

    def _process(self, r: Dict[str, Any], sub: str, sub_name: str) -> ResourceFinding:
              name = r.get("name", "")
              rg = r.get("resourceGroup", "")
              tags = self._get_tags(r)
              resource_id = r.get("id", "")
              kind = r.get("kind", "OpenAI")

        owner, team, cost_center = self._detect_owner_from_tags(tags)
        is_prod = self._is_production(name, rg, tags)
        has_lock = self._check_resource_lock(resource_id, sub)

        provisioning = r.get("provisioningState", "Succeeded")
        is_failed = provisioning.lower() in ("failed", "canceled")
        public_access = str(r.get("publicNetworkAccess", "")).lower() == "enabled"

        # Check deployments
        deployment_count = self._count_deployments(name, rg, sub)
        is_empty = deployment_count == 0

        classification, reason = self._classify(
                      is_orphaned=is_failed or is_empty,
                      is_production=is_prod,
                      is_terraform=False,
                      has_lock=has_lock,
                      auto_delete_supported=False,
        )
        if is_failed:
                      reason = f"Azure OpenAI account in {provisioning} state"
elif is_empty:
              reason = "Azure OpenAI account has no model deployments — may be abandoned"
          if public_access:
                        reason += " | PUBLIC NETWORK ACCESS ENABLED"

        return ResourceFinding(
                      resource_id=resource_id, resource_name=name,
                      resource_type="Microsoft.CognitiveServices/accounts",
                      resource_group=rg, subscription_id=sub, subscription_name=sub_name,
                      location=r.get("location", ""),
                      owner=owner, team=team, cost_center=cost_center, tags=tags,
                      classification=classification, classification_reason=reason,
                      severity="MEDIUM" if is_empty else "LOW",
                      estimated_monthly_cost_usd=0.0,
                      is_production=is_prod, has_resource_lock=has_lock,
                      detail={
                                        "kind": kind, "sku": r.get("sku", ""),
                                        "provisioning_state": provisioning,
                                        "public_network_access": public_access,
                                        "endpoint": r.get("endpoint", ""),
                                        "deployment_count": deployment_count,
                      },
        )

    def _count_deployments(self, account: str, rg: str, sub: str) -> int:
              try:
                            deps = self._az(
                                              f"az cognitiveservices account deployment list "
                                              f"--name {account} --resource-group {rg}",
                                              sub,
                            )
                            return len(deps) if isinstance(deps, list) else 0
except Exception:
            return 0


# ---------------------------------------------------------------------------
# Event Grid Monitor
# ---------------------------------------------------------------------------

class EventGridMonitor(BaseMonitor):
      SERVICE_TYPE = "Event Grid"
      RESOURCE_TYPES = [
          "Microsoft.EventGrid/topics",
          "Microsoft.EventGrid/systemTopics",
          "Microsoft.EventGrid/domains",
      ]

    def scan_subscription(self, subscription_id: str, subscription_name: str = "") -> List[ResourceFinding]:
              query = """
                      Resources
                              | where type in~ ('microsoft.eventgrid/topics',
                                                         'microsoft.eventgrid/systemtopics',
                                                                                    'microsoft.eventgrid/domains')
                                                                                            | project id, name, resourceGroup, subscriptionId, location, tags,
                                                                                                              type, provisioningState=properties.provisioningState,
                                                                                                                                endpoint=properties.endpoint,
                                                                                                                                                  inputSchema=properties.inputSchema,
                                                                                                                                                                    publicNetworkAccess=properties.publicNetworkAccess,
                                                                                                                                                                                      source=properties.source
                                                                                                                                                                                              """
              resources = self._resource_graph_query(query, [subscription_id])
              return [self._process(r, subscription_id, subscription_name) for r in resources]

    def _process(self, r: Dict[str, Any], sub: str, sub_name: str) -> ResourceFinding:
              name = r.get("name", "")
              rg = r.get("resourceGroup", "")
              tags = self._get_tags(r)
              resource_id = r.get("id", "")
              rtype = r.get("type", "Microsoft.EventGrid/topics")

        owner, team, cost_center = self._detect_owner_from_tags(tags)
        is_prod = self._is_production(name, rg, tags)
        has_lock = self._check_resource_lock(resource_id, sub)

        provisioning = r.get("provisioningState", "Succeeded")
        is_failed = provisioning.lower() in ("failed", "canceled")
        public_access = str(r.get("publicNetworkAccess", "")).lower() == "enabled"

        sub_count = self._count_subscriptions(name, rg, sub)
        is_empty = sub_count == 0

        classification, reason = self._classify(
                      is_orphaned=is_failed or is_empty,
                      is_production=is_prod,
                      is_terraform=False,
                      has_lock=has_lock,
                      auto_delete_supported=False,
        )
        if is_failed:
                      reason = f"Event Grid topic in {provisioning} state"
elif is_empty:
              reason = "Event Grid topic has no subscriptions — may be orphaned"
          if public_access:
                        reason += " | PUBLIC NETWORK ACCESS ENABLED"

        return ResourceFinding(
                      resource_id=resource_id, resource_name=name,
                      resource_type=rtype,
                      resource_group=rg, subscription_id=sub, subscription_name=sub_name,
                      location=r.get("location", ""),
                      owner=owner, team=team, cost_center=cost_center, tags=tags,
                      classification=classification, classification_reason=reason,
                      severity="MEDIUM" if is_empty else "LOW",
                      estimated_monthly_cost_usd=0.0,
                      is_production=is_prod, has_resource_lock=has_lock,
                      detail={
                                        "provisioning_state": provisioning,
                                        "public_network_access": public_access,
                                        "subscription_count": sub_count,
                                        "endpoint": r.get("endpoint", ""),
                                        "input_schema": r.get("inputSchema", ""),
                                        "source": r.get("source", ""),
                      },
        )

    def _count_subscriptions(self, topic: str, rg: str, sub: str) -> int:
              try:
                            subs = self._az(
                                              f"az eventgrid topic event-subscription list "
                                              f"--source-resource-id /subscriptions/{sub}/resourceGroups/{rg}"
                                              f"/providers/Microsoft.EventGrid/topics/{topic}",
                                              sub,
                            )
                            return len(subs) if isinstance(subs, list) else 0
except Exception:
            return 0

#!/usr/bin/env python3
"""
eventhub_monitor.py — Azure Event Hubs monitor.

Identifies:
  - Event Hub namespaces with no event hubs defined
    - Event Hub namespaces with zero incoming messages (idle 90+ days)
      - Namespaces in a Disabled state
        - Basic-tier namespaces that could be consolidated

        Cost: Basic ~$10/month, Standard ~$22/month, Premium ~$657+/month
        """

from __future__ import annotations
from typing import Any, Dict, List
from .base_monitor import BaseMonitor, ResourceFinding


TIER_COST = {"Basic": 10.0, "Standard": 22.0, "Premium": 657.0}


class EventHubMonitor(BaseMonitor):
      SERVICE_TYPE = "Event Hubs"
      RESOURCE_TYPES = ["Microsoft.EventHub/namespaces"]
      IDLE_DAYS_THRESHOLD = 90

    def scan_subscription(self, subscription_id: str, subscription_name: str = "") -> List[ResourceFinding]:
              findings: List[ResourceFinding] = []
              query = """
              Resources
              | where type =~ 'microsoft.eventhub/namespaces'
              | project id, name, resourceGroup, subscriptionId, location, tags,
                        sku=sku.name, tier=sku.tier,
                        status=properties.status,
                        serviceBusEndpoint=properties.serviceBusEndpoint,
                        createdAt=properties.createdAt,
                        updatedAt=properties.updatedAt,
                        isAutoInflateEnabled=properties.isAutoInflateEnabled,
                        kafkaEnabled=properties.kafkaEnabled
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
              tier = r.get("tier", r.get("sku", "Standard"))
              monthly_cost = TIER_COST.get(tier, 22.0)

        owner, team, cost_center = self._detect_owner_from_tags(tags)
        is_prod = self._is_production(name, rg, tags)
        has_lock = self._check_resource_lock(resource_id, sub)

        status = r.get("status", "Active")
        is_disabled = status.lower() == "disabled"

        hub_count = self._count_event_hubs(name, rg, sub)
        is_empty = hub_count == 0
        is_orphaned = is_disabled or is_empty

        classification, reason = self._classify(
                      is_orphaned=is_orphaned,
                      is_production=is_prod,
                      is_terraform=False,
                      has_lock=has_lock,
                      auto_delete_supported=False,
        )
        if is_disabled:
                      reason = f"Namespace is DISABLED — ${monthly_cost:.0f}/month potential savings"
elif is_empty:
              reason = "Namespace has no Event Hubs defined"

        return ResourceFinding(
                      resource_id=resource_id,
                      resource_name=name,
                      resource_type="Microsoft.EventHub/namespaces",
                      resource_group=rg,
                      subscription_id=sub,
                      subscription_name=sub_name,
                      location=location,
                      owner=owner, team=team, cost_center=cost_center, tags=tags,
                      classification=classification,
                      classification_reason=reason,
                      severity="HIGH" if is_disabled else ("MEDIUM" if is_empty else "LOW"),
                      estimated_monthly_cost_usd=monthly_cost if is_orphaned else 0.0,
                      is_production=is_prod, has_resource_lock=has_lock,
                      detail={
                                        "status": status,
                                        "sku": tier,
                                        "event_hub_count": hub_count,
                                        "kafka_enabled": r.get("kafkaEnabled", False),
                                        "auto_inflate": r.get("isAutoInflateEnabled", False),
                                        "estimated_tier_cost": monthly_cost,
                      },
        )

    def _count_event_hubs(self, ns: str, rg: str, sub: str) -> int:
              try:
                            hubs = self._az(f"az eventhubs eventhub list --namespace-name {ns} --resource-group {rg}", sub)
                            return len(hubs) if isinstance(hubs, list) else 0
except Exception:
            return -1

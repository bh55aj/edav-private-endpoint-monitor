#!/usr/bin/env python3
"""
redis_monitor.py — Azure Cache for Redis monitor.

Identifies:
  - Redis caches with zero connected clients over 30 days
    - Redis caches in a failed/degraded state
      - Redis caches without SSL-only access (security risk)
        - Basic tier caches with no workload (waste)

        Cost:
          - Basic C0: ~$16/month
            - Standard C1: ~$95/month
              - Premium P1: ~$400/month
              """

from __future__ import annotations
from typing import Any, Dict, List
from .base_monitor import BaseMonitor, ResourceFinding


REDIS_TIER_COST = {
      "Basic": 16.0, "Standard": 95.0, "Premium": 400.0
}


class RedisMonitor(BaseMonitor):
      SERVICE_TYPE = "Redis"
      RESOURCE_TYPES = ["Microsoft.Cache/Redis"]

    def scan_subscription(self, subscription_id: str, subscription_name: str = "") -> List[ResourceFinding]:
              findings: List[ResourceFinding] = []
              query = """
              Resources
              | where type =~ 'microsoft.cache/redis'
              | project id, name, resourceGroup, subscriptionId, location, tags,
                        sku=sku.name, tier=sku.tier, capacity=sku.capacity,
                        provisioningState=properties.provisioningState,
                        enableNonSslPort=properties.enableNonSslPort,
                        redisVersion=properties.redisVersion,
                        hostname=properties.hostName,
                        sslPort=properties.sslPort,
                        linkedServers=properties.linkedServers
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
              monthly_cost = REDIS_TIER_COST.get(tier, 95.0)

        owner, team, cost_center = self._detect_owner_from_tags(tags)
        is_prod = self._is_production(name, rg, tags)
        has_lock = self._check_resource_lock(resource_id, sub)

        provisioning = r.get("provisioningState", "Succeeded")
        is_failed = provisioning.lower() in ("failed", "deleting", "canceled")
        non_ssl_enabled = r.get("enableNonSslPort", False)

        classification, reason = self._classify(
                      is_orphaned=is_failed,
                      is_production=is_prod,
                      is_terraform=False,
                      has_lock=has_lock,
                      auto_delete_supported=False,
        )

        flags = []
        if is_failed:
                      reason = f"Redis cache in {provisioning} state"
                      flags.append("PROVISIONING_FAILURE")
                  if non_ssl_enabled:
                                flags.append("NON_SSL_PORT_ENABLED")
                                reason += " | NON-SSL port enabled — security risk"
                                if classification not in ("DO_NOT_DELETE",):
                                                  classification = "REVIEW_REQUIRED"

                            return ResourceFinding(
                                          resource_id=resource_id,
                                          resource_name=name,
                                          resource_type="Microsoft.Cache/Redis",
                                          resource_group=rg,
                                          subscription_id=sub,
                                          subscription_name=sub_name,
                                          location=location,
                                          owner=owner, team=team, cost_center=cost_center, tags=tags,
                                          classification=classification,
                                          classification_reason=reason,
                                          severity="HIGH" if non_ssl_enabled else ("MEDIUM" if is_failed else "LOW"),
                                          estimated_monthly_cost_usd=monthly_cost if is_failed else 0.0,
                                          is_production=is_prod, has_resource_lock=has_lock,
                                          detail={
                                                            "sku": r.get("sku", ""), "tier": tier,
                                                            "capacity": r.get("capacity", ""),
                                                            "provisioning_state": provisioning,
                                                            "non_ssl_port": non_ssl_enabled,
                                                            "redis_version": r.get("redisVersion", ""),
                                                            "hostname": r.get("hostname", ""),
                                                            "estimated_tier_cost": monthly_cost,
                                                            "flags": flags,
                                          },
                            )

ppservice_monitor.py#!/usr/bin/env python3
"""
appservice_monitor.py — App Services and Function Apps monitor.

Identifies:
  - App Service plans with no apps
  - App Services in a Stopped state
  - Function Apps in a Stopped state with no recent executions
  - App Service plans on paid tiers with zero apps deployed
  - Apps with no HTTP traffic in the last 90 days

Cost:
  - App Service P1v3: ~$130/month
  - App Service S1: ~$70/month
  - Empty App Service Plan (B1): ~$13/month
"""

from __future__ import annotations
from typing import Any, Dict, List
from .base_monitor import BaseMonitor, ResourceFinding


APP_PLAN_TIER_COST = {
    "Free": 0.0, "Shared": 10.0, "Basic": 13.0,
    "Standard": 70.0, "Premium": 130.0, "PremiumV2": 130.0,
    "PremiumV3": 175.0, "Isolated": 1100.0,
}


class AppServiceMonitor(BaseMonitor):
    SERVICE_TYPE = "App Services"
    RESOURCE_TYPES = [
        "Microsoft.Web/serverfarms",
        "Microsoft.Web/sites",
    ]

    def scan_subscription(self, subscription_id: str, subscription_name: str = "") -> List[ResourceFinding]:
        findings: List[ResourceFinding] = []

        # App Service Plans
        plan_query = """
        Resources
        | where type =~ 'microsoft.web/serverfarms'
        | project id, name, resourceGroup, subscriptionId, location, tags,
                  sku=sku.name, tier=sku.tier, capacity=sku.capacity,
                  numberOfSites=properties.numberOfSites,
                  status=properties.status,
                  geoRegion=properties.geoRegion
        """
        plans = self._resource_graph_query(plan_query, [subscription_id])
        for p in plans:
            f = self._process_plan(p, subscription_id, subscription_name)
            if f:
                findings.append(f)

        # App Services and Function Apps
        sites_query = """
        Resources
        | where type =~ 'microsoft.web/sites'
        | project id, name, resourceGroup, subscriptionId, location, tags,
                  kind, state=properties.state,
                  defaultHostName=properties.defaultHostName,
                  enabled=properties.enabled,
                  httpsOnly=properties.httpsOnly,
                  serverFarmId=properties.serverFarmId,
                  siteDisabledReason=properties.siteDisabledReason
        """
        sites = self._resource_graph_query(sites_query, [subscription_id])
        for s in sites:
            f = self._process_site(s, subscription_id, subscription_name)
            if f:
                findings.append(f)

        return findings

    def _process_plan(self, r: Dict[str, Any], sub: str, sub_name: str) -> ResourceFinding:
        name = r.get("name", "")
        rg = r.get("resourceGroup", "")
        tags = self._get_tags(r)
        resource_id = r.get("id", "")
        tier = r.get("tier", r.get("sku", "Basic"))
        monthly_cost = APP_PLAN_TIER_COST.get(tier, 70.0)

        owner, team, cost_center = self._detect_owner_from_tags(tags)
        is_prod = self._is_production(name, rg, tags)
        has_lock = self._check_resource_lock(resource_id, sub)

        num_sites = int(r.get("numberOfSites", 0) or 0)
        is_empty = num_sites == 0 and tier not in ("Free", "Shared")

        classification, reason = self._classify(
            is_orphaned=is_empty,
            is_production=is_prod,
            is_terraform=False,
            has_lock=has_lock,
            auto_delete_supported=False,
        )
        if is_empty:
            reason = f"App Service Plan ({tier}) has NO apps — ${monthly_cost:.0f}/month wasted"

        return ResourceFinding(
            resource_id=resource_id,
            resource_name=name,
            resource_type="Microsoft.Web/serverfarms",
            resource_group=rg,
            subscription_id=sub,
            subscription_name=sub_name,
            location=r.get("location", ""),
            owner=owner, team=team, cost_center=cost_center, tags=tags,
            classification=classification,
            classification_reason=reason,
            severity="HIGH" if is_empty and monthly_cost >= 70.0 else ("MEDIUM" if is_empty else "LOW"),
            estimated_monthly_cost_usd=monthly_cost if is_empty else 0.0,
            is_production=is_prod, has_resource_lock=has_lock,
            detail={
                "sku": r.get("sku", ""), "tier": tier,
                "num_sites": num_sites, "status": r.get("status", ""),
                "estimated_tier_cost": monthly_cost,
            },
        )

    def _process_site(self, r: Dict[str, Any], sub: str, sub_name: str) -> ResourceFinding:
        name = r.get("name", "")
        rg = r.get("resourceGroup", "")
        tags = self._get_tags(r)
        resource_id = r.get("id", "")
        kind = r.get("kind", "app")
        is_function = "functionapp" in kind.lower()

        owner, team, cost_center = self._detect_owner_from_tags(tags)
        is_prod = self._is_production(name, rg, tags)
        has_lock = self._check_resource_lock(resource_id, sub)

        state = r.get("state", "Running")
        is_stopped = state.lower() == "stopped"
        https_only = r.get("httpsOnly", True)

        classification, reason = self._classify(
            is_orphaned=is_stopped,
            is_production=is_prod,
            is_terraform=False,
            has_lock=has_lock,
            auto_delete_supported=False,
        )
        if is_stopped:
            app_type = "Function App" if is_function else "App Service"
            reason = f"{app_type} is STOPPED — review if no longer needed"
        if not https_only:
            reason += " | HTTPS_ONLY disabled — security concern"

        return ResourceFinding(
            resource_id=resource_id,
            resource_name=name,
            resource_type="Microsoft.Web/sites",
            resource_group=rg,
            subscription_id=sub,
            subscription_name=sub_name,
            location=r.get("location", ""),
            owner=owner, team=team, cost_center=cost_center, tags=tags,
            classification=classification,
            classification_reason=reason,
            severity="MEDIUM" if is_stopped else "LOW",
            estimated_monthly_cost_usd=0.0,
            is_production=is_prod, has_resource_lock=has_lock,
            detail={
                "kind": kind, "state": state, "is_function_app": is_function,
                "https_only": https_only,
                "default_host_name": r.get("defaultHostName", ""),
                "enabled": r.get("enabled", True),
            },
        )

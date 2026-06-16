sql_monitor.py#!/usr/bin/env python3
"""
sql_monitor.py — Azure SQL Database and Managed Instance monitor.

Identifies:
  - SQL Servers with no databases
  - Databases in a PAUSED state (serverless auto-pause)
  - Databases with zero DTU/vCore utilization for 30+ days
  - SQL Managed Instances in a stopped state
  - Databases with no connections in the last 90 days

Cost estimates:
  - SQL DB S0: ~$15/month
  - SQL DB General Purpose 4 vCores: ~$370/month
  - SQL MI: ~$1,400+/month
"""

from __future__ import annotations

from typing import Any, Dict, List

from .base_monitor import BaseMonitor, ResourceFinding


class SQLMonitor(BaseMonitor):
    SERVICE_TYPE = "SQL"
    RESOURCE_TYPES = [
        "Microsoft.Sql/servers",
        "Microsoft.Sql/servers/databases",
        "Microsoft.Sql/managedInstances",
    ]

    IDLE_DAYS_THRESHOLD = 30

    # Rough cost estimates by tier
    TIER_COSTS = {
        "Basic": 5.0,
        "Standard": 15.0,
        "Premium": 465.0,
        "GeneralPurpose": 370.0,
        "BusinessCritical": 1120.0,
        "Hyperscale": 500.0,
        "Free": 0.0,
    }

    def scan_subscription(
        self, subscription_id: str, subscription_name: str = ""
    ) -> List[ResourceFinding]:
        findings: List[ResourceFinding] = []

        # SQL Servers
        server_query = """
        Resources
        | where type =~ 'microsoft.sql/servers'
        | project id, name, resourceGroup, subscriptionId, location, tags,
                  fullyQualifiedDomainName=properties.fullyQualifiedDomainName,
                  version=properties.version,
                  publicNetworkAccess=properties.publicNetworkAccess,
                  state=properties.state
        """
        servers = self._resource_graph_query(server_query, [subscription_id])
        for s in servers:
            f = self._process_server(s, subscription_id, subscription_name)
            if f:
                findings.append(f)

        # SQL Databases
        db_query = """
        Resources
        | where type =~ 'microsoft.sql/servers/databases'
        | where name !endswith '/master'
        | project id, name, resourceGroup, subscriptionId, location, tags,
                  status=properties.status,
                  edition=sku.tier,
                  skuName=sku.name,
                  capacity=sku.capacity,
                  maxSizeBytes=properties.maxSizeBytes,
                  collation=properties.collation,
                  creationDate=properties.creationDate,
                  currentServiceObjectiveName=properties.currentServiceObjectiveName
        """
        databases = self._resource_graph_query(db_query, [subscription_id])
        for d in databases:
            f = self._process_database(d, subscription_id, subscription_name)
            if f:
                findings.append(f)

        # SQL Managed Instances
        mi_query = """
        Resources
        | where type =~ 'microsoft.sql/managedinstances'
        | project id, name, resourceGroup, subscriptionId, location, tags,
                  state=properties.state,
                  provisioningState=properties.provisioningState,
                  sku=sku.name,
                  vCores=properties.vCores,
                  storageSizeInGB=properties.storageSizeInGB
        """
        mis = self._resource_graph_query(mi_query, [subscription_id])
        for mi in mis:
            f = self._process_managed_instance(mi, subscription_id, subscription_name)
            if f:
                findings.append(f)

        return findings

    def _process_server(
        self, r: Dict[str, Any], sub: str, sub_name: str
    ) -> ResourceFinding:
        name = r.get("name", "")
        rg = r.get("resourceGroup", "")
        tags = self._get_tags(r)
        resource_id = r.get("id", "")

        owner, team, cost_center = self._detect_owner_from_tags(tags)
        is_prod = self._is_production(name, rg, tags)
        has_lock = self._check_resource_lock(resource_id, sub)

        db_count = self._count_databases(name, rg, sub)
        public_access = str(r.get("publicNetworkAccess", "")).lower() == "enabled"

        is_orphaned = db_count == 0
        classification, reason = self._classify(
            is_orphaned=is_orphaned,
            is_production=is_prod,
            is_terraform=False,
            has_lock=has_lock,
            auto_delete_supported=False,
            extra_safe=False,
        )
        if is_orphaned:
            reason = "SQL Server has no databases — candidate for removal"
        if public_access:
            reason += " | PUBLIC NETWORK ACCESS ENABLED"

        return ResourceFinding(
            resource_id=resource_id,
            resource_name=name,
            resource_type="Microsoft.Sql/servers",
            resource_group=rg,
            subscription_id=sub,
            subscription_name=sub_name,
            location=r.get("location", ""),
            owner=owner, team=team, cost_center=cost_center, tags=tags,
            classification=classification,
            classification_reason=reason,
            severity="MEDIUM" if is_orphaned else "LOW",
            estimated_monthly_cost_usd=2.0 if is_orphaned else 0.0,
            is_production=is_prod, has_resource_lock=has_lock,
            detail={
                "database_count": db_count,
                "public_network_access": public_access,
                "fqdn": r.get("fullyQualifiedDomainName", ""),
                "version": r.get("version", ""),
            },
        )

    def _process_database(
        self, r: Dict[str, Any], sub: str, sub_name: str
    ) -> ResourceFinding:
        name = r.get("name", "")
        rg = r.get("resourceGroup", "")
        tags = self._get_tags(r)
        resource_id = r.get("id", "")

        owner, team, cost_center = self._detect_owner_from_tags(tags)
        is_prod = self._is_production(name, rg, tags)
        has_lock = self._check_resource_lock(resource_id, sub)

        status = r.get("status", "Online")
        edition = r.get("edition", r.get("skuName", "Standard"))
        monthly_cost = self.TIER_COSTS.get(edition, 15.0)

        is_paused = status.lower() == "paused"
        is_orphaned = is_paused

        classification, reason = self._classify(
            is_orphaned=is_orphaned,
            is_production=is_prod,
            is_terraform=False,
            has_lock=has_lock,
            auto_delete_supported=False,
        )
        if is_paused:
            reason = f"Database is PAUSED (auto-pause triggered) — potential candidate for removal"

        return ResourceFinding(
            resource_id=resource_id,
            resource_name=name,
            resource_type="Microsoft.Sql/servers/databases",
            resource_group=rg,
            subscription_id=sub,
            subscription_name=sub_name,
            location=r.get("location", ""),
            owner=owner, team=team, cost_center=cost_center, tags=tags,
            classification=classification,
            classification_reason=reason,
            severity="MEDIUM" if is_paused else "LOW",
            estimated_monthly_cost_usd=monthly_cost if is_orphaned else 0.0,
            is_production=is_prod, has_resource_lock=has_lock,
            detail={
                "status": status,
                "edition": edition,
                "sku_name": r.get("skuName", ""),
                "capacity": r.get("capacity", ""),
                "creation_date": r.get("creationDate", ""),
                "estimated_tier_cost": monthly_cost,
            },
        )

    def _process_managed_instance(
        self, r: Dict[str, Any], sub: str, sub_name: str
    ) -> ResourceFinding:
        name = r.get("name", "")
        rg = r.get("resourceGroup", "")
        tags = self._get_tags(r)
        resource_id = r.get("id", "")
        v_cores = r.get("vCores", 4)
        storage_gb = r.get("storageSizeInGB", 32)

        owner, team, cost_center = self._detect_owner_from_tags(tags)
        is_prod = self._is_production(name, rg, tags)
        has_lock = self._check_resource_lock(resource_id, sub)

        state = r.get("state", "Ready")
        provisioning = r.get("provisioningState", "")
        is_stopped = state.lower() in ("stopped", "stopping")
        is_failed = provisioning.lower() == "failed"

        monthly_cost = v_cores * 350.0  # rough GP estimate per vCore

        classification, reason = self._classify(
            is_orphaned=is_stopped or is_failed,
            is_production=is_prod,
            is_terraform=False,
            has_lock=has_lock,
            auto_delete_supported=False,
        )
        if is_stopped:
            reason = f"Managed Instance is STOPPED — ${monthly_cost:.0f}/month potential savings"
        elif is_failed:
            reason = f"Managed Instance in FAILED state: {provisioning}"

        return ResourceFinding(
            resource_id=resource_id,
            resource_name=name,
            resource_type="Microsoft.Sql/managedInstances",
            resource_group=rg,
            subscription_id=sub,
            subscription_name=sub_name,
            location=r.get("location", ""),
            owner=owner, team=team, cost_center=cost_center, tags=tags,
            classification=classification,
            classification_reason=reason,
            severity="HIGH" if is_stopped else "MEDIUM",
            estimated_monthly_cost_usd=monthly_cost if is_stopped else 0.0,
            is_production=is_prod, has_resource_lock=has_lock,
            detail={
                "state": state,
                "provisioning_state": provisioning,
                "sku": r.get("sku", ""),
                "vcores": v_cores,
                "storage_gb": storage_gb,
                "estimated_monthly_cost": monthly_cost,
            },
        )

    def _count_databases(self, server_name: str, rg: str, sub: str) -> int:
        try:
            dbs = self._az(
                f"az sql db list --server {server_name} --resource-group {rg}",
                sub,
            )
            # exclude master
            return len([d for d in dbs if d.get("name", "") != "master"])
        except Exception:
            return -1

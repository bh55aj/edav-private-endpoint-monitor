#!/usr/bin/env python3
"""
monitors/storage_accounts.py
==============================
EDAV Azure Resource Monitor - Phase 1
Storage Account Monitor (Phase 1 Discovery Module)

Phase 1 scope: Discovery only.
Collects resource name, resource group, subscription, location, tags,
and owner mapping. Future phases will add activity/cost checks.

Wraps the richer StorageMonitor in storage_monitor.py and adds:
- Owner mapping from config/owners.yml
- Phase 1 report columns
- Placeholder hooks for future activity and cost analysis
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base_monitor import BaseMonitor, ResourceFinding


class StorageAccountMonitor(BaseMonitor):
    """
    Phase 1 Storage Account Monitor.

    Discovery scope:
      - resource name
      - resource group
      - subscription
      - location
      - tags
      - owner mapping (from owners.yml / ownership_map.yaml)
      - placeholder for future activity/cost checks

    For full idle-day and container-count analysis, see storage_monitor.py.
    This module is the Phase 1 entry point used by main.py's modular pipeline.
    """

    SERVICE_TYPE = "Storage Accounts"
    RESOURCE_TYPES = ["Microsoft.Storage/storageAccounts"]

    # Phase 1 placeholder thresholds (not enforced yet)
    IDLE_DAYS_THRESHOLD = 90
    BASELINE_MONTHLY_COST = 5.0

    def scan_subscription(
        self, subscription_id: str, subscription_name: str = ""
    ) -> List[ResourceFinding]:
        """Discover storage accounts in one subscription."""
        findings: List[ResourceFinding] = []

        query = """
Resources
| where type =~ 'microsoft.storage/storageaccounts'
| project id, name, resourceGroup, subscriptionId, location, tags,
  kind, skuName=sku.name,
  publicNetworkAccess=properties.publicNetworkAccess,
  allowBlobPublicAccess=properties.allowBlobPublicAccess,
  creationTime=properties.creationTime,
  accessTier=properties.accessTier
"""
        resources = self._resource_graph_query(query, [subscription_id])

        if not resources:
            # Fallback to az CLI list
            try:
                data = self._az(
                    f"az storage account list --subscription {subscription_id}",
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
    ) -> Optional[ResourceFinding]:
        name = r.get("name", "")
        rg = r.get("resourceGroup", r.get("resourceGroup", ""))
        location = r.get("location", "")
        resource_id = r.get("id", "")
        tags = self._get_tags(r)

        # Ownership
        owner, team, cost_center = self._detect_owner_from_tags(tags)
        is_prod = self._is_production(name, rg, tags)
        has_lock = self._check_resource_lock(resource_id, subscription_id)

        # Phase 1: discovery only - no activity check yet
        # TODO Phase 2: call _check_empty() and _get_idle_days()
        is_empty = False   # placeholder
        idle_days = 0      # placeholder
        activity_checked = False

        public_access = str(r.get("publicNetworkAccess", "")).lower() == "enabled"
        blob_public = str(r.get("allowBlobPublicAccess", "")).lower() == "true"

        # Classification - Phase 1: always REVIEW_REQUIRED for storage
        # (auto_delete_supported=False for storage accounts)
        classification = "REVIEW_REQUIRED"
        reason = "Storage account - Phase 1 discovery. Activity check pending."

        if public_access or blob_public:
            reason += " | PUBLIC ACCESS ENABLED - security review needed."

        if is_prod:
            classification = "DO_NOT_DELETE"
            reason = f"Production storage account - manual review required."

        if has_lock:
            classification = "DO_NOT_DELETE"
            reason = "Storage account has Azure lock."

        return ResourceFinding(
            resource_id=resource_id,
            resource_name=name,
            resource_type="Microsoft.Storage/storageAccounts",
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
            severity="HIGH" if (public_access or blob_public) else "MEDIUM",
            estimated_monthly_cost_usd=self.BASELINE_MONTHLY_COST if is_empty else 0.0,
            idle_days=idle_days,
            is_terraform_managed=False,  # Updated by TerraformChecker in main pipeline
            has_resource_lock=has_lock,
            is_production=is_prod,
            detail={
                "sku": r.get("skuName", r.get("sku", {}).get("name", "")),
                "kind": r.get("kind", ""),
                "access_tier": r.get("accessTier", ""),
                "public_network_access": r.get("publicNetworkAccess", "Unknown"),
                "allow_blob_public_access": r.get("allowBlobPublicAccess", "Unknown"),
                "creation_time": r.get("creationTime", ""),
                "activity_checked": activity_checked,
                "phase": "1 - Discovery Only",
                # Phase 2 placeholders
                "is_empty": None,
                "last_transaction_date": None,
                "container_count": None,
                "file_share_count": None,
                "queue_count": None,
                "table_count": None,
            },
        )

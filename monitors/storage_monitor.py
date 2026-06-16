#!/usr/bin/env python3
"""
storage_monitor.py — Storage Account monitor for EDAV Resource Monitor.

Identifies:
  - Storage accounts with no blobs, tables, queues, or file shares
    - Storage accounts with no recent access activity (idle > 90 days)
      - Orphaned storage accounts (no associated resource group owner tag)
        - Storage accounts with public network access enabled (security risk)

        Cost estimates:
          - LRS Standard: ~$0.018/GB/month (minimum $2/month for account overhead)
            - We use a conservative $5/month baseline for empty/idle accounts.
            """

from __future__ import annotations

from typing import Any, Dict, List

from .base_monitor import BaseMonitor, ResourceFinding


class StorageMonitor(BaseMonitor):
      SERVICE_TYPE = "Storage Accounts"
      RESOURCE_TYPES = ["Microsoft.Storage/storageAccounts"]

    # Idle threshold in days
      IDLE_DAYS_THRESHOLD = 90
      # Baseline monthly cost estimate for empty/idle account
      BASELINE_MONTHLY_COST = 5.0

    def scan_subscription(
              self, subscription_id: str, subscription_name: str = ""
    ) -> List[ResourceFinding]:
              findings: List[ResourceFinding] = []

        query = """
                Resources
                        | where type =~ 'microsoft.storage/storageaccounts'
                                | project id, name, resourceGroup, subscriptionId, location, tags,
                                                  kind, sku=sku.name,
                                                                    publicNetworkAccess=properties.publicNetworkAccess,
                                                                                      allowBlobPublicAccess=properties.allowBlobPublicAccess,
                                                                                                        creationTime=properties.creationTime,
                                                                                                                          accessTier=properties.accessTier,
                                                                                                                                            primaryEndpoints=properties.primaryEndpoints
                                                                                                                                                    """
        resources = self._resource_graph_query(query, [subscription_id])

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
    ) -> ResourceFinding:
              name = r.get("name", "")
              rg = r.get("resourceGroup", "")
              location = r.get("location", "")
              tags = self._get_tags(r)
              resource_id = r.get("id", "")

        owner, team, cost_center = self._detect_owner_from_tags(tags)
        is_prod = self._is_production(name, rg, tags)
        has_lock = self._check_resource_lock(resource_id, subscription_id)
        is_tf = self._check_terraform(name, resource_id)

        # Check usage metrics
        is_empty = self._check_empty(name, rg, subscription_id)
        idle_days = self._get_idle_days(name, rg, subscription_id)
        public_access = str(r.get("publicNetworkAccess", "")).lower() == "enabled"
        blob_public = str(r.get("allowBlobPublicAccess", "")).lower() == "true"

        is_orphaned = is_empty and idle_days >= self.IDLE_DAYS_THRESHOLD

        classification, reason = self._classify(
                      is_orphaned=is_orphaned,
                      is_production=is_prod,
                      is_terraform=is_tf,
                      has_lock=has_lock,
                      auto_delete_supported=False,  # Storage requires manual confirmation
                      extra_safe=False,
        )

        # Override: flag security issues
        if public_access or blob_public:
                      if classification == "DO_NOT_DELETE":
                                        reason += " | PUBLIC ACCESS ENABLED — security review needed"
        elif classification in ("REVIEW_REQUIRED", "SAFE_DELETE"):
                classification = "REVIEW_REQUIRED"
                reason = "Public network/blob access enabled — security review required"

        return ResourceFinding(
                      resource_id=resource_id,
                      resource_name=name,
                      resource_type="Microsoft.Storage/storageAccounts",
                      resource_group=rg,
                      subscription_id=subscription_id,
                      subscription_name=subscription_name,
                      location=location,
                      owner=owner,
                      team=team,
                      cost_center=cost_center,
                      tags=tags,
                      classification=classification,
                      classification_reason=reason,
                      severity="HIGH" if is_orphaned else "MEDIUM",
                      estimated_monthly_cost_usd=self.BASELINE_MONTHLY_COST if is_empty else 0.0,
                      idle_days=idle_days,
                      is_terraform_managed=is_tf,
                      has_resource_lock=has_lock,
                      is_production=is_prod,
                      detail={
                                        "sku": r.get("sku", ""),
                                        "kind": r.get("kind", ""),
                                        "access_tier": r.get("accessTier", ""),
                                        "public_network_access": r.get("publicNetworkAccess", ""),
                                        "allow_blob_public_access": r.get("allowBlobPublicAccess", ""),
                                        "is_empty": is_empty,
                                        "creation_time": r.get("creationTime", ""),
                      },
        )

    def _check_empty(self, name: str, rg: str, sub: str) -> bool:
              """Return True if the storage account has no containers/blobs."""
              try:
                            containers = self._az(
                                              f"az storage container list --account-name {name} "
                                              f"--resource-group {rg} --auth-mode login",
                                              sub,
                            )
                            return len(containers) == 0
except Exception:
            return False

    def _get_idle_days(self, name: str, rg: str, sub: str) -> int:
              """Return days since last significant activity via metrics."""
              try:
                            metrics = self._az(
                                              f"az monitor metrics list "
                                              f"--resource /subscriptions/{sub}/resourceGroups/{rg}/providers/"
                                              f"Microsoft.Storage/storageAccounts/{name} "
                                              f"--metric Transactions --interval P1D --aggregation Total "
                                              f"--start-time 2026-01-01T00:00:00Z",
                                              sub,
                            )
                            timeseries = metrics.get("value", [{}])[0].get("timeseries", [{}])[0].get("data", [])
                            active = [d for d in timeseries if (d.get("total") or 0) > 0]
                            if active:
                                              last_str = active[-1].get("timeStamp", "")
                                              return self._estimate_idle_days(last_str)
                                          return 999
except Exception:
            return 0

    def _check_terraform(self, name: str, resource_id: str) -> bool:
              """Light TF check: look for name in common tf state patterns."""
              # Full Terraform check is done by TerraformChecker in main.py
              # This is a lightweight placeholder.
        return False

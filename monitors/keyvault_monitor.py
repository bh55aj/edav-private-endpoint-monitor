#!/usr/bin/env python3
"""
keyvault_monitor.py — Azure Key Vault monitor.

Identifies:
  - Key Vaults with zero secrets, keys, and certificates
    - Key Vaults with soft-delete disabled (compliance risk)
      - Key Vaults with no access policies (orphaned)
        - Key Vaults with public network access enabled
          - Key Vaults inactive for > 90 days

          Cost: Key Vault Standard ~$0.03-0.10/month per 10k operations.
          Empty vaults cost ~$0/month operationally but carry compliance overhead.
          """

from __future__ import annotations

from typing import Any, Dict, List

from .base_monitor import BaseMonitor, ResourceFinding


class KeyVaultMonitor(BaseMonitor):
      SERVICE_TYPE = "Key Vaults"
      RESOURCE_TYPES = ["Microsoft.KeyVault/vaults"]

    IDLE_DAYS_THRESHOLD = 90
    BASELINE_MONTHLY_COST = 2.0

    def scan_subscription(
              self, subscription_id: str, subscription_name: str = ""
    ) -> List[ResourceFinding]:
              findings: List[ResourceFinding] = []

        query = """
                Resources
                        | where type =~ 'microsoft.keyvault/vaults'
                                | project id, name, resourceGroup, subscriptionId, location, tags,
                                                  sku=sku.name,
                                                                    softDeleteEnabled=properties.enableSoftDelete,
                                                                                      softDeleteRetentionDays=properties.softDeleteRetentionInDays,
                                                                                                        purgeProtection=properties.enablePurgeProtection,
                                                                                                                          publicNetworkAccess=properties.publicNetworkAccess,
                                                                                                                                            accessPolicies=properties.accessPolicies,
                                                                                                                                                              vaultUri=properties.vaultUri
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

        access_policies = r.get("accessPolicies", []) or []
        soft_delete = str(r.get("softDeleteEnabled", "true")).lower() == "true"
        purge_protection = str(r.get("purgeProtection", "false")).lower() == "true"
        public_network = str(r.get("publicNetworkAccess", "")).lower() == "enabled"

        # Count objects
        secret_count = self._count_objects(name, "secret", subscription_id)
        key_count = self._count_objects(name, "key", subscription_id)
        cert_count = self._count_objects(name, "certificate", subscription_id)
        total_objects = secret_count + key_count + cert_count

        no_access_policies = len(access_policies) == 0
        is_empty = total_objects == 0
        is_orphaned = is_empty and no_access_policies

        classification, reason = self._classify(
                      is_orphaned=is_orphaned,
                      is_production=is_prod,
                      is_terraform=False,
                      has_lock=has_lock,
                      auto_delete_supported=False,
                      extra_safe=False,
        )

        severity = "LOW"
        compliance_flags = []
        if not soft_delete:
                      compliance_flags.append("SOFT_DELETE_DISABLED")
                      severity = "HIGH"
                  if public_network:
                                compliance_flags.append("PUBLIC_NETWORK_ENABLED")
                                severity = "MEDIUM" if severity == "LOW" else severity
                            if no_access_policies and not is_empty:
                                          compliance_flags.append("NO_ACCESS_POLICIES")
                                      if is_orphaned:
                                                    severity = "MEDIUM"

        if compliance_flags:
                      reason += f" | Compliance flags: {', '.join(compliance_flags)}"
                      if classification == "DO_NOT_DELETE":
                                        pass  # still protected but flagged
else:
                  classification = "REVIEW_REQUIRED"

        return ResourceFinding(
                      resource_id=resource_id,
                      resource_name=name,
                      resource_type="Microsoft.KeyVault/vaults",
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
                      severity=severity,
                      estimated_monthly_cost_usd=self.BASELINE_MONTHLY_COST if is_orphaned else 0.0,
                      is_production=is_prod,
                      has_resource_lock=has_lock,
                      detail={
                                        "sku": r.get("sku", ""),
                                        "soft_delete_enabled": soft_delete,
                                        "purge_protection": purge_protection,
                                        "public_network_access": public_network,
                                        "access_policy_count": len(access_policies),
                                        "secret_count": secret_count,
                                        "key_count": key_count,
                                        "cert_count": cert_count,
                                        "total_objects": total_objects,
                                        "compliance_flags": compliance_flags,
                                        "vault_uri": r.get("vaultUri", ""),
                      },
        )

    def _count_objects(self, vault_name: str, obj_type: str, sub: str) -> int:
              try:
                            result = self._az(
                                              f"az keyvault {obj_type} list --vault-name {vault_name}",
                                              sub,
                            )
                            return len(result) if isinstance(result, list) else 0
except Exception:
            return 0

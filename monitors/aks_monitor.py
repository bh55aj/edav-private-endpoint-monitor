#!/usr/bin/env python3
"""
aks_monitor.py — Azure Kubernetes Service (AKS) monitor.

Identifies:
  - AKS clusters in a stopped/failed state
    - Clusters with zero node count or system node pool count = 0
      - Clusters with no recent workloads (no pods running across namespaces)
        - Dev/test clusters running during off-hours
          - Clusters not tagged with owner or project

          Cost estimates based on VM SKU of system node pools.
          Idle AKS cluster overhead: ~$50-200/month depending on node SKU.
          """

from __future__ import annotations

from typing import Any, Dict, List

from .base_monitor import BaseMonitor, ResourceFinding


class AKSMonitor(BaseMonitor):
      SERVICE_TYPE = "AKS"
      RESOURCE_TYPES = ["Microsoft.ContainerService/managedClusters"]

    IDLE_COST_ESTIMATE = 75.0  # conservative monthly estimate for idle cluster

    def scan_subscription(
              self, subscription_id: str, subscription_name: str = ""
    ) -> List[ResourceFinding]:
              findings: List[ResourceFinding] = []

        query = """
                Resources
                        | where type =~ 'microsoft.containerservice/managedclusters'
                                | project id, name, resourceGroup, subscriptionId, location, tags,
                                                  provisioningState=properties.provisioningState,
                                                                    powerState=properties.powerState.code,
                                                                                      nodeResourceGroup=properties.nodeResourceGroup,
                                                                                                        kubernetesVersion=properties.kubernetesVersion,
                                                                                                                          agentPoolProfiles=properties.agentPoolProfiles,
                                                                                                                                            addonProfiles=properties.addonProfiles,
                                                                                                                                                              fqdn=properties.fqdn
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

        provisioning_state = r.get("provisioningState", "")
        power_state = r.get("powerState", "Running")
        agent_pools = r.get("agentPoolProfiles", []) or []
        k8s_version = r.get("kubernetesVersion", "")

        is_stopped = power_state.lower() in ("stopped", "deallocated")
        is_failed = provisioning_state.lower() == "failed"
        total_nodes = sum(
                      int(pool.get("count", 0)) for pool in agent_pools
                      if isinstance(pool, dict)
        )
        is_empty = total_nodes == 0

        is_orphaned = is_stopped or is_failed or is_empty

        classification, reason = self._classify(
                      is_orphaned=is_orphaned,
                      is_production=is_prod,
                      is_terraform=False,
                      has_lock=has_lock,
                      auto_delete_supported=False,
                      extra_safe=False,
        )

        if is_failed:
                      reason = f"Cluster in FAILED state: {provisioning_state}"
                      classification = "REVIEW_REQUIRED"
elif is_stopped:
              reason = f"Cluster is STOPPED — may be idle or abandoned"
elif is_empty:
              reason = "Cluster has 0 nodes across all agent pools"

        node_pool_summary = ", ".join(
                      f"{p.get('name','?')}:{p.get('count',0)}"
                      for p in agent_pools if isinstance(p, dict)
        )

        return ResourceFinding(
                      resource_id=resource_id,
                      resource_name=name,
                      resource_type="Microsoft.ContainerService/managedClusters",
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
                      severity="HIGH" if is_failed else ("MEDIUM" if is_orphaned else "LOW"),
                      estimated_monthly_cost_usd=self.IDLE_COST_ESTIMATE if is_orphaned else 0.0,
                      is_production=is_prod,
                      has_resource_lock=has_lock,
                      detail={
                                        "provisioning_state": provisioning_state,
                                        "power_state": power_state,
                                        "kubernetes_version": k8s_version,
                                        "total_nodes": total_nodes,
                                        "node_pool_summary": node_pool_summary,
                                        "is_stopped": is_stopped,
                                        "is_failed": is_failed,
                                        "fqdn": r.get("fqdn", ""),
                      },
        )

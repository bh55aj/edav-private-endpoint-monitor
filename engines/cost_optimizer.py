#!/usr/bin/env python3
"""
cost_optimizer.py — Cost Optimization Engine for EDAV Resource Monitor.

Aggregates findings from all monitors and produces:
  1. CostOpportunity list — one entry per idle/orphaned resource
    2. Monthly savings estimate per team, subscription, service, and resource group
      3. Executive cost summary
        4. Cleanup candidate report (CSV + XLSX)

        Usage:
            from engines.cost_optimizer import CostOptimizer
                optimizer = CostOptimizer(findings)
                    report = optimizer.generate_report()
                    """

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
      import pandas as pd
      import openpyxl
      HAS_PANDAS = True
except ImportError:
      HAS_PANDAS = False


@dataclass
class CostOpportunity:
      """Single cost-saving opportunity identified by the optimizer."""
      resource_id: str
      resource_name: str
      resource_type: str
      resource_group: str
      subscription_id: str
      subscription_name: str
      location: str
      owner: str
      team: str
      cost_center: str
      classification: str
      reason: str
      estimated_monthly_savings_usd: float
      idle_days: int
      severity: str
      opportunity_type: str  # IDLE, EMPTY, ORPHANED, DISCONNECTED, FAILED
    action_recommendation: str
    scanned_at: str = field(
              default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
              return {
                            "resource_name": self.resource_name,
                            "resource_type": self.resource_type,
                            "resource_group": self.resource_group,
                            "subscription_name": self.subscription_name,
                            "location": self.location,
                            "owner": self.owner,
                            "team": self.team,
                            "cost_center": self.cost_center,
                            "classification": self.classification,
                            "opportunity_type": self.opportunity_type,
                            "reason": self.reason,
                            "estimated_monthly_savings_usd": round(self.estimated_monthly_savings_usd, 2),
                            "idle_days": self.idle_days,
                            "severity": self.severity,
                            "action_recommendation": self.action_recommendation,
                            "scanned_at": self.scanned_at,
                            "resource_id": self.resource_id,
              }


@dataclass
class CostReport:
      """Aggregated cost optimization report."""
      generated_at: str = field(
          default_factory=lambda: datetime.now(timezone.utc).isoformat()
      )
      total_resources_scanned: int = 0
      total_opportunities: int = 0
      total_estimated_monthly_savings_usd: float = 0.0
      total_estimated_annual_savings_usd: float = 0.0
                    opportunities: List[CostOpportunity] = field(default_factory=list)

    # Breakdowns
    savings_by_team: Dict[str, float] = field(default_factory=dict)
    savings_by_service: Dict[str, float] = field(default_factory=dict)
    savings_by_subscription: Dict[str, float] = field(default_factory=dict)
    savings_by_resource_group: Dict[str, float] = field(default_factory=dict)
    savings_by_opportunity_type: Dict[str, float] = field(default_factory=dict)

    # Top opportunities
    top_10_by_savings: List[CostOpportunity] = field(default_factory=list)

    def to_executive_summary(self) -> str:
              lines = [
                            "# EDAV Resource Monitor — Cost Optimization Executive Summary",
                            f"\nGenerated: {self.generated_at}",
                            f"\n## Top-Line Numbers",
                            f"- **Total resources scanned**: {self.total_resources_scanned:,}",
                            f"- **Cost optimization opportunities**: {self.total_opportunities:,}",
                            f"- **Estimated monthly savings**: ${self.total_estimated_monthly_savings_usd:,.2f}",
                            f"- **Estimated annual savings**: ${self.total_estimated_annual_savings_usd:,.2f}",
                            "\n## Savings by Service",
              ]
              for svc, amt in sorted(self.savings_by_service.items(), key=lambda x: -x[1]):
                            lines.append(f"  - {svc}: ${amt:,.2f}/month")
                        lines.append("\n## Savings by Team")
        for team, amt in sorted(self.savings_by_team.items(), key=lambda x: -x[1])[:10]:
                      lines.append(f"  - {team}: ${amt:,.2f}/month")
                  lines.append("\n## Savings by Opportunity Type")
        for ot, amt in sorted(self.savings_by_opportunity_type.items(), key=lambda x: -x[1]):
                      lines.append(f"  - {ot}: ${amt:,.2f}/month")
                  lines.append("\n## Top 10 Individual Opportunities")
        for i, op in enumerate(self.top_10_by_savings[:10], 1):
                      lines.append(
                                        f"  {i}. **{op.resource_name}** ({op.resource_type}) — "
                                        f"${op.estimated_monthly_savings_usd:,.2f}/month — {op.reason}"
                      )
                  return "\n".join(lines)


class CostOptimizer:
      """
          Analyzes ResourceFinding objects from all monitors and generates
              a comprehensive cost optimization report.
                  """

    # Opportunity type mapping
    OPPORTUNITY_TYPES = {
              "empty": "EMPTY",
              "no containers": "EMPTY",
              "no databases": "EMPTY",
              "no event hubs": "EMPTY",
              "no subscriptions": "EMPTY",
              "no apps": "EMPTY",
              "stopped": "IDLE",
              "paused": "IDLE",
              "idle": "IDLE",
              "0 nodes": "IDLE",
              "failed": "FAILED",
              "disconnected": "DISCONNECTED",
              "orphaned": "ORPHANED",
              "no tags": "UNOWNED",
    }

    def __init__(self, findings: List[Any]):
              """
                      findings: list of ResourceFinding objects from any monitor
                              """
        self.findings = findings

    def generate_report(self) -> CostReport:
              report = CostReport(total_resources_scanned=len(self.findings))
        opportunities: List[CostOpportunity] = []

        for f in self.findings:
                      cost = getattr(f, "estimated_monthly_cost_usd", 0.0) or 0.0
                      if cost <= 0.0 and f.classification not in ("SAFE_DELETE", "REVIEW_REQUIRED"):
                                        continue

                      opportunity_type = self._classify_opportunity(f.classification_reason)
                      action = self._recommend_action(f, opportunity_type)

            opp = CostOpportunity(
                              resource_id=f.resource_id,
                              resource_name=f.resource_name,
                              resource_type=f.resource_type,
                              resource_group=f.resource_group,
                              subscription_id=f.subscription_id,
                              subscription_name=f.subscription_name,
                              location=f.location,
                              owner=f.owner or "UNKNOWN",
                              team=f.team or "UNKNOWN",
                              cost_center=f.cost_center or "",
                              classification=f.classification,
                              reason=f.classification_reason,
                              estimated_monthly_savings_usd=cost,
                              idle_days=getattr(f, "idle_days", 0) or 0,
                              severity=f.severity,
                              opportunity_type=opportunity_type,
                              action_recommendation=action,
            )
            opportunities.append(opp)

        report.opportunities = sorted(
                      opportunities, key=lambda x: -x.estimated_monthly_savings_usd
        )
        report.total_opportunities = len(opportunities)
        report.total_estimated_monthly_savings_usd = sum(
                      o.estimated_monthly_savings_usd for o in opportunities
        )
        report.total_estimated_annual_savings_usd = (
                      report.total_estimated_monthly_savings_usd * 12
        )

        # Breakdowns
        for o in opportunities:
                      report.savings_by_team[o.team] = (
                                        report.savings_by_team.get(o.team, 0.0) + o.estimated_monthly_savings_usd
                      )
                      svc = o.resource_type.split("/")[-1] if "/" in o.resource_type else o.resource_type
                      report.savings_by_service[svc] = (
                          report.savings_by_service.get(svc, 0.0) + o.estimated_monthly_savings_usd
                      )
                      report.savings_by_subscription[o.subscription_name] = (
                          report.savings_by_subscription.get(o.subscription_name, 0.0)
                          + o.estimated_monthly_savings_usd
                      )
                      report.savings_by_resource_group[o.resource_group] = (
                          report.savings_by_resource_group.get(o.resource_group, 0.0)
                          + o.estimated_monthly_savings_usd
                      )
                      report.savings_by_opportunity_type[o.opportunity_type] = (
                          report.savings_by_opportunity_type.get(o.opportunity_type, 0.0)
                          + o.estimated_monthly_savings_usd
                      )

        report.top_10_by_savings = report.opportunities[:10]
        return report

    def write_csv(self, report: CostReport, output_path: str) -> None:
              path = Path(output_path)
              path.parent.mkdir(parents=True, exist_ok=True)
              rows = [o.to_dict() for o in report.opportunities]
              if not rows:
                            return
                        with open(path, "w", newline="", encoding="utf-8") as f:
                                      writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                                      writer.writeheader()
                                      writer.writerows(rows)

    def write_xlsx(self, report: CostReport, output_path: str) -> None:
              if not HAS_PANDAS:
                            return
                        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [o.to_dict() for o in report.opportunities]
        if not rows:
                      return
                  df = pd.DataFrame(rows)
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
                      df.to_excel(writer, sheet_name="Cost Opportunities", index=False)

            # Summary sheet
                      summary = {
                          "Metric": [
                              "Total Resources Scanned",
                              "Cost Optimization Opportunities",
                              "Estimated Monthly Savings (USD)",
                              "Estimated Annual Savings (USD)",
                          ],
                          "Value": [
                              report.total_resources_scanned,
                              report.total_opportunities,
                              f"${report.total_estimated_monthly_savings_usd:,.2f}",
                              f"${report.total_estimated_annual_savings_usd:,.2f}",
                          ],
                      }
                      pd.DataFrame(summary).to_excel(writer, sheet_name="Executive Summary", index=False)

            # By service
                      if report.savings_by_service:
                                        pd.DataFrame(
                                                              [{"Service": k, "Monthly Savings USD": round(v, 2)}
                                                                                    for k, v in sorted(report.savings_by_service.items(), key=lambda x: -x[1])]
                                        ).to_excel(writer, sheet_name="By Service", index=False)

            # By team
            if report.savings_by_team:
                              pd.DataFrame(
                                                    [{"Team": k, "Monthly Savings USD": round(v, 2)}
                                                                          for k, v in sorted(report.savings_by_team.items(), key=lambda x: -x[1])]
                              ).to_excel(writer, sheet_name="By Team", index=False)

    def write_markdown_summary(self, report: CostReport, output_path: str) -> None:
              path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
                      f.write(report.to_executive_summary())

    def _classify_opportunity(self, reason: str) -> str:
              reason_lower = (reason or "").lower()
        for keyword, otype in self.OPPORTUNITY_TYPES.items():
                      if keyword in reason_lower:
                                        return otype
                                return "REVIEW_REQUIRED"

    def _recommend_action(self, finding: Any, opportunity_type: str) -> str:
              rtype = (finding.resource_type or "").lower()
        classification = finding.classification

        if classification == "DO_NOT_DELETE":
                      return "No action — resource is active, locked, or Terraform-managed"
        if opportunity_type == "FAILED":
                      return "Investigate failure, repair or delete if no longer needed"
        if opportunity_type == "EMPTY":
                      if "storage" in rtype:
                                        return "Verify no blobs/containers exist, then delete the account"
                                    if "sql" in rtype and "server" in rtype:
                                                      return "Delete empty SQL server (no databases attached)"
                                                  if "serverfarm" in rtype:
                                                                    return "Delete empty App Service Plan to stop billing"
                                                                return "Delete empty resource after confirming no active workloads"
        if opportunity_type == "IDLE":
                      if "aks" in rtype or "managedcluster" in rtype:
                                        return "Stop or delete idle AKS cluster; export kubeconfig first"
                                    if "managedinstance" in rtype:
                                                      return "Deallocate or delete idle SQL Managed Instance"
                                                  if "redis" in rtype:
                                                                    return "Delete idle Redis cache or downgrade to lower tier"
                                                                return "Review usage metrics; delete or resize if no workload detected"
        if opportunity_type == "ORPHANED":
                      return "Verify no consumers; delete orphaned resource after team approval"
        if opportunity_type == "UNOWNED":
                      return "Assign owner tags; escalate to resource group owner for review"
        return "Review with resource owner and delete if confirmed unused"

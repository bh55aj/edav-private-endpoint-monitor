#!/usr/bin/env python3
"""
ownership_engine.py — Ownership Discovery Engine for EDAV Resource Monitor.

Automatically identifies resource owners using:
  1. Azure resource tags (owner, EDAV_Business_POC, EDAV_Created_By, etc.)
  2. Resource group naming patterns (config/ownership_map.yaml)
  3. Subscription naming patterns
  4. Resource name patterns
  5. RBAC role assignments via az CLI

Produces:
  - Team ownership mapping (resource -> team)
  - Owner report CSV/XLSX
  - Unowned resources report
  - Ownership coverage metrics
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


@dataclass
class OwnershipRecord:
    """Ownership information for a single resource."""
    resource_id: str
    resource_name: str
    resource_type: str
    resource_group: str
    subscription_id: str
    subscription_name: str
    owner: str
    team: str
    cost_center: str
    created_by: str
    ownership_source: str  # TAGS | RG_PATTERN | SUB_PATTERN | NAME_PATTERN | RBAC | UNKNOWN
    tags: Dict[str, str] = field(default_factory=dict)
    classification: str = "UNKNOWN"
    severity: str = "LOW"
    estimated_monthly_cost_usd: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resource_name": self.resource_name,
            "resource_type": self.resource_type,
            "resource_group": self.resource_group,
            "subscription_name": self.subscription_name,
            "owner": self.owner,
            "team": self.team,
            "cost_center": self.cost_center,
            "created_by": self.created_by,
            "ownership_source": self.ownership_source,
            "classification": self.classification,
            "severity": self.severity,
            "estimated_monthly_cost_usd": self.estimated_monthly_cost_usd,
            "tags": json.dumps(self.tags),
            "resource_id": self.resource_id,
        }


@dataclass
class OwnershipReport:
    """Aggregated ownership report across all resources."""
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    records: List[OwnershipRecord] = field(default_factory=list)

    total_resources: int = 0
    owned_resources: int = 0
    unowned_resources: int = 0
    ownership_coverage_pct: float = 0.0

    team_counts: Dict[str, int] = field(default_factory=dict)
    team_cost: Dict[str, float] = field(default_factory=dict)
    unowned_records: List[OwnershipRecord] = field(default_factory=list)

    def finalize(self) -> None:
        self.total_resources = len(self.records)
        for r in self.records:
            if r.owner and r.owner not in ("UNKNOWN", ""):
                self.owned_resources += 1
            else:
                self.unowned_resources += 1
                self.unowned_records.append(r)
            team = r.team or "UNKNOWN"
            self.team_counts[team] = self.team_counts.get(team, 0) + 1
            self.team_cost[team] = (
                self.team_cost.get(team, 0.0) + r.estimated_monthly_cost_usd
            )
        if self.total_resources > 0:
            self.ownership_coverage_pct = (
                self.owned_resources / self.total_resources * 100
            )

    def to_markdown_summary(self) -> str:
        lines = [
            "# EDAV Resource Monitor — Ownership Report",
            f"\nGenerated: {self.generated_at}",
            f"\n## Ownership Coverage",
            f"- Total resources: {self.total_resources:,}",
            f"- Owned resources: {self.owned_resources:,}",
            f"- Unowned resources: {self.unowned_resources:,}",
            f"- Coverage: {self.ownership_coverage_pct:.1f}%",
            "\n## Resources by Team",
        ]
        for team, count in sorted(self.team_counts.items(), key=lambda x: -x[1])[:20]:
            cost = self.team_cost.get(team, 0.0)
            lines.append(f"  - {team}: {count} resources (${cost:,.2f}/month)")
        lines.append("\n## Unowned Resources (Top 20)")
        for r in self.unowned_records[:20]:
            lines.append(
                f"  - {r.resource_name} ({r.resource_type}) — "
                f"{r.resource_group} — ${r.estimated_monthly_cost_usd:,.2f}/month"
            )
        return "\n".join(lines)


class OwnershipEngine:
    """
    Discovers and enriches ownership data for a list of ResourceFinding objects.
    """

    # Tag keys checked in priority order
    OWNER_TAGS = [
        "owner", "Owner", "OWNER",
        "EDAV_Business_POC", "EDAV_Created_By",
        "CreatedBy", "created_by",
        "application", "Application",
    ]
    TEAM_TAGS = [
        "team", "Team", "TEAM",
        "EDAV_Project_Name", "EDAV_Center_Name", "EDAV_Division_Name",
        "project", "Project", "BillingTeam",
    ]
    COST_CENTER_TAGS = [
        "cost_center", "CostCenter", "costcenter",
        "EDAV_Cost_Center", "billing", "Billing",
    ]
    CREATED_BY_TAGS = [
        "CreatedBy", "EDAV_Created_By", "created_by",
        "CreatedByObjectId",
    ]

    def __init__(self, ownership_map_path: Optional[str] = None):
        self.ownership_map: Dict[str, Any] = {}
        self._rbac_cache: Dict[str, Any] = {}
        if ownership_map_path:
            self._load_ownership_map(ownership_map_path)

    def _load_ownership_map(self, path: str) -> None:
        if not HAS_YAML:
            return
        try:
            with open(path) as f:
                self.ownership_map = yaml.safe_load(f) or {}
        except Exception:
            pass

    def enrich(self, findings: List[Any]) -> OwnershipReport:
        """
        Takes a list of ResourceFinding objects, enriches them with
        ownership data, and returns an OwnershipReport.
        """
        report = OwnershipReport()
        for f in findings:
            record = self._enrich_finding(f)
            report.records.append(record)
        report.finalize()
        return report

    def _enrich_finding(self, f: Any) -> OwnershipRecord:
        tags = getattr(f, "tags", {}) or {}
        name = getattr(f, "resource_name", "")
        rg = getattr(f, "resource_group", "")
        sub_name = getattr(f, "subscription_name", "")

        # Priority 1: Tags
        owner = self._extract_tag(tags, self.OWNER_TAGS)
        team = self._extract_tag(tags, self.TEAM_TAGS)
        cost_center = self._extract_tag(tags, self.COST_CENTER_TAGS)
        created_by = self._extract_tag(tags, self.CREATED_BY_TAGS)
        source = "TAGS" if owner else ""

        # Priority 2: RG patterns from ownership_map.yaml
        if not owner:
            owner, team_map = self._match_rg_pattern(rg)
            if not team:
                team = team_map
            if owner:
                source = "RG_PATTERN"

        # Priority 3: Subscription patterns
        if not owner:
            owner, team_map = self._match_sub_pattern(sub_name)
            if not team:
                team = team_map
            if owner:
                source = "SUB_PATTERN"

        # Priority 4: Resource name patterns
        if not owner:
            owner, team_map = self._match_name_pattern(name)
            if not team:
                team = team_map
            if owner:
                source = "NAME_PATTERN"

        if not owner:
            owner = ""
            source = "UNKNOWN"

        return OwnershipRecord(
            resource_id=getattr(f, "resource_id", ""),
            resource_name=name,
            resource_type=getattr(f, "resource_type", ""),
            resource_group=rg,
            subscription_id=getattr(f, "subscription_id", ""),
            subscription_name=sub_name,
            owner=owner,
            team=team or "UNKNOWN",
            cost_center=cost_center,
            created_by=created_by,
            ownership_source=source,
            tags=tags,
            classification=getattr(f, "classification", "UNKNOWN"),
            severity=getattr(f, "severity", "LOW"),
            estimated_monthly_cost_usd=getattr(f, "estimated_monthly_cost_usd", 0.0),
        )

    def _extract_tag(self, tags: Dict[str, str], keys: List[str]) -> str:
        return next((tags[k] for k in keys if k in tags), "")

    def _match_rg_pattern(self, rg: str) -> tuple:
        rg_patterns = self.ownership_map.get("resource_group_patterns", {})
        rg_lower = rg.lower()
        for pattern, info in rg_patterns.items():
            if re.search(pattern.lower(), rg_lower):
                if isinstance(info, dict):
                    return info.get("owner", ""), info.get("team", "")
                return str(info), ""
        return "", ""

    def _match_sub_pattern(self, sub: str) -> tuple:
        sub_patterns = self.ownership_map.get("subscription_patterns", {})
        sub_lower = sub.lower()
        for pattern, info in sub_patterns.items():
            if re.search(pattern.lower(), sub_lower):
                if isinstance(info, dict):
                    return info.get("owner", ""), info.get("team", "")
                return str(info), ""
        return "", ""

    def _match_name_pattern(self, name: str) -> tuple:
        name_patterns = self.ownership_map.get("name_patterns", {})
        name_lower = name.lower()
        for pattern, info in name_patterns.items():
            if re.search(pattern.lower(), name_lower):
                if isinstance(info, dict):
                    return info.get("owner", ""), info.get("team", "")
                return str(info), ""
        return "", ""

    def write_csv(self, report: OwnershipReport, output_path: str) -> None:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [r.to_dict() for r in report.records]
        if not rows:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    def write_xlsx(self, report: OwnershipReport, output_path: str) -> None:
        if not HAS_PANDAS:
            return
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [r.to_dict() for r in report.records]
        if not rows:
            return
        df = pd.DataFrame(rows)
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="All Resources", index=False)

            unowned = [r.to_dict() for r in report.unowned_records]
            if unowned:
                pd.DataFrame(unowned).to_excel(
                    writer, sheet_name="Unowned Resources", index=False
                )

            # Team summary
            team_rows = [
                {
                    "Team": team,
                    "Resource Count": report.team_counts.get(team, 0),
                    "Monthly Cost USD": round(report.team_cost.get(team, 0.0), 2),
                }
                for team in sorted(report.team_counts.keys())
            ]
            if team_rows:
                pd.DataFrame(team_rows).to_excel(
                    writer, sheet_name="Team Summary", index=False
                )

    def write_markdown(self, report: OwnershipReport, output_path: str) -> None:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(report.to_markdown_summary())

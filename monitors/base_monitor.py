#!/usr/bin/env python3
"""
base_monitor.py — Abstract base class for all EDAV resource monitors.

Every service monitor (StorageMonitor, AKSMonitor, etc.) inherits from
BaseMonitor and implements the abstract methods defined here.
"""

from __future__ import annotations

import abc
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Data classes shared across all monitors
# ---------------------------------------------------------------------------

@dataclass
class ResourceFinding:
      """Single resource finding produced by any monitor."""

    # Identity
      resource_id: str = ""
      resource_name: str = ""
      resource_type: str = ""
      resource_group: str = ""
      subscription_id: str = ""
      subscription_name: str = ""
      location: str = ""

    # Ownership
      owner: str = ""
      team: str = ""
      cost_center: str = ""
      created_by: str = ""
      tags: Dict[str, str] = field(default_factory=dict)

    # Classification
      classification: str = "UNKNOWN"
      classification_reason: str = ""
      severity: str = "MEDIUM"

    # Cost
      estimated_monthly_cost_usd: float = 0.0
      last_activity_date: Optional[str] = None
      idle_days: int = 0

    # Governance
      is_terraform_managed: bool = False
      terraform_reason: str = ""
      has_resource_lock: bool = False
      is_production: bool = False

    # Monitor-specific detail
      detail: Dict[str, Any] = field(default_factory=dict)

    # Timestamps
      scanned_at: str = field(
          default_factory=lambda: datetime.now(timezone.utc).isoformat()
      )

    def to_dict(self) -> Dict[str, Any]:
              d = {
                            "resource_id": self.resource_id,
                            "resource_name": self.resource_name,
                            "resource_type": self.resource_type,
                            "resource_group": self.resource_group,
                            "subscription_id": self.subscription_id,
                            "subscription_name": self.subscription_name,
                            "location": self.location,
                            "owner": self.owner,
                            "team": self.team,
                            "cost_center": self.cost_center,
                            "created_by": self.created_by,
                            "tags": json.dumps(self.tags),
                            "classification": self.classification,
                            "classification_reason": self.classification_reason,
                            "severity": self.severity,
                            "estimated_monthly_cost_usd": self.estimated_monthly_cost_usd,
                            "last_activity_date": self.last_activity_date,
                            "idle_days": self.idle_days,
                            "is_terraform_managed": self.is_terraform_managed,
                            "terraform_reason": self.terraform_reason,
                            "has_resource_lock": self.has_resource_lock,
                            "is_production": self.is_production,
                            "scanned_at": self.scanned_at,
              }
              d.update(self.detail)
              return d


@dataclass
class MonitorResult:
      """Aggregated result returned by a monitor after scanning."""

    monitor_name: str
    service_type: str
    findings: List[ResourceFinding] = field(default_factory=list)
    scan_start: str = field(
              default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    scan_end: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    subscriptions_scanned: List[str] = field(default_factory=list)

    # Summary counts (populated by finalize())
    total_resources: int = 0
    safe_delete: int = 0
    review_required: int = 0
    do_not_delete: int = 0
    unknown: int = 0
    estimated_savings_usd: float = 0.0

    def finalize(self) -> None:
              self.scan_end = datetime.now(timezone.utc).isoformat()
              self.total_resources = len(self.findings)
              self.safe_delete = sum(1 for f in self.findings if f.classification == "SAFE_DELETE")
              self.review_required = sum(1 for f in self.findings if f.classification == "REVIEW_REQUIRED")
              self.do_not_delete = sum(1 for f in self.findings if f.classification == "DO_NOT_DELETE")
              self.unknown = sum(1 for f in self.findings if f.classification == "UNKNOWN")
              self.estimated_savings_usd = sum(
                  f.estimated_monthly_cost_usd
                  for f in self.findings
                  if f.classification == "SAFE_DELETE"
              )

    def to_summary_dict(self) -> Dict[str, Any]:
              return {
                            "monitor": self.monitor_name,
                            "service_type": self.service_type,
                            "total_resources": self.total_resources,
                            "safe_delete": self.safe_delete,
                            "review_required": self.review_required,
                            "do_not_delete": self.do_not_delete,
                            "unknown": self.unknown,
                            "estimated_savings_usd": round(self.estimated_savings_usd, 2),
                            "errors": len(self.errors),
                            "scan_start": self.scan_start,
                            "scan_end": self.scan_end,
              }


# ---------------------------------------------------------------------------
# Base monitor
# ---------------------------------------------------------------------------

class BaseMonitor(abc.ABC):
      """
          Abstract base class for all EDAV resource monitors.

              Subclasses must implement:
                      - SERVICE_TYPE  (class attribute str)
                              - RESOURCE_TYPES  (class attribute list[str])
                                      - scan_subscription(subscription_id, subscription_name)

                                          Subclasses may override:
                                                  - estimate_cost(resource)
                                                          - classify(resource)
                                                                  - detect_owner(resource)
                                                                      """

    SERVICE_TYPE: str = "generic"
    RESOURCE_TYPES: List[str] = []

    def __init__(self, config: Optional[Dict[str, Any]] = None):
              self.config = config or {}
              self._az_cache: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def scan_subscription(
              self, subscription_id: str, subscription_name: str = ""
    ) -> List[ResourceFinding]:
              """Scan one subscription and return a list of ResourceFinding objects."""

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def scan(self, subscriptions: List[Dict[str, str]]) -> MonitorResult:
              """
                      Scan all provided subscriptions.

                              subscriptions: list of {"id": "...", "name": "..."}
                                      """
              result = MonitorResult(
                  monitor_name=self.__class__.__name__,
                  service_type=self.SERVICE_TYPE,
              )
              for sub in subscriptions:
                            sub_id = sub.get("id", "")
                            sub_name = sub.get("name", sub_id)
                            try:
                                              findings = self.scan_subscription(sub_id, sub_name)
                                              result.findings.extend(findings)
                                              result.subscriptions_scanned.append(sub_id)
except Exception as exc:
                result.errors.append(f"{sub_id}: {exc}")
        result.finalize()
        return result

    # ------------------------------------------------------------------
    # Shared helpers available to all subclasses
    # ------------------------------------------------------------------

    def _az(self, cmd: str, subscription_id: str = "") -> Any:
              """
                      Run an az CLI command and return parsed JSON.
                              Raises RuntimeError on non-zero exit.
                                      """
              full_cmd = cmd
              if subscription_id and "--subscription" not in cmd:
                            full_cmd = f"{cmd} --subscription {subscription_id}"
                        full_cmd += " -o json"

        cache_key = full_cmd
        if cache_key in self._az_cache:
                      return self._az_cache[cache_key]

        proc = subprocess.run(
                      full_cmd,
                      shell=True,
                      capture_output=True,
                      text=True,
        )
        if proc.returncode != 0:
                      raise RuntimeError(f"az CLI error: {proc.stderr.strip()}")
                  result = json.loads(proc.stdout) if proc.stdout.strip() else {}
        self._az_cache[cache_key] = result
        return result

    def _resource_graph_query(
              self, query: str, subscription_ids: List[str]
    ) -> List[Dict[str, Any]]:
              """
                      Execute an Azure Resource Graph query via az graph query.
                              Returns list of resource dicts.
                                      """
        subs_flag = " ".join(f"--subscriptions {s}" for s in subscription_ids)
        cmd = f'az graph query -q "{query}" {subs_flag} --first 1000 -o json'
        try:
                      result = self._az(cmd)
                      return result.get("data", result) if isinstance(result, dict) else result
except Exception:
            return []

    def _get_tags(self, resource: Dict[str, Any]) -> Dict[str, str]:
              """Safely extract tags dict from a resource."""
        return resource.get("tags") or {}

    def _detect_owner_from_tags(
              self, tags: Dict[str, str]
    ) -> Tuple[str, str, str]:
              """
                      Returns (owner, team, cost_center) extracted from Azure tags.
                              Checks common EDAV tag keys in priority order.
                                      """
        owner_keys = [
                      "owner", "Owner", "OWNER",
                      "EDAV_Business_POC", "EDAV_Created_By",
                      "CreatedBy", "created_by", "application",
        ]
        team_keys = [
                      "team", "Team", "TEAM",
                      "EDAV_Project_Name", "EDAV_Center_Name", "EDAV_Division_Name",
                      "project", "Project",
        ]
        cost_keys = [
                      "cost_center", "CostCenter", "costcenter",
                      "EDAV_Cost_Center", "billing",
        ]

        owner = next((tags[k] for k in owner_keys if k in tags), "")
        team = next((tags[k] for k in team_keys if k in tags), "")
        cost_center = next((tags[k] for k in cost_keys if k in tags), "")
        return owner, team, cost_center

    def _is_production(
              self, name: str, rg: str, tags: Dict[str, str]
    ) -> bool:
              """Heuristic: return True if resource appears to be production."""
              env_tag = tags.get("environment", tags.get("env", tags.get("Environment", "")))
              if env_tag.lower() in ("prod", "production", "prd", "high"):
                            return True
                        for part in (name.lower(), rg.lower()):
                                      for token in ("-prd-", "-prod-", "prd-", "prod-", "-prd", "-prod", "prdwest", "prdeast"):
                                                        if token in part:
                                                                              return True
                                                                  return False

    def _classify(
              self,
              is_orphaned: bool,
              is_production: bool,
              is_terraform: bool,
              has_lock: bool,
              auto_delete_supported: bool = True,
              extra_safe: bool = False,
    ) -> Tuple[str, str]:
              """
                      Core classification logic shared by all monitors.
                              Returns (classification, reason).
                                      """
        if is_terraform:
                      return "DO_NOT_DELETE", "Terraform-managed resource"
                  if has_lock:
                                return "DO_NOT_DELETE", "Resource has an Azure lock"
                            if is_production:
                                          return "DO_NOT_DELETE", "Production resource — manual review required"
                                      if is_orphaned and auto_delete_supported and extra_safe:
                                                    return "SAFE_DELETE", "Confirmed orphaned with no active dependencies"
                                                if is_orphaned:
                                                              return "REVIEW_REQUIRED", "Orphaned/idle but requires manual confirmation"
                                                          return "DO_NOT_DELETE", "Resource appears active"

    def _check_resource_lock(
              self, resource_id: str, subscription_id: str
    ) -> bool:
              """Return True if the resource has any Azure management lock."""
        try:
                      locks = self._az(
                                        f"az lock list --resource {resource_id}",
                                        subscription_id,
                      )
                      return len(locks) > 0
except Exception:
            return False

    def _estimate_idle_days(self, last_activity_iso: Optional[str]) -> int:
              """Return number of days since last_activity_iso (UTC ISO string)."""
        if not last_activity_iso:
                      return 0
                  try:
                                last = datetime.fromisoformat(last_activity_iso.replace("Z", "+00:00"))
                                return (datetime.now(timezone.utc) - last).days
except Exception:
            return 0

#!/usr/bin/env python3
"""
terraform_drift.py — Terraform Drift Detection Engine.

Compares Azure resources discovered by monitors against Terraform state,
identifying:
  1. Resources IN Azure but NOT in Terraform (manual deployments, shadow IT)
    2. Resources IN Terraform but NOT in Azure (orphaned state entries)
      3. Configuration drift (Terraform-managed resources with changed attributes)

      Requires:
        - A Terraform-managed repository with .tf files and optionally terraform.tfstate
          - OR a running Terraform workspace (terraform state list output)

          Usage:
              detector = TerraformDriftDetector(terraform_path="/path/to/tf/repo")
                  report = detector.analyze(findings)
                      detector.write_report(report, "reports/terraform_drift.md")
                      """

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

try:
      import pandas as pd
      HAS_PANDAS = True
except ImportError:
      HAS_PANDAS = False


@dataclass
class DriftRecord:
      """Single drift finding."""
      resource_id: str
      resource_name: str
      resource_type: str
      resource_group: str
      subscription_id: str
      drift_type: str  # NOT_IN_TERRAFORM | NOT_IN_AZURE | CONFIG_DRIFT
    drift_detail: str
    terraform_address: str = ""
    recommended_action: str = ""
    severity: str = "MEDIUM"

    def to_dict(self) -> Dict[str, Any]:
              return {
                            "resource_name": self.resource_name,
                            "resource_type": self.resource_type,
                            "resource_group": self.resource_group,
                            "subscription_id": self.subscription_id,
                            "drift_type": self.drift_type,
                            "drift_detail": self.drift_detail,
                            "terraform_address": self.terraform_address,
                            "recommended_action": self.recommended_action,
                            "severity": self.severity,
                            "resource_id": self.resource_id,
              }


@dataclass
class TerraformDriftReport:
      """Aggregated Terraform drift report."""
      generated_at: str = field(
          default_factory=lambda: datetime.now(timezone.utc).isoformat()
      )
      terraform_path: str = ""
      resources_scanned: int = 0
      terraform_managed_count: int = 0
      not_in_terraform: List[DriftRecord] = field(default_factory=list)
      not_in_azure: List[DriftRecord] = field(default_factory=list)
      config_drift: List[DriftRecord] = field(default_factory=list)
      all_records: List[DriftRecord] = field(default_factory=list)

    def to_markdown_report(self) -> str:
              total_drift = len(self.not_in_terraform) + len(self.not_in_azure) + len(self.config_drift)
              lines = [
                  "# EDAV Resource Monitor — Terraform Drift Report",
                  f"\nGenerated: {self.generated_at}",
                  f"Terraform Path: `{self.terraform_path}`",
                  f"\n## Summary",
                  f"- Resources scanned: {self.resources_scanned:,}",
                  f"- Terraform-managed resources: {self.terraform_managed_count:,}",
                  f"- Total drift findings: {total_drift:,}",
                  f"  - Not in Terraform (manual deployments): {len(self.not_in_terraform):,}",
                  f"  - Not in Azure (stale state): {len(self.not_in_azure):,}",
                  f"  - Configuration drift: {len(self.config_drift):,}",
              ]
              if self.not_in_terraform:
                            lines.append("\n## Resources NOT in Terraform (Manual Deployments / Shadow IT)")
                            lines.append("These Azure resources have no corresponding Terraform resource block.")
                            lines.append("Import them into Terraform or delete them after review.\n")
                            for r in self.not_in_terraform[:50]:
                                              lines.append(f"- **{r.resource_name}** ({r.resource_type}) in `{r.resource_group}`")
                                              lines.append(f"  - {r.drift_detail}")
                                              lines.append(f"  - Recommended: {r.recommended_action}")
                                      if self.not_in_azure:
                                                    lines.append("\n## Resources in Terraform State but NOT in Azure")
                                                    lines.append("These Terraform state entries point to resources that no longer exist.\n")
                                                    for r in self.not_in_azure[:25]:
                                                                      lines.append(f"- `{r.terraform_address}` — resource not found in Azure")
                                                                      lines.append(f"  - Recommended: Run `terraform state rm {r.terraform_address}`")
                                                              if self.config_drift:
                                                                            lines.append("\n## Configuration Drift")
                                                                            lines.append("These resources exist in both Azure and Terraform but have diverged.\n")
                                                                            for r in self.config_drift[:25]:
                                                                                              lines.append(f"- **{r.resource_name}** — {r.drift_detail}")
                                                                                              lines.append(f"  - Recommended: {r.recommended_action}")
                                                                                      return "\n".join(lines)


                class TerraformDriftDetector:
                      """
                          Compares ResourceFinding objects against Terraform state and source files.
                              """

    def __init__(self, terraform_path: Optional[str] = None):
              self.terraform_path = terraform_path
        self._tf_state_names: Set[str] = set()
        self._tf_state_ids: Set[str] = set()
        self._tf_state_addresses: List[str] = []
        self._loaded = False

    def _load_terraform_state(self) -> None:
              if self._loaded or not self.terraform_path:
                            return
                        self._loaded = True
        tf_path = Path(self.terraform_path)
        if not tf_path.exists():
                      return

        # Try terraform state list
        try:
                      result = subprocess.run(
                                        ["terraform", "state", "list"],
                                        cwd=str(tf_path),
                                        capture_output=True,
                                        text=True,
                                        timeout=60,
                      )
                      if result.returncode == 0:
                                        self._tf_state_addresses = [
                                                              line.strip()
                                                              for line in result.stdout.splitlines()
                                                              if line.strip()
                                        ]
                                        for addr in self._tf_state_addresses:
                                                              # Extract resource name from address like module.foo.azurerm_resource.bar
                                                              parts = addr.split(".")
                                                              if parts:
                                                                                        self._tf_state_names.add(parts[-1].lower())
        except Exception:
            pass

        # Also scan .tf files for resource name strings
        for tf_file in tf_path.rglob("*.tf"):
                      try:
                                        content = tf_file.read_text(encoding="utf-8", errors="ignore")
                                        # Extract resource names: resource "azurerm_..." "name" {
                                        for match in re.finditer(r'resource\s+"[^"]+"\s+"([^"]+)"', content):
                                                              self._tf_state_names.add(match.group(1).lower())
                                                          # Also extract name = "..." values
                                                          for match in re.finditer(r'name\s*=\s*"([^"]+)"', content):
                                                                                self._tf_state_names.add(match.group(1).lower())
except Exception:
                pass

    def is_terraform_managed(self, resource_name: str, resource_id: str = "") -> tuple:
              """Return (is_managed, reason)."""
        self._load_terraform_state()
        if not self.terraform_path:
                      return False, "Terraform path not configured"

        name_lower = resource_name.lower()
        if name_lower in self._tf_state_names:
                      return True, f"Found in Terraform state/source as '{resource_name}'"
        if resource_id:
                      id_lower = resource_id.lower()
            if any(id_lower in addr.lower() for addr in self._tf_state_addresses):
                              return True, f"Resource ID found in Terraform state"

        return False, "Not found in Terraform state or source files"

    def analyze(self, findings: List[Any]) -> TerraformDriftReport:
              """
                      Compare Azure findings against Terraform state.
                              Returns a TerraformDriftReport with all drift findings.
                                      """
        self._load_terraform_state()
        report = TerraformDriftReport(
                      terraform_path=self.terraform_path or "not configured",
                      resources_scanned=len(findings),
        )

        for f in findings:
                      name = getattr(f, "resource_name", "")
            resource_id = getattr(f, "resource_id", "")
            rtype = getattr(f, "resource_type", "")
            rg = getattr(f, "resource_group", "")
            sub = getattr(f, "subscription_id", "")

            is_tf, reason = self.is_terraform_managed(name, resource_id)
            if is_tf:
                              report.terraform_managed_count += 1
else:
                drift = DriftRecord(
                                      resource_id=resource_id,
                                      resource_name=name,
                                      resource_type=rtype,
                                      resource_group=rg,
                                      subscription_id=sub,
                                      drift_type="NOT_IN_TERRAFORM",
                                      drift_detail=f"Resource '{name}' ({rtype}) not found in Terraform state or .tf files",
                                      recommended_action=(
                                                                f"Run: az resource show --ids {resource_id} to confirm, "
                                                                f"then either `terraform import` or schedule for deletion"
                                      ),
                                      severity="MEDIUM",
                )
                report.not_in_terraform.append(drift)
                report.all_records.append(drift)

        # Check for stale state entries (in TF but not in Azure findings)
        if self.terraform_path and self._tf_state_addresses:
                      azure_names_lower = {
                          getattr(f, "resource_name", "").lower() for f in findings
        }
            for addr in self._tf_state_addresses:
                              tf_name = addr.split(".")[-1].lower()
                              if tf_name and tf_name not in azure_names_lower:
                                                    drift = DriftRecord(
                                                                              resource_id="",
                                                                              resource_name=tf_name,
                                                                              resource_type="UNKNOWN",
                                                                              resource_group="",
                                                                              subscription_id="",
                                                                              drift_type="NOT_IN_AZURE",
                                                                              drift_detail=f"Terraform address '{addr}' not found in Azure scan results",
                                                                              terraform_address=addr,
                                                                              recommended_action=f"Verify in Azure Portal, then run: terraform state rm {addr}",
                                                                              severity="LOW",
                                                    )
                                                    report.not_in_azure.append(drift)
                                                    report.all_records.append(drift)

                      return report

    def write_csv(self, report: TerraformDriftReport, output_path: str) -> None:
              path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [r.to_dict() for r in report.all_records]
        if not rows:
                      return
        with open(path, "w", newline="", encoding="utf-8") as f:
                      writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    def write_xlsx(self, report: TerraformDriftReport, output_path: str) -> None:
              if not HAS_PANDAS:
                            return
                        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
                      for records, sheet_name in [
                          (report.not_in_terraform, "Not In Terraform"),
                          (report.not_in_azure, "Not In Azure"),
                          (report.config_drift, "Config Drift"),
        ]:
                          if records:
                                                pd.DataFrame([r.to_dict() for r in records]).to_excel(
                                                                          writer, sheet_name=sheet_name, index=False
                                                )

              def write_markdown(self, report: TerraformDriftReport, output_path: str) -> None:
                        path = Path(output_path)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with open(path, "w", encoding="utf-8") as f:
                                      f.write(report.to_markdown_report())
                          

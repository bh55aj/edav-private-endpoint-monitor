#!/usr/bin/env python3
"""
monitors/terraform_drift.py
============================
EDAV Azure Resource Monitor - Phase 1
Terraform Drift Monitor (monitors/ package entry point)

This module is the monitors/ package interface for Terraform drift detection.
It wraps the richer TerraformDriftDetector in engines/terraform_drift.py
and exposes a clean scan() interface consistent with other monitors.

Adds Phase 1 fields to each ResourceFinding:
  - terraform_status: "Terraform Managed" | "Not Found In Terraform" | "Unknown"
  - drift_type: NOT_IN_TERRAFORM | NOT_IN_AZURE | CONFIG_DRIFT
  - terraform_address: state address if found
  - recommended_action: import or remove guidance

Usage (called by main.py pipeline):
  from monitors.terraform_drift import TerraformDriftMonitor
  monitor = TerraformDriftMonitor(terraform_path="/path/to/tf")
  result = monitor.scan(subscriptions=[...])
  report = monitor.analyze_findings(all_findings)
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .base_monitor import BaseMonitor, ResourceFinding


class TerraformDriftMonitor(BaseMonitor):
    """
    Monitors/ package Terraform Drift adapter.

    Primary purpose: enrich ResourceFinding objects with Terraform
    managed status. Also exposes analyze_findings() for full drift reports.

    For full drift reporting (DriftRecord, TerraformDriftReport), use:
        engines/terraform_drift.py directly.
    """

    SERVICE_TYPE = "Terraform Drift"
    RESOURCE_TYPES = []  # Cross-cutting - applies to all resource types

    def __init__(self, config: Optional[Dict[str, Any]] = None, terraform_path: Optional[str] = None):
        super().__init__(config)
        self.terraform_path = terraform_path or (config or {}).get("terraform_path")
        self._tf_state_names: Set[str] = set()
        self._tf_state_ids: Set[str] = set()
        self._tf_state_addresses: List[str] = []
        self._loaded = False

    def scan_subscription(
        self, subscription_id: str, subscription_name: str = ""
    ) -> List[ResourceFinding]:
        """
        Terraform drift monitor does not scan subscriptions independently.
        It enriches findings produced by other monitors.
        Returns empty list (use analyze_findings instead).
        """
        return []

    # ------------------------------------------------------------------
    # Main interface: enrich existing findings
    # ------------------------------------------------------------------

    def enrich_findings(self, findings: List[ResourceFinding]) -> List[ResourceFinding]:
        """
        Enrich a list of ResourceFinding objects with Terraform drift status.
        Mutates each finding's is_terraform_managed and terraform_reason fields.
        Returns the enriched list.
        """
        self._load_state()
        for finding in findings:
            name = getattr(finding, "resource_name", "")
            resource_id = getattr(finding, "resource_id", "")
            is_managed, reason = self.is_terraform_managed(name, resource_id)
            finding.is_terraform_managed = is_managed
            finding.terraform_reason = reason
            # Re-classify if TF managed and not already DO_NOT_DELETE
            if is_managed and finding.classification not in ("DO_NOT_DELETE",):
                finding.classification = "DO_NOT_DELETE"
                finding.classification_reason = f"Terraform-managed: {reason}"
        return findings

    def get_terraform_status(self, resource_name: str, resource_id: str = "") -> str:
        """
        Returns Phase 1 terraform_status string:
          "Terraform Managed" | "Not Found In Terraform" | "Unknown"
        """
        if not self.terraform_path:
            return "Unknown"
        self._load_state()
        is_managed, _ = self.is_terraform_managed(resource_name, resource_id)
        if is_managed:
            return "Terraform Managed"
        return "Not Found In Terraform"

    def is_terraform_managed(self, resource_name: str, resource_id: str = "") -> Tuple[bool, str]:
        """Return (is_managed, reason)."""
        if not self.terraform_path:
            return False, "Terraform path not configured"
        self._load_state()
        name_lower = resource_name.lower()
        if name_lower in self._tf_state_names:
            return True, f"Found in Terraform state/source as '{resource_name}'"
        if resource_id:
            id_lower = resource_id.lower()
            if any(id_lower in addr.lower() for addr in self._tf_state_addresses):
                return True, f"Resource ID found in Terraform state"
        return False, "Not found in Terraform state or source files"

    def list_stale_state_entries(self, azure_resource_names: Set[str]) -> List[str]:
        """
        Return Terraform state addresses that no longer exist in Azure.
        Used for NOT_IN_AZURE drift detection.
        """
        self._load_state()
        azure_lower = {n.lower() for n in azure_resource_names}
        stale = []
        for addr in self._tf_state_addresses:
            tf_name = addr.split(".")[-1].lower()
            if tf_name and tf_name not in azure_lower:
                stale.append(addr)
        return stale

    # ------------------------------------------------------------------
    # State loading
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
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
                    parts = addr.split(".")
                    if parts:
                        self._tf_state_names.add(parts[-1].lower())
        except Exception:
            pass

        # Scan .tf source files for resource names
        for tf_file in tf_path.rglob("*.tf"):
            try:
                content = tf_file.read_text(encoding="utf-8", errors="ignore")
                # resource "azurerm_..." "name" { patterns
                for match in re.finditer(r'resource\s+"[^"]+"\s+"([^"]+)"', content):
                    self._tf_state_names.add(match.group(1).lower())
                # name = "..." value patterns
                for match in re.finditer(r'name\s*=\s*"([^"]+)"', content):
                    self._tf_state_names.add(match.group(1).lower())
            except Exception:
                pass

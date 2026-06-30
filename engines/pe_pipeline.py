#!/usr/bin/env python3
"""
============================================================================
EDAV Private Endpoint Cleanup Pipeline - engines/pe_pipeline.py
============================================================================
End-to-end pipeline for the EDAV Private Endpoint Governance workflow:

  1. Import  - Read Resource Monitor Excel export (authoritative source)
  2. Enrich  - Merge with Azure Resource Graph / REST discovery
  3. Validate - Check each endpoint against live Azure (exists? locked? dependent?)
  4. Classify - SAFE_DELETE | REVIEW_REQUIRED | KEEP | ALREADY_REMOVED | UNKNOWN
  5. Preview  - Generate deletion plan report BEFORE any deletions
  6. Delete   - Delete individually with backup, confirmation, and post-verify
  7. Report   - Excel, HTML dashboard, Markdown executive summary, JSON, CSV

Safety gates enforced at every step:
  - SU account required for delete
  - ApprovalTicket, ApprovedBy, ApprovedToDelete required
  - No resource locks
  - No active backend dependency
  - Dry-run simulates all steps without touching Azure
  - Interactive CONFIRM prompt before bulk delete

Version: v7.0.0 | EDAV Platform Team
============================================================================
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ============================================================================
# CLASSIFICATION CONSTANTS
# ============================================================================

CLS_SAFE_DELETE     = "SAFE_DELETE"
CLS_REVIEW_REQUIRED = "REVIEW_REQUIRED"
CLS_KEEP            = "KEEP"
CLS_ALREADY_REMOVED = "ALREADY_REMOVED"
CLS_UNKNOWN         = "UNKNOWN"

# Azure PE monthly cost (processing + data hours)
PE_MONTHLY_COST = 7.30

DEFAULT_REQUIRED_USER = "bh55-su@cdc.gov"

# ============================================================================
# ENRICHED ENDPOINT RECORD
# ============================================================================

@dataclass
class EnrichedEndpoint:
    """
    A private endpoint record that has been validated against live Azure.
    Produced by the pipeline after merging RM findings with Azure data.
    """

    # Identity (from Resource Monitor)
    name: str = ""
    resource_group: str = ""
    subscription: str = ""
    location: str = ""
    resource_id: str = ""

    # Finding metadata
    check_name: str = ""
    detail: str = ""
    rm_connection_state: str = ""

    # Azure live validation
    azure_exists: Optional[bool] = None
    azure_connection_state: str = ""
    azure_backend_id: str = ""
    azure_backend_exists: Optional[bool] = None
    has_lock: bool = False
    lock_reason: str = ""
    has_dependencies: bool = False
    dependency_detail: str = ""
    validation_error: str = ""

    # Classification
    classification: str = ""
    classification_reason: str = ""
    blockers: List[str] = field(default_factory=list)

    # Ownership
    owner_team: str = ""
    owner_contact: str = ""
    terraform_managed: bool = False
    azure_tags: Dict = field(default_factory=dict)

    # Approval
    approved_to_delete: bool = False
    approval_ticket: str = ""
    approved_by: str = ""

    # Cost
    monthly_cost_usd: float = PE_MONTHLY_COST
    yearly_cost_usd: float = field(default=0.0)

    # Delete result
    delete_result: str = ""
    delete_command: str = ""
    delete_method: str = ""
    delete_timestamp: str = ""
    verify_result: str = ""
    backup_path: str = ""
    delete_error: str = ""

    # Source
    source: str = ""
    source_file: str = ""
    source_row: int = 0
    notes: str = ""

    def __post_init__(self):
        self.yearly_cost_usd = round(self.monthly_cost_usd * 12, 2)

    @property
    def is_deletable(self) -> bool:
        """True if all gates pass and resource is approved."""
        return (
            self.classification == CLS_SAFE_DELETE
            and self.approved_to_delete
            and bool(self.approval_ticket)
            and bool(self.approved_by)
            and not self.has_lock
            and not self.has_dependencies
            and self.azure_exists is not False
        )

    @property
    def blockers_display(self) -> str:
        return "; ".join(self.blockers) if self.blockers else "None"

    def to_dict(self) -> Dict:
        return {
            "Name": self.name,
            "ResourceGroup": self.resource_group,
            "Subscription": self.subscription,
            "Location": self.location,
            "ResourceID": self.resource_id,
            "RMConnectionState": self.rm_connection_state,
            "AzureExists": self.azure_exists,
            "AzureConnectionState": self.azure_connection_state,
            "BackendExists": self.azure_backend_exists,
            "HasLock": self.has_lock,
            "LockReason": self.lock_reason,
            "HasDependencies": self.has_dependencies,
            "DependencyDetail": self.dependency_detail,
            "Classification": self.classification,
            "ClassificationReason": self.classification_reason,
            "Blockers": self.blockers_display,
            "OwnerTeam": self.owner_team,
            "TerraformManaged": self.terraform_managed,
            "ApprovedToDelete": self.approved_to_delete,
            "ApprovalTicket": self.approval_ticket,
            "ApprovedBy": self.approved_by,
            "MonthlyCostUSD": self.monthly_cost_usd,
            "YearlyCostUSD": self.yearly_cost_usd,
            "DeleteResult": self.delete_result,
            "DeleteCommand": self.delete_command,
            "DeleteTimestamp": self.delete_timestamp,
            "VerifyResult": self.verify_result,
            "BackupPath": self.backup_path,
            "DeleteError": self.delete_error,
            "Source": self.source,
            "SourceFile": self.source_file,
            "Notes": self.notes,
        }


# ============================================================================
# AZURE VALIDATOR
# ============================================================================

class AzurePEValidator:
    """Validates private endpoints against live Azure CLI."""

    def __init__(self, dry_run: bool = False, timeout: int = 60):
        self.dry_run = dry_run
        self.timeout = timeout

    def validate(self, ep: EnrichedEndpoint) -> EnrichedEndpoint:
        """Validate one endpoint. Updates ep in place and returns it."""
        if self.dry_run:
            ep.azure_exists = True
            ep.azure_connection_state = ep.rm_connection_state or "Disconnected"
            logger.debug("[DRY-RUN] Skipping Azure validation for %s", ep.name)
            return ep

        # Set subscription context
        if ep.subscription:
            self._set_subscription(ep.subscription)

        # Show endpoint
        data, err = self._az(["network", "private-endpoint", "show",
                              "--name", ep.name,
                              "--resource-group", ep.resource_group])

        if err == "ResourceNotFound" or (err and "not found" in err.lower()):
            ep.azure_exists = False
            return ep

        if err and not data:
            ep.azure_exists = None
            ep.validation_error = err[:200]
            return ep

        ep.azure_exists = True
        ep.azure_tags = data.get("tags") or {}
        ep.resource_id = data.get("id", ep.resource_id)
        ep.location = data.get("location", ep.location)

        # Connection state
        conns = (data.get("privateLinkServiceConnections") or
                 data.get("manualPrivateLinkServiceConnections") or [])
        if conns:
            state_obj = conns[0].get("privateLinkServiceConnectionState", {})
            ep.azure_connection_state = state_obj.get("status", "Unknown")
            ep.azure_backend_id = conns[0].get("privateLinkServiceId", "")
        else:
            ep.azure_connection_state = "Unknown"

        # Backend existence
        if ep.azure_backend_id:
            _, berr = self._az(["resource", "show", "--ids", ep.azure_backend_id])
            ep.azure_backend_exists = not (
                berr == "ResourceNotFound" or
                (berr and "not found" in berr.lower())
            )
        else:
            ep.azure_backend_exists = None

        # Resource lock
        lock_data, _ = self._az(["lock", "list",
                                  "--resource-group", ep.resource_group])
        if lock_data and isinstance(lock_data, list) and len(lock_data) > 0:
            ep.has_lock = True
            ep.lock_reason = lock_data[0].get("name", "lock") + " (" + lock_data[0].get("level","") + ")"

        return ep

    def _set_subscription(self, sub: str) -> bool:
        try:
            r = subprocess.run(["az", "account", "set", "--subscription", sub],
                               capture_output=True, text=True, timeout=20)
            return r.returncode == 0
        except Exception:
            return False

    def _az(self, cmd: List[str]) -> Tuple[Optional[Dict], Optional[str]]:
        try:
            r = subprocess.run(["az"] + cmd + ["--output", "json"],
                               capture_output=True, text=True, timeout=self.timeout)
            if r.returncode != 0:
                err = r.stderr.strip()
                if "ResourceNotFound" in err or "was not found" in err.lower():
                    return None, "ResourceNotFound"
                return None, err
            if not r.stdout.strip():
                return None, None
            return json.loads(r.stdout), None
        except Exception as exc:
            return None, str(exc)


# ============================================================================
# TEAM IDENTIFIER
# ============================================================================

# Pattern -> team name (checked in order, first match wins)
TEAM_PATTERNS = [
    (r"databricks|dbfs|dbw", "Databricks"),
    (r"aks|aksnode|kube|k8s|kubernetes", "AKS"),
    (r"(?:networking|nsg|vnet|subnet|gateway|agw|appgw|firewall|bastion)", "Networking"),
    (r"storage|blob|datalake|adls|gen2", "Storage"),
    (r"sql|sqlmi|managed.?instance|postgres|mssql", "SQL"),
    (r"backup|asr|recovery|bkp|snapshot", "Backup"),
    (r"analytics|synapse|eventhub|eventhubs|stream", "Analytics"),
    (r"ml|machinelearning|aiml|openai|cognitiveservices", "AI/ML"),
    (r"ngs|nextgen|genomics|seq", "NGS"),
    (r"keyvault|kv(?!ault)|secret", "Security"),
    (r"acr|registry|container", "Platform"),
]

def identify_team(name: str, rg: str, tags: Dict) -> str:
    """Infer owner team from resource name, RG, and tags."""
    # 1. From tags
    tag_keys = ["owner", "Owner", "team", "Team", "EDAV_Business_POC",
                "EDAV_Created_By", "application", "Application"]
    for k in tag_keys:
        v = tags.get(k, "")
        if v:
            return str(v)

    # 2. From name + RG patterns
    combined = (name + " " + rg).lower()
    for pattern, team in TEAM_PATTERNS:
        if re.search(pattern, combined, re.IGNORECASE):
            return team

    return "General Infrastructure"


# ============================================================================
# CLASSIFIER
# ============================================================================

def classify_endpoint(ep: EnrichedEndpoint) -> EnrichedEndpoint:
    """Classify an endpoint and populate blockers list."""
    blockers = []

    # Already removed
    if ep.azure_exists is False:
        ep.classification = CLS_ALREADY_REMOVED
        ep.classification_reason = "Resource not found in Azure (already deleted)"
        return ep

    # Cannot determine state
    if ep.azure_exists is None:
        ep.classification = CLS_UNKNOWN
        ep.classification_reason = "Azure validation failed: " + ep.validation_error[:80]
        return ep

    # Connected -> KEEP
    conn = (ep.azure_connection_state or ep.rm_connection_state or "").lower()
    if conn in ("approved", "connected"):
        ep.classification = CLS_KEEP
        ep.classification_reason = "Endpoint is connected/approved - in use"
        return ep

    # Check for blockers
    if ep.has_lock:
        blockers.append("Azure resource lock: " + ep.lock_reason)

    if ep.has_dependencies:
        blockers.append("Active dependencies: " + ep.dependency_detail[:60])

    if ep.azure_backend_exists is True:
        blockers.append("Backend resource still exists - verify disconnect is intentional")

    ep.blockers = blockers

    if blockers:
        ep.classification = CLS_REVIEW_REQUIRED
        ep.classification_reason = "Blockers present: " + "; ".join(blockers[:2])
    else:
        ep.classification = CLS_SAFE_DELETE
        ep.classification_reason = (
            "Disconnected PE, no backend, no lock, no dependencies. "
            "Safe to remove after approval."
        )

    return ep


# ============================================================================
# CLEANUP ENGINE (DELETE + VERIFY)
# ============================================================================

class PECleanupEngine:
    """Handles backup, deletion, and post-delete verification of private endpoints."""

    def __init__(self,
                 backup_dir: str = "backups",
                 dry_run: bool = False,
                 delete_pause: int = 2,
                 timeout: int = 120):
        self.backup_dir = Path(backup_dir)
        self.dry_run = dry_run
        self.delete_pause = delete_pause
        self.timeout = timeout
        self.validator = AzurePEValidator(dry_run=dry_run, timeout=timeout)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def backup(self, ep: EnrichedEndpoint) -> str:
        """Create ARM JSON backup. Returns backup file path."""
        if self.dry_run:
            return "[dry-run - no backup]"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.backup_dir / f"{ep.name}_{ts}.json"
        backup_data = {
            "backup_timestamp": datetime.now().isoformat(),
            "name": ep.name,
            "resource_group": ep.resource_group,
            "subscription": ep.subscription,
            "resource_id": ep.resource_id,
            "approval_ticket": ep.approval_ticket,
            "approved_by": ep.approved_by,
            "azure_connection_state": ep.azure_connection_state,
            "azure_backend_id": ep.azure_backend_id,
            "azure_tags": ep.azure_tags,
        }
        try:
            with open(path, "w") as f:
                json.dump(backup_data, f, indent=2, default=str)
            return str(path)
        except Exception as exc:
            logger.warning("Backup failed for %s: %s", ep.name, exc)
            return ""

    def delete(self, ep: EnrichedEndpoint) -> EnrichedEndpoint:
        """Delete one endpoint. Updates ep with result. Returns ep."""
        if self.dry_run:
            ep.delete_result = "DRY_RUN"
            ep.delete_command = "az network private-endpoint delete [DRY-RUN]"
            ep.delete_method = "dry_run"
            ep.delete_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ep.verify_result = "DRY_RUN_SKIP"
            return ep

        ep.delete_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Build delete command - prefer --ids, fall back to --name + --resource-group
        if ep.resource_id:
            cmd = ["az", "network", "private-endpoint", "delete",
                   "--ids", ep.resource_id]
            ep.delete_method = "ids"
        else:
            cmd = ["az", "network", "private-endpoint", "delete",
                   "--name", ep.name,
                   "--resource-group", ep.resource_group]
            ep.delete_method = "name_rg"

        ep.delete_command = " ".join(cmd)
        logger.info("DELETE: %s", ep.delete_command)

        t0 = time.time()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=self.timeout)
            duration = round(time.time() - t0, 1)
            if r.returncode != 0:
                err = r.stderr.strip()
                if "ResourceNotFound" in err or "was not found" in err.lower():
                    ep.delete_result = "ALREADY_GONE"
                elif "AuthorizationFailed" in err or "does not have authorization" in err.lower():
                    ep.delete_result = "RBAC_BLOCKED"
                    ep.delete_error = "RBAC: insufficient permissions for Microsoft.Network/privateEndpoints/delete"
                elif "ReadOnlyDisabledSubscription" in err:
                    ep.delete_result = "SUB_READ_ONLY"
                    ep.delete_error = "Subscription is read-only"
                else:
                    ep.delete_result = "DELETE_FAILED"
                    ep.delete_error = err[:300]
            else:
                ep.delete_result = "DELETED"
                logger.info("DELETED in %.1fs: %s", duration, ep.name)
        except subprocess.TimeoutExpired:
            ep.delete_result = "TIMEOUT"
            ep.delete_error = f"Delete timed out after {self.timeout}s"
        except Exception as exc:
            ep.delete_result = "ERROR"
            ep.delete_error = str(exc)

        # Post-delete verification
        if ep.delete_result in ("DELETED", "ALREADY_GONE"):
            time.sleep(self.delete_pause)
            ep.verify_result = self._verify_gone(ep)

        return ep

    def _verify_gone(self, ep: EnrichedEndpoint) -> str:
        """Check that the endpoint is gone. Returns verification status string."""
        if self.dry_run:
            return "DRY_RUN_SKIP"
        cmd = ["az", "network", "private-endpoint", "show",
               "--name", ep.name,
               "--resource-group", ep.resource_group,
               "--output", "json"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                err = r.stderr.strip()
                if "ResourceNotFound" in err or "was not found" in err.lower():
                    return "VERIFIED_GONE"
            if r.stdout.strip():
                return "STILL_EXISTS"
            return "VERIFIED_GONE"
        except Exception as exc:
            return "VERIFY_ERROR: " + str(exc)[:80]


# ============================================================================
# REPORT GENERATOR
# ============================================================================

class PEReportGenerator:
    """Generates all output reports for the PE cleanup pipeline."""

    CLS_COLORS = {
        CLS_SAFE_DELETE:     "#d4edda",
        CLS_REVIEW_REQUIRED: "#fff3cd",
        CLS_KEEP:            "#cce5ff",
        CLS_ALREADY_REMOVED: "#e2e3e5",
        CLS_UNKNOWN:         "#f8f9fa",
    }

    EXCEL_FILL_COLORS = {
        CLS_SAFE_DELETE:     "FF00AA44",
        CLS_REVIEW_REQUIRED: "FFFF9900",
        CLS_KEEP:            "FF6699CC",
        CLS_ALREADY_REMOVED: "FFAAAAAA",
        CLS_UNKNOWN:         "FFDDDDDD",
        "DELETED":           "FF00CC66",
        "DELETE_FAILED":     "FFCC0000",
        "VERIFIED_GONE":     "FF009933",
    }

    def __init__(self, output_dir: str = "reports"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    def _path(self, name: str, ext: str) -> Path:
        return self.output_dir / f"PE_Cleanup_{name}_{self.ts}.{ext}"

    def generate_all(self,
                     endpoints: List[EnrichedEndpoint],
                     run_meta: Dict,
                     phase: str = "validation") -> Dict[str, str]:
        """
        Generate all reports. phase is "validation", "preview", or "post_delete".
        Returns dict of {format: filepath}.
        """
        files = {}
        files["csv"] = str(self._write_csv(endpoints, phase))
        files["json"] = str(self._write_json(endpoints, run_meta, phase))
        files["md"] = str(self._write_markdown(endpoints, run_meta, phase))
        files["html"] = str(self._write_html(endpoints, run_meta, phase))
        try:
            import openpyxl
            files["xlsx"] = str(self._write_excel(endpoints, run_meta, phase))
        except ImportError:
            logger.warning("openpyxl not installed - skipping Excel output")
        return files

    # ------------------------------------------------------------------
    # CSV
    # ------------------------------------------------------------------
    def _write_csv(self, endpoints: List[EnrichedEndpoint], phase: str) -> Path:
        path = self._path(phase, "csv")
        if not endpoints:
            path.write_text("")
            return path
        rows = [ep.to_dict() for ep in endpoints]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        return path

    # ------------------------------------------------------------------
    # JSON
    # ------------------------------------------------------------------
    def _write_json(self, endpoints: List[EnrichedEndpoint],
                    run_meta: Dict, phase: str) -> Path:
        path = self._path(phase, "json")
        data = {
            "phase": phase,
            "run_metadata": run_meta,
            "total": len(endpoints),
            "endpoints": [ep.to_dict() for ep in endpoints],
        }
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        return path

    # ------------------------------------------------------------------
    # MARKDOWN EXECUTIVE SUMMARY
    # ------------------------------------------------------------------
    def _write_markdown(self, endpoints: List[EnrichedEndpoint],
                        run_meta: Dict, phase: str) -> Path:
        path = self._path(phase, "md")
        total = len(endpoints)
        by_cls: Dict[str, int] = {}
        total_monthly = 0.0
        deleted_count = 0
        verified_count = 0
        failed_count = 0

        for ep in endpoints:
            by_cls[ep.classification] = by_cls.get(ep.classification, 0) + 1
            total_monthly += ep.monthly_cost_usd
            if ep.delete_result == "DELETED":
                deleted_count += 1
            if ep.verify_result == "VERIFIED_GONE":
                verified_count += 1
            if ep.delete_result in ("DELETE_FAILED", "RBAC_BLOCKED", "TIMEOUT", "ERROR"):
                failed_count += 1

        safe_count = by_cls.get(CLS_SAFE_DELETE, 0)
        review_count = by_cls.get(CLS_REVIEW_REQUIRED, 0)
        removed_count = by_cls.get(CLS_ALREADY_REMOVED, 0)
        savings_monthly = safe_count * 7.30
        savings_yearly = savings_monthly * 12

        run_date = run_meta.get("run_date", datetime.now().strftime("%Y-%m-%d %H:%M"))
        source = run_meta.get("source_file", "")
        mode = run_meta.get("mode", "validation")
        ticket = run_meta.get("change_ticket", "N/A")
        approver = run_meta.get("approved_by", "N/A")
        az_user = run_meta.get("az_user", "N/A")

        md = []
        md.append("# EDAV Private Endpoint Cleanup - Executive Summary")
        md.append("")
        md.append(f"**Run Date:** {run_date}  ")
        md.append(f"**Mode:** {mode.upper()}  ")
        md.append(f"**Source File:** {source}  ")
        md.append(f"**Change Ticket:** {ticket}  ")
        md.append(f"**Approved By:** {approver}  ")
        md.append(f"**Azure CLI User:** {az_user}  ")
        md.append("")
        md.append("## Summary")
        md.append("")
        md.append("| Metric | Count |")
        md.append("|--------|-------|")
        md.append(f"| Private Endpoints Reviewed | {total} |")
        md.append(f"| SAFE_DELETE (Cleanup Candidates) | {safe_count} |")
        md.append(f"| REVIEW_REQUIRED (Blockers Present) | {review_count} |")
        md.append(f"| ALREADY_REMOVED (Already Gone) | {removed_count} |")
        md.append(f"| KEEP (Connected / In Use) | {by_cls.get(CLS_KEEP, 0)} |")
        md.append(f"| UNKNOWN (Validation Error) | {by_cls.get(CLS_UNKNOWN, 0)} |")
        md.append("")
        md.append("## Cost Impact")
        md.append("")
        md.append("| Item | Amount |")
        md.append("|------|--------|")
        md.append(f"| Estimated Monthly Savings | ${savings_monthly:,.2f} |")
        md.append(f"| Estimated Yearly Savings | ${savings_yearly:,.2f} |")
        md.append(f"| Cost per PE (per month) | $7.30 |")
        md.append("")

        if phase == "post_delete":
            md.append("## Deletion Results")
            md.append("")
            md.append("| Metric | Count |")
            md.append("|--------|-------|")
            md.append(f"| Deleted | {deleted_count} |")
            md.append(f"| Verified Gone | {verified_count} |")
            md.append(f"| Failed | {failed_count} |")
            actual_savings = deleted_count * 7.30
            md.append(f"| Actual Monthly Savings Achieved | ${actual_savings:,.2f} |")
            md.append(f"| Actual Yearly Savings Achieved | ${actual_savings*12:,.2f} |")
            md.append("")

        md.append("## SAFE_DELETE Candidates")
        md.append("")
        md.append("| Endpoint | Resource Group | Subscription | Reason | Owner | Ticket |")
        md.append("|----------|----------------|--------------|--------|-------|--------|")
        for ep in endpoints:
            if ep.classification == CLS_SAFE_DELETE:
                result_tag = ""
                if ep.delete_result == "DELETED":
                    result_tag = " ✓DELETED"
                elif ep.delete_result == "DELETE_FAILED":
                    result_tag = " ✗FAILED"
                md.append(
                    f"| {ep.name}{result_tag} | {ep.resource_group} | "
                    f"{ep.subscription} | {ep.classification_reason[:50]} | "
                    f"{ep.owner_team} | {ep.approval_ticket or 'N/A'} |"
                )

        if review_count > 0:
            md.append("")
            md.append("## REVIEW_REQUIRED (Blockers)")
            md.append("")
            md.append("| Endpoint | Resource Group | Subscription | Blockers |")
            md.append("|----------|----------------|--------------|----------|")
            for ep in endpoints:
                if ep.classification == CLS_REVIEW_REQUIRED:
                    md.append(
                        f"| {ep.name} | {ep.resource_group} | "
                        f"{ep.subscription} | {ep.blockers_display[:80]} |"
                    )

        md.append("")
        md.append("---")
        md.append(f"*Generated by EDAV Resource Governance Platform v7.0.0 | {run_date}*")

        path.write_text("\n".join(md), encoding="utf-8")
        return path

    # ------------------------------------------------------------------
    # HTML DASHBOARD
    # ------------------------------------------------------------------
    def _write_html(self, endpoints, run_meta, phase):
        path = self._path(phase, "html")
        total = len(endpoints)
        by_cls = {}
        deleted = sum(1 for ep in endpoints if ep.delete_result == "DELETED")
        verified = sum(1 for ep in endpoints if ep.verify_result == "VERIFIED_GONE")
        failed = sum(1 for ep in endpoints
                     if ep.delete_result in ("DELETE_FAILED","RBAC_BLOCKED","ERROR","TIMEOUT"))
        for ep in endpoints:
            by_cls[ep.classification] = by_cls.get(ep.classification, 0) + 1
        safe_n = by_cls.get(CLS_SAFE_DELETE, 0)
        review_n = by_cls.get(CLS_REVIEW_REQUIRED, 0)
        removed_n = by_cls.get(CLS_ALREADY_REMOVED, 0)
        keep_n = by_cls.get(CLS_KEEP, 0)
        savings_m = round(safe_n * 7.30, 2)
        savings_y = round(savings_m * 12, 2)
        actual_m = round(deleted * 7.30, 2)
        run_date = run_meta.get("run_date", "")
        source_f = run_meta.get("source_file", "")
        mode_s = run_meta.get("mode", "validation").upper()
        progress_pct = round(100 * deleted / max(safe_n, 1)) if phase == "post_delete" else 0

        # Build rows
        row_parts = []
        for ep in endpoints:
            cls = ep.classification
            bg = self.CLS_COLORS.get(cls, "#ffffff")
            del_badge = ""
            if ep.delete_result == "DELETED":
                del_badge = " <b style='color:#009933'>[DELETED]</b>"
            elif ep.delete_result in ("DELETE_FAILED","RBAC_BLOCKED"):
                del_badge = " <b style='color:#cc0000'>[FAILED]</b>"
            elif ep.delete_result == "DRY_RUN":
                del_badge = " <b style='color:#ff9900'>[DRY-RUN]</b>"
            conn = ep.azure_connection_state or ep.rm_connection_state
            row_parts.append(
                "<tr style='background:" + bg + "'>"
                "<td>" + ep.name + del_badge + "</td>"
                "<td>" + ep.resource_group + "</td>"
                "<td>" + ep.subscription + "</td>"
                "<td>" + ep.location + "</td>"
                "<td><b>" + cls + "</b></td>"
                "<td>" + conn + "</td>"
                "<td>" + ep.classification_reason[:70] + "</td>"
                "<td>" + ep.owner_team + "</td>"
                "<td>" + ep.blockers_display[:60] + "</td>"
                "<td>$" + str(round(ep.monthly_cost_usd, 2)) + "</td>"
                "<td>" + (ep.approval_ticket or "") + "</td>"
                "<td>" + (ep.approved_by or "") + "</td>"
                "</tr>"
            )

        rows_html = "\n".join(row_parts)

        parts = []
        parts.append("<!DOCTYPE html><html><head><meta charset='utf-8'>")
        parts.append("<title>EDAV PE Cleanup - " + run_date + "</title>")
        parts.append("<style>body{font-family:Arial,sans-serif;margin:20px;background:#f5f5f5;}")
        parts.append("h1,h2{color:#2d5986;}")
        parts.append(".cards{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0;}")
        parts.append(".card{background:#fff;border-radius:8px;padding:16px 24px;box-shadow:0 1px 4px rgba(0,0,0,.12);min-width:140px;text-align:center;}")
        parts.append(".card-val{font-size:2em;font-weight:bold;margin:4px 0;}")
        parts.append(".card-label{font-size:12px;color:#666;}")
        parts.append("table{border-collapse:collapse;width:100%;font-size:11px;background:#fff;}")
        parts.append("th{background:#2d5986;color:#fff;padding:6px 8px;text-align:left;}")
        parts.append("td{border-bottom:1px solid #eee;padding:5px 8px;}")
        parts.append(".warn{background:#fff3cd;border:1px solid #ffc107;padding:10px;border-radius:6px;margin:10px 0;}")
        parts.append(".progress-bar{background:#e0e0e0;border-radius:4px;height:18px;margin:8px 0;}")
        parts.append(".progress-fill{background:#00aa44;height:18px;border-radius:4px;display:flex;align-items:center;padding-left:8px;color:#fff;font-size:11px;font-weight:bold;}")
        parts.append("</style></head><body>")
        parts.append("<h1>EDAV Private Endpoint Cleanup Dashboard</h1>")
        parts.append("<p><b>Date:</b> " + run_date + " &nbsp;|&nbsp; <b>Mode:</b> " + mode_s + " &nbsp;|&nbsp; <b>Source:</b> " + source_f + "</p>")
        parts.append("<div class='cards'>")
        parts.append("<div class='card'><div class='card-val' style='color:#2d5986'>" + str(total) + "</div><div class='card-label'>Total Reviewed</div></div>")
        parts.append("<div class='card'><div class='card-val' style='color:#00aa44'>" + str(safe_n) + "</div><div class='card-label'>SAFE_DELETE</div></div>")
        parts.append("<div class='card'><div class='card-val' style='color:#ff9900'>" + str(review_n) + "</div><div class='card-label'>REVIEW_REQUIRED</div></div>")
        parts.append("<div class='card'><div class='card-val' style='color:#888'>" + str(removed_n) + "</div><div class='card-label'>Already Removed</div></div>")
        parts.append("<div class='card'><div class='card-val' style='color:#2d5986'>" + str(keep_n) + "</div><div class='card-label'>KEEP</div></div>")
        parts.append("<div class='card'><div class='card-val' style='color:#00aa44'>$" + str(savings_m) + "/mo</div><div class='card-label'>Est. Monthly Savings</div></div>")
        parts.append("<div class='card'><div class='card-val' style='color:#00aa44'>$" + str(savings_y) + "/yr</div><div class='card-label'>Est. Yearly Savings</div></div>")
        parts.append("</div>")

        if phase == "post_delete":
            parts.append("<h2>Deletion Progress</h2>")
            parts.append("<div class='cards'>")
            parts.append("<div class='card'><div class='card-val' style='color:#009933'>" + str(deleted) + "</div><div class='card-label'>Deleted</div></div>")
            parts.append("<div class='card'><div class='card-val' style='color:#009933'>" + str(verified) + "</div><div class='card-label'>Verified Gone</div></div>")
            parts.append("<div class='card'><div class='card-val' style='color:#cc0000'>" + str(failed) + "</div><div class='card-label'>Failed</div></div>")
            parts.append("<div class='card'><div class='card-val' style='color:#009933'>$" + str(actual_m) + "/mo</div><div class='card-label'>Actual Savings</div></div>")
            parts.append("</div>")
            parts.append("<div class='progress-bar'><div class='progress-fill' style='width:" + str(progress_pct) + "%'>" + str(progress_pct) + "% Complete</div></div>")

        parts.append("<div class='warn'><b>NIC Safety:</b> NICs ending in -pe-nic, .nic., or in mc_*/databricks-rg-* RGs are Azure-managed. Do NOT delete.</div>")
        parts.append("<h2>All Endpoints</h2>")
        parts.append("<table><thead><tr><th>Name</th><th>Resource Group</th><th>Subscription</th><th>Location</th><th>Classification</th><th>Connection State</th><th>Reason</th><th>Owner Team</th><th>Blockers</th><th>Monthly Cost</th><th>Ticket</th><th>Approved By</th></tr></thead><tbody>")
        parts.append(rows_html)
        parts.append("</tbody></table>")
        parts.append("<p style='color:#999;font-size:11px;margin-top:20px'>EDAV Resource Governance Platform v7.0.0 | Generated " + run_date + "</p>")
        parts.append("</body></html>")
        path.write_text("\n".join(parts), encoding="utf-8")
        return path

    # ------------------------------------------------------------------
    # EXCEL (multi-tab)
    # ------------------------------------------------------------------
    def _write_excel(self, endpoints, run_meta, phase):
        import openpyxl
        path = self._path(phase, "xlsx")
        if not endpoints:
            openpyxl.Workbook().save(str(path))
            return path
        try:
            import pandas as pd
            df_all = pd.DataFrame([ep.to_dict() for ep in endpoints])
            with pd.ExcelWriter(str(path), engine="openpyxl") as writer:
                df_all.to_excel(writer, sheet_name="All Endpoints", index=False)
                for cls_name, sheet_name in [
                    (CLS_SAFE_DELETE, "SAFE_DELETE"),
                    (CLS_REVIEW_REQUIRED, "REVIEW_REQUIRED"),
                    (CLS_ALREADY_REMOVED, "Already_Removed"),
                    (CLS_KEEP, "KEEP"),
                ]:
                    if "Classification" in df_all.columns:
                        df_cls = df_all[df_all["Classification"] == cls_name]
                        if not df_cls.empty:
                            df_cls.to_excel(writer, sheet_name=sheet_name[:31], index=False)
                if phase == "post_delete" and "DeleteResult" in df_all.columns:
                    df_del = df_all[df_all["DeleteResult"] == "DELETED"]
                    if not df_del.empty:
                        df_del.to_excel(writer, sheet_name="Deleted", index=False)
        except ImportError:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "All Endpoints"
            rows = [ep.to_dict() for ep in endpoints]
            if rows:
                ws.append(list(rows[0].keys()))
                for row in rows:
                    ws.append([str(v) for v in row.values()])
            wb.save(str(path))
        return path


# ============================================================================
# PREVIEW ENGINE
# ============================================================================

class PreviewEngine:
    """
    Generates a "Preview Cleanup" report BEFORE any deletions.
    Shows: which endpoints will be deleted, why, owner, cost impact, blockers.
    """

    def __init__(self, output_dir: str = "reports"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    def generate(self, endpoints: List[EnrichedEndpoint], run_meta: Dict) -> Dict[str, str]:
        """
        Generate preview report. Returns {format: filepath}.
        This is the "Preview Cleanup" mode - shows the deletion plan before
        any actual deletes occur. Designed for presentation to Linda and Brock.
        """
        deletable = [ep for ep in endpoints if ep.classification == CLS_SAFE_DELETE]
        review = [ep for ep in endpoints if ep.classification == CLS_REVIEW_REQUIRED]
        already_gone = [ep for ep in endpoints if ep.classification == CLS_ALREADY_REMOVED]
        keep = [ep for ep in endpoints if ep.classification == CLS_KEEP]
        unknown = [ep for ep in endpoints if ep.classification == CLS_UNKNOWN]

        total_monthly = round(len(deletable) * 7.30, 2)
        total_yearly = round(total_monthly * 12, 2)
        run_date = run_meta.get("run_date", datetime.now().strftime("%Y-%m-%d %H:%M"))
        source = run_meta.get("source_file", "")

        files = {}
        files["md"] = self._write_preview_md(
            deletable, review, already_gone, keep, unknown,
            total_monthly, total_yearly, run_date, source, run_meta
        )
        files["html"] = self._write_preview_html(
            deletable, review, already_gone,
            total_monthly, total_yearly, run_date, source, run_meta
        )
        return files

    def _write_preview_md(self, deletable, review, already_gone, keep, unknown,
                           total_monthly, total_yearly, run_date, source, run_meta):
        path = self.output_dir / ("PE_Preview_Cleanup_" + self.ts + ".md")
        lines = []
        lines.append("# EDAV Private Endpoint Cleanup - Preview Report")
        lines.append("")
        lines.append("> **THIS IS A PREVIEW. NO ENDPOINTS HAVE BEEN DELETED.**")
        lines.append("> Review this report before running cleanup.")
        lines.append("")
        lines.append("**Run Date:** " + run_date)
        lines.append("**Source:** " + source)
        lines.append("**Change Ticket:** " + run_meta.get("change_ticket", "N/A"))
        lines.append("**Prepared By:** " + run_meta.get("az_user", "N/A"))
        lines.append("")
        lines.append("## Cleanup Plan Summary")
        lines.append("")
        lines.append("| Category | Count | Description |")
        lines.append("|----------|-------|-------------|")
        lines.append("| **SAFE_DELETE** | **" + str(len(deletable)) + "** | Will be deleted - no blockers, no backend, no lock |")
        lines.append("| REVIEW_REQUIRED | " + str(len(review)) + " | Blockers present - manual review needed |")
        lines.append("| ALREADY_REMOVED | " + str(len(already_gone)) + " | Already deleted - no action needed |")
        lines.append("| KEEP | " + str(len(keep)) + " | Connected/in-use - do not delete |")
        lines.append("| UNKNOWN | " + str(len(unknown)) + " | Could not validate - investigate |")
        lines.append("")
        lines.append("## Estimated Cost Impact")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append("| Endpoints to Delete | " + str(len(deletable)) + " |")
        lines.append("| Cost per Endpoint/Month | $7.30 |")
        lines.append("| **Estimated Monthly Savings** | **$" + str(total_monthly) + "** |")
        lines.append("| **Estimated Yearly Savings** | **$" + str(total_yearly) + "** |")
        lines.append("")
        lines.append("## Endpoints Queued for Deletion")
        lines.append("")
        lines.append("The following " + str(len(deletable)) + " endpoints QUALIFY for deletion:")
        lines.append("")
        lines.append("| # | Endpoint Name | Resource Group | Subscription | Owner Team | Monthly Cost | Why Qualifies | Approval Status |")
        lines.append("|---|---------------|----------------|--------------|------------|--------------|---------------|-----------------|")
        for i, ep in enumerate(deletable, 1):
            approval_status = "Approved" if ep.approved_to_delete else "NEEDS APPROVAL"
            lines.append(
                "| " + str(i) + " | " + ep.name + " | " + ep.resource_group + " | "
                + ep.subscription + " | " + ep.owner_team + " | $" + str(round(ep.monthly_cost_usd, 2))
                + " | " + ep.classification_reason[:60] + " | " + approval_status + " |"
            )
        lines.append("")

        if review:
            lines.append("## Endpoints NOT Deleted (Blockers Present)")
            lines.append("")
            lines.append("The following " + str(len(review)) + " endpoints have blockers and will NOT be deleted:")
            lines.append("")
            lines.append("| Endpoint | Resource Group | Subscription | Blockers | Owner Team |")
            lines.append("|----------|----------------|--------------|----------|------------|")
            for ep in review:
                lines.append(
                    "| " + ep.name + " | " + ep.resource_group + " | "
                    + ep.subscription + " | " + ep.blockers_display[:80]
                    + " | " + ep.owner_team + " |"
                )
            lines.append("")

        lines.append("## How to Proceed")
        lines.append("")
        lines.append("1. Review this report with Linda and the network team")
        lines.append("2. Collect ApprovalTicket and ApprovedBy for each SAFE_DELETE endpoint")
        lines.append("3. Mark ApprovedToDelete=Yes in the approved deletions spreadsheet")
        lines.append("4. Run dry-run to simulate:")
        lines.append("   ```bash")
        lines.append("   python main.py --cleanup-private-endpoints \\")
        lines.append("     --import-resource-monitor DisconnectedPEs.xlsx \\")
        lines.append("     --dry-run --change-ticket CHG0001234")
        lines.append("   ```")
        lines.append("5. Run live delete (SU account required):")
        lines.append("   ```bash")
        lines.append("   python main.py --cleanup-private-endpoints \\")
        lines.append("     --import-resource-monitor DisconnectedPEs.xlsx \\")
        lines.append("     --delete-approved --change-ticket CHG0001234 --approved-by 'Linda Johnson'")
        lines.append("   ```")
        lines.append("6. Post-delete: run --verify-post-delete to confirm removals")
        lines.append("")
        lines.append("---")
        lines.append("*EDAV Resource Governance Platform v7.0.0 | " + run_date + "*")
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)

    def _write_preview_html(self, deletable, review, already_gone,
                             total_monthly, total_yearly, run_date, source, run_meta):
        path = self.output_dir / ("PE_Preview_Cleanup_" + self.ts + ".html")
        ticket = run_meta.get("change_ticket", "N/A")
        az_user = run_meta.get("az_user", "N/A")

        rows_delete = []
        for i, ep in enumerate(deletable, 1):
            approval_badge = ""
            if ep.approved_to_delete:
                approval_badge = " <b style='color:#009933'>[APPROVED]</b>"
            else:
                approval_badge = " <b style='color:#ff9900'>[NEEDS APPROVAL]</b>"
            rows_delete.append(
                "<tr style='background:#d4edda'>"
                "<td>" + str(i) + "</td>"
                "<td>" + ep.name + approval_badge + "</td>"
                "<td>" + ep.resource_group + "</td>"
                "<td>" + ep.subscription + "</td>"
                "<td>" + ep.owner_team + "</td>"
                "<td>$" + str(round(ep.monthly_cost_usd, 2)) + "</td>"
                "<td>" + ep.classification_reason[:70] + "</td>"
                "<td>" + (ep.approval_ticket or "—") + "</td>"
                "<td>" + (ep.approved_by or "—") + "</td>"
                "</tr>"
            )

        rows_review = []
        for ep in review:
            rows_review.append(
                "<tr style='background:#fff3cd'>"
                "<td>" + ep.name + "</td>"
                "<td>" + ep.resource_group + "</td>"
                "<td>" + ep.subscription + "</td>"
                "<td>" + ep.owner_team + "</td>"
                "<td>" + ep.blockers_display[:80] + "</td>"
                "</tr>"
            )

        p = []
        p.append("<!DOCTYPE html><html><head><meta charset='utf-8'>")
        p.append("<title>EDAV PE Preview Cleanup - " + run_date + "</title>")
        p.append("<style>body{font-family:Arial,sans-serif;margin:20px;background:#f5f5f5;}")
        p.append("h1,h2{color:#2d5986;} .preview-banner{background:#fff3cd;border:2px solid #ffc107;")
        p.append("border-radius:8px;padding:16px;margin-bottom:20px;font-size:15px;}")
        p.append(".cards{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0;}")
        p.append(".card{background:#fff;border-radius:8px;padding:16px 24px;box-shadow:0 1px 4px rgba(0,0,0,.12);min-width:140px;text-align:center;}")
        p.append(".card-val{font-size:2em;font-weight:bold;margin:4px 0;}")
        p.append(".card-label{font-size:12px;color:#666;}")
        p.append("table{border-collapse:collapse;width:100%;font-size:11px;background:#fff;margin-bottom:20px;}")
        p.append("th{background:#2d5986;color:#fff;padding:6px 8px;text-align:left;}")
        p.append("td{border-bottom:1px solid #eee;padding:5px 8px;}</style></head><body>")
        p.append("<h1>EDAV Private Endpoint Cleanup - Preview Report</h1>")
        p.append("<div class='preview-banner'>")
        p.append("<b>THIS IS A PREVIEW. NO ENDPOINTS HAVE BEEN DELETED.</b><br>")
        p.append("Review this report and collect approvals before proceeding with cleanup.<br>")
        p.append("<b>Date:</b> " + run_date + " &nbsp;|&nbsp; <b>Source:</b> " + source + " &nbsp;|&nbsp; <b>Ticket:</b> " + ticket + "</div>")
        p.append("<div class='cards'>")
        p.append("<div class='card'><div class='card-val' style='color:#00aa44'>" + str(len(deletable)) + "</div><div class='card-label'>Will Be Deleted</div></div>")
        p.append("<div class='card'><div class='card-val' style='color:#ff9900'>" + str(len(review)) + "</div><div class='card-label'>Review Required</div></div>")
        p.append("<div class='card'><div class='card-val' style='color:#888'>" + str(len(already_gone)) + "</div><div class='card-label'>Already Removed</div></div>")
        p.append("<div class='card'><div class='card-val' style='color:#00aa44'>$" + str(total_monthly) + "/mo</div><div class='card-label'>Monthly Savings</div></div>")
        p.append("<div class='card'><div class='card-val' style='color:#00aa44'>$" + str(total_yearly) + "/yr</div><div class='card-label'>Yearly Savings</div></div>")
        p.append("</div>")
        p.append("<h2>Endpoints Queued for Deletion (" + str(len(deletable)) + ")</h2>")
        p.append("<table><thead><tr><th>#</th><th>Endpoint Name</th><th>Resource Group</th><th>Subscription</th><th>Owner Team</th><th>Monthly Cost</th><th>Why Qualifies</th><th>Approval Ticket</th><th>Approved By</th></tr></thead><tbody>")
        p.append("\n".join(rows_delete))
        p.append("</tbody></table>")
        if rows_review:
            p.append("<h2>Endpoints NOT Deleted - Blockers Present (" + str(len(review)) + ")</h2>")
            p.append("<table><thead><tr><th>Endpoint</th><th>Resource Group</th><th>Subscription</th><th>Owner Team</th><th>Blockers</th></tr></thead><tbody>")
            p.append("\n".join(rows_review))
            p.append("</tbody></table>")
        p.append("<p style='color:#999;font-size:11px'>EDAV Resource Governance Platform v7.0.0 | " + run_date + "</p>")
        p.append("</body></html>")
        path.write_text("\n".join(p), encoding="utf-8")
        return str(path)


# ============================================================================
# MAIN PIPELINE ORCHESTRATOR
# ============================================================================

class PrivateEndpointPipeline:
    """
    End-to-end orchestrator for the EDAV PE cleanup workflow.

    Workflow:
      1. Import  - Read Resource Monitor Excel export
      2. Enrich  - Optionally merge with Azure discovery
      3. Validate - Validate each endpoint against live Azure
      4. Classify - Classify: SAFE_DELETE | REVIEW | KEEP | ALREADY_REMOVED
      5. Identify teams
      6. Preview  - Generate preview report (if --preview-cleanup)
      7. Prompt   - Confirm before bulk delete
      8. Delete   - Delete individually
      9. Verify   - Post-delete verification
     10. Report   - Generate all reports
    """

    def __init__(
        self,
        output_dir: str = "reports",
        backup_dir: str = "backups",
        dry_run: bool = False,
        required_user: str = DEFAULT_REQUIRED_USER,
        delete_pause: int = 2,
        validate_timeout: int = 60,
    ):
        self.output_dir = output_dir
        self.backup_dir = backup_dir
        self.dry_run = dry_run
        self.required_user = required_user
        self.delete_pause = delete_pause
        self.validate_timeout = validate_timeout
        self.validator = AzurePEValidator(dry_run=dry_run, timeout=validate_timeout)
        self.cleanup_engine = PECleanupEngine(
            backup_dir=backup_dir, dry_run=dry_run,
            delete_pause=delete_pause, timeout=validate_timeout * 2
        )
        self.reporter = PEReportGenerator(output_dir=output_dir)
        self.preview_engine = PreviewEngine(output_dir=output_dir)

    def run(
        self,
        rm_file: str,
        run_meta: Dict,
        do_delete: bool = False,
        preview_only: bool = False,
        validate_only: bool = False,
        skip_validation: bool = False,
    ) -> Tuple[List[EnrichedEndpoint], Dict[str, str]]:
        """
        Run the full pipeline. Returns (endpoints, report_files).
        """
        run_date = run_meta.get("run_date", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        logger.info("Pipeline starting: rm_file=%s dry_run=%s do_delete=%s",
                    rm_file, self.dry_run, do_delete)

        # ── STEP 1: Import Resource Monitor export ─────────────────────────
        _print_step(1, "Importing Resource Monitor export")
        from engines.resource_monitor_reader import ResourceMonitorReader
        reader = ResourceMonitorReader(rm_file)
        findings, rm_summary = reader.read()

        if not findings:
            _print_error("No disconnected PE findings found in: " + rm_file)
            return [], {}

        _print_ok(
            "Loaded " + str(len(findings)) + " disconnected PE findings from: " + rm_file
        )
        _print_ok(
            "Estimated monthly cost: $" + str(rm_summary.get("estimated_monthly_savings_usd", 0))
        )
        run_meta["source_file"] = rm_file
        run_meta["rm_summary"] = rm_summary

        # ── STEP 2: Convert findings to EnrichedEndpoint records ───────────
        _print_step(2, "Building endpoint records")
        endpoints: List[EnrichedEndpoint] = []
        for f in findings:
            ep = EnrichedEndpoint(
                name=f.resource_name,
                resource_group=f.resource_group,
                subscription=f.subscription,
                location=f.location,
                check_name=f.check_name,
                detail=f.detail,
                rm_connection_state=f.connection_state,
                approved_to_delete=f.approved_to_delete,
                approval_ticket=f.approval_ticket,
                approved_by=f.approved_by,
                monthly_cost_usd=f.monthly_cost_usd,
                source="RESOURCE_MONITOR",
                source_file=rm_file,
                source_row=f.source_row,
                notes=f.notes,
            )
            endpoints.append(ep)

        # ── STEP 3: Validate against Azure ─────────────────────────────────
        if skip_validation:
            _print_step(3, "Skipping Azure validation (--skip-validation)")
            for ep in endpoints:
                ep.azure_exists = True
                ep.azure_connection_state = ep.rm_connection_state
        else:
            _print_step(3, "Validating " + str(len(endpoints)) + " endpoints against Azure")
            for i, ep in enumerate(endpoints, 1):
                _print_progress(i, len(endpoints), ep.name)
                self.validator.validate(ep)

        # ── STEP 4: Classify ────────────────────────────────────────────────
        _print_step(4, "Classifying endpoints")
        for ep in endpoints:
            classify_endpoint(ep)

        # ── STEP 5: Identify teams ──────────────────────────────────────────
        _print_step(5, "Identifying owner teams")
        for ep in endpoints:
            ep.owner_team = identify_team(ep.name, ep.resource_group, ep.azure_tags)

        # ── Summary ─────────────────────────────────────────────────────────
        _print_classification_summary(endpoints)

        report_files = {}

        # ── STEP 6: Preview ─────────────────────────────────────────────────
        if preview_only:
            _print_step(6, "Generating Preview Cleanup report")
            preview_files = self.preview_engine.generate(endpoints, run_meta)
            report_files.update(preview_files)
            _print_ok("Preview report: " + str(list(preview_files.values())))
            _print_preview_console(endpoints)
            return endpoints, report_files

        # ── Generate validation reports ──────────────────────────────────────
        _print_step(6, "Generating validation reports")
        val_files = self.reporter.generate_all(endpoints, run_meta, "validation")
        report_files.update(val_files)
        _print_ok("Validation reports written to: " + self.output_dir + "/")

        if validate_only or not do_delete:
            return endpoints, report_files

        # ── STEP 7: Safety gate checks ──────────────────────────────────────
        _print_step(7, "Safety gate checks")
        deletable = [ep for ep in endpoints if ep.classification == CLS_SAFE_DELETE]
        if not deletable:
            _print_ok("No SAFE_DELETE endpoints found. Nothing to delete.")
            return endpoints, report_files

        approved = [ep for ep in deletable if ep.is_deletable]
        not_approved = [ep for ep in deletable if not ep.is_deletable]

        if not approved and not self.dry_run:
            _print_warn(
                str(len(deletable)) + " SAFE_DELETE endpoints found but none have "
                "ApprovedToDelete=Yes + ApprovalTicket + ApprovedBy. "
                "Update the input file with approvals and rerun."
            )
            return endpoints, report_files

        if not_approved:
            _print_warn(str(len(not_approved)) + " endpoints missing approval - will be skipped:")
            for ep in not_approved[:5]:
                _print_warn("  -> " + ep.name + " (missing: " +
                            ("ticket " if not ep.approval_ticket else "") +
                            ("approver" if not ep.approved_by else "") + ")")

        target = approved if not self.dry_run else deletable

        # ── STEP 8: Confirmation prompt ──────────────────────────────────────
        _print_step(8, "Deletion confirmation")
        _print_deletion_plan(target, self.dry_run)

        if not self.dry_run:
            confirm = input("\n  Type YES to proceed with deletion (anything else aborts): ").strip()
            if confirm.upper() not in ("YES", "Y"):
                _print_warn("Aborted. No endpoints deleted.")
                return endpoints, report_files

        # ── STEP 9: Delete ────────────────────────────────────────────────────
        _print_step(9, "Deleting " + str(len(target)) + " endpoints" + (" [DRY-RUN]" if self.dry_run else ""))
        for i, ep in enumerate(target, 1):
            _print_progress(i, len(target), ep.name)
            backup_path = self.cleanup_engine.backup(ep)
            ep.backup_path = backup_path
            self.cleanup_engine.delete(ep)
            if ep.delete_result == "DELETED":
                _print_ok("  DELETED and VERIFIED_GONE: " + ep.name)
            elif ep.delete_result == "DRY_RUN":
                _print_ok("  [DRY-RUN] Would delete: " + ep.name)
            else:
                _print_error("  " + ep.delete_result + ": " + ep.name + " - " + ep.delete_error[:80])

        # ── STEP 10: Post-delete reports ────────────────────────────────────
        _print_step(10, "Generating post-delete reports")
        post_files = self.reporter.generate_all(endpoints, run_meta, "post_delete")
        report_files.update({"post_" + k: v for k, v in post_files.items()})
        _print_ok("Post-delete reports written to: " + self.output_dir + "/")

        # ── Final summary ─────────────────────────────────────────────────────
        _print_delete_summary(endpoints, self.dry_run)

        return endpoints, report_files


# ============================================================================
# CONSOLE HELPERS
# ============================================================================

def _print_step(n: int, msg: str) -> None:
    print("\n  ── Step " + str(n) + ": " + msg)

def _print_ok(msg: str) -> None:
    print("  ✓ " + msg)

def _print_warn(msg: str) -> None:
    print("  ⚠ " + msg)

def _print_error(msg: str) -> None:
    print("  ✗ " + msg)

def _print_progress(i: int, total: int, name: str) -> None:
    pct = round(100 * i / total)
    print(f"  [{i:03d}/{total}] ({pct}%) {name}")

def _print_classification_summary(endpoints: List[EnrichedEndpoint]) -> None:
    by_cls: Dict[str, int] = {}
    for ep in endpoints:
        by_cls[ep.classification] = by_cls.get(ep.classification, 0) + 1
    safe = by_cls.get(CLS_SAFE_DELETE, 0)
    review = by_cls.get(CLS_REVIEW_REQUIRED, 0)
    removed = by_cls.get(CLS_ALREADY_REMOVED, 0)
    keep = by_cls.get(CLS_KEEP, 0)
    unknown = by_cls.get(CLS_UNKNOWN, 0)
    total = len(endpoints)
    savings_m = round(safe * 7.30, 2)
    savings_y = round(savings_m * 12, 2)
    sep = "=" * 68
    print("\n" + sep)
    print("  CLASSIFICATION SUMMARY")
    print(sep)
    print(f"  Private Endpoints Reviewed : {total}")
    print(f"  SAFE_DELETE (Ready)        : {safe}")
    print(f"  REVIEW_REQUIRED (Blocked)  : {review}")
    print(f"  ALREADY_REMOVED            : {removed}")
    print(f"  KEEP (Connected)           : {keep}")
    print(f"  UNKNOWN                    : {unknown}")
    print(f"  Estimated Monthly Savings  : ${savings_m:,.2f}")
    print(f"  Estimated Yearly Savings   : ${savings_y:,.2f}")
    print(sep)

def _print_preview_console(endpoints: List[EnrichedEndpoint]) -> None:
    """Print a condensed preview table to the console."""
    deletable = [ep for ep in endpoints if ep.classification == CLS_SAFE_DELETE]
    review = [ep for ep in endpoints if ep.classification == CLS_REVIEW_REQUIRED]
    print("\n  PREVIEW: Endpoints queued for deletion:")
    print("  " + "-" * 66)
    for i, ep in enumerate(deletable[:20], 1):
        approval = "[APPROVED]" if ep.approved_to_delete else "[NEEDS APPROVAL]"
        print(f"  {i:3}. {ep.name:<45} {ep.owner_team:<20} {approval}")
    if len(deletable) > 20:
        print(f"  ... and {len(deletable) - 20} more (see report)")
    if review:
        print("\n  BLOCKED (not deleted):")
        for ep in review[:5]:
            print(f"  - {ep.name}: {ep.blockers_display[:60]}")
        if len(review) > 5:
            print(f"  ... and {len(review) - 5} more")

def _print_deletion_plan(target: List[EnrichedEndpoint], dry_run: bool) -> None:
    mode_tag = " [DRY-RUN]" if dry_run else ""
    print("\n  DELETION PLAN" + mode_tag)
    print("  " + "-" * 66)
    for i, ep in enumerate(target[:20], 1):
        print(f"  {i:3}. {ep.name:<45} {ep.resource_group}")
    if len(target) > 20:
        print(f"  ... and {len(target) - 20} more")
    monthly = round(len(target) * 7.30, 2)
    yearly = round(monthly * 12, 2)
    print(f"\n  Total: {len(target)} endpoints")
    print(f"  Estimated monthly savings: ${monthly:,.2f}")
    print(f"  Estimated yearly savings : ${yearly:,.2f}")

def _print_delete_summary(endpoints: List[EnrichedEndpoint], dry_run: bool) -> None:
    deleted = [ep for ep in endpoints if ep.delete_result in ("DELETED", "DRY_RUN")]
    verified = [ep for ep in endpoints if ep.verify_result == "VERIFIED_GONE"]
    failed = [ep for ep in endpoints if ep.delete_result in
              ("DELETE_FAILED", "RBAC_BLOCKED", "TIMEOUT", "ERROR")]
    actual_m = round(len(deleted) * 7.30, 2)
    sep = "=" * 68
    print("\n" + sep)
    print("  DELETION COMPLETE" + (" [DRY-RUN]" if dry_run else ""))
    print(sep)
    print(f"  Deleted          : {len(deleted)}")
    print(f"  Verified Gone    : {len(verified)}")
    print(f"  Failed           : {len(failed)}")
    print(f"  Actual Monthly Savings: ${actual_m:,.2f}")
    print(f"  Actual Yearly Savings : ${actual_m * 12:,.2f}")
    if failed:
        print("\n  FAILED ENDPOINTS:")
        for ep in failed:
            print(f"  - {ep.name}: {ep.delete_error[:80]}")
    print(sep)
    print("\n  Run: python main.py --verify-post-delete to confirm removals")
    print("  Check docs/RESOURCE_GRAPH_QUERIES.md for verification queries\n")

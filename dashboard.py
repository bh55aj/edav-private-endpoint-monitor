dashboard.py#!/usr/bin/env python3
"""
dashboard.py — EDAV Resource Monitor Console Dashboard.

Renders a rich, color-coded terminal dashboard summarizing scan results from
all service monitors. Displays:
  - Resources scanned (total, by service)
  - Cleanup candidates count and estimated savings
  - Owner review required count
  - Terraform drift findings
  - Resource counts by service and classification

Run directly:
    python dashboard.py --report-dir reports/2026-06/
Or call from main.py after a scan completes.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init(autoreset=True)
    HAS_COLOR = True
except ImportError:
    HAS_COLOR = False

    class _NoColor:
        def __getattr__(self, name): return ""
    Fore = Style = _NoColor()


# ---------------------------------------------------------------------------
# Dashboard data model
# ---------------------------------------------------------------------------

class DashboardData:
    """Aggregated data for the dashboard view."""

    def __init__(self):
        self.scan_timestamp: str = datetime.now(timezone.utc).isoformat()
        self.subscriptions_scanned: List[str] = []
        self.total_resources: int = 0
        self.by_service: Dict[str, Dict[str, int]] = {}  # service -> {SAFE_DELETE: n, ...}
        self.cleanup_candidates: int = 0
        self.review_required: int = 0
        self.do_not_delete: int = 0
        self.unknown: int = 0
        self.estimated_monthly_savings: float = 0.0
        self.terraform_not_in_tf: int = 0
        self.terraform_not_in_azure: int = 0
        self.owner_review_required: int = 0
        self.unowned_resources: int = 0
        self.errors: List[str] = []

    def add_monitor_result(self, result: Any) -> None:
        """Incorporate a MonitorResult into the dashboard."""
        svc = getattr(result, "service_type", "Unknown")
        findings = getattr(result, "findings", [])
        subs = getattr(result, "subscriptions_scanned", [])

        for sub in subs:
            if sub not in self.subscriptions_scanned:
                self.subscriptions_scanned.append(sub)

        if svc not in self.by_service:
            self.by_service[svc] = {
                "total": 0, "SAFE_DELETE": 0, "REVIEW_REQUIRED": 0,
                "DO_NOT_DELETE": 0, "UNKNOWN": 0
            }

        for f in findings:
            cls = getattr(f, "classification", "UNKNOWN")
            self.by_service[svc]["total"] = self.by_service[svc].get("total", 0) + 1
            self.by_service[svc][cls] = self.by_service[svc].get(cls, 0) + 1
            self.total_resources += 1

            if cls == "SAFE_DELETE":
                self.cleanup_candidates += 1
                self.estimated_monthly_savings += getattr(f, "estimated_monthly_cost_usd", 0.0) or 0.0
            elif cls == "REVIEW_REQUIRED":
                self.review_required += 1
            elif cls == "DO_NOT_DELETE":
                self.do_not_delete += 1
            else:
                self.unknown += 1

            if not getattr(f, "owner", ""):
                self.unowned_resources += 1

        self.errors.extend(getattr(result, "errors", []))

    def add_ownership_report(self, report: Any) -> None:
        self.owner_review_required = getattr(report, "unowned_resources", 0)

    def add_terraform_report(self, report: Any) -> None:
        self.terraform_not_in_tf = len(getattr(report, "not_in_terraform", []))
        self.terraform_not_in_azure = len(getattr(report, "not_in_azure", []))

    def add_cost_report(self, report: Any) -> None:
        self.estimated_monthly_savings = getattr(
            report, "total_estimated_monthly_savings_usd",
            self.estimated_monthly_savings
        )


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

class Dashboard:
    """Console dashboard renderer."""

    WIDTH = 72

    def __init__(self, data: DashboardData):
        self.data = data

    def render(self) -> None:
        self._print_header()
        self._print_top_line_metrics()
        self._print_by_service_table()
        self._print_terraform_section()
        self._print_ownership_section()
        self._print_errors()
        self._print_footer()

    def _print_header(self) -> None:
        w = self.WIDTH
        print()
        print(Fore.CYAN + "=" * w)
        print(Fore.CYAN + " EDAV Azure Resource Governance Platform".center(w))
        print(Fore.CYAN + " Scan Dashboard".center(w))
        print(Fore.CYAN + f" {self.data.scan_timestamp}".center(w))
        print(Fore.CYAN + "=" * w)
        print()

    def _print_top_line_metrics(self) -> None:
        d = self.data
        print(Fore.WHITE + Style.BRIGHT + "  TOP-LINE METRICS")
        print(Fore.WHITE + "  " + "-" * (self.WIDTH - 4))
        print(f"  {'Resources scanned:':<35} {Fore.WHITE}{d.total_resources:>10,}")
        print(f"  {'Subscriptions scanned:':<35} {Fore.WHITE}{len(d.subscriptions_scanned):>10}")

        color_sd = Fore.RED if d.cleanup_candidates > 0 else Fore.GREEN
        print(f"  {'Cleanup candidates (SAFE_DELETE):':<35} {color_sd}{d.cleanup_candidates:>10,}")

        color_rr = Fore.YELLOW if d.review_required > 0 else Fore.GREEN
        print(f"  {'Owner review required:':<35} {color_rr}{d.review_required:>10,}")

        print(f"  {'Protected (DO_NOT_DELETE):':<35} {Fore.GREEN}{d.do_not_delete:>10,}")
        print(f"  {'Unknown state:':<35} {Fore.YELLOW}{d.unknown:>10,}")

        savings_color = Fore.RED if d.estimated_monthly_savings > 1000 else (
            Fore.YELLOW if d.estimated_monthly_savings > 100 else Fore.GREEN
        )
        print(
            f"  {'Est. monthly savings:':<35} "
            f"{savings_color}${d.estimated_monthly_savings:>9,.2f}"
        )
        annual = d.estimated_monthly_savings * 12
        print(f"  {'Est. annual savings:':<35} {savings_color}${annual:>9,.2f}")
        print()

    def _print_by_service_table(self) -> None:
        d = self.data
        if not d.by_service:
            return
        print(Fore.WHITE + Style.BRIGHT + "  RESOURCES BY SERVICE")
        print(Fore.WHITE + "  " + "-" * (self.WIDTH - 4))
        header = f"  {'Service':<30} {'Total':>7} {'Safe Del':>9} {'Review':>8} {'Protected':>10}"
        print(Fore.CYAN + header)
        for svc, counts in sorted(d.by_service.items(), key=lambda x: -x[1].get("total", 0)):
            total = counts.get("total", 0)
            sd = counts.get("SAFE_DELETE", 0)
            rr = counts.get("REVIEW_REQUIRED", 0)
            dnd = counts.get("DO_NOT_DELETE", 0)
            sd_str = f"{Fore.RED}{sd:>9}" if sd > 0 else f"{Fore.GREEN}{sd:>9}"
            rr_str = f"{Fore.YELLOW}{rr:>8}" if rr > 0 else f"{Fore.GREEN}{rr:>8}"
            print(
                f"  {Fore.WHITE}{svc:<30} {total:>7} "
                f"{sd_str}{Fore.WHITE}"
                f"{rr_str}{Fore.WHITE} {dnd:>10}"
            )
        print()

    def _print_terraform_section(self) -> None:
        d = self.data
        print(Fore.WHITE + Style.BRIGHT + "  TERRAFORM DRIFT")
        print(Fore.WHITE + "  " + "-" * (self.WIDTH - 4))
        nit_color = Fore.YELLOW if d.terraform_not_in_tf > 0 else Fore.GREEN
        nia_color = Fore.YELLOW if d.terraform_not_in_azure > 0 else Fore.GREEN
        print(f"  {'Not in Terraform (manual deployments):':<35} {nit_color}{d.terraform_not_in_tf:>10,}")
        print(f"  {'In Terraform but not in Azure:':<35} {nia_color}{d.terraform_not_in_azure:>10,}")
        print()

    def _print_ownership_section(self) -> None:
        d = self.data
        print(Fore.WHITE + Style.BRIGHT + "  OWNERSHIP STATUS")
        print(Fore.WHITE + "  " + "-" * (self.WIDTH - 4))
        unowned_color = Fore.YELLOW if d.unowned_resources > 0 else Fore.GREEN
        print(f"  {'Unowned resources:':<35} {unowned_color}{d.unowned_resources:>10,}")
        print(f"  {'Owner review required:':<35} {unowned_color}{d.owner_review_required:>10,}")
        print()

    def _print_errors(self) -> None:
        d = self.data
        if not d.errors:
            return
        print(Fore.RED + Style.BRIGHT + "  SCAN ERRORS")
        print(Fore.RED + "  " + "-" * (self.WIDTH - 4))
        for err in d.errors[:10]:
            print(f"  {Fore.RED}! {err}")
        if len(d.errors) > 10:
            print(f"  {Fore.RED}... and {len(d.errors) - 10} more errors")
        print()

    def _print_footer(self) -> None:
        print(Fore.CYAN + "=" * self.WIDTH)
        print(
            Fore.WHITE + "  Nothing is deleted without validation, approval, "
            "a ticket, and a CONFIRM."
        )
        print(Fore.CYAN + "=" * self.WIDTH)
        print()


def render_dashboard(monitor_results: List[Any] = None,
                     ownership_report: Any = None,
                     terraform_report: Any = None,
                     cost_report: Any = None) -> DashboardData:
    """
    Convenience function: build DashboardData from results and render it.
    Returns the DashboardData for further use.
    """
    data = DashboardData()
    if monitor_results:
        for result in monitor_results:
            data.add_monitor_result(result)
    if ownership_report:
        data.add_ownership_report(ownership_report)
    if terraform_report:
        data.add_terraform_report(terraform_report)
    if cost_report:
        data.add_cost_report(cost_report)
    Dashboard(data).render()
    return data


if __name__ == "__main__":
    # Minimal standalone demo
    import argparse
    parser = argparse.ArgumentParser(description="EDAV Resource Monitor Dashboard")
    parser.add_argument("--report-dir", default="reports", help="Path to reports directory")
    args = parser.parse_args()

    data = DashboardData()
    data.scan_timestamp = datetime.now(timezone.utc).isoformat()
    Dashboard(data).render()
    print("Dashboard rendered. Run a full scan via main.py to populate with real data.")

"""
Base monitor classes for EDAV governance scanning.
"""

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional


@dataclass
class GovernanceFinding:
    name: str
    resource_group: str
    subscription_id: str
    location: str = ""
    resource_type: str = ""
    classification: str = "REVIEW_REQUIRED"
    reason: str = ""
    owner_team: str = ""
    terraform_managed: str = "Unknown"
    azure_managed: bool = False
    safe_delete_eligible: bool = False
    approval_required: bool = True
    recommended_action: str = "Review with owner"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class BaseMonitor:
    def __init__(self, subscriptions: Optional[List[str]] = None, config: Optional[Dict[str, Any]] = None):
        self.subscriptions = subscriptions or []
        self.config = config or {}
        self.findings: List[GovernanceFinding] = []

    def scan(self) -> List[GovernanceFinding]:
        raise NotImplementedError("Monitor must implement scan()")

    def add_finding(self, finding: GovernanceFinding) -> None:
        self.findings.append(finding)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "monitor": self.__class__.__name__,
            "subscriptions": self.subscriptions,
            "findings": [finding.to_dict() for finding in self.findings],
        }

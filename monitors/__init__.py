"""
EDAV Resource Monitor — monitors package
Each module in this package is a self-contained monitor for one Azure service.
"""

from .base_monitor import BaseMonitor

__all__ = ["BaseMonitor"]

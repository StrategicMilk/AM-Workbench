"""Resource monitor compatibility exports."""

from __future__ import annotations

from vetinari.system.resource_monitor import (
    DiskStatus,
    DiskThreshold,
    ResourceMonitor,
    check_disk_space,
    get_resource_monitor,
)

__all__ = ["DiskStatus", "DiskThreshold", "ResourceMonitor", "check_disk_space", "get_resource_monitor"]

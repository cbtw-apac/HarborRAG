from __future__ import annotations

from datetime import timedelta

DISCOVERY_ACTIVITY_TIMEOUT = timedelta(minutes=30)
DISCOVERY_ACTIVITY_HEARTBEAT_TIMEOUT = timedelta(minutes=2)
DISCOVERY_ACTIVITY_SCHEDULE_TO_START_TIMEOUT = timedelta(minutes=2)
CONTROL_ACTIVITY_TIMEOUT = timedelta(minutes=2)
FINALIZE_ACTIVITY_TIMEOUT = timedelta(minutes=15)
CLEANUP_ACTIVITY_TIMEOUT = timedelta(minutes=30)

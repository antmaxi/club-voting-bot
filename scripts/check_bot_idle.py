#!/usr/bin/env python3
"""
check_bot_idle.py — report whether a bookclub_bot instance has seen any
deployment-relevant activity recently, by reading its JSON activity file.

The bot writes this non-executable sidecar at most once every 30 seconds.

Usage:
    check_bot_idle.py <activity_json_file> <threshold_minutes>
    check_bot_idle.py --format-duration <threshold_minutes>

Exit codes:
    0  IDLE    — startup and ordinary/other-admin activity older than threshold + 30s
    1  ACTIVE  — non-admin activity within the threshold
    2  UNKNOWN — file missing arguments, unreadable, or unexpected contents

Prints one line describing the activity status or the formatted duration.
"""

import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path


def _format_duration(minutes: float) -> str:
    whole_minutes = int(minutes)
    if whole_minutes < 100:
        return f"{whole_minutes}m"
    hours = whole_minutes // 60
    if hours < 100:
        return f"{hours}h"
    return f"{hours // 24}d"


def _activity_utc(raw: object) -> datetime:
    """Parse and normalize an ISO-8601 activity timestamp."""
    if not isinstance(raw, str):
        raise ValueError("last_non_admin_activity must be a string")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        # Accept early JSON sidecars that may have used naive UTC.
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def main() -> int:
    if len(sys.argv) != 3:
        print(
            f"usage: {sys.argv[0]} <activity_json_file> <threshold_minutes>",
            file=sys.stderr,
        )
        return 2

    path = Path(sys.argv[1])
    try:
        threshold_minutes = float(sys.argv[2])
        if not math.isfinite(threshold_minutes) or threshold_minutes <= 0:
            raise ValueError("threshold must be positive and finite")
    except ValueError as exc:
        print(f"UNKNOWN ({exc})")
        return 2

    if sys.argv[1] == "--format-duration":
        print(_format_duration(threshold_minutes))
        return 0

    if not path.exists():
        print("UNKNOWN (activity file does not exist yet)")
        return 2

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("activity file must contain a JSON object")
        if data.get("version") != 2:
            raise ValueError("activity tracking needs a one-time upgrade")
        timestamps = [_activity_utc(data["started_at"])]
        for key in (
            "last_non_admin_activity",
            "last_other_admin_activity",
            "last_main_admin_activity",
        ):
            if data.get(key) is not None:
                value = _activity_utc(data[key])
                if value > datetime.now(UTC):
                    raise ValueError("future activity timestamp")
                if key != "last_main_admin_activity":
                    timestamps.append(value)
        last_activity = max(timestamps)
        if last_activity > datetime.now(UTC):
            raise ValueError("future startup timestamp")
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"UNKNOWN (invalid activity file: {e})")
        return 2

    age_minutes = (datetime.now(UTC) - last_activity).total_seconds() / 60
    status = "ACTIVE" if age_minutes < threshold_minutes + 0.5 else "IDLE"
    print(
        f"{status} (deployment activity {_format_duration(age_minutes)} ago, "
        f"at {last_activity.astimezone(UTC):%Y-%m-%d %H:%M:%S} UTC)"
    )
    return 1 if status == "ACTIVE" else 0


if __name__ == "__main__":
    sys.exit(main())

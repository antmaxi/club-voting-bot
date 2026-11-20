"""Tests for scripts/check_bot_idle.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_bot_idle.py"


class TestCheckBotIdle(unittest.TestCase):
    def _run(
        self, path: Path, threshold: float = 5
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(path), str(threshold)],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_aware_utc_timestamp_does_not_crash(self):
        with self.subTest("aware"):
            p = self._tmp_activity(datetime.now(UTC))
            result = self._run(p)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("ACTIVE", result.stdout)

    def test_legacy_naive_timestamp_does_not_crash(self):
        p = self._tmp_activity(datetime.now(UTC).replace(tzinfo=None))
        result = self._run(p)
        self.assertIn(result.returncode, (0, 1), result.stdout)

    def test_old_activity_is_idle(self):
        p = self._tmp_activity(datetime.now(UTC) - timedelta(hours=1))
        result = self._run(p, threshold=5)
        self.assertEqual(result.returncode, 0)
        self.assertIn("IDLE", result.stdout)

    def test_pickle_payload_is_never_deserialized(self):
        import tempfile

        fd, name = tempfile.mkstemp(suffix=".pickle")
        os.close(fd)
        path = Path(name)
        path.write_bytes(b"\x80\x04cos\nsystem\n.")
        self.addCleanup(path.unlink, missing_ok=True)
        result = self._run(path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("UNKNOWN", result.stdout)

    def test_missing_activity_file_is_unknown(self):
        import tempfile

        path = Path(tempfile.mktemp(suffix=".json"))
        result = self._run(path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("UNKNOWN", result.stdout)

    def test_admin_categories_and_startup_grace(self):
        old = datetime.now(UTC) - timedelta(hours=1)
        recent = datetime.now(UTC).isoformat()
        p = self._tmp_activity(old)
        data = json.loads(p.read_text())
        data["last_main_admin_activity"] = recent
        p.write_text(json.dumps(data))
        self.assertEqual(self._run(p).returncode, 0)
        data["last_other_admin_activity"] = recent
        p.write_text(json.dumps(data))
        self.assertEqual(self._run(p).returncode, 1)
        p.write_text(json.dumps({"version": 2, "started_at": recent}))
        self.assertEqual(self._run(p).returncode, 1)

    def test_legacy_and_future_are_unknown(self):
        p = self._tmp_activity(datetime.now(UTC) + timedelta(hours=1))
        self.assertEqual(self._run(p).returncode, 2)
        p.write_text('{"last_non_admin_activity": "2020-01-01"}')
        self.assertEqual(self._run(p).returncode, 2)

    def test_write_throttle_margin(self):
        p = self._tmp_activity(datetime.now(UTC) - timedelta(minutes=5, seconds=10))
        self.assertEqual(self._run(p).returncode, 1)

    def _tmp_activity(self, timestamp: datetime) -> Path:
        import tempfile

        fd, name = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        path = Path(name)
        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "started_at": timestamp.isoformat(),
                    "last_non_admin_activity": timestamp.isoformat(),
                }
            ),
            encoding="utf-8",
        )
        self.addCleanup(path.unlink, missing_ok=True)
        return path


if __name__ == "__main__":
    unittest.main()

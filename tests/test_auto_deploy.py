"""Offline integration coverage for scheduled multi-instance deployment."""

import fcntl
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


@pytest.mark.skipif(
    shutil.which("git") is None, reason="host deployment test requires git"
)
def test_auto_deploy_retries_and_isolates_instances(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    git(origin, "config", "user.email", "test@example.com")
    git(origin, "config", "user.name", "Test")
    (origin / "docker-compose.yml").write_text("services: {}\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "initial")
    instances = []
    for name in ("idle", "busy", "stopped"):
        repo = tmp_path / name
        subprocess.run(["git", "clone", str(origin), str(repo)], check=True)
        (repo / ".git/info/exclude").write_text("data/\n")
        (repo / "data").mkdir()
        stamp = datetime.now(UTC) - timedelta(hours=1)
        data = {"version": 2, "started_at": stamp.isoformat()}
        if name == "busy":
            data["last_other_admin_activity"] = datetime.now(UTC).isoformat()
        else:
            data["last_main_admin_activity"] = datetime.now(UTC).isoformat()
        (repo / "data/last_activity.json").write_text(json.dumps(data))
        instances.append(repo)
    git(origin, "commit", "--allow-empty", "-m", "update")
    target = git(origin, "rev-parse", "HEAD")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text("""#!/bin/bash
printf '%s %s\\n' "$(basename "$PWD")" "$*" >> "$CALLS"
if [ "$1" = inspect ]; then echo sha256:previous; exit; fi
case "$*" in
  *" ps "*) [ "$(basename "$PWD")" = stopped ] || echo container ;;
  *" build "*)
    if [ -f "$FAIL_BUILD.activity" ]; then cp "$FAIL_BUILD.activity" data/last_activity.json; fi
    [ ! -f "$FAIL_BUILD" ] ;;
  *"up -d"*)
    case "$*" in *" -f "*) exit 0 ;; esac
    [ ! -f "$FAIL_BUILD.up" ] ;;
esac
""")
    docker.chmod(0o755)
    envfile = tmp_path / "inventory.env"
    envfile.write_text(
        'DEPLOY_REPOS="' + ":".join(map(str, instances + [instances[0]])) + '"\n'
    )
    calls = tmp_path / "calls"
    fail = tmp_path / "fail"
    env = dict(
        os.environ,
        PATH=f"{binaries}:{os.environ['PATH']}",
        DEPLOY_ENV_FILE=str(envfile),
        CALLS=str(calls),
        FAIL_BUILD=str(fail),
    )

    def run():
        return subprocess.run(
            ["bash", str(ROOT / "scripts/deploy_bots.sh"), "--auto"],
            env=env,
            capture_output=True,
            text=True,
        )

    fail.touch()
    assert run().returncode == 1
    assert (instances[0] / ".git/bookclub-deploy.pending").exists()
    assert "up -d" not in calls.read_text()
    fail.unlink()
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(instances[0], "rev-parse", "HEAD") == target
    assert not (instances[0] / ".git/bookclub-deploy.pending").exists()
    assert git(instances[1], "rev-parse", "HEAD") != target
    assert git(instances[2], "rev-parse", "HEAD") != target
    calls.write_text("")
    assert run().returncode == 0
    assert " build " not in calls.read_text()
    busy = instances[1] / "data/last_activity.json"
    data = json.loads(busy.read_text())
    del data["last_other_admin_activity"]
    busy.write_text(json.dumps(data))
    assert run().returncode == 0
    assert git(instances[1], "rev-parse", "HEAD") == target

    # Activity arriving during a build defers replacement but retains pending work.
    git(origin, "commit", "--allow-empty", "-m", "another update")
    activity = Path(str(fail) + ".activity")
    activity.write_text(
        json.dumps({"version": 2, "started_at": datetime.now(UTC).isoformat()})
    )
    calls.write_text("")
    assert run().returncode == 0
    assert "up -d" not in calls.read_text()
    assert (instances[0] / ".git/bookclub-deploy.pending").exists()
    activity.unlink()
    for repo in instances[:2]:
        (repo / "data/last_activity.json").write_text(
            json.dumps({"version": 2, "started_at": stamp.isoformat()})
        )
    # A failed replacement attempts the old image but still reports failure.
    failure = Path(str(fail) + ".up")
    failure.touch()
    assert run().returncode == 1
    assert " -f " in calls.read_text()
    failure.unlink()
    # The per-instance lock protects against a concurrent manual deployment.
    with (instances[0] / ".git/bookclub-deploy.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run()
        assert "deployment locked" in result.stdout
        assert (instances[0] / ".git/bookclub-deploy.pending").exists()
    assert run().returncode == 0
    assert not (instances[0] / ".git/bookclub-deploy.pending").exists()
    # Dirty trees, other branches, and local commits are never overwritten.
    git(origin, "commit", "--allow-empty", "-m", "third update")
    (instances[0] / "local.txt").write_text("keep this")
    git(instances[1], "switch", "-c", "feature")
    calls.write_text("")
    result = run()
    assert result.returncode == 0
    assert "dirty working tree" in result.stdout
    assert "branch is not main" in result.stdout
    assert " build " not in calls.read_text()
    (instances[0] / "local.txt").unlink()
    git(
        instances[0],
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "local commit",
    )
    result = run()
    assert "local commits or divergence" in result.stdout

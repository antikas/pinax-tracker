"""
tests/conftest.py — pytest plugin (auto-loaded by pytest).

Makes normalise_for_comparison available to all test files via helpers.py.
"""

# Re-export for any test that needs it via `from tests.helpers import ...`
# This file is intentionally minimal; helpers.py holds the actual code.


# ---------------------------------------------------------------------------
# Test-lane standard: in-framework verdict emission (scale-down profile).
# Single fast lane; every invocation writes .verdicts/latest.json + a
# Dependency-free by design (json + pathlib + time only).
# ---------------------------------------------------------------------------
import json as _json
import sys as _sys
import time as _time
from datetime import datetime as _datetime
from pathlib import Path as _Path

import pytest as _pytest


def pytest_sessionstart(session: "_pytest.Session") -> None:
    session.config._lane_session_start = _time.monotonic()


def pytest_sessionfinish(session: "_pytest.Session", exitstatus: int) -> None:
    config = session.config
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    stats = getattr(reporter, "stats", {}) if reporter else {}

    def _n(key: str) -> int:
        return len(stats.get(key, []))

    counts = {
        "passed": _n("passed"),
        "failed": _n("failed"),
        "errors": _n("error"),
        "skipped": _n("skipped"),
        "deselected": _n("deselected"),
    }
    skips = []
    for report in stats.get("skipped", []):
        longrepr = getattr(report, "longrepr", None)
        reason = str(longrepr[2]) if isinstance(longrepr, tuple) and len(longrepr) == 3 else str(longrepr)
        skips.append({"test": getattr(report, "nodeid", "?"), "reason": reason.removeprefix("Skipped: ")})

    fired = any(
        "from pytest-timeout" in str(getattr(report, "longrepr", ""))
        for key in ("failed", "error")
        for report in stats.get(key, [])
    )
    start = getattr(config, "_lane_session_start", None)

    # Derive the lane label and the not_exercised confession from the
    # marker expression actually in effect for THIS invocation (addopts'
    # default -m "not deep", an explicit -m deep, -m "deep or not deep",
    # or anything else) rather than a hardcoded literal — a hardcoded
    # "fast" would silently lie on any non-default invocation.
    markexpr = (getattr(config.option, "markexpr", "") or "").strip()
    if markexpr == "not deep":
        lane = "fast"
    elif markexpr == "deep":
        lane = "deep"
    elif not markexpr:
        lane = "all"
    else:
        lane = markexpr

    not_exercised = []
    if counts["deselected"] > 0:
        if lane == "fast":
            not_exercised.append({
                "scope": f"deep lane ({counts['deselected']} tests)",
                "reason": "git-subprocess/temp-repo integration tests excluded from "
                          "fast by 'not deep'; run via 'pytest -m deep'",
            })
        else:
            not_exercised.append({
                "scope": f"{counts['deselected']} tests deselected by marker expression '{markexpr}'",
                "reason": "excluded by the active -m filter for this invocation",
            })

    payload = {
        "repo": "pinax",
        "lane": lane,
        "command": " ".join(_sys.argv),
        "duration_s": round(_time.monotonic() - start, 2) if start is not None else 0.0,
        "exit_code": int(exitstatus),
        "counts": counts,
        "skips": skips,
        "not_exercised": not_exercised,
        "timeout": {"per_test_s": config.getoption("timeout", None), "global_s": None, "fired": fired},
        "verdict": "green"
        if (exitstatus == 0 and counts["failed"] == 0 and counts["errors"] == 0 and not fired)
        else "red",
    }
    verdict_dir = _Path(config.rootpath) / ".verdicts"
    verdict_dir.mkdir(parents=True, exist_ok=True)
    body = _json.dumps(payload, indent=2)
    (verdict_dir / "latest.json").write_text(body, encoding="utf-8")
    (verdict_dir / f"{_datetime.now().strftime('%Y%m%dT%H%M%S')}.json").write_text(body, encoding="utf-8")

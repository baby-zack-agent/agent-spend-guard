"""Smoke tests for spend_guard. Run with: python -m pytest tests/ (or plain python)."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from spend_guard import SpendGuard  # noqa: E402


def test_cost_math():
    g = SpendGuard(pricing={"m": (1.0, 3.0)}, quiet=True)
    cost = g.log_call("m", prompt_tokens=1_000_000, completion_tokens=500_000)
    assert abs(cost - 2.5) < 1e-9, cost
    assert abs(g.summary()["daily_cost_usd"] - 2.5) < 1e-9


def test_task_budget_alert():
    g = SpendGuard(task_budget_usd=1.0, pricing={"m": (10.0, 10.0)}, quiet=True)
    with g.task("big"):
        g.log_call("m", prompt_tokens=1_000_000, completion_tokens=0)
    assert any("exceeded its budget" in a for a in g.summary()["alerts"])


def test_dead_lane_warning():
    g = SpendGuard(failure_streak_warn=3, quiet=True)
    with g.task("lane"):
        g.log_failure("x")
        g.log_failure("x")
        assert not any("DEAD LANE" in a for a in g.summary()["alerts"])
        g.log_failure("x")
    assert any("DEAD LANE" in a for a in g.summary()["alerts"])


def test_unknown_model_warns_and_records_zero():
    g = SpendGuard(quiet=True)
    cost = g.log_call("mystery-model", prompt_tokens=100, completion_tokens=100)
    assert cost == 0.0
    assert any("No price for model" in a for a in g.summary()["alerts"])


def test_jsonl_log():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "spend.jsonl")
        g = SpendGuard(log_path=path, quiet=True)
        g.log_call("m", prompt_tokens=10, completion_tokens=5)
        g.log_failure("boom")
        with open(path) as f:
            lines = f.readlines()
        assert len(lines) == 2
        assert '"type": "call"' in lines[0]
        assert '"type": "failure"' in lines[1]


def test_daily_report_renders():
    g = SpendGuard(daily_budget_usd=5.0, pricing={"m": (1.0, 1.0)}, quiet=True)
    with g.task("demo"):
        g.log_call("m", prompt_tokens=1000, completion_tokens=500)
    report = g.daily_report()
    assert "demo" in report and "Total:" in report


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_"):
            fn()
            print("ok -", name)
    print("all tests passed")

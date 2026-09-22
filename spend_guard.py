"""
spend_guard — token & cost guardrails for AI agent runs.

Wrap your agent's model calls and get per-task spend tracking, budget
alerts, and dead-lane detection. Model-agnostic: you report token counts
(or an explicit cost) and it does the accounting.

Built by baby Zack, an AI agent, from firsthand experience running agents
on a $0 budget. The problem it solves is described in "The Token Tax":
https://telegra.ph/The-Token-Tax-What-Running-Agents-Daily-on-0-Taught-Me-About-the-Cost-Explosion-09-22
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
# Sample per-1M-token prices. Every value below MUST be verified against your
# provider's current pricing page — model prices change constantly and these
# were last eyeballed on 2026-09-22. If a model isn't listed, log_call() will
# warn once and record $0.00 until you add it with set_price().
SAMPLE_PRICING = {
    # model name: (input USD per 1M tokens, output USD per 1M tokens)
    "gpt-4o": (2.50, 10.00),            # verify
    "gpt-4o-mini": (0.15, 0.60),        # verify
    "claude-sonnet-4": (3.00, 15.00),   # verify
    "claude-haiku-3-5": (0.80, 4.00),   # verify
    "gemini-2.5-flash": (0.30, 2.50),   # verify
}


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _new_task():
    return {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
        "failures": 0,
        "seconds": 0.0,
    }


class SpendGuard:
    """Track token spend across agent tasks. See module docstring for the idea."""

    def __init__(
        self,
        daily_budget_usd=None,
        task_budget_usd=None,
        failure_streak_warn=5,
        log_path=None,
        pricing=None,
        quiet=False,
    ):
        self.daily_budget_usd = daily_budget_usd
        self.task_budget_usd = task_budget_usd
        self.failure_streak_warn = failure_streak_warn
        self.log_path = log_path
        self.pricing = dict(pricing) if pricing else {}
        self.quiet = quiet

        self._day = _today()
        self._daily_cost_usd = 0.0
        self._tasks = {}
        self._current_task = None
        self._failure_streak = 0
        self._warned_80 = False
        self._warned_100 = False
        self._unknown_models = set()
        self._alerts = []

    # -- configuration ----------------------------------------------------
    def set_price(self, model, input_per_1m, output_per_1m):
        """Add or override a model's per-1M-token prices (USD)."""
        self.pricing[model] = (float(input_per_1m), float(output_per_1m))

    # -- task scoping ------------------------------------------------------
    @contextmanager
    def task(self, name):
        """Context manager that scopes subsequent log_call()s to a named task."""
        prev = self._current_task
        self._current_task = name
        task = self._tasks.setdefault(name, _new_task())
        started = time.time()
        try:
            yield self
        finally:
            task["seconds"] += time.time() - started
            self._current_task = prev
            if (
                self.task_budget_usd
                and task["cost_usd"] > self.task_budget_usd
            ):
                self._alert(
                    "Task '%s' exceeded its budget: $%.4f > $%.2f"
                    % (name, task["cost_usd"], self.task_budget_usd)
                )

    # -- logging ------------------------------------------------------------
    def log_call(self, model, prompt_tokens, completion_tokens,
                 cost_usd=None, task=None):
        """Record one model call. Returns the USD cost attributed."""
        self._roll_day_if_needed()
        name = task or self._current_task or "untracked"
        entry = self._tasks.setdefault(name, _new_task())

        if cost_usd is None:
            if model in self.pricing:
                pin, pout = self.pricing[model]
                cost_usd = (prompt_tokens / 1e6) * pin + (completion_tokens / 1e6) * pout
            else:
                cost_usd = 0.0
                if model not in self._unknown_models:
                    self._unknown_models.add(model)
                    self._alert(
                        "No price for model '%s' — recorded $0.00. "
                        "Add it with set_price()." % model
                    )

        entry["calls"] += 1
        entry["prompt_tokens"] += prompt_tokens
        entry["completion_tokens"] += completion_tokens
        entry["cost_usd"] += cost_usd
        self._daily_cost_usd += cost_usd

        self._check_daily_budget()
        self._write_log({
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "call",
            "task": name,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usd": round(cost_usd, 6),
        })
        return cost_usd

    def log_failure(self, note=""):
        """Record a failed step. N consecutive failures => dead-lane warning."""
        self._roll_day_if_needed()
        name = self._current_task or "untracked"
        self._tasks.setdefault(name, _new_task())["failures"] += 1
        self._failure_streak += 1
        self._write_log({
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "failure",
            "task": name,
            "note": note,
            "streak": self._failure_streak,
        })
        if self._failure_streak == self.failure_streak_warn:
            self._alert(
                "POSSIBLE DEAD LANE in '%s': %d consecutive failures%s. "
                "Consider stopping this lane instead of retrying blind."
                % (name, self._failure_streak, " (%s)" % note if note else "")
            )

    def log_success(self):
        """Reset the consecutive-failure streak (call when a lane recovers)."""
        self._failure_streak = 0

    # -- reporting -----------------------------------------------------------
    def daily_report(self):
        """Plain-text summary of today's spend. Print it, log it, whatever."""
        lines = [
            "SpendGuard daily report — %s (UTC)" % self._day,
            "Total: $%.4f" % self._daily_cost_usd,
        ]
        if self.daily_budget_usd:
            lines.append("Budget: $%.2f (%.0f%% used)" % (
                self.daily_budget_usd,
                100 * self._daily_cost_usd / self.daily_budget_usd,
            ))
        lines.append("")
        lines.append("%-28s %6s %10s %9s %8s" % (
            "task", "calls", "tokens", "cost", "fails"))
        for name, t in sorted(self._tasks.items(),
                              key=lambda kv: kv[1]["cost_usd"], reverse=True):
            tokens = t["prompt_tokens"] + t["completion_tokens"]
            lines.append("%-28s %6d %10d $%8.4f %8d" % (
                name[:28], t["calls"], tokens, t["cost_usd"], t["failures"]))
        if self._alerts:
            lines.append("")
            lines.append("Alerts:")
            lines.extend("  ! " + a for a in self._alerts)
        return "\n".join(lines)

    def summary(self):
        """Machine-readable snapshot."""
        return {
            "day": self._day,
            "daily_cost_usd": round(self._daily_cost_usd, 6),
            "daily_budget_usd": self.daily_budget_usd,
            "tasks": {k: dict(v, cost_usd=round(v["cost_usd"], 6))
                      for k, v in self._tasks.items()},
            "failure_streak": self._failure_streak,
            "alerts": list(self._alerts),
        }

    # -- internals ------------------------------------------------------------
    def _roll_day_if_needed(self):
        today = _today()
        if today != self._day:
            self._day = today
            self._daily_cost_usd = 0.0
            self._tasks = {}
            self._warned_80 = False
            self._warned_100 = False
            self._alerts = []

    def _check_daily_budget(self):
        if not self.daily_budget_usd:
            return
        frac = self._daily_cost_usd / self.daily_budget_usd
        if frac >= 1.0 and not self._warned_100:
            self._warned_100 = True
            self._alert("DAILY BUDGET EXCEEDED: $%.4f / $%.2f"
                        % (self._daily_cost_usd, self.daily_budget_usd))
        elif frac >= 0.8 and not self._warned_80:
            self._warned_80 = True
            self._alert("Daily budget 80% used: $%.4f / $%.2f"
                        % (self._daily_cost_usd, self.daily_budget_usd))

    def _alert(self, msg):
        self._alerts.append(msg)
        if not self.quiet:
            print("[spend-guard] %s" % msg)

    def _write_log(self, record):
        if not self.log_path:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as exc:
            self._alert("Could not write log file: %s" % exc)


__all__ = ["SpendGuard", "SAMPLE_PRICING"]

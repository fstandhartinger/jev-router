"""Catalogue, health tracking and published routing rules.

Every catalogue entry records why it may (or may not) receive traffic:
  paid    - our own hosted open model with a commercial licence, billed per decision
  free    - a third-party service whose terms allow routing; we add no charge
  byok    - the customer's own provider key is forwarded unchanged; we add no charge
  listed  - shown for comparison only, never called (terms do not allow routing)
The selection rule for `auto` is deterministic and documented on /docs#routing.
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any

CATALOGUE_PATH = Path(os.getenv("CATALOGUE_PATH", Path(__file__).with_name("catalogue.json")))
ROUTABLE = ("paid", "free", "byok")
PREFERENCES = ("quality", "balanced", "price", "latency")
# Task hints map onto the published JevBench tiers, so "auto" picks the model
# that measured best on that kind of question.
TASKS = {
    "general": None,
    "classification": "standard",
    "judgement": "judge",
    "hard": "hard",
    "easy": "easy",
}
FAILURES_TO_OPEN = 2
OPEN_SECONDS = 60


def load_catalogue(path: Path = CATALOGUE_PATH) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text())
    entries = {}
    for entry in data["entries"]:
        if entry["routing"] not in ROUTABLE + ("listed",):
            raise ValueError(f"unknown routing class for {entry['id']}")
        if entry["routing"] != "listed" and not entry.get("terms_basis"):
            raise ValueError(f"{entry['id']} is routable without a recorded terms basis")
        entries[entry["id"]] = entry
    return entries


class Health:
    """Per-model circuit breaker plus a latency moving average from real calls and probes."""

    def __init__(self) -> None:
        self.state: dict[str, dict[str, Any]] = {}

    def get(self, model: str) -> dict[str, Any]:
        return self.state.setdefault(model, {"healthy": None, "failures": 0, "open_until": 0.0, "ewma_ms": None, "checked": None, "last_error": None})

    def ok(self, model: str, ms: float | None = None) -> None:
        s = self.get(model)
        s.update(healthy=True, failures=0, open_until=0.0, checked=time.time(), last_error=None)
        if ms is not None:
            s["ewma_ms"] = ms if s["ewma_ms"] is None else round(0.8 * s["ewma_ms"] + 0.2 * ms, 1)

    def fail(self, model: str, error: str) -> None:
        s = self.get(model)
        s["failures"] += 1
        s["checked"] = time.time()
        s["last_error"] = error[:160]
        if s["failures"] >= FAILURES_TO_OPEN:
            s["healthy"] = False
            s["open_until"] = time.time() + OPEN_SECONDS

    def available(self, model: str) -> bool:
        s = self.get(model)
        if s["healthy"] is False and time.time() < s["open_until"]:
            return False
        # Unknown or half-open: allow a trial request; a success closes the circuit.
        return True

    def status(self, model: str) -> str:
        s = self.get(model)
        if s["healthy"] is True:
            return "live"
        if s["healthy"] is False:
            return "degraded" if time.time() >= s["open_until"] else "offline"
        return "unknown"


def score_for(entry: dict[str, Any], image: bool, task: str | None) -> float | None:
    if image:
        bench = entry.get("imagejevbench")
        return float(bench["score"]) if bench and bench.get("score") is not None else None
    bench = entry.get("jevbench")
    if not bench or bench.get("score") is None:
        return None
    tier = TASKS.get(task or "general")
    if tier and bench.get("tiers", {}).get(tier) is not None:
        return round(100 * float(bench["tiers"][tier]), 2)
    return float(bench["score"])


def price_usd_per_1k(entry: dict[str, Any]) -> float:
    return 0.0 if entry["routing"] in ("free", "byok") else entry["price_per_1k_cents"] / 100


def latency_ms(entry: dict[str, Any], health: Health) -> float:
    live = health.get(entry["id"])["ewma_ms"]
    if live is not None:
        return live
    published = (entry.get("jevbench") or {}).get("p50_s")
    if published:
        return 1000 * published
    probed = (entry.get("probe") or {}).get("p50_ms")
    return float(probed) if probed else 2000.0


def eligible(entry: dict[str, Any], *, image: bool, question_types: set[str], provider_keys: dict[str, str], health: Health) -> bool:
    if entry["routing"] not in ROUTABLE:
        return False
    if ("image" if image else "text") not in entry["modalities"]:
        return False
    if not question_types <= set(entry["question_types"]):
        return False
    if entry["routing"] == "byok" and entry["byok_vendor"] not in provider_keys:
        return False
    return health.available(entry["id"])


def rank(entries: dict[str, dict[str, Any]], health: Health, *, image: bool, question_types: set[str],
         provider_keys: dict[str, str] | None = None, prefer: str = "quality", task: str | None = None,
         max_price_per_1k: float | None = None, max_latency_ms: float | None = None, benchmarked_only: bool = True) -> list[str]:
    """Return eligible model IDs, best first, using the published rule."""
    provider_keys = provider_keys or {}
    rows = []
    for model, entry in entries.items():
        if not eligible(entry, image=image, question_types=question_types, provider_keys=provider_keys, health=health):
            continue
        score = score_for(entry, image, task)
        if score is None and benchmarked_only:
            continue
        price = price_usd_per_1k(entry)
        ms = latency_ms(entry, health)
        if max_price_per_1k is not None and price > max_price_per_1k:
            continue
        if max_latency_ms is not None and ms > max_latency_ms:
            continue
        score = score or 0.0
        if prefer == "price":
            key = (price, -score, ms)
        elif prefer == "latency":
            key = (ms, -score, price)
        elif prefer == "balanced":
            # One published formula: benchmark points minus 10 per extra second of
            # median latency and minus 10 per tenfold price above USD 0.01 / 1,000.
            value = score - 10 * (ms / 1000) - 10 * max(0.0, math.log10(max(price, 0.01) / 0.01))
            key = (-value, price, ms)
        else:
            key = (-score, price, ms)
        rows.append((key, model))
    return [model for _, model in sorted(rows, key=lambda row: (row[0], row[1]))]

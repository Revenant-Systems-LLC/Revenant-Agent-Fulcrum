"""LLM filter interface and FakeFilter.

The filter is a veto-only semantic check on candidate ENTRIES and INCREASES. It never
sees exits, stops, sizes or the kill switch. Any failure is FAIL OPEN: the rules proceed
and the failure is logged. ``ClaudeFilter`` lives in ``claude_filter.py``.
"""

from __future__ import annotations

import json
import time
from typing import Callable, Protocol

from .models import Candidate, FilterResult, MarketContext, Verdict

VERDICT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["allow", "veto"]},
                    "reason": {"type": "string", "maxLength": 500},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "invalidation": {"type": "string", "maxLength": 300},
                },
                "required": ["symbol", "verdict", "reason", "confidence", "invalidation"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


class FilterProtocol(Protocol):
    mode: str

    def evaluate(self, candidates: list[Candidate], context: MarketContext) -> FilterResult: ...


class FilterValidationError(ValueError):
    pass


def parse_verdicts(raw: str, allowed_symbols: set[str]) -> tuple[Verdict, ...]:
    """Strict parse of the model's JSON. Anything off-contract raises FilterValidationError.

    Hand-written validation instead of a jsonschema dependency: the contract is small and
    every rule below is a line a reviewer can read.
    """
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise FilterValidationError(f"not JSON: {e}") from e
    if not isinstance(obj, dict) or set(obj) != {"verdicts"} or not isinstance(obj["verdicts"], list):
        raise FilterValidationError("top level must be {'verdicts': [...]} and nothing else")
    out: list[Verdict] = []
    seen: set[str] = set()
    for i, v in enumerate(obj["verdicts"]):
        if not isinstance(v, dict):
            raise FilterValidationError(f"verdict[{i}] not an object")
        required = {"symbol", "verdict", "reason", "confidence", "invalidation"}
        if set(v) != required:
            raise FilterValidationError(f"verdict[{i}] keys {sorted(v)} != {sorted(required)}")
        sym = v["symbol"]
        if not isinstance(sym, str) or sym not in allowed_symbols:
            raise FilterValidationError(f"verdict[{i}] symbol {sym!r} not a candidate")
        if sym in seen:
            raise FilterValidationError(f"duplicate verdict for {sym}")
        seen.add(sym)
        if v["verdict"] not in ("allow", "veto"):
            raise FilterValidationError(f"verdict[{i}] verdict must be allow|veto")
        if not isinstance(v["reason"], str) or not v["reason"].strip() or len(v["reason"]) > 500:
            raise FilterValidationError(f"verdict[{i}] reason invalid")
        conf = v["confidence"]
        if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0.0 <= float(conf) <= 1.0:
            raise FilterValidationError(f"verdict[{i}] confidence must be a number in [0,1]")
        if not isinstance(v["invalidation"], str) or len(v["invalidation"]) > 300:
            raise FilterValidationError(f"verdict[{i}] invalidation invalid")
        out.append(Verdict(symbol=sym, verdict=v["verdict"], reason=v["reason"].strip(), confidence=float(conf),
                           invalidation=v["invalidation"].strip()))
    missing = allowed_symbols - seen
    if missing:
        raise FilterValidationError(f"no verdict for candidates: {sorted(missing)}")
    return tuple(out)


class FakeFilter:
    """Scripted filter for tests and dry-run.

    ``script`` maps symbol -> "allow" | "veto". Unlisted symbols are allowed.
    ``fail_with`` makes evaluate() return a failed result (to test fail-open), and
    ``raw_override`` feeds arbitrary text through the real parser (to test validation).
    """

    def __init__(self, mode: str = "shadow", script: dict[str, str] | None = None, fail_with: str | None = None,
                 raw_override: str | None = None, clock: Callable[[], float] | None = None):
        if mode not in ("off", "shadow", "active"):
            raise ValueError("mode must be off|shadow|active")
        self.mode = mode
        self.script = dict(script or {})
        self.fail_with = fail_with
        self.raw_override = raw_override
        self.calls: list[tuple[tuple[str, ...], MarketContext]] = []
        self._clock = clock or time.monotonic

    def evaluate(self, candidates: list[Candidate], context: MarketContext) -> FilterResult:
        t0 = self._clock()
        self.calls.append((tuple(c.symbol for c in candidates), context))
        symbols = {c.symbol for c in candidates}
        if self.fail_with:
            return FilterResult(verdicts=(), error=self.fail_with, latency_ms=int((self._clock() - t0) * 1000))
        if self.raw_override is not None:
            raw = self.raw_override
        else:
            raw = json.dumps({"verdicts": [
                {"symbol": c.symbol, "verdict": self.script.get(c.symbol, "allow"),
                 "reason": "scripted", "confidence": 0.5, "invalidation": "n/a"} for c in candidates]})
        try:
            verdicts = parse_verdicts(raw, symbols)
        except FilterValidationError as e:
            return FilterResult(verdicts=(), error=f"validation: {e}", latency_ms=int((self._clock() - t0) * 1000), raw=raw)
        return FilterResult(verdicts=verdicts, error=None, latency_ms=int((self._clock() - t0) * 1000), raw=raw)


class OffFilter:
    """mode = off: never called, never vetoes."""

    mode = "off"

    def evaluate(self, candidates: list[Candidate], context: MarketContext) -> FilterResult:
        return FilterResult(verdicts=tuple(
            Verdict(c.symbol, "allow", "filter off", 1.0, "") for c in candidates), error=None, latency_ms=0)

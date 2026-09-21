import json

import pytest

from fulcrum.llm_filter import FakeFilter, FilterValidationError, OffFilter, parse_verdicts
from fulcrum.models import Candidate, MarketContext
from tests.conftest import utc


def cands(*syms):
    return [Candidate(symbol=s, conviction=0.75, price=100.0, realized_vol=0.5, atr=3.0, target_weight=0.1,
                      current_weight=0.0) for s in syms]


CTX = MarketContext(as_of=utc(2026, 1, 1), summary="calm")


def good(*syms, verdict="allow"):
    return json.dumps({"verdicts": [
        {"symbol": s, "verdict": verdict, "reason": "ok", "confidence": 0.7, "invalidation": "x"} for s in syms]})


def test_parse_good():
    vs = parse_verdicts(good("BTC/USD", "ETH/USD"), {"BTC/USD", "ETH/USD"})
    assert [v.symbol for v in vs] == ["BTC/USD", "ETH/USD"]


@pytest.mark.parametrize("raw,msg", [
    ("not json", "not JSON"),
    ("[]", "top level"),
    (json.dumps({"verdicts": [], "extra": 1}), "top level"),
    (json.dumps({"verdicts": [{"symbol": "BTC/USD", "verdict": "allow", "reason": "r", "confidence": 0.5}]}), "keys"),
    (json.dumps({"verdicts": [{"symbol": "DOGE/USD", "verdict": "allow", "reason": "r", "confidence": 0.5,
                               "invalidation": ""}]}), "not a candidate"),
    (json.dumps({"verdicts": [{"symbol": "BTC/USD", "verdict": "maybe", "reason": "r", "confidence": 0.5,
                               "invalidation": ""}]}), "allow|veto"),
    (json.dumps({"verdicts": [{"symbol": "BTC/USD", "verdict": "veto", "reason": "", "confidence": 0.5,
                               "invalidation": ""}]}), "reason invalid"),
    (json.dumps({"verdicts": [{"symbol": "BTC/USD", "verdict": "veto", "reason": "r", "confidence": 1.5,
                               "invalidation": ""}]}), "confidence"),
    (json.dumps({"verdicts": [{"symbol": "BTC/USD", "verdict": "veto", "reason": "r", "confidence": True,
                               "invalidation": ""}]}), "confidence"),
    (json.dumps({"verdicts": []}), "no verdict for candidates"),
])
def test_parse_rejects_off_contract(raw, msg):
    with pytest.raises(FilterValidationError, match=msg):
        parse_verdicts(raw, {"BTC/USD"})


def test_parse_rejects_duplicate_symbol():
    raw = json.dumps({"verdicts": [
        {"symbol": "BTC/USD", "verdict": "allow", "reason": "r", "confidence": 0.5, "invalidation": ""},
        {"symbol": "BTC/USD", "verdict": "veto", "reason": "r", "confidence": 0.5, "invalidation": ""}]})
    with pytest.raises(FilterValidationError, match="duplicate"):
        parse_verdicts(raw, {"BTC/USD"})


def test_fake_filter_scripted_veto():
    f = FakeFilter(mode="active", script={"ETH/USD": "veto"})
    r = f.evaluate(cands("BTC/USD", "ETH/USD"), CTX)
    assert not r.failed
    assert r.vetoed() == {"ETH/USD"}


def test_fail_open_on_error_and_on_bad_output():
    r = FakeFilter(fail_with="timeout").evaluate(cands("BTC/USD"), CTX)
    assert r.failed and r.vetoed() == frozenset()
    r2 = FakeFilter(raw_override="{garbage").evaluate(cands("BTC/USD"), CTX)
    assert r2.failed and "validation" in r2.error and r2.vetoed() == frozenset()


def test_off_filter_never_vetoes():
    r = OffFilter().evaluate(cands("BTC/USD"), CTX)
    assert r.vetoed() == frozenset() and not r.failed


def test_candidate_conviction_bounds():
    with pytest.raises(ValueError):
        Candidate(symbol="BTC/USD", conviction=1.2, price=1.0, realized_vol=0.1, atr=1.0, target_weight=0.1,
                  current_weight=0.0)

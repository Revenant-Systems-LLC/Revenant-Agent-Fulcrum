"""SQLite ledger: the audit trail and the measurement system.

Append-only tables: runs, decisions, filter_verdicts, order_events, fills, equity_curve,
ghost_book, kill_switch_events, alerts, shadow_daily.
Current-state tables (rewritten): orders.status columns (every transition also lands in
order_events), positions_snapshot, kill_switch_state, reset_consumed.

One connection, one thread. WAL mode so a crash mid-write cannot corrupt earlier rows.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import (
    Decision, Fill, FilterResult, Order, OrderRequest, Position, TERMINAL_STATUSES, require_utc, utcnow,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, mode TEXT NOT NULL,
  config_hash TEXT NOT NULL, git_sha TEXT, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS decisions (
  decision_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, cycle_ts TEXT NOT NULL, symbol TEXT NOT NULL,
  conviction REAL NOT NULL, target_weight REAL NOT NULL, current_weight REAL NOT NULL, action TEXT NOT NULL,
  thesis TEXT NOT NULL, stop_price REAL, invalidation TEXT NOT NULL, binding_constraint TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_decisions_symbol_ts ON decisions(symbol, cycle_ts);

CREATE TABLE IF NOT EXISTS filter_verdicts (
  verdict_id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT, cycle_ts TEXT NOT NULL, symbol TEXT NOT NULL,
  mode TEXT NOT NULL, verdict TEXT NOT NULL, reason TEXT NOT NULL, confidence REAL, invalidation TEXT,
  latency_ms INTEGER NOT NULL, error TEXT, raw_json TEXT, applied INTEGER NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS orders (
  client_order_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
  purpose TEXT NOT NULL, notional_usd REAL, qty REAL, broker_order_id TEXT, status TEXT NOT NULL,
  submitted_at TEXT, last_update_at TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS ix_orders_submitted ON orders(submitted_at);

CREATE TABLE IF NOT EXISTS order_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, client_order_id TEXT NOT NULL, status TEXT NOT NULL,
  broker_order_id TEXT, note TEXT, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS fills (
  fill_id INTEGER PRIMARY KEY AUTOINCREMENT, client_order_id TEXT NOT NULL, symbol TEXT NOT NULL,
  side TEXT NOT NULL, qty REAL NOT NULL, price REAL NOT NULL, fee_usd REAL NOT NULL,
  slippage_bps_est REAL, filled_at TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_fills_symbol ON fills(symbol, filled_at);

CREATE TABLE IF NOT EXISTS positions_snapshot (
  symbol TEXT PRIMARY KEY, qty REAL NOT NULL, avg_entry REAL NOT NULL, market_value REAL NOT NULL,
  stop_price REAL, entry_decision_id TEXT, as_of TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS equity_curve (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, equity REAL NOT NULL, cash REAL NOT NULL,
  gross_exposure REAL NOT NULL, hwm REAL NOT NULL, drawdown REAL NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS ghost_book (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, book TEXT NOT NULL, equity REAL NOT NULL,
  cash REAL NOT NULL, positions_json TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS kill_switch_state (
  id INTEGER PRIMARY KEY CHECK (id = 1), tripped INTEGER NOT NULL, tripped_at TEXT, hwm REAL NOT NULL,
  drawdown_at_trip REAL, reset_at TEXT, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS kill_switch_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL, hwm REAL, drawdown REAL,
  note TEXT, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS reset_consumed (
  file_mtime_ns INTEGER PRIMARY KEY, consumed_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS alerts (
  alert_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS shadow_daily (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, symbol TEXT NOT NULL, conviction REAL NOT NULL,
  verdict TEXT NOT NULL, reason TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL);
"""


def _iso(ts: datetime) -> str:
    require_utc(ts, "timestamp")
    return ts.astimezone(timezone.utc).isoformat()


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if str(self.path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        # executescript() commits any open transaction first, so it runs outside tx().
        # Every statement in _SCHEMA is IF NOT EXISTS, so a crash mid-way is repaired on the next open.
        self._conn.executescript(_SCHEMA)
        with self.tx():
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] != SCHEMA_VERSION:
                raise RuntimeError(f"ledger schema version {row['version']} != code {SCHEMA_VERSION}; migrate first")
            if self._conn.execute("SELECT 1 FROM kill_switch_state WHERE id=1").fetchone() is None:
                self._conn.execute(
                    "INSERT INTO kill_switch_state(id, tripped, tripped_at, hwm, drawdown_at_trip, reset_at, updated_at)"
                    " VALUES (1, 0, NULL, 0.0, NULL, NULL, ?)", (_iso(utcnow()),))

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ---- runs -------------------------------------------------------------
    def start_run(self, run_id: str, mode: str, config_hash: str, git_sha: str | None, started_at: datetime) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)",
                      (run_id, _iso(started_at), mode, config_hash, git_sha, _iso(utcnow())))

    # ---- decisions --------------------------------------------------------
    def record_decision(self, d: Decision, run_id: str) -> None:
        with self.tx() as c:
            # ON CONFLICT: first write wins. Crash and restart with identical inputs should not raise.
            c.execute(
                "INSERT OR IGNORE INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (d.decision_id, run_id, _iso(d.cycle_ts), d.symbol, d.conviction, d.target_weight, d.current_weight,
                 d.action, d.thesis, d.stop_price, d.invalidation, d.binding_constraint, _iso(utcnow())))

    def latest_decision(self, symbol: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM decisions WHERE symbol=? ORDER BY cycle_ts DESC, created_at DESC LIMIT 1", (symbol,)
        ).fetchone()

    def decisions(self, since: datetime | None = None) -> list[sqlite3.Row]:
        if since is None:
            return self._conn.execute("SELECT * FROM decisions ORDER BY cycle_ts, symbol").fetchall()
        return self._conn.execute("SELECT * FROM decisions WHERE cycle_ts>=? ORDER BY cycle_ts, symbol",
                                  (_iso(since),)).fetchall()

    # ---- filter -----------------------------------------------------------
    def record_filter_result(self, cycle_ts: datetime, mode: str, result: FilterResult, applied: bool,
                             decision_ids: dict[str, str] | None = None, candidates: tuple[str, ...] = ()) -> None:
        """One row per verdict; on failure, one row per candidate symbol marked error."""
        now = _iso(utcnow())
        with self.tx() as c:
            if result.failed:
                for sym in candidates:
                    c.execute(
                        "INSERT INTO filter_verdicts(decision_id, cycle_ts, symbol, mode, verdict, reason, confidence,"
                        " invalidation, latency_ms, error, raw_json, applied, created_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ((decision_ids or {}).get(sym), _iso(cycle_ts), sym, mode, "allow", "filter failed: fail open",
                         None, None, result.latency_ms, result.error, result.raw, int(applied), now))
                return
            for v in result.verdicts:
                c.execute(
                    "INSERT INTO filter_verdicts(decision_id, cycle_ts, symbol, mode, verdict, reason, confidence,"
                    " invalidation, latency_ms, error, raw_json, applied, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ((decision_ids or {}).get(v.symbol), _iso(cycle_ts), v.symbol, mode, v.verdict, v.reason,
                     v.confidence, v.invalidation, result.latency_ms, None, result.raw, int(applied), now))

    def filter_verdicts(self, since: datetime | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM filter_verdicts"
        if since is not None:
            return self._conn.execute(q + " WHERE cycle_ts>=? ORDER BY cycle_ts", (_iso(since),)).fetchall()
        return self._conn.execute(q + " ORDER BY cycle_ts").fetchall()

    def record_shadow_daily(self, ts: datetime, symbol: str, conviction: float, verdict: str, reason: str,
                            error: str | None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO shadow_daily(ts, symbol, conviction, verdict, reason, error, created_at)"
                      " VALUES (?,?,?,?,?,?,?)", (_iso(ts), symbol, conviction, verdict, reason, error, _iso(utcnow())))

    def shadow_daily(self, since: datetime | None = None) -> list[sqlite3.Row]:
        """Daily hypothetical filter runs, oldest first. Added for report.py, which had
        no public reader for this table and would otherwise reach into the connection."""
        if since is None:
            return self._conn.execute("SELECT * FROM shadow_daily ORDER BY id").fetchall()
        return self._conn.execute("SELECT * FROM shadow_daily WHERE ts>=? ORDER BY id",
                                  (_iso(since),)).fetchall()

    # ---- orders -----------------------------------------------------------
    def record_order_intent(self, req: OrderRequest) -> bool:
        """Insert the order row BEFORE submitting to the broker. Returns False if the id already exists
        (the decision was already acted on: a restart after a crash, or a duplicate call)."""
        now = _iso(utcnow())
        with self.tx() as c:
            exists = c.execute("SELECT 1 FROM orders WHERE client_order_id=?", (req.client_order_id,)).fetchone()
            if exists:
                return False
            c.execute(
                "INSERT INTO orders(client_order_id, decision_id, symbol, side, purpose, notional_usd, qty, broker_order_id,"
                " status, submitted_at, last_update_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (req.client_order_id, req.decision_id, req.symbol, req.side, req.purpose, req.notional_usd, req.qty,
                 None, "new", None, now, now))
            c.execute("INSERT INTO order_events(client_order_id, status, broker_order_id, note, created_at)"
                      " VALUES (?,?,?,?,?)", (req.client_order_id, "new", None, "intent recorded", now))
            return True

    def update_order(self, client_order_id: str, status: str, broker_order_id: str | None = None,
                     submitted_at: datetime | None = None, note: str | None = None) -> None:
        now = _iso(utcnow())
        with self.tx() as c:
            row = c.execute("SELECT * FROM orders WHERE client_order_id=?", (client_order_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown order {client_order_id}")
            c.execute(
                "UPDATE orders SET status=?, broker_order_id=COALESCE(?, broker_order_id),"
                " submitted_at=COALESCE(?, submitted_at), last_update_at=? WHERE client_order_id=?",
                (status, broker_order_id, _iso(submitted_at) if submitted_at else None, now, client_order_id))
            c.execute("INSERT INTO order_events(client_order_id, status, broker_order_id, note, created_at)"
                      " VALUES (?,?,?,?,?)", (client_order_id, status, broker_order_id, note, now))

    def order(self, client_order_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM orders WHERE client_order_id=?", (client_order_id,)).fetchone()

    def open_orders(self) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(TERMINAL_STATUSES))
        return self._conn.execute(f"SELECT * FROM orders WHERE status NOT IN ({marks}) ORDER BY created_at",
                                  tuple(TERMINAL_STATUSES)).fetchall()

    def orders(self, since: datetime | None = None) -> list[sqlite3.Row]:
        if since is None:
            return self._conn.execute("SELECT * FROM orders ORDER BY created_at").fetchall()
        return self._conn.execute("SELECT * FROM orders WHERE created_at>=? ORDER BY created_at",
                                  (_iso(since),)).fetchall()

    def risk_increasing_orders_since(self, since: datetime) -> int:
        """Count of entry/increase orders created in the window that were not rejected. Used by the 24h cap."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE purpose IN ('entry','increase') AND status != 'rejected'"
            " AND created_at >= ?", (_iso(since),)).fetchone()
        return int(row["n"])

    # ---- fills ------------------------------------------------------------
    def record_fill(self, f: Fill) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO fills(client_order_id, symbol, side, qty, price, fee_usd, slippage_bps_est, filled_at, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (f.client_order_id, f.symbol, f.side, f.qty, f.price, f.fee_usd, f.slippage_bps_est,
                 _iso(f.filled_at), _iso(utcnow())))

    def fills(self, symbol: str | None = None, since: datetime | None = None) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM fills", []
        conds = []
        if symbol:
            conds.append("symbol=?"); args.append(symbol)
        if since is not None:
            conds.append("filled_at>=?"); args.append(_iso(since))
        if conds:
            q += " WHERE " + " AND ".join(conds)
        return self._conn.execute(q + " ORDER BY filled_at, fill_id", args).fetchall()

    def fills_for_order(self, client_order_id: str) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM fills WHERE client_order_id=? ORDER BY fill_id",
                                  (client_order_id,)).fetchall()

    def fees_since(self, since: datetime) -> float:
        row = self._conn.execute("SELECT COALESCE(SUM(fee_usd),0) AS s FROM fills WHERE filled_at>=?",
                                 (_iso(since),)).fetchone()
        return float(row["s"])

    # ---- positions / equity -----------------------------------------------
    def snapshot_positions(self, positions: list[Position], stops: dict[str, float], entry_ids: dict[str, str],
                           as_of: datetime) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM positions_snapshot")
            for p in positions:
                c.execute("INSERT INTO positions_snapshot VALUES (?,?,?,?,?,?,?)",
                          (p.symbol, p.qty, p.avg_entry, p.market_value, stops.get(p.symbol), entry_ids.get(p.symbol),
                           _iso(as_of)))

    def positions_snapshot(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM positions_snapshot ORDER BY symbol").fetchall()

    def record_equity(self, ts: datetime, equity: float, cash: float, gross_exposure: float, hwm: float,
                      drawdown: float) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO equity_curve(ts, equity, cash, gross_exposure, hwm, drawdown, created_at)"
                      " VALUES (?,?,?,?,?,?,?)", (_iso(ts), equity, cash, gross_exposure, hwm, drawdown, _iso(utcnow())))

    def equity_curve(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM equity_curve ORDER BY ts, id").fetchall()

    def record_ghost(self, ts: datetime, book: str, equity: float, cash: float, positions: dict[str, Any]) -> None:
        if book not in ("real", "counterfactual"):
            raise ValueError("book must be real|counterfactual")
        with self.tx() as c:
            c.execute("INSERT INTO ghost_book(ts, book, equity, cash, positions_json, created_at) VALUES (?,?,?,?,?,?)",
                      (_iso(ts), book, equity, cash, json.dumps(positions, sort_keys=True), _iso(utcnow())))

    def ghost_book(self, book: str | None = None) -> list[sqlite3.Row]:
        if book:
            return self._conn.execute("SELECT * FROM ghost_book WHERE book=? ORDER BY ts, id", (book,)).fetchall()
        return self._conn.execute("SELECT * FROM ghost_book ORDER BY ts, id").fetchall()

    # ---- kill switch ------------------------------------------------------
    def kill_state(self) -> sqlite3.Row:
        return self._conn.execute("SELECT * FROM kill_switch_state WHERE id=1").fetchone()

    def set_kill_state(self, *, tripped: bool, hwm: float, tripped_at: datetime | None = None,
                       drawdown_at_trip: float | None = None, reset_at: datetime | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE kill_switch_state SET tripped=?, tripped_at=?, hwm=?, drawdown_at_trip=?, reset_at=?, updated_at=?"
                " WHERE id=1",
                (int(tripped), _iso(tripped_at) if tripped_at else None, hwm, drawdown_at_trip,
                 _iso(reset_at) if reset_at else None, _iso(utcnow())))

    def record_kill_event(self, ts: datetime, kind: str, hwm: float | None, drawdown: float | None, note: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO kill_switch_events(ts, kind, hwm, drawdown, note, created_at) VALUES (?,?,?,?,?,?)",
                      (_iso(ts), kind, hwm, drawdown, note, _iso(utcnow())))

    def kill_events(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM kill_switch_events ORDER BY event_id").fetchall()

    def reset_already_consumed(self, file_mtime_ns: int) -> bool:
        return self._conn.execute("SELECT 1 FROM reset_consumed WHERE file_mtime_ns=?", (file_mtime_ns,)).fetchone() is not None

    def mark_reset_consumed(self, file_mtime_ns: int) -> None:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO reset_consumed(file_mtime_ns, consumed_at) VALUES (?,?)",
                      (file_mtime_ns, _iso(utcnow())))

    # ---- alerts -----------------------------------------------------------
    def record_alert(self, ts: datetime, kind: str, payload: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO alerts(ts, kind, payload_json, created_at) VALUES (?,?,?,?)",
                      (_iso(ts), kind, json.dumps(payload, sort_keys=True, default=str), _iso(utcnow())))

    def alerts(self, kind: str | None = None, since: datetime | None = None) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM alerts", []
        conds = []
        if kind:
            conds.append("kind=?"); args.append(kind)
        if since is not None:
            conds.append("ts>=?"); args.append(_iso(since))
        if conds:
            q += " WHERE " + " AND ".join(conds)
        return self._conn.execute(q + " ORDER BY alert_id", args).fetchall()

    # ---- helpers ----------------------------------------------------------
    def table_counts(self) -> dict[str, int]:
        names = [r["name"] for r in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: int(self._conn.execute(f"SELECT COUNT(*) AS n FROM {n}").fetchone()["n"]) for n in names}


def window_start(now: datetime, hours: int = 24) -> datetime:
    return now - timedelta(hours=hours)

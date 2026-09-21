"""Typed configuration loaded from config.toml. Validated once at startup; any bad value
raises ``ConfigError`` before anything else runs.

There is deliberately no "live" mode anywhere in this file. ``run.mode`` is
``dry_run`` or ``paper``. Adding a third value here would not make the broker
accept it either; see broker.AlpacaBroker.
"""

from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class ConfigError(ValueError):
    pass


def _req(d: dict, key: str, typ: type | tuple[type, ...], section: str) -> Any:
    if key not in d:
        raise ConfigError(f"[{section}] missing required key '{key}'")
    v = d[key]
    if typ is float and isinstance(v, int) and not isinstance(v, bool):
        v = float(v)
    if not isinstance(v, typ) or isinstance(v, bool) and typ is not bool:
        raise ConfigError(f"[{section}].{key} must be {getattr(typ, '__name__', typ)}, got {type(v).__name__}")
    return v


def _opt(d: dict, key: str, typ: type | tuple[type, ...], default: Any, section: str) -> Any:
    if key not in d:
        return default
    return _req(d, key, typ, section)


def _in_range(v: float, lo: float, hi: float, name: str, *, lo_inclusive: bool = True) -> None:
    ok_lo = v >= lo if lo_inclusive else v > lo
    if not (ok_lo and v <= hi):
        raise ConfigError(f"{name}={v} out of range {'[' if lo_inclusive else '('}{lo}, {hi}]")


def _no_unknown(d: dict, allowed: set[str], section: str) -> None:
    unknown = set(d) - allowed
    if unknown:
        raise ConfigError(f"[{section}] unknown keys: {sorted(unknown)}")


@dataclass(frozen=True, slots=True)
class RunConfig:
    mode: str
    ledger_path: str
    creds_dir: str
    kill_reset_file: str
    log_level: str

    @staticmethod
    def parse(d: dict) -> "RunConfig":
        s = "run"
        _no_unknown(d, {"mode", "ledger_path", "creds_dir", "kill_reset_file", "log_level"}, s)
        mode = _req(d, "mode", str, s)
        if mode not in ("dry_run", "paper"):
            raise ConfigError(f"[run].mode must be 'dry_run' or 'paper', got {mode!r}. There is no live mode.")
        level = _opt(d, "log_level", str, "INFO", s).upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ConfigError(f"[run].log_level invalid: {level}")
        return RunConfig(
            mode=mode,
            ledger_path=_req(d, "ledger_path", str, s),
            creds_dir=_opt(d, "creds_dir", str, "~/.fulcrum", s),
            kill_reset_file=_opt(d, "kill_reset_file", str, "~/.fulcrum/agent_kill_reset", s),
            log_level=level,
        )


@dataclass(frozen=True, slots=True)
class AssetClassConfig:
    name: str
    trading_days_per_year: int
    calendar: str
    taker_fee_bps: float
    maker_fee_bps: float
    slippage_bps: float
    min_notional_usd: float
    quote_currency: str

    @property
    def annualization(self) -> float:
        return float(self.trading_days_per_year) ** 0.5

    @staticmethod
    def parse(d: dict) -> "AssetClassConfig":
        s = "asset_class"
        _no_unknown(d, {"name", "trading_days_per_year", "calendar", "taker_fee_bps", "maker_fee_bps",
                        "slippage_bps", "min_notional_usd", "quote_currency"}, s)
        tdy = _req(d, "trading_days_per_year", int, s)
        if tdy not in (252, 365):
            raise ConfigError(f"[{s}].trading_days_per_year must be 252 or 365, got {tdy}")
        cal = _req(d, "calendar", str, s)
        if cal not in ("24x7", "us_equities"):
            raise ConfigError(f"[{s}].calendar must be '24x7' or 'us_equities'")
        taker = _req(d, "taker_fee_bps", float, s)
        maker = _req(d, "maker_fee_bps", float, s)
        slip = _req(d, "slippage_bps", float, s)
        minn = _req(d, "min_notional_usd", float, s)
        for name, v in (("taker_fee_bps", taker), ("maker_fee_bps", maker), ("slippage_bps", slip)):
            _in_range(v, 0.0, 500.0, f"[{s}].{name}")
        _in_range(minn, 0.0, 10000.0, f"[{s}].min_notional_usd", lo_inclusive=False)
        return AssetClassConfig(
            name=_req(d, "name", str, s), trading_days_per_year=tdy, calendar=cal,
            taker_fee_bps=taker, maker_fee_bps=maker, slippage_bps=slip,
            min_notional_usd=minn, quote_currency=_opt(d, "quote_currency", str, "USD", s),
        )


@dataclass(frozen=True, slots=True)
class UniverseConfig:
    core: tuple[str, ...]
    candidates: tuple[str, ...]
    max_symbols: int
    min_median_daily_volume_usd: float

    @staticmethod
    def parse(d: dict, quote: str) -> "UniverseConfig":
        s = "universe"
        _no_unknown(d, {"core", "candidates", "max_symbols", "min_median_daily_volume_usd"}, s)
        core = tuple(_req(d, "core", list, s))
        cands = tuple(_opt(d, "candidates", list, [], s))
        mx = _req(d, "max_symbols", int, s)
        if not 1 <= mx <= 8:
            raise ConfigError(f"[{s}].max_symbols must be 1..8 (spec hard cap 8), got {mx}")
        if not core:
            raise ConfigError(f"[{s}].core must list at least one symbol")
        if len(core) > mx:
            raise ConfigError(f"[{s}].core has {len(core)} symbols, more than max_symbols={mx}")
        stables = {"USDT", "USDC", "USDG", "DAI", "PYUSD", "TUSD", "BUSD", "FDUSD"}
        for sym in core + cands:
            if not isinstance(sym, str) or "/" not in sym or not sym.endswith("/" + quote):
                raise ConfigError(f"[{s}] symbol {sym!r} must look like 'BTC/{quote}'")
            if sym.split("/")[0].upper() in stables:
                raise ConfigError(f"[{s}] stablecoin {sym} is not allowed")
        if len(set(core) & set(cands)):
            raise ConfigError(f"[{s}] symbols appear in both core and candidates: {sorted(set(core) & set(cands))}")
        if len(set(core)) != len(core) or len(set(cands)) != len(cands):
            raise ConfigError(f"[{s}] duplicate symbols")
        vol = _req(d, "min_median_daily_volume_usd", float, s)
        _in_range(vol, 0.0, 1e12, f"[{s}].min_median_daily_volume_usd")
        return UniverseConfig(core=core, candidates=cands, max_symbols=mx, min_median_daily_volume_usd=vol)


@dataclass(frozen=True, slots=True)
class SignalConfig:
    lookbacks_days: tuple[int, ...]
    trend_filter_ma_days: int
    vol_window_days: int
    atr_window_days: int

    @property
    def min_history_days(self) -> int:
        return max(max(self.lookbacks_days), self.trend_filter_ma_days, self.vol_window_days + 1,
                   self.atr_window_days + 1)

    @staticmethod
    def parse(d: dict) -> "SignalConfig":
        s = "signal"
        _no_unknown(d, {"lookbacks_days", "trend_filter_ma_days", "vol_window_days", "atr_window_days"}, s)
        lbs = tuple(_req(d, "lookbacks_days", list, s))
        if not lbs or any((not isinstance(x, int)) or x < 1 or x > 365 for x in lbs) or len(set(lbs)) != len(lbs):
            raise ConfigError(f"[{s}].lookbacks_days must be distinct ints in 1..365")
        ma = _req(d, "trend_filter_ma_days", int, s)
        vw = _req(d, "vol_window_days", int, s)
        aw = _req(d, "atr_window_days", int, s)
        for name, v in (("trend_filter_ma_days", ma), ("vol_window_days", vw), ("atr_window_days", aw)):
            if not 2 <= v <= 400:
                raise ConfigError(f"[{s}].{name} must be 2..400, got {v}")
        return SignalConfig(lookbacks_days=lbs, trend_filter_ma_days=ma, vol_window_days=vw, atr_window_days=aw)


@dataclass(frozen=True, slots=True)
class SizingConfig:
    portfolio_vol_target: float
    max_weight_per_asset: float
    max_gross_exposure: float
    no_trade_band_pct_equity: float

    @staticmethod
    def parse(d: dict) -> "SizingConfig":
        s = "sizing"
        _no_unknown(d, {"portfolio_vol_target", "max_weight_per_asset", "max_gross_exposure",
                        "no_trade_band_pct_equity"}, s)
        pvt = _req(d, "portfolio_vol_target", float, s)
        mwa = _req(d, "max_weight_per_asset", float, s)
        mge = _req(d, "max_gross_exposure", float, s)
        band = _req(d, "no_trade_band_pct_equity", float, s)
        _in_range(pvt, 0.0, 2.0, f"[{s}].portfolio_vol_target", lo_inclusive=False)
        _in_range(mwa, 0.0, 1.0, f"[{s}].max_weight_per_asset", lo_inclusive=False)
        _in_range(mge, 0.0, 1.0, f"[{s}].max_gross_exposure", lo_inclusive=False)
        _in_range(band, 0.0, 0.5, f"[{s}].no_trade_band_pct_equity")
        if mwa > mge:
            raise ConfigError(f"[{s}].max_weight_per_asset ({mwa}) exceeds max_gross_exposure ({mge})")
        return SizingConfig(pvt, mwa, mge, band)


@dataclass(frozen=True, slots=True)
class RiskConfig:
    max_order_pct_equity: float
    max_trades_per_24h: int
    stop_atr_multiple: float
    kill_switch_drawdown: float
    fee_budget_annual_pct_equity: float
    slippage_alarm_multiple: float
    stale_bar_max_age_minutes: int

    @staticmethod
    def parse(d: dict) -> "RiskConfig":
        s = "risk"
        _no_unknown(d, {"max_order_pct_equity", "max_trades_per_24h", "stop_atr_multiple", "kill_switch_drawdown",
                        "fee_budget_annual_pct_equity", "slippage_alarm_multiple", "stale_bar_max_age_minutes"}, s)
        mo = _req(d, "max_order_pct_equity", float, s)
        mt = _req(d, "max_trades_per_24h", int, s)
        sam = _req(d, "stop_atr_multiple", float, s)
        ksd = _req(d, "kill_switch_drawdown", float, s)
        fee = _req(d, "fee_budget_annual_pct_equity", float, s)
        sl = _req(d, "slippage_alarm_multiple", float, s)
        stale = _req(d, "stale_bar_max_age_minutes", int, s)
        _in_range(mo, 0.0, 0.25, f"[{s}].max_order_pct_equity", lo_inclusive=False)
        if not 1 <= mt <= 100:
            raise ConfigError(f"[{s}].max_trades_per_24h must be 1..100")
        _in_range(sam, 0.5, 10.0, f"[{s}].stop_atr_multiple")
        _in_range(ksd, 0.01, 0.5, f"[{s}].kill_switch_drawdown")
        _in_range(fee, 0.0, 0.5, f"[{s}].fee_budget_annual_pct_equity", lo_inclusive=False)
        _in_range(sl, 1.0, 20.0, f"[{s}].slippage_alarm_multiple")
        if not 1 <= stale <= 7 * 24 * 60:
            raise ConfigError(f"[{s}].stale_bar_max_age_minutes must be 1..10080")
        return RiskConfig(mo, mt, sam, ksd, fee, sl, stale)


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    rebalance_weekday: int  # 0 = monday
    rebalance_hour_utc: int
    tick_minutes: int

    @staticmethod
    def parse(d: dict) -> "ScheduleConfig":
        s = "schedule"
        _no_unknown(d, {"rebalance_weekday", "rebalance_hour_utc", "tick_minutes"}, s)
        wd = _req(d, "rebalance_weekday", str, s).lower()
        if wd not in WEEKDAYS:
            raise ConfigError(f"[{s}].rebalance_weekday must be one of {WEEKDAYS}")
        hr = _req(d, "rebalance_hour_utc", int, s)
        tick = _req(d, "tick_minutes", int, s)
        if not 0 <= hr <= 23:
            raise ConfigError(f"[{s}].rebalance_hour_utc must be 0..23")
        if not 1 <= tick <= 240:
            raise ConfigError(f"[{s}].tick_minutes must be 1..240")
        return ScheduleConfig(WEEKDAYS.index(wd), hr, tick)


@dataclass(frozen=True, slots=True)
class FilterConfig:
    mode: str
    model: str
    timeout_seconds: float
    max_tokens: int
    shadow_daily_hour_utc: int
    effort: str = "high"

    @staticmethod
    def parse(d: dict) -> "FilterConfig":
        s = "llm_filter"
        _no_unknown(d, {"mode", "model", "timeout_seconds", "max_tokens", "shadow_daily_hour_utc", "effort"}, s)
        effort = _opt(d, "effort", str, "high", s)
        if effort not in ("low", "medium", "high", "xhigh", "max"):
            raise ConfigError(f"[{s}].effort must be low|medium|high|xhigh|max")
        mode = _req(d, "mode", str, s)
        if mode not in ("off", "shadow", "active"):
            raise ConfigError(f"[{s}].mode must be off|shadow|active")
        model = _req(d, "model", str, s)
        if mode != "off" and (not model or model.startswith("REPLACE")):
            raise ConfigError(f"[{s}].model must be set when mode is {mode!r}")
        to = _req(d, "timeout_seconds", float, s)
        mt = _req(d, "max_tokens", int, s)
        hr = _opt(d, "shadow_daily_hour_utc", int, 2, s)
        _in_range(to, 1.0, 300.0, f"[{s}].timeout_seconds")
        if not 64 <= mt <= 8192:
            raise ConfigError(f"[{s}].max_tokens must be 64..8192")
        if not 0 <= hr <= 23:
            raise ConfigError(f"[{s}].shadow_daily_hour_utc must be 0..23")
        return FilterConfig(mode, model, to, mt, hr, effort)


@dataclass(frozen=True, slots=True)
class NotifyConfig:
    enabled: bool

    @staticmethod
    def parse(d: dict) -> "NotifyConfig":
        _no_unknown(d, {"enabled"}, "notify")
        return NotifyConfig(enabled=_opt(d, "enabled", bool, True, "notify"))


@dataclass(frozen=True, slots=True)
class DataConfig:
    cache_dir: str
    historical_source: str

    @staticmethod
    def parse(d: dict) -> "DataConfig":
        s = "data"
        _no_unknown(d, {"cache_dir", "historical_source"}, s)
        return DataConfig(cache_dir=_req(d, "cache_dir", str, s), historical_source=_req(d, "historical_source", str, s))


@dataclass(frozen=True, slots=True)
class Config:
    run: RunConfig
    asset_class: AssetClassConfig
    universe: UniverseConfig
    signal: SignalConfig
    sizing: SizingConfig
    risk: RiskConfig
    schedule: ScheduleConfig
    llm_filter: FilterConfig
    notify: NotifyConfig
    data: DataConfig
    source_path: str = ""
    config_hash: str = ""

    SECTIONS = ("run", "asset_class", "universe", "signal", "sizing", "risk", "schedule", "llm_filter", "notify", "data")

    @staticmethod
    def from_dict(raw: dict[str, Any], source_path: str = "") -> "Config":
        unknown = set(raw) - set(Config.SECTIONS)
        if unknown:
            raise ConfigError(f"unknown top-level sections: {sorted(unknown)}")
        for sec in Config.SECTIONS:
            if sec not in raw:
                raise ConfigError(f"missing section [{sec}]")
            if not isinstance(raw[sec], dict):
                raise ConfigError(f"[{sec}] must be a table")
        asset = AssetClassConfig.parse(raw["asset_class"])
        cfg = Config(
            run=RunConfig.parse(raw["run"]),
            asset_class=asset,
            universe=UniverseConfig.parse(raw["universe"], asset.quote_currency),
            signal=SignalConfig.parse(raw["signal"]),
            sizing=SizingConfig.parse(raw["sizing"]),
            risk=RiskConfig.parse(raw["risk"]),
            schedule=ScheduleConfig.parse(raw["schedule"]),
            llm_filter=FilterConfig.parse(raw["llm_filter"]),
            notify=NotifyConfig.parse(raw["notify"]),
            data=DataConfig.parse(raw["data"]),
            source_path=source_path,
            config_hash=hashlib.sha256(repr(sorted(_flatten(raw))).encode()).hexdigest()[:16],
        )
        # Cross-section invariants.
        if cfg.risk.max_order_pct_equity > cfg.sizing.max_weight_per_asset:
            raise ConfigError("[risk].max_order_pct_equity must not exceed [sizing].max_weight_per_asset")
        return cfg

    def expanded(self, p: str) -> Path:
        return Path(os.path.expandvars(os.path.expanduser(p)))


def _flatten(d: dict, prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.extend(_flatten(v, key + "."))
        else:
            out.append((key, repr(v)))
    return out


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    with open(p, "rb") as f:
        try:
            raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{p}: TOML parse error: {e}") from e
    return Config.from_dict(raw, source_path=str(p))


def example_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config.example.toml"


def field_names(cls: type) -> tuple[str, ...]:
    return tuple(f.name for f in fields(cls))

import copy

import pytest

from fulcrum.config import Config, ConfigError, example_path, load_config


def test_example_config_loads_with_filter_off(raw_config):
    cfg = Config.from_dict(raw_config)
    assert cfg.run.mode == "dry_run"
    assert cfg.asset_class.trading_days_per_year == 365
    assert cfg.universe.core == ("BTC/USD", "ETH/USD", "SOL/USD")
    assert cfg.signal.lookbacks_days == (7, 14, 21, 28)
    assert cfg.schedule.rebalance_weekday == 0
    assert cfg.config_hash and len(cfg.config_hash) == 16


def test_example_file_exists():
    assert example_path().exists()


@pytest.mark.parametrize("section,key,value,msg", [
    ("run", "mode", "live", "no live mode"),
    ("run", "mode", "LIVE", "no live mode"),
    ("run", "mode", "", "no live mode"),
    ("asset_class", "trading_days_per_year", 360, "252 or 365"),
    ("asset_class", "taker_fee_bps", -1.0, "out of range"),
    ("asset_class", "min_notional_usd", 0, "out of range"),
    ("universe", "max_symbols", 9, "hard cap 8"),
    ("universe", "max_symbols", 0, "hard cap 8"),
    ("universe", "core", [], "at least one"),
    ("universe", "core", ["USDT/USD"], "stablecoin"),
    ("universe", "core", ["BTC"], "must look like"),
    ("universe", "core", ["BTC/USD", "BTC/USD"], "duplicate"),
    ("signal", "lookbacks_days", [], "distinct ints"),
    ("signal", "lookbacks_days", [7, 7], "distinct ints"),
    ("signal", "lookbacks_days", [0], "distinct ints"),
    ("signal", "trend_filter_ma_days", 1, "2..400"),
    ("sizing", "portfolio_vol_target", 0, "out of range"),
    ("sizing", "max_weight_per_asset", 1.5, "out of range"),
    ("sizing", "max_gross_exposure", 0.1, "exceeds max_gross_exposure"),
    ("sizing", "no_trade_band_pct_equity", 0.9, "out of range"),
    ("risk", "max_order_pct_equity", 0.3, "out of range"),
    ("risk", "max_order_pct_equity", 0.2, "must not exceed"),
    ("risk", "max_trades_per_24h", 0, "1..100"),
    ("risk", "stop_atr_multiple", 0.1, "out of range"),
    ("risk", "kill_switch_drawdown", 0.9, "out of range"),
    ("risk", "stale_bar_max_age_minutes", 0, "1..10080"),
    ("schedule", "rebalance_weekday", "someday", "one of"),
    ("schedule", "rebalance_hour_utc", 24, "0..23"),
    ("schedule", "tick_minutes", 0, "1..240"),
    ("llm_filter", "mode", "on", "off|shadow|active"),
    ("llm_filter", "timeout_seconds", 0.1, "out of range"),
    ("llm_filter", "max_tokens", 10, "64..8192"),
])
def test_bad_values_fail_fast(raw_config, section, key, value, msg):
    raw = copy.deepcopy(raw_config)
    raw[section][key] = value
    with pytest.raises(ConfigError, match=msg):
        Config.from_dict(raw)


def test_filter_mode_shadow_requires_real_model(raw_config):
    raw = copy.deepcopy(raw_config)
    raw["llm_filter"]["mode"] = "shadow"
    raw["llm_filter"]["model"] = "REPLACE_WITH_MODEL_ID"
    with pytest.raises(ConfigError, match="model must be set"):
        Config.from_dict(raw)


def test_unknown_keys_rejected(raw_config):
    raw = copy.deepcopy(raw_config)
    raw["risk"]["allow_live"] = True
    with pytest.raises(ConfigError, match="unknown keys"):
        Config.from_dict(raw)
    raw = copy.deepcopy(raw_config)
    raw["live"] = {"enabled": True}
    with pytest.raises(ConfigError, match="unknown top-level"):
        Config.from_dict(raw)


def test_missing_section_rejected(raw_config):
    raw = copy.deepcopy(raw_config)
    del raw["risk"]
    with pytest.raises(ConfigError, match=r"missing section \[risk\]"):
        Config.from_dict(raw)


def test_bool_is_not_a_number(raw_config):
    raw = copy.deepcopy(raw_config)
    raw["risk"]["max_trades_per_24h"] = True
    with pytest.raises(ConfigError):
        Config.from_dict(raw)


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_load_config_bad_toml(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text("[run\nmode = ", encoding="utf-8")
    with pytest.raises(ConfigError, match="TOML parse error"):
        load_config(p)


def test_config_hash_changes_with_values(raw_config):
    a = Config.from_dict(raw_config).config_hash
    raw = copy.deepcopy(raw_config)
    raw["risk"]["kill_switch_drawdown"] = 0.14
    assert Config.from_dict(raw).config_hash != a

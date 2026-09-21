"""Shared fixtures. Every test runs offline against fakes; nothing here touches the network
or a credential. The example config is loaded with the filter set to 'off' so the model id
placeholder does not need to be filled in for tests that do not use the filter."""

from __future__ import annotations

import tomllib
from datetime import datetime, timezone

import pytest

from fulcrum.config import Config, example_path


def utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


@pytest.fixture
def raw_config() -> dict:
    with open(example_path(), "rb") as f:
        raw = tomllib.load(f)
    raw["llm_filter"]["mode"] = "off"
    raw["data"]["historical_source"] = "csv"
    return raw


@pytest.fixture
def config(raw_config, tmp_path) -> Config:
    raw_config["run"]["ledger_path"] = str(tmp_path / "ledger.sqlite")
    raw_config["run"]["creds_dir"] = str(tmp_path / "creds")
    raw_config["run"]["kill_reset_file"] = str(tmp_path / "creds" / "agent_kill_reset")
    raw_config["data"]["cache_dir"] = str(tmp_path / "bars")
    return Config.from_dict(raw_config, source_path="<test>")

import subprocess
from types import SimpleNamespace

import pytest

from fulcrum import creds, notify
from tests.conftest import utc


def test_templates_are_plain_text():
    t = notify.msg_rebalance(utc(2026, 1, 5, 1), 1234.5, ["Buy BTC/USD $100.00"], ["ETH/USD"], "paper")
    assert "Fulcrum rebalance" in t and "$1,234.50" in t and "ETH/USD" in t
    notify.msg_stop(utc(2026, 1, 5), "BTC/USD", 100.0, 99.0, 0.5)
    notify.msg_kill(utc(2026, 1, 5), 850.0, 1000.0, 0.15)
    notify.msg_alert(utc(2026, 1, 5), "stale_data", "newest bar 400 min old")


def test_forbidden_characters_rejected():
    with pytest.raises(ValueError):
        notify.check_text("bad — dash")
    with pytest.raises(ValueError):
        notify.check_text("emoji \U0001F600")
    assert notify.FakeNotifier().send("ok") is True
    assert notify.FakeNotifier(fail=True).send("ok") is False


def test_cred_paths_and_forbidden_old_files(tmp_path):
    p = creds.cred_path(str(tmp_path), "alpaca")
    assert p.name == "alpaca_agent.cred"
    with pytest.raises(creds.CredentialError):
        creds.cred_path(str(tmp_path), "xai")
    assert "alpaca.cred" in creds.FORBIDDEN_NAMES


def test_load_credential_uses_injected_runner_and_redacts(tmp_path):
    (tmp_path / "alpaca_agent.cred").write_text("<placeholder>", encoding="utf-8")

    fake_user, fake_pw = "fake-user-line", "fake-second-line"

    def fake_runner(cmd, **kw):
        assert cmd[0] == "powershell.exe" and "Import-Clixml" in cmd[-1]
        return SimpleNamespace(stdout=f"{fake_user}\n{fake_pw}\n", returncode=0)

    c = creds.load_credential(str(tmp_path), "alpaca", runner=fake_runner)
    assert (c.user, getattr(c, "secret")) == (fake_user, fake_pw)
    assert fake_pw not in repr(c) and fake_pw not in str(c)


def test_load_credential_missing_file(tmp_path):
    with pytest.raises(creds.CredentialError, match="missing"):
        creds.load_credential(str(tmp_path), "telegram", runner=lambda *a, **k: None)


def test_load_credential_decrypt_failure_does_not_leak_stderr(tmp_path):
    (tmp_path / "anthropic_agent.cred").write_text("x", encoding="utf-8")

    def failing(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd, stderr="secret-looking text")

    with pytest.raises(creds.CredentialError) as ei:
        creds.load_credential(str(tmp_path), "anthropic", runner=failing)
    assert "secret-looking" not in str(ei.value)

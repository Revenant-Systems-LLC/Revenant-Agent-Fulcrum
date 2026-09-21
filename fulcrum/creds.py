"""Credential loading. DPAPI-encrypted PSCredential files under ~/.fulcrum, decrypted at runtime
through PowerShell ``Import-Clixml`` (same pattern as the previous Fulcrum's MCP server).

Rules enforced here:
* Values are returned to the caller and never logged, printed, or written anywhere.
* File names are fixed: alpaca_agent.cred, anthropic_agent.cred, telegram_agent.cred.
  The old bot's ``alpaca.cred`` is never read; a different name is the firewall.
* This module never creates or modifies a .cred file. Dave does that by hand (see docs/HANDOFF.md).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

CRED_FILES = {
    "alpaca": "alpaca_agent.cred",      # UserName = API key id, Password = secret key
    "anthropic": "anthropic_agent.cred",  # UserName = "anthropic", Password = API key
    "telegram": "telegram_agent.cred",  # UserName = chat id, Password = bot token
}
FORBIDDEN_NAMES = {"alpaca.cred", "xai.cred", "telegram.cred", "coingecko.cred"}  # the old bot's files
FORBIDDEN_DIRS = {".openclaw", ".hermes"}  # the old bot's homes; a creds_dir inside them is refused


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Credential:
    user: str
    secret: str

    def __repr__(self) -> str:  # never leak in tracebacks or logs
        return f"Credential(user={'<set>' if self.user else '<empty>'}, secret=<redacted>)"

    __str__ = __repr__


def cred_path(creds_dir: str, service: str) -> Path:
    if service not in CRED_FILES:
        raise CredentialError(f"unknown service {service!r}")
    name = CRED_FILES[service]
    if name in FORBIDDEN_NAMES:
        raise CredentialError("refusing to read the old bot's credential file")
    # Directory firewall (found by the security review): the config value must not walk out of
    # the Fulcrum credential folder or into the old bot's home. File-name firewall alone is not enough.
    raw = Path(creds_dir)
    if ".." in raw.parts:
        raise CredentialError("creds_dir must not contain '..'")
    root = Path(os.path.expandvars(os.path.expanduser(creds_dir))).resolve()
    if any(part.lower() in FORBIDDEN_DIRS for part in root.parts):  # never read the old bot's home
        raise CredentialError("creds_dir must not point into the old bot's folders")
    return root / name


def load_credential(creds_dir: str, service: str, *, runner=subprocess.run) -> Credential:
    """Decrypt one .cred file. ``runner`` is injectable so tests never spawn PowerShell."""
    path = cred_path(creds_dir, service)
    if not path.exists():
        raise CredentialError(f"credential file missing: {path}. See docs/HANDOFF.md to create it.")
    # Path is quoted for PowerShell; single quotes inside the path would break it, so refuse them.
    if "'" in str(path):
        raise CredentialError("credential path must not contain a single quote")
    script = (f"$c = Import-Clixml -LiteralPath '{path}'; "
              "[Console]::Out.Write($c.UserName + \"`n\" + $c.GetNetworkCredential().Password)")
    try:
        result = runner(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                        capture_output=True, text=True, timeout=20, check=True)
    except subprocess.CalledProcessError as e:
        raise CredentialError(f"DPAPI decrypt failed for {service} (exit {e.returncode}); stderr suppressed") from None
    except (subprocess.TimeoutExpired, OSError) as e:
        raise CredentialError(f"DPAPI decrypt failed for {service}: {type(e).__name__}") from None
    lines = result.stdout.split("\n")
    if len(lines) < 2 or not lines[1].strip():
        raise CredentialError(f"credential file for {service} decrypted to an unexpected shape")
    return Credential(user=lines[0].strip(), secret=lines[1].strip())

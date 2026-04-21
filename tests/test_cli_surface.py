from __future__ import annotations

import io
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python <3.11 test env fallback
    import tomli as tomllib
from contextlib import redirect_stdout
from pathlib import Path

from claude_act.cli import main as cli_main


def _strip_ansi(text: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_console_scripts_include_all_public_launchers():
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    scripts = project["project"]["scripts"]

    assert scripts["claude-act"] == "claude_act.cli.main:main"
    assert scripts["actl"] == "claude_act.cli.main:main"
    assert scripts["act-ledger"] == "claude_act.cli.main:main"


def test_help_screen_uses_act_ledger_product_language_and_public_commands():
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_main._print_home_screen()

    screen = _strip_ansi(buf.getvalue())
    assert "ACT Ledger" in screen
    assert "actl init" in screen
    assert "actl stats" in screen
    assert "actl list" in screen
    assert "actl read <session_id>" in screen
    assert "actl dump <session_id>" in screen
    assert "actl doctor" in screen


def test_dump_is_public_and_export_remains_compatibility_alias():
    assert "dump" in cli_main.COMMANDS
    assert "export" in cli_main.COMMANDS
    assert "dump" in cli_main.VISIBLE_COMMANDS
    assert "export" not in cli_main.VISIBLE_COMMANDS

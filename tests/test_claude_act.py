"""
Tests for claude-act v0.1.0
"""
from __future__ import annotations

import json
import tomllib
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_act.pricing import calculate_cost, get_rates
from claude_act.storage import ArchivistStorage
from claude_act import config as _config


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def tmp_storage(tmp_path):
    s = ArchivistStorage(root=tmp_path / "claude-act")
    s.init()
    return s


# ── Pricing ───────────────────────────────────────────────────────────

def test_known_model_exact_match():
    rates, exact = get_rates("claude-sonnet-4-6")
    assert exact is True
    assert rates["input"] == 3.00

def test_unknown_model_falls_back():
    rates, exact = get_rates("claude-unknown-99")
    assert exact is False

def test_none_model_does_not_crash():
    cost = calculate_cost(None, 5, 3)
    assert cost["exact_match"] is False
    assert cost["amount_usd"] >= 0

def test_empty_model_does_not_crash():
    cost = calculate_cost("", 5, 3)
    assert cost["exact_match"] is False

def test_cost_record_has_provenance():
    cost = calculate_cost("claude-sonnet-4-6", 1000, 500)
    assert "pricing_version" in cost
    assert "rates_used" in cost
    assert cost["exact_match"] is True

def test_cost_zero_tokens():
    cost = calculate_cost("claude-sonnet-4-6", 0, 0)
    assert cost["amount_usd"] == 0.0

def test_cost_calculation_correct():
    cost = calculate_cost("claude-sonnet-4-6", 1_000_000, 1_000_000)
    assert abs(cost["amount_usd"] - 18.0) < 0.0001


# ── Storage ───────────────────────────────────────────────────────────

def test_init_creates_structure(tmp_storage):
    assert tmp_storage.root.exists()
    assert tmp_storage.sessions_dir.exists()
    assert tmp_storage.index_path.exists()

def test_init_is_idempotent(tmp_storage):
    tmp_storage.init()
    tmp_storage.init()
    assert tmp_storage.index_path.exists()

def test_register_and_read_session(tmp_storage):
    tmp_storage.register_session("sid1", "2026-04-14")
    sessions = tmp_storage.list_sessions()
    assert any(s["session_id"] == "sid1" for s in sessions)

def test_append_and_read_call(tmp_storage):
    tmp_storage.register_session("sid2", "2026-04-14")
    record = {"call_id": str(uuid.uuid4()), "model": "claude-sonnet-4-6",
              "input_tokens": 10, "output_tokens": 5}
    tmp_storage.append_call("sid2", "2026-04-14", record)
    records = tmp_storage.read_session("sid2")
    assert len(records) == 1
    assert records[0]["model"] == "claude-sonnet-4-6"

def test_update_session_stats(tmp_storage):
    tmp_storage.register_session("sid3", "2026-04-14")
    tmp_storage.update_session_stats("sid3", 0.005)
    tmp_storage.update_session_stats("sid3", 0.003)
    sessions = tmp_storage.list_sessions()
    s = next(x for x in sessions if x["session_id"] == "sid3")
    assert s["call_count"] == 2
    assert s["error_count"] == 0
    assert abs(s["total_cost_usd"] - 0.008) < 1e-9

def test_update_session_stats_counts_errors(tmp_storage):
    tmp_storage.register_session("sid4", "2026-04-14")
    tmp_storage.update_session_stats("sid4", 0.0, error=True)
    tmp_storage.update_session_stats("sid4", 0.002, error=False)
    sessions = tmp_storage.list_sessions()
    s = next(x for x in sessions if x["session_id"] == "sid4")
    assert s["call_count"] == 2
    assert s["error_count"] == 1
    assert abs(s["total_cost_usd"] - 0.002) < 1e-9

def test_storage_permissions_hardened(tmp_path):
    import os

    storage = ArchivistStorage(root=tmp_path / "claude-act")
    storage.init()
    storage.register_session("sidp", "2026-04-14")
    storage.append_call("sidp", "2026-04-14", {"call_id": "x"})
    storage.update_session_stats("sidp", 0.001)
    storage._log_error("test", "boom")

    session_path = storage.session_path("sidp", "2026-04-14")

    assert oct(os.stat(storage.root).st_mode & 0o777) == oct(0o700)
    assert oct(os.stat(storage.sessions_dir).st_mode & 0o777) == oct(0o700)
    assert oct(os.stat(storage.index_path).st_mode & 0o777) == oct(0o600)
    assert oct(os.stat(session_path).st_mode & 0o777) == oct(0o600)
    assert oct(os.stat(storage.errors_path).st_mode & 0o777) == oct(0o600)

def test_read_missing_session_raises(tmp_storage):
    with pytest.raises(FileNotFoundError):
        tmp_storage.read_session("ghost")

def test_storage_failure_never_raises(tmp_storage):
    tmp_storage.index_path.write_text("not json")
    tmp_storage.register_session("x", "2026-04-14")
    tmp_storage.update_session_stats("x", 1.0)
    sessions = tmp_storage.list_sessions()
    assert sessions == []

def test_append_to_missing_dir_logs_not_raises(tmp_path):
    s = ArchivistStorage(root=tmp_path / "ghost")
    s.append_call("sid", "2026-04-14", {"call_id": "x"})

def test_aggregate_stats_empty(tmp_storage):
    stats = tmp_storage.aggregate_stats()
    assert stats["total_sessions"] == 0
    assert stats["total_cost_usd"] == 0.0

def test_aggregate_stats_with_data(tmp_storage):
    for i in range(3):
        sid = f"sid{i}"
        tmp_storage.register_session(sid, "2026-04-14")
        tmp_storage.update_session_stats(sid, 0.01)
    stats = tmp_storage.aggregate_stats()
    assert stats["total_sessions"] == 3
    assert abs(stats["total_cost_usd"] - 0.03) < 1e-9

def test_compact_generation_is_deterministic_and_preserves_sections(tmp_storage):
    from claude_act.compaction import ensure_compact

    tmp_storage.register_session("sidc", "2026-04-14")
    record1 = {
        "call_id": "call-1",
        "session_id": "sidc",
        "timestamp": "2026-04-14T01:00:00Z",
        "model": "claude-sonnet-4-6",
        "input_messages": [{"role": "user", "content": "Outline the plan."}],
        "input_tokens": 100,
        "output_tokens": 40,
        "cost": {"amount_usd": 0.0009},
        "output": [{"type": "text", "text": "Plan"}],
        "error": None,
    }
    record2 = {
        "call_id": "call-2",
        "session_id": "sidc",
        "timestamp": "2026-04-14T01:05:00Z",
        "model": "claude-opus-4-6",
        "input_messages": [{"role": "user", "content": "Continue with implementation details."}],
        "input_tokens": 200,
        "output_tokens": 80,
        "cost": {"amount_usd": 0.0035},
        "output": [{"type": "text", "text": "Implementation details"}],
        "warnings": [{
            "kind": "cost_spike",
            "timestamp": "2026-04-14T01:05:00Z",
            "latest_cost_usd": 0.0035,
            "trailing_avg_cost_usd": 0.0009,
        }],
        "error": None,
    }
    tmp_storage.append_call("sidc", "2026-04-14", record1)
    tmp_storage.update_session_stats("sidc", 0.0009)
    tmp_storage.append_call("sidc", "2026-04-14", record2)
    tmp_storage.update_session_stats("sidc", 0.0035)

    path, content = ensure_compact(tmp_storage, "sidc")
    assert path.endswith("sidc.compact.md")
    assert "session_id: sidc" in content
    assert "call_count: 2" in content
    assert "models: claude-opus-4-6 x1, claude-sonnet-4-6 x1" in content
    assert "spike_events: 1" in content
    assert "## State" in content

    edited = content.replace("## State\n", "## State\nCurrent state here.\n", 1)
    tmp_storage.write_compact("sidc", edited)
    _, refreshed = ensure_compact(tmp_storage, "sidc")
    assert "Current state here." in refreshed


# ── Config ────────────────────────────────────────────────────────────

def test_config_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(_config, "CONFIG_PATH", tmp_path / "config.json")
    cfg = _config.load()
    assert cfg["default_model"] == "claude-sonnet-4-6"
    assert cfg["api_key"] is None

def test_config_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(_config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(_config, "CONFIG_DIR",  tmp_path)
    _config.save({"api_key": "sk-test", "default_model": "claude-opus-4-6"})
    cfg = _config.load()
    assert cfg["api_key"] == "sk-test"
    assert cfg["default_model"] == "claude-opus-4-6"

def test_config_get_api_key_env_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(_config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env-test")
    key = _config.get_api_key()
    assert key == "sk-env-test"

def test_config_set_value_rejects_invalid_default_model(tmp_path, monkeypatch):
    monkeypatch.setattr(_config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(_config, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError, match="Invalid default_model"):
        _config.set_value("default_model", "claude-act doctor")

def test_get_default_model_rejects_invalid_saved_value(tmp_path, monkeypatch):
    monkeypatch.setattr(_config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(_config, "CONFIG_DIR", tmp_path)
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "config.json").write_text(
        json.dumps({
            "api_key": "sk-test",
            "default_model": "claude-act doctor",
            "storage_root": str(tmp_path / "sessions"),
        }),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Invalid default_model"):
        _config.get_default_model()

def test_get_storage_root_normalizes_legacy_sessions_path(tmp_path, monkeypatch):
    monkeypatch.setattr(_config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(_config, "CONFIG_DIR", tmp_path)
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "config.json").write_text(
        json.dumps({
            "api_key": "sk-test",
            "default_model": "claude-sonnet-4-6",
            "storage_root": str(tmp_path / "sessions"),
        }),
        encoding="utf-8",
    )

    assert _config.get_storage_root() == tmp_path
    assert _config.load()["storage_root"] == str(tmp_path)


# ── Model validation ─────────────────────────────────────────────────

def test_valid_models_accepted():
    from claude_act.config import is_valid_model, ALLOWED_MODELS
    for m in ALLOWED_MODELS:
        assert is_valid_model(m), f"{m} should be valid"

def test_invalid_model_rejected():
    from claude_act.config import is_valid_model
    assert not is_valid_model("claude-act")
    assert not is_valid_model("gpt-4")
    assert not is_valid_model("")
    assert not is_valid_model(None)

def test_config_permissions(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr("claude_act.config.CONFIG_DIR",  tmp_path)
    monkeypatch.setattr("claude_act.config.CONFIG_PATH", tmp_path / "config.json")
    from claude_act import config as _config
    _config.save({"api_key": "sk-test", "default_model": "claude-sonnet-4-6"})
    cfg_path = tmp_path / "config.json"
    assert cfg_path.exists()
    mode = oct(os.stat(cfg_path).st_mode & 0o777)
    assert mode == oct(0o600), f"Expected 0600 got {mode}"


# ── CLI ───────────────────────────────────────────────────────────────

def test_cli_home_and_help_render_same_screen(monkeypatch, capsys):
    from claude_act.cli.main import main

    monkeypatch.setattr("sys.argv", ["actl"])
    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    home_out = capsys.readouterr().out

    monkeypatch.setattr("sys.argv", ["actl", "help"])
    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    help_out = capsys.readouterr().out

    assert home_out == help_out
    assert "ACT Ledger" in home_out
    assert "What this does" in home_out
    assert "First run" in home_out
    assert "actl init" in home_out
    assert "actl stats" in home_out
    assert "actl list" in home_out
    assert "actl read <session_id>" in home_out
    assert "actl dump <session_id>" in home_out
    assert "actl doctor" in home_out
    assert "Current provider support: Claude API" in home_out
    assert "doctor" in home_out
    assert "ask" not in home_out
    assert "compact" not in home_out
    assert "resume" not in home_out
    assert "viewer" not in home_out
    assert "export <session_id>" not in home_out

def test_pyproject_publishes_actl_entrypoints():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]

    assert scripts["actl"] == "claude_act.cli.main:main"
    assert scripts["act-ledger"] == "claude_act.cli.main:main"
    assert scripts["claude-act"] == "claude_act.cli.main:main"

def test_cli_init_friendly_output(tmp_path, monkeypatch, capsys):
    from claude_act.cli.main import main

    config_dir = tmp_path / ".act-ledger"
    config_path = config_dir / "config.json"

    monkeypatch.setattr(_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(_config, "CONFIG_PATH", config_path)
    monkeypatch.setattr("sys.argv", ["actl", "init", "--key", "sk-ant-test"])

    main()

    out = capsys.readouterr().out
    assert "Setup complete." in out
    assert str(config_path) in out or "~/.act-ledger/config.json" in out
    assert str(config_dir / "sessions") in out or "~/.act-ledger/sessions" in out
    assert "actl stats" in out
    assert "actl list" in out
    assert "actl help" in out

def test_cli_list_empty_state(tmp_path, monkeypatch, capsys):
    from claude_act.cli.main import main

    config_dir = tmp_path / ".claude-act"
    config_path = config_dir / "config.json"

    monkeypatch.setattr(_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(_config, "CONFIG_PATH", config_path)

    _config.save({
        "api_key": "sk-test",
        "default_model": "claude-sonnet-4-6",
        "storage_root": str(config_dir),
    })

    storage = ArchivistStorage(root=config_dir)
    storage.init()

    monkeypatch.setattr("sys.argv", ["actl", "list"])
    main()

    out = capsys.readouterr().out
    assert "No saved sessions yet." in out
    assert "Python wrapper" in out

def test_cli_stats_empty_state(tmp_path, monkeypatch, capsys):
    from claude_act.cli.main import main

    config_dir = tmp_path / ".claude-act"
    config_path = config_dir / "config.json"

    monkeypatch.setattr(_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(_config, "CONFIG_PATH", config_path)

    _config.save({
        "api_key": "sk-test",
        "default_model": "claude-sonnet-4-6",
        "storage_root": str(config_dir),
    })

    storage = ArchivistStorage(root=config_dir)
    storage.init()

    monkeypatch.setattr("sys.argv", ["actl", "stats"])
    main()

    out = capsys.readouterr().out
    assert "No usage data yet." in out
    assert "captures at least one saved session" in out

def test_cli_read_missing_session_suggests_list(tmp_path, monkeypatch, capsys):
    from claude_act.cli.main import main

    config_dir = tmp_path / ".claude-act"
    config_path = config_dir / "config.json"

    monkeypatch.setattr(_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(_config, "CONFIG_PATH", config_path)

    _config.save({
        "api_key": "sk-test",
        "default_model": "claude-sonnet-4-6",
        "storage_root": str(config_dir),
    })

    storage = ArchivistStorage(root=config_dir)
    storage.init()

    monkeypatch.setattr("sys.argv", ["actl", "read", "ghost"])
    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "Session not found: ghost" in err
    assert "actl list" in err

def test_cli_doctor_reports_status(tmp_path, monkeypatch, capsys):
    from claude_act.cli.main import cmd_doctor
    from claude_act.storage import ArchivistStorage

    config_dir = tmp_path / ".claude-act"
    config_path = config_dir / "config.json"
    sessions_dir = config_dir / "sessions"

    monkeypatch.setattr(_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(_config, "CONFIG_PATH", config_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    _config.save({
        "api_key": "sk-test",
        "default_model": "claude-sonnet-4-6",
        "storage_root": str(sessions_dir),
    })

    storage = ArchivistStorage(root=config_dir)
    storage.init()
    storage.register_session("sid1", "2026-04-14")

    cmd_doctor([])

    out = capsys.readouterr().out
    assert str(config_path) in out
    assert "yes" in out
    assert "claude-sonnet-4-6" in out
    assert str(config_dir) in out
    assert str(sessions_dir) in out
    assert "1" in out

def test_cli_export_writes_jsonl(tmp_path, monkeypatch):
    from claude_act.cli.main import main

    config_dir = tmp_path / ".claude-act"
    config_path = config_dir / "config.json"

    monkeypatch.setattr(_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(_config, "CONFIG_PATH", config_path)

    _config.save({
        "api_key": "sk-test",
        "default_model": "claude-sonnet-4-6",
        "storage_root": str(config_dir),
    })

    storage = ArchivistStorage(root=config_dir)
    storage.init()
    storage.register_session("sid-export", "2026-04-14")
    storage.append_call(
        "sid-export",
        "2026-04-14",
        {
            "call_id": "call-export",
            "session_id": "sid-export",
            "timestamp": "2026-04-14T04:00:00Z",
            "model": "claude-sonnet-4-6",
            "input_messages": [{"role": "user", "content": "Export me."}],
            "input_tokens": 12,
            "output_tokens": 6,
            "cost": {"amount_usd": 0.000123},
            "output": [{"type": "text", "text": "done"}],
            "error": None,
        },
    )
    storage.update_session_stats("sid-export", 0.000123)

    out_file = tmp_path / "session.jsonl"
    monkeypatch.setattr(
        "sys.argv",
        ["claude-act", "export", "sid-export", str(out_file)],
    )
    main()

    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    exported = json.loads(lines[0])
    assert exported["session_id"] == "sid-export"
    assert exported["output"][0]["text"] == "done"

def test_cli_dump_writes_default_jsonl_and_reports_path(tmp_path, monkeypatch, capsys):
    from claude_act.cli.main import main

    config_dir = tmp_path / ".claude-act"
    config_path = config_dir / "config.json"

    monkeypatch.setattr(_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(_config, "CONFIG_PATH", config_path)

    _config.save({
        "api_key": "sk-test",
        "default_model": "claude-sonnet-4-6",
        "storage_root": str(config_dir),
    })

    storage = ArchivistStorage(root=config_dir)
    storage.init()
    storage.register_session("sid-dump", "2026-04-14")
    storage.append_call(
        "sid-dump",
        "2026-04-14",
        {
            "call_id": "call-dump",
            "session_id": "sid-dump",
            "timestamp": "2026-04-14T04:00:00Z",
            "model": "claude-sonnet-4-6",
            "input_messages": [{"role": "user", "content": "Dump me."}],
            "input_tokens": 12,
            "output_tokens": 6,
            "cost": {"amount_usd": 0.000123},
            "output": [{"type": "text", "text": "done"}],
            "error": None,
        },
    )
    storage.update_session_stats("sid-dump", 0.000123)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["actl", "dump", "sid-dump"])
    main()

    out_file = tmp_path / "sid-dump.jsonl"
    out = capsys.readouterr().out
    assert out_file.exists()
    assert str(out_file.resolve()) in out
    exported = json.loads(out_file.read_text(encoding="utf-8").strip())
    assert exported["session_id"] == "sid-dump"

def test_cli_ask_prints_response_and_logs_session(tmp_path, monkeypatch, capsys):
    pytest.importorskip("anthropic")
    from claude_act.cli.main import main

    config_dir = tmp_path / ".claude-act"
    config_path = config_dir / "config.json"
    sessions_dir = config_dir / "sessions"

    monkeypatch.setattr(_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(_config, "CONFIG_PATH", config_path)

    _config.save({
        "api_key": "sk-test",
        "default_model": "claude-sonnet-4-6",
        "storage_root": str(sessions_dir),
    })

    mock_usage = MagicMock()
    mock_usage.input_tokens = 24
    mock_usage.output_tokens = 12
    mock_response = MagicMock()
    mock_response.model = "claude-sonnet-4-6"
    mock_response.usage = mock_usage
    mock_response.content = [
        MagicMock(
            type="text",
            text="Recursion is a function calling itself.\nA base case stops it.",
        )
    ]

    with patch("anthropic.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        MockAnthropic.return_value = mock_client

        monkeypatch.setattr(
            "sys.argv",
            ["claude-act", "ask", "Explain recursion in 2 short sentences."],
        )
        main()

    out = capsys.readouterr().out
    assert "Recursion is a function calling itself." in out
    assert "A base case stops it." in out
    mock_client.messages.create.assert_called_once_with(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": "Explain recursion in 2 short sentences.",
        }],
    )

    storage = ArchivistStorage(root=config_dir)
    sessions = storage.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["call_count"] == 1

    records = storage.read_session(sessions[0]["session_id"])
    assert len(records) == 1
    assert records[0]["model"] == "claude-sonnet-4-6"
    assert records[0]["output"][0]["text"] == (
        "Recursion is a function calling itself.\nA base case stops it."
    )

def test_cli_ask_model_override_ignores_invalid_saved_default(tmp_path, monkeypatch, capsys):
    pytest.importorskip("anthropic")
    from claude_act.cli.main import main

    config_dir = tmp_path / ".claude-act"
    config_path = config_dir / "config.json"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({
            "api_key": "sk-test",
            "default_model": "claude-act doctor",
            "storage_root": str(config_dir / "sessions"),
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(_config, "CONFIG_PATH", config_path)

    mock_usage = MagicMock()
    mock_usage.input_tokens = 10
    mock_usage.output_tokens = 5
    mock_response = MagicMock()
    mock_response.model = "claude-haiku-4-5"
    mock_response.usage = mock_usage
    mock_response.content = [MagicMock(type="text", text="Short answer.")]

    with patch("anthropic.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        MockAnthropic.return_value = mock_client

        monkeypatch.setattr(
            "sys.argv",
            [
                "claude-act",
                "ask",
                "--model",
                "claude-haiku-4-5",
                "Say hi.",
            ],
        )
        main()

    out = capsys.readouterr().out
    assert "Short answer." in out
    mock_client.messages.create.assert_called_once_with(
        model="claude-haiku-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": "Say hi."}],
    )

def test_cli_ask_uses_env_api_key_without_init(tmp_path, monkeypatch, capsys):
    pytest.importorskip("anthropic")
    from claude_act.cli.main import main

    config_dir = tmp_path / ".claude-act"
    config_path = config_dir / "config.json"
    sessions_dir = config_dir / "sessions"

    monkeypatch.setattr(_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(_config, "CONFIG_PATH", config_path)
    monkeypatch.setitem(_config.DEFAULTS, "storage_root", str(sessions_dir))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env-test")

    mock_usage = MagicMock()
    mock_usage.input_tokens = 8
    mock_usage.output_tokens = 4
    mock_response = MagicMock()
    mock_response.model = "claude-sonnet-4-6"
    mock_response.usage = mock_usage
    mock_response.content = [MagicMock(type="text", text="Env key works.")]

    with patch("anthropic.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        MockAnthropic.return_value = mock_client

        monkeypatch.setattr(
            "sys.argv",
            ["claude-act", "ask", "Test env key fallback."],
        )
        main()

    out = capsys.readouterr().out
    assert "Env key works." in out
    MockAnthropic.assert_called_once_with(api_key="sk-env-test")

    storage = ArchivistStorage(root=config_dir)
    sessions = storage.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["call_count"] == 1
    assert sessions[0]["error_count"] == 0

def test_cli_list_works_with_storage_without_saved_config(tmp_path, monkeypatch, capsys):
    from claude_act.cli.main import main

    config_dir = tmp_path / ".claude-act"
    config_path = config_dir / "config.json"
    sessions_dir = config_dir / "sessions"

    monkeypatch.setattr(_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(_config, "CONFIG_PATH", config_path)
    monkeypatch.setitem(_config.DEFAULTS, "storage_root", str(sessions_dir))

    storage = ArchivistStorage(root=config_dir)
    storage.init()
    storage.register_session("sid-env", "2026-04-14")
    storage.update_session_stats("sid-env", 0.001)

    monkeypatch.setattr("sys.argv", ["claude-act", "list"])
    main()

    out = capsys.readouterr().out
    assert "sid-env" in out

def test_cli_compact_and_resume_context(tmp_path, monkeypatch, capsys):
    from claude_act.cli.main import main

    config_dir = tmp_path / ".claude-act"
    config_path = config_dir / "config.json"
    sessions_dir = config_dir / "sessions"

    monkeypatch.setattr(_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(_config, "CONFIG_PATH", config_path)

    _config.save({
        "api_key": "sk-test",
        "default_model": "claude-sonnet-4-6",
        "storage_root": str(sessions_dir),
    })

    storage = ArchivistStorage(root=config_dir)
    storage.init()
    storage.register_session("sid-compact", "2026-04-14")
    storage.append_call(
        "sid-compact",
        "2026-04-14",
        {
            "call_id": "call-compact",
            "session_id": "sid-compact",
            "timestamp": "2026-04-14T02:00:00Z",
            "model": "claude-sonnet-4-6",
            "input_messages": [{"role": "user", "content": "Summarize progress."}],
            "input_tokens": 50,
            "output_tokens": 20,
            "cost": {"amount_usd": 0.00045},
            "output": [{"type": "text", "text": "Progress"}],
            "error": None,
        },
    )
    storage.update_session_stats("sid-compact", 0.00045)

    monkeypatch.setattr("sys.argv", ["claude-act", "compact", "sid-compact"])
    main()
    assert "Wrote compact" in capsys.readouterr().out

    monkeypatch.setattr("sys.argv", ["claude-act", "resume", "sid-compact"])
    main()
    out = capsys.readouterr().out
    assert "You are resuming work from a local Claude-ACT compact." in out
    assert "session_id: sid-compact" in out

def test_cli_resume_with_message_uses_compact_context(tmp_path, monkeypatch, capsys):
    pytest.importorskip("anthropic")
    from claude_act.cli.main import main

    config_dir = tmp_path / ".claude-act"
    config_path = config_dir / "config.json"
    sessions_dir = config_dir / "sessions"

    monkeypatch.setattr(_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(_config, "CONFIG_PATH", config_path)

    _config.save({
        "api_key": "sk-test",
        "default_model": "claude-sonnet-4-6",
        "storage_root": str(sessions_dir),
    })

    storage = ArchivistStorage(root=config_dir)
    storage.init()
    storage.register_session("sidr", "2026-04-14")
    storage.append_call(
        "sidr",
        "2026-04-14",
        {
            "call_id": "call-r",
            "session_id": "sidr",
            "timestamp": "2026-04-14T03:00:00Z",
            "model": "claude-sonnet-4-6",
            "input_messages": [{"role": "user", "content": "What changed?"}],
            "input_tokens": 60,
            "output_tokens": 25,
            "cost": {"amount_usd": 0.000555},
            "output": [{"type": "text", "text": "Changes"}],
            "error": None,
        },
    )
    storage.update_session_stats("sidr", 0.000555)
    compact_path = storage.write_compact(
        "sidr",
        "# Claude-ACT Compact\n\n## Facts\n```text\nsession_id: sidr\n```\n\n## State\nWorking state.\n\n## Decisions\n\n## Open Questions\n\n## Next\n",
    )
    assert compact_path.exists()

    mock_usage = MagicMock()
    mock_usage.input_tokens = 30
    mock_usage.output_tokens = 10
    mock_response = MagicMock()
    mock_response.model = "claude-sonnet-4-6"
    mock_response.usage = mock_usage
    mock_response.content = [MagicMock(type="text", text="Resumed answer.")]

    with patch("anthropic.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        MockAnthropic.return_value = mock_client

        monkeypatch.setattr(
            "sys.argv",
            [
                "claude-act",
                "resume",
                "sidr",
                "--message",
                "Continue from here.",
            ],
        )
        main()

    out = capsys.readouterr().out
    assert "Resumed answer." in out
    _, kwargs = mock_client.messages.create.call_args
    assert kwargs["messages"] == [{"role": "user", "content": "Continue from here."}]
    assert "Claude-ACT Compact" in kwargs["system"]
    assert "Working state." in kwargs["system"]

def test_find_viewer_root_from_repo_layout(tmp_path):
    from claude_act.viewer import find_viewer_root

    repo_root = tmp_path / "repo"
    viewer_root = repo_root / "tauri-workbench-base"
    viewer_root.mkdir(parents=True)
    (viewer_root / "package.json").write_text("{}", encoding="utf-8")

    start = repo_root / "claude-act"
    start.mkdir()
    found = find_viewer_root(start)

    assert found == viewer_root

def test_viewer_launch_env_for_wayland(monkeypatch):
    from claude_act.viewer import viewer_launch_env

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("WINIT_UNIX_BACKEND", raising=False)
    monkeypatch.delenv("GDK_BACKEND", raising=False)
    monkeypatch.delenv("WEBKIT_DISABLE_DMABUF_RENDERER", raising=False)
    monkeypatch.setattr("claude_act.viewer.sys.platform", "linux")

    env, note = viewer_launch_env()

    assert env["WINIT_UNIX_BACKEND"] == "x11"
    assert env["GDK_BACKEND"] == "x11"
    assert env["WEBKIT_DISABLE_DMABUF_RENDERER"] == "1"
    assert note is not None

def test_viewer_launch_env_leaves_non_wayland_alone(monkeypatch):
    from claude_act.viewer import viewer_launch_env

    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.setattr("claude_act.viewer.sys.platform", "linux")

    env, note = viewer_launch_env()

    assert env.get("WINIT_UNIX_BACKEND") is None
    assert note is None

def test_cli_viewer_dispatches_to_launcher(monkeypatch):
    from claude_act.cli.main import main

    launched = {}

    def fake_launch_viewer(*, start=None):
        launched["start"] = start
        return 0

    monkeypatch.setattr("claude_act.cli.main.launch_viewer", fake_launch_viewer)
    monkeypatch.setattr("sys.argv", ["claude-act", "viewer"])

    main()

    assert launched["start"] == Path.cwd()


# ── Interceptor ───────────────────────────────────────────────────────

def test_claudeact_wraps_existing_client(tmp_path):
    pytest.importorskip("anthropic")
    from claude_act.interceptor import ClaudeAct

    mock_usage = MagicMock()
    mock_usage.input_tokens = 15
    mock_usage.output_tokens = 5
    mock_response = MagicMock()
    mock_response.model = "claude-sonnet-4-6"
    mock_response.usage = mock_usage
    mock_response.content = [MagicMock(type="text", text="wrapped")]

    wrapped_client = MagicMock()
    wrapped_client.messages.create.return_value = mock_response

    with patch("anthropic.Anthropic") as MockAnthropic:
        client = ClaudeAct(
            client=wrapped_client,
            session_id="existing1",
            storage_root=tmp_path / "claude-act",
        )
        result = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=32,
            messages=[{"role": "user", "content": "Hello"}],
        )

    assert result is mock_response
    MockAnthropic.assert_not_called()
    wrapped_client.messages.create.assert_called_once()

def test_interceptor_logs_call(tmp_path):
    pytest.importorskip("anthropic")
    from claude_act.interceptor import ClaudeAct

    mock_usage = MagicMock()
    mock_usage.input_tokens = 100
    mock_usage.output_tokens = 50
    mock_content = [MagicMock(type="text", text="Hello")]
    mock_response = MagicMock()
    mock_response.model = "claude-sonnet-4-6"
    mock_response.usage = mock_usage
    mock_response.content = mock_content

    with patch("anthropic.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        MockAnthropic.return_value = mock_client

        client = ClaudeAct(
            api_key="sk-ant-test",
            session_id="testlog1",
            storage_root=tmp_path / "claude-act",
        )
        result = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": "Hello"}],
        )

    assert result is mock_response
    storage = ArchivistStorage(root=tmp_path / "claude-act")
    records = storage.read_session("testlog1")
    assert len(records) == 1
    assert records[0]["model"] == "claude-sonnet-4-6"
    assert records[0]["input_tokens"] == 100
    assert records[0]["cost"]["amount_usd"] > 0

def test_detector_trips_for_clear_cost_acceleration():
    from claude_act.detector import detect_cost_spike

    previous_records = [
        {
            "timestamp": "2026-04-14T00:00:00Z",
            "model": "claude-sonnet-4-6",
            "input_tokens": 1000,
            "cost": {"amount_usd": 0.0045},
            "error": None,
        },
        {
            "timestamp": "2026-04-14T00:01:00Z",
            "model": "claude-sonnet-4-6",
            "input_tokens": 1000,
            "cost": {"amount_usd": 0.0045},
            "error": None,
        },
        {
            "timestamp": "2026-04-14T00:02:00Z",
            "model": "claude-sonnet-4-6",
            "input_tokens": 1000,
            "cost": {"amount_usd": 0.0045},
            "error": None,
        },
    ]
    latest_record = {
        "session_id": "det1",
        "timestamp": "2026-04-14T00:03:00Z",
        "model": "claude-opus-4-6",
        "input_tokens": 6000,
        "cost": {"amount_usd": 0.0205},
        "error": None,
    }

    event = detect_cost_spike(previous_records, latest_record)
    assert event is not None
    assert event["kind"] == "cost_spike"
    assert "cost acceleration detected" in event["message"]
    assert event["model_switch"] == {
        "from": "claude-sonnet-4-6",
        "to": "claude-opus-4-6",
    }

def test_detector_stays_quiet_for_small_changes():
    from claude_act.detector import detect_cost_spike

    previous_records = [
        {
            "timestamp": "2026-04-14T00:00:00Z",
            "model": "claude-sonnet-4-6",
            "input_tokens": 1000,
            "cost": {"amount_usd": 0.0045},
            "error": None,
        },
        {
            "timestamp": "2026-04-14T00:01:00Z",
            "model": "claude-sonnet-4-6",
            "input_tokens": 1000,
            "cost": {"amount_usd": 0.0048},
            "error": None,
        },
        {
            "timestamp": "2026-04-14T00:02:00Z",
            "model": "claude-sonnet-4-6",
            "input_tokens": 1100,
            "cost": {"amount_usd": 0.0049},
            "error": None,
        },
    ]
    latest_record = {
        "session_id": "det2",
        "timestamp": "2026-04-14T00:03:00Z",
        "model": "claude-sonnet-4-6",
        "input_tokens": 1200,
        "cost": {"amount_usd": 0.0051},
        "error": None,
    }

    assert detect_cost_spike(previous_records, latest_record) is None

def test_interceptor_logs_error_and_reraises(tmp_path):
    pytest.importorskip("anthropic")
    from claude_act.interceptor import ClaudeAct

    with patch("anthropic.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("API error")
        MockAnthropic.return_value = mock_client

        client = ClaudeAct(
            api_key="sk-ant-test",
            session_id="testerr1",
            storage_root=tmp_path / "claude-act",
        )
        with pytest.raises(Exception, match="API error"):
            client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=256,
                messages=[{"role": "user", "content": "Hello"}],
            )

    storage = ArchivistStorage(root=tmp_path / "claude-act")
    records = storage.read_session("testerr1")
    assert len(records) == 1
    assert records[0]["error"] is not None
    sessions = storage.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["call_count"] == 1
    assert sessions[0]["error_count"] == 1
    assert sessions[0]["total_cost_usd"] == 0.0

def test_stream_context_manager_logs_once(tmp_path):
    pytest.importorskip("anthropic")
    from claude_act.interceptor import ClaudeAct

    class DummyStream:
        def __init__(self, chunks, final_message):
            self._chunks = iter(chunks)
            self._final_message = final_message

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._chunks)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_final_message(self):
            return self._final_message

    mock_usage = MagicMock()
    mock_usage.input_tokens = 10
    mock_usage.output_tokens = 5
    mock_final = MagicMock()
    mock_final.model = "claude-sonnet-4-6"
    mock_final.usage = mock_usage
    mock_final.content = [MagicMock(type="text", text="streamed")]

    with patch("anthropic.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = DummyStream(
            chunks=["chunk-1"],
            final_message=mock_final,
        )
        MockAnthropic.return_value = mock_client

        client = ClaudeAct(
            api_key="sk-ant-test",
            session_id="stream1",
            storage_root=tmp_path / "claude-act",
        )

        with client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": "Hello"}],
            stream=True,
        ) as stream:
            assert list(stream) == ["chunk-1"]

    storage = ArchivistStorage(root=tmp_path / "claude-act")
    sessions = storage.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["call_count"] == 1

    records = storage.read_session("stream1")
    assert len(records) == 1
    assert records[0]["stream"] is True
    assert records[0]["output"][0]["text"] == "streamed"

def test_stream_iteration_logs_once_without_context_manager(tmp_path):
    pytest.importorskip("anthropic")
    from claude_act.interceptor import ClaudeAct

    class DummyStream:
        def __init__(self, chunks, final_message):
            self._chunks = iter(chunks)
            self._final_message = final_message

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._chunks)

        def get_final_message(self):
            return self._final_message

    mock_usage = MagicMock()
    mock_usage.input_tokens = 11
    mock_usage.output_tokens = 7
    mock_final = MagicMock()
    mock_final.model = "claude-sonnet-4-6"
    mock_final.usage = mock_usage
    mock_final.content = [MagicMock(type="text", text="iter-only")]

    with patch("anthropic.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = DummyStream(
            chunks=["chunk-1", "chunk-2"],
            final_message=mock_final,
        )
        MockAnthropic.return_value = mock_client

        client = ClaudeAct(
            api_key="sk-ant-test",
            session_id="stream2",
            storage_root=tmp_path / "claude-act",
        )

        stream = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": "Hello"}],
            stream=True,
        )
        assert list(stream) == ["chunk-1", "chunk-2"]

    storage = ArchivistStorage(root=tmp_path / "claude-act")
    sessions = storage.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["call_count"] == 1

    records = storage.read_session("stream2")
    assert len(records) == 1
    assert records[0]["stream"] is True
    assert records[0]["output"][0]["text"] == "iter-only"

def test_interceptor_records_cost_spike_warning(tmp_path, capsys):
    pytest.importorskip("anthropic")
    from claude_act.interceptor import ClaudeAct

    def make_response(input_tokens: int, output_tokens: int):
        usage = MagicMock()
        usage.input_tokens = input_tokens
        usage.output_tokens = output_tokens
        response = MagicMock()
        response.model = "claude-sonnet-4-6"
        response.usage = usage
        response.content = [MagicMock(type="text", text="ok")]
        return response

    with patch("anthropic.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            make_response(1000, 100),
            make_response(1000, 100),
            make_response(1000, 100),
            make_response(6000, 800),
        ]
        MockAnthropic.return_value = mock_client

        client = ClaudeAct(
            api_key="sk-ant-test",
            session_id="spike1",
            storage_root=tmp_path / "claude-act",
        )
        for _ in range(4):
            client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                messages=[{"role": "user", "content": "Hello"}],
            )

    err = capsys.readouterr().err
    assert "cost acceleration detected" in err
    storage = ArchivistStorage(root=tmp_path / "claude-act")
    records = storage.read_session("spike1")
    assert records[-1]["warnings"][0]["kind"] == "cost_spike"

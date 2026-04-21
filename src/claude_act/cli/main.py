"""
ACT Ledger CLI — local-first API cost tracking and session trace.
"""

from __future__ import annotations

import json
import os
import sys
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path

from claude_act.storage import ArchivistStorage
from claude_act import config as _config
from claude_act.compaction import build_resume_context, ensure_compact
from claude_act.config import ALLOWED_MODELS, is_valid_model, require_valid_model
from claude_act.viewer import ViewerLaunchError, launch_viewer

# ── xiCore palette ────────────────────────────────────────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
_RED    = "\033[38;2;220;60;60m"
_YELLOW = "\033[38;2;255;215;0m"
_GREEN  = "\033[38;2;80;200;120m"
_CYAN   = "\033[38;2;0;220;220m"
_PINK   = "\033[38;2;255;20;147m"
_PURPLE = "\033[38;2;160;80;220m"
_WHITE  = "\033[38;2;220;220;220m"
_GRAY   = "\033[38;2;110;110;120m"

APP_NAME = "ACT Ledger"
APP_TAGLINE = "local-first API cost tracking and session trace"
CLI_NAME = "actl"
CURRENT_PROVIDER = "Claude API"

def r(s):    return f"{_RED}{s}{RESET}"
def y(s):    return f"{_YELLOW}{s}{RESET}"
def g(s):    return f"{_GREEN}{s}{RESET}"
def c(s):    return f"{_CYAN}{s}{RESET}"
def pk(s):   return f"{_PINK}{BOLD}{s}{RESET}"
def pu(s):   return f"{_PURPLE}{s}{RESET}"
def w(s):    return f"{_WHITE}{BOLD}{s}{RESET}"
def dim(s):  return f"{_GRAY}{s}{RESET}"
def hr(n=72): return dim("─" * n)


def _pretty_path(path: Path | str) -> str:
    expanded = Path(path).expanduser()
    home = Path.home()
    try:
        return f"~/{expanded.relative_to(home)}"
    except ValueError:
        return str(expanded)


def _print_home_screen() -> None:
    print(f"\n  {pk(APP_NAME)} {dim(f'— {APP_TAGLINE}')}\n")
    print(f"  {w('What this does')}")
    print("    ACT Ledger keeps a local record of your API sessions so you can")
    print("    inspect usage, costs, and session history on your own machine.\n")

    print(f"  {w('First run')}")
    print(f"    {c(f'{CLI_NAME} init')}\n")

    print(f"  {w('Common commands')}")
    print(f"    {c(f'{CLI_NAME} stats'):28} show usage and cost summary")
    print(f"    {c(f'{CLI_NAME} list'):28} list saved sessions")
    print(f"    {c(f'{CLI_NAME} read <session_id>'):28} inspect one saved session")
    print(f"    {c(f'{CLI_NAME} dump <session_id>'):28} export one session as JSONL")
    print(f"    {c(f'{CLI_NAME} help'):28} show this screen again\n")

    print(f"  {w('Troubleshooting')}")
    print(f"    {c(f'{CLI_NAME} doctor'):28} check config and local archive health\n")

    print(f"  {w('Notes')}")
    print(f"    {dim('- Always run the full command: actl <command>')}")
    print(f"    {dim('- Your session data is stored locally')}")
    print(f"    {dim(f'- Current provider support: {CURRENT_PROVIDER}')}")
    print(f"    {dim('- Use the Python wrapper in your code to capture live API calls')}\n")


def _storage() -> ArchivistStorage:
    return ArchivistStorage(root=_config.get_storage_root())


def _require_init() -> None:
    if _config.is_initialized() or _storage().is_initialized():
        return
    print(
        r(f"✗ ACT Ledger is not set up yet. Run: {CLI_NAME} init"),
        file=sys.stderr,
    )
    sys.exit(2)


def _require_api_key() -> None:
    if _config.get_api_key():
        return
    print(
        r(
            f"✗ No {CURRENT_PROVIDER} key is configured. "
            f"Run: {CLI_NAME} init or set ANTHROPIC_API_KEY"
        ),
        file=sys.stderr,
    )
    sys.exit(2)


def _package_version() -> str:
    try:
        return package_version("claude-act")
    except PackageNotFoundError:
        return str(_config.DEFAULTS.get("version", "unknown"))


def _extract_response_text(content) -> str:
    blocks: list[str] = []

    for block in content or []:
        if isinstance(block, dict):
            if block.get("type") == "text" and block.get("text"):
                blocks.append(str(block["text"]))
            continue

        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", "")
            if text:
                blocks.append(str(text))

    return "\n\n".join(blocks).strip()


def _parse_request_args(
    args: list[str],
    *,
    allow_message_flag: bool = False,
) -> tuple[str | None, int, str | None, list[str]]:
    model = None
    max_tokens = 1024
    explicit_message = None
    positionals: list[str] = []

    i = 0
    while i < len(args):
        arg = args[i]

        if arg == "--max-tokens":
            if i + 1 >= len(args):
                print(r("✗ --max-tokens requires a value"), file=sys.stderr)
                sys.exit(2)
            try:
                max_tokens = int(args[i + 1])
            except ValueError:
                print(r("✗ --max-tokens must be an integer"), file=sys.stderr)
                sys.exit(2)
            if max_tokens <= 0:
                print(r("✗ --max-tokens must be greater than 0"), file=sys.stderr)
                sys.exit(2)
            i += 2
            continue

        if arg == "--model":
            if i + 1 >= len(args):
                print(r("✗ --model requires a value"), file=sys.stderr)
                sys.exit(2)
            try:
                model = require_valid_model(args[i + 1])
            except ValueError:
                print(r(f"✗ Invalid model '{args[i + 1]}'."), file=sys.stderr)
                print(dim(f"  Allowed: {', '.join(ALLOWED_MODELS)}"), file=sys.stderr)
                sys.exit(2)
            i += 2
            continue

        if allow_message_flag and arg == "--message":
            if i + 1 >= len(args):
                print(r("✗ --message requires a value"), file=sys.stderr)
                sys.exit(2)
            explicit_message = args[i + 1]
            i += 2
            continue

        positionals.append(arg)
        i += 1

    return model, max_tokens, explicit_message, positionals


# ── init ──────────────────────────────────────────────────────────────

def cmd_init(args: list[str]) -> None:
    print(f"\n  {pk(APP_NAME)} {dim('— setup')}\n")
    print(f"  {dim('Setting up local config and session storage.')}\n")

    # Parse --key flag
    key_from_flag = None
    for i, a in enumerate(args):
        if a == "--key" and i + 1 < len(args):
            key_from_flag = args[i + 1]
            break

    existing = _config.load()

    # API key
    if key_from_flag:
        api_key = key_from_flag
    else:
        existing_key = existing.get("api_key") or os.environ.get("ANTHROPIC_API_KEY") or ""
        masked = f"sk-ant-...{existing_key[-6:]}" if existing_key else "none"
        prompt = f"  {c('Claude API key')} {dim(f'(current: {masked})')} › "
        try:
            entered = input(prompt).strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n  {dim('Setup cancelled.')}")
            sys.exit(0)
        api_key = entered if entered else existing_key

    if not api_key:
        print(f"  {r('Setup could not finish.')}")
        print(f"  {dim('No Claude API key is configured yet.')}")
        print(f"  {dim('Set ANTHROPIC_API_KEY or rerun with:')}")
        print(f"    {c(f'{CLI_NAME} init --key <your_key>')}\n")
        sys.exit(2)

    # Keep init focused on first-run setup. Use the saved valid model or default.
    existing_model = existing.get("default_model")
    if is_valid_model(existing_model):
        default_model = str(existing_model).strip().lower()
    else:
        default_model = _config.DEFAULTS["default_model"]
        if existing_model:
            print(y(f"  ⚠ Ignoring invalid saved default model: {existing_model}"))

    # Save
    cfg = _config.load()
    cfg["api_key"]       = api_key
    cfg["default_model"] = default_model
    _config.save(cfg)

    # Init storage
    storage = _storage()
    storage.init()

    print(f"\n  {g('Setup complete.')}\n")
    print(f"  {w('Config saved to:')}")
    print(f"    {dim(_pretty_path(_config.CONFIG_PATH))}\n")
    print(f"  {w('Local archive directory:')}")
    print(f"    {dim(_pretty_path(storage.sessions_dir))}\n")
    print(f"  {w('Next steps')}")
    print(f"    {c(f'{CLI_NAME} stats')}")
    print(f"    {c(f'{CLI_NAME} list')}")
    print(f"    {c(f'{CLI_NAME} help')}\n")
    print(f"  {dim('If you want to capture live API calls in your own code,')}")
    print(f"  {dim('use the ACT Ledger Python wrapper.')}\n")


# ── config ────────────────────────────────────────────────────────────

def cmd_config(args: list[str]) -> None:
    if args and args[0] == "set":
        if len(args) < 3:
            print(r(f"✗ Usage: {CLI_NAME} config set <key> <value>"), file=sys.stderr)
            sys.exit(2)
        key, value = args[1], args[2]
        if key == "default_model":
            try:
                value = require_valid_model(value, field_name="default_model")
            except ValueError:
                print(r(f"✗ Invalid model '{value}'."), file=sys.stderr)
                print(dim(f"  Allowed: {', '.join(ALLOWED_MODELS)}"), file=sys.stderr)
                sys.exit(2)
        _config.set_value(key, value)
        print(g(f"✓ {key} = {value}"))
        return

    # Show current config (mask API key)
    cfg = _config.load()
    print(f"\n  {pk(APP_NAME)} {dim('config')}\n")
    print(f"  {dim('config path  ')} {dim(str(_config.get_active_config_path()))}")
    for k, v in cfg.items():
        if k == "api_key" and v:
            display = f"sk-ant-...{str(v)[-6:]}"
        elif k == "default_model" and v and not is_valid_model(str(v)):
            display = f"{v} (invalid)"
        else:
            display = str(v) if v is not None else dim("not set")
        label = f"{k:<16}"
        if k == "default_model" and v and not is_valid_model(str(v)):
            val_color = r(display)
        else:
            val_color = c(display) if v else r("not set")
        print(f"  {dim(label)} {val_color}")
    print()


# ── list ──────────────────────────────────────────────────────────────

def cmd_list(args: list[str]) -> None:
    _require_init()
    storage = _storage()
    sessions = storage.list_sessions()

    print(f"\n  {pk(APP_NAME)}  {dim('saved sessions')}\n")

    if not sessions:
        print(f"  {dim('No saved sessions yet.')}")
        print(f"  {dim('ACT Ledger will list sessions after the Python wrapper captures')}")
        print(f"  {dim('at least one API call on this machine.')}\n")
        return

    print(f"  {c('SESSION'):<28} {c('DATE'):<12} {c('CALLS'):>6} {c('ERRORS'):>7} {c('SPENT'):>13}  {c('STARTED')}")
    print(f"  {hr(78)}")

    for s in sessions:
        sid     = s["session_id"]
        date    = s.get("date", "?")
        calls   = s.get("call_count", 0)
        errors  = s.get("error_count", 0)
        cost    = s.get("total_cost_usd", 0.0)
        started = s.get("started_at", "?")[:19]

        err_col  = r(f"{errors:>7}") if errors > 0 else dim(f"{errors:>7}")
        cost_col = y(f"${cost:>11.6f}") if cost > 0 else dim(f"${cost:>11.6f}")
        calls_col = g(f"{calls:>6}") if calls > 0 else dim(f"{calls:>6}")

        print(f"  {pu(sid):<28} {dim(date):<12} {calls_col} {err_col}  {cost_col}  {dim(started)}")
    print()


# ── read ──────────────────────────────────────────────────────────────

def cmd_read(args: list[str]) -> None:
    if not args:
        print(r(f"✗ Usage: {CLI_NAME} read <session_id>"), file=sys.stderr)
        sys.exit(2)

    _require_init()
    session_id = args[0]
    storage = _storage()

    try:
        records = storage.read_session(session_id)
    except FileNotFoundError:
        print(r(f"✗ Session not found: {session_id}"), file=sys.stderr)
        print(dim(f"  Run `{CLI_NAME} list` to see saved session IDs."), file=sys.stderr)
        sys.exit(2)
    except RuntimeError as e:
        print(r(f"✗ {e}"), file=sys.stderr)
        sys.exit(2)

    errors     = sum(1 for rec in records if rec.get("error"))
    total_cost = sum((rec.get("cost") or {}).get("amount_usd", 0.0) for rec in records)
    clean      = len(records) - errors

    print(f"\n  {pk('session')} {pu(session_id)}")
    print(f"  {hr(50)}")
    print(f"  {g(str(clean))} clean   {r(str(errors)) if errors else dim('0')} errors   "
          f"{y(f'${total_cost:.6f}') if total_cost > 0 else dim('$0.000000')} spent\n")

    for i, rec in enumerate(records, 1):
        cost     = rec.get("cost") or {}
        error    = rec.get("error")
        stream   = " stream" if rec.get("stream") else ""
        ts       = rec.get("timestamp", "?")[:19]
        model    = rec.get("model") or "unknown"
        in_tok   = rec.get("input_tokens", 0)
        out_tok  = rec.get("output_tokens", 0)
        latency  = rec.get("latency_ms", "?")
        cost_amt = cost.get("amount_usd", 0.0)
        exact    = cost.get("exact_match", True)

        num_col = r(f"[{i:>3}]") if error else g(f"[{i:>3}]")
        print(f"  {num_col} {dim(ts)}{dim(stream)}")
        print(f"       {dim('model   ')} {c(model)}")
        print(f"       {dim('tokens  ')} {w(str(in_tok))} {dim('in')}  {w(str(out_tok))} {dim('out')}")

        cost_str = y(f"${cost_amt:.6f}") if cost_amt > 0 else dim(f"${cost_amt:.6f}")
        fallback = y("  ⚠ unknown model — using Sonnet rates") if not exact else ""
        print(f"       {dim('cost    ')} {cost_str}{fallback}")
        print(f"       {dim('latency ')} {dim(str(latency) + ' ms')}")

        if error:
            etype = error.get("type", "?")
            emsg  = error.get("message", "?")[:100]
            print(f"       {r('error   ')} {r(etype)}{dim(':')} {r(emsg)}")
        else:
            warnings = rec.get("warnings", []) or []
            for warning in warnings:
                message = warning.get("message")
                if message:
                    print(f"       {y('warning ')} {dim(message)}")

        msgs = rec.get("input_messages", [])
        if msgs:
            last_user = next((m for m in reversed(msgs) if m.get("role") == "user"), None)
            if last_user:
                content = last_user.get("content", "")
                if isinstance(content, list):
                    text = " ".join(b.get("text","") for b in content if isinstance(b,dict))
                else:
                    text = str(content)
                preview = text[:100].replace("\n", " ")
                print(f"       {dim('prompt  ')} {dim(preview)}")
        print()


# ── stats ─────────────────────────────────────────────────────────────

def cmd_stats(args: list[str]) -> None:
    _require_init()
    storage = _storage()
    stats = storage.aggregate_stats()
    total_cost = stats["total_cost_usd"]

    print(f"\n  {pk(APP_NAME)}  {dim('usage and cost summary')}\n")

    if stats["total_sessions"] == 0:
        print(f"  {dim('No usage data yet.')}")
        print(f"  {dim('Stats appear after ACT Ledger captures at least one saved session.')}\n")
        return

    print(f"  {dim('sessions   ')} {w(str(stats['total_sessions']))}")
    print(f"  {dim('api calls  ')} {w(str(stats['total_calls']))}")
    print(f"  {dim('total spent')} {y(f'${total_cost:.6f} USD') if total_cost > 0 else dim('$0.000000 USD')}")

    if stats["costliest_session"]:
        cs      = stats["costliest_session"]
        cs_cost = cs.get("total_cost_usd", 0.0)
        cs_err  = cs.get("error_count", 0)
        print(f"  {dim('costliest  ')} {pu(cs['session_id'])}  "
              f"{y(f'${cs_cost:.6f}')}  "
              f"{g(str(cs.get('call_count',0)))} calls  "
              f"{r(str(cs_err) + ' errors') if cs_err else dim('0 errors')}  "
              f"{dim(cs.get('date','?'))}")
    print()


# ── dump / export ─────────────────────────────────────────────────────

def _dump_records(
    args: list[str],
    *,
    command_name: str,
    default_to_file: bool,
) -> None:
    if not args:
        print(r(f"✗ Usage: {CLI_NAME} {command_name} <session_id> [output.jsonl]"), file=sys.stderr)
        sys.exit(2)

    _require_init()
    session_id = args[0]
    out_file = args[1] if len(args) > 1 else None
    storage = _storage()

    try:
        records = storage.read_session(session_id)
    except FileNotFoundError:
        print(r(f"✗ Session not found: {session_id}"), file=sys.stderr)
        print(dim(f"  Run `{CLI_NAME} list` to see saved session IDs."), file=sys.stderr)
        sys.exit(2)
    except RuntimeError as e:
        print(r(f"✗ {e}"), file=sys.stderr)
        sys.exit(2)

    lines = [json.dumps(rec, ensure_ascii=False) for rec in records]
    if out_file:
        output_path = Path(out_file)
    elif default_to_file:
        output_path = Path.cwd() / f"{session_id}.jsonl"
    else:
        output_path = None

    if output_path is None:
        for line in lines:
            print(line)
        return

    try:
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        print(r(f"✗ Could not write dump file: {exc}"), file=sys.stderr)
        sys.exit(2)

    record_word = "record" if len(records) == 1 else "records"
    print(f"\n  {g(f'Wrote {len(records)} {record_word} to:')}")
    print(f"    {dim(str(output_path.resolve()))}\n")


def cmd_dump(args: list[str]) -> None:
    _dump_records(args, command_name="dump", default_to_file=True)


def cmd_export(args: list[str]) -> None:
    _dump_records(args, command_name="export", default_to_file=False)


# ── compact ───────────────────────────────────────────────────────────

def cmd_compact(args: list[str]) -> None:
    if not args:
        print(r("✗ compact requires a session_id"), file=sys.stderr)
        sys.exit(2)

    _require_init()
    session_id = args[0]
    storage = _storage()

    try:
        path, _ = ensure_compact(storage, session_id)
    except (FileNotFoundError, RuntimeError) as exc:
        print(r(f"✗ {exc}"), file=sys.stderr)
        sys.exit(2)

    print(g(f"✓ Wrote compact → {path}"))


def cmd_show_compact(args: list[str]) -> None:
    if not args:
        print(r("✗ show-compact requires a session_id"), file=sys.stderr)
        sys.exit(2)

    _require_init()
    session_id = args[0]
    storage = _storage()

    try:
        _, compact_text = ensure_compact(storage, session_id)
    except (FileNotFoundError, RuntimeError) as exc:
        print(r(f"✗ {exc}"), file=sys.stderr)
        sys.exit(2)

    print(compact_text, end="")


# ── ask ───────────────────────────────────────────────────────────────

def cmd_ask(args: list[str]) -> None:
    if not args:
        print(r("✗ ask requires a prompt"), file=sys.stderr)
        sys.exit(2)

    _require_api_key()
    model, max_tokens, _, prompt_parts = _parse_request_args(args)
    prompt = " ".join(prompt_parts).strip()
    if not prompt:
        print(r("✗ ask requires a prompt"), file=sys.stderr)
        sys.exit(2)

    if model is None:
        try:
            model = _config.get_default_model()
        except ValueError as exc:
            print(r(f"✗ {exc}"), file=sys.stderr)
            sys.exit(2)

    from claude_act import ClaudeAct

    try:
        client = ClaudeAct(model=model)
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        print(r(f"✗ {exc}"), file=sys.stderr)
        sys.exit(1)

    text = _extract_response_text(getattr(response, "content", None))
    if text:
        print(text)


def cmd_resume(args: list[str]) -> None:
    if not args:
        print(r("✗ resume requires a session_id"), file=sys.stderr)
        sys.exit(2)

    _require_init()
    session_id = args[0]
    storage = _storage()

    try:
        _, compact_text = ensure_compact(storage, session_id)
    except (FileNotFoundError, RuntimeError) as exc:
        print(r(f"✗ {exc}"), file=sys.stderr)
        sys.exit(2)

    model, max_tokens, explicit_message, remaining = _parse_request_args(
        args[1:],
        allow_message_flag=True,
    )
    message = explicit_message or " ".join(remaining).strip()
    resume_context = build_resume_context(compact_text)

    if not message:
        print(resume_context, end="" if resume_context.endswith("\n") else "\n")
        return

    _require_api_key()
    if model is None:
        try:
            model = _config.get_default_model()
        except ValueError as exc:
            print(r(f"✗ {exc}"), file=sys.stderr)
            sys.exit(2)

    from claude_act import ClaudeAct

    try:
        client = ClaudeAct(model=model)
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=resume_context,
            messages=[{"role": "user", "content": message}],
        )
    except Exception as exc:
        print(r(f"✗ {exc}"), file=sys.stderr)
        sys.exit(1)

    text = _extract_response_text(getattr(response, "content", None))
    if text:
        print(text)


def cmd_viewer(args: list[str]) -> None:
    if args:
        print(r("✗ viewer does not take arguments"), file=sys.stderr)
        sys.exit(2)

    try:
        code = launch_viewer(start=Path(os.getcwd()))
    except ViewerLaunchError as exc:
        print(r(f"✗ {exc}"), file=sys.stderr)
        sys.exit(2)

    if code != 0:
        sys.exit(code)


# ── router ────────────────────────────────────────────────────────────

def cmd_doctor(args: list[str]) -> None:
    cfg = _config.load()
    storage = _storage()
    raw_model = cfg.get("default_model")
    archive_root = _config.get_storage_root()

    print(f"\n  {pk(APP_NAME)}  {dim('doctor')}\n")
    print(f"  {dim('version      ')} {w(_package_version())}")
    print(f"  {dim('config path  ')} {dim(str(_config.get_active_config_path()))}")

    api_key = _config.get_api_key()
    key_status = g("yes") if api_key else r("no")
    print(f"  {dim('api key      ')} {key_status}")

    if raw_model is None:
        model_color = r("not set")
    elif is_valid_model(str(raw_model)):
        model_color = c(str(raw_model))
    else:
        model_color = r(f"{raw_model} (invalid)")
    print(f"  {dim('default model')} {model_color}")

    print(f"  {dim('archive root ')} {dim(str(archive_root))}")
    print(f"  {dim('sessions dir ')} {dim(str(archive_root / 'sessions'))}")

    try:
        sessions = storage.list_sessions()
        print(f"  {dim('sessions     ')} {g(str(len(sessions)))}")
    except Exception:
        print(f"  {dim('sessions     ')} {dim('storage not initialized')}")

    print()


COMMANDS = {
    "help":   lambda args: _print_home_screen(),
    "init":   cmd_init,
    "stats":  cmd_stats,
    "list":   cmd_list,
    "read":   cmd_read,
    "dump":   cmd_dump,
    "export": cmd_export,
    "doctor": cmd_doctor,
    "config": cmd_config,
    "compact": cmd_compact,
    "show-compact": cmd_show_compact,
    "ask":    cmd_ask,
    "resume": cmd_resume,
    "viewer": cmd_viewer,
}


VISIBLE_COMMANDS: tuple[str, ...] = ("help", "init", "stats", "list", "read", "dump", "doctor")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        _print_home_screen()
        sys.exit(0)

    cmd = args[0]
    if cmd not in COMMANDS:
        print(r(f"✗ unknown command '{cmd}'"), file=sys.stderr)
        print(dim(f"  Run `{CLI_NAME} help` for the first-run command guide."), file=sys.stderr)
        print(dim(f"  commands: {', '.join(VISIBLE_COMMANDS)}"), file=sys.stderr)
        sys.exit(2)

    COMMANDS[cmd](args[1:])


if __name__ == "__main__":
    main()

"""
ClaudeAct — transparent wrapper around anthropic.Anthropic().

Drop-in replacement:
    client = ClaudeAct(api_key="sk-ant-...")

Every messages.create() call is logged locally.
Archive failures NEVER surface to the caller.
Streaming is supported — we assemble the full record after stream closes.
"""

from __future__ import annotations

import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import anthropic
except ImportError:
    raise ImportError(
        "anthropic package is required: pip install anthropic"
    )

from .pricing import calculate_cost
from .detector import detect_cost_spike
from .storage import ArchivistStorage
from . import config as _config

CLAUDE_ACT_VERSION = "0.1.0"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _session_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class _ArchivingMessages:
    """
    Wraps client.messages to intercept create() calls.
    Both sync and async paths are handled.
    """

    def __init__(self, real_messages, session_id: str, storage: ArchivistStorage):
        self._real = real_messages
        self._session_id = session_id
        self._storage = storage
        self._date = _session_date()

    def _maybe_record_warning(self, record: dict[str, Any]) -> None:
        try:
            previous_records = self._storage.read_session(self._session_id)
        except FileNotFoundError:
            previous_records = []
        except Exception as exc:
            self._storage._log_error("_read_session_for_detector", str(exc))
            return

        try:
            warning = detect_cost_spike(previous_records, record)
            if warning:
                record["warnings"] = [warning]
                print(warning["message"], file=sys.stderr)
        except Exception as exc:
            self._storage._log_error("_cost_spike_detector", str(exc))

    def create(self, **kwargs) -> Any:
        call_id = str(uuid.uuid4())
        t_start = time.monotonic()
        error_record = None
        response = None
        latency_ms = 0.0
        caught_exc = None

        stream = kwargs.get("stream", False)

        try:
            response = self._real.create(**kwargs)
        except Exception as exc:
            error_record = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
            caught_exc = exc
        finally:
            latency_ms = round((time.monotonic() - t_start) * 1000, 2)

        if stream and response is not None:
            # Return a recording stream wrapper — logs after exhaustion
            return _RecordingStream(
                stream=response,
                call_id=call_id,
                session_id=self._session_id,
                date_str=self._date,
                storage=self._storage,
                kwargs=kwargs,
                latency_ms=latency_ms,
            )

        # Non-streaming: log immediately, including failures
        self._log_call(
            call_id=call_id,
            latency_ms=latency_ms,
            kwargs=kwargs,
            response=response,
            error=error_record,
        )

        if caught_exc is not None:
            raise caught_exc

        return response

    def _log_call(
        self,
        call_id: str,
        latency_ms: float,
        kwargs: dict,
        response: Any,
        error: dict | None,
    ) -> None:
        try:
            model = None
            input_tokens = 0
            output_tokens = 0
            output_content = None

            if response is not None:
                model = getattr(response, "model", kwargs.get("model"))
                usage = getattr(response, "usage", None)
                if usage:
                    input_tokens = getattr(usage, "input_tokens", 0) or 0
                    output_tokens = getattr(usage, "output_tokens", 0) or 0
                content = getattr(response, "content", None)
                if content:
                    output_content = [
                        {"type": getattr(b, "type", "text"),
                         "text": getattr(b, "text", "")}
                        for b in content
                    ]
            else:
                model = kwargs.get("model")

            cost = calculate_cost(
                model or "_unknown",
                input_tokens,
                output_tokens,
            ) if not error else None

            record = {
                "call_id": call_id,
                "session_id": self._session_id,
                "timestamp": _now_utc(),
                "claude_act_version": CLAUDE_ACT_VERSION,
                "model": model,
                "system": kwargs.get("system"),
                "input_messages": kwargs.get("messages", []),
                "output": output_content,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost": cost,
                "latency_ms": latency_ms,
                "error": error,
            }

            if not error:
                self._maybe_record_warning(record)

            self._storage.append_call(self._session_id, self._date, record)
            self._storage.update_session_stats(
                self._session_id,
                (cost or {}).get("amount_usd", 0.0),
                error=bool(error),
            )

        except Exception as exc:
            self._storage._log_error("_log_call", str(exc))


class _RecordingStream:
    """
    Wraps a streaming response. Assembles the full record after
    the stream is exhausted. Caller's streaming experience is unchanged.
    """

    def __init__(self, stream, call_id, session_id, date_str,
                 storage, kwargs, latency_ms):
        self._stream = stream
        self._call_id = call_id
        self._session_id = session_id
        self._date = date_str
        self._storage = storage
        self._kwargs = kwargs
        self._latency_ms = latency_ms
        self._chunks: list = []
        self._final_message = None
        self._finalized = False

    def _maybe_record_warning(self, record: dict[str, Any]) -> None:
        try:
            previous_records = self._storage.read_session(self._session_id)
        except FileNotFoundError:
            previous_records = []
        except Exception as exc:
            self._storage._log_error("_read_session_for_detector", str(exc))
            return

        try:
            warning = detect_cost_spike(previous_records, record)
            if warning:
                record["warnings"] = [warning]
                print(warning["message"], file=sys.stderr)
        except Exception as exc:
            self._storage._log_error("_cost_spike_detector", str(exc))

    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = next(self._stream)
            self._chunks.append(chunk)
            return chunk
        except StopIteration:
            self._finalize()
            raise

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, *args):
        result = self._stream.__exit__(*args)
        self._finalize()
        return result

    def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        try:
            # Try to get final message from stream if available
            final = getattr(self._stream, "get_final_message", None)
            if callable(final):
                self._final_message = final()

            model = None
            input_tokens = 0
            output_tokens = 0
            output_content = None

            if self._final_message:
                model = getattr(self._final_message, "model",
                                self._kwargs.get("model"))
                usage = getattr(self._final_message, "usage", None)
                if usage:
                    input_tokens = getattr(usage, "input_tokens", 0) or 0
                    output_tokens = getattr(usage, "output_tokens", 0) or 0
                content = getattr(self._final_message, "content", None)
                if content:
                    output_content = [
                        {"type": getattr(b, "type", "text"),
                         "text": getattr(b, "text", "")}
                        for b in content
                    ]
            else:
                model = self._kwargs.get("model")

            cost = calculate_cost(
                model or "_unknown", input_tokens, output_tokens
            )

            record = {
                "call_id": self._call_id,
                "session_id": self._session_id,
                "timestamp": _now_utc(),
                "claude_act_version": CLAUDE_ACT_VERSION,
                "model": model,
                "system": self._kwargs.get("system"),
                "input_messages": self._kwargs.get("messages", []),
                "output": output_content,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost": cost,
                "latency_ms": self._latency_ms,
                "stream": True,
                "error": None,
            }

            self._maybe_record_warning(record)
            self._storage.append_call(self._session_id, self._date, record)
            self._storage.update_session_stats(
                self._session_id,
                cost["amount_usd"],
                error=False,
            )
        except Exception as exc:
            self._storage._log_error("_finalize_stream", str(exc))


class ClaudeAct:
    """
    Drop-in replacement for anthropic.Anthropic().

    Usage:
        from claude_act import ClaudeAct
        client = ClaudeAct(api_key="sk-ant-...")
        response = client.messages.create(...)  # logged automatically
    """

    def __init__(
        self,
        api_key: str | None = None,
        client: Any | None = None,
        session_id: str | None = None,
        storage_root: Path | None = None,
        model: str | None = None,
        **kwargs,
    ):
        if model is not None:
            self._default_model = _config.require_valid_model(
                model,
                field_name="default_model",
            )
        else:
            self._default_model = _config.get_default_model()
        if client is not None:
            self._client = client
        else:
            resolved_key = api_key or _config.get_api_key()
            self._client = anthropic.Anthropic(api_key=resolved_key, **kwargs)
        self._session_id = session_id or str(uuid.uuid4())[:8]
        resolved_root = storage_root or _config.get_storage_root()
        self._storage = ArchivistStorage(root=resolved_root)

        # Init storage silently — never fail the caller
        try:
            self._storage.init()
            self._storage.register_session(
                self._session_id,
                _session_date(),
            )
        except Exception as exc:
            self._storage._log_error("__init__", str(exc))

        self.messages = _ArchivingMessages(
            self._client.messages,
            self._session_id,
            self._storage,
        )

    @property
    def session_id(self) -> str:
        return self._session_id

    # Pass through everything else to the real client
    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

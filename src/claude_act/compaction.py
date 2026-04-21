"""
Deterministic local-only compact/resume helpers.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from .storage import ArchivistStorage

COMPACT_TITLE = "# Claude-ACT Compact"
SECTION_ORDER = ["State", "Decisions", "Open Questions", "Next"]


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _snippet(text: str, limit: int = 140) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _extract_prompt_text(record: dict[str, Any]) -> str:
    for message in reversed(record.get("input_messages", [])):
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, list):
            text = " ".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        else:
            text = str(content)
        if text.strip():
            return text.strip()
    return ""


def _format_duration_seconds(started_at: str | None, ended_at: str | None) -> str:
    started = _parse_timestamp(started_at)
    ended = _parse_timestamp(ended_at)
    if not started or not ended:
        return "unknown"
    return f"{max((ended - started).total_seconds(), 0.0):.2f}"


def _latest_spike_event(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    latest = None
    for record in records:
        for warning in record.get("warnings", []) or []:
            if warning.get("kind") == "cost_spike":
                latest = warning
    return latest


def _facts_lines(
    session_id: str,
    metadata: dict[str, Any],
    records: list[dict[str, Any]],
) -> list[str]:
    started_at = metadata.get("started_at") or (records[0].get("timestamp") if records else None)
    ended_at = metadata.get("last_call_at") or (records[-1].get("timestamp") if records else started_at)
    total_cost = sum(((record.get("cost") or {}).get("amount_usd") or 0.0) for record in records)
    models = Counter(record.get("model") for record in records if record.get("model"))
    models_str = ", ".join(
        f"{model} x{count}"
        for model, count in sorted(models.items(), key=lambda item: (-item[1], item[0]))
    ) or "none"

    prompts = [_extract_prompt_text(record) for record in records]
    prompts = [prompt for prompt in prompts if prompt]
    first_prompt = _snippet(prompts[0]) if prompts else "none"
    last_prompt = _snippet(prompts[-1]) if prompts else "none"

    largest_call = None
    if records:
        largest_call = max(
            records,
            key=lambda record: ((record.get("cost") or {}).get("amount_usd") or 0.0),
        )

    largest_call_str = "none"
    if largest_call:
        largest_call_str = (
            f"call_id={largest_call.get('call_id', '?')} "
            f"model={largest_call.get('model', 'unknown')} "
            f"cost_usd={((largest_call.get('cost') or {}).get('amount_usd') or 0.0):.6f} "
            f"input_tokens={largest_call.get('input_tokens', 0)} "
            f"output_tokens={largest_call.get('output_tokens', 0)}"
        )

    spike_events = sum(
        1
        for record in records
        for warning in (record.get("warnings", []) or [])
        if warning.get("kind") == "cost_spike"
    )
    latest_spike = _latest_spike_event(records)
    latest_spike_str = "none"
    if latest_spike:
        latest_spike_str = (
            f"timestamp={latest_spike.get('timestamp', '?')} "
            f"latest_cost_usd={latest_spike.get('latest_cost_usd', 0.0):.6f} "
            f"trailing_avg_cost_usd={latest_spike.get('trailing_avg_cost_usd', 0.0):.6f}"
        )

    return [
        f"session_id: {session_id}",
        f"started_at: {started_at or 'unknown'}",
        f"ended_at: {ended_at or 'unknown'}",
        f"duration_seconds: {_format_duration_seconds(started_at, ended_at)}",
        f"call_count: {len(records)}",
        f"total_cost_usd: {total_cost:.6f}",
        f"models: {models_str}",
        f"first_prompt: {first_prompt}",
        f"last_prompt: {last_prompt}",
        f"largest_call: {largest_call_str}",
        f"spike_events: {spike_events}",
        f"latest_spike: {latest_spike_str}",
    ]


def parse_editable_sections(text: str | None) -> dict[str, str]:
    sections = {name: "" for name in SECTION_ORDER}
    if not text:
        return sections

    current = None
    buffer: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current in sections:
                sections[current] = "\n".join(buffer).strip()
            name = line[3:].strip()
            current = name if name in sections else None
            buffer = []
            continue
        if current in sections:
            buffer.append(line)

    if current in sections:
        sections[current] = "\n".join(buffer).strip()
    return sections


def render_compact(
    session_id: str,
    metadata: dict[str, Any],
    records: list[dict[str, Any]],
    existing_text: str | None = None,
) -> str:
    sections = parse_editable_sections(existing_text)
    lines = [
        COMPACT_TITLE,
        "",
        "## Facts",
        "```text",
        *_facts_lines(session_id, metadata, records),
        "```",
        "",
    ]
    for section in SECTION_ORDER:
        lines.append(f"## {section}")
        lines.append(sections[section].rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def ensure_compact(storage: ArchivistStorage, session_id: str) -> tuple[str, str]:
    metadata = storage.get_session_metadata(session_id)
    records = storage.read_session(session_id)
    existing_text = None
    try:
        existing_text = storage.read_compact(session_id)
    except FileNotFoundError:
        existing_text = None
    content = render_compact(session_id, metadata, records, existing_text)
    path = storage.write_compact(session_id, content)
    return str(path), content


def build_resume_context(compact_text: str) -> str:
    return (
        "You are resuming work from a local Claude-ACT compact.\n"
        "Use only the archived context below as prior context.\n"
        "If relevant information is missing, say so plainly.\n\n"
        f"{compact_text.strip()}\n"
    )

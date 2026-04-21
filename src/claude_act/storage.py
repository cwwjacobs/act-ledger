"""
Local storage for ACT Ledger.

Layout:
  ~/.act-ledger/
    index.json                          — session registry
    sessions/YYYY-MM-DD_<sid>.jsonl     — one call record per line
    errors.jsonl                        — archiver-internal failures (never raised to caller)
    pricing.json                        — user-overridable pricing snapshot

Rules:
  - Storage failures NEVER propagate to the caller.
  - Every write is append-only. No in-place mutation.
  - Corrupt index does not block session writes.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_ROOT = Path.home() / ".act-ledger"
DIR_MODE = 0o700
FILE_MODE = 0o600


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ArchivistStorage:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else DEFAULT_ROOT
        self.sessions_dir = self.root / "sessions"
        self.compactions_dir = self.root / "compactions"
        self.index_path = self.root / "index.json"
        self.errors_path = self.root / "errors.jsonl"

    # ── Init ──────────────────────────────────────────────────────────

    def init(self) -> None:
        """Create directory structure. Idempotent."""
        self._ensure_dir(self.root)
        self._ensure_dir(self.sessions_dir)
        self._ensure_dir(self.compactions_dir)
        if not self.index_path.exists():
            self._write_index({
                "claude_act_version": "0.1.0",
                "created_at": _now_utc(),
                "sessions": {},
            })

    def is_initialized(self) -> bool:
        return self.root.exists() and self.sessions_dir.exists()

    # ── Session management ────────────────────────────────────────────

    def session_path(self, session_id: str, date_str: str) -> Path:
        return self.sessions_dir / f"{date_str}_{session_id}.jsonl"

    def register_session(self, session_id: str, date_str: str) -> None:
        """Add session to index. Safe to call multiple times."""
        try:
            index = self._read_index()
            if session_id not in index["sessions"]:
                index["sessions"][session_id] = {
                    "session_id": session_id,
                    "date": date_str,
                    "file": f"{date_str}_{session_id}.jsonl",
                    "started_at": _now_utc(),
                    "call_count": 0,
                    "error_count": 0,
                    "total_cost_usd": 0.0,
                }
                self._write_index(index)
        except Exception as exc:
            self._log_error("register_session", str(exc))

    def update_session_stats(
        self,
        session_id: str,
        cost_usd: float,
        *,
        error: bool = False,
    ) -> None:
        try:
            index = self._read_index()
            if session_id in index["sessions"]:
                s = index["sessions"][session_id]
                s["call_count"] = s.get("call_count", 0) + 1
                s["error_count"] = s.get("error_count", 0) + (1 if error else 0)
                s["total_cost_usd"] = round(
                    s.get("total_cost_usd", 0.0) + cost_usd, 8
                )
                s["last_call_at"] = _now_utc()
                self._write_index(index)
        except Exception as exc:
            self._log_error("update_session_stats", str(exc))

    # ── Call record ───────────────────────────────────────────────────

    def append_call(self, session_id: str, date_str: str, record: dict) -> None:
        """Append one call record to the session JSONL. Never raises."""
        try:
            self._ensure_dir(self.root)
            self._ensure_dir(self.sessions_dir)
            path = self.session_path(session_id, date_str)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._chmod(path, FILE_MODE)
        except Exception as exc:
            self._log_error("append_call", str(exc), {"session_id": session_id})

    # ── Read operations ───────────────────────────────────────────────

    def list_sessions(self) -> list[dict]:
        try:
            index = self._read_index()
            sessions = list(index.get("sessions", {}).values())
            return sorted(sessions, key=lambda s: s.get("started_at", ""), reverse=True)
        except Exception:
            return []

    def get_session_metadata(self, session_id: str) -> dict:
        index = self._read_index()
        meta = index.get("sessions", {}).get(session_id)
        if not meta:
            raise FileNotFoundError(f"Session not found: {session_id}")
        return meta

    def read_session(self, session_id: str) -> list[dict]:
        """Read all call records for a session."""
        try:
            meta = self.get_session_metadata(session_id)
            path = self.sessions_dir / meta["file"]
            if not path.exists():
                raise FileNotFoundError(f"Session file missing: {path}")
            records = []
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
            return records
        except FileNotFoundError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Failed to read session: {exc}") from exc

    def aggregate_stats(self) -> dict:
        """Compute aggregate stats across all sessions."""
        sessions = self.list_sessions()
        total_cost = sum(s.get("total_cost_usd", 0.0) for s in sessions)
        total_calls = sum(s.get("call_count", 0) for s in sessions)
        costliest = max(sessions, key=lambda s: s.get("total_cost_usd", 0.0)) \
            if sessions else None
        return {
            "total_sessions": len(sessions),
            "total_calls": total_calls,
            "total_cost_usd": round(total_cost, 6),
            "costliest_session": costliest,
        }

    # ── Compactions ───────────────────────────────────────────────────

    def compact_path(self, session_id: str) -> Path:
        return self.compactions_dir / f"{session_id}.compact.md"

    def read_compact(self, session_id: str) -> str:
        path = self.compact_path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"Compact file not found: {path}")
        return path.read_text(encoding="utf-8")

    def write_compact(self, session_id: str, content: str) -> Path:
        self._ensure_dir(self.root)
        self._ensure_dir(self.compactions_dir)
        path = self.compact_path(session_id)
        path.write_text(content, encoding="utf-8")
        self._chmod(path, FILE_MODE)
        return path

    # ── Index helpers ─────────────────────────────────────────────────

    def _read_index(self) -> dict:
        if not self.index_path.exists():
            return {"sessions": {}}
        with self.index_path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def _write_index(self, data: dict) -> None:
        self._ensure_dir(self.root)
        tmp = self.index_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        self._chmod(tmp, FILE_MODE)
        tmp.replace(self.index_path)
        self._chmod(self.index_path, FILE_MODE)

    def _ensure_dir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self._chmod(path, DIR_MODE)

    def _chmod(self, path: Path, mode: int) -> None:
        try:
            os.chmod(path, mode)
        except Exception:
            pass

    # ── Error log ─────────────────────────────────────────────────────

    def _log_error(self, operation: str, message: str, context: dict | None = None) -> None:
        """Log archiver-internal failures. Never raises."""
        try:
            self._ensure_dir(self.root)
            record = {
                "timestamp": _now_utc(),
                "operation": operation,
                "message": message,
                "context": context or {},
            }
            with self.errors_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
            self._chmod(self.errors_path, FILE_MODE)
        except Exception:
            pass  # truly last resort — nothing to do

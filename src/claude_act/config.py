"""
Config management for ACT Ledger.

Config lives at ~/.act-ledger/config.json by default.
Legacy ~/.claude-act/config.json is still read when present.
Storage root always points at the archive root, not the nested sessions dir.
Never ask the user to edit JSON directly.
All reads/writes go through this module.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

LEGACY_CONFIG_DIR = Path.home() / ".claude-act"
CONFIG_DIR  = Path.home() / ".act-ledger"
CONFIG_PATH = CONFIG_DIR / "config.json"

# ── Allowed models ───────────────────────────────────────────────────
ALLOWED_MODELS: list[str] = [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
]

def normalize_model(model: str | None) -> str | None:
    if model is None:
        return None
    normalized = str(model).strip().lower()
    return normalized or None


def is_valid_model(model: str | None) -> bool:
    """Central validation — one source of truth."""
    normalized = normalize_model(model)
    return bool(normalized and normalized in ALLOWED_MODELS)


def require_valid_model(model: str | None, *, field_name: str = "model") -> str:
    normalized = normalize_model(model)
    if normalized and normalized in ALLOWED_MODELS:
        return normalized
    allowed = ", ".join(ALLOWED_MODELS)
    raise ValueError(
        f"Invalid {field_name}: {model!r}. Allowed values: {allowed}"
    )


DEFAULTS: dict[str, Any] = {
    "api_key":       None,
    "default_model": "claude-sonnet-4-6",
    "storage_root":  str(CONFIG_DIR),
    "version":       "0.1.0",
}


def _default_config() -> dict[str, Any]:
    defaults = dict(DEFAULTS)
    defaults["storage_root"] = str(CONFIG_DIR)
    return defaults


def _legacy_config_path() -> Path:
    return LEGACY_CONFIG_DIR / "config.json"


def get_active_config_path() -> Path:
    """
    Return the config path currently in use.

    Prefer the current public path. Fall back to the legacy path only when the
    default location has not been created yet.
    """
    if CONFIG_PATH.exists():
        return CONFIG_PATH

    default_config_dir = Path.home() / ".act-ledger"
    default_config_path = default_config_dir / "config.json"
    if CONFIG_DIR == default_config_dir and CONFIG_PATH == default_config_path:
        legacy_path = _legacy_config_path()
        if legacy_path.exists():
            return legacy_path

    return CONFIG_PATH


def normalize_storage_root(root: str | Path | None) -> Path:
    """
    Return the archive root used by storage.

    Older configs stored the nested sessions directory. Coerce those values
    back to the archive root so config and storage use the same path.
    """
    if root is None:
        return CONFIG_DIR

    normalized = str(root).strip()
    if not normalized:
        return CONFIG_DIR

    candidate = Path(normalized).expanduser()
    if candidate.name == "sessions":
        return candidate.parent
    return candidate


def load() -> dict[str, Any]:
    """Load config. Returns defaults if config doesn't exist."""
    defaults = _default_config()
    config_path = get_active_config_path()
    if not config_path.exists():
        return defaults
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        # merge with defaults so new keys always have values
        merged = defaults
        merged.update(data)
        merged["storage_root"] = str(
            normalize_storage_root(merged.get("storage_root"))
        )
        return merged
    except Exception:
        return defaults


def save(config: dict[str, Any]) -> None:
    """Save config atomically with 0600 permissions."""
    config_to_save = _default_config()
    config_to_save.update(config)
    config_to_save["default_model"] = require_valid_model(
        config_to_save.get("default_model"),
        field_name="default_model",
    )
    config_to_save["storage_root"] = str(
        normalize_storage_root(config_to_save.get("storage_root"))
    )

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(config_to_save, indent=2), encoding="utf-8")
    try:
        import os as _os
        _os.chmod(tmp, 0o600)
    except Exception:
        pass  # non-Unix — best effort
    tmp.replace(CONFIG_PATH)
    try:
        import os as _os
        _os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


def get(key: str) -> Any:
    return load().get(key, _default_config().get(key))


def set_value(key: str, value: Any) -> None:
    config = load()
    if key == "default_model":
        value = require_valid_model(value, field_name="default_model")
    if key == "storage_root":
        value = str(normalize_storage_root(value))
    config[key] = value
    save(config)


def is_initialized() -> bool:
    return get_active_config_path().exists() and bool(get("api_key"))


def get_api_key() -> str | None:
    """Get API key from config, falling back to environment variable."""
    key = get("api_key")
    if key:
        return key
    return os.environ.get("ANTHROPIC_API_KEY")


def get_default_model() -> str:
    model = get("default_model") or DEFAULTS["default_model"]
    try:
        return require_valid_model(model, field_name="default_model")
    except ValueError as exc:
        raise ValueError(
            f"Invalid default_model in {get_active_config_path()}: {model!r}"
        ) from exc


def get_storage_root() -> Path:
    return normalize_storage_root(get("storage_root"))

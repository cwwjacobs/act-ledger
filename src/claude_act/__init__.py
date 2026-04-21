"""
claude-act — local Claude API logging and session archive

Usage:
    from claude_act import ClaudeAct
    client = ClaudeAct()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": "Hello"}],
    )
"""

from .storage import ArchivistStorage
from . import config

__version__ = "0.1.0"
__all__ = ["ClaudeAct", "ArchivistStorage", "config"]


def __getattr__(name: str):
    if name == "ClaudeAct":
        from .interceptor import ClaudeAct
        return ClaudeAct
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

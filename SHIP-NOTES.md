# Ship Notes

## Kept for v1

- live Claude API logging through the `ClaudeAct` wrapper
- local append-only session archive
- `init`, `list`, `read`, `export`, and `doctor`
- non-fatal logging failures on the wrapped API path
- single finalized record per completed streaming call

## Hidden or de-emphasized for v1

- `viewer`
- `compact`
- `show-compact`
- `resume`
- `ask`
- `stats`
- extra config-first setup messaging

These commands remain in the codebase for compatibility, but they are not part of the advertised v1 CLI surface or README. The desktop viewer assets are not bundled in this source release, so `viewer` should be treated as unavailable here.

## Compatibility notes

- `storage_root` now means the archive root, not the nested `sessions/` directory.
- Legacy configs that saved `storage_root` as `.../sessions` are normalized automatically to the parent archive root.
- Existing archives under `~/.claude-act/` continue to work without migration.

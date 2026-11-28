"""Claude Code project hook registration."""

import json
from pathlib import Path
from typing import Any

PRE_TOOL_USE_ENTRY = {
    "matcher": "Write|Edit|MultiEdit",
    "hooks": [
        {
            "type": "command",
            "command": "attestory hook-pre-tool-use",
        }
    ],
}

POST_TOOL_USE_ENTRY = {
    "matcher": "Write|Edit|MultiEdit",
    "hooks": [
        {
            "type": "command",
            "command": "attestory hook-post-tool-use",
        }
    ],
}


def _has_attestory_hook(entries: list[Any], command: str) -> bool:
    return any(
        isinstance(entry, dict)
        and isinstance(entry.get("hooks", []), list)
        and any(
            isinstance(hook, dict)
            and hook.get("type") == "command"
            and hook.get("command") == command
            for hook in entry.get("hooks", [])
        )
        for entry in entries
    )


def register_tool_use_hooks(root: Path) -> dict[str, bool]:
    """Merge Attestory's pre- and post-edit hooks without replacing user settings."""
    settings_path = root / ".claude" / "settings.json"
    settings: dict[str, Any] = {}
    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            raise TypeError(f"{settings_path} must contain a JSON object.")

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise TypeError(f"The 'hooks' setting in {settings_path} must be an object.")
    registered = {}
    for event, entry, command in (
        ("PreToolUse", PRE_TOOL_USE_ENTRY, "attestory hook-pre-tool-use"),
        ("PostToolUse", POST_TOOL_USE_ENTRY, "attestory hook-post-tool-use"),
    ):
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise TypeError(f"The '{event}' setting in {settings_path} must be a list.")
        if _has_attestory_hook(entries, command):
            registered[event] = False
            continue
        entries.append(entry)
        registered[event] = True

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return registered

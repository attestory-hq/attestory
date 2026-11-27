"""Claude Code project hook registration."""

import json
from pathlib import Path
from typing import Any

POST_TOOL_USE_ENTRY = {
    "matcher": "Write|Edit|MultiEdit",
    "hooks": [
        {
            "type": "command",
            "command": "attestory hook-post-tool-use",
        }
    ],
}


def _has_attestory_hook(entries: list[Any]) -> bool:
    return any(
        isinstance(entry, dict)
        and isinstance(entry.get("hooks", []), list)
        and any(
            isinstance(hook, dict)
            and hook.get("type") == "command"
            and hook.get("command") == "attestory hook-post-tool-use"
            for hook in entry.get("hooks", [])
        )
        for entry in entries
    )


def register_post_tool_use_hook(root: Path) -> bool:
    """Merge Attestory's PostToolUse hook into project settings without duplicates."""
    settings_path = root / ".claude" / "settings.json"
    settings: dict[str, Any] = {}
    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            raise TypeError(f"{settings_path} must contain a JSON object.")

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise TypeError(f"The 'hooks' setting in {settings_path} must be an object.")
    post_tool_use = hooks.setdefault("PostToolUse", [])
    if not isinstance(post_tool_use, list):
        raise TypeError(f"The 'PostToolUse' setting in {settings_path} must be a list.")
    if _has_attestory_hook(post_tool_use):
        return False

    post_tool_use.append(POST_TOOL_USE_ENTRY)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return True

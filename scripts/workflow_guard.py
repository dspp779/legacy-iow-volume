"""Fail closed before an Actions job can access the upload credentials."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

REPOSITORY = "dspp779/legacy-iow-volume"
PRODUCTION_REF = "refs/heads/main"


def boolean(value, name: str) -> bool:
    if value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    raise ValueError(f"{name} must be true or false")


def resolve_mode(event: dict, environment: dict) -> tuple[str, bool]:
    if environment.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise ValueError("only the canonical repository may use this uploader")
    if environment.get("GITHUB_REF") != PRODUCTION_REF:
        raise ValueError("only main may use this uploader")
    event_name = environment.get("GITHUB_EVENT_NAME")
    if event_name != "workflow_dispatch":
        raise ValueError("only workflow_dispatch is supported")
    inputs = event.get("inputs") or {}
    dry_run = boolean(inputs.get("dry_run", True), "dry_run")
    allow_empty = boolean(inputs.get("allow_empty_state", False), "allow_empty_state")
    if dry_run:
        if allow_empty:
            raise ValueError("allow_empty_state is only for production recovery")
        return "dry-run", False
    if not boolean(inputs.get("confirm_production", False), "confirm_production"):
        raise ValueError("production requires confirm_production=true")
    if environment.get("LEGACY_IOW_PRODUCTION_ENABLED") != "true":
        raise ValueError("production requires LEGACY_IOW_PRODUCTION_ENABLED=true")
    return "production", allow_empty


def require_state(path: Path, mode: str, allow_empty: bool) -> None:
    if mode not in {"production", "dry-run"}:
        raise ValueError("invalid upload mode")
    if not path.is_file():
        if mode == "production" and not allow_empty:
            raise ValueError("production state missing: restore progress or explicitly recover")
        return
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError("state must be an object")
    if not state and mode == "production" and not allow_empty:
        raise ValueError("production state is empty: explicit recovery required")
    for previous in state.values():
        if not isinstance(previous, dict):
            raise ValueError("invalid pump state")
        seconds = previous.get("seconds")
        volume = previous.get("volume_m3")
        if type(seconds) is not int or seconds < 0 or type(volume) is not int or volume < 0:
            raise ValueError("invalid pump state counters")
        stamp = previous.get("received_at")
        if not isinstance(stamp, str) or not stamp.strip():
            raise ValueError("invalid pump state timestamp")


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    try:
        if args == ["mode"]:
            event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
            mode, allow_empty = resolve_mode(event, os.environ)
            print(f"mode={mode}\nallow_empty_state={str(allow_empty).lower()}")
        elif len(args) == 2 and args[0] == "state":
            require_state(Path(args[1]), os.environ["UPLOAD_MODE"],
                          boolean(os.environ.get("ALLOW_EMPTY_STATE", "false"), "allow_empty_state"))
        elif len(args) == 2 and args[0] == "fingerprint":
            print(fingerprint(Path(args[1])))
        else:
            raise ValueError("usage: workflow_guard.py mode | state PATH | fingerprint PATH")
    except (KeyError, OSError, ValueError) as error:
        print(f"workflow guard: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

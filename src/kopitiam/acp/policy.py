"""Decide only the permissions the backend asks us about; this is not a sandbox."""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from ..config import norm

KOPITIAM_TOOLS = frozenset("mcp_kopitiam_" + name for name in (
    "whoami", "who", "send", "read", "history", "rename", "switch_identity", "set_cwd", "export_context"))
FILE_KINDS = frozenset({"read", "edit", "delete", "move", "search"})
PATH_KEYS = frozenset({"path", "paths", "file", "files", "filename", "filenames", "directory", "directories",
                       "dir", "dirs", "cwd", "root", "roots", "source", "destination", "src", "dest", "dst",
                       "targetfile", "sourcefile", "destinationfile", "searchdirectory", "targetdirectory",
                       "sourcedirectory", "destinationdirectory"})


def request_paths(params):
    """Gather every named path, including source/destination paths omitted by locations."""
    call = params.get("toolCall", {})
    paths = []
    invalid = False

    def value_paths(value):
        nonlocal invalid
        if isinstance(value, str) and value and "\0" not in value:
            paths.append(value)
        elif isinstance(value, list) and value:
            for item in value:
                value_paths(item)
        elif isinstance(value, dict):
            before = len(paths)
            fields(value)
            if len(paths) == before:
                invalid = True
        else:
            invalid = True

    def fields(value):
        if isinstance(value, dict):
            for key, item in value.items():
                key = re.sub(r"[_-]", "", key.lower())
                if key in PATH_KEYS or key.endswith(("path", "paths")):
                    value_paths(item)
                elif isinstance(item, (dict, list)):
                    fields(item)
        elif isinstance(value, list):
            for item in value:
                fields(item)

    locations = call.get("locations", [])
    if isinstance(locations, list):
        for location in locations:
            if isinstance(location, dict):
                value_paths(location.get("path"))
            else:
                invalid = True
    else:
        invalid = True
    fields(call.get("rawInput", {}))
    for content in call.get("content", []) or []:
        if isinstance(content, dict) and content.get("type") == "diff":
            value_paths(content.get("path"))
    return None if invalid or not paths else list(dict.fromkeys(paths))


def _resolve(path, cwd):
    # ACP replay can use /C:/...; drive-relative C:foo and URI paths are ambiguous.
    if re.match(r"^/[A-Za-z]:[/\\]", path):
        path = path[1:]
    if (re.match(r"^[A-Za-z]:(?![/\\])", path) or "://" in path or path.startswith(("~", "$"))
            or re.search(r"%[^%]+%", path)):
        raise ValueError("Ambiguous path")
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    return norm(os.path.realpath(path))


def artifact_directory(session_id):
    try:
        uuid.UUID(session_id)
    except (ValueError, TypeError, AttributeError):
        return None
    return Path.home() / ".gemini" / "antigravity-cli" / "brain" / session_id


def decide(session, params, *, policy="workspace", extra_dirs=(), artifacts=None):
    allow = next((option.get("optionId") for option in params.get("options", [])
                  if option.get("kind") == "allow_once"), None)
    if not isinstance(allow, str):
        return None  # Never substitute a permanent permission for allow_once.
    call = params.get("toolCall", {})
    allowed = call.get("name") in KOPITIAM_TOOLS or policy == "all"
    if not allowed and policy == "workspace" and call.get("kind") in FILE_KINDS:
        paths = request_paths(params)
        if paths:
            try:
                roots = [_resolve(str(root), session.cwd) for root in
                         [session.cwd, *extra_dirs, *([artifacts] if artifacts else [])]]
                resolved = [_resolve(path, session.cwd) for path in paths]
                def contains(path):
                    for root in roots:
                        try:
                            if os.path.commonpath([path, root]) == root:
                                return True
                        except ValueError:  # Different drives or UNC shares.
                            continue
                    return False
                allowed = all(contains(path) for path in resolved)
            except (OSError, ValueError, TypeError):
                allowed = False
    return {"outcome": "selected", "optionId": allow} if allowed else None

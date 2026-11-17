from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from kopitiam.acp.policy import KOPITIAM_TOOLS, artifact_directory, decide, request_paths


def request(kind, paths=None, raw=None, name="test_tool"):
    call = {"kind": kind, "name": name}
    if paths is not None:
        call["locations"] = [{"path": str(path)} for path in paths]
    if raw is not None:
        call["rawInput"] = raw
    return {"toolCall": call, "options": [
        {"optionId": "persistent", "kind": "allow_always"},
        {"optionId": "once", "kind": "allow_once"},
        {"optionId": "no", "kind": "reject_once"}]}


@pytest.fixture
def session(home):
    workspace = home / "workspace"
    workspace.mkdir()
    return SimpleNamespace(cwd=str(workspace))


@pytest.mark.parametrize("kind", ["read", "edit", "delete", "move", "search"])
def test_workspace_file_operations_allow_once(session, kind):
    params = request(kind, [os.path.join(session.cwd, "new.txt")])
    assert decide(session, params) == {"outcome": "selected", "optionId": "once"}


@pytest.mark.parametrize("path", ["../outside.txt", "..\\outside.txt", "C:ambiguous", "file:///C:/outside.txt",
                                  "~/.ssh/config", "%USERPROFILE%/outside.txt", "$HOME/outside.txt"])
def test_escape_and_ambiguous_paths_require_confirmation(session, path):
    assert decide(session, request("edit", raw={"Path": path})) is None


def test_all_paths_must_be_inside_even_when_locations_omit_source(home, session):
    target = os.path.join(session.cwd, "target.txt")
    assert decide(session, request("move", [target], {"SourcePath": str(home / "source.txt"), "DestinationPath": target})) is None
    assert decide(session, request("move", [target, home / "other.txt"])) is None


def test_missing_empty_or_invalid_paths_require_confirmation(session):
    assert decide(session, request("edit")) is None
    assert decide(session, request("edit", raw={"Path": ""})) is None
    assert decide(session, request("edit", raw={"Path": "ok.txt", "OtherPath": None})) is None
    assert decide(session, request("edit", raw={"Paths": []})) is None


@pytest.mark.parametrize("kind", ["execute", "fetch", "other", "new_unknown_kind"])
def test_non_file_operations_require_confirmation_even_with_workspace_path(session, kind):
    assert decide(session, request(kind, [session.cwd])) is None


@pytest.mark.parametrize("name", sorted(KOPITIAM_TOOLS))
def test_exact_kopitiam_tools_are_allowed_once(session, name):
    assert decide(session, request("other", name=name))["optionId"] == "once"


@pytest.mark.parametrize("name", ["mcp_kopitiam_read_extra", "mcp_kopitiam_other", "other_mcp_kopitiam_read", "MCP_KOPITIAM_READ"])
def test_similar_mcp_names_are_not_automatically_allowed(session, name):
    assert decide(session, request("other", name=name)) is None


def test_all_policy_still_uses_allow_once_and_never_persists(session):
    params = request("execute")
    assert decide(session, params, policy="all")["optionId"] == "once"
    params["options"] = [{"optionId": "persistent", "kind": "allow_always"}]
    assert decide(session, params, policy="all") is None


def test_extra_directory_and_own_artifacts_are_in_scope(home, session):
    extra = home / "extra"
    artifacts = home / "brain" / "own-id"
    for path, kwargs in [(extra / "file", {"extra_dirs": [extra]}),
                         (artifacts / "plan.md", {"artifacts": artifacts})]:
        assert decide(session, request("edit", [path]), **kwargs)["optionId"] == "once"
    assert decide(session, request("edit", [home / "brain" / "other-id" / "plan.md"]), artifacts=artifacts) is None
    assert artifact_directory("../../bad") is None


@pytest.mark.skipif(os.name != "nt", reason="Windows junction and replay paths")
def test_junction_to_outside_is_not_in_workspace(home, session):
    import _winapi
    outside = home / "outside"
    outside.mkdir()
    junction = os.path.join(session.cwd, "link")
    _winapi.CreateJunction(str(outside), junction)
    assert decide(session, request("edit", [os.path.join(junction, "file.txt")])) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows path normalization")
def test_windows_case_slashes_and_replay_path(session):
    path = os.path.join(session.cwd, "new.txt").replace("\\", "/").upper()
    assert decide(session, request("edit", ["/" + path]))["optionId"] == "once"


def test_nested_paths_and_diff_paths_are_not_ignored(home, session):
    params = request("edit", raw={"edits": [{"FilePath": "a.txt"}, {"FilePath": "b.txt"}]})
    assert request_paths(params) == ["a.txt", "b.txt"]
    assert decide(session, params)["optionId"] == "once"
    params["toolCall"]["content"] = [{"type": "diff", "path": str(home / "outside.txt")}]
    assert decide(session, params) is None

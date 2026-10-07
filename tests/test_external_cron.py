from __future__ import annotations

import importlib.util
import json
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 7, 2, 3, 45, tzinfo=timezone.utc)


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load_script("workflow_guard")
cron = load_script("dispatch_workflow")


def environment(**changes):
    return {
        "GITHUB_REPOSITORY": "dspp779/legacy-iow-volume",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        **changes,
    }


@pytest.mark.parametrize("inputs", [{}, {"dry_run": True}, {"dry_run": "true"}])
def test_omitted_or_true_input_stays_dry_without_production_switch(inputs):
    assert guard.resolve_mode({"inputs": inputs}, environment()) == ("dry-run", False)


@pytest.mark.parametrize("inputs,settings", [
    ({"dry_run": "false"}, {}),
    ({"dry_run": "false", "confirm_production": "true"}, {}),
    ({"dry_run": "", "confirm_production": "true"}, {"LEGACY_IOW_PRODUCTION_ENABLED": "true"}),
    ({"dry_run": 0, "confirm_production": "true"}, {"LEGACY_IOW_PRODUCTION_ENABLED": "true"}),
    ({"dry_run": "true", "allow_empty_state": "true"}, {}),
])
def test_ambiguous_or_unapproved_production_inputs_fail_closed(inputs, settings):
    with pytest.raises(ValueError):
        guard.resolve_mode({"inputs": inputs}, environment(**settings))


def test_confirmed_dispatch_requires_production_switch_and_allows_explicit_recovery():
    event = {"inputs": {"dry_run": "false", "confirm_production": "true"}}
    with pytest.raises(ValueError, match="PRODUCTION_ENABLED"):
        guard.resolve_mode(event, environment())
    assert guard.resolve_mode(event, environment(LEGACY_IOW_PRODUCTION_ENABLED="true")) == ("production", False)
    event["inputs"]["allow_empty_state"] = "true"
    assert guard.resolve_mode(event, environment(LEGACY_IOW_PRODUCTION_ENABLED="true")) == ("production", True)


@pytest.mark.parametrize("changes", [
    {"GITHUB_REF": "refs/heads/test"},
    {"GITHUB_REF": "refs/tags/main"},
    {"GITHUB_REPOSITORY": "someone/legacy-iow-volume"},
    {"GITHUB_EVENT_NAME": "repository_dispatch"},
    {"GITHUB_EVENT_NAME": "pull_request"},
    {"GITHUB_EVENT_NAME": "schedule", "LEGACY_IOW_SCHEDULE_BACKUP_ENABLED": "true",
     "LEGACY_IOW_PRODUCTION_ENABLED": "true"},
])
def test_other_branches_repositories_and_events_cannot_enter_uploader(changes):
    with pytest.raises(ValueError):
        guard.resolve_mode({}, environment(**changes))


def test_missing_or_empty_state_blocks_production_but_not_explicit_recovery(tmp_path):
    path = tmp_path / "state.json"
    for contents in (None, "{}"):
        if contents is not None:
            path.write_text(contents)
        with pytest.raises(ValueError):
            guard.require_state(path, "production", False)
        guard.require_state(path, "dry-run", False)
        guard.require_state(path, "production", True)


@pytest.mark.parametrize("contents", [
    "not json", "[]", '{"pump": null}',
    '{"pump": {"seconds": true, "volume_m3": 10, "received_at": "now"}}',
    '{"pump": {"seconds": 20, "volume_m3": 10}}',
])
def test_recovery_never_bypasses_corrupt_state(contents, tmp_path):
    path = tmp_path / "state.json"
    path.write_text(contents)
    with pytest.raises(ValueError):
        guard.require_state(path, "production", True)


def test_valid_existing_state_is_preserved_and_fingerprinted(tmp_path):
    path = tmp_path / "state.json"
    assert guard.fingerprint(path) == "missing"
    contents = '{"150026": {"seconds": 35172, "volume_m3": 10552, "received_at": "2026-09-07 10:49:00"}}'
    path.write_text(contents)
    before = guard.fingerprint(path)
    guard.require_state(path, "production", False)
    assert guard.fingerprint(path) == before
    assert path.read_text() == contents


def test_offline_preview_sends_no_http_and_contains_no_token(monkeypatch, capsys):
    monkeypatch.setenv(cron.TOKEN_ENV, "never-print-this-secret")
    assert cron.main(["--print-request"]) == 0
    output = capsys.readouterr().out
    assert "never-print-this-secret" not in output
    request = json.loads(output)
    assert request["body"]["ref"] == "main"
    assert request["body"]["inputs"]["dry_run"] is True
    assert request["body"]["inputs"]["confirm_production"] is False


def test_production_dispatch_requires_host_gate_and_never_bootstraps_by_default():
    with pytest.raises(ValueError):
        cron.dispatch_payload(True, False, NOW, {})
    body = cron.dispatch_payload(True, False, NOW, {"LEGACY_IOW_DISPATCH_PRODUCTION_ENABLED": "true"})
    assert body["ref"] == "main"
    assert body["inputs"] == {
        "dry_run": False, "confirm_production": True, "allow_empty_state": False,
        "tick_id": "external-20261007T0200Z",
    }
    # Same cron bucket permits correlation, but dispatch is not deduplicated by GitHub.
    assert cron.dispatch_payload(True, False, NOW.replace(minute=4), {"LEGACY_IOW_DISPATCH_PRODUCTION_ENABLED": "true"}) == body


class Response:
    def __init__(self, status, body=b""):
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class FakeAPI:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        assert timeout == 20
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.parametrize("status,body,expected", [
    (204, b"", {}),
    (200, b'{"workflow_run_id": 123}', {"workflow_run_id": 123}),
])
def test_dispatch_acceptance_is_only_one_bounded_post(status, body, expected):
    api = FakeAPI(Response(status, body))
    payload = cron.dispatch_payload(False, False, NOW, {})
    assert cron.api_request(cron.BASE_URL + "/dispatches", "test-token", payload, api) == expected
    assert len(api.requests) == 1
    request = api.requests[0]
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer test-token"
    assert json.loads(request.data)["inputs"]["dry_run"] is True


@pytest.mark.parametrize("error", [
    TimeoutError("token-must-not-leak"),
    urllib.error.HTTPError("https://api.github.com", 401, "token-must-not-leak", {}, None),
    urllib.error.HTTPError("https://api.github.com", 403, "token-must-not-leak", {}, None),
    urllib.error.HTTPError("https://api.github.com", 429, "token-must-not-leak", {}, None),
    urllib.error.HTTPError("https://api.github.com", 502, "token-must-not-leak", {}, None),
])
def test_failures_never_retry_or_print_server_secrets(error):
    api = FakeAPI(error)
    with pytest.raises(RuntimeError) as caught:
        cron.api_request(cron.BASE_URL + "/dispatches", "test-token", {}, api)
    assert "token-must-not-leak" not in str(caught.value)
    assert len(api.requests) == 1


def run_record(**changes):
    return {
        "id": 123, "head_branch": "main", "event": "workflow_dispatch",
        "display_title": "legacy IoW volume | production | external-20261007T0200Z",
        "status": "completed", "conclusion": "success", "created_at": "2026-10-07T02:00:00Z",
        **changes,
    }


def test_health_excludes_dry_run_and_old_schedule_runs_and_reads_without_post(monkeypatch, capsys):
    records = [run_record(), run_record(display_title="legacy IoW volume | dry-run | manual"),
               run_record(event="schedule", conclusion="skipped", created_at="2026-10-07T02:03:00Z"),
               run_record(event="schedule", created_at="2026-10-07T02:02:00Z")]
    assert cron.check_health({"workflow_runs": records}, NOW, 15)["id"] == 123
    with pytest.raises(RuntimeError):
        cron.check_health({"workflow_runs": records[1:]}, NOW, 15)
    monkeypatch.setenv(cron.TOKEN_ENV, "test-token")
    requests = []

    def fake_request(url, token, payload=None):
        requests.append((url, payload))
        record = run_record(created_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
        return {"workflow_runs": [record]}

    monkeypatch.setattr(cron, "api_request", fake_request)
    assert cron.main(["--check-health"]) == 0
    assert requests[0][1] is None
    assert "/runs?" in requests[0][0]
    assert json.loads(capsys.readouterr().out)["healthy"] is True


@pytest.mark.parametrize("records", [
    [run_record(created_at="2026-10-07T01:40:00Z")],
    [run_record(head_branch="test")],
    [run_record(), run_record(id=124, status="queued", conclusion=None, created_at="2026-10-07T01:40:00Z")],
    [run_record(), run_record(id=124, conclusion="failure", created_at="2026-10-07T02:01:00Z")],
    # Late completion does not make an old tick healthy.
    [run_record(created_at="2026-10-07T01:40:00Z", updated_at="2026-10-07T02:03:00Z")],
])
def test_health_detects_stale_or_failed_production(records):
    with pytest.raises(RuntimeError):
        cron.check_health({"workflow_runs": records}, NOW, 15)


def test_workflow_keeps_guards_credentials_and_state_in_the_correct_order():
    workflow = yaml.load((ROOT / ".github/workflows/legacy-iow-volume.yml").read_text(), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["on"]["workflow_dispatch"]["inputs"]["dry_run"]["default"] == "true"
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {"group": "legacy-iow-volume", "cancel-in-progress": "false"}
    job = workflow["jobs"]["upload"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert "github.event_name == 'workflow_dispatch'" in job["if"]
    steps = job["steps"]
    names = [step.get("name", "") for step in steps]
    assert names.index("Resolve upload mode") < names.index("Restore upload progress")
    assert names.index("Require valid production state") < names.index("Write pump config")
    upload = next(step for step in steps if step.get("id") == "upload")
    for key in ("IOW_CLIENT_ID", "IOW_CLIENT_SECRET"):
        assert "steps.mode.outputs.mode == 'production'" in upload["env"][key]
    for name in ("Save upload progress", "Verify saved progress exists", "Require exact saved progress key"):
        step = next(step for step in steps if step.get("name") == name)
        assert "steps.mode.outputs.mode == 'production'" in step["if"]
        assert "steps.upload.outputs.state_changed == 'true'" in step["if"]
    saved = next(step for step in steps if step.get("id") == "saved")
    assert saved["with"]["fail-on-cache-miss"] == "true"
    assert "restore-keys" not in saved["with"]

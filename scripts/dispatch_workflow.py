"""One bounded dispatch per external cron tick, or a read-only health check.

Requires Python 3.9+ and LEGACY_IOW_GITHUB_TOKEN. No upload credentials belong
on the cron host. This client deliberately does not retry ambiguous POSTs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

REPOSITORY = "dspp779/legacy-iow-volume"
WORKFLOW = "legacy-iow-volume.yml"
BASE_URL = f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/{WORKFLOW}"
TOKEN_ENV = "LEGACY_IOW_GITHUB_TOKEN"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def dispatch_payload(production: bool, allow_empty: bool, now: datetime,
                     environment: dict) -> dict:
    if production and environment.get("LEGACY_IOW_DISPATCH_PRODUCTION_ENABLED") != "true":
        raise ValueError("--production requires LEGACY_IOW_DISPATCH_PRODUCTION_ENABLED=true")
    if allow_empty and not production:
        raise ValueError("--allow-empty-state requires --production")
    tick = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    tick = tick.replace(minute=(tick.minute // 5) * 5)
    return {
        "ref": "main",
        "inputs": {
            "dry_run": not production,
            "confirm_production": production,
            "allow_empty_state": allow_empty,
            "tick_id": f"external-{tick.strftime('%Y%m%dT%H%MZ')}",
        },
    }


def api_request(url: str, token: str, payload: dict | None = None, opener=None):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "legacy-iow-volume-external-cron",
        },
        method="POST" if payload is not None else "GET",
    )
    client = opener or urllib.request.build_opener(NoRedirect())
    try:
        with client.open(request, timeout=20) as response:
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as error:
        # Do not print server bodies, request headers, or token values.
        raise RuntimeError(f"GitHub HTTP {error.code}; check permissions/rate limits, then next tick") from None
    except OSError:
        raise RuntimeError("GitHub request failed or timed out; POST outcome may be unknown; no automatic retry") from None
    if status not in ({200, 204} if payload is not None else {200}):
        raise RuntimeError(f"unexpected GitHub HTTP {status}")
    return json.loads(body) if body else {}


def check_health(data: dict, now: datetime, max_age_minutes: int) -> dict:
    if max_age_minutes < 1:
        raise ValueError("max-age-minutes must be positive")
    now = now.astimezone(timezone.utc)
    cutoff = now - timedelta(minutes=max_age_minutes)
    production = [
        run for run in data.get("workflow_runs", [])
        if run.get("head_branch") == "main"
        and run.get("event") == "workflow_dispatch"
        and run.get("display_title", "").startswith("legacy IoW volume | production | ")
    ]
    recent_successes = []
    for run in production:
        created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        # Use creation time: a very late completion must not hide a stale tick.
        if run.get("status") == "completed" and run.get("conclusion") == "success" and cutoff <= created <= now:
            recent_successes.append(run)
        if run.get("status") != "completed" and created < cutoff:
            raise RuntimeError("production run queued/running beyond health threshold")
    if not recent_successes:
        raise RuntimeError("no recent successful production tick; check cron, queue, guard and state save")
    latest = max(production, key=lambda run: run["created_at"])
    if latest.get("status") == "completed" and latest.get("conclusion") != "success":
        raise RuntimeError(f"latest production tick ended with {latest.get('conclusion')}")
    return max(recent_successes, key=lambda run: run["created_at"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--allow-empty-state", action="store_true")
    parser.add_argument("--print-request", action="store_true", help="offline; never sends an HTTP request")
    parser.add_argument("--check-health", action="store_true", help="GET only; never dispatches")
    parser.add_argument("--max-age-minutes", type=int, default=15)
    args = parser.parse_args(argv)
    try:
        now = datetime.now(timezone.utc)
        if args.check_health and (args.production or args.allow_empty_state or args.print_request):
            raise ValueError("--check-health cannot be combined with dispatch options")
        if args.max_age_minutes < 1:
            raise ValueError("max-age-minutes must be positive")
        payload = None if args.check_health else dispatch_payload(
            args.production, args.allow_empty_state, now, os.environ)
        if args.print_request:
            print(json.dumps({"url": BASE_URL + "/dispatches", "body": payload}))
            return 0
        token = os.environ.get(TOKEN_ENV, "")
        if not token:
            raise ValueError(f"set {TOKEN_ENV} through the scheduler's secret store")
        if args.check_health:
            data = api_request(BASE_URL + "/runs?branch=main&per_page=100", token)
            run = check_health(data, now, args.max_age_minutes)
            print(json.dumps({"healthy": True, "run_id": run["id"], "created_at": run["created_at"]}))
        else:
            result = api_request(BASE_URL + "/dispatches", token, payload)
            print(json.dumps({"accepted": True, "tick_id": payload["inputs"]["tick_id"],
                              "mode": "production" if args.production else "dry-run",
                              "run_id": result.get("workflow_run_id")}))
    except (OSError, RuntimeError, ValueError) as error:
        print(f"external cron: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

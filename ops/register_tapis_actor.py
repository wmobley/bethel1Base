#!/usr/bin/env python3
"""Create or update the `bethel1base-nightly` Tapis actor.

Tapis Actors cron schedules run in UTC and reject a start datetime that has
already passed ("The starting datetime is old"). This script always computes
the next upcoming occurrence of `CRON_HOUR_UTC` rather than hardcoding a date,
so re-running it later doesn't require editing a stale timestamp.
`CRON_HOUR_UTC = 5` corresponds to midnight America/Chicago while CDT is active.

The actor cannot use a local bind mount like `-v "$(pwd)/bethel1Base/data:/data"`.
Instead it writes data under `/work`, which Tapis mounts into actor containers.

Upstream uses the same username and password as Tapis in this deployment, so
the actor reuses those credentials automatically.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from tapipy.tapis import Tapis

ACTOR_NAME = "bethel1base-nightly"
ACTOR_DESCRIPTION = "Nightly Bethel 1 telemetry fetch, transform, and Upstream upload job."
ACTOR_IMAGE = "ghcr.io/wmobley/bethel1base:sha-02cbd6f"
CRON_HOUR_UTC = 5
PERMISSION_USER = "wmobley"
PERMISSION_LEVEL = "UPDATE"


def default_cron_schedule(now: datetime | None = None) -> str:
    """Next occurrence of CRON_HOUR_UTC, recurring daily, formatted for Tapis."""
    now = now or datetime.now(timezone.utc)
    next_run = now.replace(hour=CRON_HOUR_UTC, minute=0, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    return f"{next_run.strftime('%Y-%m-%d')} {CRON_HOUR_UTC:02d} + 1 day"


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable in .env: {name}")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Register or update the bethel1base-nightly Tapis actor.")
    parser.add_argument(
        "--actor-id",
        default=os.getenv("TAPIS_ACTOR_ID", "").strip() or None,
        help="Existing actor id to update in place. Omit to create a new actor.",
    )
    parser.add_argument(
        "--run-now",
        action="store_true",
        default=os.getenv("RUN_NOW", "").strip().lower() in {"1", "true", "yes", "on"},
        help="Submit one manual test execution after the actor is registered.",
    )
    parser.add_argument(
        "--cron-schedule",
        default=os.getenv("TAPIS_CRON_SCHEDULE", "").strip() or None,
        help="Override the cron schedule string. Defaults to the next occurrence of "
        f"{CRON_HOUR_UTC:02d}:00 UTC, recurring daily.",
    )
    return parser.parse_args()


def tapis_request(base_url: str, headers: dict, method: str, path: str, payload: dict | None = None) -> dict:
    url = f"{base_url.rstrip('/')}{path}"
    response = requests.request(method, url, headers=headers, json=payload, timeout=60)
    try:
        data = response.json()
    except Exception:
        response.raise_for_status()
        return {"raw_text": response.text}

    if not response.ok:
        raise RuntimeError(json.dumps(data, indent=2))
    return data


def main() -> int:
    args = parse_args()

    load_dotenv(Path(".env"))
    load_dotenv(Path(".env.example"))

    tapis_base_url = os.getenv("TAPIS_BASE_URL", "https://portals.tapis.io")
    tapis_tenant_id = os.getenv("TAPIS_TENANT_ID", "portals")
    tapis_username = require_env("TAPIS_USERNAME")
    tapis_password = require_env("TAPIS_PASSWORD")

    ts_authkey = require_env("TS_AUTHKEY")
    remote_password = os.getenv("REMOTE_PASSWORD", "bethel1base")
    work_root = os.getenv("BETHEL1BASE_WORK_ROOT", "/work/bethel1Base/data")
    upstream_base_url = os.getenv("UPSTREAM_BASE_URL", "https://upstreamapi.pods.portals.tapis.io/")

    # Upstream uses the same credentials as Tapis for this deployment.
    upstream_username = tapis_username
    upstream_password = tapis_password

    default_environment = {
        "TS_AUTHKEY": ts_authkey,
        "REMOTE_PASSWORD": remote_password,
        "FETCH_MODE": "missing",
        "TRANSFORM_AFTER_FETCH": "true",
        "UPLOAD_AFTER_TRANSFORM": "true",
        "UPSTREAM_USERNAME": upstream_username,
        "UPSTREAM_PASSWORD": upstream_password,
        "UPSTREAM_BASE_URL": upstream_base_url,
        "LOCAL_OUTPUT_DIR": f"{work_root}/out",
        "TRANSFORM_OUTPUT_DIR": f"{work_root}/transformed",
        "UPLOAD_INPUT_DIR": f"{work_root}/transformed",
    }

    cron_schedule = args.cron_schedule or default_cron_schedule()

    actor_payload = {
        "image": ACTOR_IMAGE,
        "name": ACTOR_NAME,
        "description": ACTOR_DESCRIPTION,
        "default_environment": default_environment,
        "cron_schedule": cron_schedule,
        "cron_on": True,
        "token": False,
        "stateless": True,
    }

    print(json.dumps(
        {
            "base_url": tapis_base_url,
            "tenant_id": tapis_tenant_id,
            "actor_id": args.actor_id,
            "actor_name": ACTOR_NAME,
            "actor_image": ACTOR_IMAGE,
            "cron_schedule_utc": cron_schedule,
            "permission_grant": {PERMISSION_USER: PERMISSION_LEVEL},
            "work_root": work_root,
        },
        indent=2,
    ))

    t = Tapis(
        base_url=tapis_base_url,
        tenant_id=tapis_tenant_id,
        username=tapis_username,
        password=tapis_password,
    )
    t.get_tokens()
    access_token = t.access_token.access_token
    headers = {"X-Tapis-Token": access_token, "Content-Type": "application/json"}
    print("Authenticated as:", tapis_username)

    if args.actor_id:
        result = tapis_request(tapis_base_url, headers, "PUT", f"/v3/actors/{args.actor_id}", actor_payload)
    else:
        result = tapis_request(tapis_base_url, headers, "POST", "/v3/actors", actor_payload)
    print(json.dumps(result, indent=2))

    actor_result = result.get("result", result)
    actor_id = actor_result.get("id") or actor_result.get("actor_id") or args.actor_id
    print("Actor ID:", actor_id)

    permission_headers = {"X-Tapis-Token": access_token, "Content-Type": "application/x-www-form-urlencoded"}
    permission_response = requests.post(
        f"{tapis_base_url.rstrip('/')}/v3/actors/{actor_id}/permissions",
        headers=permission_headers,
        data={"user": PERMISSION_USER, "level": PERMISSION_LEVEL},
        timeout=60,
    )
    permission_result = permission_response.json()
    if not permission_response.ok:
        raise RuntimeError(json.dumps(permission_result, indent=2))
    print(json.dumps(permission_result, indent=2))

    actor_details = tapis_request(tapis_base_url, headers, "GET", f"/v3/actors/{actor_id}")
    print(json.dumps(actor_details, indent=2))

    if args.run_now:
        execution = tapis_request(
            tapis_base_url, headers, "POST", f"/v3/actors/{actor_id}/messages", {"message": "manual test"}
        )
        print(json.dumps(execution, indent=2))
    else:
        print("--run-now not set; no manual execution submitted.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

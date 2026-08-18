#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dateutil.parser import isoparse
from upstream.client import UpstreamClient


DEFAULT_UNIT_STATION_IDS = {
    "unit1": 23,
    "unit2": 20,
    "unit3": 21,
    "unit4": 22,
}

ANCHOR_SENSOR_ALIASES = ("fCnt", "rssi", "snr", "temperature_C")


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_unit_station_map(raw: str | None) -> dict[str, int]:
    if not raw:
        return dict(DEFAULT_UNIT_STATION_IDS)

    raw = raw.strip()
    if raw.startswith("{"):
        parsed = json.loads(raw)
        return {str(unit): int(station_id) for unit, station_id in parsed.items()}

    result: dict[str, int] = {}
    for item in raw.split(","):
        unit, station_id = item.split(":", 1)
        result[unit.strip()] = int(station_id.strip())
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload Bethel unit CSVs to Upstream.")
    parser.add_argument(
        "--input-dir",
        default=os.getenv("UPLOAD_INPUT_DIR", "/data/transformed"),
        help="Directory containing unitN_sensors.csv and unitN_measurements.csv files.",
    )
    parser.add_argument(
        "--campaign-id",
        type=int,
        default=int(os.getenv("UPSTREAM_CAMPAIGN_ID", "6")),
        help="Upstream campaign ID.",
    )
    parser.add_argument(
        "--unit-station-map",
        default=os.getenv("UPSTREAM_UNIT_STATION_MAP"),
        help="Unit to station map, either JSON or comma-separated unit1:5,unit2:6.",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("UPSTREAM_BASE_URL", "https://upstreamapi.pods.portals.tapis.io"),
        help="Upstream base URL.",
    )
    parser.add_argument(
        "--validate-data",
        action=argparse.BooleanOptionalAction,
        default=env_bool("UPSTREAM_UPLOAD_VALIDATE_DATA", True),
        help="Run SDK CSV validation before upload.",
    )
    parser.add_argument(
        "--stop-on-error",
        action=argparse.BooleanOptionalAction,
        default=env_bool("UPSTREAM_UPLOAD_STOP_ON_ERROR", False),
        help="Stop immediately on the first unit upload failure.",
    )
    return parser.parse_args()


def preflight_station(*, client: UpstreamClient, campaign_id: int, station_id: int) -> dict[str, Any]:
    campaign = client.get_campaign(campaign_id)
    station = client.get_station(station_id, campaign_id)
    return {
        "campaign_id": campaign.id,
        "campaign_name": getattr(campaign, "name", None),
        "station_id": station.id,
        "station_name": getattr(station, "name", None),
    }


def normalize_collectiontime(value: str) -> str:
    """Normalize a collectiontime string to a comparable UTC ISO-8601 form.

    Naive values are treated as UTC (bethel1Base writes UTC instants), so a
    naive CSV value and the API's aware representation of the same instant
    compare equal during incremental-upload dedupe.
    """
    parsed = isoparse(value)
    if not isinstance(parsed, datetime):
        # Date-only values carry no timezone; leave as-is.
        return parsed.isoformat()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def latest_collectiontime_from_rows(rows: list[dict[str, str]]) -> str | None:
    latest_raw: str | None = None
    latest_norm: str | None = None
    for row in rows:
        raw = (row.get("collectiontime") or "").strip()
        if not raw:
            continue
        normalized = normalize_collectiontime(raw)
        if latest_norm is None or normalized > latest_norm:
            latest_raw = raw
            latest_norm = normalized
    return latest_raw


def fetch_existing_station_timestamps(
    *,
    client: UpstreamClient,
    campaign_id: int,
    station_id: int,
) -> tuple[set[str], str | None]:
    sensors = client.sensors.list(campaign_id=campaign_id, station_id=station_id, limit=200, page=1)
    anchor_sensor = None
    for preferred_alias in ANCHOR_SENSOR_ALIASES:
        for sensor in sensors.items:
            if getattr(sensor, "alias", None) == preferred_alias:
                anchor_sensor = sensor
                break
        if anchor_sensor is not None:
            break

    if anchor_sensor is None and sensors.items:
        anchor_sensor = sensors.items[0]

    if anchor_sensor is None:
        return set(), None

    timestamps: set[str] = set()
    page = 1
    latest_seen: str | None = None
    while True:
        measurement_page = client.measurements.list(
            campaign_id=campaign_id,
            station_id=station_id,
            sensor_id=anchor_sensor.id,
            limit=1000,
            page=page,
        )
        items = list(getattr(measurement_page, "items", []) or [])
        if not items:
            break
        for item in items:
            raw = str(getattr(item, "collectiontime", "")).strip()
            if not raw:
                continue
            normalized = normalize_collectiontime(raw)
            timestamps.add(normalized)
            if latest_seen is None or normalized > latest_seen:
                latest_seen = normalized

        total_pages = int(getattr(measurement_page, "pages", page) or page)
        if page >= total_pages:
            break
        page += 1

    return timestamps, latest_seen


def build_incremental_measurements(
    *,
    measurements_file: Path,
    existing_timestamps: set[str],
) -> tuple[Path | None, int, str | None]:
    if not existing_timestamps:
        with measurements_file.open("r", newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        return measurements_file, len(rows), latest_collectiontime_from_rows(rows)

    temp_file = tempfile.NamedTemporaryFile(
        mode="w",
        newline="",
        encoding="utf-8",
        prefix="incremental_measurements_",
        suffix=".csv",
        delete=False,
    )
    latest_raw: str | None = None
    kept_rows = 0

    try:
        with measurements_file.open("r", newline="", encoding="utf-8-sig") as source, temp_file:
            reader = csv.DictReader(source)
            if reader.fieldnames is None:
                raise RuntimeError(f"Measurements file has no header: {measurements_file}")
            writer = csv.DictWriter(temp_file, fieldnames=reader.fieldnames)
            writer.writeheader()

            for row in reader:
                raw = (row.get("collectiontime") or "").strip()
                if not raw:
                    continue
                normalized = normalize_collectiontime(raw)
                if normalized in existing_timestamps:
                    continue
                writer.writerow(row)
                kept_rows += 1
                if latest_raw is None or normalized > normalize_collectiontime(latest_raw):
                    latest_raw = raw
    except Exception:
        Path(temp_file.name).unlink(missing_ok=True)
        raise

    if kept_rows == 0:
        Path(temp_file.name).unlink(missing_ok=True)
        return None, 0, None

    return Path(temp_file.name), kept_rows, latest_raw


def main() -> int:
    args = parse_args()
    input_dir = Path(args.input_dir).expanduser().resolve()
    unit_station_ids = parse_unit_station_map(args.unit_station_map)

    client = UpstreamClient(
        username=require_env("UPSTREAM_USERNAME"),
        password=require_env("UPSTREAM_PASSWORD"),
        base_url=args.base_url,
    )
    client.authenticate()

    manifest: dict[str, Any] = {
        "campaign_id": args.campaign_id,
        "base_url": args.base_url,
        "validate_data": args.validate_data,
        "units": [],
    }
    uploads: list[dict[str, Any]] = []
    failures = 0
    for unit, station_id in unit_station_ids.items():
        incremental_measurements: Path | None = None
        unit_result: dict[str, Any] = {
            "unit": unit,
            "campaign_id": args.campaign_id,
            "station_id": station_id,
            "status": "pending",
        }
        try:
            print(f"Preflight {unit} against campaign {args.campaign_id}, station {station_id}")
            unit_result["preflight"] = preflight_station(
                client=client,
                campaign_id=args.campaign_id,
                station_id=station_id,
            )
            sensors_file = input_dir / f"{unit}_sensors.csv"
            measurements_file = input_dir / f"{unit}_measurements.csv"
            existing_timestamps, latest_existing = fetch_existing_station_timestamps(
                client=client,
                campaign_id=args.campaign_id,
                station_id=station_id,
            )
            incremental_measurements, incremental_rows, latest_raw = build_incremental_measurements(
                measurements_file=measurements_file,
                existing_timestamps=existing_timestamps,
            )
            unit_result["existing_timestamp_count"] = len(existing_timestamps)
            unit_result["latest_existing_collectiontime"] = latest_existing
            unit_result["candidate_measurements_file"] = str(measurements_file)
            unit_result["incremental_rows"] = incremental_rows
            if incremental_measurements is None:
                unit_result["status"] = "skipped"
                unit_result["reason"] = "No measurement timestamps were missing from the target station."
                print(f"Skipping {unit}: no timestamps missing on station {station_id}")
                manifest["units"].append(unit_result)
                continue

            print(f"Uploading {unit} to campaign {args.campaign_id}, station {station_id}")
            upload_response = client.upload_csv_data(
                campaign_id=args.campaign_id,
                station_id=station_id,
                sensors_file=sensors_file,
                measurements_file=incremental_measurements,
                validate_data=args.validate_data,
            )
            upload_result = {
                "unit": unit,
                "campaign_id": args.campaign_id,
                "station_id": station_id,
                "sensors_file": str(sensors_file),
                "measurements_file": str(incremental_measurements),
                "upload_result": upload_response,
            }
            uploads.append(upload_result)
            unit_result["status"] = "uploaded"
            unit_result["upload_result"] = upload_result["upload_result"]
            unit_result["sensors_file"] = upload_result["sensors_file"]
            unit_result["measurements_file"] = upload_result["measurements_file"]
            unit_result["uploaded_through_collectiontime"] = latest_raw
        except Exception as exc:
            failures += 1
            unit_result["status"] = "failed"
            unit_result["error_type"] = type(exc).__name__
            unit_result["error"] = str(exc)
            print(f"Upload failed for {unit}: {exc}")
            if args.stop_on_error:
                manifest["units"].append(unit_result)
                break
        finally:
            if isinstance(incremental_measurements, Path):
                if incremental_measurements.name.startswith("incremental_measurements_"):
                    incremental_measurements.unlink(missing_ok=True)
        manifest["units"].append(unit_result)

    manifest_path = input_dir / "upload_manifest.json"
    manifest["uploaded_count"] = len(uploads)
    manifest["failed_count"] = failures
    manifest["success"] = failures == 0
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"Wrote {manifest_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

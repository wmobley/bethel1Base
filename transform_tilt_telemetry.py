#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import OrderedDict
from pathlib import Path


DEVICE_STATIONS: OrderedDict[str, dict[str, object]] = OrderedDict(
    [
        (
            "ad59aefd850e4877",
            {"unit": "unit1", "name": "Unit 1", "lat": 60.79303861, "lon": -161.78003290},
        ),
        (
            "ae59aefd850e4877",
            {"unit": "unit2", "name": "Unit 2", "lat": 60.79303026, "lon": -161.77998094},
        ),
        (
            "af59aefd850e4877",
            {"unit": "unit3", "name": "Unit 3", "lat": 60.79302229, "lon": -161.77992692},
        ),
        (
            "b059aefd850e4877",
            {"unit": "unit4", "name": "Unit 4", "lat": 60.79301515, "lon": -161.77987402},
        ),
    ]
)

FIELD_METADATA: OrderedDict[str, dict[str, str]] = OrderedDict(
    [
        ("temperature_C", {"name": "Temperature", "units": "degC"}),
        ("voltage_V", {"name": "Battery Voltage", "units": "V"}),
        ("x_raw", {"name": "X Raw", "units": "count"}),
        ("y_raw", {"name": "Y Raw", "units": "count"}),
        ("x_deg", {"name": "X Tilt", "units": "deg"}),
        ("y_deg", {"name": "Y Tilt", "units": "deg"}),
        ("fCnt", {"name": "Frame Count", "units": "count"}),
        ("rssi", {"name": "RSSI", "units": "dBm"}),
        ("snr", {"name": "Signal-to-Noise Ratio", "units": "dB"}),
    ]
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transform Bethel tilt telemetry CSV into one Upstream sensors.csv/measurements.csv pair per unit."
    )
    parser.add_argument("input_csv", nargs="+", help="One or more source tilt telemetry CSV files.")
    parser.add_argument(
        "--output-dir",
        default="bethel1Base/data/transformed",
        help="Directory where per-unit output folders will be written.",
    )
    parser.add_argument(
        "--lat",
        type=float,
        default=None,
        help="Fallback latitude for unknown devEui values not in DEVICE_STATIONS.",
    )
    parser.add_argument(
        "--lon",
        type=float,
        default=None,
        help="Fallback longitude for unknown devEui values not in DEVICE_STATIONS.",
    )
    return parser.parse_args()


def discover_devices(rows: list[dict[str, str]]) -> list[str]:
    seen: OrderedDict[str, None] = OrderedDict()
    for row in rows:
        dev_eui = row["devEui"].strip()
        if dev_eui and dev_eui not in seen:
            seen[dev_eui] = None
    return list(seen.keys())


def build_sensors_rows(dev_eui: str) -> list[dict[str, str]]:
    sensor_rows: list[dict[str, str]] = []
    for field_name, metadata in FIELD_METADATA.items():
        sensor_rows.append(
            {
                "alias": field_name,
                "variablename": f"{metadata['name']} ({dev_eui})",
                "postprocess": "False",
                "units": metadata["units"],
                "datatype": "1",
            }
        )
    return sensor_rows


def build_measurements_rows(
    rows: list[dict[str, str]],
    *,
    lat: float,
    lon: float,
) -> list[dict[str, str]]:
    measurements_rows: list[dict[str, str]] = []

    for row in rows:
        measurement_row = {field_name: "" for field_name in FIELD_METADATA}
        measurement_row["collectiontime"] = row["time_utc"].strip()
        measurement_row["Lat_deg"] = str(lat)
        measurement_row["Lon_deg"] = str(lon)

        for field_name in FIELD_METADATA:
            value = row.get(field_name, "").strip()
            if value:
                measurement_row[field_name] = value

        measurements_rows.append(measurement_row)

    return measurements_rows


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def station_metadata(dev_eui: str, fallback_lat: float | None, fallback_lon: float | None) -> dict[str, object]:
    if dev_eui in DEVICE_STATIONS:
        return DEVICE_STATIONS[dev_eui]
    if fallback_lat is None or fallback_lon is None:
        raise SystemExit(
            f"Unknown devEui {dev_eui}. Add it to DEVICE_STATIONS or pass --lat and --lon fallback coordinates."
        )
    return {"unit": dev_eui, "name": dev_eui, "lat": fallback_lat, "lon": fallback_lon}


def safe_dir_name(value: object) -> str:
    return str(value).strip().lower().replace(" ", "_")


def main() -> int:
    args = parse_args()
    input_paths = [Path(value).expanduser().resolve() for value in args.input_csv]
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, str]] = []
    for input_path in input_paths:
        with input_path.open("r", newline="", encoding="utf-8-sig") as handle:
            rows.extend(csv.DictReader(handle))

    if not rows:
        raise SystemExit(f"No rows found in input files: {', '.join(str(path) for path in input_paths)}")

    devices = discover_devices(rows)
    output_manifest: list[dict[str, object]] = []
    total_rows = 0

    for dev_eui in devices:
        metadata = station_metadata(dev_eui, args.lat, args.lon)
        device_rows = [row for row in rows if row["devEui"].strip() == dev_eui]
        unit_name = safe_dir_name(metadata["unit"])
        sensors_path = output_dir / f"{unit_name}_sensors.csv"
        measurements_path = output_dir / f"{unit_name}_measurements.csv"
        measurements_rows = build_measurements_rows(
            device_rows,
            lat=float(metadata["lat"]),
            lon=float(metadata["lon"]),
        )

        write_csv(
            sensors_path,
            ["alias", "variablename", "postprocess", "units", "datatype"],
            build_sensors_rows(dev_eui),
        )
        write_csv(
            measurements_path,
            ["collectiontime", "Lat_deg", "Lon_deg", *FIELD_METADATA.keys()],
            measurements_rows,
        )

        total_rows += len(measurements_rows)
        output_manifest.append(
            {
                "devEui": dev_eui,
                "unit": metadata["unit"],
                "name": metadata["name"],
                "lat": metadata["lat"],
                "lon": metadata["lon"],
                "rows": len(measurements_rows),
                "sensors_csv": str(sensors_path),
                "measurements_csv": str(measurements_path),
            }
        )

    manifest_path = output_dir / "transform_manifest.json"
    manifest_path.write_text(json.dumps(output_manifest, indent=2) + "\n", encoding="utf-8")

    print(f"Wrote {manifest_path}")
    print(f"Input files: {len(input_paths)}")
    print(f"Devices: {', '.join(devices)}")
    print(f"Rows transformed: {total_rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

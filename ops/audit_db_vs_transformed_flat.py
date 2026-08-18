# %%
"""Audit Bethel transformed-flat CSVs against the Upstream database.

Run this as a Jupyter/VS Code notebook-style script, or execute it with Python.

Expected packages:
    pandas sqlalchemy psycopg

Set DATABASE_URL before running, or paste it when prompted. Example:
    postgresql+psycopg://fastapi_traefik:fastapi_traefik@upstreampostgres.pods.portals.tapis.io:443/fastapi_traefik?sslmode=require
"""

from __future__ import annotations

import os
from getpass import getpass
from pathlib import Path

import pandas as pd
from sqlalchemy import bindparam, create_engine, text


# %%
# Configuration
SCRIPT_DIR = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
DATA_DIR = SCRIPT_DIR / "data" / "transformed-flat"
OUT_DIR = SCRIPT_DIR / "data" / "audit-db-vs-transformed-flat"
OUT_DIR.mkdir(parents=True, exist_ok=True)

CAMPAIGN_ID = 6
UNIT_STATION_MAP = {
    "unit1": 23,
    "unit2": 20,
    "unit3": 21,
    "unit4": 22,
}

DATABASE_URL = os.getenv("DATABASE_URL") or getpass("DATABASE_URL: ")
engine = create_engine(DATABASE_URL)


# %%
def normalize_time_series(values: pd.Series) -> pd.Series:
    """Match Postgres/API timestamp precision for comparison."""
    return pd.to_datetime(values, utc=True).dt.round("us").dt.tz_convert(None)


def load_local_unit(unit: str, station_id: int) -> pd.DataFrame:
    sensors_path = DATA_DIR / f"{unit}_sensors.csv"
    measurements_path = DATA_DIR / f"{unit}_measurements.csv"

    sensors = pd.read_csv(sensors_path, keep_default_na=False)
    aliases = sensors["alias"].astype(str).tolist()

    measurements = pd.read_csv(measurements_path, keep_default_na=False)
    measurements["collectiontime_norm"] = normalize_time_series(measurements["collectiontime"])

    long = measurements.melt(
        id_vars=["collectiontime", "collectiontime_norm", "Lat_deg", "Lon_deg"],
        value_vars=[alias for alias in aliases if alias in measurements.columns],
        var_name="alias",
        value_name="local_value",
    )
    long = long[long["local_value"].astype(str).str.strip() != ""].copy()
    long["local_value"] = pd.to_numeric(long["local_value"], errors="coerce")
    long["unit"] = unit
    long["stationid"] = station_id
    return long[
        [
            "unit",
            "stationid",
            "alias",
            "collectiontime",
            "collectiontime_norm",
            "Lat_deg",
            "Lon_deg",
            "local_value",
        ]
    ]


local_df = pd.concat(
    [load_local_unit(unit, station_id) for unit, station_id in UNIT_STATION_MAP.items()],
    ignore_index=True,
)

print(f"Loaded {len(local_df):,} local measurement values from {DATA_DIR}")
display(
    local_df.groupby(["unit", "stationid", "alias"], as_index=False)
    .size()
    .rename(columns={"size": "local_count"})
)


# %%
station_ids = list(UNIT_STATION_MAP.values())
sensor_query = (
    text(
        """
        select
            st.stationid,
            st.campaignid,
            st.name as station_name,
            s.sensorid,
            s.alias
        from stations st
        join sensors s on s.stationid = st.stationid
        where st.campaignid = :campaign_id
          and st.stationid in :station_ids
        """
    )
    .bindparams(bindparam("station_ids", expanding=True))
)

with engine.connect() as conn:
    db_sensors = pd.read_sql(
        sensor_query,
        conn,
        params={"campaign_id": CAMPAIGN_ID, "station_ids": station_ids},
    )

station_to_unit = {station_id: unit for unit, station_id in UNIT_STATION_MAP.items()}
db_sensors["unit"] = db_sensors["stationid"].map(station_to_unit)

print(f"Loaded {len(db_sensors):,} database sensors")
display(db_sensors.sort_values(["unit", "alias"]))


# %%
measurement_query = (
    text(
        """
        select
            st.stationid,
            s.sensorid,
            s.alias,
            m.collectiontime,
            m.measurementvalue as db_value
        from measurements m
        join sensors s on s.sensorid = m.sensorid
        join stations st on st.stationid = s.stationid
        where st.campaignid = :campaign_id
          and st.stationid in :station_ids
          and s.alias in :aliases
        """
    )
    .bindparams(
        bindparam("station_ids", expanding=True),
        bindparam("aliases", expanding=True),
    )
)

aliases = sorted(local_df["alias"].unique().tolist())
with engine.connect() as conn:
    db_df = pd.read_sql(
        measurement_query,
        conn,
        params={
            "campaign_id": CAMPAIGN_ID,
            "station_ids": station_ids,
            "aliases": aliases,
        },
    )

db_df["unit"] = db_df["stationid"].map(station_to_unit)
db_df["collectiontime_norm"] = normalize_time_series(db_df["collectiontime"])
db_df["db_value"] = pd.to_numeric(db_df["db_value"], errors="coerce")

print(f"Loaded {len(db_df):,} database measurement values")
display(
    db_df.groupby(["unit", "stationid", "alias"], as_index=False)
    .size()
    .rename(columns={"size": "db_count"})
    .sort_values(["unit", "alias"])
)


# %%
key_cols = ["stationid", "alias", "collectiontime_norm"]
local_keys = local_df[key_cols + ["unit", "collectiontime", "Lat_deg", "Lon_deg", "local_value"]]
db_keys = db_df[key_cols + ["sensorid", "collectiontime", "db_value"]].rename(
    columns={"collectiontime": "db_collectiontime"}
)

comparison = local_keys.merge(db_keys, on=key_cols, how="left", indicator=True)
missing = comparison[comparison["_merge"] == "left_only"].drop(columns=["_merge"]).copy()
present = comparison[comparison["_merge"] == "both"].drop(columns=["_merge"]).copy()

db_extra = db_keys.merge(local_keys[key_cols], on=key_cols, how="left", indicator=True)
db_extra = db_extra[db_extra["_merge"] == "left_only"].drop(columns=["_merge"]).copy()
db_extra["unit"] = db_extra["stationid"].map(station_to_unit)

summary = (
    local_df.groupby(["unit", "stationid", "alias"], as_index=False)
    .size()
    .rename(columns={"size": "local_count"})
    .merge(
        db_df.groupby(["unit", "stationid", "alias"], as_index=False)
        .size()
        .rename(columns={"size": "db_count"}),
        on=["unit", "stationid", "alias"],
        how="outer",
    )
    .merge(
        missing.groupby(["unit", "stationid", "alias"], as_index=False)
        .size()
        .rename(columns={"size": "missing_count"}),
        on=["unit", "stationid", "alias"],
        how="left",
    )
    .merge(
        db_extra.groupby(["unit", "stationid", "alias"], as_index=False)
        .size()
        .rename(columns={"size": "db_extra_count"}),
        on=["unit", "stationid", "alias"],
        how="left",
    )
    .fillna({"local_count": 0, "db_count": 0, "missing_count": 0, "db_extra_count": 0})
)

count_cols = ["local_count", "db_count", "missing_count", "db_extra_count"]
summary[count_cols] = summary[count_cols].astype(int)
summary = summary.sort_values(["unit", "alias"])

summary_path = OUT_DIR / "summary_by_unit_alias.csv"
missing_path = OUT_DIR / "missing_local_rows.csv"
extra_path = OUT_DIR / "db_extra_rows.csv"

summary.to_csv(summary_path, index=False)
missing.to_csv(missing_path, index=False)
db_extra.to_csv(extra_path, index=False)

print(f"Wrote {summary_path}")
print(f"Wrote {missing_path}")
print(f"Wrote {extra_path}")
display(summary)


# %%
if missing.empty:
    print("No local transformed-flat rows are missing from the database for the configured stations.")
else:
    print(f"{len(missing):,} local transformed-flat rows are missing from the database.")
    display(missing.sort_values(["unit", "alias", "collectiontime_norm"]).head(50))


# %%
# Optional: compare values for rows with matching station + alias + timestamp.
value_check = present.copy()
value_check["abs_delta"] = (value_check["local_value"] - value_check["db_value"]).abs()
value_mismatches = value_check[value_check["abs_delta"] > 1e-9].copy()
value_mismatch_path = OUT_DIR / "value_mismatches.csv"
value_mismatches.to_csv(value_mismatch_path, index=False)

print(f"Wrote {value_mismatch_path}")
print(f"Value mismatches: {len(value_mismatches):,}")
display(
    value_mismatches.sort_values(["unit", "alias", "collectiontime_norm"]).head(50)
)

# bethel1Base

First-stage container for Bethel 1 that connects to the site over Tailscale and downloads tilt telemetry CSV files from the logger host.

## What it does

This image:

1. Starts `tailscaled` in userspace mode inside the container.
2. Authenticates to your tailnet with `TS_AUTHKEY`.
3. Uses `ssh`/`scp` over the Tailscale SOCKS5 proxy to reach `bethel1base`.
4. Pulls files from `/var/log/tilt_telemetry/` into a mounted output directory.

This is intentionally limited to data retrieval. It does not transform or upload to UpStream yet.
It can now optionally transform the pulled tilt CSV into Upstream-style `sensors.csv` and `measurements.csv` inside the same container run.

## Required environment variables

- `REMOTE_PASSWORD`: SSH password for `bethel1@bethel1base`

## Optional environment variables

- `TS_AUTHKEY`: Tailscale node auth key for non-interactive login
- `REMOTE_HOST`: defaults to `100.77.233.103`
- `REMOTE_USER`: defaults to `bethel1`
- `REMOTE_SOURCE_DIR`: defaults to `/var/log/tilt_telemetry`
- `REMOTE_GLOB`: defaults to `tilt_telemetry_*.csv`
- `LOCAL_OUTPUT_DIR`: defaults to `/data/out`
- `FETCH_MODE`: one of `missing`, `all`, `latest`, `since-date`; defaults to `missing`
- `DOWNLOAD_SINCE`: required when `FETCH_MODE=since-date`, format `YYYY-MM-DD`
- `TAILSCALE_HOSTNAME`: defaults to `bethel1base-puller`
- `TAILSCALE_SOCKS_PORT`: defaults to `1055`
- `TAILSCALE_LOGIN_TIMEOUT_SECONDS`: defaults to `300`
- `STRICT_HOST_KEY_CHECKING`: defaults to `false`
- `TAILSCALE_EXTRA_UP_ARGS`: extra flags passed to `tailscale up`
- `TRANSFORM_AFTER_FETCH`: set to `true` to run the transformer after download
- `TRANSFORM_INPUT_CSV`: optional explicit input file or comma-separated list of files; if omitted, all `tilt_telemetry_*.csv` files in `LOCAL_OUTPUT_DIR` are combined
- `TRANSFORM_OUTPUT_DIR`: defaults to `/data/transformed`
- `TRANSFORM_LAT`: optional fallback latitude for unknown future `devEui` values
- `TRANSFORM_LON`: optional fallback longitude for unknown future `devEui` values
- `UPLOAD_AFTER_TRANSFORM`: set to `true` to upload the per-unit CSVs to Upstream
- `UPSTREAM_USERNAME`: required when `UPLOAD_AFTER_TRANSFORM=true`
- `UPSTREAM_PASSWORD`: required when `UPLOAD_AFTER_TRANSFORM=true`
- `UPSTREAM_BASE_URL`: defaults to `https://upstreamapi.pods.portals.tapis.io`
- `UPSTREAM_CAMPAIGN_ID`: defaults to `6`
- `UPSTREAM_UNIT_STATION_MAP`: defaults to `unit1:23,unit2:20,unit3:21,unit4:22`
- `UPLOAD_INPUT_DIR`: defaults to `TRANSFORM_OUTPUT_DIR`
- `UPSTREAM_UPLOAD_VALIDATE_DATA`: defaults to `true`
- `UPSTREAM_UPLOAD_STOP_ON_ERROR`: defaults to `false`

## Build

```bash
docker build -t bethel1base-puller ./bethel1Base
```

## Run

```bash
docker run --rm \
  -e TS_AUTHKEY='tskey-auth-xxxxxxxxxxxx' \
  -e REMOTE_PASSWORD='bethel1base' \
  -e FETCH_MODE='missing' \
  -v "$(pwd)/bethel1Base/data:/data" \
  bethel1base-puller
```

## Run With Transform

This pulls data and then writes Upstream-format CSVs into `./bethel1Base/data/transformed/`:

```bash
docker run --rm \
  -e TS_AUTHKEY='tskey-auth-xxxxxxxxxxxx' \
  -e REMOTE_PASSWORD='bethel1base' \
  -e FETCH_MODE='latest' \
  -e TRANSFORM_AFTER_FETCH='true' \
  -v "$(pwd)/bethel1Base/data:/data" \
  bethel1base-puller
```

## Run With Transform And Upload

This pulls the logger data, writes four per-unit Upstream CSV pairs, and uploads them to campaign `6` stations `23,20,21,22`:

```bash
docker run --rm \
  -e TS_AUTHKEY='tskey-auth-xxxxxxxxxxxx' \
  -e REMOTE_PASSWORD='bethel1base' \
  -e FETCH_MODE='missing' \
  -e TRANSFORM_AFTER_FETCH='true' \
  -e UPLOAD_AFTER_TRANSFORM='true' \
  -e UPSTREAM_USERNAME='your-upstream-username' \
  -e UPSTREAM_PASSWORD='your-upstream-password' \
  -e UPSTREAM_BASE_URL='https://upstreamapi.pods.portals.tapis.io' \
  -v "$(pwd)/bethel1Base/data:/data" \
  bethel1base-puller
```

Default upload mapping:

- `unit1_measurements.csv` and `unit1_sensors.csv` upload to campaign `6`, station `23`
- `unit2_measurements.csv` and `unit2_sensors.csv` upload to campaign `6`, station `20`
- `unit3_measurements.csv` and `unit3_sensors.csv` upload to campaign `6`, station `21`
- `unit4_measurements.csv` and `unit4_sensors.csv` upload to campaign `6`, station `22`

After upload, the container writes `upload_manifest.json` in the transformed output directory.
Before uploading each unit, the uploader queries existing timestamps on the target station using one anchor sensor and only sends rows whose `collectiontime` is not already present there.

## Run Without TS_AUTHKEY

If you do not have a Tailscale node auth key, omit `TS_AUTHKEY`. The container will print a `https://login.tailscale.com/a/...` URL and wait up to 5 minutes for you to approve the node in your browser.

```bash
docker run --rm \
  -e REMOTE_PASSWORD='bethel1base' \
  -e FETCH_MODE='latest' \
  -v "$(pwd)/bethel1Base/data:/data" \
  bethel1base-puller
```

Downloaded files will land in `./bethel1Base/data/out/`, and each run writes `pull_manifest.json` alongside them.

## Transform To Upstream CSVs

Use the transformer to convert one pulled tilt telemetry file into `sensors.csv` and `measurements.csv`:

```bash
python3 ./bethel1Base/transform_tilt_telemetry.py \
  ./bethel1Base/data/out/tilt_telemetry_2026-04-22.csv \
  --output-dir ./bethel1Base/data/transformed
```

This writes:

- `./bethel1Base/data/transformed/unit1_sensors.csv`
- `./bethel1Base/data/transformed/unit1_measurements.csv`
- `./bethel1Base/data/transformed/unit2_sensors.csv`
- `./bethel1Base/data/transformed/unit2_measurements.csv`
- `./bethel1Base/data/transformed/unit3_sensors.csv`
- `./bethel1Base/data/transformed/unit3_measurements.csv`
- `./bethel1Base/data/transformed/unit4_sensors.csv`
- `./bethel1Base/data/transformed/unit4_measurements.csv`

The transformer treats each `devEui` as a separate station and writes one measurements file plus one sensors file per unit.
Within each station, sensor aliases are the metric names, for example `x_deg`, `y_deg`, and `temperature_C`.
When multiple tilt files are provided, it combines each unit's rows into that unit's `measurements.csv`.

## Admin / one-off scripts

`ops/` holds scripts that operate on this project but are not baked into the container image:

- `ops/register_tapis_actor.py`: creates or updates the `bethel1base-nightly` Tapis actor. Reads Tapis
  credentials and `TS_AUTHKEY` from `.env`. Pass `--actor-id <id>` to update an existing actor in place
  (omit to create a new one), and `--run-now` to submit one manual test execution after registering.

  ```bash
  cd bethel1Base
  python3 ops/register_tapis_actor.py --actor-id <existing-actor-id>
  ```

- `ops/audit_db_vs_transformed_flat.py`: audits transformed-flat CSVs in `./data/transformed-flat/` against
  the Upstream database. Requires `DATABASE_URL` (prompted if unset) and the `pandas`, `sqlalchemy`, and
  `psycopg` packages.

## Notes

- `userspace-networking` is used so this can run in a constrained container environment such as a future Tapis actor.
- `FETCH_MODE=missing` is the safest default for repeated runs against a persistent mounted volume.
- If the actor runtime does not allow long-lived background processes or outbound tailnet auth, the next step may need a sidecar or a different network pattern. This image is the smallest realistic starting point for proving remote access first.

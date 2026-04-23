#!/usr/bin/env bash
set -euo pipefail

TAILSCALE_SOCKET="${TAILSCALE_SOCKET:-/tmp/tailscaled.sock}"
TAILSCALE_STATE_DIR="${TAILSCALE_STATE_DIR:-/tmp/tailscale-state}"
TAILSCALE_SOCKS_PORT="${TAILSCALE_SOCKS_PORT:-1055}"
TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME:-bethel1base-puller}"
TAILSCALE_EXTRA_UP_ARGS="${TAILSCALE_EXTRA_UP_ARGS:-}"
TAILSCALE_LOGIN_TIMEOUT_SECONDS="${TAILSCALE_LOGIN_TIMEOUT_SECONDS:-300}"
TRANSFORM_AFTER_FETCH="${TRANSFORM_AFTER_FETCH:-false}"
TRANSFORM_INPUT_CSV="${TRANSFORM_INPUT_CSV:-}"
TRANSFORM_OUTPUT_DIR="${TRANSFORM_OUTPUT_DIR:-/data/transformed}"
TRANSFORM_LAT="${TRANSFORM_LAT:-}"
TRANSFORM_LON="${TRANSFORM_LON:-}"
LOCAL_OUTPUT_DIR="${LOCAL_OUTPUT_DIR:-/data/out}"
UPLOAD_AFTER_TRANSFORM="${UPLOAD_AFTER_TRANSFORM:-false}"
UPLOAD_INPUT_DIR="${UPLOAD_INPUT_DIR:-${TRANSFORM_OUTPUT_DIR}}"
UPSTREAM_CAMPAIGN_ID="${UPSTREAM_CAMPAIGN_ID:-6}"

mkdir -p "${TAILSCALE_STATE_DIR}"

cleanup() {
  if [[ -n "${TAILSCALED_PID:-}" ]]; then
    kill "${TAILSCALED_PID}" >/dev/null 2>&1 || true
    wait "${TAILSCALED_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

/usr/sbin/tailscaled \
  --state="${TAILSCALE_STATE_DIR}/tailscaled.state" \
  --socket="${TAILSCALE_SOCKET}" \
  --tun=userspace-networking \
  --socks5-server="127.0.0.1:${TAILSCALE_SOCKS_PORT}" &
TAILSCALED_PID=$!

for _ in $(seq 1 30); do
  if tailscale --socket="${TAILSCALE_SOCKET}" status >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if [[ -n "${TS_AUTHKEY:-}" ]]; then
  tailscale --socket="${TAILSCALE_SOCKET}" up \
    --authkey="${TS_AUTHKEY}" \
    --hostname="${TAILSCALE_HOSTNAME}" \
    ${TAILSCALE_EXTRA_UP_ARGS}
else
  echo "TS_AUTHKEY not set. Starting interactive Tailscale login flow." >&2
  tailscale --socket="${TAILSCALE_SOCKET}" up \
    --hostname="${TAILSCALE_HOSTNAME}" \
    ${TAILSCALE_EXTRA_UP_ARGS} >/tmp/tailscale-up.log 2>&1 &
  TAILSCALE_UP_PID=$!

  login_url=""
  for _ in $(seq 1 "${TAILSCALE_LOGIN_TIMEOUT_SECONDS}"); do
    if [[ -z "${login_url}" ]] && [[ -f /tmp/tailscale-up.log ]]; then
      login_url="$(grep -Eo 'https://login\.tailscale\.com/a/[A-Za-z0-9]+' /tmp/tailscale-up.log | tail -n 1 || true)"
      if [[ -n "${login_url}" ]]; then
        echo "Authenticate this container in Tailscale:" >&2
        echo "${login_url}" >&2
      fi
    fi

    if tailscale --socket="${TAILSCALE_SOCKET}" status 2>/tmp/tailscale-status.err | grep -qv '^Logged out\.$'; then
      wait "${TAILSCALE_UP_PID}" || true
      break
    fi

    if ! kill -0 "${TAILSCALE_UP_PID}" >/dev/null 2>&1; then
      cat /tmp/tailscale-up.log >&2 || true
      echo "Interactive Tailscale login exited before the node authenticated." >&2
      exit 1
    fi

    sleep 1
  done

  if ! tailscale --socket="${TAILSCALE_SOCKET}" status 2>/tmp/tailscale-status.err | grep -qv '^Logged out\.$'; then
    cat /tmp/tailscale-up.log >&2 || true
    echo "Timed out waiting for interactive Tailscale login after ${TAILSCALE_LOGIN_TIMEOUT_SECONDS}s." >&2
    exit 1
  fi
fi

if [[ -n "${REMOTE_HOST:-}" ]]; then
  tailscale --socket="${TAILSCALE_SOCKET}" ping --timeout=10s "${REMOTE_HOST}" || true
fi

python /app/fetch_tilt_telemetry.py

if [[ "${TRANSFORM_AFTER_FETCH,,}" == "true" ]]; then
  declare -a transform_inputs=()

  if [[ -z "${TRANSFORM_INPUT_CSV}" ]]; then
    while IFS= read -r file; do
      transform_inputs+=("${file}")
    done < <(find "${LOCAL_OUTPUT_DIR}" -maxdepth 1 -type f -name 'tilt_telemetry_*.csv' | sort)
  else
    while IFS= read -r file; do
      [[ -n "${file}" ]] && transform_inputs+=("${file}")
    done < <(printf '%s\n' "${TRANSFORM_INPUT_CSV}" | tr ',' '\n')
  fi

  if [[ "${#transform_inputs[@]}" -eq 0 ]]; then
    echo "TRANSFORM_AFTER_FETCH=true but no tilt telemetry CSV was found to transform." >&2
    exit 1
  fi

  transform_command=(python /app/transform_tilt_telemetry.py "${transform_inputs[@]}" --output-dir "${TRANSFORM_OUTPUT_DIR}")
  if [[ -n "${TRANSFORM_LAT}" && -n "${TRANSFORM_LON}" ]]; then
    transform_command+=(--lat "${TRANSFORM_LAT}" --lon "${TRANSFORM_LON}")
  fi

  "${transform_command[@]}"
fi

if [[ "${UPLOAD_AFTER_TRANSFORM,,}" == "true" ]]; then
  python /app/upload_to_upstream.py \
    --input-dir "${UPLOAD_INPUT_DIR}" \
    --campaign-id "${UPSTREAM_CAMPAIGN_ID}"
fi

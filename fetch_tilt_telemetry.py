#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    remote_host: str
    remote_user: str
    remote_password: str
    remote_source_dir: str
    remote_glob: str
    local_output_dir: Path
    tailscale_socks_port: int
    fetch_mode: str
    download_since: str | None
    strict_host_key_checking: bool

    @classmethod
    def from_env(cls) -> "Settings":
        fetch_mode = os.getenv("FETCH_MODE", "missing").strip().lower()
        valid_modes = {"all", "missing", "latest", "since-date"}
        if fetch_mode not in valid_modes:
            raise SystemExit(
                f"Invalid FETCH_MODE '{fetch_mode}'. Expected one of: {', '.join(sorted(valid_modes))}"
            )

        download_since = os.getenv("DOWNLOAD_SINCE", "").strip() or None
        if fetch_mode == "since-date" and not download_since:
            raise SystemExit("DOWNLOAD_SINCE is required when FETCH_MODE=since-date")

        output_dir = Path(os.getenv("LOCAL_OUTPUT_DIR", "/data/out")).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        return cls(
            remote_host=os.getenv("REMOTE_HOST", "100.77.233.103").strip(),
            remote_user=os.getenv("REMOTE_USER", "bethel1").strip(),
            remote_password=require_env("REMOTE_PASSWORD"),
            remote_source_dir=os.getenv("REMOTE_SOURCE_DIR", "/var/log/tilt_telemetry").strip(),
            remote_glob=os.getenv("REMOTE_GLOB", "tilt_telemetry_*.csv").strip(),
            local_output_dir=output_dir,
            tailscale_socks_port=int(os.getenv("TAILSCALE_SOCKS_PORT", "1055")),
            fetch_mode=fetch_mode,
            download_since=download_since,
            strict_host_key_checking=env_bool("STRICT_HOST_KEY_CHECKING", False),
        )


def run_command(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(shlex.quote(arg) for arg in args)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def ssh_base_args(settings: Settings) -> list[str]:
    strict_mode = "yes" if settings.strict_host_key_checking else "no"
    return [
        "sshpass",
        "-p",
        settings.remote_password,
        "ssh",
        "-o",
        f"ProxyCommand=nc -x 127.0.0.1:{settings.tailscale_socks_port} %h %p",
        "-o",
        f"StrictHostKeyChecking={strict_mode}",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=4",
        f"{settings.remote_user}@{settings.remote_host}",
    ]


def scp_base_args(settings: Settings) -> list[str]:
    strict_mode = "yes" if settings.strict_host_key_checking else "no"
    return [
        "sshpass",
        "-p",
        settings.remote_password,
        "scp",
        "-O",
        "-o",
        f"ProxyCommand=nc -x 127.0.0.1:{settings.tailscale_socks_port} %h %p",
        "-o",
        f"StrictHostKeyChecking={strict_mode}",
        "-o",
        "UserKnownHostsFile=/dev/null",
    ]


def list_remote_files(settings: Settings) -> list[str]:
    remote_command = (
        f"find {shlex.quote(settings.remote_source_dir)} "
        f"-maxdepth 1 -type f -name {shlex.quote(settings.remote_glob)} -printf '%f\\n' | sort"
    )
    result = run_command(ssh_base_args(settings) + [remote_command])
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not files:
        raise RuntimeError(
            f"No files matched {settings.remote_source_dir}/{settings.remote_glob} on {settings.remote_host}"
        )
    return files


def select_files(files: list[str], settings: Settings) -> list[str]:
    local_existing = {path.name for path in settings.local_output_dir.glob("*.csv")}

    if settings.fetch_mode == "all":
        return files
    if settings.fetch_mode == "missing":
        return [name for name in files if name not in local_existing]
    if settings.fetch_mode == "latest":
        return [files[-1]]
    if settings.fetch_mode == "since-date":
        prefix = f"tilt_telemetry_{settings.download_since}"
        return [name for name in files if name >= f"{prefix}.csv"]
    raise AssertionError(f"Unhandled fetch mode: {settings.fetch_mode}")


def download_files(selected_files: Iterable[str], settings: Settings) -> list[str]:
    downloaded: list[str] = []
    for filename in selected_files:
        destination = settings.local_output_dir / filename
        partial_destination = destination.with_suffix(destination.suffix + ".partial")
        remote_path = f"{settings.remote_user}@{settings.remote_host}:{settings.remote_source_dir.rstrip('/')}/{filename}"
        run_command(scp_base_args(settings) + [remote_path, str(partial_destination)])
        partial_destination.replace(destination)
        downloaded.append(filename)
    return downloaded


def write_manifest(*, available_files: list[str], selected_files: list[str], downloaded_files: list[str], settings: Settings) -> Path:
    manifest_path = settings.local_output_dir / "pull_manifest.json"
    manifest = {
        "remote_host": settings.remote_host,
        "remote_source_dir": settings.remote_source_dir,
        "remote_glob": settings.remote_glob,
        "fetch_mode": settings.fetch_mode,
        "download_since": settings.download_since,
        "available_files": available_files,
        "selected_files": selected_files,
        "downloaded_files": downloaded_files,
        "output_dir": str(settings.local_output_dir),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path


def main() -> int:
    settings = Settings.from_env()

    available_files = list_remote_files(settings)
    selected_files = select_files(available_files, settings)

    if not selected_files:
        manifest_path = write_manifest(
            available_files=available_files,
            selected_files=[],
            downloaded_files=[],
            settings=settings,
        )
        print(
            json.dumps(
                {
                    "status": "no-op",
                    "reason": "No files matched the fetch criteria.",
                    "manifest": str(manifest_path),
                },
                indent=2,
            )
        )
        return 0

    downloaded_files = download_files(selected_files, settings)
    manifest_path = write_manifest(
        available_files=available_files,
        selected_files=selected_files,
        downloaded_files=downloaded_files,
        settings=settings,
    )

    print(
        json.dumps(
            {
                "status": "ok",
                "downloaded_count": len(downloaded_files),
                "downloaded_files": downloaded_files,
                "manifest": str(manifest_path),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

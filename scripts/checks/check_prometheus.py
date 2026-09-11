#!/usr/bin/env python3
"""Validate Prometheus configuration, recording/alerting rules, and rule unit tests using the
official `promtool` binary (downloaded on demand into the gitignored tools/ directory).

Runs, in order:
  promtool check config      prometheus/prometheus.yml
  promtool check config      docker/prometheus.local.yml
  promtool check rules       prometheus/rules/recording_rules.yml prometheus/rules/alerts.yml
  promtool test rules        prometheus/tests/*.yml

The first run downloads the exact platform archive from GitHub Releases and verifies it against
the pinned upstream SHA-256 below before extraction. A missing download or checksum mismatch fails
validation; later runs can use the verified binary cached in the gitignored tools/ directory.
"""
from __future__ import annotations

import hashlib
import platform
import sys
import tarfile
import urllib.request
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Result, repo_root  # noqa: E402

PROMETHEUS_VERSION = "3.14.0"
RELEASE_BASE = f"https://github.com/prometheus/prometheus/releases/download/v{PROMETHEUS_VERSION}"
ARCHIVE_SHA256 = {
    ("darwin", "amd64"): "a14307b9726e66cadb81be9a544732623af26dabeb7702c987aa9c3c062ada34",
    ("darwin", "arm64"): "a9623f7f4fe65b1b171b423c1a72bbf23dfdf41a171dcb33e7dd302af80dc01c",
    ("linux", "amd64"): "f665c6da19eb7ba399c915d30c7d9793c9b417bf8a749b504bc470678631478d",
    ("linux", "arm64"): "077f3781ab7245dc04c9a3c9b78ba120fc8e41aa0dc97489b0af67247e50ba83",
    ("windows", "amd64"): "272bcdd15d9327c7b1e08fe916ea48633819f82f2ea0bf354e6b8c0350c156ba",
    ("windows", "arm64"): "0dfa09dfb43670ea325b7cfb835395104435c03a5302f6b88e1e122195f1580f",
}


def _platform_arch() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    os_name = {"windows": "windows", "linux": "linux", "darwin": "darwin"}.get(system)
    arch = {
        "amd64": "amd64", "x86_64": "amd64",
        "arm64": "arm64", "aarch64": "arm64",
    }.get(machine)
    if not os_name or not arch:
        raise RuntimeError(f"unsupported platform: system={system!r} machine={machine!r}")
    return os_name, arch


def ensure_promtool(result: Result) -> Path | None:
    tools_dir = repo_root() / "tools"
    tools_dir.mkdir(exist_ok=True)
    try:
        os_name, arch = _platform_arch()
    except RuntimeError as exc:
        result.error(f"{exc}; cannot run required promtool checks")
        return None

    exe_name = "promtool.exe" if os_name == "windows" else "promtool"
    archive_name = f"prometheus-{PROMETHEUS_VERSION}.{os_name}-{arch}.tar.gz"
    url = f"{RELEASE_BASE}/{archive_name}"
    archive_path = tools_dir / archive_name
    cache_dir = tools_dir / f"prometheus-{PROMETHEUS_VERSION}.{os_name}-{arch}"
    cached = cache_dir / exe_name
    expected_sha = ARCHIVE_SHA256[(os_name, arch)]

    try:
        if not archive_path.exists():
            download_path = archive_path.with_suffix(archive_path.suffix + ".download")
            urllib.request.urlretrieve(url, download_path)
            download_path.replace(archive_path)

        actual_sha = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        if actual_sha != expected_sha:
            archive_path.unlink(missing_ok=True)
            cached.unlink(missing_ok=True)
            result.error(
                f"checksum mismatch for cached/downloaded {archive_name}: "
                f"expected {expected_sha}, got {actual_sha}"
            )
            return None

        with tarfile.open(archive_path) as tf:
            member = next(
                (m for m in tf.getmembers() if Path(m.name).name == exe_name),
                None,
            )
            if member is None:
                result.error(f"{archive_name} did not contain a {exe_name} binary")
                return None
            source = tf.extractfile(member)
            if source is None:
                result.error(f"could not read {exe_name} from {archive_name}")
                return None
            cache_dir.mkdir(exist_ok=True)
            cached.write_bytes(source.read())
        if os_name != "windows":
            cached.chmod(0o755)
    except Exception as exc:  # noqa: BLE001
        result.error(f"could not download verified promtool ({url}): {exc}")
        return None

    return cached if cached.exists() else None


def run(cmd: list[str], result: Result, label: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        result.error(f"{label} failed:\n{proc.stdout}\n{proc.stderr}")
    else:
        # promtool writes informational SUCCESS text to stdout; surfaced only on failure above to
        # keep passing output quiet, per this project's "quiet on success" preference.
        pass


def main() -> int:
    result = Result("Prometheus config/rules (promtool)")
    root = repo_root()
    promtool = ensure_promtool(result)
    if promtool is None:
        result.print_report()
        return 0 if result.ok else 1

    run(
        [str(promtool), "check", "config", str(root / "prometheus" / "prometheus.yml")],
        result, "promtool check config prometheus/prometheus.yml",
    )
    docker_prom_cfg = root / "docker" / "prometheus.local.yml"
    if docker_prom_cfg.exists():
        # docker/prometheus.local.yml references rule files by their in-container absolute path
        # (/etc/prometheus/rules/...), which only exist inside the compose container, so
        # `check config` is run with --syntax-only here to validate YAML/schema shape without
        # also requiring those absolute paths to resolve on the host running validation.
        run(
            [str(promtool), "check", "config", "--syntax-only", str(docker_prom_cfg)],
            result, "promtool check config --syntax-only docker/prometheus.local.yml",
        )
    run(
        [
            str(promtool), "check", "rules",
            str(root / "prometheus" / "rules" / "recording_rules.yml"),
            str(root / "prometheus" / "rules" / "alerts.yml"),
        ],
        result, "promtool check rules",
    )
    test_files = sorted((root / "prometheus" / "tests").glob("*.yml"))
    if not test_files:
        result.warn("no promtool unit test files found under prometheus/tests/")
    else:
        # Each test file is run in its own promtool process invocation (rather than passing them
        # all to a single `promtool test rules f1 f2 ...` call). Empirically, promtool v3.14.0
        # sometimes leaks alert "for"-pending state across unrelated test files/blocks evaluated
        # within the same process invocation, causing an alert that should be firing to
        # spuriously report no alerts. Separate processes fully isolate each file.
        for f in test_files:
            run([str(promtool), "test", "rules", str(f)], result, f"promtool test rules {f.name}")

    result.print_report()
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

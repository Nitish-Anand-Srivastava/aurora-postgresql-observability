#!/usr/bin/env python3
"""Idempotent Linux setup wizard for Aurora PostgreSQL observability."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import pwd
except ImportError:  # pragma: no cover - setup applies only on Linux
    pwd = None  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
POSTGRES_EXPORTER_VERSION = "0.20.1"
YACE_VERSION = "0.67.0"
ARCHIVES = {
    ("postgres_exporter", "x86_64"): (
        "postgres_exporter-0.20.1.linux-amd64.tar.gz",
        "89d4f7e7920cad48fdc3133f789556ef5253c330a9f5fdace3bdb6344c0a8b5a",
    ),
    ("postgres_exporter", "aarch64"): (
        "postgres_exporter-0.20.1.linux-arm64.tar.gz",
        "d5d86fb98bb1f26b088d1a6fda07fd6b6f035cb5d40492f75ec3bfebb5ddfe9d",
    ),
    ("yace", "x86_64"): (
        "yet-another-cloudwatch-exporter-0.67.0.linux-amd64.tar.gz",
        "f8b3e7474c6e3dcb212733547e29dae851e399ce8d6610a9cbd87c456636b4e7",
    ),
    ("yace", "aarch64"): (
        "yet-another-cloudwatch-exporter-0.67.0.linux-arm64.tar.gz",
        "c4d8a4f89077548426aab3cf2b89ba5954aa65c93488d6645406340d8e7e6d3f",
    ),
}
MANAGED_START = "# BEGIN aurora-postgresql-observability (managed)"
MANAGED_END = "# END aurora-postgresql-observability (managed)"
RULES_START = "# BEGIN aurora-postgresql-observability rules (managed)"
RULES_END = "# END aurora-postgresql-observability rules (managed)"
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
AWS_ROLE = re.compile(r"^arn:aws[a-z-]*:iam::[0-9]{12}:role/.+$")


class SetupError(RuntimeError):
    """A safe, actionable setup failure."""


@dataclass
class Runner:
    apply: bool

    def note(self, message: str) -> None:
        print(("[apply] " if self.apply else "[dry-run] ") + message)

    def command(self, command: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess:
        self.note("run: {}".format(" ".join(command)))
        if not self.apply:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.run(command, text=True, capture_output=True, check=check)

    def write(self, path: Path, content: str, mode: int = 0o640) -> None:
        self.note("write {} (mode {:o})".format(path, mode))
        if not self.apply:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old == content:
            os.chmod(path, mode)
            return
        backup = path.with_name(path.name + ".aurora-setup.bak")
        if path.exists() and not backup.exists():
            shutil.copy2(path, backup)
        fd, temporary = tempfile.mkstemp(prefix=".aurora-setup-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def copy_secret(self, source: Path, destination: Path, owner: str) -> None:
        self.note("copy secret {} to {} without logging its contents".format(source, destination))
        if not self.apply:
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() == destination.resolve():
            account = pwd.getpwnam(owner) if pwd is not None else None
            if account is None:
                raise SetupError("POSIX account support is unavailable")
            os.chmod(destination, 0o400)
            os.chown(destination, account.pw_uid, account.pw_gid)
            return
        fd, temporary = tempfile.mkstemp(prefix=".secret-", dir=str(destination.parent))
        os.close(fd)
        try:
            shutil.copyfile(source, temporary)
            os.chmod(temporary, 0o400)
            if pwd is None:
                raise SetupError("POSIX account support is unavailable")
            account = pwd.getpwnam(owner)
            os.chown(temporary, account.pw_uid, account.pw_gid)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def normalize(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug or len(slug) > 63:
        raise SetupError("cannot normalize name {!r} to a 1-63 character label".format(value))
    return slug


def require(mapping: Dict[str, Any], key: str, context: str) -> Any:
    value = mapping.get(key)
    if value is None or value == "":
        raise SetupError("{} requires {!r}".format(context, key))
    return value


def safe_scalar(value: Any, context: str) -> str:
    text = str(value)
    if any(ord(character) < 32 for character in text):
        raise SetupError("{} contains control characters".format(context))
    return text


def pgpass_line(target: Dict[str, Any], password: str) -> str:
    if "\n" in password or "\r" in password:
        raise SetupError("database password file must contain one line only")

    def escape(value: Any) -> str:
        return str(value).replace("\\", "\\\\").replace(":", "\\:")

    return "{}:{}:{}:{}:{}\n".format(
        escape(target["endpoint"]),
        int(target.get("port", 5432)),
        escape(target["database"]),
        escape(target["db_role"]),
        escape(password),
    )


def validate_config(config: Dict[str, Any], check_files: bool = True) -> Dict[str, Any]:
    targets = require(config, "aurora_targets", "configuration")
    if not isinstance(targets, list) or not targets:
        raise SetupError("aurora_targets must be a non-empty list")
    ports = set()
    names = set()
    for index, target in enumerate(targets):
        context = "aurora_targets[{}]".format(index)
        for key in (
            "name",
            "endpoint",
            "database",
            "db_role",
            "password_file",
            "cluster",
            "instance",
            "role",
            "exporter_port",
            "sslrootcert",
        ):
            require(target, key, context)
        slug = normalize(target["name"])
        if slug in names:
            raise SetupError("duplicate normalized Aurora target name {!r}".format(slug))
        names.add(slug)
        if target["role"] not in ("writer", "reader"):
            raise SetupError("{} role must be writer or reader".format(context))
        if not re.fullmatch(r"[A-Za-z0-9.-]+", safe_scalar(target["endpoint"], context + ".endpoint")):
            raise SetupError("{} endpoint must be a DNS name or IPv4 address".format(context))
        for key in ("database", "db_role"):
            if not SAFE_NAME.fullmatch(safe_scalar(target[key], context + "." + key)):
                raise SetupError("{} {} contains unsafe characters".format(context, key))
        for key in ("password_file", "sslrootcert"):
            safe_scalar(target[key], context + "." + key)
        port = int(target["exporter_port"])
        if not 1024 <= port <= 65535 or port in ports:
            raise SetupError("{} exporter_port must be unique and between 1024 and 65535".format(context))
        ports.add(port)
        if not 1 <= int(target.get("port", 5432)) <= 65535:
            raise SetupError("{} database port is invalid".format(context))
        if check_files:
            for key in ("password_file", "sslrootcert"):
                path = Path(target[key])
                if not path.is_file():
                    raise SetupError("{} {} is not a regular file".format(context, key))
                if key == "password_file" and os.name != "nt" and path.stat().st_mode & 0o077:
                    raise SetupError("{} password_file must have mode 0600 or stricter".format(context))

    for key in ("exporter_advertise_host", "yace_advertise_host"):
        host = safe_scalar(require(config, key, "configuration"), key)
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
            raise SetupError("{} must be a DNS name or IPv4 address".format(key))
    prometheus = require(config, "prometheus", "configuration")
    for key in ("url", "config_path", "service_name", "promtool"):
        safe_scalar(require(prometheus, key, "prometheus"), "prometheus." + key)
    if not SAFE_NAME.fullmatch(prometheus["service_name"]):
        raise SetupError("prometheus.service_name contains unsafe characters")
    grafana = require(config, "grafana", "configuration")
    for key in ("url", "folder"):
        require(grafana, key, "grafana")
    token_file = grafana.get("api_token_file")
    basic_files = grafana.get("username") and grafana.get("password_file")
    if bool(token_file) == bool(basic_files):
        raise SetupError(
            "grafana requires exactly one of api_token_file or username plus password_file"
        )
    if check_files:
        for value in (token_file, grafana.get("password_file")):
            if value and not Path(value).is_file():
                raise SetupError("Grafana credential file does not exist: {}".format(value))
    if not grafana["url"].startswith("https://") and not re.match(
        r"^http://(127\.0\.0\.1|localhost)(:|/|$)", grafana["url"]
    ):
        raise SetupError("Grafana URL must use HTTPS unless it is loopback")

    aws = require(config, "aws", "configuration")
    region = require(aws, "region", "aws")
    if not re.fullmatch(r"[a-z]{2}(-gov)?-[a-z]+-\d", region):
        raise SetupError("aws.region does not look like an AWS region")
    provider = require(aws, "base_credential_provider", "aws")
    if provider not in ("instance-role", "shared-credentials-file", "web-identity"):
        raise SetupError("unsupported aws.base_credential_provider")
    if provider == "shared-credentials-file":
        credentials = safe_scalar(require(aws, "shared_credentials_file", "aws"), "aws.shared_credentials_file")
        if check_files and not Path(credentials).is_file():
            raise SetupError("AWS shared credentials file does not exist")
    if provider == "web-identity":
        require(aws, "web_identity_token_file", "aws")
        require(aws, "web_identity_role_arn", "aws")
    assume = aws.get("assume_role_arn")
    if assume and not AWS_ROLE.fullmatch(assume):
        raise SetupError("aws.assume_role_arn must be an IAM role ARN")
    require(aws, "search_tag_key", "aws")
    require(aws, "search_tag_value", "aws")
    if not 1024 <= int(config.get("yace_port", 5000)) <= 65535:
        raise SetupError("yace_port must be between 1024 and 65535")
    return config


def interactive_config() -> Dict[str, Any]:
    print("Aurora observability setup wizard (paths only; never enter secret values here).")
    targets: List[Dict[str, Any]] = []
    while True:
        name = input("Aurora target name (blank when finished): ").strip()
        if not name:
            break
        targets.append(
            {
                "name": name,
                "endpoint": input("Aurora instance endpoint: ").strip(),
                "port": int(input("Database port [5432]: ").strip() or "5432"),
                "database": input("Database name: ").strip(),
                "db_role": input("Monitoring database role [postgres_exporter]: ").strip()
                or "postgres_exporter",
                "password_file": input("Existing database password-file path: ").strip(),
                "cluster": input("DBClusterIdentifier label: ").strip(),
                "instance": input("DBInstanceIdentifier label: ").strip(),
                "role": input("Instance role (writer/reader): ").strip(),
                "exporter_port": int(input("Local exporter port [9187]: ").strip() or "9187"),
                "sslrootcert": input("Existing RDS CA bundle path: ").strip(),
            }
        )
    if not targets:
        raise SetupError("at least one Aurora target is required")
    provider = input(
        "AWS base credential provider (instance-role/shared-credentials-file/web-identity): "
    ).strip()
    aws: Dict[str, Any] = {
        "region": input("AWS region: ").strip(),
        "base_credential_provider": provider,
        "assume_role_arn": input("Optional role ARN for YACE to assume: ").strip() or None,
        "search_tag_key": input("RDS discovery tag key [aurora-observability]: ").strip()
        or "aurora-observability",
        "search_tag_value": input("RDS discovery tag value [true]: ").strip() or "true",
    }
    if provider == "shared-credentials-file":
        aws["shared_credentials_file"] = input("Existing AWS credentials-file path: ").strip()
        aws["profile"] = input("AWS profile [default]: ").strip() or "default"
    if provider == "web-identity":
        aws["web_identity_token_file"] = input("Web identity token-file path: ").strip()
        aws["web_identity_role_arn"] = input("Web identity base role ARN: ").strip()
    grafana_auth = input("Grafana auth (token/basic): ").strip()
    grafana: Dict[str, Any] = {
        "url": input("Grafana URL: ").strip(),
        "folder": input("Grafana folder [Aurora PostgreSQL]: ").strip() or "Aurora PostgreSQL",
    }
    if grafana_auth == "token":
        grafana["api_token_file"] = input("Grafana service-account token-file path: ").strip()
    else:
        grafana["username"] = input("Grafana username: ").strip()
        grafana["password_file"] = input("Grafana password-file path: ").strip()
    return {
        "aurora_targets": targets,
        "exporter_advertise_host": input("Exporter host as reached by Prometheus: ").strip(),
        "prometheus": {
            "url": input("Prometheus URL: ").strip(),
            "config_path": input("Prometheus config path [/etc/prometheus/prometheus.yml]: ").strip()
            or "/etc/prometheus/prometheus.yml",
            "service_name": input("Prometheus systemd service [prometheus]: ").strip()
            or "prometheus",
            "promtool": input("promtool path [/usr/local/bin/promtool]: ").strip()
            or "/usr/local/bin/promtool",
        },
        "grafana": grafana,
        "aws": aws,
        "yace_advertise_host": input("YACE host as reached by Prometheus: ").strip(),
        "yace_port": int(input("YACE port [5000]: ").strip() or "5000"),
    }


def managed_scrape_block(config: Dict[str, Any]) -> str:
    lines = [
        "  {}".format(MANAGED_START),
        '  - job_name: "aurora-observability-postgres-exporter"',
        "    scrape_interval: 30s",
        "    scrape_timeout: 15s",
        "    relabel_configs:",
        "      - target_label: job",
        '        replacement: "postgres_exporter"',
        "    static_configs:",
    ]
    host = config["exporter_advertise_host"]
    for target in config["aurora_targets"]:
        lines.extend(
            [
                '      - targets: ["{}:{}"]'.format(host, int(target["exporter_port"])),
                "        labels:",
                '          cluster: "{}"'.format(normalize(target["cluster"])),
                '          instance: "{}"'.format(normalize(target["instance"])),
                '          role: "{}"'.format(target["role"]),
                '          database: "{}"'.format(normalize(target["database"])),
            ]
        )
    lines.extend(
        [
            '  - job_name: "aurora-observability-yace"',
            "    scrape_interval: 300s",
            "    scrape_timeout: 30s",
            "    relabel_configs:",
            "      - target_label: job",
            '        replacement: "yace"',
            '    static_configs: [{ targets: ["%s:%s"] }]' % (
                config["yace_advertise_host"],
                int(config.get("yace_port", 5000)),
            ),
            "    metric_relabel_configs:",
            "      - source_labels: [tag_AuroraCluster]",
            '        regex: "(.+)"',
            "        target_label: cluster",
            "      - source_labels: [dimension_DBClusterIdentifier]",
            '        regex: "(.+)"',
            "        target_label: cluster",
            "      - source_labels: [dimension_DBInstanceIdentifier]",
            '        regex: "(.+)"',
            "        target_label: instance",
            "  {}".format(MANAGED_END),
        ]
    )
    return "\n".join(lines) + "\n"


def merge_prometheus_config(existing: str, block: str) -> str:
    if MANAGED_START in existing or MANAGED_END in existing:
        if existing.count(MANAGED_START) != 1 or existing.count(MANAGED_END) != 1:
            raise SetupError("Prometheus config has incomplete or duplicate managed markers")
        start = existing.index(MANAGED_START)
        line_start = existing.rfind("\n", 0, start) + 1
        end = existing.index(MANAGED_END, start) + len(MANAGED_END)
        line_end = existing.find("\n", end)
        if line_end < 0:
            line_end = len(existing)
        else:
            line_end += 1
        return existing[:line_start] + block + existing[line_end:]

    inline_empty = re.search(r"(?m)^scrape_configs:\s*\[\s*\]\s*(?:#.*)?$", existing)
    if inline_empty:
        return (
            existing[: inline_empty.start()]
            + "scrape_configs:\n"
            + block
            + existing[inline_empty.end() :].lstrip("\n")
        )
    match = re.search(r"(?m)^scrape_configs:\s*(?:#.*)?$", existing)
    if not match:
        if re.search(r"(?m)^scrape_configs:", existing):
            raise SetupError("cannot safely merge an inline non-empty scrape_configs value")
        separator = "" if existing.endswith("\n") else "\n"
        return existing + separator + "\nscrape_configs:\n" + block
    section_start = existing.find("\n", match.end())
    if section_start < 0:
        return existing + "\n" + block
    next_top = re.search(r"(?m)^[A-Za-z_][A-Za-z0-9_]*:\s*", existing[section_start + 1 :])
    insert_at = (
        section_start + 1 + next_top.start()
        if next_top
        else len(existing)
    )
    prefix = existing[:insert_at]
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + block + existing[insert_at:]


def merge_rule_files(existing: str) -> str:
    block = "\n".join(
        [
            "  {}".format(RULES_START),
            '  - "/etc/prometheus/aurora-observability/recording_rules.yml"',
            '  - "/etc/prometheus/aurora-observability/alerts.yml"',
            "  {}".format(RULES_END),
            "",
        ]
    )
    if RULES_START in existing or RULES_END in existing:
        if existing.count(RULES_START) != 1 or existing.count(RULES_END) != 1:
            raise SetupError("Prometheus config has incomplete or duplicate managed rule markers")
        start = existing.index(RULES_START)
        line_start = existing.rfind("\n", 0, start) + 1
        end = existing.index(RULES_END, start) + len(RULES_END)
        line_end = existing.find("\n", end)
        line_end = len(existing) if line_end < 0 else line_end + 1
        return existing[:line_start] + block + existing[line_end:]
    inline_empty = re.search(r"(?m)^rule_files:\s*\[\s*\]\s*(?:#.*)?$", existing)
    if inline_empty:
        return (
            existing[: inline_empty.start()]
            + "rule_files:\n"
            + block
            + existing[inline_empty.end() :].lstrip("\n")
        )
    match = re.search(r"(?m)^rule_files:\s*(?:#.*)?$", existing)
    if not match:
        if re.search(r"(?m)^rule_files:", existing):
            raise SetupError("cannot safely merge an inline non-empty rule_files value")
        separator = "" if existing.endswith("\n") else "\n"
        return existing + separator + "\nrule_files:\n" + block
    section_start = existing.find("\n", match.end())
    if section_start < 0:
        return existing + "\n" + block
    next_top = re.search(r"(?m)^[A-Za-z_][A-Za-z0-9_]*:\s*", existing[section_start + 1 :])
    insert_at = section_start + 1 + next_top.start() if next_top else len(existing)
    prefix = existing[:insert_at]
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + block + existing[insert_at:]


def render_yace_config(config: Dict[str, Any]) -> str:
    source = (ROOT / "exporters" / "yace" / "config.yml").read_text(encoding="utf-8")
    region = config["aws"]["region"]
    source = source.replace(
        "        - us-east-1 # placeholder region; set to your cluster's actual region(s)",
        "        - {}".format(region),
    )
    source = source.replace(
        "          value: \"true\"",
        "          value: {}".format(json.dumps(str(config["aws"]["search_tag_value"]))),
    ).replace(
        "        - key: aurora-observability",
        "        - key: {}".format(json.dumps(str(config["aws"]["search_tag_key"]))),
    )
    assume = config["aws"].get("assume_role_arn")
    if assume:
        role_block = '      roles:\n        - roleArn: "{}"\n'.format(assume)
        source = source.replace("      searchTags:\n", role_block + "      searchTags:\n")
    source = source.replace("apiVersion: v1alpha1", "apiVersion: v1alpha1\nsts-region: {}".format(region))
    return source


def render_yace_env(config: Dict[str, Any]) -> str:
    aws = config["aws"]
    lines = ["# Generated without static AWS keys.", "AWS_REGION={}".format(aws["region"])]
    provider = aws["base_credential_provider"]
    if provider == "shared-credentials-file":
        lines.extend(
            [
                "AWS_SHARED_CREDENTIALS_FILE=/etc/yace/aws-credentials",
                "AWS_PROFILE={}".format(aws.get("profile") or "default"),
            ]
        )
    elif provider == "web-identity":
        lines.extend(
            [
                "AWS_WEB_IDENTITY_TOKEN_FILE={}".format(aws["web_identity_token_file"]),
                "AWS_ROLE_ARN={}".format(aws["web_identity_role_arn"]),
            ]
        )
    return "\n".join(lines) + "\n"


def exporter_env(target: Dict[str, Any], slug: str) -> str:
    uri = "{}:{}/{}?sslmode=verify-full&sslrootcert={}".format(
        target["endpoint"],
        int(target.get("port", 5432)),
        urllib.parse.quote(str(target["database"]), safe=""),
        urllib.parse.quote(str(target["sslrootcert"]), safe="/"),
    )
    return "\n".join(
        [
            "DATA_SOURCE_URI={}".format(uri),
            "DATA_SOURCE_USER={}".format(target["db_role"]),
            "DATA_SOURCE_PASS_FILE=/etc/postgres_exporter/{}/pgpassword".format(slug),
            "PG_EXPORTER_WEB_TELEMETRY_PATH=/metrics",
            "PG_EXPORTER_COLLECTION_TIMEOUT=10s",
            "",
        ]
    )


def exporter_unit() -> str:
    return """[Unit]
Description=Aurora postgres_exporter instance %i
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=postgres_exporter
Group=postgres_exporter
EnvironmentFile=/etc/postgres_exporter/%i/exporter.env
ExecStart=/usr/local/bin/postgres_exporter --web.listen-address=0.0.0.0:${EXPORTER_PORT} --config.file=/etc/postgres_exporter/postgres_exporter.yml --collector.database --collector.locks --no-collector.replication --collector.replication_slots --collector.settings --collector.stat_activity --collector.stat_archiver --collector.stat_bgwriter --collector.stat_checkpointer --collector.stat_database --collector.stat_user_tables --collector.statio_user_tables --collector.stat_progress_vacuum --collector.stat_replication --collector.wal --collector.database_wraparound
Restart=on-failure
RestartSec=5s
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LockPersonality=true
MemoryDenyWriteExecute=true

[Install]
WantedBy=multi-user.target
"""


def yace_unit(port: int) -> str:
    return """[Unit]
Description=YACE AWS/RDS CloudWatch exporter
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=yace
Group=yace
EnvironmentFile=/etc/yace/yace.env
ExecStart=/usr/local/bin/yace -config.file=/etc/yace/config.yml -listen-address=0.0.0.0:%d -scraping-interval=300 -log.format=json -log.level=info
Restart=on-failure
RestartSec=5s
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LockPersonality=true
MemoryDenyWriteExecute=true

[Install]
WantedBy=multi-user.target
""" % port


def ensure_user(runner: Runner, name: str, home: str) -> None:
    if pwd is None and runner.apply:
        raise SetupError("POSIX account support is unavailable")
    try:
        if pwd is None:
            raise KeyError(name)
        pwd.getpwnam(name)
        runner.note("system user {} already exists".format(name))
    except KeyError:
        runner.command(
            [
                "useradd",
                "--system",
                "--no-create-home",
                "--home-dir",
                home,
                "--shell",
                "/usr/sbin/nologin",
                name,
            ]
        )


def safe_extract(archive: Path, destination: Path) -> None:
    with tarfile.open(str(archive), "r:gz") as tar:
        root = destination.resolve()
        for member in tar.getmembers():
            if member.issym() or member.islnk():
                raise SetupError("release archive contains a link; refusing extraction")
            target = (destination / member.name).resolve()
            if root != target and root not in target.parents:
                raise SetupError("release archive contains an unsafe path")
        tar.extractall(str(destination))


def install_binary(runner: Runner, product: str, architecture: str) -> None:
    archive_name, expected = ARCHIVES.get((product, architecture), (None, None))
    if not archive_name:
        raise SetupError("unsupported architecture {!r}; use x86_64 or aarch64".format(architecture))
    version = POSTGRES_EXPORTER_VERSION if product == "postgres_exporter" else YACE_VERSION
    repo = (
        "postgres_exporter"
        if product == "postgres_exporter"
        else "yet-another-cloudwatch-exporter"
    )
    url = "https://github.com/prometheus-community/{}/releases/download/v{}/{}".format(
        repo, version, archive_name
    )
    runner.note("download pinned {} v{} and verify SHA-256 {}".format(product, version, expected))
    if not runner.apply:
        return
    with tempfile.TemporaryDirectory(prefix="aurora-exporter-") as temp:
        archive = Path(temp) / archive_name
        urllib.request.urlretrieve(url, str(archive))
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != expected:
            raise SetupError("{} checksum mismatch; refusing installation".format(product))
        extract = Path(temp) / "extract"
        extract.mkdir()
        safe_extract(archive, extract)
        candidates = [path for path in extract.rglob(product) if path.is_file()]
        if len(candidates) != 1:
            raise SetupError("could not identify {} binary in release".format(product))
        destination = Path("/usr/local/bin") / product
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".{}-".format(product), dir=str(destination.parent))
        os.close(fd)
        try:
            shutil.copyfile(candidates[0], temporary)
            os.chmod(temporary, 0o755)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def grafana_headers(config: Dict[str, Any]) -> Dict[str, str]:
    grafana = config["grafana"]
    if grafana.get("api_token_file"):
        credential_value = Path(grafana["api_token_file"]).read_text(encoding="utf-8").strip()
        return {"Authorization": "Bearer " + credential_value, "Content-Type": "application/json"}
    credential_value = Path(grafana["password_file"]).read_text(encoding="utf-8").strip()
    encoded = base64.b64encode(
        "{}:{}".format(grafana["username"], credential_value).encode("utf-8")
    ).decode("ascii")
    return {"Authorization": "Basic " + encoded, "Content-Type": "application/json"}


def grafana_request(
    config: Dict[str, Any], method: str, path: str, payload: Optional[Dict[str, Any]] = None
) -> Tuple[int, Dict[str, Any]]:
    url = config["grafana"]["url"].rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method, headers=grafana_headers(config)
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        if exc.code == 404:
            return 404, {}
        raise SetupError("Grafana API {} {} failed: HTTP {} {}".format(
            method, path, exc.code, body[:300]
        )) from exc


def provision_grafana(config: Dict[str, Any], runner: Runner) -> None:
    runner.note("provision Grafana datasource, folder, and dashboard via API")
    if not runner.apply:
        return
    datasource = {
        "name": "Aurora Prometheus",
        "uid": "aurora-prometheus",
        "type": "prometheus",
        "access": "proxy",
        "url": config["prometheus"]["url"],
        "isDefault": False,
        "jsonData": {"httpMethod": "POST", "timeInterval": "30s"},
    }
    status, _ = grafana_request(config, "GET", "/api/datasources/uid/aurora-prometheus")
    grafana_request(
        config,
        "PUT" if status == 200 else "POST",
        "/api/datasources/uid/aurora-prometheus" if status == 200 else "/api/datasources",
        datasource,
    )
    folder_uid = "aurora-postgresql"
    status, _ = grafana_request(config, "GET", "/api/folders/{}".format(folder_uid))
    if status == 404:
        grafana_request(
            config,
            "POST",
            "/api/folders",
            {"uid": folder_uid, "title": config["grafana"]["folder"]},
        )
    dashboard = json.loads(
        (ROOT / "grafana" / "dashboards" / "aurora-postgresql-overview.json").read_text(
            encoding="utf-8"
        )
    )
    dashboard["id"] = None
    grafana_request(
        config,
        "POST",
        "/api/dashboards/db",
        {"dashboard": dashboard, "folderUid": folder_uid, "overwrite": True},
    )


def verify_runtime(config: Dict[str, Any], runner: Runner) -> None:
    if not runner.apply:
        return
    checks = [
        ("YACE", "http://127.0.0.1:{}/metrics".format(int(config["yace_port"])), None),
        ("Prometheus", config["prometheus"]["url"].rstrip("/") + "/-/ready", None),
    ]
    for target in config["aurora_targets"]:
        checks.append(
            (
                "postgres_exporter {}".format(target["name"]),
                "http://127.0.0.1:{}/metrics".format(int(target["exporter_port"])),
                "pg_up",
            )
        )
    for name, url, required_text in checks:
        last_error: Optional[Exception] = None
        for _attempt in range(10):
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    body = response.read().decode("utf-8")
                    if response.status < 300 and (
                        required_text is None or required_text in body
                    ):
                        runner.note("{} runtime check passed".format(name))
                        break
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
            time.sleep(1)
        else:
            raise SetupError("{} runtime verification failed: {}".format(name, last_error or url))


def preflight(config: Dict[str, Any], apply: bool) -> None:
    if platform.system() != "Linux":
        raise SetupError("setup applies only to Linux; use --dry-run for offline planning")
    if apply and os.geteuid() != 0:
        raise SetupError("--apply must run as root")
    prometheus_path = Path(config["prometheus"]["config_path"])
    if not prometheus_path.is_file():
        raise SetupError("existing Prometheus config was not found; this installer will not replace it")
    promtool = Path(config["prometheus"]["promtool"])
    if not promtool.is_file():
        raise SetupError("promtool was not found at the configured path")
    subprocess.run(
        [str(promtool), "check", "config", str(prometheus_path)],
        text=True,
        capture_output=True,
        check=True,
    )
    psql = shutil.which("psql")
    if not psql:
        raise SetupError("psql is required to preflight every Aurora target")
    for target in config["aurora_targets"]:
        environment = os.environ.copy()
        environment.pop("PGPASSWORD", None)
        credential_value = Path(target["password_file"]).read_text(encoding="utf-8").rstrip("\r\n")
        fd, temporary_pgpass = tempfile.mkstemp(prefix=".aurora-pgpass-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(pgpass_line(target, credential_value))
            os.chmod(temporary_pgpass, 0o600)
            environment["PGPASSFILE"] = temporary_pgpass
            result = subprocess.run(
                [
                    psql,
                    "-X",
                    "--no-psqlrc",
                    "--set",
                    "ON_ERROR_STOP=1",
                    "--host",
                    target["endpoint"],
                    "--port",
                    str(target.get("port", 5432)),
                    "--username",
                    target["db_role"],
                    "--dbname",
                    target["database"],
                    "--command",
                    "SELECT current_database(), pg_is_in_recovery();",
                ],
                env=environment,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
        finally:
            if os.path.exists(temporary_pgpass):
                os.unlink(temporary_pgpass)
        if result.returncode != 0:
            raise SetupError(
                "Aurora preflight failed for {}: {}".format(
                    target["name"], result.stderr.strip()[:300]
                )
            )
    for name, url in (
        ("Prometheus", config["prometheus"]["url"].rstrip("/") + "/-/ready"),
        ("Grafana", config["grafana"]["url"].rstrip("/") + "/api/health"),
    ):
        headers = grafana_headers(config) if name == "Grafana" else {}
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                if response.status >= 300:
                    raise SetupError("{} readiness returned HTTP {}".format(name, response.status))
        except urllib.error.URLError as exc:
            raise SetupError("{} readiness check failed: {}".format(name, exc.reason)) from exc
    if shutil.which("aws"):
        env = os.environ.copy()
        aws = config["aws"]
        if aws["base_credential_provider"] == "shared-credentials-file":
            env["AWS_SHARED_CREDENTIALS_FILE"] = aws["shared_credentials_file"]
            env["AWS_PROFILE"] = aws.get("profile") or "default"
        identity = subprocess.run(
            ["aws", "sts", "get-caller-identity", "--region", aws["region"]],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if identity.returncode != 0:
            raise SetupError(
                "AWS base credentials are unavailable; on-premises hosts need a base provider "
                "before YACE can assume another role"
            )
    elif config["aws"]["assume_role_arn"]:
        print("WARNING: aws CLI not installed; cannot preflight base credentials for AssumeRole.")


def update_prometheus(config: Dict[str, Any], runner: Runner) -> None:
    path = Path(config["prometheus"]["config_path"])
    existing = path.read_text(encoding="utf-8")
    candidate = merge_prometheus_config(existing, managed_scrape_block(config))
    candidate = merge_rule_files(candidate)
    runner.note(
        "merge managed scrape jobs and rule files into {} without replacing unrelated entries".format(
            path
        )
    )
    if not runner.apply:
        return
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", prefix=".prometheus-candidate-", dir=str(path.parent), delete=False
    ) as handle:
        handle.write(candidate)
        candidate_path = Path(handle.name)
    try:
        result = subprocess.run(
            [config["prometheus"]["promtool"], "check", "config", str(candidate_path)],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise SetupError("candidate Prometheus config failed validation: {}".format(result.stderr))
        backup = path.with_name(path.name + ".aurora-setup.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        os.replace(str(candidate_path), str(path))
    finally:
        if candidate_path.exists():
            candidate_path.unlink()
    runner.command(["systemctl", "reload", config["prometheus"]["service_name"]])


def install(config: Dict[str, Any], runner: Runner) -> None:
    architecture = platform.machine().lower()
    install_binary(runner, "postgres_exporter", architecture)
    install_binary(runner, "yace", architecture)
    ensure_user(runner, "postgres_exporter", "/etc/postgres_exporter")
    ensure_user(runner, "yace", "/etc/yace")
    runner.write(
        Path("/etc/postgres_exporter/postgres_exporter.yml"),
        (ROOT / "exporters" / "postgres_exporter" / "postgres_exporter.yml").read_text(
            encoding="utf-8"
        ),
        mode=0o644,
    )
    runner.write(
        Path("/etc/prometheus/aurora-observability/recording_rules.yml"),
        (ROOT / "prometheus" / "rules" / "recording_rules.yml").read_text(encoding="utf-8"),
        mode=0o644,
    )
    runner.write(
        Path("/etc/prometheus/aurora-observability/alerts.yml"),
        (ROOT / "prometheus" / "rules" / "alerts.yml").read_text(encoding="utf-8"),
        mode=0o644,
    )
    runner.write(Path("/etc/systemd/system/postgres_exporter@.service"), exporter_unit())
    for target in config["aurora_targets"]:
        slug = normalize(target["name"])
        env = "EXPORTER_PORT={}\n".format(int(target["exporter_port"])) + exporter_env(target, slug)
        runner.write(Path("/etc/postgres_exporter") / slug / "exporter.env", env)
        runner.copy_secret(
            Path(target["password_file"]),
            Path("/etc/postgres_exporter") / slug / "pgpassword",
            "postgres_exporter",
        )
    runner.write(Path("/etc/yace/config.yml"), render_yace_config(config), mode=0o644)
    runner.write(Path("/etc/yace/yace.env"), render_yace_env(config), mode=0o644)
    if config["aws"]["base_credential_provider"] == "shared-credentials-file":
        runner.copy_secret(
            Path(config["aws"]["shared_credentials_file"]),
            Path("/etc/yace/aws-credentials"),
            "yace",
        )
    runner.write(Path("/etc/systemd/system/yace.service"), yace_unit(int(config["yace_port"])))
    runner.command(["systemctl", "daemon-reload"])
    for target in config["aurora_targets"]:
        runner.command(
            ["systemctl", "enable", "--now", "postgres_exporter@{}.service".format(normalize(target["name"]))]
        )
    runner.command(["systemctl", "enable", "--now", "yace.service"])
    update_prometheus(config, runner)
    provision_grafana(config, runner)
    verify_runtime(config, runner)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("--config", type=Path, help="JSON config; omit for the interactive wizard")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--preflight", action="store_true", help="check inputs and dependencies only")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="make host and API changes")
    mode.add_argument("--dry-run", action="store_true", help="print planned changes (the default)")
    args = parser.parse_args(argv)
    if args.non_interactive and not args.config:
        parser.error("--non-interactive requires --config")
    if args.preflight and args.apply:
        parser.error("--preflight cannot be combined with --apply")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = parse_args(argv)
        config = (
            json.loads(args.config.read_text(encoding="utf-8"))
            if args.config
            else interactive_config()
        )
        validate_config(config, check_files=True)
        preflight(config, apply=args.apply)
        if args.preflight:
            print("Preflight passed; no changes made.")
            return 0
        install(config, Runner(apply=args.apply))
        if not args.apply:
            print("Dry run complete; rerun with --apply as root after reviewing every action.")
        else:
            print("Setup complete. Original Prometheus config backup: *.aurora-setup.bak")
        return 0
    except (SetupError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

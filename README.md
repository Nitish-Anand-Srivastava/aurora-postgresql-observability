# Aurora PostgreSQL Observability

Production-grade, deployable observability configuration for **Amazon Aurora PostgreSQL 17+**
using your existing Prometheus and Grafana: [postgres_exporter](https://github.com/prometheus-community/postgres_exporter)
for engine metrics and [YACE](https://github.com/prometheus-community/yet-another-cloudwatch-exporter)
(yet-another-cloudwatch-exporter, part of prometheus-community since
[November 2024](https://prometheus.io/blog/2024/11/19/yace-joining-prometheus-community/)) for
ordinary `AWS/RDS` CloudWatch metrics.

This repository ships **configuration and operational utilities**, not a long-running service:
Prometheus scrape/recording/alerting rules, a provisionable Grafana dashboard, systemd units,
IAM/SQL least-privilege definitions, a non-production tuning simulator, and automated validation.
See [`docs/architecture.md`](docs/architecture.md) for the full picture,
including the important **CloudWatch API boundary** (YACE can scrape the four aggregate `DBLoad*`
metrics AWS publishes to `AWS/RDS`, but not Database Insights waits or top-SQL dimensions).

## How the data gets to Grafana

There are two independent paths, and Prometheus joins them by consistent cluster/instance labels:

```text
Aurora PostgreSQL --SQL--> postgres_exporter --\
                                                  Prometheus --> Grafana
AWS/RDS CloudWatch API -------> YACE ------------/
```

`postgres_exporter` logs in to each Aurora endpoint with a read-only monitoring role and turns
PostgreSQL internals such as connections, locks, vacuum, and table statistics into Prometheus
metrics. YACE uses AWS credentials to read ordinary `AWS/RDS` CloudWatch metrics such as CPU,
memory, IOPS, storage, and replica lag. Prometheus scrapes both HTTP endpoints and evaluates the
included rules; Grafana queries only Prometheus. YACE reads the four aggregate `DBLoad*` metrics
AWS publishes into ordinary CloudWatch, but it does **not** read detailed Database Insights /
Performance Insights waits or top SQL.

## Pinned versions

| Component | Version | Source |
| --- | --- | --- |
| postgres_exporter | [v0.20.1](https://github.com/prometheus-community/postgres_exporter/releases/tag/v0.20.1) | `quay.io/prometheuscommunity/postgres-exporter:v0.20.1` |
| YACE | [v0.67.0](https://github.com/prometheus-community/yet-another-cloudwatch-exporter/releases/tag/v0.67.0) | `quay.io/prometheuscommunity/yet-another-cloudwatch-exporter:v0.67.0` |
| Prometheus | [v3.14.0](https://github.com/prometheus/prometheus/releases/tag/v3.14.0) | `prom/prometheus:v3.14.0` |
| Grafana | [v13.2.1](https://github.com/grafana/grafana/releases/tag/v13.2.1) | `grafana/grafana:13.2.1` |

Every image/binary reference in this repo is pinned to one of these exact versions -- no `latest`
tags. Each image tag above was checked against its registry (Docker Hub / Quay.io) to confirm it
is published and pullable as of this writing. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the
version-bump policy.

## What's in this repository

```text
sql/                      Least-privilege postgres_exporter role (pg_monitor-based)
iam/                      Least-privilege IAM policy for YACE + docs on why
exporters/postgres_exporter/   Env template, optional queries.yaml (deprecated mechanism), config
exporters/yace/           YACE config.yml (instance + cluster level AWS/RDS jobs), env template
systemd/                  Hardened systemd units for both exporters
prometheus/               prometheus.yml, recording rules, alert rules, promtool unit tests
grafana/                  Datasource + dashboard provisioning, the dashboard JSON itself
docker/                   Local Docker Compose validation stack (safe by default, no live infra)
scripts/                  Cross-platform Python validation (scripts/validate.py) + Makefile
workload/                 Explicitly non-production Aurora tuning workload simulator
docs/                     Architecture, setup, security, troubleshooting, metrics, dashboard guide
.github/workflows/        CI running the same validation on every push/PR
```

## Quick start

- **Try it locally first** (no real AWS/Aurora needed): [`docker/README.md`](docker/README.md).
- **Install on a Linux exporter host**: copy
  [`examples/setup-config.json`](examples/setup-config.json), point every credential field at an
  existing mode-`0600` file, then review a dry run:

  ```bash
  python3 scripts/setup_observability.py \
    --config /secure/path/aurora-observability.json \
    --non-interactive \
    --dry-run
  sudo python3 scripts/setup_observability.py \
    --config /secure/path/aurora-observability.json \
    --non-interactive \
    --apply
  ```

  The installer defaults to dry-run, preserves unrelated Prometheus jobs/rules, validates the
  candidate config before reload, and imports the datasource/dashboard through the Grafana API.
- **Deploy against a real Aurora cluster**: [`docs/setup.md`](docs/setup.md).
- **Understand the dashboard**: [`docs/dashboard-guide.md`](docs/dashboard-guide.md).
- **Look up a metric**: [`docs/metrics-reference.md`](docs/metrics-reference.md).
- **An alert fired**: [`docs/troubleshooting.md`](docs/troubleshooting.md).
- **Security model**: [`docs/security.md`](docs/security.md) / [`SECURITY.md`](SECURITY.md).
- **Generate non-production tuning load**: [`docs/tuning-workload.md`](docs/tuning-workload.md).

## First troubleshooting checks

1. `systemctl status 'postgres_exporter@*' yace prometheus` checks the processes.
2. In Prometheus, query `up{job=~"postgres_exporter|yace"}`; `pg_up == 0` means
   the exporter is reachable but its database login failed.
3. If `aws_rds_*` series are absent, check `journalctl -u yace` for AWS credential, AssumeRole,
   region, or discovery-tag errors. An on-premises host needs a base AWS credential provider
   before it can assume the configured role.
4. If Grafana is empty but Prometheus has data, select the **Aurora Prometheus** datasource and
   verify the normalized `cluster` and `instance` labels. See
   [`docs/troubleshooting.md`](docs/troubleshooting.md) for detailed runbooks and rollback.

## Validate your changes

```bash
python scripts/validate.py       # or: make validate
```

Runs YAML/JSON syntax checks, Grafana dashboard structural + metric-provenance checks, Prometheus
config/rules/unit-test checks (via a pinned `promtool`, downloaded on demand into a gitignored
`tools/` directory), database-role consistency and simulator unit tests, a shell-script linter,
best-effort Markdown link checking, and a secret-pattern scan -- all without Docker, live
credentials, or requiring PyYAML (though installing `scripts/requirements.txt` upgrades YAML validation
from a fallback heuristic to full parsing). CI (`.github/workflows/ci.yml`) runs the identical
entrypoint.

## No secrets, ever

Every hostname in this repo uses the `.invalid` placeholder TLD, every account ID is an
RFC-reserved-style placeholder, and every credential is read at runtime from a file or environment
variable outside version control (`DATA_SOURCE_PASS_FILE`, the AWS SDK default credential chain,
Docker secrets). See [`docs/security.md`](docs/security.md) and [`SECURITY.md`](SECURITY.md).

## License

[Apache License 2.0](LICENSE). Contributions welcome -- see [`CONTRIBUTING.md`](CONTRIBUTING.md).

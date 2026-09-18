# Setup Guide

End-to-end walkthrough for wiring this repository's configuration up against a real Aurora
PostgreSQL cluster. For a throwaway local demo instead, see [`docker/README.md`](../docker/README.md).

## Prerequisites

- Aurora PostgreSQL **17 or later** cluster (writer + 0 or more readers). Most of this repo works
  on PostgreSQL 13-18 generally (the versions
  [CI-tested by postgres_exporter](https://github.com/prometheus-community/postgres_exporter#quick-start)),
  but two panels/collectors are PostgreSQL 17+ only (`stat_checkpointer`) -- see
  [`docs/metrics-reference.md`](metrics-reference.md).
- An existing Prometheus and Grafana deployment (this repo ships their *configuration*, not a
  from-scratch install of either -- see `prometheus/prometheus.yml` and
  `grafana/provisioning/`).
- A Linux host (or hosts) to run the exporters, with systemd, network access to the Aurora cluster
  and to `monitoring.<region>.amazonaws.com` / `tagging.<region>.amazonaws.com`.
- An AWS IAM principal (instance role, IRSA, or assumable role) for YACE.
- The AWS CLI for credential-provider and optional AssumeRole preflight checks.

The automated path supports existing Prometheus releases as old as the observed **2.30** deployment
provided `promtool check config` accepts the merged candidate. It does not replace or upgrade
Prometheus. Grafana **12.3.1** has been verified with the datasource/folder/dashboard API calls;
newer compatible Grafana releases work through the same API.

## Automated setup wizard (recommended)

The standard-library Python wizard installs the two pinned exporters and integrates with existing
Prometheus and Grafana. It never installs Prometheus/Grafana themselves and never overwrites the
whole Prometheus config.

1. Copy [`examples/setup-config.json`](../examples/setup-config.json) outside the repository.
2. Add one `aurora_targets` item per endpoint you monitor. Across the list you can use different
   databases and monitoring roles. Each item becomes a separate
   `postgres_exporter@<normalized-name>.service`; use unique exporter ports.
3. Point each Aurora `password_file` at a protected file containing only that role's password
   (one line, not pgpass format). Point Grafana `api_token_file` (or `password_file` for basic
   auth), and any AWS shared-credentials/token fields at existing protected files. Secret values
   are never accepted as command-line arguments or written into the JSON.
4. Set the existing Prometheus URL/config path/service name and `promtool` path, plus the Grafana
   URL/folder. Grafana must use HTTPS unless it is loopback.
5. Set `aws.base_credential_provider`. On EC2, `instance-role` is preferred. An on-premises host
   must supply a base provider such as `shared-credentials-file` before
   `aws.assume_role_arn` can work; role assumption cannot bootstrap credentials from nothing.
   The installer copies that existing file to an exporter-owned mode-`0400`
   `/etc/yace/aws-credentials`; it never prints or embeds its contents.

Run validation only, then dry-run, then explicitly apply:

```bash
python3 scripts/setup_observability.py \
  --config /secure/path/aurora-observability.json \
  --non-interactive --preflight

python3 scripts/setup_observability.py \
  --config /secure/path/aurora-observability.json \
  --non-interactive --dry-run

sudo python3 scripts/setup_observability.py \
  --config /secure/path/aurora-observability.json \
  --non-interactive --apply
```

Omit `--config` for an interactive wizard. It asks only for identifiers and credential **paths**,
never secret values. The default without `--apply` is dry-run.

The preflight checks the existing Prometheus config with its own `promtool`, tests each Aurora
login using an automatically deleted mode-`0600` temporary `PGPASSFILE` derived from the
password-only input, checks Prometheus/Grafana readiness, and uses the AWS CLI to exercise the
selected base provider plus the configured AssumeRole hop. Apply mode:

- creates locked-down `postgres_exporter` and `yace` system users/directories;
- downloads only the pinned Linux archive for the host architecture and verifies an embedded
  release SHA-256 before extraction/installation;
- copies database passwords to root-created, exporter-owned mode-`0400` files without printing
  their contents;
- creates one systemd exporter service per configured Aurora target with normalized
  `cluster`/`instance`/`role`/`database` labels;
- generates YACE region/tag/optional-assume-role configuration without static AWS keys;
- copies rules, replaces only its marked Prometheus blocks, preserves every unrelated
  scrape/rule/remote-write setting, validates a temporary candidate, creates the first backup,
  atomically replaces the config, then reloads the configured service; and
- idempotently creates/updates the **Aurora Prometheus** datasource, folder, and dashboard using
  Grafana's API, replacing exported `${DS_PROMETHEUS}` tokens with the created datasource UID.

After startup, the wizard waits for a real `aws_rds_*` sample, not just an HTTP 200 from YACE.
CloudWatch discovery can take several minutes; `aws_metrics_timeout_seconds` defaults to 360 and
accepts 30-900 seconds. A timeout fails setup visibly but leaves services running for inspection.

### Automated setup rollback

The first changed Prometheus config is retained beside the original as
`<config>.aurora-setup.bak`. To roll back:

```bash
sudo systemctl disable --now 'postgres_exporter@orders-writer.service' yace.service
sudo cp /etc/prometheus/prometheus.yml.aurora-setup.bak /etc/prometheus/prometheus.yml
sudo /usr/local/bin/promtool check config /etc/prometheus/prometheus.yml
sudo systemctl reload prometheus
```

Then remove only the explicitly managed units/directories you reviewed:
`/etc/systemd/system/postgres_exporter@.service`, `/etc/systemd/system/yace.service`,
`/etc/postgres_exporter`, `/etc/yace`, and `/etc/prometheus/aurora-observability`. Delete the
Grafana folder/datasource through Grafana if desired. The installer never drops database roles,
deletes dashboards, removes users, or performs rollback automatically.

## 1. Create the least-privilege SQL role

```bash
curl --fail --silent --show-error --location \
  https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
  -o /tmp/rds-global-bundle.pem
psql "host=<your-writer-endpoint> dbname=postgres sslmode=verify-full sslrootcert=/tmp/rds-global-bundle.pem" \
  -f sql/create_monitoring_role.sql
```

Then set a password out of band (Secrets Manager, IAM auth, or interactively) -- the script
deliberately does not set one. See the comments at the top of
[`sql/create_monitoring_role.sql`](../sql/create_monitoring_role.sql).

## 2. Attach the IAM policy for YACE

```bash
aws iam create-policy \
  --policy-name yace-rds-readonly \
  --policy-document file://iam/yace-readonly-policy.json
aws iam attach-role-policy \
  --role-name <your-yace-execution-role> \
  --policy-arn arn:aws:iam::<your-account-id>:policy/yace-rds-readonly
```

See [`iam/README.md`](../iam/README.md) for why exactly these actions are needed and nothing more.

Tag your Aurora writer/reader instances and the cluster resource itself so YACE's discovery jobs
can find them, matching whatever key/value you configure in `exporters/yace/config.yml`'s
`searchTags` (defaults to `aurora-observability=true` -- change this to your own tagging
convention). Also apply `AuroraCluster=<DBClusterIdentifier>` to the cluster and every DB instance.
The Prometheus metric relabeling maps that exported tag and YACE's dimensions to the common
`cluster` and `instance` labels used by the dashboard.

## 3. Deploy postgres_exporter (one process per instance)

On each instance you want metrics for (writer and every reader), set the Prometheus target's
`instance` label to the AWS `DBInstanceIdentifier`. This keeps the dashboard's instance selector
consistent across SQL and CloudWatch metrics:

```bash
# See systemd/postgres_exporter.service for full install steps, verification, and flags.
sudo useradd --system --no-create-home --home-dir /etc/postgres_exporter \
  --shell /usr/sbin/nologin postgres_exporter
sudo install -d -o postgres_exporter -g postgres_exporter -m 0750 /etc/postgres_exporter
sudo install -o postgres_exporter -g postgres_exporter -m 0640 \
  exporters/postgres_exporter/postgres_exporter.env.template \
  /etc/postgres_exporter/postgres_exporter.env
sudo install -o postgres_exporter -g postgres_exporter -m 0640 \
  exporters/postgres_exporter/postgres_exporter.yml \
  /etc/postgres_exporter/postgres_exporter.yml
# Install the global RDS CA bundle downloaded in step 1 for the service account. Review AWS
# certificate rotation guidance before upgrades:
# https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.SSL.html
sudo install -o root -g postgres_exporter -m 0640 \
  /tmp/rds-global-bundle.pem /etc/postgres_exporter/global-bundle.pem
rm /tmp/rds-global-bundle.pem
# edit /etc/postgres_exporter/postgres_exporter.env: set DATA_SOURCE_URI to this instance's
# endpoint, DATA_SOURCE_USER=postgres_exporter
printf '%s' 'REPLACE_WITH_REAL_PASSWORD' | sudo tee /etc/postgres_exporter/pgpassword >/dev/null
sudo chmod 0400 /etc/postgres_exporter/pgpassword
sudo chown postgres_exporter:postgres_exporter /etc/postgres_exporter/pgpassword
```

The passwd home must be `/etc/postgres_exporter`, even though `--no-create-home` prevents
`useradd` from creating it. The hardened unit uses `ProtectHome=true`; leaving the default home at
`/home/postgres_exporter` makes libpq's probe for `~/.postgresql/postgresql.crt` fail with
`permission denied`, even when `DATA_SOURCE_URI` specifies an explicit `sslrootcert`.

Download and verify the pinned `postgres_exporter` v0.20.1 binary from the authoritative release
page: <https://github.com/prometheus-community/postgres_exporter/releases/tag/v0.20.1>
(verify against the release's published checksums), then:

```bash
sudo install -o root -g root -m 0755 postgres_exporter /usr/local/bin/postgres_exporter
sudo cp systemd/postgres_exporter.service /etc/systemd/system/postgres_exporter.service
sudo systemctl daemon-reload
sudo systemctl enable --now postgres_exporter.service
curl -s http://127.0.0.1:9187/metrics | grep '^pg_up'
```

## 4. Deploy YACE (once per account/region being monitored)

```bash
# See systemd/yace.service for full install steps.
sudo useradd --system --no-create-home --shell /usr/sbin/nologin yace
sudo install -d -o yace -g yace -m 0750 /etc/yace
sudo install -o yace -g yace -m 0640 exporters/yace/yace.env.template /etc/yace/yace.env
sudo install -o yace -g yace -m 0640 exporters/yace/config.yml /etc/yace/config.yml
# edit /etc/yace/config.yml: set the correct region(s) and searchTags for your cluster
```

Download and verify the pinned YACE v0.67.0 binary/tarball from
<https://github.com/prometheus-community/yet-another-cloudwatch-exporter/releases/tag/v0.67.0>,
then:

```bash
sudo install -o root -g root -m 0755 yace /usr/local/bin/yace
sudo cp systemd/yace.service /etc/systemd/system/yace.service
sudo systemctl daemon-reload
sudo systemctl enable --now yace.service
curl -s http://127.0.0.1:5000/metrics | grep '^aws_rds_' | head
```

If no AWS credentials are configured, use the AWS SDK default credential chain (an EC2 instance
profile, ECS task role, or IRSA is preferred over any static-key file) -- see
[`iam/README.md`](../iam/README.md).

An optional YACE `roleArn` is a **second hop**. The host still needs valid base credentials that
allow `sts:AssumeRole`; this is especially important for on-premises hosts, which have no EC2
instance profile by default.

## 5. Point Prometheus at both exporters

Adapt [`prometheus/prometheus.yml`](../prometheus/prometheus.yml): replace the `*.invalid`
placeholder targets in the `postgres_exporter` and `yace` scrape jobs with your real exporter
endpoints (or switch to file/EC2/Consul service discovery if you manage many instances). Copy
`prometheus/rules/*.yml` alongside your existing rule files and add them to your Prometheus's
`rule_files:` list (or merge if you already have `rule_files:` entries).

The reference systemd units listen on all interfaces so a separate Prometheus host can reach
them. Restrict TCP/9187 and TCP/5000 to your Prometheus security group or host firewall; for
untrusted networks, terminate TLS/mTLS at a reverse proxy as described in
[`docs/security.md`](security.md).

Validate before deploying:

```bash
promtool check config prometheus/prometheus.yml
promtool check rules prometheus/rules/recording_rules.yml prometheus/rules/alerts.yml
promtool test rules prometheus/tests/*.yml
```

(`python scripts/validate.py --only prometheus` does this automatically, downloading a pinned
`promtool` if needed -- see [`docs/troubleshooting.md`](troubleshooting.md).)

## 6. Provision the Grafana dashboard

Copy `grafana/provisioning/datasources/prometheus.yml` and
`grafana/provisioning/dashboards/dashboards.yml` into your Grafana's provisioning directories
(default `/etc/grafana/provisioning/{datasources,dashboards}/`), adjusting the datasource `url` to
your real Prometheus endpoint, and copy `grafana/dashboards/aurora-postgresql-overview.json` to
wherever `dashboards.yml`'s `options.path` points. Restart/reload Grafana; the dashboard will
appear under the "Aurora PostgreSQL" folder.

See [`docs/dashboard-guide.md`](dashboard-guide.md) for how to read it, and
[`docs/metrics-reference.md`](metrics-reference.md) for which panels are OPTIONAL and why.

## 7. Validate wiring end to end

- `pg_up` should read `1` for every instance.
- `up{job="postgres_exporter"}` and `up{job="yace"}` should read `1`.
- `aws_rds_cpuutilization_average` should have data within ~5 minutes of YACE starting (CloudWatch
  `AWS/RDS` metrics publish roughly every 60s-5m depending on the metric).
- Trigger `AuroraPostgresHighConnections` in a non-production environment by opening enough idle
  connections to confirm alert routing works end to end.

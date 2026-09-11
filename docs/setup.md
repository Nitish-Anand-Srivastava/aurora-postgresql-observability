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
sudo useradd --system --no-create-home --shell /usr/sbin/nologin postgres_exporter
sudo install -d -o postgres_exporter -g postgres_exporter -m 0750 /etc/postgres_exporter
sudo install -o postgres_exporter -g postgres_exporter -m 0640 \
  exporters/postgres_exporter/postgres_exporter.env.template \
  /etc/postgres_exporter/postgres_exporter.env
# Install the global RDS CA bundle downloaded in step 1 for the service account. Review AWS
# certificate rotation guidance before upgrades:
# https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.SSL.html
sudo install -o root -g postgres_exporter -m 0640 \
  /tmp/rds-global-bundle.pem /etc/postgres_exporter/global-bundle.pem
rm /tmp/rds-global-bundle.pem
# edit /etc/postgres_exporter/postgres_exporter.env: set DATA_SOURCE_URI to this instance's
# endpoint, DATA_SOURCE_USER=pg_exporter
printf '%s' 'REPLACE_WITH_REAL_PASSWORD' | sudo tee /etc/postgres_exporter/pgpassword >/dev/null
sudo chmod 0400 /etc/postgres_exporter/pgpassword
sudo chown postgres_exporter:postgres_exporter /etc/postgres_exporter/pgpassword
```

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

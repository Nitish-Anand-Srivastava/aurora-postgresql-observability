# Security

This document covers the security model of the *configuration shipped in this repository* --
least-privilege database access, least-privilege AWS access, secret handling, and network posture.
For how to report a vulnerability *in this repository's content*, see
[`../SECURITY.md`](../SECURITY.md).

## Principle: no secrets in version control, ever

- Every credential this stack needs (PostgreSQL password, AWS credentials, Grafana admin
  password) is read at **runtime** from a file or environment variable outside version control:
  - postgres_exporter: `DATA_SOURCE_PASS_FILE` (preferred over the plaintext
    `DATA_SOURCE_PASS` environment variable) -- see
    [`exporters/postgres_exporter/postgres_exporter.env.template`](../exporters/postgres_exporter/postgres_exporter.env.template).
  - YACE: the [AWS SDK default credential chain](https://aws.github.io/aws-sdk-go-v2/docs/configuring-sdk/#specifying-credentials)
    (instance role preferred; shared credentials file only for local validation) -- see
    [`exporters/yace/yace.env.template`](../exporters/yace/yace.env.template).
  - Grafana (local compose only): the `$__file{}` config provider reading a Docker secret file --
    see [`docker/docker-compose.yml`](../docker/docker-compose.yml).
- All hostnames in tracked configuration use the reserved `.invalid` TLD (never a real DNS name),
  and all account IDs are RFC-reserved-style placeholders (`123456789012`,
  `111111111111`) -- see [`iam/yace-trust-policy-example.json`](../iam/yace-trust-policy-example.json).
- `.gitignore` blocks common secret file shapes (`*.env`, `*password*.txt`, `*.pem`, `docker/secrets/*.txt`,
  `docker/aws-credentials`, etc.) while keeping their `*.example`/`*.template` counterparts tracked.
- `scripts/checks/check_secrets.py` (run by CI on every push/PR) scans tracked files for
  high-confidence secret shapes (AWS access key ID format, PEM private key headers) and a generic
  "credential-looking assignment" heuristic, failing the build if a real-looking one appears.

## Database access: least privilege

[`sql/create_monitoring_role.sql`](../sql/create_monitoring_role.sql) creates a
`postgres_exporter` role
that:

- Is granted the built-in `pg_monitor` role (bundles `pg_read_all_settings`, `pg_read_all_stats`,
  `pg_stat_scan_tables` -- read-only access to statistics/settings views, **not** table data).
- Has `CONNECT` on only the databases you explicitly grant it to.
- Cannot create objects, alter schema, or modify data (`pg_monitor` grants no `INSERT`/`UPDATE`/
  `DELETE`/`CREATE` privileges).
- Has `lock_timeout`/`statement_timeout` set so a stuck monitoring query can never hold locks or
  run indefinitely against a production database.
- Has no password set by the script itself -- you set one out-of-band (Secrets Manager, IAM DB
  auth, or interactively), so the credential material never touches this repository or your shell
  history via a `CREATE ROLE ... PASSWORD` command.

If you enable the optional `pg_stat_statements` collector, note that it can expose **normalized
SQL text** (not literal parameter values, when the extension's `pg_stat_statements.track` default
behavior is used) to anyone who can read `pg_scrape_collector_success`-adjacent metrics from
Prometheus -- treat your Prometheus/Grafana access controls accordingly, and see
[`docs/metrics-reference.md`](metrics-reference.md) for the exact opt-in flag.

## AWS access: least privilege

[`iam/yace-readonly-policy.json`](../iam/yace-readonly-policy.json) grants only:

- `cloudwatch:GetMetricData`, `cloudwatch:GetMetricStatistics`, `cloudwatch:ListMetrics` (read-only
  metric retrieval; no `cloudwatch:PutMetricData`, no alarm/dashboard management).
- `tag:GetResources` (resource discovery by tag; no ability to modify tags or resources).
- `iam:ListAccountAliases` (optional, cosmetic `aws_account_info` label only).

None of these actions can modify AWS resources. See [`iam/README.md`](../iam/README.md) for why
these specific actions (and not the broader "quick start" policy in YACE's own README, which
includes permissions for services this repo doesn't scrape) are sufficient, and why none of them
support resource-level (`Resource` other than `"*"`) restriction (an AWS API limitation, not an
oversight in this policy).

## Network posture

- postgres_exporter connects to PostgreSQL using the DSN in `DATA_SOURCE_URI` -- use
  `sslmode=verify-full` in production (the template defaults to it) so the connection is both
  encrypted and validated against Aurora's RDS CA bundle. `docs/setup.md` installs the current
  AWS global bundle at the `sslrootcert` path in the environment template.
- Prometheus scrapes both exporters over plain HTTP by default in the reference configs, assuming
  a private network / VPC boundary. Add TLS via postgres_exporter's `--web.config.file`
  ([exporter-toolkit web config](https://github.com/prometheus/exporter-toolkit/blob/master/docs/web-configuration.md))
  or a reverse proxy if scraping across an untrusted network. YACE has no equivalent native TLS
  listener flag as of v0.67.0 -- front it with a reverse proxy if needed.
- Both systemd units run as dedicated, unprivileged, non-login users with `ProtectSystem=strict`,
  `NoNewPrivileges=true`, `PrivateTmp=true`, and related sandboxing directives (see
  [`systemd/README.md`](../systemd/README.md)).

## Local Docker Compose validation stack

The stack in [`docker/`](../docker/) never connects to real Aurora/AWS infrastructure by default
(it spins up a disposable local PostgreSQL container instead, and YACE simply fails CloudWatch
auth without credentials) -- see [`docker/README.md`](../docker/README.md) for exactly what is and
isn't live, and how to opt into real AWS calls if you want to.

## CI secret scanning

`.github/workflows/ci.yml` runs `scripts/checks/check_secrets.py` (and the rest of
`scripts/validate.py`) on every push and pull request, using no live credentials -- every check in
this repository either needs no network access or, for `promtool`, only needs to download a public
release binary over HTTPS.

# Local Docker Compose validation stack

This stack is for **local validation only** (dashboards, Prometheus rules, exporter wiring). It
is not a deployment topology for production Aurora PostgreSQL monitoring — see
[`docs/setup.md`](../docs/setup.md) and [`systemd/`](../systemd/) for that.

## What it does and does not connect to

| Service | Connects to | Live/production? |
| --- | --- | --- |
| `postgres` | itself (a disposable container) | No — throwaway local PostgreSQL 17 for validation |
| `postgres_exporter` | the `postgres` container above | No |
| `yace` | real AWS CloudWatch API, **only if** you supply credentials (see below) | Optional, off by default |
| `prometheus` | `postgres_exporter` + `yace` containers on the compose network | No |
| `grafana` | `prometheus` container | No |

Running `docker compose up` with no further setup starts every service. `yace` will log
CloudWatch authentication errors (because no AWS credentials are present) and simply report no
`aws_rds_*` metrics — this is expected and does not prevent validating the rest of the stack
(dashboards, Prometheus/postgres_exporter panels, alerting rules).

## Quick start

```powershell
cd docker
Copy-Item secrets\postgres_superuser_password.txt.example secrets\postgres_superuser_password.txt
Copy-Item secrets\pg_exporter_password.txt.example secrets\pg_exporter_password.txt
Copy-Item secrets\grafana_admin_password.txt.example secrets\grafana_admin_password.txt
# Edit the three copied files to use non-default passwords for anything beyond a throwaway
# local sandbox.
docker compose up -d
```

Then open:
- Grafana: <http://localhost:3000> (user `admin`; credential stored in
  `secrets/grafana_admin_password.txt`, not printed here)
- Prometheus: <http://localhost:9090>
- postgres_exporter metrics: <http://localhost:9187/metrics>
- YACE metrics: <http://localhost:5000/metrics>

Tear down with `docker compose down -v` (the `-v` also removes the local Postgres/Prometheus/
Grafana data volumes).

## Enabling real AWS CloudWatch data (optional)

1. `Copy-Item aws-credentials.example aws-credentials` and fill in real (non-committed)
   credentials for a principal that has the permissions in
   [`../iam/yace-readonly-policy.json`](../iam/yace-readonly-policy.json).
2. Uncomment the `aws-credentials` volume mount and `AWS_SHARED_CREDENTIALS_FILE` line in the
   `yace` service of `docker-compose.yml`.
3. Tag a real RDS/Aurora resource with `aurora-observability=true` (or edit
   `../exporters/yace/config.yml`'s `searchTags` to match your tagging scheme) so YACE's
   discovery job finds it.
4. `docker compose up -d --force-recreate yace`

`aws-credentials` and the three `secrets/*.txt` files are all gitignored — only the `*.example`
templates are tracked. Never commit real values.

## Why the Prometheus config differs from `prometheus/prometheus.yml`

`prometheus/prometheus.yml` (repo root) is the production reference template and uses
placeholder `*.invalid` hostnames, since it's meant to be copied and adapted to your real exporter
endpoints. `docker/prometheus.local.yml` instead points at the actual Docker Compose service
names (`postgres_exporter`, `yace`) so the compose stack is scrape-ready out of the box. Both
files load the same rule files from `prometheus/rules/`.

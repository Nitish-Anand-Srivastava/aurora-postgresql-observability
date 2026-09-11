# Architecture

## Components

```
┌─────────────────────┐        ┌──────────────────────┐
│ Aurora PostgreSQL    │        │ AWS CloudWatch        │
│ cluster (writer +    │        │ (AWS/RDS namespace)   │
│ 0..N readers)        │        │                       │
└──────────┬───────────┘        └───────────┬───────────┘
           │ SQL (pg_monitor role,            │ GetMetricData / ListMetrics
           │ TLS, least privilege)            │ (ordinary CloudWatch API)
           ▼                                  ▼
┌──────────────────────┐        ┌──────────────────────┐
│ postgres_exporter     │        │ YACE                  │
│ v0.20.1, one process  │        │ v0.67.0, one process  │
│ per instance          │        │ for the account/region│
└──────────┬───────────┘        └───────────┬───────────┘
           │ :9187/metrics                    │ :5000/metrics
           └───────────────┬──────────────────┘
                           ▼
                 ┌───────────────────┐
                 │ Prometheus v3.14.0 │
                 │ (scrape + rules)   │
                 └─────────┬─────────┘
                           ▼
                 ┌───────────────────┐
                 │ Grafana v13.2.1    │
                 │ (provisioned       │
                 │ datasource +       │
                 │ dashboard)         │
                 └───────────────────┘
```

Neither exporter talks to the other, and neither talks to the CloudWatch **Database Insights** /
**Performance Insights** APIs (see below) -- both are independent, stateless scrapers that
Prometheus polls on its own schedule.

## Why two exporters

Aurora PostgreSQL observability has two fundamentally different data sources:

1. **Inside the engine**: connection counts, buffer cache hit ratio, per-table statistics,
   replication lag as PostgreSQL itself sees it, vacuum/XID age, lock contention. Only reachable
   by connecting to PostgreSQL and querying `pg_stat_*`/`pg_settings` -- `postgres_exporter`'s job.
2. **Outside the engine, from AWS's perspective**: CPU/memory of the underlying instance, storage
   volume usage of the shared Aurora cluster volume, AWS's own replica lag measurement, IOPS/
   throughput as billed. Only reachable via the CloudWatch API -- YACE's job.

Cross-checking the two (e.g. `pg_replication_lag_seconds` vs. `aws_rds_aurora_replica_lag_average`)
is intentional and built into the shipped dashboard: they can diverge (e.g. during
instance/network issues) and disagreement is itself a useful signal.

## CloudWatch API boundary

This is the single most important architectural boundary in this repository, called out
repeatedly in configs and docs because it is a common source of confusion. **Terminology note:**
AWS announced that Performance Insights reached end-of-life on **July 31, 2026**, migrating
existing Performance Insights users to **CloudWatch Database Insights**
(<https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Database-Insights.html>).
Database Insights has two modes:

- **Standard mode** (the default, included with RDS/Aurora): analyzes the top contributors to
  DB Load by dimension (SQL, wait-event, host, user), with up to 7 days of included retention.
- **Advanced mode** (paid, with configurable retention from 1 to 24 months): everything Standard
  does, plus --
  automatically imports Performance Insights counter metrics into CloudWatch; adds
  process-level metrics via [Amazon RDS Enhanced Monitoring](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/USER_PerfInsights_Counters.html);
  SQL lock analysis (Aurora PostgreSQL/RDS PostgreSQL only) and SQL execution-plan analysis with a
  15-month guided UX; per-query statistics visualization; slow-SQL query analysis (requires
  exporting database logs to CloudWatch Logs); a consolidated dashboard across metrics, logs,
  events, and applications; RDS events surfaced in CloudWatch; fleet-wide monitoring views; and
  on-demand analysis for an arbitrary historical time window.

| API | What it returns | Used by this repo? |
| --- | --- | --- |
| CloudWatch `GetMetricData` / `ListMetrics` (the "ordinary metrics" API, `AWS/RDS` namespace) | CPU, memory, IOPS, storage, AWS-side replica lag, etc. -- one data point per metric per period. | **Yes**, via YACE. See `exporters/yace/config.yml`. |
| The Performance Insights API (`GetResourceMetrics`, `DescribeDimensionKeys`, `GetDimensionKeyDetails`, `ListAvailableResourceMetrics`, and the report-generation actions `CreatePerformanceAnalysisReport`/`GetPerformanceAnalysisReport`/`ListPerformanceAnalysisReports`/`DeletePerformanceAnalysisReport`) -- kept under the AWS `pi:*` IAM action namespace and CLI (`aws pi ...`) as a **compatibility API surface** underneath the Database Insights product/console experience; there is no separately-named "Database Insights API" with different action names as of this writing. | **DB load / Average Active Sessions (AAS)**, wait-event breakdowns, top-SQL, per-dimension analysis. A completely separate data model (a 2-D time series of *sessions* decomposed by dimension, not simple numeric gauges) requiring its own IAM actions and its own client calls. | **No.** YACE does not implement this API and this repository does not claim otherwise. |

**Do not configure YACE to try to scrape Database Insights / Performance Insights data** -- it
cannot: YACE's CloudWatch metrics stream (`cloudwatch:GetMetricData`) and the Performance Insights
compatibility API (`pi:GetResourceMetrics`) are unrelated AWS APIs with unrelated IAM action
namespaces, and no CloudWatch namespace re-exposes DB Load/AAS as an ordinary metric --
**with one exception**: if you enable Database Insights **Advanced mode**, AWS *automatically
imports Performance Insights counter metrics into CloudWatch* for you (see the mode comparison
table above). Even then, this happens as a first-party AWS feature outside of and unrelated to
YACE's scrape jobs; this repository does not attempt to enumerate those Advanced-mode-only,
account/mode-dependent metric names, and none of them are required by, or referenced in, any
config/dashboard/alert shipped here.

### Optional: designing (not shipping) a Database Insights / Performance Insights collector

If you want true DB load / AAS / top-SQL data in Prometheus and are not using (or don't want to
rely solely on) Database Insights Advanced mode's automatic CloudWatch metric import, you need a
**separate, dedicated collector** that calls the Performance Insights compatibility API directly.
This repository intentionally does **not** ship one (to avoid shipping an unsupported,
unmaintained, "fake" integration), but here is a safe design sketch if you choose to build it
yourself:

- **API calls**: `GetResourceMetrics` (DB load / AAS, and any counter metrics) on a schedule (data
  granularity is ~1 minute for Standard mode, ~1 second for Advanced mode); optionally
  `DescribeDimensionKeys`/`GetDimensionKeyDetails` for top-SQL/wait-event breakdowns.
- **IAM**: a distinct policy from `iam/yace-readonly-policy.json` -- `pi:GetResourceMetrics`,
  `pi:DescribeDimensionKeys`, `pi:GetDimensionKeyDetails`, `pi:ListAvailableResourceMetrics`,
  scoped by resource ARN (`arn:aws:pi:<region>:<account>:metrics/rds/<dbi-resource-id>`) where the
  API supports resource-level permissions.
- **Exporter shape**: a small custom Prometheus exporter (Go/Python) that calls
  `GetResourceMetrics` on an interval matching the underlying sample granularity and exposes
  gauges such as `db_load_avg_active_sessions` and   `db_load_by_wait_event{wait_event="..."}`. Select and security-review an implementation
  independently; this repository does not endorse or vendor an API collector.
- **Cost awareness**: this API is billed separately from CloudWatch `GetMetricData` calls; poll no
  more frequently than the underlying sample granularity.

This design is documented for completeness only -- it is not implemented, wired into
`docker-compose.yml`, or referenced by any dashboard panel in this repo.

## Data flow / trust boundaries

- **Credentials never live in tracked files.** postgres_exporter reads `DATA_SOURCE_PASS_FILE`
  from a file outside version control; YACE reads AWS credentials via the SDK default credential
  chain (instance role, or a gitignored shared credentials file for local validation only). See
  `docs/security.md`.
- **postgres_exporter connects directly to Aurora** using the least-privilege `pg_exporter` SQL
  role (`sql/create_monitoring_role.sql`), typically over TLS (`sslmode=verify-full`) from within
  the same VPC or via a bastion/PrivateLink, depending on your network topology (out of scope for
  this repo).
- **YACE calls the AWS CloudWatch and Resource Groups Tagging APIs** using the IAM policy in
  `iam/yace-readonly-policy.json`. It never connects to the database directly.
- **Prometheus scrapes both exporters over plain HTTP on private networks** in the reference
  configs. The systemd units listen on all interfaces for remote Prometheus deployments, so
  firewall access to Prometheus only; add TLS/mTLS via a reverse proxy or postgres_exporter's
  `--web.config.file` if scraping across an untrusted network.
- **Grafana talks only to Prometheus**, never directly to Aurora or AWS.

## Deployment shapes documented in this repo

| Shape | Where |
| --- | --- |
| Local, disposable validation of dashboards/rules/exporter wiring | `docker/docker-compose.yml` |
| Production Linux host, systemd-managed exporters | `systemd/*.service` + `docs/setup.md` |
| Prometheus/Grafana themselves | Assumed pre-existing per the task scope; this repo ships their *configuration* (`prometheus/`, `grafana/provisioning`, `grafana/dashboards`), not a from-scratch Prometheus/Grafana install guide, though the compose file does run pinned versions of both for local validation. |

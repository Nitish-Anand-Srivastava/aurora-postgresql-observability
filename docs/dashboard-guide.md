# Dashboard Interpretation Guide

Dashboard: [`grafana/dashboards/aurora-postgresql-overview.json`](../grafana/dashboards/aurora-postgresql-overview.json)
(UID `aurora-postgresql-observability`), provisioned via
[`grafana/provisioning/dashboards/dashboards.yml`](../grafana/provisioning/dashboards/dashboards.yml)
into the "Aurora PostgreSQL" folder.

## Variables

| Variable | Purpose |
| --- | --- |
| Data source (`$DS_PROMETHEUS`) | A **datasource-type template variable**, not a hardcoded UID -- the dashboard works against whatever Prometheus datasource you select, including the auto-provisioned "Prometheus" datasource from `grafana/provisioning/datasources/prometheus.yml`. |
| `$cluster` | Filters to one or more normalized `cluster` labels. The reference Prometheus config maps YACE's `dimension_DBClusterIdentifier` or exported `tag_AuroraCluster` to this label. Multi-select, defaults to All. |
| `$instance` | Filters to one or more AWS `DBInstanceIdentifier` values, scoped by `$cluster`. Configure each postgres_exporter target with that value; Prometheus maps YACE's `dimension_DBInstanceIdentifier` to the same label. Multi-select, defaults to All. |
| `$database` | Filters to one or more `datname` label values, scoped by `$instance`. Multi-select, defaults to All. Only affects panels querying per-database metrics (connections, transactions, table stats); instance-wide panels (CPU, replication) ignore it. |
| `$rate_interval` | A plain **interval-type** variable (1m/5m/10m/30m/1h, default 5m) used inside `rate()`/`irate()` in throughput panels. Increase it if a panel looks noisy at a wide time range; decrease it to see short bursts. Fixed-window recording rules remain available for alerts and external consumers. |

## Reading the OPTIONAL markers

Panel titles and descriptions that start with **OPTIONAL** depend on something beyond the default
install:

- **OPTIONAL (AWS CloudWatch)**: requires the `yace` scrape job configured and reachable, valid AWS
  credentials, and the target resources tagged to match `exporters/yace/config.yml`'s
  `searchTags`. Without that, the panel simply shows "No data" -- this does not indicate a broken
  dashboard.
- **OPTIONAL (PostgreSQL 17+ / `stat_checkpointer`)**: only populated on PostgreSQL/Aurora
  PostgreSQL 17 or later.
- **OPTIONAL (`pg_stat_statements`)**: requires `--collector.stat_statements` enabled (off by
  default -- see `docs/metrics-reference.md`) and the `pg_stat_statements` extension created.

Every panel's own description (hover the info icon) names its exact source metric(s) and whether
it's optional -- this file gives the *why*, the panel description gives the *what*.

## Row-by-row guide

- **Availability**: `pg_up` (exporter → DB connectivity) vs. `up{job=...}` (Prometheus → exporter
  connectivity) are deliberately separate stats -- a red "postgres_exporter scrape target up" with
  a green "Instance up" is impossible (if Prometheus can't reach the exporter it can't know
  `pg_up` either); a red "Instance up" with green "postgres_exporter scrape target up" means the
  exporter process is fine but the database connection is failing.
- **Connections**: the connections-used-ratio gauge directly mirrors the
  `AuroraPostgresHighConnections{,Critical}` alert thresholds (80%/95%) via matching threshold
  colors.
- **DB Load / Active Sessions**: read the text panel first -- the "active sessions" panel is an
  approximation, not the CloudWatch Database Insights DB Load (Average Active Sessions) metric
  exposed via the Performance Insights compatibility API. See
  `docs/architecture.md#cloudwatch-api-boundary`.
- **CPU & Memory / Latency & Throughput / I/O / Storage**: mostly AWS CloudWatch (YACE) panels,
  since these are infrastructure-level signals AWS measures more directly than PostgreSQL can see
  from inside itself. Cluster-level storage panels (Aurora's shared volume) are explicitly labeled
  "cluster-level" and grouped by `dimension_DBClusterIdentifier`, not per instance.
- **Cache**: two independent cache-hit-ratio measurements (postgres_exporter's
  `pg_stat_database_blks_hit/read`-derived ratio, and AWS's own `BufferCacheHitRatio`) are shown
  side by side deliberately, as a cross-check.
- **Locks & Waits / Transactions & Errors**: `wait_event_type` buckets and lock modes come
  straight from `pg_stat_activity`/`pg_locks`; see `docs/troubleshooting.md#locks-and-waits`.
- **Query Performance**: entirely OPTIONAL (see above); the row's text panel explains the opt-in
  requirement before the panels themselves (which will show "No data" until enabled).
- **Vacuum & Transaction ID (XID) Wraparound**: the age panel's thresholds (1B/1.7B) match
  `AuroraPostgresXIDWraparoundWarning`/`Critical` in `prometheus/rules/alerts.yml` exactly -- this
  is the earliest visual warning of the single most severe failure mode this repo alerts on
  (forced read-only mode).
- **WAL & Checkpoints**: checkpoint-specific panels are PostgreSQL 17+-only (`stat_checkpointer`);
  WAL size (`pg_wal_size_bytes`) works on every supported version.
- **Replication / Readers**: AWS-side `aws_rds_aurora_replica_lag_average` is enabled when YACE is
  configured. PostgreSQL-side `pg_replication_lag_seconds` is OPTIONAL because Aurora PostgreSQL
  17.7 rejects its generic collector on writers; the per-standby `stat_replication` panel remains
  enabled.
- **Exporter Health**: `pg_scrape_collector_success`/`_duration_seconds` are the same series the
  `PostgresExporterCollectorFailing` alert watches; `yace_cloudwatch_requests_total` is YACE's own
  self-monitoring metric, useful for CloudWatch API cost/throttling awareness.

## Links

The dashboard's top-level Links menu points at this guide, `docs/metrics-reference.md`, and
`docs/troubleshooting.md`. Several panels also carry a per-panel "Runbook" link that jumps directly
to the matching `docs/troubleshooting.md` section.

## Regenerating the dashboard JSON

The dashboard is authored via a small local Python generator (not shipped in this repo) rather than
hand-edited, specifically to keep ~45 panels' worth of Grafana JSON schema (grid positions, panel
IDs, target `refId`s) internally consistent. If you extend the dashboard by hand instead, run
`python scripts/checks/check_dashboard_json.py` afterwards -- it enforces the structural
requirements this guide describes (datasource variable present, required template variables
present, every panel has a description, every panel's `expr` only references documented metrics).

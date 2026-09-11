# Troubleshooting / Runbooks

Each section below corresponds to the `runbook_url` annotation on an alert in
[`prometheus/rules/alerts.yml`](../prometheus/rules/alerts.yml) (the anchor matches the alert
name) or a dashboard panel link. Alert labels always include `instance` (and `datname` where
relevant) identifying exactly which target fired.

## AuroraPostgresInstanceDown

**Meaning:** `pg_up == 0` for 2+ minutes -- postgres_exporter is reachable but cannot connect
to/ping the PostgreSQL instance.

1. Check the instance's status in the AWS console / `aws rds describe-db-instances` -- is it
   rebooting, failing over, or in `storage-full`?
2. From the exporter host, try connecting directly: `psql "host=<endpoint> dbname=postgres
   sslmode=verify-full" -U pg_exporter` -- does it hang (network/security group issue) or reject
   auth (credential/role issue)?
3. Check security group / network ACL changes made around the alert's start time.
4. Check `pg_exporter`'s password hasn't expired/rotated without updating
   `/etc/postgres_exporter/pgpassword`.
5. If this fired during a **planned failover**, this is expected for the old writer's exporter
   instance briefly; confirm the *new* writer's `pg_up` is healthy.

## PostgresExporterScrapeFailed

**Meaning:** `up{job="postgres_exporter"} == 0` -- Prometheus cannot reach the exporter process
itself (distinct from `AuroraPostgresInstanceDown`, which means the exporter is up but the
database isn't).

1. `systemctl status postgres_exporter` on the exporter host -- did the process crash? Check
   `journalctl -u postgres_exporter -n 100`.
2. Confirm the host is reachable from Prometheus (security group / firewall / DNS).
3. Confirm the exporter's `--web.listen-address` matches what `prometheus.yml` targets.

## PostgresExporterCollectorFailing

**Meaning:** a specific `pg_scrape_collector_success{collector="..."}` has been `0` for 15+
minutes -- one collector's SQL query is failing, but the exporter process and DB connection are
otherwise healthy.

1. Check exporter logs for the specific collector's error (`journalctl -u postgres_exporter |
   grep <collector>`).
2. Common causes: the `pg_exporter` role is missing a grant needed by that collector (re-run
   `sql/create_monitoring_role.sql`), the collector requires a PostgreSQL version this instance
   doesn't have (e.g. `stat_checkpointer` needs 17+), or `pg_stat_statements` isn't created for the
   `stat_statements` collector.
3. This does not page as critical because other collectors (and `pg_up`) still functioning means
   most of the dashboard remains accurate; treat as a data-quality issue to fix during business
   hours.

## YaceExporterScrapeFailed

**Meaning:** `up{job="yace"} == 0` -- Prometheus cannot reach the YACE process.

1. `systemctl status yace` / `journalctl -u yace -n 100` on the YACE host.
2. Note: this alert does **not** fire on CloudWatch API/auth failures (those still return HTTP 200
   from `/metrics`, just with stale/absent `aws_rds_*` series) -- check YACE's own logs for
   `AccessDenied`/`ExpiredToken` messages separately, and confirm the IAM policy in
   `iam/yace-readonly-policy.json` is attached to the running identity.

## AuroraPostgresHighConnections

Warning-level threshold (80% of `max_connections` for 10 minutes). Follow the steps in
[AuroraPostgresHighConnectionsCritical](#aurorapostgreshighconnectionscritical) below -- the
remediation is identical, only the urgency differs.

## AuroraPostgresHighConnectionsCritical

**Meaning:** the ratio of current connections (`pg_stat_activity_count`) to
`pg_settings_max_connections` exceeded 80% (warning, 10m) or 95% (critical, 5m). (The dashboard's
"Connections used ratio" panel shows the same computation via the `aurora:connections_used_ratio`
recording rule; the alert itself inlines the expression to avoid depending on a recording rule
evaluated in a different rule group -- see the comment in `prometheus/rules/alerts.yml`.)

1. `SELECT state, count(*) FROM pg_stat_activity GROUP BY state ORDER BY 2 DESC;` -- is this
   legitimate traffic growth, or a connection leak (many `idle`/`idle in transaction`)?
2. Check the dashboard's "Connections by state" panel for the same breakdown over time.
3. Short-term mitigation: terminate leaked idle connections
   (`SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state = 'idle in transaction'
   AND state_change < now() - interval '10 minutes';` -- review carefully before running).
4. Longer-term: introduce/expand connection pooling (RDS Proxy, PgBouncer) rather than raising
   `max_connections` indefinitely.

## AuroraPostgresReplicaLagHigh

Warning-level threshold (>30s for 5 minutes). Follow the steps in
[AuroraPostgresReplicaLagCritical](#aurorapostgresreplicalagcritical) below -- the remediation is
identical, only the urgency differs.

## AuroraPostgresReplicaLagCritical

**Meaning:** `pg_replication_lag_seconds` exceeded 30s (warning) or 120s (critical) on a reader.

1. Check the reader's CPU/IOPS (dashboard's CPU & I/O rows) -- is the reader under-provisioned for
   replay load?
2. Check for long-running read queries or `idle in transaction` sessions on the reader holding
   back WAL replay (`SELECT * FROM pg_stat_activity WHERE state = 'idle in transaction' ORDER BY
   xact_start;`).
3. Cross-check `aws_rds_aurora_replica_lag_average` (AWS's own measurement) -- if it disagrees
   significantly with `pg_replication_lag_seconds`, suspect a clock/measurement issue rather than
   real lag.
4. If sustained and traffic-serving, consider routing read traffic away from the lagging reader
   until it recovers.

## AuroraPostgresXIDWraparoundWarning

Warning-level threshold (age > 1 billion transactions for 30 minutes). Follow the steps in
[AuroraPostgresXIDWraparoundCritical](#aurorapostgresxidwraparoundcritical) below now, before it
becomes critical.

## AuroraPostgresXIDWraparoundCritical

**Meaning:** `pg_database_wraparound_age_datfrozenxid_seconds` (a transaction-ID **age/count**,
despite the metric name's `_seconds` suffix -- see
[`docs/metrics-reference.md`](metrics-reference.md)) exceeded 1 billion (warning) or 1.7 billion
(critical) transactions old, out of a 2,146,483,648 hard limit at which PostgreSQL forces the
database into read-only mode.

1. Confirm autovacuum is actually running:
   `SELECT * FROM pg_stat_activity WHERE query ILIKE '%vacuum%';` and check
   `pg_stat_progress_vacuum` (dashboard panel: "Active vacuum progress").
2. Look for what's blocking it:
   - Long-running transactions holding back the freeze horizon:
     `SELECT pid, now() - xact_start AS age, query FROM pg_stat_activity WHERE xact_start IS NOT
     NULL ORDER BY age DESC LIMIT 10;`
   - Orphaned/inactive replication slots retaining old XIDs:
     `SELECT * FROM pg_replication_slots WHERE active = false;`
   - Prepared transactions left open: `SELECT * FROM pg_prepared_xacts;`
3. **Critical severity is an emergency**: run a manual `VACUUM (FREEZE, VERBOSE)
   <highest-age-table>;` on the table(s) with the oldest `age(relfrozenxid)`
   (`SELECT relname, age(relfrozenxid) FROM pg_class WHERE relkind = 'r' ORDER BY 2 DESC LIMIT
   10;`) immediately, in addition to fixing whatever is blocking autovacuum.
4. Cross-check `aws_rds_maximum_used_transaction_ids_maximum` (AWS's own measurement of the same
   underlying risk) to confirm this isn't a postgres_exporter-side query bug.

## AuroraPostgresDeadlocksElevated

**Meaning:** the 5-minute rate of `pg_stat_database_deadlocks` has been sustained non-zero for
10+ minutes (the same computation as the `aurora:deadlocks:rate5m` recording rule used on the
dashboard; the alert inlines the expression itself -- see `prometheus/rules/alerts.yml`).

1. Check PostgreSQL logs for deadlock detail (`log_lock_waits`/deadlock log entries include the
   two competing queries and their lock modes, if `log_min_messages` captures them).
2. Look for a recent application deploy that changed transaction/lock ordering.
3. A single isolated deadlock is normal under concurrent write load; a *sustained* non-zero rate
   (which is what pages) suggests a systemic ordering problem worth fixing in application code.

## Locks and Waits

General guidance for the "Locks & Waits" dashboard row (not tied to a specific alert):

1. `pg_locks_count` by `mode` climbing steadily (rather than being noisy/flat) usually indicates a
   long-held lock blocking others -- cross-reference "Longest running transaction".
2. Non-empty `wait_event_type` buckets (`Lock`, `LWLock`, `IO`, `Client`) show *why* backends are
   waiting: `Lock` means row/table lock contention, `IO` means storage latency, `Client` means the
   backend is waiting on the application (not a database-side problem).
3. `SELECT * FROM pg_locks WHERE NOT granted;` on the instance for the current list of blocked
   lock requests, joined to `pg_stat_activity` for the blocking/blocked query text.

## General exporter debugging

- `curl -s http://<exporter-host>:9187/metrics | head -50` -- confirm the exporter emits *any*
  metrics before assuming a specific collector/panel is broken.
- `curl -s http://<yace-host>:5000/metrics | grep yace_cloudwatch` -- YACE's own request counters
  help distinguish "no data because misconfigured `searchTags`" from "no data because AWS API
  calls are failing" from "no data because Prometheus isn't scraping it".
- Re-run `python scripts/validate.py` after any configuration change -- it catches YAML/JSON syntax
  errors, invalid Prometheus rule syntax, and dashboard panels referencing undocumented metrics
  before you deploy.

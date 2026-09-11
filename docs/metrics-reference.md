# Metrics Reference

This document is the authoritative map from **every PromQL metric or recording rule used in this
repository's dashboards and alerting rules** back to the exporter/collector that emits it, whether
it is enabled by default, and what it means. It also contains a **machine-readable inventory**
(the fenced block below) that `scripts/checks/check_dashboard_json.py` parses to guarantee no
dashboard panel ever references a metric this repository doesn't actually produce.

## How to read the tables

- **Source**: which exporter/collector emits the metric.
- **Default**: whether the collector/flag is enabled out of the box upstream. Where this repo's
  `systemd/postgres_exporter.service` or `docker/docker-compose.yml` deliberately turns on a
  collector that is disabled upstream, that is called out explicitly.
- **Optional**: metrics/panels marked OPTIONAL depend on something beyond "install and run" --
  a non-default collector flag, the `pg_stat_statements` extension, PostgreSQL 17+, or the
  `yace` scrape job / valid AWS credentials being configured at all.

## postgres_exporter (engine metrics, `pg_*` and bare `up`)

Version: [v0.20.1](https://github.com/prometheus-community/postgres_exporter/releases/tag/v0.20.1).
Flags and collector names below match that release's
[`--help`](https://github.com/prometheus-community/postgres_exporter#flags) output and
[`collector/*.go`](https://github.com/prometheus-community/postgres_exporter/tree/v0.20.1/collector)
source, verified directly against the tagged release rather than assumed from memory.

| Metric | Collector | Default | Notes |
| --- | --- | --- | --- |
| `up{job="postgres_exporter"}` | n/a (Prometheus scrape metadata) | always | 1 = Prometheus reached the exporter's `/metrics` endpoint at all. Distinct from `pg_up` below. |
| `pg_up` | base (always registered) | always | 1 = the exporter successfully pinged the target PostgreSQL instance on its last scrape. |
| `pg_scrape_collector_success` | base (always registered) | always | Per-collector last-scrape success flag, labeled `collector`. |
| `pg_scrape_collector_duration_seconds` | base (always registered) | always | Per-collector scrape duration. |
| `pg_settings_max_connections` | `settings` | enabled | From `pg_settings`; unitless integer settings get no unit suffix. To disable this collector, use `--no-collector.settings` -- the older `--disable-settings-metrics` flag / `PG_EXPORTER_DISABLE_SETTINGS_METRICS` env var was **removed in v0.20.0** and no longer exists. |
| `pg_stat_activity_count` | `stat_activity` | enabled | Backend count by `datname,state,usename,application_name,backend_type,wait_event_type,wait_event`. |
| `pg_stat_activity_max_tx_duration` | `stat_activity` | enabled | Longest-running open transaction per label set, seconds. |
| `pg_locks_count` | `locks` | enabled | Lock count by `datname,mode`. |
| `pg_stat_database_xact_commit` / `_xact_rollback` | `stat_database` | enabled | Per-database commit/rollback counters. |
| `pg_stat_database_blks_hit` / `_blks_read` | `stat_database` | enabled | Buffer cache hit/miss counters (feeds `aurora:cache_hit_ratio:ratio5m`). |
| `pg_stat_database_conflicts` | `stat_database` | enabled | Recovery-conflict cancellations; only non-zero on readers. |
| `pg_stat_database_deadlocks` | `stat_database` | enabled | Deadlocks detected per database. |
| `pg_stat_user_tables_n_tup_ins` / `_upd` / `_del` | `stat_user_tables` | enabled | Row-level DML counters per table. |
| `pg_stat_user_tables_n_live_tup` / `_n_dead_tup` | `stat_user_tables` | enabled | Live/dead row estimates (bloat proxy). |
| `pg_statio_user_tables_heap_blocks_read` / `_hit` | `statio_user_tables` | enabled | Table heap I/O (disk vs. buffer cache). |
| `pg_replication_is_replica` | `replication` | enabled | 1 on readers, 0 on the writer. |
| `pg_replication_lag_seconds` | `replication` | enabled | Only meaningful (non-zero possible) on readers. |
| `pg_replication_slots_pg_wal_lsn_diff` | `replication_slots` | enabled | Bytes of WAL a replication **slot** is retaining (per `slot_name`). Renamed from the singular `replication_slot` collector/metric family in older postgres_exporter releases -- v0.20.1 uses the plural `replication_slots` consistently for both the flag and the metric prefix. |
| `pg_stat_replication_pg_wal_lsn_diff` | `stat_replication` | enabled | Bytes of WAL lag per **connected standby** (labels `application_name,client_addr,state,slot_name`), from `pg_stat_replication` -- distinct from `pg_replication_slots_pg_wal_lsn_diff` above, which is per replication *slot* (a slot can exist without an actively connected standby, and vice versa for physical replication without slots). |
| `pg_stat_archiver_archived_count` / `_failed_count` | `stat_archiver` | enabled | Only meaningful if WAL archiving is configured/applicable. |
| `pg_stat_progress_vacuum_heap_blks` / `_heap_blks_scanned` | `stat_progress_vacuum` | enabled | Only has data points while a vacuum is actively running. |
| `pg_wal_size_bytes` / `pg_wal_segments` | `wal` | enabled | Total size / count of `pg_ls_waldir()` entries. |
| `pg_stat_checkpointer_num_timed_total` / `_num_requested_total` / `_write_time_total` / `_sync_time_total` | `stat_checkpointer` | **disabled upstream, ENABLED by this repo's systemd/compose** | **PostgreSQL 17+ only** (self-disables with a log warning on older engines). Replaces the checkpoint fields `stat_bgwriter` carried pre-17. |
| `pg_database_wraparound_age_datfrozenxid_seconds` | `database_wraparound` | **disabled upstream, ENABLED by this repo's systemd/compose** | **OPTIONAL / misnomer warning:** despite the `_seconds` suffix (an artifact of the exporter's generic unit-suffix convention), the value is a **transaction-ID age (a count)**, not a duration. Feeds the XID wraparound alerts. |
| `pg_stat_statements_calls_total` / `_seconds_total` | `stat_statements` | **disabled by default (OPTIONAL)** | Requires the `pg_stat_statements` extension (`CREATE EXTENSION`, added to `shared_preload_libraries`, reboot required) AND `--collector.stat_statements`. High-cardinality; assess before enabling. See `sql/create_monitoring_role.sql`. |
| `pg_long_running_transactions` / `pg_long_running_transactions_oldest_timestamp_seconds` | `long_running_transactions` | **disabled by default (OPTIONAL)** | Count of, and age of the oldest, open (non-idle) transaction excluding autovacuum. Overlaps with the always-on `pg_stat_activity_max_tx_duration` (per label set) above; this collector instead gives a single fleet-wide count/max-age pair. Not enabled in this repo -- prefer the built-in `stat_activity` collector unless you specifically need the aggregate shape. |

Collectors this repo explicitly disables (left at their upstream default of "off") and does not
use in any dashboard/alert: `process_idle`, `stat_wal_receiver`,
`postmaster`, `xlog_location`, `statio_user_indexes`. Flip the corresponding
`--no-collector.*`/`--collector.*` flag in `systemd/postgres_exporter.service` if you need them.

## yet-another-cloudwatch-exporter / YACE (`aws_rds_*`, `yace_*`)

Version: [v0.67.0](https://github.com/prometheus-community/yet-another-cloudwatch-exporter/releases/tag/v0.67.0).
Metric names are generated by YACE's `BuildMetricName(namespace, metricName, statistic)` (see
[`pkg/promutil/migrate.go`](https://github.com/prometheus-community/yet-another-cloudwatch-exporter/blob/master/pkg/promutil/migrate.go)
and the camelCase-to-snake_case conversion in
[`pkg/promutil/prometheus.go`](https://github.com/prometheus-community/yet-another-cloudwatch-exporter/blob/master/pkg/promutil/prometheus.go)),
verified directly against that source rather than assumed. Pattern: `aws_<namespace>_<metric>_<statistic>`,
e.g. CloudWatch `AWS/RDS` `CPUUtilization` with statistic `Average` becomes
`aws_rds_cpuutilization_average`.

**IMPORTANT CloudWatch API boundary:** every `aws_rds_*` metric below comes from the **ordinary
CloudWatch `GetMetricData`/`ListMetrics` APIs** against the `AWS/RDS` namespace, configured in
[`exporters/yace/config.yml`](../exporters/yace/config.yml). YACE does **not** call, and cannot
call, the Performance Insights compatibility API (`GetResourceMetrics` and related actions) that
underlies the CloudWatch Database Insights console feature -- see
[`docs/architecture.md`](architecture.md#cloudwatch-api-boundary) for that boundary in detail. All
of these are therefore OPTIONAL in the sense that they require the `yace` scrape job to be
configured and reachable, and the discovery job's `searchTags` to match your real Aurora
cluster/instance tags -- but they are ordinary metrics, not Database Insights data.

| Metric | CloudWatch metric | Level | Notes |
| --- | --- | --- | --- |
| `aws_rds_cpuutilization_average` / `_maximum` | `CPUUtilization` | instance | |
| `aws_rds_database_connections_average` | `DatabaseConnections` | instance | |
| `aws_rds_freeable_memory_average` | `FreeableMemory` | instance | |
| `aws_rds_swap_usage_average` | `SwapUsage` | instance | Not emitted for all instance classes. |
| `aws_rds_cpucredit_balance_average` / `aws_rds_cpucredit_usage_average` | `CPUCreditBalance` / `CPUCreditUsage` | instance | Only for burstable (`db.t3`/`db.t4g`) classes. |
| `aws_rds_read_latency_average` / `aws_rds_write_latency_average` | `ReadLatency` / `WriteLatency` | instance | Seconds. |
| `aws_rds_read_throughput_average` / `aws_rds_write_throughput_average` | `ReadThroughput` / `WriteThroughput` | instance | Bytes/sec. |
| `aws_rds_read_iops_average` / `aws_rds_write_iops_average` | `ReadIOPS` / `WriteIOPS` | instance | |
| `aws_rds_disk_queue_depth_average` | `DiskQueueDepth` | instance | |
| `aws_rds_commit_latency_average` / `aws_rds_commit_throughput_average` | `CommitLatency` / `CommitThroughput` | instance | |
| `aws_rds_buffer_cache_hit_ratio_average` | `BufferCacheHitRatio` | instance | |
| `aws_rds_aurora_optimized_reads_cache_hit_ratio_average` | `AuroraOptimizedReadsCacheHitRatio` | instance | Only present when the local NVMe Optimized Reads cache is available on the primary. |
| `aws_rds_aurora_estimated_shared_memory_bytes_average` | `AuroraEstimatedSharedMemoryBytes` | instance | Aurora PostgreSQL-specific; reported on the primary/writer instance only. |
| `aws_rds_maximum_used_transaction_ids_maximum` | `MaximumUsedTransactionIDs` | instance | Aurora PostgreSQL-specific; AWS-side cross-check for the XID wraparound risk panel/alert. |
| `aws_rds_free_local_storage_average` | `FreeLocalStorage` | instance | Aurora's per-instance ephemeral storage, distinct from the cluster volume. |
| `aws_rds_transaction_logs_disk_usage_average` | `TransactionLogsDiskUsage` | instance | Bytes consumed by Aurora PostgreSQL transaction logs. |
| `aws_rds_aurora_replica_lag_average` | `AuroraReplicaLag` | instance (readers) | Milliseconds; per-reader lag. AWS-side cross-check for `pg_replication_lag_seconds`. |
| `aws_rds_aurora_replica_lag_maximum_average` / `aws_rds_aurora_replica_lag_minimum_average` | `AuroraReplicaLagMaximum` / `AuroraReplicaLagMinimum` | instance (primary/writer) | **Distinct CloudWatch metrics, not statistics of `AuroraReplicaLag`.** Reported on the primary: the max/min lag across all readers in the cluster. |
| `aws_rds_oldest_replication_slot_lag_maximum` | `OldestReplicationSlotLag` | instance | Bytes; physical replication slots. |
| `aws_rds_volume_bytes_used_average` | `VolumeBytesUsed` | **cluster** | Shared Aurora storage volume; dimension is `DBClusterIdentifier`, not per instance. |
| `aws_rds_volume_read_iops_sum` / `aws_rds_volume_write_iops_sum` | `VolumeReadIOPs` / `VolumeWriteIOPs` | **cluster** | Billed I/O, 5-minute granularity. |
| `aws_rds_backup_retention_period_storage_used_average` | `BackupRetentionPeriodStorageUsed` | **cluster** | |
| `aws_rds_snapshot_storage_used_average` | `SnapshotStorageUsed` | **cluster** | Backup storage consumed by snapshots outside the backup retention window. |
| `aws_rds_total_backup_storage_billed_average` | `TotalBackupStorageBilled` | **cluster** | |
| `aws_rds_serverless_database_capacity_average` / `_maximum` | `ServerlessDatabaseCapacity` | **cluster** | Only emits data for Aurora Serverless v2 clusters. |
| `up{job="yace"}` | n/a (Prometheus scrape metadata) | n/a | 1 = Prometheus reached YACE's `/metrics` endpoint. Does **not** by itself mean CloudWatch API calls are succeeding. |
| `yace_cloudwatch_requests_total` | n/a (YACE's own internal counter) | n/a | Labeled by `api_name`; useful for CloudWatch API cost/throttling awareness. |

## Recording rules (`aurora:*`)

Defined in [`prometheus/rules/recording_rules.yml`](../prometheus/rules/recording_rules.yml).
These are pre-aggregated PromQL, not raw exporter output -- see that file's comments for the
underlying `expr` of each one. `scripts/checks/check_dashboard_json.py` validates that every
`aurora:*` name referenced by a dashboard panel is actually defined there.

## CloudWatch Database Insights: explicitly out of scope for YACE

AWS's Performance Insights feature reached end-of-life on **July 31, 2026**; existing Performance
Insights users were migrated to **CloudWatch Database Insights**
(<https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Database-Insights.html>), which
has a **Standard mode** (analyzes top DB Load contributors by dimension -- SQL, wait-event, host,
user) and an **Advanced mode** (adds: automatic import of Performance Insights counter metrics
into CloudWatch, Enhanced-Monitoring-based process metrics, SQL lock and execution-plan analysis,
per-query statistics, slow-SQL analysis via CloudWatch Logs export, a consolidated
metrics/logs/events/applications dashboard, RDS events in CloudWatch, fleet-wide monitoring views,
and on-demand historical analysis). Both modes expose **DB load / Average Active Sessions (AAS)**
and dimension breakdowns through the **Performance Insights API** (`GetResourceMetrics`,
`DescribeDimensionKeys`, etc.) -- kept as a compatibility API surface under the `pi:*` IAM
namespace, not renamed to match the "Database Insights" product name. This is a completely
different API surface from the CloudWatch metrics API that YACE calls. **YACE cannot and does not
scrape that data** (with one narrow exception: Database Insights Advanced mode's own automatic
import of PI counter metrics into CloudWatch is an AWS-side feature unrelated to YACE, and this
repo does not enumerate or depend on any of those account/mode-dependent metric names). Any panel
in this repo's dashboard that approximates "load" (the "DB Load / Active Sessions" row) is built
from `pg_stat_activity_count{state="active"}` via postgres_exporter, explicitly labeled as a
proxy, and is not the same measurement AWS Database Insights shows in its console. See
[`docs/architecture.md`](architecture.md#cloudwatch-api-boundary) for a design sketch of an
optional, unimplemented API collector if you want to build real Database Insights data ingestion
later.

<!-- BEGIN_MACHINE_READABLE_METRIC_INVENTORY -->
The following is parsed by `scripts/checks/check_dashboard_json.py`. Keep it in sync with the
tables above; every metric a dashboard panel references must appear here.

```text
# Prometheus / scrape metadata
up

# postgres_exporter base (always registered)
pg_up
pg_scrape_collector_success
pg_scrape_collector_duration_seconds

# postgres_exporter collectors enabled by default upstream
pg_settings_max_connections
pg_stat_activity_count
pg_stat_activity_max_tx_duration
pg_locks_count
pg_stat_database_xact_commit
pg_stat_database_xact_rollback
pg_stat_database_blks_hit
pg_stat_database_blks_read
pg_stat_database_conflicts
pg_stat_database_deadlocks
pg_stat_user_tables_n_tup_ins
pg_stat_user_tables_n_tup_upd
pg_stat_user_tables_n_tup_del
pg_stat_user_tables_n_live_tup
pg_stat_user_tables_n_dead_tup
pg_statio_user_tables_heap_blocks_read
pg_statio_user_tables_heap_blocks_hit
pg_replication_is_replica
pg_replication_lag_seconds
pg_replication_slots_pg_wal_lsn_diff
pg_stat_replication_pg_wal_lsn_diff
pg_stat_archiver_archived_count
pg_stat_archiver_failed_count
pg_stat_progress_vacuum_heap_blks
pg_stat_progress_vacuum_heap_blks_scanned
pg_wal_size_bytes
pg_wal_segments

# postgres_exporter collectors OPTIONAL / non-default (enabled by this repo's systemd/compose, or
# fully optional and left disabled)
pg_stat_checkpointer_num_timed_total
pg_stat_checkpointer_num_requested_total
pg_stat_checkpointer_write_time_total
pg_stat_checkpointer_sync_time_total
pg_database_wraparound_age_datfrozenxid_seconds
pg_stat_statements_calls_total
pg_stat_statements_seconds_total
pg_long_running_transactions
pg_long_running_transactions_oldest_timestamp_seconds

# YACE - AWS/RDS instance-level (OPTIONAL: requires the yace scrape job + valid AWS credentials)
aws_rds_cpuutilization_average
aws_rds_cpuutilization_maximum
aws_rds_database_connections_average
aws_rds_freeable_memory_average
aws_rds_swap_usage_average
aws_rds_cpucredit_balance_average
aws_rds_cpucredit_usage_average
aws_rds_read_latency_average
aws_rds_write_latency_average
aws_rds_read_throughput_average
aws_rds_write_throughput_average
aws_rds_read_iops_average
aws_rds_write_iops_average
aws_rds_disk_queue_depth_average
aws_rds_commit_latency_average
aws_rds_commit_throughput_average
aws_rds_buffer_cache_hit_ratio_average
aws_rds_aurora_optimized_reads_cache_hit_ratio_average
aws_rds_aurora_estimated_shared_memory_bytes_average
aws_rds_maximum_used_transaction_ids_maximum
aws_rds_free_local_storage_average
aws_rds_transaction_logs_disk_usage_average
aws_rds_aurora_replica_lag_average
aws_rds_aurora_replica_lag_maximum_average
aws_rds_aurora_replica_lag_minimum_average
aws_rds_oldest_replication_slot_lag_maximum

# YACE - AWS/RDS cluster-level (OPTIONAL)
aws_rds_volume_bytes_used_average
aws_rds_volume_read_iops_sum
aws_rds_volume_write_iops_sum
aws_rds_backup_retention_period_storage_used_average
aws_rds_snapshot_storage_used_average
aws_rds_total_backup_storage_billed_average
aws_rds_serverless_database_capacity_average
aws_rds_serverless_database_capacity_maximum

# YACE internal/self-monitoring
yace_cloudwatch_requests_total
```
<!-- END_MACHINE_READABLE_METRIC_INVENTORY -->

# Non-production Aurora tuning workload

The workload simulator grows one dedicated schema while producing database activity that is useful
for validating dashboards, alerts, `pg_stat_statements`, indexes, autovacuum behavior, and tuning
reports. It is deliberately destructive and resource-intensive. **Never run it against production.**
It does not discover or choose a target for you, and it never cleans up automatically.

## What it generates

- Append-heavy customers and events, with a configurable physical-size target (10 GiB by default).
- HOT-eligible updates to an unindexed column and non-HOT updates to an indexed column.
- Deletes that leave dead tuples and low autovacuum thresholds that create vacuum pressure.
- Indexed lookups, deliberate expression-based index misses, and bounded analytical scans.
- Row-lock contention with a 750 ms lock timeout. A lock holder is enabled with two or more workers.
- Tagged query families whose normalized `queryid` values are verified in `pg_stat_statements`.

All statements have a configurable statement timeout. The simulator has an immutable 100 GiB hard
ceiling, supports at most 32 workers, and limits post-growth churn to one hour.

## Prerequisites

1. Create a **dedicated non-production database** with a name you will explicitly acknowledge.
2. Use the Aurora writer endpoint. The safety preflight rejects recovery/read-only connections.
3. Grant the simulator login `CONNECT` and `CREATE` on only that database. It creates only the
   fixed `aurora_tuning_simulator` schema.
4. Add `pg_stat_statements` to the cluster parameter group's `shared_preload_libraries`, reboot if
   the parameter change requires it, and run `CREATE EXTENSION pg_stat_statements;` in the
   validation database.
5. Install Python 3.8+ and the PostgreSQL `psql` client.
6. Create a libpq password file outside the repository and restrict it to the current user:

   ```text
   writer.validation.invalid:5432:aurora_validation:simulator_login:replace-at-runtime
   ```

   ```bash
   chmod 600 "$HOME/.pgpass-aurora-validation"
   ```

Do not put a password in shell history, a process argument, this repository, or `PGPASSWORD`. The
script removes `PGPASSWORD` from child-process environments and supplies only `PGPASSFILE`.

## Run

Start with a small target, confirm the output, and then increase it. The acknowledgement value must
exactly match `--database`; this makes accidental execution against a differently named database
fail before bootstrap.

```bash
python3 workload/aurora_tuning_simulator.py \
  --host writer.validation.invalid \
  --port 5432 \
  --user simulator_login \
  --database aurora_validation \
  --pgpassfile "$HOME/.pgpass-aurora-validation" \
  --confirm-non-production aurora_validation \
  --target-gib 10 \
  --workers 4 \
  --statement-timeout-ms 30000 \
  --application-name-prefix aurora-tuning-sim
```

The bootstrap is idempotent. Existing progress is measured from the schema's physical relation
size, so the same command resumes after interruption rather than starting over. Progress prints
after completed growth batches, rate-limited to once every ten seconds. `Ctrl-C` and `SIGTERM` stop
new work, allow the current timeout-bounded statements to finish, print a partial report, and
preserve all data for a later resume.

Useful optional controls are `--batch-size` (100-100000 rows) and `--post-growth-seconds`
(0-3600). Run `python3 workload/aurora_tuning_simulator.py --help` for all bounds.

## Read the result

At successful completion the script verifies that growth, HOT-update, and analytical query tags
are represented in `pg_stat_statements`, then runs [`workload/report.sql`](../workload/report.sql).
The report includes schema size, resumability state, dead tuples, HOT versus total updates, vacuum
and analyze activity, per-index scans, and normalized query fingerprints ordered by execution time.

You can rerun the report without generating load:

```bash
PGPASSFILE="$HOME/.pgpass-aurora-validation" \
  psql -X -h writer.validation.invalid -U simulator_login -d aurora_validation \
  -f workload/report.sql
```

Review a candidate carefully before adding `EXPLAIN (ANALYZE, BUFFERS, WAL)`: it executes the query
and can add more load.

## Cleanup (manual only)

Stop every simulator process, verify the connected database and schema, then explicitly run:

```sql
SELECT current_database();
SELECT obj_description(
    'aurora_tuning_simulator'::regnamespace, 'pg_namespace'
);
-- Expected marker: aurora_tuning_simulator_owned:v1

DROP SCHEMA aurora_tuning_simulator CASCADE;
```

Dropping the dedicated validation database is usually the cleanest option. The simulator never
runs either cleanup method itself.

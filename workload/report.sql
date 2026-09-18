\set ON_ERROR_STOP on
\pset pager off

\echo '=== Aurora tuning simulator summary ==='
SELECT current_database() AS database,
       pg_size_pretty(pg_database_size(current_database())) AS database_size,
       pg_size_pretty(sum(pg_total_relation_size(c.oid))) AS simulator_size
  FROM pg_class AS c
  JOIN pg_namespace AS n ON n.oid = c.relnamespace
 WHERE n.nspname = 'aurora_tuning_simulator'
   AND c.relkind IN ('r', 'm')
 GROUP BY current_database();

\echo '=== Progress and resumability state ==='
TABLE aurora_tuning_simulator.simulator_state;

\echo '=== Table churn, dead tuples, and vacuum pressure ==='
SELECT relname,
       n_live_tup,
       n_dead_tup,
       n_tup_ins,
       n_tup_upd,
       n_tup_hot_upd,
       n_tup_del,
       vacuum_count,
       autovacuum_count,
       analyze_count,
       autoanalyze_count,
       last_autovacuum,
       last_autoanalyze
  FROM pg_stat_user_tables
 WHERE schemaname = 'aurora_tuning_simulator'
 ORDER BY relname;

\echo '=== Index usage and deliberately poor access paths ==='
SELECT relname,
       indexrelname,
       idx_scan,
       idx_tup_read,
       idx_tup_fetch,
       pg_size_pretty(pg_relation_size(indexrelid)) AS index_size
  FROM pg_stat_user_indexes
 WHERE schemaname = 'aurora_tuning_simulator'
 ORDER BY idx_scan DESC, indexrelname;

\echo '=== Captured simulator query fingerprints ==='
SELECT queryid,
       calls,
       round(total_exec_time::numeric, 2) AS total_exec_ms,
       round(mean_exec_time::numeric, 2) AS mean_exec_ms,
       rows,
       left(regexp_replace(query, '\s+', ' ', 'g'), 180) AS normalized_query
  FROM pg_stat_statements
 WHERE query LIKE '%aurora_sim_%'
 ORDER BY total_exec_time DESC
 LIMIT 30;

\echo '=== Suggested tuning snapshot queries ==='
\echo 'Run EXPLAIN (ANALYZE, BUFFERS, WAL) on a captured fingerprint only after reviewing its cost.'
\echo 'Compare n_tup_hot_upd with n_tup_upd, dead tuples with autovacuum counts, and idx_scan by index.'

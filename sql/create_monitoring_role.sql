-- Least-privilege monitoring role for postgres_exporter against Aurora PostgreSQL 17+
-- (also compatible with self-managed PostgreSQL 13-18, the versions CI-tested by
-- prometheus-community/postgres_exporter: https://github.com/prometheus-community/postgres_exporter).
--
-- Run this once per cluster (on the writer instance; Aurora replicates DDL/role changes
-- automatically to all readers). Connect as an account with CREATEROLE / rds_superuser
-- (on Aurora, the master user provisioned at cluster creation has the privileges needed).
--
--   psql "host=<writer-endpoint> dbname=postgres" -f create_monitoring_role.sql
--
-- SECURITY NOTES
--   * This script intentionally does NOT set a password. Create the login secret out of band
--     (AWS Secrets Manager, IAM database authentication, or `\password` interactively) and never
--     commit it. See docs/security.md.
--   * pg_monitor is a built-in PostgreSQL role (since 10) that bundles pg_read_all_settings,
--     pg_read_all_stats, and pg_stat_scan_tables: https://www.postgresql.org/docs/current/predefined-roles.html
--   * This role can read cluster-wide statistics views but cannot read table data, and cannot
--     write, alter schema, or change roles/permissions.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'pg_exporter') THEN
        CREATE ROLE pg_exporter WITH LOGIN;
    END IF;
END
$$;

-- Do not let this role create objects in the public schema, own tables, or bypass RLS.
ALTER ROLE pg_exporter SET log_min_duration_statement = -1;
ALTER ROLE pg_exporter SET lock_timeout = '2s';
ALTER ROLE pg_exporter SET statement_timeout = '30s';

-- pg_monitor grants read access to pg_stat_*, pg_settings, and other monitoring views/functions
-- without granting access to user table contents.
GRANT pg_monitor TO pg_exporter;

-- Needed so the exporter's connection/auth check and per-database collectors
-- (pg_stat_database, pg_database_wraparound, pg_locks, etc.) can enumerate/connect to each
-- database it is configured to scrape. Grant CONNECT explicitly per database if you scrape more
-- than the default database; CONNECT does not grant access to table contents.
GRANT CONNECT ON DATABASE postgres TO pg_exporter;
-- Repeat for each additional application database, e.g.:
-- GRANT CONNECT ON DATABASE app_db TO pg_exporter;

-- Optional: required ONLY if you enable the pg_stat_statements collector
-- (--collector.stat_statements, disabled by default -- see docs/metrics-reference.md).
-- The extension must first be created by a superuser/rds_superuser and added to
-- shared_preload_libraries (on Aurora: the DB cluster parameter group), which requires a reboot.
--   CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
-- pg_monitor already grants SELECT on pg_stat_statements via pg_read_all_stats since PG 15;
-- on PostgreSQL 13/14 grant explicitly instead:
-- GRANT SELECT ON pg_stat_statements TO pg_exporter;

COMMENT ON ROLE pg_exporter IS 'Least-privilege read-only role for postgres_exporter (pg_monitor + CONNECT only). Managed in sql/create_monitoring_role.sql.';

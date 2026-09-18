#!/usr/bin/env python3
"""Explicitly non-production Aurora PostgreSQL tuning workload simulator."""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

SCHEMA = "aurora_tuning_simulator"
SCHEMA_MARKER = "aurora_tuning_simulator_owned:v1"
SYSTEM_DATABASES = {"postgres", "template0", "template1", "rdsadmin"}
GIB = 1024 ** 3
HARD_CEILING_GIB = 100
MAX_TARGET_GIB = 98
WORKLOAD_STOP_BYTES = 99 * GIB
ESTIMATED_BYTES_PER_GROW_ROW = 12 * 1024
MAX_SAFE_GROW_BATCH = 5000
MAX_WORKERS = 32
APP_PREFIX_RE = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")
ROOT = Path(__file__).resolve().parent


class SimulatorError(RuntimeError):
    """An actionable simulator safety or execution failure."""


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    user: str
    database: str
    pgpassfile: Path
    target_gib: float
    workers: int
    statement_timeout_ms: int
    application_prefix: str
    batch_size: int
    post_growth_seconds: int
    psql: str

    @property
    def target_bytes(self) -> int:
        return int(self.target_gib * GIB)


class Psql:
    def __init__(self, config: Config) -> None:
        self.config = config

    def _environment(self, app_suffix: str) -> Dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "PGHOST": self.config.host,
                "PGPORT": str(self.config.port),
                "PGUSER": self.config.user,
                "PGDATABASE": self.config.database,
                "PGPASSFILE": str(self.config.pgpassfile),
                "PGAPPNAME": "{}-{}".format(self.config.application_prefix, app_suffix),
            }
        )
        env.pop("PGPASSWORD", None)
        return env

    def run(
        self,
        sql: str,
        app_suffix: str,
        *,
        timeout_seconds: Optional[float] = None,
        allow_statement_timeout: bool = False,
    ) -> str:
        command = [
            self.config.psql,
            "-X",
            "--no-psqlrc",
            "--set",
            "ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            "--command",
            sql,
        ]
        timeout = timeout_seconds or max(15, self.config.statement_timeout_ms / 1000 + 10)
        try:
            result = subprocess.run(
                command,
                env=self._environment(app_suffix),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SimulatorError(
                "psql was not found; install the PostgreSQL client or pass --psql"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SimulatorError("psql exceeded its client-side safety timeout") from exc

        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            timed_out = "statement timeout" in detail.lower()
            if allow_statement_timeout and timed_out:
                return ""
            raise SimulatorError("psql failed: {}".format(detail or "unknown error"))
        return result.stdout.strip()

    def run_file(self, path: Path, app_suffix: str) -> None:
        command = [
            self.config.psql,
            "-X",
            "--no-psqlrc",
            "--set",
            "ON_ERROR_STOP=1",
            "--file",
            str(path),
        ]
        try:
            result = subprocess.run(
                command,
                env=self._environment(app_suffix),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SimulatorError(
                "psql was not found; install the PostgreSQL client or pass --psql"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SimulatorError("{} exceeded its bootstrap timeout".format(path.name)) from exc
        if result.returncode != 0:
            raise SimulatorError(
                "psql failed while reading {}: {}".format(
                    path.name, (result.stderr or result.stdout).strip()
                )
            )

    def stream_file(self, path: Path, app_suffix: str) -> int:
        command = [
            self.config.psql,
            "-X",
            "--no-psqlrc",
            "--set",
            "ON_ERROR_STOP=1",
            "--file",
            str(path),
        ]
        try:
            return subprocess.call(command, env=self._environment(app_suffix), timeout=120)
        except FileNotFoundError as exc:
            raise SimulatorError(
                "psql was not found; install the PostgreSQL client or pass --psql"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SimulatorError("{} exceeded its report timeout".format(path.name)) from exc


def parse_args(argv: Optional[Sequence[str]] = None) -> Config:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--user", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument(
        "--pgpassfile",
        required=True,
        type=Path,
        help="libpq password file; passwords are never accepted on the command line",
    )
    parser.add_argument("--target-gib", type=float, default=10.0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--statement-timeout-ms", type=int, default=30000)
    parser.add_argument("--application-name-prefix", default="aurora-tuning-sim")
    parser.add_argument("--batch-size", type=int, default=10000)
    parser.add_argument("--post-growth-seconds", type=int, default=120)
    parser.add_argument("--psql", default="psql")
    parser.add_argument(
        "--confirm-non-production",
        metavar="DATABASE",
        required=True,
        help="must exactly match --database, acknowledging this target is non-production",
    )
    args = parser.parse_args(argv)

    if args.confirm_non_production != args.database:
        parser.error("--confirm-non-production must exactly match --database")
    if args.database.lower() in SYSTEM_DATABASES:
        parser.error("refusing PostgreSQL/Aurora system database {!r}".format(args.database))
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not 0 < args.target_gib <= MAX_TARGET_GIB:
        parser.error("--target-gib must be greater than 0 and at most {}".format(MAX_TARGET_GIB))
    if not 1 <= args.workers <= MAX_WORKERS:
        parser.error("--workers must be between 1 and {}".format(MAX_WORKERS))
    if not 1000 <= args.statement_timeout_ms <= 300000:
        parser.error("--statement-timeout-ms must be between 1000 and 300000")
    if not APP_PREFIX_RE.fullmatch(args.application_name_prefix):
        parser.error("--application-name-prefix must be 1-48 safe identifier characters")
    if not 100 <= args.batch_size <= 100000:
        parser.error("--batch-size must be between 100 and 100000")
    if not 0 <= args.post_growth_seconds <= 3600:
        parser.error("--post-growth-seconds must be between 0 and 3600")
    if not args.pgpassfile.is_file():
        parser.error("--pgpassfile must name an existing regular file")
    if os.name != "nt" and args.pgpassfile.stat().st_mode & 0o077:
        parser.error("--pgpassfile permissions are too broad; run chmod 600")

    return Config(
        host=args.host,
        port=args.port,
        user=args.user,
        database=args.database,
        pgpassfile=args.pgpassfile.resolve(),
        target_gib=args.target_gib,
        workers=args.workers,
        statement_timeout_ms=args.statement_timeout_ms,
        application_prefix=args.application_name_prefix,
        batch_size=args.batch_size,
        post_growth_seconds=args.post_growth_seconds,
        psql=args.psql,
    )


def preflight(client: Psql) -> None:
    sql = """
SELECT json_build_object(
  'database', current_database(),
  'in_recovery', pg_is_in_recovery(),
  'transaction_read_only', current_setting('transaction_read_only'),
  'default_transaction_read_only', current_setting('default_transaction_read_only'),
  'can_create', has_database_privilege(current_user, current_database(), 'CREATE'),
  'pgss_installed', EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'),
  'pgss_preloaded', 'pg_stat_statements' = ANY (
      string_to_array(replace(current_setting('shared_preload_libraries'), ' ', ''), ',')
  ),
  'schema_exists', EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'aurora_tuning_simulator'),
  'schema_marker', (
      SELECT obj_description(oid, 'pg_namespace')
        FROM pg_namespace WHERE nspname = 'aurora_tuning_simulator'
  )
)::text;
"""
    try:
        facts = json.loads(client.run(sql, "preflight"))
    except json.JSONDecodeError as exc:
        raise SimulatorError("preflight returned malformed data") from exc
    if facts["database"] != client.config.database:
        raise SimulatorError("connected database does not match the acknowledged database")
    if facts["in_recovery"] or facts["transaction_read_only"] == "on":
        raise SimulatorError("refusing a read replica or read-only transaction target")
    if facts["default_transaction_read_only"] == "on":
        raise SimulatorError("refusing a database with default_transaction_read_only enabled")
    if not facts["can_create"]:
        raise SimulatorError("the connecting role needs CREATE on the dedicated database")
    if not facts["pgss_installed"] or not facts["pgss_preloaded"]:
        raise SimulatorError(
            "pg_stat_statements must be preloaded and CREATE EXTENSION pg_stat_statements "
            "must already have been run"
        )
    if facts["schema_exists"] and facts["schema_marker"] != SCHEMA_MARKER:
        raise SimulatorError(
            "refusing existing schema {} because it is not simulator-owned".format(SCHEMA)
        )


def settings_sql(config: Config, tag: str) -> str:
    return (
        "SET statement_timeout = '{}ms'; "
        "SET lock_timeout = '2s'; "
        "/* {} */ "
    ).format(config.statement_timeout_ms, tag)


def simulator_size(client: Psql) -> int:
    sql = """
SELECT COALESCE(sum(pg_total_relation_size(c.oid)), 0)::bigint
  FROM pg_class AS c
  JOIN pg_namespace AS n ON n.oid = c.relnamespace
 WHERE n.nspname = 'aurora_tuning_simulator'
   AND c.relkind IN ('r', 'm');
"""
    return int(client.run(sql, "progress") or "0")


def safe_growth_batch_size(config: Config, current_size: int) -> int:
    remaining = min(config.target_bytes, WORKLOAD_STOP_BYTES) - current_size
    if remaining <= 0:
        return 0
    budget_rows = max(1, remaining // ESTIMATED_BYTES_PER_GROW_ROW)
    return min(config.batch_size, MAX_SAFE_GROW_BATCH, budget_rows)


def grow_batch(client: Psql) -> bool:
    current_size = simulator_size(client)
    batch = safe_growth_batch_size(client.config, current_size)
    if batch <= 0:
        return False
    sql = settings_sql(client.config, "aurora_sim_growth") + """
WITH new_customers AS (
    INSERT INTO aurora_tuning_simulator.customers
        (tenant_id, status, indexed_score, profile, payload)
    SELECT 1 + (g %% 2000),
           CASE WHEN g %% 20 = 0 THEN 'trial' ELSE 'active' END,
           (random() * 100000)::integer,
           jsonb_build_object('segment', g %% 12, 'source', 'simulator'),
           left(repeat(md5((g::text || random()::text)), 32), 1024)
      FROM generate_series(1, %d) AS g
    RETURNING customer_id, tenant_id
), inserted_events AS (
    INSERT INTO aurora_tuning_simulator.events
        (customer_id, tenant_id, event_type, amount, searchable_token, payload, created_at)
    SELECT c.customer_id,
           c.tenant_id,
           (ARRAY['view', 'checkout', 'purchase', 'refund'])[1 + (e.n %% 4)],
           round((random() * 500)::numeric, 2),
           md5((c.customer_id::text || e.n::text)),
           left(repeat(md5((c.customer_id::text || e.n::text || random()::text)), 128), 4096),
           clock_timestamp() - ((random() * 30)::text || ' days')::interval
      FROM new_customers AS c
      CROSS JOIN generate_series(1, 2) AS e(n)
    RETURNING 1
)
UPDATE aurora_tuning_simulator.simulator_state
   SET target_bytes = %d,
       batches_completed = batches_completed + 1,
       rows_inserted = rows_inserted + %d + (SELECT count(*) FROM inserted_events),
       last_application_prefix = '%s',
       updated_at = clock_timestamp()
 WHERE singleton;
""" % (
        batch,
        client.config.target_bytes,
        batch,
        client.config.application_prefix.replace("'", "''"),
    )
    client.run(sql, "grow")
    return True


def workload_statements(config: Config) -> List[tuple]:
    prefix = lambda tag: settings_sql(config, tag)
    return [
        (
            25,
            "insert",
            prefix("aurora_sim_insert")
            + """
INSERT INTO aurora_tuning_simulator.events
    (customer_id, tenant_id, event_type, amount, searchable_token, payload)
SELECT customer_id, tenant_id, 'purchase', round((random() * 500)::numeric, 2),
       md5(random()::text), left(repeat(md5(random()::text), 16), 512)
  FROM aurora_tuning_simulator.customers TABLESAMPLE SYSTEM (0.2)
 LIMIT 100;
""",
            False,
        ),
        (
            22,
            "hot-update",
            prefix("aurora_sim_hot_update")
            + """
UPDATE aurora_tuning_simulator.customers
   SET mutable_counter = mutable_counter + 1, updated_at = clock_timestamp()
 WHERE customer_id IN (
     SELECT customer_id FROM aurora_tuning_simulator.customers
      TABLESAMPLE SYSTEM (0.1) LIMIT 200
 );
""",
            False,
        ),
        (
            13,
            "non-hot-update",
            prefix("aurora_sim_non_hot_update")
            + """
UPDATE aurora_tuning_simulator.customers
   SET indexed_score = (indexed_score + 7919) % 100000
 WHERE customer_id IN (
     SELECT customer_id FROM aurora_tuning_simulator.customers
      TABLESAMPLE SYSTEM (0.05) LIMIT 100
 );
""",
            False,
        ),
        (
            10,
            "delete",
            prefix("aurora_sim_delete")
            + """
DELETE FROM aurora_tuning_simulator.events
 WHERE event_id IN (
     SELECT event_id FROM aurora_tuning_simulator.events
      WHERE created_at < clock_timestamp() - interval '7 days'
      ORDER BY event_id LIMIT 100
 );
""",
            False,
        ),
        (
            12,
            "index-hit",
            prefix("aurora_sim_index_hit")
            + """
SELECT count(*), sum(amount)
  FROM aurora_tuning_simulator.events
 WHERE tenant_id = 1 + (random() * 1999)::integer
   AND event_type = 'purchase';
""",
            False,
        ),
        (
            8,
            "index-miss",
            prefix("aurora_sim_index_miss")
            + """
SELECT count(*)
  FROM aurora_tuning_simulator.events
 WHERE lower(searchable_token) LIKE '00%';
""",
            False,
        ),
        (
            6,
            "bounded-lock",
            prefix("aurora_sim_bounded_lock")
            + "SELECT aurora_tuning_simulator.try_bounded_lock(1);",
            False,
        ),
        (
            4,
            "analytics",
            prefix("aurora_sim_analytics")
            + """
SELECT e.tenant_id, date_trunc('day', e.created_at) AS day,
       count(*) AS event_count, sum(e.amount) AS gross_amount
  FROM aurora_tuning_simulator.events AS e
  JOIN aurora_tuning_simulator.customers AS c USING (customer_id)
 WHERE e.created_at >= clock_timestamp() - interval '30 days'
 GROUP BY e.tenant_id, date_trunc('day', e.created_at)
 ORDER BY gross_amount DESC
 LIMIT 100;
""",
            True,
        ),
    ]


def lock_holder(client: Psql, stop: threading.Event) -> None:
    sql = settings_sql(client.config, "aurora_sim_lock_holder") + """
BEGIN;
UPDATE aurora_tuning_simulator.lock_targets
   SET touches = touches + 1, updated_at = clock_timestamp()
 WHERE lock_id = 1;
SELECT pg_sleep(1.25);
COMMIT;
"""
    while not stop.is_set():
        try:
            client.run(sql, "lock-holder")
        except SimulatorError as exc:
            print("lock holder warning: {}".format(exc), file=sys.stderr)
            stop.wait(2)
        stop.wait(0.25)


def insert_work_allowed(client: Psql) -> bool:
    return simulator_size(client) < WORKLOAD_STOP_BYTES


def churn_worker(client: Psql, worker_id: int, stop: threading.Event) -> None:
    rng = random.Random(os.urandom(16))
    statements = workload_statements(client.config)
    population = [item for item in statements for _ in range(item[0])]
    while not stop.is_set():
        _, name, sql, allow_timeout = rng.choice(population)
        try:
            if name == "insert" and not insert_work_allowed(client):
                stop.wait(1)
                continue
            client.run(
                sql,
                "worker-{}".format(worker_id),
                allow_statement_timeout=allow_timeout,
            )
        except SimulatorError as exc:
            print("worker {} {} warning: {}".format(worker_id, name, exc), file=sys.stderr)
            stop.wait(1)
        stop.wait(rng.uniform(0.02, 0.2))


def verify_fingerprints(client: Psql) -> None:
    sql = """
SELECT json_build_object(
  'total', count(*),
  'growth', count(*) FILTER (WHERE query LIKE '%aurora_sim_growth%'),
  'hot_update', count(*) FILTER (WHERE query LIKE '%aurora_sim_hot_update%'),
  'analytics', count(*) FILTER (WHERE query LIKE '%aurora_sim_analytics%')
)::text
FROM pg_stat_statements
WHERE query LIKE '%aurora_sim_%';
"""
    counts = json.loads(client.run(sql, "verify-pgss"))
    missing = [name for name in ("growth", "hot_update", "analytics") if counts[name] < 1]
    if counts["total"] < 1 or missing:
        raise SimulatorError(
            "pg_stat_statements did not capture expected fingerprints: {}".format(
                ", ".join(missing) or "none"
            )
        )
    print(
        "Verified {} pg_stat_statements fingerprints (required categories present).".format(
            counts["total"]
        )
    )


def seed_required_fingerprints(client: Psql) -> None:
    statements = [
        settings_sql(client.config, "aurora_sim_growth")
        + """
UPDATE aurora_tuning_simulator.simulator_state
   SET updated_at = clock_timestamp()
 WHERE singleton;
""",
        settings_sql(client.config, "aurora_sim_hot_update")
        + """
UPDATE aurora_tuning_simulator.customers
   SET mutable_counter = mutable_counter + 1,
       updated_at = clock_timestamp()
 WHERE customer_id = (
     SELECT min(customer_id) FROM aurora_tuning_simulator.customers
 );
""",
        settings_sql(client.config, "aurora_sim_analytics")
        + """
SELECT count(*), sum(amount)
  FROM aurora_tuning_simulator.events
 WHERE event_id <= (
     SELECT COALESCE(min(event_id), 0) + 1000
       FROM aurora_tuning_simulator.events
 );
""",
    ]
    for sql in statements:
        client.run(sql, "fingerprint-check")


def print_report(client: Psql) -> None:
    print("\nTuning report:")
    if client.stream_file(ROOT / "report.sql", "report") != 0:
        raise SimulatorError("final report SQL failed")


def run(config: Config) -> int:
    client = Psql(config)
    stop = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        print("\nSignal {} received; finishing the current bounded statement.".format(signum))
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    print("Running safety preflight against database {!r}...".format(config.database))
    preflight(client)
    client.run_file(ROOT / "bootstrap.sql", "bootstrap")
    initial_size = simulator_size(client)
    if initial_size >= HARD_CEILING_GIB * GIB:
        raise SimulatorError(
            "existing simulator schema has reached the {} GiB hard ceiling".format(
                HARD_CEILING_GIB
            )
        )
    if initial_size >= WORKLOAD_STOP_BYTES:
        raise SimulatorError(
            "existing simulator schema is inside the 1 GiB hard-ceiling guard margin"
        )
    print(
        "Simulator schema is {:.2f} GiB; target is {:.2f} GiB.".format(
            initial_size / GIB, config.target_gib
        )
    )

    threads: List[threading.Thread] = []
    for worker_id in range(config.workers):
        thread = threading.Thread(
            target=churn_worker,
            args=(client, worker_id + 1, stop),
            name="churn-{}".format(worker_id + 1),
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    if config.workers >= 2:
        holder = threading.Thread(
            target=lock_holder, args=(client, stop), name="lock-holder", daemon=True
        )
        holder.start()
        threads.append(holder)

    last_report = 0.0
    try:
        current_size = initial_size
        while current_size < config.target_bytes and not stop.is_set():
            if current_size >= WORKLOAD_STOP_BYTES:
                print("Safety guard reached; stopping all workload activity.", file=sys.stderr)
                stop.set()
                break
            if not grow_batch(client):
                stop.set()
                break
            current_size = simulator_size(client)
            now = time.monotonic()
            if now - last_report >= 10:
                percent = min(100.0, current_size * 100 / config.target_bytes)
                print(
                    "Progress: {:.2f} / {:.2f} GiB ({:.1f}%).".format(
                        current_size / GIB, config.target_gib, percent
                    ),
                    flush=True,
                )
                last_report = now

        if not stop.is_set() and config.post_growth_seconds:
            print(
                "Target reached; continuing churn for {} seconds.".format(
                    config.post_growth_seconds
                )
            )
            deadline = time.monotonic() + config.post_growth_seconds
            while not stop.is_set() and time.monotonic() < deadline:
                stop.wait(min(5, max(0, deadline - time.monotonic())))
                current_size = simulator_size(client)
                if current_size >= WORKLOAD_STOP_BYTES:
                    print(
                        "Safety guard reached during post-growth churn; stopping all activity.",
                        file=sys.stderr,
                    )
                    stop.set()
                    break
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=config.statement_timeout_ms / 1000 + 12)

    final_size = simulator_size(client)
    if final_size >= HARD_CEILING_GIB * GIB:
        raise SimulatorError("simulator reached the physical hard ceiling; workload is stopped")
    if final_size >= WORKLOAD_STOP_BYTES:
        print("Safety guard reached; no further workload statements will run.", file=sys.stderr)
        print_report(client)
        return 2
    if stop.is_set() and final_size < config.target_bytes:
        print("Stopped before target; rerun the same command to resume safely.")
        print_report(client)
        return 130

    seed_required_fingerprints(client)
    verify_fingerprints(client)
    print_report(client)
    print("\nCleanup was not run. See docs/tuning-workload.md for explicit cleanup SQL.")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return run(parse_args(argv))
    except SimulatorError as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workload.aurora_tuning_simulator import (
    GIB,
    HARD_CEILING_GIB,
    MAX_SAFE_GROW_BATCH,
    MAX_TARGET_GIB,
    WORKLOAD_STOP_BYTES,
    Config,
    Psql,
    SimulatorError,
    grow_batch,
    insert_work_allowed,
    parse_args,
    preflight,
    safe_growth_batch_size,
)


def base_args(pgpassfile: Path):
    return [
        "--host",
        "writer.validation.invalid",
        "--user",
        "simulator",
        "--database",
        "aurora_validation",
        "--pgpassfile",
        str(pgpassfile),
        "--confirm-non-production",
        "aurora_validation",
    ]


class ArgumentSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.pgpass = Path(self.temp.name) / "simulator.pgpass"
        self.pgpass.write_text("*:*:*:*:not-a-real-password\n", encoding="utf-8")
        if os.name != "nt":
            self.pgpass.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def tearDown(self):
        self.temp.cleanup()

    def test_defaults_and_exact_confirmation(self):
        config = parse_args(base_args(self.pgpass))
        self.assertEqual(config.target_bytes, 10 * GIB)
        self.assertEqual(config.workers, 4)
        self.assertEqual(config.statement_timeout_ms, 30000)

    def test_confirmation_must_match(self):
        args = base_args(self.pgpass)
        args[-1] = "different_database"
        with self.assertRaises(SystemExit):
            parse_args(args)

    def test_refuses_system_database(self):
        args = base_args(self.pgpass)
        args[args.index("--database") + 1] = "postgres"
        args[-1] = "postgres"
        with self.assertRaises(SystemExit):
            parse_args(args)

    def test_enforces_hard_size_ceiling(self):
        with self.assertRaises(SystemExit):
            parse_args(base_args(self.pgpass) + ["--target-gib", str(MAX_TARGET_GIB + 0.01)])

    def test_rejects_invalid_port(self):
        with self.assertRaises(SystemExit):
            parse_args(base_args(self.pgpass) + ["--port", "70000"])


class PreflightTests(unittest.TestCase):
    def config(self):
        return Config(
            host="writer.validation.invalid",
            port=5432,
            user="simulator",
            database="aurora_validation",
            pgpassfile=Path("unused"),
            target_gib=1,
            workers=1,
            statement_timeout_ms=30000,
            application_prefix="test",
            batch_size=1000,
            post_growth_seconds=0,
            psql="psql",
        )

    def facts(self, **overrides):
        values = {
            "database": "aurora_validation",
            "in_recovery": False,
            "transaction_read_only": "off",
            "default_transaction_read_only": "off",
            "can_create": True,
            "pgss_installed": True,
            "pgss_preloaded": True,
            "schema_exists": False,
            "schema_marker": None,
        }
        values.update(overrides)
        return values

    def test_accepts_writable_owned_target(self):
        client = mock.Mock()
        client.config = self.config()
        client.run.return_value = json.dumps(self.facts())
        preflight(client)

    def test_refuses_replica(self):
        client = mock.Mock()
        client.config = self.config()
        client.run.return_value = json.dumps(self.facts(in_recovery=True))
        with self.assertRaisesRegex(SimulatorError, "read replica"):
            preflight(client)

    def test_refuses_unowned_existing_schema(self):
        client = mock.Mock()
        client.config = self.config()
        client.run.return_value = json.dumps(
            self.facts(schema_exists=True, schema_marker="someone else's schema")
        )
        with self.assertRaisesRegex(SimulatorError, "not simulator-owned"):
            preflight(client)

    @mock.patch("workload.aurora_tuning_simulator.subprocess.run")
    def test_psql_uses_environment_and_never_password_argument(self, run_mock):
        run_mock.return_value = mock.Mock(returncode=0, stdout="ok\n", stderr="")
        config = self.config()
        client = Psql(config)
        self.assertEqual(client.run("SELECT 1", "test"), "ok")
        command = run_mock.call_args.args[0]
        environment = run_mock.call_args.kwargs["env"]
        self.assertNotIn("password", " ".join(command).lower())
        self.assertEqual(environment["PGPASSFILE"], str(config.pgpassfile))
        self.assertNotIn("PGPASSWORD", environment)

    def test_growth_sql_has_valid_modulo_operators(self):
        client = mock.Mock()
        client.config = self.config()
        client.run.side_effect = [str(100 * 1024 * 1024), ""]
        grow_batch(client)
        sql = client.run.call_args.args[0]
        self.assertIn("g % 2000", sql)
        self.assertNotIn("%%", sql)

    def test_growth_batch_is_clamped_and_stops_at_guard(self):
        config = self.config()
        self.assertEqual(
            safe_growth_batch_size(config, 0),
            min(config.batch_size, MAX_SAFE_GROW_BATCH),
        )
        self.assertEqual(safe_growth_batch_size(config, WORKLOAD_STOP_BYTES), 0)

    def test_grow_batch_rechecks_size_before_insert(self):
        client = mock.Mock()
        client.config = self.config()
        client.run.return_value = str(WORKLOAD_STOP_BYTES)
        self.assertFalse(grow_batch(client))
        self.assertEqual(client.run.call_count, 1)

    def test_target_has_margin_below_physical_ceiling(self):
        self.assertLess(MAX_TARGET_GIB, HARD_CEILING_GIB)
        self.assertLess(MAX_TARGET_GIB * GIB, WORKLOAD_STOP_BYTES)

    def test_insert_workers_stop_at_guard(self):
        client = mock.Mock()
        client.run.return_value = str(WORKLOAD_STOP_BYTES)
        self.assertFalse(insert_work_allowed(client))


if __name__ == "__main__":
    unittest.main()

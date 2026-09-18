from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.setup_observability import (
    ARCHIVES,
    MANAGED_START,
    Runner,
    SetupError,
    aws_base_environment,
    bind_dashboard_datasource,
    desired_file_metadata,
    has_metric_sample,
    merge_prometheus_config,
    merge_rule_files,
    normalize,
    pgpass_line,
    render_yace_config,
    update_prometheus,
    validate_config,
    verify_aws_credentials,
)


def config():
    return {
        "aurora_targets": [
            {
                "name": "Orders Writer",
                "endpoint": "writer.invalid",
                "port": 5432,
                "database": "orders",
                "db_role": "postgres_exporter",
                "password_file": "/not-read-in-structural-test",
                "cluster": "Orders PROD",
                "instance": "Orders Writer 1",
                "role": "writer",
                "exporter_port": 9187,
                "sslrootcert": "/not-read-in-structural-test",
            }
        ],
        "exporter_advertise_host": "exporters.invalid",
        "prometheus": {
            "url": "http://prometheus.invalid:9090",
            "config_path": "/etc/prometheus/prometheus.yml",
            "service_name": "prometheus",
            "promtool": "/usr/local/bin/promtool",
        },
        "grafana": {
            "url": "https://grafana.invalid",
            "folder": "Aurora PostgreSQL",
            "api_token_file": "/not-read-in-structural-test",
        },
        "aws": {
            "region": "us-east-1",
            "base_credential_provider": "instance-role",
            "assume_role_arn": None,
            "search_tag_key": "aurora-observability",
            "search_tag_value": "true",
        },
        "yace_advertise_host": "exporters.invalid",
        "yace_port": 5000,
    }


class SetupTests(unittest.TestCase):
    def test_normalizes_shared_labels(self):
        self.assertEqual(normalize("Orders PROD_writer"), "orders-prod-writer")

    def test_rejects_duplicate_ports(self):
        value = config()
        value["aurora_targets"].append(dict(value["aurora_targets"][0], name="reader"))
        with self.assertRaisesRegex(SetupError, "exporter_port"):
            validate_config(value, check_files=False)

    def test_requires_base_provider_even_with_assume_role(self):
        value = config()
        value["aws"]["assume_role_arn"] = "arn:aws:iam::123456789012:role/yace"
        value["aws"]["base_credential_provider"] = ""
        with self.assertRaisesRegex(SetupError, "base_credential_provider"):
            validate_config(value, check_files=False)

    def test_merge_preserves_existing_jobs_and_is_idempotent(self):
        original = """global:
  scrape_interval: 15s
scrape_configs:
  - job_name: existing
    static_configs: [{ targets: ["localhost:9090"] }]
remote_write:
  - url: https://remote.invalid/write
"""
        block = "  {}\n  - job_name: aurora\n  # END aurora-postgresql-observability (managed)\n".format(
            MANAGED_START
        )
        once = merge_prometheus_config(original, block)
        twice = merge_prometheus_config(once, block)
        self.assertEqual(once, twice)
        self.assertIn("job_name: existing", once)
        self.assertIn("remote_write:", once)
        self.assertLess(once.index("job_name: aurora"), once.index("remote_write:"))

    def test_rule_merge_preserves_existing_rules_and_is_idempotent(self):
        original = """rule_files:
  - /etc/prometheus/existing.yml
scrape_configs: []
"""
        once = merge_rule_files(original)
        self.assertEqual(once, merge_rule_files(once))
        self.assertIn("/etc/prometheus/existing.yml", once)
        self.assertIn("aurora-observability/alerts.yml", once)
        self.assertLess(once.index("aurora-observability"), once.index("scrape_configs:"))

    def test_merges_inline_empty_lists_without_duplicate_keys(self):
        scrape = merge_prometheus_config("global: {}\nscrape_configs: []\n", "  # block\n")
        rules = merge_rule_files("rule_files: []\nscrape_configs: []\n")
        self.assertEqual(scrape.count("scrape_configs:"), 1)
        self.assertEqual(rules.count("rule_files:"), 1)

    def test_managed_jobs_preserve_rule_compatible_job_labels(self):
        from scripts.setup_observability import managed_scrape_block

        block = managed_scrape_block(config())
        self.assertIn('replacement: "postgres_exporter"', block)
        self.assertIn('replacement: "yace"', block)

    def test_refuses_inline_nonempty_values(self):
        with self.assertRaisesRegex(SetupError, "inline non-empty"):
            merge_prometheus_config("scrape_configs: [{ job_name: existing }]\n", "  # block\n")

    def test_yace_renders_region_tags_and_assume_role_for_both_jobs(self):
        value = config()
        value["aws"].update(
            {
                "region": "eu-west-1",
                "search_tag_key": "monitoring",
                "search_tag_value": "enabled",
                "assume_role_arn": "arn:aws:iam::123456789012:role/yace",
            }
        )
        rendered = render_yace_config(value)
        self.assertEqual(rendered.count("roleArn:"), 2)
        self.assertEqual(rendered.count("- eu-west-1"), 2)
        self.assertEqual(rendered.count('- key: "monitoring"'), 2)

    def test_release_checksums_are_embedded_for_both_linux_architectures(self):
        for product in ("postgres_exporter", "yace"):
            for architecture in ("x86_64", "aarch64"):
                archive, digest = ARCHIVES[(product, architecture)]
                self.assertTrue(archive.endswith(".tar.gz"))
                self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_temporary_pgpass_escaping(self):
        target = config()["aurora_targets"][0]
        self.assertTrue(pgpass_line(target, r"p:a\\ss").endswith(r"p\:a\\\\ss" + "\n"))
        with self.assertRaisesRegex(SetupError, "one line"):
            pgpass_line(target, "first\nsecond")

    def test_prometheus_candidate_preserves_source_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "prometheus.yml"
            source.write_text("scrape_configs: []\n", encoding="utf-8")
            source.chmod(0o640)
            uid, gid, mode = desired_file_metadata(source)
            metadata = source.stat()
            self.assertEqual((uid, gid), (metadata.st_uid, metadata.st_gid))
            self.assertEqual(mode, stat.S_IMODE(metadata.st_mode))
            self.assertEqual(desired_file_metadata(None)[2], 0o640)

    @mock.patch("scripts.setup_observability.subprocess.run")
    def test_prometheus_atomic_replace_keeps_existing_mode(self, run_mock):
        run_mock.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prometheus.yml"
            path.write_text("rule_files: []\nscrape_configs: []\n", encoding="utf-8")
            path.chmod(0o640)
            before = stat.S_IMODE(path.stat().st_mode)
            value = config()
            value["prometheus"]["config_path"] = str(path)
            value["prometheus"]["promtool"] = "promtool"
            runner = Runner(apply=True)
            with mock.patch.object(runner, "command"):
                update_prometheus(value, runner)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), before)

    def test_dashboard_import_has_concrete_datasource_and_no_inputs(self):
        dashboard = json.loads(
            Path("grafana/dashboards/aurora-postgresql-overview.json").read_text(
                encoding="utf-8"
            )
        )
        rendered = bind_dashboard_datasource(dashboard, "aurora-prometheus")
        serialized = json.dumps(rendered)
        self.assertNotIn("${DS_PROMETHEUS}", serialized)
        self.assertNotIn('"__inputs"', serialized)
        self.assertIn('"uid": "aurora-prometheus"', serialized)

    def test_web_identity_environment_does_not_fall_back_to_ambient_keys(self):
        aws = config()["aws"]
        aws.update(
            {
                "base_credential_provider": "web-identity",
                "web_identity_token_file": "/run/secrets/web-token",
                "web_identity_role_arn": "arn:aws:iam::123456789012:role/base",
            }
        )
        with mock.patch.dict(
            "os.environ",
            {"AWS_ACCESS_KEY_ID": "ambient", "AWS_SECRET_ACCESS_KEY": "ambient"},
            clear=True,
        ):
            environment = aws_base_environment(aws)
        self.assertNotIn("AWS_ACCESS_KEY_ID", environment)
        self.assertEqual(environment["AWS_WEB_IDENTITY_TOKEN_FILE"], "/run/secrets/web-token")

    @mock.patch("scripts.setup_observability.subprocess.run")
    def test_assume_role_is_exercised_with_memory_only_credentials(self, run_mock):
        value = config()
        value["aws"]["assume_role_arn"] = "arn:aws:iam::123456789012:role/target"
        run_mock.side_effect = [
            mock.Mock(returncode=0, stdout="{}", stderr=""),
            mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "AccessKeyId": "temporary-id",
                        "SecretAccessKey": "temporary-secret",
                        "SessionToken": "temporary-token",
                    }
                ),
                stderr="",
            ),
            mock.Mock(returncode=0, stdout="{}", stderr=""),
        ]
        verify_aws_credentials(value, "aws")
        self.assertEqual(run_mock.call_count, 3)
        self.assertIn("assume-role", run_mock.call_args_list[1].args[0])
        final_environment = run_mock.call_args_list[2].kwargs["env"]
        self.assertEqual(final_environment["AWS_ACCESS_KEY_ID"], "temporary-id")
        all_arguments = " ".join(
            argument
            for call in run_mock.call_args_list
            for argument in call.args[0]
        )
        self.assertNotIn("temporary-secret", all_arguments)

    def test_yace_runtime_requires_a_real_metric_sample(self):
        self.assertFalse(has_metric_sample("# HELP aws_rds_cpu metric\n", "aws_rds_"))
        self.assertTrue(has_metric_sample("aws_rds_cpuutilization_average 12\n", "aws_rds_"))


if __name__ == "__main__":
    unittest.main()

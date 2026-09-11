# Contributing

Thanks for your interest in improving this Aurora PostgreSQL observability stack. This project ships
configuration, not a compiled binary, so contributions are mostly YAML/JSON/SQL/shell/docs changes.

## Ground rules

1. **No secrets, no real infrastructure identifiers.** Never commit hostnames, account IDs, ARNs, cluster
   identifiers, usernames, or passwords. Use the `*.invalid` placeholder hostnames and `123456789012`-style
   placeholder account IDs already used throughout the repo. See [SECURITY.md](SECURITY.md).
2. **Pin versions.** Container image tags and downloaded binaries must reference an explicit, currently
   published stable release (no `latest`). Update every reference (compose file, systemd unit comments,
   docs) together when bumping a version.
3. **Cite upstream sources.** When you change exporter flags, metric names, IAM actions, or CloudWatch
   metric names, link the authoritative upstream doc/source file you verified against in the PR
   description.
4. **Every PromQL query must resolve to a metric actually emitted** by a collector/config in this repo.
   If a panel/alert depends on an optional collector (e.g. `pg_stat_statements`, `pg_database_wraparound`,
   YACE cluster-level jobs), mark it clearly as optional in the dashboard panel description and in
   `docs/metrics-reference.md`.
5. **Validate before you push.** Run `python scripts/validate.py` (or `make validate`) locally. CI runs the
   same entrypoint and will fail the PR otherwise.

## Development workflow

```powershell
# Windows / PowerShell
python scripts/validate.py
```

```bash
# Linux / macOS
python3 scripts/validate.py
# or
make validate
```

The validation entrypoint does not require Docker. It downloads `promtool` into a local, gitignored
`tools/` directory on demand to lint Prometheus rules and run `promtool test rules`.

## Making dashboard changes

- Dashboards live in `grafana/dashboards/*.json` and must keep the `${DS_PROMETHEUS}` datasource
  templating variable (never hardcode a datasource UID).
- Add new panels to the row that matches their topic, set `unit`, `description`, and thresholds where
  meaningful, and add a one-line note in `docs/dashboard-guide.md`.
- Run `python scripts/checks/check_dashboard_json.py grafana/dashboards/*.json` to confirm every PromQL
  `expr` references only metrics documented in `docs/metrics-reference.md`.

## Commit / PR expectations

- Keep changes focused; unrelated reformatting makes review harder.
- Describe what you validated (which script/command, and the result).
- Do not add new secrets-scanning suppressions without explaining why in the PR description.

## Reporting issues

Use GitHub Issues for bugs or gaps in coverage (e.g., a CloudWatch metric that isn't wired up, or a
collector flag that changed upstream). See [SECURITY.md](SECURITY.md) for vulnerability reports instead of
filing a public issue.

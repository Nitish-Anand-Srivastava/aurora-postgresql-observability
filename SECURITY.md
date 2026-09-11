# Security Policy

## Scope

This repository contains **configuration only**: Prometheus/Grafana/exporter configuration, systemd
units, SQL, IAM policy documents, dashboards, and validation tooling. It does not ship a running service
or accept network input on its own. Security issues generally fall into one of these categories:

- A committed file that leaks (or could be mistaken for) a real secret, hostname, account ID, or other
  sensitive identifier.
- A recommended configuration that grants more privilege than necessary (SQL role, IAM policy, exporter
  flags) contrary to the least-privilege intent of this project.
- A validation/CI script that could execute untrusted content unsafely.

## Supported versions

Only the `main` branch (latest state) is supported. There are no released versions of this repository
itself; version pins referenced *inside* the configuration (Prometheus, Grafana, postgres_exporter, YACE)
follow the "currently published stable" policy described in the README and are updated on a best-effort
basis.

## Reporting a vulnerability

Please **do not open a public GitHub issue** for security reports. Instead:

1. Use GitHub's private vulnerability reporting for this repository (Security tab → "Report a
   vulnerability"), or
2. Contact the repository maintainers directly through the contact method listed on the GitHub
   organization/user profile that owns this repository.

Include:
- The file(s) and line(s) affected.
- Why the current content is unsafe (e.g., "this looks like a live account ID" or "this IAM policy grants
  `*:*`").
- A suggested remediation if you have one.

We aim to acknowledge reports within 5 business days.

## Handling secrets in this repository

- All example credentials use RFC 2606 / RFC 6761 reserved-for-documentation style placeholders
  (`*.invalid` hostnames, `123456789012` / `111111111111` placeholder account IDs, `changeme`-style
  passwords that are clearly templated).
- Real credentials must **never** be committed. All exporters are configured to read secrets at runtime
  from environment variables or files outside version control (`DATA_SOURCE_PASS_FILE`,
  `AWS_SHARED_CREDENTIALS_FILE`, systemd `EnvironmentFile=`), never from tracked YAML.
- `.gitignore` blocks common secret file patterns. CI runs a secret-pattern scan
  (`scripts/checks/check_secrets.py`) on every push/PR — see `.github/workflows/ci.yml`.
- If you believe a secret was committed in the past, treat it as compromised: rotate it immediately, then
  report it as above so history can be scrubbed if needed.

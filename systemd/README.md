# systemd units

Production Linux deployment for the two exporters as hardened, unprivileged systemd services.

| Unit | Purpose | Config | Env template |
| --- | --- | --- | --- |
| `postgres_exporter.service` | Scrapes Aurora PostgreSQL via the built-in collectors | `../exporters/postgres_exporter/postgres_exporter.yml` (optional) | `../exporters/postgres_exporter/postgres_exporter.env.template` |
| `yace.service` | Scrapes `AWS/RDS` CloudWatch metrics | `../exporters/yace/config.yml` | `../exporters/yace/yace.env.template` |

Both units:

- Run as a dedicated, unprivileged, non-login system user.
- Read secrets only from files/environment referenced by `EnvironmentFile=`, never from the unit
  file itself or from tracked configuration.
- Use `ProtectSystem=strict`, `NoNewPrivileges=true`, `PrivateTmp=true`, and related sandboxing
  directives to limit blast radius if the exporter process is compromised.
- Set `Restart=on-failure` so Prometheus's `up`/`pg_up` alerting (see
  `prometheus/rules/alerts.yml`) reflects genuine outages rather than transient crashes.
- Listen on all interfaces so a separate Prometheus host can scrape them. Restrict TCP/9187 and
  TCP/5000 to Prometheus with host/VPC firewall rules; use TLS/mTLS when crossing an untrusted
  network as documented in `docs/security.md`.

See the comment block at the top of each `.service` file for exact install steps, and
`docs/setup.md` for the end-to-end walkthrough (SQL role, IAM policy, binary verification,
Prometheus scrape wiring).

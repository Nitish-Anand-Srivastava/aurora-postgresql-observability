#!/usr/bin/env python3
"""Validate the postgres_exporter database login and least-privilege grants."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Result, repo_root, tracked_files  # noqa: E402

ROLE = "postgres_exporter"
SQL_PATH = Path("sql/create_monitoring_role.sql")


def main() -> int:
    result = Result("PostgreSQL exporter role")
    root = repo_root()
    sql = (root / SQL_PATH).read_text(encoding="utf-8")

    created_roles = re.findall(
        r"(?im)^\s*CREATE\s+ROLE\s+([a-z_][a-z0-9_]*)\s+WITH\s+LOGIN\s*;",
        sql,
    )
    if created_roles != [ROLE]:
        result.error(f"{SQL_PATH}: expected exactly one CREATE ROLE {ROLE} WITH LOGIN statement")
    for role in created_roles:
        if role.startswith("pg_"):
            result.error(
                f"{SQL_PATH}: role {role!r} uses PostgreSQL's reserved pg_ name prefix"
            )

    required_sql = (
        f"rolname = '{ROLE}'",
        f"ALTER ROLE {ROLE} SET log_min_duration_statement = -1;",
        f"ALTER ROLE {ROLE} SET lock_timeout = '2s';",
        f"ALTER ROLE {ROLE} SET statement_timeout = '30s';",
        f"GRANT pg_monitor TO {ROLE};",
        f"GRANT CONNECT ON DATABASE postgres TO {ROLE};",
        f"COMMENT ON ROLE {ROLE} IS",
    )
    for statement in required_sql:
        if statement not in sql:
            result.error(f"{SQL_PATH}: missing required role configuration: {statement}")

    user_assignment = re.compile(r"(?m)^\s*DATA_SOURCE_USER\s*[:=]\s*[\"']?([^\"'\s]+)")
    fixture_username = re.compile(r'\busename="([^"]+)"')
    for path in sorted(tracked_files(["*.yml", "*.yaml", "*.template"])):
        text = path.read_text(encoding="utf-8")
        configured_roles = user_assignment.findall(text) + fixture_username.findall(text)
        for role in configured_roles:
            if role != ROLE:
                rel = path.relative_to(root).as_posix()
                result.error(f"{rel}: exporter database login must be {ROLE!r}, found {role!r}")

    result.print_report()
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

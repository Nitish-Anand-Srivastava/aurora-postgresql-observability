#!/usr/bin/env python3
"""Structural + metric-provenance validation for grafana/dashboards/*.json.

Validates:
  1. JSON is well-formed (also covered by check_json.py, re-checked here for a clear message).
  2. The dashboard has a datasource TEMPLATE variable (type == "datasource"), never a hardcoded
     datasource UID on panels/targets.
  3. The dashboard has cluster/instance/database template variables and a rate-interval variable.
  4. Every panel has a "description" (docs requirement: every panel explains its purpose/source).
  5. Every PromQL `expr` in every panel target references ONLY:
       - a base metric listed in docs/metrics-reference.md's machine-readable inventory, or
       - a recording rule (name containing ':') defined in prometheus/rules/recording_rules.yml,
       - or the built-in Prometheus `up` metric.
     This enforces "every PromQL metric must be emitted by provided configs/exporters."
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Result, repo_root  # noqa: E402

PROMQL_FUNCS_AND_KEYWORDS = {
    "sum", "avg", "max", "min", "rate", "irate", "increase", "topk", "bottomk", "count",
    "count_values", "by", "on", "ignoring", "group_left", "group_right", "without", "offset",
    "and", "or", "unless", "bool", "clamp_min", "clamp_max", "clamp", "label_replace",
    "label_join", "absent", "absent_over_time", "vector", "scalar", "time", "abs", "ceil",
    "floor", "round", "sort", "sort_desc", "sort_by_label", "changes", "delta", "deriv",
    "predict_linear", "holt_winters", "resets", "quantile_over_time", "avg_over_time",
    "min_over_time", "max_over_time", "sum_over_time", "count_over_time", "stddev_over_time",
    "stdvar_over_time", "last_over_time", "present_over_time", "histogram_quantile",
    "day_of_month", "day_of_week", "days_in_month", "hour", "minute", "month", "year",
    "timestamp", "exp", "ln", "log2", "log10", "sqrt", "sgn", "pi", "irate", "idelta",
}

TOKEN_RE = re.compile(r"(?<![\w:$])([a-zA-Z_:][a-zA-Z0-9_:]*)(?=[\s{}\[\]()+\-*/,]|$)")

# Matches the grouping-label clauses of an aggregation (`by (a, b)`, `without (a)`) and vector
# matching modifiers (`on (a)`, `ignoring (a)`, `group_left(a)`, `group_right(a)`). The contents of
# these parens are label names, not metric names, and must be stripped before metric-name token
# extraction -- otherwise e.g. `sum by (instance) (pg_up)` would spuriously flag "instance" as an
# undocumented metric.
GROUPING_CLAUSE_RE = re.compile(
    r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)", re.IGNORECASE
)


def extract_metric_tokens(expr: str) -> set[str]:
    expr = GROUPING_CLAUSE_RE.sub(" ", expr)
    tokens = set()
    for m in TOKEN_RE.finditer(expr):
        tok = m.group(1)
        if tok in PROMQL_FUNCS_AND_KEYWORDS:
            continue
        tokens.add(tok)
    return tokens


def load_metric_inventory(result: Result) -> set[str]:
    doc = repo_root() / "docs" / "metrics-reference.md"
    if not doc.exists():
        result.error(f"{doc} not found -- cannot validate dashboard metric provenance")
        return set()
    text = doc.read_text(encoding="utf-8")
    m = re.search(
        r"<!-- BEGIN_MACHINE_READABLE_METRIC_INVENTORY -->(.*?)<!-- END_MACHINE_READABLE_METRIC_INVENTORY -->",
        text,
        re.DOTALL,
    )
    if not m:
        result.error(f"{doc}: missing BEGIN/END_MACHINE_READABLE_METRIC_INVENTORY markers")
        return set()
    names = set()
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("```") or line.startswith("#"):
            continue
        names.add(line)
    if not names:
        result.error(f"{doc}: machine-readable metric inventory block is empty")
    return names


def load_recording_rule_names(result: Result) -> set[str]:
    path = repo_root() / "prometheus" / "rules" / "recording_rules.yml"
    if not path.exists():
        result.error(f"{path} not found -- cannot validate recording-rule references")
        return set()
    text = path.read_text(encoding="utf-8")
    return set(re.findall(r"^\s*-\s*record:\s*(\S+)\s*$", text, re.MULTILINE))


def walk_panels(panels):
    for p in panels:
        yield p
        if "panels" in p:
            yield from walk_panels(p["panels"])


def check_dashboard(path: Path, base_metrics: set[str], recording_rules: set[str], result: Result) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        result.error(f"{path}: invalid JSON: {exc}")
        return

    templating = data.get("templating", {}).get("list", [])
    var_names = {v.get("name") for v in templating}
    var_types = {v.get("name"): v.get("type") for v in templating}

    if "datasource" not in [v.get("type") for v in templating]:
        result.error(f"{path}: no template variable of type 'datasource' found (dashboard must not hardcode a datasource UID)")

    for required in ("cluster", "instance", "database"):
        if required not in var_names:
            result.error(f"{path}: missing expected template variable '{required}'")

    if "interval" not in var_types.values():
        result.error(f"{path}: missing a rate/interval-type ('interval') template variable")

    if not data.get("links"):
        result.warn(f"{path}: dashboard has no top-level 'links' entries")

    panels = list(walk_panels(data.get("panels", [])))
    seen_ids = []
    for p in panels:
        seen_ids.append(p.get("id"))
        ptype = p.get("type")
        if ptype == "row":
            if not p.get("collapsed", False) and p.get("panels"):
                result.error(
                    f"{path}: expanded row '{p.get('title')}' stores nested panels; "
                    "Grafana requires expanded-row panels at dashboard top level"
                )
            continue
        if ptype != "text" and not p.get("description"):
            result.error(f"{path}: panel '{p.get('title')}' (id={p.get('id')}) has no description")
        for t in p.get("targets", []):
            expr = t.get("expr")
            if not expr:
                continue
            tokens = extract_metric_tokens(expr)
            for tok in tokens:
                if tok == "up":
                    continue  # built-in Prometheus scrape-health metric, always valid
                if ":" in tok:
                    if tok not in recording_rules:
                        result.error(
                            f"{path}: panel '{p.get('title')}' references undefined recording rule '{tok}' "
                            f"(expr: {expr!r})"
                        )
                    continue
                if tok not in base_metrics:
                    result.error(
                        f"{path}: panel '{p.get('title')}' references metric '{tok}' not listed in "
                        f"docs/metrics-reference.md's machine-readable inventory (expr: {expr!r})"
                    )

    dup_ids = {i for i in seen_ids if seen_ids.count(i) > 1}
    if dup_ids:
        result.error(f"{path}: duplicate panel ids: {sorted(dup_ids)}")


def main() -> int:
    result = Result("Grafana dashboard structure")
    base_metrics = load_metric_inventory(result)
    recording_rules = load_recording_rule_names(result)

    dash_dir = repo_root() / "grafana" / "dashboards"
    dashboards = sorted(dash_dir.glob("*.json")) if dash_dir.exists() else []
    if not dashboards:
        result.warn(f"no dashboard JSON files found under {dash_dir}")

    for d in dashboards:
        check_dashboard(d, base_metrics, recording_rules, result)

    result.print_report()
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

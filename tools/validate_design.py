#!/usr/bin/env python3
"""Validate the SPL design package, not implementation or real-world performance.

Usage: python tools/validate_design.py
Dependencies: PyYAML, jsonschema. Reads only this package and prints JSON to stdout.
"""
from __future__ import annotations
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
    from jsonschema import Draft202012Validator
except ImportError as exc:
    print(json.dumps({"status": "BLOCKED_ENV", "detail": str(exc)}, ensure_ascii=False))
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parents[1]
checks: list[dict[str, Any]] = []

def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append({"name": name, "passed": bool(ok), "detail": detail})

def reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON value prohibited: {value}")

def load_json(rel: str) -> Any:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"), parse_constant=reject_constant)

def fails(schema: dict[str, Any], value: dict[str, Any]) -> bool:
    return bool(list(Draft202012Validator(schema).iter_errors(value)))

def main() -> int:
    skip_dirs = {".git", ".cache", ".venv", ".venvs", ".tools", ".bootstrap", "__pycache__", "state", "data", "runs", "delivery", "src", "tests", "validation"}
    files = [p for p in ROOT.rglob("*") if p.is_file() and not p.is_symlink()
             and not set(p.relative_to(ROOT).parts) & skip_dirs
             and p.name not in {"DESIGN_VALIDATION.json", "CHECKSUMS.sha256"}]
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        check(f"utf8:{rel}", bool(path.read_text(encoding="utf-8")))
        if path.suffix == ".json":
            load_json(rel)
            check(f"json:{rel}", True)
        if path.suffix in {".yaml", ".yml"}:
            check(f"yaml:{rel}", isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict))
    check("agents_under_8kib", (ROOT / "AGENTS.md").stat().st_size < 8192)
    c = yaml.safe_load((ROOT / "configs/project.yaml").read_text(encoding="utf-8"))
    for name in ["report_generation", "report_generator_module", "external_submission"]:
        check(f"scope_forbids:{name}", c["scope"][name] is False)
    for name, value in c["safety"].items():
        if name.startswith("allow_"):
            check(f"safety_forbids:{name}", value is False)
    check("legacy_source_not_imported", not c["project"]["import_legacy_source"])
    check("legacy_approval_not_inherited", not c["project"]["inherit_legacy_approval"])
    check("real_synthetic_not_mixed", not c["data"]["mix_real_and_synthetic"])
    check("routine_approval_not_required", not c["autonomy"]["routine_approval_required"])
    check("continue_independent_tasks", c["autonomy"]["continue_independent_tasks_on_block"])
    check("report_owner_chatgpt", c["outputs"]["report_owner"] == "CHATGPT_AFTER_USER_HANDOFF")
    check("grid_has_nominal", 0 in c["sensitivity"]["clock_offset_s"] and 0 in c["sensitivity"]["boundary_offset_m"])
    check("15_sensitivity_scenarios", len(c["sensitivity"]["clock_offset_s"]) * len(c["sensitivity"]["boundary_offset_m"]) == 15)
    check("train_features_not_label_features", not set(c["learning"]["features"]) & set(c["learning"]["exclude_features"]))
    tasks = load_json("tasks/backlog.json")["tasks"]
    ids = [t["id"] for t in tasks]
    check("25_unique_tasks", len(tasks) == len(set(ids)) == 25)
    graph: dict[str, list[str]] = {}
    for t in tasks:
        deps = t["requires_pass"] + t["waits_for_terminal"]
        check(f"valid_deps:{t['id']}", all(d in ids and d != t["id"] for d in deps))
        graph[t["id"]] = list(set(deps))
        check(f"has_acceptance:{t['id']}", bool(t["acceptance"]) and bool(t["completion_artifacts"]))
    visiting: set[str] = set()
    done: set[str] = set()
    def visit(node: str) -> bool:
        if node in visiting:
            return False
        if node in done:
            return True
        visiting.add(node)
        for dep in graph[node]:
            if not visit(dep):
                return False
        visiting.remove(node)
        done.add(node)
        return True
    check("task_graph_acyclic", all(visit(t) for t in graph))
    kinds = {t["id"]: t["kind"] for t in tasks}
    def ancestors(node: str, seen: set[str] | None = None) -> set[str]:
        acc: set[str] = set() if seen is None else seen
        for dep in graph[node]:
            if dep not in acc:
                acc.add(dep)
                ancestors(dep, acc)
        return acc
    for t in tasks:
        if t["kind"] == "code":
            check(f"code_unblocked_by_data:{t['id']}", all(kinds[d] not in {"external", "real_run"} for d in ancestors(t["id"])))
    schemas: dict[str, Any] = {}
    for p in sorted((ROOT / "schemas").glob("*.json")):
        s = load_json(p.relative_to(ROOT).as_posix())
        Draft202012Validator.check_schema(s)
        schemas[p.stem] = s
        check(f"valid_schema:{p.name}", True)
    pairs = [("handoff_status.schema", "templates/HANDOFF_STATUS.example.json"), ("metric_fact.schema", "templates/metric_fact.example.json"), ("task_state.schema", "templates/task_state.example.json")]
    for schema, rel in pairs:
        errors = list(Draft202012Validator(schemas[schema]).iter_errors(load_json(rel)))
        check(f"example_matches_schema:{rel}", not errors, "; ".join(e.message for e in errors))
    hand = load_json("templates/HANDOFF_STATUS.example.json")
    check("no_actual_result_in_template", hand["evidence_status"] == "NONE" and hand["engineering_status"] == "NOT_STARTED")
    bad = copy.deepcopy(hand); bad["report_generated"] = True
    check("negative:report_generation_rejected", fails(schemas["handoff_status.schema"], bad))
    bad = copy.deepcopy(hand); bad["external_submission_performed"] = True
    check("negative:external_submission_rejected", fails(schemas["handoff_status.schema"], bad))
    fact = load_json("templates/metric_fact.example.json")
    bad = copy.deepcopy(fact); bad["status"] = "COMPUTED"; bad["value"] = 5
    check("negative:computed_fact_without_evidence_rejected", fails(schemas["metric_fact.schema"], bad))
    bad = copy.deepcopy(fact); bad["value"] = 0
    check("negative:uncomputed_fact_as_zero_rejected", fails(schemas["metric_fact.schema"], bad))
    state = load_json("templates/task_state.example.json")
    bad = copy.deepcopy(state); bad["state"] = "PASSED"
    check("negative:pass_without_execution_rejected", fails(schemas["task_state.schema"], bad))
    bad = copy.deepcopy(fact); bad["unexpected"] = True
    check("negative:unknown_fact_fields_rejected", fails(schemas["metric_fact.schema"], bad))
    check("no_old_source_copied", not any(p.suffix == ".py" and p.parent.name != "tools" for p in files))
    out = {
        "package_kind": "FINAL_DESIGN_VALIDATION_ONLY",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if all(x["passed"] for x in checks) else "FAIL",
        "checks_total": len(checks),
        "passed": sum(x["passed"] for x in checks),
        "failed": sum(not x["passed"] for x in checks),
        "implementation_tests_executed": False,
        "real_data_analysis_executed": False,
        "model_training_executed": False,
        "checks": checks,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["status"] == "PASS" else 1

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)

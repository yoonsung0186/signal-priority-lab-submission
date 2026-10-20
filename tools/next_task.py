#!/usr/bin/env python3
"""Read-only task selection helper; executes no commands, agents or state writes.

Inputs: backlog and optional progress object with tasks keyed by task_id.
The caller must verify actual execution evidence before committing proposed states.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from preflight import verify_root, safe_child

TERMINAL = {'PASSED', 'FAILED_FINAL', 'BLOCKED_EXTERNAL', 'BLOCKED_DEPENDENCY',
            'SKIPPED_CONDITIONAL', 'BUDGET_EXHAUSTED'}
ALL_STATES = TERMINAL | {'PENDING', 'RUNNING', 'FAILED_RETRYABLE'}


def plan(tasks: list[dict], progress: dict | None = None, finalize_only: bool = False) -> dict:
    by = {t['id']: t for t in tasks}
    if len(by) != len(tasks) or not by:
        raise ValueError('DUPLICATE_OR_EMPTY_TASKS')
    done, visiting = set(), set()
    def visit(tid: str) -> None:
        if tid in visiting:
            raise ValueError('CYCLIC_BACKLOG')
        if tid in done:
            return
        visiting.add(tid)
        for dep in by[tid].get('requires_pass', []) + by[tid].get('waits_for_terminal', []):
            if dep not in by:
                raise ValueError('UNKNOWN_DEPENDENCY')
            visit(dep)
        visiting.remove(tid)
        done.add(tid)
    for tid in by:
        visit(tid)
    entries = (progress or {}).get('tasks', {})
    if not isinstance(entries, dict) or not set(entries).issubset(by):
        raise ValueError('INVALID_PROGRESS_TASKS')
    states = {}
    changes = []
    for tid, task in by.items():
        entry = entries.get(tid, {})
        if not isinstance(entry, dict):
            raise ValueError('INVALID_TASK_ENTRY')
        state = entry.get('state', 'PENDING')
        attempts = entry.get('attempts', 0)
        same_error = entry.get('same_error_count', 0)
        if state not in ALL_STATES or not isinstance(attempts, int) or attempts < 0 or not isinstance(same_error, int) or same_error < 0:
            raise ValueError('INVALID_PROGRESS_STATE')
        states[tid] = state
        if state == 'FAILED_RETRYABLE' and (attempts >= task.get('retry_limit', 3) or same_error >= 2):
            states[tid] = 'FAILED_FINAL'
            changes.append({'task_id': tid, 'from': state, 'to': 'FAILED_FINAL', 'reason': 'RETRY_LIMIT'})
        elif finalize_only and state in {'PENDING', 'FAILED_RETRYABLE'} and task.get('kind') != 'finalizer':
            states[tid] = 'BUDGET_EXHAUSTED'
            changes.append({'task_id': tid, 'from': state, 'to': 'BUDGET_EXHAUSTED', 'reason': 'FINALIZE_ONLY'})
    while True:
        updates = []
        for tid, task in by.items():
            if states[tid] not in {'PENDING', 'FAILED_RETRYABLE'}:
                continue
            blockers = [d for d in task.get('requires_pass', []) if states[d] in TERMINAL - {'PASSED'}]
            if blockers:
                updates.append((tid, blockers))
        if not updates:
            break
        for tid, blockers in updates:
            changes.append({'task_id': tid, 'from': states[tid], 'to': 'BLOCKED_DEPENDENCY', 'blocked_by': blockers})
            states[tid] = 'BLOCKED_DEPENDENCY'
    ready = []
    for tid, task in by.items():
        if states[tid] not in {'PENDING', 'FAILED_RETRYABLE'}:
            continue
        if all(states[d] == 'PASSED' for d in task.get('requires_pass', [])) and all(states[d] in TERMINAL for d in task.get('waits_for_terminal', [])):
            ready.append(tid)
    ready.sort(key=lambda tid: (by[tid].get('priority', 999), tid))
    running = [tid for tid, state in states.items() if state == 'RUNNING']
    return {'kind': 'ADVISORY_PLAN_NOT_EXECUTION_EVIDENCE', 'ready': ready,
            'in_progress_or_needs_recovery': running, 'proposed_state_changes': changes,
            'projected_states': states, 'all_terminal': all(s in TERMINAL for s in states.values()),
            'executor_required': 'Codex must inspect real evidence and actual concurrency before executing.',
            'state_written': False, 'commands_executed': False}


def read_json(root: Path, relative: str) -> dict:
    p = safe_child(root, relative)
    if p.stat().st_size > 2_000_000:
        raise ValueError('INPUT_TOO_LARGE')
    def bad(value: str):
        raise ValueError('NONFINITE_JSON')
    value = json.loads(p.read_text(encoding='utf-8'), parse_constant=bad)
    if not isinstance(value, dict):
        raise ValueError('OBJECT_REQUIRED')
    return value


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, default=Path.cwd())
    ap.add_argument('--state', help='Explicit project-relative progress file, if resuming.')
    ap.add_argument('--backlog', default='tasks/backlog.json', help='Explicit profile backlog; default retains original v3 scope.')
    ap.add_argument('--finalize-only', action='store_true')
    args = ap.parse_args()
    try:
        root = verify_root(args.root)
        data = read_json(root, args.backlog)
        progress = read_json(root, args.state) if args.state else None
        print(json.dumps(plan(data['tasks'], progress, args.finalize_only), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        print(json.dumps({'status': 'BLOCKED_INVALID_PLAN', 'state_written': False}))
        return 2

if __name__ == '__main__':
    raise SystemExit(main())

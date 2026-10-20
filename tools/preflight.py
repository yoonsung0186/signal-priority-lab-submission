#!/usr/bin/env python3
"""Non-destructive, local-only basic diagnostics; not full runtime/analysis approval.

Python 3.11+. Run: python tools/preflight.py --root .
Only writes a NEW state/preflight/<unique-id>/basic.json under a verified root.
Never installs, authenticates, makes network requests, deletes, commits, or pushes.
Version probes invoke discovered local binaries, with a timeout; no raw stdout is saved.
"""
from __future__ import annotations
import argparse
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MARKER = 'signal-priority-lab-final-v3'
CHECKS: list[dict[str, Any]] = []

def is_link_or_reparse(p: Path) -> bool:
    try:
        s = p.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(s.st_mode) or bool(getattr(s, 'st_file_attributes', 0) & 0x400)

def verify_root(root: Path) -> Path:
    # Do not resolve away links before checking every path component.
    root = Path(os.path.abspath(root))
    for p in [*reversed(root.parents), root]:
        if is_link_or_reparse(p):
            raise ValueError('LINK_OR_REPARSE_IN_PROJECT_ROOT')
    if not root.is_dir():
        raise ValueError('ROOT_NOT_DIRECTORY')
    marker = root / '.spl-project-root'
    if is_link_or_reparse(marker) or not marker.is_file():
        raise ValueError('PROJECT_MARKER_MISSING_OR_LINKED')
    if marker.stat().st_size > 128 or marker.read_text(encoding='utf-8').strip() != MARKER:
        raise ValueError('PROJECT_MARKER_MISMATCH')
    return root

def safe_child(root: Path, relative: str) -> Path:
    r = Path(relative)
    if relative in {'', '.'} or r.is_absolute() or '..' in r.parts or '\\' in relative or ':' in relative:
        raise ValueError('UNSAFE_RELATIVE_PATH')
    p = root
    for part in r.parts:
        p = p / part
        if is_link_or_reparse(p):
            raise ValueError('LINK_OR_REPARSE_IN_CHILD_PATH')
    if not p.resolve().is_relative_to(root.resolve()):
        raise ValueError('CHILD_ESCAPES_ROOT')
    return p

def add(name: str, status: str, detail: Any = None) -> None:
    CHECKS.append({'id': name, 'status': status, 'detail': detail})

def probe(args: list[str], root: Path, timeout: int = 8) -> tuple[int | None, str]:
    try:
        p = subprocess.run(args, cwd=root, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, encoding='utf-8', errors='replace', timeout=timeout,
                           shell=False)
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None, ''

def tool_version(tool: str, root: Path) -> None:
    exe = shutil.which(tool)
    if not exe:
        add('tool_'+tool, 'NOT_FOUND', 'Optional tool absence is not full-project failure.')
        return
    code, out = probe([exe, '--version'], root)
    # Store only a numeric version and status, never command output, user paths, or env.
    m = re.search(r'(?<![A-Za-z0-9])\d+\.\d+(?:\.\d+)?', out[:1000])
    add('tool_'+tool, 'VERSION_PROBE_OK' if code == 0 else 'PROBE_UNAVAILABLE',
        {'version': m.group() if m else None, 'exit_code': code,
         'login_verified': False, 'permissions_verified': False})

def inspect_git_root(root: Path) -> bool:
    if is_link_or_reparse(root/'.git'):
        add('git_boundary', 'BLOCKED', 'Linked Git metadata is not an independent project root.')
        return False
    if any(os.path.lexists(p/'.git') for p in root.parents):
        add('git_boundary', 'BLOCKED', 'Nested repository is not an independent project root.')
        return False
    # The main workspace must have its own Git directory, not an external pointer file.
    if (root/'.git').is_file():
        add('git_boundary', 'BLOCKED', 'Git pointer file requires separately scoped worker handling; not a main root.')
        return False
    exe = shutil.which('git')
    if not exe:
        add('git_boundary', 'NOT_CHECKED', 'Git not found; Codex must inspect repository boundary before writes.')
        # Marker alone cannot prove that no parent repo exists. Inspect only .git path existence in ancestors.
        if any(os.path.lexists(p/'.git') for p in root.parents):
            add('git_parent_marker', 'BLOCKED', 'Parent repository marker exists.')
            return False
        return True
    code, out = probe([exe, 'rev-parse', '--show-toplevel'], root)
    if code == 0 and out.strip():
        try:
            same = Path(out.strip()).resolve() == root.resolve()
        except OSError:
            same = False
        add('git_boundary', 'PASS' if same else 'BLOCKED', 'Current repository root match only; no paths exported.')
        return same
    # Git errors do not always mean not a repo; detect parent markers conservatively.
    if any(os.path.lexists(p/'.git') for p in root.parents):
        add('git_boundary', 'BLOCKED', 'Parent .git found; git boundary could not be confirmed.')
        return False
    if os.path.lexists(root/'.git'):
        add('git_boundary', 'NOT_CHECKED', 'Project .git exists but command failed. Do not claim commit readiness.')
    else:
        add('git_boundary', 'NO_REPOSITORY_YET', 'No parent .git detected; this script does not git init.')
    return True

def inspect_model_config(root: Path) -> None:
    import tomllib
    try:
        path = safe_child(root, '.codex/config.toml')
        if path.stat().st_size > 65536:
            raise ValueError('CONFIG_TOO_LARGE')
        cfg = tomllib.loads(path.read_text(encoding='utf-8'))
        a = cfg.get('agents', {})
        actual = {
            'main': cfg.get('model'), 'main_effort': cfg.get('model_reasoning_effort'),
            'worker': a.get('default_subagent_model'),
            'worker_effort': a.get('default_subagent_reasoning_effort'),
            'max_workers': a.get('max_concurrent_threads_per_session')}
        expected = {'main':'gpt-6-astra','main_effort':'medium','worker':'gpt-6.1-sol',
                    'worker_effort':'xhigh','max_workers':2}
        # Do not dump any unrecognized config field or even its value.
        matches = {key: actual[key] == value for key, value in expected.items()}
        add('model_config_declared', 'MATCH' if all(matches.values()) else 'MISMATCH', matches)
    except (OSError, ValueError, TypeError, AttributeError):
        add('model_config_declared', 'NOT_CHECKED', 'Missing, unsafe, malformed or incompatible config.')
    add('actual_main_and_child_models', 'NOT_CHECKED', 'Requires effective configuration and actual child runtime observation.')

def main() -> int:
    CHECKS.clear()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, default=Path.cwd())
    ap.add_argument('--no-write', action='store_true', help='Print JSON only; no filesystem writes.')
    args = ap.parse_args()
    if sys.version_info < (3, 11):
        print(json.dumps({'status':'BLOCKED_ENV','reason':'Python 3.11+ required'}))
        return 2
    try:
        root = verify_root(args.root)
        add('root_marker_and_links', 'PASS', 'Scope marker check, not OS sandbox assurance.')
        if not inspect_git_root(root):
            print(json.dumps({'status':'BLOCKED_SCOPE','checks':CHECKS}, ensure_ascii=False, indent=2))
            return 2
        free = shutil.disk_usage(root).free / 1024**3
        add('disk_free', 'PASS' if free >= 5 else 'LOW_SPACE', {'free_gib':round(free,2),'recommended_min_gib':5})
        add('python', 'OBSERVED', {'version':platform.python_version(),'os':platform.system(),
            'machine':platform.machine(),'cpu_count':os.cpu_count(),
            'project_local_prefix':Path(sys.prefix).resolve().is_relative_to(root)})
        for tool in ['git','codex','uv','gh']:
            tool_version(tool, root)
        inspect_model_config(root)
        for name in ['sandbox_runtime','local_dependency_imports','four_skills_discoverable',
                     'git_commit','private_remote_and_outgoing_scan','actual_data_rights_and_semantics',
                     'contest_rules_and_deadline_time','session_quota_power_and_resumption']:
            add(name,'NOT_CHECKED','Codex must perform the corresponding scoped check; do not auto-PASS.')
        data = {'kind':'BASIC_PREFLIGHT_ONLY','status':'PRELIMINARY_NOT_STARTUP_APPROVAL',
                'checked_at_utc':datetime.now(timezone.utc).isoformat(),
                'network_calls_performed':False,'installs_performed':False,
                'destructive_operations_performed':False,'git_writes_performed':False,'checks':CHECKS}
        if not args.no_write:
            run = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8]
            directory = safe_child(root,'state/preflight/'+run)
            directory.mkdir(parents=True,exist_ok=False)
            # Recheck containment after creation. This is not protection against hostile concurrent mutation.
            target = safe_child(root,'state/preflight/'+run+'/basic.json')
            with target.open('x',encoding='utf-8') as f:
                json.dump(data,f,ensure_ascii=False,indent=2)
            data['saved_relative_path']=target.relative_to(root).as_posix()
        print(json.dumps(data,ensure_ascii=False,indent=2))
        return 0
    except (OSError, ValueError) as exc:
        # Keep filesystem paths and exception text out of exported diagnostics.
        print(json.dumps({'status':'BLOCKED_SCOPE_OR_IO','error_type':type(exc).__name__,
                          'message':'No alternative directory or permission escalation attempted.'}))
        return 2

if __name__ == '__main__':
    raise SystemExit(main())

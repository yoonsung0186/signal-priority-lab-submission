#!/usr/bin/env python3
"""Cross-file final design tests. Not traffic implementation or runtime validation."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import tomllib
import yaml
from jsonschema import Draft202012Validator
from next_task import plan

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    checks = []
    def check(name, value):
        checks.append({'name': name, 'passed': bool(value)})
    def load(name):
        return json.loads((ROOT / name).read_text(encoding='utf-8'))
    cfg = tomllib.loads((ROOT / '.codex/config.toml').read_text())
    pol = load('configs/startup_policy.json')
    proj = yaml.safe_load((ROOT / 'configs/project.yaml').read_text())
    check('main_model_effort', (cfg['model'], cfg['model_reasoning_effort']) == ('gpt-6-astra', 'medium'))
    check('worker_defaults', (cfg['agents']['default_subagent_model'], cfg['agents']['default_subagent_reasoning_effort']) == ('gpt-6.1-sol', 'xhigh'))
    check('max_two_workers', cfg['agents']['max_concurrent_threads_per_session'] == pol['max_concurrent_workers'] == 2)
    check('auto_review_without_full_access', cfg['sandbox_mode'] == 'workspace-write' and cfg['approval_policy'] == 'on-request' and cfg['approvals_reviewer'] == 'auto_review')
    for role in ['spl_worker', 'spl_reviewer']:
        path = ROOT / '.codex' / cfg['agents'][role]['config_file']
        check('role_path_' + role, path.resolve().is_relative_to(ROOT.resolve()) and path.is_file())
        data = tomllib.loads(path.read_text())
        check('role_model_' + role, (data['model'], data['model_reasoning_effort']) == ('gpt-6.1-sol', 'xhigh'))
        if role == 'spl_reviewer':
            check('reviewer_readonly', data['sandbox_mode'] == 'read-only' and data['approval_policy'] == 'never')
    for skill in pol['custom_repo_skills']:
        text = (ROOT / '.agents/skills' / skill / 'SKILL.md').read_text()
        header = yaml.safe_load(text.split('---', 2)[1])
        check('skill_' + skill, header.get('name') == skill and bool(header.get('description')))
    check('jev_excluded', 'Jev' in pol['excluded_integrations'])
    check('no_paid_api', proj['safety']['allow_paid_apis'] is False)
    check('one_git_authority', proj['safety']['git_gate_authority'] == 'configs/startup_policy.json#git')
    gate = pol['git']
    check('remote_requires_user_ref', gate['approved_remote'] is None or bool(gate['user_authorization_ref']))
    check('no_repo_creation_or_force', gate['auto_create_repo'] is False and gate['force_push'] is False)
    check('fixed_signal_clock', proj['sensitivity']['clock_shift_reference'] == 'TRAJECTORY_TO_FIXED_SIGNAL_CLOCK' and proj['sensitivity']['shift_both_clocks'] is False)
    check('report_owner', proj['outputs']['report_owner'] == 'CHATGPT_AFTER_USER_HANDOFF' and proj['scope']['report_generation'] is False)
    check('model_runtime_not_preclaimed', pol['runtime_models_verified'] is False)
    check('local_runtime_not_preclaimed', pol['local_environment_verified'] is False)
    check('no_deadline_time_invention', pol['deadlines']['contest_time'] is None and pol['deadlines']['time_verified'] is False)
    tasks = load('tasks/backlog.json')['tasks']
    by = {t['id']: t for t in tasks}
    def ancestors(tid):
        deps = by[tid]['requires_pass'] + by[tid]['waits_for_terminal']
        return set(deps).union(*(ancestors(x) for x in deps)) if deps else set()
    check('baseline_not_wait_ai', not {'T10', 'T11', 'T20'} & ancestors('T19'))
    check('startup_not_downstream_full_acceptance', '미구현' in by['T00']['acceptance'] and 'T07' in by['T02']['acceptance'])
    check('final_regression_explicit', '최신' in by['T23']['acceptance'] and '재실행' in by['T23']['acceptance'])
    check('initial_ready_root_only', plan(tasks)['ready'] == ['T00'])
    progress = {'tasks': {t['id']: {'state': 'PASSED' if t['id']=='T00' else 'BLOCKED_EXTERNAL'} for t in tasks if t['id']!='T24'}}
    check('partial_finalizer_reachable', plan(tasks, progress)['ready'] == ['T24'])
    hand_schema = load('schemas/handoff_status.schema.json')
    val = Draft202012Validator(hand_schema)
    hand = load('templates/HANDOFF_STATUS.example.json')
    check('original_status_template_valid', val.is_valid(hand))
    bad = copy.deepcopy(hand); bad['evidence_status']='REAL_VALIDATED'
    check('no_fake_real_validated', not val.is_valid(bad))
    bad = copy.deepcopy(hand); bad['handoff_type']='FULL_TECHNICAL_HANDOFF'
    check('no_full_handoff_from_template', not val.is_valid(bad))
    bad = copy.deepcopy(hand); bad['template']=False; bad['engineering_status']='VERIFIED'
    check('no_verified_without_snapshot', not val.is_valid(bad))
    bad = copy.deepcopy(hand); bad['report_generated']=True
    check('no_report_flag', not val.is_valid(bad))
    state_schema = load('schemas/task_state.schema.json')
    st = load('templates/task_state.example.json')
    st['state']='FAILED_FINAL'
    check('failed_final_supported', Draft202012Validator(state_schema).is_valid(st))
    st.update(state='PASSED',attempts=1,actual_commands=['test'],artifact_refs=['test.json'],source_tree_hash='a'*64,config_hash='b'*64,environment_hash='c'*64,updated_at_utc='2026-10-12T00:00:00Z')
    check('pass_requires_exit_evidence', not Draft202012Validator(state_schema).is_valid(st))
    check('no_traffic_source_shipped', not (ROOT / 'src').exists())
    result = {'kind': 'CROSS_FILE_DESIGN_CHECKS_ONLY', 'status': 'PASS' if all(x['passed'] for x in checks) else 'FAIL',
              'checks_total': len(checks), 'passed': sum(x['passed'] for x in checks),
              'failed': sum(not x['passed'] for x in checks), 'checks': checks,
              'actual_traffic_execution': False, 'actual_local_codex_execution': False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'PASS' else 1

if __name__ == '__main__':
    raise SystemExit(main())

"""Local synthetic/normalized baseline CSV+JSON output. No report or submission."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys

from .contracts import ContractError
from .engine import EngineConfig, analyze, sensitivity
from .safety import child, new_run, verify_root
from .synthetic import synthetic_fixture, verify_oracle


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def write_json(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True)
        stream.write('\n')


def write_csv(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open('x', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False, allow_nan=False) if isinstance(value, (dict, list)) else value for key, value in row.items()})


def run(data, output, root=None, input_ref='BUILTIN_SYNTHETIC', include_sensitivity=True):
    root = verify_root(root or Path.cwd())
    result = analyze(data)
    scenarios = sensitivity(data) if include_sensitivity else None
    oracle = verify_oracle(result) if input_ref == 'BUILTIN_SYNTHETIC' else None
    if oracle and oracle['status'] != 'PASS':
        raise ContractError('SYNTHETIC_ORACLE_FAILED')
    directory = new_run(root, output)
    write_json(directory/'result.json', result)
    write_csv(directory/'events.csv', result['events'])
    write_csv(directory/'cells.csv', result['cells'])
    if scenarios is not None:
        write_json(directory/'sensitivity.json', scenarios)
    if oracle:
        write_json(directory/'synthetic_oracle.json', oracle)
        write_json(directory/'synthetic_input.json', data)
    files = {path.name: {'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in sorted(directory.iterdir()) if path.is_file()}
    command = ['python', '-m', 'spl', 'synthetic' if input_ref == 'BUILTIN_SYNTHETIC' else 'analyze', '--output', output]
    if input_ref != 'BUILTIN_SYNTHETIC':
        command.extend(['--input', input_ref])
    if not include_sensitivity:
        command.append('--no-sensitivity')
    provenance = {'schema_version': 'spl-run-1', 'run_status': 'COMPLETED_BOUNDED_BASELINE',
                  'created_at_utc': datetime.now(timezone.utc).isoformat(), 'input_ref': input_ref,
                  'input_sha256': hashlib.sha256(canonical(data)).hexdigest(),
                  'engine_config_sha256': hashlib.sha256(canonical(result['config'])).hexdigest(),
                  'data_kind': result['data_kind'], 'evidence_status': 'SYNTHETIC_ONLY' if result['data_kind'] == 'SYNTHETIC' else 'NORMALIZED_REAL_INPUT_NOT_INDEPENDENTLY_VALIDATED',
                  'command': command, 'command_kind': 'RECONSTRUCTED_EQUIVALENT_CLI_INVOCATION',
                  'exit_code': 0, 'python_version': platform.python_version(), 'os': platform.system(),
                  'files': files, 'manual_review_status': 'NOT_PERFORMED', 'real_accuracy': None,
                  'source_manifest_status': 'MAIN_ORCHESTRATOR_TO_ADD_FROZEN_SOURCE_AND_ENVIRONMENT_MANIFEST',
                  'implemented_scope': 'BASELINE_EVENT_RATE_EXPOSURE_SENSITIVITY_JSON_CSV',
                  'not_implemented': ['source-specific real adapter', 'AI clustering', 'population bootstrap', 'priority rankings', 'Parquet export']}
    write_json(directory/'provenance.json', provenance)
    return directory, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('synthetic', 'analyze'):
        command = sub.add_parser(name)
        command.add_argument('--output', required=True, help='New project-relative runs/<new-id>')
        command.add_argument('--no-sensitivity', action='store_true')
        if name == 'analyze':
            command.add_argument('--input', required=True, help='Project-relative normalized JSON')
    for name in ('pet-qc','pet-analyze'):
        command=sub.add_parser(name)
        command.add_argument('--profile',required=True,help='Project-relative PET_INTERACTIONS_V1 YAML')
        command.add_argument('--output',required=True,help='New runs/<id>; prior outputs preserved')
    args = parser.parse_args(argv)
    try:
        root = verify_root(Path.cwd())
        if args.command in ('pet-qc','pet-analyze'):
            from .pet_run import run_pet
            directory,status=run_pet(root,args.profile,args.output,args.command)
            print(json.dumps({'output':directory.relative_to(root).as_posix(),**status},sort_keys=True))
            return 0
        if args.command == 'synthetic':
            data, input_ref = synthetic_fixture(), 'BUILTIN_SYNTHETIC'
        else:
            path = child(root, args.input)
            if path.stat().st_size > 32*1024*1024:
                raise ContractError('NORMALIZED_JSON_SIZE_LIMIT')
            def reject_constant(value):
                raise ContractError('JSON_NONFINITE_CONSTANT')
            data = json.loads(path.read_text(encoding='utf-8'), parse_constant=reject_constant)
            input_ref = path.relative_to(root).as_posix()
        directory, result = run(data, args.output, root, input_ref, not args.no_sensitivity)
        print(json.dumps({'status': 'COMPLETED_BOUNDED_BASELINE', 'output': directory.relative_to(root).as_posix(),
                          'data_kind': result['data_kind'], 'accuracy': None, 'manual_review_status': 'NOT_PERFORMED'}))
        return 0
    except ContractError as exc:
        print(json.dumps({'status': 'BLOCKED_INPUT', 'reason_code': str(exc)}))
        return 20
    except (OSError, ValueError):
        print(json.dumps({'status': 'BLOCKED_SCOPE_OR_IO', 'reason_code': 'NO_OVERWRITE_OR_EXTERNAL_PATH_ALLOWED'}))
        return 40


if __name__ == '__main__':
    raise SystemExit(main())

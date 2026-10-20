#!/usr/bin/env python3
"""Read-only initial release integrity check. Not signature or runtime certification."""
from __future__ import annotations
import argparse
import hashlib
import json
import re
from pathlib import Path
from preflight import verify_root, safe_child


def verify_manifest(root: Path, manifest: str = 'CHECKSUMS.sha256') -> dict:
    root = verify_root(root)
    p = safe_child(root, manifest)
    if p.stat().st_size > 2_000_000:
        raise ValueError('MANIFEST_TOO_LARGE')
    seen = set()
    results = []
    for line in p.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        digest, sep, rel = line.partition('  ')
        if not sep or not re.fullmatch('[a-f0-9]{64}', digest):
            raise ValueError('INVALID_MANIFEST_LINE')
        if rel in seen or rel == manifest:
            raise ValueError('DUPLICATE_OR_RECURSIVE_ENTRY')
        seen.add(rel)
        f = safe_child(root, rel)
        if not f.is_file():
            results.append({'file': rel, 'status': 'MISSING'})
            continue
        h = hashlib.sha256()
        with f.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                h.update(block)
        results.append({'file': rel, 'status': 'MATCH' if h.hexdigest() == digest else 'MISMATCH'})
    if not results:
        raise ValueError('EMPTY_MANIFEST')
    return {'kind': 'LAUNCH_INTEGRITY_ONLY',
            'status': 'PASS' if all(r['status'] == 'MATCH' for r in results) else 'FAIL',
            'files_checked': len(results), 'results': results,
            'runtime_or_analysis_certified': False,
            'note': 'After authorized edits, use new source snapshots; do not disguise changes by rewriting this release manifest.'}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, default=Path.cwd())
    ap.add_argument('--manifest', default='CHECKSUMS.sha256')
    args = ap.parse_args()
    try:
        result = verify_manifest(args.root, args.manifest)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['status'] == 'PASS' else 1
    except (OSError, ValueError):
        print(json.dumps({'status': 'FAIL', 'reason': 'UNSAFE_OR_INVALID_MANIFEST'}))
        return 2

if __name__ == '__main__':
    raise SystemExit(main())

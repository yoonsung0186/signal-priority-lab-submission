"""Project-local path guards, new runs only; no links or external writes."""
from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
import stat


def linked(path):
    try:
        item = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(item.st_mode) or bool(getattr(item, 'st_file_attributes', 0) & 0x400)


def verify_root(root):
    root = Path(os.path.abspath(root))
    for path in [*reversed(root.parents), root]:
        if linked(path):
            raise ValueError('PROJECT_ROOT_LINK_OR_REPARSE')
    marker = root / '.spl-project-root'
    if linked(marker) or not marker.is_file() or marker.stat().st_size > 128 or marker.read_text(encoding='utf-8').strip() != 'signal-priority-lab-final-v3':
        raise ValueError('PROJECT_ROOT_MARKER_INVALID')
    if any(os.path.lexists(path/'.git') for path in root.parents):
        raise ValueError('PROJECT_INSIDE_PARENT_REPOSITORY')
    if linked(root/'.git') or (root/'.git').is_file():
        raise ValueError('PROJECT_GIT_METADATA_LINK_OR_POINTER')
    return root


def child(root, relative):
    if not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative:
        raise ValueError('UNSAFE_RELATIVE_PATH')
    path = Path(relative)
    if path.is_absolute() or PureWindowsPath(relative).is_absolute() or '..' in path.parts or path == Path('.'):
        raise ValueError('UNSAFE_RELATIVE_PATH')
    result = root
    for part in path.parts:
        result = result / part
        if linked(result):
            raise ValueError('CHILD_LINK_OR_REPARSE')
    if not result.resolve().is_relative_to(root.resolve()):
        raise ValueError('CHILD_ESCAPES_PROJECT')
    return result


def new_run(root, relative):
    root = verify_root(root)
    parts = Path(relative).parts
    if len(parts) != 2 or parts[0] != 'runs':
        raise ValueError('OUTPUT_MUST_BE_RUNS_NEW_ID')
    path = child(root, relative)
    parent = child(root, 'runs')
    parent.mkdir(exist_ok=True)
    path.mkdir(exist_ok=False)
    # This guard does not protect against an attacker racing concurrent mutation.
    return child(root, relative)

"""Content snapshot of explicitly allowlisted technical sources (no raw/state)."""
import hashlib,json,pathlib
from preflight import safe_child,verify_root
ROOT=pathlib.Path(__file__).resolve().parents[1]
DIRS=('src','tests','tools','configs','schemas','specs','.agents','.codex','startup','templates','tasks','technical')
FILES=('README.md','pyproject.toml','requirements.lock','requirements-bootstrap.in','AGENTS.md','CODEX_START_KO.md','CODEX_RESUME_KO.md','START_HERE_KO.md','00_README_KO.md','.gitignore','.gitattributes','.spl-project-root')
def digest(data):return hashlib.sha256(data).hexdigest()
def environment_manifest_ref(root=ROOT):
    r1='state/environment_manifest_r1.json'
    return r1 if safe_child(root,r1).is_file() else 'state/environment_manifest.json'
def source_files():
    verify_root(ROOT)
    files=[]
    for d in DIRS:
        for p in (ROOT/d).rglob('*'):
            if p.is_file() and '__pycache__' not in p.parts and p.suffix in {'.py','.md','.json','.yaml','.toml'}:
                if p.is_symlink() or not p.resolve().is_relative_to(ROOT):raise ValueError('Unsafe source path')
                files.append(p)
    files.extend(ROOT/f for f in FILES if (ROOT/f).is_file())
    for name in ('CODEX_RESUME_R1_KO.md','IMPLEMENTATION_ADDENDUM_R1_KO.md','REVIEW_R1_KO.md','RESUME_TASKS_R1.json'):
        file=ROOT/'review_r1'/name
        if file.is_file():files.append(file)
    for name in ('CODEX_FINISH.md','README_KO.md','CURRENT_REVIEW.md','readiness.py','receipt.schema.json'):
        file=ROOT/'finish_to_report'/name
        if file.is_file():files.append(file)
    for p in files:safe_child(ROOT,p.relative_to(ROOT).as_posix())
    return sorted(set(files))
def snapshot():
    entries=[{'path':p.relative_to(ROOT).as_posix(),'bytes':p.stat().st_size,'sha256':digest(p.read_bytes())} for p in source_files()]
    return {'files':entries,'sha256':digest(json.dumps(entries,sort_keys=True).encode())}
if __name__=='__main__': print(json.dumps(snapshot(),indent=2))

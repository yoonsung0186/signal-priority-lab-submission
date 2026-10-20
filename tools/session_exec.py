"""Scoped command evidence; never overwrite an existing evidence directory."""
import argparse, hashlib, json, os, pathlib, subprocess, time
from snapshot import snapshot, digest, environment_manifest_ref
from preflight import verify_root, safe_child
ROOT = pathlib.Path(__file__).resolve().parents[1]
def main():
    p=argparse.ArgumentParser(); p.add_argument('name'); p.add_argument('command',nargs=argparse.REMAINDER); a=p.parse_args()
    verify_root(ROOT)
    if not a.name or any(c in a.name for c in '/\\:') or a.name in {'.','..'}:raise ValueError('Unsafe evidence name')
    out=safe_child(ROOT,'state/executions/'+a.name)
    out.mkdir(parents=True,exist_ok=False)
    env=os.environ.copy(); env['PYTHONUTF8']='1'; env['PYTHONDONTWRITEBYTECODE']='1'
    source=snapshot()
    config=digest((ROOT/'configs/project.yaml').read_bytes())
    lock_hash=digest((ROOT/'requirements.lock').read_bytes()) if (ROOT/'requirements.lock').exists() else None
    environment_path=safe_child(ROOT,environment_manifest_ref(ROOT))
    environment=digest(environment_path.read_bytes()) if environment_path.exists() else None
    start=time.monotonic()
    r=subprocess.run(a.command,cwd=ROOT,env=env,capture_output=True)
    (out/'stdout.log').write_bytes(r.stdout); (out/'stderr.log').write_bytes(r.stderr)
    record={'command':a.command,'exit_code':r.returncode,'elapsed_s':time.monotonic()-start,'stdout_sha256':hashlib.sha256(r.stdout).hexdigest(),'stderr_sha256':hashlib.sha256(r.stderr).hexdigest(),'source_tree_hash':source['sha256'],'config_hash':config,'environment_hash':environment,'lock_hash':lock_hash,'source_unchanged':snapshot()['sha256']==source['sha256']}
    (out/'source_snapshot.json').write_text(json.dumps(source,indent=2),encoding='utf-8')
    (out/'execution.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
    print(json.dumps(record)); return r.returncode
if __name__=='__main__': raise SystemExit(main())

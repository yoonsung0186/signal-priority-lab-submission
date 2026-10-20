"""Record actual local reproduction environment without changing any settings."""
import argparse,importlib.metadata,json,pathlib,platform,sys
from snapshot import ROOT,digest
from preflight import verify_root,safe_child
from final_verify import encode

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',default='state/environment_manifest_r1.json');a=p.parse_args();verify_root(ROOT)
    target=safe_child(ROOT,a.output)
    if target.exists():raise FileExistsError('Existing environment evidence preserved; use a new workspace or new evidence path')
    lock=safe_child(ROOT,'requirements.lock').read_bytes();packages={}
    for line in lock.decode('utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):continue
        name,expected=line.split('==',1);actual=importlib.metadata.version(name)
        if expected!=actual:raise ValueError('Installed dependency does not match lock: '+name)
        packages[name]=actual
    packages['pip']=importlib.metadata.version('pip')
    result={'python':sys.version,'executable':sys.executable,'prefix':sys.prefix,'base_prefix':sys.base_prefix,'isolated':sys.prefix!=sys.base_prefix,'os':platform.platform(),'packages':packages,'requirements_lock_sha256':digest(lock),'scope':'Actual local reproduction environment, not inherited runtime identity'}
    target.parent.mkdir(parents=True,exist_ok=True)
    with target.open('xb') as f:f.write(encode(result))
    print(json.dumps({'output':a.output,'sha256':digest(target.read_bytes())}))
if __name__=='__main__':main()

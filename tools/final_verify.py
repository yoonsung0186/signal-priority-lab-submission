"""Immutable, uniquely named regression and canonical replay evidence."""
import argparse,json,os,pathlib,subprocess,sys,time,uuid
from snapshot import ROOT,snapshot,digest,environment_manifest_ref
from preflight import verify_root,safe_child

RUN_ARTIFACT_SUFFIXES={'.json','.jsonl','.csv','.parquet','.png','.svg','.md','.xml'}
REAL_CANONICAL_FILES=('real_qc.json','table_inventory.csv','class_sample_counts.csv','signal_coverage.csv')
SYNTHETIC_CANONICAL_FILES=('result.json','sensitivity.json','synthetic_oracle.json','synthetic_input.json','events.csv','cells.csv')

def encode(value):return (json.dumps(value,sort_keys=True,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode('utf-8')

def identifier(value):
    if not value or len(value)>100 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in value):
        raise ValueError('Unsafe verification ID')
    return value

def verification_context(root):
    def file_hash(relative,required=False):
        path=safe_child(root,relative)
        if required or path.exists():return digest(path.read_bytes())
        return None
    return {'config_sha256':file_hash('configs/project.yaml',True),
            'lock_sha256':file_hash('requirements.lock'),
            'environment_manifest_sha256':file_hash(environment_manifest_ref(root))}

def execution_plan(root,stamp):
    """Keep the bounded command/replay list explicit for later approved additions."""
    python=sys.executable
    plan=[('bootstrap',[python,'-m','unittest','discover','-s','tools','-p','test_*.py','-v']),
          ('implementation',[python,'-m','unittest','discover','-s','tests','-v']),
          ('optimized_guards',[python,'-O','-m','unittest','discover','-s','tests','-p','test_ops.py','-v']),
          ('preflight',[python,'tools/preflight.py','--root','.'])]
    syn=[f'runs/{stamp}_synthetic_{i+1}' for i in range(2)]
    real=[f'runs/{stamp}_real_{i+1}' for i in range(2)]
    plan.extend(('synthetic_'+str(i+1),[python,'-m','spl','synthetic','--output',run]) for i,run in enumerate(syn))
    plan.extend(('real_'+str(i+1),[python,'tools/real_qc.py','--output',run]) for i,run in enumerate(real))
    return plan,syn,real

def reserve_verification(root,stamp,plan,syn,real):
    identifier(stamp)
    relative='state/verifications/'+stamp
    paths=[relative,*syn,*real,*('state/executions/'+stamp+'_'+name for name,_ in plan)]
    for ref in paths:
        path=safe_child(root,ref)
        if os.path.lexists(path):raise FileExistsError('Verification output collision; use a fresh ID')
    directory=safe_child(root,relative)
    directory.mkdir(parents=True,exist_ok=False)
    return directory

def run_artifact_files(root,run):
    """Inspect only selected run trees, rejecting links before descending."""
    path=pathlib.Path(run)
    if len(path.parts)!=2 or path.parts[0]!='runs':raise ValueError('Invalid selected run path')
    base=safe_child(root,run)
    if not base.is_dir():raise ValueError('Selected run does not exist')
    files=[]
    def fail(error):raise error
    for directory,dirs,names in os.walk(base,topdown=True,followlinks=False,onerror=fail):
        for name in dirs:
            safe_child(root,(pathlib.Path(directory)/name).relative_to(root).as_posix())
        for name in names:
            file=safe_child(root,(pathlib.Path(directory)/name).relative_to(root).as_posix())
            if file.is_file() and file.suffix in RUN_ARTIFACT_SUFFIXES:files.append(file)
    return sorted(files)

def artifact_manifest(root,stamp,source_hash,context,real_run,synthetic_run):
    artifacts=[]
    for role,run in [('real_qc',real_run),('synthetic',synthetic_run)]:
        base=safe_child(root,run)
        for file in run_artifact_files(root,run):
            raw=file.read_bytes()
            artifacts.append({'role':role,'run':run,'path':file.relative_to(base).as_posix(),'bytes':len(raw),'sha256':digest(raw)})
    return {'schema_version':'1.0','verification_id':stamp,'source_tree_hash':source_hash,
            'verification_context':context,'selected_runs':{'real_qc':real_run,'synthetic':synthetic_run},
            'all_selected_packaged_run_artifacts':True,'artifacts':artifacts}

def canonical_comparisons(root,syn,real):
    comparisons=[]
    def compare(kind,name,a,b):
        semantic=(json.loads(a)==json.loads(b)) if name.endswith('.json') else (a.decode('utf-8').replace('\r\n','\n')==b.decode('utf-8').replace('\r\n','\n'))
        return {'kind':kind,'file':name,'equal':a==b,'byte_equal':a==b,'semantic_equal':semantic,'sha256':digest(a)}
    for name in REAL_CANONICAL_FILES:
        a=safe_child(root,real[0]+'/'+name).read_bytes();b=safe_child(root,real[1]+'/'+name).read_bytes()
        comparisons.append(compare('REAL_QC',name,a,b))
    for name in SYNTHETIC_CANONICAL_FILES:
        raw=safe_child(root,syn[0]+'/'+name).read_bytes();other=safe_child(root,syn[1]+'/'+name).read_bytes()
        comparisons.append(compare('SYNTHETIC',name,raw,other))
    if not any(x['kind']=='SYNTHETIC' for x in comparisons):raise ValueError('No synthetic canonical tables')
    if not all(x['equal'] for x in comparisons):raise ValueError('Canonical replay mismatch')
    return comparisons

def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    if '--profile' in argv:
        from pet_final import main as pet_main
        return pet_main(argv)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verification-id',help='Fresh ID; defaults to UTC time plus random suffix')
    args=parser.parse_args(argv)
    verify_root(ROOT)
    stamp=identifier(args.verification_id or 'final_'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'_'+uuid.uuid4().hex[:12])
    plan,syn,real=execution_plan(ROOT,stamp)
    directory=reserve_verification(ROOT,stamp,plan,syn,real)
    before=snapshot();context=verification_context(ROOT)
    with (directory/'verification_request.json').open('xb') as stream:
        stream.write(encode({'verification_id':stamp,'status':'RESERVED_NOT_COMPLETE','source_tree_hash':before['sha256'],'verification_context':context}))
    env=os.environ.copy();env['PYTHONPATH']=str(ROOT/'src');env['PYTHONUTF8']='1';env['PYTHONDONTWRITEBYTECODE']='1';env['MPLCONFIGDIR']=str(safe_child(ROOT,'.cache/matplotlib'))
    python=sys.executable;executions=[]
    def run(name,args):
        command=[python,str(ROOT/'tools/session_exec.py'),stamp+'_'+name,*args]
        result=subprocess.run(command,cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf-8')
        print(result.stdout,flush=True)
        if result.returncode:print(result.stderr,flush=True)
        raw=(ROOT/'state/executions'/ (stamp+'_'+name)/'execution.json').read_bytes()
        rec=json.loads(raw)
        executions.append({'name':name,'ref':f'state/executions/{stamp}_{name}/execution.json',**rec,'execution_record_sha256':digest(raw)})
        if result.returncode!=0:raise RuntimeError('Final command failed: '+name)
    try:
        for name,command in plan:run(name,command)
        comparisons=canonical_comparisons(ROOT,syn,real)
        after=snapshot()
        if before!=after or not all(x['source_unchanged'] and x['source_tree_hash']==before['sha256'] and x['exit_code']==0 for x in executions):raise ValueError('Source changed or command evidence failed during final validation')
        if context!=verification_context(ROOT):raise ValueError('Verification environment/config changed')
        if not all(x['config_hash']==context['config_sha256'] and x['environment_hash']==context['environment_manifest_sha256'] and x['lock_hash']==context['lock_sha256'] for x in executions):raise ValueError('Execution context mismatch')
        manifest=artifact_manifest(ROOT,stamp,before['sha256'],context,real[1],syn[1])
        repro={'verification_id':stamp,'source_tree_hash':before['sha256'],'verification_context':context,'real_runs':real,'synthetic_runs':syn,'comparisons':comparisons,'status':'MATCH','scope':'Actual QC tables and synthetic tables only; no real entry validation'}
        summary={'verification_id':stamp,'verification_status':'COMPLETED','source_tree_hash':before['sha256'],'verification_context':context,'implemented_checks_passed':True,'engineering_status':'PARTIAL','full_spec_verified':False,'executions':executions,'real_run':real[1],'synthetic_run':syn[1],'source_snapshot':before,'native_symlink_tests':'SKIPPED_IF_HOST_PRIVILEGE_UNAVAILABLE','data_kind':'REAL_QC_AND_SEPARATE_SYNTHETIC','initial_design_pass_inherited':False,'launch_only_checks':{'verify_bundle':'Original archive checked before edits; original checksums retained, not rewritten','final_contract_checks':'Launch-only check expects no traffic source shipped; not applicable as implementation acceptance','validate_design':'Initial schema/design inspection only; not inherited as engine verification'},'artifact_manifest_sha256':digest(encode(manifest)),'reproducibility_sha256':digest(encode(repro))}
        # The complete summary is last. Failed/incomplete attempts never gain it.
        for name,value in [('run_artifact_manifest.json',manifest),('reproducibility.json',repro),('verification_summary.json',summary)]:
            with (directory/name).open('xb') as stream:stream.write(encode(value))
        print(json.dumps({'status':'IMPLEMENTED_SUBSET_PASSED','engineering':'PARTIAL','verification_dir':directory.relative_to(ROOT).as_posix(),'stamp':stamp,'real_run':real[1],'synthetic_run':syn[1]}))
    except Exception as error:
        with (directory/'verification_failure.json').open('xb') as stream:
            stream.write(encode({'verification_id':stamp,'status':'FAILED_OR_INCOMPLETE','error_type':type(error).__name__,'message':'Artifacts preserved; a fresh verification ID is required.'}))
        raise
if __name__=='__main__':main()

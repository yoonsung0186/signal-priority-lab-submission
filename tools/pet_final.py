"""Latest-source PET regression and two canonical replays, without trajectory QC."""
import argparse,csv,io,json,os,pathlib,subprocess,sys,time,uuid,importlib.metadata
import yaml
from snapshot import ROOT,snapshot,digest,environment_manifest_ref
from preflight import verify_root,safe_child
from final_verify import identifier,encode,verification_context,reserve_verification,run_artifact_files,SYNTHETIC_CANONICAL_FILES

MANDATORY={'pet_result.json','pet_qc.json','cluster_validation.json','source_inventory.json','event_ledger.csv','scenario_summary.csv','pet_thresholds.csv','signal_context.csv','timing_sensitivity.csv','inspection_candidates.csv','pet_ecdf.csv'}

def context(config_ref):
    path=safe_child(ROOT,config_ref)
    if not path.is_file() or path.stat().st_size>128*1024:raise ValueError('Invalid PET profile config')
    config=yaml.safe_load(path.read_bytes())
    if config['profile_id']!='PET_INTERACTIONS_V1' or config['data_kind']!='REAL':raise ValueError('Invalid PET profile kind')
    inputs=[]
    for item in config['inputs']:
        source=safe_child(ROOT,item['archive']);actual=digest(source.read_bytes())
        if actual!=item['sha256']:raise ValueError('PET input checksum mismatch')
        inputs.append({'archive':item['archive'],'sha256':actual,'bytes':source.stat().st_size})
    rights_ref=config['rights_manifest'];rights=safe_child(ROOT,rights_ref)
    if json.loads(rights.read_bytes())['rights']['analysis_status']!='ALLOW':raise ValueError('PET analysis rights blocked')
    environment=json.loads(safe_child(ROOT,environment_manifest_ref(ROOT)).read_bytes())
    if environment['python']!=sys.version or pathlib.Path(environment['executable']).resolve()!=pathlib.Path(sys.executable).resolve() or pathlib.Path(environment['prefix']).resolve()!=pathlib.Path(sys.prefix).resolve():raise ValueError('Actual interpreter differs from environment manifest')
    if environment['requirements_lock_sha256']!=digest(safe_child(ROOT,'requirements.lock').read_bytes()):raise ValueError('Environment lock drift')
    for name,version in environment['packages'].items():
        if importlib.metadata.version(name)!=version:raise ValueError('Actual installed package differs from manifest: '+name)
    return {**verification_context(ROOT),'profile_config_ref':config_ref,'profile_config_sha256':digest(path.read_bytes()),'inputs':inputs,'rights_manifest_ref':rights_ref,'rights_manifest_sha256':digest(rights.read_bytes()),'environment_manifest_ref':environment_manifest_ref(ROOT)}

def plan(stamp,config_ref):
    python=sys.executable
    syn=[f'runs/{stamp}_synthetic_{i+1}' for i in range(2)]
    pet=[f'runs/{stamp}_pet_{i+1}' for i in range(2)]
    jobs=[('bootstrap',[python,'-m','unittest','discover','-s','tools','-p','test_*.py','-v']),('implementation',[python,'-m','unittest','discover','-s','tests','-v']),('optimized_guards',[python,'-O','-m','unittest','discover','-s','tests','-p','test_ops.py','-v']),('preflight',[python,'tools/preflight.py','--root','.'])]
    jobs.extend(('synthetic_'+str(i+1),[python,'-m','spl','synthetic','--output',run]) for i,run in enumerate(syn))
    jobs.extend(('pet_'+str(i+1),[python,'-m','spl','pet-analyze','--profile',config_ref,'--output',run]) for i,run in enumerate(pet))
    return jobs,syn,pet

def canonical_files(run):
    manifest=json.loads(safe_child(ROOT,run+'/run_manifest.json').read_bytes())
    files=manifest['canonical_files']
    if not isinstance(files,list) or len(files)!=len(set(files)) or not MANDATORY.issubset(files):raise ValueError('Incomplete PET canonical contract')
    for name in files:
        p=pathlib.PurePosixPath(name)
        if p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name:raise ValueError('Unsafe canonical artifact')
        safe_child(ROOT,run+'/'+name)
    return sorted(files)

def comparison(kind,name,a,b):
    raw_a=a.read_bytes();raw_b=b.read_bytes();byte_equal=raw_a==raw_b
    if name.endswith('.json'):semantic=json.loads(raw_a)==json.loads(raw_b)
    elif name.endswith('.csv'):semantic=raw_a.decode('utf-8').replace('\r\n','\n')==raw_b.decode('utf-8').replace('\r\n','\n')
    elif name.endswith('.parquet'):
        import pyarrow.parquet as pq
        semantic=pq.read_table(a).equals(pq.read_table(b))
    else:raise ValueError('Unsupported canonical type')
    return {'kind':kind,'file':name,'byte_equal':byte_equal,'semantic_equal':semantic,'equal':byte_equal and semantic,'sha256':digest(raw_a)}

def validate_provenance(run,source,ctx):
    provenance=json.loads(safe_child(ROOT,run+'/provenance.json').read_bytes())
    result=json.loads(safe_child(ROOT,run+'/pet_result.json').read_bytes())
    expected={'source_snapshot':source,'config_ref':ctx['profile_config_ref'],'config_sha256':ctx['profile_config_sha256'],'environment_manifest_ref':ctx['environment_manifest_ref'],'environment_manifest_sha256':ctx['environment_manifest_sha256'],'lock_sha256':ctx['lock_sha256'],'rights_manifest_ref':ctx['rights_manifest_ref'],'rights_manifest_sha256':ctx['rights_manifest_sha256']}
    if any(provenance.get(k)!=v for k,v in expected.items()):raise ValueError('PET provenance context mismatch')
    if provenance['inputs']!=ctx['inputs'] or result['input_sha256s']!=[i['sha256'] for i in ctx['inputs']]:raise ValueError('PET provenance input mismatch')
    if result.get('profile_id')!='PET_INTERACTIONS_V1' or result.get('data_kind')!='REAL' or result.get('mode')!='pet-analyze':raise ValueError('PET result kind mismatch')
    if result.get('pet_qc_status')!='COMPLETED_PROVIDER_DERIVED_QC' or result.get('pet_analysis_status')!='COMPLETED_DESCRIPTIVE_BASELINE':raise ValueError('PET analysis incomplete')
    if provenance['status']!=result:raise ValueError('PET result/provenance mismatch')
    ai=json.loads(safe_child(ROOT,run+'/cluster_validation.json').read_bytes())
    fits=ai['fit_call_count']
    if result['fit_call_count']!=fits or provenance['fit_call_count']!=fits:raise ValueError('AI fit count mismatch')
    if ai['status'].startswith('FITTED_'):
        if result['ai_status']!='EXPLORATORY_ANALYSIS_PERFORMED' or fits<=0:raise ValueError('AI fitted status mismatch')
    elif ai['status']!='NOT_RUN' or fits!=0 or not ai.get('reason'):raise ValueError('AI attempted failure/incomplete run cannot pass final gate')
    return result

def run_manifest(selected,stamp,source,ctx):
    files=[]
    for role,run in selected.items():
        base=ROOT/run
        for file in run_artifact_files(ROOT,run):
            data=file.read_bytes();files.append({'role':role,'run':run,'path':file.relative_to(base).as_posix(),'bytes':len(data),'sha256':digest(data)})
    return {'verification_id':stamp,'source_tree_hash':source['sha256'],'context':ctx,'selected_runs':selected,'artifacts':files}

def validate_selection(reference):
    file=safe_child(ROOT,reference)
    if file.name!='verification_summary.json' or file.parent.parent!=ROOT/'state/verifications':raise ValueError('Explicit versioned verification summary required')
    identifier(file.parent.name)
    if safe_child(ROOT,(file.parent/'verification_failure.json').relative_to(ROOT).as_posix()).exists():raise ValueError('Failed verification')
    raw={name:safe_child(ROOT,(file.parent/name).relative_to(ROOT).as_posix()).read_bytes() for name in ('verification_summary.json','reproducibility.json','run_artifact_manifest.json')}
    summary=json.loads(raw['verification_summary.json']);repro=json.loads(raw['reproducibility.json']);manifest=json.loads(raw['run_artifact_manifest.json'])
    if summary.get('profile')!='pet-interactions-v1' or summary.get('verification_status')!='COMPLETED' or summary.get('implemented_checks_passed') is not True:raise ValueError('Incomplete PET verification')
    if summary['verification_id']!=file.parent.name or summary['source_snapshot']!=snapshot() or summary['context']!=context(summary['context']['profile_config_ref']):raise ValueError('Stale PET source/config/environment/input')
    for name,key in [('reproducibility.json','reproducibility_sha256'),('run_artifact_manifest.json','artifact_manifest_sha256')]:
        if digest(raw[name])!=summary[key]:raise ValueError('Verification metadata modified')
    jobs,syn,pet=plan(summary['verification_id'],summary['context']['profile_config_ref'])
    entries=summary['executions'];expected=dict(jobs)
    if len(entries)!=len(jobs) or {e['name'] for e in entries}!=set(expected):raise ValueError('Missing required execution')
    for e in entries:
        if e['command']!=expected[e['name']] or e['exit_code']!=0 or not e['source_unchanged'] or e['source_tree_hash']!=summary['source_snapshot']['sha256']:raise ValueError('Invalid execution evidence')
        if any(e[k]!=summary['context'][v] for k,v in [('config_hash','config_sha256'),('environment_hash','environment_manifest_sha256'),('lock_hash','lock_sha256')]):raise ValueError('Execution context mismatch')
        ref=f"state/executions/{summary['verification_id']}_{e['name']}/execution.json"
        if e['ref']!=ref:raise ValueError('Execution reference mismatch')
        record=safe_child(ROOT,ref).read_bytes()
        if digest(record)!=e['execution_record_sha256'] or json.loads(record)!={k:v for k,v in e.items() if k not in {'name','ref','execution_record_sha256'}}:raise ValueError('Execution record mismatch')
        for name,key in [('stdout.log','stdout_sha256'),('stderr.log','stderr_sha256')]:
            if digest(safe_child(ROOT,str(pathlib.PurePosixPath(ref).parent/name)).read_bytes())!=e[key]:raise ValueError('Execution log modified')
    selected={'pet':pet[1],'synthetic':syn[1]}
    if summary['pet_run']!=pet[1] or summary['synthetic_run']!=syn[1]:raise ValueError('Selected run mismatch')
    if repro.get('verification_id')!=summary['verification_id'] or repro.get('context')!=summary['context'] or repro.get('source_tree_hash')!=summary['source_snapshot']['sha256'] or repro.get('status')!='MATCH' or repro.get('pet_runs')!=pet or repro.get('synthetic_runs')!=syn:raise ValueError('Replay context mismatch')
    if validate_provenance(pet[1],summary['source_snapshot'],summary['context'])!=summary['result']:raise ValueError('Selected result mismatch')
    if json.loads(safe_child(ROOT,syn[1]+'/result.json').read_bytes())['data_kind']!='SYNTHETIC':raise ValueError('Synthetic kind mismatch')
    if manifest['selected_runs']!=selected or manifest['context']!=summary['context'] or manifest['source_tree_hash']!=summary['source_snapshot']['sha256']:raise ValueError('Manifest context mismatch')
    current=run_manifest(selected,summary['verification_id'],summary['source_snapshot'],summary['context'])
    if current!=manifest:raise ValueError('Selected artifacts modified')
    expected_comp={('PET',p) for p in canonical_files(pet[1])}|{('SYNTHETIC',p) for p in SYNTHETIC_CANONICAL_FILES}
    comparisons=repro['comparisons']
    if len(comparisons)!=len(expected_comp) or {(c['kind'],c['file']) for c in comparisons}!=expected_comp or not all(c['byte_equal'] and c['semantic_equal'] for c in comparisons):raise ValueError('Incomplete canonical evidence')
    for c in comparisons:
        run=selected['pet' if c['kind']=='PET' else 'synthetic']
        if digest(safe_child(ROOT,run+'/'+c['file']).read_bytes())!=c['sha256']:raise ValueError('Canonical artifact modified')
    return summary,manifest,raw

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--profile',required=True,choices=['pet-interactions-v1']);p.add_argument('--verification-id');p.add_argument('--profile-config',default='configs/pet_interactions_v1.yaml');args=p.parse_args(argv)
    verify_root(ROOT);stamp=identifier(args.verification_id or 'pet_'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'_'+uuid.uuid4().hex[:8])
    jobs,syn,pet=plan(stamp,args.profile_config)
    directory=reserve_verification(ROOT,stamp,jobs,syn,pet)
    try:source=snapshot();ctx=context(args.profile_config)
    except Exception as error:
        with (directory/'verification_failure.json').open('xb') as f:f.write(encode({'status':'FAILED_OR_INCOMPLETE','error_type':type(error).__name__,'reason':str(error)}))
        raise
    with (directory/'verification_request.json').open('xb') as f:f.write(encode({'profile':args.profile,'verification_id':stamp,'context':ctx,'source_snapshot':source}))
    env=os.environ.copy();env.update(PYTHONPATH=str(ROOT/'src'),PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1',MPLCONFIGDIR=str(safe_child(ROOT,'.cache/matplotlib')),OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1')
    executions=[]
    try:
        for name,command in jobs:
            result=subprocess.run([sys.executable,str(ROOT/'tools/session_exec.py'),stamp+'_'+name,*command],cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf-8')
            print(result.stdout,flush=True)
            ref=f'state/executions/{stamp}_{name}/execution.json';raw=safe_child(ROOT,ref).read_bytes();record=json.loads(raw)
            executions.append({'name':name,'ref':ref,**record,'execution_record_sha256':digest(raw)})
            if result.returncode or record['exit_code'] or not record['source_unchanged'] or record['source_tree_hash']!=source['sha256']:raise ValueError('Required command failed or source changed: '+name)
            if any(record[k]!=ctx[v] for k,v in [('config_hash','config_sha256'),('environment_hash','environment_manifest_sha256'),('lock_hash','lock_sha256')]):raise ValueError('Execution context mismatch')
        for run in pet:validate_provenance(run,source,ctx)
        files=canonical_files(pet[0])
        if files!=canonical_files(pet[1]):raise ValueError('Canonical membership changed')
        comparisons=[comparison(kind,name,safe_child(ROOT,runs[0]+'/'+name),safe_child(ROOT,runs[1]+'/'+name)) for kind,runs,names in [('PET',pet,files),('SYNTHETIC',syn,SYNTHETIC_CANONICAL_FILES)] for name in names]
        if not all(c['equal'] for c in comparisons):raise ValueError('Canonical PET replay mismatch')
        if snapshot()!=source or context(args.profile_config)!=ctx:raise ValueError('Final context changed')
        manifest=run_manifest({'pet':pet[1],'synthetic':syn[1]},stamp,source,ctx)
        repro={'verification_id':stamp,'profile':args.profile,'context':ctx,'source_tree_hash':source['sha256'],'pet_runs':pet,'synthetic_runs':syn,'comparisons':comparisons,'status':'MATCH'}
        summary={'verification_id':stamp,'profile':args.profile,'verification_status':'COMPLETED','implemented_checks_passed':True,'source_snapshot':source,'context':ctx,'executions':executions,'pet_run':pet[1],'synthetic_run':syn[1],'result':json.loads((ROOT/pet[1]/'pet_result.json').read_bytes()),'v3_full_spec_verified':False,'artifact_manifest_sha256':digest(encode(manifest)),'reproducibility_sha256':digest(encode(repro))}
        for name,value in [('run_artifact_manifest.json',manifest),('reproducibility.json',repro),('verification_summary.json',summary)]:
            with (directory/name).open('xb') as f:f.write(encode(value))
        print(json.dumps({'status':'PET_IMPLEMENTED_SCOPE_VERIFIED','verification':(directory/'verification_summary.json').relative_to(ROOT).as_posix(),'pet_run':pet[1],'source_tree_hash':source['sha256']}))
    except Exception as error:
        with (directory/'verification_failure.json').open('xb') as f:f.write(encode({'status':'FAILED_OR_INCOMPLETE','error_type':type(error).__name__,'reason':str(error)}))
        raise
if __name__=='__main__':main()

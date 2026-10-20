"""Allowlist-only local partial handoff; raw inputs and environments never packaged."""
import argparse,json,pathlib,zipfile,hashlib,subprocess,os,sys
from snapshot import ROOT,snapshot,source_files,digest
from preflight import verify_root,safe_child
from final_verify import identifier,run_artifact_files,verification_context,execution_plan,REAL_CANONICAL_FILES,SYNTHETIC_CANONICAL_FILES

def encode(value):return (json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n').encode('utf-8')
def verify_derived_rights(acquisition):
    if acquisition.get('rights',{}).get('share_derived_status')!='ALLOW':raise ValueError('Derived handoff rights blocked')
def valid_member(name):
    p=pathlib.PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name or not name:raise ValueError('Unsafe ZIP path')
    if set(p.parts)&{'.git','.venv','.venvs','.cache','raw','__pycache__'} or p.name.startswith('.env') or p.suffix in {'.pem','.key','.ttf','.otf','.zip'}:raise ValueError('Forbidden ZIP member')
def add_file(files,name,path):
    valid_member(name)
    safe_child(ROOT,path.relative_to(ROOT).as_posix())
    if name in files:raise ValueError('Duplicate archive path')
    if path.stat().st_size>20*1024**2:raise ValueError('Artifact size limit')
    files[name]=path.read_bytes()
def prepare_zip(files):
    files=dict(files)
    if 'manifest.json' in files:raise ValueError('Reserved archive manifest path')
    entries=[]
    for name,b in sorted(files.items()):
        valid_member(name)
        if not isinstance(b,bytes):raise ValueError('Archive content must be bytes')
        entries.append({'path':name,'bytes':len(b),'sha256':digest(b),'rights':'ALLOW','role':'technical_source_or_permitted_derived_artifact'})
    files['manifest.json']=encode({'files':entries,'manifest_self_hash':'EXCLUDED_SELF_REFERENCE','raw_inputs_included':False})
    if sum(map(len,files.values()))>100*1024**2:raise ValueError('Package size limit')
    return files

def write_zip(path,files):
    files=prepare_zip(files)
    with zipfile.ZipFile(path,'x',zipfile.ZIP_DEFLATED) as z:
        for n,b in sorted(files.items()):z.writestr(n,b)
    with zipfile.ZipFile(path) as z:
        if z.testzip() is not None:raise ValueError('Archive CRC failed')
        m=json.loads(z.read('manifest.json'))
        if set(z.namelist())!={x['path'] for x in m['files']}|{'manifest.json'}:raise ValueError('Manifest membership failed')
        for e in m['files']:
            b=z.read(e['path'])
            if len(b)!=e['bytes'] or digest(b)!=e['sha256']:raise ValueError('Manifest content failed')

def load_verification(root,verification_dir=None,legacy=False):
    if legacy:
        if verification_dir is not None:raise ValueError('Select one verification source')
        directory=safe_child(root,'state')
    else:
        if not verification_dir:raise ValueError('Explicit verification selection required')
        path=pathlib.Path(verification_dir)
        if len(path.parts)!=3 or path.parts[:2]!=('state','verifications'):raise ValueError('Unsafe verification selection')
        identifier(path.parts[2])
        directory=safe_child(root,verification_dir)
    if safe_child(root,(directory/'verification_failure.json').relative_to(root).as_posix()).exists():raise ValueError('Failed verification selection')
    raw={}
    for name in ['verification_summary.json','reproducibility.json','run_artifact_manifest.json']:
        file=safe_child(root,(directory/name).relative_to(root).as_posix())
        if not file.is_file():raise ValueError('Incomplete verification or legacy verification lacks complete artifact manifest; run fresh final verification')
        if file.stat().st_size>20*1024**2:raise ValueError('Verification metadata size limit')
        raw[name]=file.read_bytes()
    loaded={name:json.loads(content) for name,content in raw.items()}
    return {'directory':directory.relative_to(root).as_posix(),'raw':raw,
            'summary':loaded['verification_summary.json'],'repro':loaded['reproducibility.json'],
            'manifest':loaded['run_artifact_manifest.json'],'legacy':legacy}

def validate_verification(root,selection,snap,real_run,synthetic_run):
    summary,repro,manifest=selection['summary'],selection['repro'],selection['manifest']
    if summary.get('verification_status')!='COMPLETED' or summary.get('implemented_checks_passed') is not True:
        raise ValueError('Incomplete or failed verification')
    if summary.get('source_tree_hash')!=snap['sha256'] or summary.get('source_snapshot')!=snap:raise ValueError('Stale source verification')
    context=verification_context(root)
    if summary.get('verification_context')!=context:raise ValueError('Stale verification environment/config')
    verification_id=summary.get('verification_id')
    identifier(verification_id)
    if not selection['legacy'] and pathlib.Path(selection['directory']).name!=verification_id:raise ValueError('Verification ID mismatch')
    if (real_run,synthetic_run)!=(summary.get('real_run'),summary.get('synthetic_run')):raise ValueError('Run paths do not match selected verification')
    if digest(selection['raw']['reproducibility.json'])!=summary.get('reproducibility_sha256'):raise ValueError('Reproducibility metadata hash mismatch')
    if digest(selection['raw']['run_artifact_manifest.json'])!=summary.get('artifact_manifest_sha256'):raise ValueError('Artifact manifest hash mismatch')
    if repro.get('verification_id')!=verification_id or repro.get('source_tree_hash')!=snap['sha256'] or repro.get('verification_context')!=context or repro.get('status')!='MATCH':raise ValueError('Stale or mismatched reproducibility metadata')
    if real_run not in repro.get('real_runs',[]) or synthetic_run not in repro.get('synthetic_runs',[]):raise ValueError('Reproducibility selected run mismatch')
    executions=summary.get('executions')
    if not isinstance(executions,list) or not executions:raise ValueError('Missing verification command evidence')
    plan,expected_syn,expected_real=execution_plan(root,verification_id)
    required_names={name for name,_ in plan}
    if len(executions)!=len(required_names) or {entry.get('name') for entry in executions}!=required_names:raise ValueError('Incomplete verification command set')
    if repro.get('real_runs')!=expected_real or repro.get('synthetic_runs')!=expected_syn:raise ValueError('Incomplete replay run set')
    if (real_run,synthetic_run)!=(expected_real[1],expected_syn[1]):raise ValueError('Selected run is not the verified final replay')
    for entry in executions:
        if entry.get('exit_code')!=0 or entry.get('source_unchanged') is not True or entry.get('source_tree_hash')!=snap['sha256']:raise ValueError('Failed or stale verification command')
        if any(entry.get(field)!=context[key] for field,key in [('config_hash','config_sha256'),('environment_hash','environment_manifest_sha256'),('lock_hash','lock_sha256')]):raise ValueError('Verification command context mismatch')
        ref=entry.get('ref','')
        if ref!=f"state/executions/{verification_id}_{entry['name']}/execution.json":raise ValueError('Execution evidence ID mismatch')
        path=safe_child(root,ref)
        if not path.is_relative_to(root/'state/executions') or path.name!='execution.json':raise ValueError('Unsafe execution evidence selection')
        raw=path.read_bytes()
        if digest(raw)!=entry.get('execution_record_sha256'):raise ValueError('Execution record changed after verification')
        record=json.loads(raw)
        if record!={key:value for key,value in entry.items() if key not in {'name','ref','execution_record_sha256'}}:raise ValueError('Execution record and summary disagree')
        if record.get('command')!=dict(plan)[entry['name']]:raise ValueError('Execution command differs from required verification plan')
        for name,field in [('stdout.log','stdout_sha256'),('stderr.log','stderr_sha256')]:
            log=safe_child(root,(path.parent/name).relative_to(root).as_posix())
            if digest(log.read_bytes())!=entry.get(field):raise ValueError('Execution log changed after verification')
    selected={'real_qc':real_run,'synthetic':synthetic_run}
    if manifest.get('verification_id')!=verification_id or manifest.get('source_tree_hash')!=snap['sha256'] or manifest.get('verification_context')!=context or manifest.get('selected_runs')!=selected or manifest.get('all_selected_packaged_run_artifacts') is not True:raise ValueError('Artifact manifest verification mismatch')
    entries=manifest.get('artifacts')
    if not isinstance(entries,list) or not entries:raise ValueError('Empty verification artifact manifest')
    expected={}
    for entry in entries:
        role=entry.get('role');relative=entry.get('path');run=entry.get('run')
        if role not in selected or run!=selected[role] or not isinstance(relative,str):raise ValueError('Invalid artifact manifest entry')
        valid_member(role+'/'+relative)
        key=(role,relative)
        if key in expected:raise ValueError('Duplicate verification artifact')
        expected[key]=entry
    actual={};files={}
    for role,run in selected.items():
        base=safe_child(root,run)
        for file in run_artifact_files(root,run):
            relative=file.relative_to(base).as_posix();key=(role,relative)
            if key not in expected:raise ValueError('Unverified selected run artifact added')
            raw=file.read_bytes();entry=expected[key]
            if len(raw)>20*1024**2:raise ValueError('Artifact size limit')
            if len(raw)!=entry.get('bytes') or digest(raw)!=entry.get('sha256'):raise ValueError('Selected run artifact changed after verification')
            actual[key]=True;files[role+'/'+relative]=raw
    if set(actual)!=set(expected):raise ValueError('Selected run artifact missing')
    comparisons=repro.get('comparisons')
    required_comparisons={('REAL_QC',name) for name in REAL_CANONICAL_FILES}|{('SYNTHETIC',name) for name in SYNTHETIC_CANONICAL_FILES}
    if not isinstance(comparisons,list) or len(comparisons)!=len(required_comparisons) or {(item.get('kind'),item.get('file')) for item in comparisons}!=required_comparisons:raise ValueError('Missing or incomplete canonical comparison evidence')
    for item in comparisons:
        role={'REAL_QC':'real_qc','SYNTHETIC':'synthetic'}[item['kind']]
        if item.get('equal') is not True or (role,item.get('file')) not in expected or item.get('sha256')!=expected[(role,item['file'])]['sha256']:raise ValueError('Canonical artifact verification mismatch')
    return files

def worker_evidence_files(root):
    """Keep mocked resume fixtures local; package actual command records/logs."""
    base=safe_child(root,'worker_evidence');files=[]
    if not base.is_dir():return files
    def fail(error):raise error
    for directory,dirs,names in os.walk(base,topdown=True,followlinks=False,onerror=fail):
        if pathlib.Path(directory)==base:
            dirs[:]=[name for name in dirs if not name.startswith(('resume_unit_fixtures_','pet_unit_fixtures_','finish_unit_fixtures_','pet_bulk_unit_fixtures_'))]
        for name in dirs:safe_child(root,(pathlib.Path(directory)/name).relative_to(root).as_posix())
        for name in names:
            file=safe_child(root,(pathlib.Path(directory)/name).relative_to(root).as_posix())
            if file.is_file() and file.suffix in {'.json','.log'}:files.append(file)
    return sorted(files)

def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    if '--profile' in argv:
        from pet_delivery import main as pet_main
        return pet_main(argv)
    p=argparse.ArgumentParser();p.add_argument('--run-id',required=True);p.add_argument('--real-run',required=True);p.add_argument('--synthetic-run',required=True)
    chosen=p.add_mutually_exclusive_group(required=True)
    chosen.add_argument('--verification',help='Explicit project-relative verification_summary.json file')
    chosen.add_argument('--verification-dir',help='Explicit state/verifications/<id> with complete artifact manifest')
    chosen.add_argument('--legacy-verification',action='store_true',help='Explicit legacy selection; legacy evidence without complete artifact manifest is rejected')
    a=p.parse_args(argv);verify_root(ROOT)
    identifier(a.run_id)
    out=safe_child(ROOT,'delivery/'+a.run_id)
    if out.exists():raise FileExistsError('Delivery collision; use a fresh run ID')
    verification_dir=a.verification_dir
    if a.verification:
        selected=safe_child(ROOT,a.verification)
        if selected.name!='verification_summary.json':raise ValueError('Verification summary file required')
        verification_dir=selected.parent.relative_to(ROOT).as_posix()
    snap=snapshot();selection=load_verification(ROOT,verification_dir,a.legacy_verification)
    verification=selection['summary']
    verified_artifacts=validate_verification(ROOT,selection,snap,a.real_run,a.synthetic_run)
    acquisition=json.loads(safe_child(ROOT,'state/data_acquisition_manifest.json').read_text())
    verify_derived_rights(acquisition)
    src={};tech=dict(verified_artifacts)
    source_entries={entry['path']:entry for entry in snap['files']}
    for f in source_files():
        name=f.relative_to(ROOT).as_posix();add_file(src,name,f)
        entry=source_entries.get(name)
        if not entry or len(src[name])!=entry['bytes'] or digest(src[name])!=entry['sha256']:raise ValueError('Source archive bytes differ from verified snapshot')
    if set(src)!=set(source_entries):raise ValueError('Source archive membership differs from verified snapshot')
    src['source_snapshot.json']=encode(snap);tech['reproduce/source_snapshot.json']=encode(snap)
    real_provenance=json.loads(tech['real_qc/provenance.json'])
    if real_provenance['source_snapshot']['sha256']!=snap['sha256']:raise ValueError('Stale real QC')
    if real_provenance['input_sha256']!=acquisition['sha256']:raise ValueError('Input mismatch')
    if json.loads(tech['real_qc/real_qc.json'])['source_sha256']!=acquisition['sha256']:raise ValueError('QC input mismatch')
    if json.loads(tech['synthetic/result.json'])['data_kind']!='SYNTHETIC':raise ValueError('Synthetic result kind mismatch')
    for name,raw in selection['raw'].items():tech['validation/'+name]=raw
    for name in ['runtime_roles.json','environment_manifest.json','source_inventory.json','data_acquisition_manifest.json','contest_requirements.json','data_readiness.json','review_record.json','progress.json']:
        add_file(tech,'validation/'+name,ROOT/'state'/name)
    for f in (ROOT/'state/executions').rglob('*'):
        if f.is_file() and f.suffix in {'.json','.log'}:add_file(tech,'validation/executions/'+f.relative_to(ROOT/'state/executions').as_posix(),f)
    for f in worker_evidence_files(ROOT):
        add_file(tech,'validation/worker/'+f.relative_to(ROOT/'worker_evidence').as_posix(),f)
    for f in ['requirements.lock','README.md','configs/project.yaml']:
        add_file(tech,'reproduce/'+f,ROOT/f)
    for f in ['metadata.json','LICENSE.md','README.md','schema.md']:
        add_file(tech,'reproduce/sitpet/'+f,ROOT/'state/research/sitpet'/f)
    status={'schema_version':'1.0','template':False,'project_id':'signal-priority-lab','run_id':a.run_id,'handoff_type':'PARTIAL_TECHNICAL_HANDOFF','engineering_status':'PARTIAL','evidence_status':'REAL_EXPLORATORY','rights_for_handoff':'ALLOW','rights_for_contest':'UNKNOWN','contest_eligibility':'UNKNOWN','ai_status':'NOT_RUN','manual_review_status':'NOT_PERFORMED','report_generated':False,'external_submission_performed':False,'source_snapshot_ref':'reproduce/source_snapshot.json','data_manifest_ref':'validation/data_acquisition_manifest.json','verification_ref':'validation/verification_summary.json','artifact_manifest_ref':'manifest.json','blockers':['UNVERIFIED_REAL_ENTRY_BOUNDARY','UNVERIFIED_STRAIGHT_MOVEMENT_SIGNAL_MAPPING','MAIN_RUNTIME_MODEL_UNVERIFIED'],'not_performed':['real entry events/rates','AI fitting/evaluation','real sensitivity/ranking','independent ground truth accuracy','human annotation','full FINAL v3 feature implementation','GitHub push'],'next_actions':['Supply documented entry boundary and movement-signal mapping; recheck full population coverage.','Resume using README commands with NEW run paths.'],'validation_evidence_refs':['validation/verification_summary.json','validation/reproducibility.json']}
    import jsonschema
    jsonschema.validate(status,json.loads((ROOT/'schemas/handoff_status.schema.json').read_text()))
    tech['HANDOFF_STATUS.json']=encode(status)
    tech['limitations.json']=encode({'scope':'Actual data QC plus separate synthetic engine only','omitted_raw_reason':'Raw excluded by project policy regardless of CC BY permission','omitted_unit_fixture_evidence_reason':'resume_unit_fixtures_* directories contain mocked command metadata for unit testing, preserved locally; actual test command records/logs included','no_real_data_route_scope':'No qualified core entry-analysis route; actual QC route was executed','not_performed':status['not_performed']})
    tech['data_dictionary.md']=safe_child(ROOT,'technical/data_dictionary.md').read_bytes()
    tech['technical_notes.md']=safe_child(ROOT,'README.md').read_bytes()
    for file in sorted((ROOT/'technical').glob('*.md')):
        add_file(tech,'technical/'+file.name,file)
    tech['evidence/claim_constraints.json']=encode({'REAL_INPUT_QC':'ALLOWED_MEASUREMENT','SYNTHETIC_ENGINE':'TECHNICAL_TEST_ONLY','REAL_ENTRY_RATE':'UNSUPPORTED','ACCIDENT_RISK':'UNSUPPORTED','AI_IMPROVEMENT':'UNSUPPORTED'})
    qc=json.loads(tech['real_qc/real_qc.json']);facts=[]
    for metric,unit in [('trajectory_rows','rows'),('unique_object_ids','provider_object_ids')]:
        facts.append({'schema_version':'1.0','template':False,'fact_id':'QC_'+metric,'metric_name':metric,'status':'COMPUTED','value':qc[metric],'unit':unit,'numerator':qc[metric],'denominator':None,'population_definition':'All rows in publisher public example day, no event resampling','data_kind':'REAL','source_artifact_ids':['real_qc/real_qc.json'],'code_ref':'tools/real_qc.py','config_hash':digest((ROOT/'configs/project.yaml').read_bytes()),'run_id':a.run_id,'uncertainty_kind':'NOT_APPLICABLE','limitations':['Not entry counts or legal violation rates','Provider object IDs need not equal physical vehicles']})
    for fact in facts:jsonschema.validate(fact,json.loads((ROOT/'schemas/metric_fact.schema.json').read_text()))
    tech['evidence/metric_facts.jsonl']=b'\n'.join(encode(x).replace(b'\n',b'') for x in facts)+b'\n'
    tech['artifact_index.json']=encode({'artifacts':[{'artifact_id':n,'path':n} for n in sorted(tech)],'historical_attempts_included':True})
    # Complete validation and read-only Git probes precede delivery creation.
    prepare_zip(tech);prepare_zip(src)
    runtime_models=json.loads(tech['validation/runtime_roles.json'])
    git_head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    git_dirty=bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True))
    if snapshot()!=snap or verification_context(ROOT)!=verification['verification_context']:raise ValueError('Source or environment changed during packaging')
    out.mkdir(parents=True,exist_ok=False)
    archives=[]
    for prefix,files in [('SPL_TECH_HANDOFF',tech),('SPL_SOURCE',src)]:
        path=out/f'{prefix}_{a.run_id}.zip';write_zip(path,files)
        archives.append({'path':path.name,'bytes':path.stat().st_size,'sha256':digest(path.read_bytes())})
    index={'run_id':a.run_id,'files':archives,'source_snapshot':snap['sha256'],'status':status,'runtime_models':runtime_models,'verification':verification,'verification_selection':selection['directory'],'git_head':git_head,'git_dirty':git_dirty}
    with (out/'DELIVERY_INDEX.json').open('xb') as stream:stream.write(encode(index))
    checksum=''.join(f"{e['sha256']}  {e['path']}\n" for e in archives)+f"{digest((out/'DELIVERY_INDEX.json').read_bytes())}  DELIVERY_INDEX.json\n"
    with (out/'CHECKSUMS.sha256').open('x',encoding='utf-8') as stream:stream.write(checksum)
    print(json.dumps({'delivery':out.relative_to(ROOT).as_posix(),'files':archives,'source_snapshot':snap['sha256']}))
if __name__=='__main__':main()

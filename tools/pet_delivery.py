"""Package only a complete, current PET verification and permitted derived artifacts."""
import argparse,csv,io,json,subprocess
from snapshot import ROOT,snapshot,source_files,digest
from preflight import verify_root,safe_child
from final_verify import identifier
from pet_final import validate_selection,context
from package_delivery import encode,add_file,prepare_zip,write_zip,worker_evidence_files,verify_derived_rights

def add_bound(files,name,path,sha256,byte_count=None):
    add_file(files,name,path)
    if digest(files[name])!=sha256 or (byte_count is not None and len(files[name])!=byte_count):raise ValueError('Packaged bytes differ from verified evidence: '+name)

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--profile',required=True,choices=['pet-interactions-v1']);p.add_argument('--verification',required=True);p.add_argument('--run-id',required=True);a=p.parse_args(argv)
    verify_root(ROOT);identifier(a.run_id);out=safe_child(ROOT,'delivery/'+a.run_id)
    if out.exists():raise FileExistsError('Delivery collision; use a new ID')
    summary,manifest,validation=validate_selection(a.verification);snap=summary['source_snapshot'];ctx=summary['context']
    acquisition=json.loads(safe_child(ROOT,ctx['rights_manifest_ref']).read_bytes());verify_derived_rights(acquisition)
    result=summary['result'];src={};tech={};entries={e['path']:e for e in snap['files']}
    for f in source_files():
        name=f.relative_to(ROOT).as_posix();add_file(src,name,f)
        if name not in entries or digest(src[name])!=entries[name]['sha256'] or len(src[name])!=entries[name]['bytes']:raise ValueError('Source membership/content mismatch')
    if set(src)!=set(entries):raise ValueError('Source membership mismatch')
    src['source_snapshot.json']=encode(snap);tech['reproduce/source_snapshot.json']=encode(snap)
    for entry in manifest['artifacts']:add_bound(tech,entry['role']+'/'+entry['path'],safe_child(ROOT,entry['run']+'/'+entry['path']),entry['sha256'],entry['bytes'])
    for name,raw in validation.items():tech['validation/'+name]=raw
    for execution in summary['executions']:
        parent=safe_child(ROOT,execution['ref']).parent
        for name,key in [('execution.json','execution_record_sha256'),('stdout.log','stdout_sha256'),('stderr.log','stderr_sha256')]:add_bound(tech,'validation/executions/'+parent.name+'/'+name,parent/name,execution[key])
    for ref,sha in [(ctx['environment_manifest_ref'],ctx['environment_manifest_sha256']),(ctx['rights_manifest_ref'],ctx['rights_manifest_sha256'])]:
        add_bound(tech,'validation/'+ref.split('/')[-1],safe_child(ROOT,ref),sha)
    ref='state/amendments/R1_20261012T0825/runtime_roles_r1_final.json'
    add_file(tech,'validation/runtime_roles_r1.json',safe_child(ROOT,ref))
    # Historical and failed attempts are evidence, never substituted for current validation.
    for base in ('state/amendments','state/r1_20261012T0804'):
        for f in sorted(safe_child(ROOT,base).rglob('*')):
            if f.is_file() and f.suffix in {'.json','.log'}:add_file(tech,'history/'+f.relative_to(ROOT).as_posix(),f)
    for f in worker_evidence_files(ROOT):add_file(tech,'history/'+f.relative_to(ROOT).as_posix(),f)
    for directory in sorted((ROOT/'state/executions').glob('r1_*')):
        for name in ('execution.json','stdout.log','stderr.log'):
            file=directory/name
            if file.is_file():add_file(tech,'history/'+file.relative_to(ROOT).as_posix(),file)
    for directory in sorted((ROOT/'data/raw').glob('sitpet_r1_*')):
        receipt=directory/'acquisition.json'
        if receipt.is_file():add_file(tech,'history/acquisition/'+directory.name+'.json',receipt)
    for name in ('metadata.json','README.md','schema.md','LICENSE.md'):add_file(tech,'reproduce/sitpet/'+name,ROOT/'state/research/sitpet'/name)
    for ref in ('requirements.lock','README.md',ctx['profile_config_ref']):tech['reproduce/'+ref]=src[ref]
    for ref,raw in src.items():
        if ref.startswith('technical/') and ref.endswith('.md'):tech[ref]=raw
    not_performed=list(dict.fromkeys(result['not_performed']+['original straight-entry analysis','full FINAL v3 specification','GitHub push (remote unspecified)','contest eligibility verification']))
    status={'schema_version':'1.0','template':False,'project_id':'signal-priority-lab','run_id':a.run_id,'handoff_type':'PARTIAL_TECHNICAL_HANDOFF','engineering_status':'PARTIAL','profile_id':result['profile_id'],'profile_status':'IMPLEMENTED_SCOPE_VERIFIED','data_scope':result['scope'],'implemented_features':result['implemented_features'],'v3_full_spec_verified':False,'evidence_status':'REAL_EXPLORATORY','rights_for_handoff':'ALLOW','rights_for_contest':acquisition['rights'].get('contest_status','UNKNOWN'),'contest_eligibility':'UNKNOWN','ai_status':result['ai_status'],'manual_review_status':result['manual_review_status'],'report_generated':False,'external_submission_performed':False,'source_snapshot_ref':'reproduce/source_snapshot.json','data_manifest_ref':'validation/'+ctx['rights_manifest_ref'].split('/')[-1],'verification_ref':'validation/verification_summary.json','artifact_manifest_ref':'manifest.json','blockers':['UNVERIFIED_STRAIGHT_ENTRY_BOUNDARY_AND_MOVEMENT_SIGNAL_MAPPING','MULTIDAY_PROVIDER_HTTP_429','RUNTIME_MODEL_ID_UNAVAILABLE'],'not_performed':not_performed,'next_actions':['Use new verification and run IDs for every replay.','Resume multi-day acquisition only after provider access is available; preserve current evidence.'],'validation_evidence_refs':['validation/verification_summary.json','validation/reproducibility.json']}
    import jsonschema
    jsonschema.validate(status,json.loads((ROOT/'schemas/handoff_status.schema.json').read_bytes()))
    tech['HANDOFF_STATUS.json']=encode(status)
    tech['limitations.json']=encode({'population':result['population_definition'],'scope':result['scope'],'limitations':result['limitations'],'not_performed':not_performed,'raw_omitted':'Raw data excluded; obtain fixed DOI version and match input hashes.','unit_fixtures_omitted':'Mock execution metadata in resume_unit_fixtures_* and pet_unit_fixtures_* remain local. Actual test execution logs are included.','history':'Historical records preserve their original statuses and are not current acceptance evidence.'})
    tech['evidence/claim_constraints.json']=encode({'REAL_PET':'PROVIDER_DERIVED_CONDITIONAL_DESCRIPTION','SIGNAL':'OWN_CONFLICT_AREA_ENTRY_CONTEXT_ONLY','SYNTHETIC':'ENGINE_REGRESSION_ONLY','VIOLATION_OR_ACCIDENT_RISK':'UNSUPPORTED','INDEPENDENT_ACCURACY':'NOT_PERFORMED','AI_IMPROVEMENT':'UNSUPPORTED'})
    facts=[]
    def fact(name,value,numerator,denominator,unit,artifact):
        item={'schema_version':'1.0','template':False,'fact_id':'PET_'+name,'metric_name':name,'status':'COMPUTED' if value is not None else 'UNDEFINED','value':value,'unit':unit,'numerator':numerator,'denominator':denominator,'population_definition':result['population_definition'],'data_kind':'REAL','source_artifact_ids':[artifact],'code_ref':'src/spl/pet_qc.py' if artifact=='pet/pet_qc.json' else 'src/spl/pet_metrics.py','config_hash':ctx['profile_config_sha256'],'run_id':a.run_id,'uncertainty_kind':'NOT_APPLICABLE','limitations':['Provider-derived selected interactions; not all traffic or accident risk','Single-day descriptive observation; no population CI']}
        jsonschema.validate(item,json.loads(src['schemas/metric_fact.schema.json']));facts.append(item)
    for name in ('n_raw','n_negative_pet','n_valid_nonnegative_pet','n_invalid'):fact(name,result[name],result[name],None,'recorded_interactions','pet/pet_qc.json')
    rows=list(csv.DictReader(io.StringIO(tech['pet/pet_thresholds.csv'].decode('utf-8'))))
    for row in rows:
        n=int(row['n_short_pet']);d=int(row['n_valid_nonnegative_pet'])
        fact(row['scenario_id']+'_short_pet_fraction_tau_'+row['tau_s'],n/d if d else None,n,d,'fraction','pet/pet_thresholds.csv')
    tech['evidence/metric_facts.jsonl']=b''.join(json.dumps(f,ensure_ascii=False,sort_keys=True,allow_nan=False).encode('utf-8')+b'\n' for f in facts)
    tech['data_dictionary.md']=src['technical/pet_r1.md'];tech['technical_notes.md']=src['README.md']
    tech['sources.json']=encode({'inputs':ctx['inputs'],'dataset_id':'sitpet','version':'1.1','doi':'10.5281/zenodo.19231310','license':'CC-BY-4.0','acquisition_manifest_ref':status['data_manifest_ref'],'license_evidence':'reproduce/sitpet/LICENSE.md','derived_modifications':['QC','conditional descriptions','signal context','exploratory interaction types'],'reproduction_scope':'WITH_AUTHORIZED_DATA'})
    artifacts=[]
    for name,raw in sorted(tech.items()):
        rows_count=None;schema=None
        if name.endswith('.csv'):
            reader=csv.reader(io.StringIO(raw.decode('utf-8')));schema=next(reader,[]);rows_count=sum(1 for _ in reader)
        elif name.endswith('.jsonl'):rows_count=len(raw.splitlines());schema='schemas/metric_fact.schema.json' if name=='evidence/metric_facts.jsonl' else 'append-only execution records'
        elif name.endswith('.parquet'):
            import pyarrow.parquet as pq
            meta=pq.ParquetFile(io.BytesIO(raw));schema=str(meta.schema_arrow);rows_count=meta.metadata.num_rows
        artifacts.append({'artifact_id':name,'path':name,'schema':schema,'role':name.split('/')[0],'row_count':rows_count})
    tech['artifact_index.json']=encode({'artifacts':artifacts,'failed_and_historical_attempts_included':True})
    prepare_zip(tech);prepare_zip(src)
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip();dirty=bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True))
    if snapshot()!=snap or context(ctx['profile_config_ref'])!=ctx:raise ValueError('Source/context changed during packaging')
    out.mkdir(parents=True,exist_ok=False);archives=[]
    for prefix,files in [('SPL_TECH_HANDOFF',tech),('SPL_SOURCE',src)]:
        path=out/f'{prefix}_{a.run_id}.zip';write_zip(path,files);archives.append({'path':path.name,'bytes':path.stat().st_size,'sha256':digest(path.read_bytes())})
    index={'run_id':a.run_id,'files':archives,'source_snapshot':snap['sha256'],'status':status,'verification_selection':a.verification,'verification':summary,'runtime_models':json.loads(tech['validation/runtime_roles_r1.json']),'runtime_identity_verified':False,'git_head':head,'git_dirty':dirty}
    with (out/'DELIVERY_INDEX.json').open('xb') as f:f.write(encode(index))
    with (out/'CHECKSUMS.sha256').open('xb') as f:f.write((''.join(f"{e['sha256']}  {e['path']}\n" for e in archives)+digest((out/'DELIVERY_INDEX.json').read_bytes())+'  DELIVERY_INDEX.json\n').encode('ascii'))
    print(json.dumps({'delivery':out.relative_to(ROOT).as_posix(),'files':archives,'status':status,'source_snapshot':snap['sha256']}))

if __name__=='__main__':main()

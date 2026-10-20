"""Package a verified no-fit supplement; never execute analyses or upload."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile
from r2_postprocess import ROOT, source_snapshot, verify_reuse
from preflight import safe_child
from spl.pet_io import file_hash


BOUND={}
REQUIRED_R2=set(('unit_summary monthly_rank_sensitivity leave_one_month_rank_sensitivity bootstrap_rank_replicates bootstrap_rank_summary bootstrap_date_resamples period_support pair_multiplicity unit_selection signal_coverage_daily signal_missing_period_crossings signal_event_coverage_daily signal_state_transitions signal_event_changes signal_pair_tv signal_condition_pet inspection_candidates_r2').split())
REQUIRED_R2={n+'.csv' for n in REQUIRED_R2}|{n+'.json' for n in ('units_metadata','signals_metadata','figures_index','numeric_checks','reused_ai_assessment','reused_event_audit','reproducibility_context','artifact_manifest')}|{'metric_facts.jsonl'}|{'figures/'+n+'.'+ext for n in ('unit_denominators','event_state_change','common_date_rank') for ext in ('png','svg')}


def bind(path, entry):
    path=safe_child(ROOT,Path(path).relative_to(ROOT).as_posix())
    expected={'bytes':entry['bytes'],'sha256':entry['sha256']}
    if path in BOUND and BOUND[path]!=expected:raise ValueError('Conflicting expected bytes')
    BOUND[path]=expected
    return path


def read_bound(path):
    path=safe_child(ROOT,Path(path).relative_to(ROOT).as_posix())
    if path not in BOUND:raise ValueError('Unbound package input')
    data=path.read_bytes();expected=BOUND[path]
    if len(data)!=expected['bytes'] or hashlib.sha256(data).hexdigest()!=expected['sha256']:raise ValueError('Bound bytes changed')
    return data


def capture(path, expected_sha256=None):
    path=safe_child(ROOT,Path(path).relative_to(ROOT).as_posix())
    data=path.read_bytes();digest=hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and digest!=expected_sha256:raise ValueError('Controller evidence changed')
    bind(path,{'bytes':len(data),'sha256':digest})
    return data


def write_bound(path,data):
    path=safe_child(ROOT,Path(path).relative_to(ROOT).as_posix())
    with path.open('xb') as f:f.write(data)
    bind(path,{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})


def save_bound(path,value):
    write_bound(path,(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n').encode('utf-8'))


def copy_bound(source,target):write_bound(target,read_bound(source))


def archive(path,files):
    path=safe_child(ROOT,Path(path).relative_to(ROOT).as_posix())
    if sum(BOUND[p]['bytes'] for p in files.values())>100*1024**2:raise ValueError('ZIP expansion exceeds bound')
    with zipfile.ZipFile(path,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name,p in sorted(files.items()):
            relative=Path(name)
            if relative.is_absolute() or '..' in relative.parts or ':' in name or '\\' in name or p.is_symlink():raise ValueError('Unsafe archive path')
            z.writestr(name,read_bound(p))
    raw=safe_child(ROOT,path.relative_to(ROOT).as_posix()).read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        if len(z.namelist())!=len(files) or set(z.namelist())!=set(files) or z.testzip() is not None:raise ValueError('ZIP membership/CRC failure')
        for name,p in files.items():
            data=z.read(name);expected=BOUND[p]
            if len(data)!=expected['bytes'] or hashlib.sha256(data).hexdigest()!=expected['sha256']:raise ValueError('ZIP bound content mismatch')
    bind(path,{'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()});read_bound(path)


def validate_verification(vp, v):
    if (vp.parent/'failure.json').exists():raise ValueError('Verification failure marker exists')
    required={'tools','implementation','optimized','preflight','postprocess_1','postprocess_2'}
    records=v['executions']
    if len(records)!=6 or {r['name'] for r in records}!=required:raise ValueError('Incomplete required execution set')
    for r in records:
        if r['exit_code'] or not r['source_unchanged'] or r['source_tree_hash']!=v['source_tree_hash']:raise ValueError('Failed or stale execution')
        if json.loads(capture(vp.parent/(r['name']+'.execution.json')))!=r:raise ValueError('Execution record mismatch')
        for stream in ('stdout','stderr'):
            if hashlib.sha256(capture(vp.parent/(r['name']+'.'+stream+'.log'),r[stream+'_sha256'])).hexdigest()!=r[stream+'_sha256']:raise ValueError('Execution log mismatch')
        if hashlib.sha256(capture(vp.parent/'environment.json',r['environment_sha256'])).hexdigest()!=r['environment_sha256']:raise ValueError('Environment mismatch')
    rp=vp.parent/'reproducibility.json'
    repro=json.loads(capture(rp,v['reproducibility_sha256']))
    if repro['status']!='MATCH' or len(repro['run_paths'])!=2 or repro['run_paths'][-1]!=v['selected_run']:raise ValueError('Invalid selected replay')
    py=sys.executable
    expected={'tools':[py,'-m','unittest','discover','-s','tools','-p','test_*.py','-v'],
              'implementation':[py,'-m','unittest','discover','-s','tests','-v'],
              'optimized':[py,'-O','-m','unittest','discover','-s','tests','-p','test_ops.py','-v'],
              'preflight':[py,'tools/preflight.py','--root','.']}
    expected.update({'postprocess_'+str(i+1):[py,'tools/r2_postprocess.py','--out',run] for i,run in enumerate(repro['run_paths'])})
    if any(r.get('command')!=expected[r['name']] for r in records):raise ValueError('Required execution command mismatch')
    names=[x['path'] for x in repro['files']]
    if not names or len(names)!=len(set(names)):raise ValueError('Empty or duplicate canonical artifacts')
    if not REQUIRED_R2<=set(names):raise ValueError('Missing required R2 artifacts')
    if len(set(repro['run_paths']))!=2:raise ValueError('Distinct replay paths required')
    source=json.loads(capture(vp.parent/'source_snapshot.json'))
    if source!=source_snapshot() or source['sha256']!=v['source_tree_hash']:raise ValueError('Source snapshot mismatch')
    for x in source['files']:bind(safe_child(ROOT,x['path']),x)
    capture(ROOT/'requirements-r2.lock',source['r2_lock']['sha256'])
    config_path=ROOT/'configs/r2_postprocess.json';config=json.loads(read_bound(config_path))
    config_hash=BOUND[config_path]['sha256']
    if any(r.get('config_sha256')!=config_hash for r in records):raise ValueError('Execution config mismatch')
    reuse_hash=file_hash(safe_child(ROOT,config['reuse_manifest']))
    capture(safe_child(ROOT,config['reuse_manifest']),reuse_hash)
    for runref in repro['run_paths']:
        run=safe_child(ROOT,runref)
        actual=sorted(p.relative_to(run).as_posix() for p in run.rglob('*') if p.is_file())
        if actual!=sorted(names):raise ValueError('Verified artifact membership changed')
        for x in repro['files']:
            p=safe_child(ROOT,runref+'/'+x['path'])
            if not x['byte_equal'] or p.stat().st_size!=x['bytes'] or file_hash(p)!=x['sha256']:raise ValueError('Verified artifact bytes changed')
            bind(p,x)
        context=json.loads(read_bound(run/'reproducibility_context.json'))
        if context.get('source')!=source or context.get('config')!=config or context.get('config_sha256')!=config_hash or context.get('reuse_manifest_sha256')!=reuse_hash or context.get('postprocess_source_commit')!=v['source_commit']:
            raise ValueError('Replay context binding mismatch')
        if context.get('actual_fit_before')!=52 or context.get('actual_fit_after')!=52 or context.get('actual_new_fits')!=0 or context.get('baseline_full_replay_in_r2') is not False or context.get('independent_accuracy','MISSING') is not None or context.get('manual_ground_truth')!='NOT_PERFORMED':raise ValueError('Replay fit context mismatch')
        manifest=json.loads(read_bound(run/'artifact_manifest.json'))
        expected={x['path']:{'bytes':x['bytes'],'sha256':x['sha256']} for x in repro['files'] if x['path']!='artifact_manifest.json'}
        actual_manifest={x['path']:{'bytes':x['bytes'],'sha256':x['sha256']} for x in manifest['files']}
        if len(actual_manifest)!=len(manifest['files']) or actual_manifest!=expected:raise ValueError('Artifact manifest binding mismatch')
        numeric=json.loads(read_bound(run/'numeric_checks.json'))
        if numeric.get('status')!='MATCH' or not all(numeric.get(k) is True for k in ('baseline_unchanged','A_baseline_counts_and_thresholds','signal_condition_partition_matches_A')):raise ValueError('Numeric proof incomplete')
    request=json.loads(capture(vp.parent/'verification_request.json'))
    if request.get('commands')!=[[r['name'],r['command']] for r in records] or request.get('run_paths')!=repro['run_paths'] or request.get('source_tree_hash')!=source['sha256'] or request.get('actual_fit_before')!=52 or request.get('new_fit_budget')!=0 or request.get('baseline_full_replay_in_r2') is not False:raise ValueError('Verification request mismatch')
    environment=json.loads(read_bound(vp.parent/'environment.json'))
    if environment.get('r2_lock_sha256')!=source['r2_lock']['sha256']:raise ValueError('Environment lock mismatch')
    return repro


def validate_package_context(vp,v,repro):
    if vp.name!='verification_summary.json' or vp.parent.parent!=ROOT/'state/verifications' or not vp.parent.name.replace('_','').replace('-','').isalnum():raise ValueError('Verification location mismatch')
    if v.get('implemented_checks_passed') is not True or v.get('actual_fit_before')!=52 or v.get('actual_fit_after')!=52 or v.get('actual_new_fits')!=0 or v.get('baseline_full_replay_in_r2') is not False:raise ValueError('Verification completion context mismatch')
    if repro['run_paths']!=['runs/'+vp.parent.name+'_1','runs/'+vp.parent.name+'_2']:raise ValueError('Verification run identity mismatch')


def main():
    p=argparse.ArgumentParser();p.add_argument('--verification',required=True);p.add_argument('--review',required=True);p.add_argument('--run-id',required=True);a=p.parse_args()
    if not a.run_id.replace('_','').replace('-','').isalnum():raise ValueError('Invalid run ID')
    BOUND.clear()
    vp=safe_child(ROOT,a.verification);review_path=safe_child(ROOT,a.review)
    v=json.loads(capture(vp));review=json.loads(capture(review_path))
    if v['status']!='COMPLETED_R2_NO_FIT' or v['actual_new_fits']!=0:raise ValueError('No completed no-fit verification')
    if not review['review_completed'] or review['blocking_remaining'] or review['major_remaining']:raise ValueError('Review unresolved')
    if review.get('source_tree_hash')!=v['source_tree_hash'] or review.get('verification_summary_sha256')!=BOUND[vp]['sha256'] or not review.get('files_actually_read'):
        raise ValueError('Review is not bound to this source and verification')
    repro=validate_verification(vp,v);validate_package_context(vp,v,repro)
    source=source_snapshot()
    if source['sha256']!=v['source_tree_hash'] or source['r2_lock']['sha256']!=v['r2_lock_sha256']:raise ValueError('Source drift')
    config=json.loads(read_bound(ROOT/'configs/r2_postprocess.json'));reuse=verify_reuse(config)
    for x in reuse['entries']:bind(safe_child(ROOT,x['path']),x)
    run=safe_child(ROOT,v['selected_run']);manifest=json.loads(read_bound(run/'artifact_manifest.json'))
    for f in manifest['files']:
        if file_hash(run/f['path'])!=f['sha256']:raise ValueError('Verified output drift')
    dest=safe_child(ROOT,'delivery/'+a.run_id);dest.mkdir(parents=True,exist_ok=False);writer=dest/'writer_input';writer.mkdir()
    files={};omitted=[]
    def include(path,name):
        if name in files:raise ValueError('Duplicate package path')
        if path not in BOUND:raise ValueError('Unbound inclusion '+name)
        files[name]=path
    for path in sorted(run.rglob('*')):
        if not path.is_file():continue
        rel=path.relative_to(run).as_posix()
        if path.name=='unit_selection.csv' or BOUND[path]['bytes']>20*1024**2:
            omitted.append({'path':path.relative_to(ROOT).as_posix(),**BOUND[path],'reason':'Large event-level derived evidence preserved locally; no raw payload in handoff'})
        else:include(path,'analysis/'+rel)
    proof_names={'verification_summary.json','source_snapshot.json','environment.json','reproducibility.json','verification_request.json'}|{r['name']+suffix for r in v['executions'] for suffix in ('.execution.json','.stdout.log','.stderr.log')}
    for name in sorted(proof_names):include(vp.parent/name,'verification/'+name)
    fixed={'independent_review.json':a.review,'baseline_reuse_manifest.json':'state/r2_20261013/baseline_reuse_manifest_v2.json',
           'provider_comparison.json':'state/r2_20261013/provider_comparison.json','runtime_roles.json':'state/r2_20261013/runtime_roles.json',
           'chatgpt_access_update.json':'state/r2_20261013/access_update.json','REQUIREMENTS_R2.json':'submission/REQUIREMENTS_R2.json',
           'REQUIREMENTS_R2.md':'submission/REQUIREMENTS_R2.md','R2_METHODS.md':'technical/R2_METHODS.md',
           'FINDINGS.md':'state/r2_20261013/FINDINGS.md','limitations.md':'state/r2_20261013/limitations.md',
           'source_commit_binding.json':'state/r2_20261013/source_commit_binding_final.json',
           'R2_DATA_DICTIONARY.md':'technical/R2_DATA_DICTIONARY.md'}
    for name,ref in fixed.items():
        path=safe_child(ROOT,ref)
        if path not in BOUND:capture(path)  # supplemental control metadata only
        include(path,name)
    for name in ['scenario_summary.csv','pet_thresholds.csv','inspection_candidates.csv','cluster_model.json','cluster_validation.json']:
        include(safe_child(ROOT,config['baseline_run']+'/'+name),'baseline_reused/'+name)
    for name in ['event_audit.json','ai_assessment.json']:
        include(safe_child(ROOT,config['baseline_evidence_run']+'/'+name),'baseline_reused/'+name)
    for ref in review.get('worker_evidence_files',[]):
        path=safe_child(ROOT,ref)
        if path.suffix not in {'.json','.log','.md'}:raise ValueError('Unexpected worker evidence type')
        capture(path);include(path,'worker_execution/'+path.parent.name+'/'+path.name)
    save_bound(writer/'local_omissions.json',{'files':omitted,'raw_archives_and_cache':'EXCLUDED; authorized data and preserved baseline required'})
    include(writer/'local_omissions.json','local_omissions.json')
    tag='technical-'+a.run_id.replace('_','-');repo='https://github.com/yoonsung0186/signal-priority-lab'
    save_bound(writer/'CODE_HANDOFF.json',{'baseline_analysis_source_commit':'204016acf827db3dd5a3c0afe0031e73bcc7c9ec',
        'postprocess_source_commit':v['source_commit'],'fixed_postprocess_code_url':repo+'/tree/'+v['source_commit'],
        'readme_url':repo+'/blob/'+v['source_commit']+'/technical/R2_METHODS.md','environment_lock_url':repo+'/blob/'+v['source_commit']+'/requirements-r2.lock',
        'review_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT).decode().strip(),
        'review_run_id':a.run_id,'planned_new_tag':tag,'planned_release_url':repo+'/releases/tag/'+tag,
        'repository_visibility':'PRIVATE','release_upload_status':'PREPARED_NOT_YET_CONFIRMED','general_chatgpt_access':'USER_REPORTED_SUCCESS',
        'judge_access':'NOT_VERIFIED_SEPARATE_USER_AUTHORIZATION_REQUIRED','actual_new_fit_count':0,
        'baseline_full_replay_in_r2':False,'code_link_requirement':'DIRECT_OFFICIAL_REPORT_FORM_CONFIRMED',
        'reproduction':'R2_METHODS.md; original licensed archives, baseline ledger and readonly cache hashes required',
        'report_created':False,'submission_performed':False})
    include(writer/'CODE_HANDOFF.json','CODE_HANDOFF.json')
    # Small writer evidence remains usable without opening the technical ZIP.
    for name,path in list(files.items()):
        if name.startswith('analysis/') and path.name not in ('bootstrap_rank_replicates.csv',) and BOUND[path]['bytes']<5*1024**2:
            target=writer/name;target.parent.mkdir(parents=True,exist_ok=True)
            copy_bound(path,target)
    for name in fixed:
        target=writer/name
        copy_bound(files[name],target)
    save_bound(writer/'technical_manifest.json',{'files':[{'path':n,**BOUND[p]} for n,p in sorted(files.items())]})
    include(writer/'technical_manifest.json','technical_manifest.json')
    technical=dest/('SPL_TECH_HANDOFF_'+a.run_id+'.zip');archive(technical,files)
    sourcefiles={x['path']:ROOT/x['path'] for x in source['files']};sourcefiles['requirements-r2.lock']=ROOT/'requirements-r2.lock'
    save_bound(writer/'source_snapshot.json',source);sourcefiles['SOURCE_SNAPSHOT.json']=writer/'source_snapshot.json'
    sourcezip=dest/('SPL_SOURCE_'+a.run_id+'.zip');archive(sourcezip,sourcefiles)
    allfiles={p.relative_to(dest).as_posix():p for p in dest.rglob('*') if p.is_file()}
    save_bound(dest/'DELIVERY_INDEX.json',{'run_id':a.run_id,'status':'TECHNICAL_READY_WITH_LIMITATIONS','verification':a.verification,
        'postprocess_source_commit':v['source_commit'],'baseline_source_commit':'204016acf827db3dd5a3c0afe0031e73bcc7c9ec',
        'actual_new_fits':0,'baseline_full_replay_in_r2':False,'source_tree_hash':source['sha256'],
        'files':[{'path':n,**BOUND[p]} for n,p in sorted(allfiles.items())]})
    allfiles['DELIVERY_INDEX.json']=dest/'DELIVERY_INDEX.json'
    write_bound(dest/'CHECKSUMS.sha256',''.join(BOUND[p]['sha256']+'  '+n+'\n' for n,p in sorted(allfiles.items())).encode('ascii'))
    allfiles['CHECKSUMS.sha256']=dest/'CHECKSUMS.sha256'
    final=ROOT/'delivery'/('SPL_FINAL_FOR_CHATGPT_'+a.run_id+'.zip');archive(final,allfiles)
    write_bound(final.with_suffix('.zip.sha256'),(BOUND[final]['sha256']+'  '+final.name+'\n').encode('ascii'))
    from spl.pet_ai import count_actual_fit_calls
    if source_snapshot()!=source or count_actual_fit_calls(ROOT)!=52 or verify_reuse(config)!=reuse:raise ValueError('Final package context drift')
    read_bound(vp);read_bound(review_path);validate_verification(vp,v)
    for path in [*files.values(),*allfiles.values(),final,final.with_suffix('.zip.sha256')]:read_bound(path)
    print(json.dumps({'status':'TECHNICAL_READY_WITH_LIMITATIONS','final_zip':str(final),'sha256':BOUND[final]['sha256'],'new_tag':tag}))


if __name__=='__main__':main()

"""Immutable technical-only delivery; no publishing or contest narrative."""
import argparse,csv,hashlib,importlib.util,json,subprocess,sys,zipfile
from pathlib import Path,PurePosixPath
from snapshot import ROOT,snapshot,source_files,environment_manifest_ref
from final_verify import identifier,encode
from preflight import safe_child
from pet_final import context
from finish_contract import validate
sys.path.insert(0,str(ROOT/'src'))
from spl.pet_io import file_hash

LIMIT=100*1024**2
BOUND={}

def bound(path):
    safe_child(ROOT,path.relative_to(ROOT).as_posix())
    actual=(path.stat().st_size,file_hash(path))
    if path in BOUND and actual!=BOUND[path]:raise ValueError('Verified bytes changed during staging')
    return BOUND.get(path,actual)
def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('xb') as f:f.write(encode(value))

def copy(source,target):
    expected=bound(source);safe_child(ROOT,target.relative_to(ROOT).as_posix())
    target.parent.mkdir(parents=True,exist_ok=True)
    with source.open('rb') as inp,target.open('xb') as out:
        for block in iter(lambda:inp.read(1024**2),b''):out.write(block)
    if (target.stat().st_size,file_hash(target))!=expected:raise ValueError('Staged bytes differ from verified source')
    BOUND[target]=expected

def write_zip(path,files):
    safe_child(ROOT,path.relative_to(ROOT).as_posix())
    expected={name:bound(p) for name,p in files.items()}
    if sum(p.stat().st_size for p in files.values())>LIMIT:raise ValueError('Technical ZIP expansion budget exceeded')
    with zipfile.ZipFile(path,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name,source in sorted(files.items()):
            pure=PurePosixPath(name)
            if pure.is_absolute() or '..' in pure.parts or '\\' in name or ':' in name:raise ValueError('Unsafe ZIP member')
            if source.is_symlink() or (hasattr(source,'is_junction') and source.is_junction()):raise ValueError('Link rejected')
            z.write(source,name)
    with zipfile.ZipFile(path) as z:
        if z.testzip() is not None or set(z.namelist())!=set(files):raise ValueError('ZIP integrity failed')
        for name,source in files.items():
            import hashlib
            h=hashlib.sha256()
            with z.open(name) as stream:
                for block in iter(lambda:stream.read(1024**2),b''):h.update(block)
            if h.hexdigest()!=expected[name][1] or z.getinfo(name).file_size!=expected[name][0]:raise ValueError('ZIP content differs from verified bytes')

def main():
    p=argparse.ArgumentParser();p.add_argument('--verification',required=True);p.add_argument('--review',required=True);p.add_argument('--run-id',required=True);a=p.parse_args()
    stamp=identifier(a.run_id);vpath=safe_child(ROOT,a.verification);vraw=vpath.read_bytes();v,manifest=validate(a.verification);base=vpath.parent
    if json.loads(vraw)!=v:raise ValueError('Verification summary changed during validation')
    BOUND[vpath]=(len(vraw),hashlib.sha256(vraw).hexdigest())
    for name,key in [('artifact_manifest.json','artifact_manifest_sha256'),('reproducibility.json','reproducibility_sha256')]:BOUND[base/name]=((base/name).stat().st_size,v[key])
    snapshot_raw=(base/'source_snapshot.json').read_bytes()
    if json.loads(snapshot_raw)!=v['source_snapshot']:raise ValueError('Snapshot changed during validation')
    BOUND[base/'source_snapshot.json']=(len(snapshot_raw),hashlib.sha256(snapshot_raw).hexdigest())
    for ref,key in [(v['context']['environment_manifest_ref'],'environment_manifest_sha256'),(v['context']['rights_manifest_ref'],'rights_manifest_sha256')]:
        pth=safe_child(ROOT,ref);BOUND[pth]=(pth.stat().st_size,v['context'][key])
    for record in v['executions']:
        pth=safe_child(ROOT,record['path']);BOUND[pth]=(pth.stat().st_size,record['sha256'])
        for name,key in [('stdout.log','stdout_sha256'),('stderr.log','stderr_sha256')]:BOUND[pth.parent/name]=((pth.parent/name).stat().st_size,record[key])
    for run,items in manifest.items():
        for item in items:BOUND[safe_child(ROOT,run+'/'+item['path'])]=(item['bytes'],item['sha256'])
    for item in v['source_snapshot']['files']:BOUND[safe_child(ROOT,item['path'])]=(item['bytes'],item['sha256'])
    if v.get('status')!='COMPLETED' or not v.get('implemented_checks_passed') or (base/'verification_failure.json').exists():raise ValueError('Incomplete final verification')
    if v['source_snapshot']!=snapshot() or v['context']!=context('configs/pet_multiday_finish.yaml'):raise ValueError('Latest source/input context differs')
    for name,key in [('artifact_manifest.json','artifact_manifest_sha256'),('reproducibility.json','reproducibility_sha256')]:
        if file_hash(base/name)!=v[key]:raise ValueError('Verification evidence changed')
    manifest=json.loads((base/'artifact_manifest.json').read_bytes())
    for run,files in manifest.items():
        for item in files:
            path=safe_child(ROOT,run+'/'+item['path'])
            if path.stat().st_size!=item['bytes'] or file_hash(path)!=item['sha256']:raise ValueError('Verified artifact changed')
    for record in v['executions']:
        path=safe_child(ROOT,record['path'])
        if file_hash(path)!=record['sha256']:raise ValueError('Execution record changed')
        for name,key in [('stdout.log','stdout_sha256'),('stderr.log','stderr_sha256')]:
            if file_hash(path.parent/name)!=record[key]:raise ValueError('Execution log changed')
    reviewpath=safe_child(ROOT,a.review);review_raw=reviewpath.read_bytes();review=json.loads(review_raw)
    if not review.get('review_completed') or review.get('blocking_findings_remaining')!=0 or review.get('source_tree_hash')!=v['source_snapshot']['sha256']:raise ValueError('Current independent review not closed')
    BOUND[reviewpath]=(len(review_raw),hashlib.sha256(review_raw).hexdigest())
    rights=json.loads(safe_child(ROOT,v['context']['rights_manifest_ref']).read_bytes())
    if rights['rights'].get('share_derived_status')!='ALLOW':raise ValueError('Derived sharing not allowed')
    preserved=json.loads((ROOT/'state/r1_20261012T0804/resume_record.json').read_bytes())['preserved_prior_files']
    preserved += [
        {'path':'delivery/r1_pet_final_20261012_01/SPL_TECH_HANDOFF_r1_pet_final_20261012_01.zip','sha256':'3e74f267ca8dcafae6aeda88a9474dc917ba0539d1286d77ec7e3f6adc112818'},
        {'path':'delivery/r1_pet_final_20261012_01/SPL_SOURCE_r1_pet_final_20261012_01.zip','sha256':'5a8d884e34fca8ec8ce4960159fd9cf7badf928cbcc087024687fc51cae474e5'}]
    for item in preserved:
        if file_hash(safe_child(ROOT,item['path']))!=item['sha256']:raise ValueError('Prior immutable result changed')
    out=safe_child(ROOT,'delivery/report_ready_'+stamp)
    outer=safe_child(ROOT,'delivery/SPL_FINAL_FOR_CHATGPT_'+stamp+'.zip')
    if outer.exists() or outer.with_suffix('.zip.sha256').exists():raise FileExistsError('Outer delivery collision')
    if out.exists():raise FileExistsError('Delivery exists; preserve and choose new ID')
    out.mkdir(parents=True);writer=out/'writer_input';writer.mkdir()
    save(writer/'prior_result_preservation.json',{'status':'MATCH','files':preserved})
    pet=safe_child(ROOT,v['selected_pet_run']);evidence=safe_child(ROOT,v['selected_evidence_run'])
    # Full per-event local outputs remain checksummed and reproducible. Package
    # aggregates, audit cases and bounded AI descriptions, with explicit omissions.
    tech={};omitted=[]
    for directory,prefix in [(pet,'analysis'),(evidence,'writer_input')]:
        for item in manifest[directory.relative_to(ROOT).as_posix()]:
            path=safe_child(ROOT,directory.relative_to(ROOT).as_posix()+'/'+item['path'])
            if path.suffix not in {'.json','.jsonl','.csv','.parquet','.png','.svg','.md'}:continue
            name=path.relative_to(directory).as_posix()
            full=name.startswith(('event_ledger','signal_context'))
            if full or path.stat().st_size>20*1024**2:
                omitted.append({'path':path.relative_to(ROOT).as_posix(),'bytes':path.stat().st_size,'sha256':file_hash(path),'reason':'LOCAL_FULL_DERIVED_OUTPUT; bounded handoff contains aggregates/audit cases; reproduce with authorized inputs'});continue
            tech[prefix+'/'+name]=path
            if directory==evidence:copy(path,writer/name)
    for name in ('verification_summary.json','reproducibility.json','artifact_manifest.json','source_snapshot.json'):
        copy(base/name,writer/name);tech['validation/'+name]=base/name
    copy(reviewpath,writer/'review.json')
    copy(ROOT/'state/finish_20261013/runtime_roles_finish.json',writer/'runtime_roles.json')
    copy(ROOT/'state/finish_20261013/signal_timestamp_format_audit.json',writer/'signal_timestamp_format_audit.json')
    contest=ROOT/'state/finish_20261013/contest_requirements.json';copy(contest,writer/'contest_requirements.json')
    acquisition=[json.loads(p.read_bytes()) for p in sorted((ROOT/'state/finish_20261013/acquisition').glob('attempt_*.json'))]
    route={'status':'MULTIDAY_ACQUIRED_AND_ANALYZED','scope':'MULTIDAY_CASE','requests':acquisition,'new_received_bytes':sum(r['received_bytes'] for r in acquisition),'r1_cumulative_additional_received_bytes':acquisition[-1]['prior_received_bytes']+acquisition[-1]['received_bytes'],'original_sample_archive_bytes_not_redownloaded':150667218,'budget_basis':'R1 additional acquisition decision; prior base sample retained separately','wait_seconds':sum(r['wait_charged_seconds'] for r in acquisition),'request_count_basis':'Four archive acquisition invocations; redirects not exposed by inherited handler','input_inventory':json.loads((pet/'source_inventory.json').read_bytes()),'scenario_definitions':'Pinned 1.1 examples archive, scenarios table only; all events/objects/signals from full archives','tracks_parsed':False}
    save(writer/'data_route.json',route)
    limits={'technical_scope':'MULTIDAY_CASE','allowed':['Recorded provider PET interaction descriptions at this Austrian intersection','Conditional scenario/signal comparisons and date-cluster exploration','Automated timestamp/source consistency checks'],
            'unsupported':['Accident probability, violations, all-traffic risk or Korean intersection estimates','Independent geometric PET accuracy or manually labelled ground truth','Causal AI improvement or validated policy priority'],
            'limitations':['Selected provider interactions are not all traffic exposure','Signal context is own conflict-area entry, not stop-line entry','AI uses a deterministic bounded sample; no improvement claim','Date clusters at one intersection do not establish broader generalization','Official form details, exact deadline time, eligibility and AI/data rules remain unconfirmed'],
            'original_straight_entry_status':'DEFERRED_OUT_OF_SCOPE','v3_full_spec_verified':False,'report_generated_by_codex':False}
    save(writer/'claim_limits.json',limits)
    meta=json.loads((ROOT/'data/raw/sitpet_r1_20261012T0850/metadata.json').read_bytes())['metadata']
    save(writer/'sources.json',{'dataset_id':'sitpet','version':'1.1','doi':'10.5281/zenodo.19231310','title':meta.get('title'),'creators':meta.get('creators'),'license':'CC-BY-4.0','license_evidence':'reproduce/sitpet/LICENSE.md','inputs':v['context']['inputs'],'modifications':['Provider event QC and exact numeric timestamp parsing','Conditional statistics, date-cluster exploration and bounded AI descriptions','Automated source consistency audit'],'reproduction_scope':'WITH_AUTHORIZED_DATA','contest_use_status':rights['rights']['contest_use_status'],'software_license_changed':False})
    bound(pet/'pet_qc.json');bound(pet/'cluster_validation.json')
    qc=json.loads((pet/'pet_qc.json').read_bytes());ai=json.loads((pet/'cluster_validation.json').read_bytes())
    brief='# ChatGPT용 기술 근거 안내\n\n이 문서는 공모전 본문이 아닌 산출물 사용 안내입니다. 최종 보고서 작성자는 ChatGPT입니다.\n\n'
    brief+='질문: 고정 제공자 PET 상호작용의 시나리오·신호조건별 분포와 시간 정렬 민감도는 어떠한가?\n\n'
    brief+=f"범위: MULTIDAY_CASE. 원시 {qc['n_raw']:,}행, 고유 이벤트 {qc['n_unique_event_keys']:,}건, 유효 비음수 {qc['n_valid_nonnegative_pet']:,}건, 음수 검토 {qc['n_negative_pet']:,}건, 무효 {qc['n_invalid']:,}건. 정확한 분모와 수치는 metric_facts.jsonl 및 analysis/scenario_summary.csv를 사용합니다.\n\n"
    brief+=f"AI 실행 상태: {ai['status']}. ai_assessment.json과 군집 특징/기여 원천표를 함께 확인하십시오. 군집 분리·ARI는 사고 탐지 정확도나 모델 개선 증거가 아닙니다.\n\n"
    brief+='event_audit.json은 자동 수치 검토이며 사람의 정답 검수나 독립 기하학 검증이 아닙니다. figures_index.json의 원천표·축·단위를 유지하고 claim_limits.json의 주장 제한을 적용하십시오. 원래 직진 진입 분석은 보존·범위 밖 유보 상태입니다.\n\n'
    brief+='contest_requirements.json에 공식 확인 필드와 미확인 필드를 분리했습니다. 참가자 정보·서명은 추정하지 않았고, 신청서·발표자료·공모전 보고서를 생성하거나 제출하지 않았습니다.\n\n'
    brief+='재현: SOURCE ZIP을 별도 새 프로젝트에 풀고 README/technical/finish_multiday.md를 따릅니다. 허가된 고정 원자료를 동일 경로·해시로 준비해야 합니다. 이 ZIP에는 원자료·가상환경·비밀정보가 없습니다. 대용량 행 단위 산출물은 local_omissions.json에 해시와 위치를 보존했습니다.\n'
    (writer/'WRITER_BRIEF.md').write_text(brief,encoding='utf-8')
    (out/'PORTABILITY.md').write_text('The source ZIP contains the exact working-tree sources. The technical ZIP and writer_input contain derived evidence. Raw archives are intentionally absent. To rerun the structural readiness check in a new authorized project, extract SOURCE at that project root and place this outer ZIP contents under delivery/report_ready_'+stamp+'/. REPORT_READY paths are project-relative. Reproduction requires the pinned archives and project environment described in technical/finish_multiday.md. No report or external submission is included.\n',encoding='utf-8')
    save(writer/'local_omissions.json',{'omitted':omitted})
    for path in writer.rglob('*'):
        if path.is_file():tech['writer_input/'+path.relative_to(writer).as_posix()]=path
    for record in v['executions']:
        parent=safe_child(ROOT,record['path']).parent
        for name in ('execution.json','stdout.log','stderr.log'):tech['validation/executions/'+parent.name+'/'+name]=parent/name
    for ref in (environment_manifest_ref(ROOT),v['context']['rights_manifest_ref'],'state/finish_20261013/decision.json'):
        tech['validation/'+Path(ref).name]=safe_child(ROOT,ref)
    for pth in (ROOT/'state/finish_20261013/acquisition').glob('*.json'):tech['validation/acquisition/'+pth.name]=pth
    for name in ('LICENSE.md','schema.md','README.md','metadata.json'):tech['reproduce/sitpet/'+name]=ROOT/'state/research/sitpet'/name
    tech['history/pre_format_fix_input_probe.json']=ROOT/'state/finish_20261013/input_inventory.json'
    for ref in ('requirements.lock','configs/pet_multiday_finish.yaml','technical/pet_r1.md','technical/finish_multiday.md'):tech['reproduce/'+ref]=ROOT/ref
    tech['sources.json']=writer/'sources.json';tech['limitations.json']=writer/'claim_limits.json'
    tech['data_dictionary.md']=ROOT/'technical/pet_r1.md';tech['technical_notes.md']=ROOT/'technical/finish_multiday.md'
    tech['evidence/metric_facts.jsonl']=writer/'metric_facts.jsonl';tech['evidence/claim_constraints.json']=writer/'claim_limits.json'
    save(writer/'HANDOFF_STATUS.json',{'status':'TECHNICAL_SCOPE_COMPLETE_WITH_LIMITATIONS','scope':'MULTIDAY_CASE','evidence_status':'REAL_EXPLORATORY','report_owner':'CHATGPT','report_generated':False,'external_submission_performed':False,'v3_full_spec_verified':False,'original_straight_entry':'DEFERRED_OUT_OF_SCOPE','ai_status':ai['status'],'rights_for_derived_handoff':'ALLOW','rights_for_contest':rights['rights'].get('contest_use_status','UNKNOWN'),'source_tree_hash':v['source_snapshot']['sha256']})
    tech['HANDOFF_STATUS.json']=writer/'HANDOFF_STATUS.json'
    save(writer/'artifact_index.json',{'artifacts':[{'artifact_id':n,'path':n,'format':p.suffix.removeprefix('.')} for n,p in sorted(tech.items())],'row_counts':'See corresponding Parquet index or explicitly counted metrics; not inferred from file size'})
    tech['artifact_index.json']=writer/'artifact_index.json'
    save(writer/'technical_manifest.json',{'files':[{'path':n,'bytes':p.stat().st_size,'sha256':file_hash(p),'data_kind':'REAL_DERIVED' if n.startswith('analysis/') else 'TECHNICAL_EVIDENCE','rights_basis':'Dataset-derived statistics: CC-BY-4.0; software and validation retain original notices'} for n,p in sorted(tech.items())],'omitted':omitted,'self_hash':'Manifest itself is bound by outer DELIVERY_INDEX and CHECKSUMS'})
    tech['manifest.json']=writer/'technical_manifest.json'
    src={p.relative_to(ROOT).as_posix():p for p in source_files()}
    if set(src)!={i['path'] for i in v['source_snapshot']['files']}:raise ValueError('Source membership differs')
    src['source_snapshot.json']=base/'source_snapshot.json'
    techzip=out/('SPL_TECH_HANDOFF_'+stamp+'.zip');sourcezip=out/('SPL_SOURCE_'+stamp+'.zip')
    write_zip(techzip,tech);write_zip(sourcezip,src)
    git_head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    git_dirty=bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT))
    index={'run_id':stamp,'source_tree_hash':v['source_snapshot']['sha256'],'git_head':git_head,'git_dirty':git_dirty,'source_basis':'Exact current working-tree content snapshot; uncommitted changes included in source ZIP','verification':a.verification,'review':a.review,'files':[{'path':p.relative_to(out).as_posix(),'bytes':p.stat().st_size,'sha256':file_hash(p)} for p in sorted(out.rglob('*')) if p.is_file()],'scope':'MULTIDAY_CASE','raw_packaged':False,'report_generated':False,'external_submission_performed':False}
    save(out/'DELIVERY_INDEX.json',index)
    roles={'TECH_ZIP':techzip,'SOURCE_ZIP':sourcezip,'WRITER_BRIEF':writer/'WRITER_BRIEF.md','METRIC_FACTS':writer/'metric_facts.jsonl','FIGURES_INDEX':writer/'figures_index.json','CONTEST_REQUIREMENTS':writer/'contest_requirements.json','VERIFICATION':writer/'verification_summary.json','REPRODUCIBILITY':writer/'reproducibility.json','REVIEW':writer/'review.json','CLAIM_LIMITS':writer/'claim_limits.json','DELIVERY_INDEX':out/'DELIVERY_INDEX.json','DATA_ROUTE':writer/'data_route.json','EVENT_AUDIT':writer/'event_audit.json'}
    def ref(path):return {'path':path.relative_to(ROOT).as_posix(),'sha256':file_hash(path)}
    tasks={k:'PASS' for k in ('scope','data_route','event_audit','analysis','ai_assessment','contest_requirements','writer_evidence','verification','review','delivery')}
    for k in ('event_audit','analysis','ai_assessment','contest_requirements'):tasks[k]='PASS_WITH_LIMITATIONS'
    save(out/'REPORT_READY.json',{'status':'READY_FOR_REPORT_WITH_LIMITATIONS','report_owner':'CHATGPT','report_generated_by_codex':False,'external_submission_performed':False,'technical_scope':'MULTIDAY_CASE','limitations':limits['limitations'],'human_inputs':json.loads(contest.read_bytes())['human_inputs'],'tasks':tasks,'current_source_snapshot':ref(writer/'source_snapshot.json'),'files':[{'role':role,**ref(path)} for role,path in roles.items()]})
    spec=importlib.util.spec_from_file_location('readiness',ROOT/'finish_to_report/readiness.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    gate=module.check(ROOT,(out/'REPORT_READY.json').relative_to(ROOT).as_posix());save(out/'READINESS_CHECK.json',gate)
    if snapshot()!=v['source_snapshot'] or context('configs/pet_multiday_finish.yaml')!=v['context']:raise ValueError('Source/input/environment drift during package')
    with (out/'CHECKSUMS.sha256').open('x',encoding='ascii') as stream:
        for path in sorted(out.rglob('*')):
            if path.is_file() and path.name!='CHECKSUMS.sha256':stream.write(file_hash(path)+'  '+path.relative_to(out).as_posix()+'\n')
    write_zip(outer,{p.relative_to(out).as_posix():p for p in out.rglob('*') if p.is_file()})
    with outer.with_suffix('.zip.sha256').open('x',encoding='ascii') as stream:stream.write(file_hash(outer)+'  '+outer.name+'\n')
    print(json.dumps({'status':gate['status'],'final_zip':outer.relative_to(ROOT).as_posix(),'sha256':file_hash(outer),'bytes':outer.stat().st_size,'report_ready':(out/'REPORT_READY.json').relative_to(ROOT).as_posix()}))

if __name__=='__main__':main()

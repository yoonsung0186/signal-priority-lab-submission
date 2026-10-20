"""Shared immutable verification contract for final PET delivery."""
import json,sys
from pathlib import PurePosixPath
from snapshot import ROOT,snapshot
from preflight import safe_child
from final_verify import identifier,SYNTHETIC_CANONICAL_FILES,run_artifact_files
from pet_final import context,validate_provenance
sys.path.insert(0,str(ROOT/'src'))
from spl.pet_io import file_hash

MANDATORY={'pet_result.json','pet_qc.json','cluster_validation.json','source_inventory.json','event_ledger.csv','scenario_summary.csv','pet_thresholds.csv','signal_context.csv','timing_sensitivity.csv','inspection_candidates.csv','pet_ecdf.csv'}
WRITER_REQUIRED={'event_audit.json','ai_assessment.json','metric_facts.jsonl','figures_index.json','technical_summary.json'}

def plan(stamp):
    identifier(stamp);py=sys.executable
    syn=[f'runs/{stamp}_synthetic_{i}' for i in (1,2)];pet=[f'runs/{stamp}_pet_{i}' for i in (1,2)];evidence=[f'runs/{stamp}_evidence_{i}' for i in (1,2)]
    jobs=[('tools',[py,'-m','unittest','discover','-s','tools','-p','test_*.py','-v']),('implementation',[py,'-m','unittest','discover','-s','tests','-v']),('optimized',[py,'-O','-m','unittest','discover','-s','tests','-p','test_ops.py','-v']),('preflight',[py,'tools/preflight.py','--root','.'])]
    jobs += [(f'synthetic_{i+1}',[py,'-m','spl','synthetic','--output',r]) for i,r in enumerate(syn)]
    jobs += [(f'pet_{i+1}',[py,'tools/pet_bulk.py','--profile','configs/pet_multiday_finish.yaml','--output',r]) for i,r in enumerate(pet)]
    jobs += [(f'evidence_{i+1}',[py,'tools/finish_evidence.py','--pet-run',r,'--output',evidence[i]]) for i,r in enumerate(pet)]
    return jobs,syn,pet,evidence

def canonical(run):
    files=json.loads(safe_child(ROOT,run+'/run_manifest.json').read_bytes())['canonical_files']
    if not isinstance(files,list) or len(files)!=len(set(files)) or not MANDATORY<=set(files):raise ValueError('Incomplete canonical contract')
    for name in files:
        pure=PurePosixPath(name)
        if pure.is_absolute() or '..' in pure.parts or '\\' in name or ':' in name:raise ValueError('Unsafe canonical name')
        safe_child(ROOT,run+'/'+name)
    return sorted(files)

def writer_members(run):
    paths=run_artifact_files(ROOT,run);files=sorted(p.relative_to(ROOT/run).as_posix() for p in paths)
    if not WRITER_REQUIRED<=set(files):raise ValueError('Incomplete writer evidence')
    return files

def normalize(value,pet_run,evidence_run):
    if isinstance(value,str):return value.replace(pet_run,'<PET_RUN>').replace(evidence_run,'<EVIDENCE_RUN>')
    if isinstance(value,list):return [normalize(v,pet_run,evidence_run) for v in value]
    if isinstance(value,dict):return {k:normalize(v,pet_run,evidence_run) for k,v in value.items()}
    return value

def compare_writer(pet,evidence):
    files=writer_members(evidence[0])
    if files!=writer_members(evidence[1]):raise ValueError('Writer artifact membership drift')
    compared=[]
    for p_run,e_run in zip(pet,evidence):
        provenance=json.loads(safe_child(ROOT,e_run+'/provenance.json').read_bytes())
        if provenance['input_run']!=p_run or provenance['input_run_provenance_sha256']!=file_hash(safe_child(ROOT,p_run+'/provenance.json')) or provenance['source_snapshot']!=snapshot():raise ValueError('Evidence input/source provenance mismatch')
        for name,entry in provenance['files'].items():
            path=safe_child(ROOT,e_run+'/'+name)
            if file_hash(path)!=entry['sha256'] or path.stat().st_size!=entry['bytes']:raise ValueError('Evidence provenance artifact mismatch')
    for name in files:
        paths=[safe_child(ROOT,run+'/'+name) for run in evidence]
        if name.endswith(('.png','.svg')) or name=='provenance.json':continue # Run-specific provenance was separately bound above.
        if name.endswith(('.json','.jsonl')):
            values=[]
            for i,path in enumerate(paths):
                raw=path.read_text(encoding='utf-8')
                value=[json.loads(line) for line in raw.splitlines() if line.strip()] if name.endswith('.jsonl') else json.loads(raw)
                if name=='metric_facts.jsonl':
                    expected=file_hash(safe_child(ROOT,pet[i]+'/provenance.json'))
                    for fact in value:
                        if fact.get('input_run_provenance_sha256')!=expected:raise ValueError('Metric fact input provenance hash differs')
                        fact['input_run_provenance_sha256']='<VERIFIED_PET_RUN_PROVENANCE_SHA256>'
                values.append(normalize(value,pet[i],evidence[i]))
            equal=values[0]==values[1];basis='JSON semantics; exact run prefixes and independently bound metric fact input provenance hash normalized'
        else:equal=file_hash(paths[0])==file_hash(paths[1]);basis='Exact bytes'
        if not equal:raise ValueError('Writer evidence replay mismatch: '+name)
        compared.append({'file':name,'semantic_equal':equal,'basis':basis,'sha256s':[file_hash(p) for p in paths]})
    return compared

def validate_execution_records(v,jobs,stamp):
    records=v['executions'];expected=dict(jobs)
    if len(records)!=len(jobs) or {r['name'] for r in records}!=set(expected):raise ValueError('Required execution set missing')
    for record in records:
        path=safe_child(ROOT,'state/executions/'+stamp+'_'+record['name']+'/execution.json')
        if record['path']!=path.relative_to(ROOT).as_posix() or file_hash(path)!=record['sha256']:raise ValueError('Execution reference/hash differs')
        actual=json.loads(path.read_bytes())
        if actual!={k:value for k,value in record.items() if k not in {'name','path','sha256'}}:raise ValueError('Execution record differs from summary')
        if actual['command']!=expected[record['name']] or actual['exit_code']!=0 or actual['source_unchanged'] is not True or actual['source_tree_hash']!=v['source_snapshot']['sha256']:raise ValueError('Invalid execution/source')
        for key,ctxkey in [('config_hash','config_sha256'),('environment_hash','environment_manifest_sha256'),('lock_hash','lock_sha256')]:
            if actual[key]!=v['context'][ctxkey]:raise ValueError('Execution environment drift')
        for name,key in [('stdout.log','stdout_sha256'),('stderr.log','stderr_sha256')]:
            if file_hash(path.parent/name)!=actual[key]:raise ValueError('Execution log changed')

def validate_pet(run,source,ctx):
    result=validate_provenance(run,source,ctx)
    ai=json.loads(safe_child(ROOT,run+'/cluster_validation.json').read_bytes())
    path=safe_child(ROOT,run+'/fit_attempts.jsonl')
    attempts=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()] if path.exists() else []
    starts=[r for r in attempts if r.get('evidence_kind')=='REAL_ESTIMATOR_FIT' and r.get('status')=='STARTED']
    completed=[r for r in attempts if r.get('evidence_kind')=='REAL_ESTIMATOR_FIT' and r.get('status')=='COMPLETED']
    if len(starts)!=ai['fit_call_count'] or len(completed)!=len(starts) or {r['fit_call'] for r in starts}!={r['fit_call'] for r in completed}:raise ValueError('Actual fit attempt count/completion differs')
    qc=json.loads(safe_child(ROOT,run+'/pet_qc.json').read_bytes())
    if any(result[key]!=qc[key] for key in ('n_raw','n_valid_nonnegative_pet','n_negative_pet','n_invalid')):raise ValueError('Result/QC denominators differ')
    if result['scope']!='MULTIDAY_CASE':raise ValueError('Full inputs did not produce multiday scope')
    return result

def validate(reference):
    path=safe_child(ROOT,reference);base=path.parent
    if path.name!='verification_summary.json' or base.parent!=ROOT/'state/verifications':raise ValueError('Explicit immutable verification required')
    stamp=identifier(base.name);jobs,syn,pet,evidence=plan(stamp)
    v=json.loads(path.read_bytes())
    if v.get('status')!='COMPLETED' or v.get('implemented_checks_passed') is not True or (base/'verification_failure.json').exists():raise ValueError('Incomplete final verification')
    if v['source_snapshot']!=snapshot() or v['context']!=context('configs/pet_multiday_finish.yaml'):raise ValueError('Current source/input context differs')
    if json.loads((base/'source_snapshot.json').read_bytes())!=v['source_snapshot']:raise ValueError('Snapshot artifact differs from verified source')
    validate_execution_records(v,jobs,stamp)
    for name,key in [('artifact_manifest.json','artifact_manifest_sha256'),('reproducibility.json','reproducibility_sha256')]:
        if file_hash(base/name)!=v[key]:raise ValueError('Verification evidence changed')
    manifest=json.loads((base/'artifact_manifest.json').read_bytes());repro=json.loads((base/'reproducibility.json').read_bytes())
    if set(manifest)!=set([*syn,*pet,*evidence]) or v['selected_pet_run']!=pet[-1] or v['selected_evidence_run']!=evidence[-1]:raise ValueError('Selected run binding differs')
    if repro['status']!='MATCH' or repro['source_tree_hash']!=v['source_snapshot']['sha256'] or any(repro[key]!=value for key,value in [('pet_runs',pet),('evidence_runs',evidence),('synthetic_runs',syn)]):raise ValueError('Replay binding differs')
    for run,entries in manifest.items():
        actual={p.relative_to(ROOT/run).as_posix():p for p in run_artifact_files(ROOT,run)}
        if len(entries)!=len(actual) or {e['path'] for e in entries}!=set(actual):raise ValueError('Artifact membership changed')
        for entry in entries:
            p=actual[entry['path']]
            if p.stat().st_size!=entry['bytes'] or file_hash(p)!=entry['sha256']:raise ValueError('Verified artifact bytes changed')
    for kind,runs,names in [('synthetic',syn,sorted(SYNTHETIC_CANONICAL_FILES)),('pet',pet,canonical(pet[0]))]:
        if kind=='pet' and names!=canonical(pet[1]):raise ValueError('Canonical set drift')
        rows=repro['comparisons'][kind]
        if len(rows)!=len(names) or {r['file'] for r in rows}!=set(names):raise ValueError('Replay comparison incomplete')
        for row in rows:
            if row['byte_equal'] is not True or row['semantic_equal'] is not True or any(file_hash(safe_child(ROOT,run+'/'+row['file']))!=row['sha256'] for run in runs):raise ValueError('Replay comparison invalid')
    if repro['comparisons']['writer_semantics']!=compare_writer(pet,evidence):raise ValueError('Writer semantic replay invalid')
    for run in pet:validate_pet(run,v['source_snapshot'],v['context'])
    result=json.loads(safe_child(ROOT,pet[-1]+'/pet_result.json').read_bytes())
    if v['result']!=result:raise ValueError('Selected result differs')
    return v,manifest

"""One finite verification pass. No agents, controller, network, or retry loop."""
import argparse,json,os,subprocess,sys
from pathlib import Path
from snapshot import ROOT,snapshot
from final_verify import identifier,encode,SYNTHETIC_CANONICAL_FILES,run_artifact_files
from pet_final import context
from preflight import safe_child
from finish_contract import plan,canonical,compare_writer,validate_pet
sys.path.insert(0,str(ROOT/'src'))
from spl.pet_io import file_hash
from spl.pet_ai import count_actual_fit_calls

def save(path,value):
    with path.open('xb') as stream:stream.write(encode(value))

def artifacts(run):
    return [{'path':p.relative_to(ROOT/run).as_posix(),'bytes':p.stat().st_size,'sha256':file_hash(p)} for p in run_artifact_files(ROOT,run)]

def compare(a,b,names):
    result=[]
    for name in names:
        first=safe_child(ROOT,a+'/'+name);second=safe_child(ROOT,b+'/'+name)
        sha=file_hash(first);equal=sha==file_hash(second) and first.stat().st_size==second.stat().st_size
        result.append({'file':name,'sha256':sha,'bytes':first.stat().st_size,'byte_equal':equal,'semantic_equal':equal,'basis':'Exact serialized bytes; hashes computed in bounded blocks'})
    if not result or not all(r['byte_equal'] for r in result):raise ValueError('Canonical replay mismatch')
    return result

def main():
    p=argparse.ArgumentParser();p.add_argument('--id',required=True);a=p.parse_args();stamp=identifier(a.id)
    out=safe_child(ROOT,'state/verifications/'+stamp)
    if out.exists():raise FileExistsError('Use a new immutable verification ID')
    source=snapshot();ctx=context('configs/pet_multiday_finish.yaml')
    fits_before=count_actual_fit_calls(ROOT)
    if fits_before+22>64:raise ValueError('Insufficient remaining fit budget for two fixed multi-day replays')
    jobs,syn,pet,evidence=plan(stamp)
    py=sys.executable
    for ref in [*syn,*pet,*evidence,*('state/executions/'+stamp+'_'+n for n,_ in jobs)]:
        if safe_child(ROOT,ref).exists():raise FileExistsError('Output collision: '+ref)
    out.mkdir(parents=True)
    save(out/'verification_request.json',{'source_snapshot':source,'context':ctx,'jobs':jobs,'actual_fit_calls_before':fits_before,'reserved_max_new_fit_calls':22,'cumulative_fit_budget':64})
    env=os.environ.copy();env.update(PYTHONPATH=str(ROOT/'src'),PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1',OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1',MPLCONFIGDIR=str(ROOT/'.cache/matplotlib_finish'))
    records=[]
    try:
        for name,command in jobs:
            if snapshot()!=source:raise ValueError('Source changed before '+name)
            print('START '+name,flush=True)
            completed=subprocess.run([py,'tools/session_exec.py',stamp+'_'+name,*command],cwd=ROOT,env=env,capture_output=True)
            ref='state/executions/'+stamp+'_'+name+'/execution.json';path=ROOT/ref
            if not path.is_file():raise ValueError('Missing execution record '+name+': '+completed.stderr.decode('utf-8','replace')[-2000:])
            record=json.loads(path.read_bytes());records.append({'name':name,'path':ref,'sha256':file_hash(path),**record})
            print(json.dumps(records[-1]),flush=True)
            if completed.returncode or record['exit_code'] or not record['source_unchanged'] or record['source_tree_hash']!=source['sha256']:raise ValueError('Failed command/source drift: '+name)
        comparisons={'synthetic':compare(syn[0],syn[1],SYNTHETIC_CANONICAL_FILES)}
        files=canonical(pet[0])
        if files!=canonical(pet[1]):raise ValueError('PET canonical membership drift')
        comparisons['pet']=compare(pet[0],pet[1],files)
        comparisons['writer_semantics']=compare_writer(pet,evidence)
        for run in pet:
            validate_pet(run,source,ctx)
            result=json.loads((ROOT/run/'pet_result.json').read_bytes());qc=json.loads((ROOT/run/'pet_qc.json').read_bytes())
            inventory=json.loads((ROOT/run/'source_inventory.json').read_bytes())
            if inventory['inputs']!=ctx['inputs']:raise ValueError('Actual input membership/hash differs')
            if qc['n_unique_event_keys']!=sum(qc[k] for k in ('n_valid_nonnegative_pet','n_negative_pet','n_invalid')):raise ValueError('PET partition failed')
            if not all(qc['reconciliation'].values()):raise ValueError('PET reconciliation failed')
            ai=json.loads((ROOT/run/'cluster_validation.json').read_bytes())
            if ai['fit_call_count'] and not ai['status'].startswith('FITTED'):raise ValueError('AI execution failed/incomplete')
        if snapshot()!=source or context('configs/pet_multiday_finish.yaml')!=ctx:raise ValueError('Final context drift')
        fits_after=count_actual_fit_calls(ROOT)
        if fits_after>64 or fits_after-fits_before>22:raise ValueError('Actual model fit budget exceeded')
        repro={'status':'MATCH','comparisons':comparisons,'pet_runs':pet,'evidence_runs':evidence,'synthetic_runs':syn,'source_tree_hash':source['sha256']}
        save(out/'reproducibility.json',repro)
        manifest={run:artifacts(run) for run in [*syn,*pet,*evidence]};save(out/'artifact_manifest.json',manifest)
        save(out/'source_snapshot.json',source)
        save(out/'verification_summary.json',{'status':'COMPLETED','implemented_checks_passed':True,'source_snapshot':source,'context':ctx,'executions':records,'selected_pet_run':pet[-1],'selected_evidence_run':evidence[-1],'reproducibility_sha256':file_hash(out/'reproducibility.json'),'artifact_manifest_sha256':file_hash(out/'artifact_manifest.json'),'result':result,'actual_fit_calls_before':fits_before,'actual_fit_calls_after':fits_after,'cumulative_fit_budget':64,'v3_full_spec_verified':False})
        print('COMPLETED '+str(out.relative_to(ROOT)),flush=True)
    except BaseException as error:
        save(out/'verification_failure.json',{'status':'FAILED_OR_INCOMPLETE','reason':str(error),'executions':records});raise

if __name__=='__main__':main()

"""Finite no-fit verification: software regressions and two R2 postprocess runs.

No agent recursion, retries, baseline execution, scheduler or model fitting.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from r2_postprocess import ROOT, source_snapshot, save, verify_reuse
from preflight import safe_child
from spl.pet_io import file_hash
from spl.pet_ai import count_actual_fit_calls


def main():
    p=argparse.ArgumentParser();p.add_argument('--id',required=True);a=p.parse_args()
    if not a.id.replace('_','').replace('-','').isalnum():raise ValueError('Invalid verification ID')
    dest=safe_child(ROOT,'state/verifications/'+a.id)
    dest.mkdir(parents=True,exist_ok=False)
    source=source_snapshot();config=json.loads((ROOT/'configs/r2_postprocess.json').read_bytes())
    before=count_actual_fit_calls(ROOT)
    if before!=52 or config['new_actual_fit_budget']!=0:raise ValueError('Unexpected fit budget context')
    env=os.environ.copy();env.update(PYTHONPATH=str(ROOT/'src'),PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1',
        OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1',MPLCONFIGDIR=str(ROOT/'.cache/matplotlib_r2'))
    environment={'python':sys.version,'platform':platform.platform(),
                 'installed_packages':subprocess.check_output([sys.executable,'-m','pip','freeze']).decode().splitlines(),
                 'r2_lock_sha256':file_hash(ROOT/'requirements-r2.lock')}
    save(dest/'environment.json',environment);save(dest/'source_snapshot.json',source)
    runs=['runs/'+a.id+'_1','runs/'+a.id+'_2'];py=sys.executable
    jobs=[('tools',[py,'-m','unittest','discover','-s','tools','-p','test_*.py','-v']),
          ('implementation',[py,'-m','unittest','discover','-s','tests','-v']),
          ('optimized',[py,'-O','-m','unittest','discover','-s','tests','-p','test_ops.py','-v']),
          ('preflight',[py,'tools/preflight.py','--root','.'])]
    jobs += [('postprocess_'+str(i+1),[py,'tools/r2_postprocess.py','--out',r]) for i,r in enumerate(runs)]
    save(dest/'verification_request.json',{'commands':jobs,'actual_fit_before':before,'new_fit_budget':0,
         'baseline_full_replay_in_r2':False,'run_paths':runs,'source_tree_hash':source['sha256']})
    executions=[]
    try:
        for name,command in jobs:
            if source_snapshot()!=source:raise ValueError('Source changed before '+name)
            print('START '+name,flush=True);start=time.monotonic()
            with (dest/(name+'.stdout.log')).open('xb') as stdout, (dest/(name+'.stderr.log')).open('xb') as stderr:
                result=subprocess.run(command,cwd=ROOT,env=env,stdout=stdout,stderr=stderr)
            record={'name':name,'command':command,'exit_code':result.returncode,'elapsed_s':time.monotonic()-start,
                    'source_unchanged':source_snapshot()==source,'source_tree_hash':source['sha256'],
                    'config_sha256':file_hash(ROOT/'configs/r2_postprocess.json'),
                    'environment_sha256':file_hash(dest/'environment.json'),
                    'stdout_sha256':file_hash(dest/(name+'.stdout.log')),'stderr_sha256':file_hash(dest/(name+'.stderr.log'))}
            executions.append(record);save(dest/(name+'.execution.json'),record)
            print(json.dumps(record),flush=True)
            if result.returncode or not record['source_unchanged']:raise ValueError('Failed execution '+name)
        left,right=[safe_child(ROOT,r) for r in runs]
        members=lambda root:sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file())
        if members(left)!=members(right):raise ValueError('R2 artifact membership mismatch')
        comparisons=[]
        for name in members(left):
            h=file_hash(left/name);equal=h==file_hash(right/name)
            comparisons.append({'path':name,'bytes':(left/name).stat().st_size,'sha256':h,'byte_equal':equal})
            if not equal:raise ValueError('R2 reproducibility mismatch '+name)
        reuse=verify_reuse(config);after=count_actual_fit_calls(ROOT)
        if after!=before:raise ValueError('New actual fits detected')
        save(dest/'reproducibility.json',{'status':'MATCH','run_paths':runs,'comparison':'EXACT_ALL_ARTIFACT_BYTES',
             'files':comparisons,'baseline_full_replay_in_r2':False})
        save(dest/'verification_summary.json',{'status':'COMPLETED_R2_NO_FIT','implemented_checks_passed':True,
             'source_tree_hash':source['sha256'],'r2_lock_sha256':source['r2_lock']['sha256'],
             'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT).decode().strip(),
             'selected_run':runs[-1],'executions':executions,'actual_fit_before':before,'actual_fit_after':after,
             'actual_new_fits':after-before,'preserved_input_artifact_count':len(reuse['entries']),
             'baseline_full_replay_in_r2':False,'baseline_verification_reused':'finish_final_20261013_A',
             'independent_accuracy':None,'manual_ground_truth':'NOT_PERFORMED',
             'reproducibility_sha256':file_hash(dest/'reproducibility.json')})
        print('COMPLETED '+str(dest),flush=True)
    except BaseException as e:
        save(dest/'failure.json',{'status':'FAILED_OR_INCOMPLETE','error':str(e),'completed_commands':executions})
        raise


if __name__=='__main__':main()

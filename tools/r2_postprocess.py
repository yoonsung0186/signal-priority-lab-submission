"""Finite no-fit R2 postprocessing of hash-bound, previously verified evidence.

Never invokes the baseline, finish_verify, an agent, a scheduler or a fit.
Each execution requires a fresh output path. Existing inputs are read-only.
"""
import argparse
import csv
import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from spl.pet_io import file_hash
from spl.pet_ai import count_actual_fit_calls, FitLedger
from snapshot import snapshot
from preflight import safe_child


def save(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as f:
        json.dump(value, f, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        f.write('\n')


def source_snapshot():
    result = snapshot()
    p = ROOT / 'requirements-r2.lock'
    result['r2_lock'] = {'path':p.name, 'sha256':file_hash(p)}
    return result


def verify_reuse(config):
    reuse = json.loads(safe_child(ROOT, config['reuse_manifest']).read_bytes())
    names=[x['path'] for x in reuse['entries']]
    if not names or len(names)!=len(set(names)):raise ValueError('Duplicate or empty reuse manifest')
    required={config['signal_cache'],config['signal_archive'],config['scenarios_archive']}
    required.update(config['baseline_run']+'/'+n for n in ['event_ledger.csv','event_ledger_parquet_index.json','pet_qc.json','pet_thresholds.csv'])
    required.update(config['baseline_evidence_run']+'/'+n for n in ['ai_assessment.json','event_audit.json'])
    index_path=safe_child(ROOT,config['baseline_run']+'/event_ledger_parquet_index.json')
    if index_path.relative_to(ROOT).as_posix() not in names:raise ValueError('Unbound ledger index')
    # The index itself is hash-checked before using its member paths.
    entry=next(x for x in reuse['entries'] if x['path']==index_path.relative_to(ROOT).as_posix())
    if file_hash(index_path)!=entry['sha256']:raise ValueError('Ledger index changed')
    for x in json.loads(index_path.read_bytes())['shards']:
        ref=config['baseline_run']+'/'+x['path'];safe_child(ROOT,ref);required.add(ref)
    if not required<=set(names):raise ValueError('Consumed inputs absent from reuse manifest: '+str(sorted(required-set(names))))
    for x in reuse['entries']:
        p = safe_child(ROOT, x['path'])
        if p.stat().st_size != x['bytes'] or file_hash(p) != x['sha256']:
            raise ValueError('Preserved evidence changed: ' + x['path'])
    return reuse


def read_mappings(config):
    with zipfile.ZipFile(safe_child(ROOT, config['signal_archive'])) as z:
        with z.open('scenario_signal_mapping.csv.gz') as raw:
            rows = list(csv.DictReader(io.TextIOWrapper(gzip.GzipFile(fileobj=raw), encoding='utf-8')))
    if len({x['scenario_id'] for x in rows}) != len(rows):
        raise ValueError('Mapping ambiguity requires review')
    return {x['scenario_id']:x for x in rows}


def deny_fit(*args, **kwargs):
    raise RuntimeError('R2 permits zero new model fits')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--config', default='configs/r2_postprocess.json')
    p.add_argument('--out', required=True)
    a = p.parse_args()
    config = json.loads(safe_child(ROOT, a.config).read_bytes())
    if config['new_actual_fit_budget'] != 0: raise ValueError('Only no-fit R2 is implemented')
    out = safe_child(ROOT, a.out)
    if out.exists(): raise FileExistsError('Use a new immutable R2 output path')
    source = source_snapshot()
    reuse_digest=file_hash(safe_child(ROOT,config['reuse_manifest']))
    reuse = verify_reuse(config)
    before = count_actual_fit_calls(ROOT)
    if before != reuse['fit_count_before'] or before > reuse['fit_cap']:
        raise ValueError('Fit ledger drift')
    # Guard against accidental fit entry through future changes, without editing
    # or resetting the original persisted fit ledger or budget checks.
    FitLedger.fit = deny_fit
    import sklearn.cluster
    sklearn.cluster.KMeans.fit = deny_fit
    import pandas as pd
    import pyarrow.parquet as pq
    from spl.r2_units import analyze_units
    from spl.r2_signals import analyze_signals
    out.mkdir(parents=True)
    baseline = safe_child(ROOT, config['baseline_run'])
    index = json.loads((baseline/'event_ledger_parquet_index.json').read_bytes())
    columns = ['event_id','event_key','scenario_id','encroaching_object_id','priority_object_id',
               'pet_s','qc_status','event_date_local','n_source_rows','pet_eligible','conflict_lon_deg','conflict_lat_deg',
               'ts_enter_encroaching_ms','ts_leave_encroaching_ms','ts_enter_priority_ms','ts_leave_priority_ms']
    frames = [pq.read_table(safe_child(ROOT,config['baseline_run']+'/'+x['path']), columns=columns).to_pandas() for x in index['shards']]
    ledger = pd.concat(frames, ignore_index=True)
    del frames
    if len(ledger) != index['row_count']: raise ValueError('Ledger population mismatch')
    print('START observation-unit and date sensitivity', flush=True)
    units = analyze_units(ledger, thresholds=tuple(config['thresholds_s']),
                          bootstrap_repetitions=config['bootstrap_repetitions'], seed=config['bootstrap_seed'])
    del ledger
    print('START signal coverage and event-only stress', flush=True)
    signals = analyze_signals(baseline/'event_ledger.csv', safe_child(ROOT, config['signal_cache']), read_mappings(config),
                              offsets_s=tuple(config['offsets_s']), thresholds_s=tuple(config['thresholds_s']),
                              local_timezone=config['local_timezone'], min_cell_events=config['min_cell_events'],
                              source_signal_qc=json.loads((baseline/'pet_qc.json').read_bytes())['signal_rows'])
    all_tables={}
    for family, result in [('units', units), ('signals', signals)]:
        for name, table in result['tables'].items():
            target=out/(name+'.csv')
            if not hasattr(table, 'to_csv'): table=pd.DataFrame(table)
            all_tables[name]=table
            with target.open('x',encoding='utf-8',newline='') as f:
                table.to_csv(f,index=False,lineterminator='\n',float_format='%.17g',na_rep='')
        save(out/(family+'_metadata.json'), {k:v for k,v in result.items() if k!='tables'})
    from spl.r2_reporting import candidate_table, create_figures
    with zipfile.ZipFile(safe_child(ROOT, config['scenarios_archive'])) as z:
        definitions={x['scenario_id']:x['description'] for x in csv.DictReader(io.StringIO(z.read('scenarios/scenarios.csv').decode()))}
    candidates=candidate_table(all_tables,definitions)
    candidates.to_csv(out/'inspection_candidates_r2.csv',index=False,lineterminator='\n',float_format='%.17g')
    save(out/'figures_index.json',create_figures(out,all_tables))
    # Numeric consistency checks compare to preserved evidence, never coerce
    # newly computed results to the previous reference numbers.
    qc=json.loads((baseline/'pet_qc.json').read_bytes())
    point=all_tables['unit_summary']
    A=point[(point.scenario_id=='ALL')&(point.unit=='A')]
    if not A.denominator.eq(qc['n_valid_nonnegative_pet']).all():raise ValueError('A baseline denominator mismatch')
    if not A.n_raw.eq(qc['n_raw']).all() or not A.n_negative_initial_eligible.eq(qc['n_negative_pet']).all():
        raise ValueError('Baseline raw/negative count mismatch')
    for name,numerator,denominator in [('unit_summary','numerator','denominator'),
                                     ('signal_condition_pet','n_short_pet','n_valid_nonnegative_pet')]:
        table=all_tables[name]
        if not ((table[numerator]>=0)&(table[numerator]<=table[denominator])).all():raise ValueError('Numerator partition failure '+name)
        fraction='fraction' if name=='unit_summary' else 'short_pet_fraction'
        if not table.loc[table[denominator]==0,fraction].isna().all():raise ValueError('Zero denominator not null')
    old=pd.read_csv(baseline/'pet_thresholds.csv')
    joined=point[point.unit=='A'].merge(old,on=['scenario_id','tau_s'])
    if len(joined)!=len(old) or not (joined.numerator==joined.n_short_pet).all():raise ValueError('A baseline threshold mismatch')
    if not (joined.denominator==joined.n_valid_nonnegative_pet).all():raise ValueError('A scenario denominator mismatch')
    for _,group in point.groupby(['unit','scenario_id'],sort=True):
        if not group.sort_values('tau_s').numerator.is_monotonic_increasing:raise ValueError('Threshold nonmonotonicity')
    condition=all_tables['signal_condition_pet']
    for (scenario,tau),group in condition.groupby(['scenario_id','tau_s'],sort=True):
        reference=point[(point.unit=='A')&(point.scenario_id==scenario)&(point.tau_s==tau)].iloc[0]
        if group.n_short_pet.sum()!=reference.numerator or group.n_valid_nonnegative_pet.sum()!=reference.denominator:
            raise ValueError('Signal condition cells fail baseline partition')
    facts=[]
    for row in point.to_dict('records'):
        facts.append({'fact_id':'unit_'+row['unit']+'_'+row['scenario_id']+'_'+str(row['tau_s']),
            'metric_name':'Short PET conditional fraction','status':'COMPUTED' if row['denominator'] else 'UNDEFINED_ZERO_DENOMINATOR',
            'value':row['numerator']/row['denominator'] if row['denominator'] else None,'unit':'fraction',
            'numerator':row['numerator'],'denominator':row['denominator'],'population_definition':row['filter'],
            'tau_s':row['tau_s'],'scenario_id':row['scenario_id'],'data_kind':'REAL',
            'source_artifact_ids':['unit_summary.csv'],'code_ref':'src/spl/r2_units.py',
            'config_hash':file_hash(safe_child(ROOT,a.config)),'source_tree_hash':source['sha256'],
            'uncertainty_kind':'DESCRIPTIVE_POINT_WITH_SEPARATE_SHARED_DATE_RANK_SENSITIVITY',
            'limitations':['Not traffic exposure or accident probability','C/D geometric equivalence unverified']})
    with (out/'metric_facts.jsonl').open('x',encoding='utf-8',newline='\n') as f:
        for fact in facts:f.write(json.dumps(fact,sort_keys=True,ensure_ascii=False,allow_nan=False)+'\n')
    save(out/'numeric_checks.json',{'status':'MATCH','A_baseline_counts_and_thresholds':True,
         'fractions_bounded':True,'zero_denominator_null':True,'threshold_monotonicity':True,
         'signal_condition_partition_matches_A':True,'source_qc':qc,
         'baseline_unchanged':True,'observation_unit_changes_intentional':True})
    for name in ['ai_assessment.json','event_audit.json']:
        original = safe_child(ROOT, config['baseline_evidence_run']+'/'+name)
        with (out/('reused_'+name)).open('xb') as f: f.write(original.read_bytes())
    after = count_actual_fit_calls(ROOT)
    if after != before or source_snapshot() != source or file_hash(safe_child(ROOT,config['reuse_manifest']))!=reuse_digest:
        raise ValueError('Fit/source/reuse-manifest drift during R2')
    save(out/'reproducibility_context.json', {'config':config,'config_sha256':file_hash(safe_child(ROOT,a.config)),
         'source':source,'postprocess_source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT).decode().strip(),
         'reuse_manifest_sha256':reuse_digest,
         'actual_fit_before':before,'actual_fit_after':after,'actual_new_fits':after-before,
         'baseline_full_replay_in_r2':False,'baseline_automatic_numeric_audit_reused':8699,
         'independent_accuracy':None,'manual_ground_truth':'NOT_PERFORMED',
         'new_computations':['observation_unit_denominators','period_common_date_rank_sensitivity',
                             'signal_coverage','event_signal_transitions','signal_condition_pet'],
         'unchanged_ai_population':'Frozen 100000-event sample; not mixed with full-ledger denominators'})
    save(out/'artifact_manifest.json', {'files':[{'path':x.relative_to(out).as_posix(),'bytes':x.stat().st_size,'sha256':file_hash(x)}
         for x in sorted(out.rglob('*')) if x.is_file()]})
    print('COMPLETED '+a.out,flush=True)


if __name__=='__main__': main()

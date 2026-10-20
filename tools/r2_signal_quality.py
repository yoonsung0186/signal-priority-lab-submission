"""Finite additive quality-boundary overlay; never rerun R2 or fit a model."""
import argparse
import csv
import io
from decimal import Decimal
import json
import platform
import sqlite3
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from r2_postprocess import ROOT, save, source_snapshot, verify_reuse, read_mappings, deny_fit
from preflight import safe_child
from spl.pet_io import file_hash
from spl.pet_ai import count_actual_fit_calls, FitLedger


def write_overlay_csv(path,table,original):
    """Keep every literal CSV field string, including its decimal spelling."""
    with original.open(encoding='utf-8',newline='') as stream:
        reader=csv.DictReader(stream);columns=reader.fieldnames;rows=list(reader)
    rendered=list(csv.DictReader(io.StringIO(table.to_csv(index=False,lineterminator='\n',float_format='%.17g',na_rep=''))))
    if list(table.columns[:len(columns)])!=columns or len(rows)!=len(rendered):
        raise ValueError('Literal overlay columns or row order contract changed')
    keys=[key for key in ['scenario_id','encroaching_entry_signal','priority_entry_signal','signal_group_id','start_timestamp_ms','end_timestamp_ms'] if key in columns]
    for old,new in zip(rows,rendered):
        if any(old[key]!=new[key] for key in keys):raise ValueError('Literal overlay row identity changed')
        if 'tau_s' in columns and Decimal(old['tau_s'])!=Decimal(new['tau_s']):raise ValueError('Literal threshold order changed')
        new.update(old)
    with path.open('x',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(table.columns),lineterminator='\n');writer.writeheader();writer.writerows(rendered)


def check_bindings(config):
    for row in config['bindings']:
        p=safe_child(ROOT,row['path'])
        if p.stat().st_size!=row['bytes'] or file_hash(p)!=row['sha256']:
            raise ValueError('Bound R2 evidence changed: '+row['path'])
    original=safe_child(ROOT,config['r2_review_root']+'/analysis/artifact_manifest.json')
    manifest=json.loads(original.read_bytes())
    run=safe_child(ROOT,config['r2_run'])
    if (run/'artifact_manifest.json').read_bytes()!=original.read_bytes():
        raise ValueError('Original R2 manifest changed')
    expected={x['path'] for x in manifest['files']}|{'artifact_manifest.json'}
    actual={p.relative_to(run).as_posix() for p in run.rglob('*') if p.is_file()}
    if expected!=actual:raise ValueError('Original R2 membership changed')
    for row in manifest['files']:
        p=safe_child(ROOT,config['r2_run']+'/'+row['path'])
        if p.stat().st_size!=row['bytes'] or file_hash(p)!=row['sha256']:
            raise ValueError('Original R2 output changed')
    web=json.loads(safe_child(ROOT,config['r2_review_root']+'/web_artifact_manifest.json').read_bytes())
    for row in web['files']:
        p=safe_child(ROOT,config['r2_review_root']+'/'+row['path'])
        if p.stat().st_size!=row['bytes'] or file_hash(p)!=row['sha256']:
            raise ValueError('Original R2 review changed')
    return len(expected),len(web['files'])


def check_reused_context(config,oldconfig,oldconfig_hash):
    context=json.loads(safe_child(ROOT,config['r2_run']+'/reproducibility_context.json').read_bytes())
    if context['config']!=oldconfig or context['config_sha256']!=oldconfig_hash:
        raise ValueError('Consumed baseline configuration differs from completed R2')
    if context['postprocess_source_commit']!=config['r2_source_commit']:
        raise ValueError('Reused R2 source identity mismatch')
    if file_hash(safe_child(ROOT,oldconfig['reuse_manifest']))!=context['reuse_manifest_sha256']:
        raise ValueError('Baseline reuse manifest differs from completed R2')
    if file_hash(ROOT/'requirements-r2.lock')!=context['source']['r2_lock']['sha256']:
        raise ValueError('R2 environment lock changed')
    for row in context['source']['files']:
        path=safe_child(ROOT,row['path'])
        if path.stat().st_size!=row['bytes'] or file_hash(path)!=row['sha256']:
            raise ValueError('Original R2 source changed: '+row['path'])


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',default='configs/r2_signal_quality.json')
    parser.add_argument('--out',required=True)
    args=parser.parse_args()
    configpath=safe_child(ROOT,args.config);config=json.loads(configpath.read_bytes())
    if config['new_actual_fit_budget']!=0:raise ValueError('Only zero-fit closure allowed')
    out=safe_child(ROOT,args.out)
    if out.exists():raise FileExistsError('Fresh output directory required')
    source=source_snapshot();source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT).decode().strip()
    oldconfigpath=safe_child(ROOT,config['r2_config']);oldconfig=json.loads(oldconfigpath.read_bytes())
    config_hash=file_hash(configpath);oldconfig_hash=file_hash(oldconfigpath)
    original_counts=check_bindings(config)
    check_reused_context(config,oldconfig,oldconfig_hash)
    reuse=verify_reuse(oldconfig)
    before=count_actual_fit_calls(ROOT)
    if before!=reuse['fit_count_before'] or before>reuse['fit_cap']:raise ValueError('Fit ledger drift')
    FitLedger.fit=deny_fit
    import sklearn.cluster
    sklearn.cluster.KMeans.fit=deny_fit
    import pandas as pd
    import pyarrow.parquet as pq
    from spl.r2_signal_quality import analyze_boundary
    from spl.r2_signals import ReadOnlySignals
    baseline=safe_child(ROOT,oldconfig['baseline_run'])
    index=json.loads((baseline/'event_ledger_parquet_index.json').read_bytes())
    columns=['event_key','scenario_id','pet_s','qc_status','ts_enter_encroaching_ms','ts_enter_priority_ms']
    ledger=pd.concat([pq.read_table(safe_child(ROOT,oldconfig['baseline_run']+'/'+x['path']),columns=columns).to_pandas() for x in index['shards']],ignore_index=True)
    if len(ledger)!=index['row_count']:raise ValueError('Ledger count mismatch')
    prior=safe_child(ROOT,config['r2_review_root']+'/analysis')
    crossings=pd.read_csv(prior/'signal_missing_period_crossings.csv')
    literal=pd.read_csv(prior/'signal_condition_pet.csv')
    cache=safe_child(ROOT,oldconfig['signal_cache'])
    db=sqlite3.connect(cache.resolve().as_uri()+'?mode=ro',uri=True)
    db.execute('PRAGMA query_only=ON')
    try:
        for x in crossings.itertuples():
            found=db.execute('SELECT state,si,rn FROM signals WHERE s=? AND e=? AND g=?',(int(x.start_timestamp_ms),int(x.end_timestamp_ms),x.signal_group_id)).fetchall()
            if found!=[(x.signal_state,int(x.source_index),int(x.source_row_id))]:
                raise ValueError('Long interval differs from preserved source cache')
    finally:db.close()
    mapping=read_mappings(oldconfig)
    qc=json.loads((baseline/'pet_qc.json').read_bytes())['signal_rows']
    reader=ReadOnlySignals(cache,list(crossings.signal_group_id),ZoneInfo(oldconfig['local_timezone']),qc)
    queries=0
    def lookup(group,timestamp):
        nonlocal queries
        queries+=1
        return reader.lookup(group,timestamp)
    print('START bounded quality overlay; original R2 outputs reused',flush=True)
    try:result=analyze_boundary(ledger,crossings,mapping,literal,lookup,offsets_s=tuple(config['offsets_s']))
    finally:reader.close()
    after=count_actual_fit_calls(ROOT)
    if before!=after or source_snapshot()!=source or file_hash(configpath)!=config_hash or file_hash(oldconfigpath)!=oldconfig_hash:
        raise ValueError('Fit/source/config drift')
    out.mkdir(parents=True)
    for name,table in result['tables'].items():
        original={'signal_condition_quality':'signal_condition_pet.csv','quality_intervals':'signal_missing_period_crossings.csv'}.get(name)
        if original:
            write_overlay_csv(out/(name+'.csv'),table,prior/original)
            continue
        with (out/(name+'.csv')).open('x',encoding='utf-8',newline='') as stream:
            table.to_csv(stream,index=False,lineterminator='\n',float_format='%.17g',na_rep='')
    save(out/'summary.json',result['summary'])
    save(out/'provenance.json',{'config':config,'config_sha256':config_hash,'r2_config_sha256':oldconfig_hash,
         'source_snapshot':source,'source_commit':source_commit,'python':platform.python_version(),
         'baseline_reuse_manifest_sha256':file_hash(safe_child(ROOT,oldconfig['reuse_manifest'])),
         'baseline_preserved_entries':len(reuse['entries']),'r2_artifacts_preserved':original_counts[0],
         'r2_review_manifest_members_preserved':original_counts[1],
         'actual_fit_before':before,'actual_fit_after':after,'actual_new_fits':after-before,
         'new_signal_lookups':queries,'signal_values_modified':False,'baseline_recomputed':False,'r2_recomputed':False,
         'pet_units_ranks_ai':'Reused without recomputation; original R2 manifest hashes checked',
         'quality_flag_is_confirmed_signal_error':False,'manual_ground_truth':'NOT_PERFORMED'})
    save(out/'artifact_manifest.json',{'files':[{'path':p.name,'bytes':p.stat().st_size,'sha256':file_hash(p)} for p in sorted(out.iterdir()) if p.is_file()]})
    if source_snapshot()!=source or file_hash(configpath)!=config_hash or file_hash(oldconfigpath)!=oldconfig_hash or count_actual_fit_calls(ROOT)!=before:
        raise ValueError('Final source/config/fit drift')
    if verify_reuse(oldconfig)!=reuse or check_bindings(config)!=original_counts:
        raise ValueError('Final preservation check failed')
    check_reused_context(config,oldconfig,oldconfig_hash)
    print('COMPLETED '+args.out,flush=True)


if __name__=='__main__':main()

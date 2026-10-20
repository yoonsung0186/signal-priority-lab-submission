"""Profile-local PET QC and first provider-derived descriptive analysis."""
import importlib.util,json,os,sys,time
from pathlib import Path
from .pet_artifacts import write_json,write_table,figures
from .pet_contracts import digest
from .pet_io import load_profile,load_tables
from .pet_metrics import summarize,sensitivity_rows,bootstrap_descriptions
from .pet_qc import build_ledger
from .pet_signals import contexts
from .safety import verify_root,child,new_run

def ai_status(validation):
    if validation['status'].startswith('FITTED'):return 'EXPLORATORY_ANALYSIS_PERFORMED'
    if validation['status'].startswith('FAILED'):return 'AI_ATTEMPTED_FAILED'
    if validation.get('fit_call_count',0)>0:return 'AI_INCOMPLETE'
    return 'NOT_RUN'

def source_snapshot(root):
    if str(root/'tools') not in sys.path:sys.path.insert(0,str(root/'tools'))
    import snapshot
    return snapshot.snapshot(),snapshot.environment_manifest_ref(root)

def run_pet(root,profile_relative,output,mode='pet-analyze'):
    root=verify_root(root);target=child(root,output)
    if target.exists():raise FileExistsError('PET output exists; use a fresh run')
    profile,rights,config_hash=load_profile(root,profile_relative)
    os.environ['MPLCONFIGDIR']=str(child(root,'.cache/matplotlib_pet'))
    before,environment_ref=source_snapshot(root)
    start=time.monotonic();tables,signals,inventory=load_tables(root,profile)
    ledger,qc,mappings,scenarios=build_ledger(tables,profile,inventory)
    tables.clear()
    context=contexts(ledger,mappings,signals)
    metrics=summarize(ledger,context,scenarios,profile.get('thresholds_s',[.5,1,1.5,2]))
    timing=sensitivity_rows(ledger,mappings,signals,profile.get('clock_offsets_s',[-.5,-.2,0,.2,.5]))
    bootstrap,bootstrap_status=bootstrap_descriptions(ledger,metrics,profile)
    qc['signal_rows']=dict(signals.qc);qc['signal_invalid_examples']=signals.invalid_examples
    qc['signal_count_population']='VALID_NONNEGATIVE_PET_INTERACTIONS'
    directory=new_run(root,output)
    write_json(directory/'source_inventory.json',inventory);write_json(directory/'pet_qc.json',qc)
    export_ledger=[]
    for row in ledger:
        exported={key:value for key,value in row.items() if key not in {'provider_fields','conflicting_source_variants'}}
        variants=row['conflicting_source_variants']
        exported['n_conflicting_source_variants']=len(variants)
        exported['conflicting_field_names']=sorted({key for variant in variants for key,value in variant.items() if row['provider_fields'].get(key)!=value})
        exported['invalid_original_values']={key:row['provider_fields'].get(key) for key in ('event_id','pet_s','encroaching_object_id','priority_object_id','conflict_lon_deg','conflict_lat_deg','ts_enter_encroaching_ms','ts_leave_encroaching_ms','ts_enter_priority_ms','ts_leave_priority_ms') if row.get(key) is None}
        export_ledger.append(exported)
    all_tables={'event_ledger':export_ledger,'signal_context':context,'timing_sensitivity':timing,'date_cluster_intervals':bootstrap,**metrics}
    for name,rows in all_tables.items():write_table(directory,name,rows)
    ai_tables={}
    if mode=='pet-analyze':
        from .pet_ai import analyze_types,count_actual_fit_calls
        ai,ai_tables=analyze_types(ledger,profile.get('ai',{}),directory/'fit_attempts.jsonl',profile.get('scope','SAMPLE_DAY'),cumulative_counter=lambda:count_actual_fit_calls(root))
        for name,rows in ai_tables.items():write_table(directory,name,rows)
        if 'model' in ai:write_json(directory/'cluster_model.json',ai['model'])
    else:ai={'status':'NOT_RUN','reason':'PET_QC_ONLY_COMMAND','fit_call_count':0,'heldout_metrics':None,'accuracy':None}
    write_json(directory/'cluster_validation.json',ai)
    figures(directory,metrics,inventory['inputs'],ai,ai_tables)
    status={'profile_id':'PET_INTERACTIONS_V1','scope':profile.get('scope'),'data_kind':'REAL','mode':mode,
            'pet_qc_status':'COMPLETED_PROVIDER_DERIVED_QC','pet_analysis_status':'COMPLETED_DESCRIPTIVE_BASELINE',
            'ai_status':ai_status(ai),'ai_attempted':ai['fit_call_count']>0,'ai_failure_or_unavailable_reason':ai.get('reason'),
            'ai_detailed_status':ai['status'],'n_raw':qc['n_raw'],'n_valid_nonnegative_pet':qc['n_valid_nonnegative_pet'],
            'n_negative_pet':qc['n_negative_pet'],'n_invalid':qc['n_invalid'],'fit_call_count':ai['fit_call_count'],
            'manual_review_status':'NOT_PERFORMED','independent_accuracy':None,
            'original_straight_entry_profile_status':'BLOCKED_UNVERIFIED_BOUNDARY_AND_MOVEMENT_SIGNAL_MAPPING',
            'not_performed':(['AI fitting/evaluation'] if ai['fit_call_count']==0 else [])+(['heldout/generalization evaluation'] if ai.get('heldout_metrics') is None else [])+
                            (['multi-day bootstrap/rank stability'] if bootstrap_status['status']=='NOT_APPLICABLE' else [])+['independent PET geometric accuracy','paper filter validation'],
            'population_definition':qc['population_definition']}
    status['not_completed']=['AI model selection/stability/evaluation'] if ai['fit_call_count']>0 and not ai['status'].startswith('FITTED') else []
    status.update(input_sha256s=[item['sha256'] for item in inventory['inputs']],bootstrap_status=bootstrap_status,
                  implemented_features=['PROVIDER_EVENT_QC','COMPOSITE_KEY_RECONCILIATION','SCENARIO_ROLE_OBJECT_RESOLUTION',
                                        'CONFLICT_AREA_SIGNAL_CONTEXT','CONDITIONAL_PET_DESCRIPTIONS','EVENT_ONLY_CLOCK_SENSITIVITY'],
                  metrics={'n_raw':qc['n_raw'],'n_valid_nonnegative_pet':qc['n_valid_nonnegative_pet'],'n_negative_pet':qc['n_negative_pet'],'n_invalid':qc['n_invalid']},
                  limitations=['Not stop-line entry or violation counts','Not all-traffic risk denominator','Provider PET, no independent geometric accuracy'])
    if ai['status'].startswith('FITTED'):status['implemented_features'].append('BOUNDED_OCCUPANCY_SIZE_KMEANS_TYPES')
    write_json(directory/'pet_analysis.json',status)
    write_json(directory/'pet_result.json',status)
    canonical_files=[path.relative_to(directory).as_posix() for path in sorted(directory.rglob('*')) if path.is_file() and path.parts[-2]!='figures' and path.name not in {'figures_index.json','fit_attempts.jsonl'}]
    write_json(directory/'run_manifest.json',{'profile_id':'PET_INTERACTIONS_V1','data_kind':'REAL','canonical_files':canonical_files,
               'comparison_scope':'Stable technical tables and JSON only; provenance, plot pixels and fit attempt logs excluded',
               'local_full_csv_retained':'Oversized CSV may be omitted from handoff with explicit reason; Parquet shards remain'})
    after,_=source_snapshot(root)
    if before!=after:
        write_json(directory/'run_failure.json',{'status':'SOURCE_CHANGED_DURING_PET_RUN','results_preserved':True})
        raise ValueError('PET source changed during execution')
    files={path.relative_to(directory).as_posix():{'bytes':path.stat().st_size,'sha256':digest(path.read_bytes())} for path in sorted(directory.rglob('*')) if path.is_file()}
    environment_path=root/environment_ref
    provenance={'profile_id':'PET_INTERACTIONS_V1','data_kind':'REAL','status':status,'source_snapshot':before,
                'inputs':inventory['inputs'],'input_sha256_list':[item['sha256'] for item in inventory['inputs']],
                'config_ref':profile_relative,'config_sha256':config_hash,'rights_manifest_ref':profile['rights_manifest'],
                'rights_manifest_sha256':digest(child(root,profile['rights_manifest']).read_bytes()),'rights':rights['rights'],
                'environment_manifest_ref':environment_ref,'environment_manifest_sha256':digest(environment_path.read_bytes()),
                'lock_sha256':digest(child(root,'requirements.lock').read_bytes()),'files':files,'fit_call_count':ai['fit_call_count'],
                'elapsed_seconds':time.monotonic()-start,'command':[sys.executable,'-m','spl',mode,'--profile',profile_relative,'--output',output],
                'provider_definition_evidence':'Official schema plus examples README; independent paper/geometry validation not claimed',
                'export_omissions':{'provider_fields':'Full provider row payload omitted; required scalar values and source refs retained, original raw ZIP immutable',
                                    'conflicting_source_variants':'Full raw variant payload omitted; variant count, differing field names and all source refs retained'},
                'raw_archives_packaged':False,'tracks_parsed':False,'report_generated':False,'external_submission_performed':False}
    write_json(directory/'provenance.json',provenance)
    return directory,status

"""Row reconciliation and PET eligibility independent of signal availability."""
from collections import Counter
from datetime import datetime,timezone
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from .pet_contracts import TIMES,finite,integer,key_value,reconcile

def known_relation(row,prefix=''):
    fields=[prefix+field for field in ('movement_category','conflict_leg','turning_movement')]
    if not all(field in row for field in fields):return False
    # A present empty turning field means documented N/A. Missing category/leg
    # or explicit unknown markers cannot establish a scenario role.
    markers={'UNKNOWN','UNVERIFIED','NONE','NULL','N/A','NA','NAN','UNRESOLVED'}
    known=all(isinstance(row[field],str) and row[field].strip() and row[field].strip().upper() not in markers for field in fields[:2])
    turning=row[fields[2]]
    return known and isinstance(turning,str) and turning.strip().upper() not in markers

def event_time(timestamp_ms,local_timezone='Europe/Vienna'):
    if timestamp_ms is None:return None,None,None
    try:
        utc=datetime.fromtimestamp(timestamp_ms/1000,timezone.utc)
        local=utc.astimezone(ZoneInfo(local_timezone))
        return utc.isoformat(),utc.date().isoformat(),local
    except (ValueError,OverflowError,OSError):return None,None,None
    except ZoneInfoNotFoundError:
        utc=datetime.fromtimestamp(timestamp_ms/1000,timezone.utc)
        return utc.isoformat(),utc.date().isoformat(),None

def build_ledger(tables,profile,inventory=None):
    dataset,version=profile.get('dataset_id','SYNTHETIC'),str(profile.get('source_version','TEST'))
    kind=profile.get('data_kind','SYNTHETIC')
    objects,missing_objects,object_qc=reconcile(tables.get('objects',[]),'object_id',True)
    scenarios,missing_scenarios,scenario_qc=reconcile(tables.get('scenarios',[]),'scenario_id')
    mappings,missing_mapping,mapping_qc=reconcile(tables.get('scenario_signal_mapping',[]),'scenario_id')
    events,missing_events,event_qc=reconcile(tables.get('pet_events',[]),'event_id',True)
    ledger=[];reason_counts=Counter();warnings=Counter()
    all_events=[*events.items(),*((None,row) for row in missing_events)]
    for index,(event_id,entry) in enumerate(all_events):
        raw=entry['raw'];reasons=[];review=[]
        if event_id is None:reasons.append('MISSING_OR_INVALID_EVENT_KEY')
        if entry['conflict']:reasons.append('CONFLICTING_EVENT_KEY')
        scenario_id=key_value(raw.get('scenario_id'));scenario=scenarios.get(scenario_id)
        if scenario is None:reasons.append('MISSING_SCENARIO_FOREIGN_KEY')
        elif scenario['conflict']:reasons.append('CONFLICTING_SCENARIO_KEY')
        pet=finite(raw.get('pet_s'))
        if pet is None:reasons.append('MISSING_OR_NONFINITE_PET')
        timestamps={field:integer(raw.get(field)) for field in TIMES}
        if any(value is None for value in timestamps.values()):reasons.append('MISSING_OR_INVALID_TIMESTAMP')
        elif timestamps[TIMES[1]]<timestamps[TIMES[0]] or timestamps[TIMES[3]]<timestamps[TIMES[2]]:reasons.append('LEAVE_BEFORE_ENTER')
        lon,lat=finite(raw.get('conflict_lon_deg')),finite(raw.get('conflict_lat_deg'))
        if lon is None or lat is None or not -180<=lon<=180 or not -90<=lat<=90:reasons.append('INVALID_WGS84_CONFLICT_POINT')
        first=min((value for value in timestamps.values() if value is not None),default=None)
        utc,date,local=event_time(first,profile.get('local_timezone','Europe/Vienna'))
        if utc is None:reasons.append('UNREPRESENTABLE_UTC_TIMESTAMP')
        if local is None:review.append('LOCAL_TIMEZONE_UNAVAILABLE')
        sources=[]
        for source in entry['sources']:
            origin=dict(source)
            if inventory is not None:
                origin['source_file_sha256']=inventory['tables'][source['source_index']]['source_file_sha256']
            sources.append(origin)
        event={'dataset_id':dataset,'source_version':version,'data_kind':kind,
               'event_key':dataset+'/'+version+'/'+(event_id if event_id is not None else 'INVALID_KEY_'+str(index)),
               'event_id':event_id,'scenario_id':scenario_id,'pet_s':pet,**timestamps,
               'conflict_lon_deg':lon,'conflict_lat_deg':lat,'provider_fields':raw,
               'source_row_id':sources[0].get('source_row_id') if sources else None,
               'source_file_sha256':sorted({source.get('source_file_sha256','SYNTHETIC_FIXTURE') for source in sources}),
               'source_refs':sources,'n_source_rows':len(sources),'conflicting_source_variants':entry['variants'] if entry['conflict'] else [],
               'event_utc':utc,'event_date_utc':date,'event_date_local':local.date().isoformat() if local else None,
               'local_4h_bin':(local.hour//4)*4 if local else None,'local_timezone':profile.get('local_timezone','Europe/Vienna')}
        for role in ('encroaching','priority'):
            object_id=key_value(raw.get(role+'_object_id'),True);obj=objects.get(object_id)
            event[role+'_object_id']=object_id
            if obj is None:reasons.append('MISSING_'+role.upper()+'_OBJECT_FOREIGN_KEY')
            variants=obj['variants'] if obj else []
            matches=[]
            if scenario and not scenario['conflict'] and known_relation(scenario['raw'],role+'_'):
                matches=[i for i,row in enumerate(variants) if known_relation(row) and all(row[field]==scenario['raw'][role+'_'+field] for field in ('movement_category','conflict_leg','turning_movement'))]
            chosen=[variants[i] for i in matches] if matches else variants
            join_status=('MISSING_OBJECT' if not obj else 'UNIQUE_RECORD' if len(variants)==1 and len(matches)==1 else
                         'RESOLVED_BY_SCENARIO_RELATION' if len(matches)==1 else
                         'MULTIPLE_SCENARIO_ROLE_MATCHES_STATIC_CONSENSUS_ONLY' if matches else 'NO_SCENARIO_ROLE_MATCH_STATIC_CONSENSUS_ONLY')
            event[role+'_object_join_status']=join_status
            if obj and not matches:review.append(role.upper()+'_OBJECT_SCENARIO_RELATION_UNRESOLVED')
            elif len(matches)>1:review.append(role.upper()+'_MULTIPLE_OBJECT_ROLE_MATCHES')
            classes={row.get('object_class_static') for row in chosen}
            event[role+'_class']=next(iter(classes)) if len(classes)==1 and None not in classes and '' not in classes else None
            event[role+'_object_source_refs']=[origin for i in (matches or range(len(variants))) for origin in obj['variant_sources'][i]] if obj else []
            event[role+'_object_variant_count']=len(variants)
            for dimension in ('length','width'):
                values={finite(row.get(dimension+'_m_static')) for row in chosen}
                value=next(iter(values)) if len(values)==1 else None
                event[role+'_'+dimension+'_m']=value if value is not None and value>0 else None
                if len(values)>1:review.append(role.upper()+'_STATIC_'+dimension.upper()+'_AMBIGUOUS')
            enter,leave=timestamps['ts_enter_'+role+'_ms'],timestamps['ts_leave_'+role+'_ms']
            event[role+'_occupancy_s']=(leave-enter)/1000 if enter is not None and leave is not None and leave>=enter else None
        if event['encroaching_object_id'] is not None and event['encroaching_object_id']==event['priority_object_id']:reasons.append('SAME_OBJECT_BOTH_ROLES')
        if pet is not None and pet<0:review.append('NEGATIVE_PET_REVIEW')
        if pet==0:review.append('ZERO_PET_REVIEW_TIME_RESOLUTION')
        # Provider-derived PET remains authoritative. This is a document-based
        # timestamp identity check, not independent geometric accuracy.
        if pet is not None and timestamps['ts_enter_priority_ms'] is not None and timestamps['ts_leave_encroaching_ms'] is not None:
            event['provider_formula_difference_s']=pet-(timestamps['ts_enter_priority_ms']-timestamps['ts_leave_encroaching_ms'])/1000
            if abs(event['provider_formula_difference_s'])>1e-6:review.append('PROVIDER_TIMESTAMP_FORMULA_DISAGREEMENT_REVIEW')
        else:event['provider_formula_difference_s']=None
        reasons=sorted(set(reasons));review=sorted(set(review))
        event.update(pet_eligible=not reasons,valid_nonnegative_pet=not reasons and pet>=0,
                     qc_status='INVALID' if reasons else 'NEGATIVE_PET_REVIEW' if pet<0 else 'VALID_NONNEGATIVE_PET',
                     invalid_reasons=reasons,review_reasons=review,
                     provider_formula_basis='PROVIDER_EXAMPLES_README_NOT_INDEPENDENT_GEOMETRY',independent_accuracy=None)
        reason_counts.update(reasons);warnings.update(review);ledger.append(event)
    ledger.sort(key=lambda row:row['event_key'])
    partition=Counter(row['qc_status'] for row in ledger)
    qc={'profile_id':'PET_INTERACTIONS_V1','data_kind':kind,'event_rows':event_qc,'object_rows':object_qc,
        'scenario_rows':scenario_qc,'mapping_rows':mapping_qc,'n_raw':event_qc['n_raw'],'n_unique_event_keys':len(ledger),
        'n_valid_nonnegative_pet':partition['VALID_NONNEGATIVE_PET'],'n_negative_pet':partition['NEGATIVE_PET_REVIEW'],
        'n_invalid':partition['INVALID'],'n_zero_pet':sum(row['pet_eligible'] and row['pet_s']==0 for row in ledger),
        'invalid_reason_counts':dict(sorted(reason_counts.items())),'review_reason_counts':dict(sorted(warnings.items())),
        'reconciliation':{'unique_partition_preserved':len(ledger)==sum(partition.values()),
                          'raw_rows_preserved':event_qc['n_raw']==len(ledger)+event_qc['n_identical_duplicate_rows_merged']+event_qc['n_conflicting_duplicate_rows_extra']},
        'missing_static_key_counts':{'objects':len(missing_objects),'scenarios':len(missing_scenarios),'mapping':len(missing_mapping)},
        'population_definition':'Publisher-recorded interactions in fixed scenarios, not all traffic participants',
        'manual_review_status':'NOT_PERFORMED','independent_pet_accuracy':None,
        'paper_formula_and_filter_definition_status':'UNVERIFIED_UNLESS_MAIN_PROVIDES_OFFICIAL_EVIDENCE'}
    return ledger,qc,mappings,scenarios

"""Conditional PET descriptions; never vehicle exposure or violation rates."""
from collections import Counter,defaultdict
import math

def quantile(values,q):
    if not values:return None
    ordered=sorted(values);position=(len(ordered)-1)*q;lo=math.floor(position);hi=math.ceil(position)
    return ordered[lo]+(position-lo)*(ordered[hi]-ordered[lo])

def summarize(ledger,context,scenarios,thresholds=(0.5,1,1.5,2)):
    grouped=defaultdict(list)
    for row in ledger:grouped[row['scenario_id'] or 'UNKNOWN_SCENARIO'].append(row)
    for scenario in scenarios:grouped.setdefault(scenario,[])
    ctx=defaultdict(dict)
    for row in context:ctx[row['event_key']][row['role']]=row
    summaries=[];threshold_rows=[];ecdf=[];candidates=[];bins=defaultdict(list)
    for scenario,rows in sorted(grouped.items()):
        valid=[row for row in rows if row['valid_nonnegative_pet']];values=[row['pet_s'] for row in valid]
        all_values=[row['pet_s'] for row in rows if row['pet_s'] is not None]
        known=[row for row in valid if len(ctx[row['event_key']])==2 and all(role['signal_state'] not in ('UNKNOWN','SIGNAL_AMBIGUOUS') for role in ctx[row['event_key']].values())]
        days={row['event_date_local'] for row in valid if row['event_date_local'] is not None}
        n=len(valid)
        summaries.append({'scenario_id':scenario,'n_raw':sum(row['n_source_rows'] for row in rows),'n_unique_event_keys':len(rows),
                          'n_valid_nonnegative_pet':n,'n_negative_pet':sum(row['qc_status']=='NEGATIVE_PET_REVIEW' for row in rows),
                          'n_invalid':sum(row['qc_status']=='INVALID' for row in rows),'n_zero_pet':sum(row['pet_s']==0 for row in valid),
                          'n_signal_known':len(known),'n_signal_ambiguous_or_unknown':n-len(known),'n_observation_days':len(days),
                          'unique_object_count':len({row[role+'_object_id'] for row in rows for role in ('encroaching','priority') if row[role+'_object_id'] is not None}),
                          'unique_ordered_pair_count':len({(row['encroaching_object_id'],row['priority_object_id']) for row in rows}),
                          'pet_raw_min_s':min(all_values) if all_values else None,'pet_raw_max_s':max(all_values) if all_values else None,
                          **{'pet_nonnegative_q'+str(int(q*100)).zfill(2)+'_s':quantile(values,q) for q in (.05,.25,.5,.75,.95)},
                          'signal_count_population':'VALID_NONNEGATIVE_PET_INTERACTIONS','population_definition':'Recorded valid nonnegative provider PET interactions'})
        for tau in thresholds:
            numerator=sum(value<=tau for value in values);known_numerator=sum(row['pet_s']<=tau for row in known)
            threshold_rows.append({'scenario_id':scenario,'tau_s':tau,'n_short_pet':numerator,'n_valid_nonnegative_pet':n,
                                   'short_pet_fraction':numerator/n if n else None,'n_signal_known':len(known),
                                   'n_short_pet_signal_known':known_numerator,'short_pet_fraction_signal_known':known_numerator/len(known) if known else None,
                                   'conditional_denominator_definition':'Recorded valid nonnegative PET interactions; signal subset requires both role states known',
                                   'tau_basis':'OPERATIONAL_COMPARISON_NOT_LEGAL_OR_ACCIDENT_THRESHOLD'})
        cumulative=0
        for value,count in sorted(Counter(values).items()):
            cumulative+=count;ecdf.append({'scenario_id':scenario,'pet_s':value,'count_at_value':count,'cumulative_count':cumulative,'n_valid_nonnegative_pet':n,'ecdf':cumulative/n})
        candidates.append({'scenario_id':scenario,'n_valid_nonnegative_pet':n,'n_observation_days':len(days),
                           'candidate_status':'ELIGIBLE_FOR_EXPLORATORY_MULTI_DAY_COMPARISON' if n>=30 and len(days)>=7 else 'DESCRIPTIVE_ONLY',
                           'rank':None,'rank_reason':'RANK_STABILITY_NOT_RUN_STAGE1','population_ci_95':None,
                           'ci_reason':'INSUFFICIENT_DAYS_OR_NOT_RUN_STAGE1','claim_scope':'Observed scenario-condition inspection candidate, not city safety priority'})
        for row in valid:
            if row['local_4h_bin'] is not None:bins[(scenario,row['local_4h_bin'])].append(row['pet_s'])
    timebins=[{'scenario_id':scenario,'local_4h_start':start,'local_4h_end':start+4,'timezone':'Europe/Vienna',
               'n_valid_nonnegative_pet':len(values),'pet_median_s':quantile(values,.5)} for (scenario,start),values in sorted(bins.items())]
    return {'scenario_summary':summaries,'pet_thresholds':threshold_rows,'pet_ecdf':ecdf,'inspection_candidates':candidates,'timebin_summary':timebins}

def sensitivity_rows(ledger,mappings,signals,offsets=(-.5,-.2,0,.2,.5)):
    from .pet_signals import contexts,aligned_event_times
    rows=[]
    for delta in offsets:
        gap_changes=[]
        for event in ledger:
            aligned=aligned_event_times(event,delta)
            first,last='ts_leave_encroaching_ms','ts_enter_priority_ms'
            if event.get(first) is not None and event.get(last) is not None:
                gap_changes.append(((aligned[last]-aligned[first])-(event[last]-event[first]))/1000)
        context=contexts(ledger,mappings,signals,delta);grouped=defaultdict(dict)
        for row in context:grouped[row['scenario_id'] or 'UNKNOWN_SCENARIO'][row['event_key']]=grouped[row['scenario_id'] or 'UNKNOWN_SCENARIO'].get(row['event_key'],[])+[row]
        for scenario,events in sorted(grouped.items()):
            valid=[states for states in events.values() if states[0]['pet_eligible'] and states[0]['pet_s']>=0]
            known=sum(all(state['signal_state'] not in ('UNKNOWN','SIGNAL_AMBIGUOUS') for state in states) for states in valid)
            rows.append({'scenario_id':scenario,'clock_offset_s':delta,'n_valid_nonnegative_pet':len(valid),
                         'n_signal_known':known,'n_signal_ambiguous_or_unknown':len(valid)-known,
                         'pet_values_unchanged':all(abs(change)<1e-9 for change in gap_changes),'max_abs_relative_timestamp_gap_change_s':max((abs(change) for change in gap_changes),default=None),
                         'signal_clock_shifted':False,'basis':'ASSUMED_RELATIVE_EVENT_CLOCK_OFFSET_NOT_MEASURED_ERROR_DISTRIBUTION',
                         'role_state_pair_counts':dict(sorted(Counter('|'.join(state['signal_state'] for state in states) for states in valid).items()))})
    return rows

def bootstrap_descriptions(ledger,tables,profile):
    """Date-cluster exploration, conditional on recorded event dates only."""
    import numpy as np
    settings=profile.get('bootstrap',{});minimum_days=int(settings.get('min_days',7));minimum_events=int(settings.get('min_events',30))
    repetitions=int(settings.get('repetitions',200));seed=int(settings.get('seed',20261012))
    if repetitions!=200 or seed!=20261012:raise ValueError('R1 bootstrap fixed at B=200 seed=20261012')
    valid=[row for row in ledger if row['valid_nonnegative_pet'] and row.get('event_date_local')]
    dates=sorted({row['event_date_local'] for row in valid});scenarios=sorted({row['scenario_id'] for row in valid})
    thresholds=profile.get('thresholds_s',[.5,1,1.5,2]);rows=[]
    eligible={row['scenario_id'] for row in tables['scenario_summary'] if row['n_valid_nonnegative_pet']>=minimum_events and row['n_observation_days']>=minimum_days}
    if profile.get('scope')=='SAMPLE_DAY':eligible=set()
    for candidate in tables['inspection_candidates']:
        candidate.update(rank=None,rank_min_across_thresholds=None,rank_max_across_thresholds=None,population_ci_95=None,
                         ci_reason='INSUFFICIENT_OBSERVATION_DAYS_OR_EVENTS',rank_reason='INSUFFICIENT_MULTI_DAY_COMPARABLE_SUPPORT',
                         independent_observation_scope='ONE_SOURCE_SAMPLE_DAY' if profile.get('scope')=='SAMPLE_DAY' else 'RECORDED_EVENT_DATES')
        if candidate['scenario_id'] not in eligible:candidate['candidate_status']='DESCRIPTIVE_ONLY'
    if not eligible:return rows,{'status':'NOT_APPLICABLE','reason':'INSUFFICIENT_OBSERVATION_DAYS_OR_EVENTS','scope':'Conditional exploratory date clusters, not population/generalization CI','repetitions':0}
    generator=np.random.default_rng(seed);resamples=generator.integers(0,len(dates),size=(repetitions,len(dates)))
    for scenario in sorted(eligible):
        selected=[row for row in valid if row['scenario_id']==scenario]
        counts=np.asarray([sum(row['event_date_local']==date for row in selected) for date in dates])
        for tau in thresholds:
            short=np.asarray([sum(row['event_date_local']==date and row['pet_s']<=tau for row in selected) for date in dates])
            denominators=counts[resamples].sum(axis=1);numerators=short[resamples].sum(axis=1)
            fractions=numerators[denominators>0]/denominators[denominators>0]
            rows.append({'scenario_id':scenario,'tau_s':tau,'n_valid_nonnegative_pet':len(selected),'n_date_clusters':len(dates),
                         'n_valid_bootstrap_replicates':len(fractions),'n_zero_denominator_replicates':int(sum(denominators==0)),
                         'short_pet_fraction':float(short.sum()/counts.sum()),'conditional_date_cluster_interval_lower':float(np.quantile(fractions,.025)) if len(fractions) else None,
                         'conditional_date_cluster_interval_upper':float(np.quantile(fractions,.975)) if len(fractions) else None,
                         'interval_scope':'EXPLORATORY_CONDITIONAL_ON_THIS_INTERSECTION_RECORDED_DATES_NOT_GENERALIZATION_CI','seed':seed,'repetitions':repetitions})
    if len(eligible)>=2:
        rank_values=defaultdict(list)
        for tau in thresholds:
            fractions={row['scenario_id']:row['short_pet_fraction'] for row in rows if row['tau_s']==tau}
            for scenario,value in fractions.items():rank_values[scenario].append(1+sum(other>value for other in fractions.values()))
        for candidate in tables['inspection_candidates']:
            if candidate['scenario_id'] in eligible:
                ranks=rank_values[candidate['scenario_id']]
                candidate.update(rank=None,rank_min_across_thresholds=min(ranks),rank_max_across_thresholds=max(ranks),
                                 rank_reason='THRESHOLD_SENSITIVITY_RANGE_WITH_TIES; NO_SINGLE_CITY_SAFETY_RANK',ci_reason='SEE_CONDITIONAL_DATE_CLUSTER_INTERVAL_TABLE')
    return rows,{'status':'COMPUTED_EXPLORATORY_DATE_CLUSTER_INTERVALS','repetitions':repetitions,'seed':seed,
                 'eligible_scenarios':sorted(eligible),'scope':'This intersection observed date clusters only; no broader population CI'}

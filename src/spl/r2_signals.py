"""No-fit signal evidence from a preserved ledger and read-only provider cache.

The two states are measured at each role's own conflict-area entry time. They
are not simultaneous signals, stop-line observations, or violation labels.
Coverage uses half-open intervals and complete local days, including zero-event
days. No missing state is carried forward.
"""
from collections import Counter, OrderedDict, defaultdict
import csv
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from .pet_contracts import TIMES, finite, integer
from .pet_signals import SignalIndex, STATES

OFFSETS=(-2.,-1.,-.5,-.2,0.,.2,.5,1.,2.)
ROLES=('encroaching','priority')
CONDITION_STATES=('green','yellow','red','red-yellow','UNKNOWN','SIGNAL_AMBIGUOUS')
POPULATIONS=('ALL_LEDGER_EVENTS','VALID_NONNEGATIVE_PET','SHORT_NONNEGATIVE_PET_LE_1S')
SCOPE='OWN_ROLE_CONFLICT_AREA_ENTRY_NOT_SIMULTANEOUS_OR_STOP_LINE'
EPOCH=datetime(1970,1,1,tzinfo=timezone.utc)


def offset_milliseconds(value):
    if isinstance(value,bool):raise ValueError('Finite millisecond clock offset required')
    try:number=Decimal(str(value))*1000
    except (InvalidOperation,ValueError):raise ValueError('Finite millisecond clock offset required') from None
    if not number.is_finite() or number!=number.to_integral_value() or abs(number)>=2**63:
        raise ValueError('Offset must be a finite integral number of milliseconds; no rounding')
    return int(number)


def shift_event_times(event,delta_s):
    """Return four shifted event clocks. Never move the signal clock or PET."""
    delta=offset_milliseconds(delta_s)
    return {field:event.get(field)+delta if event.get(field) is not None else None for field in TIMES}


def local_day(timestamp_ms,zone):
    if timestamp_ms is None:return None
    try:return (EPOCH+timedelta(milliseconds=timestamp_ms)).astimezone(zone).date().isoformat()
    except (OverflowError,ValueError):return None


def day_bounds(day,zone):
    day=date.fromisoformat(day) if isinstance(day,str) else day
    start=datetime.combine(day,time.min,zone).astimezone(timezone.utc)
    end=datetime.combine(day+timedelta(days=1),time.min,zone).astimezone(timezone.utc)
    return (start-EPOCH)//timedelta(milliseconds=1),(end-EPOCH)//timedelta(milliseconds=1)


def _dates(first,last):
    current=date.fromisoformat(first);end=date.fromisoformat(last)
    while current<=end:
        yield current.isoformat();current+=timedelta(days=1)


def _mapping(mappings,scenario,role):
    entry=mappings.get(scenario)
    if not entry:return None,'MISSING_SCENARIO_SIGNAL_MAPPING'
    if entry.get('conflict'):return None,'CONFLICTING_SCENARIO_SIGNAL_MAPPING'
    raw=entry.get('raw',entry);group=raw.get(role+'_signal_group_id')
    return (str(group),None) if group else (None,'MISSING_ROLE_SIGNAL_GROUP')


def _events(path):
    with Path(path).open(encoding='utf-8',newline='') as stream:
        reader=csv.DictReader(stream)
        required={'event_key','scenario_id','pet_s','qc_status',*TIMES}
        if not required<=set(reader.fieldnames or []):raise ValueError('Incomplete preserved ledger schema')
        for raw in reader:
            if not raw['event_key']:raise ValueError('Ledger key must not be empty')
            row={field:integer(raw.get(field)) for field in TIMES}
            row.update(event_key=raw['event_key'],scenario_id=raw['scenario_id'] or 'UNKNOWN_SCENARIO',
                       pet_s=finite(raw['pet_s']),qc_status=raw['qc_status'])
            eligible=row['qc_status']=='VALID_NONNEGATIVE_PET'
            if eligible and (row['pet_s'] is None or row['pet_s']<0):raise ValueError('Preserved QC/PET eligibility contradiction')
            if 'valid_nonnegative_pet' in raw and raw['valid_nonnegative_pet'] not in ('True' if eligible else 'False',''):
                raise ValueError('Preserved eligibility flag contradicts QC status')
            row['eligible']=eligible
            yield row


class ReadOnlySignals:
    """Bounded day indexes include long intervals starting before the day.

    Existing cache contains only representable signal times. The caller must
    provide the frozen source QC so dropped unlocatable times cannot silently
    become known coverage. Such unresolved input conservatively blocks all
    groups because the cache alone cannot recover the affected group identities.
    """
    def __init__(self,path,groups,zone,source_signal_qc):
        if source_signal_qc is None or 'n_invalid' not in source_signal_qc:
            raise ValueError('Frozen source signal QC is required for cache reuse')
        invalid=source_signal_qc['n_invalid'];invalid_state=source_signal_qc.get('n_invalid_state_rows_retained_as_unknown',0)
        if any(isinstance(n,bool) or not isinstance(n,int) or n<0 for n in (invalid,invalid_state)) or invalid_state>invalid:
            raise ValueError('Invalid source signal QC')
        self.blocked=bool(source_signal_qc.get('n_conservatively_blocked_groups',0) or invalid>invalid_state)
        self.groups=tuple(sorted(set(groups)));self.zone=zone;self.cache=OrderedDict()
        self.current_bounds=None;self.current_index=None
        self.db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
        self.db.execute('PRAGMA query_only=ON')
        self.max_duration=int(self.db.execute('SELECT COALESCE(MAX(e-s),0) FROM signals').fetchone()[0])

    def day_index(self,day):
        if day in self.cache:
            self.cache.move_to_end(day);return self.cache[day]
        lo,hi=day_bounds(day,self.zone);index=SignalIndex();raw_counts=Counter()
        if self.groups:
            placeholders=','.join('?' for _ in self.groups)
            query=f'SELECT g,s,e,state,si,rn FROM signals WHERE s>=? AND s<? AND e>? AND g IN ({placeholders}) ORDER BY rowid'
            low=max(-(2**63),lo-self.max_duration)
            for group,start,end,state,si,rn in self.db.execute(query,(low,hi,lo,*self.groups)):
                raw_counts[group]+=1
                index.add(dict(signal_group_id=group,start_timestamp_ms=start,end_timestamp_ms=end,signal_state=state),si,rn)
        index.finalize()
        if self.blocked:index.unidentified_invalid_row=True
        value=index,raw_counts;self.cache[day]=value
        while len(self.cache)>3:self.cache.popitem(last=False)
        return value

    def lookup(self,group,timestamp):
        if timestamp is None:return {'state':'UNKNOWN','reason':'INVALID_EVENT_TIMESTAMP','source_rows':[]}
        if self.current_bounds and self.current_bounds[0]<=timestamp<self.current_bounds[1]:
            return self.current_index.lookup(group,timestamp)
        day=local_day(timestamp,self.zone)
        if day is None:return {'state':'UNKNOWN','reason':'INVALID_EVENT_TIMESTAMP','source_rows':[]}
        self.current_bounds=day_bounds(day,self.zone);self.current_index=self.day_index(day)[0]
        return self.current_index.lookup(group,timestamp)

    def close(self):self.db.close()


def coverage_for_day(index,group,day,zone,raw_intervals=None):
    """Exclusive known/unknown/gap/overlap seconds partition the actual day."""
    lo,hi=day_bounds(day,zone);actions=[];data=index.groups.get(group);longest=0;clipped=0;count=0
    if data:
        for i,(start,end) in enumerate(zip(data['start'],data['end'])):
            a=max(lo,start);b=min(hi,end)
            if a>=b:continue
            count+=1;longest=max(longest,end-start);clipped=max(clipped,b-a)
            actions.extend(((a,1,i),(b,-1,i)))
    actions.sort();totals=Counter();active=set();cursor=lo;position=0;max_overlap=0;gap_count=0;longest_gap=0;gap_start=None
    while cursor<hi:
        boundary=actions[position][0] if position<len(actions) else hi
        duration=boundary-cursor
        if duration:
            if not active:label='GAP'
            elif len(active)>1:label='SIGNAL_AMBIGUOUS'
            else:label=STATES[data['state'][next(iter(active))]]
            totals[label]+=duration
            if label=='GAP':
                if gap_start is None:gap_count+=1;gap_start=cursor
                longest_gap=max(longest_gap,boundary-gap_start)
            else:gap_start=None
            max_overlap=max(max_overlap,len(active))
        while position<len(actions) and actions[position][0]==boundary:
            _,direction,i=actions[position]
            if direction==1:active.add(i)
            else:active.discard(i)
            position+=1
        cursor=boundary
    blocked=index.unidentified_invalid_row or group in index.blocked_groups
    row={'signal_group_id':group,'local_date':day,'local_timezone':zone.key,
         'day_start_ms':lo,'day_end_ms':hi,'day_seconds':(hi-lo)/1000,
         'n_raw_intervals_intersecting':raw_intervals if raw_intervals is not None else count,
         'n_unique_intervals_intersecting':count,'gap_count':gap_count,'gap_seconds':totals['GAP']/1000,
         'longest_gap_seconds':longest_gap/1000,'overlap_seconds':totals['SIGNAL_AMBIGUOUS']/1000,
         'maximum_overlap_multiplicity':max_overlap,'observed_unknown_seconds':totals['unknown']/1000,
         'covered_union_seconds':(hi-lo-totals['GAP'])/1000,'longest_original_interval_seconds':longest/1000,
         'longest_clipped_interval_seconds':clipped/1000,'conservative_block':bool(blocked),
         'effective_unknown_or_ambiguous_seconds':(hi-lo if blocked else totals['GAP']+totals['SIGNAL_AMBIGUOUS']+totals['unknown'])/1000,
         'ffill_performed':False,'day_independence_status':'NOT_ESTABLISHED'}
    for state in STATES[:-1]:row[state.replace('-','_')+'_covered_seconds']=0. if blocked else totals[state]/1000
    known=sum(row[state.replace('-','_')+'_covered_seconds'] for state in STATES[:-1])
    row['effective_known_seconds']=known
    if abs(known+row['effective_unknown_or_ambiguous_seconds']-row['day_seconds'])>1e-6:
        raise ValueError('Signal day partition does not sum to actual DST-aware day')
    return row


def aggregate_tv(baseline,shifted,n_events):
    """TV compares distributions; it cannot detect a swap of individual events."""
    if not n_events:return None
    if sum(baseline.values())!=n_events or sum(shifted.values())!=n_events:
        raise ValueError('TV populations differ')
    return sum(abs(baseline.get(k,0)-shifted.get(k,0)) for k in set(baseline)|set(shifted))/(2*n_events)


def _crossings(reader,start_day,end_day,claim_status):
    lo=day_bounds(start_day,reader.zone)[0];hi=day_bounds(end_day,reader.zone)[1];rows=[]
    if not reader.groups:return rows
    placeholders=','.join('?' for _ in reader.groups)
    query=f'SELECT g,s,e,state,si,rn FROM signals WHERE s<? AND e>? AND (s<? OR e>? OR (s<=? AND e>=?)) AND g IN ({placeholders}) ORDER BY g,s,e,state,si,rn'
    for group,start,end,state,si,rn in reader.db.execute(query,(hi,lo,lo,hi,lo,hi,*reader.groups)):
        rows.append({'signal_group_id':group,'start_timestamp_ms':start,'end_timestamp_ms':end,
                     'signal_state':state if state in STATES else 'UNKNOWN','source_index':si,'source_row_id':rn,
                     'duration_seconds':(end-start)/1000,'candidate_period_start_ms':lo,'candidate_period_end_ms':hi,
                     'intersection_seconds':(min(hi,end)-max(lo,start))/1000,'crosses_period_start':start<lo<end,
                     'crosses_period_end':start<hi<end,'covers_entire_candidate_period':start<=lo and end>=hi,
                     'claim_status':claim_status,'meaning':'FILE_INTERVAL_EVIDENCE_NOT_PROOF_OF_OBSERVED_SIGNAL_CONTINUITY'})
    return rows


def analyze_signals(ledger_path,signal_cache_path,mappings,*,offsets_s=OFFSETS,thresholds_s=(.5,1.,1.5,2.),
                    local_timezone='Europe/Vienna',min_cell_events=30,source_signal_qc=None,
                    missing_period=('2025-06-05','2025-06-11'),
                    missing_claim_status='USER_REQUEST_UNVERIFIED'):
    """Return stable list-of-dict tables and metadata; no writes or model fits."""
    offsets=[(float(value),offset_milliseconds(value)) for value in offsets_s]
    if not offsets or len({ms for _,ms in offsets})!=len(offsets):raise ValueError('Distinct nonempty clock offsets required')
    thresholds=[finite(value) for value in thresholds_s]
    if not thresholds or any(v is None or v<0 for v in thresholds) or len(set(thresholds))!=len(thresholds):raise ValueError('Distinct nonnegative PET thresholds required')
    if isinstance(min_cell_events,bool) or not isinstance(min_cell_events,int) or min_cell_events<1:raise ValueError('Positive minimum descriptive cell count required')
    zone=ZoneInfo(local_timezone);scenarios=set(mappings);observed_scenarios=set();seen=set();first=last=None;n_rows=0;n_invalid_time=0
    period_days=set(_dates(*missing_period))
    if not period_days:raise ValueError('Candidate missing-period end precedes start')
    for row in _events(ledger_path):
        if row['event_key'] in seen:raise ValueError('Duplicate event key in preserved reconciled ledger')
        seen.add(row['event_key']);n_rows+=1;scenarios.add(row['scenario_id']);observed_scenarios.add(row['scenario_id'])
        times=[row[f] for f in TIMES if row[f] is not None]
        day=local_day(min(times),zone) if times else None
        if day is None:n_invalid_time+=1
        for value in times:
            own_day=local_day(value,zone)
            if own_day:first=min(first or own_day,own_day);last=max(last or own_day,own_day)
    if not n_rows:raise ValueError('No preserved ledger events')
    del seen
    groups={g for scenario in observed_scenarios for role in ROLES if (g:=_mapping(mappings,scenario,role)[0]) is not None}
    reader=ReadOnlySignals(signal_cache_path,groups,zone,source_signal_qc)
    transitions=Counter();changes=defaultdict(Counter);baseline=defaultdict(Counter);shifted=defaultdict(Counter)
    cells=defaultdict(Counter);cell_dates=defaultdict(set);event_cover=defaultdict(Counter);n_four_time_checks=0
    try:
        for row in _events(ledger_path):
            scenario=row['scenario_id'];populations=[POPULATIONS[0]]
            if row['eligible']:
                populations.append(POPULATIONS[1])
                if row['pet_s']<=1:populations.append(POPULATIONS[2])
            times={field:row[field] for field in TIMES};anchor=min((v for v in times.values() if v is not None),default=None)
            anchor_day=local_day(anchor,zone)
            candidate_event=any(local_day(row['ts_enter_'+role+'_ms'],zone) in period_days for role in ROLES)
            def pair(delta_ms):
                states=[]
                for role in ROLES:
                    group,reason=_mapping(mappings,scenario,role);original=row['ts_enter_'+role+'_ms']
                    timestamp=original+delta_ms if original is not None else None
                    context=reader.lookup(group,timestamp) if group and not reason else {'state':'UNKNOWN','reason':reason}
                    states.append(context['state'])
                    if delta_ms==0:
                        day=local_day(timestamp,zone) or 'UNKNOWN_DATE';label=group or 'UNMAPPED:'+scenario+':'+role
                        counts=event_cover[(label,day,role)]
                        counts['n_events']+=1;counts[context['state']]+=1
                        if row['eligible']:counts['n_valid_nonnegative_pet']+=1
                        if row['eligible'] and row['pet_s']<=1:counts['n_short_pet_le_1s']+=1
                return tuple(states)
            base_pair=pair(0)
            for population in populations:
                for sid in (scenario,'ALL_SCENARIOS'):baseline[(population,sid)][base_pair]+=1
            if row['eligible']:
                key=(scenario,*base_pair);cells[key]['n']+=1
                if anchor_day:cell_dates[key].add(anchor_day)
                else:cells[key]['n_missing_local_date']+=1
                if candidate_event:cells[key]['n_candidate_period']+=1
                for tau in thresholds:
                    if row['pet_s']<=tau:
                        cells[key][tau]+=1
                        if candidate_event:cells[key][('candidate',tau)]+=1
            for offset,delta_ms in offsets:
                target=base_pair if delta_ms==0 else pair(delta_ms)
                if all(v is not None for v in times.values()):
                    shifted_times={field:value+delta_ms for field,value in times.items()}
                    if any(shifted_times[TIMES[j]]-shifted_times[TIMES[i]]!=times[TIMES[j]]-times[TIMES[i]] for i,j in ((0,1),(2,3),(1,2))):
                        raise ValueError('Event-only shift changed a relative timestamp gap')
                    n_four_time_checks+=1
                for population in populations:
                    for sid in (scenario,'ALL_SCENARIOS'):
                        key=(population,sid,offset);counts=changes[key];counts['n_events']+=1
                        for i,role in enumerate(ROLES):
                            counts['n_'+role+'_state_changed']+=base_pair[i]!=target[i]
                            transitions[(*key,role,base_pair[i],target[i])]+=1
                        counts['n_pair_state_changed']+=base_pair!=target
                        transitions[(*key,'PAIR',json.dumps(base_pair,separators=(',',':')),json.dumps(target,separators=(',',':')))]+=1
                        shifted[key][target]+=1
        coverage=[]
        if first:
            for day in _dates(first,last):
                index,raw=reader.day_index(day)
                for group in sorted(groups):
                    value=coverage_for_day(index,group,day,zone,raw[group])
                    value['n_role_entry_queries']=sum(event_cover[(group,day,role)]['n_events'] for role in ROLES)
                    value['candidate_missing_period_day']=day in period_days
                    value['missing_period_claim_status']=missing_claim_status
                    value['withheld_signal_conclusion']=value['effective_unknown_or_ambiguous_seconds']>0 or day in period_days
                    value['source_quality_status']='SOURCE_QUALITY_UNRESOLVED_CANDIDATE_PERIOD' if day in period_days else 'UNRESOLVED_COVERAGE' if value['effective_unknown_or_ambiguous_seconds']>0 else 'FILE_RECORDED_CONTEXT_ONLY'
                    coverage.append(value)
        crossing=_crossings(reader,*missing_period,missing_claim_status)
    finally:reader.close()
    condition_rows=[]
    for scenario in sorted(scenarios):
        for encroaching in CONDITION_STATES:
            for priority in CONDITION_STATES:
                key=(scenario,encroaching,priority);counts=cells[key];n=counts['n'];unresolved=any(x in ('UNKNOWN','SIGNAL_AMBIGUOUS') for x in key[1:])
                for tau in sorted(thresholds):
                    numerator=counts[tau]
                    condition_rows.append({'scenario_id':scenario,'encroaching_entry_signal':encroaching,'priority_entry_signal':priority,
                        'tau_s':tau,'clock_offset_s':0.,'n_short_pet':numerator,'n_valid_nonnegative_pet':n,
                        'short_pet_fraction':numerator/n if n else None,'n_observed_local_dates':len(cell_dates[key]),
                        'n_missing_local_date':counts['n_missing_local_date'],'n_unresolved_condition_events':n if unresolved else 0,
                        'candidate_period_n_valid_nonnegative_pet':counts['n_candidate_period'],
                        'candidate_period_n_short_pet':counts[('candidate',tau)],
                        'candidate_period_short_pet_fraction':counts[('candidate',tau)]/counts['n_candidate_period'] if counts['n_candidate_period'] else None,
                        'candidate_period_basis':'ANY_OWN_ROLE_ENTRY_LOCAL_DATE',
                        'observed_date_basis':'EARLIEST_ORIGINAL_EVENT_TIMESTAMP',
                        'missing_period_claim_status':missing_claim_status,'withheld_signal_conclusion':unresolved or counts['n_candidate_period']>0,
                        'signal_condition_resolved':not unresolved,
                        'signal_conclusion_status':'DEFERRED_UNKNOWN_OR_AMBIGUOUS' if unresolved else 'MIXED_WITH_SOURCE_QUALITY_UNRESOLVED_PERIOD' if counts['n_candidate_period'] else 'DESCRIPTIVE_ONLY',
                        'sample_status':'NO_OBSERVATIONS' if n==0 else 'INSUFFICIENT_DESCRIPTIVE_COUNT' if n<min_cell_events else 'OBSERVED_DESCRIPTIVE_COUNT',
                        'independent_observation_days':None,'signal_context_scope':SCOPE})
    change_rows=[];tv_rows=[]
    for population in POPULATIONS:
        for scenario in sorted(scenarios|{'ALL_SCENARIOS'}):
            for offset,_ in offsets:changes[(population,scenario,offset)]
    for (population,scenario,offset),counts in sorted(changes.items()):
        n=counts['n_events'];key=population,scenario,offset
        change_rows.append({'population':population,'scenario_id':scenario,'clock_offset_s':offset,'n_events':n,
            **{name:counts[name] for name in ('n_encroaching_state_changed','n_priority_state_changed','n_pair_state_changed')},
            'encroaching_state_change_rate':counts['n_encroaching_state_changed']/n if n else None,
            'priority_state_change_rate':counts['n_priority_state_changed']/n if n else None,
            'pair_state_change_rate':counts['n_pair_state_changed']/n if n else None,'signal_context_scope':SCOPE})
        tv_rows.append({'population':population,'scenario_id':scenario,'clock_offset_s':offset,'n_events':n,
            'aggregate_role_pair_tv':aggregate_tv(baseline[(population,scenario)],shifted[key],n),
            'same_event_pair_state_change_rate':counts['n_pair_state_changed']/n if n else None,
            'baseline_pair_counts':[{'encroaching':a,'priority':b,'n':count} for (a,b),count in sorted(baseline[(population,scenario)].items())],
            'shifted_pair_counts':[{'encroaching':a,'priority':b,'n':count} for (a,b),count in sorted(shifted[key].items())],
            'meaning':'AGGREGATE_DISTRIBUTION_CHANGE_NOT_INDIVIDUAL_EVENT_INVARIANCE','signal_context_scope':SCOPE})
    transition_rows=[{'population':population,'scenario_id':scenario,'clock_offset_s':offset,'role':role,'from_state':a,'to_state':b,'n_events':n,'signal_context_scope':SCOPE}
                     for (population,scenario,offset,role,a,b),n in sorted(transitions.items())]
    cover_rows=[]
    for (group,day,role),counts in sorted(event_cover.items()):
        n=counts['n_events']
        if not n:continue
        known=sum(counts[state] for state in STATES[:-1])
        cover_rows.append({'signal_group_id':group,'local_date':day,'role':role,'n_events':n,'n_known_signal':known,
            'n_unknown_signal':counts['UNKNOWN'],'n_ambiguous_signal':counts['SIGNAL_AMBIGUOUS'],
            'known_signal_fraction':known/n if n else None,'n_valid_nonnegative_pet':counts['n_valid_nonnegative_pet'],
            'n_short_pet_le_1s':counts['n_short_pet_le_1s'],'signal_context_scope':SCOPE})
    return {'tables':{'signal_coverage_daily':coverage,'signal_missing_period_crossings':crossing,
                     'signal_event_coverage_daily':cover_rows,'signal_state_transitions':transition_rows,
                     'signal_event_changes':change_rows,'signal_pair_tv':tv_rows,'signal_condition_pet':condition_rows},
            'summary':{'n_ledger_events':n_rows,'n_invalid_or_unrepresentable_anchor_time':n_invalid_time,
                       'local_date_start':first,'local_date_end':last,'local_timezone':local_timezone,
                       'used_signal_groups':sorted(groups),'n_calendar_days':sum(1 for _ in _dates(first,last)) if first else 0,
                       'coverage_status':'UNRESOLVED_NO_REPRESENTABLE_LOCAL_DAY' if not first else 'UNRESOLVED_NO_USED_SIGNAL_GROUPS' if not groups else 'CALCULATED_COMPLETE_CALENDAR_RANGE',
                       'source_signal_qc':source_signal_qc,'conservative_all_group_block':reader.blocked,
                       'missing_period':list(missing_period),'missing_claim_status':missing_claim_status,
                       'candidate_period_signal_conclusions_withheld':True,
                       'clock_offsets_s':[value for value,_ in offsets],'four_timestamp_shift_checks':n_four_time_checks,
                       'pet_and_event_keys_unchanged':True,'signal_clock_shifted':False,'ffill_performed':False,
                       'populations':list(POPULATIONS),'additional_fit_calls':0,'baseline_recomputed':False,
                       'limitations':['Existing cache and ledger are reused; caller must bind their actual hashes.',
                                      'States concern each role\'s own entry, not simultaneous signals or stop-line violations.',
                                      'Stress offsets are not an empirical error distribution or guaranteed tolerance.',
                                      'Recorded interactions and observed days do not establish traffic exposure or day independence.']}}

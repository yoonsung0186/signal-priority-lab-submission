"""Scenario-role signal contexts at conflict-area entry, never stop-line truth."""
from array import array
from bisect import bisect_right
from collections import Counter
from .pet_contracts import timestamp_integer,TIMES

STATES=('green','yellow','red','red-yellow','unknown')

class SignalIndex:
    """Compact arrays avoid retaining millions of raw signal dictionaries."""
    def __init__(self):
        self.groups={};self.qc=Counter();self.invalid_examples=[];self.finalized=False;self.blocked_groups=set();self.unidentified_invalid_row=False

    def add(self,row,source_index=0,row_number=0):
        self.qc['n_raw']+=1
        group=row.get('signal_group_id');start=timestamp_integer(row.get('start_timestamp_ms'));end=timestamp_integer(row.get('end_timestamp_ms'))
        state=row.get('signal_state')
        if not group or start is None or end is None or start>=end:
            self.qc['n_invalid']+=1
            if group:self.blocked_groups.add(group)
            else:self.unidentified_invalid_row=True
            if len(self.invalid_examples)<100:self.invalid_examples.append({'source_index':source_index,'source_row_id':row_number,'reason':'INVALID_SIGNAL_TIME_OR_GROUP_CONSERVATIVE_BLOCK'})
            return
        quality=0
        if state not in STATES:
            quality=1;state='unknown';self.qc['n_invalid']+=1;self.qc['n_invalid_state_rows_retained_as_unknown']+=1
            if len(self.invalid_examples)<100:self.invalid_examples.append({'source_index':source_index,'source_row_id':row_number,'reason':'INVALID_STATE_RETAINED_UNKNOWN_INTERVAL'})
        data=self.groups.setdefault(group,{'start':array('q'),'end':array('q'),'state':array('B'),'quality':array('B'),'source':array('H'),'row':array('I')})
        for field,value in [('start',start),('end',end),('state',STATES.index(state)),('quality',quality),('source',source_index),('row',row_number)]:data[field].append(value)

    def finalize(self):
        if self.finalized:return
        for group,data in self.groups.items():
            order=sorted(range(len(data['start'])),key=lambda i:(data['start'][i],data['end'][i],data['state'][i],data['quality'][i]))
            result={field:array(values.typecode) for field,values in data.items()};previous=None
            for i in order:
                key=data['start'][i],data['end'][i],data['state'][i],data['quality'][i]
                if key==previous:self.qc['n_identical_duplicates_merged']+=1;continue
                for field in result:result[field].append(data[field][i])
                previous=key
            maximum=0;prefix=array('q')
            for start,end in zip(result['start'],result['end']):
                if start<maximum:self.qc['n_overlapping_intervals']+=1
                maximum=max(maximum,end);prefix.append(maximum)
            result['max_end']=prefix;self.groups[group]=result
            self.qc['n_valid_unique_intervals']+=sum(quality==0 for quality in result['quality'])
            self.qc['n_retained_unknown_state_intervals']+=sum(quality==1 for quality in result['quality'])
        self.qc['n_conservatively_blocked_groups']=len(self.blocked_groups)
        self.finalized=True

    def lookup(self,group,timestamp_ms):
        self.finalize()
        if timestamp_ms is None:return {'state':'UNKNOWN','reason':'INVALID_EVENT_TIMESTAMP','source_rows':[]}
        if self.unidentified_invalid_row or group in self.blocked_groups:return {'state':'UNKNOWN','reason':'SIGNAL_TIME_OR_GROUP_UNRESOLVED_CONSERVATIVE_BLOCK','source_rows':[]}
        data=self.groups.get(group)
        if data is None:return {'state':'UNKNOWN','reason':'NO_SIGNAL_GROUP_COVERAGE','source_rows':[]}
        index=bisect_right(data['start'],timestamp_ms)-1;matches=[]
        while index>=0 and data['max_end'][index]>timestamp_ms:
            if data['start'][index]<=timestamp_ms<data['end'][index]:matches.append(index)
            index-=1
        sources=[{'source_index':data['source'][i],'source_row_id':data['row'][i]} for i in matches]
        if len(matches)>1:return {'state':'SIGNAL_AMBIGUOUS','reason':'OVERLAPPING_SIGNAL_INTERVALS','source_rows':sources}
        if not matches:return {'state':'UNKNOWN','reason':'SIGNAL_GAP','source_rows':[]}
        state=STATES[data['state'][matches[0]]]
        return {'state':state if state!='unknown' else 'UNKNOWN','reason':None if state!='unknown' else 'INVALID_SIGNAL_STATE' if data['quality'][matches[0]] else 'OBSERVED_UNKNOWN','source_rows':sources}

    @classmethod
    def from_rows(cls,rows):
        result=cls()
        for number,row in enumerate(rows,1):result.add(row,0,number)
        result.finalize();return result

def aligned_event_times(event,delta_s):
    return {field:event[field]+delta_s*1000 if event.get(field) is not None else None for field in TIMES}

def contexts(ledger,mappings,signals,delta_s=0):
    rows=[]
    for event in ledger:
        mapping=mappings.get(event.get('scenario_id'))
        aligned=aligned_event_times(event,delta_s)
        for role in ('encroaching','priority'):
            group=None;reason=None
            if mapping is None:reason='MISSING_SCENARIO_SIGNAL_MAPPING'
            elif mapping['conflict']:reason='CONFLICTING_SCENARIO_SIGNAL_MAPPING'
            else:group=mapping['raw'].get(role+'_signal_group_id') or None
            if reason or group is None:context={'state':'UNKNOWN','reason':reason or 'MISSING_ROLE_SIGNAL_GROUP','source_rows':[]}
            else:context=signals.lookup(group,aligned['ts_enter_'+role+'_ms'])
            rows.append({'event_key':event['event_key'],'event_id':event.get('event_id'),'scenario_id':event.get('scenario_id'),
                         'role':role,'clock_offset_s':delta_s,'signal_group_id':group,'signal_state':context['state'],
                         'event_enter_aligned_ms':aligned['ts_enter_'+role+'_ms'],
                         'signal_reason':context['reason'],'signal_source_rows':context['source_rows'],
                         'pet_s':event.get('pet_s'),'pet_eligible':event['pet_eligible'],
                         'signal_context_scope':'CONFLICT_AREA_ENTRY_NOT_STOP_LINE_OR_VIOLATION',
                         'representative_pedestrian_signal_possible':event.get(role+'_class')=='pedestrian'})
    return rows

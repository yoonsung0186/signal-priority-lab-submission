"""Provider PET contracts. Interactions are distinct from straight-entry events."""
import hashlib,json,math,re
from .contracts import ContractError

TABLE_MEMBERS={'pet_events':'pet_events/pet_events.csv.gz','objects':'objects/objects.csv.gz',
               'scenarios':'scenarios/scenarios.csv','scenario_signal_mapping':'signals/scenario_signal_mapping.csv.gz',
               'signal_states':'signals/signal_states.csv.gz'}
TIMES=('ts_enter_encroaching_ms','ts_leave_encroaching_ms','ts_enter_priority_ms','ts_leave_priority_ms')
REQUIRED={'pet_events':('event_id','scenario_id','encroaching_object_id','priority_object_id',*TIMES,'pet_s','conflict_lon_deg','conflict_lat_deg'),
          'objects':('object_id','object_day','object_class_static','length_m_static','width_m_static','movement_category','conflict_leg','turning_movement'),
          'scenarios':('scenario_id',*(role+'_'+field for role in ('encroaching','priority') for field in ('movement_category','conflict_leg','turning_movement'))),
          'scenario_signal_mapping':('scenario_id','encroaching_signal_group_id','priority_signal_group_id'),
          'signal_states':('signal_group_id','start_timestamp_ms','end_timestamp_ms','signal_state')}

def stable_json(value):return (json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,indent=2)+'\n').encode('utf-8')
def digest(raw):return hashlib.sha256(raw).hexdigest()

def integer(value):
    if isinstance(value,bool):return None
    if isinstance(value,int):return value
    if isinstance(value,str) and re.fullmatch(r'-?\d+',value.strip()):return int(value.strip())
    return None

def timestamp_integer(value):
    """Exact integral numeric timestamp text, including provider exponent form.

    This does not round a float or infer lost timestamp precision. Identity keys
    continue to use the stricter integer parser. Nonintegral/huge values fail.
    """
    parsed=integer(value)
    if parsed is not None:return parsed if -(2**63)<=parsed<2**63 else None
    if not isinstance(value,str) or len(value)>40 or not re.fullmatch(r'-?\d+(?:\.\d+)?[eE][+-]?\d{1,3}',value.strip()):return None
    from decimal import Decimal,InvalidOperation
    try:
        number=Decimal(value.strip())
        if not number.is_finite() or number!=number.to_integral_value() or not -(2**63)<=number<2**63:return None
        return int(number)
    except (InvalidOperation,ValueError,OverflowError):return None

def finite(value):
    if value is None or value=='' or isinstance(value,bool):return None
    try:number=float(value)
    except (ValueError,TypeError):return None
    return number if math.isfinite(number) else None

def key_value(value,numeric=False):
    if numeric:
        parsed=integer(value)
        return str(parsed) if parsed is not None and parsed>=0 else None
    return str(value).strip() if value is not None and str(value).strip() else None

def reconcile(rows,key_field,numeric=False):
    """One key, merged identical origins; conflicting variants remain excluded."""
    index={};missing=[];identical=0;conflicting=0
    for row in rows:
        raw={key:value for key,value in row.items() if not key.startswith('__')}
        origin=row.get('__source',{'fixture_kind':'SYNTHETIC_UNIT_FIXTURE'})
        key=key_value(raw.get(key_field),numeric)
        if key is None:
            missing.append({'raw':raw,'sources':[origin],'conflict':False,'variants':[raw]});continue
        if key not in index:
            index[key]={'raw':raw,'sources':[origin],'conflict':False,'variants':[raw],'variant_sources':[[origin]]}
        else:
            record=index[key];record['sources'].append(origin)
            if raw in record['variants']:
                identical+=1;record['variant_sources'][record['variants'].index(raw)].append(origin)
            else:
                record['conflict']=True;record['variants'].append(raw);record['variant_sources'].append([origin]);conflicting+=1
    return index,missing,{'n_raw':len(rows),'n_unique_keys':len(index),'n_missing_key_rows':len(missing),
                          'n_identical_duplicate_rows_merged':identical,'n_conflicting_duplicate_rows_extra':conflicting}

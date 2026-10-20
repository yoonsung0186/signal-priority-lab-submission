"""Small independent SAMPLE_DAY reference calculation; no tracks or AI reads."""
import argparse,csv,gzip,io,json,math,zipfile,collections
from snapshot import ROOT,digest,snapshot,environment_manifest_ref
from preflight import verify_root,safe_child
from final_verify import encode

EXPECTED='60ffaaf43c4bc1287f50111aa70c1eef311616ad72e8667ca7c09a04c5750f82'
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    verify_root(ROOT);out=safe_child(ROOT,args.output)
    if out.parent!=ROOT/'runs' or out.exists():raise ValueError('New runs/<id> required')
    archive=safe_child(ROOT,'data/raw/sitpet_v1_1/examples.zip')
    rights=json.loads((ROOT/'state/data_acquisition_manifest.json').read_bytes())
    if rights['rights']['analysis_status']!='ALLOW' or rights['sha256']!=EXPECTED or digest(archive.read_bytes())!=EXPECTED:raise ValueError('Input rights/hash mismatch')
    source=snapshot()
    with zipfile.ZipFile(archive) as z:
        def read(name):
            with z.open(name) as stream:
                raw=gzip.GzipFile(fileobj=stream) if name.endswith('.gz') else stream
                with io.TextIOWrapper(raw,encoding='utf-8-sig',newline='') as text:
                    rows=list(csv.DictReader(text))
            return rows
        events=read('pet_events/pet_events.csv.gz');objects=read('objects/objects.csv.gz')
        scenarios=read('scenarios/scenarios.csv');mapping=read('signals/scenario_signal_mapping.csv.gz');states=read('signals/signal_states.csv.gz')
    def unique(rows,key):
        result={r[key]:r for r in rows}
        if len(result)!=len(rows):raise ValueError('Duplicate reference key; use full QC pipeline')
        return result
    unique(events,'event_id');object_rows=len(objects)
    object_multiplicity=collections.Counter(r['object_id'] for r in objects)
    objects=set(object_multiplicity)  # FK existence only; no ambiguous attributes selected.
    scenarios=unique(scenarios,'scenario_id');mapping=unique(mapping,'scenario_id')
    signals=collections.defaultdict(list)
    for row in states:
        lo=int(row['start_timestamp_ms']);hi=int(row['end_timestamp_ms'])
        if hi<=lo:raise ValueError('Invalid signal interval; use full QC pipeline')
        signals[row['signal_group_id']].append((lo,hi,row['signal_state']))
    def signal(group,timestamp):
        matches=[state for lo,hi,state in signals[group] if lo<=timestamp<hi]
        if len(matches)>1:return 'SIGNAL_AMBIGUOUS'
        if not matches:return 'UNKNOWN'
        return matches[0] if matches[0] in {'red','green','yellow','red-yellow'} else 'UNKNOWN'
    ledger=[]
    for row in events:
        reasons=[];pet=None;times={}
        try:
            pet=float(row['pet_s']);lon=float(row['conflict_lon_deg']);lat=float(row['conflict_lat_deg'])
            if not all(map(math.isfinite,(pet,lon,lat))) or not -180<=lon<=180 or not -90<=lat<=90:raise ValueError()
            times={key:int(row[key]) for key in ('ts_enter_encroaching_ms','ts_leave_encroaching_ms','ts_enter_priority_ms','ts_leave_priority_ms')}
            for role in ('encroaching','priority'):
                if times['ts_leave_'+role+'_ms']<times['ts_enter_'+role+'_ms']:reasons.append('INVALID_OCCUPANCY_ORDER')
        except (ValueError,KeyError):reasons.append('INVALID_NUMERIC_OR_TIME')
        if row['scenario_id'] not in scenarios:reasons.append('MISSING_SCENARIO')
        for role in ('encroaching','priority'):
            if row[role+'_object_id'] not in objects:reasons.append('MISSING_'+role.upper()+'_OBJECT')
        result={'event_id':row['event_id'],'scenario_id':row['scenario_id'],'pet_s':pet,'valid':not reasons,'reasons':'|'.join(reasons)}
        for role in ('encroaching','priority'):
            group=mapping.get(row['scenario_id'],{}).get(role+'_signal_group_id')
            t=times.get('ts_enter_'+role+'_ms')
            result[role+'_signal']=signal(group,t) if group and t is not None else 'UNKNOWN'
        ledger.append(result)
    summary=[]
    for scenario in sorted(scenarios):
        rows=[r for r in ledger if r['scenario_id']==scenario];valid=[r for r in rows if r['valid'] and r['pet_s']>=0]
        known=[r for r in valid if all(r[role+'_signal'] not in {'UNKNOWN','SIGNAL_AMBIGUOUS'} for role in ('encroaching','priority'))]
        for tau in (.5,1.,1.5,2.):
            count=sum(r['pet_s']<=tau for r in valid)
            summary.append({'scenario_id':scenario,'n_raw':len(rows),'n_invalid':sum(not r['valid'] for r in rows),'n_negative_pet':sum(r['valid'] and r['pet_s']<0 for r in rows),'n_valid_nonnegative_pet':len(valid),'n_zero_pet':sum(r['pet_s']==0 for r in valid),'n_signal_known_among_valid_nonnegative':len(known),'tau_s':tau,'n_short_pet':count,'short_pet_share':count/len(valid) if valid else None,'data_kind':'REAL','scope':'SAMPLE_DAY'})
    out.mkdir(parents=True,exist_ok=False)
    for name,rows in [('event_reference',ledger),('scenario_threshold_reference',summary)]:
        with (out/(name+'.csv')).open('x',encoding='utf-8',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    provenance={'profile':'PET_INTERACTIONS_V1','scope':'SAMPLE_DAY_REFERENCE_ONLY','input_sha256':EXPECTED,'source_snapshot':source,'source_unchanged':source==snapshot(),'table_rows':{'pet_events':len(events),'objects':len(objects),'scenarios':len(scenarios),'mapping':len(mapping),'signals':len(states)},'n_invalid':sum(not r['valid'] for r in ledger),'n_negative_pet':sum(r['valid'] and r['pet_s']<0 for r in ledger),'n_valid_nonnegative_pet':sum(r['valid'] and r['pet_s']>=0 for r in ledger),'environment_manifest_ref':environment_manifest_ref(ROOT),'denominator':'Recorded valid nonnegative PET interactions only; not traffic volume','signal_reference':'Each role at its own conflict-area enter timestamp, [start,end)','measurement':'Provider-derived PET values; no independent geometric validation','accuracy':None,'ai_status':'NOT_RUN_REFERENCE_CALCULATION','not_performed':['deduplication repair (duplicate keys abort this reference)','multiday uncertainty','AI','full R1 pipeline validation']}
    provenance['table_rows']['objects']=object_rows
    provenance['object_unique_ids']=len(objects)
    provenance['object_ids_with_multiple_rows']=sum(n>1 for n in object_multiplicity.values())
    provenance['object_link_scope']='ID existence only; no static/movement attributes selected or claimed unambiguous'
    with (out/'reference_provenance.json').open('xb') as f:f.write(encode(provenance))
    print(json.dumps({k:v for k,v in provenance.items() if k!='source_snapshot'}))
if __name__=='__main__':main()

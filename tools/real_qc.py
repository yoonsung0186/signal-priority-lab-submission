"""SIT-PET descriptive QC only: does not infer stop lines or entry events."""
import argparse,collections,csv,gzip,hashlib,io,json,math,pathlib,zipfile,os
from snapshot import snapshot,digest,ROOT
from preflight import safe_child,verify_root

def jsonout(p,value):
    with p.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False)
def table(out,name,rows):
    import pyarrow as pa
    import pyarrow.parquet as pq
    with (out/(name+'.csv')).open('x',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    pq.write_table(pa.Table.from_pylist(rows),out/(name+'.parquet'))
def analyze(archive,out):
    manifest=json.loads((archive.parent/'acquisition.json').read_text())
    if manifest['rights']['analysis_status']!='ALLOW':raise ValueError('Analysis rights blocked')
    if digest(archive.read_bytes())!=manifest['sha256']:raise ValueError('Raw hash mismatch')
    out.mkdir(parents=True,exist_ok=False)
    members=[];summary=[];classes=collections.Counter();statuses=collections.Counter();tracks=set();last={};duplicate=0;reverse=0;gaps=0;invalid=0;min_t=None;max_t=None;speed_sum=0.;speed_n=0;signals=collections.defaultdict(list)
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            if not info.filename.endswith(('.csv','.csv.gz')):continue
            member={'path':info.filename,'uncompressed_member_bytes':info.file_size}; members.append(member)
            with z.open(info) as raw:
                stream=gzip.GzipFile(fileobj=raw) if info.filename.endswith('.gz') else raw
                with io.TextIOWrapper(stream,encoding='utf-8-sig',newline='') as text:
                    reader=csv.DictReader(text);fields=reader.fieldnames or [];n=0
                    is_tracks={'object_id','timestamp_ms','lon_deg','lat_deg','tracking_status'}.issubset(fields)
                    for row in reader:
                        n+=1
                        if n>25000000:raise ValueError('Row resource limit')
                        if {'signal_group_id','start_timestamp_ms','end_timestamp_ms','signal_state'}.issubset(fields):
                            signals[row['signal_group_id']].append((int(row['start_timestamp_ms']),int(row['end_timestamp_ms']),row['signal_state']))
                        if is_tracks:
                            oid=row['object_id'];tracks.add(oid);classes[row.get('object_class','UNKNOWN')]+=1;statuses[row['tracking_status']]+=1
                            try:
                                t=int(row['timestamp_ms']);lon=float(row['lon_deg']);lat=float(row['lat_deg'])
                                if not(math.isfinite(lon) and math.isfinite(lat) and -180<=lon<=180 and -90<=lat<=90):raise ValueError()
                            except (ValueError,TypeError):invalid+=1;continue
                            min_t=t if min_t is None else min(min_t,t);max_t=t if max_t is None else max(max_t,t)
                            key=(info.filename,oid)
                            if key in last:
                                gap=t-last[key];duplicate+=gap==0;reverse+=gap<0;gaps+=gap>300
                            last[key]=t
                            try:
                                speed=float(row['speed_ms'])
                                if not math.isfinite(speed) or speed<0:raise ValueError()
                                if row['tracking_status']=='TRACKING':speed_sum+=speed;speed_n+=1
                            except (ValueError,TypeError,KeyError):pass
                    member.update(rows=n,columns=fields,role='trajectories' if is_tracks else 'context')
                    summary.append({'table':info.filename,'row_count':n,'data_kind':'REAL','role':member['role']})
    table(out,'table_inventory',summary)
    class_rows=[{'object_class':k,'trajectory_rows':v,'data_kind':'REAL','count_unit':'trajectory_samples_not_vehicles'} for k,v in sorted(classes.items())]
    table(out,'class_sample_counts',class_rows)
    signal_rows=[]
    for group,intervals in sorted(signals.items()):
        intervals.sort(); end=None;covered=0;overlaps=0;bad=0
        for lo,hi,state in intervals:
            if hi<=lo:bad+=1;continue
            if end is not None and lo<end:overlaps+=1
            covered+=max(0,hi-max(lo,end if end is not None else lo));end=hi if end is None else max(end,hi)
        signal_rows.append({'signal_group_id':group,'interval_rows':len(intervals),'union_duration_s':covered/1000,'invalid_intervals':bad,'overlapping_intervals':overlaps,'first_timestamp_ms':intervals[0][0],'last_timestamp_ms':max(v[1] for v in intervals),'data_kind':'REAL','use':'signal_only_coverage_not_approach_exposure'})
    table(out,'signal_coverage',signal_rows)
    metrics={'data_kind':'REAL','analysis_kind':'DESCRIPTIVE_INPUT_QC_ONLY','source_id':'sitpet','source_version':'1.1','source_sha256':manifest['sha256'],'trajectory_rows':sum(classes.values()),'unique_object_ids':len(tracks),'tracking_status_counts':dict(statuses),'invalid_time_or_coordinates':invalid,'adjacent_duplicate_timestamps':duplicate,'timestamp_reversals':reverse,'adjacent_gaps_over_0_3s':gaps,'first_timestamp_ms':min_t,'last_timestamp_ms':max_t,'tracking_sample_mean_speed_mps':speed_sum/speed_n if speed_n else None,'speed_denominator_tracking_samples':speed_n,'C':None,'G':None,'U':None,'N':None,'red_entry_rate':None,'accuracy':None,'population_ci':None,'manual_review_status':'NOT_PERFORMED','primary_entry_status':'BLOCKED_UNVERIFIED_BOUNDARY_AND_MOVEMENT_SIGNAL_MAPPING','selection':'Entire publisher-provided example archive; not a random site/population sample. Metadata objects are PET-selected. No event-driven resampling.'}
    jsonout(out/'real_qc.json',metrics);jsonout(out/'member_inventory.json',members)
    jsonout(out/'provenance.json',{'source_snapshot':snapshot(),'config_sha256':digest((ROOT/'configs/project.yaml').read_bytes()),'input_sha256':manifest['sha256'],'license':'CC-BY-4.0','license_url':'https://creativecommons.org/licenses/by/4.0/','attribution':'Steinmassl, Rehrl, Vornberger (2026). SIT-PET. doi:10.3390/data11040068. Dataset doi:10.5281/zenodo.19231310','changes':'Computed descriptive input QC and sample-count tables; no entry labels inferred.','raw_included':False})
    os.environ['MPLCONFIGDIR']=str(safe_child(ROOT,'.cache/matplotlib'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,ax=plt.subplots(figsize=(8,4.6));ax.bar([x['object_class'] for x in class_rows],[x['trajectory_rows'] for x in class_rows],color='#246c84');ax.set(ylabel='Trajectory samples (rows)',title='SIT-PET public example: observed sample classes');ax.ticklabel_format(axis='y',style='plain');fig.text(.02,.015,'Descriptive input QC only. Samples are not vehicle counts or entry rates.',fontsize=9);fig.tight_layout(rect=(0,.05,1,1))
    fig.savefig(out/'class_samples.png',dpi=150);fig.savefig(out/'class_samples.svg');plt.close(fig)
    jsonout(out/'figures_index.json',[{'figure_id':'QC01','files':['class_samples.png','class_samples.svg'],'plot_data':'class_sample_counts.csv','source':'provenance.json','status':'REAL_DESCRIPTIVE_QC'}])
    print(json.dumps(metrics))
def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args();verify_root(ROOT)
    out=safe_child(ROOT,a.output)
    if not out.is_relative_to(ROOT/'runs'):raise ValueError('Run output required')
    analyze(ROOT/'data/raw/sitpet_v1_1/examples.zip',out)
if __name__=='__main__':main()

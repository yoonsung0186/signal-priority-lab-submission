"""Stable technical PET tables/plots; no report generation or raw archive copies."""
import csv,json
from pathlib import Path
from .pet_contracts import stable_json

def write_json(path,value):
    with path.open('xb') as stream:stream.write(stable_json(value))

def flattened(row):
    return {key:json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')) if isinstance(value,(dict,list)) else value for key,value in row.items()}

def write_table(directory,name,rows):
    import pyarrow as pa
    import pyarrow.parquet as pq
    fields=sorted({key for row in rows for key in row})
    if not fields:fields=['status']
    with (directory/(name+'.csv')).open('x',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,lineterminator='\n');writer.writeheader()
        for row in rows:writer.writerow(flattened(row))
    shards=[]
    if len(rows)<=25000:
        path=directory/(name+'.parquet')
        with path.open('xb') as stream:pq.write_table(pa.Table.from_pylist([flattened(row) for row in rows]) if rows else pa.table({'status':pa.array([],type=pa.string())}),stream,compression='zstd')
        shards=[{'path':path.name,'row_count':len(rows)}]
    else:
        child=directory/(name+'_parquet');child.mkdir(exist_ok=False)
        for start in range(0,len(rows),25000):
            path=child/('part_%05d.parquet' % (start//25000))
            with path.open('xb') as stream:pq.write_table(pa.Table.from_pylist([flattened(row) for row in rows[start:start+25000]]),stream,compression='zstd')
            shards.append({'path':path.relative_to(directory).as_posix(),'row_count':min(25000,len(rows)-start)})
    write_json(directory/(name+'_parquet_index.json'),{'table':name,'row_count':len(rows),'shards':shards,'nested_columns':'STABLE_JSON_STRINGS'})

def figures(directory,tables,input_refs,ai_validation=None,ai_tables=None):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    out=directory/'figures';out.mkdir(exist_ok=False)
    entries=[]
    ecdf=tables['pet_ecdf'];plot=[]
    # At most 256 deterministic display points per scenario. Full ECDF retained.
    scenarios=sorted({row['scenario_id'] for row in ecdf})
    for scenario in scenarios:
        rows=[row for row in ecdf if row['scenario_id']==scenario]
        indices=sorted({round(i*(len(rows)-1)/min(255,len(rows)-1)) for i in range(min(256,len(rows)))}) if len(rows)>1 else [0]
        plot.extend(dict(rows[i],display_sampling='DETERMINISTIC_ECDF_INDEX_GRID_MAX_256_PER_SCENARIO') for i in indices)
    write_table(directory,'figure_pet_ecdf_plot_data',plot)
    if plot:
        fig,axis=plt.subplots(figsize=(8,5))
        for scenario in scenarios:
            rows=[row for row in plot if row['scenario_id']==scenario]
            axis.step([row['pet_s'] for row in rows],[row['ecdf'] for row in rows],where='post',label=scenario)
            axis.scatter([rows[-1]['pet_s']],[rows[-1]['ecdf']],s=14)
        axis.set(xlabel='Provider PET (s), valid nonnegative interactions',ylabel='Empirical cumulative fraction',title='Recorded interaction PET by scenario')
        axis.set_ylim(0,1.03);axis.set_yticks([0,.2,.4,.6,.8,1])
        axis.legend(fontsize=7);fig.tight_layout()
        for suffix in ('png','svg'):fig.savefig(out/('pet_ecdf.'+suffix),dpi=160)
        plt.close(fig)
        entries.append({'figure_id':'PET_ECDF','files':['figures/pet_ecdf.png','figures/pet_ecdf.svg'],'source_table':'figure_pet_ecdf_plot_data.csv','full_source_table':'pet_ecdf.csv','input_refs':input_refs,'data_kind':'REAL','display_point_cap_per_scenario':256})
    ai_validation=ai_validation or {};ai_tables=ai_tables or {}
    if ai_validation.get('status','').startswith('FITTED') and ai_tables.get('cluster_profile'):
        plot=[{'cluster':row['cluster'],'n_events':row['n_events'],
               'encroaching_occupancy_median_s':row['encroaching_occupancy_s_median'],
               'priority_occupancy_median_s':row['priority_occupancy_s_median'],
               'scope':'NO_HELDOUT_SINGLE_DAY' if ai_validation.get('heldout_metrics') is None else 'CHRONOLOGICAL_HELDOUT_DESCRIPTION'} for row in ai_tables['cluster_profile']]
        write_table(directory,'figure_ai_cluster_plot_data',plot)
        fig,axis=plt.subplots(figsize=(8,5));x=list(range(len(plot)))
        axis.bar([value-.18 for value in x],[row['encroaching_occupancy_median_s'] for row in plot],width=.36,label='Encroaching role')
        axis.bar([value+.18 for value in x],[row['priority_occupancy_median_s'] for row in plot],width=.36,label='Priority role')
        axis.set_xticks(x,['Type %s\nN=%s' % (row['cluster'],row['n_events']) for row in plot])
        axis.set(ylabel='Median own-role occupancy duration (s)',title='Occupancy and size types: descriptive analysis')
        axis.legend();fig.tight_layout()
        for suffix in ('png','svg'):fig.savefig(out/('ai_occupancy_types.'+suffix),dpi=160)
        plt.close(fig)
        entries.append({'figure_id':'AI_OCCUPANCY_TYPES','files':['figures/ai_occupancy_types.png','figures/ai_occupancy_types.svg'],
                        'source_table':'figure_ai_cluster_plot_data.csv','input_refs':input_refs,'heldout_metrics':ai_validation.get('heldout_metrics'),
                        'claim_scope':'Descriptive occupancy/size types; no accident or violation accuracy'})
    if ai_validation.get('status','').startswith('FITTED') and ai_validation.get('stability'):
        plot=ai_validation['stability'];write_table(directory,'figure_ai_stability_plot_data',plot)
        fig,axis=plt.subplots(figsize=(7,4));axis.bar([str(row['seed']) for row in plot],[row['ari'] for row in plot])
        axis.set(ylim=(-1,1),ylabel='Adjusted Rand index (cluster agreement)',xlabel='Refit seed versus 20261012',title='Type agreement across fixed seeds')
        fig.tight_layout()
        for suffix in ('png','svg'):fig.savefig(out/('ai_seed_agreement.'+suffix),dpi=160)
        plt.close(fig)
        entries.append({'figure_id':'AI_SEED_AGREEMENT','files':['figures/ai_seed_agreement.png','figures/ai_seed_agreement.svg'],
                        'source_table':'figure_ai_stability_plot_data.csv','input_refs':input_refs,'meaning':'Agreement, not independent detection accuracy'})
    write_json(directory/'figures_index.json',{'figures':entries,'not_generated':[] if entries else ['NO_VALID_NONNEGATIVE_PET'],'no_ai_figures_when_not_run':True})

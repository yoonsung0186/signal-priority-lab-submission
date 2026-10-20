"""Technical evidence tables and deterministic scientific figures for R2."""
import json
from pathlib import Path
import pandas as pd
from .pet_io import file_hash


def candidate_table(tables, definitions):
    point=tables['unit_summary']; rows=[]
    for scenario in sorted(set(point['scenario_id'])-{'ALL'}):
        p=point[(point.scenario_id==scenario)&(point.unit=='A')&(point.tau_s==1)].iloc[0]
        def bounds(name):
            t=tables[name];t=t[(t.scenario_id==scenario)&(t.tau_s==1)]
            if name!='unit_summary':t=t[t.unit=='A']
            vals=t['rank'].dropna() if 'rank' in t else pd.concat([t.rank_min,t.rank_max]).dropna()
            return (int(vals.min()),int(vals.max())) if len(vals) else (None,None)
        unit=bounds('unit_summary');month=bounds('monthly_rank_sensitivity')
        leave=bounds('leave_one_month_rank_sensitivity');boot=bounds('bootstrap_rank_summary')
        changes=tables['signal_event_changes'];changes=changes[(changes.scenario_id==scenario)&(changes.population=='SHORT_NONNEGATIVE_PET_LE_1S')]
        multiplicity=tables['pair_multiplicity'];m=multiplicity[multiplicity.scenario_id==scenario]
        ranges=[r for r in (unit,month,leave,boot) if r[0] is not None]
        base_rank=int(p['rank']) if pd.notna(p['rank']) else None
        observed=([base_rank] if base_rank is not None else [])+[v for r in ranges for v in r]
        insufficient=base_rank is None or len(ranges)<4
        unstable=bool(observed) and min(observed)!=max(observed)
        rows.append({'scenario_id':scenario,'scenario_definition':definitions[scenario],
            'tau_s':1.,'original_A_numerator':int(p.numerator),'original_A_denominator':int(p.denominator),
            'original_A_fraction':float(p.fraction) if pd.notna(p.fraction) else None,'original_A_rank':base_rank,
            'unit_rank_min':unit[0],'unit_rank_max':unit[1],'monthly_rank_min':month[0],'monthly_rank_max':month[1],
            'leave_month_rank_min':leave[0],'leave_month_rank_max':leave[1],'bootstrap_rank_min':boot[0],'bootstrap_rank_max':boot[1],
            'max_short_event_signal_pair_change_rate':float(changes.pair_state_change_rate.max()) if len(changes) else None,
            'n_pairs_multiple_conflict_coordinates':int(m.iloc[0].n_pairs_multiple_provider_conflict_coordinates) if len(m) and pd.notna(m.iloc[0].n_pairs_multiple_provider_conflict_coordinates) else None,
            'candidate_status':'INSUFFICIENT_SUPPORT_ADDITIONAL_OBSERVATION_REQUIRED' if insufficient else 'ADDITIONAL_OBSERVATION_REQUIRED_UNSTABLE' if unstable else 'DESCRIPTIVELY_STABLE_WITH_UNRESOLVED_QUALITY',
            'clock_interpretation':'Unconditioned PET rank is invariant to event-only shift; signal contexts may change',
            'unknown_factors':'Independent geometry/truth; source-paper precision and missing-period verification; representativeness; day dependence',
            'evidence_tables':'unit_summary;monthly_rank_sensitivity;leave_one_month_rank_sensitivity;bootstrap_rank_summary;signal_event_changes;pair_multiplicity'})
    return pd.DataFrame(rows)


def create_figures(out, tables):
    import matplotlib
    matplotlib.use('Agg')
    matplotlib.rcParams['svg.hashsalt']='SPL_R2_FIXED'
    import matplotlib.pyplot as plt
    out=Path(out);figs=out/'figures';figs.mkdir()
    records=[]
    def save(fig,name,source,caption):
        fig.tight_layout()
        fig.savefig(figs/(name+'.png'),dpi=150,metadata={'Software':'Signal Priority Lab R2'})
        fig.savefig(figs/(name+'.svg'),metadata={'Date':None,'Creator':'Signal Priority Lab R2'})
        plt.close(fig)
        records.append({'figure_id':name,'files':['figures/'+name+'.png','figures/'+name+'.svg'],
                        'file_sha256':{ext:file_hash(figs/(name+'.'+ext)) for ext in ('png','svg')},
                        'source_table':source,'source_table_sha256':file_hash(out/source),'caption':caption,
                        'subset':{'unit_denominators':'scenario != ALL; tau_s == 1; units A/B/C/D',
                                  'event_state_change':'scenario_id == ALL_SCENARIOS; all named populations',
                                  'common_date_rank':'unit == A; scenario != ALL; tau_s == 1'}[name]})
    p=tables['unit_summary'];p=p[(p.scenario_id!='ALL')&(p.tau_s==1)]
    fig,ax=plt.subplots(figsize=(9,5))
    for unit,t in p.groupby('unit',sort=True):ax.plot(t.scenario_id,t.fraction*100,marker='o',label=unit)
    ax.set(xlabel='Provider scenario',ylabel='PET <= 1 s / selected nonnegative events (%)',title='Observation-unit and denominator sensitivity')
    ax.legend(title='Unit');save(fig,'unit_denominators','unit_summary.csv','Conditional recorded-event fractions, not traffic risk')
    p=tables['signal_event_changes'];p=p[p.scenario_id=='ALL_SCENARIOS']
    fig,ax=plt.subplots(figsize=(9,5))
    for population,t in p.groupby('population',sort=True):ax.plot(t.clock_offset_s,t.pair_state_change_rate*100,marker='o',label=population)
    ax.set(xlabel='Event-only offset (s); signal clock fixed',ylabel='Same-event role-state pair changed (%)',title='Signal context stress sensitivity')
    ax.legend(fontsize=7);save(fig,'event_state_change','signal_event_changes.csv','Own-role entry times differ; no stop-line inference')
    p=tables['bootstrap_rank_summary'];p=p[(p.unit=='A')&(p.scenario_id!='ALL')&(p.tau_s==1)]
    fig,ax=plt.subplots(figsize=(9,5));x=list(range(len(p)))
    ax.vlines(x,p.rank_min,p.rank_max,color='tab:blue',lw=4);ax.scatter(x,p.rank_min,color='tab:blue');ax.scatter(x,p.rank_max,color='tab:blue')
    ax.set_xticks(x,p.scenario_id);ax.set(xlabel='Provider scenario',ylabel='Exploratory rank (1 = largest fraction)',title='Shared-date resampling rank range: A, PET <= 1 s')
    ax.invert_yaxis();save(fig,'common_date_rank','bootstrap_rank_summary.csv','200 common-date draws; date independence not assumed')
    return records

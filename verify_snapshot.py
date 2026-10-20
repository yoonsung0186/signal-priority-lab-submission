"""Read-only snapshot/hash and saved-table checks. No fit or raw replay."""
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def rows(path):
    with (ROOT/path).open(encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))

def main():
    manifest=json.loads((ROOT/'FILE_MANIFEST.json').read_bytes())
    for x in manifest['files']:
        p=ROOT/x['path']
        assert p.resolve().is_relative_to(ROOT) and not p.is_symlink()
        d=p.read_bytes()
        assert len(d)==x['bytes'] and hashlib.sha256(d).hexdigest()==x['sha256'],x['path']
    base='review/runs/finish_multiday_20261013_01/'
    b={x['metric_id']:x for x in rows(base+'metrics.csv')}
    assert int(b['qc_valid_nonnegative']['numerator'])==924775
    assert int(b['qc_valid_negative_review']['numerator'])==4183
    assert int(b['qc_invalid']['numerator'])==0
    assert int(b['qc_valid_nonnegative']['denominator'])==928958==924775+4183
    r2='review/runs/r2_finish_20261013_01/analysis/'
    expected={'A':(4492,924775),'B':(4492,72223),'C':(3089,781168),'D':(3089,63427)}
    selected=[x for x in rows(r2+'unit_summary.csv') if x['scenario_id']=='ALL' and x['period_type']=='FULL_PERIOD' and float(x['tau_s'])==1]
    assert len(selected)==4
    for x in selected:assert (int(x['numerator']),int(x['denominator']))==expected[x['unit']]
    q='review/runs/r2_quality_boundary_20261013_01/'
    x=next(x for x in rows(q+'metrics.csv') if float(x['tau_s'])==1)
    parts=[(int(x[k+'_n_short_pet']),int(x[k+'_n_valid_nonnegative_pet'])) for k in ['quality_held','remaining_unknown','interpretable']]
    assert parts==[(267,42259),(5,1250),(4220,881266)]
    assert sum(n for n,d in parts)==4492 and sum(d for n,d in parts)==924775
    literal=rows(r2+'signal_condition_pet.csv'); overlay=rows(q+'analysis/signal_condition_quality.csv')
    assert len(literal)==len(overlay)==1152
    for old,new in zip(literal,overlay):
        assert all(new[k]==v for k,v in old.items())
        for tail in ['n_short_pet','n_valid_nonnegative_pet']:
            assert sum(int(new[k+'_'+tail]) for k in ['quality_held','remaining_unknown','interpretable'])==int(new[tail])
        if int(new['interpretable_n_valid_nonnegative_pet'])==0:assert new['interpretable_short_pet_fraction']==''
    # Sum disjoint numerators and denominators, never add fractions.
    print(json.dumps({'status':'PASS','manifest_files':len(manifest['files']),'saved_table_checks':'A/B/C/D and partition; 1152 literal rows preserved','full_raw_data_reproduction':False,'new_real_fits':0}))

if __name__=='__main__':main()

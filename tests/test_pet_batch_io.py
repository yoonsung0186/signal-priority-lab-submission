"""Bounded-input regressions use synthetic fixtures, never provider counts."""
import unittest,uuid,csv,io,gzip,zipfile
from copy import deepcopy
from pathlib import Path
from spl.pet_batch_io import BatchStore,offset_padding_ms
from spl.pet_qc import build_ledger
from spl.pet_io import load_tables,file_hash
from spl.pet_contracts import timestamp_integer,integer
from spl.pet_signals import SignalIndex
from test_pet import fixture,PROFILE,BASE

class BatchInputTests(unittest.TestCase):
    def test_exact_scientific_timestamp_without_key_relaxation_or_rounding(self):
        self.assertEqual(timestamp_integer('1.742945e+12'),1742945000000)
        self.assertIsNone(integer('1.742945e+12'))
        for value in ('1.234e2','NaN','1e999','1.5',True,1.5):
            with self.subTest(value=value):self.assertIsNone(timestamp_integer(value))
        index=SignalIndex.from_rows([{'signal_group_id':'S','start_timestamp_ms':'1742944998908','end_timestamp_ms':'1.742945e+12','signal_state':'red-yellow'},
            {'signal_group_id':'S','start_timestamp_ms':'1.742945e+12','end_timestamp_ms':'1742945020920','signal_state':'green'}])
        self.assertEqual(index.lookup('S',1742944999999)['state'],'red-yellow')
        self.assertEqual(index.lookup('S',1742945000000)['state'],'green')
        self.assertEqual(index.qc['n_invalid'],0)
    def make(self,clock_offsets_s=None):
        path=Path(__file__).resolve().parents[1]/'worker_evidence'/('r2_signals_batch_'+uuid.uuid4().hex)/'cache.sqlite'
        store=BatchStore(path,clock_offsets_s);self.addCleanup(store.close);return store

    def test_duplicate_keys_across_dates_and_global_object_variants(self):
        data=fixture();other=dict(data['pet_events'][0],ts_enter_encroaching_ms=str(BASE+86400000))
        data['pet_events']+=[deepcopy(data['pet_events'][0]),other]
        data['objects'].append(dict(data['objects'][0],object_day='2025-06-02',movement_category='other'))
        s=self.make()
        for table,rows in data.items():
            for i,row in enumerate(rows):s.sink(table,dict(row,__source={'source_index':0,'source_row_id':i+1}))
        s.finish({'tables':[{'source_file_sha256':'synthetic'}]})
        batches=list(s.batches());self.assertEqual(len(batches),1)
        ledger,qc,_,_=build_ledger(batches[0][0],PROFILE,s.inventory)
        self.assertEqual(qc['n_invalid'],1);self.assertEqual(ledger[0]['n_source_rows'],3)
        self.assertEqual(ledger[0]['encroaching_object_variant_count'],2)
        self.assertEqual(s.global_qc['event_rows']['n_identical_duplicate_rows_merged'],1)
        self.assertEqual(s.global_qc['event_rows']['n_conflicting_duplicate_rows_extra'],1)

    def test_long_overlap_and_invalid_signal_block_retained(self):
        s=self.make()
        for table,rows in fixture().items():
            for i,row in enumerate(rows):s.sink(table,dict(row,__source={'source_index':0,'source_row_id':i+1}))
        rows=[dict(signal_group_id='E',start_timestamp_ms=str(BASE-86400000),end_timestamp_ms=str(BASE+5000),signal_state='red'),
              dict(signal_group_id='E',start_timestamp_ms=str(BASE),end_timestamp_ms=str(BASE+2000),signal_state='green'),
              dict(signal_group_id='P',start_timestamp_ms='bad',end_timestamp_ms='bad',signal_state='red')]
        for i,row in enumerate(rows):s.sink('signal_states',dict(row,__source={'source_index':0,'source_row_id':i+1}))
        s.finish({'tables':[{'source_file_sha256':'synthetic'}]})
        _,signals=next(s.batches())
        self.assertEqual(signals.lookup('E',BASE+1000)['state'],'SIGNAL_AMBIGUOUS')
        self.assertEqual(signals.lookup('E',BASE+2500)['state'],'red')
        self.assertEqual(signals.lookup('P',BASE+1000)['state'],'UNKNOWN')
        self.assertEqual(s.global_qc['signal_rows']['n_overlapping_intervals'],1)
        self.assertEqual(s.global_qc['signal_rows']['n_invalid'],1)

    def test_existing_cache_not_overwritten(self):
        s=self.make()
        with self.assertRaises(FileExistsError):BatchStore(s.path)

    def test_configured_stress_offset_and_boundary_signal_window(self):
        s=self.make([-2,0,2])
        for table,rows in fixture().items():
            for i,row in enumerate(rows):s.sink(table,dict(row,__source={'source_index':0,'source_row_id':i+1}))
        # A transition queried only after +2 s, at a half-open interval start.
        for i,row in enumerate([
            dict(signal_group_id='E',start_timestamp_ms=str(BASE+3000),end_timestamp_ms=str(BASE+5000),signal_state='yellow'),
            dict(signal_group_id='E',start_timestamp_ms=str(BASE-5000),end_timestamp_ms=str(BASE-500),signal_state='green')]):
            s.sink('signal_states',dict(row,__source={'source_index':0,'source_row_id':i+1}))
        s.finish({'tables':[{'source_file_sha256':'synthetic'}]})
        _,old=next(s.batches([-.5,0,.5]));self.assertEqual(old.lookup('E',BASE+3000)['state'],'UNKNOWN')
        _,stress=next(s.batches());self.assertEqual(stress.lookup('E',BASE+3000)['state'],'yellow')
        self.assertEqual(stress.lookup('E',BASE-1000)['state'],'green')

    def test_padding_rounds_outward_and_rejects_nonfinite(self):
        self.assertEqual(offset_padding_ms(),500)
        self.assertEqual(offset_padding_ms([-2,.5]),2000)
        self.assertEqual(offset_padding_ms([-.5001]),501)
        for value in ([True],[float('nan')],[float('inf')]):
            with self.subTest(value=value),self.assertRaises(ValueError):offset_padding_ms(value)

    def test_root_member_mapping_and_stream_sink_same_inventory(self):
        root=Path(__file__).resolve().parents[1];s=self.make();path=s.path.parent/'fixture.zip'
        data=fixture();members={t:t+'.csv.gz' for t in data}
        with zipfile.ZipFile(path,'x') as archive:
            for table,rows in data.items():
                stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
                archive.writestr(members[table],gzip.compress(stream.getvalue().encode(),mtime=0))
        profile={'inputs':[{'archive':path.relative_to(root).as_posix(),'sha256':file_hash(path),'tables':list(data),'members':members}]}
        eager,_,a=load_tables(root,profile);empty,_,b=load_tables(root,profile,sink=s.sink)
        self.assertEqual(a,b);self.assertEqual(len(eager['pet_events']),1);self.assertFalse(empty['pet_events'])
        s.finish(b);tables,_=next(s.batches());self.assertEqual(tables['pet_events'],eager['pet_events'])

if __name__=='__main__':unittest.main()

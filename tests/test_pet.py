"""Synthetic PET contract fixtures, independent of provider sample counts."""
from copy import deepcopy
import csv,gzip,io,json,pathlib,sys,unittest,uuid,zipfile
from spl.pet_contracts import TABLE_MEMBERS,REQUIRED,digest
from spl.pet_io import load_tables,LimitedReader
from spl.pet_qc import build_ledger,event_time
from spl.pet_signals import SignalIndex,contexts
from spl.pet_metrics import summarize,sensitivity_rows
from spl.contracts import ContractError

ROOT=pathlib.Path(__file__).resolve().parents[1]
BASE=1748736000000

def fixture():
    scenario={'scenario_id':'S','encroaching_movement_category':'vehicle','encroaching_conflict_leg':'east','encroaching_turning_movement':'straight',
              'priority_movement_category':'pedestrian','priority_conflict_leg':'north','priority_turning_movement':''}
    objects=[{'object_id':'1','object_day':'2025-06-01','object_class_static':'vehicle','length_m_static':'4','width_m_static':'2',
              'movement_category':'vehicle','conflict_leg':'east','turning_movement':'straight'},
             {'object_id':'2','object_day':'2025-06-01','object_class_static':'pedestrian','length_m_static':'0.5','width_m_static':'0.5',
              'movement_category':'pedestrian','conflict_leg':'north','turning_movement':''}]
    event={'event_id':'10','scenario_id':'S','encroaching_object_id':'1','priority_object_id':'2',
           'ts_enter_encroaching_ms':str(BASE+1000),'ts_leave_encroaching_ms':str(BASE+1500),
           'ts_enter_priority_ms':str(BASE+2000),'ts_leave_priority_ms':str(BASE+2300),
           'pet_s':'0.5','encroachment_duration_s':'0.5','conflict_lon_deg':'13','conflict_lat_deg':'47'}
    return {'pet_events':[event],'objects':objects,'scenarios':[scenario],
            'scenario_signal_mapping':[{'scenario_id':'S','encroaching_signal_group_id':'E','priority_signal_group_id':'P'}]}

def signals():
    return SignalIndex.from_rows([{'signal_group_id':group,'start_timestamp_ms':str(BASE),'end_timestamp_ms':str(BASE+5000),'signal_state':state} for group,state in [('E','red'),('P','red-yellow')]])

PROFILE={'profile_id':'PET_INTERACTIONS_V1','dataset_id':'SYNTHETIC_UNIT_FIXTURE','source_version':'TEST','data_kind':'SYNTHETIC','local_timezone':'Europe/Vienna'}

class PETContractTests(unittest.TestCase):
    def ledger(self,data=None):return build_ledger(data or fixture(),PROFILE)

    def test_reconciliation_and_conditional_denominator(self):
        ledger,qc,mapping,scenarios=self.ledger();context=contexts(ledger,mapping,signals())
        result=summarize(ledger,context,scenarios)
        self.assertEqual(qc['n_raw'],1);self.assertEqual(qc['n_valid_nonnegative_pet'],1)
        self.assertTrue(all(qc['reconciliation'].values()))
        row=result['pet_thresholds'][0];self.assertEqual(row['n_short_pet'],1);self.assertEqual(row['short_pet_fraction'],1)
        self.assertEqual(result['scenario_summary'][0]['unique_object_count'],2)
        self.assertEqual(row['n_valid_nonnegative_pet'],1)
        self.assertEqual(ledger[0]['data_kind'],'SYNTHETIC')
        self.assertIsNone(qc['independent_pet_accuracy'])

    def test_negative_zero_large_preserved(self):
        data=fixture();original=data['pet_events'][0]
        data['pet_events']=[dict(original,event_id=str(i),pet_s=str(value)) for i,value in enumerate([-.5,0,100000])]
        ledger,qc,_,_=self.ledger(data)
        self.assertEqual(sorted(row['pet_s'] for row in ledger),[-.5,0,100000])
        self.assertEqual((qc['n_negative_pet'],qc['n_zero_pet'],qc['n_valid_nonnegative_pet']),(1,1,2))
        self.assertEqual(qc['n_invalid'],0)

    def test_identical_duplicate_merges_provenance(self):
        data=fixture();data['pet_events'].append(deepcopy(data['pet_events'][0]))
        ledger,qc,_,_=self.ledger(data)
        self.assertEqual(len(ledger),1);self.assertEqual(ledger[0]['n_source_rows'],2)
        self.assertEqual(qc['event_rows']['n_identical_duplicate_rows_merged'],1)
        self.assertTrue(qc['reconciliation']['raw_rows_preserved'])

    def test_conflicting_event_key_excluded_all_variants_preserved(self):
        data=fixture();data['pet_events'].append(dict(data['pet_events'][0],pet_s='9'))
        ledger,qc,_,_=self.ledger(data)
        self.assertEqual(qc['n_invalid'],1);self.assertIn('CONFLICTING_EVENT_KEY',ledger[0]['invalid_reasons'])
        self.assertEqual(len(ledger[0]['conflicting_source_variants']),2)
        self.assertTrue(qc['reconciliation']['raw_rows_preserved'])

    def test_missing_object_scenario_and_key(self):
        for field,value in [('priority_object_id','missing'),('scenario_id','missing'),('event_id','')]:
            data=fixture();data['pet_events'][0][field]=value
            with self.subTest(field=field):self.assertEqual(self.ledger(data)[1]['n_invalid'],1)

    def test_missing_mapping_only_signal_excluded(self):
        data=fixture();data['scenario_signal_mapping']=[]
        ledger,qc,mapping,scenarios=self.ledger(data);context=contexts(ledger,mapping,signals())
        self.assertEqual(qc['n_valid_nonnegative_pet'],1)
        self.assertEqual({row['signal_state'] for row in context},{'UNKNOWN'})
        result=summarize(ledger,context,scenarios)['pet_thresholds'][0]
        self.assertIsNone(result['short_pet_fraction_signal_known'])
        self.assertEqual(result['short_pet_fraction'],1)

    def test_object_multiplicity_resolves_role_without_expanding_events(self):
        data=fixture();data['objects'].append(dict(data['objects'][1],conflict_leg='west'))
        ledger,qc,_,_=self.ledger(data)
        self.assertEqual(len(ledger),1);self.assertEqual(qc['n_valid_nonnegative_pet'],1)
        self.assertEqual(ledger[0]['priority_object_variant_count'],2)
        self.assertEqual(ledger[0]['priority_object_join_status'],'RESOLVED_BY_SCENARIO_RELATION')
        self.assertEqual(ledger[0]['priority_width_m'],.5)

    def test_object_matching_attribute_disagreement_null_ai_only(self):
        data=fixture();data['objects'].append(dict(data['objects'][1],width_m_static='0.9'))
        ledger,qc,_,_=self.ledger(data)
        self.assertEqual(qc['n_valid_nonnegative_pet'],1);self.assertIsNone(ledger[0]['priority_width_m'])
        self.assertIn('PRIORITY_STATIC_WIDTH_AMBIGUOUS',ledger[0]['review_reasons'])

    def test_missing_or_unknown_relation_never_resolves_from_empty_equality(self):
        for field in ('movement_category','conflict_leg','turning_movement'):
            for value in (None,'UNKNOWN'):
                data=fixture()
                if value is None:
                    data['objects'][1].pop(field);data['scenarios'][0].pop('priority_'+field)
                else:
                    data['objects'][1][field]=value;data['scenarios'][0]['priority_'+field]=value
                with self.subTest(field=field,value=value):
                    ledger,qc,_,_=self.ledger(data)
                    self.assertEqual(qc['n_valid_nonnegative_pet'],1)
                    self.assertIn('NO_SCENARIO_ROLE_MATCH',ledger[0]['priority_object_join_status'])
                    self.assertEqual(ledger[0]['priority_width_m'],.5)

    def test_documented_empty_turning_still_resolves(self):
        self.assertEqual(self.ledger()[0][0]['priority_object_join_status'],'UNIQUE_RECORD')

    def test_ecdf_terminal_fraction_one(self):
        data=fixture();original=data['pet_events'][0]
        data['pet_events']=[dict(original,event_id=str(i),pet_s=str(value)) for i,value in enumerate([0,.5,.5,155.8])]
        ledger,_,mapping,scenarios=self.ledger(data)
        rows=summarize(ledger,contexts(ledger,mapping,signals()),scenarios)['pet_ecdf']
        self.assertEqual(rows[-1]['ecdf'],1)
        self.assertEqual(rows[-1]['cumulative_count'],4)

    def test_invalid_times_nonfinite_coordinates(self):
        for field,value in [('pet_s','nan'),('pet_s','inf'),('ts_leave_encroaching_ms',str(BASE)),('ts_enter_priority_ms','1.5'),('conflict_lat_deg','91')]:
            data=fixture();data['pet_events'][0][field]=value
            with self.subTest(field=field,value=value):self.assertEqual(self.ledger(data)[1]['n_invalid'],1)

    def test_zero_denominator_null_and_ecdf_population(self):
        data=fixture();data['pet_events'][0]['pet_s']='-1'
        ledger,qc,mapping,scenarios=self.ledger(data)
        rows=summarize(ledger,contexts(ledger,mapping,signals()),scenarios)
        self.assertEqual(rows['pet_ecdf'],[])
        self.assertIsNone(rows['pet_thresholds'][0]['short_pet_fraction'])
        self.assertEqual(rows['inspection_candidates'][0]['candidate_status'],'DESCRIPTIVE_ONLY')

    def test_utc_and_vienna_dst_from_actual_timestamp(self):
        from datetime import datetime,timezone
        timestamp=int(datetime(2025,3,30,1,30,tzinfo=timezone.utc).timestamp()*1000)
        utc,day,local=event_time(timestamp)
        self.assertTrue(utc.endswith('+00:00'));self.assertEqual(local.hour,3);self.assertEqual(local.utcoffset().total_seconds(),7200)

class PETSignalTests(unittest.TestCase):
    def test_halfopen_red_yellow_preserved(self):
        index=SignalIndex.from_rows([{'signal_group_id':'E','start_timestamp_ms':'0','end_timestamp_ms':'10','signal_state':'red'},
                                    {'signal_group_id':'E','start_timestamp_ms':'10','end_timestamp_ms':'20','signal_state':'red-yellow'}])
        self.assertEqual(index.lookup('E',9)['state'],'red')
        self.assertEqual(index.lookup('E',10)['state'],'red-yellow')
        self.assertEqual(index.lookup('E',20)['state'],'UNKNOWN')

    def test_overlap_ambiguous_identical_duplicate_not_overlap(self):
        base={'signal_group_id':'E','start_timestamp_ms':'0','end_timestamp_ms':'10','signal_state':'red'}
        duplicate=SignalIndex.from_rows([base,deepcopy(base)])
        self.assertEqual(duplicate.lookup('E',5)['state'],'red')
        overlap=SignalIndex.from_rows([base,dict(base,start_timestamp_ms='4',end_timestamp_ms='8',signal_state='green')])
        self.assertEqual(overlap.lookup('E',5)['state'],'SIGNAL_AMBIGUOUS')
        self.assertEqual(overlap.lookup('E',9)['state'],'red')

    def test_clock_shift_only_event_and_pet_invariant(self):
        ledger,_,mapping,_=build_ledger(fixture(),PROFILE);original=deepcopy(ledger)
        index=SignalIndex.from_rows([{'signal_group_id':'E','start_timestamp_ms':str(BASE),'end_timestamp_ms':str(BASE+1100),'signal_state':'green'},
                                    {'signal_group_id':'E','start_timestamp_ms':str(BASE+1100),'end_timestamp_ms':str(BASE+5000),'signal_state':'red'},
                                    {'signal_group_id':'P','start_timestamp_ms':str(BASE),'end_timestamp_ms':str(BASE+5000),'signal_state':'red-yellow'}])
        nominal=contexts(ledger,mapping,index,0);shifted=contexts(ledger,mapping,index,.2)
        self.assertEqual(nominal[0]['signal_state'],'green');self.assertEqual(shifted[0]['signal_state'],'red')
        self.assertEqual(shifted[0]['pet_s'],nominal[0]['pet_s']);self.assertEqual(ledger,original)
        self.assertTrue(all(row['pet_values_unchanged'] and not row['signal_clock_shifted'] for row in sensitivity_rows(ledger,mapping,index)))

    def test_all_four_absolute_event_times_share_offset(self):
        from spl.pet_signals import aligned_event_times
        from spl.pet_contracts import TIMES
        ledger,_,_,_=build_ledger(fixture(),PROFILE);event=ledger[0]
        shifted=aligned_event_times(event,.2)
        self.assertTrue(all(shifted[field]-event[field]==200 for field in TIMES))
        self.assertEqual(shifted['ts_enter_priority_ms']-shifted['ts_leave_encroaching_ms'],event['ts_enter_priority_ms']-event['ts_leave_encroaching_ms'])

    def test_invalid_state_interval_blocks_false_confirmation(self):
        base={'signal_group_id':'E','start_timestamp_ms':'0','end_timestamp_ms':'100','signal_state':'green'}
        index=SignalIndex.from_rows([base,dict(base,start_timestamp_ms='20',end_timestamp_ms='30',signal_state='INVALID')])
        self.assertEqual(index.lookup('E',25)['state'],'SIGNAL_AMBIGUOUS')
        self.assertEqual(index.lookup('E',50)['state'],'green')
        only=SignalIndex.from_rows([dict(base,signal_state='INVALID')])
        self.assertEqual(only.lookup('E',25)['state'],'UNKNOWN')
        self.assertEqual(only.lookup('E',25)['reason'],'INVALID_SIGNAL_STATE')

    def test_unresolved_timestamp_blocks_group_and_preserves_pet(self):
        ledger,qc,mapping,scenarios=build_ledger(fixture(),PROFILE)
        index=signals();index=SignalIndex.from_rows([{'signal_group_id':'E','start_timestamp_ms':str(BASE),'end_timestamp_ms':str(BASE+5000),'signal_state':'green'},
                                                  {'signal_group_id':'E','start_timestamp_ms':'UNKNOWN','end_timestamp_ms':str(BASE+5000),'signal_state':'red'},
                                                  {'signal_group_id':'P','start_timestamp_ms':str(BASE),'end_timestamp_ms':str(BASE+5000),'signal_state':'green'}])
        context=contexts(ledger,mapping,index)
        self.assertEqual(context[0]['signal_state'],'UNKNOWN')
        rows=summarize(ledger,context,scenarios)
        self.assertEqual(qc['n_valid_nonnegative_pet'],1)
        self.assertIsNone(rows['pet_thresholds'][0]['short_pet_fraction_signal_known'])

class PETArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base=ROOT/'worker_evidence'/('pet_unit_fixtures_'+uuid.uuid4().hex[:12]);cls.base.mkdir(parents=True,exist_ok=False)

    def archive(self,extra=None):
        path=self.base/(uuid.uuid4().hex+'.zip');data=fixture();data['signal_states']=[{'signal_group_id':'E','start_timestamp_ms':str(BASE),'end_timestamp_ms':str(BASE+5000),'signal_state':'red'}]
        with zipfile.ZipFile(path,'x') as archive:
            for table,member in TABLE_MEMBERS.items():
                stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=list(data[table][0]),lineterminator='\n');writer.writeheader();writer.writerows(data[table])
                raw=stream.getvalue().encode();archive.writestr(member,gzip.compress(raw,mtime=0) if member.endswith('.gz') else raw)
            archive.writestr('00_do_not_execute.py',b'raise RuntimeError("MUST_NOT_EXECUTE")')
            if extra:
                info=zipfile.ZipInfo('placeholder');info.filename=extra
                archive.writestr(info,b'UNIT_TEST_ONLY')
        return path

    def test_selected_small_tables_no_scripts_or_tracks(self):
        path=self.archive();profile={'inputs':[{'archive':path.relative_to(ROOT).as_posix(),'sha256':digest(path.read_bytes())}]}
        tables,index,inventory=load_tables(ROOT,profile)
        self.assertEqual(len(tables['pet_events']),1);self.assertFalse(inventory['tracks_parsed'])
        self.assertEqual(len(inventory['tables']),5);self.assertEqual(index.lookup('E',BASE+1)['state'],'red')

    def test_bad_zip_paths_and_hash(self):
        for bad in ('../outside','a\\outside','C:/outside'):
            path=self.archive(bad);profile={'inputs':[{'archive':path.relative_to(ROOT).as_posix(),'sha256':digest(path.read_bytes())}]}
            with self.subTest(path=bad),self.assertRaises(ContractError):load_tables(ROOT,profile)
        path=self.archive();profile={'inputs':[{'archive':path.relative_to(ROOT).as_posix(),'sha256':'bad'}]}
        with self.assertRaisesRegex(ContractError,'HASH_MISMATCH'):load_tables(ROOT,profile)

    def test_stream_expansion_limit(self):
        stream=LimitedReader(io.BytesIO(b'0123456789'),5)
        with self.assertRaisesRegex(ContractError,'EXPANSION_LIMIT'):stream.read(10)

    def test_csv_parquet_ledger_roundtrip(self):
        from spl.pet_artifacts import write_table
        import pyarrow.parquet as pq
        directory=self.base/uuid.uuid4().hex;directory.mkdir(exist_ok=False)
        ledger,_,_,_=build_ledger(fixture(),PROFILE)
        write_table(directory,'synthetic_ledger',ledger)
        rows=pq.read_table(directory/'synthetic_ledger.parquet').to_pylist()
        self.assertEqual(rows[0]['pet_s'],.5)
        self.assertTrue(rows[0]['valid_nonnegative_pet'])
        self.assertEqual(json.loads(rows[0]['provider_fields'])['pet_s'],'0.5')
        self.assertNotIn(b'\r\n',(directory/'synthetic_ledger.csv').read_bytes())


if __name__=='__main__':unittest.main()

"""Synthetic R2 boundary and no-fit diagnostics; no provider baseline execution."""
import csv
from collections import Counter
from copy import deepcopy
import hashlib
from pathlib import Path
import sqlite3
import unittest
import uuid
from zoneinfo import ZoneInfo

from spl.pet_signals import SignalIndex
from spl.pet_contracts import TIMES
from spl.r2_signals import (analyze_signals,aggregate_tv,coverage_for_day,day_bounds,
                            shift_event_times,ReadOnlySignals)

ROOT=Path(__file__).resolve().parents[1]
ZONE=ZoneInfo('Europe/Vienna')
QC={'n_invalid':0,'n_invalid_state_rows_retained_as_unknown':0,'n_conservatively_blocked_groups':0}
MAP={'S':{'encroaching_signal_group_id':'E','priority_signal_group_id':'P'}}


def event(key,encroaching,priority,pet=None):
    value=(priority-encroaching-100)/1000 if pet is None else pet
    return {'event_key':str(key),'scenario_id':'S','pet_s':value,
            'qc_status':'VALID_NONNEGATIVE_PET' if value>=0 else 'NEGATIVE_PET_REVIEW',
            'valid_nonnegative_pet':value>=0,'ts_enter_encroaching_ms':encroaching,
            'ts_leave_encroaching_ms':encroaching+100,'ts_enter_priority_ms':priority,
            'ts_leave_priority_ms':priority+100}


def index(rows):
    return SignalIndex.from_rows([dict(signal_group_id=g,start_timestamp_ms=s,end_timestamp_ms=e,signal_state=st)
                                  for g,s,e,st in rows])


class R2SignalTests(unittest.TestCase):
    def fixture(self,events,signals):
        directory=ROOT/'worker_evidence'/('r2_signals_fixture_'+uuid.uuid4().hex)
        directory.mkdir(parents=True,exist_ok=False);cache=directory/'signals.sqlite';ledger=directory/'ledger.csv'
        db=sqlite3.connect(cache)
        db.executescript('CREATE TABLE signals(g TEXT,s INTEGER,e INTEGER,state TEXT,si INTEGER,rn INTEGER); CREATE INDEX signal_starts ON signals(s);')
        db.executemany('INSERT INTO signals VALUES(?,?,?,?,?,?)',[(g,s,e,st,0,i+1) for i,(g,s,e,st) in enumerate(signals)])
        db.commit();db.close()
        with ledger.open('x',encoding='utf-8',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(events[0]));writer.writeheader();writer.writerows(events)
        return ledger,cache

    def analyze(self,events,signals,**kw):
        ledger,cache=self.fixture(events,signals)
        return analyze_signals(ledger,cache,MAP,source_signal_qc=kw.pop('source_signal_qc',QC),**kw)

    def test_daily_partition_half_open_gap_overlap_unknown_no_ffill(self):
        lo,hi=day_bounds('2025-06-05',ZONE)
        idx=index([('E',lo,lo+1000,'green'),('E',lo+1000,lo+2000,'red'),
                   ('E',lo+1500,lo+2500,'yellow'),('E',lo+4000,lo+5000,'unknown')])
        self.assertEqual(idx.lookup('E',lo+1000)['state'],'red')
        self.assertEqual(idx.lookup('E',lo+2000)['state'],'yellow')
        self.assertEqual(idx.lookup('E',lo+2500)['reason'],'SIGNAL_GAP')
        row=coverage_for_day(idx,'E','2025-06-05',ZONE)
        self.assertEqual(row['green_covered_seconds'],1)
        self.assertEqual(row['red_covered_seconds'],.5)
        self.assertEqual(row['yellow_covered_seconds'],.5)
        self.assertEqual(row['overlap_seconds'],.5)
        self.assertEqual(row['observed_unknown_seconds'],1)
        self.assertEqual(row['covered_union_seconds'],3.5)
        self.assertEqual(row['gap_count'],2)
        self.assertEqual(row['gap_seconds'],86400-3.5)
        self.assertEqual(row['longest_gap_seconds'],86400-5)
        self.assertEqual(row['effective_known_seconds']+row['effective_unknown_or_ambiguous_seconds'],86400)
        self.assertFalse(row['ffill_performed'])

    def test_dst_days_have_23_and_25_hours(self):
        for day,hours in [('2025-03-30',23),('2025-10-26',25)]:
            with self.subTest(day=day):
                lo,hi=day_bounds(day,ZONE);row=coverage_for_day(index([('E',lo,hi,'red')]),'E',day,ZONE)
                self.assertEqual(row['day_seconds'],hours*3600)
                self.assertEqual(row['red_covered_seconds'],hours*3600)
                self.assertEqual(row['gap_seconds'],0)

    def test_equal_distribution_swap_has_tv_zero_but_all_events_change(self):
        lo,hi=day_bounds('2025-06-05',ZONE)
        result=self.analyze([event('a',lo+500,lo+1000),event('b',lo+1500,lo+2000)],
            [('E',lo,lo+1000,'green'),('E',lo+1000,lo+2000,'red'),('E',lo+2000,lo+4000,'green'),('P',lo,hi,'green')],offsets_s=[0,1])
        row=next(r for r in result['tables']['signal_pair_tv'] if r['population']=='VALID_NONNEGATIVE_PET' and r['scenario_id']=='S' and r['clock_offset_s']==1)
        self.assertEqual(row['aggregate_role_pair_tv'],0)
        self.assertEqual(row['same_event_pair_state_change_rate'],1)
        changes=next(r for r in result['tables']['signal_event_changes'] if r['population']=='SHORT_NONNEGATIVE_PET_LE_1S' and r['scenario_id']=='S' and r['clock_offset_s']==1)
        self.assertEqual(changes['n_encroaching_state_changed'],2)
        self.assertEqual(changes['n_priority_state_changed'],0)
        transitions=[r for r in result['tables']['signal_state_transitions'] if r['population']=='VALID_NONNEGATIVE_PET' and r['scenario_id']=='S' and r['clock_offset_s']==1 and r['role']=='encroaching']
        self.assertEqual({(r['from_state'],r['to_state'],r['n_events']) for r in transitions},{('green','red',1),('red','green',1)})

    def test_four_time_shift_preserves_pet_and_input(self):
        original=event('a',1000,1500);before=deepcopy(original)
        for delta in [-2,-1,-.5,-.2,0,.2,.5,1,2]:
            times=shift_event_times(original,delta)
            self.assertEqual(times[TIMES[2]]-times[TIMES[1]],original[TIMES[2]]-original[TIMES[1]])
            self.assertEqual((times[TIMES[2]]-times[TIMES[1]])/1000,original['pet_s'])
        self.assertEqual(original,before)
        for value in [True,float('nan'),.0001]:
            with self.assertRaises(ValueError):shift_event_times(original,value)

    def test_cross_day_stress_includes_next_day_interval_and_source_is_read_only(self):
        lo,hi=day_bounds('2025-06-05',ZONE)
        ledger,cache=self.fixture([event('a',hi-500,hi+100)],
            [('E',lo,hi,'red'),('E',hi,hi+10000,'green'),('P',lo,hi+10000,'green')])
        before=hashlib.sha256(cache.read_bytes()).hexdigest()
        result=analyze_signals(ledger,cache,MAP,offsets_s=[0,2],source_signal_qc=QC)
        row=next(r for r in result['tables']['signal_event_changes'] if r['population']=='ALL_LEDGER_EVENTS' and r['scenario_id']=='S' and r['clock_offset_s']==2)
        self.assertEqual(row['n_encroaching_state_changed'],1)
        self.assertEqual(result['summary']['n_calendar_days'],2)
        self.assertEqual(hashlib.sha256(cache.read_bytes()).hexdigest(),before)
        reader=ReadOnlySignals(cache,['E'],ZONE,QC);self.addCleanup(reader.close)
        with self.assertRaises(sqlite3.OperationalError):reader.db.execute('DELETE FROM signals')

    def test_negative_population_distinction_zero_and_empty_denominator(self):
        lo,hi=day_bounds('2025-06-05',ZONE)
        result=self.analyze([event('zero',lo+100,lo+200),event('negative',lo+400,lo+450)],
            [('E',lo,hi,'red'),('P',lo,hi,'green')],offsets_s=[0])
        totals={r['population']:r['n_events'] for r in result['tables']['signal_event_changes'] if r['scenario_id']=='ALL_SCENARIOS'}
        self.assertEqual(totals,{'ALL_LEDGER_EVENTS':2,'VALID_NONNEGATIVE_PET':1,'SHORT_NONNEGATIVE_PET_LE_1S':1})
        cells=result['tables']['signal_condition_pet']
        zero=next(r for r in cells if r['encroaching_entry_signal']=='green' and r['priority_entry_signal']=='green')
        self.assertEqual(zero['n_valid_nonnegative_pet'],0);self.assertIsNone(zero['short_pet_fraction'])
        actual=next(r for r in cells if r['encroaching_entry_signal']=='red' and r['priority_entry_signal']=='green' and r['tau_s']==.5)
        self.assertEqual(actual['n_short_pet'],1);self.assertEqual(actual['short_pet_fraction'],1)
        self.assertEqual(actual['candidate_period_n_valid_nonnegative_pet'],1)

    def test_no_short_population_is_explicit_null(self):
        lo,hi=day_bounds('2025-06-05',ZONE)
        result=self.analyze([event('large',lo+100,lo+3000)], [('E',lo,hi,'red'),('P',lo,hi,'green')],offsets_s=[0])
        row=next(r for r in result['tables']['signal_pair_tv'] if r['population']=='SHORT_NONNEGATIVE_PET_LE_1S' and r['scenario_id']=='S')
        self.assertEqual(row['n_events'],0);self.assertIsNone(row['aggregate_role_pair_tv']);self.assertIsNone(row['same_event_pair_state_change_rate'])

    def test_long_interval_candidate_period_exact_edges_and_zero_event_days_retained(self):
        lo=day_bounds('2025-06-05',ZONE)[0];hi=day_bounds('2025-06-11',ZONE)[1]
        result=self.analyze([event('first',lo+100,lo+500),event('last',hi-1000,hi-500)],
            [('E',lo,hi,'red'),('P',lo,hi,'green')],offsets_s=[0])
        self.assertEqual(result['summary']['n_calendar_days'],7)
        rows=result['tables']['signal_coverage_daily']
        self.assertEqual(len(rows),14)
        quiet=next(r for r in rows if r['signal_group_id']=='E' and r['local_date']=='2025-06-08')
        self.assertEqual(quiet['n_role_entry_queries'],0);self.assertEqual(quiet['red_covered_seconds'],86400)
        self.assertEqual(quiet['effective_unknown_or_ambiguous_seconds'],0)
        self.assertTrue(quiet['withheld_signal_conclusion'])
        self.assertEqual(quiet['source_quality_status'],'SOURCE_QUALITY_UNRESOLVED_CANDIDATE_PERIOD')
        mixed=next(r for r in result['tables']['signal_condition_pet'] if r['encroaching_entry_signal']=='red' and r['priority_entry_signal']=='green')
        self.assertEqual(mixed['n_valid_nonnegative_pet'],2);self.assertEqual(mixed['candidate_period_n_valid_nonnegative_pet'],2)
        self.assertTrue(mixed['signal_condition_resolved']);self.assertTrue(mixed['withheld_signal_conclusion'])
        self.assertEqual(mixed['signal_conclusion_status'],'MIXED_WITH_SOURCE_QUALITY_UNRESOLVED_PERIOD')
        crossings=result['tables']['signal_missing_period_crossings']
        self.assertEqual(len(crossings),2);self.assertTrue(all(r['covers_entire_candidate_period'] for r in crossings))
        self.assertTrue(all(r['claim_status']=='USER_REQUEST_UNVERIFIED' for r in crossings))

    def test_candidate_period_uses_either_role_entry_not_only_event_anchor(self):
        lo,hi=day_bounds('2025-06-05',ZONE)
        result=self.analyze([event('cross',lo-500,lo+100)], [('E',lo-10000,hi,'red'),('P',lo-10000,hi,'green')],offsets_s=[0])
        cell=next(r for r in result['tables']['signal_condition_pet'] if r['encroaching_entry_signal']=='red' and r['priority_entry_signal']=='green')
        self.assertEqual(cell['n_valid_nonnegative_pet'],1);self.assertEqual(cell['candidate_period_n_valid_nonnegative_pet'],1)
        self.assertTrue(cell['withheld_signal_conclusion'])
        self.assertEqual(cell['candidate_period_basis'],'ANY_OWN_ROLE_ENTRY_LOCAL_DATE')

    def test_unlocatable_source_rows_conservatively_block_and_ambiguity_retained(self):
        lo,hi=day_bounds('2025-06-05',ZONE)
        result=self.analyze([event('a',lo+100,lo+500)],[('E',lo,hi,'red'),('P',lo,hi,'green')],
            source_signal_qc={'n_invalid':1},offsets_s=[0])
        self.assertTrue(result['summary']['conservative_all_group_block'])
        self.assertTrue(all(r['effective_known_seconds']==0 for r in result['tables']['signal_coverage_daily']))
        unresolved=next(r for r in result['tables']['signal_condition_pet'] if r['encroaching_entry_signal']=='UNKNOWN' and r['priority_entry_signal']=='UNKNOWN')
        self.assertEqual(unresolved['n_valid_nonnegative_pet'],1);self.assertTrue(unresolved['withheld_signal_conclusion'])
        overlap=self.analyze([event('a',lo+100,lo+500)], [('E',lo,hi,'red'),('E',lo,hi,'green'),('P',lo,hi,'green')],offsets_s=[0])
        c=next(r for r in overlap['tables']['signal_condition_pet'] if r['encroaching_entry_signal']=='SIGNAL_AMBIGUOUS' and r['priority_entry_signal']=='green')
        self.assertEqual(c['n_valid_nonnegative_pet'],1);self.assertTrue(c['withheld_signal_conclusion'])

    def test_duplicate_keys_and_missing_source_qc_fail_without_empty_proof(self):
        lo,hi=day_bounds('2025-06-05',ZONE);row=event('a',lo+100,lo+500)
        with self.assertRaises(ValueError):self.analyze([row,row],[('E',lo,hi,'red')],offsets_s=[0])
        ledger,cache=self.fixture([row],[('E',lo,hi,'red')])
        with self.assertRaises(ValueError):analyze_signals(ledger,cache,MAP,offsets_s=[0])
        self.assertIsNone(aggregate_tv(Counter(),Counter(),0))
        self.assertEqual(aggregate_tv({'red':1},{'green':1},1),1)
        with self.assertRaises(ValueError):aggregate_tv({'red':1},{'green':2},1)


if __name__=='__main__':unittest.main()

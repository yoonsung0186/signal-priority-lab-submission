"""Disk-backed provider input reconciliation; never partitions duplicate keys.

The cache is a disposable derived index, not source evidence. Every new analysis
rebuilds it from checksummed archives. All object variants are fetched by ID,
irrespective of object_day; signal windows include every overlapping interval.
"""
import hashlib,json,sqlite3
from collections import Counter
from decimal import Decimal,InvalidOperation,ROUND_CEILING
from pathlib import Path
from .pet_io import load_tables
from .pet_contracts import integer,timestamp_integer,key_value,TIMES
from .pet_qc import event_time
from .pet_signals import SignalIndex,STATES


class BatchStore:
    def __init__(self,path,clock_offsets_s=None):
        self.signal_window_padding_ms=offset_padding_ms(clock_offsets_s)
        self.path=Path(path)
        if self.path.exists():raise FileExistsError('Batch cache exists; use a fresh path')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path)
        self.db.executescript('''
          PRAGMA journal_mode=OFF; PRAGMA temp_store=FILE; PRAGMA cache_size=-32768;
          CREATE TABLE events(rid INTEGER PRIMARY KEY,k TEXT,day TEXT,payload TEXT,sig TEXT);
          CREATE TABLE objects(k TEXT,payload TEXT,sig TEXT);
          CREATE TABLE signals(g TEXT,s INTEGER,e INTEGER,state TEXT,si INTEGER,rn INTEGER);
        ''')
        self.small={'scenarios':[],'scenario_signal_mapping':[]}
        self.counts=Counter();self.bad_signals=[];self.max_duration=0

    def sink(self,table,row):
        self.counts[table]+=1
        if table in self.small:self.small[table].append(row);return
        origin=row['__source']
        if table=='signal_states':
            start=timestamp_integer(row.get('start_timestamp_ms'));end=timestamp_integer(row.get('end_timestamp_ms'));group=row.get('signal_group_id')
            if not group or start is None or end is None or start>=end:
                self.bad_signals.append(row)
                if len(self.bad_signals)>100000:raise ValueError('Too many invalid signal rows for bounded processing')
            else:
                self.max_duration=max(self.max_duration,end-start)
                self.db.execute('INSERT INTO signals VALUES(?,?,?,?,?,?)',(group,start,end,row.get('signal_state'),origin['source_index'],origin['source_row_id']))
        else:
            raw={k:v for k,v in row.items() if not k.startswith('__')}
            payload=json.dumps(row,sort_keys=True,separators=(',',':'))
            sig=hashlib.sha256(json.dumps(raw,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if table=='objects':self.db.execute('INSERT INTO objects VALUES(?,?,?)',(key_value(row.get('object_id'),True),payload,sig))
            elif table=='pet_events':
                key=key_value(row.get('event_id'),True)
                if key is None:key='INVALID:'+str(origin['source_index'])+':'+str(origin['source_row_id'])
                first=min((n for f in TIMES if (n:=integer(row.get(f))) is not None),default=None)
                _,date,_=event_time(first)
                self.db.execute('INSERT INTO events(k,day,payload,sig) VALUES(?,?,?,?)',(key,date or '',payload,sig))
        if sum(self.counts.values())%50000==0:self.db.commit()

    def finish(self,inventory):
        self.inventory=inventory;self.db.commit()
        if any(not self.counts[t] for t in ('pet_events','objects','scenarios')):raise ValueError('Missing required provider table')
        self.db.executescript('''
          CREATE INDEX event_keys ON events(k); CREATE INDEX object_keys ON objects(k);
          CREATE INDEX signal_starts ON signals(s);
          CREATE TABLE buckets AS SELECT k,MIN(day) AS day FROM events GROUP BY k;
          CREATE INDEX bucket_days ON buckets(day,k);
          CREATE TEMP TABLE wanted(k TEXT PRIMARY KEY);
        ''')
        self.global_qc={'row_counts':dict(self.counts),'object_rows':self.key_qc('objects'),
                        'event_rows':self.key_qc('events'),'signal_rows':self.signal_qc(),
                        'batch_basis':'Earliest UTC date across all variants of each event key; invalid timestamps retained',
                        'object_lookup':'Global ID with all variants, never filtered by object_day'}
        self.db.commit()

    def key_qc(self,table):
        invalid="k IS NULL" if table=='objects' else "k LIKE 'INVALID:%'"
        total=self.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
        missing=self.db.execute(f'SELECT count(*) FROM {table} WHERE {invalid}').fetchone()[0]
        keys=self.db.execute(f'SELECT count(DISTINCT k) FROM {table} WHERE NOT ({invalid})').fetchone()[0]
        variants=self.db.execute(f'SELECT count(*) FROM (SELECT k,sig FROM {table} WHERE NOT ({invalid}) GROUP BY k,sig)').fetchone()[0]
        return dict(n_raw=total,n_unique_keys=keys,n_missing_key_rows=missing,
                    n_identical_duplicate_rows_merged=total-missing-variants,n_conflicting_duplicate_rows_extra=variants-keys)

    def signal_qc(self):
        bad=SignalIndex()
        for row in self.bad_signals:bad.add(row,row['__source']['source_index'],row['__source']['source_row_id'])
        bad.finalize();qc=Counter(bad.qc)
        # Group/state normalization exactly matches SignalIndex.add/finalize.
        # Materialize once on disk: repeated GROUP BY over 11M intervals would
        # otherwise rerun for each QC count. This changes no interval semantics.
        self.db.execute("CREATE TEMP TABLE unique_signals AS SELECT g,s,e,CASE WHEN state IN ('green','yellow','red','red-yellow','unknown') THEN state ELSE 'unknown' END AS st,CASE WHEN state IN ('green','yellow','red','red-yellow','unknown') THEN 0 ELSE 1 END AS q FROM signals GROUP BY g,s,e,st,q")
        raw=self.db.execute('SELECT count(*) FROM signals').fetchone()[0]
        unique=self.db.execute('SELECT count(*) FROM unique_signals').fetchone()[0]
        invalid=self.db.execute("SELECT count(*) FROM signals WHERE state NOT IN ('green','yellow','red','red-yellow','unknown') OR state IS NULL").fetchone()[0]
        qc['n_raw']+=raw;qc['n_invalid']+=invalid;qc['n_invalid_state_rows_retained_as_unknown']+=invalid
        qc['n_identical_duplicates_merged']=raw-unique
        qc['n_valid_unique_intervals']=self.db.execute('SELECT count(*) FROM unique_signals WHERE q=0').fetchone()[0]
        qc['n_retained_unknown_state_intervals']=unique-qc['n_valid_unique_intervals']
        qc['n_overlapping_intervals']=self.db.execute('SELECT count(*) FROM (SELECT s,MAX(e) OVER(PARTITION BY g ORDER BY s,e,st,q ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev FROM unique_signals) WHERE s<prev').fetchone()[0]
        return dict(qc)

    def batches(self,clock_offsets_s=None):
        padding=self.signal_window_padding_ms if clock_offsets_s is None else offset_padding_ms(clock_offsets_s)
        dates=[r[0] for r in self.db.execute('SELECT DISTINCT day FROM buckets ORDER BY day')]
        for date in dates:
            events=[json.loads(r[0]) for r in self.db.execute('SELECT e.payload FROM events e JOIN buckets b ON e.k=b.k WHERE b.day=? ORDER BY e.rid',(date,))]
            if len(events)>100000:raise ValueError('Date batch exceeds 100000 rows; preserve input and use smaller key batches')
            ids={key_value(row.get(role+'_object_id'),True) for row in events for role in ('encroaching','priority')};ids.discard(None)
            self.db.execute('DELETE FROM wanted') # Only ephemeral SQL rows; no user file is deleted.
            self.db.executemany('INSERT INTO wanted VALUES(?)',((key,) for key in sorted(ids)))
            objects=[json.loads(r[0]) for r in self.db.execute('SELECT o.payload FROM objects o JOIN wanted w ON o.k=w.k ORDER BY o.rowid')]
            times=[n for row in events for f in TIMES if (n:=integer(row.get(f))) is not None]
            signals=SignalIndex()
            for row in self.bad_signals:signals.add(row,row['__source']['source_index'],row['__source']['source_row_id'])
            if times:
                lo=min(times)-padding;hi=max(times)+padding
                for group,start,end,state,si,rn in self.db.execute('SELECT g,s,e,state,si,rn FROM signals WHERE s>=? AND s<=? AND e>? ORDER BY rowid',(lo-self.max_duration,hi,lo)):
                    signals.add(dict(signal_group_id=group,start_timestamp_ms=str(start),end_timestamp_ms=str(end),signal_state=state),si,rn)
            signals.finalize()
            yield {**self.small,'pet_events':events,'objects':objects},signals

    def close(self):self.db.close()


def prepare(root,profile,cache_path):
    store=BatchStore(cache_path,profile.get('clock_offsets_s'))
    try:
        _,_,inventory=load_tables(root,profile,sink=store.sink)
        store.finish(inventory)
        return store
    except BaseException:
        store.close();raise


def offset_padding_ms(offsets=None):
    """Outward query padding; old callers retain their 500 ms window."""
    values=(-.5,.5) if offsets is None else offsets
    maximum=Decimal(0)
    for value in values:
        if isinstance(value,bool):raise ValueError('Clock offsets must be finite numbers')
        try:number=Decimal(str(value))
        except (InvalidOperation,ValueError):raise ValueError('Clock offsets must be finite numbers') from None
        if not number.is_finite():raise ValueError('Clock offsets must be finite numbers')
        maximum=max(maximum,abs(number))
    padding=int((maximum*1000).to_integral_value(rounding=ROUND_CEILING))
    if padding>=2**63:raise ValueError('Clock offset exceeds supported timestamp range')
    return padding

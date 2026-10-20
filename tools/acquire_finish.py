"""One bounded provider request per invocation; never sleeps or launches agents."""
import argparse,email.utils,hashlib,json,time,urllib.error,urllib.request
from datetime import datetime,timezone,timedelta
from pathlib import Path
from acquire_sitpet_r1 import approved,ProviderRedirect,inspect_zip,TOTAL_LIMIT,FILE_LIMIT
from snapshot import ROOT,digest
from preflight import safe_child,verify_root
from final_verify import encode

MISSION='state/finish_20261013'
NAMES=('pet_events.zip','objects.zip','signals.zip','auxiliary.zip')

def retry_time(raw,now,index):
    if raw is not None:
        try:seconds=max(0,int(raw));return now+timedelta(seconds=seconds),'RETRY_AFTER_SECONDS'
        except ValueError:
            try:
                date=email.utils.parsedate_to_datetime(raw)
                if date.tzinfo is None:date=date.replace(tzinfo=timezone.utc)
                return max(now,date),'RETRY_AFTER_HTTP_DATE'
            except (ValueError,TypeError,OverflowError):pass
    # Fixed positive jitter makes the published schedule reproducible, not earlier.
    return now+timedelta(seconds=(300,900,1800)[min(index,2)]+7),'CONSERVATIVE_BACKOFF_PLUS_7S'

def records(root):
    base=safe_child(root,MISSION+'/acquisition')
    if not base.exists():return []
    completed={p.stem:json.loads(p.read_bytes()) for p in sorted(base.glob('attempt_[0-9][0-9].json'))}
    for p in sorted(base.glob('request_[0-9][0-9].json')):
        key=p.stem.replace('request_','attempt_')
        if key not in completed:completed[key]=json.loads(p.read_bytes())
    return [completed[key] for key in sorted(completed)]

def cached_metadata(root):
    path=safe_child(root,'data/raw/sitpet_r1_20261012T0850/metadata.json')
    prior=json.loads((path.parent/'acquisition.json').read_bytes())
    expected=next(x for x in prior['files'] if x['name']=='metadata.json')
    if expected['status']!='VERIFIED_TRANSFER' or digest(path.read_bytes())!=expected['sha256']:raise ValueError('Unverified cached metadata')
    meta=json.loads(path.read_bytes())
    if meta['id']!=19231310 or meta['metadata']['version']!='1.1' or meta['metadata']['license']['id']!='cc-by-4.0':raise ValueError('Pinned metadata mismatch')
    files={f['key']:f for f in meta['files']};license_bytes=safe_child(root,'state/research/sitpet/LICENSE.md').read_bytes()
    if 'md5:'+hashlib.md5(license_bytes).hexdigest()!=files['LICENSE.md']['checksum']:raise ValueError('Cached license mismatch')
    rights=json.loads(safe_child(root,'state/data_acquisition_manifest.json').read_bytes())
    if digest(license_bytes)!=rights['license_sha256'] or any(rights['rights'][k]!='ALLOW' for k in ('access_status','analysis_status')):raise ValueError('Rights not verified')
    return files

def prior_spent(root):
    total=0
    for directory in safe_child(root,'data/raw').glob('sitpet_r1_*'):
        safe_child(root,directory.relative_to(root).as_posix())
        record=directory/'acquisition.json';reported=json.loads(record.read_bytes()).get('received_bytes',0) if record.exists() else 0
        physical=sum(p.stat().st_size for p in directory.iterdir() if p.name in (*NAMES,'figures.zip','metadata.json','LICENSE.md') and p.is_file())
        total+=max(reported,physical)
    return total

def main():
    p=argparse.ArgumentParser();p.add_argument('--execute',action='store_true');args=p.parse_args();verify_root(ROOT)
    files=cached_metadata(ROOT);history=records(ROOT);now=datetime.now(timezone.utc)
    if any(r['status']=='STARTED' for r in history):print(json.dumps({'status':'INTERRUPTED_REQUEST_REQUIRES_RECONCILIATION','network_request_performed':False}));return 20
    verified={r['name'] for r in history if r['status']=='VERIFIED_ARCHIVE'};pending=[n for n in NAMES if n not in verified]
    base=safe_child(ROOT,MISSION+'/acquisition');base.mkdir(parents=True,exist_ok=True)
    if not pending:print(json.dumps({'status':'ACQUIRED_REQUIRED_ARCHIVES'}));return 0
    if len(history)>=4:print(json.dumps({'status':'ATTEMPT_BUDGET_EXHAUSTED','attempts':len(history)}));return 20
    if history and history[-1].get('next_allowed_utc'):
        next_time=datetime.fromisoformat(history[-1]['next_allowed_utc'])
        if now<next_time:print(json.dumps({'status':'WAIT_EXTERNAL','next_allowed_utc':next_time.isoformat()}));return 20
    waited=sum(r.get('wait_charged_seconds',0) for r in history)
    wait_charge=0
    if history and history[-1].get('next_allowed_utc'):
        wait_charge=max(0,(datetime.fromisoformat(history[-1]['next_allowed_utc'])-datetime.fromisoformat(history[-1]['observed_at_utc'])).total_seconds())
    if waited+wait_charge>1800:print(json.dumps({'status':'WAIT_BUDGET_EXHAUSTED','required_total_wait_seconds':waited+wait_charge}));return 20
    spent=prior_spent(ROOT)+sum(r.get('received_bytes',0) for r in history);name=pending[0];expected=files[name]
    if expected['size']>FILE_LIMIT or spent+expected['size']>TOTAL_LIMIT:print(json.dumps({'status':'BYTE_BUDGET_EXHAUSTED'}));return 20
    url=expected['links']['self'];approved(url)
    if url!='https://zenodo.org/api/records/19231310/files/'+name+'/content':raise ValueError('Pinned file URL mismatch')
    if not args.execute:print(json.dumps({'status':'REQUEST_READY','name':name,'url':url,'prior_received_bytes':spent,'attempt':len(history)+1}));return 0
    index=len(history)+1;out=safe_child(ROOT,f'data/raw/sitpet_finish_20261013/attempt_{index:02d}');out.mkdir(parents=True,exist_ok=False)
    record={'attempt':index,'name':name,'url':url,'expected_bytes':expected['size'],'prior_received_bytes':spent,'received_bytes':0,'wait_charged_seconds':wait_charge,'status':'STARTED','started_at_utc':now.isoformat(),'retry_after_raw':None,'next_allowed_utc':None}
    with (base/f'request_{index:02d}.json').open('xb') as f:f.write(encode(record))
    sha=hashlib.sha256();md5=hashlib.md5();started=time.monotonic()
    try:
        opener=urllib.request.build_opener(ProviderRedirect())
        with opener.open(url,timeout=45) as response,(out/name).open('xb') as stream:
            approved(response.url);record['http_status']=response.status
            while record['received_bytes']<expected['size']:
                block=response.read(min(1024**2,expected['size']-record['received_bytes']))
                if not block:break
                record['received_bytes']+=len(block);stream.write(block);sha.update(block);md5.update(block)
        if record['received_bytes']!=expected['size'] or 'md5:'+md5.hexdigest()!=expected['checksum']:raise ValueError('Provider checksum/size mismatch')
        members,expansion=inspect_zip(out/name)
        record.update(status='VERIFIED_ARCHIVE',sha256=sha.hexdigest(),md5=md5.hexdigest(),path=(out/name).relative_to(ROOT).as_posix(),members=members,zip_expansion_bytes=expansion)
    except urllib.error.HTTPError as error:
        record.update(status='WAIT_EXTERNAL' if error.code in (429,503,504) else 'BLOCKED_HTTP',http_status=error.code,retry_after_raw=error.headers.get('Retry-After'),error_type=type(error).__name__)
    except Exception as error:record.update(status='FAILED_TRANSFER',error_type=type(error).__name__,reason=str(error))
    observed=datetime.now(timezone.utc);record['observed_at_utc']=observed.isoformat();record['elapsed_seconds']=time.monotonic()-started
    if record['status'] in ('WAIT_EXTERNAL','FAILED_TRANSFER'):
        when,basis=retry_time(record['retry_after_raw'],observed,sum(r['status']!='VERIFIED_ARCHIVE' for r in history));record.update(next_allowed_utc=when.isoformat(),retry_basis=basis)
    with (base/f'attempt_{index:02d}.json').open('xb') as f:f.write(encode(record))
    print(json.dumps(record));return 0 if record['status']=='VERIFIED_ARCHIVE' else 20

if __name__=='__main__':raise SystemExit(main())

"""Bounded, append-only acquisition of the R1 pinned provider archives."""
import argparse,hashlib,json,pathlib,stat,time,urllib.parse,urllib.request,zipfile
from datetime import datetime,timezone
from preflight import verify_root,safe_child
from snapshot import ROOT,digest
from final_verify import identifier,encode

FILES=('pet_events.zip','objects.zip','signals.zip','auxiliary.zip','figures.zip')
TOTAL_LIMIT=180*1024**2
FILE_LIMIT=100*1024**2
EXPANSION_LIMIT=2*1024**3

def approved(url):
    p=urllib.parse.urlsplit(url)
    if p.scheme!='https' or p.hostname!='zenodo.org' or p.username or p.password or p.port not in (None,443):
        raise ValueError('Unapproved provider URL')
    return url

class ProviderRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        approved(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def inspect_zip(path):
    rows=[];seen=set();total=0
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            original=info.orig_filename
            p=pathlib.PurePosixPath(original)
            if not original or '\x00' in original or p.is_absolute() or '..' in p.parts or '\\' in original or ':' in original or str(p)=='.':raise ValueError('Unsafe ZIP member')
            if str(p).casefold() in seen:raise ValueError('Duplicate ZIP member')
            if stat.S_ISLNK(info.external_attr>>16):raise ValueError('ZIP symlink')
            seen.add(str(p).casefold());total+=info.file_size
            if total>EXPANSION_LIMIT:raise ValueError('ZIP expansion limit')
            rows.append({'path':info.filename,'bytes':info.file_size})
    return rows,total

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--run-id',required=True);ap.add_argument('--reuse-metadata',help='Previously downloaded and hashed R1 metadata file');args=ap.parse_args()
    verify_root(ROOT);identifier(args.run_id)
    # Earlier failed attempts consume budget as well; no retry counter resets.
    spent=0
    for directory in safe_child(ROOT,'data/raw').glob('sitpet_r1_*'):
        safe_child(ROOT,directory.relative_to(ROOT).as_posix())
        physical=0
        for file in directory.iterdir():
            safe_child(ROOT,file.relative_to(ROOT).as_posix())
            if file.is_file() and file.name in (*FILES,'metadata.json','LICENSE.md'):physical+=file.stat().st_size
        prior=directory/'acquisition.json'
        received=json.loads(prior.read_bytes()).get('received_bytes',0) if prior.is_file() else 0
        spent+=max(physical,received)
    out=safe_child(ROOT,'data/raw/sitpet_r1_'+args.run_id)
    out.mkdir(parents=True,exist_ok=False)
    opener=urllib.request.build_opener(ProviderRedirect())
    ledger={'version':'1.1','doi':'10.5281/zenodo.19231310','prior_r1_received_bytes':spent,'received_bytes':0,'files':[],'errors':[],'raw_packaging':False}
    def fetch(name,url,limit,expected=None):
        approved(url);item={'name':name,'url':url,'received_bytes':0,'status':'STARTED'};ledger['files'].append(item)
        pinned='https://zenodo.org/api/records/19231310'+('/files/'+name+'/content' if name!='metadata.json' else '')
        if url!=pinned:raise ValueError('File URL does not match pinned record/name')
        if expected and (expected['size']>limit or spent+ledger['received_bytes']+expected['size']>TOTAL_LIMIT):raise ValueError('Acquisition budget')
        sha=hashlib.sha256();md5=hashlib.md5();start=time.monotonic()
        with opener.open(url,timeout=45) as response,(out/name).open('xb') as stream:
            approved(response.url);item['resolved_url']=response.url
            length=response.headers.get('Content-Length')
            if length and int(length)>limit:raise ValueError('Content length limit')
            if expected and length and int(length)!=expected['size']:raise ValueError('Provider content length mismatch')
            while True:
                if expected and item['received_bytes']==expected['size']:break
                remaining=min(limit-item['received_bytes'],TOTAL_LIMIT-spent-ledger['received_bytes'])
                if expected:remaining=min(remaining,expected['size']-item['received_bytes'])
                if remaining<=0:raise ValueError('Transfer budget reached')
                chunk=response.read(min(1024*1024,remaining))
                if not chunk:break
                item['received_bytes']+=len(chunk);ledger['received_bytes']+=len(chunk)
                if item['received_bytes']>limit or spent+ledger['received_bytes']>TOTAL_LIMIT:raise ValueError('Transfer budget')
                sha.update(chunk);md5.update(chunk);stream.write(chunk)
        item.update(sha256=sha.hexdigest(),md5=md5.hexdigest(),elapsed_s=time.monotonic()-start,retrieved_at_utc=datetime.now(timezone.utc).isoformat())
        if expected and (item['received_bytes']!=expected['size'] or 'md5:'+item['md5']!=expected['checksum']):raise ValueError('Provider checksum mismatch')
        item['status']='VERIFIED_TRANSFER';return item
    try:
        if args.reuse_metadata:
            cached=safe_child(ROOT,args.reuse_metadata)
            if cached.name!='metadata.json' or cached.parent.parent!=ROOT/'data/raw' or not cached.parent.name.startswith('sitpet_r1_'):raise ValueError('Invalid metadata reuse path')
            prior=json.loads(safe_child(ROOT,(cached.parent/'acquisition.json').relative_to(ROOT).as_posix()).read_bytes())
            evidence=next(x for x in prior['files'] if x['name']=='metadata.json')
            if evidence['status']!='VERIFIED_TRANSFER' or digest(cached.read_bytes())!=evidence['sha256']:raise ValueError('Unverified cached metadata')
            meta=json.loads(cached.read_bytes())
            ledger['files'].append({'name':'metadata.json','status':'REUSED_VERIFIED_METADATA','source_ref':args.reuse_metadata,'sha256':evidence['sha256'],'received_bytes':0})
        else:
            fetch('metadata.json','https://zenodo.org/api/records/19231310',2*1024**2)
            meta=json.loads((out/'metadata.json').read_bytes())
        if meta['id']!=19231310 or meta['metadata']['version']!='1.1' or meta['metadata']['license']['id']!='cc-by-4.0':raise ValueError('Pinned version or license mismatch')
        published={f['key']:f for f in meta['files']}
        if args.reuse_metadata:
            license_path=safe_child(ROOT,'state/research/sitpet/LICENSE.md');license_bytes=license_path.read_bytes()
            if len(license_bytes)!=published['LICENSE.md']['size'] or 'md5:'+hashlib.md5(license_bytes).hexdigest()!=published['LICENSE.md']['checksum']:raise ValueError('Cached license does not match fresh metadata')
            ledger['files'].append({'name':'LICENSE.md','status':'REUSED_CURRENT_MD5_VERIFIED_LICENSE','source_ref':'state/research/sitpet/LICENSE.md','sha256':digest(license_bytes),'received_bytes':0})
        else:
            fetch('LICENSE.md',published['LICENSE.md']['links']['self'],128*1024,published['LICENSE.md']);license_path=out/'LICENSE.md'
        old=json.loads((ROOT/'state/data_acquisition_manifest.json').read_bytes())
        if digest(license_path.read_bytes())!=old['license_sha256']:raise ValueError('License evidence changed; review required')
        if sum(published[n]['size'] for n in FILES)+spent+ledger['received_bytes']>TOTAL_LIMIT:raise ValueError('Aggregate transfer budget')
        ledger['rights']=old['rights'];ledger['license']='CC-BY-4.0';expanded=0
        for name in FILES:
            try:
                item=fetch(name,published[name]['links']['self'],FILE_LIMIT,published[name])
                entries,total=inspect_zip(out/name);expanded+=total
                if expanded>EXPANSION_LIMIT:raise ValueError('Aggregate ZIP expansion limit')
                item.update(entries=entries,zip_expansion_bytes=total,status='VERIFIED_ARCHIVE')
            except Exception as error:
                ledger['errors'].append({'file':name,'type':type(error).__name__,'reason':str(error)});break
        ledger['zip_expansion_bytes']=expanded
    except Exception as error:
        ledger['errors'].append({'type':type(error).__name__,'reason':str(error)})
    ledger['status']='COMPLETE' if not ledger['errors'] and len(ledger['files'])==7 else 'PARTIAL_OR_BLOCKED'
    with (out/'acquisition.json').open('xb') as stream:stream.write(encode(ledger))
    print(json.dumps({'output':out.relative_to(ROOT).as_posix(),'status':ledger['status'],'received_bytes':ledger['received_bytes'],'errors':ledger['errors']}))
    return 0 if ledger['status']=='COMPLETE' else 20
if __name__=='__main__':raise SystemExit(main())

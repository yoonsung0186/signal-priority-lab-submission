"""Acquire pinned public sample; metadata license checked before bounded download."""
import hashlib,json,pathlib,urllib.request,urllib.parse,time,zipfile,stat
from datetime import datetime,timezone
from preflight import verify_root,safe_child
ROOT=pathlib.Path(__file__).resolve().parents[1]
def main():
    meta=json.loads((ROOT/'state/research/sitpet/metadata.json').read_text())
    verify_root(ROOT)
    if meta['metadata']['license']['id']!='cc-by-4.0':raise ValueError('Rights not allowed')
    f=next(x for x in meta['files'] if x['key']=='examples.zip')
    url=f['links']['self']
    if urllib.parse.urlparse(url).hostname!='zenodo.org' or urllib.parse.urlparse(url).scheme!='https':raise ValueError('Unapproved source')
    if not 0<f['size']<1024**3:raise ValueError('Download budget')
    out=safe_child(ROOT,'data/raw/sitpet_v1_1'); out.mkdir(parents=True,exist_ok=False)
    h=hashlib.sha256(); md=hashlib.md5(); total=0; start=time.monotonic()
    with urllib.request.urlopen(url,timeout=60) as r, (out/'examples.zip').open('xb') as w:
        if urllib.parse.urlparse(r.url).hostname!='zenodo.org':raise ValueError('Unapproved redirect')
        while chunk:=r.read(1024*1024):
            total+=len(chunk)
            if total>f['size'] or total>1024**3: raise ValueError('Download budget exceeded')
            h.update(chunk);md.update(chunk);w.write(chunk)
    if total!=f['size'] or 'md5:'+md.hexdigest()!=f['checksum']:raise ValueError('Content integrity mismatch')
    with zipfile.ZipFile(out/'examples.zip') as z:
        entries=[]; names=set(); size=0
        for info in z.infolist():
            p=pathlib.PurePosixPath(info.filename)
            if p.is_absolute() or '..' in p.parts or '\\' in info.filename or ':' in info.filename:raise ValueError('Unsafe member')
            if info.filename.casefold() in names:raise ValueError('Duplicate member')
            names.add(info.filename.casefold());size+=info.file_size
            if stat.S_ISLNK(info.external_attr>>16):raise ValueError('Link member')
            entries.append({'path':info.filename,'bytes':info.file_size})
        if size>4*1024**3:raise ValueError('Expansion budget')
    manifest={'source_id':'sitpet','version':'1.1','doi':'10.5281/zenodo.19231310','url':url,'resolved_url':r.url,'retrieved_at_utc':datetime.now(timezone.utc).isoformat(),'official_page':'https://zenodo.org/records/19231310','license_evidence':'state/research/sitpet/LICENSE.md','license_sha256':hashlib.sha256((ROOT/'state/research/sitpet/LICENSE.md').read_bytes()).hexdigest(),'bytes':total,'sha256':h.hexdigest(),'md5':md.hexdigest(),'elapsed_s':time.monotonic()-start,'rights':{'access_status':'ALLOW','analysis_status':'ALLOW','share_derived_status':'ALLOW','share_raw_status':'ALLOW','contest_use_status':'UNKNOWN'},'license':'CC-BY-4.0','entries':entries,'raw_packaging':False}
    (out/'acquisition.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps(manifest))
if __name__=='__main__':main()

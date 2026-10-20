"""Read-only archive/checksum/source audit; save a new local audit record."""
import argparse,hashlib,json,pathlib,zipfile
from snapshot import ROOT,snapshot,digest
from preflight import safe_child,verify_root
from final_verify import encode,identifier

def audit(delivery_ref):
    directory=safe_child(ROOT,delivery_ref)
    if directory.parent!=ROOT/'delivery':raise ValueError('Select one local delivery')
    index=json.loads((directory/'DELIVERY_INDEX.json').read_bytes());checks={}
    for line in (directory/'CHECKSUMS.sha256').read_text(encoding='ascii').splitlines():
        sha,name=line.split('  ',1)
        if pathlib.PurePosixPath(name).name!=name or '\\' in name or ':' in name:raise ValueError('Unsafe checksum path')
        if name in checks:raise ValueError('Duplicate checksum member')
        checks[name]=sha
    expected={entry['path'] for entry in index['files']}|{'DELIVERY_INDEX.json'}
    if set(checks)!=expected:raise ValueError('Incomplete checksums')
    for name,sha in checks.items():
        if digest(safe_child(ROOT,(directory/name).relative_to(ROOT).as_posix()).read_bytes())!=sha:raise ValueError('Checksum mismatch')
    source=snapshot();audited=[]
    if index['source_snapshot']!=source['sha256']:raise ValueError('Delivery is not latest source')
    for entry in index['files']:
        path=directory/entry['path']
        if path.stat().st_size!=entry['bytes'] or checks[entry['path']]!=entry['sha256']:raise ValueError('Archive index mismatch')
        with zipfile.ZipFile(path) as archive:
            names=archive.namelist()
            if len(names)!=len(set(names)) or archive.testzip() is not None:raise ValueError('Duplicate/invalid ZIP')
            for info in archive.infolist():
                p=pathlib.PurePosixPath(info.filename)
                if p.is_absolute() or '..' in p.parts or '\\' in info.filename or ':' in info.filename or info.file_size>20*1024**2:raise ValueError('Unsafe/oversized member')
                if set(p.parts)&{'.venv','.cache','.git','raw','__pycache__'} or p.name.startswith('.env') or p.suffix in {'.key','.pem','.zip'}:raise ValueError('Forbidden member')
                if any(part.startswith(('pet_unit_fixtures_','resume_unit_fixtures_')) for part in p.parts):raise ValueError('Mock fixture packaged')
            manifest=json.loads(archive.read('manifest.json'))
            if set(names)!={x['path'] for x in manifest['files']}|{'manifest.json'}:raise ValueError('Manifest membership mismatch')
            for item in manifest['files']:
                raw=archive.read(item['path'])
                if len(raw)!=item['bytes'] or digest(raw)!=item['sha256']:raise ValueError('Manifest hash mismatch')
            if entry['path'].startswith('SPL_SOURCE_'):
                if json.loads(archive.read('source_snapshot.json'))!=source:raise ValueError('Source snapshot mismatch')
                if set(names)!={x['path'] for x in source['files']}|{'source_snapshot.json','manifest.json'}:raise ValueError('Source ZIP membership mismatch')
                for item in source['files']:
                    if digest(archive.read(item['path']))!=item['sha256']:raise ValueError('Source ZIP not verified source')
            else:
                if json.loads(archive.read('HANDOFF_STATUS.json'))!=index['status']:raise ValueError('Status mismatch')
                if json.loads(archive.read('validation/verification_summary.json'))!=index['verification']:raise ValueError('Selected verification mismatch')
            audited.append({'archive':entry['path'],'sha256':entry['sha256'],'members_verified':len(names)})
    preserved=json.loads((ROOT/'state/r1_20261012T0804/resume_record.json').read_bytes())['preserved_prior_files']
    for item in preserved:
        if digest(safe_child(ROOT,item['path']).read_bytes())!=item['sha256']:raise ValueError('Previous result changed: '+item['path'])
    return {'status':'PASS','delivery':delivery_ref,'archives':audited,'prior_files_unchanged':len(preserved),'source_snapshot':source['sha256'],'report_generated':False,'external_submission_performed':False}

def main():
    p=argparse.ArgumentParser();p.add_argument('--delivery',required=True);p.add_argument('--audit-id',required=True);a=p.parse_args();verify_root(ROOT);identifier(a.audit_id)
    target=safe_child(ROOT,'state/delivery_audits/'+a.audit_id+'.json')
    if target.exists():raise FileExistsError('Audit ID collision')
    result=audit(a.delivery);target.parent.mkdir(parents=True,exist_ok=True)
    with target.open('xb') as f:f.write(encode(result))
    print(json.dumps(result))
if __name__=='__main__':main()

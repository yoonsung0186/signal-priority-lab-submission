"""Read only selected small logical tables, never provider scripts or tracks."""
import csv,gzip,hashlib,io,json,stat,zipfile
from pathlib import Path,PurePosixPath
from .contracts import ContractError
from .pet_contracts import TABLE_MEMBERS,REQUIRED,digest
from .pet_signals import SignalIndex
from .safety import child

class LimitedReader:
    def __init__(self,stream,limit,hashing=False):self.stream=stream;self.limit=limit;self.count=0;self.hash=hashlib.sha256() if hashing else None
    def read(self,size=-1):
        if size<0:size=min(1024*1024,self.limit-self.count+1)
        raw=self.stream.read(size);self.count+=len(raw)
        if self.count>self.limit:raise ContractError('PET_STREAM_EXPANSION_LIMIT')
        if self.hash is not None:self.hash.update(raw)
        return raw
    def readable(self):return True
    def seekable(self):return False
    def writable(self):return False
    def flush(self):pass
    def close(self):pass
    @property
    def closed(self):return False

def file_hash(path):
    result=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):result.update(block)
    return result.hexdigest()

def load_profile(root,relative):
    import yaml
    path=child(root,relative)
    if path.stat().st_size>256*1024:raise ContractError('PET_PROFILE_SIZE_LIMIT')
    profile=yaml.safe_load(path.read_text(encoding='utf-8'))
    if not isinstance(profile,dict) or profile.get('profile_id')!='PET_INTERACTIONS_V1':raise ContractError('INVALID_PET_PROFILE')
    if profile.get('data_kind','REAL')!='REAL':raise ContractError('CLI_PET_PROFILE_REQUIRES_REAL_AUTHORIZED_INPUT')
    manifest_path=child(root,profile['rights_manifest']);rights=json.loads(manifest_path.read_text(encoding='utf-8'))
    if any(rights.get('rights',{}).get(field)!='ALLOW' for field in ('access_status','analysis_status')):raise ContractError('PET_ACCESS_OR_ANALYSIS_RIGHTS_BLOCKED')
    if rights.get('version')!=str(profile.get('source_version')) or rights.get('source_id')!=profile.get('dataset_id') or not rights.get('license_evidence'):raise ContractError('PET_SOURCE_VERSION_OR_LICENSE_UNVERIFIED')
    if not isinstance(profile.get('inputs'),list) or not profile['inputs']:raise ContractError('PET_INPUTS_MISSING')
    return profile,rights,digest(path.read_bytes())

def load_tables(root,profile,sink=None):
    tables={name:[] for name in TABLE_MEMBERS if name!='signal_states'};signals=SignalIndex();inventory=[];inputs=[]
    for specification in profile['inputs']:
        path=child(root,specification['archive']);actual=file_hash(path)
        if actual!=specification.get('sha256'):raise ContractError('PET_ARCHIVE_HASH_MISMATCH')
        inputs.append({'archive':specification['archive'],'sha256':actual,'bytes':path.stat().st_size})
        selected=specification.get('tables',list(TABLE_MEMBERS))
        if not isinstance(selected,list) or any(name not in TABLE_MEMBERS for name in selected):raise ContractError('INVALID_PET_TABLE_SELECTION')
        with zipfile.ZipFile(path) as archive:
            names=set();canonical_names=set();total=0
            for item in archive.infolist():
                name=item.filename;original=item.orig_filename;pure=PurePosixPath(original)
                if not original or pure.is_absolute() or '..' in pure.parts or '\\' in original or ':' in original or '\0' in original or str(pure).casefold() in canonical_names or stat.S_ISLNK(item.external_attr>>16):raise ContractError('UNSAFE_PET_ZIP_MEMBER')
                names.add(name);canonical_names.add(str(pure).casefold());total+=item.file_size
                if total>2*1024**3 or len(names)>10000:raise ContractError('PET_ZIP_EXPANSION_LIMIT')
            for table in selected:
                member=specification.get('members',{}).get(table,TABLE_MEMBERS[table])
                if not isinstance(member,str):raise ContractError('INVALID_PET_MEMBER_MAPPING')
                if member not in names:raise ContractError('SELECTED_PET_TABLE_MISSING_'+table)
                source_index=len(inventory);source={'archive':specification['archive'],'archive_sha256':actual,'member':member,'table':table}
                inventory.append(source);count=0
                with archive.open(member) as stream:
                    compressed=LimitedReader(stream,2*1024**3,True)
                    decompressed=gzip.GzipFile(fileobj=compressed) if member.endswith('.gz') else compressed
                    expanded=LimitedReader(decompressed,4*1024**3)
                    text=io.TextIOWrapper(io.BufferedReader(_Raw(expanded)),encoding='utf-8-sig',newline='')
                    try:
                        reader=csv.DictReader(text)
                        if not set(REQUIRED[table])<=set(reader.fieldnames or []):raise ContractError('PET_TABLE_HEADER_MISMATCH_'+table)
                        for count,row in enumerate(reader,1):
                            if count>50000000:raise ContractError('PET_TABLE_ROW_LIMIT')
                            if None in row:raise ContractError('PET_CSV_EXTRA_FIELDS')
                            if sink is not None:
                                row['__source']={'source_index':source_index,'source_row_id':count}
                                sink(table,row)
                            elif table=='signal_states':signals.add(row,source_index,count)
                            else:
                                row['__source']={'source_index':source_index,'source_row_id':count}
                                tables[table].append(row)
                    finally:text.close()
                    source.update(row_count=count,source_file_sha256=compressed.hash.hexdigest(),expanded_bytes=expanded.count)
    if sink is None and any(not tables[name] for name in ('pet_events','objects','scenarios')):raise ContractError('PET_REQUIRED_TABLE_EMPTY_OR_MISSING')
    signals.finalize()
    return tables,signals,{'inputs':inputs,'tables':inventory,'tracks_parsed':False}

class _Raw(io.RawIOBase):
    def __init__(self,reader):super().__init__();self.reader=reader
    def readable(self):return True
    def readinto(self,buffer):
        raw=self.reader.read(len(buffer));buffer[:len(raw)]=raw;return len(raw)

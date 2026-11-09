"""Explicit-member APK extraction; never enumerate/extract the entire archive."""
from pathlib import Path, PurePosixPath
import hashlib
import os
import zipfile
import zlib
from ._types import DEFAULT_LIMITS, FormatError

def extract_members(apk_path, members, output_dir, *, limits=DEFAULT_LIMITS):
    root=Path(output_dir).resolve()
    root.mkdir(parents=True,exist_ok=True)
    selected=list(members)
    if len(selected)>limits.max_chunks or len(set(selected))!=len(selected):
        raise FormatError('invalid_member_selection')
    paths=[]
    for member in selected:
        if not isinstance(member,str) or '\\' in member or ':' in member:
            raise FormatError('unsafe_archive_member')
        rel=PurePosixPath(member)
        if rel.is_absolute() or '..' in rel.parts or not rel.parts:
            raise FormatError('unsafe_archive_member')
        target=(root/Path(*rel.parts)).resolve()
        if not target.is_relative_to(root):
            raise FormatError('archive_path_escape')
        if target.exists():
            raise FileExistsError(target)
        paths.append((member,target))
    results=[]
    with zipfile.ZipFile(apk_path) as archive:
        for member,target in paths:
            info=archive.getinfo(member)
            if info.is_dir() or info.file_size>limits.max_file_bytes:
                raise FormatError('archive_member_size_limit')
            with archive.open(info) as stream:
                data=stream.read(limits.max_file_bytes+1)
            if len(data)!=info.file_size or len(data)>limits.max_file_bytes or zlib.crc32(data)&0xffffffff!=info.CRC:
                raise FormatError('archive_member_integrity')
            target.parent.mkdir(parents=True,exist_ok=True)
            # Target was resolved/contained before writes; write bytes atomically.
            temporary=target.with_name(target.name+'.extracting.tmp')
            with temporary.open('xb') as stream:stream.write(data)
            os.replace(temporary,target)
            results.append({'member':member,'file':str(target),'bytes':len(data),
                            'crc32':f'{info.CRC:08x}','sha256':hashlib.sha256(data).hexdigest()})
    return results

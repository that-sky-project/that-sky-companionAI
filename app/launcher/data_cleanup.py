"""Offline, preview-bound cleanup of generated data. No account discovery.

Only the explicit roots below are eligible. Mixed registration JSON is edited
by member spans: only removed member values enter the recovery journal, never
the surrounding connection/profile document. All public results omit values.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import threading
import time
import uuid

CATEGORIES = ('terrain', 'catalog', 'names', 'captures', 'props')
LEGACY_CATEGORIES = frozenset(('terrain', 'catalog', 'names', 'captures'))
RELEASE = 'runtime/terrain-experimental-20261104/release-v20-hop-trial'
MIXED = {
    'sources': 'runtime/private/catalog_sources.json',
    'pins': 'runtime/private/catalog_account_pins.json',
    'profiles': 'runtime/private/connection0167_profiles.local.json',
}
VERSION = re.compile(r'[0-9][A-Za-z0-9_.-]{0,79}\Z')
BATCH = re.compile(r'[0-9]{8}T[0-9]{6}-[0-9a-f]{16}\Z')
ACCOUNT_FOLDER = re.compile(r'(?:[A-Za-z0-9_-]{1,80}|\.[tb][0-9a-f]{8})\Z')
MAGIC = 'xiaoguang-generated-data-cleanup-v1'
MANIFEST = '.xg-cleanup.json'
INDEX = '.xg-cleanup-index.json'
SCAN_LIMIT=1024*1024
COPY_CHUNK=1024*1024
# 仅由离线地形 worker 生成；普通 JSON、用户路线以及私有场景块不是缓存。
HOP_NAME = re.compile(r'hop-([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\.json(?:\.pending)?\Z')
HOP_LIMIT = 8 * 1024 * 1024
HOP_SCHEMAS = frozenset(('bounded-hop-proof-v21-contact', 'bounded-aerial-proof-v22'))
PROTECTED_NAMES = frozenset(('.env', 'config.json', 'settings.json', 'tour_routes.json',
    'spawn_y_fix.json', 'scene_blobs', 'light_candle.json', 'zero_darkstones.json'))
_DOS_DEVICE = re.compile(r'(?i)(?:con|prn|aux|nul|conin\$|conout\$|(?:com|lpt)[1-9¹²³])\Z')

MAX_JSON = 32 * 1024 * 1024
SENSITIVE = re.compile(r'(?i)(?:^|[_.\- ])(?:secrets?|credentials?|passwords?|passwd|passphrase|'
    r'tokens?|cookies?|sessions?|session3|login|accounts?|ai[_-]?keys?|api[_-]?keys?|'
    r'authorization|private[_-]?keys?|personas?|memories|memory|owner[_-]?uuid|owner[_-]?binding)(?:$|[_.\- ])')
SECRET_KEYS = frozenset(('password','passwd','passphrase','secret','secrets','credential','credentials',
    'apikey','aikey','accesskey','accesskeyid','accesskeysecret','accesstoken','refreshtoken','authtoken',
    'logintoken','token','cookie','cookies','authorization','privatekey','session','session3','sessionkey',
    'owneruuid','ownerbinding','persona','personas','memory','chatmemory','chat history','accounts'))
EXTENSIONS = frozenset(('.json','.bin','.f32','.u32','.i32','.f64','.u16','.u8','.nav','.navmesh',
    '.mesh','.npz','.npy','.gz','.zst','.obj','.mtl','.dat','.apk'))
TEXT_FILES = frozenset(('sha256sums','sha256sums.txt','build.txt','status.txt'))


class CleanupError(Exception):
    def __init__(self, code, message='', path=None):
        self.code, self.message, self.path = code, message or code, path
        super().__init__(self.message)


def _reject(code, message='', path=None):
    raise CleanupError(code, message, path)


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _secret(value):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r'[^a-z0-9]', '', str(key).lower())
            if (normalized in SECRET_KEYS or normalized.endswith(('password','secret','apikey','aikey',
                    'accesstoken','refreshtoken','privatekey'))):
                return True
            if _secret(item):
                return True
    elif isinstance(value, list):
        return any(_secret(item) for item in value)
    elif isinstance(value, str) and re.search(r'-----BEGIN [A-Z ]*PRIVATE KEY-----', value):
        return True
    return False


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _reject('ambiguous_json', 'JSON 含重复字段，拒绝修改')
            result[key] = value
        return result
    if len(raw) > MAX_JSON:
        _reject('json_too_large', 'JSON 超出可核验大小')
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: _reject('invalid_json', 'JSON 数值无效'))
    except (ValueError, UnicodeError, RecursionError):
        _reject('invalid_json', 'JSON 无效，未修改')


@dataclass
class _Node:
    start: int
    end: int
    members: dict = field(default_factory=dict)  # key -> (key_start, value_node)
    object: bool = False


class _Spans:
    """Token positions, retaining all untouched value bytes and original encoding."""
    def __init__(self, raw):
        _json(raw)  # reject duplicates/non-finite numbers before editing
        self.text = raw.decode('utf-8-sig')
        self.bom = raw.startswith(b'\xef\xbb\xbf')
        self.decoder = json.JSONDecoder()
        self.root, end = self._node(0, 0)
        if self.text[end:].strip():
            _reject('invalid_json')

    def _ws(self, offset):
        while offset < len(self.text) and self.text[offset].isspace():
            offset += 1
        return offset

    def _node(self, offset, depth):
        if depth > 100:
            _reject('json_too_deep')
        offset = self._ws(offset)
        if self.text[offset] != '{':
            _, end = self.decoder.raw_decode(self.text, offset)
            return _Node(offset,end), end
        node = _Node(offset,0,object=True)
        cursor = self._ws(offset+1)
        while self.text[cursor] != '}':
            key_start = cursor
            key, cursor = self.decoder.raw_decode(self.text,cursor)
            cursor = self._ws(cursor)
            if self.text[cursor] != ':':
                _reject('invalid_json')
            child, cursor = self._node(cursor+1,depth+1)
            node.members[key] = (key_start,child)
            cursor = self._ws(cursor)
            if self.text[cursor] == '}':
                break
            cursor = self._ws(cursor+1)
        node.end = cursor+1
        return node,node.end

    def get(self, path):
        node = self.root
        for key in path:
            row = node.members.get(key)
            if row is None:
                return None
            node = row[1]
        return node

    def value(self, path):
        node = self.get(path)
        return self.text[node.start:node.end] if node else None

    def remove(self, paths):
        selected={tuple(path) for path in paths}
        def transform(node,path):
            if not node.object or not any(key[:len(path)]==path for key in selected):
                return self.text[node.start:node.end]
            members=list(node.members.items());parts=[]
            for key,(start,child) in members:
                if path+(key,) not in selected:
                    parts.append(self.text[start:child.start]+transform(child,path+(key,)))
            if not parts:return '{}'
            prefix=self.text[node.start+1:members[0][1][0]]
            suffix=self.text[members[-1][1][1].end:node.end-1]
            return '{'+prefix+','.join(parts)+suffix+'}'
        text=self.text[:self.root.start]+transform(self.root,())+self.text[self.root.end:]
        return (b'\xef\xbb\xbf' if self.bom else b'')+text.encode('utf-8')

    def restore(self, fragments, *, allow_existing=False):
        groups = {}
        for fragment in fragments:
            path,raw = fragment['path'],fragment['raw']
            existing = self.value(path)
            if existing is not None:
                if allow_existing and existing == raw:
                    continue
                _reject('restore_conflict','登记字段已重新生成，拒绝覆盖')
            parent = self.get(path[:-1])
            if parent is None or not parent.object:
                _reject('restore_conflict','登记所属对象已变化，拒绝覆盖')
            groups.setdefault(tuple(path[:-1]),[]).append((path[-1],raw))
        text = self.text
        replacements=[]
        for parent,fragments in groups.items():
            node=self.get(parent)
            addition=','.join(json.dumps(key,ensure_ascii=False)+':'+raw for key,raw in fragments)
            replacements.append((node.end-1, (',' if node.members else '')+addition))
        for offset,addition in sorted(replacements,reverse=True):
            text=text[:offset]+addition+text[offset:]
        return (b'\xef\xbb\xbf' if self.bom else b'')+text.encode('utf-8')


class _DiskPath(type(Path())):
    """仅在 Windows I/O 边界使用长路径前缀，日志、清单和比较仍用普通路径。"""
    def __fspath__(self):
        value=str(self)
        if (os.name=='nt' and self.is_absolute() and not value.startswith(('\\\\?\\','\\\\.\\'))
                and (len(value)>=224 or len(value.encode('utf-16-le', 'surrogatepass'))>=448)):
            return ('\\\\?\\UNC\\'+value[2:]) if value.startswith('\\\\') else ('\\\\?\\'+value)
        return value

    def canonical(self):
        value=str(self.resolve())
        if os.name=='nt' and value.startswith('\\\\?\\UNC\\'):value='\\\\'+value[8:]
        elif os.name=='nt' and value.startswith(chr(92)*2+'?'+chr(92)) and value[4:5].isalpha() and value[5:7]==':'+chr(92):value=value[4:]
        return _DiskPath(value)


class CleanupService:
    def __init__(self, data_root, *, is_online, is_busy, clock=time.time, preview_ttl=120, progress=None):
        if os.name=='nt':
            # 允许普通扩展盘符/UNC，不允许设备命名空间绕过 data 根的目录检查。
            value=os.fspath(data_root).replace('/', '\\')
            if value.startswith('\\\\.\\'):_reject('invalid_data_root')
            if value.startswith('\\\\?\\'):
                tail=value[4:]
                if tail[:4].casefold()=='unc\\':value='\\\\'+tail[4:]
                elif re.match(r'^[A-Za-z]:\\',tail):value=tail
                else:_reject('invalid_data_root')
            data_root=value
        root=_DiskPath(data_root)
        if not root.is_absolute() or root == Path(root.anchor) or '..' in root.parts:
            _reject('invalid_data_root','data 根必须是明确的绝对目录')
        self.root=root
        self.clock=clock
        self.ttl=max(1,min(int(preview_ttl),300))
        self.is_online,self.is_busy=is_online,is_busy
        self._plans={}
        self.progress=progress
        self._lock=threading.RLock()
        self._check_path(root)
        if not root.is_dir():
            _reject('invalid_data_root','data 根不存在')
        self.root=root.canonical()
        if self.root == Path(self.root.anchor):_reject('invalid_data_root')

    def set_progress(self, callback):
        # The API uses one job slot and sets this before entering a core method.
        with self._lock:self.progress=callback

    def _emit(self, phase, message, files_done=0, files_total=None, bytes_done=0, bytes_total=None):
        if self.progress:
            try:self.progress(dict(phase=phase,message=message,files_done=files_done,
                                  files_total=files_total,bytes_done=bytes_done,bytes_total=bytes_total))
            except Exception:pass  # UI telemetry must never abort a transaction.

    @staticmethod
    def _check_entry(info):
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            _reject('unsafe_link','路径包含符号链接、junction 或 reparse point')
        if stat.S_ISREG(info.st_mode) and info.st_nlink>1:_reject('unsafe_link','路径包含硬链接')

    @staticmethod
    def _safe_relative(value, dot=False):
        if value=='.' and dot:return True
        if not isinstance(value,str) or not value or any(c in value for c in '\\:<>"|?*'):return False
        parts=value.split('/')
        return all(p and p not in ('.','..') and not p.endswith((' ','.')) and
                   not _DOS_DEVICE.fullmatch(p.partition('.')[0].rstrip(' '))
                   and not any(ord(c)<32 for c in p) for p in parts)

    @staticmethod
    def _matches(current, expected, *, subset=False, partial=False):
        # v1 full hashes on large files are deliberately NOT reread. Metadata
        # cannot detect same-size/same-mtime changes; small hashes remain strict.
        if not isinstance(expected,list):return False
        wanted={r[0]:r for r in expected}
        if not subset and len(current)!=len(expected):return False
        for row in current:
            old=wanted.get(row[0])
            if old is None or row[1]!=old[1]:return False
            if row[1]=='dir':
                if not subset and row[:4]!=old[:4]:return False
            elif partial:
                if row[2]>old[2]:return False
            elif row[:4]!=old[:4] or (row[4] is not None and row[4]!=old[4]):return False
        return True

    @staticmethod
    def _apk_allowed(relative):
        # APK caches are eligible only in the existing terrain generation roots.
        # Other categories keep their original unknown-file refusal unchanged.
        parts=PurePosixPath(relative).parts
        if parts[:1]==('trash',):parts=parts[2:]
        if len(parts)>=3 and parts[0]=='game' and VERSION.fullmatch(parts[1]):
            return parts[2]=='terrain'
        rel='/'.join(parts)
        return any(rel==RELEASE+'/'+name or rel.startswith(RELEASE+'/'+name+'/')
                   for name in ('assets','terrain'))

    @staticmethod
    def _large_json_allowed(relative):
        # Name/path inventory from the local generators, not an all-JSON bypass.
        # Payloads >1 MiB have metadata-only assurance, including these files.
        # Atomic writers may leave the same known artifact with one terminal
        # .pending suffix. Normalize only that suffix, never its parent path.
        if relative.endswith('.pending'):relative=relative[:-len('.pending')]
        parts=PurePosixPath(relative).parts
        if parts[:1]==('trash',):parts=parts[2:]
        if len(parts)>=4 and parts[0]=='game' and parts[2]=='props':
            tail=parts[3:]
            return tail==('catalog.json',) or (len(tail)==3 and tail[:2]==('all','runtime') and
                re.fullmatch(r'[A-Za-z0-9_-]+\.json',tail[2]) is not None)
        if len(parts)==4 and parts[0]=='game' and parts[2]=='catalog-inputs':
            # scene_inputs_build writes <Scene>.json and index.json here.
            return re.fullmatch(r'[A-Za-z0-9_-]+\.json',parts[3]) is not None
        if len(parts)==2 and parts[0]=='hop-proofs':
            # 2026-11-16: hop proof records written by the terrain hop worker.
            return HOP_NAME.fullmatch(parts[1]) is not None
        if len(parts)>=4 and parts[0]=='game' and parts[2]=='terrain':tail='/'.join(parts[3:])
        elif '/'.join(parts)==RELEASE+'/multimap-catalog.json':return True
        elif '/'.join(parts).startswith(RELEASE+'/assets/'):tail='assets/'+ '/'.join(parts[len(PurePosixPath(RELEASE).parts)+1:])
        elif '/'.join(parts).startswith(RELEASE+'/terrain/'):tail='/'.join(parts[len(PurePosixPath(RELEASE).parts)+1:])
        else:return False
        mapname=r'[A-Za-z0-9_-]+'
        patterns=(
            r'(?:multimap-catalog|build-summary)\.json',
            r'assets(?:\.work)?/(?:catalog|build-summary)\.json',
            r'assets\.work/(?:bindings|\.bindings-[0-9]+-[0-9]+)/(?:bindings|summary|prior-registry)\.json',
            r'assets\.work/(?:bindings|\.bindings-[0-9]+-[0-9]+)/scenes/'+mapname+r'\.json',
            r'assets(?:\.work)?/(?:all-scenes-v1|beamo-world-all-v1|beamo-navigation-all-v1)/(?:catalog|summary)\.json',
            r'assets(?:\.work)?/(?:all-scenes-v1|beamo-world-all-v1)/'+mapname+r'/build\.json',
            r'assets(?:\.work)?/(?:all-scenes-v1|beamo-world-all-v1)/'+mapname+r'/(?:static-world|initial-collision|render)/(?:manifest|runtime-manifest)\.json',
            # 2026-11-16: per-map beamo manifests (often >1 MiB) and final assembled assets.
            r'assets(?:\.work)?/beamo-world-all-v1/'+mapname+r'/beamo/manifest\.json',
            r'assets/'+mapname+r'/(?:static-world|initial-collision|render)/(?:manifest|runtime-manifest)\.json',
            r'assets(?:\.work)?/(?:all-scenes-v1|beamo-navigation-all-v1)/'+mapname+r'/(?:navigation-layered-v2|nav-v3|reused-verified|attempts/[0-9]+)/nav-build(?:-[0-9a-f]{64})?\.json',
            r'assets\.work/beamo-navigation-all-v1/'+mapname+r'/nav-build\.json',
            r'assets/on-demand-navigation-v13/'+mapname+r'/descriptor\.json',
        )
        return any(re.fullmatch(pattern,tail) for pattern in patterns)

    def _check_path(self,path):
        path=_DiskPath(path)
        # Never resolve first: doing so would conceal junction/symlink ancestors.
        for part in [*reversed(path.parents),path]:
            try:
                info=part.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or (getattr(info,'st_file_attributes',0)&0x400):
                _reject('unsafe_link','路径包含符号链接、junction 或 reparse point')
            if stat.S_ISREG(info.st_mode) and info.st_nlink>1:
                _reject('unsafe_link','路径包含硬链接')

    def _path(self,relative):
        if not self._safe_relative(relative):
            _reject('unsafe_path')
        parts=PurePosixPath(relative).parts
        if not parts or any(p in ('','.','..') for p in parts) or relative.startswith('/'):
            _reject('unsafe_path')
        path=self.root.joinpath(*parts)
        self._check_path(path)
        if not path.canonical().is_relative_to(self.root):
            _reject('unsafe_path')
        return path

    def _state(self):
        try:
            if self.is_online():
                _reject('client_online','请先让小光下线并停止聊天/采集')
            if self.is_busy():
                _reject('task_running','仍有生成、采集或启动任务运行')
        except CleanupError:
            raise
        except Exception:
            _reject('state_unknown','运行状态未确认，拒绝修改')

    def _read(self,path):
        self._check_path(path)
        # Only our own journals need the larger cap. Private mixed documents
        # are never read beyond the small-text budget.
        managed=path.name in (MANIFEST,INDEX) and path.is_relative_to(self.root/'trash')
        limit=MAX_JSON if managed else SCAN_LIMIT
        if path.stat().st_size>limit:_reject('json_too_large','登记文本过大，保留原文件并拒绝修改')
        with path.open('rb') as stream:return stream.read(limit+1)

    def _atomic(self,path,raw):
        self._check_path(path)
        path.parent.mkdir(parents=True,exist_ok=True)
        self._check_path(path.parent)
        temp=path.with_name('.cleanup-'+uuid.uuid4().hex+'.tmp')
        try:
            with temp.open('xb') as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
            self._check_path(path)
            # 共享冲突常见于杀软瞬时扫描。只重试原子替换，不覆盖写原件。
            for delay in (0.03, 0.09, 0.18, None):
                try:
                    self._check_path(temp);self._check_path(path)
                    os.replace(temp,path)
                    break
                except PermissionError:
                    if delay is None:raise
                    time.sleep(delay)
        finally:
            if temp.exists():
                temp.unlink()

    def _write_doc(self,path,doc):
        self._atomic(path,json.dumps(doc,ensure_ascii=False,separators=(',',':')).encode('utf-8'))

    def _allowed(self,rel,category):
        if category not in CATEGORIES or not isinstance(rel,str):return False
        parts=PurePosixPath(rel).parts
        if len(parts)>=3 and parts[0]=='game' and VERSION.fullmatch(parts[1]):
            return len(parts)==3 and parts[2] in {'terrain':('terrain',),'catalog':('catalog',),
                'names':('names','catalog-inputs'),'captures':(),'props':('props',)}[category]
        if category=='terrain':
            return (len(parts)==2 and parts[0]=='hop-proofs' and HOP_NAME.fullmatch(parts[1]) is not None) or rel in (RELEASE+'/assets',RELEASE+'/terrain',RELEASE+'/multimap-catalog.json')
        if category=='catalog':
            return rel==RELEASE+'/asset-catalog-v32c' or (rel.startswith(RELEASE+'/asset-catalog-acct/')
                and len(parts)==len(PurePosixPath(RELEASE).parts)+2 and
                ACCOUNT_FOLDER.fullmatch(parts[-1]) is not None)
        return False

    def _roots(self,selection):
        found=[]
        game=self._path('game')
        if game.exists():
            if not game.is_dir():_reject('unsafe_path',path='game')
            for version in sorted(game.iterdir()):
                if not VERSION.fullmatch(version.name):continue
                self._check_path(version)
                for category,names in [('terrain',('terrain',)),('catalog',('catalog',)),('names',('names','catalog-inputs')),('props',('props',))]:
                    if selection[category]:
                        found.extend((category,'game/'+version.name+'/'+name) for name in names)
        if selection['terrain']:
            found.extend(('terrain',RELEASE+'/'+name) for name in ('assets','terrain','multimap-catalog.json'))
            proofs=self._path('hop-proofs')
            if proofs.is_dir():
                for entry in sorted(proofs.iterdir()):
                    if HOP_NAME.fullmatch(entry.name):
                        found.append(('terrain','hop-proofs/'+entry.name))
        if selection['catalog']:
            found.append(('catalog',RELEASE+'/asset-catalog-v32c'))
            accounts=self._path(RELEASE+'/asset-catalog-acct')
            if accounts.exists():
                for path in sorted(accounts.iterdir()):
                    if not ACCOUNT_FOLDER.fullmatch(path.name):
                        _reject('unknown_generated_path','按号目录包含未知项目',str(path.relative_to(self.root)))
                    found.append(('catalog',RELEASE+'/asset-catalog-acct/'+path.name))
        return [(category,rel) for category,rel in found if self._path(rel).exists()]

    def _tree(self,rel,*,sensitivity=True,policy_rel=None,partial=False):
        base=self._path(rel);rows=[];files=0;size=0
        self._emit('inventory','正在按目录元数据扫描')
        logical_parts=PurePosixPath(policy_rel or rel).parts
        if logical_parts[:1]==('trash',):logical_parts=logical_parts[2:]
        hop=HOP_NAME.fullmatch(logical_parts[-1]) if len(logical_parts)==2 and logical_parts[0]=='hop-proofs' else None
        def visit(path):
            nonlocal files,size
            info=path.lstat();self._check_entry(info)
            local=path.relative_to(base).as_posix()
            if not self._safe_relative(local,dot=True):_reject('unsafe_path')
            name=path.name.lower()
            if name.startswith('.xg-write-') and name.endswith('.pending'):
                _reject('unclassified_pending','未完成的通用写入可能含私有配置，保留原件',rel)
            if ('memdump' in name or name.removesuffix('.pending') in PROTECTED_NAMES
                    or (sensitivity and SENSITIVE.search(name))):
                _reject('sensitive_data','所选目录含敏感文件名',rel)
            if hop and not stat.S_ISREG(info.st_mode):_reject('unknown_generated_file','跳跃证明必须为普通文件',rel)
            if stat.S_ISDIR(info.st_mode):
                rows.append([local,'dir',0,info.st_mtime_ns])
                for child in sorted(path.iterdir(),key=lambda p:p.name):visit(child)
            elif stat.S_ISREG(info.st_mode):
                suffix=path.suffix.lower()
                if suffix=='.pending':suffix=Path(path.stem).suffix.lower()
                if ((suffix=='.apk' and not self._apk_allowed(policy_rel or rel)) or
                    (suffix not in EXTENSIONS and name not in TEXT_FILES)):
                    _reject('unknown_generated_file','所选目录含非生成文件',rel)
                digest=None
                if hop and info.st_size>HOP_LIMIT:_reject('large_text_unverified','跳跃证明超过生成器上限',rel)
                if info.st_size<=SCAN_LIMIT or hop:
                    if info.st_size:
                        with path.open('rb') as stream:raw=stream.read((HOP_LIMIT if hop else SCAN_LIMIT)+1)
                    else:raw=b''
                    if len(raw)!=info.st_size:_reject('preview_stale')
                    if sensitivity:
                        if (re.search(rb'-----BEGIN [A-Z ]*PRIVATE KEY-----',raw) or
                            re.search(rb'(?i)["\x27](?:api[_-]?key|password|passwd|session3|access[_-]?token|refresh[_-]?token|private[_-]?key)["\x27]\s*:',raw)):
                            _reject('sensitive_data','所选目录含敏感内容',rel)
                        if not partial and suffix=='.json':
                            content=_json(raw)
                            if hop and (not isinstance(content,dict) or content.get('schema') not in HOP_SCHEMAS
                                    or content.get('hopId')!=hop[1]):
                                _reject('unknown_generated_file','名称与生成凭据不匹配，保留文件',rel)
                            if _secret(content):_reject('sensitive_data',path=rel)
                    digest=_digest(raw)
                elif suffix=='.json' or name in TEXT_FILES or suffix in ('.obj','.mtl'):
                    logical=(PurePosixPath(policy_rel or rel)/(local if local!='.' else '')).as_posix()
                    if suffix!='.json' or not self._large_json_allowed(logical):
                        _reject('large_text_unverified','未知大文本未读取，拒绝纳入清理',rel)
                after=path.lstat();self._check_entry(after)
                if (after.st_size,after.st_mtime_ns)!=(info.st_size,info.st_mtime_ns):_reject('preview_stale')
                rows.append([local,'file',info.st_size,info.st_mtime_ns,digest]);files+=1;size+=info.st_size
                if files%128==0:self._emit('inventory','正在按目录元数据扫描',files,None,size,None)
            else:_reject('unsafe_file_type',path=rel)
        visit(base)
        self._emit('inventory','目录元数据扫描完成',files,files,size,size)
        return rows

    def _mixed_item(self,kind,paths):
        rel=MIXED[kind];path=self._path(rel)
        if not path.exists() or not paths:return None
        raw=self._read(path);spans=_Spans(raw);fragments=[]
        for keypath in paths:
            value=spans.value(keypath)
            if value is None:continue
            if _secret(_json(value.encode('utf-8'))):
                _reject('sensitive_registration','选中登记字段内含凭据，拒绝修改',rel)
            fragments.append({'path':list(keypath),'raw':value})
        if not fragments:return None
        info=path.stat()
        return {'action':'json_members','kind':kind,'category':'captures' if kind=='profiles' else 'catalog',
                'path':rel,'bytes':sum(len(f['raw'].encode('utf-8')) for f in fragments),'files':1,
                'fingerprint':[info.st_size,info.st_mtime_ns,_digest(raw)],'fragments':fragments}

    def _mixed(self,selection,items):
        out=[];bindings=set()
        if selection['catalog']:
            for item in items:
                if item['category']!='catalog':continue
                path=self._path(item['path'])/'manifest.json'
                if path.is_file():
                    value=_json(self._read(path))
                    binding=value.get('source_account_binding_hash') if isinstance(value,dict) else None
                    if isinstance(binding,str) and re.fullmatch('[0-9a-f]{64}',binding):bindings.add(binding)
            for kind in ('pins','sources'):
                path=self._path(MIXED[kind])
                if not path.exists():continue
                spans=_Spans(self._read(path));selected=[]
                for key in spans.root.members:
                    if kind=='pins':
                        binding=key
                        if re.fullmatch('[0-9a-f]{64}',key) is None:continue
                    else:
                        try:
                            from qfsky.join0167_profile import account_binding
                            binding=account_binding(str(uuid.UUID(key)))
                        except (ValueError,TypeError,AttributeError):continue
                    # This reset covers all account catalogues. Also clear orphan
                    # registrations, but only when their row schema is known.
                    value=_json(spans.value((key,)).encode('utf-8'))
                    if _secret(value):_reject('sensitive_registration','目录登记含受保护字段',MIXED[kind])
                    if binding not in bindings:
                        if kind=='sources':
                            valid=(isinstance(value,dict) and set(value)=={'record_dir','netversion_u16'}
                                and isinstance(value.get('record_dir'),str)
                                and re.fullmatch('[A-Za-z0-9_-]{1,160}',value['record_dir']) is not None
                                and isinstance(value.get('netversion_u16'),list) and len(value['netversion_u16'])==6
                                and all(type(x)is int and 0<=x<=65535 for x in value['netversion_u16']))
                        else:
                            valid=(isinstance(value,dict) and set(value)=={'source_actor_sha256','source_metadata_sha256','entries_sha256','variants'}
                                and all(isinstance(value.get(k),str) and re.fullmatch('[0-9a-f]{64}',value[k])
                                    for k in ('source_actor_sha256','source_metadata_sha256','entries_sha256'))
                                and isinstance(value.get('variants'),dict))
                        if not valid:_reject('unknown_catalog_registration','孤立目录登记形状不明，保留并拒绝整项',MIXED[kind])
                    selected.append((key,))
                entry=self._mixed_item(kind,selected)
                if entry:out.append(entry)
        if selection['captures']:
            path=self._path(MIXED['profiles'])
            if path.exists():
                spans=_Spans(self._read(path));selected=[]
                for field_name in ('current_capture','captures'):
                    if spans.get((field_name,)) is not None:selected.append((field_name,))
                maps=spans.get(('maps',))
                if maps is not None and maps.object:
                    for mapid,(_,entry) in maps.members.items():
                        for field_name in ('current_capture','captures'):
                            if field_name in entry.members:selected.append(('maps',mapid,field_name))
                entry=self._mixed_item('profiles',selected)
                if entry:out.append(entry)
        return out

    def _public_item(self,item):
        result={k:item[k] for k in ('category','path','action','bytes','files')}
        if item['action']=='json_members':result['records']=len(item['fragments'])
        return result

    def _plan(self,action,**data):
        now=self.clock();token=uuid.uuid4().hex
        self._plans={key:p for key,p in self._plans.items() if p['expires_at']>=now}
        self._plans[token]={'action':action,'expires_at':now+self.ttl,**data}
        return token,self._plans[token]

    def _take(self,token,action,confirm):
        if confirm is not True:_reject('confirmation_required','需要二次确认')
        plan=self._plans.get(token)
        if not plan or plan['action']!=action:_reject('invalid_preview','请重新预览')
        if self.clock()>plan['expires_at']:_reject('preview_expired','预览已过期，请重新预览')
        self._state()
        return plan

    def _guard(self,function):
        with self._lock:
            try:return function()
            except CleanupError as error:
                result={'ok':False,'error_code':error.code,'message':error.message}
                if error.path:result['path']=error.path
                return result
            except OSError as error:
                return {'ok':False,'error_code':'io_error','message':'文件操作失败，数据未被静默丢弃','errno':error.errno}
            except Exception:
                return {'ok':False,'error_code':'internal_error','message':'清理操作失败；请保留恢复记录'}

    def preview(self,selection):
        def run():
            if not isinstance(selection,dict) or set(selection)-set(CATEGORIES) or any(type(v)is not bool for v in selection.values()):
                _reject('invalid_selection','清理选项必须是五类布尔值')
            selected={key:selection.get(key,False) for key in CATEGORIES}
            if not any(selected.values()):_reject('empty_selection','请至少选择一项')
            items=[]
            for category,rel in self._roots(selected):
                if not self._allowed(rel,category):_reject('unsafe_path')
                rows=self._tree(rel)
                items.append({'category':category,'path':rel,'action':'move','fingerprint':rows,
                    'bytes':sum(row[2] for row in rows),'files':sum(row[1]=='file' for row in rows)})
            items+=self._mixed(selected,items)
            token,plan=self._plan('execute',selection=selected,items=items)
            return {'ok':True,'error_code':None,'preview_id':token,'plan_id':token,'expires_at':plan['expires_at'],
                    'selection':selected,'items':[self._public_item(x) for x in items],
                    'total_bytes':sum(x['bytes'] for x in items),'total_files':sum(x['files'] for x in items),
                    'notice':'仅生成数据及选中登记字段；原始录制、账号登录、AI、人设、主人及聊天记忆保留'}
        return self._guard(run)

    def _ensure_trash(self):
        trash=self._path('trash');trash.mkdir(exist_ok=True);self._check_path(trash)
        return trash

    @contextmanager
    def _process_lock(self):
        path=self._ensure_trash()/'.xg-cleanup.lock';self._check_path(path)
        with path.open('a+b') as stream:
            stream.seek(0)
            if os.name=='nt':
                import msvcrt
                if path.stat().st_size==0:stream.write(b'0');stream.flush()
                stream.seek(0)
                try:msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                except OSError:_reject('cleanup_busy','另一个清理操作尚未结束')
                try:yield
                finally:
                    stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                try:fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                except OSError:_reject('cleanup_busy')
                try:yield
                finally:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)

    def _index(self):
        path=self._path('trash/'+INDEX)
        if not path.exists():return {'magic':MAGIC,'batches':[]}
        doc=_json(self._read(path))
        if (not isinstance(doc,dict) or set(doc) not in ({'magic','batches'},{'magic','batches','retiring'}) or doc.get('magic')!=MAGIC
            or not isinstance(doc.get('batches'),list) or _secret(doc)):_reject('untrusted_trash')
        if len(doc['batches'])>10000 or any(not isinstance(x,str) or not BATCH.fullmatch(x) for x in doc['batches']):_reject('untrusted_trash')
        if 'retiring' in doc and doc['retiring'] not in doc['batches']:_reject('untrusted_trash')
        return doc

    def _batch(self,batchid):
        if not isinstance(batchid,str) or not BATCH.fullmatch(batchid):_reject('untrusted_trash')
        path=self._path('trash/'+batchid+'/'+MANIFEST)
        if not path.exists() and self._index().get('retiring')==batchid:
            # 在最后一个 journal 已移除、index 尚未提交时断电。只能收尾空目录。
            if path.parent.exists() and (not path.parent.is_dir() or any(path.parent.iterdir())):_reject('untrusted_trash')
            return {'batch_id':batchid,'status':'purging','items':[],'selection':{k:False for k in CATEGORIES}}
        doc=_json(self._read(path))
        if (not isinstance(doc,dict) or set(doc) not in ({'magic','batch_id','root_hash','created_at','status','selection','items'}, {'magic','batch_id','root_hash','created_at','status','selection','items','fingerprint_version'})
            or doc.get('magic')!=MAGIC or doc.get('batch_id')!=batchid or _secret(doc)
            or doc.get('root_hash')!=_digest(str(self.root).encode()) or not isinstance(doc.get('items'),list)
            or doc.get('status') not in ('moving','trashed','partial','restoring','partial_restore','restored','purging')
            or not isinstance(doc.get('selection'),dict) or set(doc['selection']) not in (set(CATEGORIES),LEGACY_CATEGORIES)
            or any(type(x)is not bool for x in doc['selection'].values())):_reject('untrusted_trash')
        if doc.get('fingerprint_version',1) not in (1,2):_reject('untrusted_trash')
        # v1 的四类回收记录仍可恢复；新类别缺省关闭，不扩大旧批次范围。
        doc['selection']={key:doc['selection'].get(key,False) for key in CATEGORIES}
        for item in doc['items']:
            if not isinstance(item,dict) or type(item.get('files')) is not int or type(item.get('bytes')) is not int or item['files']<0 or item['bytes']<0:_reject('untrusted_trash')
            expected={'category','path','action','fingerprint','bytes','files','state'}
            if item.get('action')=='json_members':expected|={'kind','fragments'}
            if 'transfer' in item:
                if item.get('action')!='move':_reject('untrusted_trash')
                expected.add('transfer')
            if set(item)!=expected or item.get('state') not in ('planned','pending','stored','restore_pending','restored'):_reject('untrusted_trash')
            if item.get('action')=='move':
                if not self._allowed(item.get('path',''),item.get('category')) or not self._safe_relative(item['path']):_reject('untrusted_trash')
                rows=item.get('fingerprint');seen=set()
                if not isinstance(rows,list) or not rows:_reject('untrusted_trash')
                for row in rows:
                    if (not isinstance(row,list) or len(row) not in (4,5) or not self._safe_relative(row[0],dot=True)
                        or row[0].casefold() in seen or row[1] not in ('dir','file')
                        or type(row[2]) is not int or row[2]<0 or type(row[3]) is not int):_reject('untrusted_trash')
                    seen.add(row[0].casefold())
                    if row[1]=='dir':
                        if len(row)!=4 or row[2]!=0:_reject('untrusted_trash')
                    elif len(row)!=5 or not ((isinstance(row[4],str) and re.fullmatch('[0-9a-f]{64}',row[4])) or
                        (row[4] is None and doc.get('fingerprint_version')==2 and row[2]>SCAN_LIMIT)):_reject('untrusted_trash')
                by_name={r[0]:r for r in rows}
                for row in rows[1:]:
                    parent=PurePosixPath(row[0]).parent.as_posix()
                    if parent not in by_name or by_name[parent][1]!='dir':_reject('untrusted_trash')
                if rows[0][0]!='.' or item['files']!=sum(r[1]=='file' for r in rows) or item['bytes']!=sum(r[2] for r in rows):_reject('untrusted_trash')
                if 'transfer' in item:
                    tr=item['transfer']
                    if (not isinstance(tr,dict) or set(tr)!={'direction','id'} or tr['direction'] not in ('out','back')
                        or not isinstance(tr['id'],str) or re.fullmatch('[0-9a-f]{32}',tr['id']) is None):_reject('untrusted_trash')
            elif item.get('action')=='json_members':
                kind=item.get('kind')
                if kind not in MIXED or item.get('path')!=MIXED[kind] or not isinstance(item.get('fragments'),list):_reject('untrusted_trash')
                for fragment in item['fragments']:
                    if not isinstance(fragment,dict) or set(fragment)!={'path','raw'}:_reject('untrusted_trash')
                    path=fragment.get('path');raw=fragment.get('raw')
                    if not isinstance(path,list) or not all(isinstance(k,str) for k in path) or not isinstance(raw,str):_reject('untrusted_trash')
                    if kind=='profiles' and not ((len(path)==1 and path[0] in ('current_capture','captures')) or
                        (len(path)==3 and path[0]=='maps' and path[-1] in ('current_capture','captures'))):_reject('untrusted_trash')
                    if kind!='profiles' and len(path)!=1:_reject('untrusted_trash')
                    if kind=='pins' and re.fullmatch('[0-9a-f]{64}',path[0]) is None:_reject('untrusted_trash')
                    if kind=='sources':
                        try:uuid.UUID(path[0])
                        except (ValueError,TypeError,AttributeError):_reject('untrusted_trash')
                    if _secret(_json(raw.encode())):_reject('sensitive_registration')
            else:_reject('untrusted_trash')
        return doc

    def _save_batch(self,doc):
        self._write_doc(self._path('trash/'+doc['batch_id']+'/'+MANIFEST),doc)

    def _verify_item(self,item):
        path=self._path(item['path'])
        if item['action']=='move':
            if not path.exists() or not self._matches(self._tree(item['path']),item['fingerprint']):_reject('preview_stale','目录、大小或内容已变化，请重新预览')
        else:
            if not path.is_file():_reject('preview_stale')
            info=path.stat();raw=self._read(path)
            if [info.st_size,info.st_mtime_ns,_digest(raw)]!=item['fingerprint']:_reject('preview_stale','登记文件已变化，请重新预览')

    def execute(self,preview_id,confirm=False):
        def run():
            plan=self._take(preview_id,'execute',confirm)
            with self._process_lock():
                self._state()
                for item in plan['items']:self._verify_item(item)
                batchid=time.strftime('%Y%m%dT%H%M%S',time.localtime(self.clock()))+'-'+uuid.uuid4().hex[:16]
                folder=self._path('trash/'+batchid);folder.mkdir()
                doc={'magic':MAGIC,'batch_id':batchid,'root_hash':_digest(str(self.root).encode()),
                     'created_at':self.clock(),'fingerprint_version':2,'status':'moving','selection':dict(plan['selection']),
                     'items':json.loads(json.dumps(plan['items']))}
                for item in doc['items']:item['state']='planned'
                self._save_batch(doc)
                index=self._index();index['batches'].append(batchid);self._write_doc(self._path('trash/'+INDEX),index)
                try:
                    for item in doc['items']:
                        self._state();self._verify_item(item)
                        source=self._path(item['path'])
                        item['state']='pending';self._save_batch(doc)
                        if item['action']=='move':
                            target=self._path('trash/'+batchid+'/'+item['path']);target.parent.mkdir(parents=True,exist_ok=True)
                            self._check_path(target)
                            if target.exists():_reject('trash_conflict')
                            self._move(doc,item,source,target,'out')
                        else:
                            raw=self._read(source)
                            updated=_Spans(raw).remove([f['path'] for f in item['fragments']])
                            self._atomic(source,updated)
                        item['state']='stored';self._save_batch(doc)
                    doc['status']='trashed';self._save_batch(doc)
                except Exception as error:
                    doc['status']='partial';self._save_batch(doc)
                    try:
                        self._restore_doc(doc,rollback=True)
                        code='operation_rolled_back'
                    except Exception:
                        code='rollback_incomplete'
                    message=('执行失败，已完整回滚；原数据已恢复。' if code=='operation_rolled_back' else
                             '执行失败且回滚未完成，仍有数据留在回收区；请使用“恢复上次清理”继续恢复。')
                    return {'ok':False,'error_code':code,'message':message,
                            'selection':dict(plan['selection']),
                            'batch_id':batchid,'cause':error.code if isinstance(error,CleanupError) else type(error).__name__}
                self._plans.pop(preview_id,None)
                return {'ok':True,'error_code':None,'batch_id':batchid,'trash_path':'trash/'+batchid,
                        'selection':dict(plan['selection']),
                        'items':[self._public_item(x) for x in doc['items']],'total_bytes':sum(x['bytes'] for x in doc['items'])}
        return self._guard(run)

    def _transfer_paths(self,doc,item):
        original=self._path(item['path']);archived=self._path('trash/'+doc['batch_id']+'/'+item['path'])
        source,target=(original,archived) if item['transfer']['direction']=='out' else (archived,original)
        stage=target.with_name('.xg-transfer-'+item['transfer']['id']+'-'+target.name)
        self._check_path(stage)
        return source,target,stage

    def _copy_rows(self,source,target,rows,*,missing=False):
        done=0;size=0;total=sum(r[2] for r in rows);count=sum(r[1]=='file' for r in rows)
        self._state();next_check=time.monotonic()+0.5
        def check_state():
            nonlocal next_check
            now=time.monotonic()
            if now>=next_check:self._state();next_check=now+0.5
        for row in rows:
            check_state();src=source/(row[0] if row[0]!='.' else '');dst=target/(row[0] if row[0]!='.' else '')
            self._check_entry(src.lstat());self._check_path(dst)
            if row[1]=='dir':dst.mkdir(exist_ok=missing)
            elif not (missing and dst.exists()):
                with src.open('rb') as reader,dst.open('xb') as writer:
                    remaining=row[2]
                    while remaining:
                        check_state();chunk=reader.read(min(COPY_CHUNK,remaining))
                        if not chunk:raise OSError('copy source shortened')
                        writer.write(chunk);remaining-=len(chunk);size+=len(chunk)
                        self._emit('copy','跨卷分块复制中',done,count,size,total)
                    if reader.read(1):raise OSError('copy source grew')
                    writer.flush();os.fsync(writer.fileno())
                os.utime(dst,ns=(row[3],row[3]))
            if row[1]=='file':done+=1
        for row in reversed(rows):
            if row[1]=='dir':os.utime(target/(row[0] if row[0]!='.' else ''),ns=(row[3],row[3]))
        self._state()
        self._emit('copy','跨卷副本计数与大小校验',count,count,total,total)

    def _remove_rows(self,base,rows):
        for row in reversed(rows):
            path=base/(row[0] if row[0]!='.' else '')
            self._check_path(path)
            if row[1]=='dir':path.rmdir()
            else:path.unlink()

    def _move(self,doc,item,source,target,direction):
        self._emit('move','正在同卷重命名',0,item['files'],0,item['bytes'])
        if target.exists():_reject('restore_conflict')
        try:os.rename(source,target)
        except OSError as error:
            if error.errno!=errno.EXDEV and getattr(error,'winerror',None)!=17:raise
        else:
            rows=self._tree(target.relative_to(self.root).as_posix(),policy_rel=item['path'])
            if not self._matches(rows,item['fingerprint']):_reject('move_changed','重命名后计数、大小或元数据变化，保留现场供恢复')
            self._emit('move','同卷重命名完成',item['files'],item['files'],item['bytes'],item['bytes']);return
        item['transfer']={'direction':direction,'id':uuid.uuid4().hex};self._save_batch(doc)
        source,target,stage=self._transfer_paths(doc,item)
        self._copy_rows(source,stage,item['fingerprint'])
        for path in (source,stage):
            rows=self._tree(path.relative_to(self.root).as_posix(),policy_rel=item['path'])
            if not self._matches(rows,item['fingerprint']):_reject('copy_changed')
        if target.exists():_reject('restore_conflict')
        self._state();os.rename(stage,target)
        # The full verified copy remains until all source entries are removed.
        self._state();self._remove_rows(source,item['fingerprint'])
        del item['transfer'];self._save_batch(doc)

    def _recover_transfer(self,doc,item):
        self._emit('rollback','恢复未完成的跨卷迁移')
        source,target,stage=self._transfer_paths(doc,item)
        expected=item['fingerprint']
        if target.exists():
            rows=self._tree(target.relative_to(self.root).as_posix(),policy_rel=item['path'])
            if not self._matches(rows,expected):_reject('restore_conflict')
            existing=self._tree(source.relative_to(self.root).as_posix(),policy_rel=item['path']) if source.exists() else []
            if not self._matches(existing,expected,subset=True):_reject('restore_conflict')
            self._copy_rows(target,source,expected,missing=True)
            if not self._matches(self._tree(source.relative_to(self.root).as_posix(),policy_rel=item['path']),expected):_reject('restore_conflict')
            self._remove_rows(target,rows)
        else:
            if not source.exists() or not self._matches(self._tree(source.relative_to(self.root).as_posix(),policy_rel=item['path']),expected):_reject('restore_conflict')
        if stage.exists():
            rows=self._tree(stage.relative_to(self.root).as_posix(),policy_rel=item['path'],partial=True)
            if not self._matches(rows,expected,subset=True,partial=True):_reject('restore_conflict')
            self._remove_rows(stage,rows)
        del item['transfer'];self._save_batch(doc)

    def _archive(self,doc):
        batch='trash/'+doc['batch_id'];allowed={MANIFEST};rows=[]
        for item in doc['items']:
            if item['action']!='move':continue
            rel=batch+'/'+item['path'];path=self._path(rel)
            if not path.exists():continue
            current=self._tree(rel)
            if not self._matches(current,item['fingerprint'],subset=doc['status']=='purging' or 'transfer' in item):
                _reject('trash_changed','回收内容发生变化，拒绝恢复/删除')
            for row in current:
                member=PurePosixPath(item['path'])/(row[0] if row[0]!='.' else '')
                allowed.add(member.as_posix())
                allowed.update(p.as_posix() for p in member.parents if str(p)!='.')
        for item in doc['items']:
            if 'transfer' not in item:continue
            source,target,stage=self._transfer_paths(doc,item)
            for location,partial in ((source,False),(target,False),(stage,True)):
                if not location.exists():continue
                rel=location.relative_to(self.root).as_posix()
                current=self._tree(rel,policy_rel=item['path'],partial=partial)
                if not self._matches(current,item['fingerprint'],subset=True,partial=partial):_reject('trash_changed')
                for row in current:
                    member=location/(row[0] if row[0]!='.' else '')
                    rows.append(['transfer:'+member.relative_to(self.root).as_posix(),*row[1:4]])
                    if member.is_relative_to(self.root/batch):
                        m=member.relative_to(self.root/batch);allowed.add(m.as_posix())
                        allowed.update(p.as_posix() for p in m.parents if str(p)!='.')
        folder=self._path(batch)
        pending=[folder] if folder.exists() else []
        while pending:
            directory=pending.pop();self._check_path(directory)
            for path in directory.iterdir():
                info=path.lstat();self._check_entry(info)
                rel=path.relative_to(folder).as_posix();is_dir=stat.S_ISDIR(info.st_mode)
                if rel not in allowed:
                    # 恢复成功后可留下只含目录的树；不遍历未登记的陌生目录。
                    structural=any(item['action']=='move' and PurePosixPath(rel) in PurePosixPath(item['path']).parents for item in doc['items'])
                    if not (is_dir and structural):_reject('untrusted_trash','回收目录含非本服务内容')
                rows.append([rel,'dir' if is_dir else 'file',info.st_size,info.st_mtime_ns])
                if is_dir:pending.append(path)
        return sorted(rows)

    def _restore_doc(self,doc,*,rollback=False):
        self._archive(doc)
        for item in doc['items']:
            if 'transfer' in item:self._recover_transfer(doc,item)
        # Preflight every destination before moving anything back.
        operations=[]
        for item in reversed(doc['items']):
            if item.get('state')=='restored':continue
            dest=self._path(item['path'])
            if item['action']=='move':
                source=self._path('trash/'+doc['batch_id']+'/'+item['path'])
                if source.exists():
                    if dest.exists():_reject('restore_conflict','原位置已生成新文件，拒绝覆盖',item['path'])
                    operations.append((item,source,dest,None))
                elif item.get('state') in ('stored','pending'):
                    # Crash after a restore rename but before its journal commit.
                    if not dest.exists() or not self._matches(self._tree(item['path']),item['fingerprint']):_reject('restore_conflict','恢复源缺失')
            else:
                if not dest.is_file():_reject('restore_conflict','混合登记文件缺失，不重建凭据配置')
                spans=_Spans(self._read(dest))
                if rollback or item.get('state') in ('planned','restore_pending'):
                    updated=spans.restore(item['fragments'],allow_existing=True)
                else:
                    updated=spans.restore(item['fragments'])
                operations.append((item,None,dest,updated))
        try:
            doc['status']='restoring';self._save_batch(doc)
            for item,source,dest,updated in operations:
                self._state()
                previous_state=item.get('state')
                item['state']='restore_pending';self._save_batch(doc)
                if source:
                    dest.parent.mkdir(parents=True,exist_ok=True);self._check_path(dest)
                    if dest.exists():_reject('restore_conflict')
                    self._move(doc,item,source,dest,'back')
                else:
                    # Recompute immediately before writing; unrelated fields remain current.
                    updated=_Spans(self._read(dest)).restore(item['fragments'],allow_existing=rollback or previous_state in ('planned','restore_pending'))
                    self._atomic(dest,updated)
                item['state']='restored';self._save_batch(doc)
            doc['status']='restored';self._save_batch(doc)
        except Exception:
            doc['status']='partial_restore';self._save_batch(doc)
            raise

    def restore_preview(self):
        def run():
            docs=[self._batch(x) for x in self._index()['batches']]
            if docs and docs[-1]['status']=='purging':
                _reject('permanently_deleted_partial','上次永久删除已开始；剩余内容可继续清空，已删除内容不可完整恢复')
            docs=[d for d in docs if d['status'] in ('trashed','partial','moving','restoring','partial_restore')]
            if not docs:_reject('nothing_to_restore','没有可恢复的清理记录')
            doc=docs[-1];signature=self._archive(doc)
            token,plan=self._plan('restore',batch_id=doc['batch_id'],signature=signature)
            return {'ok':True,'error_code':None,'plan_id':token,'preview_id':token,'expires_at':plan['expires_at'],
                    'selection':dict(doc.get('selection',{})),
                    'batch_id':doc['batch_id'],'items':[self._public_item(x) for x in doc['items']],
                    'total_bytes':sum(x['bytes'] for x in doc['items']),
                    'total_files':sum(x['files'] for x in doc['items'])}
        return self._guard(run)

    def restore(self,plan_id,confirm=False):
        def run():
            plan=self._take(plan_id,'restore',confirm)
            with self._process_lock():
                self._state();doc=self._batch(plan['batch_id'])
                if self._archive(doc)!=plan['signature']:_reject('preview_stale')
                self._restore_doc(doc)
                self._plans.pop(plan_id,None)
                return {'ok':True,'error_code':None,'batch_id':doc['batch_id'],'restored':True,
                        'selection':dict(doc.get('selection',{}))}
        return self._guard(run)

    def purge_preview(self):
        def run():
            docs=[self._batch(x) for x in self._index()['batches']]
            if any('transfer' in i for d in docs for i in d['items']):_reject('transfer_incomplete','跨卷迁移未完成，请先恢复上次清理')
            signatures={d['batch_id']:self._archive(d) for d in docs}
            if not docs:_reject('nothing_to_purge','没有本服务创建的回收记录')
            token,plan=self._plan('purge',signatures=signatures)
            return {'ok':True,'error_code':None,'plan_id':token,'preview_id':token,'expires_at':plan['expires_at'],
                    'batches':[d['batch_id'] for d in docs],'items':[{'path':'trash/'+d['batch_id'],'action':'purge',
                        'category':'trash','files':sum(row[1]=='file' for row in signatures[d['batch_id']]),
                        'bytes':sum(row[2] for row in signatures[d['batch_id']] if row[1]=='file')} for d in docs],
                    'total_bytes':sum(row[2] for rows in signatures.values() for row in rows if row[1]=='file'),
                    'total_files':sum(row[1]=='file' for rows in signatures.values() for row in rows),
                    'notice':'永久删除，仅限本服务创建且未被修改的回收内容；不可恢复'}
        return self._guard(run)

    def purge(self,plan_id,confirm=False):
        def run():
            plan=self._take(plan_id,'purge',confirm)
            with self._process_lock():
                self._state();index=self._index()
                for batchid,signature in plan['signatures'].items():
                    if batchid not in index['batches'] or self._archive(self._batch(batchid))!=signature:_reject('preview_stale')
                removed=[];files_done=0;bytes_done=0
                self._emit('purge','正在删除已确认回收内容')
                for batchid in plan['signatures']:
                    self._state();folder=self._path('trash/'+batchid)
                    doc=self._batch(batchid);self._archive(doc)
                    if index.get('retiring')!=batchid:
                        doc['status']='purging';self._save_batch(doc)
                    try:
                        # 绝不重新遍历并删除目录中的全部项目；预览后新增文件属于用户。
                        rows=plan['signatures'][batchid]
                        for rel,kind,size,mtime in sorted(rows,key=lambda row:row[0].count('/'),reverse=True):
                            if rel==MANIFEST:continue
                            path=self._path('trash/'+batchid+'/'+rel)
                            info=path.lstat();self._check_entry(info)
                            if kind=='dir' and stat.S_ISDIR(info.st_mode):path.rmdir()
                            elif kind=='file' and stat.S_ISREG(info.st_mode) and (size,mtime)==(info.st_size,info.st_mtime_ns):
                                path.unlink();bytes_done+=size;files_done+=1
                                if files_done%128==0:self._emit('purge','正在删除已确认回收内容',files_done,None,bytes_done,None)
                            else:_reject('trash_changed','待清空项目已变化，保留现场')
                        manifest=folder/MANIFEST;self._check_path(manifest)
                        if folder.exists() and any(c.name!=MANIFEST for c in folder.iterdir()):_reject('untrusted_trash')
                        if index.get('retiring')!=batchid:
                            index['retiring']=batchid;self._write_doc(self._path('trash/'+INDEX),index)
                        if manifest.exists():manifest.unlink()
                        if folder.exists():folder.rmdir()
                        index['batches'].remove(batchid);index.pop('retiring',None);self._write_doc(self._path('trash/'+INDEX),index)
                        removed.append(batchid)
                    except OSError:
                        return {'ok':False,'error_code':'purge_partial','message':'永久删除部分完成；已删除内容不可恢复，剩余内容可重新预览后继续删除',
                                'batch_id':batchid,'purged':removed}
                self._plans.pop(plan_id,None)
                return {'ok':True,'error_code':None,'purged':removed}
        return self._guard(run)

"""Offline, preview-bound cleanup of generated data. No account discovery.

Only the explicit roots below are eligible. Mixed registration JSON is edited
by member spans: only removed member values enter the recovery journal, never
the surrounding connection/profile document. All public results omit values.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
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
MAX_JSON = 32 * 1024 * 1024
SENSITIVE = re.compile(r'(?i)(?:^|[_.\- ])(?:secrets?|credentials?|passwords?|passwd|passphrase|'
    r'tokens?|cookies?|sessions?|session3|login|accounts?|ai[_-]?keys?|api[_-]?keys?|'
    r'authorization|private[_-]?keys?|personas?|memories|memory|owner[_-]?uuid|owner[_-]?binding)(?:$|[_.\- ])')
SECRET_KEYS = frozenset(('password','passwd','passphrase','secret','secrets','credential','credentials',
    'apikey','aikey','accesskey','accesskeyid','accesskeysecret','accesstoken','refreshtoken','authtoken',
    'logintoken','token','cookie','cookies','authorization','privatekey','session','session3','sessionkey',
    'owneruuid','ownerbinding','persona','personas','memory','chatmemory','chat history','accounts'))
EXTENSIONS = frozenset(('.json','.bin','.f32','.u32','.i32','.f64','.u16','.u8','.nav','.navmesh',
    '.mesh','.npz','.npy','.gz','.zst','.obj','.mtl','.dat'))
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


class CleanupService:
    def __init__(self, data_root, *, is_online, is_busy, clock=time.time, preview_ttl=120):
        root=Path(data_root)
        if not root.is_absolute() or root == Path(root.anchor):
            _reject('invalid_data_root','data 根必须是明确的绝对目录')
        self.root=root
        self.clock=clock
        self.ttl=max(1,min(int(preview_ttl),300))
        self.is_online,self.is_busy=is_online,is_busy
        self._plans={}
        self._lock=threading.RLock()
        self._check_path(root)
        if not root.is_dir():
            _reject('invalid_data_root','data 根不存在')
        self.root=root.resolve()

    def _check_path(self,path):
        path=Path(path)
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
        if not isinstance(relative,str) or '\\' in relative or ':' in relative:
            _reject('unsafe_path')
        parts=PurePosixPath(relative).parts
        if not parts or any(p in ('','.','..') for p in parts) or relative.startswith('/'):
            _reject('unsafe_path')
        path=self.root.joinpath(*parts)
        self._check_path(path)
        if not path.resolve().is_relative_to(self.root):
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
        with path.open('rb') as stream:
            return stream.read(MAX_JSON+1)

    def _atomic(self,path,raw):
        self._check_path(path)
        path.parent.mkdir(parents=True,exist_ok=True)
        self._check_path(path.parent)
        temp=path.with_name('.cleanup-'+uuid.uuid4().hex+'.tmp')
        try:
            with temp.open('xb') as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
            self._check_path(path)
            os.replace(temp,path)
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
            return rel in (RELEASE+'/assets',RELEASE+'/terrain',RELEASE+'/multimap-catalog.json')
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
        if selection['catalog']:
            found.append(('catalog',RELEASE+'/asset-catalog-v32c'))
            accounts=self._path(RELEASE+'/asset-catalog-acct')
            if accounts.exists():
                for path in sorted(accounts.iterdir()):
                    if not ACCOUNT_FOLDER.fullmatch(path.name):
                        _reject('unknown_generated_path','按号目录包含未知项目',str(path.relative_to(self.root)))
                    found.append(('catalog',RELEASE+'/asset-catalog-acct/'+path.name))
        return [(category,rel) for category,rel in found if self._path(rel).exists()]

    def _tree(self,rel,*,check_sensitive=True):
        base=self._path(rel)
        rows=[]
        def visit(path):
            self._check_path(path)
            info=path.lstat()
            local=path.relative_to(base).as_posix()
            if check_sensitive and (SENSITIVE.search(path.name) or path.name.lower() in ('.env','config.json','settings.json')):
                _reject('sensitive_data','候选子树含受保护文件，整项拒绝',str(path.relative_to(self.root)))
            if stat.S_ISDIR(info.st_mode):
                rows.append([local,'dir',0,info.st_mtime_ns])
                for child in sorted(path.iterdir()):visit(child)
            elif stat.S_ISREG(info.st_mode):
                suffix=path.suffix.lower()
                if suffix=='.pending':suffix=Path(path.stem).suffix.lower()
                if check_sensitive and suffix not in EXTENSIONS and path.name.lower() not in TEXT_FILES:
                    _reject('unknown_generated_file','候选子树含非白名单文件，整项拒绝',str(path.relative_to(self.root)))
                digest=hashlib.sha256()
                carry=b''
                with path.open('rb') as stream:
                    while chunk:=stream.read(1024*1024):
                        digest.update(chunk)
                        if check_sensitive and (re.search(rb'-----BEGIN [A-Z ]*PRIVATE KEY-----',carry+chunk)
                            or re.search(rb'(?i)["\x27](?:api[_-]?key|password|passwd|session3|access[_-]?token|refresh[_-]?token|private[_-]?key)["\x27]\s*:',carry+chunk)):
                            _reject('sensitive_data','候选子树含敏感内容，整项拒绝')
                        carry=chunk[-256:]
                if check_sensitive and suffix=='.json':
                    if _secret(_json(self._read(path))):
                        _reject('sensitive_data','候选JSON含受保护字段，整项拒绝',str(path.relative_to(self.root)))
                rows.append([local,'file',info.st_size,info.st_mtime_ns,digest.hexdigest()])
            else:_reject('unsafe_file_type')
        visit(base)
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
        if (not isinstance(doc,dict) or set(doc)!={'magic','batches'} or doc.get('magic')!=MAGIC
            or not isinstance(doc.get('batches'),list) or _secret(doc)):_reject('untrusted_trash')
        if len(doc['batches'])>10000 or any(not isinstance(x,str) or not BATCH.fullmatch(x) for x in doc['batches']):_reject('untrusted_trash')
        return doc

    def _batch(self,batchid):
        if not isinstance(batchid,str) or not BATCH.fullmatch(batchid):_reject('untrusted_trash')
        path=self._path('trash/'+batchid+'/'+MANIFEST)
        doc=_json(self._read(path))
        if (not isinstance(doc,dict) or set(doc)!={'magic','batch_id','root_hash','created_at','status','selection','items'}
            or doc.get('magic')!=MAGIC or doc.get('batch_id')!=batchid or _secret(doc)
            or doc.get('root_hash')!=_digest(str(self.root).encode()) or not isinstance(doc.get('items'),list)
            or doc.get('status') not in ('moving','trashed','partial','restoring','partial_restore','restored','purging')
            or not isinstance(doc.get('selection'),dict) or set(doc['selection']) not in (set(CATEGORIES),LEGACY_CATEGORIES)
            or any(type(x)is not bool for x in doc['selection'].values())):_reject('untrusted_trash')
        # v1 的四类回收记录仍可恢复；新类别缺省关闭，不扩大旧批次范围。
        doc['selection']={key:doc['selection'].get(key,False) for key in CATEGORIES}
        for item in doc['items']:
            expected={'category','path','action','fingerprint','bytes','files','state'}
            if item.get('action')=='json_members':expected|={'kind','fragments'}
            if set(item)!=expected or item.get('state') not in ('planned','pending','stored','restore_pending','restored'):_reject('untrusted_trash')
            if item.get('action')=='move':
                if not self._allowed(item.get('path',''),item.get('category')):_reject('untrusted_trash')
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
            if not path.exists() or self._tree(item['path'])!=item['fingerprint']:_reject('preview_stale','目录、大小或内容已变化，请重新预览')
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
                     'created_at':self.clock(),'status':'moving','selection':dict(plan['selection']),
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
                            os.rename(source,target)
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

    def _archive(self,doc):
        batch='trash/'+doc['batch_id'];allowed={MANIFEST};rows=[]
        for item in doc['items']:
            if item['action']!='move':continue
            rel=batch+'/'+item['path'];path=self._path(rel)
            if not path.exists():continue
            current=self._tree(rel)
            if doc['status']=='purging':
                expected={row[0]:row for row in item['fingerprint']}
                # A confirmed permanent-delete may have stopped midway. Missing
                # entries are expected; extra/changed remaining files are not.
                if any(row[0] not in expected or (row[1]=='file' and row!=expected[row[0]])
                       or row[1]!=expected[row[0]][1] for row in current):_reject('trash_changed')
            elif current!=item['fingerprint']:_reject('trash_changed','回收内容发生变化，拒绝恢复/删除')
            for row in current:
                member=PurePosixPath(item['path'])/(row[0] if row[0]!='.' else '')
                allowed.add(member.as_posix())
                allowed.update(p.as_posix() for p in member.parents if str(p)!='.')
        folder=self._path(batch)
        for current,dirs,files in os.walk(folder,followlinks=False):
            for name in dirs+files:
                path=Path(current)/name;self._check_path(path)
                rel=path.relative_to(folder).as_posix()
                if rel not in allowed:
                    # Empty structural parents can remain after successful restore.
                    structural=any(item['action']=='move' and PurePosixPath(rel) in PurePosixPath(item['path']).parents for item in doc['items'])
                    if not (path.is_dir() and structural):_reject('untrusted_trash','回收目录含非本服务内容')
                info=path.stat();rows.append([rel,'dir' if path.is_dir() else 'file',info.st_size,info.st_mtime_ns])
        return sorted(rows)

    def _restore_doc(self,doc,*,rollback=False):
        self._archive(doc)
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
                    if not dest.exists() or self._tree(item['path'])!=item['fingerprint']:_reject('restore_conflict','恢复源缺失')
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
                    os.rename(source,dest)
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
                removed=[]
                for batchid in plan['signatures']:
                    self._state();folder=self._path('trash/'+batchid)
                    doc=self._batch(batchid);self._archive(doc)
                    doc['status']='purging';self._save_batch(doc)
                    try:
                        # Checked individual files only, never rmtree(data/trash).
                        for current,dirs,files in os.walk(folder,topdown=False,followlinks=False):
                            for name in files:
                                path=Path(current)/name
                                if path.name==MANIFEST:continue
                                self._check_path(path);path.unlink()
                            for name in dirs:
                                path=Path(current)/name;self._check_path(path);path.rmdir()
                        self._check_path(folder/MANIFEST);(folder/MANIFEST).unlink();folder.rmdir()
                        removed.append(batchid);index['batches'].remove(batchid);self._write_doc(self._path('trash/'+INDEX),index)
                    except OSError:
                        return {'ok':False,'error_code':'purge_partial','message':'永久删除部分完成；已删除内容不可恢复，剩余内容可重新预览后继续删除',
                                'batch_id':batchid,'purged':removed}
                self._plans.pop(plan_id,None)
                return {'ok':True,'error_code':None,'purged':removed}
        return self._guard(run)

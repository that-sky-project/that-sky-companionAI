"""Generate optional collision candidates from a user-selected local APK.

No catalog, source mesh, reviewed geometry array, APK, or account data is bundled.
Unknown APK identities fail closed; generated data belongs to the user's versioned data directory.
"""
from __future__ import annotations
from pathlib import Path
from collections import Counter
import hashlib
import json
import os
import shutil
import tempfile
import uuid
import zipfile

BUILD = 421629
VERSION = '0.16.8'
SCHEMA = 'xg-props-generation-v1'

class PropGenerationError(RuntimeError):
    def __init__(self, code, message=None):
        self.code = str(code)
        super().__init__(message or self.code)

def _sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block:=stream.read(8*1024*1024):h.update(block)
    return h.hexdigest()

def _local(path):
    raw=Path(path).absolute()
    if str(raw).startswith(('\\\\','//')):
        raise PropGenerationError('props_local_path_required')
    for component in (raw,*raw.parents):
        if component.is_symlink() or (hasattr(component,'is_junction') and component.is_junction()):
            raise PropGenerationError('props_link_rejected')
    return raw.resolve()

def _write(path,doc):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.tmp')
    raw=(json.dumps(doc,ensure_ascii=False,separators=(',',':'),allow_nan=False)+'\n').encode('utf8')
    with temporary.open('wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
    os.replace(temporary,path)
    return hashlib.sha256(raw).hexdigest()

def validate_generated(output_dir, expected_build=None):
    """Read-only validation shared with the runtime, including every shard pin."""
    result=dict(ready=False,count=0,known=0,code='props_missing',message='尚未生成本版本道具',warnings=[])
    try:
        root=_local(output_dir)
        from skyterrain.props import load_generated_catalog
        catalog=load_generated_catalog(root,expected_build=BUILD if expected_build is None else expected_build)
        marker=json.loads((root/'generation.json').read_text(encoding='utf8'))
        # load_catalog returns an internal store, not the public index.
        index=json.loads((root/'catalog.json').read_text(encoding='utf8'))
        known=len(index['props']);count=sum(v.get('status')=='ready' for v in index['props'].values())
        counts=marker.get('counts',{})
        if (type(counts.get('ready')) is not int or type(counts.get('known')) is not int
                or counts['ready']!=count or counts['known']!=known):
            raise ValueError('props_generated_counts_mismatch')
        result.update(ready=True,count=count,known=known,code='ready',message='本机道具目录已生成并校验',warnings=marker.get('warnings',[]))
    except (OSError,ValueError,TypeError,KeyError,PropGenerationError) as exc:
        result.update(code=getattr(exc,'code',str(exc)),message='道具尚未完成或已损坏，需从本机 APK 重新生成')
    return result

def _build(apk, scratch, progress):
    from .prop_build.extract import build_all_index
    from .prop_build.budget import extend
    from .prop_build.rules import POLICY
    progress(dict(stage='hash',message='正在校验 APK 来源'))
    stat=apk.stat();identity=dict(sha256=_sha(apk),bytes=stat.st_size)
    if identity['sha256']!=POLICY['apk_sha256']:
        raise PropGenerationError('unsupported_apk_geometry_policy','当前 APK 不匹配已审核道具规则，保留现有道具数据')
    with zipfile.ZipFile(apk) as z:
        if len(z.infolist())>100000:raise PropGenerationError('apk_entry_budget')
        build_all_index(z,identity,scratch/'all',primary_index=scratch/'catalog.json',progress=progress)
    if apk.stat().st_size!=stat.st_size or apk.stat().st_mtime_ns!=stat.st_mtime_ns:
        raise PropGenerationError('apk_changed_during_generation')
    index=json.loads((scratch/'catalog.json').read_text(encoding='utf8'))
    index=extend(scratch,index,progress=progress)
    statuses=dict(Counter(row['status'] for row in index['props'].values()))
    ready=statuses.get('ready',0);known=len(index['props'])
    if ready!=174 or known!=522:
        raise PropGenerationError('reviewed_ready_set_count_mismatch')
    # Only runtime shards and public index are promoted. Never promote raw/review arrays.
    index.pop('manifest',None)
    index['counts'].update(ready=ready,known=known,runtime_status=statuses,runtime_eligible_names=ready,
        runtime_shard_bytes=sum(r['byte_length'] for r in index['props'].values()),
        runtime_vertices=sum(r['vertex_count'] for r in index['props'].values()),
        runtime_triangles=sum(r['triangle_count'] for r in index['props'].values()))
    index['generation_scope']='local APK render-main candidates, not native physics or live placement proof'
    return index,identity

def _remove_tree(path,parent):
    path=_local(path);parent=_local(parent)
    if path==parent or not path.is_relative_to(parent):raise PropGenerationError('props_cleanup_scope')
    if path.exists():shutil.rmtree(path)

def _target_token(target):
    """Only a managed artifact directory may be replaced; detect intervening edits."""
    _local(target)
    if not target.exists():return None
    if not target.is_dir():raise PropGenerationError('props_output_not_directory')
    entries=list(target.rglob('*'))
    if not entries:return 'empty'
    try:managed=json.loads((target/'generation.json').read_text(encoding='utf8')).get('schema')==SCHEMA
    except (OSError,ValueError,AttributeError):managed=False
    if not managed:raise PropGenerationError('props_unmanaged_output_rejected')
    try:
        index=json.loads((target/'catalog.json').read_text(encoding='utf8'))
        allowed={'catalog.json','generation.json'}
        for name,row in index['props'].items():
            if row.get('status')=='ready':
                rel=row.get('shard')
                if rel!=f'all/runtime/{name}.json':raise ValueError('unmanaged shard')
                allowed.add(rel)
    except (OSError,ValueError,KeyError,TypeError,AttributeError):
        raise PropGenerationError('props_unmanaged_output_rejected','道具索引已损坏或目录含非托管文件，请先清理道具后重建') from None
    records=[]
    for path in entries:
        _local(path)
        rel=path.relative_to(target).as_posix()
        if path.is_dir():
            if rel not in ('all','all/runtime'):raise PropGenerationError('props_unmanaged_output_rejected')
            continue
        if rel not in allowed:
            raise PropGenerationError('props_unmanaged_output_rejected')
        stat=path.stat();records.append((rel,stat.st_size,stat.st_mtime_ns))
    return hashlib.sha256(json.dumps(sorted(records)).encode()).hexdigest()

def _publish(stage,target,*,expected_target=None):
    """Validate before calling. Rename old/new directories; rollback on any failed promotion."""
    parent=target.parent;backup=parent/(target.name+'.previous-'+uuid.uuid4().hex)
    old=False
    try:
        _local(target);_local(stage)
        if _target_token(target)!=expected_target:raise PropGenerationError('props_output_changed_during_generation')
        if target.exists():os.replace(target,backup);old=True
        os.replace(stage,target)
    except BaseException:
        if old and not target.exists():os.replace(backup,target)
        raise
    if old:
        try:_remove_tree(backup,parent)
        except Exception:return '旧道具目录备份清理失败：'+backup.name+'；新完整目录已提交。'
    return None

def _acquire_lock(lock, workspace):
    # A terminated task must not permanently poison generation. An unreadable or
    # live owner remains busy; never signal/terminate a process from this helper.
    import psutil
    token=uuid.uuid4().hex
    body=dict(pid=os.getpid(),create_time=psutil.Process().create_time(),token=token,workspace=workspace.name)
    for attempt in range(2):
        try:
            fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
        except FileExistsError:
            try:
                with lock.open('rb') as f:raw=f.read(4097)
                if len(raw)>4096:raise ValueError('lock budget')
                old=json.loads(raw);pid=old['pid'];created=old['create_time']
                if type(pid) is not int or pid<=0 or type(created) not in (int,float):raise ValueError('lock identity')
                try:stale=abs(psutil.Process(pid).create_time()-created)>.01
                except psutil.NoSuchProcess:stale=True
                except (psutil.AccessDenied,OSError):stale=False
                if not stale or lock.read_bytes()!=raw:raise ValueError('live or changed')
                previous=old.get('workspace','')
                if (isinstance(previous,str) and Path(previous).name==previous
                        and previous.startswith('.'+lock.name.removesuffix('.generation.lock')+'-build-')):
                    _remove_tree(lock.parent/previous,lock.parent)
                lock.unlink()
            except (OSError,ValueError,KeyError,TypeError):
                raise PropGenerationError('props_generation_busy') from None
            continue
        else:
            with os.fdopen(fd,'w',encoding='utf8') as f:
                json.dump(body,f);f.flush();os.fsync(f.fileno())
            return token
    raise PropGenerationError('props_generation_busy')

def generate(apk_path,output_dir=None,*,progress=None):
    """Build in a sibling staging directory and publish only a complete checked catalog."""
    if output_dir is None:
        import xg_paths
        output_dir=xg_paths.PROP_ASSETS
    apk=_local(apk_path);target=_local(output_dir)
    if not apk.is_file():raise PropGenerationError('apk_not_found')
    if apk==target or apk.is_relative_to(target):raise PropGenerationError('props_output_contains_input')
    target.parent.mkdir(parents=True,exist_ok=True)
    _local(target.parent)
    expected_target=_target_token(target)
    lock=target.parent/(target.name+'.generation.lock')
    workspace_path=target.parent/('.'+target.name+'-build-'+uuid.uuid4().hex)
    token=_acquire_lock(lock,workspace_path)
    workspace=None
    cleanup_warnings=[]
    callback=progress if callable(progress) else lambda event:None
    try:
        workspace=workspace_path;workspace.mkdir()
        scratch=workspace/'scratch';scratch.mkdir()
        index,identity=_build(apk,scratch,callback)
        publish=workspace/'publish';publish.mkdir()
        for name,row in index['props'].items():
            if row['status']!='ready':continue
            path=Path(row['shard'])
            if path.is_absolute() or '..' in path.parts or path.as_posix()!=f'all/runtime/{name}.json':
                raise PropGenerationError('props_generated_shard_path')
            dest=publish/path;dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(scratch/path,dest)
        catalog_sha=_write(publish/'catalog.json',index)
        counts=dict(ready=sum(r['status']=='ready' for r in index['props'].values()),known=len(index['props']))
        warnings=cleanup_warnings
        warnings.extend(['仅渲染主网格候选；非原生碰撞认证，运行时仍受姿态、缩放、误差和总预算门限制。',
            '其余道具按歧义、动画、未验证部件或几何预算等原因保持禁用。'])
        marker=dict(schema=SCHEMA,status='complete',build=BUILD,version=VERSION,apk_sha256=identity['sha256'],
            counts=counts,ready=counts['ready'],known=counts['known'],catalog_sha256=catalog_sha,warnings=warnings)
        _write(publish/'generation.json',marker)
        callback(dict(stage='verify',message='正在校验全部运行时分片'))
        checked=validate_generated(publish,expected_build=BUILD)
        if not checked['ready']:raise PropGenerationError(checked['code'])
        backup_warning=_publish(publish,target,expected_target=expected_target)
        if backup_warning:warnings.append(backup_warning)
        try:callback(dict(stage='complete',**counts))
        except Exception:warnings.append('完成进度回调失败；完整目录已成功提交。')
        return dict(ok=True,catalog=str(target/'catalog.json'),schema=index['schema'],**counts,counts=counts,
            source_apk_sha256=identity['sha256'],warnings=warnings)
    except PropGenerationError:raise
    except Exception as exc:
        raise PropGenerationError('props_generation_failed',str(exc)) from exc
    finally:
        try:
            if workspace is not None:_remove_tree(workspace,target.parent)
        except Exception:
            warning='临时提取目录清理失败：'+workspace.name+'；完整目录提交状态不变。'
            cleanup_warnings.append(warning)
            try:callback(dict(stage='cleanup_warning',message=warning))
            except Exception:pass
        finally:
            try:
                if json.loads(lock.read_text(encoding='utf8')).get('token')==token:lock.unlink()
            except (OSError,ValueError):pass

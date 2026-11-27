"""本地巡游指令预览。只解释文字并选取路线；不连接服务、不启动执行器、不改状态。"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import re
import sys

_COMMANDS = {
    'start': ('巡游', '开始巡游', '带我巡游', '带我跑图', '巡游吧', '去巡游'),
    'all': ('巡游全部', '全部巡游', '巡游所有图', '全图巡游'),
    'stop': ('停止巡游', '结束巡游', '不巡游了', '别巡游了', '巡游结束'),
    'pause': ('暂停巡游', '巡游暂停', '先停一下巡游'),
    'resume': ('继续巡游', '巡游继续', '接着巡游'),
    'skip': ('跳过', '跳过这张', '跳过这张图', '下一张', '下一张图', '换下一张'),
    'status': ('巡游到哪了', '巡游状态', '巡游进度', '到哪了'),
}
_SOFT = {'pause': ('停下', '停一下', '先停下', '停', '等一下', '等等', '等等我', '先等等'),
         'resume': ('继续跑', '接着跑', '继续走', '接着走', '继续', '走吧')}
_FROM = re.compile(r'^(?:从|由)(?P<name>.{1,24}?)(?:开始)?(?:巡游|跑图)(?P<all>全部)?$|^巡游(?P<all2>全部)?从(?P<name2>.{1,24}?)开始$')
_TAIL = re.compile(r'[\s～~!！。.…?？,，吧呀啦嘛哦呢]+$')
_LABELS = dict(start='开始巡游',pause='暂停',resume='继续',stop='结束',skip='下一张',status='查询进度')


def parse_command(content):
    """保留服务端软暂停/软继续的区别；兼容直接输入斜杠指令。"""
    if not isinstance(content,str) or len(content) > 200:
        return None
    text = re.sub(r'^(?:小光|光光)[\s，,、：:]*', '', _TAIL.sub('',content.strip()))
    if text.startswith(('/','／')):
        text = text[1:].strip()
    if not text or len(text)>40:
        return None
    for op, words in _COMMANDS.items():
        if text in words:
            if op in ('start','all'):
                return 'start', {'all':op=='all','from':None}
            return op,None
    found=_FROM.fullmatch(text)
    if found:
        place=(found['name'] or found['name2']).strip()
        if place:
            return 'start',{'all':bool(found['all'] or found['all2']),'from':place}
    for op,words in _SOFT.items():
        if text in words:
            return op,'soft'
    return None


def _key(name):
    return re.sub(r'[\s·・:：()（）\-_]+','',name).casefold() if isinstance(name,str) else ''


def preview(content, table):
    """返回可预览的地图列表。活动图为候选，不判断线上活动，也不进行移动。"""
    cmd=parse_command(content)
    if cmd is None:
        return {'错误':'未识别指令'}
    op,arg=cmd
    if op!='start':
        return {'指令':_LABELS[op],'软指令':arg=='soft'}
    rows=table['maps']
    def selected(m):
        return arg['all'] or m.get('default',True) or m.get('auto_when_event',False)
    available=[m for m in rows if selected(m)]
    name=arg['from']
    if name:
        k=_key(name)
        if not k:
            return {'错误':'未找到起点'}
        def names(m,with_region):
            fields=('scene','label','travel_query','region') if with_region else ('scene','label','travel_query')
            return [_key(m.get(f)) for f in fields]
        # 精确地图名优先于地区前缀，避免“晨岛试验图”被相似标签截走。
        first=next((m for m in rows if k in names(m,False)),None)
        if first is None:
            first=next((m for m in available if any(k==v or (len(k)>=2 and k in v)
                                                    for v in names(m,True))),None)
        if first is None:
            return {'错误':'未找到起点'}
        if not selected(first):
            available=[m for m in rows if m is first or selected(m)]
        available=available[available.index(first):]
    return {'指令':'开始巡游','地图数':len(available),'路径点数':sum(len(m['waypoints']) for m in available),
            '起点':(available[0].get('label') or available[0]['scene']) if available else None,
            '活动候选数':sum(m.get('auto_when_event') is True for m in available)}


def main(argv=None):
    # 便携 Python 的 ._pth 可能没有脚本目录。固定从同目录读取路径模块。
    spec=importlib.util.spec_from_file_location('_tour_preview_paths',Path(__file__).with_name('xg_paths.py'))
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    for stream in (sys.stdout,sys.stderr):
        mod.safe_console(stream)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',metavar='指令',help='如 /巡游、/停下、/从晨岛开始巡游')
    args=p.parse_args(argv)
    value=preview(args.command,mod.read_tour_routes())
    def display(v):
        if type(v) is bool:
            return '是' if v else '否'
        return '无' if v is None else str(v)
    print('仅预览：'+'；'.join(f'{k}={display(v)}' for k,v in value.items()))
    return int('错误' in value)


if __name__=='__main__':
    raise SystemExit(main())

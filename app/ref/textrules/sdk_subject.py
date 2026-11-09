"""Prepare a UTF-8 subject for the 0.16.7 CN SDK's compiled rules, offline.

Derived from the Java call sites of the paired DEX, not from probing other players.
The three optional normalizations below default to true in this SDK but can be
overridden by app settings. Python and Android Unicode database versions can differ.
"""
from pathlib import Path
import json,unicodedata
from functools import lru_cache

@lru_cache(maxsize=1)
def _t2s():
    return json.loads(Path(__file__).with_name('t2s.json').read_text(encoding='utf-8'))

def format_content(text,*,un=True,t2s=True,f2h=True):
    if not isinstance(text,str):raise TypeError('text must be str')
    text.encode('utf-8',errors='strict')  # SDK rejects invalid UTF-8 text.
    if un:
        result=[]
        for ch in text:
            normalized=unicodedata.normalize('NFKC',ch)
            result.append(normalized if len(normalized)<=1 else ch)
        text=''.join(result)
    if t2s:
        m=_t2s();text=''.join(m.get(ch,ch) for ch in text)
    if f2h:
        text=''.join(' ' if ch=='\u3000' else chr(ord(ch)-65248) if 65280<ord(ch)<65375 else ch for ch in text)
    return ''.join(chr(ord(c)+1) if 0x4e00<=ord(c)<=0x9fff else c for c in text)

def make_subject(text,group,*,level='1',channel='chat_max',**flags):
    """Return the actual search subject for the group; send to PCRE2 as UTF-8."""
    formatted=format_content(text,**flags)
    if group in ('replace','nickname'):return formatted
    if group not in ('shield','intercept','remind'):raise ValueError('unknown group')
    return 'level='+str(level)+'_channel='+str(channel)+'_content='+formatted

"""中央神坛配对的 outer 2 有界诊断；不返回正文、散列、凭据、原始数字或任意词。"""
import hashlib
import re
import struct

_NATIVE = bytes.fromhex("2ff94b58157d60acb2422e266ff635c12f9f19ea002f006489384dec13202389")
# 这些精确短句是假设字典，不是已见过的服务器枚举。仅在整句逐字匹配时译义。
_EXACT = {
    b"invalid signature":"签名无效", b"signature invalid":"签名无效",
    b"invalid timestamp":"时间戳无效", b"timestamp invalid":"时间戳无效",
    b"invalid move time":"迁移时间无效", b"move time invalid":"迁移时间无效",
    b"player not found.":"找不到玩家", b"player not found":"找不到玩家",
    b"invalid player id":"玩家标识无效", b"invalid server id":"服务器标识无效",
    b"invalid ticket id":"票据标识无效", b"invalid session":"会话无效",
    b"not enough slots":"房间空位不足", b"already connected":"已经连接",
    b"invalid ticket":"票据无效", b"ticket is invalid":"票据无效",
    b"ticket expired":"票据过期", b"ticket has expired":"票据过期",
    b"ticket already used":"票据已用", b"ticket not found":"找不到票据",
    b"reservation not found":"找不到预约", b"reservation expired":"预约已过期",
    b"server is full":"服务器已满", b"game is full":"房间已满",
    b"invalid level":"地图无效", b"level mismatch":"地图不匹配",
    b"server mismatch":"服务器不匹配", b"version mismatch":"版本不匹配",
}
_VOCAB = (
    ("签名", {b"signature",b"signatures"}),
    ("票据", {b"ticket",b"tickets"}),
    ("预约", {b"reservation",b"reservations",b"reserved"}),
    ("时间", {b"time",b"timestamp",b"timestamps",b"timeout",b"expired"}),
    ("源实例", {b"source",b"src"}),
    ("目的实例", {b"destination",b"target",b"dst"}),
    ("服务端", {b"server",b"servers"}),
    ("地图", {b"level",b"levels",b"scene",b"map"}),
    ("容量", {b"full",b"capacity"}),
    ("版本", {b"version",b"build"}),
    ("会话", {b"session",b"login"}),
)
_MAX_BODY=260


def describe(body):
    """返回固定键集合；由调用侧限定本次配对、下行 outer=2，不作自动处置。"""
    length=len(body) if type(body) is bytes else None
    out=dict(body_len=length, format="未知",format_matched=False, declared_len=None,
             nul_terminated=None,text_len=None,encoding=None,native_match=False,
             classification="未知",classification_basis="未识别",hints=[],
             reason_code=None,subcode=None,text=None)
    if length is None or not 5<=length<=_MAX_BODY:
        return out
    declared=struct.unpack_from("<I",body)[0]
    if declared!=length-4:
        return out  # 可能是敏感二进制，连“前4B整数”也不能导出。
    out.update(declared_len=declared,nul_terminated=body[-1]==0)
    if body[-1]!=0 or b"\x00" in body[4:-1]:
        return out
    raw=body[4:-1]
    try:
        txt=raw.decode("utf-8",errors="strict")
    except UnicodeDecodeError:
        return out
    if not txt or not txt.isprintable():
        return out
    out.update(format="u32LE字节数_文本_单NUL",format_matched=True,text_len=len(raw),
               encoding="ASCII" if raw.isascii() else "UTF-8",classification_basis="仅匹配结构")
    # Claude 11-21：服务器拒因是英文说明句；不含数字/ID 时原样记录（这正是要找的原因）。
    if re.fullmatch(r"[A-Za-z .,'!?:;()_-]{1,80}", txt):
        out["text"] = txt
    if raw.isascii() and len(raw)==30 and hashlib.sha256(raw).digest()==_NATIVE:
        out.update(native_match=True,classification="源服务器或地图无效",
                   classification_basis="原生样本整句精确匹配")
        return out
    key=raw.lower()
    if key in _EXACT:
        out.update(classification=_EXACT[key],classification_basis="整句字面匹配；非数值枚举")
        return out
    # 任意姓名、地址、uuid、数字、密文一概不回显。未知句最多返回固定词类集合，
    # 标签顺序也是固定的，不是正文词序；提示不能当作服务端给出的原因码。
    if re.fullmatch(rb"[a-z]+(?: [a-z]+){0,11}[.!?]?",key):
        words=set(re.findall(rb"[a-z]+",key))
        out["hints"]=[label for label,group in _VOCAB if group.intersection(words)]
        if out["hints"]:out["classification_basis"]="仅词类提示；原因未知"
    return out

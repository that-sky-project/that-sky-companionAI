"""Build-421629 scene confirmation, not a general gameplay command encoder.

Native evidence: header 1f7dd2c, dispatch 1f9c774/1f9b330, payload
1f7dfdc, response 1f9a69c/1f9a530. Header byte 1 is the active
connection's generation (registration field00), NOT EnterPlayer.local_id.
Only the observed server-origin subtype 0x11 is answered here.
"""
import struct

class SceneControlError(ValueError):
    pass

def uint(value,bits,name):
    if type(value) is not int or not 0<=value<(1<<bits):
        raise SceneControlError(name+' is out of range')
    return value

class SceneConfirmation:
    def __init__(self, *, connection_generation, level_id, max_replies=8):
        self.generation=uint(connection_generation,8,'connection generation')
        if not self.generation:raise SceneControlError('generation must be nonzero')
        self.level=uint(level_id,32,'level ID')
        if type(max_replies) is not int or not 1<=max_replies<=128:
            raise SceneControlError('reply bound is invalid')
        self.max_replies=max_replies
        self.request_count=0;self.reply_count=0;self.ignored_count=0

    def reply(self, body):
        if not isinstance(body,bytes) or len(body)<3:
            raise SceneControlError('truncated game message header')
        kind,generation,sender=body[:3]
        if kind!=0x11:return None
        self.request_count+=1
        if len(body)!=9:raise SceneControlError('scene confirmation must have exactly nine bytes')
        level,echo=struct.unpack_from('<IH',body,3)
        if generation!=self.generation or sender!=0 or level!=self.level:
            self.ignored_count+=1
            return None
        if self.reply_count>=self.max_replies:raise SceneControlError('scene confirmation reply bound reached')
        self.reply_count+=1
        return bytes((0x11,self.generation,0))+struct.pack('<IH',self.level,echo)

    def summary(self):
        return {'requests':self.request_count,'replies_built':self.reply_count,
                'mismatched_requests_ignored':self.ignored_count,'reply_limit':self.max_replies}

def frame_scene_reply(peer_key, previous_counter, body):
    uint(peer_key,32,'peer key');uint(previous_counter,8,'previous app counter')
    if not isinstance(body,bytes) or len(body)!=9 or body[0]!=0x11 or body[2]!=0:
        raise SceneControlError('invalid scene confirmation reply')
    # Native 1f6804c increments and skips zero on rollover.
    counter=previous_counter+1 if previous_counter<255 else 1
    return struct.pack('<IBB',peer_key,0x0e,counter)+body,counter

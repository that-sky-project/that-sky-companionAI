"""Explicit Zstd trained-dictionary adapter for the recovered native type-2 path.

Verified dictionary is extracted from APK421589; implementation is independently
round-trip tested. Native live compressed response equivalence remains unobserved.
No network, device access or secret output. Install zstandard separately.
"""
import hashlib
from pathlib import Path
import struct
from .receive0167 import ReceiveError,MAX_ENET_PAYLOAD,MAX_DECOMPRESSED

DICTIONARY_SHA256='18fc4ec8f4250e729f7277769b6f782f950c0f3798850ef2f2211d131a37efed'
DICTIONARY_ID=0x46f7ab50
DICTIONARY_SIZE=2097152
DEFAULT_DICTIONARY=Path(__file__).resolve().parent/'resources'/'CompressDictionary.zst'


def load_type2_decoder(path=DEFAULT_DICTIONARY,*,apk_build=421589):
    if apk_build!=421589:raise ReceiveError('unsupported dictionary APK build')
    path=Path(path)
    if path.stat().st_size!=DICTIONARY_SIZE:raise ReceiveError('dictionary length mismatch')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=DICTIONARY_SHA256 or raw[:4]!=bytes.fromhex('37a430ec'):
        raise ReceiveError('dictionary identity mismatch')
    if struct.unpack_from('<I',raw,4)[0]!=DICTIONARY_ID:raise ReceiveError('dictionary ID mismatch')
    try:import zstandard as zstd
    except ImportError:raise ReceiveError('zstandard dependency required; configure local Python path') from None
    try:
        dictionary=zstd.ZstdCompressionDict(raw)
        if dictionary.dict_id()!=DICTIONARY_ID:raise ReceiveError('parsed dictionary ID mismatch')
        decoder=zstd.ZstdDecompressor(dict_data=dictionary)
    except Exception:raise ReceiveError('verified dictionary initialization failed') from None
    def decompress(block,expected_size):
        if type(block) is not bytes or not 0<len(block)<=MAX_ENET_PAYLOAD:
            raise ReceiveError('compressed block exceeds native input bounds')
        if type(expected_size) is not int or not 3<=expected_size<=MAX_DECOMPRESSED:
            raise ReceiveError('decompressed length exceeds native bounds')
        try:
            params=zstd.get_frame_parameters(block)
            if params.dict_id not in (0,DICTIONARY_ID):raise ReceiveError('frame dictionary ID mismatch')
            if params.content_size not in (expected_size,zstd.CONTENTSIZE_UNKNOWN):
                raise ReceiveError('frame content size differs from native envelope')
            result=decoder.decompress(block,max_output_size=expected_size,allow_extra_data=False)
        except Exception:raise ReceiveError('type-2 dictionary decompression rejected input') from None
        if len(result)!=expected_size:raise ReceiveError('decoded size differs from native envelope')
        return result
    return decompress

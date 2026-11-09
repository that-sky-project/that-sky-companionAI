"""Explicit project-local official meshoptimizer WASM bridge (no downloads)."""
from pathlib import Path
import hashlib
import subprocess
import sys
import tempfile
for _p in Path(__file__).resolve().parents:
    if (_p/'xg_exec.py').is_file():
        sys.path.insert(0,str(_p));break
import xg_exec  # noqa: E402
from ._types import DEFAULT_LIMITS, FormatError, MissingDecoderError

class NodeMeshoptDecoder:
    def __init__(self, node_executable, module_path, *, timeout=30, max_output=DEFAULT_LIMITS.max_uncompressed_bytes):
        self.node_executable=str(node_executable)
        self.module_path=Path(module_path).resolve(strict=True)
        self.timeout=timeout
        self.max_output=max_output
        self.module_sha256=hashlib.sha256(self.module_path.read_bytes()).hexdigest()
        self.bridge=Path(__file__).with_name('meshopt_bridge.mjs')

    def __call__(self, vertex_count, stride, compressed):
        if type(vertex_count) is not int or vertex_count<0 or stride!=36 or vertex_count*stride>self.max_output:
            raise FormatError('meshopt_bridge_size_limit')
        if len(compressed)>DEFAULT_LIMITS.max_file_bytes:
            raise FormatError('meshopt_bridge_input_limit')
        with tempfile.TemporaryDirectory(prefix='skyterrain-meshopt-') as folder:
            path=Path(folder)/'input.bin'
            path.write_bytes(compressed)
            try:
                result=xg_exec.run([self.node_executable,str(self.bridge),str(self.module_path),
                                    str(vertex_count),str(stride),str(path)],capture_output=True,
                                   timeout=self.timeout,check=False)
            except (OSError,subprocess.TimeoutExpired) as exc:
                raise MissingDecoderError('node_meshopt_unavailable_or_timeout') from exc
        if result.returncode or len(result.stdout)!=vertex_count*stride:
            raise FormatError('node_meshopt_decode_failed')
        return result.stdout

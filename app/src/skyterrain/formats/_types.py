"""Native-coordinate data contracts. Geometry is not asserted to be collision data."""
from dataclasses import dataclass, field
from typing import Any
import numpy as np

class FormatError(ValueError):
    """A malformed, oversized, or semantically inconsistent asset."""

class UnsupportedVersionError(FormatError):
    pass

class MissingDecoderError(FormatError):
    pass

@dataclass(frozen=True)
class Limits:
    max_file_bytes: int = 128 * 1024 * 1024
    max_uncompressed_bytes: int = 128 * 1024 * 1024
    max_vertices: int = 2_000_000
    max_indices: int = 12_000_000
    max_chunks: int = 200_000
    max_subchunks: int = 1_000_000
    max_markers: int = 4096
    max_bones: int = 4096
    max_segments: int = 8

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in self.__dict__.values()):
            raise ValueError('limits_must_be_positive_integers')

DEFAULT_LIMITS = Limits()

@dataclass(frozen=True)
class MeshChunk:
    group: str
    kind: str
    vertex_start: int
    vertex_count: int
    index_start: int
    index_count: int
    triangle_start: int
    triangle_count: int
    subchunk_start: int = 0
    subchunk_count: int = 0
    bounds_min: tuple[float, float, float] | None = None
    bounds_max: tuple[float, float, float] | None = None
    flags: dict[str, Any] = field(default_factory=dict)
    raw_descriptor: bytes = b''

@dataclass(frozen=True)
class MeshData:
    positions: np.ndarray
    triangles: np.ndarray
    source: str
    version: int
    chunks: tuple[MeshChunk, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    animated: bool = False
    geometry_kind: str = 'render_only'
    coordinate_space: str = 'sky_native'

@dataclass(frozen=True)
class MeshBundle:
    positions: np.ndarray
    triangles: np.ndarray
    source: str
    version: int
    meshes: tuple[MeshData, ...]
    chunks: tuple[MeshChunk, ...]
    metadata: dict[str, Any] = field(default_factory=dict)
    geometry_kind: str = 'render_only'
    coordinate_space: str = 'sky_native'

"""Network-free, one-account Home continuation for build 421589.

The caller owns ticket authentication, encryption, ENet and the deadline.
Templates are mandatory RAM-only inputs and are never loaded by this package.
"""
from .engine import Engine, Frame, FrameGroup, validate_templates

__all__ = ['Engine', 'Frame', 'FrameGroup', 'validate_templates']

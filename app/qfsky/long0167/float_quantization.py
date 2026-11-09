"""Offline, explicit-profile model of native scalar quantizers.

Public ELF: 3190f64 (float32), 3191558 (float64 storage with a float32
final encoder), 3191b24 (three float32 components).  This implements the
finite-parameter subset with IEEE nearest-even and gradual underflow, using
exact rational arithmetic for each rounded operation and fused multiply-add.
Float32 encoder inputs may also be either infinity: the native unit clamp
maps their ratio to an endpoint. This is needed for the public rain-state
constructor's +Inf default (222082c); NaN inputs and infinite parameters
remain unsupported. Float64 infinite inputs are not modeled here.
It does not assert the current game's FPCR, emulate FPSR/traps, serialize an
Actor, read account state, or open files/network connections.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math


class QuantizationError(ValueError):
    """Unsupported input/profile, or a native-invalid decoded result."""


@dataclass(frozen=True)
class FloatingPointProfile:
    rounding: str
    flush_to_zero: bool


NEAREST_EVEN_GRADUAL = FloatingPointProfile("nearest_even", False)


@dataclass(frozen=True)
class QuantizedCode:
    width: int
    split_code: int
    unmasked_code: int
    code: int


def _check_profile(profile: FloatingPointProfile) -> None:
    if type(profile) is not FloatingPointProfile or profile != NEAREST_EVEN_GRADUAL:
        raise QuantizationError("Only explicit nearest-even with gradual underflow is modeled")


def _round_ratio(numerator: int, denominator: int) -> int:
    quotient, remainder = divmod(numerator, denominator)
    doubled = remainder * 2
    return quotient + (doubled > denominator or (doubled == denominator and quotient & 1))


def _round_fraction(value: Fraction, precision: int) -> float:
    """One IEEE binary32/64 nearest-even rounding, including subnormals."""
    if precision not in (24, 53):
        raise QuantizationError("Unsupported IEEE precision")
    if not value:
        return 0.0
    negative = value < 0
    magnitude = abs(value)
    numerator, denominator = magnitude.numerator, magnitude.denominator
    exponent = numerator.bit_length() - denominator.bit_length()
    below_power = (numerator < denominator << exponent) if exponent >= 0 else (numerator << -exponent < denominator)
    if below_power:
        exponent -= 1
    minimum_exponent, maximum_exponent = (-126, 127) if precision == 24 else (-1022, 1023)
    unit_exponent = max(exponent, minimum_exponent) - (precision - 1)
    if unit_exponent >= 0:
        mantissa = _round_ratio(numerator, denominator << unit_exponent)
    else:
        mantissa = _round_ratio(numerator << -unit_exponent, denominator)
    if mantissa == 0:
        return -0.0 if negative else 0.0
    if mantissa >= 1 << precision:
        mantissa >>= 1
        unit_exponent += 1
    if unit_exponent + (mantissa.bit_length() - 1) > maximum_exponent:
        return -math.inf if negative else math.inf
    result = math.ldexp(float(mantissa), unit_exponent)
    return -result if negative else result


def _cast(value: float | int, precision: int, *, allow_infinity: bool = False) -> float:
    if type(value) not in (float, int):
        raise QuantizationError("Expected a finite numeric scalar")
    try:
        converted = float(value)
    except (OverflowError, ValueError) as exc:
        raise QuantizationError("Numeric scalar is outside the modeled range") from exc
    if allow_infinity and math.isinf(converted):
        return converted
    if not math.isfinite(converted):
        raise QuantizationError("Nonfinite scalar input is not modeled")
    if converted == 0:
        return converted
    result = _round_fraction(Fraction(converted), precision)
    if not math.isfinite(result):
        raise QuantizationError("Scalar does not fit the selected IEEE format")
    return result


def _op(a: float, b: float, operation: str, precision: int) -> float:
    if not math.isfinite(a) or not math.isfinite(b):
        raise QuantizationError("Nonfinite intermediate is outside the finite subset")
    left, right = Fraction(a), Fraction(b)
    if operation == "add":
        result = left + right
    elif operation == "sub":
        result = left - right
    elif operation == "mul":
        result = left * right
    elif operation == "div":
        if b == 0:
            return math.nan if a == 0 else math.copysign(math.inf, a * math.copysign(1.0, b))
        result = left / right
    else:
        raise QuantizationError("Unsupported arithmetic operation")
    return _round_fraction(result, precision)


def _fma32(a: float, b: float, c: float) -> float:
    if not all(math.isfinite(value) for value in (a, b, c)):
        raise QuantizationError("Nonfinite FMADD input is outside the finite subset")
    return _round_fraction(Fraction(a) * Fraction(b) + Fraction(c), 24)


def _clamp_numeric_unit(value: float) -> float:
    # ARM FMAXNM/FMINNM choose the numeric operand when the other is NaN.
    if math.isnan(value):
        return 0.0
    return min(1.0, max(0.0, value))


def _fcvtzu32(value: float) -> int:
    # FCVTZU round-to-zero is independent of the FPCR rounding direction.
    if math.isnan(value) or value <= 0:
        return 0
    if value >= 0xFFFFFFFF:
        return 0xFFFFFFFF
    return int(value)


def _parameters(minimum, maximum, center, width, precision, profile):
    _check_profile(profile)
    if type(width) is not int or not 1 <= width <= 31:
        raise QuantizationError("Native quantizer width must be 1..31")
    a, b, c = (_cast(value, precision) for value in (minimum, maximum, center))
    if not a < b or not a <= c <= b:
        raise QuantizationError("Expected minimum < maximum and an inclusive center")
    mask = (1 << width) - 1
    span = _op(b, a, "sub", precision)
    lower_span = _op(c, a, "sub", precision)
    mask_float = _round_fraction(Fraction(mask), precision)
    product = _op(lower_span, mask_float, "mul", precision)
    if not all(math.isfinite(value) for value in (span, lower_span, product)):
        raise QuantizationError("Nonfinite parameter intermediates are not modeled")
    split = _fcvtzu32(_op(product, span, "div", precision))
    return a, b, c, mask, split


def _encode(value, minimum, maximum, center, width, precision, profile):
    a, b, c, mask, split = _parameters(minimum, maximum, center, width, precision, profile)
    value = _cast(value, precision, allow_infinity=precision == 24)
    if value < c and c > a:
        # The finite parameter spans are nonnegative, and the lower branch
        # requires a positive span. Native +/-Inf subtraction and division
        # preserve the input sign; FMAXNM/FMINNM then yield exactly 0 or 1.
        ratio = value if math.isinf(value) else _op(
            _op(value, a, "sub", precision), _op(c, a, "sub", precision), "div", precision)
        ratio32 = _cast(_clamp_numeric_unit(ratio), 24)
        result = _fma32(ratio32, _round_fraction(Fraction(split), 24), 0.5)
    else:
        # The upper span may be +0 when center == maximum. +Inf / +0
        # remains +Inf. -Inf only enters this branch when center == minimum,
        # where the span is strictly positive. Do not substitute a finite
        # endpoint: it would introduce 0/0 for the +Inf/center==maximum case.
        ratio = value if math.isinf(value) else _op(
            _op(value, c, "sub", precision), _op(b, c, "sub", precision), "div", precision)
        ratio32 = _cast(_clamp_numeric_unit(ratio), 24)
        # Native SUB x8,x22,w21,uxtw is unsigned 64-bit, not signed Python.
        upper_count = (mask - split) & 0xFFFFFFFFFFFFFFFF
        base = _op(_round_fraction(Fraction(split), 24), 0.5, "add", 24)
        result = _fma32(ratio32, _round_fraction(Fraction(upper_count), 24), base)
    unmasked = _fcvtzu32(result)
    return QuantizedCode(width, split, unmasked, unmasked & mask)


def quantize_float32(value, minimum, maximum, center, width, *, profile):
    return _encode(value, minimum, maximum, center, width, 24, profile)


def quantize_float64(value, minimum, maximum, center, width, *, profile):
    """3191558: parameter/ratio math is FP64, final FMADD is FP32."""
    return _encode(value, minimum, maximum, center, width, 53, profile)


def _decode(code, minimum, maximum, center, width, precision, profile):
    a, b, c, mask, split = _parameters(minimum, maximum, center, width, precision, profile)
    if type(code) is not int or not 0 <= code <= mask:
        raise QuantizationError("Expected an in-width unsigned code")
    if precision == 24:
        if code <= split:
            if split == 0:
                return a
            numerator = _op(_op(c, a, "sub", 24), _round_fraction(Fraction(code), 24), "mul", 24)
            result = _op(_op(numerator, _round_fraction(Fraction(split), 24), "div", 24), a, "add", 24)
        elif mask - split == 0:
            return b
        else:
            numerator = _op(_op(b, c, "sub", 24), _round_fraction(Fraction(code - split), 24), "mul", 24)
            result = _op(_op(numerator, _round_fraction(Fraction(mask - split), 24), "div", 24), c, "add", 24)
    else:
        # FP64 chooses upper for equality, unlike the FP32 <= split branch.
        if code >= split:
            numerator = _op(_op(b, c, "sub", 53), float((code - split) & 0xFFFFFFFF), "mul", 53)
            ratio = _op(numerator, float((mask - split) & 0xFFFFFFFF), "div", 53)
            if not math.isfinite(ratio):
                raise QuantizationError("Native FP64 decode selects a nonfinite result")
            result = _op(ratio, c, "add", 53)
        else:
            numerator = _op(_op(c, a, "sub", 53), float(code), "mul", 53)
            ratio = _op(numerator, float(split), "div", 53)
            if not math.isfinite(ratio):
                raise QuantizationError("Native FP64 decode selects a nonfinite result")
            result = _op(ratio, a, "add", 53)
    if not math.isfinite(result):
        raise QuantizationError("Nonfinite decoded results are outside the finite subset")
    return result


def decode_float32(code, minimum, maximum, center, width, *, profile):
    return _decode(code, minimum, maximum, center, width, 24, profile)


def decode_float64(code, minimum, maximum, center, width, *, profile):
    return _decode(code, minimum, maximum, center, width, 53, profile)


def quantize_float3(values, minimum, maximum, center, width, *, profile):
    """3191b24 writes x/y/z consecutively, each using its own bounds/center."""
    if any(not isinstance(vector, (tuple, list)) or len(vector) != 3 for vector in (values, minimum, maximum, center)):
        raise QuantizationError("Expected four vectors of exactly three components")
    return tuple(quantize_float32(values[i], minimum[i], maximum[i], center[i], width, profile=profile) for i in range(3))

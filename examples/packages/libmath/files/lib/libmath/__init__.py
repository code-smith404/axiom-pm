"""A deliberately small math helper used by the axiom examples."""

__version__ = "1.2.0"


def clamp(value: float, low: float, high: float) -> float:
    """Return ``value`` limited to the inclusive range ``low..high``."""
    if low > high:
        raise ValueError("low must not exceed high")
    return max(low, min(high, value))

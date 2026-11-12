"""Small, dependency-free statistics used to put a confidence on each proposal.

Conversions in a window are treated as Poisson counts. With a Jeffreys prior the posterior
on the true conversion count is Gamma(n + 0.5, 1). Because CPA = spend / conversions, the
probability that the *true* CPA beats a target is a Gamma tail probability:

    P(true CPA < target) = P(lambda > spend / target) = 1 - P(n + 0.5, spend / target)

where P(a, x) is the regularized lower incomplete gamma function. The same trick works for
ROAS if value per conversion is held at its observed mean (a stated simplification).
"""
from __future__ import annotations

import math

_EPS = 1e-14
_MAX_ITER = 500


def gammainc_lower(a: float, x: float) -> float:
    """Regularized lower incomplete gamma P(a, x). Numerical Recipes series / continued fraction."""
    if a <= 0:
        raise ValueError("a must be > 0")
    if x <= 0:
        return 0.0
    if x < a + 1.0:
        # Series representation.
        term = 1.0 / a
        total = term
        ap = a
        for _ in range(_MAX_ITER):
            ap += 1.0
            term *= x / ap
            total += term
            if abs(term) < abs(total) * _EPS:
                break
        return min(1.0, total * math.exp(-x + a * math.log(x) - math.lgamma(a)))
    # Continued fraction (Lentz) for Q(a, x), then P = 1 - Q.
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, _MAX_ITER):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            break
    q = math.exp(-x + a * math.log(x) - math.lgamma(a)) * h
    return max(0.0, 1.0 - q)


def prob_cpa_below(spend: float, conversions: float, target_cpa: float) -> float:
    """P(true CPA < target_cpa) given observed spend and conversions."""
    if target_cpa <= 0 or spend <= 0:
        return 0.0
    threshold = spend / target_cpa  # conversions needed to hit the target exactly
    return 1.0 - gammainc_lower(conversions + 0.5, threshold)


def prob_cpa_above(spend: float, conversions: float, target_cpa: float) -> float:
    return 1.0 - prob_cpa_below(spend, conversions, target_cpa)


def prob_roas_above(spend: float, conversions: float, value: float, target_roas: float) -> float:
    """P(true ROAS > target), holding value per conversion at its observed mean."""
    if conversions <= 0 or value <= 0 or spend <= 0:
        return 0.0
    value_per_conv = value / conversions
    threshold = target_roas * spend / value_per_conv
    return 1.0 - gammainc_lower(conversions + 0.5, threshold)


def poisson_interval(n: float, z: float = 1.96) -> tuple[float, float]:
    """Approximate 95% interval for a Poisson count (Wilson-Hilferty style on sqrt scale)."""
    lo = max(0.0, (math.sqrt(n) - z / 2) ** 2) if n > 0 else 0.0
    hi = (math.sqrt(n + 1) + z / 2) ** 2
    return lo, hi

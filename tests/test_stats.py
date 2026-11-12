import math

import pytest

from ads_guardrail.stats import gammainc_lower, poisson_interval, prob_cpa_below, prob_roas_above


@pytest.mark.parametrize("x", [0.1, 0.5, 1.0, 3.0, 10.0, 40.0])
def test_gamma_a1_is_exponential_cdf(x):
    assert gammainc_lower(1.0, x) == pytest.approx(1 - math.exp(-x), abs=1e-12)


@pytest.mark.parametrize("x", [0.2, 1.0, 2.5, 9.0])
def test_gamma_half_is_erf(x):
    assert gammainc_lower(0.5, x) == pytest.approx(math.erf(math.sqrt(x)), abs=1e-12)


def test_gamma_integer_matches_poisson_tail():
    # P(a, x) for integer a = P(Poisson(x) >= a)
    a, x = 5, 3.7
    tail = 1 - sum(math.exp(-x) * x**k / math.factorial(k) for k in range(a))
    assert gammainc_lower(a, x) == pytest.approx(tail, abs=1e-12)


def test_cpa_confidence_moves_the_right_way():
    # $1,000 spend, target $50 -> need 20 conversions to hit target
    assert prob_cpa_below(1000, 40, 50) > 0.99
    assert prob_cpa_below(1000, 5, 50) < 0.01
    assert prob_cpa_below(1000, 30, 50) > prob_cpa_below(1000, 22, 50)
    # same CPA, more data -> more confidence
    assert prob_cpa_below(2000, 60, 50) > prob_cpa_below(1000, 30, 50)


def test_roas_confidence():
    assert prob_roas_above(1000, 40, 4000, 2.0) > 0.99
    assert prob_roas_above(1000, 10, 1000, 2.0) < 0.01
    assert prob_roas_above(0, 0, 0, 2.0) == 0.0


def test_poisson_interval_contains_n():
    lo, hi = poisson_interval(20)
    assert lo < 20 < hi

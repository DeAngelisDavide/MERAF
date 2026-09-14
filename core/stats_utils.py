"""
Statistical helpers shared by run_experiments.py and aggregate_results.py:
mean/sample-std/CI95 across seeds, TOST equivalence test, and a guard
against suspiciously identical results between variants. Stdlib only.
"""
from __future__ import annotations

import math
import statistics as st
from dataclasses import dataclass
from typing import Any, Dict, Hashable, List, Optional, Sequence


# Two-sided critical t, alpha=0.05, df 1..10; normal approx (1.96) beyond.
T_TABLE_95_TWOSIDED = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
                       6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228}

# One-sided critical t, alpha=0.05, df 1..10 (used by TOST); 1.645 beyond.
T_TABLE_90_ONESIDED = {1: 6.314, 2: 2.920, 3: 2.353, 4: 2.132, 5: 2.015,
                       6: 1.943, 7: 1.895, 8: 1.860, 9: 1.833, 10: 1.812}


def t_value_95(n: int) -> float:
    """Two-sided critical t for a 95% CI with n observations."""
    df = max(n - 1, 1)
    return T_TABLE_95_TWOSIDED.get(df, 1.96)


def t_value_onesided_05(df: float) -> float:
    """One-sided critical t (alpha=0.05); fractional df is floored (conservative)."""
    df_int = max(int(math.floor(df)), 1)
    return T_TABLE_90_ONESIDED.get(df_int, 1.645)


@dataclass
class MeanStdCI:
    mean: float
    std: float       # sample standard deviation (ddof=1)
    ci95: float       # half-width of the 95% confidence interval
    n: int
    min: float
    max: float


def mean_std_ci95(vals: Sequence[float]) -> MeanStdCI:
    """Mean, sample std (ddof=1) and 95% CI half-width (Student t).

    With n=1, std and ci95 are reported as 0.0 (not NaN) for CSV compatibility.
    """
    vals = list(vals)
    n = len(vals)
    mean = st.mean(vals)
    if n > 1:
        std = st.stdev(vals)  # ddof=1, not pstdev
        ci95 = t_value_95(n) * (std / math.sqrt(n))
    else:
        std = 0.0
        ci95 = 0.0
    return MeanStdCI(mean=mean, std=std, ci95=ci95, n=n,
                      min=min(vals), max=max(vals))


@dataclass
class TOSTResult:
    mean_a: float
    mean_b: float
    diff: float             # mean_a - mean_b
    margin: float           # practical equivalence threshold (same units as diff)
    se: float                # standard error of the difference (Welch)
    df: float
    t_lower: float
    t_upper: float
    t_crit: float
    equivalent: bool         # True if both one-sided tests reject
    reason: Optional[str] = None  # why the verdict could not be computed


def tost_equivalence(a: Sequence[float], b: Sequence[float], margin: float,
                      alpha: float = 0.05) -> TOSTResult:
    """Two One-Sided Tests for practical equivalence of two independent samples.

    H0: |mean_a - mean_b| >= margin; H1: |mean_a - mean_b| < margin.
    Welch t with Satterthwaite df. `alpha` is documentary only: the critical
    values table is fixed at 0.05.
    """
    a = list(a)
    b = list(b)
    n_a, n_b = len(a), len(b)
    if n_a < 2 or n_b < 2:
        return TOSTResult(mean_a=st.mean(a) if a else float("nan"),
                           mean_b=st.mean(b) if b else float("nan"),
                           diff=float("nan"), margin=margin, se=float("nan"),
                           df=float("nan"), t_lower=float("nan"),
                           t_upper=float("nan"), t_crit=float("nan"),
                           equivalent=False,
                           reason=f"at least 2 observations per group are required "
                                  f"(n_a={n_a}, n_b={n_b})")

    mean_a, mean_b = st.mean(a), st.mean(b)
    var_a, var_b = st.variance(a), st.variance(b)  # ddof=1
    se2 = var_a / n_a + var_b / n_b
    se = math.sqrt(se2) if se2 > 0 else 0.0
    diff = mean_a - mean_b

    if se == 0.0:
        # Zero variance in both samples: decide by point comparison, no inference.
        return TOSTResult(mean_a=mean_a, mean_b=mean_b, diff=diff,
                           margin=margin, se=0.0, df=float("inf"),
                           t_lower=float("inf"), t_upper=float("-inf"),
                           t_crit=0.0, equivalent=abs(diff) < margin,
                           reason="zero sample variance in both groups")

    # Welch-Satterthwaite
    num = se2 ** 2
    den = ((var_a / n_a) ** 2) / (n_a - 1) + ((var_b / n_b) ** 2) / (n_b - 1)
    df = num / den if den > 0 else min(n_a, n_b) - 1

    t_crit = t_value_onesided_05(df)
    t_lower = (diff + margin) / se   # H0: diff <= -margin
    t_upper = (diff - margin) / se   # H0: diff >= +margin

    equivalent = (t_lower > t_crit) and (t_upper < -t_crit)

    return TOSTResult(mean_a=mean_a, mean_b=mean_b, diff=diff, margin=margin,
                       se=se, df=df, t_lower=t_lower, t_upper=t_upper,
                       t_crit=t_crit, equivalent=equivalent)


# ---- "identical output = suspected bug" guard ----

class SuspiciousIdenticalResultsError(RuntimeError):
    """Raised by assert_variants_differ()."""


def assert_variants_differ(cells_a: Dict[Hashable, float], cells_b: Dict[Hashable, float],
                            name_a: str, name_b: str,
                            trivial_values: Sequence[float] = (0.0, 100.0),
                            max_identical: int = 0) -> None:
    """Raise if two structurally different variants coincide exactly on more
    than `max_identical` non-trivial cells.

    Only for pairs of mechanisms that must differ by design (e.g. two admission
    policies); never for ablation comparisons, where "no difference" is a valid result.
    """
    identical = []
    for key, va in cells_a.items():
        if key not in cells_b:
            continue
        vb = cells_b[key]
        if va == vb and va not in trivial_values:
            identical.append((key, va))
    if len(identical) > max_identical:
        preview = ", ".join(f"{k}={v}" for k, v in identical[:5])
        raise SuspiciousIdenticalResultsError(
            f"{name_a!r} and {name_b!r} produce IDENTICAL results on "
            f"{len(identical)} non-trivial cell(s) (e.g. {preview}) -- "
            f"a pattern observed three times in this work as a signal of "
            f"a real bug (shared tenant, EWMA-LRT cold start, per-workflow "
            f"admission that protected nothing). Check the implementation "
            f"of one of the two variants before interpreting these results."
        )

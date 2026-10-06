"""Descriptive statistics and PAIRED strategy comparisons.

Every strategy is run on the same scenarios, so comparisons use the per-scenario paired differences
d_k = metric(strategy, k) - metric(reference, k) over scenarios k where both values exist.

Test selection (documented in every result row):
  * n < MIN_PAIRS (10)             -> no test ("insufficient pairs"); only descriptive numbers are reported
  * all differences zero           -> no test ("identical")
  * Shapiro-Wilk on d (n <= 5000): p >= 0.05 -> paired t-test (differences compatible with normality)
                                   p <  0.05 -> Wilcoxon signed-rank test (non-normal differences; zeros dropped,
                                                the 'wilcox' zero method)
  * 95 % confidence interval of the mean difference: Student t interval; for non-normal differences a
    percentile bootstrap (10 000 resamples, fixed seed) is reported instead.
  * p-values are also Holm-Bonferroni adjusted within each (strategy vs reference) family of metrics, because
    many metrics are tested at once. "significant" uses the ADJUSTED p-value < 0.05.
"""
from __future__ import annotations

import math

import numpy as np
from scipy import stats as sps

from app.evaluation.metrics import METRICS

MIN_PAIRS = 10
ALPHA = 0.05


def describe(values: list[float | None]) -> dict:
    x = np.array([v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))], dtype=float)
    if not len(x):
        return {"n": 0, "mean": None, "median": None, "std": None, "min": None, "max": None, "p25": None, "p75": None}
    return {"n": int(len(x)), "mean": float(x.mean()), "median": float(np.median(x)),
            "std": float(x.std(ddof=1)) if len(x) > 1 else 0.0, "min": float(x.min()), "max": float(x.max()),
            "p25": float(np.percentile(x, 25)), "p75": float(np.percentile(x, 75))}


def _bootstrap_ci(d: np.ndarray, seed: int = 12345, n: int = 10_000) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    means = rng.choice(d, size=(n, len(d)), replace=True).mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def paired(ref: dict[int, float | None], other: dict[int, float | None], metric: str) -> dict:
    keys = sorted(k for k in ref if k in other and ref[k] is not None and other[k] is not None)
    a = np.array([ref[k] for k in keys], dtype=float)
    b = np.array([other[k] for k in keys], dtype=float)
    d = b - a
    direction = METRICS.get(metric, ("", "", 0))[2]
    out = {"metric": metric, "pairs": int(len(d)), "reference_mean": float(a.mean()) if len(a) else None,
           "strategy_mean": float(b.mean()) if len(b) else None, "mean_difference": float(d.mean()) if len(d) else None,
           "pct_change": None, "improvement": None, "ci95_low": None, "ci95_high": None, "ci_method": None,
           "test": None, "statistic": None, "p_value": None, "normality_p": None, "note": None}
    if len(d) and a.mean() != 0:
        out["pct_change"] = float(100 * d.mean() / abs(a.mean()))
    if len(d) and direction:
        # positive improvement = better for this metric (lower-is-better metrics invert the sign)
        out["improvement"] = float(direction * d.mean())
    if len(d) < MIN_PAIRS:
        out["note"] = f"insufficient pairs (n={len(d)} < {MIN_PAIRS}): no significance test"
        return out
    if np.allclose(d, 0):
        out.update(test="none", note="identical results in every scenario", ci95_low=0.0, ci95_high=0.0, ci_method="exact")
        return out
    normal_p = float(sps.shapiro(d).pvalue) if 3 <= len(d) <= 5000 and np.ptp(d) > 0 else None
    out["normality_p"] = normal_p
    if normal_p is not None and normal_p >= ALPHA:
        t = sps.ttest_rel(b, a)
        se = d.std(ddof=1) / math.sqrt(len(d))
        h = float(sps.t.ppf(0.975, len(d) - 1) * se)
        out.update(test="paired t-test", statistic=float(t.statistic), p_value=float(t.pvalue),
                   ci95_low=float(d.mean() - h), ci95_high=float(d.mean() + h), ci_method="t")
    else:
        nz = d[d != 0]
        if len(nz) < MIN_PAIRS:
            out.update(note=f"only {len(nz)} non-zero differences: no significance test", test="none")
            lo, hi = _bootstrap_ci(d)
            out.update(ci95_low=lo, ci95_high=hi, ci_method="bootstrap")
            return out
        w = sps.wilcoxon(b, a, zero_method="wilcox")
        lo, hi = _bootstrap_ci(d)
        out.update(test="Wilcoxon signed-rank", statistic=float(w.statistic), p_value=float(w.pvalue),
                   ci95_low=lo, ci95_high=hi, ci_method="bootstrap")
    return out


def holm(rows: list[dict]) -> None:
    """Adds p_adjusted (Holm-Bonferroni) and significant to rows that have a p-value (in place)."""
    tested = sorted([r for r in rows if r.get("p_value") is not None], key=lambda r: r["p_value"])
    m = len(tested)
    running = 0.0
    for k, r in enumerate(tested):
        running = max(running, min(1.0, (m - k) * r["p_value"]))
        r["p_adjusted"] = running
        r["significant"] = running < ALPHA
    for r in rows:
        r.setdefault("p_adjusted", None)
        r.setdefault("significant", None)


def compare(per_strategy: dict[str, dict[int, dict]], reference: str, metrics: list[str]) -> list[dict]:
    """per_strategy: name -> scenario -> metric dict. Returns paired rows for every other strategy vs reference."""
    out = []
    ref = per_strategy[reference]
    for name, rows in per_strategy.items():
        if name == reference:
            continue
        fam = []
        for m in metrics:
            r = paired({k: v.get(m) for k, v in ref.items()}, {k: v.get(m) for k, v in rows.items()}, m)
            r.update(strategy=name, reference=reference)
            fam.append(r)
        holm(fam)
        out += fam
    return out

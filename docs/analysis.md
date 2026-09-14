# MERAF analysis scripts: reference

This document is the engineering reference for the post-processing scripts in
`MERAF/analysis/`. The scripts never run simulations: they read the CSV files
written by `run_experiments.py` / `run_workflow_experiments.py` and derive
aggregated statistics, equivalence tests and robustness checks from them.
Everything that used to live in long in-code comments (design rationale,
statistical choices, historical bug notes) is collected here.

Contents

1. Shared conventions
2. `aggregate_results.py`
3. `equivalence_test.py`
4. `analyze_client_timeout.py`
5. `analyze_deadline_sensitivity.py`
6. `analyze_profile_pairing.py`
7. Known limitations and inconsistencies

---

## 1. Shared conventions

### Invocation

All scripts use relative imports (`from ..core.stats_utils import ...`), so
they must be run as modules from the directory that *contains* `MERAF/`:

```
python3 -m MERAF.analysis.<script> [options]
```

Running a script as a plain file (`python3 analysis/foo.py`) fails at import.

### Statistics come from one place

Every mean / standard deviation / confidence interval in the analysis package
is computed by `MERAF.core.stats_utils.mean_std_ci95`, and every equivalence
test by `MERAF.core.stats_utils.tost_equivalence`. `run_experiments.py` uses
the same functions. This is deliberate: an earlier version of
`aggregate_results.py` had its own local implementation that disagreed with
the experiment runner (see the CI95 bug note in section 2), and the shared
module is what prevents the two pipelines from diverging again.

`mean_std_ci95(vals)` returns an object with fields `n, mean, std, ci95, min,
max`, where:

- `std` is the **sample** standard deviation (ddof = 1);
- `ci95` is the **half-width** of the 95 % confidence interval of the mean,
  `t_{0.975, n-1} * std / sqrt(n)`, using Student's t so that small samples
  (typically n = 5 seeds) are handled correctly;
- with `n = 1` both `std` and `ci95` are reported as `0.0` rather than NaN for
  CSV compatibility; a single seed is not a sufficient basis for any claim.

### The client-timeout constant

`MERAF.core.metrics.CLIENT_TIMEOUT_MS = 60_000.0` is the end-to-end HTTP
timeout of the real benchmark client. It is the penalty charged to every
rejected request in the "offered" latency distribution (section 2.4). It is a
property of the client, not of the SLO, and must not be replaced by a multiple
of the SLO.

### Percentiles

`MERAF.core.metrics._percentile(values, p)` (p in `[0, 1]`) uses linear
interpolation between order statistics: `k = (n-1)*p`, then interpolate between
`sorted[floor(k)]` and `sorted[min(floor(k)+1, n-1)]`. Returns NaN for an
empty list. `analyze_client_timeout.pctile` implements the same rule but takes
`p` in `[0, 100]`.

### Sample layout of result directories

The experiment runners write into a single output directory, e.g.
`MERAF/results/outputs/` for request-level runs and
`MERAF/results/outputs_wf_<profile_mode>/` for workflow-level runs. Files
relevant to the analysis scripts:

| File | Producer | Content |
|---|---|---|
| `summary_<scenario>_raw_perseed.csv` | `run_experiments.py` | one row per (algorithm, load, seed) with all request-level metrics |
| `completed_<scenario>_<algo>_<load>[_seed<N>].csv` | `run_experiments.py` | one row per completed request; column `total_ms` is end-to-end latency |
| `profile_breakdown_<scenario>.csv` | `run_experiments.py` | one row per (algorithm@load@seed, security profile) with per-profile latency metrics |
| `workflows_<scenario>_<topology>_<mode>_<algo>_<load>_seed<i>.csv` | `run_workflow_experiments.py` | one row per workflow: `completed`, `makespan_ms`, `depth`, ... |
| `workflow_summary_<scenario>_<topology>_<mode>_raw_perseed.csv` | `run_workflow_experiments.py` | one row per (algorithm, load, seed) with `completion_rate_pct` etc. |

Loads are formatted with three decimals in file names (`f"{load:.3f}"`, e.g.
`0.120`). Scenario names used in the paper are `rete` (network) and `locale`
(local).

---

## 2. `aggregate_results.py`

### 2.1 Purpose

Aggregates replicated runs (one replica = one seed) into per-(scenario,
algorithm, load) statistics. Typical use is when `run_experiments.py` was
executed once per seed into separate sub-directories, e.g.

```
output/
  seed_1/summary_rete_raw_perseed.csv
  seed_2/summary_rete_raw_perseed.csv
  ...
```

The script walks `--root` recursively, so any nesting works as long as file
names match the expected patterns.

Outputs, per scenario, in `--outdir`:

- `<scenario>_raw_all_seeds.csv`: one row per (algorithm, load, seed), the
  union of all input rows;
- `<scenario>_aggregated.csv`: one row per (algorithm, load) with
  `<field>_mean/_std/_ci95/_min/_max` for every metric, `n_seeds`, and
  `reject_penalty_ms`;
- `<scenario>_table.tex`: the main comparison table, produced by
  `MERAF.reporting.make_latex_tables.write_main_comparison_table`.

A console summary (throughput, p50/p95/p99, reject %) with `mean +/- ci95` per
cell is printed as well.

### 2.2 Input discovery: primary path

`load_raw_perseed()` looks for files matching

```
RAW_PERSEED_RE = ^summary_(?P<scenario>[a-zA-Z0-9]+)_raw_perseed\.csv$
```

These already contain one row per (algorithm, load, seed) with every metric
computed by the runner, including `latency_p95_offered_ms`,
`latency_p99_offered_ms`, `latency_p95_offered_reachable`,
`latency_p99_offered_reachable` and `reject_penalty_ms`. Rows from all matching
files are concatenated per scenario. This is the expected, most direct route.

### 2.3 Input discovery: fallback reconstruction from per-request logs

If no `summary_*_raw_perseed.csv` is found under `--root`,
`load_from_completed()` rebuilds the per-seed rows from per-request logs
matching

```
COMPLETED_RE = ^completed_(?P<scenario>[a-zA-Z0-9]+)_(?P<algo>[a-zA-Z0-9\-]+)_
               (?P<load>[0-9.]+)(?:_seed(?P<seed>\d+))?\.csv$
```

Seed resolution: if `_seed<N>` is absent from the file name, the first integer
found in the **immediately enclosing directory name** is used
(`DIR_SEED_RE = (\d+)`, e.g. `seed_3/` -> 3). Files whose seed cannot be
determined are skipped (reported with `--verbose`).

Reconstruction per file:

1. `totals` = list of `total_ms` for completed requests; `completed = len(totals)`.
2. **Total generated requests** are not in the log. They are rebuilt by
   re-running `MERAF.core.workload.WorkloadGenerator(rate_per_ms=load,
   seed=seed).generate(horizon_ms)`. The generator is deterministic in
   (rate, seed, horizon) and independent of algorithm and scenario, so the
   count is exact provided `--horizon-ms` matches the horizon used in the
   original run (default 20000 ms).
3. `rejected = max(total - completed, 0)`; `reject_rate_pct = 100 * rejected / total`.
4. `throughput_rps = completed / horizon_s`; `goodput_rps = #(total_ms <= --slo-ms) / horizon_s`.
5. Percentiles p50/p95/p99 via `_percentile`, mean via `statistics.mean`.
6. Offered percentiles and reachability flags as described in 2.4.

The fallback needs `MERAF.core` importable under the absolute name
`MERAF.core...` (the `try/except` around `WorkloadGenerator` / `_percentile`
sets `_HAS_MERAF`); if that import failed, `load_from_completed()` raises a
`RuntimeError` telling the user to run the script as a module.

When the directory structure does not match, `--verbose` prints exactly which
files were matched and how each was interpreted, so `RAW_PERSEED_RE`,
`COMPLETED_RE` or `DIR_SEED_RE` can be adjusted instead of guessing.

### 2.4 The "offered" latency metric and its penalty

Admitted-only percentiles reward an algorithm that rejects aggressively. The
"offered" distribution therefore includes every generated request: admitted
ones at their measured latency, rejected ones at a penalty value.

**Penalty = `CLIENT_TIMEOUT_MS` (60 s), not a multiple of the SLO.**
Historical bug: an earlier version charged rejects `2 x SLO = 4000 ms`. At
high load the p99 of *admitted* requests can exceed 4000 ms, so a rejected
request was being counted as *better* than many admitted ones, the metric's
sign flipped and rejection was rewarded instead of penalised. Charging the real
client timeout (the worst outcome a client can observe) removes this
inversion. The same convention is used by `MERAF.core.metrics.summarize()`
in the experiment runner; the fallback path here must match it for a
like-for-like comparison. The value used is written to every row as
`reject_penalty_ms` and copied into the aggregated CSV.

**Reachability.** `offered_percentile_reachable(reject_rate_pct, p)` is true
when the p-th percentile of the offered distribution still falls inside the
mass of admitted requests, i.e. when `reject_rate_pct / 100 < 1 - p`
(p99 requires < 1 % rejects, p95 requires < 5 %). When the rejected fraction is
at or above `1 - p`, the percentile lands in the censored tail: its numerical
value would equal the penalty and depend only on that arbitrary constant, not
on the data. It is then reported as *not reachable* instead of as a number.
Reachability is evaluated **per seed** and stored in the per-row columns
`latency_p95_offered_reachable` / `latency_p99_offered_reachable`, both by
`run_experiments.py` and by the fallback path.

### 2.5 `aggregate()`

Groups rows by `(algorithm, float(load))` and, for each field in
`NUMERIC_FIELDS` (`throughput_rps, goodput_rps, latency_mean_ms,
latency_p50_ms, latency_p95_ms, latency_p99_ms, reject_rate_pct`), stores
`mean/std/ci95/min/max` across the seeds present in that cell.

**Offered percentiles are handled separately** and intentionally excluded from
`NUMERIC_FIELDS`. For each of p95 and p99 the mean/std/ci95/min/max are
computed **only over seeds whose per-row reachable flag is true**; the count
of such seeds is stored in `latency_p9X_offered_ms_n_reachable`, and all
statistics are NaN when it is zero. The flag check is
`r.get(reach_field) in ("True", True)` because rows from the primary CSV carry
the string `"True"` while rows from the fallback carry a Python bool.

Historical bug: the previous version evaluated reachability once per cell,
on the cell's *mean* reject rate. A cell can contain seeds with reject rate
under the threshold (real offered p99 values, e.g. 4000-5000 ms) and seeds
above it (offered p99 saturated at 60 000 ms). Averaging both produced a
meaningless number. Per-seed filtering fixes this.

`reject_penalty_ms` in the aggregated row is taken from the first row of the
group, defaulting to `CLIENT_TIMEOUT_MS` when the column is missing (older
CSVs).

### 2.6 CI95 correction (historical bug)

`aggregate()` used to compute the 95 % CI from `statistics.pstdev`
(population standard deviation, ddof = 0). The Student-t interval assumes the
sample standard deviation (ddof = 1) in the denominator; with n = 5 seeds the
resulting intervals were systematically too narrow by a factor of
`sqrt(4/5) ≈ 0.89`. This was a substantive error, not merely a stylistic
difference from `run_experiments.py`. Both pipelines now call
`mean_std_ci95`.

### 2.7 LaTeX table

`write_latex_table()` used to be a local copy of the table writer. It was
removed in favour of `MERAF.reporting.make_latex_tables.write_main_comparison_table`
so that the paper table and the aggregation script cannot drift apart.

### 2.8 Command-line options

| Option | Default | Notes |
|---|---|---|
| `--root` | `output` | root directory walked recursively |
| `--outdir` | `aggregated` | destination for CSV and `.tex` files |
| `--horizon-ms` | `20000.0` | fallback path only; must equal the simulated horizon |
| `--slo-ms` | `2000.0` | fallback path only; used for `goodput_rps` (requests within SLO). The help text still describes it as the reject-penalty parameter, which is stale: the penalty is `CLIENT_TIMEOUT_MS` |
| `--verbose` | off | print every matched file and its interpretation |

Exit status is 1 when no input files are found at all.

---

## 3. `equivalence_test.py`

### 3.1 Why an equivalence test

The paper claims that MERAF's latency is "functionally indistinguishable"
across security profiles (the P axis has no measurable effect on latency).
That is a claim of **equivalence**, which is not the same as "no statistically
significant difference": a non-significant t-test can simply mean low power.
Likewise "the differences fall within one standard deviation" is not an
equivalence test. The correct tool is TOST, Two One-Sided Tests, which
rejects a null hypothesis of *non*-equivalence.

### 3.2 Input

`profile_breakdown_<scenario>.csv` from `run_experiments.py`. Its `algorithm`
column does **not** contain the bare algorithm name: it holds the composite
key used internally to build `breakdown_by_algo`,

```
<algo>@<load>@seed<N>        KEY_RE = ^(?P<algo>.+)@(?P<load>[0-9.]+)@seed(?P<seed>\d+)$
```

Rows whose key does not match are silently skipped. `load_profile_breakdown()`
returns `{(algo, load): {profile: [metric value per seed]}}` for the selected
`--metric` (`latency_mean_ms`, `latency_p50_ms`, `latency_p95_ms` (default),
`latency_p99_ms`).

### 3.3 Test procedure

For every (algorithm, load) cell and every unordered pair of profiles
`(pa, pb)` with `pa < pb` lexicographically, `tost_equivalence(a, b, margin)`
is applied to the two per-seed vectors:

- H0 (non-equivalence): `|mean_a - mean_b| >= margin`;
  H1 (equivalence): `|mean_a - mean_b| < margin`.
- Welch statistic with Satterthwaite degrees of freedom, equal variances are
  **not** assumed, because one profile class can have a different number of
  eligible servers or a heavier tail than another.
- `t_lower = (diff + margin) / se` tests H0: `diff <= -margin`;
  `t_upper = (diff - margin) / se` tests H0: `diff >= +margin`.
- Equivalence is declared iff **both** one-sided tests reject at
  alpha = 0.05: `t_lower > t_crit` and `t_upper < -t_crit`, with `t_crit` the
  one-sided 5 % critical value for `df`. `alpha` is fixed at 0.05 by the
  critical-value table and is not a free parameter.
- Fewer than 2 observations in either group: no inference, `equivalent=False`,
  `reason` explains why.
- Zero sample variance in both groups (deterministic identical values): the
  decision is `abs(diff) < margin` directly, with `df=inf`, `t_crit=0`.

### 3.4 The margin

`--margin-ms` (default 100 ms) is a **judgement of practical relevance**, not
a data-derived quantity: 100 ms is 5 % of the 2000 ms SLO used in the
simulation, encoding "a difference smaller than 5 % of the SLO is irrelevant
to the end user even if statistically detectable". Change it if your relevance
criterion differs, and report the value used (it is stored per row as
`margin_ms`).

### 3.5 Output

`equivalence_<scenario>.csv` in `--result-dir` (default `--outdir`), one row
per (cell, profile pair) with columns

```
scenario, algorithm, load, metric, profile_a, profile_b, n_a, n_b,
mean_a, mean_b, diff, margin_ms, t_lower, t_upper, t_crit, df, equivalent, note
```

Numeric statistics are formatted as strings (`.3f`, `df` as `.2f`); NaN is
written as the literal `nan` (the `x == x` idiom detects NaN). Unless
`--no-latex` is given, `generate_equivalence_table(out_path)` immediately
writes `equivalence_<scenario>_table.tex` next to the CSV. Non-equivalent
pairs are listed on the console. Exit status is 1 if no scenario produced at
least one comparable pair (needs >= 2 profiles per cell).

---

## 4. `analyze_client_timeout.py`

### 4.1 Motivation

Reviewers asked: "what if the client's timeout were much lower than 60 s?"
This script answers without new simulations by reinterpreting the saved
per-request logs of a scenario under a hypothetical client abandonment
threshold `X` (`--thresholds-ms`, default 5 s, 10 s, 20 s).

### 4.2 Reinterpretation rule

For each (algorithm, load, seed):

- `total` generated requests comes from `summary_<scenario>_raw_perseed.csv`
  (`load_counts()`), because the per-request log lists completed requests
  only; `total` includes rejected requests and requests still in flight at the
  end of the horizon.
- A request is a **success** if it appears in
  `completed_<scenario>_<algo>_<load:.3f>_seed<seed>.csv` with `total_ms <= X`.
- Everything else is a **failure**: rejected, in flight at horizon end, or
  completed after `X`.

**Conservative charging convention** (the same as the paper's offered
metric): every failure is charged exactly `X` in the client-view distribution,
including rejections, which in reality are near-instantaneous. Charging
rejects ~0 would favour the algorithm that rejects (MERAF), so `X` is the
worst case for it.

Per-seed quantities: `success_pct = 100 * #ok / total`,
`goodput@X = #ok / HORIZON_S`, and censored percentiles
`pctile(sorted(ok) + [X] * (total - #ok), 95 | 99)`. Per-(algorithm, load, X)
they are summarised with `mean_std_ci95` over the available seeds; missing
seeds (no log or no count) are skipped.

### 4.3 Censoring / saturation

The client-view distribution is censored at `X`: the client never observes a
latency above its own timeout. Consequently, if more than `(100 - p)` % of the
generated requests fail, the p-th percentile equals exactly `X`. The output
flags `p95_saturated_at_threshold = (100 - success_mean) > 5` and
`p99_saturated_at_threshold = (100 - success_mean) > 1`, evaluated on the
**mean** success rate across seeds. This is the variable-threshold analogue of
the paper's reachability rule (section 2.4). The console report prints
`=Xs sat.` in place of a number for saturated cells.

### 4.4 Output

`<outdir>/client_timeout_view.csv`, one row per (algorithm, load, X):

```
algorithm, load, threshold_ms, success_pct_mean, success_pct_ci95,
goodput_rps_mean, goodput_rps_ci95, p95_client_ms_mean, p95_client_ci95,
p95_saturated_at_threshold, p99_client_ms_mean, p99_client_ci95,
p99_saturated_at_threshold
```

followed by a console table per (load, X) sorted by goodput descending.

### 4.5 Constants and assumptions

- `HORIZON_S = 30.0` is hard-coded and must match the horizon of the runs being
  reinterpreted (the request-level experiments in the paper use 30 s).
- `ALGOS` is the hard-coded list of internal algorithm identifiers as they
  appear in file names; the paper renders `security-constraint-*` as `sec-*`.
- Defaults: scenario `rete`, loads `0.12 0.16` req/ms, seeds `1..5`.

---

## 5. `analyze_deadline_sensitivity.py`

### 5.1 Purpose

Second robustness check of the workflow-level evaluation. Workflow goodput
(paper Eq. wf_goodput) counts workflows completed within the deadline
`D_w = depth * SLO` (SLO = 2000 ms). This script recomputes goodput with the
deadline multiplied by `k` (`D_w' = k * depth * SLO`) for several `k`
(`--ks`, default `0.75 1.0 1.5 2.0`), using the saved per-workflow logs. The
makespan and depth of each completed workflow are enough to re-evaluate the
deadline condition a posteriori, so no simulation is rerun.

The paper's claim, "recomputing the goodput with the deadline multiplier
varied from 0.75x to 2x shifts the plateau's level but preserves its
flatness", is checked by reading `goodput_mean` for a fixed `k` across
loads.

### 5.2 Inputs

`workflows_<scenario>_<topology>_<profile_mode>_<algorithm>_<load:.3f>_seed<i>.csv`
in `--outdir` (default `MERAF/results/outputs_wf_by_stage`, `--profile-mode
by_stage`, `--algorithm meraf-workflow-admission`). The set of loads for a
(scenario, topology) is **discovered from the seed-1 files** using the regex
`_(\d+\.\d+)_seed1\.csv$`; a load with no seed-1 file is not analysed even if
other seeds exist.

`goodput_for_k(file, k)` counts rows with `completed == "True"` (string
comparison, the CSV stores Python bool repr) and
`makespan_ms <= k * depth * SLO_MS`, divided by `HORIZON_S = 30.0`, which
must match `run_workflow_experiments.py`.

### 5.3 Output

`<outdir>/deadline_sensitivity.csv`, one row per (scenario, topology, load, k):
`n_seeds, goodput_mean, goodput_ci95` (rounded to 3 decimals). The console
then prints, per (topology, k), the min-max range of `goodput_mean` over loads
`>= 0.02` req/ms (the plateau region). This is the number quoted in the
paper.

---

## 6. `analyze_profile_pairing.py`

### 6.1 Purpose

Compares two ways of assigning security profiles to workflow stages,
`by_stage` (deterministic per stage) and `random`, on the workflow completion
rate, for the last paragraph of the workflow-level evaluation.

### 6.2 Paired design

Both runs use the same seeds (default 1..5) for each (scenario, topology,
load) cell, so the per-seed difference
`d_s = completion(by_stage, s) - completion(random, s)` is **paired**. The
statistic is the paired t

```
t = mean(d) / (sd(d) / sqrt(n))        sd with ddof = 1
```

Edge cases:

- `sd == 0` and `mean == 0`: the two runs produced identical completion rates
  on every seed (typical in the unsaturated regime). `t = 0`, no evidence.
- `sd == 0` and `mean != 0`: a constant non-zero difference on every seed.
  `t = inf`; written as the string `"inf"` with `significant_t2 = True`.
- Fewer than 2 paired seeds: the cell is skipped.

Inputs are the `completion_rate_pct` column of
`workflow_summary_<scenario>_<topology>_<mode>_raw_perseed.csv` in
`--bystage` and `--random`, filtered to `--algorithm`. Only loads present in
**both** directories are compared.

### 6.3 Significance heuristic

The flag `significant_t2 = |t| > 2` is a rule of thumb, not an exact test.
With `n = 5` seeds (`df = 4`) the two-sided probability `P(|t| > 2)` under the
null is about 0.116, so with `N` cells one expects `~0.116 * N` flagged cells
by chance alone. The console summary prints the total number of cells, the
number with `|t| > 2` (finite `t` only), the chance expectation, and the
flagged cells sorted by `|t|` descending. The paper's argument is that the
observed count is close to this expectation, i.e. the pairing does not affect
the completion rate.

### 6.4 Output

`<bystage>/profile_pairing_ttest.csv`, one row per cell:
`scenario, topology, load, n, diff_pp_mean, diff_pp_sd, t, significant_t2`
(differences in percentage points). Default scenarios `rete locale`,
topologies `linear fan_out_in montage_like`.

---

## 7. Known limitations and inconsistencies

- **Horizon constants differ between scripts.** `aggregate_results.py`
  defaults `--horizon-ms` to 20000, while `analyze_client_timeout.py` and
  `analyze_deadline_sensitivity.py` hard-code `HORIZON_S = 30.0`. Each must be
  set to the horizon actually used by the runs being processed; a mismatch
  silently scales throughput/goodput and (in the fallback path) the
  reconstructed request count.
- **`--slo-ms` help text is stale** in `aggregate_results.py`: it says the SLO
  drives the reject penalty, but the penalty is `CLIENT_TIMEOUT_MS`; the SLO
  is only used to count within-SLO completions for `goodput_rps`.
- **Unused imports** kept for now: `tost_equivalence` in
  `aggregate_results.py`; `math` and `statistics` in
  `analyze_deadline_sensitivity.py`.
- **Saturation flags use the mean success rate** across seeds
  (`analyze_client_timeout.py`), whereas reachability in `aggregate_results.py`
  is per seed. A cell can therefore be "not saturated" on average while some
  seeds individually are.
- **`inf` t-statistics** in `analyze_profile_pairing.py` are flagged
  significant in the CSV but excluded from the console count of `|t| > 2`
  cells (the count only considers finite `t`).
- **Load discovery from seed 1** in `analyze_deadline_sensitivity.py` means
  cells lacking a seed-1 log are dropped entirely.
- **Algorithm list is hard-coded** in `analyze_client_timeout.py` (`ALGOS`);
  new algorithms must be added there by hand.
- **String booleans.** Several scripts compare CSV cells against the string
  `"True"` (`completed`, `latency_p9X_offered_reachable`). Any change in how
  the runners serialise booleans breaks these checks.
- **Fallback reconstruction assumes** the workload generator is unchanged
  since the run: any change to `WorkloadGenerator`'s RNG stream makes the
  rebuilt `total_requests` wrong. Prefer the primary path whenever
  `summary_*_raw_perseed.csv` is available.

## 8. Historical note: `aggregate_results.py` was not importable

An early version contained the placeholder line
`@dataclass_fields_helper = None`, a decorator `@` followed by an assignment,
which is a `SyntaxError`. The script failed at import time, before any code
ran. It was removed when the shared `stats_utils` functions and the reporting
module replaced the local copies (§2.6, §2.7).

# MERAF experiment scripts: engineering reference

This document is the detailed reference for the scripts in `MERAF/experiments/`.
The in-code comments are intentionally terse; the design rationale, the
meaning of every algorithm variant, the statistical conventions, the
resumability protocol and the historical bug notes that explain current
behaviour live here.

Scripts covered:

| Script | Purpose | Main outputs |
|---|---|---|
| `run_experiments.py` | Independent requests: MERAF vs ablations vs baselines over loads x scenarios x seeds | `summary_<scenario>_{raw_perseed,aggregated}.csv`, `profile_breakdown_<scenario>.csv`, `completed_*.csv`, `.tex` tables |
| `run_workflow_experiments.py` | Task DAGs: per-task vs per-workflow admission, plus baselines | `workflow_summary_<scenario>_<topology>_<profile_mode>_{raw_perseed,aggregated}.csv`, `workflows_*.csv` |
| `sensitivity_sweep.py` | One-factor-at-a-time hyperparameter sweep of MERAF vs best baseline | `sensitivity_sweep_<scenario>_load<load>.csv`, `.tex` table |

All scripts are run as modules from the directory that contains the `MERAF`
package:

```
python3 -m MERAF.experiments.run_experiments
python3 -m MERAF.experiments.run_experiments --horizon-ms 60000 --loads 50 150 300
python3 -m MERAF.experiments.run_workflow_experiments \
    --topologies linear fan_out_in --loads 0.02 0.06 0.12 0.16 \
    --scenarios locale rete --seeds 1 2 3 4 5
python3 -m MERAF.experiments.run_workflow_experiments --profile-mode random ...
python3 -m MERAF.experiments.sensitivity_sweep --scenario locale --load 0.16 --seeds 1 2 3
```

---

## 1. Shared experimental design

### 1.1 Scenarios

Two scenarios, selected with `--scenarios` (default: both):

| Scenario | Meaning | Bandwidth per node | Base RTT |
|---|---|---|---|
| `locale` | Client and backends on the same host (loopback) | ~900-2200 MB/s | 0 ms |
| `rete` | Backends reached over a real network | ~60-180 MB/s | 120-400 ms |

The bandwidth figures come from the empirical measurement reports
(`report_locali.md`, `report_rete.md`) that accompany the paper. Scenario
names are used verbatim in file names and in `BackendSpec.name`
(`<scenario>-be<i>`).

### 1.2 Loads

`--loads` is a list of arrival rates in requests/ms (or workflows/ms for the
workflow script). The default grid `0.02 0.06 0.12 0.16` was chosen so that
the highest levels sit around and above the network bandwidth ceiling of the
`rete` fleet, i.e. it spans from comfortable to sustained overload. The same
four values are reused by the workflow script so that per-request and
per-workflow results are comparable in load intensity.

### 1.3 Seeds and replicas

`--seeds` (default `1 2 3 4 5`) gives one independent replica per seed for
every (scenario, algorithm, load) cell. The seed drives:

- the workload generator (arrival times, sizes, profiles, tenant assignment);
- the dispatcher's internal RNG, offset per dispatcher family so that two
  algorithms never share a random stream:

  | Dispatcher | Seed offset |
  |---|---|
  | MERAF and all MERAF variants | `seed + 1000` |
  | Power-of-2 choices | `seed + 2000` |
  | Least connections | `seed + 3000` |
  | EWMA least response time | `seed + 4000` |
  | Security-constraint least-loaded | `seed + 5000` |
  | Security-constraint fastest | `seed + 6000` |
  | Round robin | deterministic, no seed |

  Least-connections originally had no seed: its random tie-break used the
  same sequence in every replica, which under-estimated the real
  between-seed variance. It now takes `seed + 3000`.

The same offsets are used by all three scripts, so a given (algorithm, seed)
pair is the same dispatcher instance everywhere.

### 1.4 Tenants

`--tenants` (default 50) is the number of independent clients. Requests are
assigned to `tenant0 … tenant{N-1}`.

**Historical bug.** An early version gave every request the same tenant
(`"default"`). MERAF's session-affinity bonus is designed for a single real
client, not for the aggregate traffic; with one shared tenant it pinned the
entire simulation to a single backend as soon as resource risk stopped
differentiating candidates. With a pool of independent tenants the affinity
applies per client, as in Algorithm 1 of the paper. Running with `--tenants 1`
reproduces the pathological behaviour and is not a meaningful configuration.

### 1.5 Fleet construction (`run_experiments.make_backend_fleet`)

`make_backend_fleet(scenario, n_backends=4, slo_ms=2000.0)` is shared by all
three scripts.

**Heterogeneity.** The last node (`i == n_backends - 1`) is deliberately
weaker on every axis so that the load-balancing decision is non-trivial:

| Parameter | Strong node | Weak node |
|---|---|---|
| Net capacity, `locale` | 2200 MB/s | 900 MB/s |
| Net capacity, `rete` | 180 MB/s | 60 MB/s |
| CPU capacity (`cpu_capacity_per_ms`) | 40 | 15 |
| Decrypt capacity (`decrypt_capacity_per_ms`) | 20 | 8 |
| Memory | 4096 MB | 2048 MB |
| Base RTT, `rete` | 120 ms | 400 ms |
| Base RTT, `locale` | 0 | 0 |

MB/s is converted to bytes/ms with the approximation 1 MB/s ≈ 1024 byte/ms.

**Resource thresholds.** The admission thresholds `h_cpu`, `h_net`,
`h_decrypt` are *derived per node* with `model.derive_resource_threshold`
from (a) the node's real capacity, (b) the target completion time `slo_ms`,
and (c) the cost of a reference request: a 5 MiB DECRYPT request with KEM
envelope, costed with `CostCalibration().cost_decrypt(...)`. Its bytes, CPU
ms (excluding decryption) and decrypt ms are the reference units for the
three axes. `h_mem` is a constant 0.9.

*Historical bug.* An earlier version used a constant `h_net = 8.0` for every
node. For the strongest nodes that value was up to ~9x lower than what their
real bandwidth implied, which made MERAF reject far more requests than
necessary and distorted the comparison with the baselines. Deriving the
thresholds from capacity removed this artefact.

**Security tiers.** Each `BackendSpec` carries a `security_tier`:
2 = highly trusted (lht), 1 = semi-trusted (lst), 0 = untrusted (lut),
following the three-level classification of Singh & Auluck (2020). The
first `max(1, n_backends // 4)` nodes are tier 2, the next
`max(1, n_backends // 4)` are tier 1, the rest are tier 0. With the default
4-node fleet: one lht node, one lst node, two untrusted nodes. The minority
of trusted nodes reflects the standard assumption of that literature
(trusted/premium infrastructure is scarce). The tier is **only** read by the
two security-constraint baselines; MERAF and the security-agnostic baselines
ignore it. With the default fleet the tier assignment is
independent of capacity: the weak node is always untrusted.

### 1.6 Candidate sampling (`candidates_d`)

`MERAFDispatcher` samples `candidates_d` nodes per request (power-of-d
choices). The default is 6 and the default fleet has 4 nodes, so
`d >= n_backends` and MERAF scans the **whole fleet** on every request; the
sampling mechanism is inert in the paper's default configuration and must
not be described as a verified scalability feature. `candidates_d` is an
explicit parameter of `build_dispatcher` (rather than a literal repeated in
every branch) precisely so this can be tested:

```
python3 -m MERAF.experiments.run_experiments --n-backends 50 --candidates-d 6 ...
```

The default of 6 was kept unchanged so that previously reported results with
the 4-node fleet are not altered.

### 1.7 Statistics (`_aggregate` in both runners)

Every aggregated cell reports, per metric, `mean`, `std`, `ci95`, and (for
the request runner) `min`/`max`, computed by
`MERAF.core.stats_utils.mean_std_ci95`:

- **sample** standard deviation (ddof = 1);
- 95% confidence half-width from the Student-t distribution with n−1 degrees
  of freedom.

*Historical bug.* `_aggregate` originally used the **population** standard
deviation (`statistics.pstdev`, ddof = 0) and produced no confidence
interval, while the offline pipeline (`aggregate_results.py`, used for the
paper's tables) computed a CI but from the same population deviation. The
Student-t formula assumes the sample deviation in the denominator; with n = 5
replicas the two differ by about 12%, which is not negligible at the
precision the numbers are reported with. Both pipelines now call the same
function so they cannot diverge silently.

---

## 2. `run_experiments.py`: independent requests

### 2.1 What it does

For each scenario, load and algorithm in `ALGORITHMS`, and each seed, it
builds a fresh fleet and dispatcher, generates a request stream with
`WorkloadGenerator`, runs the `Simulator`, and summarises the run with
`metrics.summarize` and `metrics.per_profile_breakdown`.

### 2.2 Outputs (in `--outdir`, default `MERAF/results/outputs`)

| File | Content |
|---|---|
| `summary_<scenario>_raw_perseed.csv` | One row per (algorithm, load, seed): every `RunSummary` field. Intended for external statistical analysis. |
| `summary_<scenario>_aggregated.csv` | One row per (algorithm, load): mean/std/ci95/min/max over seeds (see §1.7 and §2.5). |
| `completed_<scenario>_<algo>_<load:.3f>_seed<N>.csv` | Per-request log of one replica. Suppressed with `--no-save-completed` to save disk on long runs. |
| `profile_breakdown_<scenario>.csv` | Metrics per (T, P) security profile, keyed `<algo>@<load>@seed<N>`. |
| `summary_<scenario>_table.tex` | Main comparison table (`make_latex_tables.write_main_comparison_table`). |
| per-profile `.tex` tables | One per load for `--profile-table-algo` (default `meraf`, matching Tables 6/7 of the paper), via `generate_profile_breakdown_table`. |

LaTeX tables are generated right after the CSVs so that no second manual
command is needed; `--no-latex` disables this. `make_latex_tables.py`
remains usable from the command line to regenerate tables for a different
algorithm or load.

### 2.3 Command-line options

| Option | Default | Notes |
|---|---|---|
| `--horizon-ms` | 30000 | Simulated duration. |
| `--loads` | `0.02 0.06 0.12 0.16` | Requests/ms, see §1.2. |
| `--scenarios` | `locale rete` | |
| `--seeds` | `1 2 3 4 5` | |
| `--outdir` | `MERAF/results/outputs` | |
| `--no-save-completed` | off | Skip per-request logs. |
| `--tenants` | 50 | See §1.4. |
| `--n-backends` | 4 | Fleet size, see §1.5/§1.6. |
| `--candidates-d` | 6 | See §1.6. |
| `--no-latex` | off | |
| `--profile-table-algo` | `meraf` | |

The per-cell SLO used for thresholds and summaries is fixed at 2000 ms in
`run_one` (not exposed on the CLI here; `sensitivity_sweep.py` exposes it as
`--slo-ms`).

### 2.4 Algorithms and ablation variants (`build_dispatcher`, `ALGORITHMS`)

All MERAF variants share the same fleet, the same rho-based admission
control, the same `candidates_d` and, unless stated, the same weights
`ScoreWeights(lam=1.2, eta=0.6, gamma=0.25)`.

#### Full MERAF

- **`meraf`**: the algorithm as described in the paper: linear scoring
  (`slo_penalty_mode="linear"`, SLO term = L95/SLO), cost differentiation by
  payload profile P on, eta = 0.6.

#### Ablations on the security axes

Two "security-awareness" axes enter MERAF's decisions:

- **P (payload profile)**: the per-request cost model distinguishes PLAIN /
  DECRYPT / OPAQUE payloads (`differentiate_cost=True`).
- **T (transport profile)**: a full PQC handshake multiplies the predicted
  latency sigma by `handshake_sigma_multiplier` (1.5x). In the linear scoring
  this multiplier reaches the score **only** through the additive term
  `eta * sigma / SLO`: L95 is computed *before* the multiplier is applied
  (see `dispatchers.py::_score_candidates`). So with eta = 0 the T axis stops
  influencing any dispatch decision under linear scoring.

- **`meraf-no-cost-diff`**: P off (`differentiate_cost=False`): every
  request is costed as PLAIN for admission and resource-risk purposes;
  weights and admission are otherwise identical to `meraf`. If this variant
  is indistinguishable from `meraf`, the observed advantage comes from
  admission control alone, not from cryptographic awareness.
- **`meraf-eta0`**: T off (`eta=0.0`), P on. eta = 0 does **not** remove
  variance information from the score: sigma still enters through L95
  (+1.645·sigma). It removes only the *additive, redundant* count of the same
  variance.
- **`meraf-security-blind`**: T off (`eta=0.0`) and P off
  (`differentiate_cost=False`): admission control only.

Together these form the 2x2 factorial on security awareness, all with
linear scoring:

| | P active | P inactive |
|---|---|---|
| **T active** | `meraf` | `meraf-no-cost-diff` |
| **T inactive** | `meraf-eta0` | `meraf-security-blind` |

If the security-blind cell behaves like `meraf`, the reformulation of the
contribution around admission control alone is a measured result, not an
interpretation.

#### Alternative SLO penalty

- **`meraf-prob-slo`**: `slo_penalty_mode="probability"`: replaces the
  unbounded L95/SLO term (which treats "10x over budget" as ten times worse
  than "1x over budget" at equal variance) with P(latency > SLO) under a
  Gaussian model (`model.prob_exceed_slo`), a term bounded in [0, 1] that
  penalises the probability of violation rather than the expected latency
  relative to the SLO, taking mean and variance into account jointly. Same
  weights, fleet and admission as `meraf`, so the substitution is isolated
  and flows through the whole pipeline (tables, equivalence checks,
  sensitivity sweep) without extra infrastructure.
- **`meraf-prob-slo-eta0`**: probability variant with `eta=0.0`.

Why both eta = 0 variants exist: the pair {L95/SLO, P(exceed SLO)} x
{eta = 0.6, eta = 0} separates the effect of *replacing the SLO term* from
the effect of *double-counting variance*. Both L95 (via +1.645·sigma) and
`eta*sigma/SLO` penalise the same sigma with different weights, the double
count already exists in the original formula, not just in the probability
variant. Probing eta = 0 on only one variant would confound the two effects
in the very comparison meant to isolate them.

Caveat for the T-axis reading: in the probability variant the
post-multiplier sigma enters `prob_exceed_slo` regardless of eta, so
`meraf-prob-slo-eta0` does **not** switch off T. It serves the
double-counting comparison only; the T x P factorial above must always be
read on the linear-scoring variants.

#### Security-agnostic baselines

- **`round-robin`**: `RoundRobinDispatcher`, deterministic.
- **`least-connections`**: `LeastConnectionsDispatcher`, seeded random
  tie-break (see §1.3).
- **`power-of-2`**: `PowerOfTwoChoicesDispatcher`.
- **`ewma-lrt`**: `EWMALeastResponseTimeDispatcher` (EWMA of observed
  response time, pick the lowest).

#### Security-as-constraint baselines

Both treat security as a *static eligibility constraint* (a request may only
go to nodes whose `security_tier` satisfies its requirement) and then apply
an ordinary policy among eligible nodes. Neither has a public reference
implementation; both are adaptations of the published algorithms ([22] Singh &
Auluck 2020, Table 2 / Eq. 3; [24] Wang et al. 2026, SeSiC without batch
rescheduling) built from the full texts in `related/`, see
`core-dispatchers.md` for exactly what each does and does not reproduce.

- **`security-constraint-loadaware`**: `SecurityConstraintLeastLoadedDispatcher`,
  representative of Singh & Auluck (2020): eligibility filter, then
  least-loaded.
- **`security-constraint-satisfaction`**: `SecurityConstraintFastestDispatcher`,
  representative of Wang et al. (2026): eligibility filter, then the lowest
  predicted p95 latency as a proxy for "user satisfaction".

### 2.5 Aggregation details (`_aggregate`)

Aggregated fields: `throughput_rps`, `goodput_rps`, `latency_mean_ms`,
`latency_p50_ms`, `latency_p95_ms`, `latency_p99_ms`, `reject_rate_pct`,
each with `_mean/_std/_ci95/_min/_max`. Constant fields (`algorithm`,
`n_seeds`, `total_requests`, `horizon_ms`, `reject_penalty_ms`) are copied
from the first summary.

**Offered percentiles.** `RunSummary` also carries
`latency_p95_offered_ms` / `latency_p99_offered_ms`, percentiles over the
*offered* load, where rejected requests count with a fixed penalty latency
(`reject_penalty_ms`), together with a boolean
`latency_p95_offered_reachable` / `latency_p99_offered_reachable` that is
true when the reject rate is low enough for the percentile to be a real
latency rather than the penalty value. `_aggregate` evaluates reachability
**per seed**, averages only over reachable seeds, and reports the count as
`<field>_n_reachable`; when no seed is reachable the statistics are NaN.

*Historical bug.* Reachability used to be decided on the cell's *mean*
reject rate. Seeds of the same cell can straddle the threshold (observed:
4 of 5 seeds below 1% rejects, one seed at 2.6%); averaging all of them mixed
real values (~4000-5000 ms) with penalty-saturated ones (60000 ms) into a
number that corresponded to no actual state of the system.

---

## 3. `run_workflow_experiments.py`: task DAGs

### 3.1 Motivation

The paper's original evaluation used independent requests only; the review
pointed out that no scientific workflow (inter-task dependencies) was
modelled. This script extends the pipeline to DAGs of dependent tasks and
tests two hypotheses quantitatively:

1. **Per-task admission is insufficient for workflows.** When each task is
   admitted independently as it becomes ready, a workflow can be rejected
   half-way, after its predecessors have already consumed resources,    wasting the work done so far.
2. **(T, P) composability pays off when the profile is correlated with
   workflow structure** rather than drawn uniformly at random per task
   (`--profile-mode`, §3.4).

### 3.2 Admission modes

| Algorithm | `admission_mode` in output | Behaviour |
|---|---|---|
| `meraf` | `per-task` | Existing MERAF, unchanged. Every task is admitted or rejected independently when it becomes ready, even if its predecessors have completed. |
| `meraf-workflow-admission` | `per-workflow` | `workflow_admission=True`, `admission_criterion="risk"` (default). The whole DAG is accepted or rejected at submission; once accepted, none of its tasks is later rejected for resource risk. The capacity evaluated at admission is **reserved** for the workflow's entire lifetime (`admit_workflow()` / `release_workflow()` in `dispatchers.py`), not merely checked at submission time. Without the reservation, two workflows admitted a few ms apart could jointly exceed the capacity each of them saw, and the system would degrade in latency instead of rejecting. See `admit_workflow` for the exact criterion and its declared limits. |
| `meraf-workflow-deadline` | `per-workflow-deadline` | Ablation ("deadline gate"): same per-DAG decision point, but the criterion is *estimated makespan vs. deadline*, with **no** reservation. |
| `meraf-no-cost-diff` | `per-task` | P axis off, as in §2.4. |
| baselines | `per-task` | See below. |

`run_one` derives `admission_mode` from the dispatcher's `workflow_admission`
and `admission_criterion` attributes and passes it to `summarize_workflows`.

**Baselines.** `round-robin`, `least-connections`, `power-of-2`, `ewma-lrt`,
`security-constraint-loadaware`, `security-constraint-satisfaction` are the
same instances (same seed offsets) as in `run_experiments.py`. They have no
notion of a workflow, so `Simulator._handle_workflow_arrival` treats them in
per-task mode automatically with no code change. Two baseline-specific
consequences worth knowing:

- `security-constraint-loadaware` confines OPAQUE tasks (the intermediate
  stages under `by_stage`) to the tenant's local node only;
- `security-constraint-satisfaction` applies a per-task SLO-based admission
  whose rejections fail the workflow exactly like MERAF's per-task
  rejections do.

### 3.3 Topologies (`--topologies`, `core.workload.WorkflowTopology`)

| Name | Structure | Size parameters |
|---|---|---|
| `linear` | stage0 → stage1 → … → stage(N−1) | `--n-stages` (default 5) |
| `fan_out_in` | ingest → W parallel tasks → aggregate | `--fan-width` (default 4) |
| `montage_like` | W project → concatfit → bgmodel → W background → mosaic (Montage-style astronomy pipeline) | `--fan-width` |

Default: `linear fan_out_in`. Tasks are stored in topological order, sources
first.

### 3.4 Profile modes (`--profile-mode`)

| Mode | Assignment of the payload profile P | Role |
|---|---|---|
| `by_stage` (default) | Correlated with the stage: first stage (ingest) = DECRYPT, intermediate stages = OPAQUE, final stage = PLAIN | The hypothesis under test |
| `random` | Uniform random per task, like `WorkloadGenerator` for independent requests | The control that isolates the effect of structural correlation |

The intended experiment is the same grid run twice, once per mode, and
compared.

### 3.5 Metrics (`core.metrics.summarize_workflows` / `WorkflowSummary`)

Per cell: `completion_rate_pct`, `makespan_mean_ms`, `makespan_p50_ms`,
`makespan_p95_ms`, `makespan_p99_ms`, `goodput_workflows_per_sec`,
`within_deadline_pct`, `wasted_cpu_ms_total`, `wasted_bytes_total`,
`total_workflows`.

- **Deadline** is not a fixed constant: it scales with DAG depth,
  `deadline = depth_in_levels * SLO_per_task`, so each level must meet the
  per-task SLO.
- **Goodput** = completed workflows within deadline per second of horizon.
  It is the headline metric because it captures the real trade-off between
  completion and makespan; taken separately, those two can hide the same
  ambiguity already seen between admitted-p99 and offered-p99 for
  independent requests (§2.5).
- **Wasted work** (`wasted_cpu_ms_total`, `wasted_bytes_total`) is a
  *diagnostic* of the per-task admission problem, how much is thrown away
  when a workflow fails mid-way, not evidence in favour of per-workflow
  admission, which drives it to zero by construction (it rejects before
  starting). The "0 vs N" comparison is therefore nearly tautological; the
  comparison that matters is on `completion_rate_pct` and goodput.

### 3.6 Outputs (in `--outdir`, default `MERAF/results/outputs_wf`)

| File | Content |
|---|---|
| `workflow_summary_<scenario>_<topology>_<profile_mode>_raw_perseed.csv` | One row per (algorithm, load, seed). Appended and flushed per cell (§3.7). |
| `workflow_summary_<scenario>_<topology>_<profile_mode>_aggregated.csv` | One row per (algorithm, load), rewritten at the end of each block. |
| `workflows_<scenario>_<topology>_<profile_mode>_<algo>_<load:.3f>_seed<N>.csv` | Per-workflow log of one replica (`write_workflow_records_csv`). Suppressed with `--no-save-records`. |

### 3.7 Resumability

The full grid is long-running, so the script is designed to be interrupted
(Ctrl-C, kill, shutdown) and relaunched with the **same command**:

1. Each (algorithm, load, seed) cell is appended to the block's
   `raw_perseed.csv` **immediately** after it is computed and the file is
   flushed, so an interruption in the middle of a (scenario, topology) block
   loses none of the cells already finished in that block.
2. On start, `_load_existing_cells(raw_file)` reads any existing
   `raw_perseed.csv` for the block (if present and non-empty) and returns its
   cells keyed by `(algorithm, load, seed)`. Those cells are skipped and their
   rows reused for aggregation. The number of resumed cells is printed.
3. `--restart` ignores existing CSVs and recomputes everything from scratch
   (e.g. after a code change that invalidates saved results).

Implementation details that matter:

- The raw file is opened in `"w"` mode when there are no existing cells (or
  `--restart`) and in `"a"` mode otherwise. The header is written only in the
  former case (`need_header`). The `csv.DictWriter` is created lazily on the
  first *new* row because it needs the column names.
- `_cast_row` converts a reloaded row (all strings) back to the types
  produced by a fresh run: `_STR_FIELDS = {algorithm, topology, admission_mode}`
  stay strings, `seed` becomes `int`, everything else `float`. This is what
  allows `_aggregate` to accept plain dicts (not `WorkflowSummary` objects)
  and treat fresh and resumed cells identically through one code path.
- Resumption is keyed only on (algorithm, load, seed) within a file whose
  name already encodes scenario, topology and profile mode. Changing
  `--horizon-ms`, `--tenants`, `--n-stages` or `--fan-width` between runs
  without `--restart` will silently mix incompatible cells; the script does
  not detect this.
- Results are independent of `--duty-cycle` (§3.9), so a run can be resumed
  with a different duty cycle.

### 3.8 Automatic divergence check

If both `meraf` and `meraf-workflow-admission` are among `--algorithms`, the
mean completion rate of every (scenario, topology, load) cell is collected
for each of the two (resumed cells included) and, at the end of the run,
`stats_utils.assert_variants_differ` raises `SuspiciousIdenticalResultsError`
if they coincide **exactly** on more than `max_identical=0` non-trivial cells
(a cell is trivial when the value is 0.0 or 100.0, i.e. nothing or
everything completed). The process therefore *fails* instead of leaving
results to be analysed.

Rationale: two structurally different admission mechanisms producing
identical results has, in this work, always been the signature of a bug
rather than a coincidence. It was observed three times: the shared-tenant
bug (§1.4), an EWMA-LRT cold-start bug, and a per-workflow admission that
protected nothing. The check applies only to pairs of mechanisms that must
differ by design, **never** to ablations, where "no difference" is a
legitimate outcome. For that reason `meraf-workflow-deadline` is *not*
asserted against the risk variant; its cells are collected
(`per_deadline_cells`) for the summary log only.

### 3.9 CPU throttling (`--duty-cycle`)

`--duty-cycle d` with `0 < d <= 1` (default 1.0 = no throttling) limits
average CPU usage without depending on `cpulimit` or cgroups, which are not
always available. After each computed cell, the process sleeps for
`elapsed * (1 - d) / d`, where `elapsed` is the wall time the cell took;
e.g. `d = 0.5` works for T then sleeps for T, keeping the CPU at ~50% on
average. This only slows the pace: results are bit-identical for any value,
and resumability is unaffected. Intended for long unattended runs on a
machine that would otherwise overheat.

### 3.10 Aggregation and NaN handling (`_aggregate`)

Same statistics as §1.7. Additionally, each field is aggregated only over
seeds whose value is not NaN and the count is reported as `<field>_n_valid`.

*Historical bug.* `makespan_*` are NaN for a seed in which **no** workflow
completed (no data from which to compute a percentile). This is a legitimate
edge case under sustained overload: at high load the completion rate can
fall below 1%, and with 5 seeds it is not rare that one completes nothing.
However, `statistics.stdev` does not tolerate NaN and raised an unhandled
exception that aborted a nearly complete grid run. Filtering NaN without
reporting it would hide that a cell is partially or wholly without makespan
signal, hence the explicit `_n_valid` column.

### 3.11 Command-line options

| Option | Default | Notes |
|---|---|---|
| `--horizon-ms` | 30000 | |
| `--loads` | `0.02 0.06 0.12 0.16` | Workflows/ms. |
| `--scenarios` | `locale rete` | |
| `--topologies` | `linear fan_out_in` | Also `montage_like`. |
| `--n-stages` | 5 | `linear` only. |
| `--fan-width` | 4 | `fan_out_in` / `montage_like`. |
| `--profile-mode` | `by_stage` | Or `random`. |
| `--algorithms` | all of `ALGORITHMS` | |
| `--outdir` | `MERAF/results/outputs_wf` | |
| `--seeds` | `1 2 3 4 5` | |
| `--tenants` | 50 | |
| `--no-save-records` | off | |
| `--restart` | off | §3.7 |
| `--duty-cycle` | 1.0 | §3.9 |

The module docstring is used as the argparse description, so `--help`
prints it verbatim. The fleet is always the default 4-node fleet with
SLO 2000 ms (`make_backend_fleet(scenario)`); `--n-backends` and
`--candidates-d` are not exposed here.

---

## 4. `sensitivity_sweep.py`: hyperparameter sensitivity

### 4.1 Motivation

Several model hyperparameters were set by assumption, without calibration
or sensitivity analysis:

- the sigma multiplier for full PQC handshakes (originally hard-coded to
  1.5x in `dispatchers.py`, now `MERAFDispatcher.handshake_sigma_multiplier`);
- the "sticky" session-affinity tolerance (1.10, `sticky_tolerance`);
- `beta_relay`, the OPAQUE relay/buffering cost factor
  (`CostCalibration.beta_relay`);
- the scoring weights lambda / eta / gamma (Eq. 5 of the paper), set in
  `run_experiments.build_dispatcher` to (1.2, 0.6, 0.25) although the paper
  describes them as hyperparameters whose optimal values should be
  established by rigorous empirical calibration.

The review explicitly asked for a coarse sweep showing that the *ranking*
is stable across parameter values. This script provides that.

### 4.2 Method

One-factor-at-a-time (OFAT): for each parameter in `--params`, every grid
value is tried while all other parameters stay at their defaults. A full
multi-parameter factorial was rejected because the run count grows too fast
for a tool meant to run in minutes.

For each (parameter, value) and each seed:

1. `run_meraf(...)` runs MERAF with the modified parameter and returns its
   `latency_p99_ms`. Fleet from `make_backend_fleet(scenario, slo_ms)`;
   `candidates_d=6`; seed offset `+1000`.
2. `run_baseline_best_p99(...)` runs each of `BASELINE_NAMES`
   (`round-robin`, `least-connections`, `power-of-2`, `ewma-lrt`) with the
   same seed, fleet and calibration and returns the **lowest** p99 among
   them. `beta_relay` changes the `CostCalibration` shared by
   MERAF and the baselines, so the baselines' costs also move along that
   axis; the other five parameters affect MERAF only.

Across seeds, both p99 series are reduced with `mean_std_ci95` and the
`advantage_ratio = best_baseline_p99_mean / meraf_p99_mean` is reported
(> 1 means MERAF has the lower p99; NaN if MERAF's mean p99 is 0). The
security-constraint baselines are not part of this comparison.

### 4.3 Grid

| Parameter | Grid | Default |
|---|---|---|
| `handshake_sigma_multiplier` | 1.0, 1.25, 1.5, 2.0, 3.0 | 1.5 |
| `sticky_tolerance` | 1.00, 1.05, 1.10, 1.20, 1.50 | 1.10 |
| `beta_relay` | 0.5, 0.75, 1.0, 1.5, 2.0 | 1.0 |
| `lambda` | 0.6, 0.9, 1.2, 1.8, 2.4 | 1.2 |
| `eta` | 0.3, 0.45, 0.6, 0.9, 1.2 | 0.6 |
| `gamma` | 0.0, 0.125, 0.25, 0.5, 1.0 | 0.25 |

Each grid is centred on the default used by `run_experiments.build_dispatcher`
(`SWEEPS` / `DEFAULTS` in the script). The default value is always a grid
point, so the sweep reproduces the main-experiment configuration at that
point.

### 4.4 Outputs and options

Output: `<outdir>/sensitivity_sweep_<scenario>_load<load:.3f>.csv` with
columns `param, value, meraf_p99_mean, meraf_p99_ci95,
best_baseline_p99_mean, best_baseline_p99_ci95, advantage_ratio, n_seeds`,
followed (unless `--no-latex`) by a `.tex` table from
`make_latex_tables.generate_sensitivity_table`.

| Option | Default | Notes |
|---|---|---|
| `--scenario` | `locale` | Single scenario per run. |
| `--load` | 0.16 | The most interesting overload regime of the paper. |
| `--horizon-ms` | 20000 | Shorter than the main runs, for speed. |
| `--seeds` | `1 2 3` | |
| `--tenants` | 50 | |
| `--slo-ms` | 2000 | Used both for thresholds and summaries. |
| `--params` | all six | Subset of `SWEEPS` keys. |
| `--outdir` | `MERAF/results/outputs` | |
| `--no-latex` | off | |

### 4.5 Interpretation

If `advantage_ratio` stays > 1 across the whole grid of every parameter, the
paper's main result does not depend fragilely on the chosen defaults.
Otherwise the paper must report the threshold beyond which the advantage
reverses. The sweep measures p99 only; it says nothing about reject rate or
goodput, which should be checked with `run_experiments.py` at the
parameter values of interest.

---

## 5. Assumptions and known limitations

- **Fleet realism.** The fleet is a stylised 4-node model calibrated on two
  measurement reports; capacities are constant over time, the weak node is
  always the last one, and security tiers are assigned by index, not by
  capacity.
- **Power-of-d sampling is inert by default** (§1.6). Any claim about MERAF's
  scalability via candidate sampling requires `--n-backends > --candidates-d`.
- **Security-constraint baselines are adaptations** of published algorithms
  without a public reference implementation (§2.4); see `core-dispatchers.md`
  for their scope.
- **Probability-mode variants do not isolate the T axis** (§2.4): the T x P
  factorial is valid only for linear scoring.
- **Wasted-work metrics are diagnostic only** (§3.5).
- **Resume does not validate run parameters** beyond scenario, topology and
  profile mode (§3.7). Use `--restart` after any change to code or to the
  other CLI parameters.
- **The divergence check is a heuristic**: it compares aggregated means for
  exact equality and is meant to catch gross bugs (a variant that silently
  does nothing), not to test statistical significance.
- **The sensitivity sweep is OFAT** and p99-only (§4.2, §4.5); interactions
  between parameters are not explored.
- **Seeds are shared across algorithms by design** (same workload per seed,
  different dispatcher RNG streams); comparisons within a seed are paired.

## 6. Historical bug notes (index)

| Where | Bug | Fix / current behaviour |
|---|---|---|
| §1.4 | Single shared tenant let MERAF's session affinity pin all traffic to one backend | Pool of `--tenants` independent tenants |
| §1.5 | Constant `h_net = 8.0` for all nodes, up to ~9x too low for strong nodes, inflated reject rate | Thresholds derived per node from capacity and SLO |
| §1.3 | Least-connections tie-break unseeded, under-estimating between-seed variance | `seed + 3000` |
| §1.7 | Population std (ddof=0) and no CI in `_aggregate`; offline pipeline computed CI from population std | Both use `stats_utils.mean_std_ci95` (sample std, Student-t CI) |
| §2.5 | Offered-percentile reachability decided on the cell mean, mixing real and penalty-saturated values | Reachability per seed, `_n_reachable` reported |
| §3.10 | `statistics.stdev` crashed on NaN makespan when a seed completed no workflow | Per-field NaN filtering with `_n_valid` |
| §3.8 | Identical results between per-task and per-workflow admission (and, earlier, shared-tenant and EWMA-LRT cold-start bugs) went unnoticed | End-of-run `assert_variants_differ` fails the run |
| §3.2 | Per-workflow admission checked capacity at submission only, so near-simultaneous admissions overcommitted the fleet | Capacity reserved for the workflow's lifetime |

## 7. Modelling simplifications relative to the real benchmark

Points originally stated in the package README, kept here because they bound
how the simulated numbers may be quoted. The structure of the algorithm and
the relative comparison between MERAF and the baselines are unaffected;
absolute numbers are not directly comparable to the real HTTP benchmark
until these are recalibrated.

- **Calibration source.** The `CostCalibration` constants are calibrated on
  the mean/median values of the real benchmark reports
  `CommunicationProtocolsEvaluation/risultati_locali_dettaglio.md` and
  `CommunicationProtocolsEvaluation/risultati_rete_dettaglio.md` (referred to
  as `report_locali.md` / `report_rete.md` elsewhere in these docs). They are
  single point estimates; with more replicas, recalibrate them on the observed
  distribution.
- **Fleet capacities are illustrative.** `make_backend_fleet` is dimensioned
  to produce qualitatively correct behaviour (saturation, selective
  admission), not to copy the 1 vCPU / 4 GB container used in the real tests.
  Substitute the real capacities if absolute comparability with the benchmark
  is wanted.
- **Network model.** Pure processor sharing on bandwidth plus a fixed RTT
  (`base_rtt_ms`). Packet loss, TCP congestion control and fragmentation are
  not modelled (listed as future work in the paper's Results section).
- **OPAQUE relay cost.** `gamma_io_ms_per_byte` is a proportional estimate
  (0.15 x the cipher cost), not a measurement: the benchmark has no relay-only
  test bed that separates I/O cost from a real backend, the same experimental
  gap already noted in the paper.
- **No energy model.** Only CPU and network time are simulated, as stated in
  the paper.
- **Weights and thresholds.** The default `lambda, eta, gamma` and the derived
  `h_k` are a reasonable starting point, not the outcome of a full calibration
  (grid search or Bayesian optimisation). The OFAT sweep of §4 is a partial
  but concrete substitute: it shows whether MERAF's relative advantage is
  stable around the defaults. It was run on the `rete` scenario at loads 0.12
  and 0.16 with 5 seeds (`results/outputs/sensitivity_sweep_rete_load0.120.csv`
  and `..._load0.160.csv`).

## 8. Additional historical notes

### 8.1 Effect of recalibrating `h_k`, and why `candidates_d` went from 3 to 6

With the constant `h_net = 8.0`, MERAF rejected 21.9% of the traffic at
0.12 req/ms (already close to the fleet's theoretical aggregate limit of
~117 req/s) although the real bandwidth would have admitted almost all of it.
With thresholds derived from capacity (§1.5) the reject rate at 0.12 fell to
1.9%, and admission control only engages materially at 0.16 req/ms (37% above
the fleet's theoretical capacity), where it protects p99 by a factor of ~6
relative to Round Robin (7.0 s vs 42.8 s) instead of discarding traffic the
network could carry. Lesson: when reporting a reject rate, check that `h_k`
is calibrated on the capacity of the node it applies to; a fixed threshold is
the most likely cause of an anomalous reject rate.

At the same time `candidates_d` was raised from 3 to 6: with 4 backends,
sampling 3 excluded by chance the only node with headroom about one time in
four, producing rejections caused by sampling rather than by resource risk.
With `d >= n_backends` every node is always considered (§1.6).

### 8.2 EWMA-LRT after the tenant and cold-start fixes

Before the fixes (§1.4 here; the cold-start bug in `core-dispatchers.md`),
MERAF and EWMA-LRT were bit-identical on all 15 (load x seed) cells of the
local scenario. After them the two diverge on every one of the 50 cells
tested, and EWMA-LRT shows a real pathology rather than an artefact: at
moderate-to-high load it collapses with 50-70% rejects and very high variance
across seeds, herding through positive feedback on the historical latency
mean alone, with no awareness of the load currently in flight.

### 8.3 Workflow admission: analytical motivation and first observed trade-off

If a workflow has `n` tasks admitted independently, each rejected with
probability `p` (the per-request reject rate), the whole workflow completes
with probability `(1-p)^n`: with `p = 0.247` (MERAF at 0.16 req/ms) and
`n = 10` that is 6.4%; with `n = 20` it is 0.4%. A prudent per-request
admission control becomes destructive at workflow level, and it also wastes
the work of upstream tasks that already completed.

The first version of per-workflow admission (before the binding reservation,
§3.2) appeared to dominate per-task admission on every axis. With the
reservation the picture became a trade-off: on a 5-stage linear pipeline in
the local scenario under sustained overload, per-workflow admission completed
slightly *fewer* workflows (e.g. 57.1% vs 62.0% at one load, 34.3% vs 36.7% at
a higher one) but the completed ones finished with a p95 makespan almost
halved (e.g. 4570 ms vs 8819 ms), because admitted workflows are shielded
from the unbounded queueing that per-task admission suffers under pressure,
at the cost of rejecting more aggressively at entry. This is why
`goodput_workflows_per_sec` (§3.5) exists: it turns two metrics in tension
into one judgement. The case it is designed to expose was also observed:
Power-of-Two-Choices with a 78.85% nominal completion rate and zero goodput
(every completion past the deadline). The figures above come from development
runs; the numbers of record are in `results/outputs_wf_*` and the paper
tables.

The structured-mix hypothesis (§3.4) is a hypothesis, not a guaranteed
prediction: if P differentiation stays without measurable effect even under
`by_stage`, the paper obtains a *stronger* negative result, because it is
obtained in the regime most favourable to the hypothesis it contradicts.

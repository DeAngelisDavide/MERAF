# Core simulation, metrics and statistics

Engineering reference for `core/simulator.py`, `core/metrics.py` and
`core/stats_utils.py`. It documents the design decisions, assumptions,
metric definitions and historical bug fixes that explain the current
behaviour of these modules. Source comments are deliberately terse; the
"why" lives here.

---

## 1. `core/simulator.py`: flow-level discrete-event simulator

### 1.1 Design overview

The simulator is a **flow-level** discrete-event simulator. Time is never
discretized: the only events are arrivals, service starts (after RTT) and
resource completions. Each backend exposes shared resources, `cpu`, `net`
and `decrypt`, each implemented as a `SharedResource` with **dynamic
fair-share** semantics (processor sharing / bandwidth sharing): every flow
active on a resource receives an equal share of its capacity.

Because the share received by each flow changes whenever a flow joins or
leaves a resource, any completion time computed earlier becomes invalid.
The standard technique used here is **generation-counter invalidation**:

- `_resource_gen[(backend_name, resource_name)]` holds an integer
  generation per resource.
- Every `_add_flow` / `_remove_flow` bumps the generation and schedules a
  fresh `resource_complete` event tagged with the new generation.
- When a `resource_complete` event is popped, it is discarded if its
  generation no longer matches the current one (`_handle_resource_complete`).

Only one live completion event per resource is ever meaningful: the one for
the flow that will finish first given the current flow set
(`SharedResource.next_completion`). Stale events are cheap to skip and are
never removed from the heap, so the heap may contain many dead entries; this
is expected.

### 1.2 Event queue

`_heap` is a `heapq` of tuples `(time_ms, seq, event_type, data)`. `seq`
comes from `itertools.count()` and guarantees deterministic FIFO ordering
among events with the same timestamp, which matters for reproducibility
across seeds and for comparing algorithms on identical traces.

Event types:

| Event | Payload | Handler |
|---|---|---|
| `arrival` | `Request` | `_handle_arrival` |
| `begin_service` | `(req, backend, cost, resources, dispatch_ms)` | `_begin_service` |
| `resource_complete` | `(backend_name, resource_name, flow_id, gen)` | `_handle_resource_complete` |
| `workflow_arrival` | `Workflow` | `_handle_workflow_arrival` (only in `run_workflows`) |

`run()` raises `ValueError` on `workflow_arrival` because independent-request
runs must never see workflow events; `run_workflows()` handles all four.

### 1.3 Request lifecycle

1. **Arrival** (`_handle_arrival`): the dispatcher's `select_backend(req,
   backends, now_ms, piano_a)` returns a result with `backend`, `cost` and
   `resources`. If `backend is None` the request is rejected, appended to
   `sim.rejected`, and, if it belongs to a workflow, reported to the
   workflow bookkeeping.
2. **RTT delay**: the routing decision is taken at arrival time, but data
   only starts flowing after `backend.spec.base_rtt_ms`. If RTT > 0, a
   `begin_service` event is scheduled at `now + rtt` carrying the original
   `dispatch_ms = now`; otherwise service begins synchronously.
3. **Service start** (`_begin_service`): flows are added to the resources
   listed in `resources`:
   - `net`: work = `cost["bytes"]` (shared backend bandwidth).
   - `cpu` (also matches the `"cpu(io)"` label, mapped onto the same `cpu`
     resource): work = `cost["cpu_ms"]`. For the DECRYPT class, where
     `cost` contains `decrypt_ms`, the cryptographic part is *subtracted*
     from the CPU work (`max(cpu_ms - decrypt_ms, 1e-6)`) because it is
     accounted on the dedicated `decrypt` resource. The `1e-6` floor keeps
     the flow non-degenerate.
   - `decrypt`: work = `cost["decrypt_ms"]`, only when the resource is
     requested and `decrypt_ms > 0`.

   A `RequestState` records the pending resource set. A request with an
   empty pending set (theoretical zero-cost request) completes immediately.
4. **Resource completion** (`_handle_resource_complete`): after the
   generation check, the flow is removed from the resource (which bumps the
   generation and reschedules), and the resource is discarded from the
   request's pending set. When the set becomes empty the request finishes.
   A request completes only when **all** its resources have completed, i.e.
   the resources are consumed *in parallel*, not sequentially.
5. **Finish** (`_finish_request`): memory is released
   (`mem_used_mb -= cost["mem_mb"]`, clamped at 0), the dispatcher is
   notified via `on_completion(req, backend, latency_ms, now_ms)` (used by
   feedback-driven dispatchers such as EWMA-LRT), and a `CompletedRecord` is
   appended.

Memory is the only resource modelled as a *capacity* rather than a shared
flow: it is reserved by the dispatcher at admission and released at
completion.

### 1.4 `CompletedRecord` fields

| Field | Definition |
|---|---|
| `queueing_ms` | `dispatch_ms - arrival_ms`: admission wait. For independent requests this is always 0 (dispatch is immediate); for workflow tasks it is the time between the *workflow* task's arrival and its dispatch. |
| `service_ms` | `completion_ms - dispatch_ms`: includes RTT plus fair-share service. |
| `total_ms` | `completion_ms - arrival_ms`: end-to-end latency used by all latency metrics. |
| `workflow_id` | `None` for independent requests. |

`arrival_ms` for a workflow task is `req.arrival_ms` as set by the
workload generator, not the time the task became ready. The workflow-level
makespan is measured separately in `WorkflowRecord`.

### 1.5 Workflow (DAG) handling

`run_workflows()` extends the loop for workflows: DAGs of dependent tasks
(`Workflow.tasks`, each a `Request` with `predecessors` / `successors` and a
shared `workflow_id`).

**Arrival** (`_handle_workflow_arrival`):

1. A `_WorkflowState` is created with `pending_predecessors[task] =
   len(task.predecessors)` and `depth = _dag_depth(tasks)`.
2. If the dispatcher exposes `admit_workflow(workflow, backends, now_ms)`
   (per-workflow admission, e.g. `MERAFDispatcher`), the whole DAG is
   accepted or rejected **before any task is dispatched**:
   - Rejected: `rejected_at_admission = True`, every task is marked
     unreachable and the workflow is finalized immediately. No resource is
     ever consumed, this is exactly the waste that per-workflow admission
     is meant to avoid.
   - Admitted: every task gets `workflow_admitted = True`. This flag tells
     the dispatcher (see `Request.workflow_admitted` and
     `MERAFDispatcher._score_candidates`) not to reject the task later on
     resource-risk grounds. **Historical bug**: an earlier version evaluated
     the risk correctly at admission but did not protect downstream tasks,
     so per-workflow admission was behaviourally indistinguishable from
     per-task admission (see §3.5 on the identical-results guard).
3. Source tasks (no predecessors) are released through the same
   `_handle_arrival` path used for independent requests.

**Task completion** (`_on_workflow_task_completed`): the task's cost is
appended to `wasted_cost` (always accumulated; only reported if the
workflow eventually fails), then each successor's pending-predecessor count
is decremented; a successor reaching 0 is dispatched immediately at the
current time. Successors already marked unreachable are skipped.

**Task rejection** (`_on_workflow_task_rejected`): the task is added to
`rejected_task_ids` and `_propagate_unreachable` recursively marks all
descendants that are not already completed/rejected/unreachable. A task with
a discarded predecessor can never have all predecessors completed, so it
would otherwise stay pending forever and the workflow would never finalize.

**Finalization** (`_maybe_finalize_workflow`): a workflow is finalized
exactly once, when `completed + rejected + unreachable == n_tasks`.

- Failed (`rejected_task_ids` or `unreachable_task_ids` non-empty):
  `completed=False`, `wasted_cpu_ms = Σ cost["cpu_ms"]`,
  `wasted_bytes = Σ cost["bytes"]` over completed tasks. For workflows
  rejected at admission both are 0 by construction.
- Completed: `completion_ms = now`, `makespan_ms = now - workflow.arrival_ms`.

After recording, `dispatcher.release_workflow(workflow_id)` is called if
available. **Without this release, a dispatcher with per-workflow admission
would reserve capacity forever and stop admitting after a finite number of
workflows.**

### 1.6 `_dag_depth`

Returns the number of topological levels of the DAG: sources are level 0,
any other task is `1 + max(level of predecessors)`. Levels are never
concurrent when dependencies are respected, so `depth` is the critical path
measured in *tasks* (not milliseconds).

Used by `metrics.summarize_workflows` to derive the workflow deadline
`depth * slo_ms`: each level must meet the per-task SLO, so an honest
end-to-end deadline scales with workflow length rather than being a fixed
constant.

The same level-grouping logic exists in `MERAFDispatcher.admit_workflow`.
The duplication is deliberate: depth is a property of the DAG, not a
dispatcher decision, and it is needed even when the dispatcher has no
per-workflow admission. Requires `tasks` in topological order (sources
first), which `WorkflowGenerator` guarantees.

### 1.7 Assumptions and limitations

- Fair-share is ideal: no scheduling overhead, no context-switch cost, no
  minimum quantum.
- Resources of a request are consumed in parallel; there is no pipeline
  ordering (e.g. "receive bytes, then compute").
- RTT is a fixed one-way propagation delay added before service; response
  transmission is folded into `net`.
- Memory is a hard capacity checked by the dispatcher, never a shared flow.
- Workflow task `arrival_ms` is the generator's value, not the ready time;
  `queueing_ms` for tasks therefore includes dependency wait.
- Stale heap entries are never purged; memory grows with the number of flow
  changes (fine for the run sizes used).

---

## 2. `core/metrics.py`: run summaries

### 2.1 Percentiles

`_percentile(values, p)` uses linear interpolation between order statistics
(equivalent to NumPy's default `"linear"` method): `k = (n-1)·p`, result
`s[f] + (s[c]-s[f])·(k-f)`. Returns `nan` for empty input, which propagates
into CSVs as `nan`.

### 2.2 `RunSummary` (independent requests)

| Field | Definition |
|---|---|
| `throughput_rps` | `completed / (horizon_ms/1000)` |
| `goodput_rps` | completions with `total_ms <= slo_ms`, per second |
| `latency_*_ms` | percentiles of `total_ms` over **completed** requests only |
| `queueing_*_ms` | percentiles of `queueing_ms` over completed requests |
| `reject_rate_pct` | `100 · rejected / total_requests` |
| `reject_penalty_ms` | penalty applied to rejected requests in offered metrics |
| `latency_p95/p99_offered_ms` | percentiles over completed `total_ms` plus one `reject_penalty_ms` sample per rejected request |
| `latency_p95/p99_offered_reachable` | whether the offered percentile is statistically meaningful for *this seed* |

#### Goodput vs throughput (historical fix)

`goodput_rps` was originally an exact alias of `throughput_rps` (identical
value for every algorithm and load) and carried no information. It is now
the rate of completions **within the SLO**: a request admitted but served in
8 s against a 2 s SLO is not useful throughput from the client's point of
view. Interpreted in the strict RFC sense: useful data delivered on time.

#### Offered-load percentiles

Latency percentiles computed only over completed requests are not
comparable across algorithms with different reject rates: an algorithm
rejecting 25% of traffic and one rejecting 0.5% are not serving the same
population. The **offered** percentiles include the rejected requests, each
represented by a penalty value, giving a like-for-like comparison.

**Historical bug (penalty value)**: the penalty used to be a fixed `2 × SLO`
(4000 ms at the default 2000 ms SLO). Under heavy load the p99 of *admitted*
requests easily exceeds 4000 ms (e.g. 7080 ms at 0.16 req/ms in the network
scenario), so a mass of rejects penalized at 4000 ms fell *below* the
admitted p99 and pulled the percentile *down*. The metric rewarded
rejecting, the opposite of its purpose. The penalty is now
`CLIENT_TIMEOUT_MS = 60 000 ms`, the real end-to-end timeout of the
reference HTTP benchmark client (`client.Timeout: 60 * time.Second` in
`CommunicationProtocolsEvaluation/client/client.go`). This is the worst-case
cost a real client actually observes, not an arbitrary SLO multiple, and it
stays above any latency observed in the tested loads.

Even so, the resulting number is a **declared convention**, not a
measurement. Always report it next to `reject_rate_pct`, never instead of
it. Above a certain reject fraction the offered percentile lands inside the
rejected mass (censored at "worse than anything observed") and is no longer
statistically grounded, which is what reachability captures.

#### Reachability (`offered_percentile_reachable`)

Rejected requests are treated as **right-censored**: they are only known to
be worse than every observed value, so by construction they occupy the upper
tail of the offered distribution. If the rejected fraction
`r = reject_rate_pct/100` satisfies `r >= 1 - p`, the p-th percentile falls
inside the censored tail: its value would depend solely on the chosen
penalty, not on the data. In that case the percentile is reported as
"not reachable" (`False`). Consequently p99 is reachable only if rejects
< 1%, p95 only if rejects < 5%.

**Historical bug (aggregation granularity)**: reachability used to be
evaluated on the *mean* reject rate of an aggregated cell (algorithm ×
load), not per seed. Seeds oscillating around the threshold within the same
cell (observed: 4 of 5 seeds below 1% rejects, 1 seed at 2.6%) kept the mean
below threshold while individual seeds already saturated at the penalty. The
aggregate then averaged real values (~4000-5000 ms) with saturated ones
(60 000 ms) into a number corresponding to no actual system state.
Reachability is now computed **per seed** in `summarize()`, and the
multi-seed aggregation (`_aggregate()` in `run_experiments.py`,
`aggregate()` in `aggregate_results.py`) averages **only reachable seeds**,
reporting explicitly how many of the seeds were reachable.

### 2.3 `per_profile_breakdown`

Latency mean/p50/p95/p99 and count per security profile `(T,P)`, computed
over completed requests only. Returns plain dicts (the `RunSummary` return
annotation is historical). Exported by `write_profile_breakdown_csv` with
3-decimal formatting.

### 2.4 `WorkflowSummary` and `summarize_workflows`

Workflow-level metrics (as opposed to per-task):

| Field | Definition |
|---|---|
| `completion_rate_pct` | `100 · completed / total` |
| `makespan_*_ms` | percentiles over completed workflows' `makespan_ms` |
| `goodput_workflows_per_sec` | workflows completed **and** `makespan_ms <= depth · slo_ms`, per second |
| `within_deadline_pct` | share of *completed* workflows that also met the deadline |
| `rejected_at_admission` | subset of failed workflows rejected upfront (per-workflow mode only) |
| `wasted_cpu_ms_total`, `wasted_bytes_total` | summed over failed workflows |
| `wasted_cpu_ms_mean_per_failed` | 0 when no workflow failed |

**Deadline definition**: the per-workflow deadline is `depth × slo_ms`
(see §1.6), so a longer critical path gets a proportionally longer deadline.

**Reading guidance**: completion rate and makespan taken separately do not
capture the real trade-off, exactly as p99-over-admitted and reject rate
did not for independent requests. `goodput_workflows_per_sec` is the
workflow-level analogue of `goodput_rps`: an admission policy that completes
everything it admits but with huge makespans is not good goodput for whoever
waits for the result.

**Wasted work is a diagnostic, not evidence**: `wasted_*` quantify the
problem of per-task admission (work thrown away when a workflow fails
midway). Per-workflow admission zeroes waste *by construction* (it rejects
before starting any task), so a "0 vs N" comparison between the two modes is
near-tautological and must be presented as such. The comparison that
actually decides which approach is preferable is on `completion_rate_pct`
and `goodput_workflows_per_sec`.

### 2.5 CSV writers

- `write_summaries_csv`, `write_workflow_summaries_csv`,
  `write_workflow_records_csv`: field names derived from `asdict()` of the
  first record; no-op on empty input.
- `write_completed_csv`: fixed column list matching `CompletedRecord`.
- `write_workflow_records_csv`: per-workflow (non-aggregated) log so any
  additional metric can be recomputed offline without re-running the
  simulation, mirroring what `completed_<...>.csv` provides for independent
  requests.

---

## 3. `core/stats_utils.py`: statistical helpers

### 3.1 Motivation

Introduced to give `run_experiments.py` and `aggregate_results.py` a
**single** implementation of mean / standard deviation / CI across
replicas (seeds). Before, the two pipelines diverged silently:
`run_experiments.py` used the *population* standard deviation and computed
no CI; `aggregate_results.py` computed a CI but from the population
deviation as well. That was an error, not just an inconsistency: the
Student-t CI formula assumes the **sample** standard deviation (`ddof=1`).
With n=5 the two differ by ~12%, which is not negligible at the precision
the results are reported with.

No external dependencies (no SciPy): stdlib only, consistent with the rest
of the package. Critical values are therefore tabulated.

### 3.2 Critical-value tables

- `T_TABLE_95_TWOSIDED`: two-sided critical t at α=0.05 for df 1..10;
  `t_value_95(n)` uses `df = max(n-1, 1)` and falls back to the normal
  approximation 1.96 for df > 10.
- `T_TABLE_90_ONESIDED`: one-sided critical t at α=0.05 (equivalent to a
  two-sided 90% CI) for df 1..10; `t_value_onesided_05(df)` floors
  fractional df (Welch-Satterthwaite), a conservative choice that
  slightly widens the critical region, and falls back to 1.645 beyond
  df 10.

### 3.3 `mean_std_ci95`

Returns `MeanStdCI(mean, std, ci95, n, min, max)` where `std` is the sample
standard deviation (`statistics.stdev`, never `pstdev`) and `ci95` is the
half-width `t_{95}(n) · std / √n`.

With `n = 1`, std and CI are undefined; both are reported as `0.0` (not
NaN) to stay compatible with the existing CSV export. A single seed is
nevertheless not a sufficient basis for estimating between-replica
variability; see the README/paper for the recommended minimum number of
replicas.

### 3.4 `tost_equivalence` (TOST)

Two One-Sided Tests for **practical equivalence** between two independent
samples (e.g. per-seed p95 latency of profile A vs profile B under the same
algorithm and load), within a margin expressed in the data's units.

- H0 (non-equivalence): `|mean_a − mean_b| ≥ margin`
- H1 (equivalence): `|mean_a − mean_b| < margin`

Equivalence is concluded only if **both** one-sided tests reject at
α = 0.05:

```
t_lower = (diff + margin) / se   # tests H0: diff <= -margin, reject if t_lower >  t_crit
t_upper = (diff - margin) / se   # tests H0: diff >= +margin, reject if t_upper < -t_crit
```

The standard error is Welch's `sqrt(var_a/n_a + var_b/n_b)` with
Satterthwaite degrees of freedom. Equal variances are *not* assumed: there
is no reason for that to hold here (one profile may have more samples or a
heavier tail than another).

Edge cases:

- Fewer than 2 observations in either group: verdict not computable;
  `equivalent=False`, all statistics NaN, `reason` set.
- Zero variance in both groups (e.g. deterministic identical values on all
  seeds): `se = 0`, no inference is possible; equivalence is decided by the
  point comparison `|diff| < margin`, with `df = inf`, `t_crit = 0` and
  `reason` set.
- Satterthwaite denominator 0 (cannot happen when se > 0, kept as a guard):
  `df = min(n_a, n_b) − 1`.

The `alpha` parameter is documentary: the tables are fixed at 0.05. It
exists to make the significance level explicit at call sites, not to be
varied freely.

This replaces the informal argument "differences fall within one standard
deviation" previously used for claims of the type "functionally
indistinguishable" (e.g. no measurable effect of P on latency).

### 3.5 `assert_variants_differ`: identical-results guard

Raises `SuspiciousIdenticalResultsError` when two result maps with the same
keys (e.g. `(scenario, topology, load) → aggregated metric`) coincide
**exactly** on more than `max_identical` non-trivial cells. Values in
`trivial_values` (default `0.0` and `100.0`, i.e. 0% rejects or 100%
completion) are ignored: identity is expected there and carries no
information.

**Motivation**: the same pattern was observed three times in this project,
each time signalling a real bug faster and more reliably than eyeballing
aggregated metrics:

1. A shared tenant that pinned both MERAF and EWMA-LRT to the same node,
   producing bit-for-bit identical runs.
2. EWMA-LRT's cold-start estimate being independent of real capacity,
   producing the same pinning.
3. `admit_workflow()` evaluating risk correctly but not protecting
   downstream tasks, making per-workflow admission indistinguishable from
   per-task admission (see §1.5).

In all three cases, two variants that **by construction must differ**
produced identical output.

**Do not use for ablation comparisons** (e.g. `differentiate_cost` on/off,
`eta = 0`, `meraf` vs `meraf-no-cost-diff`): there the study's goal is
precisely to determine whether a difference exists, and "no difference" is
often the honest, expected outcome. Raising would break the experiment
instead of reporting its result. Use only for pairs of **structurally
different mechanisms** where some difference is guaranteed by design (two
admission policies, not two weights of the same policy).

The error message (Italian, user-facing) lists up to 5 offending cells and
recalls the three historical cases.

## 4. Further declared simplification: no cancellation of sibling tasks

When a workflow fails because one task is rejected, only the descendants of
that task are marked unreachable (§1.5). Independent branches already in
flight (e.g. the other parallel tasks of a fan-out whose sibling was rejected)
run to completion instead of being cancelled. Active cancellation would be a
realistic optimisation and would reduce waste; its absence makes the reported
`wasted_*` figures a conservative upper bound, not an optimistic estimate.

# `core/dispatchers.py`: engineering reference

This document preserves the design rationale, assumptions, known limitations,
paper mappings, ablation variants and historical bug notes that used to live as
long comments inside `core/dispatchers.py`. The source file now carries only
short comments; read this document for the "why".

## Module overview

`core/dispatchers.py` implements the `OnRequest` procedure of Algorithm 1 of the
MERAF paper (`MERAFDispatcher`, "Plan B") plus the baseline dispatchers used as
comparison terms in the experimental plan: Round Robin, Least Connections,
Power-of-Two-Choices, EWMA Least Response Time, and two
"security-as-constraint" baselines reproduced from the related literature
([22] Singh & Auluck 2020, [24] Wang et al. 2026).

Every dispatcher exposes the same interface so the simulator treats them
interchangeably:

```
select_backend(request, backends, now_ms, piano_a) -> DispatchResult
    DispatchResult(backend | None, cost_profile_dict | None, resources_needed_tuple)
on_completion(request, backend, latency_ms, now_ms) -> None
```

All dispatchers keep two counters, `rejections` and `dispatched`, and every
`select_backend` implementation calls `piano_a.on_connection(...)` first so that
Plan A (transport/handshake accounting) is exercised identically regardless of
the dispatcher under test.

### `_sim_resource_name(r)`

Maps the paper's `"cpu(io)"` notation onto the backend's `cpu` resource. In the
simulation the relay I/O performed for OPAQUE payloads is modelled as work on
the same shared cpu resource. This is a simplifying assumption, stated
explicitly for methodological transparency; it is not a claim that relay I/O
and cpu contend identically in a real deployment.

### `DispatchResult`

Plain dataclass: `backend` (or `None` on rejection), the `cost` profile dict
used for the decision (`cpu_ms`, `bytes`, `mem_mb`, ...), and the tuple of
resources the request occupies. Returning cost and resources alongside the
backend lets the simulator charge the exact same quantities the dispatcher
reasoned about.

---

## `MERAFDispatcher`

MERAF Plan B: cost-aware backend selection, admission based on the projected
dominant-resource risk `rho`, and a conditional client-affinity bonus.

### Constructor parameters

| Parameter | Default | Meaning |
|---|---|---|
| `calibration` | `CostCalibration()` | Cost model (`cost_plain`, `cost_decrypt`, `cost_opaque`). |
| `weights` | `ScoreWeights()` | Weights of the scoring formula (`model.score`). |
| `candidates_d` | 3 | Size `d` of the random candidate sample. |
| `sticky_tolerance` | 1.10 | Affinity tolerance: the sticky backend is kept if its score is within `best_score * tolerance`. |
| `slo_ms` | 2000 | Latency SLO used by the scoring term and by the deadline-gate ablation. |
| `stale_after_ms` | 10 000 | `IsStale()` threshold, see below. |
| `handshake_sigma_multiplier` | 1.5 | Sigma inflation during a full PQC handshake, see below. |
| `differentiate_cost` | `True` | Ablation switch, see below. |
| `slo_penalty_mode` | `"linear"` | `"linear"` or `"probability"`, see below. |
| `workflow_admission` | `False` | Enables per-workflow admission (`admit_workflow`). |
| `admission_criterion` | `"risk"` | `"risk"` or `"deadline"`; only relevant when `workflow_admission=True`. |
| `seed` | 7 | Seed of the private RNG used for candidate sampling. |

Invalid `admission_criterion` or `slo_penalty_mode` values raise `ValueError`
at construction time.

#### `stale_after_ms`: the `IsStale()` check

Maximum age, in simulation milliseconds, beyond which a per-class EWMA latency
estimate is no longer trusted (`Backend.is_stale`). A previous version of
Algorithm 1 contained this check; it was absent from the version delivered with
the paper and was raised in the review ("Questions for the authors"). It is
kept here because without it a backend that served a request class long ago
would be scored with an estimate that no longer reflects its current state.

#### `handshake_sigma_multiplier`

During a full PQC handshake (`piano_a.on_connection` returns `True` and the
request transport is `Transport.PQC`) the predicted sigma is multiplied by this
factor. It used to be a hard-coded literal `1.5` inside `_score_candidates`; it
is now an explicit hyperparameter so it can be probed by a sensitivity sweep
(`sensitivity_sweep.py`) instead of being buried in the code.

#### `differentiate_cost`: ablation "no crypto cost differentiation"

Ablation switch (review, Section 4.3). With `differentiate_cost=False` the
dispatcher stops differentiating the cost profile by payload class `P`: every
request is priced with `cost_plain` (the cheapest class) and is charged to the
PLAIN resource set (cpu, mem, net), with no dedicated decrypt/opaque cost or
resource. Everything else, the rho-based admission gate, scoring, affinity, is left intact. Comparing the two variants isolates the contribution of
cryptographic cost differentiation from that of projective admission.

#### `slo_penalty_mode`: "threshold" scoring variant

Proposed during the paper review. The default `"linear"` mode uses the paper's
`L95 / SLO` term inside `model.score`. That term is unbounded: at equal
variance, being 10x over budget is treated as ten times worse than being 1x
over budget.

`"probability"` replaces it with `P(latency > SLO)` under a Gaussian model
(`model.prob_exceed_slo(mean, sigma, slo_ms)`), a term bounded in `[0, 1]` that
penalises the estimated probability of violating the SLO rather than the
expected latency relative to it. It accounts for mean **and** variance jointly,
whereas `L95 = mean + 1.645 * std` already blurs them into one number. The rest
of the formula (risk barrier, uncertainty term, affinity bonus) is unchanged on
purpose, so the two variants can be compared cleanly (see `"meraf-prob-slo"` in
`run_experiments.py`).

#### `workflow_admission`: per-workflow admission

If `True`, the simulator calls `admit_workflow()` when a DAG arrives and
accepts or rejects the **whole** workflow before dispatching any of its tasks.
If `False` (default) the dispatcher behaves exactly as before the feature was
added: each task is admitted or rejected independently when it becomes ready,
with no view of the DAG it belongs to.

#### `admission_criterion`: `"risk"` vs `"deadline"`

* `"risk"` (default): project rho on the widest topological level of the DAG
  and take a binding capacity reservation (see `admit_workflow`).
* `"deadline"`: the **deadline-gate ablation**. Same per-DAG decision point at
  submission, but the criterion is the estimated makespan compared with a
  deadline of `depth * SLO`, and **no capacity is reserved**. Its purpose is to
  separate how much of the per-workflow gain comes from having a per-DAG
  decision point at all, and how much comes from the rho-based reservation.

#### Capacity reservation state

`_reserved_width: Dict[backend_name, int]`, `_reserved_mem_mb: Dict[backend_name, float]`
and `_workflow_reservation: Dict[workflow_id, (backend_name, width, mem_mb)]`.

Motivation (review, "technical question"): without this bookkeeping,
`admit_workflow()` would evaluate every new workflow against the **current**
snapshot of active flows, ignoring workflows already admitted but not yet
finished. Two workflows submitted a few milliseconds apart could both be
admitted against the same free capacity and together exceed it. Because
`Request.workflow_admitted=True` disables the downstream per-task risk gate
(see `_score_candidates`), nothing would catch the over-admission: the system
would degrade silently in latency (unbounded queueing) instead of in
rejections. The reservation makes the admitted capacity explicit and binding
for the whole life of the workflow, not just at the instant of admission.

### `admit_workflow(workflow, backends, now_ms) -> bool`

Workflow-level admission (review, "The intuition..." section). Evaluates the
projected resource risk for the **entire** DAG at submission, instead of
letting each task discover it on its own when it becomes ready. A workflow that
cannot complete anyway is rejected **immediately**, before work is wasted on
the upstream tasks it would otherwise have executed (see
`Simulator._handle_workflow_arrival`).

Returns `True` unconditionally when `self.workflow_admission` is `False`.
Rejects (and increments `workflows_rejected_at_admission`) if no backend is
healthy.

#### Level computation

Tasks are grouped by topological level: level 0 for sources, otherwise
`1 + max(level of predecessors)`. `workflow.tasks` is assumed to be already in
topological order (sources first), which is what makes the single pass valid.
Tasks on different levels are never concurrent in a DAG whose dependencies are
respected.

#### `"risk"` criterion

The criterion is a stated choice, not the only possible one. It does **not**
assume all `n` tasks of the workflow run concurrently: a linear pipeline runs
one task at a time, and assuming `n` concurrent tasks would over-estimate the
demand by a factor `n`, erasing the difference from per-task admission.
Instead:

* `width` = size of the most populous level (1 for a linear pipeline, the
  number of parallel branches for a fan-out/fan-in);
* `resources_union` = union of the resource sets of all tasks;
* `peak_mem_mb` = memory of the heaviest level (sum of `mem_mb` of its tasks).

For each healthy backend, in list order:

1. skip if `mem_used_mb + reserved_mem + peak_mem_mb > mem_capacity_mb`
   (memory already reserved for other workflows counts);
2. `proj = projected_utilization(resources_union, extra = width + reserved_width)`,
   `rho = dominant_risk(proj, thresholds, resources_union)`;
3. if `rho < 1.0`: record the reservation (`_reserved_width`,
   `_reserved_mem_mb`, `_workflow_reservation[workflow_id]`) and return `True`.

The workflow is admitted if **there exists** at least one healthy backend that
would absorb the peak concurrency **added to** what is already reserved for
other in-flight workflows on that node.

Known limitation: this is still a worst-case bound on a **single** backend
pinned at admission. The workflow's tasks may later be dispatched to a
different backend during scoring, so the reservation is a prudent
approximation, not a physical reservation of flows. No more precise multi-node
bin-packing logic exists elsewhere in the package.

#### `"deadline"` criterion (deadline-gate ablation)

Estimated makespan = sum over levels of the maximum per-task estimated L95 in
that level, each task evaluated on its best backend using the same
EWMA/structural estimate as scoring (`default_l95 = cpu_ms + bytes / net_capacity`,
replaced by `predicted_l95` when the class telemetry is not stale). Admit if
`est_makespan <= len(levels) * slo_ms`. No capacity is reserved. Admitted
workflows still bypass the per-task risk gate (`workflow_admitted`), so the
per-DAG decision point is identical to the `"risk"` variant and the only
differences are the criterion and the absence of the reservation.

### `release_workflow(workflow_id)`

Releases the capacity reserved by `admit_workflow()` for a workflow that has
finished (completed or failed). Called by the simulator in
`Simulator._maybe_finalize_workflow()`. Without this release the reservation
would grow monotonically and the system would stop admitting any new workflow
after a finite number of admissions. Values are clamped at zero; unknown ids
are ignored.

### `_cost_profile(req) -> (cost, resources)`

Selects the cost function by payload class: `cost_plain`, `cost_decrypt`
(which also takes `req.use_kem_envelope`), `cost_opaque`; the resource set comes
from `model.resource_set(payload)`. An unknown payload raises `ValueError`.
With `differentiate_cost=False` the PLAIN cost and PLAIN resource set are
returned for every request (see the ablation above).

### `_score_candidates(candidates, req, cost, resources, is_full_handshake, sticky_name, now_ms)`

Returns `(best_backend, best_score, scored_dict)`.

**Risk gate bypass for admitted workflows.** `skip_risk_gate` is `True` when
`workflow_admission` is on and `req.workflow_admitted` is set. The upfront
workflow admission already assumed the projected risk; rejecting the task again
here would void that guarantee. Memory, however, remains a hard physical limit
even for an admitted workflow (`has_mem_headroom` is never bypassed): it is a
capacity bound, not a soft risk like rho.

Per candidate:

1. skip if `not has_mem_headroom(cost["mem_mb"])`;
2. `rho = dominant_risk(projected_utilization(resources, extra=1), thresholds, resources)`;
   skip if `rho >= 1.0` and the gate is not bypassed (`rho_i(r, P) >= 1` in the paper);
3. latency estimate for class `req.cls_key`:
   * structural default `default_l95 = cpu_ms + bytes / max(net_capacity_bytes_per_ms, 1e-9)`;
   * if `is_stale(cls, now_ms, stale_after_ms)`, telemetry absent or too old,      the EWMA is not trusted and the structural estimate is used for `mean`
     and `l95`, with `sigma = 0.5 * default_l95`. This is exactly the
     first-use behaviour of the class (same prudent default as
     `predicted_sigma()` with `n_obs = 0`);
   * otherwise `predicted_mean`, `predicted_l95`, `predicted_sigma`;
4. if `is_full_handshake` and transport is PQC: `sigma *= handshake_sigma_multiplier`;
5. `affinity = 1.0` if this is the sticky backend, else `0.0`;
6. `latency_term = prob_exceed_slo(mean, sigma, slo_ms)` in `"probability"`
   mode, otherwise `None` so that `model.score` uses its default `L95 / SLO`;
7. `s = score(l95, slo_ms, rho, sigma, affinity, weights, latency_term=latency_term)`;
   lowest score wins.

### `select_backend(req, backends, now_ms, piano_a) -> DispatchResult`

1. compute cost/resources; call `piano_a.on_connection(tenant, transport, now_ms)`
   to learn whether this request triggers a full handshake;
2. reject if no backend is healthy;
3. sample `min(d, len(healthy))` candidates with the private RNG;
4. score them (`_score_candidates`) with the tenant's sticky backend name;
5. **fallback fix**: if no candidate is admissible and the sample did not cover
   all healthy nodes, score the remaining unsampled healthy nodes too. Before
   this fix a request could be rejected merely because of an unlucky `d`-sample.
   It is a low-cost safety net: it runs only when the initial sample fails
   entirely, not on every request;
6. reject if still no admissible backend;
7. **affinity bonus within tolerance** (final lines of Algorithm 1): if the
   tenant's sticky backend is among the scored candidates and
   `scored[sticky] <= best_score * sticky_tolerance`, keep the sticky backend
   instead of the strict best;
8. update `_sticky[tenant]`, charge `mem_mb` to the chosen backend, count the
   dispatch.

Sticky affinity is keyed by `req.tenant` (the `client_id`).

### `on_completion(req, backend, latency_ms, now_ms)`

Feeds the observed latency into the backend's per-class EWMA
(`observe_latency(req.cls_key, ...)`), which is what `predicted_*` and
`is_stale` read.

---

## Baseline dispatchers (experimental plan of the paper)

All baselines derive from `RoundRobinDispatcher` and reuse its
`_cost_profile`, which is **only** used to size memory and bytes for the
simulator, never as a selection criterion (they are deliberately not
crypto-aware). Each performs the same memory headroom check as MERAF before
committing.

### `RoundRobinDispatcher`

No cost model: cycles over the healthy backends with a running index, ignoring
transport `T` and payload `P`. Rejects if the chosen node lacks memory
headroom. `on_completion` is a no-op.

### `LeastConnectionsDispatcher`

Chooses the backend with the fewest total active flows
(`cpu.active_count + net.active_count + decrypt.active_count`).

**Fixed bug (review, Section 4.8).** Python's `min()` returns the first element
among ties. At simulation start all backends have zero active flows, so the
entire traffic was routed to the first healthy node in the list until a load
difference appeared, which, with strong backends, never saturates, so in
practice traffic stayed **always** on the same node. The standard Least
Connections formulation breaks ties at random; the class now keeps a seeded RNG
(`seed=13`) and picks uniformly among the tied minimum-load backends.

### `PowerOfTwoChoicesDispatcher`

Samples two healthy backends at random (`seed=11`) and picks the one with fewer
active flows, a scalable, generic, resource-agnostic baseline. Uses the same
random tie-break as Least Connections (Section 4.8 of the review): when both
sampled nodes carry equal load, the first element of the sample must not be
systematically favoured.

### `EWMALeastResponseTimeDispatcher`

Chooses the backend with the lowest predicted p95 latency for a single
undifferentiated request class (`"generic"`), i.e. a latency-aware but not
crypto-aware balancer. `on_completion` records observed latency under that
same `"generic"` class.

**Fixed bug (cold-start estimate).** The cold estimate used a fixed constant
(`bytes / 1000`) independent of the node's real bandwidth. For the simulated
fleet (60-2200 MB/s) this amounted to assuming ~1 MB/s for every node, so
pessimistic that the first node ever observed looked best for the rest of the
simulation, with no real exploration. The cold estimate now uses each node's
actual `net_capacity_bytes_per_ms`, exactly like MERAF's structural default.

**Random tie-break** (Section 4.8 of the review): with homogeneous nodes the
cold estimate is identical across several backends and `min()` would always
favour the first in the list; ties are broken with a seeded RNG (`seed=17`).

---

## "Security-as-constraint" baselines (review, Sections 2 / 4.3)

In these baselines security is a **static** workload requirement, a node is
either eligible or not, and **not** a dynamic scoring variable as in MERAF. If
no healthy node satisfies the constraint the request is dropped for security
infeasibility, a rejection mode MERAF does not have at all (MERAF rejects only
for projected resource risk). Both reuse the MERAF risk projection
(`model.dominant_risk` over `projected_utilization(resources, extra=1)`) as the
"remaining capacity" measure, so the comparison differs only in the security
policy, not in how capacity is estimated.

### `SecurityConstraintLeastLoadedDispatcher`: ML algorithm of [22] Singh & Auluck 2020

A faithful reproduction of the Minimum Load (ML) algorithm (Section 4 and
Table 2 of [22]; full text in `related/`), not a generic adaptation.

1. **Fixed local mdc per tenant.** "Jobs are initially assigned to their
   nearest local mdc." MERAF has no real network hierarchy (iFogSim does, this
   simulator does not), so the first healthy node a tenant encounters, chosen
   at random with `seed=19`, becomes its fixed local node for the whole
   simulation. The mapping is per tenant, **not** per request class, because in
   the paper locality is a physical property of node/user, independent of what a
   specific job requires. State: `_local: Dict[tenant, backend_name]`.
2. **Security label per request** from
   `model.required_trust_label_singh_auluck(payload)` (Table 2):
   `lp` (private, from OPAQUE), `lsp` (semi-private, from DECRYPT),
   `lpu` (public, from PLAIN).
3. **Eligibility per label**, exactly as Table 2, not symmetric across classes:
   * `lp` (`TRUST_HIGHLY_TRUSTED`): **only** the local mdc; never migrated,
     regardless of other `lht` nodes available ("needs to be executed in the
     local environment");
   * `lsp` (`TRUST_SEMI_TRUSTED`): the local node or any remote node with
     `security_tier >= TRUST_SEMI_TRUSTED` (`lht` or `lst`);
   * `lpu`: any healthy node.
4. **Selection among eligible nodes with spare capacity** (`rho < 1`, the
   analogue of the paper's `CL(mk) < Threshold` check, Eq. 3): prefer the local
   node if it is among them ("jobs are *typically* assigned to the nearest local
   mdc"); otherwise pick the node with the **most** spare capacity, i.e.
   **minimum rho** among eligible nodes (Section 4: "we pick the mdc ... which
   has the maximum spare capacity available"). This is a margin relative to the
   node's nominal capacity, not the raw active-flow count used by the other
   baselines. Ties on rho are broken at random.

Edge cases and stated deviations:

* If the tenant's local node is not (or no longer) healthy, the request is
  rejected: the paper provides no explicit fallback for this corner case.
* If an `lp` job finds its local node saturated, the paper makes it **wait** in
  the local queue (neither dropped nor migrated) until capacity frees up. This
  simulator admits or rejects at request time with no dispatcher-side queue, so
  an `lp` request without headroom at its local node is **dropped**. This is a
  declared deviation on this single detail, not an approximation of the whole
  algorithm.
* The memory headroom check is applied after selection, like all baselines.

### `SecurityConstraintFastestDispatcher`: online adaptation of SeSiC, [24] Wang et al. 2026

**SeSiC** (Sections 2-3, Algorithms 1-4 of [24]; full text in `related/`) is
security-aware scheduling on a hybrid cloud (local/private pool plus elastic
public pool) that maximises the number of requests completed within a deadline
by reserving the public cloud (abundant but insecure) for traffic that does not
need security and the private cloud (scarce but secure) for traffic that
actually requires it.

Mapping onto the MERAF model:

* **Public / private pools**: the same two backend groups used by
  `SecurityConstraintLeastLoadedDispatcher` (`BackendSpec.security_tier >= 1`
  = local/private, otherwise public), but with the paper's semantics, a
  two-way partition, not a hierarchy of increasing trust.
* **`s_i`** (binary task security indicator, Section 2):
  `model.required_security_tier(profile) >= 1`. `0` when the payload needs no
  application-level encryption (PLAIN, allowed on any pool); `1` otherwise
  (DECRYPT/OPAQUE, private pool **only**, Eq. 15: task security requirements
  can be satisfied only by the local cloud).
* **`d_i`** (per-task deadline, Eq. 14): `self.slo_ms`, the same SLO target as
  MERAF. "Finishes within the deadline" is estimated with the same predicted
  p95 (`Backend.predicted_l95(req.cls_key, default_l95)`) used elsewhere,
  together with the spare-capacity constraint `rho < 1`.

Algorithm (online adaptation of SeSiC's first two procedures):

1. If the request has **no** security requirement (Procedure 1, "exploit the
   abundance of the public cloud"): try the **public** pool first. If a public
   node can finish within the estimated deadline (`predicted_l95 <= SLO`) with
   spare capacity (`rho < 1`), the request goes there.
2. Otherwise, security required, **or** no public node meets the deadline
   (Procedure 2, "use local resources for the rest"): try the private/local
   pool with the same criterion. If no node satisfies it, the request is
   **dropped** (Section 3: the task is rejected under hard deadline
   constraints).
3. Among several eligible nodes in the same pool, pick the one with the largest
   remaining capacity margin (**minimum rho**), ties broken at random
   (`seed=23`). The paper specifies no fine-grained choice among equally
   eligible VMs/cores within a pool (it just "finds one"), so the admissibility
   criterion already validated in the rest of the package is reused rather than
   an arbitrary, unspecified order.

Declared simplifications (batch to online):

* SeSiC works on a **batch** of tasks sorted by deadline (earliest deadline
  first, Section 3) and decides on the whole set in three passes. MERAF
  dispatches one request at a time on arrival with no dispatcher-side queue, so
  there is nothing to sort: each request is decided in isolation, in arrival
  order.
* **Procedure 3 omitted** (a posteriori re-scheduling from public to local to
  save VM rental cost, releasing unused VMs at the end of the batch). It is an
  optimisation over an already computed batch solution with no meaningful
  analogue in an online dispatcher that decides one request at a time and does
  not model dynamic VM rental/release. Its cost objective (not paying for
  public capacity when local would suffice) is therefore outside this
  comparison, no other baseline in the package models monetary cost either.
* **Public-cloud elasticity omitted** (renting new VMs, `n_k` in the paper):
  the MERAF backend fleet is fixed for the whole simulation.

`on_completion` records latency under `req.cls_key`, so `predicted_l95` learns
per class as in MERAF.

---

## Cross-cutting notes

* **Seeds.** Each dispatcher owns a private `random.Random` with a distinct
  default seed (MERAF 7, LC 13, P2C 11, EWMA 17, ML 19, SeSiC 23) so that
  baseline randomness never perturbs the simulator's or the workload's RNG
  streams and runs are reproducible per dispatcher.
* **Memory is always hard.** Every dispatcher, MERAF included, refuses a
  backend without `has_mem_headroom(cost["mem_mb"])` and charges `mem_mb` on
  dispatch; the simulator releases it on completion. Only the rho gate can be
  bypassed (for admitted workflows), never memory.
* **Structural latency default.** The cold-start / stale estimate
  `cpu_ms + bytes / max(net_capacity_bytes_per_ms, 1e-9)` is shared by MERAF,
  the EWMA baseline and SeSiC so that no baseline is disadvantaged by a worse
  bootstrap estimate (see the EWMA cold-start bug above).

## Security-as-constraint baselines: observed effects, presentation, and the excluded [26]

**Observed effects of the faithful reproductions.**
`security-constraint-loadaware` shows a small reject rate even at very low
load (e.g. 0.05% at 0.06 req/ms, `rete`), due solely to private (`lp`, i.e.
OPAQUE) jobs whose fixed local node is untrusted, a real property of [22]
(locality rigidity for private jobs) that the earlier binary tier-gate
adaptation, which always allowed migration to any trusted node, did not
capture. `security-constraint-satisfaction` has a double admission constraint
(estimated deadline *and* capacity margin, not capacity alone), so it is
stricter: at 0.16 req/ms (`rete`) it drops ~42% of requests against ~24% for
the other security-as-constraint baseline, the hard-deadline, no-queue
behaviour of [24] that the earlier version (predicted p95 as a preference
only, no deadline rejection) did not capture.

**Expected shape of the results.** With the default traffic mix and 25% of
the fleet per trusted tier (`make_backend_fleet`), both baselines are expected
to queue longer than MERAF as soon as the load exceeds what the trusted
minority can absorb alone. That is a substantive result, not a procedural
one: it quantifies the cost of treating security as a static gate instead of
a scoring variable.

**How to present them in the paper.** As reproductions of the original
algorithms ("the ML algorithm of [22]" and "the SeSiC algorithm of [24]"),
qualified by the declared simplifications listed above (batch/hierarchical
scheduler adapted to an online dispatcher on a flat fleet; drop instead of
wait for a saturated local `lp` job; Procedure 3 and public-cloud elasticity
omitted), not as generic "representative" adaptations. Both full texts are
under `related/`; `core-model.md` §1.9 and `experiments.md` §2.4 point here for
the exact scope of each adaptation.

**[26] SABA is not reproduced.** SABA schedules *workflows* (DAGs under an
aggregate budget constraint), not independent HTTP requests; a "SABA-like"
baseline on independent requests would discard SABA's central idea. Now that
MERAF has DAG workflows (`run_workflow_experiments.py`), an honest comparison
would apply the same disclaimer as above to a SABA adaptation at workflow
level. This has not been done.

# MERAF core model: design reference

This document is the engineering reference for the `core/` package of MERAF
(Modular Encryption- and Resource-Aware Framework). It records the design
rationale, cost-model equations, calibration sources, assumptions, known
limitations and historical notes that the source comments used to carry. The
code comments themselves are intentionally terse; anything a maintainer needs
beyond "what the line does" belongs here.

Modules covered:

| Module | Provides |
|---|---|
| `core/__init__.py` | Package docstring only. |
| `core/model.py` | Security-profile taxonomy, cost model (Eqs. (1)-(3)), dominant-resource risk (Eq. (4)), scoring (Eq. (5)), static tier/trust helpers for the baselines. |
| `core/resources.py` | `SharedResource`: processor-sharing (fair-share) resource used by the flow-level simulator. |
| `core/backend.py` | `BackendSpec` / `Backend`: a heterogeneous node with cpu/net/decrypt shared resources, a memory capacity, and per-class latency EWMA learning. |
| `core/piano_a.py` | Plane A (`PianoA`): TLS negotiation, resumption and PQC handshake rate limiting (Algorithm 1, `OnConnection`). |
| `core/workload.py` | `Request`, `WorkloadGenerator`, `Workflow`, `WorkflowGenerator`. |

Design constraint for the whole package: **standard library only** (Python
3.9+, `math.erf` is available from 3.8). No numpy/scipy. The point is that
anyone can reproduce the simulation results without installing anything.

---

## 1. `core/model.py`

Direct implementation of Eqs. (1)-(5) and of the T x P taxonomy from the
paper section "MERAF: Request-Level" (design section).

### 1.1 Taxonomy of security axes (`Transport`, `Payload`, `SecurityProfile`)

A security profile is a composable tuple `(T, P)`:

- **Transport `T`** (TLS key exchange):
  - `CLASSIC` = X25519.
  - `PQC` = X25519MLKEM768, the hybrid classical + ML-KEM-768 group.
- **Payload `P`** (application-layer treatment of the body):
  - `PLAIN`: no application-level encryption.
  - `DECRYPT`: application-encrypted, **decrypted server-side** (AES, optionally
    with an ML-KEM-768 envelope for the symmetric key).
  - `OPAQUE`: application-encrypted, **never decrypted server-side**; the
    server only relays/stores the ciphertext.

`str(SecurityProfile)` renders as `"<transport>+<payload>"`, e.g.
`"pqc+decrypt"`; this string is used as-is in CSV output and plots.

### 1.2 `HANDSHAKE_OVERHEAD_BYTES`

Client + server key material exchanged in a full handshake:

| Transport | Bytes | Source |
|---|---|---|
| `CLASSIC` (X25519) | 64 | 32 B per side. |
| `PQC` (X25519MLKEM768) | 2336 | theoretical/measured values reported in the paper (ML-KEM-768 encapsulation key 1184 B + ciphertext 1088 B + 2 x 32 B X25519). |

Plane A exposes this via `PianoA.handshake_overhead_bytes()`. It is **not**
added as a bandwidth cost to Plane B: by design the only Plane A signal that
reaches the scoring is the boolean `is_full_handshake` (see Section 4).

### 1.3 `CostCalibration`: the cost model, Eqs. (1)-(3)

All constants are in milliseconds unless noted. They are **defaults, meant to
be overridden**; the intent is a model grounded in the empirical benchmarks
collected for the paper (`report_locali.md`, `report_rete.md`), not arbitrary
numbers.

Each `cost_*` method returns a dict with:

- `cpu_ms` (`c_hat`): projected CPU work for the request;
- `mem_mb` (`m_hat`): projected memory footprint held for the request's lifetime;
- `bytes`: payload size, used as the network work;
- `decrypt_ms` (DECRYPT only): work placed on the dedicated `decrypt` resource.

#### Fixed constants

| Field | Default | Meaning |
|---|---|---|
| `c_fixed_ms` | 0.05 | Fixed per-request handling overhead. |
| `c_business_ms` | 0.05 | Minimal business logic. |
| `c_parse_ms` | 0.02 | Parsing of the encrypted envelope (DECRYPT only). |
| `c_meta_ms` | 0.02 | Metadata handling (OPAQUE only). |
| `m_base_mb` | 2.0 | Base memory per request, MB. |

#### Symmetric-cipher cost: `alpha_cipher_hw_ms_per_byte`

Calibrated on the `Server_AES_Time_ms` column of the local benchmark
(1000 requests, loopback, HTTPS + AES, 5 MB payload):

```
alpha_cipher_hw_ms_per_byte = <AES time for 5 MB> / (5120 * 1024)   [ms/byte]
```

The code uses `27.5765 / (5120 * 1024)`. The original comment quoted a mean
of 14.59 ms for the same column; the 27.5765 ms figure in the expression is
the value actually in effect and should be treated as the calibrated one
(the comment was stale relative to the code). If the benchmark is re-run,
update the numerator only.

#### ML-KEM envelope cost: `kem_envelope_fixed_ms`

Fixed cost of the application-level ML-KEM-768 envelope (decapsulation of the
wrapped symmetric key), calibrated on `Server_KEM_Time_ms` (1000 local
requests). The **median** (0.164 ms) is used rather than the mean, because
the mean is distorted by rare heavy-tailed outliers observed in the data. The
default is rounded up to 0.200 ms.

#### OPAQUE relay cost: `gamma_io_ms_per_byte`, `beta_relay`

OPAQUE requests are never decrypted, so their per-byte CPU cost is pure
copy/framing rather than cryptographic work: an order of magnitude below the
cipher cost. The default is `0.15 * alpha_cipher_hw_ms_per_byte`.
`beta_relay` (default 1.0) is the buffering factor for the streamed relay:
the fraction of the payload assumed to be resident in memory during relay.

#### DECRYPT memory: `decrypt_mem_prior_ratio`

For DECRYPT the memory estimate should ideally be the p95 of the RSS
historically observed for that class (`rss_p95_mb` argument). Until enough
samples exist, a conservative prior is used: `m_base_mb + 0.20 * payload_MB`.
The 0.20 ratio is a prudential prior, not a measurement.

#### Equations

Let `B` = `payload_bytes`, `MB = 1024*1024`.

**Eq. (1), P = PLAIN**

```
c_hat = c_fixed + c_business
m_hat = m_base
```

**Eq. (2), P = DECRYPT** (with or without ML-KEM envelope)

```
aes   = alpha_cipher_hw * B
kem   = kem_envelope_fixed          if use_kem_envelope else 0
c_hat = c_fixed + aes + kem + c_parse + c_business
m_hat = rss_p95_mb                  if available
      = m_base + decrypt_mem_prior_ratio * B / MB   otherwise
decrypt_ms = aes + kem              (work on the dedicated decrypt resource)
```

**Eq. (3), P = OPAQUE**

```
io    = gamma_io * B
c_hat = c_fixed + io + c_meta
m_hat = m_base + beta_relay * B / MB
```

Note the asymmetry: OPAQUE is cheap on CPU but holds the *whole* payload in
memory (`beta_relay = 1.0`), whereas DECRYPT is CPU/decrypt-heavy but has a
small memory prior. This is what makes the T x P composability matter for
placement: the two encrypted classes stress different resources.

### 1.4 `resource_set(payload)`: the relevant resource set R(P)

| Payload | R(P) |
|---|---|
| PLAIN | `("cpu", "mem", "net")` |
| DECRYPT | `("cpu", "mem", "net", "decrypt")` |
| OPAQUE | `("cpu(io)", "mem", "net")` |

`"cpu(io)"` is a label for the same physical CPU resource: `Backend.
projected_utilization` maps both `"cpu"` and `"cpu(io)"` onto `self.cpu`. The
distinct label exists to make explicit, in the risk set, that OPAQUE's CPU
demand is I/O-bound relay rather than compute.

### 1.5 Dominant-resource risk, Eq. (4): `dominant_risk`

```
rho_i(r, P) = max_{k in R(P)}  u'_{i,k} / h_k
```

where `u'_{i,k}` is the *projected* utilisation of resource `k` on node `i`
if request `r` were admitted (see `Backend.projected_utilization`) and `h_k`
is the node's tolerance threshold for that resource (`BackendSpec.h_*`). A
missing utilisation counts as 0; a missing threshold counts as 1; a
non-positive threshold yields infinite risk. `rho >= 1` means "inadmissible"
and is filtered upstream by the dispatcher.

### 1.6 Scoring, Eq. (5): `ScoreWeights`, `score`

```
s = latency_term
  + lambda * [ -log(1 - rho) ]        risk barrier
  + eta    * sigma / SLO              uncertainty
  - gamma  * A                        affinity bonus
```

Defaults: `lambda = 1.0`, `eta = 0.5`, `gamma = 0.3`. Lower score is better.

- **Risk barrier.** `-log(1 - rho)` grows without bound as `rho -> 1`. `rho`
  is clamped to `[0, 1 - 1e-6]` purely to keep the barrier finite; `rho = 1`
  is already excluded upstream as inadmissible.
- **Latency term, two variants.**
  - Default (`latency_term=None`): `L95 / SLO`. **Unbounded**: a node 10x over
    budget is treated as 10x worse than a node 1x over budget, regardless of
    variance. This is the behaviour of all earlier versions.
  - Threshold variant: the caller passes `latency_term = prob_exceed_slo(mean,
    sigma, SLO)`. **Bounded in [0, 1]**: penalises the estimated probability
    of violating the SLO instead of the expected latency relative to it, and
    accounts for mean *and* variance together. At equal L95, a node with a
    heavier tail has a higher violation probability, which `L95/SLO` alone
    cannot distinguish. Everything else in the formula is deliberately left
    unchanged so that experiments isolate the effect of this one
    substitution. See `MERAFDispatcher(slo_penalty_mode=...)` and the
    `"meraf-prob-slo"` configuration in `run_experiments.py`.
- **Uncertainty.** `sigma` is the per-class EWMA standard deviation from
  `Backend.predicted_sigma`, inflated when there are few samples and, per the
  paper, modulated by Plane A's `is_full_handshake` signal.
- **Affinity bonus** `A` rewards nodes already warm for the class (e.g. TLS
  session/tenant affinity); supplied by the dispatcher.

### 1.7 `prob_exceed_slo(mean_ms, sigma_ms, slo_ms)`

Probability that latency exceeds the SLO under a Gaussian latency model with
the given mean and standard deviation, i.e. the same `(EWMA mean, EWMA std)`
pair already used to derive L95, but not collapsed into a single percentile:

```
Phi(z)      = 0.5 * (1 + erf(z / sqrt(2)))          standard normal CDF
P(X > SLO)  = 1 - Phi((SLO - mean) / sigma)
```

`sigma <= 0` is treated as "no uncertainty": the result is 1 if `mean > SLO`
else 0 (deterministic limit). Uses `math.erf` to honour the no-dependency rule.

### 1.8 `derive_resource_threshold(capacity_per_ms, slo_ms, typical_work_per_request)`

Calibrates `h_k` from physical quantities instead of a hard-coded constant:

```
h_k = max( capacity_per_ms * slo_ms / typical_work_per_request , 1 )
```

Interpretation: the number of concurrent flows that, sharing the resource
fairly, would each still complete within the SLO on average. Returns
`inf` when `typical_work_per_request <= 0`.

Rationale (paper, Results section): a fixed threshold such as "8 flows" has
no meaning independent of node capacity; the same flow count is generous for
a high-bandwidth node and insufficient for a weak one. The initial `h_net = 8`
applied uniformly to every node turned out to be up to ~9x lower than the
value implied by the real capacity of the weakest nodes, systematically
over-estimating their risk. Deriving `h_k` from capacity and SLO removes this
context-independent bias.

### 1.9 Static security tiers for the baselines (not part of MERAF)

MERAF has **no notion of a security tier**: it routes on projected resource
risk regardless of which node serves which profile. The helpers below exist
only for the "security-as-constraint" baselines in `dispatchers.py`, which
model security as a deployment constraint (Sections 2 / 4.3 of the revision).
Two different formalisations serve two different baselines:

#### `required_security_tier(profile) -> {0, 1}`

Binary: `PLAIN -> 0`, anything encrypted (`DECRYPT`, `OPAQUE`) `-> 1`. Used
**only** by `SecurityConstraintFastestDispatcher` (`"security-constraint-
satisfaction"`), representative of **[24] Wang et al. 2026** (SeSiC, JPDC; full text in
`related/`). The dispatcher is an online adaptation of SeSiC without batch
rescheduling; the mapping to Eq. 14/15 and Procedures 1-3 of [24], and what is
deliberately not reproduced, is documented in `core-dispatchers.md`.
Node scale: `security_tier` 0 = standard/public, 1 =
trusted/premium.

#### `TRUST_*` constants and `required_trust_label_singh_auluck(payload)`

Three ordered trust levels of a datacentre/backend from **[22] Singh &
Auluck 2020**, Section 3 (full text available under `related/`):

| Constant | Value | Label |
|---|---|---|
| `TRUST_UNTRUSTED` | 0 | `lut` (untrusted) |
| `TRUST_SEMI_TRUSTED` | 1 | `lst` (semi-trusted) |
| `TRUST_HIGHLY_TRUSTED` | 2 | `lht` (highly trusted) |

Eligibility is by order: a node is eligible if `BackendSpec.security_tier >=
required_label`, exactly as in [22] Table 2: `lp` (private) requires `lht`;
`lsp` (semi-private) requires `lht` or `lst`; `lpu` (public) accepts any.

MERAF has no explicit private/semi-private/public jobs, but the (T, P)
taxonomy already has exactly three payload classes ordered by increasing
server-side sensitivity, which map 1:1 onto [22]'s labels in the same order:

| Payload | Server-side treatment | [22] job label | Required node label |
|---|---|---|---|
| PLAIN | no application encryption | `lpu` | any (>= 0) |
| DECRYPT | encrypted, decrypted server-side | `lsp` | `lst` or `lht` (>= 1) |
| OPAQUE | encrypted, never decrypted server-side | `lp` | `lht` only (>= 2) |

Used by `SecurityConstraintLeastLoadedDispatcher` (`"security-constraint-
loadaware"`), which faithfully implements the **ML (Minimum Load)** algorithm
of [22]; see `dispatchers.py`.

---

## 2. `core/resources.py`: `SharedResource`

### 2.1 Motivation

The paper's Results section shows that under high concurrency the observed
throughput saturates while latency grows proportionally to the number of
in-flight requests, consistent with Little's law. To reproduce this in
simulation, each shared resource (CPU, network bandwidth, decryption
subsystem) is modelled as a **processor-sharing** server: its fixed capacity
(work units per ms) is split equally (max-min fair share) among all
currently active flows, and remaining work is recomputed whenever a flow
enters or leaves. This is the standard flow-level simulation technique for
bandwidth/CPU-shared resources.

### 2.2 Semantics

- `_Flow.remaining_work` is in the resource's own units: CPU-ms for `cpu`
  and `decrypt`, bytes for `net`.
- `_advance(now_ms)` charges every active flow `share * elapsed` where
  `share = capacity_per_ms / n_active`, then stamps `_last_update_ms`. With
  no flows, or `elapsed <= 0`, it only updates the timestamp. It is called at
  the start of every mutating/query method, so the flow set is always
  brought up to `now_ms` before being inspected.
- `add_flow` clamps `work_units` to at least `1e-9` so that a zero-work flow
  still completes strictly after it is added (avoids zero-length events).
- `next_completion(now_ms)` returns `(flow_id, absolute_completion_time)` of
  the flow that would finish first **assuming the active set does not change
  before then**. The simulator re-queries after every arrival/departure
  precisely because that assumption breaks whenever another flow joins or
  leaves. Returns `None` when idle or when `share <= 0`.
- `projected_active_count(extra_flows)` is the raw count `n_active + extra`;
  the dispatcher divides it by `h_k` to obtain `u'_{i,k}` (paper section
  "Proiezione del Rischio").

### 2.3 Assumptions and limitations

- Perfect fair share: no scheduling overhead, no per-flow minimum, no
  priorities.
- Capacity is constant over time (no frequency scaling, no background load).
- `mem` is deliberately **not** a `SharedResource` (see Section 3.1).

---

## 3. `core/backend.py`: `BackendSpec`, `Backend`

### 3.1 Resource model

A backend has three fair-share resources (`cpu`, `net`, `decrypt`), each a
`SharedResource`, plus memory. Memory is a **concurrent-capacity constraint**,
not fair-share: the memory a request occupies stays locked for the request's
whole duration and is not "shared" in the sense of slowing anything down.
Hence `mem_used_mb` is a plain counter and `has_mem_headroom` is a hard check.

### 3.2 `BackendSpec` fields

| Field | Default | Meaning |
|---|---|---|
| `name` | required | Node identifier (appears in CSV/plots). |
| `cpu_capacity_per_ms` | required | CPU work units processable per ms. |
| `net_capacity_bytes_per_ms` | required | Network bandwidth. |
| `decrypt_capacity_per_ms` | required | Dedicated decryption capacity (e.g. AES-NI / crypto offload). |
| `mem_capacity_mb` | required | Memory capacity. |
| `h_cpu`, `h_net`, `h_decrypt` | 40, 8, 20 | Tolerance thresholds `h_k`: the maximum acceptable risk per resource, expressed as a number of "equivalent concurrent flows". See `derive_resource_threshold` (Section 1.8) for why fixed defaults are a known weakness and how they are recalibrated from capacity. |
| `h_mem` | 1.0 | For memory the projected utilisation is already a ratio in [0, 1], so the threshold is 1. |
| `base_rtt_ms` | 0.0 | Fixed propagation/RTT latency before data starts flowing: 0 on loopback, tens to thousands of ms on a real network path. Calibrated on the minimum latencies observed in `report_locali.md` / `report_rete.md`. |
| `security_tier` | 0 | Static trust tier of the node. **Not used by MERAF.** Used by the security-as-constraint baselines with a baseline-dependent scale: for `SecurityConstraintLeastLoadedDispatcher` ([22]) 0 = `lut`, 1 = `lst`, 2 = `lht` (`model.required_trust_label_singh_auluck`); for `SecurityConstraintFastestDispatcher` ([24]-style) 0 = standard/public, 1 = trusted/premium (`model.required_security_tier`). A node is eligible when `security_tier >=` the required minimum. |

### 3.3 Risk projection

`projected_utilization(resources_needed, extra=1)` returns `u'_{i,k}` for
each resource in `R(P)`:

- `cpu` (also selected by the `"cpu(io)"` label), `net`, `decrypt`:
  `SharedResource.projected_active_count(extra)`, i.e. the flow count after
  admission. Divided by `h_k` in `dominant_risk`.
- `mem`: `mem_used_mb / mem_capacity_mb` (current ratio; the candidate's own
  memory is checked separately via `has_mem_headroom`). The denominator is
  floored at `1e-9` to avoid division by zero for a zero-capacity spec.

`thresholds()` packages the four `h_*` values in the dict shape
`dominant_risk` expects.

### 3.4 Adaptive learning: per-class latency EWMA

The class key is `Request.cls_key` (route x size-bucket x P x tenant; the
paper describes learning per route/size/P/tenant and the demo keeps exactly
that granularity via the key string). For each class the backend keeps an
EWMA mean, an EWMA variance, the sample count and the time of the last
observation.

`observe_latency(cls, latency_ms, now_ms, alpha=0.2)`:

```
first sample:  mean = x ; var = 0
otherwise:     mean' = alpha*x + (1-alpha)*mean
               var'  = (1-alpha) * ( var + alpha * (x - mean)^2 )
```

The variance update is the standard EWMA-variance approximation (it uses the
*previous* mean in the deviation).

`predicted_l95(cls, default_ms)`:

```
L95 ~ max( mean + 1.645 * std , 0 )
```

with prior `var = (0.3 * default_ms)^2` when the class has never been seen.
The 1.645 factor is the 95th percentile of a standard normal, i.e. a Gaussian
prior. With few samples the estimate is still conservative because
`predicted_sigma` inflates sigma (below).

`predicted_sigma(cls, default_ms)`: EWMA std, with prior `(0.5 * default_ms)^2`
when unseen. For `n_obs < 5` it returns `max(std, 0.5 * default_ms)`: a
prudentially inflated uncertainty until enough samples exist.

`predicted_mean(cls, default_ms)`: the raw EWMA mean without the
`+1.645*std` offset. Needed by the threshold ("probabilistic") scoring
variant (`model.prob_exceed_slo`), which requires mean and sigma separately
rather than a percentile already combined into one number.

### 3.5 Staleness: `is_stale(cls, now_ms, max_age_ms)`

Implements `IsStale()` of Algorithm 1: true if there is no EWMA observation
for the class at all, or the last one is older than `max_age_ms`.

Historical note: the paper revision correctly pointed out that the dispatcher
had stopped checking staleness. In a real deployment per-class EWMA metrics
can arrive late, or stop arriving altogether when a class stops receiving
traffic; blindly trusting an aged mean is worse than admitting that no recent
observation exists. A dispatcher that receives `True` must **not** trust
`predicted_l95` / `predicted_sigma` for that class and must fall back to a
structural estimate (e.g. a `default_l95` derived from the node's nominal
capacity), exactly as it would on first use (`n_obs == 0`).

---

## 4. `core/piano_a.py`: Plane A

### 4.1 Role

Implements the `OnConnection` procedure of Algorithm 1: TLS negotiation
parametrised **exclusively by T**, session resumption, and rate limiting of
new PQC handshakes. Plane A exports exactly **one** signal to Plane B (the
dispatcher/scoring): the boolean `is_full_handshake`, used to modulate the
uncertainty term sigma in the score. It never adds an extra bandwidth cost to
Plane B (paper section "Modellazione dei Profili di Costo"): the handshake
byte overhead (`HANDSHAKE_OVERHEAD_BYTES`) is reported for analysis, not fed
into the network resource.

### 4.2 Resumption model

State is keyed by `(tenant, transport)`. The first connection of a tenant on
a given transport is a full handshake; subsequent connections within
`resumption_window_ms` (default 5000 ms) of the previous one are resumptions.
This mirrors TLS 1.3 PSK / `NewSessionTicket` behaviour. The window is
sliding: every connection (full or resumed) refreshes `_last_seen_ms`.

### 4.3 Rate limiting of PQC handshakes (Algorithm 1, lines 4-5)

For full **PQC** handshakes only, Plane A keeps the timestamps of full PQC
handshakes in the last 1 ms. If their count is already `>=
max_full_handshakes_per_ms` (default 1.0), the connection is counted in
`stats.rate_limited`. In the demo the connection is **not** blocked: the
event is only recorded (this is the "tier amortisation" signal of the
algorithm; enforcement would be a deployment policy). The timestamp is
appended regardless, so the counter reflects offered load.

### 4.4 `PianoAStats`

Counters: `full_handshakes`, `resumptions`, `rate_limited`. Reported by the
simulator alongside dispatcher metrics.

---

## 5. `core/workload.py`

### 5.1 `Request`

One request, or one task of a workflow. Fields:

| Field | Meaning |
|---|---|
| `req_id` | Unique id; for workflow tasks it is also the DAG node id. |
| `arrival_ms` | Arrival time (simulation ms). |
| `profile` | `SecurityProfile (T, P)`. |
| `payload_bytes` | Body size; drives cost Eqs. (1)-(3). |
| `use_kem_envelope` | True if the encrypted payload wraps its key in an ML-KEM envelope (DECRYPT only; adds `kem_envelope_fixed_ms`). |
| `tenant` | Tenant id; part of the class key and of Plane A's resumption key. |
| `route` | Logical route; for workflow tasks this is the **stage name** (`"ingest"`, `"transfer"`, `"aggregate"`, `"stage0"`, `"project"`, ...). |
| `workflow_id`, `predecessors`, `successors` | Workflow membership. `None` / empty for independent requests, in which case behaviour is exactly as before workflows were introduced. When set, the simulator dispatches the task only after all `predecessors` completed and propagates completion to `successors` (see `simulator.py`). |
| `workflow_admitted` | See below. |

**`workflow_admitted`.** True if the workflow this task belongs to passed
workflow-level admission (`Simulator._handle_workflow_arrival`,
`MERAFDispatcher.admit_workflow`). Capacity was then already "reserved" for
the whole DAG at submission time, so a dispatcher that supports per-workflow
admission must **not** reject individual tasks of this workflow again for
projected resource risk. Otherwise upfront admission protects nothing: an
admitted workflow could still fail deeper in the DAG exactly as under
per-task admission, defeating the purpose of the extension. It stays `False`
for independent requests and for dispatchers with no notion of per-workflow
admission.

**`cls_key`.** Key for adaptive learning:
`"{route}|{size_bucket}|{payload}|{tenant}"` with size buckets `small`
(< 512 KiB), `medium` (< 4 MiB), `large`. Because `route` is the stage name
for workflow tasks, the class also distinguishes stages without a dedicated
field.

### 5.2 `WorkloadGenerator`

Independent requests with **Poisson arrivals** (exponential inter-arrival
times, rate `rate_per_ms`) and a configurable profile mix. The default mix
replicates the five configurations empirically benchmarked in the paper:

- CLASSIC+PLAIN, CLASSIC+DECRYPT (AES), CLASSIC+DECRYPT (PQC envelope),
  PQC+PLAIN, PQC+DECRYPT (AES),

with OPAQUE added to complete the "v4" catalogue. In `DEFAULT_MIX` the two
CLASSIC+DECRYPT variants are one profile at 0.20 and are split by
`kem_envelope_fraction` (default 0.5); the five entries are 0.20 each.

Other parameters: `payload_bytes` (default 5 MiB, matching the benchmark
payload), `tenants` (uniform choice), `seed` (private `random.Random`, so
generators are reproducible and independent of each other).

### 5.3 Workflows: motivation

Section 2 of the revision observed that the paper had "no scientific
workflow, no task graph, no inter-task dependency". `WorkflowGenerator`
addresses this while avoiding any duplication: tasks are still `Request`
objects (same cost model, same dispatchers), linked through
`predecessors`/`successors`; the simulator releases a task only when all its
predecessors have completed.

### 5.4 `WorkflowTopology`

| Topology | Structure | Depth | Tasks |
|---|---|---|---|
| `LINEAR` | `stage0 -> stage1 -> ... -> stage{N-1}` (pipeline) | `n_stages` | `n_stages` |
| `FAN_OUT_IN` | `ingest -> W parallel transfer tasks -> aggregate` | 3 | `W + 2` |
| `MONTAGE_LIKE` | `W project -> concatfit -> bgmodel -> W background -> mosaic` | 5 | `2W + 3` |

`MONTAGE_LIKE` is a simplified version of the Montage astronomy workflow
(Deelman et al.): a parallel projection phase (`mProject`, width W), a
sequential fitting bottleneck (`mConcatFit` / `mBgModel`, modelled as two
sequential tasks), a second parallel background-correction phase
(`mBackground`, width W) and the final mosaic aggregation (`mAdd`). Its peak
width equals `fan_width`, the same as `FAN_OUT_IN`, so comparing the two
isolates the effect of **structure** (multiple parallel phases separated by
bottlenecks) at equal peak demand.

`Workflow.tasks` is in topological order (sources first); `task_by_id` is a
convenience index.

### 5.5 `WorkflowGenerator`: per-stage profile assignment

Workflows arrive as a Poisson process (on the **workflow**, not the task: all
tasks share the workflow's `arrival_ms`; dependencies then govern when each
becomes dispatchable). The tenant is drawn per workflow; the transport `T` is
drawn per task from `transport_mix` (default 50/50 CLASSIC/PQC).

`profile_mode="by_stage"` (default) assigns `P` by position in the DAG:

| Stage | Payload | Rationale |
|---|---|---|
| first (`stage_index == 0`) | DECRYPT | ingestion: data must be read server-side |
| last (`stage_index == n-1`) | PLAIN | post-processing / aggregation of already-processed data |
| intermediate | OPAQUE | relay without reading |

For `MONTAGE_LIKE` the five levels map to `project`=DECRYPT,
`concatfit`/`bgmodel`/`background`=OPAQUE, `mosaic`=PLAIN.

The hypothesis under test (README, "Workflow: per-task vs per-workflow
admission, and the structured mix") is that T x P composability pays off when
the profile mix is **correlated with workflow structure** rather than averaged
away by a uniform assignment.

`profile_mode="random"` assigns `P` uniformly at random per task (the same
distribution as `WorkloadGenerator`) with the same topology and load: the
control needed to compare structured vs uniform mixes.

Only `"by_stage"` and `"random"` are accepted; anything else raises
`ValueError` at construction (message intentionally in Italian, as shipped).

### 5.6 Why these two (three) topologies

LINEAR and FAN_OUT_IN were chosen because together they cover the majority of
real scientific workflows (Montage-style pipelines, genomic ETL) while
remaining trivial to generate and analyse. MONTAGE_LIKE was added later to
test whether multi-phase structure changes the conclusions drawn from the
single fan-out/fan-in.

---

## 6. Cross-cutting notes and known limitations

- **Class granularity.** Learning is keyed by `route|size|P|tenant`. Comments
  in earlier versions described a simplification to `P` only "to keep the
  demo readable"; the current `cls_key` includes all four dimensions.
- **Gaussian latency prior.** Both `predicted_l95` (1.645 sigma) and
  `prob_exceed_slo` assume normally distributed latency. Measured
  distributions are heavy-tailed (the same reason the KEM cost uses the
  median), so L95 is a lower-biased estimate under load; the probabilistic
  variant partly compensates by using the variance directly.
- **Fixed `h_k` defaults.** `BackendSpec` defaults (`40/8/20`) are legacy and
  context-independent; experiments should derive them with
  `derive_resource_threshold` (Section 1.8).
- **Stale calibration comment.** The AES per-byte coefficient in code
  (27.5765 ms / 5 MiB) did not match the 14.59 ms figure quoted in the old
  comment; the code value is authoritative.
- **Plane A does not block.** Rate limiting only counts violations.
- **Memory is not fair-shared.** By design; see Section 3.1.
- **No external dependencies.** Keep it that way; `math.erf` replaces
  `scipy.stats.norm`.

# MERAF: reference implementation and simulator

MERAF (Modular Encryption- and Resource-Aware Framework) is a security-aware
load balancer: it routes requests carrying a composable security profile
(T = TLS key exchange, P = payload treatment) on projected resource risk and
cost, with admission control. This package contains the reference
implementation of the algorithm formalised in the paper (`paper.tex`), a
flow-level discrete-event simulator that exercises it against standard and
security-aware baselines, and the experiment / analysis / reporting pipeline
that produces every table in the paper. Standard library only, Python 3.9+:
no external dependencies.

## Repository layout

All commands are run from the repository root (the directory that contains
`MERAF/` and `paper.tex`) as `python3 -m MERAF.<subpackage>.<script>`.

```text
MERAF/                       single Python package
  core/                      library: algorithm and simulator
    model.py         Eqs. (1)-(5): (T,P) profiles, per-class cost, dominant-
                     resource risk, scoring (with optional "threshold" variant)
    resources.py     Processor-sharing resource (dynamic fair share) used to
                     model CPU / bandwidth / decrypt saturation
    backend.py       Backend node: heterogeneous capacities, per-class EWMA
    piano_a.py       Plane A (OnConnection): TLS resumption, PQC handshake
                     rate limiting
    workload.py      Request generator (Poisson arrivals, profile mix) and
                     WorkflowGenerator (DAGs of dependent tasks)
    dispatchers.py   MERAFDispatcher (OnRequest, per-task and per-workflow
                     admission), security-agnostic baselines, and the two
                     "security-as-constraint" baselines
    simulator.py     Flow-level discrete-event engine: run() and run_workflows()
    metrics.py       Run summaries (p50/p95/p99, goodput, rejects, offered
                     percentiles), workflow summaries, CSV writers
    stats_utils.py   Sample std / CI95 (Student t), TOST, identical-results guard
  experiments/               scripts that run simulations
    run_experiments.py          independent requests: all algorithms x loads x
                                scenarios x seeds; CSVs + .tex tables
    run_workflow_experiments.py same for task DAGs (resumable)
    sensitivity_sweep.py        one-factor-at-a-time hyperparameter sweep
  analysis/                  post-processing of the CSVs (no simulation)
    aggregate_results.py        merge replicas spread over sub-directories
    equivalence_test.py         TOST equivalence between (T,P) profiles
    analyze_client_timeout.py   client-side view under a finite client timeout
    analyze_deadline_sensitivity.py  workflow goodput vs deadline multiplier
    analyze_profile_pairing.py  paired test: by_stage vs random profile mode
  reporting/                 LaTeX tables
    make_latex_tables.py        generic tables, called by the scripts above
    make_paper_tables.py        the paper's tables; --write updates paper.tex
  tools/
    merge.py                 all summary CSVs under results/ -> results/results.md
  results/                   data produced by the experiments
    outputs/                 request level (run_experiments, TOST, sweep, paper tables)
    outputs_wf_by_stage/     workflows, profile assigned per stage
    outputs_wf_random/       workflows, profile assigned at random
    for_merge/               hand-picked copies of summary CSVs
    results.md               every summary CSV as Markdown (tools/merge.py)
  docs/                      design rationale and engineering references
```

The public repository tracks the code and the documentation only:
`results/` (experiment CSVs and generated tables) and
`reporting/make_paper_tables.py` (paper-specific table generator) are
excluded via `.gitignore`.

## Quick start

Each command below accepts `--help`; the option names are authoritative there.

```bash
# 1. Request-level experiments (12 algorithms, 2 scenarios, 4 loads, 5 seeds).
#    Writes CSVs and .tex tables to MERAF/results/outputs/.
python3 -m MERAF.experiments.run_experiments
python3 -m MERAF.experiments.run_experiments --horizon-ms 30000 \
    --loads 0.02 0.06 0.12 0.16 --scenarios locale rete --seeds 1 2 3 4 5 \
    --outdir MERAF/results/outputs
#    Useful switches: --tenants N, --n-backends N --candidates-d D (exercise
#    power-of-d sampling), --no-save-completed, --no-latex, --profile-table-algo.

# 2. TOST equivalence of latency across (T,P) profiles.
python3 -m MERAF.analysis.equivalence_test --outdir MERAF/results/outputs \
    --metric latency_p95_ms --margin-ms 100

# 3. Hyperparameter sensitivity sweep (one scenario and load per run).
python3 -m MERAF.experiments.sensitivity_sweep --scenario rete --load 0.16 \
    --seeds 1 2 3 4 5 --outdir MERAF/results/outputs

# 4. Workflow experiments: per-task vs per-workflow admission on task DAGs.
#    by_stage = profile correlated with the stage (hypothesis under test)
python3 -m MERAF.experiments.run_workflow_experiments --profile-mode by_stage \
    --topologies linear fan_out_in montage_like --loads 0.02 0.06 0.12 0.16 \
    --scenarios locale rete --seeds 1 2 3 4 5 \
    --outdir MERAF/results/outputs_wf_by_stage
#    random = uniformly random profile per task (the control)
python3 -m MERAF.experiments.run_workflow_experiments --profile-mode random \
    --topologies linear fan_out_in montage_like --loads 0.02 0.06 0.12 0.16 \
    --scenarios locale rete --seeds 1 2 3 4 5 \
    --outdir MERAF/results/outputs_wf_random
#    Other switches: --algorithms <keys>, --n-stages, --fan-width,
#    --no-save-records, --duty-cycle 0.5 (throttle CPU), --restart.

# 5. Aggregate replicas that were run into separate sub-directories
#    (e.g. output/seed_1/, output/seed_2/, ...).
python3 -m MERAF.analysis.aggregate_results --root output --outdir aggregated

# 6. Derived analyses on saved logs (no new simulation).
python3 -m MERAF.analysis.analyze_client_timeout --scenario rete \
    --loads 0.12 0.16 --thresholds-ms 5000 10000 20000
python3 -m MERAF.analysis.analyze_deadline_sensitivity \
    --outdir MERAF/results/outputs_wf_by_stage --ks 0.75 1.0 1.5 2.0
python3 -m MERAF.analysis.analyze_profile_pairing \
    --bystage MERAF/results/outputs_wf_by_stage \
    --random MERAF/results/outputs_wf_random

# 7. Paper tables (make_paper_tables.py is not in the public repository):
#    print to stdout, save paper_tab_*.tex; --write splices the data rows
#    into paper.tex between its AUTO-ROWS markers.
python3 -m MERAF.reporting.make_paper_tables
python3 -m MERAF.reporting.make_paper_tables --write paper.tex
#    Regenerate a single generic table for another algorithm / load:
python3 -m MERAF.reporting.make_latex_tables profile-breakdown \
    --file MERAF/results/outputs/profile_breakdown_rete.csv --algo meraf --load 0.16

# 8. Merge every summary CSV under MERAF/results/outputs* into one Markdown file.
#    Plain script, no options: it runs the merge as soon as it is invoked.
python3 MERAF/tools/merge.py
```

Loads are arrival rates in requests/ms (workflows/ms for step 4); `0.02` is
20 requests per second. Scenario names are `locale` (loopback) and `rete`
(real network) and appear verbatim in file names.

`run_workflow_experiments` is resumable: every (algorithm, load, seed) cell is
flushed to the block's `raw_perseed.csv` as soon as it is computed, and a
rerun with the same command skips the cells already present. Pass
`--restart` to recompute from scratch (required after any code change or any
change of `--horizon-ms`, `--tenants`, `--n-stages`, `--fan-width`).

## Outputs

`MERAF/results/outputs/` (request level):

- `summary_<scenario>_raw_perseed.csv`: one row per (algorithm, load, seed), every `RunSummary` field.
- `summary_<scenario>_aggregated.csv`: one row per (algorithm, load): mean/std/CI95/min/max over seeds; `summary_<scenario>_table.tex` is the matching comparison table.
- `completed_<scenario>_<algo>_<load>_seed<N>.csv`: per-request log of one replica (arrival, dispatch, completion, backend, profile).
- `profile_breakdown_<scenario>.csv`: latency metrics per (T,P) profile, keyed `<algo>@<load>@seed<N>`; `profile_breakdown_<scenario>_<algo>_<load>.tex` one table per load.
- `equivalence_<scenario>.csv` (+ `_table.tex`): TOST verdict per (algorithm, load, profile pair).
- `sensitivity_sweep_<scenario>_load<load>.csv` (+ `_table.tex`): MERAF p99 vs best baseline p99 and advantage ratio per hyperparameter value.
- `client_timeout_view.csv`: success rate, goodput and censored percentiles under a finite client timeout.
- `paper_tab_<name>.tex`: the six paper tables (`sim_network`, `sim_profiles`, `sim_TOST`, `sim_local`, `wf_network`, `wf_ablation`).

`MERAF/results/outputs_wf_by_stage/` and `MERAF/results/outputs_wf_random/` (workflows, one directory per profile mode):

- `workflow_summary_<scenario>_<topology>_<mode>_raw_perseed.csv`: one row per (algorithm, load, seed): completion rate, makespan p50/p95/p99, goodput, within-deadline share, wasted work.
- `workflow_summary_<scenario>_<topology>_<mode>_aggregated.csv`: mean/std/CI95 over seeds, with `_n_valid` counts.
- `workflows_<scenario>_<topology>_<mode>_<algo>_<load>_seed<N>.csv`: per-workflow log of one replica (suppressed by `--no-save-records`).
- `deadline_sensitivity.csv`, `profile_pairing_ttest.csv`, outputs of the two workflow analyses (written next to the `by_stage` data).

Per-seed logs (`completed_*`, `workflows_*`) dominate disk usage: the three
result directories currently total about 3 GB, of which the summary files are
a few MB. `tools/merge.py` skips them by pattern; use `--no-save-completed` /
`--no-save-records` when the logs are not needed.

## Algorithms

Keys accepted by `--algorithms` and used in every CSV. R = `run_experiments`,
W = `run_workflow_experiments`.

| Key | Runner | What it is |
|---|---|---|
| `meraf` | R, W | Full MERAF as in the paper: cost differentiated by P, rho-based admission, linear `L95/SLO` term, eta = 0.6. In W: per-task admission. |
| `meraf-no-cost-diff` | R, W | Ablation, P axis off: every request costed as PLAIN; admission and weights unchanged. |
| `meraf-eta0` | R | Ablation, T axis off: eta = 0 removes the additive sigma term, the only place a PQC handshake influences linear scoring. |
| `meraf-security-blind` | R | Both axes off (eta = 0, PLAIN cost): projective admission control alone. Completes the 2x2 T x P factorial with the three above. |
| `meraf-prob-slo` | R | "Threshold" scoring: `L95/SLO` replaced by `P(latency > SLO)` under a Gaussian model, bounded in [0, 1]. |
| `meraf-prob-slo-eta0` | R | Threshold scoring with eta = 0; separates the SLO-term substitution from the double counting of variance. Does not switch off T. |
| `meraf-workflow-admission` | W | Whole-DAG admission on projected risk of the widest topological level, with capacity reserved for the workflow's lifetime; admitted tasks bypass the per-task risk gate. |
| `meraf-workflow-deadline` | W | Ablation: same per-DAG decision point, criterion = estimated makespan vs `depth x SLO`, no reservation. |
| `round-robin` | R, W | Cyclic assignment, no cost model. |
| `least-connections` | R, W | Fewest active flows, seeded random tie-break. |
| `power-of-2` | R, W | Two random candidates, fewer active flows wins. |
| `ewma-lrt` | R, W | Lowest EWMA-predicted p95 latency for a single undifferentiated class. |
| `security-constraint-loadaware` | R, W | ML algorithm of Singh & Auluck 2020: trust-tier eligibility per payload label (OPAQUE pinned to the tenant's local node), then the eligible node with most spare capacity (minimum rho). |
| `security-constraint-satisfaction` | R, W | Online adaptation of SeSiC (Wang et al. 2026): PLAIN tries the public pool first, encrypted payloads only the private pool; a node must meet the deadline estimate and have spare capacity, otherwise the request is dropped. |

Baselines have no notion of a workflow; in W the simulator runs them per task.

## Further reading

- `MERAF/docs/README.md`: index of the engineering references (cost model
  and calibration, dispatchers and baselines, simulator and metrics,
  experimental design and its historical bug notes, analysis scripts,
  reporting tools).
- `paper.tex`: formal description of the algorithm (Eqs. (1)-(5),
  Algorithm 1) and the results the pipeline reproduces.

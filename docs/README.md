# MERAF developer documentation

Source comments are kept short on purpose: they say *what* a piece of code
does and flag non-obvious gotchas. The *why* (design rationale, modelling
assumptions, calibration sources, ablation variants, statistical choices,
and the bug history that explains current behaviour) lives here.

| Document | Covers |
|---|---|
| [core-model.md](core-model.md) | `core/model.py`, `core/backend.py`, `core/resources.py`, `core/piano_a.py`, `core/workload.py`: security profiles, cost model, resource risk, scoring, backend fleet, Plane A, request/workflow generators |
| [core-dispatchers.md](core-dispatchers.md) | `core/dispatchers.py`: `MERAFDispatcher` (per-task and per-workflow admission), baselines, ablation variants |
| [core-simulation.md](core-simulation.md) | `core/simulator.py`, `core/metrics.py`, `core/stats_utils.py`: event loop, DAG execution, metric definitions, CI95/TOST |
| [experiments.md](experiments.md) | `experiments/`: experimental grid, fleet construction, algorithm variants, resumability, workflow experiments, sensitivity sweep |
| [analysis.md](analysis.md) | `analysis/`: aggregation of replicas, TOST equivalence, client-timeout view, deadline sensitivity, profile pairing |
| [reporting-tools.md](reporting-tools.md) | `reporting/` and `tools/`: LaTeX table generation, in-place update of `paper.tex`, CSV-to-Markdown merge |

Running instructions and the directory layout are in the top-level
[README.md](../README.md).

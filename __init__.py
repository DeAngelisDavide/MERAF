"""MERAF: reference implementation, discrete-event simulator and experiment pipeline.

Subpackages:
  MERAF.core         library (cost model, backends, dispatcher, simulator, metrics)
  MERAF.experiments  simulation drivers (run_*, sensitivity_sweep)
  MERAF.analysis     CSV post-processing (aggregation, TOST, derived analyses)
  MERAF.reporting    LaTeX table generation for the paper

Run scripts from the directory containing `MERAF/` with
`python3 -m MERAF.<subpackage>.<script>`; data lives in MERAF/results/.
"""

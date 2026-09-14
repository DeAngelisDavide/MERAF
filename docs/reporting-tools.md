# Reporting tools

Reference for the scripts that turn the CSVs in `MERAF/results/` into paper
artifacts: `reporting/make_latex_tables.py`, `reporting/make_paper_tables.py`
and `tools/merge.py`. All commands are run from the directory that contains
`MERAF/`.

## Data flow

```
experiments/run_experiments.py ─┬─> results/outputs/summary_<scenario>_aggregated.csv
                                ├─> results/outputs/profile_breakdown_<scenario>.csv
analysis/equivalence_test.py ───┼─> results/outputs/equivalence_<scenario>.csv
experiments/sensitivity_sweep.py┼─> results/outputs/sensitivity_sweep_<scenario>_load<X>.csv
run_workflow_experiments.py ────┴─> results/outputs_wf_by_stage/workflow_summary_<scenario>_<topo>_by_stage_aggregated.csv
                                        │
          make_latex_tables.py  <───────┤  generic .tex next to each CSV (called from the scripts above)
          make_paper_tables.py  <───────┤  paper tables, results/outputs/paper_tab_<name>.tex, --write paper.tex
          tools/merge.py        <───────┘  results/results.md (all summary CSVs as Markdown)
```

Scenario names are the Italian ones used in the CSV file names: `rete`
(cloud network) and `locale` (local). Aggregated CSVs carry one row per
(algorithm, load) with `n_seeds` and, for every metric, `<metric>_mean` and
`<metric>_ci95` (Student-t 95% half-width over seeds). Per-seed CSVs
(`profile_breakdown_*`) key rows by `algorithm = "<algo>@<load>@seed<N>"`,
parsed with `KEY_RE` in both reporting modules.

---

## `reporting/make_latex_tables.py`

Generic, scenario-agnostic tables: each `generate_*_table()` is imported and
called by the experiment script right after it writes its CSV, so a `.tex`
appears next to every CSV without a separate step. The CLI exists to
regenerate a single table for another algorithm/load without re-running a
simulation:

```
python3 -m MERAF.reporting.make_latex_tables profile-breakdown \
    --file MERAF/results/outputs/profile_breakdown_locale.csv --algo meraf --load 0.16
python3 -m MERAF.reporting.make_latex_tables equivalence --file MERAF/results/outputs/equivalence_locale.csv
python3 -m MERAF.reporting.make_latex_tables sensitivity \
    --file MERAF/results/outputs/sensitivity_sweep_locale_load0.160.csv
```

Output path defaults to `<csv basename>_<suffix>.tex` in the CSV's directory
(`_default_out`); `--out` overrides it. Underscores in algorithm/profile names
are escaped for LaTeX (`_latex_escape`), nothing else is.

### `write_main_comparison_table(path, scenario, agg)`

Algorithm x load table for one scenario (`\label{tab:meraf_<scenario>}`).
`agg` is the dict produced by `run_experiments._aggregate` or
`aggregate_results.aggregate`: key `(algorithm, load)`, value with
`algorithm`, `load`, `n_seeds`, `<metric>_mean/_ci95`. Rows are sorted by
load then algorithm; the load cell is printed only on the first row of each
load group.

Columns: load, algorithm, RPS, p50, p95, p99, **offered p99**, reject rate.
The offered p99 is computed on the *offered* distribution (completed +
rejected requests, rejected ones penalised at the client timeout stated in the
caption). It is deliberately kept as a separate column next to the admitted
p99 and the reject rate: above a rejection threshold (1% of requests) the
percentile falls into the censored tail and is only a convention, so the number
is not interpretable without the other two (see
`core.metrics.offered_percentile_reachable`). Cell rules:

- `latency_p99_offered_ms_n_reachable == 0` -> `n/a`;
- `0 < n_reachable < n_seeds` -> `mean ± ci95 (k/n)`, mean over the k seeds
  under threshold only;
- otherwise plain `mean ± ci95`.

The caption states the penalty value, taken from `reject_penalty_ms_mean` or
`reject_penalty_ms` of the first row (`?` if absent).

### `generate_profile_breakdown_table(file, algo, load, out=None)`

Per-security-profile (T,P) latency table for a single (scenario, algorithm,
load), in the style of the paper's profile-isolation tables. Reads
`profile_breakdown_<scenario>.csv`, keeps rows whose key matches `algo` and
`load` (tolerance 1e-9), groups by `profile`, and reports p50/p95/p99 as
mean ± CI95 of the per-seed statistics (`core.stats_utils.mean_std_ci95`).
Profiles are sorted alphabetically. The scenario for the label is guessed from
the file name (`profile_breakdown_(<scenario>).csv`, fallback `scenario`).
Label: `tab:profile_<scenario>_<algo without dashes>_<load without dot>`.

Returns the written path, or `None` after printing a message when no row
matches; it never raises, because `run_experiments.py` calls it for every load
and one empty combination must not abort the run.

### `generate_equivalence_table(file, algo=None, load=None, out=None)`

TOST verdict table from `equivalence_<scenario>.csv` (one row per profile
pair, algorithm and load). Optional filters on `algorithm` and `load`. Scenario,
metric and margin are read from the first row and shown in the caption. Rows
sorted by (algorithm, load); the verdict column shows `\checkmark` when the
CSV field `equivalent` is the string `True`, `$\times$` otherwise. Returns
`None` for an empty file or an empty filter result.

### `generate_sensitivity_table(file, out=None)`

Sweep table from `sensitivity_sweep_<scenario>_load<X>.csv`: for each
hyperparameter value, MERAF p99, best-baseline p99 (both mean ± CI95) and the
advantage ratio (`advantage_ratio`, printed with a trailing `×`). The parameter
name is printed only on the first row of each parameter block; rows keep the
CSV order. Label is always `tab:sensitivity`.

---

## `reporting/make_paper_tables.py`

Produces the exact tables of the paper so that no number is transcribed by
hand. Every cell is `mean ± CI95` (Student t) over the seeds. Six tables are
rendered on every run:

| Label | Source CSV | Renderer |
|---|---|---|
| `tab:sim_network` | `results/outputs/summary_<scenario>_aggregated.csv` | `render(build_rows(...))` |
| `tab:sim_profiles` | `results/outputs/profile_breakdown_<scenario>.csv` | `render_profiles` |
| `tab:sim_TOST` | `results/outputs/equivalence_<scenario>.csv` | `render_tost` |
| `tab:sim_local` | `results/outputs/summary_locale_aggregated.csv` (fixed) | `render_local` |
| `tab:wf_network` | `results/outputs_wf_by_stage/workflow_summary_rete_<topo>_by_stage_aggregated.csv` | `render_wf_network` |
| `tab:wf_ablation` | same as above | `render_wf_ablation` |

`--scenario` (default `rete`) selects the network-scenario CSVs for the first
three tables; `tab:sim_local` always reads `locale` and the workflow tables
always read `rete`.

```
python3 -m MERAF.reporting.make_paper_tables                      # print LaTeX to stdout
python3 -m MERAF.reporting.make_paper_tables --write paper.tex    # also update paper.tex in place
```

Each full table (caption, label, header, rows) is printed and saved to
`results/outputs/paper_tab_<name>.tex` (`:` replaced by `_`), wrapped in
`% BEGIN AUTO <label> -- ...` / `% END AUTO <label>` comment lines.

### Algorithm names and paper labels

`ALGOS` maps CSV algorithm identifiers to the labels used in the paper and
also acts as the whitelist for the request-level tables: rows whose algorithm is
not in `ALGOS` (the `meraf-*` ablation variants) are dropped.

| CSV identifier | Paper label |
|---|---|
| `meraf` | `\textbf{MERAF}` |
| `round-robin` | Round Robin |
| `least-connections` | Least Connections (`Least Conn.` in `tab:sim_local`) |
| `power-of-2` | Power-of-Two |
| `ewma-lrt` | EWMA-LRT |
| `security-constraint-loadaware` | sec-loadaware |
| `security-constraint-satisfaction` | sec-satisfaction |

Workflow tables add `meraf-workflow-admission` (shown as `\textbf{MERAF}` in
`tab:wf_network`; the `chr(92)+...` expression in the code is just that string
built without a raw-string escape) and the `WF_VARIANTS` labels for the
ablation: `meraf-workflow-admission` -> per-workflow, `meraf` -> per-task,
`meraf-workflow-deadline` -> deadline-gate.

Security profiles (`PROFILE_NAMES`, fixed order `PROFILE_ORDER`):
`classic+plain`, `classic+decrypt`, `classic+opaque`, `pqc+plain`,
`pqc+decrypt` -> "Classic + Plain", ..., "PQC + Decrypt".

Loads: `LOADS = [0.02, 0.06, 0.12, 0.16]` req/ms for request-level tables;
`WF_LOADS_GOODPUT = [0.01, 0.02, 0.06, 0.12, 1.0]`, `WF_LOAD_DETAIL = 0.12`
and `WF_ABL_LOADS = [0.02, 0.06, 0.12, 1.0]` workflows/ms for workflow tables.
Loads are rounded to 3 decimals before matching.

### Boldface rule (all tables)

A value is bolded only when it is the *unique* best in its comparison group
(`unique_best` / `_mark_best`, tolerance 1e-9). Ties get no boldface (e.g.
goodput at 0.02 req/ms, where every algorithm admits everything). In the
ablation table, a load whose rows have no boldface at all means the variants
are bit-identical (unsaturated regime), which the caption states.

### `tab:sim_network` (`build_rows`, `render`)

- Rows grouped per load (`\multirow`), sorted by increasing mean p99.
- Columns: algorithm, RPS, p50, p95, p99, dropped %, goodput,
  `L^offered_p95`.
- Goodput is always shown; it equals admitted RPS whenever every admitted
  request meets the SLO.
- `L^offered_p95` is shown only if the reachability condition held in *all*
  replications (`latency_p95_offered_ms_n_reachable == n_seeds`), else `n/a`.
  With no rejections it coincides with the admitted p95.
- Bold per load: min p95, min p99, max goodput, min offered p95 among the
  reachable rows.
- Uses `\resizebox{\textwidth}{!}` and `table*`.

### `tab:sim_profiles` (`render_profiles`)

MERAF at 0.12 req/ms by default. Per profile, p50/p95/p99 as mean ± CI95 of
the per-seed statistics, computed by the local `_mean_ci95` (t-critical
lookup table for n = 2..10; n = 1 gives CI 0). Cells are typeset as
`$m \pm ci$`. The "Decrypt" profiles pool requests with and without the ML-KEM
envelope, half each, as generated by the load generator. `n_seeds` for the
caption is the number of samples of the first profile.

### `tab:sim_TOST` (`render_tost`)

For MERAF only, per load: number of equivalent pairs over the total (10
pairwise comparisons for 5 profiles) and the maximum |diff|. `equivalent` is
accepted as `true`/`1`/`yes` (case-insensitive); the margin in the caption is
the last `margin_ms` read.

### `tab:sim_local` (`render_local`)

p99 per algorithm at the four loads, from `summary_locale_aggregated.csv`.

- Asserts that the maximum mean reject rate is exactly 0: the caption claims
  0% rejections in the local scenario, and the script must fail loudly if the
  data no longer supports it.
- Algorithms whose (mean, ci95) p99 cells, rounded to 2 decimals, are identical
  at every load are merged into one row (`Round Robin / Least Conn.` in the
  current data). Groups are sorted by the sum of mean p99 over loads.
- Bold: unique column minimum, even when it is a baseline. At 0.16 req/ms the
  fixed p99 of Round Robin/Least Connections is below MERAF's.
- Dagger (`\textsuperscript{\dag}`): a group with a CI of exactly 0.0 at some
  load is a deterministic artifact (no queueing on the weak node, fixed
  computation cost). The dagger goes on the row name, and also on a bolded
  cell whose CI is 0, so the footnote applies visibly to that minimum.
  Power-of-Two departs from the pattern at 0.12 and 0.16 req/ms.

### `tab:wf_network` (`render_wf_network`)

Per topology (`WF_TOPOS`: linear, fan-out/in, montage-like), MERAF
per-workflow admission first, then the baselines present at all goodput loads,
sorted by decreasing total goodput. Columns: goodput (wf/s) at the five loads,
then completion %, makespan p99 (ms, 0 decimals) and deadline % at 0.12 wf/ms.
Deadline % is printed as a mean only (no CI). Bold per topology and column.

### `tab:wf_ablation` (`render_wf_ablation`)

Per topology and load, the three admission variants with completion %,
makespan p99, goodput, deadline % (mean only) and wasted CPU (ms total, mean
only, min is best). Bold per topology, load and metric.

### `_wpm` and `pm`

Cells are typeset as `m $\pm$ c`, with only `\pm` in math mode. `\textbf{}`
has no effect inside `$...$`, so the earlier `$m \pm c$` form could not be
bolded; this also matches the request-level tables.

### AUTO-ROWS marker mechanism (`rows_only`, `splice`, `--write`)

The paper keeps caption, label and header under the author's control; only the
data rows are machine-written. In `paper.tex` each table body is delimited by

```latex
% BEGIN AUTO-ROWS tab:<name>
... data rows ...
% END AUTO-ROWS tab:<name>
```

With `--write PAPER_TEX`:

1. `rows_only(tex)` extracts, from the full generated table, the lines after
   the first `\midrule` and before `\bottomrule`. Consequence: a table's header
   must end with exactly one `\midrule` before the data; intermediate
   `\midrule`s between load groups are part of the rows and are copied along.
2. `splice(paper, name, tex)` replaces everything between the two markers
   (regex, `re.S`, first match) with those rows. Trailing text on the BEGIN
   line is preserved; the END marker must be followed by a newline.
3. If the markers for a table are missing (for instance the paper was
   overwritten from an Overleaf copy), a warning is printed and that table is
   skipped; the other tables are still updated.
4. The file is rewritten in place; a list of the labels processed is printed.

The `% BEGIN AUTO <label>` / `% END AUTO <label>` lines emitted around the full
table are a different marker pair: they annotate the standalone
`paper_tab_*.tex` files and are *not* what `splice` looks for.

### Assumptions and known limitations

- CSV columns are accessed by name; missing columns raise `KeyError`.
- All loads in `LOADS`/`WF_*` must exist in the CSVs, and all `ALGOS` entries
  must be present in `summary_locale_aggregated.csv` (`render_local` indexes
  `by[a][L]` directly).
- `_mean_ci95` supports only 2..10 seeds (`KeyError` otherwise);
  `make_latex_tables` uses `core.stats_utils.mean_std_ci95` instead.
- `render_profiles` requires every profile in `PROFILE_ORDER` to be present.
- Comparisons for boldface are made on floating-point means with tolerance
  1e-9 (`tab:sim_local` uses values rounded to 2 decimals).
- The scripts write only to `results/outputs/` and to the `.tex` passed to
  `--write`; no backup of the paper is kept.

---

## `tools/merge.py`

Concatenates every *summary* CSV under `MERAF/results/` into a single Markdown
file `MERAF/results/results.md`, for reading results without opening dozens of
files. It is a plain script (module-level call, no `__main__` guard):

```
python3 MERAF/tools/merge.py
```

### Selection rules

- `EXPERIMENTS_ROOT = "MERAF/results"`: root searched (relative to the
  current directory, hence the requirement to run from the parent of `MERAF/`).
- `DIR_PATTERN = "outputs*"`: any direct subdirectory whose name starts
  with `outputs` is an experiment directory (`outputs`, `outputs_wf_by_stage`,
  `outputs_wf_random`, and any future directory following the scheme). No
  manual list to maintain.
- `EXCLUDE_PATTERN = _seed\d+\.csv$`: per-request and per-workflow logs
  (`completed_locale_meraf_0.020_seed1.csv`,
  `workflows_locale_linear_by_stage_meraf_0.060_seed1.csv`, ...) are excluded.
  They total hundreds of MB, one row per request/workflow, and carry no
  information in aggregate form. The `_seed<N>.csv` suffix is shared by both
  naming schemes used in the project, so the rule holds for every scenario,
  algorithm and topology. Summary files (`summary_*`, `workflow_summary_*`,
  `equivalence_*`, `profile_breakdown_*`, `sensitivity_sweep_*`, ...) are kept.

### Output format (`merge_csv_to_markdown`)

Files are sorted by directory name, then file name, so tables from the same
experiment stay together. For each directory a `# <dir>` heading is written
once; each CSV becomes a `## <file>` heading followed by a GitHub-style table
(header row, `|---|` separator, one row per non-empty CSV line). A CSV with no
header row produces the note `*This CSV file is empty.*`. Comma delimiter
only. The final message reports how many files were merged and how many
per-seed logs were skipped.

### Limitations

- Cell content is not escaped: a `|` inside a CSV field breaks the Markdown
  table.
- Only CSVs directly inside each `outputs*` directory are considered (no
  recursion).
- Header count is not validated against row lengths.

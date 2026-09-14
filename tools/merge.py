import csv
import re
from pathlib import Path

# Root holding the experiment directories (outputs, outputs_wf_by_stage, ...).
EXPERIMENTS_ROOT = "MERAF/results"

# Glob (relative to EXPERIMENTS_ROOT) selecting experiment directories.
DIR_PATTERN = "outputs*"

# Per-request/per-workflow logs end in "_seed<N>.csv" in both naming schemes used by
# the project; they are hundreds of MB and meaningless in aggregate, so always skip them.
EXCLUDE_PATTERN = re.compile(r"_seed\d+\.csv$")


def find_files_to_merge(root):
    """Return (experiment_dirs, summary_csvs) under `root`.

    CSVs matching EXCLUDE_PATTERN are skipped; the list is sorted by
    directory then file name so the output groups files per experiment.
    """
    root = Path(root)
    dirs = sorted((p for p in root.glob(DIR_PATTERN) if p.is_dir()),
                       key=lambda p: p.name)

    found = []
    for d in dirs:
        for f in sorted(d.glob("*.csv"), key=lambda p: p.name):
            if EXCLUDE_PATTERN.search(f.name):
                continue
            found.append(f)
    return dirs, found


def merge_csv_to_markdown(experiments_root, file_output="merged_results.md"):
    """Concatenate all summary CSVs into one Markdown file, one table per CSV."""
    dirs, csv_files = find_files_to_merge(experiments_root)

    if not dirs:
        print(f"Error: no directory matching '{DIR_PATTERN}' "
              f"found under '{experiments_root}'.")
        return
    if not csv_files:
        names = ", ".join(c.name for c in dirs)
        print(f"No summary CSV found in the discovered directories ({names}). "
              f"Check EXCLUDE_PATTERN if you expected files.")
        return

    n_excluded = 0
    for d in dirs:
        n_excluded += sum(1 for f in d.glob("*.csv") if EXCLUDE_PATTERN.search(f.name))

    with open(file_output, 'w', encoding='utf-8') as out:
        current_dir = None

        for csv_path in csv_files:
            if csv_path.parent != current_dir:
                current_dir = csv_path.parent
                out.write(f"# {current_dir.name}\n\n")

            out.write(f"## {csv_path.name}\n\n")

            with open(csv_path, 'r', encoding='utf-8') as fh:
                reader = csv.reader(fh, delimiter=',')

                try:
                    header = next(reader)

                    header_line = "| " + " | ".join(header) + " |\n"
                    out.write(header_line)

                    separator_line = "|" + "|".join(["---"] * len(header)) + "|\n"
                    out.write(separator_line)

                    for row in reader:
                        if row:
                            data_line = "| " + " | ".join(row) + " |\n"
                            out.write(data_line)

                except StopIteration:
                    # Completely empty CSV (no header row).
                    out.write("*This CSV file is empty.*\n")

            out.write("\n\n")

    size_kb = Path(file_output).stat().st_size / 1024
    dir_names = ", ".join(c.name for c in dirs)
    print(f"Done: {len(csv_files)} files from {len(dirs)} "
          f"directories ({dir_names}) merged into '{file_output}' ({size_kb:.0f} KB).")
    print(f"Skipped {n_excluded} per-request/per-workflow log files "
          f"(pattern '{EXCLUDE_PATTERN.pattern}') -- tens/hundreds of MB "
          f"in total, not informative in aggregate.")


# Entry point: run as `python3 MERAF/tools/merge.py` from the directory containing MERAF/.
output_file = "MERAF/results/results.md"

merge_csv_to_markdown(EXPERIMENTS_ROOT, output_file)

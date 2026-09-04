# Qualitative Terminal Bench metrics

This report measures successful (`reward == 1`) source artifacts associated with
the public Terminal Bench 4.0 leaderboard. Its task manifest is intentionally
hard-coded: each task has reviewed artifact paths, language rules, exclusions,
and—where applicable—packaged baseline paths.

## Build

Install the Harbor CLI, then use a work directory with several gigabytes free.
The analysis command downloads the official task packages and successful trial
archives, installs the pinned AST analyzer under the work directory, and writes
`report.json` plus `data.js` next to the scripts.

```bash
uv tool install harbor
uv run python build_report.py \
  --work-dir ~/tmp/terminal-bench-static-analysis \
  --install-metrics
```

Subsequent builds reuse valid archives. Pass `--refresh` to refetch public Hub
metadata and artifacts, `--extract-only` to stop after extraction, or
`--skip-extract` for an offline rebuild.

`template.html` loads `data.js` locally. Regenerate the single-file Pages artifact
with:

```bash
uv run python inline_report.py \
  --html template.html \
  --data data.js \
  --output index.html
```

GitHub Pages publishes `index.html` through `.github/workflows/pages.yml`.

## Metrics

- SLOC: physical lines containing a non-comment Pygments token.
- Bytes: UTF-8 bytes in non-comment, non-whitespace lexer tokens.
- Lexical tokens: non-comment, non-whitespace source-code tokens counted by
  Pygments. These are not model or API tokens.
- Comment density: comment words per 100 final-side lexical tokens. Docstrings are
  comments; preprocessor directives and hashbangs are code metadata.
- ARI grade: Automated Readability Index over extracted comment prose. Lower is
  easier.
- Flesch ease: Flesch Reading Ease over extracted comment prose. Higher is
  easier; English syllables use a deterministic vowel-group heuristic.
- Cyclomatic and cognitive complexity: root per-file AST metrics from
  `rust-code-analysis-cli` 0.0.25, summed across supported source files.

Greenfield tasks use final scoped source. Repair tasks use added plus deleted
source against the exported task baseline for SLOC/bytes/tokens; comment density
uses final-side changed lines, and complexity uses final touched files.
ARI and Flesch use the same final-side comment scope and require at least 100
English prose words and five punctuated sentences in an artifact. Scores are
averaged only across eligible successful artifacts, with coverage shown beside
the result.

AST complexity is available for C/C++, JavaScript/TypeScript/TSX, Python, and
Rust. It remains null for unsupported languages instead of mixing in a
different estimator. Model rollups average task-relative indices, where the
lowest successful model mean on each task is 100.

Each task also carries Terminal Bench's canonical category, subcategory, and
tags from its exported `task.toml`. The solve map renders category rollups and
individual tasks either as successful attempts or as the percentage-point
difference from the other leaderboard models.

The category fingerprint shows actual solve rate and an adjusted lift. Expected
solve rate is the model's overall rate plus the category cohort rate minus the
overall cohort rate. The lift is actual minus expected, so it separates category
specialization from general model strength and category difficulty. The signal
board lists every model's strongest positive and negative category deviation;
the fingerprint matrix retains all model-category cells.

## Coverage

The 66 tasks are partitioned as follows:

- 27 tasks with at least one successful, directly measurable source artifact;
- 5 source tasks with no successful artifact on this leaderboard;
- 5 repair tasks deferred because they require patch reconstruction or an
  external baseline;
- 29 tasks whose required deliverable is data, a binary/model, or live state
  rather than source code.

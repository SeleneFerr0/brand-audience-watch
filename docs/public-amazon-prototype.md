# Public Amazon prototype (legacy)

> Historical v1 workflow. Use [v2 implementation](v2-implementation.md) for the current CLI, data contracts, rolling evaluation and hierarchical preferences. The legacy choice adapter separately requires pymc-marketing and is not the v2 preference model.

This project uses only explicitly downloaded public Amazon Reviews 2023 category files from McAuley Lab. No JD.com or Tmall connector, path, credentials or records are used. Source data and model artifacts stay out of Git.

## What this prototype measures

- Classification: whether an already reviewed product will receive at least a training-derived number of reviews in the next 90 days.
- Regression: future published review counts, not unit sales or revenue.
- Time series: weekly published review counts with last-value / recent-mean baselines and an optional Chronos-2 comparison.
- Audience descriptions: overlapping keyword-mention cohorts of publicly observed reviewers, not representative market audiences, demographics or verified purchase intent.

`first_observed_review` is not a launch date. Historical features use only reviews strictly before a global cutoff. Training/validation label horizons do not cross into the next split. The review data are retrospective: historical ingestion/revision times and listings with no reviews are unavailable. This is not a zero-history launch benchmark and cannot estimate failure rates over the full product universe.

Snapshot metadata prices, lifetime rating counts/means, helpful votes, and raw user identifiers are excluded from predictive features. A static listing snapshot is not historical price data. Verified-purchase reviews are still not a transaction ledger. Do not infer stock, exposure, demand or sales from absent reviews.

## Install and run

Python 3.12 is the tested interpreter. From this repository:

```sh
uv venv --python 3.12 .venv-baseline
uv pip install --python .venv-baseline/bin/python -e '.[baseline]'
.venv-baseline/bin/brand-intel-legacy download
.venv-baseline/bin/brand-intel-legacy ingest
.venv-baseline/bin/brand-intel-legacy prepare
.venv-baseline/bin/brand-intel-legacy baseline --seconds 60
.venv-baseline/bin/brand-intel-legacy forecast
.venv-baseline/bin/python -m unittest discover -s tests -v
```

The default config is `configs/amazon_subscription_boxes.json`, a small complete category for the first empirical pilot. `configs/amazon_all_beauty.json` is a larger follow-up. Pass `--config` **before** each subcommand. Use a separate `--root` (containing a `configs/` directory) per category to avoid mixing processed data. Category provenance checks reject mismatched processed inputs.

Optional descriptive product lookup: run `brand-intel-legacy download --metadata`, then `brand-intel-legacy catalog`. The snapshot's title, store, reported brand and price are exported for inspection, not historical predictors. Store names are not assumed to be brand names. Missing brands remain missing.

The downloader retrieves a complete category, checks gzip integrity and records SHA-256, size and provenance. It never executes a dataset-provided Python loader. A partially downloaded file is not an input dataset. The byte cap is 250 MB. Official source: https://amazon-reviews-2023.github.io/ . The inspected card did not specify an explicit dataset license; confirm upstream terms before redistribution or commercial use. Code and data licenses are different.

## Optional forecasting model

```sh
uv venv --python 3.12 .venv-forecast
uv pip install --python .venv-forecast/bin/python -e '.[forecast]'
HF_HOME=.cache/huggingface .venv-forecast/bin/brand-intel-legacy forecast --chronos
```

This downloads model weights. The adapter fixes cross-learning off and uses a CPU model for portability. The item set is selected by past activity only. Its 52-week history includes zeros for weeks with no observed review events; those zeros mean no review events, not no sales. Forecast MAE and quantile interval metrics concern review counts only. Pretrained model data contamination must be considered when interpreting historical comparisons.

## Choice / price model

Amazon reviews do not identify the alternatives offered, their prices or the option to buy nothing. Therefore this pipeline never fabricates choice experiments from review histories and cannot validate unlaunched concept-price preferences on these data.

An experimental MNLogit adapter is included for future genuine observed choice studies:

```sh
uv venv --python 3.12 .venv-choice
uv pip install --python .venv-choice/bin/python -e '.[choice]' pymc-marketing
.venv-choice/bin/brand-intel-legacy validate-choices --input choices.csv
.venv-choice/bin/brand-intel-legacy fit-choice --input choices.csv
```

Required long-table columns: `respondent_id,choice_set_id,alternative_id,price,attribute_score,chosen,is_outside_option,data_origin`. Each task needs at least two fixed-ID alternatives, exactly one chosen option and one consistent outside option. Outside attributes and price are zero-coded; `data_origin` must be `observed_choice_experiment`. This marker is a declared provenance assertion, not proof the study is genuine. The initial adapter supports one numeric attribute; additional attributes, pricing design, preference identification and population calibration require deliberate study design. Choice fitting is not part of the Amazon empirical benchmark and remains experimental until tested with a real study.

## Artifacts

- `data/raw/source-manifest.json`: source URL, content hash and data provenance.
- `data/processed/data-quality.json`: input quality and observed coverage.
- `data/processed/task-manifest.json`: cutoffs, eligibility, features and train-only hit threshold.
- `data/processed/{train,validation,test}.csv`: reproducible temporal model tables.
- `data/processed/weekly_reviews.csv`: shared temporal comparison data.
- `data/processed/reviewer_profiles.csv`: past-only derived reviewer profiles.
- `artifacts/baseline/metrics.json`: AutoGluon, prevalence and persistence comparisons.
- `artifacts/forecast-naive/metrics.json`: temporal naive forecasting results.
- `artifacts/chronos/metrics.json`: optional Chronos results, only after actual execution.

Keep AutoGluon/LightGBM, Chronos/PyTorch and PyMC in separate environments. The combined macOS environment failed while rerunning LightGBM after PyTorch installation; backend isolation is the supported setup for this pilot. Dependencies can be frozen with `uv pip freeze --python .venv-baseline/bin/python > requirements-baseline.lock.txt`. Do not load model artifacts from untrusted sources.

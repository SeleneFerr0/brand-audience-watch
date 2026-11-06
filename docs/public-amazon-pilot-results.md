> Historical v1 results. See [v2 validation](v2-validation-results.md) for the current same-task rolling benchmark. Target periods differ; do not directly compare MAEs.

# Public Amazon pilot: implementation and results

## Scope

Implemented in the independent `brand-audience-intelligence` repository using only public Amazon data. JD.com and Tmall confidential records were neither accessed nor used. This pilot measures future **published review activity**, not sales, revenue, consumer demand, ad incrementality or the success of unlaunched concepts.

Source: [McAuley Lab Amazon Reviews 2023](https://amazon-reviews-2023.github.io/). The complete Subscription Boxes category was used because the larger All Beauty download was too slow. A larger-category configuration is included for later experiments.

## Implemented

- Bounded public-data download, gzip validation on download, SHA-256 provenance and explicit category checks.
- Streaming SQLite ingestion and exact-fingerprint deduplication.
- Past-only activity, rating and mention features; global temporal splits with separated label horizons.
- AutoGluon/LightGBM classification and count-regression baselines, plus prevalence and persistence comparisons.
- Weekly time-series export and naive forecasts; optional Chronos-2 adapter.
- Descriptive product/store/brand lookup and overlapping reviewer mention cohorts.
- Choice-experiment schema validation and an experimental PyMC-Marketing MNLogit adapter. No fake choice outcomes, offered prices, exposure logs or demographics are derived from Amazon reviews.
- An isolated baseline environment, dependency lock and nine passing invariant tests.

## Data

| Measure | Value |
| --- | ---: |
| Raw valid review rows | 16,216 |
| Duplicate fingerprints removed | 200 |
| Reviews used | 16,016 |
| Products | 641 |
| Training snapshots | 661 |
| Validation snapshots | 248 |
| Test snapshots | 257 |
| High-activity test products | 2 |

Training cutoffs: 2020-01, 2020-07, 2021-01, 2021-07. Validation cutoff: 2022-01-01. Test cutoff: 2023-01-01. Target horizon: 90 days. High activity means at least 12 future reviews, using a threshold determined from training outcomes only. Eligibility requires previous reviews, so this is not a zero-history launch evaluation.

## Results

| Metric | AutoGluon | Persistence baseline |
| --- | ---: | ---: |
| 90-day review-count MAE, lower is better | 0.8565 | 0.9416 |
| High-activity average precision, higher is better | 0.3088 | 0.4167 |
| Precision@100 | 0.0200 | 0.0200 |

AutoGluon has lower count error but does not beat persistence on ranking in this pilot. There are only two positive test products, and the positive rate changes materially between splits; these results cannot establish reliable hit-product performance.

The separate four-week forecasting test uses 100 product series. Last-value MAE: 0.2250; recent-four-week-mean MAE: 0.1888.

## Execution status

- AutoGluon classification/regression: executed successfully in the isolated baseline environment.
- Naive weekly forecasts: executed successfully.
- Tests: 9 passed, covering future-feature leakage, half-open label windows, deduplication, invalid inputs, category consistency and choice-data constraints.
- Chronos: dependencies installed; model retrieval/inference exceeded the 150-second pilot budget. No Chronos accuracy metrics were generated.
- PyMC choice fitting: not executed; the public data lacks genuine choice alternatives and offered prices. The fitting adapter remains experimental.

The combined macOS environment failed during a LightGBM rerun after PyTorch installation. AutoGluon was rerun successfully without PyTorch in `.venv-baseline`; optional forecasting and choice environments are documented separately. The earlier successful run and incomplete run are archived under ignored `runs/`.

## Reproduce

See `docs/public-amazon-prototype.md` in the repository for setup and CLI commands. `requirements-baseline.lock.txt` pins the validated baseline environment. Existing model outputs are protected from accidental overwrite; archive them explicitly or use a fresh root for a new run.

## Data boundaries

- Review count is not unit sales. Verified purchase does not make these data a transaction ledger.
- First observed review is not launch date; products with no reviews are absent.
- Snapshot metadata prices and lifetime rating statistics are not historical features.
- Reviewer mention cohorts are not representative market segments or stated positive preferences.
- Historical ingestion/edit times are unavailable; only retrospective publication timestamps are observed.
- The inspected source card did not specify an explicit dataset license. Upstream terms require review before redistribution or commercial use.

The next empirical step is a larger complete category with more positive test products and rolling backtests. Actual concept-price validation still requires a separate public observed-choice dataset or a purpose-built study.

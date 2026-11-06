# Brand Audience Watch

A research platform for understanding product trends, evaluating new product concepts, and modeling consumer preferences. It helps brand and product teams compare forecasting methods using traceable data and reproducible evaluations.

**Current release: 0.2.0 · Python 3.12+**

The current benchmark uses public Amazon Reviews data. Its forecasting target is published review activity; predicting unit sales requires suitable transaction data.

## Capabilities

- **Flexible data integration:** source adapters, configurable column mappings, and shared event and product schemas support additional datasets.
- **Separate research tasks:** trend forecasting for products with observed histories, and cold-start evaluation for new products with attributes available at the prediction date.
- **Probabilistic forecasting:** compare Poisson, Negative Binomial, two-stage hurdle, and quantile models against zero, last-week, and recent-average baselines.
- **Text and product representations:** compare numeric features with word and character TF-IDF, while retaining review text, product attributes, and evidence references.
- **Consumer preference modeling:** hierarchical random coefficients, validation on entirely held-out consumers, and sampling convergence diagnostics.
- **Reproducible experiments:** versioned data builds, configuration and content hashes, rolling backtests, code snapshots, and explicit run status.

## Validation status

The current implementation passes **34 regression and integration tests**. The public Amazon benchmark includes **four rolling folds and 60 method–fold evaluations**.

The cold-start workflow and hierarchical preference model have been validated with synthetic fixtures. Real-world evaluation requires historical product catalog versions and observed consumer choice experiments. Convergence diagnostics and synthetic results establish implementation checks, rather than evidence of market performance.

A Chronos-2 adapter uses the same weekly forecasting task as the other models. Model weights were unavailable during the benchmark, so Chronos results are pending.

## Quick start

From the repository directory, install the core package in a dedicated environment:

```sh
uv venv --python 3.12 .venv-baseline
uv pip install --python .venv-baseline/bin/python -e .
```

Download the public Amazon Subscription Boxes reviews and product metadata:

```sh
.venv-baseline/bin/brand-intel-legacy download
.venv-baseline/bin/brand-intel-legacy download --metadata
```

Build the versioned dataset, run the benchmark, and check the test suite:

```sh
.venv-baseline/bin/brand-intel data-build --spec configs/public_trend_v2.json
.venv-baseline/bin/brand-intel backtest --spec configs/public_trend_v2.json
.venv-baseline/bin/python -B -m unittest discover -s tests -v
```

Paths are relative to the current directory. To use a different project directory, pass `--root /path/to/project` before the subcommand.

Each experiment creates a separate `runs/v2/<run-id>/` directory. Check `state.json` before interpreting its scores: unavailable backends and insufficient evidence are recorded explicitly.

## Bring your own data

Start with the [data source configuration template](configs/canonical_template_v2.json) and the CSV headers for [events](data_templates/events_v2.csv), [product versions](data_templates/products_v2.csv), and [consumer choices](data_templates/choices_v2.csv).

Event and product records include timestamps for when information became available, along with evidence references. These fields allow historical evaluations to use only information known at each prediction date. Configure the target unit, observation coverage, product attributes, and evaluation periods for your dataset.

Consumer preference studies use the [hierarchical preference configuration](configs/hierarchical_preferences.json). Choice observations must identify the alternatives offered, the selected option, and an outside option. Sales forecasting for new concepts also requires appropriate sales, exposure, price, and availability data.

## Optional models and environments

The core installation does not require AutoGluon, PyMC, or PyTorch. Install optional components in separate environments:

| Component | Installation extra | Purpose |
|---|---|---|
| AutoGluon | `.[baseline]` | Original historical baseline through the legacy CLI |
| PyMC / NumPyro | `.[choice]` | Hierarchical consumer preferences |
| Chronos / PyTorch | `.[forecast]` | Optional time-series comparison |

Keep numerical benchmarks, preference sampling, and Chronos in separate virtual environments to avoid native-library conflicts. Validated environment snapshots are available in [requirements-baseline.lock.txt](requirements-baseline.lock.txt) and [requirements-choice.lock.txt](requirements-choice.lock.txt).

The primary CLI is `brand-intel`. The original AutoGluon workflow remains available through `brand-intel-legacy`. Its target periods differ from the current weekly benchmark, so their MAE scores are not directly comparable.

## Research documentation

The detailed research documents below are currently in Chinese:

- [Implementation guide, data contracts, and model definitions](docs/v2-implementation.md)
- [Benchmark results and current limitations](docs/v2-validation-results.md)
- [Architecture review and improvement plan](docs/architecture-review-and-improvement-plan.md)
- [Product opportunity research and repository shortlist](docs/hit-product-best-practices-and-top-repositories.md)
- [Industry research](docs/industry-research.md)

This release provides research and evaluation modules. Validating demand for a product that has not launched requires suitable study design, representative consumer evidence, and external market validation.

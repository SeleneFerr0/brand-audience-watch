# Brand Audience Intelligence

面向品牌的人群偏好、产品机会与新品需求研究平台。本仓库独立安装和运行，只使用公开 Amazon 数据进行实测；不接入 JD.com / Tmall 机密数据。

当前主入口为 v0.2：可更换数据源、趋势与冷启动任务、滚动回测、概率模型比较、分层消费者偏好。Amazon 评论数量是评论活动指标，不代表销量。

## 开始运行

```sh
uv venv --python 3.12 .venv-baseline
uv pip install --python .venv-baseline/bin/python -e .
# 已有项目公开数据：data/raw/Subscription_Boxes.jsonl.gz 和 meta_Subscription_Boxes.jsonl.gz
.venv-baseline/bin/brand-intel data-build --spec configs/public_trend_v2.json
.venv-baseline/bin/brand-intel backtest --spec configs/public_trend_v2.json
.venv-baseline/bin/python -B -m unittest discover -s tests -v
```

首次获取该公开类别的数据，可使用保留的下载入口：

```sh
.venv-baseline/bin/brand-intel-legacy download
.venv-baseline/bin/brand-intel-legacy download --metadata
```

默认数据目录及输出目录相对于当前目录。也可在子命令前传 `--root /path/to/project`。每次执行生成独立的 `runs/v2/<run-id>/`，请先检查 `state.json`，再读取成绩。未安装或未下载的 Chronos 会显示为 unavailable，不会生成替代成绩。

## 文档与接口

- [v2 使用说明、数据契约与模型定义](docs/v2-implementation.md)
- [v2 实测结果与剩余限制](docs/v2-validation-results.md)
- 数据源模板：[canonical_template_v2.json](configs/canonical_template_v2.json)
- 选择实验配置：[hierarchical_preferences.json](configs/hierarchical_preferences.json)
- CSV 表头模板：[events](data_templates/events_v2.csv)、[products](data_templates/products_v2.csv)、[choices](data_templates/choices_v2.csv)
- 原架构评审：[改进方案](docs/architecture-review-and-improvement-plan.md)
- 前期调研：[爆品预测实践与优先仓库](docs/hit-product-best-practices-and-top-repositories.md)、[业界实践](docs/industry-research.md)

AutoGluon 的原历史基线保留在 `brand-intel-legacy`，旧报告和旧 CLI 是历史记录。v2 的模型比较采用相同周级目标、候选产品、预测时点与预测长度，不能直接与旧的整段计数 MAE 比大小。

## 运行环境

数值回测、PyMC 偏好和 Chronos 分别使用虚拟环境，避免混用 LightGBM/PyTorch 原生库。验证过的环境见 `requirements-baseline.lock.txt` 与 `requirements-choice.lock.txt`。主安装不要求 AutoGluon、PyMC 或 Torch；分别通过 `.[baseline]`、`.[choice]`、`.[forecast]` 安装。

本版本实现研究评估模块。真实新品的销售预测仍需要有历史版本的目录、销售与曝光记录以及真实选择实验；采样收敛或合成数据结果不能替代这些证据。

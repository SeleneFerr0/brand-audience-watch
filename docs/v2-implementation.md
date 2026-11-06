# v2 使用说明与实现边界

本次实现聚焦 brand-audience intelligence；不修改独立的个人电影/音乐推荐项目。主入口 `brand-intel` 为 v2，旧流程入口 `brand-intel-legacy` 保留。

## 1. 可替换的数据层

流程为 `source adapter → immutable canonical build → task frames → rolling folds → representations → models → scores`。只有 adapter 解释源数据格式，任务及模型不依赖 Amazon 字段名。当前支持 Amazon Reviews JSONL(.gz) 与 canonical CSV；新数据源可先映射为 CSV，格式差异较大时增加 adapter。

`configs/canonical_template_v2.json` 是需要填写的模板，不是虚构的真实数据。复制后修改路径、数据 ID、观测截止时间、目标单位和切分日期。`event_columns` / `product_columns` 的映射方向为“标准列名 → 源列名”；提供映射时，应包含所有需要保留的列。

```json
{
  "event_columns": {
    "event_id": "order_line_id", "item_id": "sku",
    "event_time": "ordered_at", "available_at": "ingested_at",
    "count": "quantity", "evidence_uri": "source_record_uri",
    "text": "review_body", "attributes": "context_json"
  }
}
```

### 事件表

| 列 | 类型与用途 |
|---|---|
| `event_id`, `item_id` | 非空字符串；事件唯一键、产品键 |
| `event_time` | UTC ISO 时间，或毫秒时间戳；事件发生时间 |
| `available_at` | 系统实际可使用该事件的时间，不能早于发生时间 |
| `count` | 非负整数计数；目标单位由 source.target_unit 显式指定 |
| `evidence_uri` | 能定位原始记录的引用；必须提供 |
| `entity_id` | 可选，匿名消费者/主体 ID；不作为人口属性推断依据 |
| `rating`, `text` | 可选评分和完整正文；原始正文保存在 canonical 数据库 |
| `attributes` | 可选 JSON 对象，例如促销、库存、渠道；保留为扩展上下文 |

`target_unit` 当前支持 `published_reviews`、`units_sold`、`events`。不能把评论数改名为销量。取消、退货、曝光和负销售量应先按清楚的业务定义形成非负目标表；本版不是净销量回归器。

### 产品版本表

| 列 | 类型与用途 |
|---|---|
| `item_id` | 产品键，可有多个历史版本 |
| `available_at` | 该版本信息首次可供模型使用的时间 |
| `effective_at` | 该版本业务生效时间，缺省为 available_at |
| `title`, `description` | 产品/概念文本 |
| `attributes` | JSON 对象，例如 price、size、material、flavor |
| `evidence_uri` | 对应目录版本、概念卡或其他真实记录的引用 |

特征只取预测时点前“已生效且已可知”的最后版本。数值特征通过 `task.numeric_attributes` 配置，类别属性随已知产品文本进入 TF-IDF。`task.dynamic_numeric_attributes` 从该产品预测时点前已可用的最近 100 条事件中向后提取最近可见值；缺失保持缺失，训练折内填补，不假定库存或促销为零。更长、稀疏的外部状态历史建议导出为产品版本表。

Amazon 目录是无历史发布时间的快照：数据库保留 title/description/attributes/evidence，但其 historical availability 设为未知，因此不会进入过去的特征，也不能支持真实的历史冷启动实验。

`coverage_end` 是排他的观测边界，必须确认来源在此之前覆盖完整。当前未提供逐产品曝光/上架观测掩码；没有事件会按计数零处理。因此只应把完整覆盖的产品/时段纳入任务。不能把缺失日志或缺货当作真实的零需求。以后有更全面的数据时，应先加入可观测性/在售状态约束，再评估销量。

### 构建与追溯

```sh
.venv-baseline/bin/brand-intel data-build --spec configs/my_source.json
.venv-baseline/bin/brand-intel backtest --spec configs/my_source.json
```

构建身份包括原文件 SHA256、source 配置、schema 版本及 normalizer 代码 SHA256。路径负责定位，内容哈希负责身份。同一内容重用构建；原数据或 adapter 改变会要求新构建。产物位于 `data/builds/<id>/canonical.sqlite`，完成后按只读方式打开，并验证数据库哈希。

重复事件完全一致时去重；同 ID 内容冲突默认报错。禁止执行远程数据加载代码。原始输入不被修改。

## 2. 时间任务、补零与滚动验证

`task.kind` 明确分为：

- `trend`：过去已有足够观测、过去一年仍活跃的产品，用历史活动及可知文本/属性预测后续活动。
- `cold_start`：预测时点已存在可知产品版本、但没有可知历史事件的产品，用概念文本/属性预测。同一份数据不满足条件时返回 insufficient_evidence，不退回趋势任务。

每个样本键为 `(item_id, cutoff, lead_week)`，标签为对应未来一周计数。`horizon_weeks=4` 得到第 1–4 周四行，而不是把 4 周总量与周级时序预测混比。所有切分使用 UTC 周一，特征窗口 `[start, cutoff)`，标签窗口 `[target_start, target_end)`。产品上限 `max_items` 只依据历史活动筛选。

每个测试时点重新构建训练数据：仅使用标签区间已经结束的训练 cutoff；训练标签也只纳入测试时点前已到达的记录。测试标签按声明的 observation coverage 读取。TF-IDF、SVD、填补、缩放和爆品阈值全部在训练折拟合。后续折可使用此前已观测的测试期间数据，这是扩展窗口的时间验证；当前各折是开发回测，不是未触碰的最终测试集。

强制基线包括零预测、上一周计数、最近四周均值。稀疏数据中零预测可以赢得 MAE，必须保留。精度@K 对同分边界使用随机排列的期望精度，防止常数预测利用产品排序获得虚高成绩。分位数、排名、区间覆盖率和概率分数分别报告，不从单个 MAE 判定爆品能力。

## 3. 文本与证据

先比较 `numeric`、`word_tfidf`、`char_tfidf`。文本使用时点前已知产品名称、描述、属性，以及过去 28 天最近最多 100 条可用评论；默认截取 8,000 字符。数据库保留未截断正文，任务表同时保留使用过的文本和直接证据引用。训练折 TF-IDF 最多 1,500 词/字符 n-gram，经 SVD 最多 12 维，与数值特征拼接。测试词不会用于建词表。

缺少可训练文本时明确标为 `unsupported_missing_text`，不能用数值模型的成绩冒充文本比较。`feature_evidence` 为文本、产品和动态属性的直接来源；历史统计可结合 prepared.json 的 data_id、canonical.sqlite、item_id、cutoff 及代码快照复核。`label_evidence` 单独保存目标记录引用，不能输入表示层。

## 4. 概率模型与 Chronos 对照

| 模型 | 实现 | 比较注意事项 |
|---|---|---|
| Poisson | 正则化 log-link GLM | 方差等于均值；易低估过度离散 |
| Negative Binomial | NB2 log-link；联合估计回归系数与 dispersion，带正则 | 方差为 μ+αμ²；比 Poisson 更灵活 |
| 两阶段 hurdle | Logistic 预测 P(Y>0)，正计数拟合零截断 Poisson | 正分布归一化；明确区分是否发生和发生多少 |
| Quantile | 三个 Gradient Boosting quantile 模型，τ=.1/.5/.9 | 负值截零、逐行重排防交叉；不伪造完整分布 |
| Chronos-2 | 独立 Python 子进程，52 周同产品历史，预测相同未来周 | 固定权重 revision；不使用未知未来协变量；无历史新品不适用 |

参数模型同时输出均值、分位数、训练阈值以上的概率和预测对数密度。分位数模型与 Chronos 以中位数作为 point。比较 MAE 应用共同的中位数列；分位数误差用平均 pinball。负对数似然及 Brier 只对原生支持对应概率的模型计算，缺失用 null 表示。

Chronos 的适配器已经接入同一份 test.csv、cutoff、候选产品、周目标和预测长度，并验证任务/文件哈希。当前公共实测由于权重未下载成功，Chronos 没有成绩。运行时需要的设置：

```json
{
  "enabled": true,
  "python": ".venv-forecast/bin/python",
  "model_id": "amazon/chronos-2",
  "revision": "29ec3766d36d6f73f0696f85560a422f50e8498c",
  "cache_dir": ".cache/huggingface/hub",
  "local_files_only": true,
  "timeout_seconds": 150
}
```

在独立 `.[forecast]` 环境中，用 huggingface_hub 的 `snapshot_download(repo_id=..., revision=..., cache_dir=...)` 显式下载，再运行回测。缓存应包含相同 SHA snapshot 的 config.json 和 model.safetensors。本地现有配置指向已安装 Chronos 的 `.venv/bin/python`；新环境应按实际路径修改。权重固定不意味着消除了时序基础模型预训练数据重叠，完成对照后仍需审核。

## 5. 分层偏好与消费者留出

采用 PyMC 直接表达可变候选集的 mixed logit：

`β_i = μ + σ ⊙ z_i`，`z_i ~ Normal(0,I)`，`P(choice=j) = softmax(x_ij · β_i)`。

这是消费者随机系数的分层偏好模型，属性数通过配置变更。使用非中心化参数化；μ 的先验 Normal(0,1.5)，σ 的先验 HalfNormal(1)。本版属性系数条件独立、没有品牌/地区第二级分层、价格符号约束或市场规模扩散模型。直接 PyMC 实现便于支持变化的备选集；原 PyMC-Marketing 包装入口保留在 legacy 流程。

选择数据为 long table，每行一个备选方案：

`respondent_id, choice_set_id, alternative_id, <configured attributes>, chosen, is_outside_option, data_origin, evidence_uri`。

每个消费者任务至少两个备选、恰好一个被选、恰好一个 outside option；outside 属性全部零编码。价格非负；如提供 currency 必须统一。属性对比必须满秩。来源必须是 `observed_choice_experiment`；只有显式 `--allow-synthetic-validation` 才接受 `synthetic_fixture`，且状态和成绩标明仅验证流程。评论不能自动改造成选择实验。

消费者整体划分训练与留出；训练消费者的任何任务都不会属于留出消费者。尺度仅从训练计算。对新消费者的预测从已学总体后验积分，不用其留出选择拟合个人参数。输出 held-out NLL、消费者宏平均 NLL、准确率和均匀选择基线。它评估新消费者总体泛化，不是已知消费者个性化，也不是销量验证。

```sh
uv venv --python 3.12 .venv-choice
uv pip install --python .venv-choice/bin/python -e '.[choice]'
.venv-choice/bin/brand-intel preferences \
  --spec configs/hierarchical_preferences.json --input data/public/choices.csv
```

在没有本地 C++ toolchain 的本机可使用配置内的 NumPyro NUTS，并设置 `PYTENSOR_FLAGS=cxx=`。运行时缓存目录可通过环境变量指向项目 `.cache/`。合成验证单独运行：

```sh
.venv-choice/bin/brand-intel synthetic-choice-fixture --output data/synthetic/choices.csv
PYTENSOR_FLAGS=cxx= .venv-choice/bin/brand-intel preferences \
  --spec configs/hierarchical_preferences.json --input data/synthetic/choices.csv \
  --allow-synthetic-validation
```

默认四链、1,000 tune+1,000 posterior draws、target_accept=.95。诊断覆盖总体参数及所有消费者 offset，使用未四舍五入的统计：最大 R-hat≤1.01，最小 bulk/tail ESS≥400，divergence=0。诊断失败保留 posterior 与结果，但状态为 diagnostics_failed，不宣布成功。采样通过后还需要真实数据的后验预测检查、设计有效性、代表性与外部需求验证。

## 6. 产物与运行状态

`runs/v2/<id>/state.json` 保存完整配置、任务指纹、数据 ID、代码哈希、Python/库版本和时间；`code/` 保存执行源码。异常记录 failed 和 traceback。缺少真实能力为 insufficient_evidence；必需模型失败为 partial_required_model_failures；可选 Chronos 不可用为 completed_with_unavailable_optional_backend。运行进程被操作系统强制杀死时 finally 无法执行，遗留 running 应结合进程状态与时间人工核对。

每折包含 train/test CSV、prepared.json、表示词表哈希、model statuses、总体/逐 lead 的分数及预测。配置、任务实现或 CSV 内容变更后，独立读取器拒绝复用旧任务表。修改预测期必须重建任务，不能复用旧的 4 周结果当 8 周结果。

本次没有自动提交、推送或发布。原模型与历史结果保留；执行新实验生成新运行目录。

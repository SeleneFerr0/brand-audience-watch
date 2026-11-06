# 爆品预测实践与三个优先仓库

调研日期：2026-11-05。范围仅限本项目。本次完成官方资料、源码、测试目录、发布记录与许可检查；未安装依赖、执行第三方测试或开展业务数据基准实验。“优先三个”是项目适配判断，不是全球精度排行榜。

## 结论

优先深入研究 PyMC-Marketing、AutoGluon、Chronos。前者支持消费者选择与扩散；AutoGluon 建立表格和时间序列的统一强基线；Chronos 提供可深入研究的预训练时序模型。AutoGluon 已包装 Chronos，第一版无需重复部署两个预测服务。品牌广告增量优先备选 EconML；专注自定义神经时序模型时备选 NeuralForecast。

没有发现经公开充分验证、可以直接从任意产品概念预测未来爆款的完整开源系统。全新概念需要属性化、消费者证据、可比历史和前瞻验证；代码框架不能代替这些数据。

## 方法家族与适用边界

| 路线 | 典型方法 | 优点 | 缺点与边界 |
| --- | --- | --- | --- |
| 已上市趋势预测 | 滞后/增长特征、树模型、全局时序模型、基础预测模型 | 可用搜索、销售、讨论等信号捕捉增长及持续性 | 信号可能滞后；促销、铺货、缺货与刷量会扭曲表面需求 |
| 未上市新品迁移 | 属性匹配、类似品、层级模型、预训练表示 | 零或少量新品历史也可借助相关产品 | 类比错误与跨品类分布变化；不能外推到完全未知属性而不增加不确定性 |
| 消费者选择/联合分析 | MNL、Mixed Logit、潜在偏好分群 | 直接评估属性、价格、竞争替代及受众差异 | 需要真实选择数据或设计良好的概念实验；意向不等于销量；MNL 的 IIA 假设有限制 |
| 扩散与生命周期 | Bass、层级贝叶斯扩散 | 描述采用规模、外部影响与口碑传播 | 初期参数难识别；标准 Bass 不是复购销量模型，不能自动处理反复流行与竞争突变 |
| 因果与增量 | 随机实验、uplift、DML/因果森林 | 区分自然需求与营销带来的改变 | 需要实验或可辩护的识别假设；增加预测变量不能自动消除混杂 |
| 多模态与知识表示 | 文本、图像、属性、需求/替代图 | 对尚无 ID 和交互的概念构造表示 | 语义相似不等于需求或支付意愿；LLM 易生成看似合理的解释 |
| 主动验证与探索 | 概念测试、价格实验、原型/试用、受控探索 | 把新概念从猜想转成可更新的证据 | 耗费时间、样本与预算；必须考虑样本代表性和竞争环境 |

Amazon TrendSpotter 属于趋势预测；AWS 新品指南支持利用类似产品的完整生命周期与属性。Amazon BFS 支持用替代关系缓解搜索冷启动，但其搜索排序结果不能当作销量预测结果。Ipsos 公开方法把概念、价格、竞争选择、试用与复购纳入验证。

来源：[TrendSpotter](https://www.amazon.science/publications/trendspotter-forecasting-e-commerce-product-trends)、[AWS 新品实践](https://docs.aws.amazon.com/prescriptive-guidance/latest/forecast-demand-new-product/best-practices.html)、[BFS](https://www.amazon.science/publications/behavioral-feature-boosting-via-substitute-relationships-for-e-commerce-search)、[Ipsos InnoCPT](https://www.ipsos.digital/inno-cpt)。

## 三个优先仓库

### 1. PyMC-Marketing：概念、人群与扩散

仓库：https://github.com/pymc-labs/pymc-marketing

核验发布：1.3.0，2026-10-22；许可证 Apache-2.0。对 1.3.0 的消费者选择、Bass 源文件及对应测试进行了静态阅读。

重点入口：

- `pymc_marketing/customer_choice/mnl_logit.py`：多项 Logit，属性效用通过 softmax 转成竞争选择概率。
- `pymc_marketing/customer_choice/mixed_logit.py`：随机系数捕捉个体/群体偏好异质性。
- `pymc_marketing/bass/model.py`：贝叶斯采用扩散，支持市场规模、创新与模仿参数。
- `tests/customer_choice/test_mixed_logit.py`、`tests/bass/test_model.py`：接口、模型和模拟数据相关测试。

选择模型的简化表达：P(j|u,C)=exp(V_uj)/sum_k exp(V_uk)，Mixed Logit 再对随机偏好系数积分。C 必须反映竞争选择，必要时明确“不购买”选项。选择份额不是自动校准的总体销量。

Bass 简化表达：dN/dt=(p+q*N/m)*(m-N)。m 为潜在采用人数，p 为外部影响，q 为模仿影响。没有新品历史时需要可比产品先验和敏感性分析；不能凭一条拟合曲线确认未来爆款。

优势：最贴近未上市概念、价格与人群差异；可以表达先验与后验不确定性；还能接 CLV 等复购分析。局限：需要统计建模、选择实验与采样诊断；计算成本可能较高；不能把 MMM 的市场级效果直接转成个人画像。

### 2. AutoGluon：统一实验与强基线

仓库：https://github.com/autogluon/autogluon

核验发布：v1.6.3，2026-09-18；许可证 Apache-2.0。源码快照记录在 companion JSON。

重点入口：

- `tabular/src/autogluon/tabular/predictor/predictor.py`：商品/人群特征上的监督学习入口；本次以仓库说明核对能力，未逐行审阅此模块。
- `timeseries/src/autogluon/timeseries/predictor.py`：预测、评估、模型比较与协变量接口。
- `timeseries/src/autogluon/timeseries/splitter.py`：ExpandingWindowSplitter。
- `timeseries/src/autogluon/timeseries/models/chronos/`：Chronos 集成。
- `timeseries/tests/unittests/`：预测器、切分、指标与模型测试目录。

优势：比较传统统计、树模型、深度模型与集成；可建立爆品分类与概率需求预测两套任务；减少只与弱基线比较的问题。局限：自动化不保证无泄漏、不修复错标签，也不提供因果识别；集成可能增加成本。源码过滤过短训练序列，必须记录哪些新品被排除，不能将普通时序评估称作零历史新品评估。

### 3. Chronos：时序基础模型研究

仓库：https://github.com/amazon-science/chronos-forecasting

核验发布：v2.3.2，2026-09-08；许可证 Apache-2.0。源码快照记录在 companion JSON。

重点入口：

- `src/chronos/chronos2/pipeline.py`：推理、分位数、协变量、微调及跨序列学习。
- `test/test_chronos2.py`：数值/类别协变量、预测输入和模型加载等测试。
- `notebooks/chronos-2-quickstart.ipynb`：官方 README 指向的示例。

优势：快速得到少量领域训练条件下的预测基线；Chronos-2 支持多变量、协变量与微调。局限：zero-shot 指不需要针对目标任务训练，不代表无需历史上下文或外部证据；时序分位数不自动等于爆品成功概率，也不保证外推校准。源码说明 cross_learning 不总提高效果，结果依赖批次组成/大小，实验必须固定并记录分组。

官方集成文档：https://auto.gluon.ai/stable/tutorials/timeseries/forecasting-chronos.html

## 比较过的其他候选

| 仓库 | 判断 |
| --- | --- |
| [EconML](https://github.com/py-why/EconML) | 广告干预与人群增量的优先候选；当有实验数据时可能优先于直接使用 Chronos。不是未知产品需求预测器。 |
| [NeuralForecast](https://github.com/Nixtla/neuralforecast) | 需要深入比较/修改 NHITS、TFT 等架构时很有价值；当前先由 AutoGluon 建立统一基线。 |
| [RecBole](https://github.com/RUCAIBox/RecBole) | 强项是用户—物品推荐实验；历史交互排名不能直接回答未上市概念需求和总体市场潜力。 |

这些是互补工具，不是将不同任务的精度放在一起评分。筛选依据为任务适配、实际代码与测试、实验接口、维护与发布、许可、可扩展性，不采用 star 数量作为效果证据。

## 第一轮实验协议

1. 先定义市场、品类、90 天等期限与“爆”的阈值；同时保留销量、毛利、试用、复购等不同目标。
2. 建立完整上市队列，包含失败、退市与低销量商品；新品属性、竞争、价格和供给按当时版本保存。
3. 将零历史、上市 7/14/30 天分别回测；用历史发布时间滚动切分，并额外留出品牌/概念家族验证外推。
4. 静态监督学习保留一个真正的未来测试集，不依赖 AutoML 的默认随机验证。所有外部搜索、评论、价格和 embedding 只使用当时可得的信息；检查基础模型预训练污染。
5. 分类比较 PR-AUC、Precision@K、Brier 与校准；数量预测比较分位数损失、区间覆盖与成本。平均误差好不等于尾部爆品识别好。
6. 品牌受众同时看需求/偏好和干预增量，预算分配以可验证的净增价值衡量。随机营销数据可参考 [Criteo Uplift](https://ailab.criteo.com/criteo-uplift-prediction-dataset/)，注意其版本与数据许可。
7. 全新概念先做竞争与价格明确的真实人群选择实验，再对试用、复购与销量校准。LLM 可生成和结构化概念，模拟消费者不能自行证明市场需求。
8. 输出证据来源、预测区间、关键情景和下一项最值得做的验证；供给、价格、营销和竞争变化造成的差异应与模型误差区分。

## 推荐实现次序

先用 AutoGluon 建可复现的历史发布基线；并行设计消费者选择数据格式，以 PyMC-Marketing 验证概念—人群匹配、价格与替代。Chronos 通过 AutoGluon 接入作为对照，只有研究特定时序机制时才直接使用原库。随后用真实实验数据引入增量建模。

本次未实施训练或给三个仓库做实测性能排序。

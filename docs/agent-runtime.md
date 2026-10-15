# Agent 执行与评测

更新日期：2026-10-15。

## 实际执行链路

| 链路 | 入口 | 执行与降级 |
| --- | --- | --- |
| 搜索 | `api/routes/search.py` | 显式会话数据 → 新建意图 Agent → SearchRecipe 计划 → 多源召回、过滤、精排 → 结果缓存；失败保留阶段和降级信息 |
| 阅读 | `services/reader/paper_reader_service.py` | 当前论文及客户端历史 → 独立阅读执行实例和工具回调 → 证据筛选后的卡片；历史按论文写入数据库 |
| 保存分类 | `agents/paper_analysis_agent.py` | 每步独立 Agent；分类体系按数据目录缓存并保存到 `memory/taxonomy-v1.json`；JSON 重试会重新解析 |
| 每日推荐 | `services/daily/daily_service.py` | 库内兴趣、偏好和反馈 → 外部候选 → 去重和选择 → 主题标签；策略说明直接生成 |
| 反馈 | `record_user_daily_feedback` | 先保存基础反馈、推进缓存版本；负偏好摘要后台执行，失败保留基础反馈 |
| 图谱 | `services/graph/kg_relations.py` | 分类、历史及引用候选 → 分批独立 Agent → 枚举、分数和证据校验 → SQLite 关系及出处 |

会话上下文来自 API 输入，框架不替业务长期保存对话。搜索仅在追问有指代或补充约束时使用最近 8 轮历史；新话题、替换和清空约束通过提示词明确表达，理解效果仍需真实模型评测。阅读持久历史按 `paper_id` 保存，客户端决定本轮发送哪些历史。

公共配置关闭框架自动注册的 Skills、Task、TodoWrite、DevLog、会话和原文 trace。阅读只显式注册 PDF、表格、参考文献和论文检索工具，最多 5 轮工具调用。HelloAgents 固定为 `1.0.0`，没有全局 monkey patch，也不删除进程代理变量。供应商消息转换、DeepSeek 参数和客户端配置集中在 `PaperGraphLLM` 实例中。

## 预算、取消与并发

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `AGENT_REQUEST_TIMEOUT_SEC` | 180 秒 | 整个 HTTP/SSE 请求和单轮后台每日计算的总预算 |
| `AGENT_RUNTIME_DEFAULT_TIMEOUT_SEC` | 20 秒 | 默认模型执行和底层请求超时；阶段配置不能突破父预算 |
| `AGENT_CONTEXT_WINDOW` | 32768 tokens | 应与模型容量匹配；阅读输入最多占一半且不超过 12000，工具单次输出上限 8000 bytes |
| `AGENT_RUNTIME_MAX_WORKERS` | 8 | 同步执行池大小；线程尚未退出也占名额，没有无界等待队列 |
| `PAPERGRAPH_TIMEZONE` | `Asia/Shanghai` | 推荐日期与跨日刷新；Windows 依赖 `tzdata` |

阅读服务预算为 120 秒，主阅读 Agent 阶段上限 60 秒。实际取请求、阶段和底层 HTTP 剩余预算中的较小值。同步工作通过 `run_sync` 或 `_run_with_optional_timeout` 进入有界执行池，原生异步流使用有界通道；两类通道分别限制。容量耗尽会拒绝新任务并记录错误。

超时或取消后立即停止等待，并将取消信号传给执行上下文；缓存、记忆和图谱等回写点检查该信号。Python 线程无法安全强杀，已经发出的阻塞调用可能继续运行至底层超时，期间仍占用名额。预算检查不能替代底层 socket 超时，也不能撤回已经提交的数据。

只有连接类错误、429 和服务端错误允许有限重试；超时不重复支付调用预算。JSON 格式修复最多一次，并共享原任务 deadline。搜索原有阶段预算继续生效，`PAPERGRAPH_SEARCH_AGENT_WALL_SEC` 即使设置更大，也受请求总预算约束。

同输入计算使用固定数量的键锁，覆盖意图、搜索结果计算、导读、PDF 产物、反馈摘要和图谱。锁是进程内的；当前单进程部署可去重。多 worker 部署需要外部分布式锁或持久任务队列，不能据此宣称跨进程只执行一次。

## 上下文、记忆和输出

阅读提示先放完整最新问题，再给正文、历史和偏好分配 token 预算；超过问题预算会返回 422。工具输出另有限制，截断材料会明确标记。普通解读不再额外从回答里提取论文；只有明确推荐或查找需求才走相关检索。所有推荐入口统一排除当前论文、按题名/DOI/arXiv 去重，参考文献模式排除外部主题候选。卡片筛选、重排之后重新生成最终编号清单。

`paper_memory` 保留数据库、scope、paper_id、kind 和时间。当前论文记忆与可共享偏好通过同一个召回接口打分，可选 embedding 失败时退回关键词。共享召回仅接受 `kind=preference`；旧的无论文 ID 问答不会自动作为其他论文的事实。问答摘要写成 `model_summary`，提示中标明未经原文验证，清理同时覆盖 working、short 和 model_summary。分类偏好属于推断出的兴趣信号，也不是论文事实。

意图、每日索引、主题标签、参考题名、阅读排序和负偏好校验字段类型；索引要求整数、去重、有效范围和跨组互斥。精排和图谱分数拒绝布尔值、NaN、无穷及越界值。图谱模型关系仅允许声明的九种关系；手动编辑仍保留原有关系名称契约。模型输出的引用题名必须能在参考文献文本中逐字核对。

## 缓存与后台回写

| 缓存或任务 | 身份与失效 |
| --- | --- |
| 搜索意图 | 完整消息和显式历史、模式、数据库目录、模型/服务地址、提示词、日期；有界 LRU，读取副本 |
| 搜索结果 | 数据库隔离、完整解析计划、模型/服务地址、主会及方法约束、缓存版本；同请求并发计算串行复查 |
| 每日画像 | 数据库绝对路径、文献 ID 集、排除选项、日期、模型及数据文件版本；反馈清理并防止迟到加载重新缓存 |
| 每日结果 | 日期、模型/服务和提示版本；反馈推进持久 revision，旧计算不能覆盖新反馈后的缓存 |
| 导读 | 论文资料、PDF 路径/纳秒时间/大小、模型/服务及提示版本；相同生成串行，回写前复查文件 |
| PDF 和阅读档案 | 路径、纳秒时间和大小；档案另含解析版本及论文元信息；原子替换，改变文件时放弃旧回写 |
| 图谱 | 数据库、论文及候选内容、PDF 版本、抽取版本；仅成功后登记去重，失败立即允许下次重试 |
| 负偏好摘要 | 数据库与论文身份锁、近期成功记录和反馈 revision；失败不冒充已完成，新反馈可阻止旧摘要写入 |

每日循环每轮重新取日期。手动和后台刷新共用计算锁；失败与取消释放锁。附加反馈和记忆采用独立预算，不阻塞核心响应；任务仍属于进程内 best effort，进程退出后不会自动恢复。基础反馈已持久保存，可在后续反馈或维护任务中重试摘要。

图谱新增 `paper_relations.evidence_json` 保存源/目标 ID、位置、引句、文件版本和抽取版本。强关系及 references 必须有能在输入原文找到的引句；仍属于模型推断，不等同于人工确认。候选加入历史同分类样本及全库 DOI/arXiv 引用命中，保持候选上限。语义候选检索没有默认启用，先用实际文献库的人工标注衡量收益和成本。

## 追踪与验证

响应带 `x-request-id`，请求、工具、模型和 Agent 日志沿用同一标识，记录耗时、重试、终态和 usage。不记录密钥、完整提示词及论文原文。不提供 usage 的兼容服务或流式接口明确计入 `usage_missing`，不能按零 token 解释。

确定性验证：

```powershell
backend/.venv/Scripts/python.exe -B -m pytest backend/tests -q -p no:cacheprovider
```

本次新增回归放在被 `*.local` 忽略的 `backend/tests/agent_regressions.local/` 中，不加入提交。禁止在推送前运行的 `pnpm typecheck` 和 `pnpm lint` 不属于本轮验证。

固定案例位于 `backend/evals/agent_cases.json`，运行器位于 `backend/scripts/evaluate_agents.py`。默认不触网，无预测数据时指标返回 null：

```powershell
backend/.venv/Scripts/python.exe -B backend/scripts/evaluate_agents.py --output backend/data/experiments/agent-report.json
```

汇总已有基线或候选版本的预测：

```powershell
backend/.venv/Scripts/python.exe -B backend/scripts/evaluate_agents.py --predictions predictions.json --output backend/data/experiments/agent-report.json
```

真实意图评测按需手动执行，限制实际 invoke 次数（包括修复重试），使用现有模型配置：

```powershell
backend/.venv/Scripts/python.exe -B backend/scripts/evaluate_agents.py --live --max-calls 10 --output backend/data/experiments/agent-live.json
```

预测格式为数组，每条至少包含案例 `id`，可提供 `intent`、`ranked_ids`、人工 `evidence_reviews: [{supported: true}]`、`latency_ms` 和 `metrics: {calls, total_tokens, usage_missing}`。报告包含意图正确率、约束满足率、Recall@K、nDCG、证据支持率、P95 延迟、调用数及 token；缺少的维度留空。内置图谱判断样例是抽象示例，正式评测必须换为真实文献 ID 和独立人工标注。不要用 fake Agent 测试、模型自评分或缺失数据推断真实效果提升。

本轮只验证确定性行为与离线评测入口，没有运行付费真实模型评测，没有得出召回准确率或成本改善幅度结论。

本地验收：52 项后端测试通过（15 项原有测试、37 项不加入提交的新增回归），Python AST 与 UTF-8 无 BOM 检查通过。反馈额外修复了 `str(Enum)` 不等于动作值而导致 skip 摘要未触发的问题。

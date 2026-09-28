# PaperGraph 学习路线图

> 面向：计算机四大件略懂、没做过完整项目、想真真实实学架构和代码的人。
> 哲学：**以"一条用户请求的完整旅程"为主线**，从入口追到出口。追完一条链路就懂 60% 架构，其余链路同构，越读越快。
> 节奏：10 个阶段，每阶段 = 读几个文件 + 学几个概念 + 1 个小练习。建议每阶段结束把"自测题"答一遍再进下一阶段。
> 说明：本文所有文件链接与行号均对照当前仓库真实代码，边读边点即可跳转。

---

## 0. 先建立心智模型（不要跳）

### 0.1 项目的五层架构（背下来）

```
前端界面层   Vue 3 + Ant Design Vue   你看到的页面（搜索/每日/文献库/阅读/图谱）
   ↓ HTTP(REST) + SSE(流式)
API 接口层    FastAPI routes           把 HTTP 请求翻译成函数调用，薄薄一层
   ↓
智能体层      3 个 Agent（HelloAgents） "大脑"：用 LLM 解析意图、决定调哪些工具
   ↓
核心服务层    services/*               真正干活的业务函数（检索/PDF/推荐/记忆/图谱）
   ↓
数据持久层    SQLite + 本地文件          论文库存 papers.db，PDF 存 downloads/
```

**关键认知**：每一层只和相邻层说话。Route 不直接写 SQL，Service 不直接接 HTTP。这就是"分层架构"，工业项目的标配。

### 0.2 三个核心 Agent（都继承 `BaseAgent`）

| Agent | 文件 | 干什么 |
|---|---|---|
| SearchAgent | [search_agent.py](backend/app/agents/search_agent.py) | 把自然语言变成结构化 `SearchIntent`，编排多源召回 + 精排，并解释结果 |
| PaperAnalysisAgent | [paper_analysis_agent.py](backend/app/agents/paper_analysis_agent.py) | PDF 导读、阅读问答、参考文献查找、表格上下文 |
| KnowledgeGraphAgent | [knowledge_graph_agent.py](backend/app/agents/knowledge_graph_agent.py) | 从论文里抽关系，建知识图谱，解释节点/边 |

**它们不是从零写的**：底层用了开源框架 **HelloAgents**（`SimpleAgent` + `HelloAgentsLLM` + `Tool`/`ToolRegistry`）。你可以把 HelloAgents 理解成"帮你把 LLM 调用、工具注册、对话循环封装好的脚手架"，本项目在它之上做业务编排。

### 0.3 跑起来（必须有 LLM Key）

```bash
# 后端
cd backend
pip install -r requirements.txt
# 复制 .env.example 为 .env，填入 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_ID
python run.py          # 看到 🚀 启动... 就成功了，默认 http://localhost:8000

# 前端（另开一个终端）
cd frontend
npm install
npm run dev            # 打开 http://localhost:5173
```

也可以在项目根目录直接 `./start.sh` 一键起前后端。

**验证跑通**：浏览器打开前端 → 在"文献搜索"输入一句话 → 看到论文结果流式出现（左边有"正在解析意图 / 正在检索…"的阶段进度）。点"保存"→ 去"我的文献库"看到它。这条链路就是后面要拆解的主角。

---

## Phase 1 — 启动链路：项目是怎么"立"起来的

**目标**：搞懂"从 `python run.py` 到服务能接请求"中间发生了什么。这是理解任何后端项目的第一步。

**读这几个文件（按顺序）**：
1. [backend/run.py](backend/run.py) — 启动脚本。注意 L8（抑制 MuPDF 警告）、L11-13（把项目根加进 `sys.path`）、L30-43（`uvicorn.run` 拉起 `app.api.main:app`，以及 `PAPERGRAPH_UVICORN_RELOAD` 热重载开关）。
2. [backend/app/settings/config.py](backend/app/settings/config.py) — 配置中心。看 `get_settings()` 怎么从环境变量/`.env` 读配置、怎么缓存成单例。
3. [backend/app/api/main.py](backend/app/api/main.py) — FastAPI app 的"装配车间"。重点看：
   - L31-71 `lifespan`：应用启动/关闭时干的事（配置校验、启动每日刷新后台任务、关闭时取消任务）。这是"生命周期钩子"概念。
   - L73-80 `FastAPI(...)`：实例怎么创建（注意 `docs_url=None` 关掉了默认文档）。
   - L82-90 中间件：CORS（跨域）+ 自定义 `_MeaningfulActivityMiddleware`（记录"有意义活动"时间戳，供每日刷新判断）。
   - L92-94 路由挂载：`include_router(..., prefix="/api")`，三个路由模块（papers / paper_reader / search）。
   - L114-127 全局异常处理：所有未捕获异常都走这里，统一返回 500。

**要掌握的概念**：
- **ASGI 服务器**：uvicorn 是"跑 Python 异步 Web 服务"的容器，FastAPI 是框架。两者关系 ≈ Tomcat : Spring。
- **单例模式**：`get_settings()`/`get_database()`/`get_searcher()` 全局只建一个实例，避免重复开销。看 [dependencies.py L17-43](backend/app/api/dependencies.py#L17-L43) 的**双重检查锁**（先无锁判断，再加锁二次判断）。
- **依赖注入（DI）**：FastAPI 的 `Depends()` —— 函数参数里写 `db=Depends(get_database)`，框架自动把单例塞进来。这是后端解耦的核心手段。

**自测题**：
- [ ] 一个请求 `GET /health` 走了哪几层？画出来。
- [ ] 为什么 `get_database()` 要加锁？（提示：并发下只建一个）
- [ ] 想加一个"每次请求打印客户端 IP"的中间件，该改哪个文件、加在哪？

---

## Phase 2 — 最简单的一条请求链路：论文库 CRUD（建立"请求旅程"心智模型）

**目标**：追完一条**不带 LLM**的完整请求，建立"前端→Route→Service→DB→返回"的心智模型。这是后面所有链路的模板。

**主角请求**：`GET /api/library`（获取我的文献库列表）

**追链路（按顺序读）**：
1. [routes/papers.py L218-242](backend/app/api/routes/papers.py#L218-L242) — 路由函数 `get_library`。注意：参数校验（`Query` 的 ge/le）、`Depends(get_database)`，它自己**几乎不写业务逻辑**，只把活转给 service。
2. [services/papers/papers_library_service.py](backend/app/services/papers/papers_library_service.py) — 找到 `get_library` 相关函数。这里是业务逻辑所在：拼查询条件、调 db、转格式。
3. [core/storage.py](backend/app/core/storage.py) — `PaperDatabase` 类，真正执行 SQL 的地方（全文 725 行，是本项目的 DAO 主体）。看它怎么 `connect`、`execute`、`fetchall`。
4. [models/schemas.py](backend/app/models/schemas.py) — Pydantic 数据模型（`Paper`、`PapersResponse`…）。这是**接口契约**：API 返回什么形状，由它定义。
5. [services/papers/papers_converters.py](backend/app/services/papers/papers_converters.py) — `litpaper_to_api_paper`：内部领域模型 ↔ 对外 API 模型互转。为什么要转？因为"存数据库的形状"和"给前端的形状"不该耦合。

**要掌握的概念**：
- **分层职责**：Route（接 HTTP）→ Service（业务规则）→ Storage（数据访问）。Route 薄、Service 厚、Storage 专一。
- **DTO/Schema**：Pydantic 模型 = 数据传输对象，自动做校验和文档生成。
- **转换器模式**：内部模型和对外模型分离，改一边不影响另一边。

**练习（强烈建议动手）**：
> 加一个端点 `GET /api/library/count`，返回文献库论文总数。
> 提示：在 `routes/papers.py` 加一个 `@router.get(...)`，在 service 加一个函数，在 storage 加一个 `SELECT COUNT(*)`。三处各加一点点。跑通后用浏览器或 curl 验证。

**自测题**：
- [ ] 为什么 route 里不直接写 SQL？（说出 2 个理由）
- [ ] `Depends(get_database)` 每次请求都会新建一个数据库连接吗？为什么？
- [ ] 如果前端要加一个"按阅读状态筛选"，从哪一层开始改？

---

## Phase 3 — 数据持久层深入：领域模型 + SQLite + 文件存储

**目标**：理解"数据怎么存、怎么读、文件怎么管"。

**读这几个文件**：
1. [core/paper.py](backend/app/core/paper.py) — 领域模型 `Paper`（内部表示一篇论文的完整结构）。这是整个系统的"通用语言"，四个数据源最后都归一到它。
2. [core/storage.py](backend/app/core/storage.py) — 全文读。看建表 SQL、CRUD 方法、分类/标签/作者怎么存。**这是你第一次完整读一个 DAO**。
3. [core/pdf_download.py](backend/app/core/pdf_download.py) + [core/paper_paths.py](backend/app/core/paper_paths.py) — PDF 文件怎么下载、存哪、路径怎么管。文件存储 ≠ 数据库存储。
4. [services/reading_log/log.py](backend/app/services/reading_log/log.py) + 路由 [papers.py L307-311](backend/app/api/routes/papers.py#L307-L311) `reading_calendar` — 阅读记录怎么追加、怎么按天聚合（阅读日历的数据来源）。

**要掌握的概念**：
- **领域模型 vs DTO**：`core/paper.py` 是内部领域模型，`models/schemas.py` 是对外 DTO。Phase 2 的转换器就是在两者间翻译。
- **关系型数据建模**：论文-作者-标签是多对多，看 SQL 怎么建关联表。
- **文件存储 + 元数据**：PDF 放磁盘（`downloads/papers/`），路径/状态放数据库。这是常见模式（图片、视频也一样）。

**自测题**：
- [ ] 一篇论文有 3 个作者，在数据库里是几张表、几行记录？
- [ ] 阅读日历的"按天聚合"是在 Python 里算还是 SQL 里算？为什么这样选？

---

## Phase 4 — LLM 接入层：HelloAgents 怎么把"大模型"接进项目

**目标**：理解项目怎么封装 LLM 调用与 Agent 运行时，为后面所有 Agent 打底。**这一阶段是本项目区别于普通 CRUD 后端的关键。**

**读这几个文件**：
1. [services/llm/llm_service.py](backend/app/services/llm/llm_service.py) — `get_llm()`：把 OpenAI 兼容接口包成 HelloAgents 的 `HelloAgentsLLM`，做成单例。注意开头对 `invoke*` 打的补丁（把 `summary` 角色归一成 `user`，兼容不同厂商）。
2. [services/llm/agent_config.py](backend/app/services/llm/agent_config.py) — `papergraph_agent_config()`：基于 `hello_agents.core.config.Config` 生成 Agent 默认运行配置（温度、最大 token 等）。
3. [services/llm/agent_runtime.py](backend/app/services/llm/agent_runtime.py) — **核心封装**。`run_agent_task`（每次 new 一个 `SimpleAgent` 跑一轮，带线程池超时 + 重试）、`run_json_task`（跑完再把输出解析成 JSON，失败给默认值）。看它怎么用 `_exception_chain_predicate` 判断"是不是超时导致的失败"。
4. [agents/base.py](backend/app/agents/base.py) — `BaseAgent`：所有 Agent 的父类。一出生就持有 `settings` 和 `llm`（`_init_llm` 失败会抛 `xxx_llm_init_failed`）。

**要掌握的概念**：
- **客户端封装**：永远不要在业务代码里直接 `import openai`，而是包一层 `llm_service`。换模型/换厂商时只改一处。
- **Agent 运行时三件套**：超时（`timeout_sec`）+ 重试（`retries`）+ 结构化输出（`run_json_task`）。这是把"不稳定的 LLM"变成"可用工程组件"的关键。
- **配置驱动**：模型名、温度、超时都从 settings 来，不硬编码。
- **Prompt 与代码分离**：看 [agents/prompts/](backend/app/agents/prompts/) 目录（`search.py`/`paper_analysis.py`/`knowledge_graph.py`）——prompt 是单独文件管理的，不混在逻辑里。

**自测题**：
- [ ] 为什么 LLM 客户端要做成单例？
- [ ] `run_agent_task` 里为什么要用 `ThreadPoolExecutor` 而不是直接调 `agent.run`？（提示：超时怎么强制中断）
- [ ] 如果要把当前模型换成另一个 OpenAI 兼容服务，要改哪几个地方？

---

## Phase 5 — 搜索链路：带 Agent 的完整旅程（项目核心，最有架构含量）

**目标**：追完一条**带 LLM + SSE 流式 + 多源召回 + 精排 + 缓存**的复杂链路。这是这个项目最值钱的部分，慢慢读。

**主角请求**：`POST /api/search-agent/stream`（自然语言搜论文，SSE 流式返回）

**追链路（这一段较长，分三块读）**：

**5a. SSE 入口与编排** — [routes/search.py](backend/app/api/routes/search.py)（全文 446 行）
1. L323-446 `search_agent_chat_stream`：SSE 流式响应。看 `anyio.create_memory_object_stream`（内存通道）+ `ToolCallTracker(sink=...)`（把事件塞进通道）+ `StreamingResponse`。这是"边算边推"的事件驱动模型。
2. L135-283 `_run_search_agent_core`：编排主干。顺序是 `understand_intent`（意图）→ `ResolvedSearchPlan.from_search_intent`（计划）→ **查缓存** → `run_search_pipeline_async`（召回精排）→ `explain_results`（解释）→ **写缓存**。
3. [api/tool_events.py](backend/app/api/tool_events.py) — `ToolCallTracker` + `sse_pack`：怎么把"工具调用/阶段状态"打包成 SSE 事件（`data: {...}\n\n`）发给前端。这就是前端"正在检索…"进度条的来源。
4. 超时兜底：`_prepare_agent_and_query` 的初始化超时、`_search_agent_impl` 里 `asyncio.wait_for(..., _SEARCH_AGENT_WALL_SEC)` 的墙钟上限。

**5b. Agent 意图解析**
5. [agents/search_agent.py](backend/app/agents/search_agent.py) — `SearchAgent.understand_intent` 实际委托给 `IntentParser.parse`：**带 5 分钟 LRU 缓存**（`_INTENT_CACHE`）+ **失败重试并回灌纠错提示**（`_parse_with_retry`）。内部用 HelloAgents 的 `SimpleAgent` 只输出 JSON。这是"LLM 当意图解析器"的范式。
6. [agents/prompts/search.py](backend/app/agents/prompts/search.py) + [services/search_intent/](backend/app/services/search_intent/)（`parsing.py`/`arxiv_normalization.py`…）— 意图 prompt 与解析后处理（`finalize_llm_intent`、`apply_llm_intent_hygiene`）。看它怎么要求 LLM 输出结构化 JSON、怎么清洗。
7. [services/retrieval/search_recipe.py](backend/app/services/retrieval/search_recipe.py) + [search_plan.py](backend/app/services/retrieval/search_plan.py) — `SearchRecipe` / `ResolvedSearchPlan`：意图 → "可执行检索计划"的中间表示（关键词、年份窗、会议、是否 LLM 精排、兜底策略）。

**5c. 多源召回 + 精排 pipeline**
8. [services/retrieval/search_pipeline.py](backend/app/services/retrieval/search_pipeline.py) — **核心**。`run_search_pipeline_async` 明确分 4 阶段：① 多源并行召回 → ② 补回用户指定 arXiv ID（pinned）→ ③ 去重/过滤非主会/相关性守卫 → ④ LLM 精排（或召回直接截断）。这是 pipeline 模式的教科书例子。
9. [services/retrieval/recall_jobs.py](backend/app/services/retrieval/recall_jobs.py) — 多源并行召回。看 `build_recall_jobs` / `execute_recall_jobs` / `dedupe_papers` / `merge_candidates` 怎么对多源并发调用、单个失败怎么不影响整体（**优雅降级**）。
10. [core/search/sources/](backend/app/core/search/sources/) — 四个数据源适配器（`arxiv.py`/`dblp.py`/`openalex.py`/`tavily.py`）。每个都是"把外部 API 翻译成内部 `Paper` 模型"的适配器。看 [source_common.py](backend/app/core/search/sources/source_common.py) 抽出的公共逻辑。
11. [services/retrieval/paper_ranker.py](backend/app/services/retrieval/paper_ranker.py) + [ranking_prompt.py](backend/app/services/retrieval/ranking_prompt.py) — LLM 精排：把召回的 N 篇让 LLM 重排（`LlmPaperRanker.rank`）。
12. [services/retrieval/search_cache.py](backend/app/services/retrieval/search_cache.py) — `SearchResultCache`：按 plan 生成 key，命中就跳过召回精排直接返回。
13. [core/search/paper_searcher.py](backend/app/core/search/paper_searcher.py) — `PaperSearcher`：对四个 source 的高层封装，被 `dependencies.py` 做成单例注入路由。

**要掌握的概念（这一阶段概念最密）**：
- **SSE（Server-Sent Events）**：服务器单向推流给浏览器。比 WebSocket 简单，适合"过程进度 + 最终结果"场景。
- **Agent = LLM + 工具 + 循环**：这里 `understand_intent` 是简化版（LLM 只做意图解析），Phase 6 的阅读 Agent 才是完整的"LLM 决定调哪个工具"。
- **Pipeline 编排**：把复杂流程拆成"召回→去重→过滤→精排"几个阶段，每阶段输入输出明确。可读性 + 可测试性暴增。
- **多源召回 + 优雅降级**：四个数据源互为补充，一个挂了不影响整体。
- **适配器模式**：每个外部 API 长得不一样，但都翻译成统一的 `Paper`。新增数据源 = 加一个适配器文件。
- **LLM 精排**：传统按相关性分数排序 vs 让 LLM 看摘要重排，后者更懂"语义"。
- **超时与缓存**：`anyio.fail_after` / `asyncio.wait_for` 防慢请求拖垮服务；`SearchResultCache` 防重复昂贵计算。

**练习（这个做完你就出师一半）**：
> pipeline 出口其实已经算好了"每个数据源各贡献几篇"（看 `search_pipeline.py` L231-241 的 `candidates_by_source` / `ranked_by_source`）。把它透出到前端结果卡片上。
> 提示：这些字段已经在 `SearchPipelineResult.metadata` 里，顺着 `_run_search_agent_core` 的 `metadata` 一路传到 `SearchAgentResponse`，再在前端读取渲染即可。

**自测题**：
- [ ] 用户输入一句话到看到第一篇论文，数据流经过了哪些函数？画时序图。
- [ ] 如果 arXiv 挂了，用户还能看到结果吗？为什么？关键代码在哪？
- [ ] SSE 相比直接返回 JSON，好处是什么？这个场景为什么必须 SSE？
- [ ] 命中缓存和未命中缓存，`_run_search_agent_core` 的执行路径差在哪？
- [ ] 想新增一个数据源（比如 Semantic Scholar），要改/加哪些文件？

---

## Phase 6 — 阅读链路：PDF 解析 + 工具型 Agent + 阅读问答

**目标**：理解 PDF 怎么解析、阅读上下文怎么构建、"LLM + 工具"的完整 Agent 怎么跑。

**追链路**：
1. [routes/paper_reader.py](backend/app/api/routes/paper_reader.py) — 阅读相关路由：`/ai/paper-reader/opening`（首次打开生成导读）、`/chat`（问答）、`/history`（历史）。注意 `Depends(get_paper_reader_service)` 和 `BackgroundTasks`（后台异步干活）。
2. [services/pdf/pdf_service.py](backend/app/services/pdf/pdf_service.py) — PyMuPDF（fitz）解析 PDF：抽正文、表格、结构。
3. [services/reader/](backend/app/services/reader/) — 阅读服务全家桶：
   - [paper_reader_context.py](backend/app/services/reader/paper_reader_context.py) — **重点**（532 行）：怎么把"正文 + 表格 + 参考文献 + 历史记忆"拼成给 LLM 的上下文。这就是 RAG 的雏形。
   - [paper_reader_service.py](backend/app/services/reader/paper_reader_service.py) — 问答主流程 `process_chat`。
   - [paper_reader_history.py](backend/app/services/reader/paper_reader_history.py) — 对话历史管理。
4. [agents/paper_analysis_agent.py](backend/app/agents/paper_analysis_agent.py)（773 行）+ [agents/support/](backend/app/agents/support/) — PaperAnalysisAgent 和它的工具集。重点看 `reader_pdf_parse_tool.py` / `reader_reference_lookup_tool.py` / `reader_table_tool.py` / `reader_paper_lookup_tool.py`：它们都继承 **HelloAgents 的 `Tool`**（`from hello_agents.tools.base import Tool, ToolParameter, ToolResponse`），被注册成 Agent 可按需调用的"工具"。

**要掌握的概念**：
- **PDF 解析**：PDF 是排版格式不是文本，要靠库抽正文，表格尤其难。
- **工具注册（Tool）**：Agent 持有一组"工具函数"，LLM 决定调哪个、传什么参数。看 support 目录里每个 `Tool` 怎么声明参数（`ToolParameter`）和返回（`ToolResponse`）。
- **上下文构建（Context Builder）**：LLM 答题质量取决于你喂它什么。把相关片段精选出来喂进去 = RAG 思想。
- **对话历史**：多轮对话要把历史带上，但要截断/摘要防止 token 爆炸。

**自测题**：
- [ ] 用户问"这篇论文用了什么数据集"，LLM 拿到的上下文里有哪些东西？分别来自哪个文件？
- [ ] 为什么不直接把整篇 PDF 塞给 LLM？
- [ ] 一个 `Tool` 从"声明"到"被 LLM 调用"经历了什么？

---

## Phase 7 — 每日推荐 + 共享记忆机制

**目标**：理解后台任务、缓存、用户行为分析，以及本项目一大亮点——**多 Agent 共享记忆（GSSC）**。

**读这几个文件**：
1. [services/daily/daily_service.py](backend/app/services/daily/daily_service.py)（567 行）— 每日论文计算主流程。
2. [services/daily/daily_cache_store.py](backend/app/services/daily/daily_cache_store.py) + [daily_auto_refresh.py](backend/app/services/daily/daily_auto_refresh.py) — 缓存 + 后台自动刷新（在 [main.py lifespan L50-55](backend/app/api/main.py#L50-L55) 用 `spawn_daily_auto_refresh` 启动的常驻协程）。
3. [services/daily/user_behavior_analytics.py](backend/app/services/daily/user_behavior_analytics.py) — 从用户行为算兴趣词。
4. [services/daily/daily_recommend_feedback.py](backend/app/services/daily/daily_recommend_feedback.py) + [services/feedback/negative_feedback_memory.py](backend/app/services/feedback/negative_feedback_memory.py) — 正/负反馈怎么沉淀成"记忆"，影响后续推荐。
5. [services/memory/agent_memory.py](backend/app/services/memory/agent_memory.py) + [memory_store.py](backend/app/services/memory/memory_store.py) — **共享记忆核心**。`MemoryStore.get_context_for_query` 就是 GSSC 流水线的落地：**Gather**（按 paper_id/scope 捞候选记忆）→ **Score**（`overlap*2 + importance + recency*0.1` 打分）→ **Select**（取 top-k 拼成上下文块）。多个 Agent 通过 `shared` 标记共享偏好/工作记忆。

**要掌握的概念**：
- **后台任务**：`lifespan` 里 `spawn_daily_auto_refresh` —— 应用启动时开一个常驻协程定时刷新，不阻塞主请求；关闭时 `task.cancel()` 优雅收尾。
- **缓存**：算一次很贵，结果存起来，下次直接给。
- **记忆机制 / GSSC**：Agent 不是无状态的，用户反馈会被记录并影响后续决策。记忆不是全塞给 LLM，而是"打分后选最相关的几条"——这就是 Gather→Score→Select。
- **锁**：每日计算入口有并发锁，防止多个请求同时重复计算（在 daily 路由/service 里找 compute lock）。

**自测题**：
- [ ] 每日论文是每次请求都重算吗？什么情况下重算、什么情况下走缓存？
- [ ] 用户点"不感兴趣"后，这个信号流向了哪里？最终怎么影响下次推荐？
- [ ] GSSC 的 Score 公式里，为什么 `overlap` 权重最大、`recency` 权重最小？

---

## Phase 8 — 知识图谱

**目标**：理解关系抽取和图谱数据怎么生成、怎么给前端 D3 可视化。

**读这几个文件**：
1. [services/graph/graph_service.py](backend/app/services/graph/graph_service.py) — `build_library_graph`：从文献库生成节点 + 边。
2. [services/graph/kg_relations.py](backend/app/services/graph/kg_relations.py)（378 行）— 关系抽取逻辑（主题/方法/引用/相关），以及 `get_kg_metrics`（`/health` 里会用到）。
3. [agents/knowledge_graph_agent.py](backend/app/agents/knowledge_graph_agent.py) + [agents/prompts/knowledge_graph.py](backend/app/agents/prompts/knowledge_graph.py) — 用 LLM 解释节点/关系。
4. 图谱增删改查路由：[routes/papers.py L76-211](backend/app/api/routes/papers.py#L76-L211)（`/graph/library`、`/graph/library/export`、`PUT/DELETE /graph/relations`）。

**要掌握的概念**：
- **图数据结构**：节点（论文/主题/作者）+ 边（引用/相关/属于）。前端 D3 可视化吃这种结构。
- **关系抽取**：从非结构化文本里抽结构化关系，规则 + LLM 混合。

**自测题**：
- [ ] 图谱的一个"边"在数据里长什么样？是谁连谁、什么关系？
- [ ] 前端 `/graph/library/export` 导出的和 `/graph/library` 返回的有何不同？

---

## Phase 9 — 前后端契约 + 前端速览（补"完整链路"的最后一块）

**目标**：你不一定写前端，但要看懂"前端怎么调后端、类型怎么对齐"。这样才叫理解了"完整运行链路"。

**读这几个文件**：
1. [frontend/package.json](frontend/package.json) 的 `scripts` 段 — `openapi:export`（从后端 app 导出 `openapi.json`）/ `openapi:types`（生成 `src/types/openapi.ts`）/ `openapi:gen`（两步串起来）：**契约生成流水线**。
2. [frontend/src/services/api/client.ts](frontend/src/services/api/client.ts) — axios 客户端封装。
3. [frontend/src/services/api/search.ts](frontend/src/services/api/search.ts) — 搜索接口的前端封装。看它怎么消费 SSE 流（fetch 流式读，边收边解析 `data:` 事件）。
4. [frontend/src/composables/useSearchAgentChat.ts](frontend/src/composables/useSearchAgentChat.ts) — 搜索对话的组合式逻辑（Vue 的 composable = 可复用的业务逻辑块，对应 React 的 hook）。
5. [frontend/src/views/SearchAgent.vue](frontend/src/views/SearchAgent.vue) — 搜索页面。看一个 view 怎么串起 composable + 组件 + API。

**要掌握的概念**：
- **契约驱动开发**：后端改字段 → 重新生成前端类型 → TS 编译报错 → 前端被迫改。类型安全跨前后端。
- **SSE 客户端消费**：前端用 fetch 流式读，边收边渲染阶段进度和最终结果。
- **Composable 模式**：Vue 3 把"逻辑"从组件里抽出来复用。

**自测题**：
- [ ] 后端给 `Paper` 加了一个字段，前端要怎么做才能用上？（说出完整步骤，含命令）
- [ ] 搜索过程的"正在检索…"进度，前端是怎么从 SSE 里收到并渲染的？

---

## Phase 10 — 毕业项目：自己端到端加一个小功能

**目标**：验证你真的懂了。从后端端点到前端按钮，全链路自己加。

**建议题目（任选一个，由易到难）**：
1. **加一个"导出 BibTeX"端点**：`GET /api/{paper_id}/bibtex` 返回纯文本 BibTeX。纯后端，练 storage + 转换 + 文本生成。
2. **加一个"论文笔记"功能**：给文献库的每篇论文加一段用户自己写的笔记。
   - 后端：storage 加表/字段 → service 加 get/set note → route 加 `PUT /api/{id}/note` → schema 加字段。
   - 前端：`npm run openapi:gen` 重新生成类型 → api 封装 → 文献库页加笔记输入框。
3. **给搜索加一个"只看今年"快捷开关**：前端按钮 → 传参 → 后端在 intent/plan 里生效。练全链路传参。

**做完后自检**：
- [ ] 我新增的代码，分层对不对？（route 薄、service 厚、storage 专一）
- [ ] 我有没有把业务逻辑塞进 route？（不该）
- [ ] 我加的字段，前后端类型对齐了吗？（跑了 `openapi:gen` 吗）
- [ ] 我有没有写异常处理？（参考现有 route 的 `try/except` + `safe_http_500`）
- [ ] 涉及 LLM 的调用，有没有走 `agent_runtime` 的超时 + 重试，而不是裸调？

---

## 附：小白读代码的几条心法

1. **先读"形状"再读"细节"**：先看文件/函数叫什么、互相怎么调用（画依赖图），再钻进具体实现。别一上来逐行读。
2. **追一条请求，不要横扫**：选一个具体功能，从入口追到出口。比"把 services 目录全读一遍"高效十倍。
3. **看不懂的函数先跳过**：当成黑盒，先理解它在整体里的角色，回头再补细节。
4. **善用 grep / 全局搜索**：看到一个函数被调用，搜它的定义；看到一个类型，搜它在哪被构造。
5. **改一点点 + 跑起来**：读懂一个模块后，故意改一个变量名/加一个 `print`/加一条 `logger.info`，跑起来看日志。这是验证你真懂了的最快办法。
6. **看 git blame**：一行代码不懂为什么这么写，`git blame` 看提交信息。

---

## 附：概念速查表（遇到不懂的词回来查）

| 词 | 一句话解释 | 在本项目哪看 |
|---|---|---|
| 分层架构 | Route/Service/Storage 各管一摊 | 全项目 |
| 依赖注入 (Depends) | 框架自动把依赖塞进函数参数 | api/dependencies.py |
| 单例 + 双重检查锁 | 全局只一个实例，并发下也只建一个 | dependencies.py L17-43 |
| Pydantic / DTO | 用类定义数据形状，自动校验 + 生成文档 | models/schemas.py |
| HelloAgents | Agent 脚手架：SimpleAgent / LLM / Tool | services/llm/*, agents/support/* |
| Agent 运行时 | 给 LLM 调用加超时 + 重试 + JSON 解析 | services/llm/agent_runtime.py |
| SSE | 服务器单向推流给浏览器 | routes/search.py, api/tool_events.py |
| Pipeline | 把复杂流程拆成有序阶段 | retrieval/search_pipeline.py |
| 适配器 | 把外部 API 翻译成内部 Paper 模型 | core/search/sources/* |
| 优雅降级 | 某个数据源挂了不影响整体 | retrieval/recall_jobs.py |
| 检索缓存 | 相同 plan 命中缓存跳过昂贵计算 | retrieval/search_cache.py |
| RAG / 上下文构建 | 精选相关片段喂给 LLM | reader/paper_reader_context.py |
| GSSC 记忆 | Gather→Score→Select 选最相关记忆 | memory/memory_store.py |
| 后台任务 | lifespan 里起的常驻协程定时刷新 | daily/daily_auto_refresh.py |
| 契约驱动 | OpenAPI 生成前端 TS 类型 | package.json openapi:* |
| 生命周期钩子 | 应用启动/关闭时执行的代码 | main.py lifespan |
| 熔断/超时 | 防止慢依赖拖垮服务 | anyio.fail_after, asyncio.wait_for |

---

> 走完这 10 个阶段，你不只是"读过这个项目"，而是掌握了**任何后端项目的通用读法**，外加一套"LLM 应用工程化"的实战经验（意图解析、多源召回、精排、缓存、记忆、工具型 Agent）。卡住就把"文件名 + 行号 + 你不懂的点"发给我，我带你过。

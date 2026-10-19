# 文献库与阅读流程

保存接口先将论文和后处理任务一起持久化，再返回 `ids`、`added`、`updated` 和 `processing_queued`。`pdf_downloaded`、`llm_classified` 保留兼容字段，立即响应中为 0；后续进度以任务接口为准。缺失摘要保持为空，不再把搜索网页片段写入论文摘要。

应用启动时创建一个后台消费者，每进程最多处理一篇论文。处理顺序为分类、PDF、图谱；PDF 保存后再构建关系。任务存储在 SQLite 中，支持原子领取、重复保存合并和失败重试，最多自动尝试 3 次。异常退出遗留的运行任务在 15 分钟租约过期后恢复；正常关闭会发送协作取消信号。多进程部署时每个进程各有一个消费者，SQLite 防止同一有效租约被重复领取。

- `GET /api/papers/processing/status?ids=1,2`：查询最多 100 篇论文的状态、阶段、尝试次数和错误。
- `POST /api/papers/processing/{paper_id}/retry`：重新排队失败任务。
- 文献库展示可见论文的处理状态；有运行任务时每 3 秒刷新，离开页面取消轮询。

PDF 下载使用按目标路径加锁和唯一临时文件，默认上限 100 MiB、总预算 120 秒，检查 PDF 文件头后才替换目标文件。网络取消采用分段检查，正在进行的 socket 读取仍需等待其超时；不强杀线程。无法下载 PDF 的论文仍保留入库记录，在任务状态中显示失败。

删除论文在事务中清理作者关联、阅读历史、论文记忆、导读与 PDF 文本缓存、阅读时长、后处理任务和图谱关系。全局偏好与推荐反馈继续保留。删除标记阻止迟到请求写回这些派生表；阅读产物写入也检查删除标记。文件清理任务与数据库删除一起提交，提交后删除托管 PDF 和阅读产物，失败由后台继续重试；共享引用和数据目录之外的路径不会删除。

文献库列表和计数使用同一组筛选条件，标签筛选在分页前执行。图谱同样先筛选分类再限制数量。阅读页与文献库使用请求取消和响应归属校验，防止旧请求覆盖新页面状态。

图谱接口返回 `evidence_details`，包含来源论文、原文位置、引用、来源类别与版本标识。反向边仍指向原始证据论文。页面可打开来源阅读页或 PDF 核对引用；只有原始证据确实带有位置时才展示位置，不生成虚构页码。

搜索会话在 24 小时内恢复；过期状态清理。历史限制为 50 条、2 MiB，当前状态限制为 1 MiB，达到上限时提示用户并保留当前页面内容，不自动删除历史。离开页面会刷出尚未写入的状态。

# 真实论文评测与版本对比

以下命令在项目根目录执行。导出只读取文献库，不调用模型，不修改论文；结果默认放在被忽略的运行目录：

```powershell
backend/.venv/Scripts/python.exe backend/scripts/export_library_eval_cases.py --db backend/data/papers.db --limit 20 --output backend/data/experiments/library-cases.json
```

导出的是待标注草案，包含真实论文 ID、标题、年份、DOI/arXiv ID。需独立人工确定检索候选池、填写 `judgments`（0..3 相关性）；阅读引用使用人工 `evidence_reviews`。空标签不产生召回与排序指标。预测中的 `ranked_ids` 必须与标签使用同一套 DOI/arXiv 身份键。

```powershell
backend/.venv/Scripts/python.exe backend/scripts/evaluate_agents.py --cases backend/data/experiments/library-cases.json --predictions backend/data/experiments/baseline-predictions.json --min-coverage 1 --output backend/data/experiments/baseline.json
backend/.venv/Scripts/python.exe backend/scripts/evaluate_agents.py --cases backend/data/experiments/library-cases.json --predictions backend/data/experiments/candidate-predictions.json --baseline backend/data/experiments/baseline.json --min-coverage 1 --fail-on-regression --output backend/data/experiments/comparison.json
```

基线必须使用完全相同的案例集和 K 值。报告给出覆盖率、缺失案例、共同案例上的指标差值；覆盖率不足或门禁发现回退时以非零状态退出，仍保留报告。延迟和成本也纳入严格回退检查，可省略 `--fail-on-regression`，人工判断质量与成本取舍。

本轮验证是确定性回归和构建验证，没有付费模型实测，也不宣称召回率或生成质量已经提高。新增测试保留在 `*.local` 目录，不加入提交；推送前才运行约定的 typecheck/lint。

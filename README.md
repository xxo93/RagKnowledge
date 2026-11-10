# RagKnowledge · 企业文档智能问答 Agent（RAG + LangGraph 工具 Agent）

面向简历展示与本地运行的私有文档知识库 + 多工具 Agent Demo：覆盖 RAG 全链路（解析→清洗→切片→向量化→检索→生成→引用溯源）与 LangGraph 三工具 Agent（知识库检索 / 数学计算 / 文档统计），SSE 流式输出、会话历史持久化。

> 设计依据：[需求文档.md](./需求文档.md) V2.1（pgvector 单库版）、[架构设计文档.md](./架构设计文档.md)。
> 当前阶段：**M0–M6 主链路完成并持续打磨（截至 V55）**：PG17 + pgvector 四表与 `vector(1024)` HNSW；上传解析切片入库、向量检索、LangGraph 三工具 Agent、SSE 流式与会话落库、双页面 UI 均已接通；已接入真实 Rerank（gte-rerank-v2，复用 Embedding Key、失败降级原序）、SSE 前置准备 + token 期心跳、工具调用防重复键、来源卡并列展示重排/相似度分；动态多轮再规划默认关闭、经开关门控与单次真实再决策轻测（V51）；小规模双模式复评显示 Rerank 首名增益持平、阈值维持 0.0（V52）；轻量离线 CI、本地回归脚本与《运维与回归手册》就位（V53）；后端重解析刷新向量与 HNSW REINDEX 能力就位（V54）；服务端批量聚合接口、文档页重解析/重建索引按钮就位（V55）。逐版本验收见 [交付验收报告.md](./交付验收报告.md)。

## 一、技术栈

| 层 | 技术 |
| --- | --- |
| 前端 | Vue3 + Element Plus + Vite（fetch 手写 SSE 解析） |
| 后端 | Python FastAPI + Uvicorn，分层单体（路由/服务/Agent/基础设施） |
| Agent | LangGraph 状态图 + 循环上限（默认 3 轮） |
| 存储 | PostgreSQL 16/17 + pgvector，业务表与向量表同库同事务，HNSW 余弦索引 |
| Embedding | 当前 = 方案② API 托管：百炼 `text-embedding-v3`（默认 1024 维，OpenAI 兼容 `/embeddings`，单批 ≤10，匹配 `vector(1024)`）；本地 BGE / Ollama 为预留分支，适配器模式可切换 |
| LLM | OpenAI 兼容协议（DeepSeek 默认，可切豆包 / 通义 / OpenAI） |
| Rerank | 百炼 `gte-rerank-v2`（原生 text-rerank 端点，复用 Embedding Key；`RERANK_ENABLED` 开关，失败降级原序） |

明确不做：登录鉴权、多用户隔离、Docker 化项目、部署发布、异步队列、OCR、Milvus 集群（见需求文档 §十一）；现有仅为不接数据库/密钥的轻量离线 CI，说明见第六节与 [运维与回归手册.md](./运维与回归手册.md)。

## 二、目录结构

```
RagKnowledge/
├── 需求文档.md / 架构设计文档.md
├── README.md / .env.example / .gitignore
├── backend/
│   ├── main.py                 # FastAPI 入口（lifespan、CORS、路由注册、全局异常）
│   ├── config.py               # Pydantic Settings 统一配置
│   ├── db.py                   # 异步连接池 + register_vector + to_vector 包装(ADR-006)
│   ├── errors.py / app_logging.py / llm.py / prompts.py
│   ├── init_db.sql             # DDL（__EMBEDDING_DIM__ 占位符，幂等）
│   ├── scripts/init_db.py      # 渲染维度并执行建库建表
│   ├── schemas/ routers/ services/ agent(tools)/ tests/
├── frontend/                   # Vue3：api(sse)、composables、views、components
├── .github/workflows/ci.yml    # 轻量离线 CI：后端单测/装配 + 前端构建（不接 DB/密钥/发布）
└── data/uploads/               # 原始文档留档（不入库）
```

分层依赖规则（架构 §2.2）：routers 只做协议；services 做流程；SQL 只出现在 `services/vector_store.py` 与 `db.py`；agent 不直接读写库；外部 LLM/Embedding 只经 `llm.py`/`embedder.py`。

## 三、本地启动（完成 M0 环境后）

### 1. 数据库（任选其一）

Docker（最省事，镜像已内置 pgvector）：

```powershell
docker run -d --name pgvector -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=rag_agent -p 5432:5432 pgvector/pgvector:pg16
```

或本机 PostgreSQL 安装 pgvector 扩展后建库：`CREATE DATABASE rag_agent;`

### 2. 后端

```powershell
cd backend
python -m venv .venv ; .venv\Scripts\activate
python -m pip install -r requirements.txt
copy ..\.env.example ..\.env     # 填入 DB_DSN、LLM_API_KEY、EMBEDDING_DIM
python scripts\create_database.py   # 幂等建 rag_agent 库（本机 PG；Docker 方式可跳过）
python scripts\init_db.py           # 幂等建扩展/表/HNSW 索引并回读校验（vector(1024)）
python scripts\run_dev.py            # Windows 推荐：SelectorEventLoop 起异步 DB 连接，监听 127.0.0.1:8000
# 无库冷启动 / 非 Windows 可直接：uvicorn main:app --host 127.0.0.1 --port 8000
```

接口文档：<http://127.0.0.1:8000/docs>；健康检查：`GET /health`；配置元信息：`GET /api/system/info`。文档接口：`POST /api/docs/upload`（单文件兼容）、`POST /api/docs/upload/batch`（服务端顺序批量聚合）、`POST /api/docs/{doc_id}/reparse`（重解析替换切片向量）、`POST /api/docs/reindex`（重建 HNSW 索引）；前端文档页已接入批量上传、重解析与重建索引操作。

### 3. 前端

```powershell
cd frontend
npm install
npm run dev        # http://127.0.0.1:5173 ，Vite 代理 /api 到 8000
```

## 四、里程碑与当前进度

| 里程碑 | 内容 | 状态 |
| --- | --- | --- |
| 骨架 + 服务层 | 目录/配置/契约/纯函数；后端依赖安装与自检；前端依赖安装、`build`、`dev` 与代理联通（阶段1–3） | ✅ 阶段1–3 完成；DB 数据链路见 M0 |
| M0 | PG + pgvector 环境、建库建表建 HNSW、扩展版本验证、异步连接 `db_ready=true` | ✅ 阶段4完成（PG 17.11 + vector 0.8.6，四表 + `vector(1024)` HNSW；详见报告四点七 / V17–V20） |
| M1 | 上传解析切片入库单事务链路、文档列表/删除 | ✅（事务入库、FK 级联删除；详见报告 M1/V22–V30） |
| M2 | 向量检索 SQL + Prompt + LLM 生成 + 引用来源 | ✅（HNSW 余弦检索、阈值过滤、来源结构；V31–V35） |
| M3 | LangGraph 状态图、三工具、意图路由、循环上限 | ✅（关键词兜底决策、三工具节点、单轮收敛；V36–V40） |
| M4 | SSE 流式、会话与记录落库、历史回填 | ✅（决策/流式/降级、FK 双保险、落库 saved 标志；V41–V42） |
| M5 | 两页面 UI 与交互、来源卡片、中断恢复 | ✅（会话管理、token 渲染、来源卡重排分、中断复位；V35–V43） |
| M6 | 调优、异常兜底、日志、评测与 Rerank | ✅ 基线完成（gte-rerank-v2、Hit@1=1.0 基线、心跳/防重复/HNSW 验证；大语料复评待后续） |

> 逐阶段开发与验收记录见 [交付验收报告.md](./交付验收报告.md)。

## 五、关键安全与一致性设计（面试要点）

- **单库同事务**：切片向量与文档元数据同库，上传 T2 原子入库、删除显式事务清切片，无双写补偿；
- **SQL 全参数化**：统计工具枚举白名单、计算器正则 + AST 白名单（不使用 eval）；
- **向量维度三维约束**：模型输出维度 = `EMBEDDING_DIM` = 表列 `vector(N)`，启动与入库双重校验；
- **向量写入统一 `Vector(...)` 包装**：pgvector-python 未注册 list dumper，直传 list 会报类型错误（ADR-006）；
- 服务仅绑定 `127.0.0.1`，无鉴权为明确边界，不暴露局域网。

## 六、常用回归命令（离线零费用 / 联网按需）

**纯离线、零模型调用（日常首选；无网络费用）**：

```powershell
# 一键本地离线回归（后端 pytest + check_env + compileall；加 -Frontend 追加 npm ci 与 vite build）
powershell -ExecutionPolicy Bypass -File backend\scripts\offline_regression.ps1

cd backend
python -m pytest tests -q                  # 纯函数/解析/防重复/心跳/再规划单测（基线 44 passed）
python scripts\check_env.py                 # 依赖与内部模块导入、FastAPI 装配、路由注册（不连库不调模型）
python -m compileall -q .                   # 静态字节编译，只编译不执行、不联网
cd ..\frontend
npm run build                              # 前端生产构建（验证无编译错误）
```

> 需要本地 PostgreSQL 但不调用模型、无模型费用的脚本：`check_chat_sse.py`（REAL_LLM 默认 SKIP）、`check_hnsw_index.py`（事务内造数验证 HNSW 计划、结束回滚不污染数据）；详细分级与排障见 [运维与回归手册.md](./运维与回归手册.md) 第 4、9 节。

**联网（按需执行，会产生少量 Embedding / Rerank / LLM 调用费用）**：

```powershell
cd backend
python scripts\check_rerank.py             # 真实 gte-rerank-v2 排序 + 坏端点降级探针
python scripts\assess_retrieval.py         # 召回基线评测（Hit@K/MRR；复用已入库样料，仍会调 embedding/rerank）
python scripts\score_distribution.py       # 只读向量余弦分数分布（不写库、不调 LLM；需先有入库向量）
python scripts\check_docx_pdf_ingest.py     # DOCX/PDF 真实 embedding 入库 + 向量命中验证，脚本内关 rerank、测完自动清理复原基线
python scripts\check_replan_live.py          # 动态多轮再规划门控轻测：默认离线零费用不触网；置 $env:REPLAN_LIVE="1" 才发起 1 次真实再决策、不写库
python scripts\assess_rerank_gain.py         # B档 Rerank 增益复评：内嵌10篇测试语料真实入库，OFF纯向量/ON重排双模式对比指标，自动清理复原基线、不改生产阈值
python scripts\check_maintenance.py          # C档运维能力：临时文档重解析事务替换切片 + HNSW REINDEX 验证；自动删除临时 DB 记录与本地文件，不重嵌入留存样料
```

> 轻量离线 CI 位于 `.github/workflows/ci.yml`：`backend-offline`（pytest + check_env + compileall）与 `frontend-build`（npm ci + vite build）；触发于 push/PR 到 main，刻意不纳入数据库、密钥与联网评测脚本，也不做部署发布；本地等价步骤由 `offline_regression.ps1` 覆盖。

> Agent 多轮：图链路为 工具 → assess → replan →（工具 | answer）。`MULTI_ROUND_ENABLED` 控制是否允许再决策回跳（默认 True；`LLM_REPLAN_ENABLED=False` 时 `plan_next` 恒收敛、行为与单轮一致、零额外 LLM 调用）；`LLM_REPLAN_ENABLED` 默认 False，置 True 后由 `replan_node` 调 LLM 产出带新 `args` 的决策、写回 `state.decision` 并回跳对应工具（开启会增加 LLM 决策调用）；轮次受 `MAX_TOOL_ROUNDS=3` 上限、最近工具失败不重试、`dedup` 重复键三重兜底；前端工具标签在第二轮及以上显示“· 第N轮”、首轮不显示；真实再决策门控轻测见 `scripts/check_replan_live.py`（默认离线零费用、`REPLAN_LIVE=1` 单次联网、不写库）。

> 启动顺序：先 PostgreSQL（`127.0.0.1:5432/rag_agent`）→ `python scripts\run_dev.py`（后端 8000）→ `npm run dev`（前端 5173，Vite 代理 `/api`→8000）。Windows 下后端勿裸 `uvicorn main:app`：Proactor 事件循环会导致 psycopg 异步连接异常，须经 `run_dev.py` 选择器循环启动。
>
> 数据边界：当前留存 4 篇样料（document/chunk=4/4）；测试产生的会话/记录应在用完后删除，权威计数保持 `session/record=0`。

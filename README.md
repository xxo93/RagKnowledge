# RagKnowledge · 企业文档智能问答 Agent（RAG + LangGraph 工具 Agent）

面向简历展示与本地运行的私有文档知识库 + 多工具 Agent Demo：覆盖 RAG 全链路（解析→清洗→切片→向量化→检索→生成→引用溯源）与 LangGraph 三工具 Agent（知识库检索 / 数学计算 / 文档统计），SSE 流式输出、会话历史持久化。

> 设计依据：[需求文档.md](./需求文档.md) V2.1（pgvector 单库版）、[架构设计文档.md](./架构设计文档.md)。
> 当前阶段：**骨架（M0 前框架搭建）**，目录分层、配置、数据层契约与纯函数能力已就位，业务链路按 M1→M6 逐步接通。

## 一、技术栈

| 层 | 技术 |
| --- | --- |
| 前端 | Vue3 + Element Plus + Vite（fetch 手写 SSE 解析） |
| 后端 | Python FastAPI + Uvicorn，分层单体（路由/服务/Agent/基础设施） |
| Agent | LangGraph 状态图 + 循环上限（默认 3 轮） |
| 存储 | PostgreSQL 16/17 + pgvector，业务表与向量表同库同事务，HNSW 余弦索引 |
| Embedding | BGE 系列（本地 / API / Ollama 可配，适配器模式） |
| LLM | OpenAI 兼容协议（DeepSeek 默认，可切豆包 / 通义 / OpenAI） |

明确不做：登录鉴权、多用户隔离、Docker 化项目、CI/CD、异步队列、OCR、Milvus 集群（见需求文档 §十一）。

## 二、目录结构

```
RagKnowledge/
├── 需求文档.md / 架构设计文档.md
├── README.md / .env.example / .gitignore
├── backend/
│   ├── main.py                 # FastAPI 入口（lifespan、CORS、路由注册、全局异常）
│   ├── config.py               # Pydantic Settings 统一配置
│   ├── db.py                   # 异步连接池 + register_vector + to_vector 包装(ADR-006)
│   ├── errors.py / logging.py / llm.py / prompts.py
│   ├── init_db.sql             # DDL（__EMBEDDING_DIM__ 占位符，幂等）
│   ├── scripts/init_db.py      # 渲染维度并执行建库建表
│   ├── schemas/ routers/ services/ agent(tools)/ tests/
├── frontend/                   # Vue3：api(sse)、composables、views、components
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
python -m scripts.init_db        # 幂等建扩展/表/HNSW 索引并回读校验
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

接口文档：<http://127.0.0.1:8000/docs>；健康检查：`GET /health`；配置元信息：`GET /api/system/info`。

### 3. 前端

```powershell
cd frontend
npm install
npm run dev        # http://127.0.0.1:5173 ，Vite 代理 /api 到 8000
```

## 四、里程碑与当前进度

| 里程碑 | 内容 | 状态 |
| --- | --- | --- |
| 骨架 | 目录、配置、连接池契约、路由骨架、纯函数（清洗/切片/计算器）、前端结构 | ✅ 本轮完成 |
| M0 | PG + pgvector 环境、建库建表建 HNSW、扩展版本验证 | ⬜ |
| M1 | 上传解析切片入库单事务链路、文档列表/删除 | ⬜ |
| M2 | 向量检索 SQL + Prompt + LLM 生成 + 引用来源 | ⬜ |
| M3 | LangGraph 状态图、三工具、意图路由、循环上限 | ⬜ |
| M4 | SSE 流式、会话与记录落库、历史回填 | ⬜ |
| M5 | 两页面 UI 与交互、来源卡片、中断恢复 | ⬜ |
| M6 | 调优、异常兜底、日志、README 收尾 | ⬜ |

> 逐阶段开发与验收记录见 [交付验收报告.md](./交付验收报告.md)。

## 五、关键安全与一致性设计（面试要点）

- **单库同事务**：切片向量与文档元数据同库，上传 T2 原子入库、删除显式事务清切片，无双写补偿；
- **SQL 全参数化**：统计工具枚举白名单、计算器正则 + AST 白名单（不使用 eval）；
- **向量维度三维约束**：模型输出维度 = `EMBEDDING_DIM` = 表列 `vector(N)`，启动与入库双重校验；
- **向量写入统一 `Vector(...)` 包装**：pgvector-python 未注册 list dumper，直传 list 会报类型错误（ADR-006）；
- 服务仅绑定 `127.0.0.1`，无鉴权为明确边界，不暴露局域网。

## 六、常用单测（无外部依赖）

```powershell
cd backend
python -m pytest tests/test_calc_tool.py tests/test_text_pipeline.py -v
```

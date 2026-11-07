# RagKnowage · 企业文档智能问答 Agent（RAG + LangGraph）

面向简历展示与本地运行的项目：私有文档知识库 + 多工具 Agent 调度，覆盖 RAG 全链路。
不做登录鉴权、不做容器化与 CI/CD，单用户单机运行。

## 技术栈

| 分层 | 选型 |
| --- | --- |
| 前端 | Vue3 + Element Plus + Axios（SSE 流式） |
| 后端 | Python 3.10+ + FastAPI + Uvicorn |
| Agent | LangGraph 状态图，LLM 自主判断意图并路由工具 |
| 向量存储 | PostgreSQL 16 + pgvector（HNSW 余弦索引），业务表与向量同库同事务 |
| Embedding | BGE 系列（本地）或 Embedding API，维度由 `EMBEDDING_DIM` 固定 |
| LLM | 大模型 API（豆包 / 通义千问 / OpenAI），Provider 可配置 |
| 文档解析 | PyPDF2 / python-docx / markdown |

## 当前状态

本仓库目前处于 **M0 需求与设计阶段**：

- ✅ 完整需求文档：[`需求文档.md`](./需求文档.md)（V2.1，pgvector 单库方案，含数据库设计、接口清单、验收标准、pgvector 安装附录）
- ✅ 学习笔记：[`Agent开发实战（从入门到落地）.md`](./Agent开发实战（从入门到落地）.md)
- ⏳ 后端 / 前端代码尚未实现，规划见下方目录结构

## 目录结构规划

```
RagKnowage/
├── 需求文档.md
├── README.md
├── .env.example                 # 待建
├── backend/
│   ├── main.py                  # FastAPI 入口、CORS、路由注册、启动校验向量维度
│   ├── config.py                # EMBEDDING_DIM、Top-K、切片参数等配置
│   ├── db.py                    # psycopg ConnectionPool + register_vector
│   ├── init_db.sql              # 建扩展、建表、建 HNSW 索引
│   ├── routers/                 # docs / session / chat(SSE)
│   ├── services/                # loader / splitter / embedder / vector_store / rag / memory
│   ├── agent/                   # graph / state / tools(search, calc, doc_stats)
│   └── requirements.txt
├── frontend/                    # Vue3 + Vite：知识库管理页、对话页
└── data/uploads/                # 原始文档（不入库）
```

## 接口一览（详见需求文档第七章）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/docs/upload` | 上传 PDF/DOCX/MD，同步解析切片入库 |
| GET | `/api/docs/list` | 文档分页列表 |
| DELETE | `/api/docs/{doc_id}` | 删除文档及切片向量（单事务） |
| POST | `/api/chat/session` | 新建会话 |
| GET | `/api/chat/session/list` | 会话列表 |
| DELETE | `/api/chat/session/{session_id}` | 删除会话及记录 |
| POST | `/api/chat/stream` | SSE 流式问答（tool / token / source / done / error） |
| GET | `/api/chat/record/{session_id}` | 会话历史记录 |
| GET | `/api/system/info` | 当前配置与文档总数 |

## 环境准备（摘要）

1. PostgreSQL 16 + pgvector 扩展，推荐 Docker：

```powershell
docker run -d --name pgvector -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=rag_agent -p 5432:5432 pgvector/pgvector:pg16
```

2. 目标库执行 `CREATE EXTENSION IF NOT EXISTS vector;`，用 `SELECT extversion FROM pg_extension WHERE extname='vector';` 验证。
3. Python 侧：`pip install "psycopg[binary,pool]" pgvector`，连接建立后 `register_vector(conn)` 才能直接写 `list[float]`。

完整安装步骤（Docker / apt / dnf / 源码编译）与踩坑清单见需求文档附录 A。

## 注意

- `.env`、`data/uploads/` 等本地产物与密钥已在 `.gitignore` 中排除。
- `查询余额.py` 因内含硬编码 API Key，未纳入版本管理。

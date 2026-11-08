-- ============================================================================
-- RagKnowledge 数据库初始化 DDL（PostgreSQL + pgvector，单库 rag_agent）
-- 说明：
-- 1. PG 的 DDL 不支持变量，向量维度由 scripts/init_db.py 读取 EMBEDDING_DIM 后
--    替换占位符 __EMBEDDING_DIM__ 再执行（需求 §6.2 / 架构 §5.4）；
-- 2. 全部对象使用 IF NOT EXISTS，脚本可重复执行（幂等）；
-- 3. 索引选型：HNSW + vector_cosine_ops（余弦距离，与检索算子 <=> 匹配）。
-- ============================================================================

-- 扩展：向量 + 可选模糊匹配
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------- 文档表 document ----------
CREATE TABLE IF NOT EXISTS document (
  doc_id       BIGSERIAL PRIMARY KEY,
  doc_name     VARCHAR(255) NOT NULL,
  file_path    VARCHAR(512) NOT NULL,
  file_size    BIGINT       NOT NULL DEFAULT 0,
  chunk_count  INT          NOT NULL DEFAULT 0,
  status       SMALLINT     NOT NULL DEFAULT 0,   -- 0 处理中 1 成功 2 失败
  error_msg    VARCHAR(512) DEFAULT NULL,
  create_time  TIMESTAMP    NOT NULL DEFAULT now()
);
COMMENT ON TABLE document IS '文档元信息';

-- ---------- 切片向量表 document_chunk（核心）----------
CREATE TABLE IF NOT EXISTS document_chunk (
  chunk_id     BIGSERIAL PRIMARY KEY,
  doc_id       BIGINT NOT NULL REFERENCES document(doc_id) ON DELETE CASCADE,
  chunk_index  INT    NOT NULL,
  content      TEXT   NOT NULL,
  embedding    vector(__EMBEDDING_DIM__) NOT NULL,
  metadata     JSONB  DEFAULT NULL,
  create_time  TIMESTAMP NOT NULL DEFAULT now()
);
COMMENT ON TABLE document_chunk IS '文本切片与向量';

-- 按文档过滤（删除、指定文档问答）
CREATE INDEX IF NOT EXISTS idx_chunk_doc_id ON document_chunk (doc_id);

-- 向量近似检索（余弦距离，HNSW）；算子类必须与 <=> 一致：vector_cosine_ops
CREATE INDEX IF NOT EXISTS idx_chunk_embedding ON document_chunk
  USING hnsw (embedding vector_cosine_ops);

-- ---------- 会话表 chat_session ----------
CREATE TABLE IF NOT EXISTS chat_session (
  session_id    BIGSERIAL PRIMARY KEY,
  session_name  VARCHAR(128) NOT NULL DEFAULT '新会话',
  create_time   TIMESTAMP NOT NULL DEFAULT now()
);
COMMENT ON TABLE chat_session IS '会话';

-- ---------- 对话记录表 chat_record ----------
CREATE TABLE IF NOT EXISTS chat_record (
  record_id       BIGSERIAL PRIMARY KEY,
  session_id      BIGINT NOT NULL REFERENCES chat_session(session_id) ON DELETE CASCADE,
  query           TEXT   NOT NULL,
  answer          TEXT   NOT NULL,
  tool_call_info  JSONB  DEFAULT NULL,
  source_ref      JSONB  DEFAULT NULL,
  create_time     TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_session_id ON chat_record (session_id);
COMMENT ON TABLE chat_record IS '对话记录';

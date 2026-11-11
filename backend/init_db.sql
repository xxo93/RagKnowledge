-- ============================================================================
-- RagKnowledge 数据库初始化 DDL（PostgreSQL + pgvector，单库 rag_agent）
-- 说明：
-- 1. PG 的 DDL 不支持变量，向量维度由 scripts/init_db.py 读取 EMBEDDING_DIM 后
--    替换占位符 __EMBEDDING_DIM__ 再执行（需求 §6.2 / 架构 §5.4）；
-- 2. 全部对象使用 IF NOT EXISTS，脚本可重复执行（幂等）；
-- 3. 索引选型：HNSW + vector_cosine_ops（余弦距离，与检索算子 <=> 匹配）；
-- 4. 多用户：app_user / auth_session / operation_log 与 chat_session.user_id。
--    全新安装直接带 NOT NULL 与外键，历史库由 scripts/migrate_auth_multitenant.py
--    回填后补约束（本文件中的 ALTER ... IF NOT EXISTS 仅用于兼容旧库）。
-- 5. 注意：scripts/init_db.py 按 ";" 拆分语句，这里不要写 DO $$ ... $$ 块。
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
  summary      TEXT         DEFAULT NULL,
  create_time  TIMESTAMP    NOT NULL DEFAULT now()
);
ALTER TABLE document ADD COLUMN IF NOT EXISTS summary TEXT;
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

-- ---------- 账号与登录会话（多用户隔离，设计 §5.1 / §5.2）----------
CREATE TABLE IF NOT EXISTS app_user (
  user_id       BIGSERIAL PRIMARY KEY,
  username      VARCHAR(64)  NOT NULL UNIQUE,
  display_name  VARCHAR(128) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  role          VARCHAR(16)  NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
  status        SMALLINT     NOT NULL DEFAULT 1,   -- 1 启用 0 禁用
  created_at    TIMESTAMP    NOT NULL DEFAULT now(),
  last_login_at TIMESTAMP   NULL
);
COMMENT ON TABLE app_user IS '本地账号（密码只存哈希）';
CREATE INDEX IF NOT EXISTS idx_app_user_username ON app_user (username);

CREATE TABLE IF NOT EXISTS auth_session (
  token_hash  CHAR(64)   PRIMARY KEY,             -- SHA-256(不透明令牌)
  user_id     BIGINT     NOT NULL REFERENCES app_user(user_id) ON DELETE CASCADE,
  expires_at  TIMESTAMP  NOT NULL,
  revoked_at  TIMESTAMP  NULL,
  created_at  TIMESTAMP  NOT NULL DEFAULT now(),
  user_agent  TEXT       NULL,
  client_ip   VARCHAR(64) NULL
);
COMMENT ON TABLE auth_session IS '登录会话（只存令牌哈希）';
CREATE INDEX IF NOT EXISTS idx_auth_session_user ON auth_session (user_id);

-- ---------- 操作审计（只记管理/权限操作，不记聊天内容，设计 §5.4）----------
CREATE TABLE IF NOT EXISTS operation_log (
  log_id        BIGSERIAL PRIMARY KEY,
  actor_user_id BIGINT       NULL,
  action        VARCHAR(64)  NOT NULL,            -- AUTH_LOGIN/DOC_UPLOAD/DOC_DELETE/REPARSE/REINDEX/USER_MANAGE
  target_type   VARCHAR(32)  NULL,
  target_id     VARCHAR(64)  NULL,
  result        SMALLINT     NOT NULL DEFAULT 1,  -- 1 成功 0 失败
  detail        JSONB        NULL,
  created_at    TIMESTAMP    NOT NULL DEFAULT now()
);
COMMENT ON TABLE operation_log IS '管理操作审计（不含聊天内容）';
CREATE INDEX IF NOT EXISTS idx_operation_log_actor ON operation_log (actor_user_id);
CREATE INDEX IF NOT EXISTS idx_operation_log_created ON operation_log (created_at DESC);

-- ---------- 会话表 chat_session（按用户私有）----------
CREATE TABLE IF NOT EXISTS chat_session (
  session_id    BIGSERIAL PRIMARY KEY,
  user_id       BIGINT NOT NULL CONSTRAINT fk_chat_session_user
                REFERENCES app_user(user_id) ON DELETE CASCADE,
  session_name  VARCHAR(128) NOT NULL DEFAULT '新会话',
  create_time   TIMESTAMP NOT NULL DEFAULT now()
);
COMMENT ON TABLE chat_session IS '会话（按 user_id 私有）';

-- 旧库升级：先加可空列，回填后由迁移脚本补外键与 NOT NULL
ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS user_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_chat_session_user_time ON chat_session (user_id, session_id DESC);

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

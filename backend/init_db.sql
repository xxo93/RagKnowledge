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

-- ---------- 会话记忆（v3 / WP1）----------
-- summary：滚动摘要正文（≤400 字）；summarized_until_record_id：已摘要到的 record_id 水位；
-- memory_enabled：临时会话开关，false 时该会话不注入事实/摘要、也不再自动抽取
ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS summary TEXT DEFAULT NULL;
ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS summarized_until_record_id BIGINT DEFAULT NULL;
ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS memory_enabled BOOLEAN NOT NULL DEFAULT TRUE;
-- 会话置顶 / 归档（v3 / M8 WP5-9）：last_active_at 用于自动归档判定
ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS is_pinned BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP DEFAULT NULL;
ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMP DEFAULT NULL;
CREATE INDEX IF NOT EXISTS idx_chat_session_active
  ON chat_session (user_id, archived_at, is_pinned, last_active_at DESC);

-- 会话事实：kind 限 constraint/fact/entity/todo；同一会话同一 (kind, fact_key) 只允许一条 active；
-- invalidated_at 非空 = 被新版本覆盖或用户删除，保留历史但不注入上下文。
CREATE TABLE IF NOT EXISTS session_memory (
  mem_id          BIGSERIAL PRIMARY KEY,
  session_id      BIGINT NOT NULL REFERENCES chat_session(session_id) ON DELETE CASCADE,
  user_id         BIGINT NOT NULL REFERENCES app_user(user_id) ON DELETE CASCADE,
  kind            VARCHAR(16) NOT NULL,
  fact_key        VARCHAR(64) NOT NULL,
  fact_value      TEXT NOT NULL,
  confidence      DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  source_record_id BIGINT NULL,
  invalidated_at  TIMESTAMP NULL DEFAULT NULL,
  create_time     TIMESTAMP NOT NULL DEFAULT now(),
  update_time     TIMESTAMP NOT NULL DEFAULT now(),
  CONSTRAINT ck_session_memory_kind CHECK (kind IN ('constraint', 'fact', 'entity', 'todo'))
);
CREATE INDEX IF NOT EXISTS idx_session_memory_active
  ON session_memory (session_id, kind, update_time DESC) WHERE invalidated_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_session_memory_active_key
  ON session_memory (session_id, kind, fact_key) WHERE invalidated_at IS NULL;
-- 口径变更提示（v3 / M8 WP5-8）：同 key 被改写时保留上一版值与变更时间
ALTER TABLE session_memory ADD COLUMN IF NOT EXISTS previous_value TEXT DEFAULT NULL;
ALTER TABLE session_memory ADD COLUMN IF NOT EXISTS value_changed_at TIMESTAMP DEFAULT NULL;
COMMENT ON TABLE session_memory IS '会话事实（结构化长期记忆，按会话隔离）';

CREATE INDEX IF NOT EXISTS idx_chat_session_user_time ON chat_session (user_id, session_id DESC);

-- ---------- 对话记录表 chat_record ----------
CREATE TABLE IF NOT EXISTS chat_record (
  record_id       BIGSERIAL PRIMARY KEY,
  session_id      BIGINT NOT NULL REFERENCES chat_session(session_id) ON DELETE CASCADE,
  query           TEXT   NOT NULL,
  answer          TEXT   NOT NULL,
  tool_call_info  JSONB  DEFAULT NULL,
  source_ref      JSONB  DEFAULT NULL,
  -- 本条回答走了哪条能力链路：knowledge / web / fallback_guide / fallback_contact / tool / chat / biz_query
  answer_mode     VARCHAR(16) NOT NULL DEFAULT 'knowledge',
  -- 业务库查询的表格帧（列定义 + 已脱敏行 + 下载链接），供历史回放还原表格卡；非业务查询为 NULL
  table_payload   JSONB  DEFAULT NULL,
  -- v3 / WP2：意图分类结果（knowledge / biz_query / calculator / doc_stats / general / workflow / clarify）
  intent          VARCHAR(32) DEFAULT NULL,
  create_time     TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_session_id ON chat_record (session_id);
COMMENT ON TABLE chat_record IS '对话记录';
-- 旧库升级（M3）：已存在的 chat_record 补列，历史行默认按 knowledge 处理
ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS answer_mode VARCHAR(16) NOT NULL DEFAULT 'knowledge';
-- 旧库升级（M5+）：补表格帧列，历史行保持 NULL（回放时只显示文字汇总）
ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS table_payload JSONB DEFAULT NULL;
-- 旧库升级（v3 / M2）：补意图列，历史行保持 NULL（读取时按 answer_mode 反推）
ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS intent VARCHAR(32) DEFAULT NULL;

-- ---------- 会话记忆（v3 / WP1）----------
-- answer_digest：答案摘要（头 300 字 + 尾 150 字），供后续轮次低成本注入；
-- prompt_tokens / completion_tokens：模型用量，M3 I/O 日志与 M1 上下文观测共用
ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS answer_digest TEXT DEFAULT NULL;
ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS prompt_tokens INT DEFAULT NULL;
ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS completion_tokens INT DEFAULT NULL;

-- ---------- 运行时配置中心表 app_config（M1）----------
-- 只存被管理员显式改过的参数；未出现的 key 一律回退 .env / 代码默认值。
-- 取值优先级：app_config > .env > 代码默认；「恢复默认」= 删除对应行。
CREATE TABLE IF NOT EXISTS app_config (
  key          VARCHAR(64) PRIMARY KEY,
  value_json   JSONB       NOT NULL,
  group_name   VARCHAR(32) NOT NULL,
  value_type   VARCHAR(16) NOT NULL,              -- int / float / bool / str / enum
  default_json JSONB       NULL,
  min_value    DOUBLE PRECISION NULL,
  max_value    DOUBLE PRECISION NULL,
  updated_by   BIGINT      NULL REFERENCES app_user(user_id) ON DELETE SET NULL,
  updated_at   TIMESTAMP   NOT NULL DEFAULT now()
);
COMMENT ON TABLE app_config IS '运行时配置（热生效，仅存显式改过的项）';
CREATE INDEX IF NOT EXISTS idx_app_config_group ON app_config (group_name);

-- ---------- 大结果导出令牌 export_token（M5）----------
-- 只存 token 的 SHA-256，文件落在 data/exports/；expires_at 为 NULL 表示永不过期，
-- 有效期内可重复下载；仅“设置过有效期且已过期”的记录由后台任务清理。
CREATE TABLE IF NOT EXISTS export_token (
  token_hash    CHAR(64) PRIMARY KEY,
  user_id       BIGINT      NOT NULL REFERENCES app_user(user_id) ON DELETE CASCADE,
  file_path     VARCHAR(512) NOT NULL,
  filename      VARCHAR(255) NOT NULL,
  row_count     INT         NOT NULL DEFAULT 0,
  expires_at    TIMESTAMP   NULL,
  downloaded_at TIMESTAMP   NULL,
  created_at    TIMESTAMP   NOT NULL DEFAULT now()
);
COMMENT ON TABLE export_token IS '业务查询 CSV 导出令牌（仅存哈希，绑定用户 + 可选有效期）';
CREATE INDEX IF NOT EXISTS idx_export_token_expires ON export_token (expires_at);

-- ---------- 输入输出运行日志 chat_io_log / llm_call_log（v3 / WP3）----------
-- 与 operation_log（审计）隔离：这里记录聊天输入输出，用于排障与质量分析，
-- 默认保留 90 天（配置中心可调），用户可导出/删除自己的日志。
CREATE TABLE IF NOT EXISTS chat_io_log (
  log_id            BIGSERIAL PRIMARY KEY,
  trace_id          VARCHAR(32)  NOT NULL,
  user_id           BIGINT       NULL,
  session_id        BIGINT       NULL,
  record_id         BIGINT       NULL,
  turn_index        INT          NULL,
  intent            VARCHAR(32)  NULL,
  answer_mode       VARCHAR(16)  NULL,
  query             TEXT         NOT NULL,
  answer            TEXT         NULL,
  status            VARCHAR(16)  NOT NULL,
  error_code        VARCHAR(48)  NULL,
  error_msg         VARCHAR(512) NULL,
  model             VARCHAR(64)  NULL,
  prompt_tokens     INT          NULL,
  completion_tokens INT          NULL,
  total_tokens      INT          NULL,
  first_token_ms    INT          NULL,
  elapsed_ms        INT          NULL,
  frame_count       INT          NULL,
  tool_chain        JSONB        NULL,
  context_usage     JSONB        NULL,
  ua                VARCHAR(256) NULL,
  ip_masked         VARCHAR(64)  NULL,
  created_at        TIMESTAMP    NOT NULL DEFAULT now()
);
COMMENT ON TABLE chat_io_log IS '一轮问答一条运行日志（ok/abort/error/timeout/rejected），不参与业务判断';
CREATE INDEX IF NOT EXISTS idx_io_log_user_time ON chat_io_log (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_io_log_trace ON chat_io_log (trace_id);
CREATE INDEX IF NOT EXISTS idx_io_log_status ON chat_io_log (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_io_log_session ON chat_io_log (session_id);

CREATE TABLE IF NOT EXISTS llm_call_log (
  call_id           BIGSERIAL PRIMARY KEY,
  trace_id          VARCHAR(32) NOT NULL,
  user_id           BIGINT      NULL,
  purpose           VARCHAR(24) NOT NULL,
  model             VARCHAR(64) NOT NULL,
  temperature       REAL        NULL,
  prompt_tokens     INT         NULL,
  completion_tokens INT         NULL,
  latency_ms        INT         NULL,
  status            VARCHAR(16) NOT NULL,
  error_code        VARCHAR(48) NULL,
  created_at        TIMESTAMP   NOT NULL DEFAULT now()
);
COMMENT ON TABLE llm_call_log IS '单次 LLM 调用一条日志，按 trace_id 与问答轮次关联，用于 token 对账';
CREATE INDEX IF NOT EXISTS idx_llm_log_trace ON llm_call_log (trace_id);
CREATE INDEX IF NOT EXISTS idx_llm_log_purpose ON llm_call_log (purpose, created_at DESC);


-- ============================================================
-- v3 / WP4：工作流任务（planning → running → finished）
-- ============================================================
CREATE TABLE IF NOT EXISTS workflow_task (
  task_id        BIGSERIAL PRIMARY KEY,
  user_id        BIGINT       NOT NULL,
  session_id     BIGINT       NULL,
  goal           TEXT         NOT NULL,
  plan_json      JSONB        NOT NULL DEFAULT '{}'::jsonb,
  status         VARCHAR(24)  NOT NULL DEFAULT 'planning',
  current_step   INT          NOT NULL DEFAULT 0,
  budget_json    JSONB        NULL,
  used_json      JSONB        NULL,
  result_summary TEXT         NULL,
  error_msg      TEXT         NULL,
  trace_id       VARCHAR(32)  NULL,
  created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
  finished_at    TIMESTAMPTZ  NULL
);
COMMENT ON TABLE workflow_task IS '多步任务：计划、状态、预算与最终报告；启动时 running 置 interrupted';
CREATE INDEX IF NOT EXISTS idx_workflow_task_user ON workflow_task (user_id, task_id DESC);

CREATE TABLE IF NOT EXISTS workflow_step (
  step_id          BIGSERIAL PRIMARY KEY,
  task_id          BIGINT       NOT NULL REFERENCES workflow_task(task_id) ON DELETE CASCADE,
  step_index       INT          NOT NULL,
  title            TEXT         NOT NULL,
  tool             VARCHAR(32)  NOT NULL,
  args_json        JSONB        NOT NULL DEFAULT '{}'::jsonb,
  depends_on       INT[]        NOT NULL DEFAULT '{}',
  status           VARCHAR(20)  NOT NULL DEFAULT 'pending',
  requires_confirm BOOLEAN      NOT NULL DEFAULT false,
  expected         TEXT         NULL,
  output_json      JSONB        NULL,
  error_msg        TEXT         NULL,
  retry_count      INT          NOT NULL DEFAULT 0,
  started_at       TIMESTAMPTZ  NULL,
  finished_at      TIMESTAMPTZ  NULL,
  UNIQUE (task_id, step_index)
);
COMMENT ON TABLE workflow_step IS '任务步骤：顺序、依赖、状态、输出（8KB 截断）与重试次数';
CREATE INDEX IF NOT EXISTS idx_workflow_step_task ON workflow_step (task_id, step_index);

ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS workflow_task_id BIGINT DEFAULT NULL;
-- ---------- 回答反馈 / 评测集（v3 / WP5-3）----------
-- 赞/踩与原因结构化留存；同一条回答同一用户只保留一条，重复提交覆盖更新。
CREATE TABLE IF NOT EXISTS chat_feedback (
  feedback_id       BIGSERIAL PRIMARY KEY,
  record_id         BIGINT       NOT NULL,
  session_id        BIGINT       NOT NULL,
  user_id           BIGINT       NOT NULL,
  trace_id          VARCHAR(32)  NULL,
  rating            SMALLINT     NOT NULL,
  reason_code       VARCHAR(32)  NULL,
  reason_text       VARCHAR(500) NULL,
  query_snapshot    TEXT         NULL,
  answer_snapshot   TEXT         NULL,
  intent            VARCHAR(32)  NULL,
  answer_mode       VARCHAR(16)  NULL,
  sources_json      JSONB        NULL,
  context_json      JSONB        NULL,
  created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
  UNIQUE (record_id, user_id)
);
COMMENT ON TABLE chat_feedback IS '回答赞/踩反馈，直接作为评测集样本（WP5-3）';
CREATE INDEX IF NOT EXISTS idx_feedback_rating_time ON chat_feedback (rating, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_user_time ON chat_feedback (user_id, created_at DESC);
-- ============================================================
-- v3 / WP5-6：每用户每分钟请求窗口（原子 UPSERT 自增，跨进程有效）
-- ============================================================
CREATE TABLE IF NOT EXISTS chat_rate_window (
  user_id       BIGINT      NOT NULL,
  window_start  TIMESTAMP   NOT NULL,
  request_count INTEGER     NOT NULL DEFAULT 0,
  updated_at    TIMESTAMP   NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, window_start)
);
COMMENT ON TABLE chat_rate_window IS '每用户自然分钟请求计数，超限请求也计数（WP5-6）';

-- ============================================================
-- v3 / WP5-7：提示词版本化 + 灰度开关
-- 同一 name 仅一条 is_active=true；gray_percent 为当前版本灰度比例
-- ============================================================
CREATE TABLE IF NOT EXISTS prompt_template (
  prompt_id    BIGSERIAL PRIMARY KEY,
  name         VARCHAR(64)  NOT NULL,
  version      INTEGER      NOT NULL,
  content      TEXT         NOT NULL,
  gray_percent SMALLINT     NOT NULL DEFAULT 100,
  is_active    BOOLEAN      NOT NULL DEFAULT FALSE,
  created_by   BIGINT       NULL,
  created_at   TIMESTAMP    NOT NULL DEFAULT now(),
  updated_at   TIMESTAMP    NOT NULL DEFAULT now(),
  UNIQUE (name, version)
);
COMMENT ON TABLE prompt_template IS '提示词版本与灰度（WP5-7）；默认版本由 prompt_store 启动时补齐';
CREATE INDEX IF NOT EXISTS idx_prompt_name_active ON prompt_template (name, is_active);

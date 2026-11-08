# PostgreSQL 17 + pgvector 本地安装指南（Windows）

> 本文档基于**本机真实安装过程**整理，所有命令均可复制执行，文末附实际执行记录与验收清单。
> 编写日期：2026-11-07

---

## 一、最终安装结果

| 项目 | 值 |
| --- | --- |
| 操作系统 | Windows 11 家庭版 中文版（10.0.26200） |
| 主机名 | DESKTOP-CME6DBO |
| PostgreSQL 版本 | **17.11**（安装包 17.11-5，x86_64-windows，msvc-19.44 编译） |
| pgvector 版本 | **0.8.6** |
| 安装目录 | `C:\Program Files\PostgreSQL\17` |
| 数据目录 | `C:\Program Files\PostgreSQL\17\data` |
| 监听端口 | **5432**（`listen_addresses = '*'`） |
| 服务名 | `postgresql-x64-17`（状态 Running，启动类型 Automatic） |
| 超级用户 / 密码 | `postgres` / `postgres`（winget 静默安装默认密码） |
| pgAdmin | 已随包安装（`C:\Program Files\PostgreSQL\17\pgAdmin 4`） |
| 测试库 | `pgvector_test`（已启用 vector 扩展，含演示表 `items`） |

**轻量版一分钟命令（适用于全新机器）**

```powershell
# 1) 安装 PostgreSQL 17（静默、默认密码 postgres、自动创建服务）
winget install --id PostgreSQL.PostgreSQL.17 -e --source winget --silent `
  --accept-package-agreements --accept-source-agreements

# 2) 下载与本机 PG 大版本匹配的 pgvector 预编译包（直连 GitHub 易超时，用镜像）
curl.exe -L -o "$env:TEMP\vector.v0.8.6-pg17.zip" `
  "https://ghfast.top/https://github.com/andreiramani/pgvector_pgsql_windows/releases/download/0.8.6_17/vector.v0.8.6-pg17.zip"

# 3) 停服务 → 解压到 PG 安装目录 → 启服务（需管理员权限）
Expand-Archive -LiteralPath "$env:TEMP\vector.v0.8.6-pg17.zip" -DestinationPath "$env:TEMP\pgvector" -Force
Stop-Service postgresql-x64-17
Copy-Item "$env:TEMP\pgvector\lib\vector.dll" "C:\Program Files\PostgreSQL\17\lib\" -Force
Copy-Item "$env:TEMP\pgvector\share\extension\*" "C:\Program Files\PostgreSQL\17\share\extension\" -Force
Start-Service postgresql-x64-17

# 4) 启用扩展
$env:PGPASSWORD='postgres'
psql -U postgres -h 127.0.0.1 -c "CREATE EXTENSION vector;"
```

> 提示：第 3 步写 `C:\Program Files` 必须管理员权限；静默安装与 pgAdmin 解包较慢，整机耗时约 25–30 分钟。

---

## 二、安装前检查

```powershell
# 是否已装过 PostgreSQL（有则先卸载或换端口，勿直接覆盖安装）
Get-Service | Where-Object { $_.Name -like '*postgres*' }
Test-Path 'C:\Program Files\PostgreSQL'
Get-Command psql -ErrorAction SilentlyContinue

# 5432 端口占用情况（被占用则安装时改用 5433）
Get-NetTCPConnection -LocalPort 5432 -State Listen -ErrorAction SilentlyContinue

# 包管理器版本
winget --version
```

本机检查结果：无任何 PostgreSQL 残留、5432 未被占用、winget v1.29.380 可用 → 可直接安装。

**注意**：本机未安装 Visual Studio / MSVC 生成工具，因此选择**预编译 pgvector 二进制包**方案；若需自行编译，参见第七章。

---

## 三、安装 PostgreSQL 17

### 3.1 安装命令

```powershell
winget install --id PostgreSQL.PostgreSQL.17 -e --source winget --silent `
  --accept-package-agreements --accept-source-agreements
```

执行过程说明：

1. winget 从 `https://get.enterprisedb.com/postgresql/postgresql-17.11-5-windows-x64.exe` 下载安装包（约 356 MB）并校验 SHA256；
2. **会弹出 UAC 管理员授权窗口**，需手动点"是"——这是唯一需要人工干预的一步；
3. 安装器以 unattended 模式运行：默认装到 `C:\Program Files\PostgreSQL\17`、默认密码 `postgres`、默认端口 5432、自动注册服务并设开机自启；
4. 同时安装 pgAdmin 4 与 Stack Builder（pgAdmin 的 Python 依赖解包最慢）。

### 3.2 安装后校验

```powershell
Get-Service postgresql-x64-17 | Select-Object Name, Status, StartType
Get-NetTCPConnection -LocalPort 5432 -State Listen
& 'C:\Program Files\PostgreSQL\17\bin\psql.exe' --version
```

实测输出：

```
Name               Status   StartType
postgresql-x64-17  Running  Automatic

LocalAddress
------------
::
0.0.0.0

psql (PostgreSQL) 17.11
```

### 3.3 连接自测

```powershell
$env:PGPASSWORD = 'postgres'
psql -U postgres -h 127.0.0.1 -p 5432 -d postgres -c "select version();"
```

```
PostgreSQL 17.11 on x86_64-windows, compiled by msvc-19.44.35229, 64-bit
```

---

## 四、安装 pgvector 扩展

pgvector 不在 PostgreSQL 官方 Windows 安装包内，需单独提供三样东西：

| 文件 | 作用 | 目标位置 |
| --- | --- | --- |
| `vector.dll` | 扩展的动态库（C 实现） | `C:\Program Files\PostgreSQL\17\lib\` |
| `vector.control` + `vector*.sql` | 扩展定义与建表脚本（共 43 个文件） | `C:\Program Files\PostgreSQL\17\share\extension\` |
| `halfvec.h` / `sparsevec.h` / `vector.h` | 可选，供 C 扩展开发引用 | `C:\Program Files\PostgreSQL\17\include\server\extension\vector\` |

### 4.1 获取预编译包（关键：版本必须匹配 PG 大版本）

社区维护仓库 `andreiramani/pgvector_pgsql_windows` 提供官方源码的 Windows x64 编译产物，命名规则为 `vector.v{pgvector版本}-pg{PG大版本}.zip`。

```powershell
# PG 17 对应 vector.v0.8.6-pg17.zip；PG 16 → pg16；PG 18 → pg18
curl.exe -L --retry 2 --connect-timeout 15 `
  -o "$env:TEMP\vector.v0.8.6-pg17.zip" `
  "https://ghfast.top/https://github.com/andreiramani/pgvector_pgsql_windows/releases/download/0.8.6_17/vector.v0.8.6-pg17.zip"
```

> **实测坑点**：本机直连 `https://github.com/.../releases/download/...` 三次连接超时（curl 报 `(28) Connection timed out` / `(56)`），走 `ghfast.top` 镜像 6 秒下载完成（169,689 字节）。备用镜像：`https://gh-proxy.com/https://github.com/...`。
> 也可用 GitHub API 查看全部可用版本：`curl.exe -sL https://api.github.com/repos/andreiramani/pgvector_pgsql_windows/releases`

### 4.2 部署文件（必须管理员权限，且先停服务）

`C:\Program Files` 写字需要提权，`vector.dll` 在服务运行时会占用。做法：停服务 → 复制 → 启服务，整段放进以管理员身份运行的 PowerShell。

```powershell
# 以管理员身份运行（会弹 UAC）
Stop-Service postgresql-x64-17 -Force
Start-Sleep -Seconds 2

# lib
Copy-Item "$env:TEMP\pgvector\lib\vector.dll" 'C:\Program Files\PostgreSQL\17\lib\' -Force
# share\extension（43 个文件全量复制）
Copy-Item "$env:TEMP\pgvector\share\extension\*" 'C:\Program Files\PostgreSQL\17\share\extension\' -Force
# include（可选）
$inc = 'C:\Program Files\PostgreSQL\17\include\server\extension\vector'
New-Item -ItemType Directory -Path $inc -Force | Out-Null
Copy-Item "$env:TEMP\pgvector\include\server\extension\vector\*.h" $inc -Force

Start-Service postgresql-x64-17
Get-Service postgresql-x64-17
```

也可以直接用"解压覆盖"的方式：把 zip 解压后整体合并进 `C:\Program Files\PostgreSQL\17`（目录结构与 PG 安装目录一一对应，官方 readme 即是此做法）。

### 4.3 部署校验

```powershell
(Get-ChildItem 'C:\Program Files\PostgreSQL\17\lib' -Filter 'vector*')   # vector.dll 279,552 字节
(Get-ChildItem 'C:\Program Files\PostgreSQL\17\share\extension' -Filter 'vector*' | Measure-Object).Count  # 43
```

### 4.4 配置 PATH（便于随处使用 psql）

```powershell
# 管理员权限；仅机器级 PATH 持久化，不影响已有终端
$mp = [Environment]::GetEnvironmentVariable('Path','Machine')
if ($mp -notmatch [regex]::Escape('PostgreSQL\17\bin')) {
    [Environment]::SetEnvironmentVariable('Path', $mp.TrimEnd(';') + ';C:\Program Files\PostgreSQL\17\bin', 'Machine')
}
```

> **注意**：PATH 是写入注册表（Machine 级）的，**已打开的终端/PowerShell 不会立刻生效**，必须新开一个窗口才认 `psql`。在此之前请用全路径 `C:\Program Files\PostgreSQL\17\bin\psql.exe`。

---

## 五、启用扩展并验证

### 5.1 建测试库并启用扩展

```powershell
$env:PGPASSWORD = 'postgres'
psql -U postgres -h 127.0.0.1 -p 5432 -d postgres -c "CREATE DATABASE pgvector_test;"
psql -U postgres -h 127.0.0.1 -p 5432 -d pgvector_test -c "CREATE EXTENSION IF NOT EXISTS vector;"
psql -U postgres -h 127.0.0.1 -p 5432 -d pgvector_test -c "SELECT extname, extversion FROM pg_extension WHERE extname='vector';"
```

实测：

```
 extname | extversion
---------+------------
 vector  | 0.8.6
```

扩展是**按库启用**的：每个需要向量能力的 database 都要各自执行一次 `CREATE EXTENSION vector;`。

### 5.2 建表 + 相似度检索

```sql
CREATE TABLE items (id bigserial PRIMARY KEY, embedding vector(3));
INSERT INTO items (embedding) VALUES ('[1,2,3]'),('[4,5,6]'),('[7,8,9]'),('[1,2,4]');

-- L2 距离升序取最近 3 条
SELECT id, embedding::text AS emb, round((embedding <-> '[3,1,2]')::numeric, 4) AS l2_dist
FROM items ORDER BY embedding <-> '[3,1,2]' LIMIT 3;
```

实测输出（`[1,2,3]` 距查询点最近，符合预期）：

```
 id |   emb   | l2_dist
----+---------+---------
  1 | [1,2,3] |  2.4495
  4 | [1,2,4] |  3.0000
  2 | [4,5,6] |  5.7446
```

### 5.3 建 HNSW 索引并确认走索引

```sql
CREATE INDEX items_hnsw_idx ON items USING hnsw (embedding vector_l2_ops);
SET enable_seqscan = off;
EXPLAIN SELECT id FROM items ORDER BY embedding <-> '[3,1,2]' LIMIT 3;
```

```
 Limit  (cost=8.03..11.07 rows=3 width=16)
   ->  Index Scan using items_hnsw_idx on items  (cost=8.03..12.08 rows=4 width=16)
         Order By: (embedding <-> '[3,1,2]'::vector)
```

### 5.4 数据类型与函数验证

```sql
SELECT vector_dims('[1,2,3]'::vector)  AS dims,      -- 3
       vector_norm('[3,4]'::vector)    AS norm,      -- 5.00
       '[1,2,3]'::halfvec(3)           AS halfvec,   -- 半精度，省一半内存
       '{1:1,3:3}/5'::sparsevec        AS sparsevec, -- 稀疏向量，格式 {位置:值}/维度
       binary_quantize('[1,2,3,4]'::vector) AS bq;   -- 1111，二值量化
SELECT '[1,2,3]'::vector <=> '[1,2,9]'::vector AS cosine_dist;  -- 0.0778
```

以上全部通过，说明 `vector / halfvec / sparsevec / binary_quantize` 均可用。

> **注意**：`vector_dims('[1,2,3]')` 不带 `::vector` 转换会报错
> `函数 vector_dims(unknown) 不是唯一的`——因为 0.8.6 里该函数同时接受 vector/halfvec/sparsevec，必须显式指定类型。

---

## 六、常用操作速查

### 6.1 距离运算符

| 运算符 | 含义 | 适用索引 ops |
| --- | --- | --- |
| `<->` | L2 / 欧氏距离 | `vector_l2_ops` |
| `<#>` | 负内积（值越小越相似） | `vector_ip_ops` |
| `<=>` | 余弦距离（1 - 余弦相似度） | `vector_cosine_ops` |
| `<+>` | L1 / 曼哈顿距离（0.7+） | `vector_l1_ops` |
| `<%>` | 汉明距离（二值向量） | `bit_hamming_ops` |
| `Jaccard` | `<%>` / `jaccard_distance`（稀疏二值） | — |

文本 Embedding 常用 `<=>` + `vector_cosine_ops`；已做过归一化的向量用 `<#>` + `vector_ip_ops` 更快。

### 6.2 索引选型

| 索引 | 建索引速度 | 查询速度 | 内存 | 适用 |
| --- | --- | --- | --- | --- |
| HNSW | 慢 | 快 | 高 | 默认首选，召回稳定，支持增量插入 |
| IVFFlat | 快 | 中 | 低 | 数据量大、能接受调 `lists`/`probes`，需先有数据再建 |

```sql
-- HNSW（推荐）
CREATE INDEX ON documents USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
SET hnsw.ef_search = 100;   -- 查询时召回/速度权衡，默认 40

-- IVFFlat
CREATE INDEX ON documents USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
SET ivfflat.probes = 10;
```

### 6.3 RAG 场景典型表结构（1536 维，OpenAI text-embedding-3-small）

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE documents (
    id          bigserial PRIMARY KEY,
    content     text        NOT NULL,
    metadata    jsonb       DEFAULT '{}',
    embedding   vector(1536),
    created_at  timestamptz DEFAULT now()
);

CREATE INDEX documents_embedding_idx ON documents
    USING hnsw (embedding vector_cosine_ops);

-- 检索
SELECT id, content, 1 - (embedding <=> $1::vector) AS similarity
FROM documents
ORDER BY embedding <=> $1::vector
LIMIT 5;
```

### 6.4 Python 连接（psycopg3 + pgvector）

```powershell
pip3 install "psycopg[binary]" pgvector
```

```python
import psycopg
from pgvector.psycopg import register_vector

conn = psycopg.connect(
    "host=127.0.0.1 port=5432 dbname=pgvector_test user=postgres password=postgres"
)
register_vector(conn)

with conn.cursor() as cur:
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    cur.execute("CREATE TABLE IF NOT EXISTS items (id bigserial PRIMARY KEY, embedding vector(3))")
    cur.execute("INSERT INTO items (embedding) VALUES (%s), (%s)", ([1, 2, 3], [4, 5, 6]))
    cur.execute(
        "SELECT id, embedding <-> %s AS dist FROM items ORDER BY dist LIMIT 3",
        ([3, 1, 2],),
    )
    print(cur.fetchall())
conn.commit()
```

SQLAlchemy 场景用 `pip3 install pgvector` 后 `from pgvector.sqlalchemy import Vector`，字段声明 `Column(Vector(1536))`；LangChain 用 `langchain-postgres` 的 `PGVector`。

### 6.5 服务与运维命令

```powershell
Get-Service postgresql-x64-17                    # 查看状态
Restart-Service postgresql-x64-17                # 重启（管理员）
net start postgresql-x64-17 / net stop postgresql-x64-17
& 'C:\Program Files\PostgreSQL\17\bin\pg_ctl.exe' status -D 'C:\Program Files\PostgreSQL\17\data'

# 改密码（本地开发默认 postgres，生产务必修改）
psql -U postgres -c "ALTER USER postgres WITH PASSWORD 'YourNewPwd';"

# 备份 / 恢复
& 'C:\Program Files\PostgreSQL\17\bin\pg_dump.exe' -U postgres -Fc pgvector_test > D:\backup\pgvector_test.dump
& 'C:\Program Files\PostgreSQL\17\bin\pg_restore.exe' -U postgres -d pgvector_test_restore D:\backup\pgvector_test.dump
```

---

## 七、常见问题排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `psql` 不是内部或外部命令 | bin 未进 PATH，或终端未重开 | 用全路径 `C:\Program Files\PostgreSQL\17\bin\psql.exe`；或新开终端 |
| `CREATE EXTENSION vector` 报 `could not open extension control file "...\vector.control"` | 只复制了 dll，漏了 `share\extension` 下的 control/sql | 全量复制 43 个 `vector*` 文件到 `share\extension` |
| 报 `The specified module could not be found` / 加载 vector.dll 失败 | DLL 与 PG 大版本不匹配，或 DLL 在服务运行时被占用后未重启 | 换 `pg17` 对应包；复制后必须 `Stop-Service` → 复制 → `Start-Service` |
| `函数 vector_dims(unknown) 不是唯一的` | 0.8.6 多态重载，未给明确类型 | 写 `vector_dims('[1,2,3]'::vector)` |
| `invalid input syntax for type sparsevec: "[1,2,3]"` | sparsevec 语法是 `{位置:值,...}/维度` | 写 `'{1:1,3:3}/5'::sparsevec` |
| GitHub 下载 pgvector 超时 `(28)/(56)` | 国内直连 releases 不稳定 | 用 `https://ghfast.top/` 或 `https://gh-proxy.com/` 前缀镜像 |
| 复制文件提示拒绝访问 | `C:\Program Files` 需提权 | 以管理员身份运行 PowerShell 再复制 |
| winget 安装长时间无输出 | 正在下载 356MB / 解包 pgAdmin，属正常 | 观察 `%TEMP%\installbuilder_installer.log` 尾部、安装目录写入时间 |
| 安装后想换端口 | 5432 被占用 | 编辑 `data\postgresql.conf` 的 `port`，改完重启服务 |
| 升级 pgvector | 版本迭代 | 停服务 → 覆盖新包文件 → 启服务 → `ALTER EXTENSION vector UPDATE;` |

---

## 八、卸载与回滚

```powershell
# 1) 停服务
Stop-Service postgresql-x64-17 -Force

# 2) 卸载（走自带卸载器，会保留/询问数据目录）
& 'C:\Program Files\PostgreSQL\17\uninstall-postgresql.exe' --mode unattended

# 3) 只回退 pgvector：删除以下即可
#    C:\Program Files\PostgreSQL\17\lib\vector.dll
#    C:\Program Files\PostgreSQL\17\share\extension\vector*
#    C:\Program Files\PostgreSQL\17\include\server\extension\vector\
#    再进库执行 DROP EXTENSION vector; （或删库）

# 4) winget 卸载方式
winget uninstall --id PostgreSQL.PostgreSQL.17 -e
```

数据目录 `C:\Program Files\PostgreSQL\17\data` **默认不在卸载器中删除**，需要手工清理；删前务必先做 `pg_dump` 备份。

---

## 九、本次实际执行记录

| 时间 | 动作 | 结果 |
| --- | --- | --- |
| 16:03 | `winget install PostgreSQL.PostgreSQL.17` 启动 | 下载 356MB 安装包，校验 SHA256 通过 |
| 16:09 | EDB 安装器进程启动（UAC 授权后） | 创建 `C:\Program Files\PostgreSQL\17` |
| 16:11–16:35 | 解包 server / commandlinetools / pgAdmin 4 | `%TEMP%\installbuilder_installer.log` 持续写入 |
| ~16:35 | 安装器结束、服务注册 | `postgresql-x64-17` Running / Automatic，5432 监听 |
| 16:20 | 直连 GitHub 下载 pgvector 包 | 失败：3 次超时（curl 28） |
| 16:24 | 镜像 `ghfast.top` 重下 | 成功，169,689 字节 |
| 16:38 | 管理员脚本：停服务 → 复制 dll/43 个扩展文件/3 个头文件 → 写 PATH → 启服务 | 全部成功，服务 Running |
| 16:40 | 建库 `pgvector_test` + `CREATE EXTENSION vector` | vector 0.8.6 已安装 |
| 16:41 | 建表、插入、L2 检索、HNSW 索引、EXPLAIN | 结果正确，查询走 `items_hnsw_idx` |
| 16:42 | halfvec / sparsevec / binary_quantize / 余弦距离 | 全部可用 |

安装过程中唯一的人工操作：两次 UAC 授权点击。

---

## 十、验收清单（可逐条勾选）

- [x] `Get-Service postgresql-x64-17` 状态为 Running、启动类型 Automatic
- [x] 5432 端口处于 LISTEN（0.0.0.0 与 ::）
- [x] `psql --version` 输出 `psql (PostgreSQL) 17.11`
- [x] 能用 `postgres/postgres` 连接 `127.0.0.1:5432`
- [x] `lib\vector.dll` 存在（279,552 字节）
- [x] `share\extension` 下 43 个 `vector*` 文件存在
- [x] `SELECT extversion FROM pg_extension WHERE extname='vector'` 返回 `0.8.6`
- [x] `embedding <-> '[3,1,2]'` 排序结果正确
- [x] HNSW 索引建成，`EXPLAIN` 显示 Index Scan
- [x] `vector / halfvec / sparsevec / binary_quantize` 均可调用
- [x] 系统 PATH 已包含 `C:\Program Files\PostgreSQL\17\bin`

---

## 附：参考资料

- PostgreSQL 官方 Windows 下载与安装文档：<https://www.postgresql.org/download/windows/>
- EDB（安装包提供方）：<https://www.enterprisedb.com/downloads/postgres-postgresql-downloads>
- pgvector 官方仓库（源码与全部语法）：<https://github.com/pgvector/pgvector>
- pgvector Windows 预编译包（社区）：<https://github.com/andreiramani/pgvector_pgsql_windows>

# Knowgrain 本地离线备份与恢复

状态：手工运维说明（M5 配套）。当前没有 Web 备份按钮，也没有完整的损坏工作区重建工具。本流程要求操作者显式停写、备份到独立目录，并恢复到新目录和新数据库；不会自动覆盖原实例。

## 适用范围与数据边界

一次可恢复备份必须来自同一个停写窗口，包含：

- 应用 PostgreSQL 数据库（`KNOWGRAIN_POSTGRES_DB`，默认 `knowgrain`）。
- LightRAG PostgreSQL 数据库（`POSTGRES_DATABASE`，默认 `lightrag`）。
- 完整 Vault 目录树，包括隐藏文件/目录（例如 `.knowgrain`、`.obsidian`）、`Sources/`、`Wiki/`、`Trash/` 和其他用户文件。
- 完整 `LIGHTRAG_WORKING_DIR` 目录，以及不含密码或密钥的恢复元数据：代码 Git commit、工作区改动状态、锁文件、应用 schema revision、PostgreSQL/pgvector 与 LightRAG 版本、模型名、Embedding 维度、LightRAG workspace 名称和各目录配置。
- 运行需要的模型与 tokenizer 资源。按[本地安装说明](dependencies-and-local-setup.md)安装/下载模型和 tokenizer；若要求断网恢复，还需在独立的离线介质中保留对应 Ollama 模型缓存、tokenizer 缓存和锁定依赖。

`VAULT_ROOT` 只用于首次绑定。已绑定 Vault 的实际根路径必须从应用库 `vault_binding` 的 `id = 1` 读取，不能根据 `.env`、默认值或文件夹名称猜测。`VAULT_PARENT_DIR` 是 Web 选择 Vault 的父目录，不会取代数据库里的绑定记录。配置项和环境变量名称以 [`config.py`](../src/knowgrain/config.py) 为准；Knowgrain 直接读取 `.env`，但不得用 `source .env` 执行它。

先停止 Knowgrain 写入和 Obsidian、同步客户端、编辑器等外部写入，再导出两个库和文件目录。PostgreSQL 服务在备份期间继续运行。`pg_dump` 每次给单个数据库提供一致的快照，但两个独立 `pg_dump` 不构成跨数据库原子快照；同一个停写窗口是应用库、RAG 库和 Vault 相互对应的前提。参见 PostgreSQL 对 [`pg_dump`](https://www.postgresql.org/docs/current/app-pgdump.html) 和 [`pg_restore`](https://www.postgresql.org/docs/current/app-pgrestore.html) 的说明。

## 1. 备份

### 1.1 停止所有写入者

1. 等待正在执行的导入、归档/恢复、Wiki 编辑、生成、审阅和问答任务结束；记录仍排队的任务，它们会随数据库状态一起备份。
2. 在运行 `make api` / `uv run knowgrain-api` 的终端按 `Ctrl-C` 正常关闭 Knowgrain。若用其他进程管理器，只停止 Knowgrain API/作业进程。
3. 关闭 Obsidian 及会写入 Vault 的编辑器、同步工具或文件监听器；确认没有其他进程仍在修改 Vault。
4. 保持 PostgreSQL 运行。仓库的 `make db-down` 会停止数据库，不适用于本流程。

### 1.2 设置本次备份参数

在仓库根目录打开一个交互式 shell，按正在运行的实例填写非机密配置。所有路径必须是绝对路径；备份目录必须位于 Vault 和 LightRAG working 目录之外。以下值仅为示例，须替换成实例实际配置。不要把密码放进变量赋值、命令参数、终端历史或备份元数据。

```sh
set -eu
umask 077

KG_PGHOST="127.0.0.1"
KG_PGPORT="5432"
KG_PGUSER="knowgrain"
KG_PGPASSFILE="/absolute/path/to/existing/protected.pgpass"
export PGPASSFILE="$KG_PGPASSFILE"
KG_APP_DB="knowgrain"
KG_RAG_DB="lightrag"
KG_BACKUP_DIR="/Volumes/OfflineDisk/KnowgrainBackups"
KG_LR_WORKING_DIR="/absolute/path/to/data/lightrag"
KG_LR_WORKSPACE="knowgrain"  # 填写存储实际 workspace；POSTGRES_WORKSPACE 可覆盖构造值。
KG_LLM_MODEL="qwen3.6:35b"
KG_EMBEDDING_MODEL="qwen3-embedding:0.6b"
KG_EMBEDDING_DIM="1024"
KG_EMBEDDING_MAX_TOKEN_SIZE="32768"
KG_LLM_CONTEXT_SIZE="16384"
KG_OLLAMA_HOST="http://127.0.0.1:11434"
KG_TOKENIZER_CACHE_DIR="/absolute/path/to/data/tokenizers"
KG_VAULT_PARENT_DIR="/absolute/path/to/data/vaults"
KG_API_HOST="127.0.0.1"
KG_API_PORT="8787"

python3 - "$KG_PGPASSFILE" <<'PY'
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
if path.is_symlink() or not path.is_file():
    raise SystemExit("PGPASSFILE must be an existing regular file, not a symlink")
if stat.S_IMODE(path.stat().st_mode) & 0o077:
    raise SystemExit("PGPASSFILE must not be readable or writable by group/other users")
PY

```

`KG_APP_DB` 对应 `KNOWGRAIN_POSTGRES_DB`；`KG_RAG_DB` 对应 `POSTGRES_DATABASE`。`KG_LR_WORKSPACE` 必须填原实例实际写入 LightRAG Postgres 的 workspace。固定版上游实际读取 `POSTGRES_WORKSPACE`，日志将其称为 `PG_WORKSPACE`；自定义覆盖时先从实际运行配置/日志核对，不能根据默认值猜测。新运行时会将这两个值明确绑定到 Core 身份。如实际运行时用了不同数据库名、模型、维度、workspace 或路径，必须照实填写。不要使用 `PGPASSWORD`、明文命令参数或 `source .env`。

PG 客户端统一从已有且权限受限的密码文件读取认证（例如权限为 0600 的 .pgpass）；文件需包含实际 host/port、数据库名和用户的匹配凭据。它只留在本机凭据位置，不复制进备份或元数据，也不改宿主机全局配置。确认文件是普通文件且组/其他用户不可读后再继续；下方的 -w 在缺少匹配凭据时立即失败，不显示密码提示。

### 1.3 从应用库读取 Vault 根路径

下面的只读查询直接从当前应用数据库取绑定。结果必须是唯一、非空的绝对路径。不存在绑定或连接信息不符时停止并先查明原因，不能回退猜 `VAULT_ROOT`。

```sh
KG_OLD_VAULT_ROOT="$(psql -X -A -t -q -v ON_ERROR_STOP=1 -w \
  -h "$KG_PGHOST" -p "$KG_PGPORT" -U "$KG_PGUSER" -d "$KG_APP_DB" \
  -c 'SELECT root_path FROM vault_binding WHERE id = 1')"
KG_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
KG_RUN_DIR="$KG_BACKUP_DIR/knowgrain-$KG_STAMP"

kg_validate_backup_paths() {
  python3 - "$KG_OLD_VAULT_ROOT" "$KG_LR_WORKING_DIR" "$KG_BACKUP_DIR" "$KG_RUN_DIR" "$1" <<'PY'
import os
import stat
import sys
from pathlib import Path

vault, working, backup, run, phase = sys.argv[1:]
def checked_path(raw: str, *, required: bool, label: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or str(path) != raw or os.path.normpath(raw) != raw or ".." in path.parts:
        raise SystemExit(f"{label} must be an absolute canonical path without '..': {raw}")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise SystemExit(f"{label} path contains a symlink: {current}")
        if not stat.S_ISDIR(mode):
            raise SystemExit(f"{label} parent is not a directory: {current}")
    if required:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            raise SystemExit(f"{label} does not exist: {path}")
        if not stat.S_ISDIR(mode):
            raise SystemExit(f"{label} must be an ordinary directory: {path}")
        if path.resolve(strict=True) != path:
            raise SystemExit(f"{label} is not canonical: {path}")
    elif path.resolve(strict=False) != path:
        raise SystemExit(f"{label} is not canonical: {path}")
    return path

vault_path = checked_path(vault, required=True, label="Vault root")
working_path = checked_path(working, required=True, label="LightRAG working directory")
backup_path = checked_path(backup, required=(phase == "after"), label="backup directory")
run_path = checked_path(run, required=(phase == "after"), label="backup run directory")
if run_path.parent != backup_path:
    raise SystemExit("backup run directory must be a direct child of KG_BACKUP_DIR")
if phase == "before" and run_path.exists():
    raise SystemExit(f"backup run directory already exists: {run_path}")
def overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents
for source in (vault_path, working_path):
    if overlaps(backup_path, source) or overlaps(run_path, source):
        raise SystemExit("backup output must be disjoint from Vault and LightRAG working directories")
PY
}

kg_validate_backup_paths before
mkdir -p "$KG_BACKUP_DIR"
mkdir "$KG_RUN_DIR"
kg_validate_backup_paths after
```

Vault 根必须与数据库绑定完全一致。应用初始化会校验绑定目录；若该目录不存在、路径不规范或来源哈希不符，不要通过改环境变量绕过检查。

### 1.4 生成可比较的目录 SHA-256 清单

清单对每个相对路径记录目录、普通文件内容 SHA-256 或符号链接目标，不跟随链接，也会保留空目录的存在性。特殊文件会令清单失败。将此函数复制到备份和恢复 shell 中使用；它只用 Python 标准库，不新增文件或依赖。

```sh
kg_tree_manifest() {
  python3 - "$1" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

raw_root = sys.argv[1]
root = Path(raw_root)
if not root.is_absolute() or str(root) != raw_root or os.path.normpath(raw_root) != raw_root or ".." in root.parts:
    raise SystemExit(f"root must be an absolute canonical path without '..': {raw_root}")
current = Path(root.anchor)
for part in root.parts[1:]:
    current = current / part
    try:
        mode = current.lstat().st_mode
    except FileNotFoundError:
        raise SystemExit(f"root path component does not exist: {current}")
    if stat.S_ISLNK(mode):
        raise SystemExit(f"root path contains a symlink: {current}")
    if not stat.S_ISDIR(mode):
        raise SystemExit(f"root path component is not a directory: {current}")
if root.resolve(strict=True) != root:
    raise SystemExit(f"root is not canonical: {root}")
records = []

def visit(directory: Path, relative: str) -> None:
    with os.scandir(directory) as scan:
        entries = sorted(scan, key=lambda entry: entry.name)
    for entry in entries:
        rel = f"{relative}/{entry.name}" if relative else entry.name
        path = Path(entry.path)
        mode = entry.stat(follow_symlinks=False).st_mode
        if stat.S_ISLNK(mode):
            records.append({"path": rel, "type": "symlink", "target": os.readlink(path)})
        elif stat.S_ISDIR(mode):
            records.append({"path": rel, "type": "directory"})
            visit(path, rel)
        elif stat.S_ISREG(mode):
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            records.append({"path": rel, "type": "file", "sha256": digest.hexdigest()})
        else:
            raise SystemExit(f"unsupported special file: {path}")

visit(root, "")
for record in records:
    print(json.dumps(record, sort_keys=True, ensure_ascii=True))
PY
}
```

### 1.5 记录版本与配置元数据

元数据不包含密码、API key 或其他机密。机密配置如确需保留，放入单独的加密凭据库，并使用组织既有密钥管理方式。不要把 `.env`、数据库密码或真实密钥复制到 Vault 或普通备份目录。

```sh
KG_GIT_COMMIT="$(git rev-parse HEAD)"
KG_APP_SCHEMA_REV="$(psql -X -A -t -q -v ON_ERROR_STOP=1 -w \
  -h "$KG_PGHOST" -p "$KG_PGPORT" -U "$KG_PGUSER" -d "$KG_APP_DB" \
  -c "SELECT string_agg(version_num, ',' ORDER BY version_num) FROM alembic_version")"
KG_PG_VERSION="$(psql -X -A -t -q -v ON_ERROR_STOP=1 -w \
  -h "$KG_PGHOST" -p "$KG_PGPORT" -U "$KG_PGUSER" -d "$KG_RAG_DB" \
  -c 'SHOW server_version')"
KG_VECTOR_VERSION="$(psql -X -A -t -q -v ON_ERROR_STOP=1 -w \
  -h "$KG_PGHOST" -p "$KG_PGPORT" -U "$KG_PGUSER" -d "$KG_RAG_DB" \
  -c "SELECT COALESCE((SELECT extversion FROM pg_extension WHERE extname = 'vector'), 'MISSING')")"

cp uv.lock "$KG_RUN_DIR/uv.lock"
cp apps/web/package-lock.json "$KG_RUN_DIR/package-lock.json"
git status --short > "$KG_RUN_DIR/git-status.txt"
printf '%s\n' \
  "git_commit=$KG_GIT_COMMIT" \
  "application_schema_revision=$KG_APP_SCHEMA_REV" \
  "postgres_version=$KG_PG_VERSION" \
  "pgvector_version=$KG_VECTOR_VERSION" \
  "lightrag_hku_version=1.5.7" \
  "postgres_host=$KG_PGHOST" \
  "postgres_port=$KG_PGPORT" \
  "application_database=$KG_APP_DB" \
  "rag_database=$KG_RAG_DB" \
  "old_vault_root=$KG_OLD_VAULT_ROOT" \
  "lightrag_working_dir=$KG_LR_WORKING_DIR" \
  "lightrag_workspace=$KG_LR_WORKSPACE" \
  "pg_workspace=$KG_LR_WORKSPACE" \
  "postgres_workspace=$KG_LR_WORKSPACE" \
  "llm_model=$KG_LLM_MODEL" \
  "llm_context_size=$KG_LLM_CONTEXT_SIZE" \
  "ollama_host=$KG_OLLAMA_HOST" \
  "embedding_model=$KG_EMBEDDING_MODEL" \
  "embedding_dim=$KG_EMBEDDING_DIM" \
  "embedding_max_token_size=$KG_EMBEDDING_MAX_TOKEN_SIZE" \
  "tokenizer_cache_dir=$KG_TOKENIZER_CACHE_DIR" \
  "vault_parent_dir=$KG_VAULT_PARENT_DIR" \
  "api_host=$KG_API_HOST" \
  "api_port=$KG_API_PORT" \
  > "$KG_RUN_DIR/recovery-metadata.txt"
```

检查 `git-status.txt`。若运行代码不是干净的 Git commit，必须另外保留并审查确切的源码改动和所需未跟踪源码，排除 `.env`、密钥、数据和缓存；仅有 commit hash 不足以重建未提交代码。恢复时用匹配的代码、`uv.lock`、前端锁文件和 schema，不要先升级代码。

### 1.6 备份两个数据库和完整目录

先记录目录清单，再归档整个 Vault 与 LightRAG working 目录。归档从父目录开始，因此会包括隐藏文件和空目录。命令的非零退出状态或任何 tar/pg_dump 警告都必须调查；不要把有告警的产物标为可用备份。

```sh
KG_VAULT_PARENT="${KG_OLD_VAULT_ROOT%/*}"
KG_VAULT_NAME="${KG_OLD_VAULT_ROOT##*/}"
KG_LR_PARENT="${KG_LR_WORKING_DIR%/*}"
KG_LR_NAME="${KG_LR_WORKING_DIR##*/}"

kg_tree_manifest "$KG_OLD_VAULT_ROOT" > "$KG_RUN_DIR/vault.manifest.before.jsonl"
kg_tree_manifest "$KG_LR_WORKING_DIR" > "$KG_RUN_DIR/lightrag-working.manifest.before.jsonl"

if tar -cpf "$KG_RUN_DIR/vault.tar" -C "$KG_VAULT_PARENT" "$KG_VAULT_NAME" \
    2> "$KG_RUN_DIR/vault-tar.stderr"; then
  if test -s "$KG_RUN_DIR/vault-tar.stderr"; then
    cat "$KG_RUN_DIR/vault-tar.stderr" >&2
    printf '%s\n' "tar 有告警；检查后重新备份，不要继续标记成功。" >&2
    exit 1
  fi
else
  cat "$KG_RUN_DIR/vault-tar.stderr" >&2
  exit 1
fi

if tar -cpf "$KG_RUN_DIR/lightrag-working.tar" -C "$KG_LR_PARENT" "$KG_LR_NAME" \
    2> "$KG_RUN_DIR/lightrag-tar.stderr"; then
  if test -s "$KG_RUN_DIR/lightrag-tar.stderr"; then
    cat "$KG_RUN_DIR/lightrag-tar.stderr" >&2
    printf '%s\n' "tar 有告警；检查后重新备份，不要继续标记成功。" >&2
    exit 1
  fi
else
  cat "$KG_RUN_DIR/lightrag-tar.stderr" >&2
  exit 1
fi

if pg_dump -Fc -w -h "$KG_PGHOST" -p "$KG_PGPORT" -U "$KG_PGUSER" \
    -d "$KG_APP_DB" -f "$KG_RUN_DIR/application.dump" \
    2> "$KG_RUN_DIR/application-pg-dump.stderr"; then
  if test -s "$KG_RUN_DIR/application-pg-dump.stderr"; then
    cat "$KG_RUN_DIR/application-pg-dump.stderr" >&2
    printf '%s\n' "pg_dump 有告警；检查后重新备份，不要继续标记成功。" >&2
    exit 1
  fi
else
  cat "$KG_RUN_DIR/application-pg-dump.stderr" >&2
  exit 1
fi

if pg_dump -Fc -w -h "$KG_PGHOST" -p "$KG_PGPORT" -U "$KG_PGUSER" \
    -d "$KG_RAG_DB" -f "$KG_RUN_DIR/lightrag.dump" \
    2> "$KG_RUN_DIR/lightrag-pg-dump.stderr"; then
  if test -s "$KG_RUN_DIR/lightrag-pg-dump.stderr"; then
    cat "$KG_RUN_DIR/lightrag-pg-dump.stderr" >&2
    printf '%s\n' "pg_dump 有告警；检查后重新备份，不要继续标记成功。" >&2
    exit 1
  fi
else
  cat "$KG_RUN_DIR/lightrag-pg-dump.stderr" >&2
  exit 1
fi

pg_restore --list "$KG_RUN_DIR/application.dump" > "$KG_RUN_DIR/application.toc"
pg_restore --list "$KG_RUN_DIR/lightrag.dump" > "$KG_RUN_DIR/lightrag.toc"
kg_tree_manifest "$KG_OLD_VAULT_ROOT" > "$KG_RUN_DIR/vault.manifest.after.jsonl"
kg_tree_manifest "$KG_LR_WORKING_DIR" > "$KG_RUN_DIR/lightrag-working.manifest.after.jsonl"
cmp "$KG_RUN_DIR/vault.manifest.before.jsonl" "$KG_RUN_DIR/vault.manifest.after.jsonl"
cmp "$KG_RUN_DIR/lightrag-working.manifest.before.jsonl" "$KG_RUN_DIR/lightrag-working.manifest.after.jsonl"
mv "$KG_RUN_DIR/vault.manifest.before.jsonl" "$KG_RUN_DIR/vault.manifest.jsonl"
mv "$KG_RUN_DIR/lightrag-working.manifest.before.jsonl" "$KG_RUN_DIR/lightrag-working.manifest.jsonl"

(
  cd "$KG_RUN_DIR"
  shasum -a 256 application.dump lightrag.dump vault.tar lightrag-working.tar \
    uv.lock package-lock.json recovery-metadata.txt git-status.txt \
    application.toc lightrag.toc vault-tar.stderr lightrag-tar.stderr \
    application-pg-dump.stderr lightrag-pg-dump.stderr \
    vault.manifest.jsonl lightrag-working.manifest.jsonl \
    vault.manifest.after.jsonl lightrag-working.manifest.after.jsonl
) > "$KG_RUN_DIR/SHA256SUMS"
```

将整个 `KG_RUN_DIR` 复制到独立、离线且访问受控的介质；不要在 Vault 目录内存放备份。复制后在介质上运行 `cd "$KG_RUN_DIR" && shasum -a 256 -c SHA256SUMS`，并保留这份离线副本。不要将两个数据库分别导出的“单库一致”误认为它们与 Vault 跨库原子一致。

## 2. 恢复到隔离实例

本流程要求先匹配原备份的 Knowgrain 代码/schema、PostgreSQL 主版本、pgvector 扩展、角色和模型配置。目标应用库和 RAG 库必须是新建的明确命名数据库；原库、原 Vault 和原 working 目录保持原样。不要用 `pg_restore --clean`、`--drop`、`DROP DATABASE`，也不要把文件解压到现有 Vault 上。

### 2.1 校验备份并准备新位置

使用新的绝对路径和数据库名。示例名称带 `restore`，每次演练应选择尚不存在的名字。新 Vault 目标必须是一个全新的空目录；新 Vault 根路径及其所有父路径都必须规范化且不含符号链接。

```sh
set -eu
umask 077

KG_BACKUP_RUN_DIR="/absolute/path/to/KnowgrainBackups/knowgrain-YYYYMMDDTHHMMSSZ"
KG_PGHOST="127.0.0.1"
KG_PGPORT="5432"
KG_PGUSER="knowgrain"
KG_TARGET_ROLE="knowgrain"
KG_TARGET_APP_DB="knowgrain_restore_20261003"
KG_TARGET_RAG_DB="lightrag_restore_20261003"
KG_NEW_PARENT="/absolute/canonical/path/to/recovered-vaults"
KG_NEW_ROOT="$KG_NEW_PARENT/KnowgrainVault-restore-20261003"
KG_NEW_LR_PARENT="/absolute/canonical/path/to/recovered-data"
KG_NEW_LR_WORKING_DIR="$KG_NEW_LR_PARENT/lightrag-restore-20261003"
KG_REPO_DIR="/absolute/path/to/knowgrain-matching-backup"
KG_RESTORE_LOG_DIR="/absolute/path/to/restore-validation/restore-20261003"
KG_PGPASSFILE="/absolute/path/to/existing/protected.pgpass"
export PGPASSFILE="$KG_PGPASSFILE"

python3 - "$KG_PGPASSFILE" <<'PY'
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
if path.is_symlink() or not path.is_file():
    raise SystemExit("PGPASSFILE must be an existing regular file, not a symlink")
if stat.S_IMODE(path.stat().st_mode) & 0o077:
    raise SystemExit("PGPASSFILE must not be readable or writable by group/other users")
PY

cd "$KG_BACKUP_RUN_DIR"
shasum -a 256 -c SHA256SUMS
KG_SOURCE_APP_DB="$(sed -n 's/^application_database=//p' "$KG_BACKUP_RUN_DIR/recovery-metadata.txt")"
KG_SOURCE_RAG_DB="$(sed -n 's/^rag_database=//p' "$KG_BACKUP_RUN_DIR/recovery-metadata.txt")"
KG_OLD_VAULT_ROOT="$(sed -n 's/^old_vault_root=//p' "$KG_BACKUP_RUN_DIR/recovery-metadata.txt")"
KG_SOURCE_LR_WORKING_DIR="$(sed -n 's/^lightrag_working_dir=//p' "$KG_BACKUP_RUN_DIR/recovery-metadata.txt")"
KG_EXPECTED_GIT_COMMIT="$(sed -n 's/^git_commit=//p' "$KG_BACKUP_RUN_DIR/recovery-metadata.txt")"
test "$KG_TARGET_APP_DB" != "$KG_SOURCE_APP_DB"
test "$KG_TARGET_RAG_DB" != "$KG_SOURCE_RAG_DB"
test "$KG_TARGET_APP_DB" != "$KG_TARGET_RAG_DB"
test "$KG_NEW_ROOT" != "$KG_OLD_VAULT_ROOT"
test "$KG_NEW_LR_WORKING_DIR" != "$KG_SOURCE_LR_WORKING_DIR"
test "$KG_NEW_ROOT" != "$KG_NEW_LR_WORKING_DIR"
for KG_CANDIDATE in "$KG_NEW_ROOT" "$KG_NEW_LR_WORKING_DIR" "$KG_RESTORE_LOG_DIR"; do
  case "$KG_CANDIDATE" in
    "$KG_OLD_VAULT_ROOT"|"$KG_OLD_VAULT_ROOT/"*|"$KG_SOURCE_LR_WORKING_DIR"|"$KG_SOURCE_LR_WORKING_DIR/"*|"$KG_BACKUP_RUN_DIR"|"$KG_BACKUP_RUN_DIR/"*)
      printf '%s\n' "恢复目标和验收日志目录不得位于原 Vault、LightRAG working 目录或备份目录内。" >&2
      exit 1
      ;;
  esac
done
mkdir -p "$KG_RESTORE_LOG_DIR"
test "$(git -C "$KG_REPO_DIR" rev-parse HEAD)" = "$KG_EXPECTED_GIT_COMMIT"
cmp "$KG_BACKUP_RUN_DIR/uv.lock" "$KG_REPO_DIR/uv.lock"
cmp "$KG_BACKUP_RUN_DIR/package-lock.json" "$KG_REPO_DIR/apps/web/package-lock.json"
pg_restore --list "$KG_BACKUP_RUN_DIR/application.dump" > /dev/null
pg_restore --list "$KG_BACKUP_RUN_DIR/lightrag.dump" > /dev/null
test ! -e "$KG_NEW_ROOT"
test ! -e "$KG_NEW_LR_WORKING_DIR"
mkdir -p "$KG_NEW_PARENT" "$KG_NEW_LR_PARENT"

python3 - "$KG_NEW_PARENT" "$KG_NEW_ROOT" "$KG_NEW_LR_PARENT" "$KG_NEW_LR_WORKING_DIR" <<'PY'
import os
import stat
import sys
from pathlib import Path

vault_parent, vault_root, lr_parent, lr_root = map(Path, sys.argv[1:])
def validate_parent(parent: Path) -> None:
    raw = str(parent)
    if not parent.is_absolute() or os.path.normpath(raw) != raw or ".." in parent.parts:
        raise SystemExit("restore parent must be an absolute canonical path")
    current = Path(parent.anchor)
    for part in parent.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise SystemExit(f"restore parent component is missing: {current}")
        if stat.S_ISLNK(mode):
            raise SystemExit(f"symlink in restore parent path: {current}")
        if not stat.S_ISDIR(mode):
            raise SystemExit(f"restore parent component is not a directory: {current}")
    if parent.resolve(strict=True) != parent:
        raise SystemExit("restore parent is not canonical")
for parent in (vault_parent, lr_parent):
    validate_parent(parent)
for root, parent, label in ((vault_root, vault_parent, "Vault root"), (lr_root, lr_parent, "LightRAG working directory")):
    raw = str(root)
    if not root.is_absolute() or os.path.normpath(raw) != raw or ".." in root.parts:
        raise SystemExit(f"{label} must be an absolute canonical path")
    if root.parent != parent or root.exists() or root.is_symlink():
        raise SystemExit(f"{label} must be a new direct child of its canonical parent")
def overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents
if overlaps(vault_root, lr_root):
    raise SystemExit("new Vault and LightRAG working directories must be disjoint")
if vault_root.resolve(strict=False) != vault_root or lr_root.resolve(strict=False) != lr_root:
    raise SystemExit("restore targets are not canonical")
PY
```

如果某个目标数据库名已存在，停止并换一个新名字；不要清空该库。目标 PostgreSQL 主版本及服务器端 pgvector 必须兼容备份，扩展可用版本应与元数据记录一致。`pg_dump` 不导出 PostgreSQL 全局角色、密码或角色授权，恢复前必须显式准备 `KG_TARGET_ROLE` 及应用/RAG 库所需角色和权限。pg_restore 使用哪个登录角色会影响对象归属；建议以目标应用角色执行并确保该角色能创建所需对象/扩展。不得把密码写入命令行。

### 2.2 在新目录恢复 Vault 和 LightRAG working 目录

再次定义上一节的 `kg_tree_manifest` 函数，然后分别解到新的 staging 目录。归档源目录名从本次恢复元数据中的原路径计算，不依赖 `.env`。抽取到 staging 后再移动为明确的新根；任何哈希或清单差异都必须阻止启动恢复服务。

```sh
KG_OLD_VAULT_ROOT="$(sed -n 's/^old_vault_root=//p' "$KG_BACKUP_RUN_DIR/recovery-metadata.txt")"
KG_LR_WORKING_DIR="$(sed -n 's/^lightrag_working_dir=//p' "$KG_BACKUP_RUN_DIR/recovery-metadata.txt")"
KG_VAULT_NAME="${KG_OLD_VAULT_ROOT##*/}"
KG_LR_NAME="${KG_LR_WORKING_DIR##*/}"
KG_VAULT_STAGE="$(mktemp -d "$KG_NEW_PARENT/.knowgrain-vault-restore.XXXXXX")"
KG_LR_STAGE="$(mktemp -d "$KG_NEW_LR_PARENT/.knowgrain-lightrag-restore.XXXXXX")"

tar -xpf "$KG_BACKUP_RUN_DIR/vault.tar" -C "$KG_VAULT_STAGE"
test -d "$KG_VAULT_STAGE/$KG_VAULT_NAME"
mv "$KG_VAULT_STAGE/$KG_VAULT_NAME" "$KG_NEW_ROOT"
rmdir "$KG_VAULT_STAGE"

tar -xpf "$KG_BACKUP_RUN_DIR/lightrag-working.tar" -C "$KG_LR_STAGE"
test -d "$KG_LR_STAGE/$KG_LR_NAME"
mv "$KG_LR_STAGE/$KG_LR_NAME" "$KG_NEW_LR_WORKING_DIR"
rmdir "$KG_LR_STAGE"

python3 - "$KG_NEW_ROOT" "$KG_NEW_LR_WORKING_DIR" "$KG_BACKUP_RUN_DIR" "$KG_RESTORE_LOG_DIR" "$KG_OLD_VAULT_ROOT" "$KG_LR_WORKING_DIR" <<'PY'
import os
import stat
import sys
from pathlib import Path

new_vault, new_working, backup, logs, old_vault, old_working = map(Path, sys.argv[1:])
def validate_existing_directory(path: Path, label: str) -> Path:
    raw = str(path)
    if not path.is_absolute() or os.path.normpath(raw) != raw or ".." in path.parts:
        raise SystemExit(f"{label} must be an absolute canonical path")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise SystemExit(f"{label} component does not exist: {current}")
        if stat.S_ISLNK(mode):
            raise SystemExit(f"{label} path contains a symlink: {current}")
        if not stat.S_ISDIR(mode):
            raise SystemExit(f"{label} component is not a directory: {current}")
    if path.resolve(strict=True) != path:
        raise SystemExit(f"{label} is not canonical")
    return path

vault = validate_existing_directory(new_vault, "new Vault root")
working = validate_existing_directory(new_working, "new LightRAG working directory")
backup = validate_existing_directory(backup, "backup run directory")
logs = validate_existing_directory(logs, "restore log directory")
def overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents
for protected in (backup, logs):
    if overlaps(vault, protected) or overlaps(working, protected):
        raise SystemExit("restored directories must not overlap backup or restore logs")
for old in (old_vault, old_working):
    raw = str(old)
    if not old.is_absolute() or os.path.normpath(raw) != raw or ".." in old.parts:
        raise SystemExit("original instance paths in metadata must be absolute canonical paths")
    protected = old.resolve(strict=False)
    if overlaps(vault, protected) or overlaps(working, protected):
        raise SystemExit("restored directories must not overlap original instance paths")
if overlaps(vault, working):
    raise SystemExit("new Vault and LightRAG working directories must be disjoint")
PY

kg_tree_manifest "$KG_NEW_ROOT" > "$KG_RESTORE_LOG_DIR/vault.manifest.restored.jsonl"
kg_tree_manifest "$KG_NEW_LR_WORKING_DIR" > "$KG_RESTORE_LOG_DIR/lightrag-working.manifest.restored.jsonl"
cmp "$KG_BACKUP_RUN_DIR/vault.manifest.jsonl" "$KG_RESTORE_LOG_DIR/vault.manifest.restored.jsonl"
cmp "$KG_BACKUP_RUN_DIR/lightrag-working.manifest.jsonl" "$KG_RESTORE_LOG_DIR/lightrag-working.manifest.restored.jsonl"
```

恢复清单包含整棵树，因此会覆盖检查 `.knowgrain`、`.obsidian`、`Sources/`、`Wiki/`、`Trash/`、隐藏文件和空目录。要确保 `KG_NEW_ROOT` 以及其父路径没有符号链接，并且 `KG_NEW_ROOT` 与 `KG_NEW_LR_WORKING_DIR` 均是刚恢复出的规范绝对路径。若 `shasum`、`tar` 或 `cmp` 失败，保留原实例和备份，不启动恢复后的 Knowgrain，也不把有差异的 Vault 绑定进数据库。

### 2.3 创建新数据库并恢复归档

用 `template0` 创建全新目标库。以下 `createdb` 若因目标已存在而失败，应改用另一个新名字；不要删库重试。用现有管理员账号执行 `createdb`，并将 `-O` 指向已准备好的目标所有者；`-w` 要求 PG 客户端从受保护的 PGPASSFILE 读取匹配凭据。

```sh
createdb -T template0 -O "$KG_TARGET_ROLE" -w \
  -h "$KG_PGHOST" -p "$KG_PGPORT" -U "$KG_PGUSER" "$KG_TARGET_APP_DB"
createdb -T template0 -O "$KG_TARGET_ROLE" -w \
  -h "$KG_PGHOST" -p "$KG_PGPORT" -U "$KG_PGUSER" "$KG_TARGET_RAG_DB"

pg_restore --single-transaction --exit-on-error --no-owner --no-privileges -w \
  -h "$KG_PGHOST" -p "$KG_PGPORT" -U "$KG_TARGET_ROLE" \
  -d "$KG_TARGET_APP_DB" "$KG_BACKUP_RUN_DIR/application.dump"
pg_restore --single-transaction --exit-on-error --no-owner --no-privileges -w \
  -h "$KG_PGHOST" -p "$KG_PGPORT" -U "$KG_TARGET_ROLE" \
  -d "$KG_TARGET_RAG_DB" "$KG_BACKUP_RUN_DIR/lightrag.dump"

KG_EXPECTED_SCHEMA="$(sed -n 's/^application_schema_revision=//p' "$KG_BACKUP_RUN_DIR/recovery-metadata.txt")"
KG_RESTORED_SCHEMA="$(psql -X -A -t -q -v ON_ERROR_STOP=1 -w \
  -h "$KG_PGHOST" -p "$KG_PGPORT" -U "$KG_TARGET_ROLE" -d "$KG_TARGET_APP_DB" \
  -c "SELECT string_agg(version_num, ',' ORDER BY version_num) FROM alembic_version")"
KG_EXPECTED_VECTOR="$(sed -n 's/^pgvector_version=//p' "$KG_BACKUP_RUN_DIR/recovery-metadata.txt")"
KG_RESTORED_VECTOR="$(psql -X -A -t -q -v ON_ERROR_STOP=1 -w \
  -h "$KG_PGHOST" -p "$KG_PGPORT" -U "$KG_TARGET_ROLE" -d "$KG_TARGET_RAG_DB" \
  -c "SELECT COALESCE((SELECT extversion FROM pg_extension WHERE extname = 'vector'), 'MISSING')")"
test "$KG_RESTORED_SCHEMA" = "$KG_EXPECTED_SCHEMA"
test "$KG_RESTORED_VECTOR" = "$KG_EXPECTED_VECTOR"
```

`--single-transaction --exit-on-error` 使每个目标库的恢复在发生错误时整体回滚；这仍然不是两个库之间的共同事务。如果任一恢复失败，不要启动服务或改原数据库；保留日志、备份和原实例，换一组全新的目标库名称后再排查重试。
恢复后的应用迁移头和 RAG 库 pgvector 版本必须与元数据完全一致；上面的只读核对会在不一致时停止后续步骤。

### 2.4 仅迁移新应用库中的 Vault 绑定

新 Vault 内容必须先通过 SHA 清单。接着只在新恢复的应用库里更改 `vault_binding`。下面的临时内联 Python 示例使用仓库已有 SQLAlchemy/asyncpg；不新增脚本。它在同一连接、同一事务内：断言 `current_database()` 是目标应用库，以 `SELECT ... FOR UPDATE` 锁定并读取旧 `root_path`，将其与备份元数据中的预期路径比较，再以绑定参数执行 `UPDATE ... WHERE id = 1 AND root_path = :old_root RETURNING ...`；必须恰好更新一行才允许提交。若目标库、旧路径或行数不符，事务回滚。

```sh
cd "$KG_REPO_DIR"
test "$(git rev-parse HEAD)" = "$KG_EXPECTED_GIT_COMMIT"
uv run python - "$KG_PGHOST" "$KG_PGPORT" "$KG_TARGET_ROLE" "$KG_TARGET_APP_DB" "$KG_NEW_ROOT" "$KG_OLD_VAULT_ROOT" <<'PY'
import asyncio
import getpass
import sys
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

host, port, user, target_db, root_arg, expected_old_root = sys.argv[1:]
new_root = Path(root_arg)
if not new_root.is_absolute() or not new_root.is_dir():
    raise SystemExit("new Vault root must be an existing absolute directory")
cursor = Path(new_root.anchor)
for part in new_root.parts[1:]:
    cursor = cursor / part
    if cursor.is_symlink():
        raise SystemExit(f"symlink in new Vault root path: {cursor}")
if new_root.resolve(strict=True) != new_root:
    raise SystemExit("new Vault root is not canonical")

password = getpass.getpass("Target PostgreSQL password: ")
url = URL.create(
    "postgresql+asyncpg", username=user, password=password,
    host=host, port=int(port), database=target_db,
)
engine = create_async_engine(url)

async def migrate_binding() -> None:
    async with engine.begin() as connection:
        current_db = await connection.scalar(text("SELECT current_database()"))
        if current_db != target_db:
            raise RuntimeError(f"connected to {current_db!r}, expected {target_db!r}")
        old_root = await connection.scalar(
            text("SELECT root_path FROM vault_binding WHERE id = 1 FOR UPDATE")
        )
        if not old_root:
            raise RuntimeError("expected exactly one existing vault_binding row")
        if old_root != expected_old_root:
            raise RuntimeError(
                f"database Vault root {old_root!r} does not match backup root "
                f"{expected_old_root!r}"
            )
        if str(new_root) == old_root:
            raise RuntimeError("new Vault root must differ from the restored binding root")
        result = await connection.execute(
            text("""
                UPDATE vault_binding
                SET root_path = :new_root,
                    binding_id = :new_binding_id,
                    updated_at = now()
                WHERE id = 1 AND root_path = :old_root
                RETURNING id, root_path, binding_id, created_at, updated_at
            """),
            {
                "new_root": str(new_root),
                "new_binding_id": uuid4(),
                "old_root": old_root,
            },
        )
        rows = result.mappings().all()
        if len(rows) != 1:
            raise RuntimeError(f"expected one binding update, got {len(rows)}")
        print(f"database={current_db}; old_root={old_root}; new_root={rows[0]['root_path']}")

async def main() -> None:
    try:
        await migrate_binding()
    finally:
        await engine.dispose()

asyncio.run(main())
PY
```

该迁移只更新 `root_path`、生成新的 `binding_id`、刷新 `updated_at`；保留 `created_at`、来源/Wiki/领域 ID 和其他业务数据。不得把绑定更新应用到原应用库，也不得在启动服务后用 Web 设置尝试迁移已有内容。

### 2.5 启动隔离实例并验收

恢复进程必须明确指向新数据库、新 Vault、新 working 目录和单独 API 端口。`POSTGRES_DATABASE` 是 RAG 库；`KNOWGRAIN_POSTGRES_DB` 是应用库。`VAULT_ROOT` 是初始候选值，但启动实际使用新应用库里的已更新绑定。恢复命令显式令 `POSTGRES_WORKSPACE` 及 `PG_WORKSPACE` 等于备份记录的实际 workspace，避免 shell 中残留的旧覆盖值指向其他 LightRAG namespace。保留原 Embedding 模型名、`EMBEDDING_DIM`、模型上下文等值。`.env` 由 Pydantic 读取；不要 `source .env`。可在私有配置文件中编辑，或用进程管理器的环境注入覆盖，但不能把秘密写进命令行/代码。

将以下非机密值设置为本次恢复的实际值，并确保服务端密码从已有受保护配置或凭据管理器读取：

```sh
export POSTGRES_HOST="$KG_PGHOST"
export POSTGRES_PORT="$KG_PGPORT"
export POSTGRES_USER="$KG_TARGET_ROLE"
export POSTGRES_DATABASE="$KG_TARGET_RAG_DB"
export KNOWGRAIN_POSTGRES_DB="$KG_TARGET_APP_DB"
export VAULT_ROOT="$KG_NEW_ROOT"
export VAULT_PARENT_DIR="$KG_NEW_PARENT"
export LIGHTRAG_WORKING_DIR="$KG_NEW_LR_WORKING_DIR"
export LIGHTRAG_WORKSPACE="$(sed -n 's/^lightrag_workspace=//p' "$KG_BACKUP_RUN_DIR/recovery-metadata.txt")"
export PG_WORKSPACE="$LIGHTRAG_WORKSPACE"
export POSTGRES_WORKSPACE="$LIGHTRAG_WORKSPACE"
export OLLAMA_HOST="$(sed -n 's/^ollama_host=//p' "$KG_BACKUP_RUN_DIR/recovery-metadata.txt")"
export LLM_MODEL="$(sed -n 's/^llm_model=//p' "$KG_BACKUP_RUN_DIR/recovery-metadata.txt")"
export LLM_CONTEXT_SIZE="$(sed -n 's/^llm_context_size=//p' "$KG_BACKUP_RUN_DIR/recovery-metadata.txt")"
export EMBEDDING_MODEL="$(sed -n 's/^embedding_model=//p' "$KG_BACKUP_RUN_DIR/recovery-metadata.txt")"
export EMBEDDING_DIM="$(sed -n 's/^embedding_dim=//p' "$KG_BACKUP_RUN_DIR/recovery-metadata.txt")"
export EMBEDDING_MAX_TOKEN_SIZE="$(sed -n 's/^embedding_max_token_size=//p' "$KG_BACKUP_RUN_DIR/recovery-metadata.txt")"
export TOKENIZER_CACHE_DIR="/absolute/path/to/available/tokenizer-cache"
export API_HOST="127.0.0.1"
export API_PORT="8788"
test "$(psql -X -A -t -q -w -v ON_ERROR_STOP=1 -h "$KG_PGHOST" -p "$KG_PGPORT" -U "$KG_TARGET_ROLE" -d "$KG_TARGET_APP_DB" -c 'SELECT current_database()')" = "$KG_TARGET_APP_DB"
test "$(psql -X -A -t -q -w -v ON_ERROR_STOP=1 -h "$KG_PGHOST" -p "$KG_PGPORT" -U "$KG_TARGET_ROLE" -d "$KG_TARGET_RAG_DB" -c 'SELECT current_database()')" = "$KG_TARGET_RAG_DB"
cd "$KG_REPO_DIR"
test "$(git rev-parse HEAD)" = "$KG_EXPECTED_GIT_COMMIT"
```

上面的模型变量从 `recovery-metadata.txt` 读取；将 `TOKENIZER_CACHE_DIR` 指向恢复机已有或按[本地安装说明](dependencies-and-local-setup.md)准备好的词表缓存。该说明中的 `make models` / `make tokenizer` 是联网准备命令，锁定 Python/npm 依赖使用 `uv sync --locked` 和 `npm ci --prefix apps/web`。Embedding 模型、维度或相关前缀不同需要专门的全量重建/重索引流程，不能悄悄改配置启动。

使用与备份 commit、schema 和依赖锁相同的 Knowgrain 代码启动隔离 API（例如当前 shell 中运行 `make api`）。启动不会自动迁移旧 schema；不要先运行 `make migrate` 将备份升级到新 schema。先验证：

1. `curl -i http://127.0.0.1:8788/api/v1/health/live` 返回 HTTP 200，随后 `/api/v1/health/ready` 返回 HTTP 200。
2. 查看 `GET /api/v1/system/reconciliation`，确认启动对账完成并逐项检查是否发现缺失/不一致；若排队了修复，等待其完成并验证结果，不要仅凭进程存活判定恢复成功。
3. 在 Web/API 中读取来源、修订和当前修订状态；下载/查看代表性原件并将其 SHA-256 与原修订记录核对。检查历史修订仍可访问。
4. 打开已审阅 Wiki 正文，检查其历史/审阅状态、wikilink 和对应 Markdown 文件；确认恢复后没有自动覆盖已审阅正文。
5. 打开代表性证据，确认来源 ID、当前修订、原件哈希和页/块位置仍对应恢复文件。然后提交一个新的问答，等待任务成功，确认结果由真实 LightRAG Core 检索产生且引用能跳回原件。
6. 对照备份元数据和启动命令中显式设置的 Vault 根/父目录、数据库名、working 目录、API 端口、模型、维度和 workspace。用只读 `psql ... SELECT current_database()` 分别确认应用库与 RAG 库的目标名称；API 就绪接口只报告依赖状态，不显示这些配置值。Obsidian 只在上述检查通过后打开新的 Vault。

任一健康检查、SHA、来源当前修订、Wiki 历史或证据链接不符时，停止恢复实例并保留原实例和备份。哈希异常时绝不放行。调查后使用新的空目录和另一组新数据库名称重试；不要向原 Vault 合并文件，不要对原库执行恢复命令。

## 验收边界

截至 2026-10-03，数据库/Vault 恢复与真实 API 查询的实际验收范围是同一台 macOS 主机上的 PostgreSQL 16.14、pgvector 0.8.1 和 `lightrag-hku` 1.5.7。该演练用新的空 LightRAG working 目录启动恢复实例，因此没有验证有内容 working 目录的归档/解包往返；常规备份仍需纳入它。验收不能证明 Compose PostgreSQL 18 在新机器上的恢复行为，也没有覆盖物理断电恢复或 Obsidian Desktop 实际打开/同步验收。完整损坏工作区重建、自动备份/恢复和应用实体映射丢失后的重建仍未交付；本手册不将它们描述为已支持功能。

"""
candidates.db 初始化与公共数据库操作

所有脚本（collect / chat_loop / greeting 等）共用此模块，
保证表结构一致，不再各自散落 ALTER TABLE 补丁。
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "candidates.db"

# ── 完整 schema（新建表用） ──
_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT,
    name TEXT NOT NULL,
    job_position TEXT,
    school TEXT,
    degree TEXT,
    resume_content TEXT,
    resume_filename TEXT,
    has_resume INTEGER DEFAULT 0,
    wechat TEXT,
    has_wechat INTEGER DEFAULT 0,
    phone TEXT,
    email TEXT,
    score INTEGER DEFAULT 0,
    status TEXT DEFAULT 'collected',
    chat_history TEXT,
    notes TEXT,
    extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

# ── 兼容旧表：需要补齐的列 ──
_PATCH_COLUMNS = ("uid", "chat_history")


def init_db() -> sqlite3.Connection:
    """初始化 candidates.db，返回已连接的 sqlite3.Connection。

    - 表不存在 → 按完整 schema 创建
    - 表已存在但缺列 → ALTER TABLE 补齐
    - uid 唯一索引不存在 → 创建
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(_SCHEMA)

    # 兼容旧表：补齐新增列
    for col in _PATCH_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE candidates ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass  # 列已存在

    # uid 唯一索引
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_uid ON candidates(uid)"
        )
    except sqlite3.OperationalError:
        pass

    conn.commit()
    return conn

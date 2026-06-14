"""
candidates.db 初始化与公共数据库操作

所有脚本（collect / chat_loop / greeting 等）共用此模块，
保证表结构一致，不再各自散落 ALTER TABLE 补丁。
"""
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "candidates.db"
BACKUP_DIR = DB_PATH.parent / "backups"

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
    resume_path TEXT,
    has_resume INTEGER DEFAULT 0,
    wechat TEXT,
    has_wechat INTEGER DEFAULT 0,
    phone TEXT,
    email TEXT,
    score REAL DEFAULT 0,
    score_summary TEXT,
    scored_at TIMESTAMP,
    status TEXT DEFAULT 'collected',
    chat_history TEXT,
    notes TEXT,
    interview_type TEXT,
    interview_date TEXT,
    interview_time TEXT,
    interview_at TIMESTAMP,
    extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

# ── 兼容旧表：需要补齐的列（均为 TEXT，SQLite 动态类型对 score 数值无影响） ──
_PATCH_COLUMNS = (
    "uid", "chat_history", "resume_path",
    "score_summary", "scored_at",
    "interview_type", "interview_date", "interview_time", "interview_at",
    "updated_at",
)

# ── 数据更新时间戳：collect/chat_loop 等改动「相关数据列」时自动刷新 updated_at。
#    评分(score/scored_at)、面试(interview_*/status) 列不在监听范围，写它们不会
#    误触发 → 保证 scored_at 与 updated_at 的先后关系可用来判断「数据是否变新」。──
_TOUCH_COLUMNS = (
    "name", "job_position", "school", "degree",
    "resume_content", "resume_filename", "resume_path", "has_resume",
    "wechat", "has_wechat", "phone", "email", "chat_history", "notes",
)
_TOUCH_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS trg_candidates_touch
AFTER UPDATE OF {', '.join(_TOUCH_COLUMNS)} ON candidates
FOR EACH ROW
BEGIN
    UPDATE candidates SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;
"""


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

    # 旧表补列后 updated_at 为空 → 回填为 extracted_at（视为「上次数据时间」）
    conn.execute(
        "UPDATE candidates SET updated_at = extracted_at WHERE updated_at IS NULL"
    )

    # 数据变更自动刷新 updated_at 的触发器
    conn.execute(_TOUCH_TRIGGER)

    conn.commit()
    return conn


def record_score(
    conn: sqlite3.Connection,
    uid: str,
    score: float,
    summary: str = "",
) -> bool:
    """把评分结果写回 candidates 表（按 uid 匹配）。

    返回是否命中一行。scored_at 记为当前时间，供排行榜「最近 N 天」过滤。
    """
    if not uid:
        return False
    # scored_at 用 SQL CURRENT_TIMESTAMP（UTC），与 updated_at 触发器同基准，
    # 才能正确比较「数据是否比上次评分更新」。
    cur = conn.execute(
        "UPDATE candidates SET score = ?, score_summary = ?, "
        "scored_at = CURRENT_TIMESTAMP WHERE uid = ?",
        (round(float(score), 1), summary, uid),
    )
    conn.commit()
    return cur.rowcount > 0


def record_interview(
    conn: sqlite3.Connection,
    uid: str,
    interview_type: str,
    interview_date: str,
    interview_time: str,
) -> bool:
    """记录已预约的面试（按 uid 匹配），并把 status 置为 'interviewed'。

    返回是否命中一行。排行榜据此排除、面试提醒据此读取。
    """
    if not uid:
        return False
    cur = conn.execute(
        """UPDATE candidates
           SET interview_type = ?, interview_date = ?, interview_time = ?,
               interview_at = CURRENT_TIMESTAMP, status = 'interviewed'
           WHERE uid = ?""",
        (interview_type, interview_date, interview_time, uid),
    )
    conn.commit()
    return cur.rowcount > 0


def backup_db(suffix: str = "") -> Path:
    """备份当前 candidates.db 到 data/backups/ 目录

    文件名格式: candidates_YYYYMMDD_HHMMSS_<suffix>.db
    如果 DB 文件不存在则跳过，返回空 Path。

    用法:
      from app.db import backup_db
      path = backup_db("before-clear")
    """
    if not DB_PATH.exists():
        return Path()

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = f"_{suffix}" if suffix else ""
    dest = BACKUP_DIR / f"candidates_{ts}{tag}.db"
    shutil.copy2(str(DB_PATH), str(dest))
    return dest


def clear_db(backup: bool = True) -> None:
    """清空 candidates 表所有数据

    默认先备份再清空，防止误操作丢失数据。
    表结构和索引保留不变。

    用法:
      from app.db import clear_db
      clear_db()           # 自动备份 + 清空
      clear_db(backup=False) # 不备份直接清空（谨慎）
    """
    if backup and DB_PATH.exists():
        path = backup_db("before-clear")
        print(f"  ✓ 已备份: {path}")

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("DELETE FROM candidates")
    conn.commit()
    conn.close()
    print(f"  ✓ 已清空: {DB_PATH}")

#!/usr/bin/env python3
"""查询候选人数据库

用法:
  python scripts/query_db.py                          # 全部列表
  python scripts/query_db.py --name 张                 # 按名字搜索
  python scripts/query_db.py --school 清华             # 按学校搜索
  python scripts/query_db.py --has-resume              # 有简历的
  python scripts/query_db.py --has-wechat              # 有微信的
  python scripts/query_db.py --stats                   # 统计
  python scripts/query_db.py --export candidates.csv   # 导出CSV
"""
import sqlite3, argparse, csv, sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "candidates.db"


def get_conn():
    if not DB_PATH.exists():
        print(f"❌ DB 不存在: {DB_PATH}")
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def cmd_list(conn, args):
    wheres = ["1=1"]
    params = []
    if args.name:
        wheres.append("name LIKE ?")
        params.append(f"%{args.name}%")
    if args.school:
        wheres.append("school LIKE ?")
        params.append(f"%{args.school}%")
    if args.has_resume:
        wheres.append("has_resume = 1")
    if args.has_wechat:
        wheres.append("has_wechat = 1")
    if args.min_degree:
        degree_order = {"博士": 4, "硕士": 3, "本科": 2, "大专": 1}
        min_val = degree_order.get(args.min_degree, 2)
        allowed = [k for k, v in degree_order.items() if v >= min_val]
        wheres.append(f"degree IN ({','.join('?'*len(allowed))})")
        params.extend(allowed)

    sql = f"SELECT * FROM candidates WHERE {' AND '.join(wheres)} ORDER BY extracted_at DESC"
    rows = conn.execute(sql, params).fetchall()

    if not rows:
        print("(无结果)")
        return

    cols = ["id", "uid", "name", "job_position", "school", "degree",
            "has_resume", "has_wechat", "phone", "email", "extracted_at"]
    widths = {c: len(c) for c in cols}
    for r in rows:
        for c in cols:
            v = str(r[c] or "")
            widths[c] = max(widths[c], min(len(v), 40))

    # header
    header = " │ ".join(c.ljust(widths[c]) for c in cols)
    sep = "─┼─".join("─" * widths[c] for c in cols)
    print(f"\n{len(rows)} 条结果\n")
    print(header)
    print(sep)
    for r in rows:
        parts = []
        for c in cols:
            v = str(r[c] or "")
            parts.append(v[:40].ljust(widths[c]))
        print(" │ ".join(parts))


def cmd_stats(conn, _args):
    total = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    with_resume = conn.execute("SELECT COUNT(*) FROM candidates WHERE has_resume=1").fetchone()[0]
    with_wechat = conn.execute("SELECT COUNT(*) FROM candidates WHERE has_wechat=1").fetchone()[0]
    with_uid = conn.execute("SELECT COUNT(*) FROM candidates WHERE uid IS NOT NULL AND uid != ''").fetchone()[0]

    print(f"""
{'='*40}
  总候选人:      {total}
  有简历:        {with_resume}  ({with_resume*100//total if total else 0}%)
  有微信:        {with_wechat}  ({with_wechat*100//total if total else 0}%)
  有UID:         {with_uid}
{'='*40}

按学历:
""")
    for row in conn.execute("SELECT degree, COUNT(*) as c FROM candidates GROUP BY degree ORDER BY c DESC"):
        print(f"  {row[0] or '?'}: {row[1]}")

    print("\n按学校:")
    for row in conn.execute("SELECT school, COUNT(*) as c FROM candidates WHERE school != '' GROUP BY school ORDER BY c DESC LIMIT 10"):
        print(f"  {row[0]}: {row[1]}")


def cmd_export(conn, args):
    rows = conn.execute("SELECT * FROM candidates ORDER BY extracted_at DESC").fetchall()
    if not rows:
        print("(无数据)"); return
    with open(args.export, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(rows[0].keys())
        for r in rows:
            writer.writerow(r)
    print(f"✅ 导出 {len(rows)} 条 → {args.export}")


def main():
    p = argparse.ArgumentParser(description="查询候选人数据库")
    p.add_argument("--name", help="按名字搜索 (LIKE)")
    p.add_argument("--school", help="按学校搜索 (LIKE)")
    p.add_argument("--has-resume", action="store_true", help="只显示有简历的")
    p.add_argument("--has-wechat", action="store_true", help="只显示有微信的")
    p.add_argument("--min-degree", help="最低学历: 大专/本科/硕士/博士")
    p.add_argument("--stats", action="store_true", help="显示统计")
    p.add_argument("--export", help="导出 CSV 文件路径")
    args = p.parse_args()

    conn = get_conn()

    if args.stats:
        cmd_stats(conn, args)
    elif args.export:
        cmd_export(conn, args)
    else:
        cmd_list(conn, args)

    conn.close()


if __name__ == "__main__":
    main()

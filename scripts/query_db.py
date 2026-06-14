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

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
DB_PATH = PROJECT_ROOT / "data" / "candidates.db"

_DEGREE_ORDER = {"博士": 4, "硕士": 3, "本科": 2, "大专": 1}


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


def _degree_filter(min_degree):
    """生成学历过滤的 (sql片段, 参数列表)。"""
    min_val = _DEGREE_ORDER.get(min_degree, 2)
    allowed = [k for k, v in _DEGREE_ORDER.items() if v >= min_val]
    return f"degree IN ({','.join('?'*len(allowed))})", allowed


def cmd_rank(conn, args):
    """评分排行榜：评分 → 缓存 → 按总分排行。

    评分对象（默认）：**未评分** 的候选人，或 **相关数据在 N 天内更新过且比上次评分新**
    的候选人（重新评分）。N = scoring.json input_limits.rescore_window_days。
    - `--rescore`：展示窗口内全部强制重算  / `--no-score`：只读缓存不调 DeepSeek
    - 展示窗口：活跃时间 COALESCE(updated_at, extracted_at) 在最近 --days 天（默认 2，0=不限）
    - 默认排除 status='interviewed'（`--include-interviewed` 包含）；展示前 --top 名
    评分维度/权重/评级线/输入上限全部在 config/scoring.json，改配置即生效。
    """
    from app.scoring import load_scoring_config, input_limits, grade
    scfg = load_scoring_config()
    rescore_window = input_limits(scfg)["rescore_window_days"]

    # ── 展示窗口过滤（活跃时间 = 数据更新时间，没有则用收集时间）──
    disp_wheres = ["1=1"]
    disp_params = []
    if args.days and args.days > 0:
        disp_wheres.append(
            "julianday('now') - julianday(COALESCE(updated_at, extracted_at)) <= ?")
        disp_params.append(args.days)
    if not args.include_interviewed:
        disp_wheres.append("(status IS NULL OR status != 'interviewed')")
    if args.min_degree:
        frag, vals = _degree_filter(args.min_degree)
        disp_wheres.append(frag)
        disp_params.extend(vals)
    disp_where = " AND ".join(disp_wheres)

    # ── 选出需要评分的候选人 ──
    if not args.no_score:
        from app.db import record_score
        from app.scoring import (build_candidate_data, evaluate_candidate,
                                  evaluate_candidate_auto)
        from app.chat_reply import load_jobs_config, infer_category
        jobs = load_jobs_config().get("jobs", [])

        has_uid = "uid IS NOT NULL AND uid != ''"
        not_interviewed = "(status IS NULL OR status != 'interviewed')"
        # 简历附件是主要评分依据：无简历内容的候选人不评分
        has_resume = "resume_content IS NOT NULL AND TRIM(resume_content) != ''"
        if args.rescore:
            # 强制重算：展示窗口内、有 uid、有简历、未面试的全部
            to_score = conn.execute(
                f"SELECT * FROM candidates WHERE {disp_where} AND {has_uid} AND {has_resume}",
                disp_params,
            ).fetchall()
        else:
            # 默认：未评分(任意时间) ∪ (数据更新且在窗口内、比上次评分新)，且有简历
            to_score = conn.execute(
                f"SELECT * FROM candidates WHERE {has_uid} AND {not_interviewed} AND {has_resume} AND ("
                "  scored_at IS NULL"
                "  OR (julianday(COALESCE(updated_at, extracted_at)) > julianday(scored_at)"
                "      AND julianday('now') - julianday(COALESCE(updated_at, extracted_at)) <= ?)"
                ")",
                [rescore_window],
            ).fetchall()

        if to_score:
            mode = f"强制岗位={args.job_id}" if args.job_id else "DeepSeek 自行判断岗位"
            why = "强制重算" if args.rescore else f"未评分 + 近{rescore_window}天数据有更新"
            print(f"⚙ 需评分 {len(to_score)} 人（{why}；{mode}）…")
        for i, r in enumerate(to_score, 1):
            cdata = build_candidate_data(r)
            if args.job_id:
                job = next((j for j in jobs if j.get("id") == args.job_id), None)
                cat = args.category or (
                    infer_category(job.get("title", ""), job.get("requirements", ""))
                    if job else infer_category(cdata.get("job_position") or ""))
                job_ctx = (f"{job.get('title', '')} — {job.get('requirements', '')}"
                           if job else (cdata.get("job_position") or ""))
                sc = evaluate_candidate(cdata, job_id=args.job_id, category=cat,
                                        job_context=job_ctx, config=scfg)
            else:
                sc = evaluate_candidate_auto(cdata, jobs, config=scfg)
            # 跳过的（如无简历）不写分，保持未评分状态
            if sc.skipped:
                print(f"  [{i}/{len(to_score)}] {cdata['name']} → 跳过: "
                      f"{sc.errors[0] if sc.errors else '未满足评分条件'}")
                continue
            record_score(conn, cdata["uid"], sc.total_score, sc.summary)
            flag = " ⚠" if (sc.errors and sc.total_score == 0) else ""
            print(f"  [{i}/{len(to_score)}] {cdata['name']} → {sc.job_id or '?'}: "
                  f"{sc.total_score:.1f}{flag}")

        # 无 uid 的未评分候选人无法缓存，提示数量
        no_uid = conn.execute(
            f"SELECT COUNT(*) FROM candidates WHERE (uid IS NULL OR uid='') "
            f"AND scored_at IS NULL AND {disp_where}", disp_params
        ).fetchone()[0]
        if no_uid:
            print(f"  ⚠ {no_uid} 人无 uid，无法缓存评分，已跳过")

        # 无简历附件内容的未评分候选人，提示数量（简历是主要评分依据）
        no_resume = conn.execute(
            f"SELECT COUNT(*) FROM candidates WHERE {has_uid} "
            f"AND (resume_content IS NULL OR TRIM(resume_content) = '') "
            f"AND scored_at IS NULL AND {disp_where}", disp_params
        ).fetchone()[0]
        if no_resume:
            print(f"  ⚠ {no_resume} 人无简历附件内容，已跳过评分")

    # ── 排行榜展示 ──
    ranked = conn.execute(
        f"SELECT * FROM candidates WHERE {disp_where} "
        f"ORDER BY score DESC, scored_at DESC", disp_params
    ).fetchall()[:args.top]

    win = "不限" if not args.days or args.days <= 0 else f"最近{args.days}天活跃"
    excl = "" if args.include_interviewed else "，已排除面试过的"
    print(f"\n{'='*72}")
    print(f"  评分排行榜（{win}{excl}，前 {min(args.top, len(ranked))} 名）")
    print(f"{'='*72}")
    print(f"  {'#':<3s}{'姓名':<12s}{'学校':<14s}{'学历':<6s}{'总分':>6s}  {'评级':<10s}")
    print(f"  {'-'*68}")
    for i, r in enumerate(ranked, 1):
        score = float(r["score"] or 0)
        print(f"  {i:<3d}{(r['name'] or '')[:11]:<12s}{(r['school'] or '')[:13]:<14s}"
              f"{(r['degree'] or '')[:5]:<6s}{score:>6.1f}  {grade(score, scfg):<10s}")
        if r["score_summary"]:
            print(f"      └ {r['score_summary'][:60]}")
    print()


def main():
    p = argparse.ArgumentParser(description="查询候选人数据库")
    p.add_argument("--name", help="按名字搜索 (LIKE)")
    p.add_argument("--school", help="按学校搜索 (LIKE)")
    p.add_argument("--has-resume", action="store_true", help="只显示有简历的")
    p.add_argument("--has-wechat", action="store_true", help="只显示有微信的")
    p.add_argument("--min-degree", help="最低学历: 大专/本科/硕士/博士")
    p.add_argument("--stats", action="store_true", help="显示统计")
    p.add_argument("--export", help="导出 CSV 文件路径")
    # ── 评分排行榜 (--rank) ──
    p.add_argument("--rank", action="store_true",
                   help="评分排行榜：懒评分+缓存后按总分排行")
    p.add_argument("--days", type=int, default=2,
                   help="排行榜时间窗口(天)，默认 2，0=不限")
    p.add_argument("--top", type=int, default=10,
                   help="排行榜展示人数，默认 10")
    p.add_argument("--include-interviewed", action="store_true",
                   help="排行榜包含已面试候选人(默认排除)")
    p.add_argument("--rescore", action="store_true",
                   help="强制重新评分(忽略已缓存的 score)")
    p.add_argument("--no-score", action="store_true",
                   help="不调用 DeepSeek，仅按已缓存 score 排行")
    p.add_argument("--job-id", help="强制指定岗位 id(评分上下文，覆盖自动检测)")
    p.add_argument("--category", help="强制指定类别 tech/nontech(评分维度)")
    args = p.parse_args()

    conn = get_conn()

    if args.rank:
        cmd_rank(conn, args)
    elif args.stats:
        cmd_stats(conn, args)
    elif args.export:
        cmd_export(conn, args)
    else:
        cmd_list(conn, args)

    conn.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""面试提醒 — 读取 candidates.db 中已预约的面试并提醒

数据来源: cua_interview.py 预约成功后写入的 interview_date/time/type 字段
(status='interviewed')。

用法:
  python scripts/interview_reminder.py                 # 今天+明天的面试(默认窗口1天)
  python scripts/interview_reminder.py --within 3       # 未来3天内
  python scripts/interview_reminder.py --date 2026-06-20 # 指定某天
  python scripts/interview_reminder.py --all            # 所有未来面试
  python scripts/interview_reminder.py --notify         # 额外发 macOS 系统通知

适合做定时任务(如每天 6 点)，配合 --notify 推送提醒。
"""
import argparse
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from app.db import init_db  # noqa: E402


def _parse_date(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _rel_label(delta: int) -> str:
    if delta == 0:
        return "今天"
    if delta == 1:
        return "明天"
    if delta == 2:
        return "后天"
    if delta < 0:
        return f"{-delta}天前"
    return f"{delta}天后"


def _notify(title: str, message: str) -> None:
    """发送 macOS 系统通知(失败静默)。"""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "{title}"'],
            check=False, capture_output=True, timeout=5,
        )
    except Exception:
        pass


def main() -> None:
    p = argparse.ArgumentParser(description="BOSS直聘 — 面试提醒")
    p.add_argument("--within", type=int, default=1,
                   help="提醒未来几天内的面试，默认 1(今天+明天)")
    p.add_argument("--date", help="只看指定日期 YYYY-MM-DD")
    p.add_argument("--all", action="store_true", help="所有未来面试(忽略 --within)")
    p.add_argument("--notify", action="store_true", help="额外发送 macOS 系统通知")
    args = p.parse_args()

    conn = init_db()
    rows = conn.execute(
        "SELECT name, interview_type, interview_date, interview_time, "
        "school, job_position, wechat, phone FROM candidates "
        "WHERE interview_date IS NOT NULL AND interview_date != '' "
        "ORDER BY interview_date, interview_time"
    ).fetchall()
    conn.close()

    today = date.today()
    target = _parse_date(args.date) if args.date else None

    upcoming = []
    for r in rows:
        d = _parse_date(r[2])
        if not d:
            continue
        delta = (d - today).days
        if target is not None:
            if d != target:
                continue
        elif args.all:
            if delta < 0:
                continue
        else:
            if delta < 0 or delta > args.within:
                continue
        upcoming.append((delta, r))

    upcoming.sort(key=lambda x: (x[1][2], x[1][3] or ""))

    if not upcoming:
        scope = (f"{args.date}" if target else
                 ("未来所有" if args.all else f"未来 {args.within} 天内"))
        print(f"📭 {scope}没有已预约的面试")
        return

    print(f"\n{'='*64}")
    print(f"  📅 面试提醒（共 {len(upcoming)} 场）")
    print(f"{'='*64}")
    for delta, r in upcoming:
        name, itype, idate, itime, school, job, wechat, phone = r
        when = _rel_label(delta)
        contact = wechat or phone or "—"
        print(f"  ⏰ [{when}] {idate} {itime or ''}  {itype or ''}")
        print(f"     {name}  |  {school or '?'}  |  {job or '?'}  |  联系: {contact}")
        if args.notify:
            _notify(
                f"面试提醒 [{when}] {name}",
                f"{idate} {itime or ''} {itype or ''} · {job or ''}",
            )
    print()


if __name__ == "__main__":
    main()

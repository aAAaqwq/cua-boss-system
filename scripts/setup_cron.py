#!/usr/bin/env python3
"""
setup_cron.py — BOSS直聘自动化定时任务管理

安装/卸载/查看 crontab 定时任务。每天早上 9:30 跑全流程 pipeline。

用法:
    # 安装定时任务（每天 9:30）
    python scripts/setup_cron.py install

    # 安装带自定义参数
    python scripts/setup_cron.py install --greet 20 --collect 10 --chat 10

    # 查看当前任务
    python scripts/setup_cron.py list

    # 卸载所有 cua-boss-system 定时任务
    python scripts/setup_cron.py uninstall

    # 添加面试提醒（每天早上 6 点，可选）
    python scripts/setup_cron.py install --with-reminder
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent

# ── crontab 标记：用于标识/识别哪些行是我们添加的 ──
MARKER = "# cua-boss-system auto"
MARKER_START = f"{MARKER} (start)"
MARKER_END = f"{MARKER} (end)"


def get_crontab() -> list[str]:
    """读取当前用户的 crontab，返回行列表"""
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return r.stdout.splitlines()
        return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def set_crontab(lines: list[str]) -> bool:
    """写入 crontab"""
    text = "\n".join(lines) + "\n" if lines else ""
    try:
        r = subprocess.run(
            ["crontab", "-"],
            input=text,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def build_boss_path() -> str:
    """构建 PATH 和项目路径（含 homebrew python）"""
    homebrew = "/opt/homebrew/bin"
    local_bin = os.path.expanduser("~/.local/bin")
    path = f"PATH=\"{homebrew}:{local_bin}:/usr/bin:/bin\""
    return path


def build_pipeline_cmd(
    greet: int = 20,
    collect: int = 10,
    chat: int = 10,
    min_degree: str = "",
    schools: str = "",
) -> str:
    """构建 boss-pipeline 命令"""
    path = build_boss_path()
    cmd = f"cd {PROJECT_DIR} && {path} python3 scripts/boss_pipeline.py"
    cmd += f" --greet {greet} --collect {collect} --chat {chat}"
    if min_degree:
        cmd += f" --min-degree {min_degree}"
    if schools:
        cmd += f' --schools "{schools}"'
    cmd += f" >> {PROJECT_DIR}/data/pipeline.log 2>&1"
    return cmd


def build_reminder_cmd(within_days: int = 1) -> str:
    """构建面试提醒命令"""
    path = build_boss_path()
    cmd = f"cd {PROJECT_DIR} && {path} python3 scripts/interview_reminder.py"
    cmd += f" --within {within_days} --notify"
    cmd += f" >> {PROJECT_DIR}/data/reminder.log 2>&1"
    return cmd


def build_sync_cmd() -> str:
    """构建职位同步命令"""
    path = build_boss_path()
    cmd = f"cd {PROJECT_DIR} && {path} python3 scripts/cua_sync_jobs.py"
    cmd += f" >> {PROJECT_DIR}/data/sync.log 2>&1"
    return cmd


def install(
    greet: int = 20,
    collect: int = 10,
    chat: int = 10,
    min_degree: str = "",
    schools: str = "",
    with_reminder: bool = False,
    reminder_time: str = "6",
    with_weekly_sync: bool = True,
) -> bool:
    """安装定时任务"""
    # 读取现有 crontab，移除旧版 cua-boss-system 标记块
    existing = get_crontab()
    new_lines = []
    skip_block = False
    for line in existing:
        if MARKER_START in line:
            skip_block = True
            continue
        if MARKER_END in line:
            skip_block = False
            continue
        if not skip_block:
            # 也移除旧版无标记的行（精确匹配之前版本的 cron 行）
            if "boss_pipeline.py" not in line and "cua-boss-system" not in line:
                new_lines.append(line)

    # ── 添加新任务 ──
    new_lines.append("")
    new_lines.append(MARKER_START)
    new_lines.append(f"# 安装时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}")

    # 1) 每天 9:30 跑全流程 pipeline
    pipeline_cmd = build_pipeline_cmd(greet, collect, chat, min_degree, schools)
    new_lines.append(f"# Boss直聘全流程：打招呼{greet}人 → 收集{collect}人 → 沟通{chat}人")
    new_lines.append(f"30 9 * * * {pipeline_cmd}")

    # 2) 面试提醒（每天可选）
    if with_reminder:
        reminder_cmd = build_reminder_cmd()
        new_lines.append(f"# 面试提醒（每天 {reminder_time}:00）")
        new_lines.append(f"0 {reminder_time} * * * {reminder_cmd}")

    # 3) 每周一同步职位
    if with_weekly_sync:
        sync_cmd = build_sync_cmd()
        new_lines.append(f"# 每周一同步职位信息")
        new_lines.append(f"0 9 * * 1 {sync_cmd}")

    new_lines.append(MARKER_END)
    new_lines.append("")

    ok = set_crontab(new_lines)
    if ok:
        print("✅ 定时任务已安装！每天早上 9:30 自动跑全流程。")
        print(f"   项目路径: {PROJECT_DIR}")
        print(f"   打招呼={greet}  收集={collect}  沟通={chat}")
        if with_reminder:
            print(f"   面试提醒: ✅ 每天 {reminder_time}:00")
        if with_weekly_sync:
            print("   职位同步: ✅ 每周一 9:00")
        print()
        print("查看日志: tail -f data/pipeline.log")
        print("取消任务: python scripts/setup_cron.py uninstall")
    else:
        print("❌ 写入 crontab 失败")
    return ok


def uninstall() -> bool:
    """卸载所有 cua-boss-system 定时任务"""
    existing = get_crontab()
    new_lines = []
    removed = 0
    skip_block = False
    for line in existing:
        if MARKER_START in line:
            skip_block = True
            removed += 1
            continue
        if MARKER_END in line:
            skip_block = False
            removed += 1
            continue
        if not skip_block:
            new_lines.append(line)

    if removed == 0:
        print("📭 未找到 cua-boss-system 定时任务")
        return True

    ok = set_crontab(new_lines)
    if ok:
        print(f"✅ 已卸载 {removed} 行 cua-boss-system 定时任务")
    else:
        print("❌ 卸载失败")
    return ok


def list_tasks() -> None:
    """列出所有 cua-boss-system 定时任务"""
    existing = get_crontab()
    if not existing:
        print("📭 crontab 为空")
        return

    found = False
    in_block = False
    for line in existing:
        if MARKER_START in line:
            in_block = True
            found = True
            print("───── cua-boss-system 定时任务 ─────")
            continue
        if MARKER_END in line:
            in_block = False
            print("──────────────────────────────────")
            continue
        if in_block:
            print(f"  {line}")

    if not found:
        print("📭 未安装 cua-boss-system 定时任务")
        print()
        print("安装: python scripts/setup_cron.py install")


def main():
    parser = argparse.ArgumentParser(
        description="cua-boss-system 定时任务管理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="action", required=True)

    # install
    p_install = sub.add_parser("install", help="安装定时任务（每天 9:30 跑全流程）")
    p_install.add_argument("--greet", type=int, default=20, help="打招呼人数（默认 20）")
    p_install.add_argument("--collect", type=int, default=10, help="收集人数（默认 10）")
    p_install.add_argument("--chat", type=int, default=10, help="沟通人数（默认 10）")
    p_install.add_argument("--min-degree", default="", help="最低学历过滤")
    p_install.add_argument("--schools", default="", help="学校白名单（逗号分隔）")
    p_install.add_argument(
        "--with-reminder",
        action="store_true",
        help="额外安装面试提醒",
    )
    p_install.add_argument(
        "--reminder-time",
        default="6",
        help="面试提醒时间（24小时制，默认 6）",
    )
    p_install.add_argument(
        "--no-weekly-sync",
        action="store_true",
        help="不安装每周职位同步",
    )

    sub.add_parser("uninstall", help="卸载所有 cua-boss-system 定时任务")
    sub.add_parser("list", help="查看当前定时任务")

    args = parser.parse_args()

    if args.action == "install":
        install(
            greet=args.greet,
            collect=args.collect,
            chat=args.chat,
            min_degree=args.min_degree,
            schools=args.schools,
            with_reminder=args.with_reminder,
            reminder_time=args.reminder_time,
            with_weekly_sync=not args.no_weekly_sync,
        )
    elif args.action == "uninstall":
        uninstall()
    elif args.action == "list":
        list_tasks()


if __name__ == "__main__":
    main()

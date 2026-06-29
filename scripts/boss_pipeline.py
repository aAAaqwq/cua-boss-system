#!/usr/bin/env python3
"""boss-pipeline — BOSS直聘全流程编排：打招呼 → 收集 → 智能沟通

把原先分散的三个脚本串成一条可参数化的流水线，顺序执行、前一步成功才进下一步。
取代独立的 boss-full-pipeline skill —— agent 读 SKILL.md 后直接调本脚本并按需调参。

用法:
  python scripts/boss_pipeline.py                          # 打招呼20 / 收集5 / 沟通5(默认)
  python scripts/boss_pipeline.py --greet 100 --collect 30 --chat 30
  python scripts/boss_pipeline.py --min-degree 硕士 --schools "清华,北大"
  python scripts/boss_pipeline.py --dry-run                 # 全程预览不操作
  python scripts/boss_pipeline.py --skip-greet              # 从收集开始(续跑)

退出码: 0=全部成功；非 0=某步失败(失败步骤之后不再执行)。
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
SCRIPTS = PROJECT_ROOT / "scripts"

TO_LIMIT_WORDS = {"max", "上限", "不限", "满", "顶"}


def parse_limit(value: str) -> int:
    """--greet 接受正整数，或 max/上限/0(=打招呼打到每日上限自动停)。"""
    v = str(value).strip().lower()
    if v in TO_LIMIT_WORDS or v == "0":
        return 0  # 0 = 打到每日上限或候选人耗尽才停
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--greet 需为正整数或 max/上限，收到: {value!r}")
    if n < 0:
        raise argparse.ArgumentTypeError("--greet 不能为负")
    return n


def _run_step(name: str, script: str, step_args: list[str]) -> bool:
    """执行单个步骤脚本，实时透传输出。返回是否成功(退出码 0)。"""
    cmd = [sys.executable, str(SCRIPTS / script), *step_args]
    print(f"\n{'='*64}")
    print(f"▶ {name}")
    print(f"  $ {' '.join(cmd)}")
    print(f"{'='*64}", flush=True)

    env = dict(os.environ)
    # 确保 cua-driver 在 PATH 中
    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin not in env.get("PATH", ""):
        env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"

    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env)
    ok = result.returncode == 0
    print(f"{'✓ 成功' if ok else f'✗ 失败(退出码 {result.returncode})'}: {name}")
    return ok


def main() -> None:
    p = argparse.ArgumentParser(
        description="BOSS直聘全流程编排(打招呼→收集→沟通)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--greet", type=parse_limit, default=20,
                   help="打招呼步骤的 --limit：成功打招呼的人数(非读卡片数)，"
                        "不符合筛选的会自动跳过并多翻卡片直到打满，默认 20；"
                        "填 max/上限/0 = 打到每日上限自动停")
    p.add_argument("--collect", type=int, default=20,
                   help="收集步骤的 --limit：从聊天联系人列表【顶部往下处理的联系人个数】"
                        "(含被筛掉/无简历跳过的，按列表顺序前 N 个)，默认 20")
    p.add_argument("--chat", type=int, default=20,
                   help="沟通步骤的 --limit：从聊天联系人列表【顶部往下审查的联系人个数】"
                        "(含被筛掉/已回复/跳过的，按列表顺序前 N 个)，默认 20")
    p.add_argument("--min-degree", help="最低学历(本科/硕士/博士)，传给各步骤")
    p.add_argument("--schools", help="学校白名单(逗号分隔)，传给打招呼/沟通步骤")
    p.add_argument("--dry-run", action="store_true", help="全程预览，不实际操作")
    p.add_argument("--skip-greet", action="store_true", help="跳过打招呼")
    p.add_argument("--skip-collect", action="store_true", help="跳过收集")
    p.add_argument("--skip-chat", action="store_true", help="跳过智能沟通")
    args = p.parse_args()

    from app.cloud_sync import require_account
    require_account()  # 许可门禁：必须用我们下发的账号登录才能运行

    greet_disp = "到上限" if args.greet <= 0 else f"{args.greet}人"

    common = []
    if args.min_degree:
        common += ["--min-degree", args.min_degree]
    if args.dry_run:
        common += ["--dry-run"]

    # 步骤定义: (开关, 名称, 脚本, 该步参数)
    steps = [
        (
            not args.skip_greet,
            f"Step 1/3 主动打招呼 (limit={greet_disp})",
            "cua_greeting_loop.py",
            ["--limit", str(args.greet)]
            + (["--schools", args.schools] if args.schools else [])
            + common,
        ),
        (
            not args.skip_collect,
            f"Step 2/3 收集简历+微信 (limit={args.collect})",
            "cua_collect.py",
            ["--limit", str(args.collect)] + common,
        ),
        (
            not args.skip_chat,
            f"Step 3/3 智能沟通 (limit={args.chat})",
            "cua_chat_loop.py",
            ["--limit", str(args.chat)]
            + (["--schools", args.schools] if args.schools else [])
            + common,
        ),
    ]

    print("🚀 boss-pipeline 启动")
    print(f"   打招呼={greet_disp} 收集={args.collect} 沟通={args.chat}"
          f" | dry_run={args.dry_run}")

    ran = 0
    for enabled, name, script, step_args in steps:
        if not enabled:
            print(f"\n⏭️  跳过: {name}")
            continue
        ran += 1
        if not _run_step(name, script, step_args):
            print(f"\n❌ pipeline 在「{name}」中断。修复后可用 --skip-* 跳过已完成步骤续跑。")
            sys.exit(1)

    print(f"\n🎉 boss-pipeline 完成（执行 {ran} 步）")


if __name__ == "__main__":
    main()

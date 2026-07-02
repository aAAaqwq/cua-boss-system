#!/usr/bin/env python3
"""scripts/doctor.py — 装机自检（前置就绪体检）

跑一下把【能自动测的前置】全测了，输出 ✅/✗ 报告 + 对「必须人工确认」项的提示。
既是排障工具，也是未来桌面 App「首次引导」的内核。

用法:
  python scripts/doctor.py            # 体检并打印报告
  python scripts/doctor.py --json     # JSON 输出(给 App/前端)
退出码: 0=关键项全过；非 0=有关键项未过。
"""
import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _ok(name, ok, detail="", critical=True):
    return {"name": name, "ok": bool(ok), "detail": detail, "critical": critical}


def _cmd_ver(cmd, args=("--version",)):
    exe = shutil.which(cmd)
    if not exe:
        return False, ""
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=8)
        return True, (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else ""
    except Exception as e:  # 二进制在但跑不起来：仍算装了，但把故障暴露出来
        return True, f"已安装但无法执行: {type(e).__name__}"


def run_checks() -> list[dict]:
    checks = []

    # 系统依赖
    checks.append(_ok("Python ≥3.10", sys.version_info >= (3, 10),
                      f"{sys.version_info.major}.{sys.version_info.minor}"))
    ok, ver = _cmd_ver("cua-driver")
    checks.append(_ok("cua-driver 已装", ok, ver))
    checks.append(_ok("swiftc 可用", shutil.which("swiftc") is not None, critical=False))
    checks.append(_ok("Chrome 已装", Path("/Applications/Google Chrome.app").exists()))

    # .env 密钥
    env = {}
    envp = ROOT / ".env"
    if envp.exists():
        for line in envp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    checks.append(_ok("DeepSeek API Key", bool(env.get("DEEPSEEK_API_KEY")),
                      "未配则智能回复/伯乐降级"))
    checks.append(_ok("Supabase 配置", True,
                      "URL/anon 已内置默认，无需手配", critical=False))

    # 业务配置（缺则用 -template 兜底）
    for f in ("config/jobs.json", "config/reply.json", "config/filter.json", "config/scoring.json"):
        checks.append(_ok(f, (ROOT / f).exists()
                          or (ROOT / f.replace(".json", "-template.json")).exists()
                          or (ROOT / f.replace(".json", "-templates.json")).exists(),
                          critical=False))

    # 登录态（许可门禁）
    email = ""
    authp = ROOT / "data" / ".cloud_auth.json"
    if authp.exists():
        try:
            d = json.loads(authp.read_text(encoding="utf-8"))
            email = d.get("email") or d.get("user", {}).get("email", "")
        except Exception:
            pass
    checks.append(_ok("已登录(许可门禁)", bool(email), email or "未登录→脚本会拒跑"))

    # 数据库
    dbp = ROOT / "data" / "candidates.db"
    n = ""
    if dbp.exists():
        try:
            n = str(sqlite3.connect(str(dbp)).execute(
                "SELECT COUNT(*) FROM candidates").fetchone()[0]) + " 行"
        except Exception:
            n = "存在"
    checks.append(_ok("candidates.db", dbp.exists(), n or "首跑自动建", critical=False))

    return checks


# 脚本测不到的「运行时状态」，必须人工确认
MANUAL = [
    "Chrome 已登录 BOSS 直聘，并停在对应页面",
    "系统设置→隐私→辅助功能 + 屏幕录制：已授权运行程序（否则 AX 识别失败）",
    "Chrome 菜单→显示→开发者→☑️ 允许来自 Apple 事件的 JavaScript",
    "跑长任务/定时时：电脑保持开机登录、关闭自动睡眠",
]

# 缺失即可从模板自动生成的配置
_CONFIG_TEMPLATES = {
    "config/jobs.json": "config/jobs-template.json",
    "config/reply.json": "config/reply-templates.json",
    "config/filter.json": "config/filter-template.json",
    "config/scoring.json": "config/scoring-template.json",
}
# macOS TCC 权限：程序【不能】自行授予（SIP/TCC 设计），只能深链到设置面板由用户手动开
_PERM_LINKS = [
    {"name": "辅助功能（Accessibility）",
     "url": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"},
    {"name": "屏幕录制（Screen Recording）",
     "url": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"},
    {"name": "自动化（Apple 事件）",
     "url": "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"},
]


def fix() -> dict:
    """自动修可自动修的：缺失配置从模板生成。返回结果 + 仍需人工的项（权限深链/安装命令）。

    注意：macOS 权限（辅助功能/屏幕录制/自动化）程序无法自行授予，只能深链引导；
    cua-driver 二进制来源未内置，Chrome 给命令，均不静默安装（避免擅自改机器）。
    DeepSeek Key 到桌面端「设置」页填。
    """
    fixed, still = [], []
    for tgt, tpl in _CONFIG_TEMPLATES.items():
        p, t = ROOT / tgt, ROOT / tpl
        if p.exists():
            continue
        if not t.exists():
            still.append(f"{tgt}: 模板 {tpl} 也不存在")
            continue
        try:
            p.write_text(t.read_text(encoding="utf-8"), encoding="utf-8")
            fixed.append(f"{tgt} ← {tpl}")
        except Exception as e:  # noqa: BLE001
            still.append(f"{tgt} 生成失败: {e}")

    manual_install = []
    if not shutil.which("cua-driver"):
        manual_install.append({"name": "cua-driver",
                               "how": "按官方说明装其二进制到 PATH（本项目不内置安装源）"})
    if not Path("/Applications/Google Chrome.app").exists():
        how = "brew install --cask google-chrome" if shutil.which("brew") else "从 google.com/chrome 下载"
        manual_install.append({"name": "Google Chrome", "how": how})

    return {"ok": True, "fixed": fixed, "still": still,
            "perm_links": _PERM_LINKS, "manual_install": manual_install}


def main() -> None:
    p = argparse.ArgumentParser(description="装机自检（前置就绪体检）")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument("--fix", action="store_true", help="自动修可修项(缺失配置从模板生成)+列出需人工的权限/安装")
    args = p.parse_args()

    if args.fix:
        r = fix()
        if args.json:
            print(json.dumps(r, ensure_ascii=False))
            sys.exit(0)
        print("=" * 56 + "\n  伯乐 · 一键修复\n" + "=" * 56)
        for f in r["fixed"]:
            print(f"  ✅ 已生成 {f}")
        if not r["fixed"]:
            print("  （没有可自动生成的缺失配置）")
        for s in r["still"]:
            print(f"  ⚠️  {s}")
        if r["manual_install"]:
            print("\n  ── 需你手动安装 ──")
            for m in r["manual_install"]:
                print(f"  ◻︎ {m['name']}: {m['how']}")
        print("\n  ── 需你手动授权（macOS 不允许程序自行授予）──")
        for pl in r["perm_links"]:
            print(f"  ◻︎ {pl['name']}\n     {pl['url']}")
        print("  ℹ️  DeepSeek Key 到桌面端「设置」页填。")
        print("=" * 56)
        sys.exit(0)

    checks = run_checks()
    crit_fail = [c for c in checks if c["critical"] and not c["ok"]]

    if args.json:
        print(json.dumps({"checks": checks, "manual": MANUAL,
                          "pass": not crit_fail}, ensure_ascii=False))
        sys.exit(0 if not crit_fail else 1)

    print("=" * 56)
    print("  伯乐 · 装机自检")
    print("=" * 56)
    for c in checks:
        mark = "✅" if c["ok"] else ("❌" if c["critical"] else "⚠️ ")
        tail = f"  ({c['detail']})" if c["detail"] else ""
        print(f"  {mark} {c['name']}{tail}")
    print("\n  ── 以下需你人工确认（脚本测不到）──")
    for m in MANUAL:
        print(f"  ◻︎ {m}")
    print("=" * 56)
    if crit_fail:
        print(f"  ❌ 有 {len(crit_fail)} 项关键前置未就绪，请先补齐：")
        for c in crit_fail:
            print(f"     - {c['name']}")
        sys.exit(1)
    print("  ✅ 关键前置就绪，可以开跑。")
    sys.exit(0)


if __name__ == "__main__":
    main()

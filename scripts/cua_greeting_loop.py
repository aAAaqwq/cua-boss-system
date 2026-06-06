#!/usr/bin/env python3
"""
cua-driver 驱动的 BOSS直聘打招呼
==================================

逐个遍历候选人卡片 → 提取学校/学历 → 筛选判断 → 打招呼 → 检测上限

用法:
  python scripts/cua_greeting_loop.py              # 扫描→筛选→打招呼(最多判断20人)
  python scripts/cua_greeting_loop.py --dry-run    # 仅预览
  python scripts/cua_greeting_loop.py --limit 10   # 最多判断10人
  python scripts/cua_greeting_loop.py --schools "清华,北大,浙大"  # 自定义学校
"""
import argparse, json, random, subprocess, sys, time, re
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.filter_criteria import ALL_ELITE_SCHOOLS, match_school

SESSION_ID = "boss-greeting"
CHROME_BUNDLE_ID = "com.google.Chrome"

# ── 限制检测关键词 ──
LIMIT_KEYWORDS = [
    # 打招呼次数限制 (完整短语, 避免误匹配)
    "已达上限", "次数已用完", "今日已达", "已达每日",
    "沟通人数已达", "打招呼次数", "超出限制",
    "明天再来", "今日上限", "已达当天",
    "每天最多", "上限了", "用完了", "今日沟通",
    # 开料权益限制
    "权益不足", "开料次数", "剩余次数", "次数不足",
    "会员权益", "升级会员", "额度不足", "免费次数",
    "今日剩余",
]


def cua(*args: str) -> dict:
    cmd = ["cua-driver"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return {}
    stdout = result.stdout.strip()
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"text": stdout[:200]}


# ══════════════════════════════════════════════════
# 页面加载等待
# ══════════════════════════════════════════════════

def wait_for_page(pid: int, window_id: int, timeout: float = 15.0, label: str = "") -> bool:
    """等待 SPA 渲染完成。双重检测: JS readyState + AX 树卡片数"""
    prefix = f"[{label}] " if label else ""
    delays = [0.5, 0.5, 1.0, 1.0, 1.5, 1.5, 2.0, 2.0, 3.0, 3.0]
    elapsed = 0.0

    for delay in delays:
        time.sleep(delay)
        elapsed += delay

        # 1: JS readyState
        r = cua("page", json.dumps({
            "pid": pid, "window_id": window_id,
            "action": "execute_javascript",
            "javascript": "document.readyState",
        }))
        ready_val = " ".join(str(x) for x in r) if isinstance(r, list) else str(r.get("result", r.get("text", "")))
        if ready_val.strip().strip('"') != "complete":
            continue

        # 2: AX 树
        snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": window_id}))
        tree = snap.get("tree_markdown", "")
        greet_count = tree.count("打招呼") if tree else 0
        elem_count = snap.get("element_count", 0)

        if greet_count >= 5 and elem_count > 500:
            print(f"  {prefix}✓ 就绪 ({greet_count}卡片, {elapsed:.1f}s)")
            return True
        if greet_count > 0:
            print(f"  {prefix}加载中... ({greet_count}卡片, {elapsed:.1f}s)")

    print(f"  {prefix}⚠ 超时 ({elapsed:.1f}s)")
    return False


# ══════════════════════════════════════════════════
# 限制检测
# ══════════════════════════════════════════════════

def dismiss_limit_popup(pid: int, window_id: int):
    """关闭限制弹窗 — JS移除DOM (BOSS弹窗无AX关闭按钮,像素点击也常无效)"""
    print(f"  关闭弹窗...", end=" ", flush=True)

    # 直接移除弹窗DOM (最可靠)
    r = cua("page", json.dumps({
        "pid": pid, "window_id": window_id,
        "action": "execute_javascript",
        "javascript": """
        (function(){
            var removed = 0;
            document.querySelectorAll(
                '.dialog-wrap, [class*=overlay], [class*=mask], [class*=backdrop], ' +
                '.boss-popup__wrapper, [class*=modal]'
            ).forEach(function(el){
                var s = getComputedStyle(el);
                if ((s.position === 'fixed' || s.zIndex > 100) && s.display !== 'none') {
                    el.remove(); removed++;
                }
            });
            return 'removed ' + removed;
        })()
        """
    }))
    time.sleep(0.3)

    # Escape 兜底
    cua("press_key", json.dumps({"pid": pid, "window_id": window_id, "key": "escape"}))
    time.sleep(0.3)

    # 验证
    snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": window_id}))
    tree = snap.get("tree_markdown", "")
    closed = not any(kw in tree for kw in LIMIT_KEYWORDS)
    print("✓ 已关闭" if closed else "⚠ 仍在")


def check_limit_popup(pid: int, window_id: int) -> Optional[str]:
    """点击打招呼后检测是否弹出每日上限提示

    BOSS直聘限制弹窗出现在点击"打招呼"按钮之后:
      - toast: 页面顶部/中间浮层提示
      - modal: 居中弹窗 "今日沟通人数已达上限"
      - 按钮变灰/消失
    """
    # 快速轮询 3 次 (弹窗通常在 0.3-0.8s 内出现)
    for attempt in range(3):
        if attempt > 0:
            time.sleep(0.4)

        # 1. JS 只扫描可见弹窗/toast (跳过隐藏和固定侧边栏)
        r = cua("page", json.dumps({
            "pid": pid, "window_id": window_id,
            "action": "execute_javascript",
            "javascript": """
            (function(){
                var texts = [];
                document.querySelectorAll(
                    '[class*=toast], [class*=popup], [class*=modal], [class*=dialog], ' +
                    '[class*=notice], [class*=tip], [class*=message], [class*=snackbar], ' +
                    '[class*=alert], [class*=confirm]'
                ).forEach(function(el){
                    var style = getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return;
                    var rect = el.getBoundingClientRect();
                    if (rect.width < 50 || rect.height < 10) return;
                    var t = (el.textContent || '').trim();
                    if (t.length > 2 && t.length < 200) texts.push(t);
                });
                return JSON.stringify(texts);
            })()
            """
        }))
        popup_text = " ".join(str(x) for x in r) if isinstance(r, list) else str(r.get("result", r.get("text", "")))
        for kw in LIMIT_KEYWORDS:
            if kw in popup_text:
                return f"弹窗: {kw}"

        # 2. AX 树扫描 (兜底 JS 不可达的情况)
        snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": window_id}))
        tree = snap.get("tree_markdown", "")
        for line in tree.split("\n"):
            for kw in LIMIT_KEYWORDS:
                if kw in line and ("StaticText" in line or "AXButton" in line or "AXGroup" in line):
                    return f"页面: {kw}"

        # 3. 检查打招呼按钮是否仍然存在 (按钮消失=可能已达上限)
        greet_count = tree.count("打招呼") if tree else 0
        if greet_count == 0:
            return "按钮消失: 页面可能已切换或触发限制"

    return None


# ══════════════════════════════════════════════════
# 会话 & 窗口
# ══════════════════════════════════════════════════

def start_session():
    print("1. 启动会话...")
    if "running" not in str(cua("status")).lower():
        subprocess.run(["cua-driver", "serve"], capture_output=True)
        time.sleep(1)
    cua("start_session", json.dumps({"session": SESSION_ID}))


def find_boss_window() -> dict:
    print("2. 查找 BOSS直聘窗口...")
    apps = cua("list_apps")
    chrome_pid = None
    for app in apps.get("apps", []):
        if app.get("bundle_id") == CHROME_BUNDLE_ID and app.get("running"):
            chrome_pid = app.get("pid")
            break
    if not chrome_pid:
        print("  ❌ Chrome 未运行"); sys.exit(1)

    lw = cua("list_windows", json.dumps({"pid": chrome_pid}))

    # 优先匹配 zhipin.com 页面（排除控制台等其他 BOSS 窗口）
    for w in lw.get("windows", []):
        title = w.get("title", "")
        if ("zhipin" in title or "BOSS直聘" == title.strip()) and w.get("is_on_screen"):
            print(f"  ✓ id={w['window_id']} '{title[:60]}'")
            return {"pid": chrome_pid, "window_id": w["window_id"]}

    # 兜底: 任何含 BOSS 的窗口
    for w in lw.get("windows", []):
        title = w.get("title", "")
        if ("BOSS" in title or "zhipin" in title) and w.get("is_on_screen"):
            print(f"  ✓ id={w['window_id']} '{title[:60]}'")
            return {"pid": chrome_pid, "window_id": w["window_id"]}

    for w in lw.get("windows", []):
        if "BOSS" in w.get("title", "") or "zhipin" in w.get("title", ""):
            print(f"  ⚠ 窗口隐藏, 请点 Dock 中 Chrome 使其可见"); sys.exit(1)

    print("  ❌ 找不到 BOSS直聘窗口"); sys.exit(1)


RECOMMEND_URL = "https://www.zhipin.com/web/chat/recommend"


def navigate_to_recommend(pid: int, window_id: int):
    """跳转到推荐牛人页面并等待加载完成"""
    print("3. 进入推荐牛人页面...")
    cua("page", json.dumps({
        "pid": pid, "window_id": window_id,
        "action": "execute_javascript",
        "javascript": f'window.location.href = "{RECOMMEND_URL}"',
    }))
    # 先等页面基本加载，再检测 AX 树
    print("  等待页面加载...", end=" ", flush=True)
    time.sleep(5)
    wait_for_page(pid, window_id, label="加载")


# ══════════════════════════════════════════════════
# 核心: 逐个遍历 → 提取 → 筛选 → 打招呼
# ══════════════════════════════════════════════════

def _parse_candidates_from_tree(tree: str) -> list[dict]:
    """从 AX 树文本解析候选人卡片列表 (学校/学历/名字/打招呼按钮索引)"""
    candidates = []
    current = {}
    for line in tree.split("\n"):
        s = line.strip()

        m = re.search(r'AXStaticText\s*=\s*"([一-龥]{2,8}(?:大学|学院|学校))"', s)
        if m:
            current["school"] = m.group(1)
            current.pop("degree", None)  # 遇到新学校, 重置学历

        m = re.search(r'AXStaticText\s*=\s*"(博士|硕士|本科|大专)"', s)
        if m and "school" in current and "degree" not in current:
            current["degree"] = m.group(1)

        m = re.search(r'AXImage\s*\(\s*(\S+?)\s*\)', s)
        if m:
            name = m.group(1)
            if not name.startswith("/") and "." not in name and len(name) < 20:
                current["name"] = name

        if "打招呼" in s and "AXButton" in s:
            m = re.search(r'\[(\d+)\]', s)
            if m:
                current["greet_index"] = int(m.group(1))
            if current.get("school"):
                candidates.append(dict(current))
            current = {}

    # 去重
    seen = set()
    unique = []
    for c in candidates:
        key = (c.get("name", ""), c.get("school", ""))
        if key not in seen and key[0] != "":
            seen.add(key)
            unique.append(c)

    return unique


def _refresh_page(pid: int, wid: int):
    """刷新推荐页并等待加载"""
    print("  🔄 刷新页面获取新候选人...", end=" ", flush=True)
    cua("hotkey", json.dumps({"pid": pid, "window_id": wid, "keys": ["cmd", "r"]}))
    wait_for_page(pid, wid, label="刷新")


def process_candidates(
    pid: int,
    wid: int,
    school_whitelist: list[str],
    target_degree: str,
    limit: int,
    dry_run: bool = False,
) -> tuple[int, int, str]:
    """逐个遍历候选人卡片, 边筛边打, 卡片耗尽自动刷新

    对每个卡片:
      1. 提取学校名 / 学历 / 名字 / 打招呼按钮索引
      2. 判断: 学历 == target_degree 且 学校 in whitelist → 通过
      3. 通过 → 点击打招呼 (dry_run 跳过)
      4. 检测上限弹窗

    当前页卡片耗尽时自动刷新获取新卡片, 直到:
      - 已判断卡片数 >= limit
      - 沟通次数上限弹窗

    返回: (greeted, judged, stop_reason)
    """
    print("4. 扫描候选人并逐个处理...\n")

    greeted = 0
    judged = 0
    seen_candidates = set()  # 跨页去重: (name, school)
    stop_reason = "完成"
    page_round = 1

    while judged < limit:
        # ── 获取 AX 树 ──
        snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": wid}))
        tree = snap.get("tree_markdown", "")
        if not tree:
            print("  ⚠ AX 树为空")
            stop_reason = "AX树为空"
            break

        candidates = _parse_candidates_from_tree(tree)

        # 跨页去重
        fresh = [c for c in candidates if (c.get("name"), c.get("school")) not in seen_candidates]
        dupes = len(candidates) - len(fresh)

        if page_round == 1:
            print(f"  [第{page_round}页] 扫描到 {len(candidates)} 人")
        else:
            print(f"\n  [第{page_round}页] 扫描到 {len(candidates)} 人 (去重 {dupes} | 累计判断 {judged}/{limit})")

        # ── 逐个判断 + 打招呼 ──
        page_judged = 0

        for c in fresh:
            if judged >= limit:
                stop_reason = f"达到判断上限 ({limit})"
                print(f"\n⏹ {stop_reason}")
                break

            name = c.get("name", "?")
            school = c.get("school", "?")
            degree = c.get("degree", "?")
            idx = c.get("greet_index")

            seen_candidates.add((name, school))
            judged += 1
            page_judged += 1

            # 筛选判断
            degree_pass = degree == target_degree
            school_pass = match_school(school, school_whitelist)
            passed = degree_pass and school_pass

            # 状态标记
            status = "✅" if passed else "  "
            fail_reason = ""
            if not passed:
                parts = []
                if not degree_pass:
                    parts.append(f"学历不达标({degree}!={target_degree})")
                if not school_pass:
                    parts.append(f"学校不在白名单({school})")
                fail_reason = " | ".join(parts)

            print(f"  [{judged:>3}/{limit}] {status} {name:10s} | {school:16s} | {degree:4s}"
                  + (f" | {fail_reason}" if fail_reason else ""))

            if not passed:
                continue

            # 通过 → 打招呼
            if not idx:
                print(f"         ⚠ 无按钮索引, 跳过")
                continue

            if dry_run:
                continue

            # 页面间隔
            if greeted > 0:
                time.sleep(random.uniform(2, 4))

            print(f"         打招呼 (idx={idx})...", end=" ", flush=True)
            result = cua("click", json.dumps({"pid": pid, "window_id": wid, "element_index": idx}))
            err = result.get("error", "")
            if err:
                print(f"❌ {err}")
            else:
                print("✅")
                greeted += 1

            # 检测上限弹窗
            limit_reason = check_limit_popup(pid, wid)
            if limit_reason:
                print(f"  🛑 {limit_reason}")
                dismiss_limit_popup(pid, wid)
                stop_reason = f"沟通次数上限 ({limit_reason})"
                print(f"\n⏹ {stop_reason}")
                break

            time.sleep(random.uniform(1.5, 3))

        # ── 本轮循环后的判断 ──
        if stop_reason != "完成":
            break  # 上限弹窗触发了

        if judged >= limit:
            break  # 达到 limit

        if page_judged == 0 and len(fresh) == 0:
            # 空页 — 刷新试试
            print(f"  ⚠ 当前页无候选人, 刷新...")
            _refresh_page(pid, wid)
            page_round += 1
            continue

        # 卡片耗尽但未达 limit → 刷新页面获取新卡片
        _refresh_page(pid, wid)
        page_round += 1

    else:
        stop_reason = f"达到判断上限 ({limit})"

    return (greeted, judged, stop_reason)


# ══════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="cua-driver 驱动 BOSS直聘打招呼")
    parser.add_argument("--limit", type=int, default=20, help="最多判断候选人卡片数 (默认20)")
    parser.add_argument("--dry-run", action="store_true", help="仅预览, 不实际打招呼")
    parser.add_argument("--schools", type=str, help="自定义学校白名单, 逗号分隔")
    parser.add_argument("--min-degree", type=str, default="本科", help="学历精确匹配 (默认本科)")
    args = parser.parse_args()

    school_whitelist = (
        [s.strip() for s in args.schools.split(",")]
        if args.schools else ALL_ELITE_SCHOOLS
    )

    print("=" * 50)
    print(f"BOSS打招呼 | {len(school_whitelist)}所学校 | "
          f"学历={args.min_degree} | 上限{args.limit}人 | "
          f"{'预览' if args.dry_run else '执行'}")
    print("=" * 50)

    start_session()
    chrome = find_boss_window()
    pid, wid = chrome["pid"], chrome["window_id"]

    navigate_to_recommend(pid, wid)

    greeted, judged, reason = process_candidates(
        pid, wid,
        school_whitelist=school_whitelist,
        target_degree=args.min_degree,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    print(f"\n{'='*50}")
    print(f"结果: 打招呼 {greeted} 人 / 判断 {judged} 人 | {reason}")
    print("=" * 50)


if __name__ == "__main__":
    main()

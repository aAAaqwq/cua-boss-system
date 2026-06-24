#!/usr/bin/env python3
"""
cua-driver 驱动的 BOSS直聘候选人审查 + 批量回复
=================================================

逐个查看未读联系人，判断：
  1. 上一条消息是否候选人的（未回复）—— 还是我们已经回复过
  2. 学校是否在名校白名单中 — 不符合直接点"不合适"
  3. 符合条件 → 匹配话术模板 → 输入回复

用法:
  python scripts/cua_chat_loop.py                # 审查+回复(最多20人)
  python scripts/cua_chat_loop.py --dry-run      # 仅预览，输入不发送
  python scripts/cua_chat_loop.py --limit 10     # 最多10人
  python scripts/cua_chat_loop.py --schools "清华,北大,浙大"  # 自定义学校
  python scripts/cua_chat_loop.py --min-degree 硕士
"""
import argparse
import json
import random
import subprocess
import sys
import time
import re
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.filter_criteria import ALL_ELITE_SCHOOLS, DEFAULT_MIN_DEGREE, match_school, check_candidate, check_degree, find_school
from app.chat_reply import (
    load_jobs_config, generate_reply, detect_job, check_deepseek_configured,
    classify_intent, decide_rejection,
    MATCH_JOB, MATCH_CATEGORY, MATCH_FALLBACK, MATCH_NONE, SOURCE_TEMPLATE,
)

# 模板命中层级中文标签（日志/告警用）
_LAYER_LABEL = {
    MATCH_JOB: "①岗位专属",
    MATCH_CATEGORY: "②类别通用",
    MATCH_FALLBACK: "③全局兜底",
    MATCH_NONE: "✗未命中",
}
from app.db import DB_PATH
from scripts.boss_click_buheshi import click_buheshi

SESSION_ID = "boss-chat"
CHROME_BUNDLE_ID = "com.google.Chrome"
CHAT_URL = "https://www.zhipin.com/web/chat/index"

# ── 自己发出的消息特征（用于判断"已回复"） ──

# ── 限制检测关键词 ──
LIMIT_KEYWORDS = [
    "已达上限", "次数已用完", "今日已达", "已达每日",
    "沟通人数已达", "打招呼次数", "超出限制",
    "明天再来", "今日上限", "已达当天",
    "每天最多", "上限了", "用完了", "今日沟通",
    "权益不足", "开料次数", "剩余次数", "次数不足",
    "会员权益", "升级会员", "额度不足", "免费次数",
    "今日剩余",
]


def cua(*args: str) -> dict:
    cmd = ["cua-driver", "call"] + list(args)
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
# 页面加载
# ══════════════════════════════════════════════════

def wait_for_page(pid: int, window_id: int, timeout: float = 15.0, label: str = "") -> bool:
    prefix = f"[{label}] " if label else ""
    delays = [0.5, 0.5, 1.0, 1.0, 1.5, 1.5, 2.0, 2.0, 3.0, 3.0]
    elapsed = 0.0

    for delay in delays:
        time.sleep(delay)
        elapsed += delay

        r = cua("page", json.dumps({
            "pid": pid, "window_id": window_id,
            "action": "execute_javascript",
            "javascript": "document.readyState",
        }))
        ready_val = " ".join(str(x) for x in r) if isinstance(r, list) else str(r.get("result", r.get("text", "")))
        if ready_val.strip().strip('"') != "complete":
            continue

        snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": window_id}))
        elem_count = snap.get("element_count", 0)

        if elem_count > 300:
            print(f"  {prefix}✓ 就绪 ({elem_count}元素, {elapsed:.1f}s)")
            return True
        if elem_count > 100:
            print(f"  {prefix}加载中... ({elem_count}元素, {elapsed:.1f}s)")

    print(f"  {prefix}⚠ 超时 ({elapsed:.1f}s)")
    return False


# ══════════════════════════════════════════════════
# 限制弹窗
# ══════════════════════════════════════════════════

def dismiss_limit_popup(pid: int, window_id: int):
    print(f"  关闭弹窗...", end=" ", flush=True)
    cua("page", json.dumps({
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
        """,
    }))
    time.sleep(0.3)
    cua("press_key", json.dumps({"pid": pid, "window_id": window_id, "key": "escape"}))
    time.sleep(0.3)
    print("✓")


def check_limit_popup(pid: int, window_id: int) -> Optional[str]:
    for attempt in range(3):
        if attempt > 0:
            time.sleep(0.4)

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
            """,
        }))
        popup_text = " ".join(str(x) for x in r) if isinstance(r, list) else str(r.get("result", r.get("text", "")))
        for kw in LIMIT_KEYWORDS:
            if kw in popup_text:
                return f"弹窗: {kw}"

        snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": window_id}))
        tree = snap.get("tree_markdown", "")
        for line in tree.split("\n"):
            for kw in LIMIT_KEYWORDS:
                if kw in line and ("StaticText" in line or "AXButton" in line):
                    return f"页面: {kw}"

    return None


# ══════════════════════════════════════════════════
# 会话 & 窗口
# ══════════════════════════════════════════════════

def start_session():
    print("1. 启动 cua-driver 会话...")
    if "running" not in str(cua("status")).lower():
        subprocess.run(["cua-driver", "serve"], capture_output=True)
        time.sleep(1)
    cua("start_session", json.dumps({"session": SESSION_ID}))
    print("   ✓")


def find_boss_window() -> dict:
    print("2. 查找 BOSS直聘窗口...")
    apps = cua("list_apps")
    chrome_pid = None
    for app in apps.get("apps", []):
        if app.get("bundle_id") == CHROME_BUNDLE_ID and app.get("running"):
            chrome_pid = app.get("pid")
            break
    if not chrome_pid:
        print("  ❌ Chrome 未运行")
        sys.exit(1)

    lw = cua("list_windows", json.dumps({"pid": chrome_pid}))

    for w in lw.get("windows", []):
        title = w.get("title", "")
        if ("zhipin" in title or "BOSS直聘" == title.strip()) and w.get("is_on_screen"):
            print(f"  ✓ pid={chrome_pid} wid={w['window_id']} '{title[:60]}'")
            return {"pid": chrome_pid, "window_id": w["window_id"]}

    for w in lw.get("windows", []):
        title = w.get("title", "")
        if ("BOSS" in title or "zhipin" in title) and w.get("is_on_screen"):
            print(f"  ✓ pid={chrome_pid} wid={w['window_id']} '{title[:60]}'")
            return {"pid": chrome_pid, "window_id": w["window_id"]}

    # 离屏兜底
    for w in lw.get("windows", []):
        if "BOSS" in w.get("title", "") or "zhipin" in w.get("title", ""):
            print(f"  ⚠ 窗口隐藏, 尝试操作离屏窗口 wid={w['window_id']}")
            return {"pid": chrome_pid, "window_id": w["window_id"]}

    print("  ❌ 找不到 BOSS直聘窗口")
    sys.exit(1)


def navigate_to_chat(pid: int, window_id: int):
    print("3. 进入聊天页面...")
    cua("page", json.dumps({
        "pid": pid, "window_id": window_id,
        "action": "execute_javascript",
        "javascript": f'window.location.href = "{CHAT_URL}"',
    }))
    print("  等待页面加载...")
    time.sleep(5)
    wait_for_page(pid, window_id, label="聊天页")

    # 确认在联系人列表视图
    snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": window_id, "capture_mode": "ax"}))
    tree = snap.get("tree_markdown", "")
    if "未读" not in tree:
        print("  切换到联系人列表...")
        for line in tree.split("\n"):
            if "沟通" in line and "AXLink" in line:
                m = re.search(r'\[(\d+)\]', line)
                if m:
                    cua("click", json.dumps({"pid": pid, "window_id": window_id, "element_index": int(m.group(1))}))
                    time.sleep(3)
                    break
        print("  ✓")


# ══════════════════════════════════════════════════
# 扫描所有联系人
# ══════════════════════════════════════════════════

def scan_all_contacts(pid: int, window_id: int) -> list[dict]:
    """扫描左侧联系人列表（全部，非仅未读）

    BOSS 聊天页 AX 树结构 (联系人列表区域):
      "沟通" / "全部" / "新招呼" / ... ← 标签栏
      [73] "1"             ← 未读条数
      [74] "15:39"         ← 时间
      [75] "严彭杰"         ← 姓名
      [76] "AI 技术总监"    ← 职位
      [77] "消息内容..."    ← 消息预览

    联系人在"全部"标签下按时间排列，不限于未读。
    """
    print("4. 扫描联系人列表...")

    snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": window_id, "capture_mode": "ax"}))
    tree = snap.get("tree_markdown", "")
    if not tree:
        print("  ⚠ AX 树为空")
        return []

    lines = tree.split("\n")
    contacts = []
    current_name = None
    current_job = None
    current_msg = None
    current_time = None
    in_contact_list = False

    for line in lines:
        s = line.strip()
        val_m = re.search(r'AXStaticText\s*=\s*"(.+?)"', s)
        idx_m = re.search(r'\[(\d+)\]', s)
        if not val_m or not idx_m:
            continue
        val = val_m.group(1)
        idx = int(idx_m.group(1))

        # 进入联系人列表区域：以"未读"或联系人名作为起始标记
        # "沟通"标签可能在列表之前很远，先找"未读"+"批量"组合
        if not in_contact_list and val == "未读":
            # 下一个应该是"批量"，确认是联系人列表
            in_contact_list = True
            continue

        if not in_contact_list:
            continue

        # 跳过无关标记
        if val in ("批量", "买赠", "帮你问牛人", "不符牛人", "未读"):
            continue

        # 筛选标签 — 遇到右侧面板标记则退出列表区域
        if val in ("全部职位", "意向沟通", "招聘数据", "账号权益",
                    "面试", "道具", "工具箱", "牛人管理", "互动", "搜索",
                    "推荐牛人", "职位管理", "直聘企业版", "招聘规范",
                    "我的客服", "BOSS直聘", "沟通"):
            in_contact_list = False
            continue

        # 顶部标签栏 — 跳过但留在列表区域
        if val in ("全部", "新招呼", "沟通中", "已约面",
                    "已获取简历", "已交换电话", "已交换微信", "收藏", "更多"):
            continue
        if re.match(r'^\(\d+\)$', val):
            continue

        # 未读数字 — 保存上一个联系人
        if re.match(r'^\d{1,2}$', val):
            if current_name:
                contacts.append({
                    "name": current_name,
                    "job": current_job or "",
                    "message": current_msg or "",
                    "time": current_time or "",
                    "unread": int(val),
                    "ax_index": idx,
                })
                # 重置，准备收集下一个
                current_name = None
                current_job = None
                current_msg = None
                # current_time 不重置——下一条可能是新联系人的时间
            continue

        # 时间 — 可能是当前联系人的时间，也可能是新联系人的开始
        if re.match(r'^(?:\d{1,2}:\d{2}|昨天|前天|\d{1,2}-\d{1,2}|\d+月\d+日)$', val):
            # 如果已有名字，先保存
            if current_name:
                contacts.append({
                    "name": current_name,
                    "job": current_job or "",
                    "message": current_msg or "",
                    "time": current_time or "",
                    "unread": 0,
                    "ax_index": idx,
                })
                current_name = None
                current_job = None
                current_msg = None
            current_time = val
            continue

        # 状态标记
        if re.match(r'^\[.+\]$', val):
            continue

        # 姓名: 2-4个中文字 或 2-10个字母/数字/下划线（英文昵称如 Kim_）
        if not current_name and (re.match(r'^[一-鿿]{2,4}$', val) or re.match(r'^[a-zA-Z0-9_]{2,10}$', val)):
            current_name = val
            continue

        # 职位 (名字后的短文本)
        if current_name and not current_job and len(val) <= 20:
            current_job = val
            continue

        # 消息内容 (长文本)
        if current_name and not current_msg and len(val) > 5:
            current_msg = val[:80]
            continue

    # 保存最后一个（没有被未读数字触发）
    if current_name:
        contacts.append({
            "name": current_name,
            "job": current_job or "",
            "message": current_msg or "",
            "time": current_time or "",
            "unread": 0,
            "ax_index": -1,
        })

    # 去重（同一个名字+时间）
    seen = set()
    unique_contacts = []
    for c in contacts:
        key = (c["name"], c["time"])
        if key not in seen:
            seen.add(key)
            unique_contacts.append(c)

    print(f"  找到 {len(unique_contacts)} 个联系人 (去重后)")
    for c in unique_contacts[:5]:
        print(f"    {c['name']:8s} | {c['job']:14s} | {c['time']} | {c['message'][:30]}")
    if len(unique_contacts) > 5:
        print(f"    ... 还有 {len(unique_contacts) - 5} 个")
    return unique_contacts


# ══════════════════════════════════════════════════
# 点击联系人
# ══════════════════════════════════════════════════

def click_contact(pid: int, window_id: int, name: str) -> tuple[bool, Optional[str]]:
    """通过 JS 点击联系人名字，同时提取 data-id 作为 UID

    参考 cua_collect.py 的 click_sidebar()，在 DOM 中查找匹配名字的元素，
    向上遍历父元素获取 data-id 属性（格式: "<数字>-<索引>"，数字部分为用户加密 ID）。

    Returns:
        (clicked, uid) — clicked: 是否成功点击; uid: 用户唯一标识或 None
    """
    safe_name = name.replace("'", "\\'")
    r = cua("page", json.dumps({
        "pid": pid, "window_id": window_id,
        "action": "execute_javascript",
        "javascript": f"""
        (function(){{
            var all = document.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {{
                var el = all[i];
                if ((el.textContent || '').trim() === '{safe_name}' &&
                    el.children.length <= 1 && el.offsetWidth > 0) {{
                    var uid = null;
                    for (var p = el; p && p !== document.body; p = p.parentElement) {{
                        var did = p.getAttribute('data-id');
                        if (did) {{ uid = did.replace(/-\\d+$/, ''); break; }}
                    }}
                    for (var lvl = 0; lvl < 8; lvl++) {{
                        if (el.onclick || getComputedStyle(el).cursor === 'pointer') {{
                            el.click();
                            return JSON.stringify({{status:'clicked', uid: uid}});
                        }}
                        el = el.parentElement; if (!el) break;
                    }}
                    return JSON.stringify({{status:'not_clickable', uid: uid}});
                }}
            }}
            return JSON.stringify({{status:'not_found', uid: null}});
        }})()
        """,
    }))
    # r 是 cua-driver 直接返回的 JSON: {status, uid}
    if isinstance(r, dict) and "status" in r:
        return r.get("status") == "clicked", r.get("uid")
    # fallback: 纯文本返回值
    result_text = ""
    if isinstance(r, list):
        result_text = " ".join(str(x) for x in r)
    else:
        result_text = str(r.get("result", r.get("text", "")))
    return "clicked" in result_text, None


# ══════════════════════════════════════════════════
# JS DOM 聊天历史提取 — 主路径(A)
# ══════════════════════════════════════════════════

def _js_chat_history(pid: int, window_id: int) -> list[dict]:
    """主路径(A): 用 JS 读 DOM 对话气泡，按 BOSS 方向性 class 判定角色。

    BOSS 对话面板每条消息是一个带方向 class 的行：
      - item-myself → 我方(boss)，正文在 .text 子节点(开头可能含「送达/已读」投递前缀，需剔除)
      - item-friend → 候选人(candidate)，正文在 .text 子节点(另有空的 .figure 头像节点)
      - item-system → 系统消息(日期分割线 / 简历请求已发送 / 在线预览提示等)
    方向 class 是稳定的角色信号，与「送达/已读」瞬时投递标记无关，最新消息也能正确判定。

    系统消息仅保留对阶段推算有意义的事件(简历/微信请求已发送)，其余噪声(日期分割线、
    在线预览提示)丢弃。

    Returns: [{"role": "boss"|"candidate"|"system", "content": str}, ...] 最近 10 条
    """
    r = cua("page", json.dumps({
        "pid": pid, "window_id": window_id,
        "action": "execute_javascript",
        "javascript": """
        (function(){
            var rows=document.querySelectorAll(
                '[class*="item-myself"],[class*="item-friend"],[class*="item-system"]');
            if(!rows.length)
                return JSON.stringify({status:'no_rows',selector:'',conf:0,messages:[]});
            var msgs=[], conf=0;
            for(var i=0;i<rows.length;i++){
                var el=rows[i], cls=(el.className||'').toString(), role=null;
                if(/item-myself/.test(cls)) role='boss';
                else if(/item-friend/.test(cls)) role='candidate';
                else if(/item-system/.test(cls)) role='system';
                if(!role) continue;
                conf++;
                var t=el.querySelector('.text');
                var txt=((t?t.textContent:el.textContent)||'').replace(/\\s+/g,' ').trim();
                txt=txt.replace(/^(送达|已读|未读|已送达)\\s*/,'');
                if(!txt) continue;
                msgs.push({role:role, content:txt.slice(0,500)});
            }
            return JSON.stringify({status:'ok',selector:'item-direction',conf:conf,messages:msgs});
        })()
        """,
    }))

    # 解析 cua-driver 返回（多形态，与 click_contact 同款处理）
    payload = None
    if isinstance(r, dict) and "status" in r and "messages" in r:
        payload = r
    else:
        raw = ""
        if isinstance(r, list):
            raw = " ".join(str(x) for x in r)
        elif isinstance(r, dict):
            raw = str(r.get("result", r.get("text", "")))
        try:
            payload = json.loads(raw) if raw else None
        except (json.JSONDecodeError, TypeError, ValueError):
            payload = None

    if not isinstance(payload, dict):
        return []

    raw_msgs = payload.get("messages") or []
    conf = payload.get("conf") or 0

    # 无方向性行 → 放弃，回退 AX
    if conf < 1 or not raw_msgs:
        return []

    # 系统消息仅保留阶段信号(简历/微信「请求已发送」类短事件)，其余系统噪声丢弃。
    # 模糊匹配(短文本 + 含简历/微信 + 含发送)，兼容 BOSS 文案改版，同时排除长噪声
    # (如「您可以在线预览牛人简历…投递的简历会同时发送到您的邮箱」含简历+发送但很长)。
    def _is_stage_signal(c: str) -> bool:
        return len(c) <= 16 and ("简历" in c or "微信" in c) and "发送" in c

    seen = set()
    deduped = []
    for m in raw_msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role not in ("boss", "candidate", "system") or len(content) < 1:
            continue
        if role == "system" and not _is_stage_signal(content):
            continue  # 丢弃日期分割线 / 在线预览提示等系统噪声
        if role in ("boss", "candidate") and len(content) < 2:
            continue
        key = (role, " ".join(content.split()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append({"role": role, "content": content})

    return deduped[-10:]


# ══════════════════════════════════════════════════
# AX 兜底聊天历史提取 — JS 提取失败时使用
# ══════════════════════════════════════════════════

# AX 树右侧面板中的系统消息（非对话内容）
_AX_SYSTEM_MSGS = {
    "简历请求已发送", "请求交换微信已发送", "没有更多了",
}
_AX_SYSTEM_PREFIXES = (
    "牛人", "您可以在线预览", "设置邮箱", "后投递的简历会同时发送到您的邮箱",
    "复制微信号", "查看微信",
)
# AX 树右侧面板中的操作按钮（非对话内容）
_AX_ACTION_BUTTONS = {
    "求简历", "换电话", "查看微信", "约面试", "不合适",
    "在线简历", "附件简历",
}


def _ax_fallback_chat_history(tree: str) -> list[dict]:
    """JS 提取失败时，从 AX 树推断聊天历史

    AX 树右侧对话面板结构（点击联系人后）:
      - "沟通职位：" 标记对话区域起点
      - 日期分割线: "6月3日 沟通的职位-开发"
      - 系统消息: "牛人XX向您发起了沟通", "简历请求已发送"
      - [送达]/[已读] → 紧接的上一条是我们发的
      - 时间戳: "18:30"
      - 操作按钮: "求简历", "不合适" 等
      - "设置邮箱" / "全部职位" 等标记右侧面板底部，对话到此为止

    推断规则:
      1. [送达]/[已读] 之前的文本 → assistant (我们发的)
      2. 系统消息后的第一条 → user (候选人)
      3. 连续文本无 [送达] → 候选人发的
    """
    if not tree:
        return []

    # 第一步：从 AX 树收集所有 (index, text)
    all_nodes = []
    for line in tree.split("\n"):
        idx_m = re.search(r"\[(\d+)\]", line)
        val_m = re.search(r'AXStaticText\s*=\s*"(.+?)"', line)
        if not idx_m or not val_m:
            continue
        idx = int(idx_m.group(1))
        val = val_m.group(1)
        if len(val) < 2:
            continue
        all_nodes.append((idx, val))

    # 第二步：定位右侧面板对话区域（取最后一个/最高 index 的面板）
    # AX 树中同一对话面板出现两次（页面虚拟化），当前显示的总是最后出现的
    # 策略：先收集所有日期分割线位置，取最后一个作为起点，再往后找操作按钮终点
    date_seps = []
    for i, (idx, val) in enumerate(all_nodes):
        if re.match(r"\d+月\d+日\s+沟通的职位", val):
            date_seps.append(i)

    if not date_seps:
        return []

    start_idx = date_seps[-1]  # 取最后一个（当前面板）
    end_idx = None
    for i in range(start_idx + 1, len(all_nodes)):
        if all_nodes[i][1] in ("求简历", "换电话", "约面试", "不合适", "发送"):
            end_idx = i
            break

    if start_idx is None:
        return []

    # 如果没找到明确终点，取到操作按钮前的范围
    if end_idx is None:
        end_idx = len(all_nodes)

    panel = all_nodes[start_idx:end_idx]

    # 第三步：遍历，推断 role
    # AX 树中 [送达]/[已读] 出现在消息之前：[已读] → 方便发简历吗（= 我们发的）
    messages = []
    next_is_self = False  # 下一句是否是我们发的

    for i, (idx, val) in enumerate(panel):

        # 送达/已读/未读/已送达 → 标记下一条为我们发的
        # 左侧联系人列表带方括号；右侧对话面板无方括号
        # "未读"/"已送达" 覆盖刚发出尚未被对方读取的最新消息（否则会漏判成候选人发的）
        if val in ("[送达]", "[已读]", "[未读]", "送达", "已读", "未读", "已送达"):
            next_is_self = True
            continue

        # 跳过非对话内容
        if re.match(r"^\d{1,2}:\d{2}$", val):          # 时间戳
            continue
        if re.match(r"\d+月\d+日", val):                 # 日期分割线
            continue
        if val in _AX_SYSTEM_MSGS:                        # 系统消息 — 保留用于阶段推算
            messages.append(("system", val))
            continue
        if any(val.startswith(p) for p in _AX_SYSTEM_PREFIXES):  # 系统消息前缀
            continue
        # BOSS UI 按钮文本（非候选人消息）
        if val in ("点击预览附件简历", "查看附件简历", "在线简历", "附件简历",
                    "求简历", "换电话", "查看微信", "约面试", "不合适", "复制微信号"):
            continue
        if "微信号" in val and ("*****" in val or val.endswith("微信号：")):
            continue
        if re.match(r"^\d{11}$", val):                    # 电话号码
            continue
        if re.match(r"^\d+岁$", val):                     # 年龄
            continue
        if re.match(r"^\d{4}\.\d{2}-", val):             # 时间段
            continue
        if len(val) <= 12 and "·" in val:                 # 信息标签
            continue
        # 跳过"加了"等系统确认
        if val == "加了":
            continue

        # 是对话文本（>= 4字）
        if len(val) >= 4:
            role = "boss" if next_is_self else "candidate"
            messages.append((role, val))
            next_is_self = False

    # 去重
    seen = set()
    deduped = []
    for role, content in messages:
        key = (role, content)
        if key not in seen:
            seen.add(key)
            deduped.append({"role": role, "content": content})

    return deduped[-10:]


# ══════════════════════════════════════════════════
# 读取对话 — 学校 / 学历 / 最新消息 / 是否未回复
# ══════════════════════════════════════════════════

def read_conversation(pid: int, window_id: int) -> dict:
    """读取右侧对话面板：学校、学历、聊天历史

    返回:
      {
        "name": str,
        "school": str | None,
        "degree": str | None,
        "info_line": str,
        "chat_history": [{"role": "candidate"|"boss", "content": str}, ...],
        "last_sender": "boss" | "candidate" | "",
        "latest_candidate_msg": str,
      }
    """
    result = {
        "name": "",
        "school": None,
        "degree": None,
        "info_line": "",
        "chat_history": [],
        "last_sender": "",
        "latest_candidate_msg": "",
        "job_position": "",  # 右侧面板权威沟通岗位（= BOSS 招聘岗位名，与 jobs 配置 key 对齐）
    }

    # ── 1. AX 树读取候选人信息 ──
    snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": window_id, "capture_mode": "ax"}))
    tree = snap.get("tree_markdown", "")

    if tree:
        for line in tree.split("\n"):
            s = line.strip()
            # 沟通岗位 (右侧面板日期分割线: "6月3日 沟通的职位-开发")
            # 这是 BOSS 招聘岗位名（与 jobs 配置 key 对齐），权威来源，优先于左侧列表
            if not result["job_position"]:
                jm = re.search(r'沟通的职位[-－—:：]\s*(.+?)\s*"', s)
                if jm:
                    pos = jm.group(1).strip()
                    if pos and len(pos) <= 30:
                        result["job_position"] = pos
            # 学校 (独立出现)
            m = re.search(r'AXStaticText\s*=\s*"([一-龥]{2,8}(?:大学|学院|学校))"', s)
            if m and not result["school"]:
                result["school"] = m.group(1)
            # 学历 (独立出现)
            m = re.search(r'AXStaticText\s*=\s*"(博士|硕士|本科|大专)"', s)
            if m and not result["degree"]:
                result["degree"] = m.group(1)
            # 候选人信息行: "郑州大学 · 经济统计学 · 本科" 或 "某 · 技术总监"
            m = re.search(r'AXStaticText\s*=\s*"(.+)"', s)
            if m and "·" in m.group(1) and len(m.group(1)) < 80:
                info = m.group(1)
                # 解析 · 分隔的字段
                parts = [p.strip() for p in info.split("·")]
                # 尝试从各部分中提取学校和学历
                for p in parts:
                    # 学校
                    school_m = re.match(r'^([一-龥]{2,8}(?:大学|学院|学校))$', p)
                    if school_m and not result["school"]:
                        result["school"] = school_m.group(1)
                    # 学历
                    if p in ("博士", "硕士", "本科", "大专") and not result["degree"]:
                        result["degree"] = p
                if not result["info_line"]:
                    result["info_line"] = info

    # ── 2. 读取聊天历史 ──
    # 主路径(A): JS 读 DOM 气泡按 class 判定角色（不依赖瞬时投递标记，最新消息也能正确判断）
    # 兜底: JS 失败 → AX 树 送达/已读 标记推断
    history = _js_chat_history(pid, window_id)
    source = "JS-DOM"
    if not history:
        history = _ax_fallback_chat_history(tree)
        source = "AX兜底"

    if history:
        result["chat_history"] = history
        # last_sender 取最后一条「非系统」消息的角色：末尾的系统消息(简历请求已发送等)
        # 不代表对话发言方，否则 boss 发完后紧跟系统消息会让"已回复"判断失效
        _non_system = [m for m in history if m.get("role") in ("boss", "candidate")]
        # 无非系统消息时留空字符串（"system" 不是合法的发言方，避免下游误判陷阱）
        result["last_sender"] = _non_system[-1].get("role", "") if _non_system else ""
        print(f"    聊天历史: {len(history)}条 (来源={source})")

        # 找到最后一条候选人消息（任何非空内容均算）。
        # 注意: 不能用 >=4 的长度门槛——"OK/好/嗯/可以"这类极短确认是候选人真实最新回复，
        # 旧门槛会跳过它们、误把更早的长消息当成"最新待回复"，导致回复答非所问。
        # history 已在 _js_chat_history 层过滤系统噪声并保证 boss/candidate >=2 字。
        for msg in reversed(history):
            if msg.get("role") == "candidate" and len(msg.get("content", "").strip()) >= 1:
                result["latest_candidate_msg"] = msg["content"]
                break

        if not result["last_sender"]:
            print("    ⚠ 聊天历史提取成功，但无法判断最后发送者")
    elif tree:
        # JS + AX 均未提取到聊天历史 → BOSS 面板结构可能变化
        print("    ⚠ 聊天历史提取失败（JS+AX 均未命中，右侧面板可能结构变化），将无上下文生成回复")

    return result


# ══════════════════════════════════════════════════
# 点击"不合适"
# ══════════════════════════════════════════════════

# click_unsuitable 已提取到 scripts/boss_click_buheshi.py，通过 click_buheshi() 调用


# ══════════════════════════════════════════════════
# 输入回复
# ══════════════════════════════════════════════════

def find_input_index(pid: int, window_id: int) -> Optional[int]:
    """在 AX 树中查找聊天输入框的 element_index"""
    snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": window_id}))
    tree = snap.get("tree_markdown", "")
    if tree:
        for line in tree.split("\n"):
            if ("AXTextArea" in line or "textArea" in line) and "[" in line:
                m = re.search(r'\[(\d+)\]', line)
                if m:
                    return int(m.group(1))
    return None


# BOSS 聊天输入框是 contenteditable 的 React 编辑器(div.boss-chat-editor-input)，
# 不是 textarea。cua type_text / AX 写值对它静默无效(返回成功但框内为空)，
# 必须用 execCommand('insertText') 触发编辑器原生输入处理。已实测验证(见 type_reply)。
_EDITOR_SELECTOR = ('div.boss-chat-editor-input, [contenteditable="true"], '
                    'textarea, [class*="chat-input"], [class*="input-box"]')


def _editor_text(pid: int, window_id: int) -> str:
    """回读输入框当前文本(textarea.value 或 contenteditable.textContent)，用于校验输入是否落框。"""
    js = ("(function(){var el=document.querySelector('" + _EDITOR_SELECTOR + "');"
          "if(!el)return JSON.stringify({found:false,value:''});"
          "var v=(el.value!==undefined&&el.value!=='')?el.value:(el.textContent||'');"
          "return JSON.stringify({found:true,value:v});})()")
    r = cua("page", json.dumps({
        "pid": pid, "window_id": window_id,
        "action": "execute_javascript", "javascript": js,
    }))
    payload = r if isinstance(r, dict) and "value" in r else None
    if payload is None:
        raw = " ".join(str(x) for x in r) if isinstance(r, list) else \
              str(r.get("result", r.get("text", "")) if isinstance(r, dict) else r)
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            payload = None
    return payload.get("value", "") if isinstance(payload, dict) else ""


def _type_into_editor(pid: int, window_id: int, text: str) -> bool:
    """聚焦输入框 → 全选替换 → execCommand 插入文本，并回读校验是否真的落框。

    Returns: True 仅当文本确实出现在输入框内（不再凭 cua 返回值臆断"已输入"）。
    """
    safe = json.dumps(text)  # 转义为 JS 字符串字面量
    js = ("(function(){var el=document.querySelector('" + _EDITOR_SELECTOR + "');"
          "if(!el)return JSON.stringify({ok:false,landed:false,reason:'no_editor'});"
          "el.focus();"
          # 全选已有内容(contenteditable)或清空(textarea)，使 insertText 形成替换
          "try{if(el.value!==undefined){el.value='';}else{var s=window.getSelection();"
          "s.selectAllChildren(el);}}catch(e){}"
          "var ok=document.execCommand('insertText',false," + safe + ");"
          "el.dispatchEvent(new InputEvent('input',{bubbles:true,data:" + safe +
          ",inputType:'insertText'}));"
          "var v=(el.value!==undefined&&el.value!=='')?el.value:(el.textContent||'');"
          "return JSON.stringify({ok:ok,landed:v.indexOf(" + safe + ")>=0});})()")
    r = cua("page", json.dumps({
        "pid": pid, "window_id": window_id,
        "action": "execute_javascript", "javascript": js,
    }))
    payload = r if isinstance(r, dict) and "landed" in r else None
    if payload is None:
        raw = " ".join(str(x) for x in r) if isinstance(r, list) else \
              str(r.get("result", r.get("text", "")) if isinstance(r, dict) else r)
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            payload = None
    if isinstance(payload, dict):
        return bool(payload.get("landed"))
    # 返回值解析失败 → 直接回读确认(用前若干字符匹配，规避截断)
    return bool(text) and text[:12] in _editor_text(pid, window_id)


def _clear_input(pid: int, window_id: int) -> None:
    """清空 BOSS 聊天输入框(contenteditable)。

    直接 JS 清空并触发 input 事件，使 React 内部状态同步。比 Cmd+A+Delete
    更可靠——后者依赖键盘焦点落在正确元素，对 contenteditable 常常打偏。
    """
    js = ("(function(){var el=document.querySelector('" + _EDITOR_SELECTOR + "');"
          "if(!el)return'nf';el.focus();"
          "if(el.value!==undefined){el.value='';}else{el.textContent='';}"
          "el.dispatchEvent(new InputEvent('input',{bubbles:true,"
          "inputType:'deleteContentBackward'}));return'ok';})()")
    cua("page", json.dumps({
        "pid": pid, "window_id": window_id,
        "action": "execute_javascript", "javascript": js,
    }))
    time.sleep(0.15)


def type_reply(pid: int, window_id: int, text: str, dry_run: bool = True) -> bool:
    """输入回复到 BOSS 聊天框(contenteditable)；dry_run=True 只输入不发送。

    用 execCommand('insertText') 写入并回读校验是否真的落框——cua type_text
    对 React contenteditable 编辑器无效会静默失败，旧实现因此报"已输入"实则空框。
    校验失败返回 False，绝不在文本未落框时假报成功(更不会空框回车误发)。
    """
    landed = _type_into_editor(pid, window_id, text)
    if not landed:
        print("    ❌ 输入失败: 编辑器未接收文本(contenteditable insertText 失败)")
        return False

    if dry_run:
        return True

    # 发送: 回车(编辑器此时已聚焦)
    time.sleep(0.3)
    input_idx = find_input_index(pid, window_id)
    if input_idx:
        cua("press_key", json.dumps({
            "pid": pid, "window_id": window_id,
            "element_index": input_idx, "key": "return",
        }))
    else:
        cua("press_key", json.dumps({
            "pid": pid, "window_id": window_id,
            "key": "return",
        }))
    time.sleep(0.5)
    return True


# ══════════════════════════════════════════════════
# 单个候选人审查
# ══════════════════════════════════════════════════

def _note_reject(db, uid, name, reason: str) -> None:
    """把拒绝依据写入 candidates.notes（best-effort，失败不影响主流程）。"""
    if not db or not reason:
        return
    try:
        if uid:
            db.execute("UPDATE candidates SET notes = ? WHERE uid = ?", (reason, uid))
        else:
            db.execute("UPDATE candidates SET notes = ? WHERE name = ?", (reason, name))
        db.commit()
    except Exception:
        pass


def review_one_candidate(
    pid: int,
    window_id: int,
    contact: dict,
    school_whitelist: list[str],
    jobs_config: dict,
    min_degree: str,
    dry_run: bool,
    db=None,
) -> dict:
    """审查一个候选人：读对话 → 学校筛选 → 未回复判断 → 回复/不合适

    决策流程:
      1. 点击联系人，打开对话
      2. 读取学校、学历、最新消息
      3. 判断上一句是否候选人发的（未回复）→ 已经是我们的消息 → 跳过
      4. 学校不在白名单 → 点"不合适"
      5. 学校在白名单 → 匹配模板 → 输入回复
    """
    name = contact.get("name", "?")
    print(f"\n  ── {name} ──")

    # a. 点击 + 提取 UID
    contact_uid = None
    clicked, contact_uid = click_contact(pid, window_id, name)
    if not clicked:
        print(f"    ❌ 点击失败")
        return {"status": "click_error", "name": name, "uid": None}
    if contact_uid:
        print(f"    uid: {contact_uid}")
    time.sleep(1.5)

    # 清空输入框（模拟真实键盘操作，兼容 React/Vue 框架）
    _clear_input(pid, window_id)

    # b. 读取
    convo = read_conversation(pid, window_id)
    school = convo.get("school")
    degree = convo.get("degree")
    info_line = convo.get("info_line", "")
    latest_msg = convo.get("latest_candidate_msg", "")
    last_sender = convo.get("last_sender", "")
    # 右侧面板权威沟通岗位（= BOSS 招聘岗位名）；提取不到则回退左侧列表 contact["job"]
    job_position = convo.get("job_position", "") or contact.get("job", "")

    # 输出审查信息
    school_flag = "✅" if school and match_school(school, school_whitelist) else "❌"
    reply_flag = "🟢待回复" if last_sender == "candidate" else "🔵已回复"
    pos_src = "右侧面板" if convo.get("job_position") else ("左侧列表" if contact.get("job") else "无")
    print(f"    信息: {info_line or '?'}")
    print(f"    沟通岗位: {job_position or '?'} (来源={pos_src})")
    print(f"    学校: {school or '?'} {school_flag} | 学历: {degree or '?'} | {reply_flag}")
    if latest_msg:
        print(f"    最新: {latest_msg[:60]}")

    # c. 读取 DB 上下文 & 推算对话阶段
    stage, stage_context = "early_stage", ""
    ctx = {"has_resume": False, "has_wechat": False}
    if db:
        ctx = _load_candidate_context(db, contact_uid, name)
        stage, stage_context = _compute_stage(ctx, convo.get("chat_history", []))
        if stage != "early_stage":
            flags = []
            if ctx["has_resume"]:
                flags.append("📄简历")
            if ctx["has_wechat"]:
                flags.append("💬微信")
            print(f"    阶段: {stage} {' '.join(flags)}")

    # d. 最后一条是我们发的 → 已回复，跳过
    if last_sender == "boss":
        print(f"    → 已回复过（上一句是我们发的），跳过")
        return {"status": "already_replied", "name": name, "uid": contact_uid, "job_position": job_position, "chat_history": convo.get("chat_history", [])}

    # d2. 安全网(C): 不依赖 AX 投递标记，用 DB 历史判断"是否已回复过最新消息"。
    #     DB 最后一条非系统消息是我方(boss)，且候选人最新消息已在 DB 历史中(无新消息) → 跳过。
    #     专治"刚发出的消息未读/无标记被误判成 candidate，导致重复回复"。
    if db and latest_msg:
        db_msgs = [m for m in ctx.get("db_chat_history", []) if m.get("role") != "system"]
        if db_msgs and db_msgs[-1].get("role") == "boss":
            latest_norm = " ".join(latest_msg.split())
            db_contents = {" ".join((m.get("content") or "").split()) for m in db_msgs}
            if latest_norm in db_contents:
                print(f"    → 已回复过（DB 显示最新消息已回复，无新消息），跳过 [安全网]")
                return {"status": "already_replied", "name": name, "uid": contact_uid, "job_position": job_position, "chat_history": convo.get("chat_history", [])}

    # d. 学校筛选
    # 学校识别：先用 find_school 在信息行做白名单整词反查（解决「（北京）/分校/科学技术院」
    # 被窄正则截断而漏配的误杀），命中取规范白名单名；否则退回 read 到的 school。
    school_resolved = find_school(
        f"{convo.get('info_line', '')} {school or ''}", school_whitelist) or school

    # 学校没读到（面板未识别）→ 绝不误点"不合适"，跳过等下一轮
    if not school_resolved:
        print(f"    → ⚠ 学校未识别(面板未读到)，跳过(不点'不合适')")
        return {"status": "skipped_no_school", "name": name, "uid": contact_uid, "job_position": job_position, "chat_history": convo.get("chat_history", [])}

    if not match_school(school_resolved, school_whitelist):
        print(f"    → 学校不符 ({school_resolved} 不在白名单)，标记'不合适'")
        if not dry_run:
            click_buheshi(pid, window_id)
        else:
            print(f"    [预览] 将点击'不合适'")
        return {"status": "unsuitable", "name": name, "uid": contact_uid, "school": school_resolved, "job_position": job_position, "chat_history": convo.get("chat_history", [])}

    # e. 学历筛选
    if degree and not check_degree(degree, min_degree):
        print(f"    → 学历不符 ({degree} < {min_degree})，标记'不合适'")
        if not dry_run:
            click_buheshi(pid, window_id)
        else:
            print(f"    [预览] 将点击'不合适'")
        return {"status": "rejected_degree", "name": name, "uid": contact_uid, "degree": degree, "job_position": job_position, "chat_history": convo.get("chat_history", [])}

    # f. 无候选人消息
    if not latest_msg:
        print(f"    → 无候选人消息，跳过")
        return {"status": "no_message", "name": name, "uid": contact_uid}

    # g. 生成回复 (岗位感知)
    jobs = jobs_config.get("jobs", [])
    fallback_tpls = jobs_config.get("fallback_templates", [])

    # 检测候选人对应的岗位
    job_id = detect_job(latest_msg, contact.get("job", ""), jobs)
    job_templates = []
    category_templates = []
    job_context = ""
    matched_job = None
    if job_id:
        for job in jobs:
            if job.get("title") == job_id:  # 岗位名即唯一键
                job_templates = job.get("templates", [])
                category_templates = job.get("category_templates", [])
                # 地点/薪资是短而关键的事实(约面试要用)，放在最前；requirements 可能很长，
                # 放最后由 JOB_CONTEXT_MAX_CHARS 截断时只削要求、不丢地点。缺失字段不拼。
                _parts = [job["title"]]
                if job.get("location"):
                    _parts.append(f"地点:{job['location']}")
                if job.get("salary"):
                    _parts.append(f"薪资:{job['salary']}")
                if job.get("requirements"):
                    _parts.append(f"要求:{job['requirements']}")
                job_context = " | ".join(_parts)
                matched_job = job
                print(f"    岗位: {job['title']}")
                break

    chat_history = convo.get("chat_history", [])

    # 合并 DB 历史（更完整）+ AX 树历史（更实时），去重保序
    db_history = ctx.get("db_chat_history", [])
    if db_history:
        merged = db_history + chat_history
        seen_keys = set()
        deduped = []
        for msg in merged:
            # 归一化空白后比较：AX 抽取与 DB 存储的同一句常因空白/换行差异导致去重失效
            norm_content = " ".join((msg.get("content") or "").split())
            key = (msg.get("role", ""), norm_content)
            if key not in seen_keys:
                seen_keys.add(key)
                deduped.append(msg)
        chat_history = deduped[-20:]  # 保留最近 20 条

    if not chat_history:
        print("    ⚠ 无聊天历史（AX+DB均为空），DeepSeek 将仅凭最新消息生成回复")

    # 简历正文注入（collect 写入 candidates.resume_content），用于针对性提问
    resume_context = ctx.get("resume_content", "")
    if resume_context:
        print(f"    📄 简历上下文: {len(resume_context)} 字注入")

    # g2. 拒绝意图识别 (Issue #1)：明显拒绝→标'不合适'；委婉拒绝→停止追问(不标记)。
    #     放在生成回复之前——判为拒绝就跳过回复。DeepSeek 不可用/不确定 → 照常回复(绝不误标)。
    policy = jobs_config.get("rejection_policy", {})
    if policy.get("enabled", True) and check_deepseek_configured():
        intent = classify_intent(
            latest_msg, history=chat_history,
            job_context=job_context, stage_context=stage_context,
        )
        decision = decide_rejection(intent, policy)
        print(f"    意图: {intent['intent']}({intent['confidence']:.2f}) → {decision['action']}"
              + (f" — {intent['reason']}" if intent.get('reason') else ""))
        if decision["action"] == "mark":
            print(f"    → 检测到拒绝，标记'不合适' [{decision['reason']}]")
            if not dry_run:
                click_buheshi(pid, window_id)
                _note_reject(db, contact_uid, name, decision["reason"])
            else:
                print(f"    [预览] 将点击'不合适'(拒绝)")
            return {"status": "unsuitable", "name": name, "uid": contact_uid,
                    "reject_reason": decision["reason"], "job_position": job_position,
                    "chat_history": convo.get("chat_history", [])}
        if decision["action"] == "stop":
            print(f"    → 委婉拒绝，停止追问(不标记、不回复) [{decision['reason']}]")
            return {"status": "soft_reject", "name": name, "uid": contact_uid,
                    "reject_reason": decision["reason"], "job_position": job_position,
                    "chat_history": convo.get("chat_history", [])}

    gen = generate_reply(
        latest_msg,
        templates=[],  # 旧格式兼容（jobs模式下为空）
        candidate_name=name,
        history=chat_history,
        job_templates=job_templates,
        category_templates=category_templates,
        fallback_templates=fallback_tpls,
        job_context=job_context,
        job=matched_job,
        stage_context=stage_context,
        resume_context=resume_context,
    )
    reply = gen["reply"]
    match_layer = gen["match_layer"]
    reply_source = gen["source"]

    # 模板命中层级可观测性：未命中①岗位专属即告警，提示补充该岗位话术
    layer_label = _LAYER_LABEL.get(match_layer, match_layer)
    if match_layer == MATCH_JOB:
        print(f"    模板层: {layer_label} ✓")
    else:
        hint = f"（岗位={job_id or '未识别'}）" if match_layer != MATCH_NONE else "（无任何关键词命中）"
        print(f"    ⚠ 模板层: {layer_label}{hint} — 未命中岗位专属模板，建议补充该岗位话术")
    if reply_source == SOURCE_TEMPLATE:
        print(f"    ⚠ DeepSeek 未生效，已降级为模板原文")

    # 兜底: 回复还在问已有的东西 → 用阶段兜底文本替换
    if _reply_redundant(reply, ctx):
        fallback = _STAGE_FALLBACK.get(stage)
        if fallback:
            print(f"    ⚠ 回复冗余(阶段{stage})，替换为阶段兜底")
            reply = fallback

    print(f"    → 回复: {reply[:60]}")

    # h. 输入（dry-run 不发送）
    ok = type_reply(pid, window_id, reply, dry_run=dry_run)
    if not ok:
        return {"status": "send_error", "name": name, "uid": contact_uid}

    if dry_run:
        print(f"    [预览] 已输入，未发送")
    else:
        print(f"    ✓ 已发送")

    return {
        "status": "replied",
        "name": name,
        "uid": contact_uid,
        "school": school,
        "degree": degree,
        "job_position": job_position,
        "reply": reply,
        "match_layer": match_layer,
        "reply_source": reply_source,
        "chat_history": chat_history,
    }


# ══════════════════════════════════════════════════
# 候选人上下文 & 对话阶段
# ══════════════════════════════════════════════════

def _load_candidate_context(db, uid: Optional[str], name: str) -> dict:
    """从 candidates.db 读取已知上下文: 简历/微信/状态/历史聊天

    Returns:
        {
            "has_resume": bool,
            "has_wechat": bool,
            "wechat": str,
            "status": str,
            "db_chat_history": list[dict],
            "resume_content": str,
        }
    """
    defaults = {
        "has_resume": False,
        "has_wechat": False,
        "wechat": "",
        "status": "new",
        "db_chat_history": [],
        "resume_content": "",
    }
    if uid:
        row = db.execute(
            "SELECT has_resume, has_wechat, wechat, status, chat_history, resume_content FROM candidates WHERE uid = ?",
            (uid,),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT has_resume, has_wechat, wechat, status, chat_history, resume_content FROM candidates WHERE name = ?",
            (name,),
        ).fetchone()
    if not row:
        return defaults

    db_history = []
    if row[4]:
        try:
            db_history = json.loads(row[4])
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "has_resume": bool(row[0]),
        "has_wechat": bool(row[1]),
        "wechat": row[2] or "",
        "status": row[3] or "new",
        "db_chat_history": db_history,
        "resume_content": row[5] or "",
    }


def _compute_stage(ctx: dict, chat_history: list[dict]) -> tuple[str, str]:
    """根据 DB 上下文 + 聊天历史推算对话阶段

    Returns:
        (stage_name, stage_context_str)
        stage_context_str 用于注入 DeepSeek system prompt
    """
    has_resume = ctx.get("has_resume", False)
    has_wechat = ctx.get("has_wechat", False)
    wechat_id = ctx.get("wechat", "")

    # 从聊天历史检测"已请求但未确认"的状态
    resume_requested = False
    wechat_requested = False
    for msg in chat_history:
        content = msg.get("content", "")
        role = msg.get("role", "")
        if role == "system":
            if "简历请求已发送" in content:
                resume_requested = True
            if "请求交换微信已发送" in content:
                wechat_requested = True

    # 阶段推算
    if has_resume and has_wechat:
        stage = "ready_for_interview"
        known = [f"已收到简历", f"已交换微信(微信号: {wechat_id})" if wechat_id else "已交换微信"]
        hint = "简历和微信都已收到，绝对不要再问简历或微信。推动约面试时间或聊岗位具体细节。"
    elif has_resume and not has_wechat:
        stage = "has_resume_no_wechat"
        known = ["已收到简历"]
        if resume_requested:
            known.append("简历请求已发送")
        hint = "已收到简历，不要再问简历。可以聊岗位细节、约面试，或确认微信交换。"
    elif has_wechat and not has_resume:
        stage = "has_wechat_no_resume"
        known = [f"已交换微信(微信号: {wechat_id})" if wechat_id else "已交换微信"]
        hint = "已交换微信，不要再问微信。可以聊岗位具体细节或直接约面试。"
    else:
        # 无 DB 数据，检查历史中的请求信号
        extras = []
        if resume_requested:
            extras.append("简历请求已发送")
        if wechat_requested:
            extras.append("请求交换微信已发送")
        if extras:
            stage = "awaiting_response"
            known = extras
            hint = f"已发出以下请求: {'、'.join(extras)}。不要重复请求，等对方回复后推动下一步。"
        else:
            stage = "early_stage"
            return stage, ""  # 早期阶段无约束

    context_str = f"阶段: {stage}\n已知: {'、'.join(known)}\n注意: {hint}"
    return stage, context_str


_STAGE_FALLBACK = {
    "ready_for_interview": "简历和微信都收到了，方便的话我们约个时间聊聊具体岗位细节？",
    "has_resume_no_wechat": "简历收到了，具体岗位细节可以进一步沟通，你觉得怎么样？",
    "has_wechat_no_resume": "好的，具体岗位细节可以进一步聊，方便说说你主要的项目经历吗？",
    "awaiting_response": "好的，等你方便回复的时候我们再继续聊～",
}

_RESUME_PATTERNS = ["方便发简历", "发一份简历", "简历发", "你的简历", "发简历过来", "简历过来"]
_WECHAT_PATTERNS = ["加微信", "交换微信", "方便加个微信", "微信号多少", "你的微信"]


def _reply_redundant(reply: str, ctx: dict) -> bool:
    """检查回复是否在问已有的东西（简历/微信）"""
    if ctx.get("has_resume") and any(p in reply for p in _RESUME_PATTERNS):
        return True
    if ctx.get("has_wechat") and any(p in reply for p in _WECHAT_PATTERNS):
        return True
    return False


# ══════════════════════════════════════════════════
# 聊天记录存库
# ══════════════════════════════════════════════════

def _save_chat_history(
    db,
    contact: dict,
    result: dict,
    chat_history: list[dict],
) -> None:
    """将聊天记录 upsert 到 candidates.db

    优先按 uid 匹配已有记录（跨脚本唯一），uid 为空时 fallback 到 name。
    chat_history 存为 JSON 数组: [{"role":"candidate"|"boss"|"system","content":"..."}, ...]
    role 可为 system（仅保留「简历/微信请求已发送」类阶段信号，供 _compute_stage 跨轮
    读取）；下游消费方(scoring/导出等)若不需要应自行按 role 过滤。
    """
    name = result.get("name") or contact.get("name", "")
    uid = result.get("uid") or contact.get("uid")
    school = result.get("school") or ""  # 不再误用 contact["job"] 作学校兜底
    degree = result.get("degree") or ""
    # 沟通岗位：优先 result（右侧面板权威值），回退左侧列表 contact["job"]
    job_pos = result.get("job_position") or contact.get("job", "")
    history_json = json.dumps(chat_history, ensure_ascii=False)
    status = result.get("status", "unknown")

    # 优先按 uid 查找，fallback 到 name
    if uid:
        cursor = db.execute(
            "SELECT id, chat_history FROM candidates WHERE uid = ?",
            (uid,),
        )
    else:
        cursor = db.execute(
            "SELECT id, chat_history FROM candidates WHERE name = ?",
            (name,),
        )
    row = cursor.fetchone()

    if row:
        # 合并：新历史追加到旧历史后（去重）
        old_history = []
        try:
            old_history = json.loads(row[1]) if row[1] else []
        except (json.JSONDecodeError, TypeError):
            pass
        # 用最后一条消息内容去重
        merged = old_history + chat_history
        # 去重：按 (role, content) 去重保序
        seen_keys = set()
        deduped = []
        for msg in merged:
            key = (msg.get("role", ""), msg.get("content", ""))
            if key not in seen_keys:
                seen_keys.add(key)
                deduped.append(msg)
        merged_json = json.dumps(deduped[-20:], ensure_ascii=False)  # 保留最近20条

        # job_position 仅在拿到非空值时刷新，避免用空值覆盖已有的好数据
        if job_pos:
            db.execute(
                "UPDATE candidates SET chat_history = ?, status = ?, job_position = ? WHERE id = ?",
                (merged_json, status, job_pos, row[0]),
            )
        else:
            db.execute(
                "UPDATE candidates SET chat_history = ?, status = ? WHERE id = ?",
                (merged_json, status, row[0]),
            )
    else:
        # 新建记录（含 uid，便于后续 collect 脚本精准匹配）
        db.execute(
            """INSERT INTO candidates (uid, name, school, degree, job_position, chat_history, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (uid, name, school, degree, job_pos, history_json, status),
        )
    db.commit()


# ══════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="cua-driver 驱动 BOSS直聘候选人审查 + 批量回复",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s                          审查+回复(最多20人)
  %(prog)s --dry-run                仅预览，输入不发送
  %(prog)s --limit 10               最多10人
  %(prog)s --schools "清华,北大,浙大"  自定义学校
  %(prog)s --min-degree 硕士         最低硕士
        """,
    )
    parser.add_argument("--limit", type=int, default=20,
                        help="从聊天联系人列表【顶部往下审查的联系人个数】(前 N 个，"
                             "含被学校/学历筛掉、已回复/无消息跳过的，都计入)，默认 20。"
                             "注意：这是'审查前N个联系人'，不是'回复N个人'")
    parser.add_argument("--dry-run", action="store_true", help="仅预览: 输入回复但不发送，不点不合适")
    parser.add_argument("--schools", type=str, default=None, help="学校白名单，逗号分隔 (默认ALL_ELITE_SCHOOLS)")
    parser.add_argument("--min-degree", type=str, default=DEFAULT_MIN_DEGREE, help=f"最低学历要求 (默认{DEFAULT_MIN_DEGREE})")
    parser.add_argument("--config", type=str, default=None, help="话术模板文件路径")
    args = parser.parse_args()

    # 学校白名单
    school_whitelist = (
        [s.strip() for s in args.schools.split(",")]
        if args.schools else ALL_ELITE_SCHOOLS
    )

    jobs_config = load_jobs_config(args.config)

    job_count = len(jobs_config.get("jobs", []))
    tpl_count = sum(len(j.get("templates", [])) for j in jobs_config.get("jobs", []))
    tpl_count += len(jobs_config.get("fallback_templates", []))

    print("=" * 60)
    print(f"BOSS候选人审查 | {len(school_whitelist)}所学校 | "
          f"最低{args.min_degree} | 上限{args.limit}人")
    print(f"模式: {'预览(dry-run)' if args.dry_run else '执行'} | "
          f"{job_count}个岗位 | {tpl_count}条话术")
    print("=" * 60)

    # 启动时检查 DeepSeek 配置（未配置时打印醒目警告，后续不再重复）
    if not args.dry_run:
        check_deepseek_configured()

    # ── 初始化 ──
    start_session()
    chrome = find_boss_window()
    pid, wid = chrome["pid"], chrome["window_id"]

    navigate_to_chat(pid, wid)

    # ── 扫描 ──
    contacts = scan_all_contacts(pid, wid)
    if not contacts:
        print("\n✅ 没有联系人")
        return

    contacts = contacts[:args.limit]
    print(f"\n5. 逐个审查 ({len(contacts)} 人)...")

    import sqlite3 as _sqlite3
    db = _sqlite3.connect(str(DB_PATH))

    replied_results = []  # 收集已回复结果，用于结尾模板层级统计
    for i, contact in enumerate(contacts):
        print(f"\n  [{i + 1}/{len(contacts)}] {contact.get('name', '?')} "
              f"| {contact.get('job', '?')} | {contact.get('time', '?')}")

        # 检测上限
        limit = check_limit_popup(pid, wid)
        if limit:
            print(f"  🛑 {limit}")
            dismiss_limit_popup(pid, wid)
            break

        result = review_one_candidate(
            pid, wid, contact, school_whitelist, jobs_config, args.min_degree, args.dry_run,
            db=db,
        )
        if result.get("status") == "replied":
            replied_results.append(result)

        # 存聊天记录到 candidates.db
        chat_history = result.get("chat_history")
        if chat_history:
            _save_chat_history(
                db, contact, result, chat_history
            )

        # 随机间隔
        if i < len(contacts) - 1:
            delay = random.uniform(1.5, 3)
            time.sleep(delay)

    db.close()
    _print_layer_summary(replied_results)
    print(f"\n{'=' * 60}")
    print(f"审查完成，聊天记录已存入 {DB_PATH}")
    print(f"{'=' * 60}")


def _print_layer_summary(replied_results: list[dict]) -> None:
    """结尾打印模板命中层级统计；未命中①岗位专属的逐条给出 warning"""
    if not replied_results:
        return

    counts = {MATCH_JOB: 0, MATCH_CATEGORY: 0, MATCH_FALLBACK: 0, MATCH_NONE: 0}
    for r in replied_results:
        counts[r.get("match_layer", MATCH_NONE)] = counts.get(r.get("match_layer", MATCH_NONE), 0) + 1

    total = len(replied_results)
    print(f"\n{'=' * 60}")
    print(f"模板命中层级统计（共 {total} 条回复）")
    for layer in (MATCH_JOB, MATCH_CATEGORY, MATCH_FALLBACK, MATCH_NONE):
        n = counts.get(layer, 0)
        if n:
            pct = n * 100 // total
            print(f"  {_LAYER_LABEL[layer]}: {n} 条 ({pct}%)")

    # 未命中①岗位专属的候选人 → warning 列表
    non_job = [r for r in replied_results if r.get("match_layer") != MATCH_JOB]
    if non_job:
        print(f"\n  ⚠ {len(non_job)} 条未命中①岗位专属模板，建议为对应岗位补充话术：")
        for r in non_job:
            layer = _LAYER_LABEL.get(r.get("match_layer"), r.get("match_layer"))
            degraded = "（DeepSeek降级）" if r.get("reply_source") == SOURCE_TEMPLATE else ""
            print(f"    - {r.get('name', '?')}: {layer}{degraded}")
    else:
        print(f"\n  ✓ 全部命中①岗位专属模板")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()

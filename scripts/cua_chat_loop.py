#!/usr/bin/env python3
"""
cua-driver 驱动的 BOSS直聘批量自动聊天
======================================

用法:
  python scripts/cua_chat_loop.py              # 最多处理20个未读
  python scripts/cua_chat_loop.py --dry-run    # 仅预览
  python scripts/cua_chat_loop.py --limit 10   # 最多10个
  python scripts/cua_chat_loop.py --min-degree 硕士  # 最低学历硕士
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
from app.filter_criteria import ALL_ELITE_SCHOOLS, match_school
from app.chat_reply import load_templates, generate_reply, check_degree

SESSION_ID = "boss-chat"
CHROME_BUNDLE_ID = "com.google.Chrome"
CHAT_URL = "https://www.zhipin.com/web/chat/index"

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
    """等待页面渲染完成"""
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
        tree = snap.get("tree_markdown", "")
        elem_count = snap.get("element_count", 0)

        if elem_count > 300:
            print(f"  {prefix}✓ 就绪 ({elem_count}元素, {elapsed:.1f}s)")
            return True
        if elem_count > 100:
            print(f"  {prefix}加载中... ({elem_count}元素, {elapsed:.1f}s)")

    print(f"  {prefix}⚠ 超时 ({elapsed:.1f}s)")
    return False


# ══════════════════════════════════════════════════
# 限制检测
# ══════════════════════════════════════════════════

def dismiss_limit_popup(pid: int, window_id: int):
    """关闭限制弹窗"""
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
    print("✓ 已关闭")


def check_limit_popup(pid: int, window_id: int) -> Optional[str]:
    """检测是否弹出每日上限提示"""
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
# 会话与窗口
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
        print("  ❌ Chrome 未运行")
        sys.exit(1)

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
            print(f"  ⚠ 窗口隐藏, 请点 Dock 中 Chrome 使其可见")
            sys.exit(1)

    print("  ❌ 找不到 BOSS直聘窗口")
    sys.exit(1)


def navigate_to_chat(pid: int, window_id: int):
    """跳转到聊天页面并确保显示联系人列表"""
    print("3. 进入聊天页面...")
    cua("page", json.dumps({
        "pid": pid, "window_id": window_id,
        "action": "execute_javascript",
        "javascript": f'window.location.href = "{CHAT_URL}"',
    }))
    print("  等待页面加载...", end=" ", flush=True)
    time.sleep(5)
    wait_for_page(pid, window_id, label="聊天页")

    # 检查是否已经在联系人列表（有"未读"标记）
    snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": window_id, "capture_mode": "ax"}))
    tree = snap.get("tree_markdown", "")
    if "未读" not in tree:
        # 可能落在了数据子页面，点击左侧"沟通"链接
        print("  切换到联系人列表...", end=" ", flush=True)
        for line in tree.split("\n"):
            if "沟通" in line and "AXLink" in line:
                m = re.search(r'\[(\d+)\]', line)
                if m:
                    cua("click", json.dumps({"pid": pid, "window_id": window_id, "element_index": int(m.group(1))}))
                    time.sleep(3)
                    break
        print("✓")


# ══════════════════════════════════════════════════
# 扫描未读联系人
# ══════════════════════════════════════════════════

def scan_unread_contacts(pid: int, window_id: int) -> list[dict]:
    """扫描左侧未读联系人列表（基于 AX 树解析）

    BOSS 聊天页 AX 树结构:
      [74] 时间 "11:50"
      [75] 姓名 "程先生"
      [76] 职位 "首席科学家"
      [77] 状态 "[送达]"
      [78] 消息内容
      [79] 时间 "10:25"
      [80] 姓名 "陈泽颖"
      ...
      [83] 未读数 "1"      ← 这就是未读标记
    """
    print("4. 扫描未读联系人...")

    snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": window_id, "capture_mode": "ax"}))
    tree = snap.get("tree_markdown", "")
    if not tree:
        print("  ⚠ AX 树为空")
        return []

    # 逐行解析，找到"未读"标记之后的联系人列表区域
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

        # 检测联系人列表起始标记
        if val == "未读":
            in_contact_list = True
            continue
        if val in ("批量", "买赠", "帮你问牛人", "不符牛人"):
            continue

        if not in_contact_list:
            continue

        # 检测未读数字 (1-9 或 10-99)
        if re.match(r'^\d{1,2}$', val) and int(val) > 0 and current_name:
            contacts.append({
                "name": current_name,
                "job": current_job or "",
                "message": current_msg or "",
                "time": current_time or "",
                "unread": int(val),
                "ax_index": idx,
            })
            current_name = None
            current_job = None
            current_msg = None
            current_time = None
            continue

        # 时间模式: "11:50", "昨天", "前天", "06-03" 等
        if re.match(r'^(?:\d{1,2}:\d{2}|昨天|前天|\d{1,2}-\d{1,2})$', val):
            current_time = val
            continue

        # 状态标记: "[送达]", "[盼回复]" 等
        if re.match(r'^\[.+\]$', val):
            continue

        # 筛选相关标记: "全部", "新招呼", "(6)", "沟通中" 等
        if val in ("全部", "新招呼", "沟通中", "已约面", "已获取简历",
                    "已交换电话", "已交换微信", "收藏", "更多", "全部职位"):
            in_contact_list = False
            continue
        if re.match(r'^\(\d+\)$', val):
            continue

        # 名字: 2-4 个中文字符（非职位、非时间后的字段）
        if not current_name and re.match(r'^[一-鿿]{2,4}$', val):
            current_name = val
            continue

        # 职位/消息内容: 名字后面第一个短文本视为职位
        if current_name and not current_job and len(val) <= 20:
            current_job = val
            continue

        # 消息内容: 较长的文本
        if current_name and not current_msg and len(val) > 5:
            current_msg = val[:60]
            continue

    print(f"  找到 {len(contacts)} 个未读联系人")
    for c in contacts[:5]:
        print(f"    {c['name']:8s} | {c['job']:14s} | 未读{c['unread']}条 | {c['time']}")
    if len(contacts) > 5:
        print(f"    ... 还有 {len(contacts) - 5} 个")
    return contacts


# ══════════════════════════════════════════════════
# 点击联系人 & 读取对话
# ══════════════════════════════════════════════════

def click_contact(pid: int, window_id: int, contact: dict) -> bool:
    """点击联系人打开对话

    AXStaticText 不支持 AXPress，必须通过 JS 找到名字元素的父级卡片并点击
    """
    name = contact.get("name", "")
    if not name:
        return False

    # JS 点击: 找到包含名字文本的叶子元素，点击其可点击父级
    r = cua("page", json.dumps({
        "pid": pid, "window_id": window_id,
        "action": "execute_javascript",
        "javascript": f"""
        (function(){{
            var name = '{name}';
            // 找所有包含该名字的叶子元素
            var all = document.querySelectorAll('*');
            var found = null;
            for (var i = 0; i < all.length; i++) {{
                var el = all[i];
                // 只匹配叶子节点（没有子元素或者子元素很少）
                if (el.children.length > 3) continue;
                var text = (el.textContent || '').trim();
                if (text === name && el.offsetWidth > 0 && el.offsetHeight > 0) {{
                    found = el;
                    break;
                }}
            }}
            if (!found) return 'name_not_found';
            // 逐级向上查找可点击的父元素
            var target = found;
            for (var j = 0; j < 5; j++) {{
                var parent = target.parentElement;
                if (!parent) break;
                var style = getComputedStyle(parent);
                // 可点击的父元素通常有 cursor:pointer 或是 a/button
                if (parent.tagName === 'A' || parent.tagName === 'BUTTON' ||
                    style.cursor === 'pointer' || parent.onclick) {{
                    parent.click();
                    return 'clicked_parent';
                }}
                target = parent;
            }}
            // 最后兜底: 点击名字元素的父级
            found.parentElement && found.parentElement.click();
            return 'clicked_fallback';
        }})()
        """,
    }))
    result_text = ""
    if isinstance(r, list):
        result_text = " ".join(str(x) for x in r)
    else:
        result_text = str(r.get("result", r.get("text", "")))
    return "clicked" in result_text


def read_conversation(pid: int, window_id: int) -> dict:
    """读取当前对话的候选人信息和最近消息

    点击联系人后右侧面板加载对话，从中提取:
    - 姓名/学校/学历（对话头部区域）
    - 最近消息（对话内容区域）
    """
    result = {"name": "", "school": None, "degree": None, "messages": [], "latest_message": ""}

    # 1. AX 树读取候选人信息和消息
    snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": window_id, "capture_mode": "ax"}))
    tree = snap.get("tree_markdown", "")
    if tree:
        ax_messages = []
        found_header = False
        for line in tree.split("\n"):
            s = line.strip()
            # 学校
            m = re.search(r'AXStaticText\s*=\s*"([一-龥]{2,8}(?:大学|学院|学校))"', s)
            if m and not result["school"]:
                result["school"] = m.group(1)
            # 学历
            m = re.search(r'AXStaticText\s*=\s*"(博士|硕士|本科|大专)"', s)
            if m and not result["degree"]:
                result["degree"] = m.group(1)
            # 收集所有较长的文本作为消息候选
            m = re.search(r'AXStaticText\s*=\s*"(.+?)"', s)
            if m:
                val = m.group(1)
                # 候选人信息行: "广州华南商贸职业学院 · 软件技术 · 大专"
                if "·" in val and not result["name"]:
                    parts = val.split("·")
                    # 从中提取名字（如果有的话）
                    for p in parts:
                        p = p.strip()
                        if re.match(r'^[一-鿿]{2,4}$', p):
                            result["name"] = p
                            break
                if len(val) > 15:
                    ax_messages.append(val)

        # AX 消息兜底: 取最后几条长文本中候选人的消息
        if ax_messages and not result["latest_message"]:
            # 过滤掉明显是自己发的消息（以"您好，我们是"开头）
            for msg in reversed(ax_messages):
                if not msg.startswith("您好，我们") and "沟通的职位" not in msg:
                    result["latest_message"] = msg[:200]
                    break
            if not result["latest_message"]:
                result["latest_message"] = ax_messages[-1][:200]

    # 2. JS 读取最近消息（更精确）
    r = cua("page", json.dumps({
        "pid": pid, "window_id": window_id,
        "action": "execute_javascript",
        "javascript": """
        (function(){
            var result = {latest_message: ''};
            // 方法1: 通过 DOM 查找消息元素
            var msgEls = document.querySelectorAll(
                '[class*="message"], [class*="msg-item"], [class*="chat-msg"], ' +
                '[class*="bubble"], [class*="msg-content"]'
            );
            var msgs = [];
            var recent = Array.from(msgEls).slice(-10);
            recent.forEach(function(el){
                var isSelf = el.querySelector('[class*="self"], [class*="right"], [class*="mine"]');
                var text = (el.innerText || '').trim();
                if (text.length < 1) return;
                msgs.push({
                    role: isSelf ? "assistant" : "user",
                    content: text.substring(0, 200)
                });
            });
            // 最后一条候选人消息
            for (var i = msgs.length - 1; i >= 0; i--) {
                if (msgs[i].role === 'user') {
                    result.latest_message = msgs[i].content;
                    break;
                }
            }
            // 方法2: 兜底 - 通过 page get_text 获取全部文本
            if (!result.latest_message) {
                // 尝试找右侧对话区域的大段文本
                var allText = document.body.innerText || '';
                var lines = allText.split('\\n').filter(function(l){return l.trim().length > 5});
                // 取最后一段非导航文本
                for (var j = lines.length - 1; j >= 0; j--) {
                    var line = lines[j].trim();
                    if (line.length > 10 && line.length < 300 &&
                        line.indexOf('沟通') < 0 && line.indexOf('推荐') < 0 &&
                        line.indexOf('管理') < 0 && line.indexOf('全部') < 0) {
                        result.latest_message = line;
                        break;
                    }
                }
            }
            return JSON.stringify(result);
        })()
        """,
    }))
    msg_text = ""
    if isinstance(r, list):
        msg_text = " ".join(str(x) for x in r)
    else:
        msg_text = str(r.get("result", r.get("text", "")))

    try:
        parsed = json.loads(msg_text.strip().strip('"'))
        if parsed.get("latest_message"):
            result["latest_message"] = parsed["latest_message"]
    except (json.JSONDecodeError, TypeError):
        pass

    return result


# ══════════════════════════════════════════════════
# 不合适 & 发送回复
# ══════════════════════════════════════════════════

def click_unsuitable(pid: int, window_id: int) -> bool:
    """点击'不合适'按钮"""
    # 1. AX 树查找
    snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": window_id}))
    tree = snap.get("tree_markdown", "")
    if tree:
        for line in tree.split("\n"):
            if "不合适" in line and "AXButton" in line:
                # 已标记则跳过
                if "已标记" in line or "已" in line:
                    print("    已标记不合适，跳过")
                    return True
                m = re.search(r'\[(\d+)\]', line)
                if m:
                    idx = int(m.group(1))
                    r = cua("click", json.dumps({"pid": pid, "window_id": window_id, "element_index": idx}))
                    if not r.get("error"):
                        print("    ✓ 已标记不合适")
                        return True

    # 2. JS DOM 查找
    r = cua("page", json.dumps({
        "pid": pid, "window_id": window_id,
        "action": "execute_javascript",
        "javascript": """
        (function(){
            var btns = document.querySelectorAll('button, [class*="btn"], [class*="button"]');
            for (var i = 0; i < btns.length; i++) {
                var t = (btns[i].textContent || '').trim();
                if (t === '不合适') {
                    btns[i].click();
                    return 'clicked';
                }
                if (t.indexOf('已标记') >= 0 || t.indexOf('已') >= 0) {
                    if (t.indexOf('不合适') >= 0) return 'already';
                }
            }
            return 'not_found';
        })()
        """,
    }))
    result_text = ""
    if isinstance(r, list):
        result_text = " ".join(str(x) for x in r)
    else:
        result_text = str(r.get("result", r.get("text", "")))

    if "clicked" in result_text:
        print("    ✓ 已标记不合适 (JS)")
        return True
    if "already" in result_text:
        print("    已标记不合适，跳过")
        return True

    print("    ⚠ 未找到'不合适'按钮")
    return False


def send_reply(pid: int, window_id: int, text: str, skip_send: bool = False) -> bool:
    """输入回复，skip_send=True 时只输入不按 Enter"""
    # 1. 定位输入框
    input_idx = None

    # AX 树查找 textarea
    snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": window_id}))
    tree = snap.get("tree_markdown", "")
    if tree:
        for line in tree.split("\n"):
            if ("AXTextArea" in line or "textArea" in line) and "[" in line:
                m = re.search(r'\[(\d+)\]', line)
                if m:
                    input_idx = int(m.group(1))
                    break

    # AX 没找到则用 JS 聚焦
    if input_idx is None:
        cua("page", json.dumps({
            "pid": pid, "window_id": window_id,
            "action": "execute_javascript",
            "javascript": """
            (function(){
                var el = document.querySelector(
                    'textarea, [contenteditable="true"], [class*="chat-input"], [class*="input-box"]'
                );
                if (el) { el.focus(); return 'focused'; }
                return 'not_found';
            })()
            """,
        }))
        time.sleep(0.3)

    # 2. 输入文本
    if input_idx:
        r = cua("type_text", json.dumps({
            "pid": pid, "window_id": window_id,
            "element_index": input_idx, "text": text,
        }))
    else:
        r = cua("type_text", json.dumps({
            "pid": pid, "window_id": window_id,
            "text": text,
        }))

    if r.get("error"):
        print(f"    ❌ 输入失败: {r['error']}")
        return False

    # 3. 发送（skip_send 模式跳过）
    if skip_send:
        return True

    time.sleep(0.3)
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
# 单个联系人处理
# ══════════════════════════════════════════════════

def process_one_contact(
    pid: int,
    window_id: int,
    contact: dict,
    templates: list[dict],
    min_degree: str,
    dry_run: bool,
) -> dict:
    """处理一个未读联系人，返回结果"""
    preview = contact.get("text", "?")[:30]
    name = ""

    # a. 点击联系人
    click_contact(pid, window_id, contact)
    time.sleep(1.5)

    # b. 读取对话
    convo = read_conversation(pid, window_id)
    name = convo.get("name", "")
    school = convo.get("school")
    degree = convo.get("degree")
    latest_msg = convo.get("latest_message", "")

    school_match = "✅" if school and match_school(school, ALL_ELITE_SCHOOLS) else "  "
    print(f"    {name or '?':8s} | {school or '?':14s} {school_match} | {degree or '?':4s} | 最新: {latest_msg[:30]}")

    # c. 学历筛选
    if degree and not check_degree(degree, min_degree):
        print(f"    → 学历不符 ({degree} < {min_degree})，标记不合适")
        if not dry_run:
            click_unsuitable(pid, window_id)
        return {"status": "rejected_degree", "name": name, "degree": degree}

    # d. 无候选人消息则跳过
    if not latest_msg:
        print("    → 无候选人消息，跳过")
        return {"status": "no_message", "name": name}

    # e. 生成回复
    history = convo.get("messages", [])
    reply = generate_reply(latest_msg, templates, name, history)
    print(f"    → 回复: {reply[:50]}")

    # f. 输入回复
    ok = send_reply(pid, window_id, reply, skip_send=dry_run)
    if not ok:
        return {"status": "send_error", "name": name}

    if dry_run:
        print("    [预览] 已输入未发送")

    return {"status": "replied", "name": name, "reply": reply}


# ══════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="cua-driver 驱动 BOSS直聘批量自动聊天")
    parser.add_argument("--limit", type=int, default=20, help="最多处理N个未读")
    parser.add_argument("--dry-run", action="store_true", help="仅预览不发送")
    parser.add_argument("--min-degree", type=str, default="本科", help="最低学历要求")
    parser.add_argument("--config", type=str, default=None, help="话术模板文件路径")
    args = parser.parse_args()

    templates = load_templates(args.config)

    print("=" * 50)
    print(f"BOSS自动聊天 | 最低{args.min_degree} | 上限{args.limit}人 | "
          f"{'预览' if args.dry_run else '执行'} | {len(templates)}条话术")
    print("=" * 50)

    start_session()
    chrome = find_boss_window()
    pid, wid = chrome["pid"], chrome["window_id"]

    navigate_to_chat(pid, wid)

    contacts = scan_unread_contacts(pid, wid)
    if not contacts:
        print("\n✅ 没有未读消息")
        return

    contacts = contacts[: args.limit]

    print(f"\n5. 处理未读联系人 ({len(contacts)} 个)...")

    stats = {"replied": 0, "rejected_degree": 0, "no_message": 0, "send_error": 0}

    for i, contact in enumerate(contacts):
        print(f"\n  [{i + 1}/{len(contacts)}] {contact.get('text', '?')[:40]}")

        # 检测上限
        limit = check_limit_popup(pid, wid)
        if limit:
            print(f"  🛑 {limit}")
            dismiss_limit_popup(pid, wid)
            break

        result = process_one_contact(pid, wid, contact, templates, args.min_degree, args.dry_run)
        stats[result["status"]] = stats.get(result["status"], 0) + 1

        # 随机间隔
        if i < len(contacts) - 1:
            time.sleep(random.uniform(1.5, 3))

    print(f"\n{'=' * 50}")
    print(f"完成: {stats.get('replied', 0)} 已回复 | "
          f"{stats.get('rejected_degree', 0)} 学历不符 | "
          f"{stats.get('no_message', 0)} 无消息 | "
          f"{stats.get('send_error', 0)} 发送失败")
    print("=" * 50)


if __name__ == "__main__":
    main()

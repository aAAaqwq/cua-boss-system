#!/usr/bin/env python3
"""
沟通页批量收集候选人 — 简历 & 微信 → SQLite

流程:
  ① 进入聊天页 → 滚动加载
  ② AX树扫描所有联系人
  ③ 逐个审查:
      学校不在白名单/学历不达标 → 点"不合适"
      符合条件 → 点"附件简历"(BOSS自动处理3种情况) → 提取 → 换微信
  ④ 直接点侧边栏下一个，不刷新页面

用法:
  python scripts/cua_collect.py --dry-run           # 预览
  python scripts/cua_collect.py --limit 10           # 前10个
  python scripts/cua_collect.py --min-degree 硕士    # 学历筛选
"""
import json, sqlite3, subprocess, sys, time, re, random
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.filter_criteria import ALL_ELITE_SCHOOLS, match_school
from app.chat_reply import check_degree
from scripts.boss_click_buheshi import click_buheshi

SESSION = "boss-collect"
CHROME = "com.google.Chrome"
CHAT = "https://www.zhipin.com/web/chat/index"
DB_PATH = Path(__file__).parent.parent / "data" / "candidates.db"


def cua(*args):
    cmd = ["cua-driver"] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0: return {}
    try: return json.loads(r.stdout.strip() or "{}")
    except json.JSONDecodeError: return {"text": (r.stdout or "")[:200]}


def ax_tree(pid, wid):
    return cua("get_window_state", json.dumps({
        "pid": pid, "window_id": wid, "capture_mode": "ax"
    })).get("tree_markdown", "")


def find_window():
    apps = cua("list_apps")
    for a in apps.get("apps", []):
        if a.get("bundle_id") == CHROME and a.get("running"):
            pid = a["pid"]; break
    else: print("❌ Chrome 未运行"); sys.exit(1)
    lw = cua("list_windows", json.dumps({"pid": pid}))
    for w in lw.get("windows", []):
        t = w.get("title", "")
        if ("zhipin" in t or "BOSS" in t) and w.get("is_on_screen"):
            return pid, w["window_id"]
    for w in lw.get("windows", []):
        if "BOSS" in w.get("title", "") or "zhipin" in w.get("title", ""):
            return pid, w["window_id"]
    print("❌ 找不到窗口"); sys.exit(1)


# ══════════════════════════════════════════════════
# 扫描 & 点击
# ══════════════════════════════════════════════════

def scan_contacts(pid, wid):
    """扫描左侧联系人列表（时间→名字→职位模式）

    AX 树一次性暴露全部已渲染联系人，无需滚动。
    """
    tree = ax_tree(pid, wid)
    contacts = []
    cur_name, cur_job, cur_time = None, None, None

    # 不可能是人名的系统关键词
    SKIP_NAMES = {
        "全部", "未读", "批量", "全部职位", "买赠", "帮你问牛人",
        "不符牛人", "意向沟通", "已约面", "已获取简历", "已交换电话",
        "已交换微信", "收藏", "更多", "沟通中", "新招呼", "沟通",
        "升级VIP", "招聘规范", "我的客服", "面试", "招聘数据", "账号权益",
        "直聘企业版", "设置邮箱", "没有更多了",
        # 系统消息标识
        "已读", "送达", "发送",
        # 底部操作栏
        "求简历", "换电话", "查看微信", "约面试", "不合适", "复制微信号",
    }

    for line in tree.split("\n"):
        m = re.search(r'AXStaticText\s*=\s*"(.+?)"', line)
        if not m: continue
        val = m.group(1)

        # 时间/日期: 14:19 | 昨天 | 前天 | 06-04 | 06月04日
        if re.match(r'^(?:\d{1,2}:\d{2}|昨天|前天|\d{1,2}-\d{1,2}|\d{2}月\d{2}日)$', val):
            if cur_name:
                contacts.append({"name": cur_name, "job": cur_job or "", "time": cur_time or ""})
            cur_name = cur_job = None
            cur_time = val
            continue

        if cur_time and not cur_name:
            # 名字: 2-20 字符, 含中英文数字下划线, 排除系统关键词
            if (2 <= len(val) <= 20
                    and val not in SKIP_NAMES
                    and '顾问' not in val
                    and '心仪' not in val
                    and not val.startswith('(')
                    and not re.match(r'^\d+$', val)
                    and not re.match(r'^\[.+\]$', val)
                    and not re.search(r'\.(docx?|pdf)$', val)
                    and not re.match(r'^\d{1,2}:\d{2}$', val)):
                cur_name = val
                continue

        if cur_name and not cur_job:
            # 职位: 2-30 字符, 排除系统关键词和纯数字
            if (2 <= len(val) <= 30
                    and val not in SKIP_NAMES
                    and not re.match(r'^\d+$', val)
                    and not re.match(r'^\[.+\]$', val)
                    and not re.search(r'\.(docx?|pdf)$', val)):
                cur_job = val
                continue

        # 名字+职位之后的聊天消息预览 (>8 字符跳过, 短系统消息也跳过)
        if cur_name and cur_job:
            if len(val) > 8 or val in SKIP_NAMES:
                continue

    if cur_name:
        contacts.append({"name": cur_name, "job": cur_job or "", "time": cur_time or ""})

    # 去重 (同名只保留第一个)
    seen, unique = set(), []
    for c in contacts:
        if c["name"] not in seen:
            seen.add(c["name"])
            unique.append(c)
    return unique


def get_contact_uid(name, pid, wid):
    """从侧边栏 DOM 提取 BOSS 用户唯一标识 (data-id 属性)

    BOSS 侧边栏每个联系人 li 上有 data-id="<数字>-<索引>",
    数字部分即为用户加密 ID，跨会话唯一。
    """
    safe = name.replace("'", "\\'")
    r = cua("page", json.dumps({
        "pid": pid, "window_id": wid, "action": "execute_javascript",
        "javascript": f"""
        (function(){{
            var all = document.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {{
                var el = all[i];
                if ((el.textContent||'').trim()==='{safe}' && el.children.length<=1 && el.offsetWidth>0) {{
                    for (var p = el; p && p !== document.body; p = p.parentElement) {{
                        var did = p.getAttribute('data-id');
                        if (did) {{
                            var clean = did.replace(/-\\d+$/, '');
                            return JSON.stringify({{uid: clean, key: 'data-id', raw: did}});
                        }}
                    }}
                    break;
                }}
            }}
            return JSON.stringify({{uid: null}});
        }})()
        """,
    }))
    # r 是 cua-driver 直接返回的 JSON 解析结果
    if isinstance(r, dict) and "uid" in r:
        return r.get("uid")
    return None


def click_sidebar(name, pid, wid):
    """JS点侧边栏联系人，同时提取 data-id 作为 UID"""
    safe = name.replace("'", "\\'")
    r = cua("page", json.dumps({
        "pid": pid, "window_id": wid, "action": "execute_javascript",
        "javascript": f"""
        (function(){{
            var all = document.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {{
                var el = all[i];
                if ((el.textContent||'').trim()==='{safe}' && el.children.length<=1 && el.offsetWidth>0) {{
                    var uid = null;
                    for (var p = el; p && p !== document.body; p = p.parentElement) {{
                        var did = p.getAttribute('data-id');
                        if (did) {{ uid = did.replace(/-\\d+$/, ''); break; }}
                    }}
                    for (var lvl=0; lvl<8; lvl++) {{
                        if (el.onclick || getComputedStyle(el).cursor==='pointer') {{
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
    try:
        # r 是 cua-driver 直接返回的 JSON: {status, uid}
        if isinstance(r, dict) and "status" in r:
            return r.get("status") == "clicked", r.get("uid")
        # fallback: 纯文本返回值 (如 "clicked", "not_found")
        return "clicked" in str(r), None
    except (TypeError, AttributeError):
        return False, None


def js_click(text, pid, wid, last=False):
    """JS点击任意文字元素（找父级可点击）。last=True时取最后一个匹配元素"""
    safe = text.replace("'", "\\'")
    r = cua("page", json.dumps({
        "pid": pid, "window_id": wid, "action": "execute_javascript",
        "javascript": f"""
        (function(){{
            var all = document.querySelectorAll('*');
            var candidates = [];
            for (var i = 0; i < all.length; i++) {{
                if ((all[i].textContent||'').trim()==='{safe}' && all[i].children.length===0) {{
                    candidates.push(all[i]);
                }}
            }}
            var el = {f"candidates[candidates.length-1]" if last else "candidates[0]"};
            if (!el) return 'not_found';
            for (var lvl=0; lvl<8; lvl++) {{
                if (el.onclick || getComputedStyle(el).cursor==='pointer' ||
                    el.tagName==='BUTTON' || el.tagName==='A') {{
                    el.click(); return 'clicked';
                }}
                el = el.parentElement; if (!el) break;
            }}
            return 'not_clickable';
        }})()
        """,
    }))
    # r 可能是: dict (JS返回JSON) 或 {"text": "..."} (纯文本) 或 纯字符串
    if isinstance(r, dict):
        if "status" in r:
            return r.get("status") == "clicked"
        return "clicked" in str(r.get("text", ""))
    return "clicked" in str(r)


# _click_unfit 已提取到 scripts/boss_click_buheshi.py，通过 click_buheshi() 调用


def ax_click(text, pid, wid):
    """AX树找元素点击"""
    tree = ax_tree(pid, wid)
    for line in tree.split("\n"):
        if text in line and ('AXLink' in line or 'AXButton' in line):
            m = re.search(r'\[(\d+)\]', line)
            if m:
                r = cua("click", json.dumps({
                    "pid": pid, "window_id": wid, "element_index": int(m.group(1))
                }))
                if not r.get("error"): return True
    return False


def _right_panel_text(tree: str) -> str:
    """从 AX 树提取右侧对话面板区域（排除左侧联系人和聊天历史干扰）"""
    lines = tree.split("\n")
    panel_start = 0
    # 找到"没有更多了"之后第一个带名字+岁 的区块作为面板起点
    for i, line in enumerate(lines):
        m = re.search(r'\[(\d+)\]', line)
        idx = int(m.group(1)) if m else 0
        # 右侧面板在"没有更多了"之后: index ≈ 262+
        if idx >= 262 and 'AXStaticText' in line:
            panel_start = i
            break
    if not panel_start:
        return tree  # fallback
    return "\n".join(lines[panel_start:])


# ══════════════════════════════════════════════════
# 面板读取
# ══════════════════════════════════════════════════

def read_panel(pid, wid):
    """读右侧对话面板: name, school, degree, job"""
    tree = ax_tree(pid, wid)
    result = {"name": "", "school": "", "degree": "", "job": "",
              "has_attachment": False, "resume_filename": ""}

    for line in tree.split("\n"):
        # 学校
        m = re.search(r'AXStaticText\s*=\s*"([一-龥]{2,8}(?:大学|学院|学校))"', line)
        if m and not result["school"]: result["school"] = m.group(1)

        # 学历
        m = re.search(r'AXStaticText\s*=\s*"(博士|硕士|本科|大专)"', line)
        if m and not result["degree"]: result["degree"] = m.group(1)

        # "·" 分隔信息行 → job
        m = re.search(r'AXStaticText\s*=\s*"(.+)"', line)
        if m and "·" in m.group(1) and len(m.group(1)) < 80:
            parts = [p.strip() for p in m.group(1).split("·")]
            for p in parts:
                school_m = re.match(r'^([一-龥]{2,8}(?:大学|学院|学校))$', p)
                if school_m and not result["school"]: result["school"] = school_m.group(1)
                if p in ("博士","硕士","本科","大专") and not result["degree"]:
                    result["degree"] = p
            if not result["job"]: result["job"] = m.group(1)

        # 附件简历: AXLink=对方已发 / AXWebArea PDF=已打开预览
        idx_m = re.search(r'\[(\d+)\]', line)
        if idx_m and 270 <= int(idx_m.group(1)) <= 350:
            if 'AXLink (附件简历)' in line or ('AXWebArea' in line and 'PDF' in line):
                result["has_attachment"] = True

        # 附件文件名 (.pdf .docx .doc) — 仅限右侧面板区域 (idx 250-760)
        if idx_m and 250 <= int(idx_m.group(1)) <= 760:
            m = re.search(r'(?:AXStaticText|AXHeading)\s*=\s*"([^"]+\.(?:docx?|pdf|doc))"', line)
            if m: result["resume_filename"] = m.group(1)

    return result


def _wait_pdf_ready(pid, wid, timeout=45.0):
    """等待 PDF 在 AX 树中渲染完成。

    BOSS PDF 预览加载时 AX 树中会出现 hash token (如 ab1afeba...~~)，
    hash 消失 + 文本行数稳定 = PDF 渲染完成。
    """
    elapsed = 0.0
    prev_count = 0
    stable = 0
    interval = 2.0

    while elapsed < timeout:
        time.sleep(interval)
        elapsed += interval
        tree = ax_tree(pid, wid)

        # 扫描 PDF 区域: 统计文本行数 + 检测 hash token
        in_pdf, count = False, 0
        has_hash = False
        for line in tree.split("\n"):
            if 'AXWebArea' in line and 'PDF' in line:
                in_pdf = True
                continue
            if in_pdf:
                if any(t in line for t in ('AXPopUpButton', 'AXRadioButton', 'AXMenuBar')):
                    break
                if 'AXStaticText' in line:
                    m = re.search(r'AXStaticText\s*=\s*"([^"]*)"', line)
                    val = m.group(1) if m else ""
                    # hash token = PDF 还在渲染中
                    if re.match(r'^[a-f0-9]{20,}~*$', val):
                        has_hash = True
                        continue
                    count += 1

        # 有 hash 但已过 15s 且有文本 → hash 是持久 token, 忽略
        if has_hash:
            if elapsed > 15 and count > 0:
                has_hash = False  # 忽略持久 hash, 走下方稳定检测
            else:
                stable = 0
                prev_count = count
                continue

        # 无 hash 且无文本: PDF 是图片型 → 快速退出
        if not has_hash and count == 0 and prev_count == 0 and elapsed > 6:
            return False  # 图片 PDF, 无需继续等

        # 文本稳定检测: 连续 3 次 count 不变且 > 0
        if count > 0 and count == prev_count:
            stable += 1
            if stable >= 3:
                time.sleep(2)  # 额外等 2s 确保 AX 完全填充
                return True
        else:
            stable = 0
        prev_count = count

    return False  # 超时


def click_attachment_resume(pid, wid, name=""):
    """点击附件简历并根据返回状态处理

    修正后的流程:
      click_attachment_resume()
          │
          ├─ 等待一段时间（让PDF有时间渲染）
          │
          ├─ Step 1: 检测 PDF 预览是否弹出/展开
          │   └─ 是 → Case-1 → 提取附件 → advance ✅
          │
          ├─ Step 2: 检测是否出现"索取简历"确认弹窗
          │   └─ 是 → Case-2 → 点击确认索取 → advance ✅
          │
          └─ Step 3: 以上都不满足（以下四种全部跳过）
              ├─ "双方回复后可以向TA请求" → Case-4 → skip ❌
              ├─ "附件简历请求中"           → Case-3 → skip ❌
              ├─ 点击无任何反应（未沟通）      → 也跳过 ❌
              └─ 统一：不做任何操作，直接 advance

    Returns:
        dict: {case, resume_content, action}
            case: "case-1"|"case-2"|"case-3"|"case-4"|"unknown"
            resume_content: str
            action: "extract"|"request"|"skip"
    """
    # 点击附件简历
    if not ax_click("附件简历", pid, wid):
        js_click("附件简历", pid, wid)

    # 等待 PDF 渲染或弹窗出现
    time.sleep(2)
    tree = ax_tree(pid, wid)

    # 轮询等待 (最长 ~8s)
    for _ in range(4):
        if 'AXWebArea' in tree and 'PDF' in tree:
            break  # PDF 已打开
        if '向牛人请求简历' in tree:
            break  # 索取简历弹窗
        if '附件简历请求中' in tree:
            break  # 请求中
        if '双方回复后可以向TA请求' in tree:
            break  # 需双方回复
        time.sleep(1.5)
        tree = ax_tree(pid, wid)

    # ── Step 1: PDF 预览弹出 → Case-1: 已发附件简历 ──
    if 'AXWebArea' in tree and 'PDF' in tree:
        ready = _wait_pdf_ready(pid, wid)
        if not ready:
            print(f"    → 简历(Case-1): PDF 加载超时")
        # 滚动 PDF 确保渲染完整内容（BOSS PDF 预览只渲染可视区域）
        for _ in range(3):
            cua("scroll", json.dumps({"pid": pid, "window_id": wid,
                                       "direction": "down", "amount": 5}))
            time.sleep(1)
        resume_content = extract_resume_text(pid, wid, name)

        # PDF 提取失败 → 回退到在线简历
        if not resume_content or len(resume_content) <= 50:
            print(f"    → PDF 提取不足, 回退在线简历...")
            cua("press_key", json.dumps({"pid": pid, "window_id": wid, "key": "escape"}))
            time.sleep(2)
            resume_content = extract_resume_text(pid, wid, name)
            if resume_content and len(resume_content) > 50:
                print(f"    → 简历(Case-1→在线): 提取 {len(resume_content)} 字")
            elif resume_content:
                print(f"    → 简历(Case-1→在线): 仅 {len(resume_content)} 字")
            else:
                print(f"    → 简历(Case-1→在线): 也为空")
        else:
            print(f"    → 简历(Case-1): 已发附件, 提取 {len(resume_content)} 字")
        return {"case": "case-1", "resume_content": resume_content, "action": "extract"}

    # ── Step 2: "索取简历"确认弹窗 → Case-2: 已沟通未发简历 ──
    if '向牛人请求简历' in tree:
        clicked = ax_click("确认", pid, wid) or ax_click("确定", pid, wid)
        if not clicked:
            clicked = js_click("确认", pid, wid, last=True) or js_click("确定", pid, wid, last=True)
        print(f"    → 简历(Case-2): {'已确认索取' if clicked else '❌ 点击失败'}")
        time.sleep(1.5)
        # 确认后检查 PDF 是否打开
        tree2 = ax_tree(pid, wid)
        if 'AXWebArea' in tree2 and 'PDF' in tree2:
            _wait_pdf_ready(pid, wid)
            for _ in range(3):
                cua("scroll", json.dumps({"pid": pid, "window_id": wid,
                                           "direction": "down", "amount": 5}))
                time.sleep(1)
            resume_content = extract_resume_text(pid, wid, name)

            # PDF 提取失败 → 回退在线简历
            if not resume_content or len(resume_content) <= 50:
                print(f"    → PDF 提取不足, 回退在线简历...")
                cua("press_key", json.dumps({"pid": pid, "window_id": wid, "key": "escape"}))
                time.sleep(2)
                resume_content = extract_resume_text(pid, wid, name)
                if resume_content and len(resume_content) > 50:
                    print(f"    → 简历(Case-2→在线): 提取 {len(resume_content)} 字")
                elif resume_content:
                    print(f"    → 简历(Case-2→在线): 仅 {len(resume_content)} 字")
                else:
                    print(f"    → 简历(Case-2→在线): 也为空")
            else:
                print(f"    → 简历(Case-2→Case-1): 索取后提取 {len(resume_content)} 字")
            return {"case": "case-2", "resume_content": resume_content, "action": "extract"}
        return {"case": "case-2", "resume_content": "", "action": "request"}

    # ── Step 3: 以上都不满足 → 全部跳过 ──
    if '附件简历请求中' in tree:
        print(f"    → 简历(Case-3): 附件简历请求中, 跳过")
        return {"case": "case-3", "resume_content": "", "action": "skip"}

    if '双方回复后可以向TA请求' in tree:
        print(f"    → 简历(Case-4): 双方回复后可请求, 跳过")
        return {"case": "case-4", "resume_content": "", "action": "skip"}

    # 其他: "简历请求已发送"、点击无反应(未沟通) 等
    if '简历请求已发送' in tree:
        print(f"    → 简历: 请求已发送, 等待对方, 跳过")
    else:
        print(f"    → 简历: 未知状态(对方未上传附件或无反应), 跳过")
    return {"case": "unknown", "resume_content": "", "action": "skip"}


def extract_resume_text(pid, wid, expected_name: str = ""):
    """从 PDF 附件预览区域提取文本; 无 PDF 则回退到在线简历区域

    expected_name: 校验 PDF 内容是否属于当前候选人, 不匹配返回空
    """
    tree = ax_tree(pid, wid)
    lines = tree.split("\n")

    # ── 先找 PDF 预览区域 (AXWebArea) ──
    pdf_start = pdf_end = 0
    for i, line in enumerate(lines):
        if 'AXWebArea' in line and 'PDF' in line:
            pdf_start = i
        elif pdf_start and not pdf_end:
            if any(t in line for t in ('AXPopUpButton', 'AXRadioButton', 'AXMenuBar')):
                pdf_end = i
                break

    # 确定提取范围
    if pdf_start:
        extract_lines = lines[pdf_start:pdf_end] if pdf_end else lines[pdf_start:]
    else:
        extract_lines = [l for l in lines if re.search(r'\[(\d+)\]', l) and 250 <= int(re.search(r'\[(\d+)\]', l).group(1)) <= 760]

    # ── 提取并过滤 ──
    text_parts = []
    for line in extract_lines:
        m = re.search(r'AXStaticText\s*=\s*"([^"]*)"', line)
        if not m: continue
        val = m.group(1).strip()
        if not val: continue

        # 时间/日期
        if re.match(r'^\d{1,2}:\d{2}$', val): continue
        if re.match(r'^(?:昨天|前天|\d{1,2}-\d{1,2})$', val): continue
        # 纯数字 / 纯符号
        if re.match(r'^\d+$', val): continue
        if val in ('~', '~~', '-', '--'): continue
        # 哈希 token
        if re.match(r'^[a-f0-9]{20,}~*$', val): continue
        # 短名混入（但不排除候选人自己的姓名）
        if re.match(r'^[一-鿿a-zA-Z]{2,4}$', val) and val != expected_name: continue
        # 关键词
        if val in ('已读','送达','没有更多了','在线简历','附件简历','发送',
                   '求简历','换电话','换微信','约面试','不合适','复制微信号',
                   '查看微信','设置邮箱','内容:','业绩:','收藏','更多','沟通中','新招呼',
                   '刚刚活跃','今日活跃'):
            continue
        if any(kw in val for kw in ('沟通的职位','优先提醒','您可以在线预览',
                                      '后投递的简历','对方想发送','点击预览附件简历',
                                      '简历请求已发送', '请求交换微信已发送',
                                      '对方刚收藏了您的职位')):
            continue

        text_parts.append(val)

    # ── 拼接被截断的行: 前后行无句末标点 → 说明是同一句被 AX 切断 → 拼接 ──
    if len(text_parts) > 1:
        merged = [text_parts[0]]
        for i in range(1, len(text_parts)):
            prev = merged[-1]
            curr = text_parts[i]
            # 前一行非句末, 且当前行非句首 → 截断拼接
            prev_end = prev[-1] if prev else ''
            curr_start = curr[0] if curr else ''
            if (prev_end not in '。！？；：、，）)」』' and len(prev) > 10
                    and curr_start not in '（(「『◆●▪①②③④⑤⑥⑦⑧⑨' and not curr[0].isdigit()):
                merged[-1] = prev + curr
            else:
                merged.append(curr)
        text_parts = merged

    result = "\n".join(text_parts)

    # ── 校验: PDF 内容是否属于当前候选人 ──
    if expected_name and result and len(expected_name) >= 2:
        if expected_name not in result:
            # 名字可能被 AX 拆分: 检查所有字符是否出现在全文中
            if not all(c in result for c in expected_name):
                return ""  # PDF 不属于当前候选人

    return result


# ══════════════════════════════════════════════════
# SQLite
# ══════════════════════════════════════════════════

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT,
            name TEXT NOT NULL,
            job_position TEXT,
            school TEXT,
            degree TEXT,
            resume_content TEXT,
            resume_filename TEXT,
            has_resume INTEGER DEFAULT 0,
            wechat TEXT,
            has_wechat INTEGER DEFAULT 0,
            phone TEXT,
            email TEXT,
            score INTEGER DEFAULT 0,
            status TEXT DEFAULT 'collected',
            notes TEXT,
            extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 兼容旧表: 添加 uid 列（如果不存在）
    try:
        conn.execute("ALTER TABLE candidates ADD COLUMN uid TEXT")
    except sqlite3.OperationalError:
        pass  # 列已存在
    # uid 唯一索引（如果不存在）
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_uid ON candidates(uid)")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return conn


def upsert(conn, data):
    """按 uid 唯一键更新或插入。uid=None 时 fallback 到 (name, job_position)。

    已存在 → 只更新变化的字段，保留原有 id。
    不存在 → 插入新行。
    """
    uid = data.get("uid")
    if uid:
        conn.execute("""
            INSERT INTO candidates
                (uid, name, job_position, school, degree, resume_content, resume_filename,
                 has_resume, wechat, has_wechat, phone, email, score, status, notes, extracted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET
                name = excluded.name,
                job_position = excluded.job_position,
                school = excluded.school,
                degree = excluded.degree,
                resume_content = CASE WHEN excluded.resume_content != '' THEN excluded.resume_content ELSE candidates.resume_content END,
                resume_filename = CASE WHEN excluded.resume_filename != '' THEN excluded.resume_filename ELSE candidates.resume_filename END,
                has_resume = CASE WHEN excluded.has_resume = 1 THEN 1 ELSE candidates.has_resume END,
                wechat = CASE WHEN excluded.wechat != '' THEN excluded.wechat ELSE candidates.wechat END,
                has_wechat = CASE WHEN excluded.has_wechat = 1 THEN 1 ELSE candidates.has_wechat END,
                phone = CASE WHEN excluded.phone != '' THEN excluded.phone ELSE candidates.phone END,
                email = CASE WHEN excluded.email != '' THEN excluded.email ELSE candidates.email END,
                status = excluded.status,
                notes = excluded.notes,
                extracted_at = CURRENT_TIMESTAMP
        """, (
            uid, data.get("name",""), data.get("job",""),
            data.get("school",""), data.get("degree",""),
            data.get("resume_content",""), data.get("resume_filename",""),
            1 if data.get("has_resume") else 0,
            data.get("wechat",""), 1 if data.get("has_wechat") else 0,
            data.get("phone",""), data.get("email",""),
            data.get("score",0), data.get("status","collected"),
            data.get("notes",""), datetime.now().isoformat(),
        ))
    else:
        # 无 uid: fallback 到旧逻辑 (name, job_position)
        conn.execute("""
            INSERT INTO candidates
                (name, job_position, school, degree, resume_content, resume_filename,
                 has_resume, wechat, has_wechat, phone, email, score, status, notes, extracted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name, job_position) DO UPDATE SET
                school = excluded.school,
                degree = excluded.degree,
                resume_content = CASE WHEN excluded.resume_content != '' THEN excluded.resume_content ELSE candidates.resume_content END,
                resume_filename = CASE WHEN excluded.resume_filename != '' THEN excluded.resume_filename ELSE candidates.resume_filename END,
                has_resume = CASE WHEN excluded.has_resume = 1 THEN 1 ELSE candidates.has_resume END,
                wechat = CASE WHEN excluded.wechat != '' THEN excluded.wechat ELSE candidates.wechat END,
                has_wechat = CASE WHEN excluded.has_wechat = 1 THEN 1 ELSE candidates.has_wechat END,
                phone = CASE WHEN excluded.phone != '' THEN excluded.phone ELSE candidates.phone END,
                email = CASE WHEN excluded.email != '' THEN excluded.email ELSE candidates.email END,
                status = excluded.status,
                notes = excluded.notes,
                extracted_at = CURRENT_TIMESTAMP
        """, (
            data.get("name",""), data.get("job",""),
            data.get("school",""), data.get("degree",""),
            data.get("resume_content",""), data.get("resume_filename",""),
            1 if data.get("has_resume") else 0,
            data.get("wechat",""), 1 if data.get("has_wechat") else 0,
            data.get("phone",""), data.get("email",""),
            data.get("score",0), data.get("status","collected"),
            data.get("notes",""), datetime.now().isoformat(),
        ))
    conn.commit()


# ══════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════

def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--min-degree", default="本科")
    p.add_argument("--schools", type=str)
    args = p.parse_args()

    whitelist = ([s.strip() for s in args.schools.split(",")] if args.schools
                 else ALL_ELITE_SCHOOLS)

    print("=" * 60)
    print(f"BOSS候选人收集 | {len(whitelist)}所学校 | 最低{args.min_degree} | "
          f"上限{args.limit}人")
    print(f"模式: {'dry-run(操作但不写库+不点不合适)' if args.dry_run else '执行'}")
    print("=" * 60)

    if "running" not in str(cua("status")).lower():
        subprocess.run(["cua-driver", "serve"], capture_output=True); time.sleep(1)
    cua("start_session", json.dumps({"session": SESSION}))

    pid, wid = find_window()
    print(f"✓ pid={pid} wid={wid}")

    # ① 确保在聊天页（检测+导航+等待）
    print("\n① 进入聊天页...")
    # 先检测: 页面是否已经有联系人列表
    tree = ax_tree(pid, wid)
    times = re.findall(r'AXStaticText\s*=\s*"(\d{1,2}:\d{2})"', tree)
    if len(times) < 5:
        # 导航+等渲染
        print("  导航到聊天页...")
        cua("page", json.dumps({
            "pid": pid, "window_id": wid, "action": "execute_javascript",
            "javascript": f'window.location.href = "{CHAT}"',
        }))
        for i in range(30):
            time.sleep(1)
            times = re.findall(r'AXStaticText\s*=\s*"(\d{1,2}:\d{2})"', ax_tree(pid, wid))
            if len(times) >= 5:
                break
    print(f"  ✓ {len(times)} 个时间标记 ({len(times)}个联系人)")

    # ② 扫描 + 逐个收集
    print("\n② 开始收集...")
    conn = init_db() if not args.dry_run else None
    stats = {"collected": 0, "unsuitable": 0, "skipped": 0}

    contacts = scan_contacts(pid, wid)
    print(f"  {len(contacts)} 个联系人")

    for i, contact in enumerate(contacts):
        if stats["collected"] + stats["unsuitable"] >= args.limit:
            break

        name = contact["name"]
        print(f"\n  [{i+1}/{min(len(contacts), args.limit)}] {name} | {contact['job']}")

        # 点侧边栏, 失败重试一次; 同时提取 UID
        contact_uid = None
        clicked, contact_uid = click_sidebar(name, pid, wid)
        if not clicked:
            time.sleep(1)
            clicked, contact_uid = click_sidebar(name, pid, wid)
            if not clicked:
                print(f"    ❌ 点击失败"); stats["skipped"] += 1; continue
        if contact_uid:
            print(f"    uid: {contact_uid}")
        time.sleep(2)  # 等右侧面板加载

        panel = read_panel(pid, wid)
        school = panel["school"] or ""
        degree = panel["degree"] or ""
        job = panel["job"] or contact.get("job", "")

        ok = "✅" if match_school(school, whitelist) else "❌"
        print(f"    学校: {school or '?'} {ok}  学历: {degree or '?'}  岗位: {job}")

        # 筛选
        if not match_school(school, whitelist):
            print(f"    → 学校不符，点'不合适'")
            if not args.dry_run:
                click_buheshi(pid, wid)
            stats["unsuitable"] += 1
        elif degree and not check_degree(degree, args.min_degree):
            print(f"    → 学历不符，点'不合适'")
            if not args.dry_run:
                click_buheshi(pid, wid)
            stats["unsuitable"] += 1
        else:
            resume_content = ""
            # 检查DB是否已有简历(>200字才是有效简历)
            existing_resume = None
            if conn:
                if contact_uid:
                    row = conn.execute(
                        "SELECT resume_content FROM candidates WHERE uid=?",
                        (contact_uid,)).fetchone()
                else:
                    row = conn.execute(
                        "SELECT resume_content FROM candidates WHERE name=? AND job_position=?",
                        (name, job)).fetchone()
                if row and row[0]:
                    existing_resume = row[0]

            if existing_resume:
                resume_content = existing_resume
                print(f"    → 简历: 已存在({len(resume_content)}字), 跳过提取")
            else:
                result = click_attachment_resume(pid, wid, name)
                resume_content = result.get("resume_content", "")

            # 微信: 已交换→提取微信号, 可换→点换微信→确认, DB已有→跳过
            wechat_id = ""
            wechat_requested = False
            if conn:
                if contact_uid:
                    wx_row = conn.execute(
                        "SELECT wechat, has_wechat FROM candidates WHERE uid=?",
                        (contact_uid,)).fetchone()
                else:
                    wx_row = conn.execute(
                        "SELECT wechat, has_wechat FROM candidates WHERE name=? AND job_position=?",
                        (name, job)).fetchone()
                if wx_row and wx_row[1] and wx_row[0]:
                    wechat_id = wx_row[0]
                    wechat_requested = True
                    print(f"    → 微信: 已存在({wechat_id}), 跳过")

            if not wechat_requested:
                tree = ax_tree(pid, wid)
                # 已交换: 点"查看微信"→读微信号
                if "查看微信" in tree:
                    js_click("查看微信", pid, wid); time.sleep(1)
                    tree2 = ax_tree(pid, wid)
                    for line in tree2.split('\n'):
                        if '微信号' in line and 'AXHeading' in line:
                            m = re.search(r'"([^"]+)"', line)
                            if m:
                                parts = m.group(1).split('：')
                                if len(parts) > 1:
                                    wechat_id = parts[-1].strip()
                                    wechat_requested = True
                                    print(f"    → 微信: {wechat_id}")
                                    break
                # 未交换: 点"换微信"→确认
                elif "换微信" in tree:
                    js_click("换微信", pid, wid); time.sleep(1.5)
                    if "确定与对方交换微信" in ax_tree(pid, wid):
                        js_click("确定", pid, wid)
                        wechat_requested = True
                        print(f"    → 微信: 已请求交换")

            # 从简历内容中提取手机号&邮箱（不依赖标签, 直接搜模式）
            phone = email = ""
            if resume_content:
                # 手机号: 11位1开头
                pm = re.search(r'(?<!\d)(1[3-9]\d{9})(?!\d)', resume_content)
                if pm: phone = pm.group(1)
                # 邮箱
                em = re.search(r'(\S+@\S+\.\S{2,})', resume_content)
                if em: email = em.group(1).rstrip('.,;:）)')

            data = {
                "uid": contact_uid,
                "name": name, "job": job, "school": school, "degree": degree,
                "resume_content": resume_content,
                "resume_filename": panel.get("resume_filename", ""),
                "has_resume": bool(resume_content),
                "wechat": wechat_id, "has_wechat": wechat_requested or bool(wechat_id),
                "phone": phone, "email": email, "status": "collected",
            }
            if not args.dry_run: upsert(conn, data)
            stats["collected"] += 1
            print(f"    ✓ 已收集")

        # 关掉简历预览 → Escape 关闭浮层
        cua("press_key", json.dumps({"pid": pid, "window_id": wid, "key": "escape"}))
        time.sleep(1)

    print(f"\n{'=' * 60}")
    print(f"收集完成: ✅{stats['collected']} 🚫{stats['unsuitable']} ⏭{stats['skipped']}")
    if conn:
        count = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        print(f"数据库: {DB_PATH} ({count} 条)")
        conn.close()


if __name__ == "__main__":
    main()

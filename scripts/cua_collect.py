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
    """扫描左侧联系人列表（时间→名字→职位模式）"""
    tree = ax_tree(pid, wid)
    contacts = []
    cur_name, cur_job, cur_time = None, None, None

    for line in tree.split("\n"):
        m = re.search(r'AXStaticText\s*=\s*"(.+?)"', line)
        if not m: continue
        val = m.group(1)

        if re.match(r'^(?:\d{1,2}:\d{2}|昨天|前天|\d{1,2}-\d{1,2})$', val):
            if cur_name: contacts.append(
                {"name": cur_name, "job": cur_job or "", "time": cur_time or ""})
            cur_name = cur_job = None; cur_time = val; continue

        if cur_time and not cur_name:
            if re.match(r'^[一-鿿a-zA-Z]{2,10}$', val) \
                    and '顾问' not in val and '心仪' not in val \
                    and val not in ("全部","未读","批量","全部职位","买赠","帮你问牛人",
                                   "不符牛人","意向沟通","已约面","已获取简历","已交换电话",
                                   "已交换微信","收藏","更多","沟通中","新招呼"):
                cur_name = val; continue

        if cur_name and not cur_job:
            if 2 <= len(val) <= 20 and not re.match(r'^\d+$', val) \
                    and not re.match(r'^\[.+\]$', val) \
                    and not re.search(r'\.(docx?|pdf)$', val):
                cur_job = val; continue

        if cur_name and cur_job and len(val) > 5: continue

    if cur_name: contacts.append(
        {"name": cur_name, "job": cur_job or "", "time": cur_time or ""})

    seen, unique = set(), []
    for c in contacts:
        if c["name"] not in seen: seen.add(c["name"]); unique.append(c)
    return unique


def click_sidebar(name, pid, wid):
    """JS点侧边栏联系人"""
    safe = name.replace("'", "\\'")
    r = cua("page", json.dumps({
        "pid": pid, "window_id": wid, "action": "execute_javascript",
        "javascript": f"""
        (function(){{
            var all = document.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {{
                var el = all[i];
                if ((el.textContent||'').trim()==='{safe}' && el.children.length<=1 && el.offsetWidth>0) {{
                    for (var lvl=0; lvl<8; lvl++) {{
                        if (el.onclick || getComputedStyle(el).cursor==='pointer') {{
                            el.click(); return 'clicked';
                        }}
                        el = el.parentElement; if (!el) break;
                    }}
                }}
            }}
            return 'not_found';
        }})()
        """,
    }))
    return "clicked" in str(r.get("result", r.get("text", "")))


def js_click(text, pid, wid):
    """JS点击任意文字元素（找父级可点击）"""
    safe = text.replace("'", "\\'")
    r = cua("page", json.dumps({
        "pid": pid, "window_id": wid, "action": "execute_javascript",
        "javascript": f"""
        (function(){{
            var all = document.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {{
                if ((all[i].textContent||'').trim()==='{safe}' && all[i].children.length===0) {{
                    for (var lvl=0; lvl<8; lvl++) {{
                        if (all[i].onclick || getComputedStyle(all[i]).cursor==='pointer' ||
                            all[i].tagName==='BUTTON' || all[i].tagName==='A') {{
                            all[i].click(); return 'clicked';
                        }}
                        all[i] = all[i].parentElement; if (!all[i]) break;
                    }}
                }}
            }}
            return 'not_found';
        }})()
        """,
    }))
    return "clicked" in str(r.get("result", r.get("text", "")))


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

        # 附件简历 AXLink（只取右侧面板: index 270-340）
        idx_m = re.search(r'\[(\d+)\]', line)
        if idx_m and 270 <= int(idx_m.group(1)) <= 340:
            if 'AXLink (附件简历)' in line:
                result["has_attachment"] = True

        # 附件文件名
        m = re.search(r'AXStaticText\s*=\s*"([^"]+\.(?:docx?|pdf|doc))"', line)
        if m: result["resume_filename"] = m.group(1)

    return result


def extract_resume_text(pid, wid):
    """从简历预览区提取文本（AX树 250-760 区间）"""
    tree = ax_tree(pid, wid)
    lines = []

    for line in tree.split("\n"):
        m = re.search(r'\[(\d+)\].*AXStaticText\s*=\s*"([^"]+)"', line)
        if not m: continue
        idx, val = int(m.group(1)), m.group(2)

        if not (250 <= idx <= 760): continue

        # 过滤
        if re.match(r'^\d{1,2}:\d{2}$', val): continue
        if re.match(r'^(?:昨天|前天|\d{1,2}-\d{1,2})$', val): continue
        if val in ('开发','CEO标注助理','已读','送达','没有更多了',
                   '拒绝','同意','在线简历','附件简历','发送',
                   '求简历','换电话','换微信','约面试','不合适'): continue
        if re.match(r'^(?:你好|您好|BOSS|Boss|boss|牛人|对方|此牛人|顾问|比较感兴趣|岗位主要是)', val): continue
        if re.match(r'^\d+$', val): continue  # 纯数字（未读计数/分页）
        if re.match(r'^(?:06月|07月|08月|09月|10月|11月|12月)\d{2}日$', val): continue
        if re.match(r'^[一-鿿a-zA-Z]{2,4}$', val) and len(val) <= 4: continue  # 名字混入（左侧面板）
        if any(kw in val for kw in ('沟通的职位','优先提醒','设置邮箱',
                                      '您可以在线预览','后投递的简历','对方想发送',
                                      '点击预览附件简历')): continue
        if re.match(r'^[a-f0-9]{40,}~+$', val): continue

        lines.append(val)

    return "\n".join(lines)


# ══════════════════════════════════════════════════
# SQLite
# ══════════════════════════════════════════════════

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(name, job_position)
        )
    """)
    conn.commit()
    return conn


def upsert(conn, data):
    conn.execute("""
        INSERT OR REPLACE INTO candidates
            (name, job_position, school, degree, resume_content, resume_filename,
             has_resume, wechat, has_wechat, phone, email, score, status, notes, extracted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    # ① 进入聊天页
    print("\n① 进入聊天页...")
    cua("page", json.dumps({
        "pid": pid, "window_id": wid, "action": "execute_javascript",
        "javascript": f'window.location.href = "{CHAT}"',
    }))
    for _ in range(20):
        time.sleep(1)
        if ax_tree(pid, wid).count("AXStaticText") > 100: break

    for pg in range(3):
        cua("scroll", json.dumps({"pid": pid, "window_id": wid,
                                   "direction": "down", "amount": 8}))
        time.sleep(1.5)

    # ② 扫描
    print("\n② 扫描联系人...")
    contacts = scan_contacts(pid, wid)
    if not contacts: print("❌ 未找到联系人"); sys.exit(1)

    total = len(contacts) if not args.limit else min(len(contacts), args.limit)
    print(f"  {len(contacts)} 个联系人 (处理 {total})")

    # ③ 逐个
    print(f"\n③ 逐个收集 ({total} 人)...")
    conn = init_db() if not args.dry_run else None
    stats = {"collected": 0, "unsuitable": 0, "skipped": 0}

    for i, contact in enumerate(contacts[:total]):
        name = contact["name"]
        print(f"\n  [{i+1}/{total}] {name} | {contact['job']}")

        # 点侧边栏, 失败重试一次
        if not click_sidebar(name, pid, wid):
            time.sleep(1)
            if not click_sidebar(name, pid, wid):
                print(f"    ❌ 点击失败"); stats["skipped"] += 1; continue
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
            if not args.dry_run: ax_click("不合适", pid, wid)
            stats["unsuitable"] += 1
        elif degree and not check_degree(degree, args.min_degree):
            print(f"    → 学历不符，点'不合适'")
            if not args.dry_run: ax_click("不合适", pid, wid)
            stats["unsuitable"] += 1
        else:
            resume_content = ""
            # 检查DB是否已有简历(>200字才是有效简历)
            existing_resume = None
            if conn:
                row = conn.execute(
                    "SELECT resume_content FROM candidates WHERE name=? AND job_position=?",
                    (name, job)).fetchone()
                if row and row[0] and len(row[0]) > 200:
                    existing_resume = row[0]

            if existing_resume:
                resume_content = existing_resume
                print(f"    → 简历: 已存在({len(resume_content)}字), 跳过提取")
            else:
                # 点"附件简历" — BOSS自动处理3种情况
                if not ax_click("附件简历", pid, wid):
                    js_click("附件简历", pid, wid)
                # 等预览加载: 轮询"个人简历/基本信息/个人资料", 最多15秒
                preview_opened = False
                for _ in range(15):
                    time.sleep(1)
                    tree = ax_tree(pid, wid)
                    if '个人简历' in tree or '基本信息' in tree or '个人资料' in tree:
                        time.sleep(3)
                        preview_opened = True
                        break
                if preview_opened:
                    resume_content = extract_resume_text(pid, wid)
                    print(f"    → 简历: {len(resume_content)} 字")
                else:
                    print(f"    → 简历: 无附件/需同意")

            # 换微信（DB已有则跳过）
            wechat_requested = False
            if conn:
                wx_row = conn.execute(
                    "SELECT has_wechat FROM candidates WHERE name=? AND job_position=?",
                    (name, job)).fetchone()
                if wx_row and wx_row[0]:
                    wechat_requested = True
                    print(f"    → 微信: 已存在, 跳过")
            if not wechat_requested and "换微信" in ax_tree(pid, wid):
                js_click("换微信", pid, wid); time.sleep(1.5)
                if "确定与对方交换微信" in ax_tree(pid, wid):
                    js_click("确定", pid, wid)
                    wechat_requested = True
                    print(f"    → 微信: 已请求交换")

            data = {
                "name": name, "job": job, "school": school, "degree": degree,
                "resume_content": resume_content,
                "resume_filename": panel.get("resume_filename", ""),
                "has_resume": bool(resume_content),
                "wechat": "", "has_wechat": wechat_requested,
                "status": "collected",
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

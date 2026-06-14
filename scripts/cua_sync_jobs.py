#!/usr/bin/env python3
"""
从 BOSS直聘职位管理页提取岗位详情 → 覆盖写入 config/jobs.json

提取规则:
  1. 只提取状态为"开放中"的岗位，跳过"关闭"的
  2. 列表页同名岗位只取第一个（去重）
  3. 逐个点击"编辑"进入详情 → 提取 title/requirements/salary/degree/location
  4. 覆盖写入 jobs.json（替换旧数据，保留话术模板）

用法:
  python scripts/cua_sync_jobs.py              # 提取 + 自动写入 config/jobs.json
  python scripts/cua_sync_jobs.py --dry-run    # 仅预览不写入
  python scripts/cua_sync_jobs.py --limit 3    # 只处理前N个
"""
import json
import subprocess
import sys
import time
import re
import random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

SESSION = "boss-sync"
CHROME = "com.google.Chrome"
JOB_LIST = "https://www.zhipin.com/web/chat/job/list"
CHAT = "https://www.zhipin.com/web/chat/index"
CONFIG = Path(__file__).parent.parent / "config" / "jobs.json"
TEMPLATE_PATH = Path(__file__).parent.parent / "config" / "jobs-template.json"

NAV_LINKS = {
    "职位管理","推荐牛人","搜索","沟通","意向沟通","互动","牛人管理",
    "道具","工具箱","更多","直聘企业版","招聘规范","","投递保",
    "关闭","编辑","1","2","直播招聘","道具 首充礼",
}

ID_MAP = [
    ("首席科学家","chief-scientist"),("技术合伙人","tech-partner"),
    ("运营实习生","ai-ops-intern"),("技术实习生","tech-intern"),
    ("产品经理","ai-product-manager"),("技术总监","tech-director"),
    ("合伙人","partner"),("全栈","ai-fullstack"),
    ("运营","ai-ops-intern"),("开发","dev"),("标注","annotation"),
    ("总监","director"),("助理","assistant"),("销售","sales"),
    ("咨询","consulting"),("实习","tech-intern"),
]
# ↑ 按 key 长度降序排列：长的优先匹配，避免 "全栈开发" 被 "开发" 误匹配


def cua(*args):
    cmd = ["cua-driver", "call"] + list(args)
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


def nav_to(url, pid, wid, check_fn, timeout=25):
    # 先检查是否已在目标页（避免不必要的导航）
    if check_fn(pid, wid):
        return True
    # 策略: 硬刷新优先（BOSS SPA 对 JS navigation 渲染不稳定）
    cua("page", json.dumps({
        "pid": pid, "window_id": wid, "action": "execute_javascript",
        "javascript": f'window.location.href = "{url}"',
    }))
    time.sleep(2)
    cua("hotkey", json.dumps({"pid": pid, "window_id": wid, "keys": ["cmd", "r"]}))
    for _ in range(timeout):
        time.sleep(1)
        if check_fn(pid, wid): return True
    # 兜底: 再试一次硬刷新
    cua("hotkey", json.dumps({"pid": pid, "window_id": wid, "keys": ["cmd", "r"]}))
    for _ in range(timeout):
        time.sleep(1)
        if check_fn(pid, wid): return True
    return False


def has_edit_links(pid, wid):
    return len(re.findall(r'AXLink\s*\(\s*编辑\s*\)', ax_tree(pid, wid))) >= 1


def has_textarea(pid, wid):
    return 'AXTextArea' in ax_tree(pid, wid)


def scan_open_jobs(pid, wid):
    """扫描列表页: 返回开放中 + 去重的岗位 [{title, edit_index}]

    每张卡片真实结构（岗位名在编辑前面!）:
      [80] AXLink(开发) [86] 16-30K ... [94] 开放中 [96] AXLink(编辑)
    """
    tree = ax_tree(pid, wid)
    lines = tree.split("\n")

    items = []
    for line in lines:
        m_idx = re.search(r'\[(\d+)\]', line)
        if not m_idx: continue
        idx = int(m_idx.group(1))
        m = re.search(r'AXStaticText\s*=\s*"([^"]*)"', line)
        if m: items.append((idx, 'text', m.group(1))); continue
        m = re.search(r'AXLink\s*\(\s*(.+?)\s*\)', line)
        if m: items.append((idx, 'link', m.group(1).strip()))
    items.sort()

    # 状态机: 跟踪 current_title → current_status → 编辑
    jobs = []
    seen_titles = set()
    current_title = None
    current_status = None

    def is_job_title(val):
        """判断 AXLink 文本是否是岗位名"""
        if val in NAV_LINKS: return False
        if val in ('编辑', '关闭', '打开'): return False
        if not re.search(r'[一-鿿]', val): return False
        if re.match(r'^沟通\s*\d*$', val): return False
        return len(val) >= 2

    for idx, typ, val in items:
        # 岗位名 AXLink
        if typ == 'link' and is_job_title(val):
            current_title = val
            current_status = None  # 重置状态，等下一个
            continue

        # 状态标记（开放中/关闭/待开放）
        if typ == 'text' and val in ('开放中', '关闭', '待开放'):
            current_status = val
            continue

        # 编辑按钮 → 配对!
        if typ == 'link' and val == '编辑':
            if current_title and current_status == '开放中':
                # 用 StaticText 版全名（取标题 AXLink 后面那个同名 StaticText）
                full_title = current_title
                if current_title not in seen_titles:
                    seen_titles.add(current_title)
                    jobs.append({"title": full_title, "edit_index": idx})
            current_title = None
            current_status = None
            continue

        # 关闭/打开按钮 → 复位
        if typ == 'link' and val in ('关闭', '打开'):
            continue

    return jobs


def extract_edit_page(pid, wid):
    """从编辑页提取表单字段（AX 树 + JS iframe 双通道）

    AX 树在 iframe 内容上会截断 → 职位描述用 JS 直接读 textarea.value
    """
    tree = ax_tree(pid, wid)
    result = {
        "title": "", "requirements": "", "salary": "",
        "degree": "", "location": "", "experience": "",
    }
    salary_tokens = []  # AXStaticText 字符串序列（如 ["40k","-","55k"] 或 ["120","-","180","元/天"]）
    in_salary_section = False
    salary_done = False  # 只取第一个薪资块（页面有重复预览）
    text_fields = []

    # 薪资区域结束标志（遇到这些即退出薪资 token 收集）
    SALARY_BOUNDARY = {
        "职位关键词", "工作地点", "奖金绩效", "职位类型",
        "职位要求", "补充信息", "实习要求",
    }

    for line in tree.split("\n"):
        # AXTextField — location
        m = re.search(r'AXTextField\s*=\s*"([^"]+)"', line)
        if m and m.group(1) and "zhipin.com" not in m.group(1) and "/" not in m.group(1):
            val = m.group(1)
            if 2 <= len(val) < 30: text_fields.append(val)
            if re.search(r'[区路街大厦座层号\d]', val) and len(val) > 5:
                result["location"] = val

        # 学历 — AXStaticText 精确匹配
        m = re.search(r'AXStaticText\s*=\s*"(博士|硕士|本科|大专)"', line)
        if m and not result["degree"]:
            result["degree"] = m.group(1)

        # 经验
        m = re.search(r'AXStaticText\s*=\s*"([^"]*[年应届].*)"', line)
        if m and not result["experience"]:
            val = m.group(1)
            if val not in ("1", "2", "职位管理", "招聘规范", "推荐牛人"):
                result["experience"] = val

        # 薪资区域: 只取第一个 "薪资范围" 块（页面有重复的预览副本）
        if not in_salary_section and not salary_done and re.search(r'AXStaticText\s*=\s*"薪资范围"', line):
            in_salary_section = True
            continue
        if in_salary_section:
            # 遇到边界标志则结束第一个薪资块
            m_bound = re.search(r'AXStaticText\s*=\s*"([^"]+)"', line)
            if m_bound and m_bound.group(1) in SALARY_BOUNDARY:
                in_salary_section = False
                salary_done = True
                continue
            # 只收集薪资相关的 AXStaticText
            m_st = re.search(r'AXStaticText\s*=\s*"([^"]+)"', line)
            if m_st:
                val = m_st.group(1)
                # K 格式: "40k", "55k"
                if re.match(r'^\d+[kK]$', val):
                    salary_tokens.append(val.lower())
                # 分隔符
                elif re.match(r'^[~\-—–]$', val):
                    salary_tokens.append("-")
                # 纯数字（元格式的 120, 180）
                elif re.match(r'^\d+$', val) and len(val) <= 4:
                    salary_tokens.append(val)
                # 单位
                elif re.match(r'^元/[天月]$', val):
                    salary_tokens.append(val)

    # 解析: 找 "-" 分隔或自动配对
    if salary_tokens:
        delim_idx = next((i for i, t in enumerate(salary_tokens) if t == "-"), -1)
        if delim_idx > 0:
            lo_tokens = salary_tokens[:delim_idx]
            hi_tokens = salary_tokens[delim_idx+1:]
        else:
            lo_tokens = salary_tokens
            hi_tokens = []

        # K 格式: tokens 含 "12k", "18k" 等
        k_vals = [t for t in salary_tokens if re.match(r'^\d+[kK]$', t)]
        if k_vals:
            if delim_idx >= 0:
                lo_k = [t for t in lo_tokens if re.match(r'^\d+[kK]$', t)]
                hi_k = [t for t in hi_tokens if re.match(r'^\d+[kK]$', t)]
                lo = "".join(lo_k).upper()
                hi = "".join(hi_k).upper()
            else:
                # 无分隔符: 前两个 K 值作为范围（BOSS 用下拉箭头分隔）
                if len(k_vals) >= 2:
                    lo, hi = k_vals[0].upper(), k_vals[1].upper()
                else:
                    lo, hi = k_vals[0].upper(), ""
            if lo and hi:
                result["salary"] = f"{lo}-{hi}"
            elif lo:
                result["salary"] = lo

        # 元格式: tokens 含 "元/天" 或 "元/月"
        elif any("元/" in t for t in salary_tokens):
            unit = next((t for t in salary_tokens if "元/" in t), "")
            nums = [t for t in salary_tokens if re.match(r'^\d+$', t)]
            if delim_idx >= 0:
                lo_nums = [t for t in lo_tokens if re.match(r'^\d+$', t)]
                hi_nums = [t for t in hi_tokens if re.match(r'^\d+$', t)]
                lo = "".join(lo_nums)
                hi = "".join(hi_nums)
            else:
                # 无分隔符: 前两个数字作为范围
                if len(nums) >= 2:
                    lo, hi = nums[0], nums[1]
                else:
                    lo, hi = nums[0] if nums else "", ""
            if lo and hi:
                result["salary"] = f"{lo}-{hi}{unit}"
            elif lo:
                result["salary"] = f"{lo}{unit}"

    # ★ 职位描述: JS 直读 iframe 内 textarea（AX 树会截断 iframe 内容）
    # 注意: cua() 对非 JSON 返回值截断 200 字 → JS 必须返回 JSON 字符串
    r = cua("page", json.dumps({
        "pid": pid, "window_id": wid,
        "action": "execute_javascript",
        "javascript": """
        (function(){
            var text = "";
            for (var i = 0; i < document.querySelectorAll("iframe").length; i++) {
                try {
                    var doc = document.querySelectorAll("iframe")[i].contentDocument;
                    var tas = doc.querySelectorAll("textarea, [contenteditable=true]");
                    for (var j = 0; j < tas.length; j++) {
                        var val = (tas[j].value || tas[j].textContent || "").trim();
                        if (val.length > 5) text = val;
                    }
                } catch(e) {}
            }
            return JSON.stringify({text: text});
        })()
        """,
    }))
    # cua() 已 parse JSON → r 是 dict，直接取 text
    js_text = r.get("text", "") if isinstance(r, dict) else ""
    if js_text and len(js_text) > 5:
        result["requirements"] = js_text.strip()

    # 薪资兜底: 从 requirements 文本中匹配
    if not result["salary"] and result["requirements"]:
        reqs = result["requirements"]
        # 匹配 "16K-30K" / "12K" 格式
        m = re.search(r'(\d{1,3}[Kk]\s*[-–—~]\s*\d{1,3}[Kk])', reqs)
        if m:
            result["salary"] = m.group(1).upper().replace(" ", "")
        else:
            m = re.search(r'(\d{1,3}[Kk])', reqs)
            if m:
                result["salary"] = m.group(1).upper()
        # 匹配 "1000-9000元/月" / "120-180元/天"
        if not result["salary"]:
            m = re.search(r'(\d+[-–—~]\d+元/[天月])', reqs)
            if m: result["salary"] = m.group(1)

    # 学历兜底
    if not result["degree"] and result["requirements"]:
        for d in ["博士", "硕士", "本科", "大专"]:
            if d in result["requirements"]: result["degree"] = d; break

    return result


def gen_id(title):
    for cn, en in ID_MAP:
        if cn in title.lower() or cn in title: return en
    return re.sub(r'[^a-z0-9]+', '-', title.lower())[:30]


def dedup(jobs):
    seen, out = {}, []
    for j in jobs:
        jid = j["id"]
        key = (j.get("title",""), j.get("salary",""))
        if jid not in seen:
            seen[jid] = [key]; out.append(j)
        else:
            if key in seen[jid]: continue
            j["id"] = f"{jid}-{len(seen[jid])+1}"
            seen[jid].append(key); out.append(j)
    return out


def load_existing_templates():
    """只从旧 jobs.json 中提取话术模板（按 title+salary 索引）"""
    tpls = {}
    if CONFIG.exists():
        try:
            old = json.loads(CONFIG.read_text(encoding="utf-8"))
            for j in old.get("jobs", []):
                key = (j.get("title",""), j.get("salary",""))
                if j.get("templates"):
                    tpls[key] = j["templates"]
            return tpls, old.get("fallback_templates", [])
        except: pass
    return {}, []


def load_job_template() -> dict:
    """从 jobs-template.json 读取手动维护的元数据（id→title 映射）"""
    if not TEMPLATE_PATH.exists():
        return {}
    try:
        data = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        mapping = {}
        for j in data.get("jobs", []):
            jid = j.get("id", "")
            if jid:
                mapping[jid] = {
                    "template_title": j.get("title", ""),
                }
        return mapping
    except (json.JSONDecodeError, KeyError):
        return {}


def merge_template_metadata(jobs: list[dict]) -> list[dict]:
    """将 jobs-template.json 中的 id 覆盖合并到提取的岗位数据

    策略: 模板 title 精确匹配 → 模板 id 权威覆盖。
    模糊匹配不覆盖 id（避免不同岗位被错误合并到同一 id）。
    """
    template = load_job_template()
    if not template:
        return jobs

    for job in jobs:
        jid = job.get("id", "")
        title = job.get("title", "")

        # 模板 title 精确匹配 → 模板 id 权威覆盖
        for tid, tmeta in template.items():
            ttitle = tmeta.get("template_title", "")
            if ttitle == title:
                job["id"] = tid
                break

    # 去重检查: 检测是否有两个不同 title 的岗位共享同一 id
    id_titles = {}
    for job in jobs:
        jid = job.get("id", "")
        title = job.get("title", "")
        if jid not in id_titles:
            id_titles[jid] = []
        id_titles[jid].append(title)
    for jid, titles in id_titles.items():
        if len(titles) > 1:
            # 碰撞: 给后面的岗位追加后缀
            for job in jobs:
                if job.get("id") == jid and job.get("title") != titles[0]:
                    suffix = 2
                    new_id = f"{jid}-{suffix}"
                    while any(j.get("id") == new_id for j in jobs):
                        suffix += 1
                        new_id = f"{jid}-{suffix}"
                    job["id"] = new_id

    return jobs


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="仅预览不写入")
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args()

    print("=" * 60)
    print("BOSS岗位提取 — 仅开放中 + 去重 + 覆盖")
    print("=" * 60)

    if "running" not in str(cua("status")).lower():
        subprocess.run(["cua-driver", "serve"], capture_output=True); time.sleep(1)
    cua("start_session", json.dumps({"session": SESSION}))

    pid, wid = find_window()
    print(f"✓ pid={pid} wid={wid}")

    # ① 职位管理列表
    print("\n① 进入职位管理...")
    if not nav_to(JOB_LIST, pid, wid, has_edit_links, timeout=30):
        print("❌ 未渲染。请刷新 Chrome 中 BOSS 页面后重试。")
        sys.exit(1)

    # ② 扫描开放中岗位
    print("\n② 扫描开放中岗位...")
    open_jobs = scan_open_jobs(pid, wid)

    # 也统计关闭的
    tree = ax_tree(pid, wid)
    closed_count = len(re.findall(r'AXStaticText\s*=\s*"关闭"', tree))

    if not open_jobs:
        # 兜底: 如果状态检测失败，退化为取所有编辑
        all_edits = []
        for line in tree.split("\n"):
            m = re.search(r'\[(\d+)\].*AXLink\s*\(\s*编辑\s*\)', line)
            if m: all_edits.append(int(m.group(1)))
        print(f"  ⚠ 状态检测失败，取全部 {len(all_edits)} 个编辑按钮")
        # 取唯一的，用岗位名去重
        # 简化: 直接取前N个编辑（跳过重复）
        items = []
        for line in tree.split("\n"):
            m = re.search(r'\[(\d+)\].*AXLink\s*\(\s*((?!编辑|关闭|沟通|职位管理|推荐牛人|搜索|意向沟通|互动|牛人管理|道具|工具箱|更多|直聘企业版|招聘规范|投递保|直播招聘|道具 首充礼)[^)]+)\s*\)', line)
            if m: items.append(m.group(2).strip())
    else:
        print(f"  开放中: {len(open_jobs)} 个 | 关闭: ~{closed_count} 个")

    # 如果有 open_jobs，用 open_jobs；否则从 all_edits 构建
    if open_jobs:
        total = len(open_jobs) if not args.limit else min(len(open_jobs), args.limit)
        targets = open_jobs[:total]
    else:
        print("  ❌ 无开放中岗位"); sys.exit(1)

    for j in targets[:5]:
        print(f"    [{j['edit_index']:>4}] {j['title']}")
    if len(targets) > 5:
        print(f"    ... 还有 {len(targets)-5} 个")

    # ③ 逐岗提取 — 每次返回后用标题匹配找编辑按钮
    print(f"\n③ 提取 ({total} 个)...")
    extracted, seen_keys = [], set()
    remaining = list(targets)  # 待处理的岗位列表

    for i in range(total):
        if not remaining: break

        # 重新扫描，按标题匹配下一个待处理岗位
        fresh_jobs = scan_open_jobs(pid, wid)
        # 找第一个在 remaining 中的
        next_job = None
        for fj in fresh_jobs:
            if fj["title"] in {r["title"] for r in remaining}:
                next_job = fj; break

        if not next_job:
            print(f"  ⚠ 未找到下一个待处理岗位"); break

        title = next_job["title"]
        edit_idx = next_job["edit_index"]
        remaining = [r for r in remaining if r["title"] != title]

        print(f"\n  [{i+1}/{total}] {title} (idx={edit_idx})")

        # 点击
        r = cua("click", json.dumps({
            "pid": pid, "window_id": wid, "element_index": edit_idx
        }))
        if r.get("error"): print(f"    ❌ 点击失败"); continue

        # 等编辑页
        for _ in range(12):
            time.sleep(1)
            if has_textarea(pid, wid): break
        time.sleep(2)

        detail = extract_edit_page(pid, wid)
        # 以列表页 title 为准（编辑页表单字段提取不准）
        detail["title"] = title
        detail["id"] = gen_id(title)

        key = (title, detail["salary"])
        if key in seen_keys:
            print(f"    ⏭ {title} 重复，跳过"); continue

        seen_keys.add(key)
        extracted.append(detail)
        print(f"    ✓ {title} | {detail['salary'] or '?'} | {detail['degree'] or '?'}")
        reqs = (detail.get('requirements') or '')[:80]
        if reqs: print(f"      要求: {reqs}")

        # 返回列表
        if remaining:
            delay = 3 + random.random() * 4
            print(f"    ← 休息 {delay:.0f}s")
            time.sleep(delay)
            if not nav_to(JOB_LIST, pid, wid, has_edit_links, timeout=25):
                print("    ⚠ 返回列表失败，跳过剩余岗位")
                break

    if not extracted:
        print("\n❌ 未提取到任何岗位"); sys.exit(1)

    # ④ 去重 + 合并模板元数据 + 合并话术
    print(f"\n④ 处理...")
    extracted = dedup(extracted)
    extracted = merge_template_metadata(extracted)
    old_templates, fallback = load_existing_templates()

    for j in extracted:
        key = (j.get("title",""), j.get("salary",""))
        if key in old_templates:
            j["templates"] = old_templates[key]

    jobs_config = {
        "version": 3,
        "description": "BOSS直聘岗位配置 — 从职位管理页自动同步",
        "jobs": extracted,
        "fallback_templates": fallback,
    }

    print(f"\n  {len(extracted)} 个岗位:")
    for j in extracted:
        salary = j.get("salary") or "?"
        degree = j.get("degree") or "?"
        exp = j.get("experience") or ""
        exp_str = f" | {exp}" if exp else ""
        reqs = (j.get("requirements", "") or "")[:80]
        print(f"  ✅ {j['title']:35s} | {salary} | {degree}{exp_str}")
        if reqs: print(f"      {reqs}")

    if args.dry_run:
        print(f"\n⚠ 预览 — 未写入")
    else:
        CONFIG.write_text(json.dumps(jobs_config, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✓ 已覆盖写入 {CONFIG}")


if __name__ == "__main__":
    main()

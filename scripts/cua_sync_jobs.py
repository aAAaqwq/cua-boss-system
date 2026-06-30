#!/usr/bin/env python3
"""
从 BOSS直聘职位管理页提取岗位详情 → 覆盖写入 config/jobs.json

提取规则:
  1. 只提取状态为"开放中"的岗位，跳过"关闭"的
  2. 列表页同名岗位只取第一个（去重）
  3. 逐个点击"编辑"进入详情 → 提取 title/requirements/salary/degree/location
  4. 覆盖写入 jobs.json（替换旧数据，保留话术模板）

用法:
  python scripts/cua_sync_jobs.py              # 提取 + 覆盖写入 config/jobs.json（默认）
  python scripts/cua_sync_jobs.py --write      # 同上（--write 可省略，显式更清晰）
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

NAV_LINKS = {
    "职位管理","推荐牛人","搜索","沟通","意向沟通","互动","牛人管理",
    "道具","工具箱","更多","直聘企业版","招聘规范","","投递保",
    "关闭","编辑","1","2","直播招聘","道具 首充礼",
}

def cua(*args, _retries=2):
    """调用 cua-driver；超时 / 瞬时失败自动重试，提升跨机稳定性(不再因单次超时崩溃)。"""
    cmd = ["cua-driver", "call"] + list(args)
    for attempt in range(_retries + 1):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            if attempt < _retries:
                time.sleep(1.0); continue
            return {}
        except FileNotFoundError:
            print("❌ 未找到 cua-driver，请确认已安装且在 PATH"); sys.exit(1)
        if r.returncode != 0:
            if attempt < _retries:
                time.sleep(0.8); continue
            return {}
        try:
            return json.loads(r.stdout.strip() or "{}")
        except json.JSONDecodeError:
            return {"text": (r.stdout or "")[:200]}
    return {}


def ax_tree(pid, wid):
    return cua("get_window_state", json.dumps({
        "pid": pid, "window_id": wid, "capture_mode": "ax"
    })).get("tree_markdown", "")


def stable_ax_tree(pid, wid, want=None, tries=4):
    """抓 AX 树并等它「就绪/稳定」——缓解渲染时序导致的跨机识别不稳。

    want(tree)->bool：满足即返回(如「含编辑按钮」)；否则渐进退避重试，
    最终回退到最后一次非空结果(交由上层容错)。
    """
    last = ""
    for i in range(tries):
        t = ax_tree(pid, wid)
        if t and len(t) >= 200:
            if want is None or want(t):
                return t
            last = t
        time.sleep(0.6 + 0.4 * i)
    return last


# ── AX 序列化格式容错 ──
# 不同 cua-driver / Chrome / macOS 版本对 AX 节点的写法不一：
#   AXLink (文本)  |  AXLink "文本"  |  AXLink = "文本"  ；StaticText 同理。
# 统一用下面两个 helper 抽取，避免「换台机器就识别不到」。
def _ax_role_text(line, roles):
    rolepat = "|".join(roles)
    # 引号形式: AXLink "x" / AXLink = "x"
    m = re.search(rf'AX(?:{rolepat})\b\s*(?:=\s*)?"([^"]+)"', line)
    if m:
        return m.group(1).strip()
    # 括号形式: AXLink (x) [actions...]（锚定 [ 或行尾，标题含括号也不截断）
    m = re.search(rf'AX(?:{rolepat})\b\s*\(\s*(.+?)\s*\)\s*(?:\[|$)', line)
    if m:
        return m.group(1).strip()
    return None


def _ax_link_text(line):
    return _ax_role_text(line, ("Link", "Button", "MenuItem"))


def _ax_static_text(line):
    return _ax_role_text(line, ("StaticText",))


_EDIT_LABELS = {"编辑", "编辑职位", "编辑岗位"}
_STATUS_OPEN = "开放中"
_STATUS_LABELS = {"开放中", "关闭", "待开放"}


# ══════════════════════════════════════════════════
# 兜底：AX 识别失败时走 DOM(JS) 定位 + CGEvent 真鼠标点击(isTrusted=true，防检测)
# 坐标换算与 boss_click_buheshi 同(实测正确)；CGEvent 像素点击需 Chrome 前台。
# ══════════════════════════════════════════════════

def _activate_chrome_front():
    """把 Chrome 带到前台——CGEvent 像素点击要求目标窗口可见/前台，否则点击落空。"""
    try:
        subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to activate'],
                       capture_output=True, timeout=5)
    except Exception:
        pass
    time.sleep(0.3)


def _run_js(pid, wid, js):
    """执行 JS（须 return JSON.stringify(...)）→ 解析为 dict；失败返回 None。"""
    r = cua("page", json.dumps({
        "pid": pid, "window_id": wid, "action": "execute_javascript", "javascript": js,
    }))
    if isinstance(r, str):
        try:
            return json.loads(r)
        except json.JSONDecodeError:
            return None
    if isinstance(r, dict):
        if "ok" in r:
            return r
        if "text" in r:                       # cua() 解码失败兜底包了一层
            try:
                return json.loads(r["text"])
            except Exception:
                return None
    return None


def _screenshot_dims(pid, wid):
    st = cua("get_window_state", json.dumps({
        "pid": pid, "window_id": wid, "capture_mode": "screenshot",
    }))
    return st.get("screenshot_width"), st.get("screenshot_height")


def _cgclick_rect(rect, pid, wid):
    """CSS rect → 截图像素 → cua-driver CGEvent 真鼠标点击 {x,y}（isTrusted=true）。

    scale = 截图宽 / 视口宽；顶部浏览器 chrome = 截图高 − 视口高×scale。需 Chrome 前台。
    """
    sw, sh = _screenshot_dims(pid, wid)
    if not sw or not sh or not rect.get("iw"):
        return False
    scale = sw / rect["iw"]
    chrome_top = sh - rect["ih"] * scale
    cgx = int((rect["x"] + rect["w"] / 2) * scale)
    cgy = int((rect["y"] + rect["h"] / 2) * scale + chrome_top)
    r = cua("click", json.dumps({"pid": pid, "window_id": wid, "x": cgx, "y": cgy}))
    return not (isinstance(r, dict) and r.get("error"))


# ── DOM(JS) 扫描：完全不依赖 AX 树 ──
# BOSS 职位列表在 iframe 内 → 必须遍历同源 iframe 文档；坐标要加 iframe 偏移转成顶层视口坐标。
_JS_DOCS = (
    "function _docs(){var d=[document];var f=document.querySelectorAll('iframe');"
    "for(var i=0;i<f.length;i++){try{if(f[i].contentDocument)d.push(f[i].contentDocument);}"
    "catch(e){}}return d;}"
    "function _off(D){if(D===document)return{x:0,y:0};var f=document.querySelectorAll('iframe');"
    "for(var i=0;i<f.length;i++){try{if(f[i].contentDocument===D){var r=f[i].getBoundingClientRect();"
    "return{x:r.left,y:r.top};}}catch(e){}}return{x:0,y:0};}"
)

_JS_DOM_SCAN = (
    "(function(){" + _JS_DOCS + "var out=[],seen={};"
    "_docs().forEach(function(D){"
    "var btns=[].slice.call(D.querySelectorAll('a,button,span,div'))"
    ".filter(function(e){var r=e.getBoundingClientRect();"
    "return (e.textContent||'').trim()==='编辑'&&r.width>0&&r.height>0;});"
    "btns.forEach(function(ed){var card=ed,title='',open=null;"
    "for(var k=0;k<8&&card;k++){card=card.parentElement;if(!card)break;"
    "var cands=[].slice.call(card.querySelectorAll('a,h1,h2,h3,span'))"
    ".map(function(x){return (x.textContent||'').trim();})"
    ".filter(function(t){return t.length>=2&&t.length<=40&&/[\\u4e00-\\u9fa5]/.test(t)"
    "&&!{'编辑':1,'打开':1,'关闭':1,'下线':1,'上线':1,'置顶':1,'刷新':1,'推广':1,"
    "'删除':1,'复制':1,'立即沟通':1,'沟通':1,'开放中':1,'待开放':1,'已关闭':1}[t]"
    "&&t.indexOf('开放中')<0&&t.indexOf('关闭')<0&&t.indexOf('待开放')<0"
    "&&t.indexOf('沟通')!==0;});"
    "if(cands.length){title=cands[0];var ct=card.textContent||'';"
    "open=(ct.indexOf('开放中')>=0)?true:((ct.indexOf('关闭')>=0)?false:null);break;}}"
    "if(title&&!seen[title]){seen[title]=1;out.push({title:title,open:open});}});});"
    "return JSON.stringify({ok:true,jobs:out});})()"
)

_JS_EDIT_RECT = (
    "(function(){var T='__TITLE__';" + _JS_DOCS + "var DD=_docs();"
    "for(var di=0;di<DD.length;di++){var D=DD[di];"
    "var all=[].slice.call(D.querySelectorAll('a,span,div,h1,h2,h3')),te=null;"
    "for(var i=0;i<all.length;i++){if((all[i].textContent||'').trim()===T){te=all[i];break;}}"
    "if(!te)continue;var card=te;"
    "for(var k=0;k<8&&card;k++){"
    "var b=[].slice.call(card.querySelectorAll('a,button,span,div'))"
    ".filter(function(e){return (e.textContent||'').trim()==='编辑';});"
    "if(b.length){var r=b[0].getBoundingClientRect(),o=_off(D);"
    "if(r.width>0&&r.height>0)return JSON.stringify({ok:true,x:r.left+o.x,y:r.top+o.y,"
    "w:r.width,h:r.height,iw:window.innerWidth,ih:window.innerHeight});}"
    "card=card.parentElement;}}return JSON.stringify({ok:false});})()"
)

_JS_HAS_EDIT = (
    "(function(){" + _JS_DOCS + "var n=0;_docs().forEach(function(D){"
    "n+=[].slice.call(D.querySelectorAll('a,button,span,div')).filter(function(e){"
    "return (e.textContent||'').trim()==='编辑';}).length;});"
    "return JSON.stringify({ok:true,n:n});})()"
)

_JS_HAS_TEXTAREA = (
    "(function(){" + _JS_DOCS + "var n=0;_docs().forEach(function(D){"
    "try{n+=D.querySelectorAll('textarea').length;}catch(e){}});"
    "return JSON.stringify({ok:true,n:n});})()"
)


def dom_scan_edit_buttons(pid, wid):
    """JS DOM 扫描可编辑岗位(AX 无关) → [{title}]（去重、按 DOM 顺序）。

    优先返回检测到「开放中」的岗位；若 DOM 里状态完全测不出(全 unknown)，
    则返回所有可编辑岗位(宁多不漏，绝不因状态测不出而同步到空)。
    """
    r = _run_js(pid, wid, _JS_DOM_SCAN)
    if not (isinstance(r, dict) and r.get("ok")):
        return []
    jobs = [j for j in r.get("jobs", []) if j.get("title")]
    open_jobs = [j for j in jobs if j.get("open") is True]
    return open_jobs if open_jobs else jobs


def cgclick_edit_by_title(title, pid, wid):
    """按岗位名在 DOM 里实时定位其「编辑」按钮 → CGEvent 真鼠标点击(防检测)。"""
    safe = title.replace("\\", "\\\\").replace("'", "\\'")
    rect = _run_js(pid, wid, _JS_EDIT_RECT.replace("__TITLE__", safe))
    if not (isinstance(rect, dict) and rect.get("ok")):
        return False
    _activate_chrome_front()
    return _cgclick_rect(rect, pid, wid)


def _dom_has_edit(pid, wid):
    r = _run_js(pid, wid, _JS_HAS_EDIT)
    return bool(r and r.get("ok") and r.get("n", 0) >= 1)


def _dom_has_textarea(pid, wid):
    r = _run_js(pid, wid, _JS_HAS_TEXTAREA)
    return bool(r and r.get("ok") and r.get("n", 0) >= 1)


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
    tree = ax_tree(pid, wid)
    if any((_ax_link_text(ln) or "") in _EDIT_LABELS for ln in tree.split("\n")):
        return True
    return _dom_has_edit(pid, wid)            # AX 没有 → JS DOM 兜底确认


def has_textarea(pid, wid):
    if 'AXTextArea' in ax_tree(pid, wid):
        return True
    return _dom_has_textarea(pid, wid)        # AX 没有 → JS DOM 兜底确认


def _is_job_title(val):
    """判断 AXLink 文本是否是岗位名"""
    if not val or val in NAV_LINKS: return False
    if val in _EDIT_LABELS or val in ('关闭', '打开', '下线', '上线'): return False
    if not re.search(r'[一-鿿]', val): return False           # 必须含中文
    if re.match(r'^沟通\s*\d*$', val): return False
    if val in _STATUS_LABELS: return False
    return len(val) >= 2


def _parse_open_jobs(tree):
    """从 AX 树文本解析「开放中」岗位 → [{title, edit_index}]（格式容错）。

    每张卡片真实结构（岗位名在编辑前面!）:
      [80] AXLink(开发) [86] 16-30K ... [94] 开放中 [96] AXLink(编辑)
    用 _ax_link_text/_ax_static_text 兼容 括号/引号/等号 三种序列化写法。
    """
    items = []
    for line in tree.split("\n"):
        m_idx = re.search(r'\[(\d+)\]', line)
        if not m_idx: continue
        idx = int(m_idx.group(1))
        st = _ax_static_text(line)
        if st is not None:
            items.append((idx, 'text', st)); continue
        lk = _ax_link_text(line)
        if lk is not None:
            items.append((idx, 'link', lk))
    items.sort()

    # 状态机: current_title → current_status → 编辑
    jobs, seen_titles = [], set()
    current_title = current_status = None
    for idx, typ, val in items:
        if typ == 'link' and _is_job_title(val):
            current_title = val
            current_status = None
            continue
        if typ == 'text' and val in _STATUS_LABELS:
            current_status = val
            continue
        if typ == 'link' and val in _EDIT_LABELS:      # 编辑按钮 → 配对
            if current_title and current_status == _STATUS_OPEN and current_title not in seen_titles:
                seen_titles.add(current_title)
                jobs.append({"title": current_title, "edit_index": idx})
            current_title = current_status = None
            continue
        if typ == 'link' and val in ('关闭', '打开', '下线', '上线'):
            continue
    return jobs


def scan_open_jobs(pid, wid):
    """扫描列表页开放中岗位；抓不到就重试(渲染时序/跨机 AX 不稳的容错)。"""
    jobs = []
    for attempt in range(2):
        tree = stable_ax_tree(pid, wid, want=lambda t: any(
            (_ax_link_text(ln) or "") in _EDIT_LABELS for ln in t.split("\n")))
        jobs = _parse_open_jobs(tree)
        if jobs:
            return jobs
        time.sleep(0.8)
    return jobs  # 仍为空 → 交由上层兜底(取全部编辑按钮)


def parse_editable_jobs(tree):
    """容错兜底：状态检测失败时，把每个「编辑」按钮与其【前面最近的岗位名链接】配对，
    不看开放中/关闭状态 → 至少能同步到所有可编辑岗位(宁可多同步，不漏)。"""
    jobs, seen = [], set()
    last_title = None
    items = []
    for line in tree.split("\n"):
        m_idx = re.search(r'\[(\d+)\]', line)
        if not m_idx: continue
        idx = int(m_idx.group(1))
        lk = _ax_link_text(line)
        if lk is not None:
            items.append((idx, lk))
    items.sort()
    for idx, val in items:
        if _is_job_title(val):
            last_title = val
        elif val in _EDIT_LABELS and last_title and last_title not in seen:
            seen.add(last_title)
            jobs.append({"title": last_title, "edit_index": idx})
            last_title = None
    return jobs


_DOM_MODE = False  # 一旦 AX 失效降级 DOM，后续直接走 DOM（不再每轮重试慢 AX，省时）


def scan_jobs(pid, wid):
    """统一扫描：AX 优先，AX 失败自动降级到 DOM(JS)，且降级后保持 DOM 模式。

    返回 [{title, edit_index?}]：
      - AX 路径项带 edit_index（用 AX element_index 点击）；
      - DOM 兜底项不带 edit_index（点击时走 cgclick_edit_by_title 真鼠标兜底）。
    """
    global _DOM_MODE
    if not _DOM_MODE:
        jobs = scan_open_jobs(pid, wid)                   # ① AX：仅开放中
        if jobs:
            return jobs
        jobs = parse_editable_jobs(stable_ax_tree(pid, wid))  # ② AX：不分状态(状态测失败兜底)
        if jobs:
            print("  ⚠ 状态识别失败，AX 回退：按编辑按钮取岗位(不分状态)")
            return jobs
    dom = dom_scan_edit_buttons(pid, wid)                 # ③ DOM(JS)：AX 失效兜底
    if dom and not _DOM_MODE:
        print(f"  ⚠ AX 树识别失败，降级 DOM+真鼠标点击：取 {len(dom)} 个岗位（后续保持 DOM 模式）")
        _DOM_MODE = True
    return dom


def click_edit_button(job, pid, wid):
    """点击某岗位的「编辑」：优先 AX element_index；失败/无 index → DOM+CGEvent 真鼠标兜底。"""
    idx = job.get("edit_index")
    if idx is not None:
        r = cua("click", json.dumps({"pid": pid, "window_id": wid, "element_index": idx}))
        if not (isinstance(r, dict) and r.get("error")):
            return True
        print("    ↩ AX 点击失败，改用真鼠标(CGEvent)兜底...")
    return cgclick_edit_by_title(job["title"], pid, wid)


def extract_edit_page(pid, wid):
    """从编辑页提取表单字段（AX 树 + JS iframe 双通道）

    AX 树在 iframe 内容上会截断 → 职位描述用 JS 直接读 textarea.value
    """
    tree = stable_ax_tree(pid, wid, want=lambda t: ("薪资范围" in t or "AXTextArea" in t))
    result = {
        "title": "", "requirements": "", "salary": "",
        "degree": "", "location": "", "experience": "", "boss_id": "",
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
        m = re.search(r'AXTextField\s*(?:=\s*)?"([^"]+)"', line)
        if m and m.group(1) and "zhipin.com" not in m.group(1) and "/" not in m.group(1):
            val = m.group(1)
            if 2 <= len(val) < 30: text_fields.append(val)
            if re.search(r'[区路街大厦座层号\d]', val) and len(val) > 5:
                result["location"] = val

        # 学历 — AXStaticText 精确匹配
        m = re.search(r'AXStaticText\s*(?:=\s*)?"(博士|硕士|本科|大专)"', line)
        if m and not result["degree"]:
            result["degree"] = m.group(1)

        # 经验
        m = re.search(r'AXStaticText\s*(?:=\s*)?"([^"]*[年应届].*)"', line)
        if m and not result["experience"]:
            val = m.group(1)
            if val not in ("1", "2", "职位管理", "招聘规范", "推荐牛人"):
                result["experience"] = val

        # 薪资区域: 只取第一个 "薪资范围" 块（页面有重复的预览副本）
        if not in_salary_section and not salary_done and re.search(r'AXStaticText\s*(?:=\s*)?"薪资范围"', line):
            in_salary_section = True
            continue
        if in_salary_section:
            # 遇到边界标志则结束第一个薪资块
            m_bound = re.search(r'AXStaticText\s*(?:=\s*)?"([^"]+)"', line)
            if m_bound and m_bound.group(1) in SALARY_BOUNDARY:
                in_salary_section = False
                salary_done = True
                continue
            # 只收集薪资相关的 AXStaticText
            m_st = re.search(r'AXStaticText\s*(?:=\s*)?"([^"]+)"', line)
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

    # ★ 尽力探测 BOSS 真实 jobId（编辑页 URL/DOM 携带当前岗位 id）
    # 探测到则存入 boss_id 备用；探测不到不影响主流程（id 仍用岗位名）
    rj = cua("page", json.dumps({
        "pid": pid, "window_id": wid,
        "action": "execute_javascript",
        "javascript": """
        (function(){
            var jid = "";
            try {
                var m = (location.href || "").match(/(?:encryptJobId|jobId|jobid)=([^&#]+)/i);
                if (m) jid = m[1];
                if (!jid) {
                    var el = document.querySelector(
                        '[data-jobid],[data-job-id],[data-encryptjobid],[data-encrypt-job-id]');
                    if (el) jid = el.getAttribute('data-jobid') || el.getAttribute('data-job-id')
                                || el.getAttribute('data-encryptjobid')
                                || el.getAttribute('data-encrypt-job-id') || "";
                }
            } catch(e) {}
            return JSON.stringify({jobId: jid});
        })()
        """,
    }))
    boss_id = rj.get("jobId", "") if isinstance(rj, dict) else ""
    if boss_id and isinstance(boss_id, str):
        result["boss_id"] = boss_id.strip()

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


# 开头的装饰性标签(BOSS/HR 列表常加: (标注)/（急聘）/【双休】/[包住] 等)。
# 仅剥短标签(≤8字)，避免误伤把括号当正名一部分的岗位。支持中英文括号。
_TAG_PREFIX_RE = re.compile(r"^\s*[\(（【\[][^\)）】\]]{1,8}[\)）】\]]\s*")


def norm_title(title):
    """规范化岗位名（折叠空白 + 剥离开头装饰性标签）——岗位名即唯一键，无独立 id 字段。

    不再生成英文 id：BOSS 真实 jobId 是加密哈希(不可读、不适合做话术/评分 key)，
    而岗位名本身唯一、可读，且与候选人聊天的 job_position 一致 →
    用岗位名让 chat↔job↔reply-templates↔scoring 四处天然对齐，无需任何映射。
    (BOSS 真实 jobId 仍由 extract_edit_page 尽力探测并存入 boss_id 字段备用。)

    兼容性：列表页岗位名常带 (标注)/（急聘）等列表侧标签，而候选人侧 job_position、
    话术/评分的 key 都不带 → 这里剥掉开头短标签，让四处 key 对齐(否则精确键查找落空)。
    """
    t = re.sub(r"\s+", " ", (title or "").strip())
    while True:
        stripped = _TAG_PREFIX_RE.sub("", t).strip()
        if stripped == t or not stripped:  # 无变化 / 不会剥成空 → 停
            break
        t = stripped
    return t


def dedup(jobs):
    """按岗位名(唯一键)去重；同名不同薪资极少见，追加后缀保证键唯一"""
    seen, out = {}, []
    for j in jobs:
        title = j.get("title", "")
        key = (title, j.get("salary", ""))
        if title not in seen:
            seen[title] = [key]; out.append(j)
        else:
            if key in seen[title]: continue
            j["title"] = f"{title}-{len(seen[title])+1}"
            seen[title].append(key); out.append(j)
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


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--write", action="store_true",
                   help="显式写入（默认即提取+覆盖写入 config/jobs.json，此 flag 可省略）")
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

    # ② 扫描岗位（AX 优先；AX 失败自动降级 DOM+真鼠标点击）
    print("\n② 扫描开放中岗位...")
    open_jobs = scan_jobs(pid, wid)

    tree = stable_ax_tree(pid, wid)
    closed_count = len(re.findall(r'AXStaticText\s*(?:=\s*)?"关闭"', tree))

    if not open_jobs:
        print("  ❌ 未识别到任何可编辑岗位。请确认：Chrome 已在「职位管理」页并渲染完成 / "
              "已授辅助功能权限 / 窗口在前台可见，然后刷新重试。"); sys.exit(1)
    print(f"  识别到 {len(open_jobs)} 个岗位 | 关闭: ~{closed_count} 个")

    total = len(open_jobs) if not args.limit else min(len(open_jobs), args.limit)
    targets = open_jobs[:total]

    for j in targets[:5]:
        tag = j.get('edit_index')
        tag = str(tag) if tag is not None else 'DOM'
        print(f"    [{tag:>4}] {j['title']}")
    if len(targets) > 5:
        print(f"    ... 还有 {len(targets)-5} 个")

    # ③ 逐岗提取 — 每次返回后用标题匹配找编辑按钮
    print(f"\n③ 提取 ({total} 个)...")
    extracted, seen_keys = [], set()
    remaining = list(targets)  # 待处理的岗位列表

    for i in range(total):
        if not remaining: break

        # 重新扫描（AX→DOM 自动降级），按标题匹配下一个待处理岗位
        fresh_jobs = scan_jobs(pid, wid)
        # 找第一个在 remaining 中的
        next_job = None
        for fj in fresh_jobs:
            if fj["title"] in {r["title"] for r in remaining}:
                next_job = fj; break

        if not next_job:
            print(f"  ⚠ 未找到下一个待处理岗位"); break

        title = next_job["title"]
        edit_idx = next_job.get("edit_index")
        remaining = [r for r in remaining if r["title"] != title]

        idx_tag = edit_idx if edit_idx is not None else "DOM"
        print(f"\n  [{i+1}/{total}] {title} (idx={idx_tag})")

        # 点击：AX element_index 优先，失败/无 index → DOM+CGEvent 真鼠标兜底
        if not click_edit_button(next_job, pid, wid):
            print(f"    ❌ 点击失败"); continue

        # 等编辑页
        for _ in range(12):
            time.sleep(1)
            if has_textarea(pid, wid): break
        time.sleep(2)

        detail = extract_edit_page(pid, wid)
        # 以列表页 title 为准（编辑页表单字段提取不准）；岗位名即唯一键，无独立 id
        detail["title"] = norm_title(title)

        key = (title, detail["salary"])
        if key in seen_keys:
            print(f"    ⏭ {title} 重复，跳过"); continue

        seen_keys.add(key)
        extracted.append(detail)
        print(f"    ✓ {title} | {detail['salary'] or '?'} | {detail['degree'] or '?'}")
        if detail.get("boss_id"):
            print(f"      BOSS jobId: {detail['boss_id']}")
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

    # ④ 去重 + 合并话术
    print(f"\n④ 处理...")
    extracted = dedup(extracted)
    # 清理空 boss_id（探测不到则不写入配置，保持干净）
    for j in extracted:
        if not j.get("boss_id"):
            j.pop("boss_id", None)
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
        print(f"\n⚠ 预览 — 未写入（去掉 --dry-run 即覆盖写入 config/jobs.json）")
    else:
        CONFIG.write_text(json.dumps(jobs_config, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✓ 已覆盖写入 {CONFIG}")


if __name__ == "__main__":
    main()

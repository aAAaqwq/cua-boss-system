#!/usr/bin/env python3
"""
沟通页批量收集候选人 — 简历 & 微信 → SQLite + 附件下载到本地

流程:
  ① 进入聊天页 → 滚动加载
  ② AX树扫描所有联系人
  ③ 逐个审查:
      学校不在白名单/学历不达标 → 点"不合适"
      符合条件 → 点"附件简历"(BOSS自动处理3种情况) → 提取 → 下载到本地 → 换微信
  ④ 直接点侧边栏下一个，不刷新页面

用法:
  python scripts/cua_collect.py --dry-run           # 预览
  python scripts/cua_collect.py --limit 10           # 前10个
  python scripts/cua_collect.py --min-degree 硕士    # 学历筛选
"""
import base64, json, sqlite3, subprocess, sys, time, re, random, os, urllib.request, shutil
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.filter_criteria import ALL_ELITE_SCHOOLS, DEFAULT_MIN_DEGREE, match_school, find_school
from app.chat_reply import check_degree
from app.db import init_db, DB_PATH
from app.pdf_util import extract_resume_from_pdf, extract_contacts
from scripts.boss_click_buheshi import click_buheshi, _activate_chrome_front

SESSION = "boss-collect"
CHROME = "com.google.Chrome"
CHAT = "https://www.zhipin.com/web/chat/index"
RESUMES_DIR = Path(__file__).parent.parent / "data" / "resumes"  # 附件简历本地存储
RESUMES_DIR.mkdir(parents=True, exist_ok=True)


# cua-driver 文本响应中的错误特征
_CUA_ERROR_PATTERNS = [
    "not found in cache", "No cached", "Call get_window_state first",
    "failed", "error", "Error", "invalid", "Invalid",
    "could not", "Could not", "unable", "Unable",
    "denied", "permission", "timeout", "not found",
]


def cua(*args):
    """调用 cua-driver CLI。返回 dict，失败时带 "error" 键。"""
    cmd = ["cua-driver", "call"] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    stdout = r.stdout.strip()
    stderr = r.stderr.strip()

    if r.returncode != 0:
        err_msg = stderr or stdout or f"exit code {r.returncode}"
        return {"error": err_msg[:300]}

    if not stdout:
        return {}

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass

    text = stdout[:300]
    for pat in _CUA_ERROR_PATTERNS:
        if pat.lower() in text.lower():
            return {"error": text}
    return {"text": text}


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

        # 提取元素索引
        idx_m = re.search(r'\[(\d+)\]', line)
        elem_idx = int(idx_m.group(1)) if idx_m else None

        # 时间/日期: 14:19 | 昨天 | 前天 | 06-04 | 06月04日
        if re.match(r'^(?:\d{1,2}:\d{2}|昨天|前天|\d{1,2}-\d{1,2}|\d{2}月\d{2}日)$', val):
            if cur_name:
                contacts.append({"name": cur_name, "job": cur_job or "", "time": cur_time or "", "idx": cur_idx})
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
                cur_idx = elem_idx
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
        contacts.append({"name": cur_name, "job": cur_job or "", "time": cur_time or "", "idx": cur_idx})

    # 去重 (同名+同岗位只保留第一个；同名不同岗位的候选人不丢弃)
    seen, unique = set(), []
    for c in contacts:
        key = (c["name"], c.get("job", ""))
        if key not in seen:
            seen.add(key)
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
            // #2 修脆弱匹配：只在带 data-id 的联系人 li 内精确匹配姓名叶子节点
            // （避开聊天区/标题里出现的同名文本、避免取到非联系人元素）
            var lis = document.querySelectorAll('[data-id]');
            for (var i = 0; i < lis.length; i++) {{
                var li = lis[i];
                var did = li.getAttribute('data-id');
                if (!did || !/^\\d/.test(did) || !(li.offsetWidth > 0)) continue;
                var els = li.querySelectorAll('*');
                for (var j = 0; j < els.length; j++) {{
                    if ((els[j].textContent||'').trim() === '{safe}') {{
                        return JSON.stringify({{uid: did.replace(/-\\d+$/, ''), key: 'data-id'}});
                    }}
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


def dom_contact_uids(pid, wid):
    """一次性按 DOM 顺序读取所有联系人的 (uid, name)。

    BOSS 侧边栏每个联系人是 `<div class="geek-item" data-id="<uid>-0">`，内含 `.geek-name`。
    返回按自上而下顺序的 [{"uid","name"}, ...]。供循环按「姓名+出现次序」**原子匹配** uid——
    修 #2「重名张冠李戴」：点击是按 AX 索引(正确的人)，但旧 uid 按姓名搜会取到第一个同名卡片，
    重名时配错 uid。改用本函数 + 出现次序，使 uid 与实际点击的联系人同源。
    """
    r = cua("page", json.dumps({
        "pid": pid, "window_id": wid, "action": "execute_javascript",
        "javascript": """
        (function(){
            var out = [];
            var items = document.querySelectorAll('.geek-item[data-id]');
            for (var i = 0; i < items.length; i++) {
                var li = items[i];
                var did = li.getAttribute('data-id');
                if (!did || !/^\\d/.test(did)) continue;
                var nmEl = li.querySelector('.geek-name');
                out.push({uid: did.replace(/-\\d+$/, ''), name: nmEl ? (nmEl.textContent||'').trim() : ''});
            }
            return JSON.stringify({contacts: out});
        })()
        """,
    }))
    if isinstance(r, dict) and isinstance(r.get("contacts"), list):
        return r["contacts"]
    return []


def click_sidebar_ax(idx: int, pid: int, wid: int) -> bool:
    """AX 点击侧边栏联系人（替代 JS 方案，避免 AppleScript 桥接问题）"""
    result = cua("click", json.dumps({"pid": pid, "window_id": wid, "element_index": idx}))
    err = result.get("error", "")
    if err:
        print(f"      AX点击失败: {err}")
        return False
    return True


def click_sidebar(name, pid, wid):
    """JS点侧边栏联系人，同时提取 data-id 作为 UID（保留作为 fallback）"""
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
    # 如果 JS 执行失败（返回 error），说明 AppleScript 桥接不可用
    if isinstance(r, dict) and r.get("error"):
        return False, None
    try:
        if isinstance(r, dict) and "status" in r:
            return r.get("status") == "clicked", r.get("uid")
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


def _pdf_belongs_to(path, name: str):
    """PDF 是否属于该候选人。三态返回，专供下载文件防串档判断：
        True  = PDF 正文确认含姓名（属于本人）
        False = 可读 PDF 但不含姓名（疑似别人的文件）
        None  = 无法判定（非 PDF / 图片型 PDF / Quartz 不可用）

    与 extract_pdf_text_quartz 的区别：那个返回空串时无法区分「别人」和「图片PDF」，
    这里用三态把「确认是别人」单独拎出来，让调用方能拒绝串档文件。
    """
    if not str(path).lower().endswith(".pdf"):
        return None
    try:
        import Quartz
        from Foundation import NSURL
    except Exception:
        return None  # pyobjc/Quartz 不可用 → 无法核验
    try:
        doc = Quartz.PDFDocument.alloc().initWithURL_(NSURL.fileURLWithPath_(str(path)))
        if doc is None:
            return None
        raw = (doc.string() or "").strip()
    except Exception:
        return None
    if not raw:
        return None  # 图片型 PDF，无正文可校验
    if name and len(name) >= 2:
        # 姓名整体出现，或被 PDF 排版拆字时逐字均在全文
        return name in raw or all(c in raw for c in name)
    return None


def _pick_candidate_file(files: list, name: str):
    """从新下载文件中挑出属于该候选人的那个（防串档）。

    返回 (Path|None, verdict):
      - 有文件经正文校验确认属于本人 → (该文件, "verified")
      - 有可读 PDF 但都不含姓名     → (None, "mismatch")   调用方应跳过本地保存
      - 全部无法判定(图片PDF/非PDF/Quartz不可用) → (最新一个, "unverified")
      - 全部过小(疑似限流 JSON)     → (None, "too_small")  调用方应跳过本地保存
    """
    MIN_FILE_SIZE = 1024  # 限流 JSON 仅几十字节，按此阈值剔除
    readable_mismatch = False
    for f in files:
        verdict = _pdf_belongs_to(f, name)
        if verdict is True:
            return f, "verified"
        if verdict is False:
            readable_mismatch = True
    if readable_mismatch:
        return None, "mismatch"
    # 无法核验：按修改时间倒序(最新优先)挑，跳过过小文件(疑似限流 JSON)
    def _size(f):
        try:
            return f.stat().st_size
        except OSError:
            return 0
    def _mtime(f):
        try:
            return f.stat().st_mtime
        except OSError:
            return 0.0
    big_enough = [f for f in files if _size(f) >= MIN_FILE_SIZE]
    if not big_enough:
        return None, "too_small"
    big_enough.sort(key=_mtime, reverse=True)
    return big_enough[0], "unverified"


def _fetch_pdf_via_page(pid, wid, pdf_url: str) -> bytes | None:
    """【最可靠】在已登录的 Chrome 页面上下文里用同步 XHR 取 PDF 字节。

    为什么最稳：XHR 跑在正在渲染这份预览的【同一个登录会话】里，cookie/session
    自动带上 → 不会像 urllib 那样拿到登录页；且【不依赖】Chrome 下载机制 / ~/Downloads
    目录。用 overrideMimeType('...x-user-defined') 取原始二进制 → btoa 转 base64 回传。
    返回 PDF bytes，失败返回 None（大文件 base64 回传若被截断会校验 %PDF- 失败而降级）。
    """
    safe = pdf_url.replace("\\", "\\\\").replace("'", "\\'")
    js = (
        "JSON.stringify((function(){try{"
        "var x=new XMLHttpRequest();x.open('GET','" + safe + "',false);"
        "x.overrideMimeType('text/plain; charset=x-user-defined');x.send();"
        "if(x.status!==200)return{ok:false,status:x.status};"
        "var r=x.responseText,s='';for(var i=0;i<r.length;i++)s+=String.fromCharCode(r.charCodeAt(i)&255);"
        "return{ok:true,b64:btoa(s),size:r.length};"
        "}catch(e){return{ok:false,err:''+e};}})())"
    )
    res = cua("page", json.dumps({
        "pid": pid, "window_id": wid, "action": "execute_javascript", "javascript": js,
    }))
    if not isinstance(res, dict) or not res.get("ok") or not res.get("b64"):
        return None
    try:
        data = base64.b64decode(res["b64"])
    except Exception:
        return None
    if data[:5] != b"%PDF-" or len(data) < 1024:   # 截断/限流/登录页 → 降级
        return None
    return data


def _download_attachment(pid, wid, name: str, filename: str = "") -> str | None:
    """从 BOSS PDF 预览中提取下载 URL 并把 PDF 落盘到 data/resumes/

    下载链路（任一层失败自动降级，不短路）：
      ① 页面内同步 XHR 鉴权下载（最可靠，直接写 data/resumes/，不碰 ~/Downloads）
      ② Chrome 原生下载 → 轮询下载目录 → 防串档/校验 → 拷贝
      ③ urllib 直连（最后兜底，无登录态多半失败）

    Chrome 内嵌 PDF viewer 的工具栏（下载按钮）对 macOS Accessibility API
    完全不可见，无法通过 AX 树定位。实际方案：从 PDF viewer iframe 的
    src 参数中解码真实下载地址，用 JS 触发 Chrome 下载。

    返回本地文件路径，失败返回 None。
    """
    RESUMES_DIR.mkdir(parents=True, exist_ok=True)

    # 从 PDF viewer iframe src 提取真实下载 URL
    # BOSS 格式: https://www.zhipin.com/bzl-office/pdf-viewer-b
    #   ?url=%2Fwflow%2Fzpgeek%2Fdownload%2Fpreview4boss%2F{id}%3Fd%3D{ts}...
    r = cua("page", json.dumps({
        "pid": pid, "window_id": wid,
        "action": "execute_javascript",
        # 关键：只取**当前可见**的 PDF 预览 iframe。
        # BOSS 关闭简历预览后会把 iframe 留在 DOM 里(隐藏, 0 尺寸); 若不判可见性,
        # 下一个候选人会**首先匹配到上一个人残留的隐藏 iframe** → 反复下载同一份简历(死循环)。
        # 故先按 src 收集候选, 再用 getBoundingClientRect 过滤掉不可见的, 取可见的。
        "javascript": """
        JSON.stringify((function(){
            function visible(el){var r=el.getBoundingClientRect();return r.width>0&&r.height>0;}
            var iframes = document.querySelectorAll('iframe');
            for (var i = 0; i < iframes.length; i++) {
                if (!visible(iframes[i])) continue;   // 跳过隐藏/残留的预览 iframe
                var src = iframes[i].src || '';
                var m = src.match(/bzl-office\\/pdf-viewer-b\\?url=([^&]+)/);
                if (m) {
                    var decoded = decodeURIComponent(m[1]);
                    return {url: 'https://www.zhipin.com' + decoded, source: 'iframe'};
                }
                m = src.match(/\\.pdf(\\?|$)/);
                if (m) return {url: src, source: 'iframe-direct'};
            }
            var embeds = document.querySelectorAll('embed[src*=".pdf"], object[data*=".pdf"]');
            for (var j = 0; j < embeds.length; j++) {
                if (!visible(embeds[j])) continue;
                var u = embeds[j].src || embeds[j].getAttribute('data') || '';
                if (u) return {url: u, source: 'embed'};
            }
            return {url: null};   // 无可见预览 → 不下载(候选人没附件简历, 或预览已关闭)
        })())
        """,
    }))

    pdf_url = ""
    if isinstance(r, dict):
        pdf_url = r.get("url", "") or ""

    if not pdf_url:
        return None

    # 构造安全文件名
    safe_name = re.sub(r'[<>:"/\\|?*]', '_', name)
    ext = ".pdf"
    if filename:
        m = re.search(r'\.(\w+)$', filename)
        if m:
            ext = f".{m.group(1)}"
    dest = RESUMES_DIR / f"{safe_name}{ext}"

    # 去重: 已有同名文件则加时间戳
    if dest.exists():
        ts = datetime.now().strftime("%H%M%S")
        dest = RESUMES_DIR / f"{safe_name}_{ts}{ext}"

    # ── 策略0（首选, 最可靠）: 页面内同步 XHR 鉴权下载 → 直接写 data/resumes/ ──
    data = _fetch_pdf_via_page(pid, wid, pdf_url)
    if data:
        try:
            dest.write_bytes(data)
            print(f"    📥 附件下载(页面鉴权): {dest.name} ({len(data):,} bytes)")
            time.sleep(random.uniform(3, 6))   # 节流, 规避限流
            return str(dest)
        except OSError as e:
            print(f"    ⚠ 页面下载写盘失败({e})，转其他方式")

    # 策略1: JS 触发 Chrome 下载 (a.click) -> 从下载目录拷贝
    # 下载目录默认 ~/Downloads，可用 BOSS_CHROME_DOWNLOAD_DIR 覆盖（若改过 Chrome 下载位置）
    dl_dir = Path(os.environ.get("BOSS_CHROME_DOWNLOAD_DIR", str(Path.home() / "Downloads")))
    before = set(str(p) for p in dl_dir.iterdir()) if dl_dir.exists() else set()

    safe_pdf_url = pdf_url.replace("'", "\\'")
    r = cua("page", json.dumps({
        "pid": pid, "window_id": wid,
        "action": "execute_javascript",
        "javascript": f"""
        (function(){{
            var a = document.createElement('a');
            a.href = '{safe_pdf_url}';
            a.download = '';
            a.click();
            return 'clicked';
        }})()
        """,
    }))

    # 等待下载完成
    # Chrome 下载先落地为 *.crdownload 临时文件，完成后才重命名为最终文件，
    # 直接拷贝临时文件会在重命名瞬间触发 FileNotFoundError —— 必须忽略未完成的临时文件。
    _PARTIAL_SUFFIXES = (".crdownload", ".download", ".part", ".tmp")

    def _completed_new_files() -> list[Path]:
        after = set(str(p) for p in dl_dir.iterdir())
        return [
            Path(p) for p in (after - before)
            if not p.endswith(_PARTIAL_SUFFIXES)
        ]

    for _ in range(20):
        time.sleep(1)
        done = _completed_new_files()
        if not done:
            continue
        # 等待每个新文件写入稳定（Chrome 先落 .crdownload，完成后才重命名）
        for f in list(done):
            for __ in range(10):
                s1 = f.stat().st_size if f.exists() else 0
                time.sleep(0.5)
                s2 = f.stat().st_size if f.exists() else 0
                if s1 == s2 and s1 > 0:
                    break
        # 防串档：多个新文件(或并发下载)时按 PDF 正文姓名校验挑出属于本人的那个
        src, verdict = _pick_candidate_file(done, name)
        if src is None:
            if verdict == "too_small":
                # 新下载文件均过小 → 疑似 BOSS 限流 JSON，不取它，转直连兜底
                print(f"    ⚠ 下载文件均过小(疑似限流), 转直连兜底")
            else:
                # 可读 PDF 都不含本人姓名 → 疑似别人的下载，不取它，转直连兜底
                print(f"    ⚠ 新下载文件均不含「{name}」(疑似串档), 转直连兜底")
            break
        if verdict == "unverified" and len(done) > 1:
            print(f"    ⚠ 无法核验下载归属(图片PDF/Quartz不可用), 取最新一个")
        # 拷贝前再核验 src：限流 JSON 可能被存成 .pdf，按 PDF 魔术字节 + 体积兜底拦截
        try:
            with open(src, "rb") as fh:
                head = fh.read(5)
            src_size = src.stat().st_size
        except OSError as e:
            print(f"    ⚠ 附件读取失败({e}), 转直连兜底")
            break
        if head != b"%PDF-" or src_size < 1024:
            print(f"    ⚠ 下载文件非PDF/过小(疑似限流), 转直连兜底")
            break
        try:
            shutil.copy2(str(src), str(dest))
        except (FileNotFoundError, OSError) as e:
            print(f"    ⚠ 附件拷贝失败({e}), 转直连兜底")
            break
        sz = src.stat().st_size if src.exists() else 0
        print(f"    📥 附件下载: {dest} ({sz:,} bytes)")
        time.sleep(random.uniform(3, 6))  # 下载节流，规避 BOSS 限流
        return str(dest)

    # 策略2: 用 urllib 直连下载（可能无登录态）
    try:
        req = urllib.request.Request(pdf_url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Referer": "https://www.zhipin.com/",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read()
        # 校验确实是 PDF：无登录态时 BOSS 直链会返回 HTML 登录/错误页，
        # 不能把这种垃圾存成 .pdf（否则后续解析/评分拿到的是登录页文字）。
        if content[:5] != b"%PDF-":
            print(f"    ⚠ 直链返回非PDF({len(content)}B, 疑似登录页/无登录态)，跳过保存")
            return None
        dest.write_bytes(content)
        print(f"    📥 直链下载: {dest} ({len(content):,} bytes)")
        time.sleep(random.uniform(3, 6))  # 下载节流，规避 BOSS 限流
        return str(dest)
    except Exception as e:
        print(f"    ⚠ 下载失败: {e}")
        return None


# ══════════════════════════════════════════════════
# 面板读取
# ══════════════════════════════════════════════════

def read_panel(pid, wid, whitelist=None):
    """读右侧对话面板: name, school, degree, job

    学校识别：先收集面板里含学校的文本（"·"信息行 + 含「大学/学院」的短文本），
    用 find_school 做白名单整词反查（解决「（北京）/分校/科学技术院」被窄正则截断而漏配
    的误杀），命中即取规范白名单名；未命中再退化窄正则提取（用于展示/非白名单候选人）。
    """
    tree = ax_tree(pid, wid)
    result: dict = {"name": "", "school": "", "degree": "", "job": "",
              "has_attachment": False, "resume_filename": ""}
    school_blob = []  # 可能含学校的文本片段，供白名单反查

    for line in tree.split("\n"):
        # 学历
        m = re.search(r'AXStaticText\s*=\s*"(博士|硕士|本科|大专)"', line)
        if m and not result["degree"]: result["degree"] = m.group(1)

        m = re.search(r'AXStaticText\s*=\s*"(.+?)"', line)
        if m:
            val = m.group(1)
            if "·" in val and len(val) < 80:                 # 候选人信息行
                school_blob.append(val)
                for p in (x.strip() for x in val.split("·")):
                    if p in ("博士", "硕士", "本科", "大专") and not result["degree"]:
                        result["degree"] = p
                if not result["job"]: result["job"] = val
            elif any(s in val for s in ("大学", "学院", "学校", "研究院")) and len(val) < 30:
                school_blob.append(val)                       # 独立学校文本

        # 附件简历: AXLink=对方已发 / AXWebArea PDF=已打开预览
        idx_m = re.search(r'\[(\d+)\]', line)
        if idx_m and 270 <= int(idx_m.group(1)) <= 350:
            if 'AXLink (附件简历)' in line or ('AXWebArea' in line and 'PDF' in line):
                result["has_attachment"] = True

        # 附件文件名 (.pdf .docx .doc) — 仅限右侧面板区域 (idx 250-760)
        if idx_m and 250 <= int(idx_m.group(1)) <= 760:
            m = re.search(r'(?:AXStaticText|AXHeading)\s*=\s*"([^"]+\.(?:docx?|pdf|doc))"', line)
            if m: result["resume_filename"] = m.group(1)

    # 学校：白名单整词反查优先（规范名，含括号/分校），未命中退化窄正则（展示用）
    blob = " ".join(school_blob)
    hit = find_school(blob, whitelist) if whitelist else ""
    if hit:
        result["school"] = hit
    else:
        m = re.search(r'([一-龥]{2,8}(?:大学|学院|学校))', blob)
        if m: result["school"] = m.group(1)

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


def _pdf_preview_present(pid, wid) -> bool:
    """页面上是否存在可见的简历 PDF 预览 iframe（pdf-viewer / *.pdf）。"""
    r = cua("page", json.dumps({
        "pid": pid, "window_id": wid, "action": "execute_javascript",
        "javascript": (
            "(function(){var f=document.querySelectorAll('iframe');"
            "for(var i=0;i<f.length;i++){var s=f[i].src||'';var r=f[i].getBoundingClientRect();"
            "if((s.indexOf('pdf-viewer')>=0||s.indexOf('.pdf')>=0)&&r.width>0&&r.height>0)return true;}"
            "return false;})()"),
    }))
    return r is True or str(r).strip().lower() == "true"


def close_resume_preview(pid, wid) -> bool:
    """可靠关闭简历 PDF 预览浮层；返回 True 表示已关闭(或本就没有)。

    BOSS 简历预览是 boss-dialog 模态内嵌 pdf-viewer iframe，**Escape 常关不掉该模态**。
    若不关闭，下一个候选人点"附件简历"时 AX 树仍显示上一个人残留的 PDF → 复用同一 iframe
    URL **反复下载同一份简历(死循环) + 串档**（实测同一文件被下 10 次、还存成了别人的名字）。

    故：Escape →（仍在则）JS 点该 PDF 预览所属模态的关闭按钮(.boss-popup__close 等)，
    每轮校验 iframe 是否消失，最多重试 3 次。关闭浮层不影响候选人状态、非反爬敏感操作，
    用 JS 点击即可（与"不合适/打招呼/换微信"等敏感操作不同）。
    """
    # 注意：重复打开会在 DOM 里**叠多个** boss-dialog（实测残留 2 个 iframe），
    # 只关"就近一个"会漏 → 每轮点掉**所有可见**的 .boss-popup__close/.icon-close 排空。
    # 实测一轮即把 2 个 pdf 预览清到 0。
    # Escape 是键盘事件，只送达前台窗口 → 后台跑时打空。先激活 Chrome 到前台再按 Esc，
    # JS 点关闭按钮作为不依赖前台的兜底(两者并用最稳)。
    _activate_chrome_front()
    for _ in range(4):
        if not _pdf_preview_present(pid, wid):
            return True
        cua("press_key", json.dumps({"pid": pid, "window_id": wid, "key": "escape"}))
        time.sleep(0.3)
        cua("page", json.dumps({
            "pid": pid, "window_id": wid, "action": "execute_javascript",
            "javascript": (
                "(function(){var n=0;"
                "document.querySelectorAll('.boss-popup__close,.icon-close').forEach(function(c){"
                "var r=c.getBoundingClientRect();if(r.width>0&&r.height>0){c.click();n++;}});"
                "return n;})()"),
        }))
        time.sleep(0.6)
    return not _pdf_preview_present(pid, wid)


def _read_wechat_for(name: str, tree: str) -> str:
    """从 AX 树里读「{name}的微信号：xxx」——**必须匹配当前候选人姓名**。

    BOSS 聊天页 AX 树会同时包含多个候选人的微信交换消息(如"游荣亮的微信号：wxid_..."、
    "许平的微信号：anykcryXu")。原代码取**第一个**'微信号'行、不认人 → 把别人的微信号
    写给当前候选人(串档, 实测同一号码串到 4 个人)。按姓名前缀匹配即可各取各的。
    """
    if not name:
        return ""
    for line in tree.split('\n'):
        if f'{name}的微信号' in line:
            m = re.search(r'的微信号[:：]\s*([^\s"\]]+)', line)
            if m:
                return m.group(1).strip()
    return ""


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
    # 防御性关闭：先清掉上一个候选人可能残留的 PDF 预览，避免本次复用其 iframe → 串档/重复下载
    if _pdf_preview_present(pid, wid):
        close_resume_preview(pid, wid)
        time.sleep(0.5)

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


# extract_pdf_text_quartz 已移至 app/pdf_util.py（collect 与评分回捞共用），此处 import 使用


# ══════════════════════════════════════════════════
# SQLite（init_db / upsert）
# ══════════════════════════════════════════════════

# init_db() 已提取到 app/db.py，此处通过 from app.db import init_db 使用


def upsert(conn, data):
    """按 uid 唯一键更新或插入。uid=None 时 fallback 到 INSERT OR REPLACE。"""
    uid = data.get("uid")
    if uid:
        conn.execute("""
            INSERT INTO candidates
                (uid, name, job_position, school, degree, resume_content, resume_filename,
                 resume_path, has_resume, wechat, has_wechat, wechat_requested, phone, email, score, status, notes, extracted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET
                name = excluded.name,
                job_position = excluded.job_position,
                school = excluded.school,
                degree = excluded.degree,
                resume_content = CASE WHEN excluded.resume_content != '' THEN excluded.resume_content ELSE candidates.resume_content END,
                resume_filename = CASE WHEN excluded.resume_filename != '' THEN excluded.resume_filename ELSE candidates.resume_filename END,
                resume_path = CASE WHEN excluded.resume_path != '' THEN excluded.resume_path ELSE candidates.resume_path END,
                has_resume = CASE WHEN excluded.has_resume = 1 THEN 1 ELSE candidates.has_resume END,
                wechat = CASE WHEN excluded.wechat != '' THEN excluded.wechat ELSE candidates.wechat END,
                has_wechat = CASE WHEN excluded.has_wechat = 1 THEN 1 ELSE candidates.has_wechat END,
                wechat_requested = CASE WHEN excluded.wechat_requested = 1 THEN 1 ELSE candidates.wechat_requested END,
                phone = CASE WHEN excluded.phone != '' THEN excluded.phone ELSE candidates.phone END,
                email = CASE WHEN excluded.email != '' THEN excluded.email ELSE candidates.email END,
                status = excluded.status,
                notes = excluded.notes,
                extracted_at = CURRENT_TIMESTAMP
        """, (
            uid, data.get("name",""), data.get("job",""),
            data.get("school",""), data.get("degree",""),
            data.get("resume_content",""), data.get("resume_filename",""),
            data.get("resume_path",""),
            1 if data.get("has_resume") else 0,
            data.get("wechat",""), 1 if data.get("has_wechat") else 0,
            1 if data.get("wechat_requested") else 0,
            data.get("phone",""), data.get("email",""),
            data.get("score",0), data.get("status","collected"),
            data.get("notes",""), datetime.now().isoformat(),
        ))
    else:
        # 无 uid: 先按 name+job_position 尝试 UPDATE，命中则合并(避免重复孤儿行)，
        # 未命中再 INSERT。盲插入会在 uid 缺失时产生重复行(历史 bug)。
        name = data.get("name", "")
        job = data.get("job", "")
        cur = conn.execute("""
            UPDATE candidates SET
                school = ?, degree = ?,
                resume_content = CASE WHEN ? != '' THEN ? ELSE resume_content END,
                resume_filename = CASE WHEN ? != '' THEN ? ELSE resume_filename END,
                resume_path = CASE WHEN ? != '' THEN ? ELSE resume_path END,
                has_resume = CASE WHEN ? = 1 THEN 1 ELSE has_resume END,
                wechat = CASE WHEN ? != '' THEN ? ELSE wechat END,
                has_wechat = CASE WHEN ? = 1 THEN 1 ELSE has_wechat END,
                wechat_requested = CASE WHEN ? = 1 THEN 1 ELSE wechat_requested END,
                phone = CASE WHEN ? != '' THEN ? ELSE phone END,
                email = CASE WHEN ? != '' THEN ? ELSE email END,
                status = ?, notes = ?, extracted_at = CURRENT_TIMESTAMP
            WHERE name = ? AND job_position = ?
        """, (
            data.get("school",""), data.get("degree",""),
            data.get("resume_content",""), data.get("resume_content",""),
            data.get("resume_filename",""), data.get("resume_filename",""),
            data.get("resume_path",""), data.get("resume_path",""),
            1 if data.get("has_resume") else 0,
            data.get("wechat",""), data.get("wechat",""),
            1 if data.get("has_wechat") else 0,
            1 if data.get("wechat_requested") else 0,
            data.get("phone",""), data.get("phone",""),
            data.get("email",""), data.get("email",""),
            data.get("status","collected"), data.get("notes",""),
            name, job,
        ))
        if cur.rowcount == 0:
            conn.execute("""
                INSERT INTO candidates
                    (name, job_position, school, degree, resume_content, resume_filename,
                     resume_path, has_resume, wechat, has_wechat, wechat_requested, phone, email, score, status, notes, extracted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                name, job,
                data.get("school",""), data.get("degree",""),
                data.get("resume_content",""), data.get("resume_filename",""),
                data.get("resume_path",""),
                1 if data.get("has_resume") else 0,
                data.get("wechat",""), 1 if data.get("has_wechat") else 0,
                1 if data.get("wechat_requested") else 0,
                data.get("phone",""), data.get("email",""),
                data.get("score",0), data.get("status","collected"),
                data.get("notes",""), datetime.now().isoformat(),
            ))
    conn.commit()


# ══════════════════════════════════════════════════
# 收集后实时评分（复用 query_db --rank 同一路径，best-effort）
# ══════════════════════════════════════════════════

def auto_score_candidates(conn, uids):
    """对本轮刚收集、已有简历正文的候选人即时评分并缓存(record_score)。

    复用 evaluate_candidate_auto（与 query_db --rank 同一条路径、同一份缓存），
    收集完即出分，看排行榜时秒显示。best-effort：任一候选人评分异常都不影响其他人，
    更不影响收集结果（数据已入库）。DeepSeek 未配置时各维度记 0 分并在评价里提示。
    """
    if not uids:
        return
    try:
        from app.scoring import (load_scoring_config, build_candidate_data,
                                 evaluate_candidate_auto)
        from app.db import record_score
        from app.chat_reply import load_jobs_config
    except Exception as e:
        print(f"  ⚠ 评分模块加载失败，跳过自动评分: {e}")
        return

    scfg = load_scoring_config()
    jobs = load_jobs_config().get("jobs", [])
    conn.row_factory = sqlite3.Row
    print(f"\n⭐ 自动评分 {len(uids)} 人（刚收集且有简历，DeepSeek 判断岗位）…")
    for i, uid in enumerate(uids, 1):
        try:
            row = conn.execute("SELECT * FROM candidates WHERE uid=?", (uid,)).fetchone()
            if not row:
                continue
            cdata = build_candidate_data(row)
            sc = evaluate_candidate_auto(cdata, jobs, config=scfg)
            if sc.skipped:
                print(f"  [{i}/{len(uids)}] {cdata['name']} → 跳过"
                      f"（{sc.errors[0] if sc.errors else '未满足评分条件'}）")
                continue
            record_score(conn, uid, sc.total_score, sc.summary)
            flag = " ⚠" if (sc.errors and sc.total_score == 0) else ""
            print(f"  [{i}/{len(uids)}] {cdata['name']} → "
                  f"{sc.job_id or '?'}: {sc.total_score:.1f}{flag}")
        except Exception as e:
            print(f"  [{i}/{len(uids)}] uid={uid} 评分异常，跳过: {e}")


# ══════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════

def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=20,
                   help="从聊天联系人列表【顶部往下处理的联系人个数】(前 N 个，"
                        "含被学校/学历筛掉、无简历跳过的，都计入)，默认 20。"
                        "注意：这是'处理前N个联系人'，不是'收集到N份简历'")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--min-degree", default=DEFAULT_MIN_DEGREE, help=f"最低学历 (默认{DEFAULT_MIN_DEGREE})")
    p.add_argument("--schools", type=str)
    p.add_argument("--no-score", action="store_true",
                   help="收集后不自动评分(默认收集完即对有简历者实时评分并缓存)")
    args = p.parse_args()

    from app.cloud_sync import require_account
    require_account()  # 许可门禁：必须用我们下发的账号登录才能运行

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
        print("  导航到聊天页...")
        # 优先 JS 导航
        r = cua("page", json.dumps({
            "pid": pid, "window_id": wid, "action": "execute_javascript",
            "javascript": f'window.location.href = "{CHAT}"',
        }))
        # JS 失败 → AX 点击"沟通"链接兜底
        if r.get("error"):
            print("  JS 导航失败, 改用 AX 点击...")
            comm_tree = ax_tree(pid, wid)
            for line in comm_tree.split("\n"):
                if "沟通" in line and "AXLink" in line and "意向" not in line:
                    m = re.search(r'\[(\d+)\]', line)
                    if m:
                        cua("click", json.dumps({"pid": pid, "window_id": wid, "element_index": int(m.group(1))}))
                        time.sleep(3)
                        break
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
    scored_uids = []  # 本轮收集到、有简历正文、待自动评分的 uid
    collected_uids = []  # 本轮收集到的所有 uid，用于采集后自动云同步

    contacts = scan_contacts(pid, wid)
    print(f"  {len(contacts)} 个联系人")

    # #2 修重名张冠李戴：按 DOM 顺序一次性取 (uid,name)，循环里按「姓名+出现次序」原子匹配 uid
    from collections import defaultdict
    name_uids = defaultdict(list)
    for d in dom_contact_uids(pid, wid):
        if d.get("name"):
            name_uids[d["name"]].append(d["uid"])
    name_occ = defaultdict(int)

    for i, contact in enumerate(contacts):
        if stats["collected"] + stats["unsuitable"] >= args.limit:
            break

        name = contact["name"]
        # #2: 按 DOM 顺序「姓名+出现次序」取该联系人 uid（与实际点击的人同源，重名不串）
        _k = name_occ[name]; name_occ[name] += 1
        pos_uid = name_uids[name][_k] if _k < len(name_uids[name]) else None
        print(f"\n  [{i+1}/{min(len(contacts), args.limit)}] {name} | {contact['job']}")

        # 点侧边栏: 优先用 AX 点击（索引来自 scan_contacts），失败再试 JS
        contact_uid = None
        clicked = False
        contact_idx = contact.get("idx")
        if contact_idx:
            clicked = click_sidebar_ax(contact_idx, pid, wid)
        if not clicked:
            # AX 失败 → JS fallback
            clicked, contact_uid = click_sidebar(name, pid, wid)
            if not clicked:
                time.sleep(1)
                clicked, contact_uid = click_sidebar(name, pid, wid)
        if not clicked:
            print(f"    ❌ 点击失败"); stats["skipped"] += 1; continue
        # #2: uid 优先用「DOM 顺序按姓名出现次序」的原子匹配（与实际点击的人同源，重名不串）；
        # 其次用 JS 点击带回的 uid；最后才回退脆弱的姓名搜索 get_contact_uid。
        contact_uid = pos_uid or contact_uid
        if not contact_uid:
            contact_uid = get_contact_uid(name, pid, wid)
            if not contact_uid:  # 渲染时序可能没读到 → 短暂重试一次
                time.sleep(0.8)
                contact_uid = get_contact_uid(name, pid, wid)
        if contact_uid:
            print(f"    uid: {contact_uid}")
        else:
            print(f"    ⚠ uid 未取到（{name}）——将写为无 uid 行，无法上云/跨脚本匹配（#2）")
        time.sleep(2)  # 等右侧面板加载

        panel = read_panel(pid, wid, whitelist)
        school = panel["school"] or ""
        # 学校没读到 → 面板可能没加载好：再等久点重读一次，仍空则跳过(绝不误点"不合适")
        if not school:
            time.sleep(2)
            panel = read_panel(pid, wid, whitelist)
            school = panel["school"] or ""
        degree = panel["degree"] or ""
        job = panel["job"] or contact.get("job", "")

        # 学校识别失败兜底：宁可漏放也不误杀好候选人 → 跳过(不点不合适)
        if not school:
            print(f"    → ⚠ 学校未识别(面板未读到)，跳过不处理(不点'不合适')")
            stats["skipped"] += 1
            cua("press_key", json.dumps({"pid": pid, "window_id": wid, "key": "escape"}))
            time.sleep(1)
            continue

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

            resume_action = ""
            if existing_resume:
                resume_content = existing_resume
                print(f"    → 简历: 已存在({len(resume_content)}字), 跳过提取")
            else:
                result = click_attachment_resume(pid, wid, name)
                resume_content = result.get("resume_content", "")
                resume_action = result.get("action", "")

            # 附件下载 + PDF 直解析。
            # 关键修正: 下载不再以 AX 是否提到正文为前提——只要 PDF 可得(action=extract)
            # 就下载，再用 Quartz 直接解析 PDF 文件(比 AX 树更可靠)。这样即便 AX 提取为空
            # (李艳萍即此例: PDF 已下载到 data/ 但 AX 提取失败、正文未入库)也能补回正文。
            resume_path = ""
            if not args.dry_run and not existing_resume and resume_action == "extract":
                try:
                    resume_path = _download_attachment(
                        pid, wid, name,
                        filename=panel.get("resume_filename", ""),
                    ) or ""
                except Exception as e:
                    print(f"    ⚠ 附件下载异常({e})")
                    resume_path = ""
                if resume_path:
                    print(f"    ✓ 附件已保存: {resume_path}")
                    # PDF 解析: 文本层优先, 扫描件/图片型自动 OCR; 校验姓名后取更完整的一份
                    pdf_text, src = extract_resume_from_pdf(resume_path, expected_name=name)
                    tag = "OCR" if src == "ocr" else "直解析"
                    if pdf_text and len(pdf_text) >= len(resume_content):
                        if len(resume_content) == 0:
                            print(f"    📄 PDF {tag}补回正文: {len(pdf_text)} 字 (AX 提取为空)")
                        else:
                            print(f"    📄 PDF {tag}: {len(pdf_text)} 字 (优于 AX {len(resume_content)} 字, 采用)")
                        resume_content = pdf_text
                    elif pdf_text:
                        print(f"    📄 PDF {tag} {len(pdf_text)} 字 (AX {len(resume_content)} 字更全, 保留 AX)")

            # 微信: 已交换→提取微信号, 可换→点换微信→确认, DB已有→跳过
            wechat_id = ""
            wechat_requested = False
            if conn:
                if contact_uid:
                    wx_row = conn.execute(
                        "SELECT wechat, has_wechat, wechat_requested FROM candidates WHERE uid=?",
                        (contact_uid,)).fetchone()
                else:
                    wx_row = conn.execute(
                        "SELECT wechat, has_wechat, wechat_requested FROM candidates WHERE name=? AND job_position=?",
                        (name, job)).fetchone()
                if wx_row and wx_row[0]:  # DB 已有微信号 → 直接用，跳过
                    wechat_id = wx_row[0]
                    wechat_requested = True
                    print(f"    → 微信: 已存在({wechat_id}), 跳过")
                elif wx_row and (wx_row[2] or wx_row[1]):  # 已请求过待通过(或老库 has_wechat=1)无号 → 不重复请求
                    wechat_requested = True
                    print(f"    → 微信: 已请求待对方通过, 跳过")

            if not wechat_requested:
                tree = ax_tree(pid, wid)
                # 已交换: 直接按姓名从聊天里读「{name}的微信号：xxx」(自带姓名, 不串档)
                wechat_id = _read_wechat_for(name, tree)
                # 没读到但有"查看微信"按钮 → 点开再按姓名读一次
                if not wechat_id and "查看微信" in tree:
                    js_click("查看微信", pid, wid); time.sleep(1)
                    wechat_id = _read_wechat_for(name, ax_tree(pid, wid))
                if wechat_id:
                    wechat_requested = True
                    print(f"    → 微信: {wechat_id}")
                # 未交换: 点"换微信"→确认 (请求交换)
                elif "换微信" in tree:
                    js_click("换微信", pid, wid); time.sleep(1.5)
                    if "确定与对方交换微信" in ax_tree(pid, wid):
                        js_click("确定", pid, wid)
                        wechat_requested = True
                        print(f"    → 微信: 已请求交换")

            # 从简历内容中提取手机号&邮箱（统一走 app.pdf_util.extract_contacts，
            # 已修正旧正则把「邮箱：」等中文标签前缀吞进 email 的问题）
            phone, email = extract_contacts(resume_content)

            data = {
                "uid": contact_uid,
                "name": name, "job": job, "school": school, "degree": degree,
                "resume_content": resume_content,
                "resume_filename": panel.get("resume_filename", ""),
                "resume_path": resume_path,
                # 有正文 或 有附件文件 都算「有简历」（文件存在但正文暂为空时也如实标记，
                # 评分阶段会按 resume_path 二次回捞正文，见 query_db --rank）
                "has_resume": bool(resume_content) or bool(resume_path),
                # has_wechat 仅在真有微信号时为真（修 #3：原 `wechat_requested or ...` 把
                # 「已请求未通过」误标成有微信，误导网页 + 让 chat 阶段逻辑不再追微信）；
                # 「已请求」单独存 wechat_requested，避免下次重复点换微信。
                "wechat": wechat_id, "has_wechat": bool(wechat_id),
                "wechat_requested": wechat_requested,
                "phone": phone, "email": email, "status": "collected",
            }
            if not args.dry_run: upsert(conn, data)
            stats["collected"] += 1
            if contact_uid:
                collected_uids.append(contact_uid)  # 用于本轮采集后云同步
            # 有 uid + 简历正文 → 纳入本轮自动评分队列(无简历者评分会跳过, 不入队)
            if contact_uid and resume_content:
                scored_uids.append(contact_uid)
            print(f"    ✓ 已收集")

        # 关掉简历预览（Escape 关不掉 boss-dialog → 用 close_resume_preview 点关闭按钮并校验）
        close_resume_preview(pid, wid)
        time.sleep(1)

    print(f"\n{'=' * 60}")
    print(f"收集完成: ✅{stats['collected']} 🚫{stats['unsuitable']} ⏭{stats['skipped']}")
    if conn:
        # Chrome 采集已结束 → 此时对本轮收集到的人实时评分（默认开，--no-score 关）
        if not args.no_score:
            auto_score_candidates(conn, scored_uids)
        count = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        print(f"数据库: {DB_PATH} ({count} 条)")
        conn.close()
        # 自动云同步（CLOUD_SYNC=on 时）：把本轮收集到的人增量上传一份。
        # best-effort：未开/未配置直接跳过；失败入队列；绝不影响已入库的本地数据。
        # 自动增量同步：推所有「未同步/改动过」的行(含历史漏网)，失败下次自动补，无需手动 push。
        if not args.dry_run:
            try:
                from app.cloud_sync import sync_pending
                sync_pending()
            except Exception as e:  # noqa: BLE001 — 云同步异常绝不影响采集
                print(f"  ⚠ 云同步异常(已忽略，不影响本地数据): {e}")


if __name__ == "__main__":
    main()

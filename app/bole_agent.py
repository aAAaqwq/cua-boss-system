"""app/bole_agent.py — 「伯乐」对话 agent（DeepSeek 驱动）

产品化内核：桌面 App 的「问伯乐」对话框、CLI（scripts/bole.py）都走这里。
人设来自 IDENTITY/SOUL/AGENTS.md + references/faq.md，用 .env 的 DEEPSEEK_API_KEY。
零第三方依赖（纯 urllib）；未配 key 时返回明确错误（不静默）。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent

# 人设三件套 + 应对手册（面向用户时的行为层）
_PERSONA_FILES = ["IDENTITY.md", "SOUL.md", "AGENTS.md", "references/faq.md"]

_TIMEOUT = 60
_MAX_TOKENS = 800
_TEMPERATURE = 0.6
_RETRIES = 2

# 让伯乐知道自己能驱动哪些产品能力（默认参数即「最佳」）
_CAPABILITIES = """# === 你能驱动的产品能力（脚本，技术细节别外露给用户）===
- **跑全流程 / 设定时**（用户说「直接干活吧/跑一遍/全自动/每天自动跑」等，没给数量就用这套最佳默认）：
  `python scripts/boss_pipeline.py --greet max --collect 50 --chat 50`（打招呼到上限 + 收/聊各 50）
- 单步：打招呼 cua_greeting_loop / 收简历 cua_collect / 智能沟通 cua_chat_loop / 同步岗位 cua_sync_jobs
- 看人：评分榜 `query_db.py --rank` / 约面 cua_interview / 面试提醒 interview_reminder
- 前置缺失（登录/岗位/话术/筛选）要先引导配好再跑；详见 SKILL.md / references/cli.md。

回答用户时：面向不懂技术的 HR，**简短、用结果说话、循循善诱、每次给下一步**；
不外露脚本/参数等技术细节，除非用户明确要。守住产品功能边界，别跑题。
"""

_persona_cache: Optional[str] = None


def _cfg() -> dict:
    """复用 chat_reply 的 .env 加载与 DeepSeek 配置。"""
    from app.chat_reply import _get_deepseek_config
    return _get_deepseek_config()


def load_persona() -> str:
    """拼出伯乐 system prompt（人设三件套 + 应对手册 + 能力清单），带缓存。"""
    global _persona_cache
    if _persona_cache is not None:
        return _persona_cache
    parts = []
    for rel in _PERSONA_FILES:
        p = ROOT / rel
        if not p.exists():
            continue
        try:
            parts.append(f"# ===== {rel} =====\n{p.read_text(encoding='utf-8')}")
        except Exception as e:  # noqa: BLE001 单个文件坏了不拖垮整个人设加载
            print(f"[bole] 跳过人设文件 {rel}: {e}", file=sys.stderr)
    parts.append(_CAPABILITIES)
    _persona_cache = "\n\n".join(parts)
    return _persona_cache


def chat(messages: list[dict], system: Optional[str] = None,
         max_tokens: int = _MAX_TOKENS, temperature: float = _TEMPERATURE
         ) -> tuple[Optional[str], str]:
    """多轮对话。messages=[{role:'user'|'assistant', content}]。返回 (回复, 错误)。"""
    cfg = _cfg()
    if not cfg["api_key"]:
        return None, "DEEPSEEK_API_KEY 未配置（cp .env.example .env 并填入）"

    sys_prompt = system if system is not None else load_persona()
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": sys_prompt}, *messages],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key']}"},
    )

    last = ""
    for attempt in range(_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip(), ""
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code != 429 and 400 <= e.code < 500:
                return None, last
        except Exception as e:  # noqa: BLE001 网络层统一重试
            last = str(e)
        if attempt < _RETRIES:
            time.sleep(1.5 * (2 ** attempt))
    return None, last


def ask(question: str, history: Optional[list[dict]] = None
        ) -> tuple[Optional[str], str]:
    """单轮便捷封装（可带历史）。"""
    msgs = list(history or []) + [{"role": "user", "content": question}]
    return chat(msgs)


_SUGGEST_SYSTEM = (
    "你是招聘助手对话的『快捷回复』生成器。站在 HR（用户）角度，"
    "针对伯乐刚说的话，给出 2-4 个最可能的下一步简短回复。"
    "要求：每个 ≤10 字、口语化、可直接点发；紧扣伯乐的话（他若在问选择/确认，就给对应选项）。"
    "只输出 JSON 字符串数组，例如 [\"好，开始吧\",\"先看简历\"]，不要任何多余文字或解释。"
)


def suggest_replies(history: Optional[list[dict]], reply: str) -> list[str]:
    """根据对话与伯乐最新回复，动态生成 HR 可能点的快捷回复。失败/未配置返回 []。"""
    if not reply or not _cfg().get("api_key"):
        return []
    ctx = ""
    for m in (history or [])[-4:]:
        role = "用户" if m.get("role") == "user" else "伯乐"
        ctx += f"{role}：{str(m.get('content', ''))[:120]}\n"
    prompt = f"对话上文：\n{ctx}\n伯乐最新回复：{reply[:600]}\n\n给出快捷回复 JSON 数组："
    text, _ = chat([{"role": "user", "content": prompt}], system=_SUGGEST_SYSTEM,
                   max_tokens=120, temperature=0.5)
    if not text:
        return []
    try:
        import re
        m = re.search(r"\[.*\]", text, re.S)
        arr = json.loads(m.group(0)) if m else []
        out = [str(x).strip()[:16] for x in arr if str(x).strip()]
        return out[:4]
    except Exception:  # noqa: BLE001 生成失败就不给动态建议，前端有兜底
        return []

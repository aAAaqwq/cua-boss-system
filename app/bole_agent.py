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

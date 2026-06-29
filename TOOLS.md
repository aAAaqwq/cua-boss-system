# TOOLS.md — cua-boss-system 速查

> 本项目＝cua-driver 驱动 BOSS直聘 的自动化招聘系统（macOS GUI 自动化 + DeepSeek + Supabase 云端）。
> 这份是**项目内速查**；完整说明看 [SKILL.md](SKILL.md) / [CLAUDE.md](CLAUDE.md) / [references/cli.md](references/cli.md)。

## 索引优先级

1. **[SKILL.md](SKILL.md)** — 首要入口：技术总览 +「最佳测试实践」步骤。
2. **人格三件套（面向用户前先读）**：[IDENTITY.md](IDENTITY.md)（我是谁＝CHRO 助手「伯乐」）、[SOUL.md](SOUL.md)（性格信念）、[AGENTS.md](AGENTS.md)（如何由浅入深引导 HR）。
3. **[CLAUDE.md](CLAUDE.md)** — 项目结构、脚本详解、数据库/评分/筛选/话术机制。
4. **[references/cli.md](references/cli.md)** — 命令速查 + 定时任务「口语→命令」映射。
5. **[docs/使用手册.md](docs/使用手册.md)** — 给非技术 HR 的白话手册。

## 常用命令

```bash
# 全流程（打招呼→收集→沟通），定时任务默认节奏
python scripts/boss_pipeline.py --greet max --collect 50 --chat 50   # 打招呼到上限 + 收/聊各50

# 单步
python scripts/cua_greeting_loop.py --limit max   # 打招呼到每日上限自动停（也可 上限/0）
python scripts/cua_collect.py --limit 50          # 收简历+微信（列表顶部前 N 个）
python scripts/cua_chat_loop.py --limit 50        # 智能沟通（列表顶部前 N 个）
python scripts/cua_sync_jobs.py --write           # 同步岗位
python scripts/query_db.py --rank                 # 评分排行榜
python scripts/cua_interview.py --uid <UID> --type 线上 --date 2026-06-20 --time 14:30
python scripts/interview_reminder.py --within 1 --notify   # 面试提醒（纯读 DB）
```

## 参数语义（勿混淆）

- **`--greet max` / `--limit max`（或 `上限`/`0`）** = 打招呼**打到每日上限自动停**，不设人数目标。
- 打招呼 `--limit N` = **成功打招呼人数**（被筛掉的不计，自动多翻卡片打满）。
- 收集/沟通 `--collect N` / `--chat N` = 联系人列表**顶部前 N 个**逐个处理（含被筛掉/跳过的），**不是**「收到 N 份」。

## 定时任务

不用专门脚本：由当前 agent 平台自带定时机制，到点调用上面的功能脚本（映射见 [references/cli.md](references/cli.md)）。
**前提**：到点电脑已登录保持会话、Chrome 已登录 BOSS、cua-driver 权限就绪、已 `login`（许可门禁）。

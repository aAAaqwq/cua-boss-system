# 安装与运行前准备

> cua-boss-system 的一次性环境安装、运行前检查清单、推荐运行顺序与新项目接入流程。
> 对应「最佳测试实践」流程的**步骤 1-3**。日常操作命令见 [cli.md](cli.md)，配置详解见 [config.md](config.md)。

## 前置依赖安装

### 1. Python 3.10+

```bash
# 检查版本
python3 --version   # 需要 >= 3.10

# 未安装时（macOS）：
brew install python@3.14    # 当前测试通过版本，>= 3.10 即可
# 或从 https://www.python.org/downloads/ 下载安装包
```

### 2. Xcode Command Line Tools（提供 swiftc 编译器）

```bash
# 安装（弹窗确认，约 1-2 GB）
xcode-select --install

# 验证
swiftc --version
# 输出类似: Swift version 6.x ...

# 注意: 不需要完整 Xcode.app，Command Line Tools 即可
# swiftc 用于首次运行时自动编译 CGEvent 鼠标工具到 /tmp/cua_hid
```

### 3. Chrome

```bash
# 下载安装
# https://www.google.com/chrome/

# 验证
pgrep -x "Google Chrome" && echo "x Chrome 运行中" || echo "请先启动 Chrome"
```

### 4. cua-driver (>= 0.5.x)

cua-driver 是本项目核心依赖，macOS 原生 `.app` 应用（带 GUI），通过 Accessibility API 操控 Chrome。当前测试版本 `0.5.1`。

#### 下载

| 渠道 | 链接 |
|------|------|
| **GitHub Releases（推荐）** | https://github.com/trycua/cua/releases |
| **具体版本** | 找 `cua-driver-rs` 开头的 tag，下载 `.dmg`（如 `cua-driver-rs-v0.5.2`） |
| **项目主页** | https://github.com/trycua/cua |

> **架构选择**: 下载页有 `aarch64`（Apple Silicon M1/M2/M3/M4）和 `x86_64`（Intel）两个 `.dmg`，选错会无法启动。在终端运行 `uname -m` 确认架构。

#### 安装步骤

```bash
# 1. 下载 .dmg -> 打开 -> 拖到 /Applications
#    安装后自动在 ~/.local/bin/ 创建 cua-driver 软链接

# 2. 首次授权 macOS 权限（Accessibility + Screen Recording）
cua-driver permissions grant

# 3. 安装 Agent Skills（Claude Code / OpenClaw 等）
cua-driver skills install

# 4. 启动后台服务
cua-driver serve

# 5. 设置开机自启（可选）
cua-driver autostart enable

# 6. 后续更新
cua-driver update --apply
```

#### GUI 功能

`CuaDriver.app` 是 macOS 菜单栏应用：
- **光标叠加层** -- agent 操作时显示虚拟光标，不移动真实鼠标（可用 `--no-overlay` 关闭）
- **权限管理** -- Accessibility / Screen Recording 授权弹窗
- **daemon 常驻** -- 后台运行，CLI 通过 Unix socket 通信
- **autostart** -- 开机自启

#### 常用命令

```bash
cua-driver status          # 检查服务状态
cua-driver doctor          # 全面诊断
cua-driver permissions status  # 权限状态
cua-driver skills status   # Agent skills 安装状态
cua-driver check-update    # 检查新版本
```

---

## 运行前检查清单

运行任何脚本前，逐项确认以下条件。任何一项不满足都需要先修复。

### 1. 系统依赖

```bash
# Python 3.10+
python3 --version

# cua-driver 已安装且在 PATH
cua-driver status

# swiftc 可用（macOS Xcode 自带）
swiftc --version

# Chrome 已启动
pgrep -x "Google Chrome" && echo "x Chrome 运行中"
```

### 2. Chrome 状态

- [ ] Chrome 已打开并**登录** BOSS直聘（zhipin.com）
- [ ] 登录态未过期（打开 https://www.zhipin.com/web/chat/index 能看到联系人列表）
- [ ] **已开启 "允许来自 Apple 事件的 JavaScript"**（Chrome 菜单栏 -> 显示 -> 开发者 -> [x] 允许来自 Apple 事件的 JavaScript）-- cua-driver `page` 命令执行 JS 的前提
- [ ] 目标页面已在标签页中打开：
  - 打招呼 -> 推荐牛人页
  - 智能沟通 / 收集 -> 沟通页（聊天页）
  - 同步职位 -> 职位管理页

### 3. 配置文件

```bash
# 岗位配置存在
cat config/jobs.json | python3 -m json.tool > /dev/null && echo "x jobs.json"

# 话术模板存在（先找 reply.json -> 兜底 reply-templates.json）
cat config/reply.json 2>/dev/null || cat config/reply-templates.json | python3 -m json.tool > /dev/null && echo "x reply templates OK"

# 系统提示词存在
test -f config/system_prompt.md && echo "x system_prompt.md"
```

### 4. DeepSeek API（必须提前配置，未配置时降级为模板原文，智能回复质量大幅下降）

```bash
# 复制模板并填入 API Key
cp .env.example .env
# 编辑 .env，填入: DEEPSEEK_API_KEY=sk-your-key-here

# 验证
test -f .env && grep -q "DEEPSEEK_API_KEY=sk-" .env && echo "x DeepSeek 已配置" || echo "x 未配置，请先执行 cp .env.example .env 并填入密钥"
```

> **注意**: DeepSeek 密钥必须**运行前**在 `.env` 中配置好。未配置时脚本不会报错，但所有智能回复会降级为模板原文，回复质量显著下降。建议在首次运行前完成配置。

### 5. 数据库（首次运行自动创建）

```bash
# 检查 candidates.db 是否存在
test -f data/candidates.db && echo "x DB 已存在 ($(sqlite3 data/candidates.db 'SELECT COUNT(*) FROM candidates') 条记录)" || echo "首次运行将自动创建"
```

### 6. 快速一键检查

```bash
python3 -c "
import subprocess, json
checks = []

# Python
v = subprocess.run(['python3','--version'], capture_output=True, text=True).stdout.strip()
checks.append(('Python 3.10+', '3.1' in v))

# cua-driver
r = subprocess.run(['cua-driver','status'], capture_output=True, text=True)
checks.append(('cua-driver', r.returncode == 0))

# Chrome
r = subprocess.run(['pgrep','-x','Google','Chrome'], capture_output=True)
checks.append(('Chrome 运行', r.returncode == 0))

# 配置文件
from pathlib import Path
checks.append(('jobs.json', Path('config/jobs.json').exists()))
checks.append(('templates.json', Path('config/reply.json').exists() or Path('config/reply-templates.json').exists()))
checks.append(('system_prompt.md', Path('config/system_prompt.md').exists()))

# DeepSeek
import os
for line in (Path('.env').read_text().splitlines() if Path('.env').exists() else []):
    if 'DEEPSEEK_API_KEY=sk-' in line:
        checks.append(('DeepSeek API', True)); break
else:
    checks.append(('DeepSeek API', False))

for name, ok in checks:
    print(f'  {'x' if ok else 'x'} {name}')
passed = sum(1 for _,ok in checks if ok)
print(f'\n  {passed}/{len(checks)} 通过')
"
```

---

## 推荐运行顺序

```bash
# 1. 先 dry-run 预览，确认脚本行为正确
python scripts/cua_sync_jobs.py             # 预览（自动写入）
python scripts/cua_collect.py --dry-run     # 预览收集
python scripts/cua_chat_loop.py --dry-run   # 预览沟通

# 2. 实际执行（先 collect 再 chat_loop，chat_loop 依赖 collect 写入的 DB 上下文）
python scripts/cua_collect.py --limit 10
python scripts/cua_chat_loop.py --limit 20

# 或一条命令跑完整 pipeline（打招呼→收集→沟通）
python scripts/boss_pipeline.py --greet 20 --collect 5 --chat 5
```

## 数据库管理

```python
from app.db import backup_db, clear_db, init_db

backup_db("manual")    # 备份到 data/backups/candidates_YYYYMMDD_HHMMSS_manual.db
clear_db()             # 自动备份 + 清空 candidates 表（保留表结构）
init_db()              # 重建/确认表结构
```

---

## 新项目接入完整流程

### 首次部署

```bash
# 0. 检查前置条件（Python 3.10+, cua-driver, Chrome, swiftc）
python3 --version && swiftc --version && cua-driver status && pgrep -x "Google Chrome"

# 1. 配置 DeepSeek
cp .env.example .env
# 编辑 .env 填入 DEEPSEEK_API_KEY

# 2. 配置筛选条件（可选）
cp config/filter-template.json config/filter.json
# 编辑 config/filter.json 自定义学校白名单

# 3. 同步职位（Chrome 需在职位管理页）
python scripts/cua_sync_jobs.py --dry-run  # 预览
python scripts/cua_sync_jobs.py            # 自动写入

# 4. 检查新岗位的 id/category 是否正确
#    如不符，更新 jobs-template.json 后重新 sync

# 5. 生成话术模板
python scripts/gen_reply_templates.py --all --write

# 6. 收集 -> 沟通
python scripts/cua_collect.py --dry-run
python scripts/cua_collect.py --limit 10
python scripts/cua_chat_loop.py --dry-run
python scripts/cua_chat_loop.py --limit 20
```

### 新增岗位

```bash
# 1. 在 BOSS 直聘发布新岗位
# 2. 同步到系统
python scripts/cua_sync_jobs.py
# 3. 检查 jobs-template.json 是否覆盖了新岗位的 id/category
#    如不覆盖 -> 手动添加
# 4. 生成话术模板
python scripts/gen_reply_templates.py --job-id <new-id> --write
# 5. 检查 scoring.json 是否需新增评分维度
```

### 日常维护

```bash
# 定期同步职位（BOSS 上改动了要求后）
python scripts/cua_sync_jobs.py

# 调整筛选条件
# 编辑 config/filter.json

# 优化回复策略
# 编辑 config/system_prompt.md

# 检查候选人数据
python scripts/query_db.py
```

# cua-boss-system

CUA 驱动的 BOSS直聘自动化打招呼系统。

## 依赖

| 依赖 | 类型 | 说明 |
|------|------|------|
| Python 3.10+ | 运行时 | 系统 Python 即可，零 pip 依赖 |
| `cua-driver` | 外部 CLI | 通过 macOS Accessibility API 操控 Chrome |
| Chrome 浏览器 | 运行环境 | 需运行并打开 BOSS直聘推荐页 |

## 原理

```
cua_greeting_loop.py
├── Python 标准库（argparse, json, subprocess, re ...）
├── app/filter_criteria.py（名校白名单 + 匹配逻辑，纯标准库）
└── cua-driver CLI
    ├── 启动会话 / 查找 Chrome 窗口
    ├── 读取 Accessibility 树扫描候选人
    └── 点击"打招呼"按钮 + 检测上限弹窗
```

## 用法

```bash
# 刷新页面 → 扫描 → 打招呼（最多5人）
python scripts/cua_greeting_loop.py

# 仅预览，不实际点击
python scripts/cua_greeting_loop.py --dry-run

# 最多打3人
python scripts/cua_greeting_loop.py --limit 3

# 不刷新页面，用当前页直接扫描
python scripts/cua_greeting_loop.py --no-refresh

# 指定学校白名单
python scripts/cua_greeting_loop.py --schools "清华大学,北京大学,浙江大学"
```

## 文件结构

```
cua-boss-system/
├── app/
│   └── filter_criteria.py    # 名校白名单 + 学校匹配
├── scripts/
│   └── cua_greeting_loop.py  # 主脚本
└── README.md
```

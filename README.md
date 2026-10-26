# news_ai_science · AI 科技头条机器人

TechCrunch / MIT TR / Wired / VentureBeat 抓正文 -> 中文标题 + 3~10 条摘要 -> 飞书**聚合图文卡**推群。

一条消息装 1~5 条新闻（`column_set` 左图右文 + 折叠要点 + 点击查看原文），
纯文本推送已废弃：卡片发不出去就保持 pending 等下一轮，不再降级成文本。

## 快速开始

```bash
cd D:/!pythonproject/feishu_bot/news_ai_science

# 1. 环境（已就绪，无需重装）
#    .venv 里已有 camoufox 0.5.8 / playwright / openai / pyyaml / dotenv / bs4
#    ⚠️ 严禁 pip install camoufox、严禁 python -m camoufox fetch

# 2. 配置
cp .env.example .env          # 填 LLM_API_KEY / FEISHU_WEBHOOK_URL / FEISHU_APP_*
# 新闻源改 config/sources.yaml；其它参数改 config/config.yaml

# 3. 跑一轮
./.venv/Scripts/python.exe main.py --once
```

## 常用命令

| 命令 | 作用 |
|---|---|
| `python main.py --once` | 抓取 + 摘要 + 推送（定时任务跑的就是这个） |
| `python main.py --once --dry-run` | 只生成卡片 JSON 到 `logs/`，不发群 |
| `python main.py --once --no-push` | 只抓取入库 |
| `python main.py --push-only` | 不抓取，只推队列里的待推条目 |
| `python main.py --stats` | 看队列：`{"pending": n, "pushed": m}` |
| `make test` / `make lint` | pytest（21 例）/ ruff |

## 目录

```
config/       sources.yaml（新闻源）  config.yaml（参数+密钥）  settings.py（只读常量）
src/news_ai_science/
  core/       路径、配置、日志、文本、异常
  contracts/  Article
  stages/     crawl(web) / generate(client, translator) / push(card_builder, image_uploader, feishu_sender)
  pipeline/   runner
  storage/    SQLite
tests/        离线单测，不联网
docs/         architecture.md
logs/         运行日志 + dry-run 卡片
```

## 注意

- **抓取必须走 Camoufox**：没有 RSS 路径，没有 requests 降级；单源失败跳过，全源失败才抛错。
- **没有配图就不推这一条**：`crawl.require_image: true`，绝不用无关图凑数。
- **不要重复装 Camoufox 内核**：内核在 `%LOCALAPPDATA%\camoufox\...\156.0.1-beta.36`，与 venv 无关。
- **配图需要飞书应用凭证**（群机器人没有上传图片的权限），凭证缺失时卡片会无图。
- **定时任务**：每日 08:00 / 20:00，命令是 `python main.py --once`。

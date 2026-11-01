---
name: techradar-news-ops
description: TechRadar_news 增量部署、只读审计、核验、回滚与清理运维技能。触发词：TechRadar_news、news_ai_science、增量镜像、Dockerfile.deploy、回滚、JobPulse、悬空镜像、techcrunch 超时、cron 08:00。
---
# TechRadar_news 运维 Skill

> 线上环境与本地开发环境不是一回事：本地跑 `python main.py`，线上是 `/var/www/TechRadar_news`
> 里的容器 `news_ai_science`，每天 08:00 由宿主 cron 驱动。本文只讲线上。

## 铁律（违反前先停下）

1. **禁止在 `/var/www/TechRadar_Digest` 执行 `git pull` 或 `docker compose up`**。该目录属于 AI 岗位日报项目，不是本项目。
2. **容器名保持 `news_ai_science`，禁止改名**。cron 依赖该容器名。
3. **`/var/www/JobPulse` 禁止删除、禁止移动、禁止清理**。容器 `news_ai_job` 挂载其中，包含岗位库和 Boss 登录态。
4. **纯代码变更一律走增量镜像**：`/var/www/TechRadar_news/Dockerfile.deploy`，base 为 `news_ai_science:backup-20261101`。
5. **完整构建仅在依赖、浏览器内核、模型变更时使用**。
6. **悬空镜像与 build cache 禁止随意 `prune`**。属于其他项目。
7. **任何删除或移动前，先只读审计**。
8. **核验必须进容器验代码，不能只看 `docker ps` 显示 Up**。

## 环境事实表

| 项 | 值 |
|---|---|
| 服务器 | `root@10.191.44.66`（SSH 免密已配） |
| 部署目录 | `/var/www/TechRadar_news` |
| 仓库 | `ma-lichen/TechRadar_Digest`，`master` |
| 容器名 | `news_ai_science` |
| 镜像标签 | `news_ai_science:1.0`，备份 `news_ai_science:backup-20261101` |
| 挂载 | `data/ logs/ output/` → `/app/{data,logs,output}` |
| cron | `0 8 * * * docker exec news_ai_science python main.py --once >> /var/log/techradar.log 2>&1` |
| 日志 | 宿主 `/var/log/techradar.log`；容器外亦可 `docker logs` |
| LLM | DeepSeek 官方 `https://api.deepseek.com` / `deepseek-flash`，key 在 `.env` |
| 浏览器内核 | Camoufox 156.0.1-beta.36 打在主镜像里，**不在宿主**；换 base 镜像 = 重下 1.29 GB |

## 增量部署

```bash
cd /var/www/TechRadar_news
git fetch && git checkout master && git pull --ff-only
git rev-parse --abbrev-ref HEAD        # 服务器 git 较老，不支持 branch --show-current
git log --oneline -1                   # 核对 commit 是不是你要上线的那个

docker image inspect news_ai_science:backup-20261101 >/dev/null   # base 必须在，否则 build 直接失败

docker build -f Dockerfile.deploy -t news_ai_science:1.0 .
docker compose up -d --no-build --force-recreate
docker ps --filter name=news_ai_science --format '{{.Names}} | {{.Image}} | {{.Status}}'
```

- 构建耗时约 **6 秒**，镜像增量约 **100 KB**（apt、依赖、内核、fpgen 模型全复用）。
- `.env` 里新增变量时，**必须在 `up` 之前写好**；compose 只在创建容器时注入环境变量，`restart` 不生效，要 `--force-recreate`。
- `Dockerfile.deploy` 是服务器专有文件、不进 git。目录若重建，按下面内容重建即可：

```dockerfile
FROM news_ai_science:backup-20261101
WORKDIR /app
COPY main.py ./
COPY config ./config
COPY src ./src
```

## 只读审计（删除 / 移动前必做）

```bash
docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
docker inspect <容器名> --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'
crontab -l
grep -rl 'TechRadar\|news_ai_science\|JobPulse' /etc/systemd/system /lib/systemd/system 2>/dev/null
```

判据：**只要某个目录被任意一个容器的 Mounts 命中，就不是“旧副本”**，不能删。
`/var/www/JobPulse` 就是这样被误判过——它挂着 `news_ai_job` 的库和 Boss 登录 cookie。

## 核验（部署后四步，缺一不可）

```bash
# 1) 代码真的进了镜像，不是旧镜像换了名字
docker exec news_ai_science grep -c chinese_problems /app/src/news_ai_science/stages/generate/translator.py   # 应 =3
docker exec news_ai_science grep -n 'THINKING_OFF_PAYLOADS' /app/src/news_ai_science/stages/generate/client.py

# 2) 配置解析（key 长度应为 35）
docker exec news_ai_science python -c "import sys;sys.path.insert(0,'/app/src');from news_ai_science.core.config import load_config;c=load_config();print(c['llm']['base_url'],c['llm']['model'],len(c['llm']['api_key']))"

# 3) 静默出卡，再查卡片里有没有纯英文长句
docker exec news_ai_science python main.py --push-only --dry-run
ls -t logs/card_dryrun_news_*.json | head -1        # 用脚本扫文本块，英文长句数应为 0

# 4) 真推一轮
docker exec news_ai_science python main.py --once
```

推完**回库里反查**（比看日志可信）：

```bash
python3 - <<'PY'
import sqlite3, json
c = sqlite3.connect('/var/www/TechRadar_news/data/news_ai_science.db'); c.row_factory = sqlite3.Row
for r in c.execute("SELECT * FROM articles WHERE status='pushed' ORDER BY pushed_at DESC LIMIT 4"):
    p = json.loads(r['payload'] or '{}')
    print(r['source_name'], '|', p.get('title_zh'), '| 兜底', p.get('is_fallback'), '|', len(p.get('full_translation') or ''), '字')
print(dict(c.execute('SELECT status, COUNT(*) FROM articles GROUP BY status').fetchall()))
PY
```

## 回滚

```bash
docker tag news_ai_science:backup-20261101 news_ai_science:1.0
cd /var/www/TechRadar_news && docker compose up -d --no-build --force-recreate
```

只回代码，不回数据。数据库结构向后兼容（`status` 多一个 `rejected` 取值不影响旧代码读取）。

## 清理边界

| 对象 | 体积 | 结论 |
|---|---|---|
| `/var/www/TechRadar_Digest` 里残留的 TechRadar `docker-compose.yml` | 5 KB | 已移到 `/var/www/TechRadar_news/_legacy/`；那份 compose 写着 `container_name: news_ai_science`，在岗位目录里跑一次就撞名 |
| 旧 `news_ai_science.db`、旧 `bot.log*`（`/var/www/TechRadar_Digest/{data,logs}`） | ~250 KB | 已完整复制到新目录，可归档；`data/` 里还有岗位库，整目录别动 |
| `/var/www/JobPulse` | 1.5 MB | **禁止删除**（铁律 3） |
| 悬空镜像 | 14.4 GB / 6 个 | **不是本项目产物**（创建于 10-19 / 10-27 / 10-28），属其他项目的中间层 |
| Build cache | ~6 GB | 清只影响构建速度，不删数据；要清用 `docker builder prune --filter until=24h`，别用 `--all` |

## techcrunch 超时（2026-11-01 已定性并修复）

**真因不是网络、不是反爬、不是浏览器指纹。** TechCrunch 页面 `<head>` 里同步加载
`https://www.google.com/recaptcha/api.js` 与 `https://api.rlcdn.com/...`，这两个域名
在服务器出口上是 TCP 黑洞（SYN 无响应，10 秒超时）。HTML 解析器停在那一行，
`DOMContentLoaded` 永远不触发——所以 `wait_until="domcontentloaded"` 是永等一个不会来的事件，
45 秒、15 秒结局相同。

三步判定（缺一不可）：

1. **网络层**：容器内 `urllib` 直取列表页，应 200 / 1 秒内 → 说明网络没问题
2. **内容层**：`page.on("response")` 看主文档应 200；`page.content()` 应有 300 KB、几十个文章链接
3. **卡点**：`document.readyState` 停在 `loading`，且有几个 `script` 请求永不返回 → 第三方脚本挂死

修复（已入库 `314f5b9`）：`_goto` 改 `wait_until="commit"` + `_settle()` 软等待
（`crawl.dom_settle_ms`，默认 3000，等不到只记 INFO、不算失败）；正文页从固定 sleep
改成轮询抽取（`BODY_WAIT_MS`）。线上实测 techcrunch 22.6s 入库 3 篇，全轮 39.1s 零失败。

> 教训：这类问题**不要靠调大 `page_timeout_ms`** 解决。它等的是一个永远不会来的事件，
> 给多久都是白等；正确做法是把「页面有没有内容」和「第三方脚本有没有加载完」解耦。


## 排障速查

| 症状 | 排查 |
|---|---|
| 群里出英文 | 查 `docker logs news_ai_science \| grep -E '丢弃\|拦下'`；`rejected` 增长属正常（历史英文条目被拉黑） |
| 群里断更而不是出英文 | LLM 不可用 → 外文源全被丢弃。先查 DeepSeek 连通与余额，再看 key 是否注入 |
| key 没生效 | `docker exec news_ai_science printenv \| grep -E '^(DEEPSEEK\|LLM)_' \| sed 's/=.*/=***/'`，空则 `.env` 未写或没 `--force-recreate` |
| 整批无配图 | `crawl.require_image: true` 会整批跳过，日志见 `无可用配图，按规则跳过` |
| 容器 Up 但行为没变 | 铁律 8：进容器 `grep` 代码，或比对镜像 ID `docker images news_ai_science` |

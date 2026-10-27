# 部署说明 · Docker 镜像（CentOS 7 / 内网服务器）

> 本文只讲**容器化部署**。本地开发怎么跑见 `README.md`，两者互不影响：
> 本地内核在 `%LOCALAPPDATA%\camoufox\...`，镜像内核在 `/root/.cache/camoufox`。

---

## 一、为什么必须容器化

| 事实 | 后果 |
|---|---|
| 目标服务器是 CentOS 7，系统 glibc 只有 **2.17** | 装不上 Camoufox |
| Camoufox 是定制版 Firefox 156，要求 glibc **≥ 2.28** | 宿主机直装必然 `GLIBC_2.28 not found` |
| 容器与宿主**只共用内核**，不共用系统库 | 镜像自带 Debian bullseye 的 glibc **2.31**，问题消失 |

**为什么是 `bullseye` 而不是 `bookworm`**：bullseye 的 glibc 2.31 对 CentOS 7 的 3.10 内核更友好，
不触发 `clone3` 等新 syscall，避开老内核 + 老版本 docker 的 seccomp 拦截；bookworm 的 glibc 2.36 容易在
这类环境里直接 `Operation not permitted`。

---

## 二、与部署相关的文件

| 路径 | 作用 | 是否进仓库 |
|---|---|---|
| `Dockerfile` | 两阶段构建：`kernel` 阶段只装浏览器内核，`runtime` 阶段装业务依赖 | 是 |
| `.dockerignore` | 构建上下文裁剪：排除 `.venv/`、`.git/`、`data/`、`logs/`、`.env`、`tests/`、`docs/` | 是 |
| `.docker-assets/install_offline_kernel.py` | **构建期**把官方内核 zip 摆成 SDK 认识的布局，实现零联网装核 | 是 |
| `.docker-assets/camoufox-lin.zip` | 官方 Linux 内核包，**需自己放进去**，约 1.3 GB | **否**（已在 `.gitignore` 排除） |

`install_offline_kernel.py` 只在 `docker build` 时执行，运行期完全用不到——它不被 `COPY src` 带进
业务代码，也不会被 `python main.py` 调用。

---

## 三、第一步：准备离线内核包（建议做）

### 3.1 版本必须对得上

内核版本由 **Camoufox SDK 的 `browser-pin.json`** 决定，不能随便下：

| 项 | 值 |
|---|---|
| Dockerfile 里装的 SDK | `camoufox==0.5.8` |
| 该 SDK 期望的内核 | **156.0.1-beta.36** |

### 3.2 在能上网的机器上取包

任选其一：

```bash
# 方式 A：让 SDK 自己下（会落到 ~/.cache/camoufox，然后从里面找 zip / 压回来）
pip install "camoufox==0.5.8"
python -m camoufox fetch

# 方式 B：直接拿官方 release 附件 camoufox-156.0.1-beta.36-lin.x86_64.zip
#        （GitHub: daijro/camoufox 的 releases）
```

### 3.3 命名规则（脚本靠文件名解析版本，改错会直接报错）

脚本用的正则是 `camoufox-(.+?)-lin\.`：

- 合法的：`camoufox-156.0.1-beta.36-lin.x86_64.zip`
- 解析结果：`version = 156.0.1`，`build = beta.36`
- 落盘位置：`<cache>/browsers/official/156.0.1-beta.36/`

改好名字后放到项目里：

```
.docker-assets/camoufox-lin.zip
```

### 3.4 不准备会怎样

Dockerfile 第 34–40 行是 `if [ -f ... ]` 分支：没有 zip 就走 `python -m camoufox fetch`，
构建期需要访问 GitHub。国内服务器上这一步基本必失败，所以内网部署**必须**把 zip 带上。

---

## 四、构建

```bash
cd D:/!pythonproject/feishu_bot/TechRadar_Digest     # 或服务器上的项目目录
docker build -t techradar-digest:1.0 .
```

构建日志里确认走的是哪条路：

```
==> 离线安装内核（不访问 GitHub）        ← 有 zip，正确
==> 构建期联网拉取官方 Linux 内核        ← 没 zip，内网会失败
[OK] 内核已离线安装：/root/.cache/camoufox/browsers/official/156.0.1-beta.36
[OK] active_version = browsers/official/156.0.1-beta.36
```

**体积预警**：内核解压后有几个 GB，`docker images` 里单镜像会到 3~4 GB 量级。
构建前确认 `/var/lib/docker` 所在分区留足余量，别在 20 GB 的小盘上构建。

---

## 五、运行

### 5.1 密钥从哪来

`.env` **不会**被拷进镜像（`.dockerignore` 已排除），必须在运行时注入。
配置优先级是 `环境变量 > .env > config/config.yaml`，所以 `--env-file` 就能覆盖 `config.yaml` 里的空字段。

### 5.2 最小可用命令

```bash
docker run --rm \
  --env-file .env \
  -v "$PWD/data:/app/data" \
  -v "$PWD/logs:/app/logs" \
  techradar-digest:1.0 --once
```

- `data/` 必须挂载：SQLite 去重库在里面，不挂等于每轮都是“全新文章”
- `logs/` 建议挂载：日志按天轮转，`--dry-run` 的卡片 JSON 也写在这里

### 5.3 三个起步动作（换机器/换群后必做）

```bash
# 1. 确认镜像能起来、配置读对了
docker run --rm --env-file .env techradar-digest:1.0 --stats

# 2. 确认真实落群（发一条纯文本探针，人要在群里看）
docker run --rm --env-file .env techradar-digest:1.0 --test-push

# 3. 不推群，只生成卡片 JSON 到 logs/
docker run --rm --env-file .env -v "$PWD/logs:/app/logs" techradar-digest:1.0 --once --dry-run
```

### 5.4 参数速查（与本地完全一致）

| 参数 | 作用 |
|---|---|
| `--once` | 跑一轮：抓取 + 摘要 + 推送（定时任务用的就是它） |
| `--once --dry-run` | 只生成卡片 JSON 到 `logs/`，不发群 |
| `--once --no-push` | 只抓取入库 |
| `--push-only` | 不抓取，只推队列里的待推条目 |
| `--stats` | 打印 `{"pending": n, "pushed": m}` 后退出 |
| `--test-push` | 发一条连通性测试文本，验证 webhook 落在哪个群 |
| `--config PATH` | 指定配置文件路径 |

> `CMD ["--stats"]` 是默认值：直接 `docker run 镜像` 不带参数时只打印队列状态，绝不会误推群。

---

## 六、定时（宿主机 cron）

项目内部**没有**调度器（旧 `feishu_bot` 的 `scheduler.py` 已废弃），定时交给宿主机 cron：

```cron
# /etc/crontab 或 crontab -e，每日 08:00 / 20:00
0 8,20 * * * cd /opt/techradar && /usr/bin/docker run --rm \
  --env-file .env \
  -v /opt/techradar/data:/app/data \
  -v /opt/techradar/logs:/app/logs \
  techradar-digest:1.0 --once >> /var/log/techradar-cron.log 2>&1
```

- 用 `--rm` 跑一次性容器，跑完即销毁，容器本身就是一层很薄的入口
- 一定写 `cd /opt/techradar`，否则 `--env-file .env` 和 `$PWD` 挂载路径都会错
- 镜像更新后 cron 不用改，重新 `docker build -t techradar-digest:1.0 .` 即可

---

## 七、网络

| 目标 | 说明 |
|---|---|
| 新闻站（TechCrunch / MIT TR / Wired / VentureBeat） | 需要服务器能出外网 |
| 大模型 `LLM_BASE_URL` | 现用公网 `IP:端口`，同样需要出网 |
| 飞书 webhook / 开放平台 | `open.feishu.cn`，服务器通常可直连 |

**服务器要走代理时**：容器默认是 bridge 网络，代理环境变量得显式传进去：

```bash
docker run --rm --env-file .env \
  -e HTTPS_PROXY=http://<代理地址>:<端口> \
  -e HTTP_PROXY=http://<代理地址>:<端口> \
  techradar-digest:1.0 --once
```

或直接 `--network host` 复用宿主机网络与代理设置。
`config/config.yaml` 里的 `crawl.bypass_proxy` 与这里配合：`false`（默认）= 走环境代理，
只有“服务器直连就通”的环境才改成 `true`。

---

## 八、常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `GLIBC_2.28 not found` | 用了宿主机 Python 或非 bullseye 的基础镜像；必须用本 Dockerfile 的 `python:3.11-slim-bullseye` |
| 启动报缺 `libgtk-3-0` / `libnss3` 等 | runtime 阶段的 apt 列表被精简过；这些是 Camoufox（Firefox）的运行期硬依赖，不能删 |
| 卡片推出来了但没有图 | 缺 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`：群机器人没有上传图片权限，`img_key` 必须走应用凭证 |
| 中文变成方块 | 基础层 `fonts-noto-cjk` 被删了；中文标题/正文全靠它 |
| 构建期卡在下载内核 | 没放 `camoufox-lin.zip`，实际在访问 GitHub；按第三节补包 |
| 每轮都抓“新文章”，去重失效 | `data/` 没挂载，容器销毁时 SQLite 一起没了 |
| 想改代码后重编，结果依赖又装一遍 | 正常：`COPY pyproject.toml README.md` + `pip install .` 在 `COPY src` 之前，只改业务代码时这层缓存仍然命中 |
| 镜像莫名很大 | 内核解压后数 GB 属正常；别把 `.docker-assets/*.zip` 也 COPY 进 runtime 阶段（现在没有） |

---

## 九、可选：需要 headful 时

`main.py` 在 Linux 且没有 `DISPLAY` 时会尝试用 `pyvirtualdisplay` 起一块虚拟屏，起不来就退回纯无头，
不影响主流程。容器里已设 `ENV MOZ_HEADLESS=1`，**默认不需要**这块。真要用就自己加：

```dockerfile
RUN pip install pyvirtualdisplay && apt-get install -y --no-install-recommends xvfb
```

---

## 十、安全提醒

- `.env` 里有 `LLM_API_KEY`、`FEISHU_WEBHOOK_URL`（token 等价密码）、`FEISHU_APP_SECRET`，
  已被 `.dockerignore` 排除，**不要**改成 `COPY . .` 那种写法
- `config/config.yaml` 里的对应字段保持空串，真实值只放 `.env`
- 别把 `.docker-assets/*.zip` 提交进仓库（1.3 GB，且属于可再下载的二进制）

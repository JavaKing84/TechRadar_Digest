# 部署说明 · Docker（CentOS 7 / 校园服务器 10.191.44.66）

> 本文只讲**容器化部署**。本地怎么跑见 `README.md`，两者互不影响：
> 本地内核在 `%LOCALAPPDATA%\camoufox\...`，镜像内核在 `/root/.cache/camoufox`。
>
> 本文所有版本号、包名、报错信息均来自**实际拉取镜像 / 实际构建**的验证，
> 未验证的推测会明确标注。

---

## 一、目标环境（已核实）

| 事实 | 证据 | 信心 |
|---|---|---|
| 服务器是 CentOS 7 级老系统 | SSH banner `SSH-2.0-OpenSSH_7.4`（CentOS 7 自带 7.4p1） | 高 |
| 系统 glibc 只有 2.17 | CentOS 7 固定值；由此推出必须容器化 | 高 |
| Camoufox 要求 glibc ≥ 2.28 | 官方内核基于 Firefox 156 | 高 |
| 22 端口从开发机可达 | TCP 连通性测试通过 | 高 |

宿主机直装 Python 必死于 `GLIBC_2.28 not found`。容器与宿主**只共用内核**，
镜像自带 Debian bullseye 的 glibc **2.31**，问题消失，且服务器系统库一根手指都不用动。

---

## 二、镜像设计（每条都是实测结论）

| 决策 | 原因 |
|---|---|
| 基础镜像 `python:3.10-slim-bullseye` | 实测 = Python **3.10.18** + glibc **2.31-13+deb11u13** |
| 不用 `python:3.10-slim` | 实测该 tag 已指向 **Debian 13 / trixie，glibc 2.41**。glibc ≥2.34 的 `posix_spawn` 会用 `clone3`，在 CentOS 7 的 3.10 内核 + 老旧 docker seccomp 下可能被 EPERM 拦死 |
| 不用 `bookworm` | glibc 2.36，同样踩 `clone3`；bullseye 的 2.31 是**低于**该阈值的最省事选择 |
| apt 走**归档源**（阿里云镜像） | bullseye 已于 2026-08 EOL，官方 `deb.debian.org` 的 bullseye-security 索引指向已删除的 `.deb`，实测 `apt-get install` 直接 404 失败。归档源 + `Acquire::Check-Valid-Until=false` 可正常装（实测 271 个包装好） |
| pip 走清华源 | 国内速度，且可信 |
| 依赖装自 `requirements.txt`，不用 `pip install .` | `pyproject.toml` 写着 `requires-python = ">=3.11"`，3.10 下会被元数据**直接拒装**；`main.py` 自己把 `src` 注入 `sys.path`，行为等价 |

代码层兼容性已核验：全部 `.py` 用 `ast.parse(feature_version=(3,10))` 解析通过，
未使用 `tomllib` / `typing.Self` / `StrEnum` / `except*` / `TaskGroup` 等 3.11+ 特性；
依赖包自身要求均 ≤3.10（camoufox 0.5.8 `>=3.10,<4.0`、playwright/openai/requests `>=3.10`）。

---

## 三、部署相关文件

| 路径 | 作用 | 进仓库 |
|---|---|---|
| `Dockerfile` | 两阶段：`kernel` 只装浏览器内核，runtime 装业务依赖 | 是 |
| `docker-compose.yml` | 启动编排：`restart: unless-stopped`、`env_file: .env`、挂载 `./data` `./logs` | 是 |
| `requirements.txt` | 镜像内依赖清单（由 pyproject 派生） | 是 |
| `.dockerignore` | 裁剪上下文，排除 `.venv/` `.git/` `data/` `logs/` `.env` | 是 |
| `.docker-assets/install_offline_kernel.py` | 构建期把官方内核 zip 摆成 SDK 认识的布局，实现零联网装核 | 是 |
| `.docker-assets/install_fpgen_model.py` | 构建期离线装 fpgen 指纹模型（1.5 MB，Camoufox 首次启动否则要去 GitHub 下） | 是 |
| `.docker-assets/fpgen-model-release.zip` | 指纹模型包，1.5 MB，**已入库**，服务器直接 clone 就有 | 是 |
| `.docker-assets/camoufox-<ver>-lin.x86_64.zip` | 官方 Linux 内核包，约 1.3 GB，**需自己传** | 否（`.gitignore` 只排除 `camoufox-*.zip`） |
| `.env` | 真实密钥，**只在宿主机**，不进 git、不进镜像 | 否 |

---

## 四、浏览器内核从哪来（构建期唯一的高风险点）

`Dockerfile` 在 kernel 阶段自动二选一：

1. **`.docker-assets/` 里有 `camoufox-*-lin*.zip`** → 调 `install_offline_kernel.py`，全程不联网。
2. **没有 zip** → 执行 `python -m camoufox fetch`，从 GitHub 下载约 1.3 GB。

服务器如果不能直连 GitHub，必须走第 1 条。命名规则：

```
camoufox-156.0.1-beta.36-lin.x86_64.zip      ← 推荐（脚本从文件名解析版本）
camoufox-lin.zip                              ← 也支持（脚本回退读 SDK 的 browser-pin.json）
```

> 离线包必须与 SDK 版本对齐：`camoufox==0.5.8` ↔ 内核 `156.0.1-beta.36`
> （见 `site-packages/camoufox/browser-pin.json`）。版本错配 SDK 会判定"未安装"并清缓存。

**如果两边都拿不到内核（服务器出不了 GitHub、本地也下不来）**，只有一条路：
在有网的机器上构建好镜像，`docker save` 成 tar，用 MobaXterm 的 SFTP 上传，再 `docker load`。
镜像实测约 4~5 GB，传输耗时按校园网上行带宽估算。

### 官方包地址（可直接用 curl 下载）

```
https://github.com/daijro/camoufox/releases/download/v156.0.1-beta.36/camoufox-156.0.1-beta.36-lin.x86_64.zip
```

大小 **1,294,875,941 字节**（约 1.21 GB）。国内直连实测约 65 KB/s 且会中途停滞，
建议加加速前缀：

```bash
# 单线程（会限速，约 60~300 KB/s）
curl -L -C - --retry 30 --retry-all-errors \
  -o camoufox-156.0.1-beta.36-lin.x86_64.zip \
  "https://ghfast.top/https://github.com/daijro/camoufox/releases/download/v156.0.1-beta.36/camoufox-156.0.1-beta.36-lin.x86_64.zip"
```

构建时也可以直接指定镜像地址，不必手工下：

```bash
sudo docker compose build \
  --build-arg CAMOUFOX_KERNEL_URL="https://ghfast.top/https://github.com/daijro/camoufox/releases/download/v156.0.1-beta.36/camoufox-156.0.1-beta.36-lin.x86_64.zip"
```

### ⚠ 不要用 `python -m camoufox fetch`

实测（本机）：该命令在下载停滞/失败时**以 0 退出**，`docker build` 显示成功，
但镜像里 `/root/.cache/camoufox` 只有一个 8 KB 的 `repo_cache.json`，没有任何浏览器本体，
上线后才在运行时炸。本 Dockerfile 已改为 `curl -fL`（HTTP 非 2xx 即失败）+
安装后 `test -x .../camoufox` 强校验，**内核缺失会让构建直接失败**，而不是产出空壳镜像。

> 注意范围：这条禁令只针对 **Docker 构建期**（构建日志没人会盯，且希望失败即中断）。
> 开发者在**本机**装内核时 `python -m camoufox fetch` 完全可用，装完用
> `python -m camoufox version` 确认 `Installed: Yes` 即可，详见根目录 README 第 1.5 节。
> 另：`install_offline_kernel.py` 已改为按平台校验启动文件，
> Windows 内核包（`camoufox-*-win*.zip`）也能用它离线安装。

---

## 五、服务器端步骤（MobaXterm，逐行粘贴）

### 0. 登录后先确认位置（`/var/www` 下并排有其他小组项目）

```bash
cd /var/www && ls -1
```

**只允许出现你自己的 `TechRadar_Digest`**，别动别的目录。

### 1. 拉代码

```bash
cd /var/www
sudo git clone https://gitee.com/ma-lichen/TechRadar_Digest.git
```

私有仓库才需要账号密码；公开仓库直接 clone。

### 2. 上传两个文件：`.env` 与浏览器内核包（**都必须做**）

git 里只有源码，这两个都被 `.gitignore` 排除，clone 下来是没有的。

#### (a) `.env` —— 没有它容器起不来

在**本机**另开一个 MobaXterm 标签页：

```bash
# 本机执行（Windows 可用 MobaXterm 的 SFTP 面板直接拖拽）
scp -P 22 "D:/!pythonproject/feishu_bot/TechRadar_Digest/.env" 用户名@10.191.44.66:/var/www/TechRadar_Digest/.env
```

传完在服务器上把权限收紧：

```bash
sudo chmod 600 /var/www/TechRadar_Digest/.env
```

#### (b) 浏览器内核包 1.23 GB —— 没有它镜像里就没浏览器

服务器大概率出不了 GitHub。不传这个包，构建会退化成构建期下载，**要么卡 5 小时，要么失败**。

```bash
# 本机执行。文件名必须保持原样（构建脚本按 camoufox-*-lin*.zip 匹配）
scp -P 22 \
  "D:/!pythonproject/feishu_bot/TechRadar_Digest/.docker-assets/camoufox-156.0.1-beta.36-lin.x86_64.zip" \
  用户名@10.191.44.66:/var/www/TechRadar_Digest/.docker-assets/
```

传完在服务器上核对字节数，**必须正好是 `1294875941`**。不一致说明传输中断：

```bash
ls -l /var/www/TechRadar_Digest/.docker-assets/*.zip
# 期望：-rw-r--r-- 1 user user 1294875941 ... camoufox-156.0.1-beta.36-lin.x86_64.zip
```

不一致时的续传办法（比 `scp` 重新传整个包省时间）：

```bash
# 本机执行；rsync 支持断点续传
rsync -avP --partial \
  "D:/!pythonproject/feishu_bot/TechRadar_Digest/.docker-assets/camoufox-156.0.1-beta.36-lin.x86_64.zip" \
  用户名@10.191.44.66:/var/www/TechRadar_Digest/.docker-assets/
```

> 1.23 GB 建议用 `scp`/`rsync`，不要用 MobaXterm 左侧 SFTP 面板拖拽（大文件容易卡死且看不到进度）。
> 校园网上行一般 2~10 MB/s，预计 2~10 分钟。
> 如果服务器确实能直连 GitHub（先测 `curl -I https://github.com`），这一步可以跳过。

### 3. 检查 docker / compose 版本

```bash
docker --version
docker compose version      # v2 插件
# 若报 "docker: 'compose' is not a docker command"，说明只有 v1：
docker-compose --version
```

- compose ≥ 2.25 的用户：按你的要求降到 v2.24.7 的话执行（可选，非必需）：
  ```bash
  sudo curl -SL https://github.com/docker/compose/releases/download/v2.24.7/docker-compose-linux-x86_64 \
       -o /usr/local/bin/docker-compose
  sudo chmod +x /usr/local/bin/docker-compose
  ```
  国内下不动 GitHub 时可换成镜像地址或在能联网的机器下好再传。
- 只有 v1（`docker-compose`）时，把下面所有 `docker compose` 换成 `docker-compose` 即可，
  compose 文件里的 `version: "2.4"` 就是为兼容 v1 留的（用 3.x 的话 `mem_limit`、
  `shm_size` 会被判为非法配置直接报错）。

### 4. 构建并启动

```bash
cd /var/www/TechRadar_Digest

# 若服务器拉不到 Docker Hub，先换基础镜像源：
#   export BASE_IMAGE=docker.m.daocloud.io/library/python:3.10-slim-bullseye

sudo docker compose up -d --build
```

> 请等待手动输入密码或回车执行（`sudo` 会要密码）。

首次构建时间长（apt 271 个包 + 1.3 GB 内核 + pip 依赖），**10~30 分钟属正常**。
构建完镜像约 4~5 GB，先确认磁盘：

```bash
df -h /var
```

### 5. 验证

```bash
sudo docker compose ps                 # 状态应为 Up
sudo docker compose logs -f            # 实时日志，看到「---- 开始一轮 ----」即正常
```

先做一次**出网自检**（容器要同时访问新闻站、飞书、大模型，任一不通都会"看起来在跑但发不出去"）：

```bash
sudo docker compose exec news-ai-science python - <<'PY'
import os, urllib.request, ssl
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
targets = {
  "飞书 webhook": os.environ.get("FEISHU_WEBHOOK_URL","").split("/hook/")[0] or "https://open.feishu.cn",
  "大模型":       os.environ.get("LLM_BASE_URL","").rsplit("/v1",1)[0],
  "新闻站":       "https://www.technologyreview.com",
}
for name, url in targets.items():
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent":"curl/8"})
        code = urllib.request.urlopen(req, timeout=10, context=ctx).status
        print(f"[OK]   {name:12} {code}  {url}")
    except Exception as e:
        print(f"[FAIL] {name:12} {type(e).__name__}: {e}  {url}")
PY
```

全绿再往下走；有 FAIL 先解决网络/代理（见第七节）。

日志正常长相（参考本机 dry-run 实测）：

```
[INFO] 抓取源 4 个，失败 0 个
[INFO] 大模型调用 4 次，全部 200
[INFO] 卡片已写入 logs/card_dryrun_news_*.json
```

确认数据落盘：

```bash
ls -l /var/www/TechRadar_Digest/data /var/www/TechRadar_Digest/logs
```

想先只发一条测试、不抓取：

```bash
sudo docker compose run --rm news-ai-science python main.py --test-push   # 仅 TechRadar 有该参数时可用
sudo docker compose run --rm news-ai-science python main.py --dry-run --once
```

### 6. 停 / 起 / 更新

```bash
sudo docker compose down                 # 停（./data 与 ./logs 不受影响）
sudo docker compose up -d                # 起
sudo docker compose up -d --build        # 代码更新后重建
```

---

## 六、排错表

| 现象 | 原因 | 处理 |
|---|---|---|
| `404 Not Found ... bullseye-security` | 用了官方源，bullseye 已 EOL | 不要改回 `deb.debian.org`；本 Dockerfile 已用归档源 |
| 构建卡在 `python -m camoufox fetch` | 服务器直连不上 GitHub | 按第四节放离线 zip，或改用 `docker save/load` |
| `GLIBC_2.28 not found` | 在宿主机直接跑了 Python，没用镜像 | 一律 `docker compose` 里跑 |
| 容器起来就退出，日志 `no X display` | 只会在 `headless: false` 时发生 | 本项目 `crawl.headless: true`，正常不会；检查 `config.yaml` 是否被改 |
| 卡片没图 / 图片上传失败 | 缺 `FEISHU_APP_ID/SECRET` 或容器出不了外网 | `docker compose exec news-ai-science env | grep FEISHU` |
| 去重失效、每轮都推旧新闻 | `./data` 没挂上，容器重建后库丢了 | 确认 `docker-compose.yml` 的 volumes 生效 |
| 中文变方块 | 缺字体 | 镜像已装 `fonts-noto-cjk`，别用精简基础镜像重做 |
| 时间不对 | 时区 | compose 已设 `TZ=Asia/Shanghai` |

---

## 七、两条硬性约束

1. **绝不 `COPY . .`**：`.env` 会被烤进镜像层。当前只 `COPY main.py / config / src`。
2. **`.env` 永远不入库**：镜像靠 `env_file` 运行时注入。`config/config.yaml` 里的
   `app_id` / `app_secret` / `api_key` 保持空串，真实值只在宿主机 `.env`。

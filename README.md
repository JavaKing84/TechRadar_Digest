# TechRadar Digest

抓科技媒体正文 → 大模型生成中文摘要 → 飞书图文卡片推群。

需要 Python 3.11+、Git、6 GB 磁盘，能访问新闻站 / 飞书 / 大模型接口。

下面所有命令都在**项目根目录**执行，都用 `.\.venv\Scripts\python.exe`，不要用系统 `python`。
顺序不能换：依赖 → 内核 → 指纹模型 → `.env` → 试跑 → 正式跑，每一步是下一步的前提。

---

## 1. 克隆并建虚拟环境

```powershell
git clone https://gitee.com/ncepu-bj/Techradar_Digest.git; cd Techradar_Digest
python -m venv .venv
```

验证：`.\.venv\Scripts\python.exe --version` 输出 3.11 以上。

**常见问题**

| 报错 | 解决 |
|---|---|
| `'python' 不是内部或外部命令` | 用 `py -3 -m venv .venv` 代替 |
| `无法加载文件 Activate.ps1，因为在此系统上禁止运行脚本` | 不用激活，直接敲 `.\.venv\Scripts\python.exe` |
| `python --version` 低于 3.11 | 重装 Python，勾上 Add python.exe to PATH |

---

## 2. 装依赖

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

验证：`.\.venv\Scripts\python.exe -c "import camoufox, openai, yaml, bs4, lxml; print('ok')"` 输出 `ok`。

**常见问题**

| 报错 | 解决 |
|---|---|
| pip 很慢或 `ReadTimeoutError` | 命令末尾加 `-i https://mirrors.aliyun.com/pypi/simple` |
| `ModuleNotFoundError: No module named 'yaml'` | 用了系统 python，命令必须是上面那一行 |
| 装完发现 camoufox 是 0.5.9 | `.\.venv\Scripts\python.exe -m pip install camoufox==0.5.8 --force-reinstall`，再重做第 3 步 |

> camoufox 必须是 0.5.8，它对应内核 156.0.1-beta.36。装成别的版本，SDK 会把已经下好的内核判为无效。

---

## 3. 装浏览器内核

```powershell
.\.venv\Scripts\python.exe -m camoufox fetch
```

验证：`.\.venv\Scripts\python.exe -m camoufox version` 里 `Installed: Yes`。

**常见问题**

| 现象 | 解决 |
|---|---|
| 下载卡住 / 中断 / `Connection was reset` | 用下面的离线包 |
| `BadZipFile: File is not a zip file` | 下载到的是错误页，删了重下，校验不通过就别装 |
| `[FAIL] 内核包不完整，缺少 camoufox.exe` | 下成 Linux 包了，换 `win` 那个 |
| 缓存目录里文件都在，还是 `Installed: No` | 手动解压不算安装，必须跑 `install_offline_kernel.py` |
| `CamoufoxNotInstalled` | 内核没装或版本不对，重做本步 |

离线包（GitHub 下不动时）：

```powershell
curl.exe -fL -o camoufox-156.0.1-beta.36-win.x86_64.zip "https://ghfast.top/https://github.com/daijro/camoufox/releases/download/v156.0.1-beta.36/camoufox-156.0.1-beta.36-win.x86_64.zip"
(Get-Item .\camoufox-156.0.1-beta.36-win.x86_64.zip).Length   # 必须是 1311638126
.\.venv\Scripts\python.exe .docker-assets\install_offline_kernel.py .\camoufox-156.0.1-beta.36-win.x86_64.zip
```

大小对不上就是没下完或者下到了网页，别往下装。装完可以删掉 1.2 GB 的 zip。

---

## 4. 装指纹模型

```powershell
curl.exe -fL -o fpgen-model-release.zip "https://ghfast.top/https://github.com/scrapfly/fingerprint-generator/releases/download/model-2/2026/model-release.zip"
(Get-Item .\fpgen-model-release.zip).Length   # 必须是 1564571
.\.venv\Scripts\python.exe .docker-assets\install_fpgen_model.py .\fpgen-model-release.zip
```

只有 1.5 MB。不装的话，浏览器第一次启动时才会去 GitHub 拉，拉不到就抛异常，一条都抓不了。

**常见问题**

| 报错 | 解决 |
|---|---|
| `[FAIL] 模型包 sha256 不匹配` | 包下坏了，删了重下 |
| `ValueError: Unable to create a ZSTDSeekContext` | 模型数据不完整，跑下面两行 |
| `FpgenModelError` | 同上，说明自动下载也失败了 |

```powershell
Remove-Item ".\.venv\Lib\site-packages\fpgen\data" -Recurse -Force -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe .docker-assets\install_fpgen_model.py .\fpgen-model-release.zip
```

---

## 5. 配 .env

```powershell
Copy-Item .env.example .env; notepad .env
```

只填这三个，等号右边不加引号、不留空格：

| 变量 | 从哪来 |
|---|---|
| `DEEPSEEK_API_KEY` | 大模型平台控制台 |
| `FEISHU_WEBHOOK_URL` | 飞书群 → 群设置 → 群机器人 → 添加自定义机器人 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书开放平台 → 自建应用（不填卡片没配图） |

验证：`.\.venv\Scripts\python.exe main.py --stats` 输出 `{"pending": 0, ...}`。

**常见问题**

| 报错 | 解决 |
|---|---|
| `[FATAL] 配置错误: 缺少 webhook_url` | `.env` 不在项目根，或名字不是 `.env`，或值还是空的 |
| `LLM 调用失败 ... 404`，URL 里有两段 `chat/completions` | `LLM_BASE_URL` 只写到 `/v1`，不要带 `/chat/completions` |
| `LLM 调用失败 ... 401` | Key 错了或没填 |
| 改了 `.env` 不生效 | 用记事本重存；不要用 PowerShell 的 `-Encoding utf8` 写，PS 5.1 会加 BOM 导致第一个变量读不到 |
| 连不上 `202.204.64.234` | 校内网地址，校外换成公网服务并同时改 `LLM_BASE_URL`、`LLM_MODEL` |

`LLM_BASE_URL` 正确写法：默认留空走 DeepSeek，私有部署写 `http://ip:port/v1`，末尾只能是 `/v1`。

---

## 6. 验证 webhook

```powershell
.\.venv\Scripts\python.exe main.py --test-push
```

去群里看有没有收到。**这一步是唯一能确认消息落在哪个群的办法**，webhook 在添加机器人时就绑死了群。

**常见问题**

| 报错 | 解决 |
|---|---|
| `19021` | 机器人开了加签，补填 `FEISHU_WEBHOOK_SECRET` |
| `19024` / `chat not found` | 机器人被删了，重新添加并换 URL |
| 收到了但不在预期的群 | 换 `.env` 里的 `FEISHU_WEBHOOK_URL` |

---

## 7. 全链路演练，不发群

```powershell
.\.venv\Scripts\python.exe main.py --once --dry-run
```

正常会看到抓取、摘要、配图上传，最后把卡片写到 `logs\card_dryrun_*.json`，群里不会有消息。

**常见问题**

| 现象 | 解决 |
|---|---|
| 所有源 0 篇，日志全是 `DOMContentLoaded 未在 3000ms 内触发` | 新闻站要代理：`$env:HTTPS_PROXY = "http://127.0.0.1:7890"` 后重跑 |
| `Target page, context or browser has been closed` | 内核或指纹模型没装好，回第 3、4 步 |
| 一直 404 重试 | `LLM_BASE_URL` 写多了，见第 5 步 |
| `本轮无新文章入库` | 正常去重，等新文章 |

---

## 8. 正式跑

```powershell
.\.venv\Scripts\python.exe main.py --once
```

群里收到卡片就通了。

**常见问题**

| 现象 | 解决 |
|---|---|
| 卡片没有配图 | `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 没填，或应用没发布 |
| 每轮推的都是旧新闻 | `data\` 被删了，去重库丢了，别删它 |
| 定时任务报「配置文件不存在」 | 任务计划程序的「起始于」必须填项目根目录 |

日志在 `logs\bot.log`。

---

## 附：其他参数

| 参数 | 作用 |
|---|---|
| `--stats` | 看队列 |
| `--no-push` | 只抓取入库，不推送 |
| `--push-only` | 不抓取，只推队列里待推的 |

---

## 附：Linux / Docker

容器部署见 [`docs/deployment.md`](docs/deployment.md)。Linux 裸机安装：

```bash
git clone https://gitee.com/ncepu-bj/Techradar_Digest.git
cd Techradar_Digest
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt

sudo apt-get update && sudo apt-get install -y libgtk-3-0 libdbus-glib-1-2 libxt6 libasound2 \
  libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
  libatk1.0-0 libatk-bridge2.0-0 libcups2 libnss3 libnspr4 libxfixes3 libxkbcommon0 \
  libxrender1 libxi6 libgl1 libxext6 libxcb1 fonts-noto-cjk

cp .env.example .env
./.venv/bin/python -m camoufox fetch
./.venv/bin/python -m camoufox version
./.venv/bin/python main.py --once
```

其余步骤与 Windows 相同，把 `.\.venv\Scripts\python.exe` 换成 `./.venv/bin/python`。Linux 内核包用 `*-lin*.zip`，`install_offline_kernel.py` 会自动补可执行权限。

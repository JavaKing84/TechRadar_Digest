# TechRadar Digest

抓科技媒体正文 → 大模型生成中文标题和摘要 → 飞书图文卡片推到群里。

## 1. 环境

Python 3.11 以上、Git、能访问外网、磁盘 6 GB。

## 2. 安装依赖

Windows（PowerShell）：

```powershell
git clone https://gitee.com/ncepu-bj/Techradar_Digest.git
cd Techradar_Digest
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Linux：

```bash
git clone https://gitee.com/ncepu-bj/Techradar_Digest.git
cd Techradar_Digest
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
```

Linux 还要装系统库：

```bash
sudo apt-get update && sudo apt-get install -y libgtk-3-0 libdbus-glib-1-2 libxt6 libasound2 \
  libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
  libatk1.0-0 libatk-bridge2.0-0 libcups2 libnss3 libnspr4 libxfixes3 libxkbcommon0 \
  libxrender1 libxi6 libgl1 libxext6 libxcb1 fonts-noto-cjk
```

pip 慢就给安装命令加阿里源，清华源目前取不到 camoufox，别用：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple
```

## 3. 配置 .env

Windows：

```powershell
Copy-Item .env.example .env
```

Linux：

```bash
cp .env.example .env
```

打开 .env 在等号右边填值，不加引号不留空格。

| 变量 | 必填 | 从哪来 |
|---|---|---|
| DEEPSEEK_API_KEY | 是 | 大模型 API Key |
| FEISHU_WEBHOOK_URL | 是 | 飞书群 → 群设置 → 群机器人 → 自定义机器人 |
| FEISHU_APP_ID / FEISHU_APP_SECRET | 建议 | 飞书开放平台自建应用，不填卡片没有配图 |
| LLM_BASE_URL / LLM_MODEL | 否 | 换别家模型时才改 |

## 4. 装浏览器内核

pip 只装 SDK，浏览器本体要单独装。先查：

Windows：

```powershell
.\.venv\Scripts\python.exe -m camoufox version
```

Linux：

```bash
./.venv/bin/python -m camoufox version
```

Installed: Yes 就跳到第 5 步。

下载内核包，版本号不要改。

Windows：

```powershell
curl.exe -fL -o camoufox-156.0.1-beta.36-win.x86_64.zip "https://github.com/daijro/camoufox/releases/download/v156.0.1-beta.36/camoufox-156.0.1-beta.36-win.x86_64.zip"
```

Linux：

```bash
curl -fL -o camoufox-156.0.1-beta.36-lin.x86_64.zip "https://github.com/daijro/camoufox/releases/download/v156.0.1-beta.36/camoufox-156.0.1-beta.36-lin.x86_64.zip"
```

国内慢就把 github 地址前面加上 https://ghfast.top/ ，Windows 例子：

```powershell
curl.exe -fL -o camoufox-156.0.1-beta.36-win.x86_64.zip "https://ghfast.top/https://github.com/daijro/camoufox/releases/download/v156.0.1-beta.36/camoufox-156.0.1-beta.36-win.x86_64.zip"
```

Linux 用 curl，文件名换 lin 那个。

校验并安装。testzip 必须输出 None，最后一条 Installed 必须是 Yes。

Windows：

```powershell
.\.venv\Scripts\python.exe -c "import zipfile;print(zipfile.ZipFile('camoufox-156.0.1-beta.36-win.x86_64.zip').testzip())"
.\.venv\Scripts\python.exe .docker-assets/install_offline_kernel.py camoufox-156.0.1-beta.36-win.x86_64.zip
.\.venv\Scripts\python.exe -m camoufox version
```

Linux：

```bash
./.venv/bin/python -c "import zipfile;print(zipfile.ZipFile('camoufox-156.0.1-beta.36-lin.x86_64.zip').testzip())"
./.venv/bin/python .docker-assets/install_offline_kernel.py camoufox-156.0.1-beta.36-lin.x86_64.zip
./.venv/bin/python -m camoufox version
```

首次启动浏览器还会拉一个 1.5 MB 的指纹模型，能出网会自动下。下不动就手动。

Windows 下载：

```powershell
curl.exe -fL -o fpgen-model-release.zip "https://ghfast.top/https://github.com/scrapfly/fingerprint-generator/releases/download/model-2/2026/model-release.zip"
```

Linux 下载：

```bash
curl -fL -o fpgen-model-release.zip "https://ghfast.top/https://github.com/scrapfly/fingerprint-generator/releases/download/model-2/2026/model-release.zip"
```

Windows 安装：

```powershell
.\.venv\Scripts\python.exe .docker-assets/install_fpgen_model.py fpgen-model-release.zip
```

Linux 安装：

```bash
./.venv/bin/python .docker-assets/install_fpgen_model.py fpgen-model-release.zip
```

## 5. 运行

Windows：

```powershell
.\.venv\Scripts\python.exe main.py --stats
.\.venv\Scripts\python.exe main.py --once --dry-run
.\.venv\Scripts\python.exe main.py --once
```

Linux：

```bash
./.venv/bin/python main.py --stats
./.venv/bin/python main.py --once --dry-run
./.venv/bin/python main.py --once
```

--stats 看队列。--dry-run 抓取并调模型，卡片写到 logs/card_dryrun_*.json，不发群。--once 真发群。

## 6. 测试

Windows：

```powershell
.\.venv\Scripts\python.exe -m pip install pytest ruff
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```

Linux：

```bash
./.venv/bin/python -m pip install pytest ruff
./.venv/bin/python -m pytest
./.venv/bin/python -m ruff check .
```

## 7. 常见错误

| 现象 | 处理 |
|---|---|
| 配置错误: 缺少 webhook_url | .env 里 FEISHU_WEBHOOK_URL 没填 |
| 'python' 不是内部或外部命令 | 装 Python 时没勾 Add to PATH，用 py -3 代替 python |
| ModuleNotFoundError: news_ai_science / yaml | 用了系统 python，改成 .venv 里的 python |
| CamoufoxNotInstalled 或抓取全失败 | 内核没装好，重做第 4 步 |
| FpgenModelError | 指纹模型没下来，见第 4 步最后 |
| 页面全部超时 | 新闻站要代理，给终端设 HTTPS_PROXY 环境变量 |
| 大模型 401 | .env 里 Key 没填或填错 |
| 连不上 202.204.64.234 | 校内网地址，校外换公网服务并同时改 LLM_BASE_URL |
| 卡片没有配图 | 没填 FEISHU_APP_ID / FEISHU_APP_SECRET |
| 飞书报 19021 | 机器人开了加签，补填 FEISHU_WEBHOOK_SECRET |
| .env 改了不生效 | 文件要在项目根目录，名字必须叫 .env |

日志在 logs/bot.log。

## 8. 部署到服务器

Docker 部署见 docs/deployment.md。

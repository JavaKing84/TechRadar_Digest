# ============================================================================
# news_ai_science（TechRadar_Digest）—— 容器化部署镜像
#
# [刻意不写 # syntax=docker/dockerfile:1]
#   那行会让 BuildKit 先去 Docker Hub 拉 docker/dockerfile 前置镜像，
#   在内网/无梯子环境直接构建失败（本机实测报 registry-1.docker.io 超时）。
#   本文件不使用任何 BuildKit 专属语法，去掉后兼容性更好。
#
# [为什么必须容器化] 服务器是 CentOS 7（SSH banner: OpenSSH_7.4 已确认），
#   glibc 只有 2.17；而 Camoufox 基于 Firefox 156，要求 glibc >= 2.28。
#   镜像自带 Debian bullseye 的 glibc 2.31，容器与宿主只共用内核，
#   服务器的系统库一根手指都不用动。
#
# [为什么是 3.10-slim-bullseye 而不是 3.10-slim]
#   `python:3.10-slim` 现在指向 bookworm（glibc 2.36），它会在老内核上触发
#   clone3 等新 syscall，被 CentOS 7 的 3.10 内核 / 老 docker 的 seccomp 拦掉，
#   表现为 "Operation not permitted" 的诡异崩溃。bullseye 的 glibc 2.31 可绕开。
#   实测该镜像 = Python 3.10.18 + glibc 2.31-13+deb11u13。
#
# [为什么绕开 pip install .] pyproject 声明 requires-python >= 3.11，
#   3.10 下会被元数据直接拒装；依赖改由 requirements.txt 装，
#   入口 main.py 自己把 src 注入 sys.path，功能等价。
#
# [构建]
#   docker build -t news_ai_science:1.0 .
#   若内网拉不到 Docker Hub，用镜像源覆盖基础镜像：
#   docker build --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.10-slim-bullseye \
#                -t news_ai_science:1.0 .
#
# [浏览器内核] 两条路，构建期自动二选一：
#   1) .docker-assets/ 下有 camoufox-*-lin*.zip  -> 离线安装，完全不联网
#   2) 没有 zip                                  -> python -m camoufox fetch 联网拉
#   学校服务器若出不了 GitHub，必须走第 1 条（见 docs/deployment.md）。
#
# [⚠ bullseye 已 EOL，必须换归档源]
#   Debian 11 于 2026-08 停止支持，官方 deb.debian.org 的 bullseye-security 索引
#   指向已被删除的 .deb，直接 apt-get 会 404 失败（实测复现）。解法是改用归档源：
#   官方 archive.debian.org 或 阿里云镜像（国内快）。下面默认用阿里云，
#   可按需 --build-arg APT_ARCHIVE=... 覆盖。
# ============================================================================

ARG BASE_IMAGE=python:3.10-slim-bullseye

# ---------- 阶段 1：只负责把浏览器内核装好 ----------
FROM ${BASE_IMAGE} AS kernel

# 清华源：构建期只用于装 camoufox SDK（体积很小，但走源更快更稳）
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

# 归档源 + 关闭 Valid-Until 校验（归档仓库的 Release 文件已过期）
ARG APT_ARCHIVE=https://mirrors.aliyun.com/debian-archive
RUN set -eux; \
    printf 'deb %s/debian bullseye main\ndeb %s/debian-security bullseye-security main\n' \
        "$APT_ARCHIVE" "$APT_ARCHIVE" > /etc/apt/sources.list; \
    apt-get -o Acquire::Check-Valid-Until=false update; \
    apt-get install -y --no-install-recommends -o Acquire::Check-Valid-Until=false \
        ca-certificates curl unzip xz-utils; \
    rm -rf /var/lib/apt/lists/*

# 版本必须与 requirements.txt 一致；SDK 的 browser-pin.json 决定它找哪个内核
ARG CAMOUFOX_VERSION=0.5.8
# 官方 Linux 内核包地址。国内直连 GitHub 往往只有几十 KB/s（实测 65 KB/s，
# 1.3 GB 要 5 小时，且连接会中途停滞），可用加速前缀覆盖，例如：
#   --build-arg CAMOUFOX_KERNEL_URL=https://ghfast.top/https://github.com/daijro/...
ARG CAMOUFOX_KERNEL_URL=https://github.com/daijro/camoufox/releases/download/v156.0.1-beta.36/camoufox-156.0.1-beta.36-lin.x86_64.zip
RUN pip install "camoufox==${CAMOUFOX_VERSION}"

COPY .docker-assets/ /opt/assets/

# [为什么不用 python -m camoufox fetch]
#   实测：fetch 在下载停滞/失败时会以 0 退出，构建"成功"但镜像里没有浏览器，
#   上线后才报 browser not found。这里改成 curl -fL（HTTP 失败即非零）+ 安装后
#   强制校验可执行文件存在，把问题挡在构建期。
RUN set -eux; \
    zipf="$(ls /opt/assets/camoufox-*-lin*.zip 2>/dev/null | head -1 || true)"; \
    if [ -z "$zipf" ]; then \
        echo "==> 未提供离线内核包，构建期下载：$CAMOUFOX_KERNEL_URL"; \
        curl -fL --retry 3 --retry-delay 3 -o /tmp/kernel.zip "$CAMOUFOX_KERNEL_URL"; \
        zipf=/tmp/kernel.zip; \
    else \
        echo "==> 使用离线内核包：$zipf"; \
    fi; \
    python /opt/assets/install_offline_kernel.py "$zipf"; \
    chmod +x /root/.cache/camoufox/browsers/official/*/camoufox \
             /root/.cache/camoufox/browsers/official/*/camoufox-bin; \
    du -sh /root/.cache/camoufox; \
    test -x /root/.cache/camoufox/browsers/official/*/camoufox-bin

# ---------- 阶段 2：运行镜像 ----------
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn \
    TZ=Asia/Shanghai \
    DEBIAN_FRONTEND=noninteractive \
    MOZ_HEADLESS=1

# Camoufox 是定制版 Firefox，下面这些是它的运行期依赖，缺一个就起不来。
# fonts-noto-cjk 保证中文页面的字体度量正常（抓取判定依赖真实渲染结果）。
ARG APT_ARCHIVE=https://mirrors.aliyun.com/debian-archive
RUN set -eux; \
    printf 'deb %s/debian bullseye main\ndeb %s/debian-security bullseye-security main\n' \
        "$APT_ARCHIVE" "$APT_ARCHIVE" > /etc/apt/sources.list; \
    apt-get -o Acquire::Check-Valid-Until=false update; \
    apt-get install -y --no-install-recommends -o Acquire::Check-Valid-Until=false \
      libgtk-3-0 libdbus-glib-1-2 libxt6 libasound2 libx11-xcb1 \
      libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
      libcairo2 libatk1.0-0 libatk-bridge2.0-0 libcups2 libnss3 libnspr4 \
      libxfixes3 libxkbcommon0 libxrender1 libxi6 libgl1 libxext6 libxcb1 \
      fonts-noto-cjk fonts-liberation tzdata ca-certificates procps; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖再拷代码：只改业务代码时这一层缓存仍然有效
COPY requirements.txt ./
RUN pip install -r requirements.txt

# [fpgen 指纹模型也必须离线预置]
#   实测：容器里第一次 Camoufox(...) 会去 GitHub 下载 1.5 MB 的 model-2/2026，
#   服务器出不了 GitHub 时直接抛 FpgenModelError，浏览器起不来。
#   fpgen-model-release.zip 已不随仓库分发（2026-10-28 移除）；若本地
#   .docker-assets/ 下有这个包就离线安装，否则构建期联网获取（仅构建机需外网）。
COPY .docker-assets/install_fpgen_model.py /opt/assets/
RUN set -eux; \
    if [ -f /opt/assets/fpgen-model-release.zip ]; then \
        python /opt/assets/install_fpgen_model.py /opt/assets/fpgen-model-release.zip; \
    else \
        echo "==> 无离线模型包，构建期联网获取"; \
        python -c "from camoufox.fpgen_model import ensure_fpgen_model as e; e()"; \
    fi

COPY main.py ./
COPY config ./config
COPY src ./src

# 内核从阶段 1 搬过来（SDK 在 Linux 下固定用 /root/.cache/camoufox）
COPY --from=kernel /root/.cache/camoufox /root/.cache/camoufox

# 数据与日志目录：compose 会把这些挂到宿主，重建容器不丢历史
RUN mkdir -p /app/data /app/logs

# [本项目是"一次性批处理"，不是常驻服务]
#   容器内跑 while：跑一轮 -> 睡 RUN_INTERVAL_SECONDS -> 再跑
#   因此 restart: unless-stopped 才有意义（进程意外退出会被拉起，
#   而不是变成"秒退-重启"的死循环去刷群消息）。
#   只想跑一次就退出：
#     docker compose run --rm news-ai-science python main.py --once
CMD ["sh", "-c", "while true; do echo \"[$(date '+%F %T')] ---- 开始一轮 ----\"; python main.py --once || echo '[warn] 本轮失败，等待下一轮'; sleep ${RUN_INTERVAL_SECONDS:-43200}; done"]

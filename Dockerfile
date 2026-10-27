# syntax=docker/dockerfile:1
# ============================================================================
# news_ai_science —— 自包含部署镜像
#
# [为什么用镜像] 目标服务器 CentOS 7 的 glibc 只有 2.17，而 Camoufox 基于
#   Firefox 156，要求 glibc >= 2.28。镜像自带 Debian bullseye 的 glibc 2.31，
#   容器与宿主只共用内核 —— 既保留 Camoufox，又不动服务器任何系统库。
#
# [为什么是 bullseye 不是 bookworm] glibc 2.31 对 CentOS 7 的 3.10 内核更友好，
#   避开新 syscall（clone3 等）在老内核/老 docker 的 seccomp 下被拦的问题。
#
# [构建]
#   docker build -t news_ai_science:1.0 .
#   （若 assets/camoufox-lin.zip 存在则完全离线装内核，否则构建期联网拉官方 Linux 版）
# ============================================================================

# ---------- 阶段 1：只负责把浏览器内核装好 ----------
FROM python:3.11-slim-bullseye AS kernel

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl unzip xz-utils \
 && rm -rf /var/lib/apt/lists/*

# 版本必须与 runtime 阶段一致；SDK 的 browser-pin 决定它找哪个内核版本
ARG CAMOUFOX_SDK_VERSION=0.5.8
RUN pip install "camoufox==${CAMOUFOX_SDK_VERSION}"

COPY .docker-assets/ /opt/assets/

RUN if [ -f /opt/assets/camoufox-lin.zip ]; then \
        echo "==> 离线安装内核（不访问 GitHub）" && \
        python /opt/assets/install_offline_kernel.py /opt/assets/camoufox-lin.zip ; \
    else \
        echo "==> 构建期联网拉取官方 Linux 内核" && \
        python -m camoufox fetch ; \
    fi \
 && du -sh /root/.cache/camoufox \
 && find /root/.cache/camoufox -maxdepth 6 -name 'camoufox' -type f -exec chmod +x {} \;

# ---------- 阶段 2：运行镜像 ----------
FROM python:3.11-slim-bullseye

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    DEBIAN_FRONTEND=noninteractive

# Camoufox 是定制版 Firefox，这些是它的运行期依赖，缺一个就起不来
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libgtk-3-0 libdbus-glib-1-2 libxt6 libasound2 libx11-xcb1 \
      libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
      libcairo2 libatk1.0-0 libatk-bridge2.0-0 libcups2 libnss3 libnspr4 \
      libxfixes3 libxkbcommon0 libxrender1 libxi6 libgl1 libxext6 \
      fonts-noto-cjk fonts-liberation tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖再拷代码：只改业务代码时这层缓存仍然有效
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY main.py ./
COPY config ./config

# 内核从阶段 1 搬过来（缓存目录固定 /root/.cache/camoufox）
COPY --from=kernel /root/.cache/camoufox /root/.cache/camoufox

# 容器内没有 X，强制无头（config.yaml 里 headless 已是 true）
ENV MOZ_HEADLESS=1

ENTRYPOINT ["python", "main.py"]
CMD ["--stats"]

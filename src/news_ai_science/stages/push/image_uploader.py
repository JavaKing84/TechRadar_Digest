# -*- coding: utf-8 -*-
"""
=====================================================================
[文件] news_ai_science/feishu_image.py
[职责] 把新闻封面图变成飞书卡片能用的 img_key：
      应用凭证换 tenant_access_token -> 下载图片 -> 调 /open-apis/im/v1/images 上传。
=====================================================================
"""
from __future__ import annotations

import time

import requests

from news_ai_science.core.logger import get_logger

logger = get_logger(__name__)

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
IMAGE_UPLOAD_URL = "https://open.feishu.cn/open-apis/im/v1/images"

# [表面] 图片字节上限
# [本质] 飞书 im/v1/images 单片上限 10MB，超了直接返回 234001；
#        新闻配图极少超过 2MB，超限的基本是抓错了资源（视频/整页截图）
# [后果] 不在无意义的超大文件上浪费一次上传往返
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# [表面] 部分站点对无 Referer 的请求返回 403
# [本质] 伪装成浏览器直连（不带 Referer，避免被判定为盗链）
# [后果] CDN 防盗链宽松的站点（TechCrunch/36氪）能正常取到图
DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
}


class FeishuImageUploader:
    """
    # [表面] 上传器：url -> img_key
    # [本质] 卡片 img 组件只认 img_key，不接受外链 URL；而拿 img_key 必须走
    #        「自建应用」的 tenant_access_token（群机器人 webhook 没有上传权限）。
    #        所以本类整体是"可缺席"的组件：凭证没配就 enabled=False，
    #        上层自动降级为无图卡片，而不是报错中断推送
    # [后果] 有没有图都不影响推送链路，配了凭证立刻生效
    """

    def __init__(
        self,
        app_id: str = "",
        app_secret: str = "",
        *,
        timeout: float = 10.0,
        session: requests.Session | None = None,
    ) -> None:
        self.app_id = (app_id or "").strip()
        self.app_secret = (app_secret or "").strip()
        self.timeout = timeout
        self.session = session or requests.Session()
        # [表面] token 缓存与其过期时间戳
        # [本质] tenant_access_token 有效期 2 小时，单轮推送里可能传多张图，
        #        每张都去换一次 token 属于纯浪费，还会撞频控
        # [后果] 一轮推送只换一次 token
        self._token: str | None = None
        self._token_expire_at: float = 0.0

    @property
    def enabled(self) -> bool:
        """凭证齐全才认为图片能力可用；缺一个就走无图降级。"""
        return bool(self.app_id and self.app_secret)

    def _get_token(self) -> str | None:
        """
        # [表面] 拿（或复用）tenant_access_token
        # [本质] 提前 60 秒判定过期，避免"刚好在请求途中过期"的边界失败
        # [后果] 连续上传多张图只换一次 token，稳定且省频控额度
        """
        now = time.time()
        if self._token and now < self._token_expire_at:
            return self._token
        try:
            resp = self.session.post(
                TOKEN_URL,
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            logger.warning("飞书 tenant_access_token 请求失败: %s", exc)
            return None
        except ValueError as exc:
            logger.warning("飞书 tenant_access_token 响应不是 JSON: %s", exc)
            return None

        if data.get("code") != 0:
            logger.warning(
                "飞书 tenant_access_token 获取失败 code=%s msg=%s（检查 FEISHU_APP_ID / FEISHU_APP_SECRET）",
                data.get("code"),
                data.get("msg"),
            )
            return None

        token = data.get("tenant_access_token")
        if not token:
            logger.warning("飞书返回体里没有 tenant_access_token")
            return None
        expire = int(data.get("expire", 7200) or 7200)
        self._token = token
        self._token_expire_at = now + max(expire - 60, 60)
        return token

    def _download(self, image_url: str) -> tuple[bytes, str] | None:
        """
        # [表面] 下载图片字节并推断扩展名
        # [本质] 扩展名必须跟真实格式一致——飞书按文件名后缀校验格式，
        #        把 png 存成 .jpg 会被判"图片格式不支持"
        # [后果] Content-Type 缺失时退回 jpg，兼容大多数 CDN
        """
        if not image_url:
            return None
        try:
            resp = self.session.get(image_url, headers=DOWNLOAD_HEADERS, timeout=self.timeout)
            resp.raise_for_status()
            content = resp.content
        except requests.RequestException as exc:
            logger.warning("配图下载失败: %s | %s", image_url[:80], exc)
            return None

        if not content:
            logger.warning("配图下载为空: %s", image_url[:80])
            return None
        if len(content) > MAX_IMAGE_BYTES:
            logger.warning("配图超过 10MB 上限，跳过: %s", image_url[:80])
            return None

        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "png" in ctype:
            ext = "png"
        elif "gif" in ctype:
            ext = "gif"
        elif "webp" in ctype:
            ext = "webp"
        elif "bmp" in ctype:
            ext = "bmp"
        else:
            ext = "jpg"
        return content, ext

    def upload_from_url(self, image_url: str, *, image_type: str = "message") -> str | None:
        """
        # [表面] 外链图片 -> img_key
        # [本质] 三个阶段任一失败都返回 None（没凭证/下载失败/上传被拒），
        #        由 card_builder 决定是带图还是无图，调用方无需 try
        # [后果] 图片是"锦上添花"，永远不会成为推送失败的原因
        """
        if not self.enabled:
            return None
        if not image_url:
            return None

        token = self._get_token()
        if token is None:
            return None

        downloaded = self._download(image_url)
        if downloaded is None:
            return None
        content, ext = downloaded

        try:
            resp = self.session.post(
                IMAGE_UPLOAD_URL,
                headers={"Authorization": "Bearer " + token},
                data={"image_type": image_type},
                files={"image": (f"cover.{ext}", content, f"image/{ext}")},
                timeout=self.timeout,
            )
            # [表面] 不先 raise_for_status，先尝试解析响应体
            # [本质] 飞书的权限类错误（如 code 99991672 缺 im:resource:upload）
            #        是以 HTTP 400 + JSON 体返回的，直接 raise 只会得到
            #        "400 Bad Request"，把真正的原因（缺哪个权限）吞掉
            # [后果] 日志里能直接看到"该开哪个权限"，不用再手动复现
            payload = resp.json()
        except ValueError as exc:
            logger.warning("飞书图片上传响应不是 JSON: %s", exc)
            return None
        except requests.RequestException as exc:
            logger.warning("飞书图片上传失败: %s", exc)
            return None

        if payload.get("code") != 0:
            logger.warning(
                "飞书图片上传被拒 code=%s msg=%s（code 99991672 表示应用缺 im:resource:upload 权限）",
                payload.get("code"),
                payload.get("msg"),
            )
            return None

        image_key = (payload.get("data") or {}).get("image_key")
        if not image_key:
            logger.warning("飞书返回体里没有 image_key")
            return None
        logger.info("配图上传成功 img_key=%s", image_key)
        return image_key

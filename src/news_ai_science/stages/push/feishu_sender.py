# -*- coding: utf-8 -*-
"""
=====================================================================
[文件] news_ai_job/feishu_sender.py
[职责] 与飞书 webhook 通信的唯一模块：HMAC-SHA256 加签、发送、限流重试、
      错误码分支、回执解析。本文件不知道「岗位」是什么。
=====================================================================
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

import requests

from news_ai_science.core.logger import get_logger

logger = get_logger(__name__)

# [表面] 值得退避重试的错误码
# [本质] 这些是「过会儿再试可能就好」的临时错误（频率限制等）；
#        签名错、webhook 失效重试一万次也一样，直接判失败
# [后果] 重试预算只花在有机会成功的地方
RETRYABLE_CODES = {11232, 9499, 99991672}


def gen_sign(timestamp: int, secret: str) -> str:
    """
    # [表面] 飞书加签算法
    # [本质] string_to_sign = "{timestamp}\\n{secret}"，HMAC-SHA256 的 key 和
    #        消息都是 string_to_sign 本身，结果再 base64——与企业微信
    #        「以 secret 为 key」不同，别混
    # [后果] 逐字节照官方示例实现，不会因签名错被拒
    """
    string_to_sign = "{}\n{}".format(timestamp, secret)
    hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256)
    hmac_code.update(string_to_sign.encode("utf-8"))
    return base64.b64encode(hmac_code.digest()).decode("utf-8")


class RateLimiter:
    """最小间隔限流：飞书群机器人 100 次/分钟，本项目远远用不到，但留个闸。"""

    def __init__(self, min_interval: float = 1.0) -> None:
        self.min_interval = min_interval
        self._last = 0.0

    def acquire(self) -> None:
        gap = time.time() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.time()


class FeishuSender:
    """
    # [表面] 发送器：send_card（业务唯一通道）/ send_text（仅连通性探针）
    # [本质] 依赖注入（webhook / secret / limiter），不读全局配置——
    #        单测可以塞假 limiter 和 mock 的 requests.post
    # [后果] 单元测试不产生真实网络请求
    """

    def __init__(self, webhook: str, secret: str = "", limiter: RateLimiter | None = None,
                 chat_name: str = "") -> None:
        self.webhook = (webhook or "").strip()
        self.secret = (secret or "").strip()
        self.limiter = limiter
        # [表面] 目标群名只用于日志与回执，不参与寻址
        # [本质] 群机器人 webhook 在创建时就已经绑定到某一个群，飞书不提供
        #        「按群名投递」的能力；写清群名是为了让日志能一眼看出这条
        #        消息「应该」落在哪，换错 webhook 时对不上号立刻暴露
        # [后果] 日志形如「已投递到群「iDeal工作室」」，群名与实际收到的群不一致时好排查
        self.chat_name = (chat_name or "").strip()

    def sign(self, timestamp: int | None = None) -> tuple[int, str] | None:
        """生成 (timestamp, sign)；未配 secret 返回 None 走免签。"""
        if not self.secret:
            return None
        if timestamp is None:
            timestamp = int(time.time())
        return timestamp, gen_sign(timestamp, self.secret)

    def _post(self, payload: dict) -> dict:
        """POST 一次 webhook；异常/非 JSON 归一化成 {code:-1}。"""
        if self.limiter is not None:
            self.limiter.acquire()
        try:
            resp = requests.post(self.webhook, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                return {"code": -1, "msg": "响应不是 JSON 对象"}
            return data
        except requests.RequestException as exc:
            logger.error("飞书请求异常: %s", exc)
            return {"code": -1, "msg": f"网络异常: {exc}"}
        except ValueError as exc:
            logger.error("飞书响应解析失败: %s", exc)
            return {"code": -1, "msg": "响应解析失败"}

    def send_with_retry(self, payload: dict, times: int = 3) -> dict:
        """带退避重试的发送；只有 RETRYABLE_CODES 才重试。"""
        last = {"code": -1, "msg": "未发送"}
        for attempt in range(times):
            signed = dict(payload)
            if self.secret:
                timestamp, sign = self.sign()
                signed["timestamp"] = str(timestamp)
                signed["sign"] = sign
            result = self._post(signed)
            last = result
            code = result.get("code", -1)
            if code == 0:
                data = result.get("data") or {}
                logger.info(
                    "[Feishu] 已投递到群「%s」 message_id=%s",
                    self.chat_name or "未命名群", data.get("message_id"),
                )
                return {
                    "code": 0,
                    "msg": result.get("msg", "success"),
                    "message_id": data.get("message_id"),
                }
            if code in RETRYABLE_CODES and attempt < times - 1:
                wait = 2.0 * (attempt + 1)
                logger.warning(
                    "飞书临时错误 code=%s，%.1fs 后重试(%d/%d)",
                    code, wait, attempt + 1, times - 1,
                )
                time.sleep(wait)
                continue
            logger.error(
                "飞书发送失败 code=%s msg=%s（目标群「%s」）",
                code, result.get("msg"), self.chat_name or "未命名群",
            )
            return {"code": code, "msg": result.get("msg", "unknown"), "message_id": None}
        return {"code": last.get("code", -1), "msg": last.get("msg", "unknown"), "message_id": None}

    def send_card(self, card: dict) -> dict:
        """发送 interactive 卡片（卡片 2.0 结构，schema 字段在 card 里）。"""
        return self.send_with_retry({"msg_type": "interactive", "card": card})

    def send_text(self, content: str) -> dict:
        """
        # [表面] 纯文本消息
        # [本质] **仅供 `--test-push` 连通性探针使用**，业务推送一律走 send_card。
        #        历史上它还被当作「卡片发不出去时的降级通道」，结果是群里同一批内容
        #        出现「纯文本 + 卡片」两条消息，看起来像重复推送。该降级已删除：
        #        卡片失败就记 ERROR 并保持 pending，下一轮重发
        # [后果] 定时任务里永远不会有 text 类型的消息发出
        """
        return self.send_with_retry({"msg_type": "text", "content": {"text": content}})

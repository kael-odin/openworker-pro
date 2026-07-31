"""企业微信自建应用 API 封装 —— access_token 缓存 + 消息收发。

access_token 管理：
  - 企微 access_token 有效期 7200 秒（2 小时），用 corpid + corpsecret 获取。
  - 本类维护内存缓存（_token / _expires_at），过期前 300 秒提前刷新（防边界）。
  - 不持久化到 SecretStore —— token 短期有效，每次启动重新获取即可。

消息发送（出站）：
  - 文本消息：POST /cgi-bin/message/send?access_token=... {touser, msgtype:"text", agent_id, text:{content}}
  - 文本卡片：msgtype:"textcard"，markdown：msgtype:"markdown"
  - chat_id 由 inbound 回调的 FromUserName（用户 userid）携带。

消息接收（入站）：
  - 回调由 server/app.py 的 webhook 端点接收，解密后调 provider.parse_inbound()
  - 本模块只负责把解密后的 XML 转成 MessageEvent，不直接监听 HTTP。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

from ..base import MessageEvent, MessageType, SessionSource
from . import crypto

logger = logging.getLogger("coworker.connectors.wecom")

QYAPI_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
# access_token 有效期 7200s，提前 300s 刷新防边界。
_TOKEN_REFRESH_MARGIN = 300


class WeComAppClient:
    """企微自建应用 API 客户端：access_token 缓存 + 出站消息发送。"""

    def __init__(
        self,
        *,
        corpid: str,
        secret: str,
        agent_id: str,
        token: str = "",  # 回调校验 Token（非 access_token）
        encoding_aes_key: str = "",
    ) -> None:
        if not corpid or not secret or not agent_id:
            raise ValueError("corpid / secret / agent_id 不能为空")
        self.corpid = corpid
        self.secret = secret
        self.agent_id = agent_id
        self.token = token  # 回调签名校验用 Token
        self.encoding_aes_key = encoding_aes_key
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    # -- access_token ----------------------------------------------------------
    def _get_access_token(self) -> str:
        """获取（或复用缓存的）access_token。过期则重新获取。

        同步 httpx —— 在 adapter.send() 或 adapter.connect() 里通过 asyncio.to_thread
        调用，避免阻塞事件循环。token 缓存在实例内存，进程生命周期内有效。
        """
        now = time.time()
        if self._access_token and now < self._token_expires_at - _TOKEN_REFRESH_MARGIN:
            return self._access_token
        # 重新获取
        try:
            resp = httpx.get(
                f"{QYAPI_BASE}/gettoken",
                params={"corpid": self.corpid, "corpsecret": self.secret},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            err = f"获取 access_token 失败: {exc}"
            logger.error(err)
            raise RuntimeError(err)
        if data.get("errcode") != 0:
            err = f"企微返回错误: {data.get('errmsg')} (code={data.get('errcode')})"
            logger.error(err)
            raise RuntimeError(err)
        self._access_token = data["access_token"]
        self._token_expires_at = now + int(data.get("expires_in", 7200))
        logger.info("wecom access_token 刷新成功，有效期 %ss", data.get("expires_in"))
        return self._access_token

    def invalidate_token(self) -> None:
        """强制下次重新获取 access_token（token 失效时调用）。"""
        self._access_token = None
        self._token_expires_at = 0.0

    # -- 出站消息发送 ----------------------------------------------------------
    def send_text(self, userid: str, content: str) -> dict[str, Any]:
        """给指定用户（userid）发文本消息。返回企微 API 响应 dict。"""
        token = self._get_access_token()
        payload = {
            "touser": userid,
            "msgtype": "text",
            "agentid": int(self.agent_id),
            "text": {"content": content},
        }
        return self._post_message(token, payload)

    def send_markdown(self, userid: str, content: str) -> dict[str, Any]:
        """发 markdown 消息（企微 markdown 不支持复杂语法，但换行/加粗/链接可用）。"""
        token = self._get_access_token()
        payload = {
            "touser": userid,
            "msgtype": "markdown",
            "agentid": int(self.agent_id),
            "markdown": {"content": content},
        }
        return self._post_message(token, payload)

    def _post_message(self, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = httpx.post(
                f"{QYAPI_BASE}/message/send",
                params={"access_token": token},
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            err = f"企微消息发送失败: {exc}"
            logger.error(err)
            return {"errcode": -1, "errmsg": err}
        # 42001: access_token 过期 —— 清缓存让下次重试。
        if data.get("errcode") in (40014, 42001):
            logger.info("wecom access_token 失效，已清缓存待重试")
            self.invalidate_token()
        return data

    # -- 入站消息解析 ----------------------------------------------------------
    def parse_inbound_xml(self, xml_str: str) -> MessageEvent:
        """把解密后的企微回调 XML 解析成 MessageEvent。

        企微文本消息 XML 字段：
          ToUserName (企业 corpid) / FromUserName (用户 userid) / CreateTime /
          MsgType (text/image/...) / Content / MsgId / AgentID
        事件（如关注/菜单点击）：MsgType=event，Event=subscribe/CLICK/...
        """
        fields = crypto.parse_callback_xml(xml_str)
        userid = fields.get("FromUserName", "")
        msg_type = fields.get("MsgType", "")
        content = fields.get("Content", "")
        msg_id = fields.get("MsgId") or fields.get("CreateTime")
        event = fields.get("Event", "")

        # event 类型消息（如用户关注应用、点击菜单）—— 文本化为可读描述。
        if msg_type == "event" and event:
            text = f"[企微事件] {event}"
            if userid:
                text += f"（来自 {userid}）"
        elif msg_type == "text":
            text = content
        else:
            # 图片/语音/视频/文件等非文本 —— 占位描述，agent 暂不处理二进制。
            text = f"[企微消息类型: {msg_type}]"

        return MessageEvent(
            text=text,
            source=SessionSource(
                platform="wecom",
                chat_id=userid,  # 企微应用消息以 userid 为会话标识（1v1 私聊）
                user_id=userid,
                user_name=userid,  # 企微回调不含用户名，用 userid 占位（可后续调通讯录补全）
                chat_type="dm",  # 自建应用消息都是 1v1 私聊
                team_id=self.corpid,  # 企业标识，allowlist 按企业隔离
            ),
            message_id=msg_id,
            message_type=MessageType.TEXT,
            raw=fields,
            mentions_me=True,  # 企微应用消息等同 @bot —— 必须回复
        )

    # -- 回调校验 + 解密 -------------------------------------------------------
    def verify_and_decrypt(self, query: dict[str, str], body: bytes) -> tuple[bool, str]:
        """校验企微回调签名并解密，返回 (ok, plaintext_or_echostr)。

        企微回调两步：
          1. URL 验证（GET）：query 带 msg_signature/timestamp/nonce/echostr，
             签名校验通过后原样返回 echostr 明文。
          2. 消息接收（POST）：body 是 XML，内含 Encrypt 字段，解密后得消息 XML。

        本方法处理两种：若 body 为空且 query 有 echostr → 验证模式；否则解密 POST body。
        """
        sig = query.get("msg_signature", "")
        ts = query.get("timestamp", "")
        nonce = query.get("nonce", "")
        echostr = query.get("echostr", "")

        # 模式 1：URL 验证（GET echostr）
        if echostr and not body:
            if not self.token:
                return True, echostr  # 明文模式不校验，直接回 echostr
            if not crypto.verify_signature(self.token, ts, nonce, echostr, sig):
                return False, "签名校验失败"
            # echostr 本身是 base64 密文，需解密后返回明文（企微验证流程要求）
            if self.encoding_aes_key:
                try:
                    plain, _ = crypto.decrypt_message(echostr, self.encoding_aes_key, self.corpid)
                    return True, plain
                except ValueError as exc:
                    return False, f"echostr 解密失败: {exc}"
            return True, echostr

        # 模式 2：消息接收（POST）
        if not body:
            return False, "空 body"

        xml_dict = crypto.parse_callback_xml(body)
        encrypt = xml_dict.get("Encrypt", "")
        if not encrypt:
            # 明文模式：XML 直接是消息体（无 Encrypt 字段）
            return True, body.decode("utf-8", errors="replace")

        if not self.token or not self.encoding_aes_key:
            return False, "收到加密消息但未配置 Token/EncodingAESKey"

        if not crypto.verify_signature(self.token, ts, nonce, encrypt, sig):
            return False, "签名校验失败"
        try:
            plain, corpid = crypto.decrypt_message(encrypt, self.encoding_aes_key, self.corpid)
            return True, plain
        except ValueError as exc:
            return False, f"消息解密失败: {exc}"

    # -- 主动验证 token（配置面板"测试"按钮用）-----------------------------------
    def validate_credentials(self) -> dict[str, Any]:
        """校验 corpid/secret 能否获取 access_token（配置测试按钮用）。"""
        try:
            token = self._get_access_token()
            return {"ok": True, "access_token_preview": token[:8] + "...", "agent_id": self.agent_id}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def client_from_profile(profile: dict[str, Any]) -> Optional[WeComAppClient]:
    """从 SecretStore profile 构造 WeComAppClient。缺关键字段返回 None。"""
    corpid = profile.get("corpid")
    secret = profile.get("secret")
    agent_id = profile.get("agent_id")
    if not (corpid and secret and agent_id):
        return None
    return WeComAppClient(
        corpid=corpid,
        secret=secret,
        agent_id=agent_id,
        token=profile.get("token", ""),
        encoding_aes_key=profile.get("encoding_aes_key", ""),
    )

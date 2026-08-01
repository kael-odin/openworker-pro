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

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from ..base import InteractionEvent, MessageEvent, MessageType, SessionSource
from . import crypto

logger = logging.getLogger("coworker.connectors.wecom")

QYAPI_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
# access_token 有效期 7200s，提前 300s 刷新防边界。
_TOKEN_REFRESH_MARGIN = 300
_CALLBACK_TIMESTAMP_TOLERANCE_SECONDS = 300
_REPLAY_TTL_SECONDS = 600
_REPLAY_CACHE_MAX = 2048


@dataclass(frozen=True)
class CallbackVerification:
    ok: bool
    payload: str = ""
    error_code: str = ""
    replay_key: str = ""


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
        token = (token or "").strip()
        encoding_aes_key = (encoding_aes_key or "").strip()
        if bool(token) != bool(encoding_aes_key):
            raise ValueError("回调 Token 与 EncodingAESKey 必须同时配置")
        if encoding_aes_key:
            crypto._decode_aes_key(encoding_aes_key)
        self.corpid = corpid
        self.secret = secret
        self.agent_id = agent_id
        self.token = token
        self.encoding_aes_key = encoding_aes_key
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = threading.Lock()
        self._callback_replays: OrderedDict[str, float] = OrderedDict()

    # -- access_token ----------------------------------------------------------
    def _get_access_token(self) -> str:
        """获取或复用 token；锁内二次检查避免并发刷新风暴。"""
        now = time.time()
        if self._access_token and now < self._token_expires_at - _TOKEN_REFRESH_MARGIN:
            return self._access_token
        with self._token_lock:
            now = time.time()
            if self._access_token and now < self._token_expires_at - _TOKEN_REFRESH_MARGIN:
                return self._access_token
            return self._refresh_access_token(now)

    def _refresh_access_token(self, now: float) -> str:
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

    def send_template_card(
        self, userid: str, title: str, body: str, buttons
    ) -> dict[str, Any]:
        """发 button_list 模板卡片 —— 审批/问答的按钮交互载体。

        每个按钮的 ``key`` 就是 ``interactions.encode`` 产出的不透明 value（item_id +
        resolution），点击后企微回调 ``template_card_event`` 把 key 带回来，adapter 转成
        InteractionEvent，走与 Slack/Telegram 同一条解析路径。``task_id`` 用第一个按钮的
        value 派生，使 update_taskcard 能在 resolve 后替换按钮为结果文本。
        """
        token = self._get_access_token()
        from ...interactions import decode

        # task_id binds this card so update_taskcard can target it later. Derive from the
        # first button's decoded item_id — stable per Inbox item.
        first_item_id = ""
        for b in buttons or []:
            decoded = decode(str(getattr(b, "value", "") or ""))
            if decoded:
                first_item_id = decoded[0]
                break
        task_id = f"ow_{first_item_id}"[:40] or "ow_card"
        button_list = [
            {
                "text": str(getattr(b, "label", ""))[:20],
                "style": 1,  # 1 = 主色（绿/蓝），2 = 次色
                "key": str(getattr(b, "value", "")),
            }
            for b in (buttons or [])
        ]
        payload = {
            "touser": userid,
            "msgtype": "template_card",
            "agentid": int(self.agent_id),
            "template_card": {
                "card_type": "button_list",
                "source": {"icon_url": "", "desc": "OpenWorker"},
                "main_title": {"title": title[:20]},
                "sub_title_text": (body or "")[:200],
                "task_id": task_id,
                "button_list": button_list,
            },
        }
        return self._post_message(token, payload)

    def update_taskcard(
        self, userid: str, task_id: str, outcome_text: str
    ) -> dict[str, Any]:
        """把已 resolve 的审批卡片按钮替换为一条结果文本（企微不支持 editMessageText，
        只能把 button_list 换成一个 disabled 状态的按钮）。"""
        token = self._get_access_token()
        payload = {
            "userids": [userid],
            "agentid": int(self.agent_id),
            "task_id": task_id,
            "replaced_button_key": "ow_resolved",
            "button_list": [
                {
                    "text": (outcome_text or "已处理")[:20],
                    "style": 3,  # 3 = 灰色 disabled 视感
                    "key": "ow_resolved",
                }
            ],
        }
        try:
            resp = httpx.post(
                f"{QYAPI_BASE}/message/update_taskcard",
                params={"access_token": token},
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            err = f"企微更新卡片失败: {exc}"
            logger.error(err)
            return {"errcode": -1, "errmsg": err}
        if data.get("errcode") in (40014, 42001):
            self.invalidate_token()
        return data

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

    def parse_template_card_event(self, xml_str: str) -> Optional[InteractionEvent]:
        """If the decrypted callback XML is a template_card button click, return the
        InteractionEvent for it; otherwise None. The button's encoded value rides in the
        ``ButtonKeys`` field (button_list card type). Falls back to ``SelectedItems`` /
        ``EventKey`` for other card variants."""
        fields = crypto.parse_callback_xml(xml_str)
        if fields.get("MsgType") != "event" or fields.get("Event") != "template_card_event":
            return None
        userid = fields.get("FromUserName", "")
        # button_list cards put the selected key in ButtonKeys; other card types use
        # SelectedItems (JSON with SelectedKey) or EventKey. Take the first non-empty.
        value = (
            fields.get("ButtonKeys")
            or fields.get("EventKey")
            or ""
        ).strip()
        if not value:
            sel = fields.get("SelectedItems", "")
            # SelectedItems may be JSON like [{"SelectedKey":"..."}]; extract loosely.
            import json as _json

            try:
                items = _json.loads(sel)
                if isinstance(items, list) and items:
                    value = str(items[0].get("SelectedKey", "")).strip()
            except (ValueError, TypeError):
                pass
        if not value:
            return None
        task_id = fields.get("TaskId", "")
        return InteractionEvent(
            platform="wecom",
            chat_id=userid,
            message_id=task_id,  # task_id is how update_message targets the card
            value=value,
            user_id=userid,
            user_name=userid,
            team_id=self.corpid,
        )

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
        msg_id = fields.get("MsgId") or None
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

    @property
    def inbound_state(self) -> str:
        """``encrypted`` 或 ``outbound_only``；不再提供不安全的明文入站。"""
        return "encrypted" if self.token and self.encoding_aes_key else "outbound_only"

    @staticmethod
    def _timestamp_is_fresh(timestamp: str, now: Optional[float] = None) -> bool:
        try:
            value = int(timestamp)
        except (TypeError, ValueError):
            return False
        current = time.time() if now is None else now
        return abs(current - value) <= _CALLBACK_TIMESTAMP_TOLERANCE_SECONDS

    def _consume_replay_key(self, timestamp: str, nonce: str, signature: str) -> tuple[bool, str]:
        now = time.time()
        cutoff = now - _REPLAY_TTL_SECONDS
        while self._callback_replays:
            _, seen_at = next(iter(self._callback_replays.items()))
            if seen_at >= cutoff:
                break
            self._callback_replays.popitem(last=False)
        raw = f"{timestamp}\0{nonce}\0{signature}".encode("utf-8")
        key = hashlib.sha256(raw).hexdigest()
        if key in self._callback_replays:
            return False, key
        self._callback_replays[key] = now
        self._callback_replays.move_to_end(key)
        while len(self._callback_replays) > _REPLAY_CACHE_MAX:
            self._callback_replays.popitem(last=False)
        return True, key

    # -- 回调校验 + 解密 -------------------------------------------------------
    def verify_and_decrypt(self, query: dict[str, str], body: bytes) -> CallbackVerification:
        """严格校验加密企微回调；公开错误只返回稳定的脱敏错误码。"""
        if self.inbound_state != "encrypted":
            return CallbackVerification(False, error_code="callback_not_configured")

        sig = (query.get("msg_signature") or "").strip()
        ts = (query.get("timestamp") or "").strip()
        nonce = (query.get("nonce") or "").strip()
        echostr = (query.get("echostr") or "").strip()
        if not sig or not nonce or not self._timestamp_is_fresh(ts):
            return CallbackVerification(False, error_code="invalid_callback_metadata")

        if echostr and not body:
            encrypted = echostr
        else:
            if not body:
                return CallbackVerification(False, error_code="empty_body")
            try:
                xml_dict = crypto.parse_callback_xml(body)
            except ValueError:
                return CallbackVerification(False, error_code="invalid_xml")
            encrypted = xml_dict.get("Encrypt", "")
            if not encrypted:
                return CallbackVerification(False, error_code="plaintext_not_allowed")

        if (
            len(sig) != 40
            or len(ts) > 16
            or len(nonce) > 256
            or len(encrypted) > 1_048_576
        ):
            return CallbackVerification(False, error_code="invalid_callback_metadata")
        if not crypto.verify_signature(self.token, ts, nonce, encrypted, sig):
            return CallbackVerification(False, error_code="invalid_signature")
        fresh, replay_key = self._consume_replay_key(ts, nonce, sig)
        if not fresh:
            return CallbackVerification(False, error_code="replay", replay_key=replay_key)
        try:
            plain, _ = crypto.decrypt_message(
                encrypted, self.encoding_aes_key, self.corpid
            )
        except (ValueError, TypeError):
            return CallbackVerification(
                False, error_code="decrypt_failed", replay_key=replay_key
            )
        return CallbackVerification(True, payload=plain, replay_key=replay_key)

    # -- 主动验证 token（配置面板"测试"按钮用）-----------------------------------
    def validate_credentials(self) -> dict[str, Any]:
        """校验 corpid/secret 能否获取 access_token（配置测试按钮用）。"""
        try:
            token = self._get_access_token()
            return {"ok": True, "access_token_preview": token[:8] + "...", "agent_id": self.agent_id}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def client_from_profile(profile: dict[str, Any]) -> Optional[WeComAppClient]:
    """从 SecretStore profile 构造客户端；不完整回调配置降级为仅出站。"""
    corpid = profile.get("corpid")
    secret = profile.get("secret")
    agent_id = profile.get("agent_id")
    if not (corpid and secret and agent_id):
        return None
    token = (profile.get("token") or "").strip()
    encoding_aes_key = (profile.get("encoding_aes_key") or "").strip()
    if bool(token) != bool(encoding_aes_key):
        logger.warning("wecom callback config incomplete; inbound disabled until migrated")
        token = ""
        encoding_aes_key = ""
    try:
        return WeComAppClient(
            corpid=corpid,
            secret=secret,
            agent_id=agent_id,
            token=token,
            encoding_aes_key=encoding_aes_key,
        )
    except ValueError:
        logger.warning("wecom callback config invalid; inbound disabled until migrated")
        return WeComAppClient(corpid=corpid, secret=secret, agent_id=agent_id)

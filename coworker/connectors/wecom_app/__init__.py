"""企业微信自建应用连接器 —— 合规的双向消息通道。

合规边界（与 Halo 的 weixin-ilink 逆向协议划清界限）：
  - 本模块只走企微自建应用官方 API（corpid + secret + agent_id），合规。
  - 个人微信 ilink 逆向协议不移植（法律风险）。
  - 企微群机器人 webhook 见 coworker/notify/channels/wecom_webhook.py（单向出站）。

子模块：
  - crypto.py: AES-256-CBC 加解密 + SHA1 签名校验（纯函数）。
  - provider.py: WeComAppClient（access_token 缓存 + 消息收发 API 封装）。
  - adapter.py: WeComAppAdapter（实现 BasePlatformAdapter 契约，接入 gateway）。
"""

from __future__ import annotations

from .adapter import WeComAppAdapter
from .provider import WeComAppClient, client_from_profile
from . import crypto

__all__ = [
    "WeComAppAdapter",
    "WeComAppClient",
    "client_from_profile",
    "crypto",
]

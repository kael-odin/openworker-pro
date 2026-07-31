"""邮件通知渠道 — stdlib SMTP，无外部依赖。

凭证（SMTP 主机/端口/账号/授权码/收件人）存在 notify config，由 ``NotifyConfigStore``
转存到 SecretStore —— 与 ``connectors/email_tools.py`` 一致，密钥不落明文、不进模型上下文。

config:
    smtp_host:  必填，SMTP 服务器
    smtp_port:  必填，端口（465 SSL / 587 STARTTLS）
    username:   必填，发件邮箱
    password:   必填，授权码
    from_addr:  可选，默认 = username
    to_addr:    必填，收件邮箱
    use_ssl:    可选，True=465 implicit SSL，False=587 STARTTLS（默认按端口推断）
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

from .._result import NotifyResult


def send(title: str, body: str, config: dict[str, Any], *, status: str = "") -> NotifyResult:
    host = (config.get("smtp_host") or "").strip()
    username = (config.get("username") or "").strip()
    password = config.get("password") or ""
    to_addr = (config.get("to_addr") or "").strip()
    if not (host and username and password and to_addr):
        return NotifyResult(ok=False, channel="email", error="邮件 SMTP 配置不完整")

    port = int(config.get("smtp_port") or 465)
    use_ssl = config.get("use_ssl")
    if use_ssl is None:
        use_ssl = port == 465
    from_addr = config.get("from_addr") or username

    subject = title
    text = body
    if status:
        text += f"\n\n状态: {status}"

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(text)

    try:
        if use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=context, timeout=15) as s:
                s.login(username, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(username, password)
                s.send_message(msg)
    except Exception as exc:
        return NotifyResult(ok=False, channel="email", error=str(exc))
    return NotifyResult(ok=True, channel="email")

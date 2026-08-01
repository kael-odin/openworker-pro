"""企业微信消息加解密 —— 企微自建应用回调消息体的 AES-256-CBC 解密 + SHA1 签名校验。

企微「接收消息」加解密协议（官方 @WXMP/msg-crypt）:
  1. EncodingAESKey（43 字符 base64）→ base64 解码得 32 字节 AES key。
  2. IV = key 前 16 字节。
  3. 密文 base64 解码 → AES-256-CBC 解密 → 去掉 PKCS#7 填充。
  4. 明文布局：16B 随机串 + 4B 网络字节序 msg_len + msg_body + corpid。
  5. 签名：sha1(排序后 [token, timestamp, nonce, encrypt] 拼接) == msg_signature。

纯函数，无 I/O。pycryptodome 提供 AES（messaging 可选依赖）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
from typing import Optional


def _aes():
    """Lazy-load pycryptodome's AES module, raising ValueError (not ImportError) on miss.

    pycryptodome is a ``messaging`` optional dependency; the inbound-callback callers
    (provider.verify_and_decrypt) catch ``(ValueError, TypeError)``. Raising ValueError
    keeps the "not installed" case inside the existing error path instead of bubbling an
    unhandled ImportError that would crash the callback endpoint.
    """
    try:
        from Crypto.Cipher import AES  # type: ignore[import]
    except ImportError as exc:  # pragma: no cover - depends on optional dep
        raise ValueError(
            "pycryptodome 未安装：企微回调加解密需要它。"
            "装 messaging 可选依赖：pip install '.[messaging]'"
        ) from exc
    return AES


def _decode_aes_key(encoding_aes_key: str) -> bytes:
    """43 字符 base64 → 32 字节 AES key；非法编码一律拒绝。"""
    if not encoding_aes_key or len(encoding_aes_key) != 43:
        raise ValueError("EncodingAESKey 长度应为 43 字符")
    try:
        key = base64.b64decode(encoding_aes_key + "=", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("EncodingAESKey 格式无效") from exc
    if len(key) != 32:
        raise ValueError("EncodingAESKey 解码后长度无效")
    return key


def _pkcs7_unpad(data: bytes) -> bytes:
    """严格去除企微使用的 32 字节 PKCS#7 填充。"""
    if not data:
        raise ValueError("PKCS#7 数据为空")
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 32 or pad_len > len(data):
        raise ValueError("PKCS#7 填充长度无效")
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError("PKCS#7 填充无效")
    return data[:-pad_len]


def verify_signature(
    token: str,
    timestamp: str,
    nonce: str,
    encrypt: str,
    signature: str,
) -> bool:
    """校验企微回调签名。

    signature = sha1( sorted([token, timestamp, nonce, encrypt]).join("") )
    """
    parts = sorted([token, timestamp, nonce, encrypt])
    raw = "".join(parts).encode("utf-8")
    digest = hashlib.sha1(raw).hexdigest()
    # 企微签名是 hex 小写；常量时间比较避免时序攻击。
    return _const_eq(digest, signature or "")


def _const_eq(a: str, b: str) -> bool:
    """常量时间字符串比较。"""
    try:
        return hmac.compare_digest(a, b)
    except TypeError:
        return False


def decrypt_message(
    encrypt: str,
    encoding_aes_key: str,
    expected_corpid: Optional[str] = None,
) -> tuple[str, str]:
    """解密企微回调密文，返回 (message_xml, corpid)。

    - encrypt: 回调里的密文（base64 字符串）。
    - encoding_aes_key: 43 字符 base64 串。
    - expected_corpid: 可选，校验解出的 corpid 是否匹配（防伪造）。

    返回明文 XML 和解出的 corpid。解密失败抛 ValueError。
    """
    if not encrypt:
        raise ValueError("空密文")
    if not encoding_aes_key or len(encoding_aes_key) != 43:
        raise ValueError("EncodingAESKey 长度应为 43 字符")

    key = _decode_aes_key(encoding_aes_key)
    iv = key[:16]
    try:
        ciphertext = base64.b64decode(encrypt, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("密文不是合法 base64") from exc
    if not ciphertext or len(ciphertext) % 16:
        raise ValueError("密文长度无效")

    # 延迟导入：pycryptodome 是 messaging 可选依赖，不装的用户走纯出站不会触发。
    AES = _aes()

    cipher = AES.new(key, AES.MODE_CBC, iv)
    plain = cipher.decrypt(ciphertext)
    plain = _pkcs7_unpad(plain)

    # 明文布局：16B 随机 + 4B msg_len(网络字节序) + msg + corpid
    if len(plain) < 20:
        raise ValueError("解密后明文过短，格式异常")
    msg_len = struct.unpack(">I", plain[16:20])[0]
    if 20 + msg_len > len(plain):
        raise ValueError("msg_len 超出明文长度，格式异常")
    msg_bytes = plain[20 : 20 + msg_len]
    corp_bytes = plain[20 + msg_len :]
    if not corp_bytes:
        raise ValueError("解密后 corpid 为空")
    try:
        msg = msg_bytes.decode("utf-8")
        corpid = corp_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("解密后文本不是合法 UTF-8") from exc

    if expected_corpid and corpid != expected_corpid:
        raise ValueError("corpid 不匹配")

    return msg, corpid


def encrypt_message(
    reply_xml: str,
    encoding_aes_key: str,
    corpid: str,
    token: Optional[str] = None,
    timestamp: Optional[str] = None,
    nonce: Optional[str] = None,
) -> dict[str, str]:
    """加密企微回复消息，返回回调应答所需的 {encrypt, msg_signature, timestamp, nonce}。

    用于被动回复（加密模式）：收到用户消息后，回复也要加密。
    timestamp/nonce 缺省用当前时间戳 + 随机串（本模块不引入时间源，由调用方注入；
    若不注入则用 socket.gethostname 做熵源凑数 —— 生产场景调用方应注入真随机）。
    """
    if not encoding_aes_key or len(encoding_aes_key) != 43:
        raise ValueError("EncodingAESKey 长度应为 43 字符")

    key = _decode_aes_key(encoding_aes_key)
    iv = key[:16]

    # 16B 随机串 + 4B msg_len + msg + corpid
    import os

    random_bytes = os.urandom(16)
    msg_bytes = reply_xml.encode("utf-8")
    corp_bytes = corpid.encode("utf-8")
    msg_len = struct.pack(">I", len(msg_bytes))
    plain = random_bytes + msg_len + msg_bytes + corp_bytes

    # PKCS#7 填充到 32 字节倍数（AES block = 16，企微要求 32 倍数）。
    block = 32
    pad_len = block - (len(plain) % block)
    if pad_len == 0:
        pad_len = block
    plain += bytes([pad_len]) * pad_len

    AES = _aes()

    cipher = AES.new(key, AES.MODE_CBC, iv)
    ciphertext = cipher.encrypt(plain)
    encrypt_b64 = base64.b64encode(ciphertext).decode("ascii")

    if timestamp is None or nonce is None:
        # 调用方应注入；这里只做兜底，不引入 time/random 全局。
        import time
        import secrets

        timestamp = timestamp or str(int(time.time()))
        nonce = nonce or secrets.token_hex(8)

    sig = ""
    if token:
        parts = sorted([token, timestamp, nonce or "", encrypt_b64])
        sig = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()

    return {
        "encrypt": encrypt_b64,
        "msg_signature": sig,
        "timestamp": timestamp,
        "nonce": nonce or "",
    }


# XML 解析 —— 企微回调是 XML（明文模式或加密模式外层都是 XML）。
# 用标准库 xml.etree，不做外部 DTD/实体（防 XXE，企微是可信源但默认安全）。
def parse_callback_xml(body: bytes | str) -> dict[str, str]:
    """解析有界的企微回调 XML body，返回顶层字段。"""
    import xml.etree.ElementTree as ET

    if isinstance(body, bytes):
        if len(body) > 1_048_576:
            raise ValueError("回调 XML 过大")
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("回调 XML 不是合法 UTF-8") from exc
    elif len(body.encode("utf-8")) > 1_048_576:
        raise ValueError("回调 XML 过大")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError("回调 XML 格式无效") from exc
    if root.tag != "xml":
        raise ValueError("回调 XML 根节点无效")
    if len(root) > 64:
        raise ValueError("回调 XML 字段过多")
    out: dict[str, str] = {}
    for child in root:
        value = (child.text or "").strip()
        if len(child.tag) > 64 or len(value) > 262_144:
            raise ValueError("回调 XML 字段过大")
        out[child.tag] = value
    return out

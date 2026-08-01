"""Personal WeChat connector over the iLink HTTP protocol."""

from .adapter import AccountRuntime, WeChatIlinkAdapter
from .auth import QrAttemptRegistry
from .client import IlinkClient
from .models import (
    IlinkMessage,
    IlinkProtocolError,
    MessageItem,
    QrCode,
    QrStatus,
    SendResponse,
    Updates,
)
from .profiles import (
    AccountProfile,
    account_profile_key,
    account_rows,
    default_settings,
    delete_account,
    delete_all,
    get_account,
    iter_accounts,
    save_confirmation,
    set_default,
    set_needs_reauth,
)
from .transport import (
    CHANNEL_VERSION,
    DEFAULT_BASE_URL,
    IlinkTransport,
    IlinkTransportError,
    auth_headers,
    validate_base_url,
)

__all__ = [
    "AccountProfile",
    "AccountRuntime",
    "CHANNEL_VERSION",
    "DEFAULT_BASE_URL",
    "IlinkClient",
    "IlinkMessage",
    "IlinkProtocolError",
    "IlinkTransport",
    "IlinkTransportError",
    "MessageItem",
    "QrAttemptRegistry",
    "QrCode",
    "QrStatus",
    "SendResponse",
    "Updates",
    "WeChatIlinkAdapter",
    "account_profile_key",
    "account_rows",
    "auth_headers",
    "default_settings",
    "delete_account",
    "delete_all",
    "get_account",
    "iter_accounts",
    "save_confirmation",
    "set_default",
    "set_needs_reauth",
    "validate_base_url",
]

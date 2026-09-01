"""NotmyFault 插件扩展清单、命令和自有数据协议。"""

from notmyfault.extensions.protocol import (
    MAX_OWNED_VALUE_BYTES,
    OWNED_VALUE_KEY,
    OwnedValueError,
    make_owned_value,
    owned_value_identity,
    owned_value_summary,
    qualified_data_type,
    unpack_owned_value,
)
from notmyfault.extensions.registry import CONTRIBUTION_KINDS, ExtensionRegistry
from notmyfault.extensions.session import (
    ExtensionContext,
    ExtensionSession,
    ExtensionSessionManager,
)

__all__ = [
    "CONTRIBUTION_KINDS",
    "ExtensionContext",
    "ExtensionRegistry",
    "ExtensionSession",
    "ExtensionSessionManager",
    "MAX_OWNED_VALUE_BYTES",
    "OWNED_VALUE_KEY",
    "OwnedValueError",
    "make_owned_value",
    "owned_value_identity",
    "owned_value_summary",
    "qualified_data_type",
    "unpack_owned_value",
]

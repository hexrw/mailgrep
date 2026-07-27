from .store import (
    AccessState,
    FullDiskAccessError,
    MailStore,
    MailStoreError,
    default_mail_root,
    describe_access,
    open_mail_store,
)

__all__ = [
    "AccessState",
    "FullDiskAccessError",
    "MailStore",
    "MailStoreError",
    "default_mail_root",
    "describe_access",
    "open_mail_store",
]

__version__ = "0.1.0"

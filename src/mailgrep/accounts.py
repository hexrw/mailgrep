from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class Account:
    scheme: str
    username: str
    host: str
    mailbox_count: int
    message_count: int

    @property
    def identifier(self) -> str:
        if self.username and self.host:
            return f"{self.scheme}://{self.username}@{self.host}"
        if self.host:
            return f"{self.scheme}://{self.host}"
        return self.scheme or "local"

    def as_dict(self) -> dict:
        return {
            "identifier": self.identifier,
            "scheme": self.scheme,
            "username": self.username,
            "host": self.host,
            "mailbox_count": self.mailbox_count,
            "message_count": self.message_count,
        }


def parse_mailbox_url(mailbox_url: str) -> tuple[str, str, str, str]:
    if not mailbox_url:
        return "", "", "", ""
    parts = urlsplit(mailbox_url)
    scheme = parts.scheme or "local"
    username = unquote(parts.username or "")
    host = parts.hostname or ""
    mailbox_name = unquote(parts.path or "").lstrip("/")
    if scheme == "file" and not mailbox_name:
        mailbox_name = unquote(parts.netloc or "")
    return scheme, username, host, mailbox_name


def group_accounts(mailbox_counts: list[tuple[str, int]]) -> list[Account]:
    grouped: dict[tuple[str, str, str], list[int]] = {}
    for mailbox_url, message_count in mailbox_counts:
        scheme, username, host, _ = parse_mailbox_url(mailbox_url)
        grouped.setdefault((scheme, username, host), []).append(message_count)
    accounts = [
        Account(
            scheme=scheme,
            username=username,
            host=host,
            mailbox_count=len(counts),
            message_count=sum(counts),
        )
        for (scheme, username, host), counts in grouped.items()
    ]
    accounts.sort(key=lambda account: (-account.message_count, account.identifier))
    return accounts


def mailbox_display_name(mailbox_url: str) -> str:
    scheme, username, host, mailbox_name = parse_mailbox_url(mailbox_url)
    location = mailbox_name or "(root)"
    if username and host:
        return f"{username}@{host}/{location}"
    if host:
        return f"{host}/{location}"
    return f"{scheme}/{location}" if scheme else location

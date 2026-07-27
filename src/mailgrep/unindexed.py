from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from .emlx import ParsedMessage, read_emlx
from .envelope import MessageFilter, MessageRecord
from .locator import MessageLocator

MAILBOX_BUNDLE_SUFFIX = ".mbox"
OLDEST_SORTABLE_MOMENT = datetime.min.replace(tzinfo=timezone.utc)


@dataclass
class UnindexedScanResult:
    records: list[MessageRecord]
    scanned_count: int
    unreadable_ids: list[int]
    skipped_for_unknowable_state: int


def mailbox_path_from_message_path(message_path: Path, version_directory: Path) -> str:
    try:
        relative = message_path.relative_to(version_directory)
    except ValueError:
        return ""
    bundles = [part[: -len(MAILBOX_BUNDLE_SUFFIX)] for part in relative.parts if part.endswith(MAILBOX_BUNDLE_SUFFIX)]
    return "/".join(bundles)


def header_addresses(parsed: ParsedMessage, *names: str) -> list[tuple[str, str]]:
    raw_values = [parsed.header(name) for name in names]
    return getaddresses([value for value in raw_values if value])


def state_from_flags(parsed: ParsedMessage) -> tuple[bool | None, bool | None, bool | None]:
    flags = parsed.flags
    if flags is None:
        return None, None, None
    return flags.has("read"), flags.has("flagged"), flags.has("deleted")


def record_from_file(parsed: ParsedMessage, message_id: int, mailbox_path: str) -> MessageRecord:
    senders = header_addresses(parsed, "From")
    sender_name, sender_address = senders[0] if senders else ("", "")
    received = None
    date_header = parsed.header("Date")
    if date_header:
        try:
            parsed_date = parsedate_to_datetime(date_header)
            received = parsed_date.astimezone(timezone.utc) if parsed_date.tzinfo else parsed_date.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            received = None
    is_read, is_flagged, is_deleted = state_from_flags(parsed)
    return MessageRecord(
        message_id=message_id,
        subject=parsed.header("Subject"),
        sender_address=sender_address,
        sender_name=sender_name,
        mailbox_url=mailbox_path,
        date_received=received,
        date_sent=received,
        is_read=is_read,
        is_flagged=is_flagged,
        is_deleted=is_deleted,
        conversation_id=None,
        source="disk",
    )


def matches_filter(record: MessageRecord, parsed: ParsedMessage, message_filter: MessageFilter) -> bool:
    if message_filter.sender:
        needle = message_filter.sender.casefold()
        haystack = f"{record.sender_address} {record.sender_name}".casefold()
        if needle not in haystack:
            return False
    if message_filter.subject and message_filter.subject.casefold() not in record.subject.casefold():
        return False
    if message_filter.mailbox and message_filter.mailbox.casefold() not in record.mailbox_url.casefold():
        return False
    if message_filter.recipient:
        needle = message_filter.recipient.casefold()
        recipients = header_addresses(parsed, "To", "Cc", "Bcc")
        joined = " ".join(f"{name} {address}" for name, address in recipients).casefold()
        if needle not in joined:
            return False
    if message_filter.since:
        if record.date_received is None or record.date_received < message_filter.since:
            return False
    if message_filter.until:
        if record.date_received is None or record.date_received > message_filter.until:
            return False
    if message_filter.unread_only and record.is_read is not False:
        return False
    if message_filter.flagged_only and record.is_flagged is not True:
        return False
    if not message_filter.include_deleted and record.is_deleted is True:
        return False
    return True


def record_state_is_unknown(record: MessageRecord, message_filter: MessageFilter) -> bool:
    if message_filter.unread_only and record.is_read is None:
        return True
    return bool(message_filter.flagged_only and record.is_flagged is None)


def scan_unindexed_messages(
    locator: MessageLocator,
    indexed_ids: set[int],
    message_filter: MessageFilter,
    body_needle: str | None = None,
) -> UnindexedScanResult:
    orphan_ids = sorted(locator.message_ids() - indexed_ids)
    if not orphan_ids:
        return UnindexedScanResult(records=[], scanned_count=0, unreadable_ids=[], skipped_for_unknowable_state=0)

    version_directory = locator.mail_store.version_directory
    records: list[MessageRecord] = []
    unreadable: list[int] = []
    scanned = 0
    unknown_state_count = 0
    for message_id in orphan_ids:
        path = locator.path_for(message_id)
        if path is None or not path.exists():
            unreadable.append(message_id)
            continue
        try:
            parsed = read_emlx(path)
        except Exception:
            unreadable.append(message_id)
            continue
        scanned += 1
        record = record_from_file(parsed, message_id, mailbox_path_from_message_path(path, version_directory))
        if record_state_is_unknown(record, message_filter):
            unknown_state_count += 1
            continue
        if not matches_filter(record, parsed, message_filter):
            continue
        if body_needle is not None:
            try:
                if body_needle not in parsed.text_body().casefold():
                    continue
            except Exception:
                unreadable.append(message_id)
                continue
        records.append(record)
    records.sort(key=lambda item: item.date_received or OLDEST_SORTABLE_MOMENT, reverse=True)
    return UnindexedScanResult(
        records=records,
        scanned_count=scanned,
        unreadable_ids=unreadable,
        skipped_for_unknowable_state=unknown_state_count,
    )

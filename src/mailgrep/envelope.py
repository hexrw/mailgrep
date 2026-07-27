from __future__ import annotations

import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .store import FullDiskAccessError, MailStoreError

ENVELOPE_SIDECAR_SUFFIXES = ("", "-wal", "-shm")
COCOA_EPOCH_OFFSET_SECONDS = 978307200
UNIX_TIMESTAMP_LOWER_BOUND = 1_000_000_000

REQUIRED_TABLES = ("messages", "subjects", "addresses", "mailboxes")

MESSAGE_COLUMN_CANDIDATES = {
    "date_received": ("date_received", "date_sent"),
    "subject_reference": ("subject",),
    "sender_reference": ("sender",),
    "mailbox_reference": ("mailbox",),
}

OPTIONAL_MESSAGE_COLUMN_CANDIDATES = {
    "date_sent": ("date_sent",),
    "subject_prefix": ("subject_prefix",),
    "read_flag": ("read",),
    "flagged_flag": ("flagged",),
    "deleted_flag": ("deleted",),
    "conversation_reference": ("conversation_id", "conversation"),
}

SUBJECT_COLUMN_CANDIDATES = {"subject_text": ("subject",)}
ADDRESS_COLUMN_CANDIDATES = {"address_text": ("address",)}
OPTIONAL_ADDRESS_COLUMN_CANDIDATES = {"display_name": ("comment", "name")}
MAILBOX_COLUMN_CANDIDATES = {"mailbox_url": ("url",)}
RECIPIENT_COLUMN_CANDIDATES = {
    "message_reference": ("message_id", "message"),
    "address_reference": ("address_id", "address"),
}


class EnvelopeSchemaError(MailStoreError):
    pass


class EnvelopeIndexMissingError(MailStoreError):
    pass


def copy_envelope_index(envelope_index_path: Path, destination_directory: Path) -> Path:
    copied_primary = False
    for suffix in ENVELOPE_SIDECAR_SUFFIXES:
        source = envelope_index_path.parent / f"{envelope_index_path.name}{suffix}"
        try:
            if not source.exists():
                continue
            shutil.copy2(source, destination_directory / source.name)
        except PermissionError:
            raise FullDiskAccessError(source) from None
        if suffix == "":
            copied_primary = True
    if not copied_primary:
        raise EnvelopeIndexMissingError(
            f"{envelope_index_path} not found. Apple Mail has not built an envelope index for this store."
        )
    return destination_directory / envelope_index_path.name


@contextmanager
def open_envelope_index(envelope_index_path: Path) -> Iterator[sqlite3.Connection]:
    with tempfile.TemporaryDirectory(prefix="mailgrep-envelope-") as temporary_directory:
        snapshot = copy_envelope_index(envelope_index_path, Path(temporary_directory))
        connection = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()


def read_table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def read_column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {row[1] for row in rows}


def dump_schema(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return "\n\n".join(str(row[1]) for row in rows if row[1])


def resolve_columns(
    available: set[str],
    candidates: dict[str, tuple[str, ...]],
) -> tuple[dict[str, str], list[str]]:
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for logical_name, options in candidates.items():
        for option in options:
            if option in available:
                resolved[logical_name] = option
                break
        else:
            missing.append(f"{logical_name} (tried: {', '.join(options)})")
    return resolved, missing


@dataclass
class EnvelopeSchema:
    message_columns: dict[str, str]
    subject_columns: dict[str, str]
    address_columns: dict[str, str]
    mailbox_columns: dict[str, str]
    recipient_columns: dict[str, str] = field(default_factory=dict)
    has_recipients_table: bool = False
    timestamp_offset_seconds: int = COCOA_EPOCH_OFFSET_SECONDS

    def message_column(self, logical_name: str) -> str | None:
        return self.message_columns.get(logical_name)


def detect_timestamp_offset(connection: sqlite3.Connection, date_column: str) -> int:
    row = connection.execute(
        f'SELECT MAX("{date_column}") AS newest FROM messages WHERE "{date_column}" IS NOT NULL'
    ).fetchone()
    newest = row["newest"] if row else None
    if newest is None:
        return COCOA_EPOCH_OFFSET_SECONDS
    if float(newest) >= UNIX_TIMESTAMP_LOWER_BOUND:
        return 0
    return COCOA_EPOCH_OFFSET_SECONDS


def introspect_schema(connection: sqlite3.Connection) -> EnvelopeSchema:
    tables = read_table_names(connection)
    absent_tables = [name for name in REQUIRED_TABLES if name not in tables]
    if absent_tables:
        raise EnvelopeSchemaError(
            "Apple Mail's envelope index is missing expected tables: "
            + ", ".join(absent_tables)
            + "\n\nTables present: "
            + ", ".join(sorted(tables))
            + "\n\nThis usually means the schema changed in a macOS release that mailgrep has not been "
            "updated for. Please open an issue including the schema below.\n\n"
            + dump_schema(connection)
        )

    message_columns, missing_message = resolve_columns(
        read_column_names(connection, "messages"), MESSAGE_COLUMN_CANDIDATES
    )
    subject_columns, missing_subject = resolve_columns(
        read_column_names(connection, "subjects"), SUBJECT_COLUMN_CANDIDATES
    )
    address_column_names = read_column_names(connection, "addresses")
    address_columns, missing_address = resolve_columns(address_column_names, ADDRESS_COLUMN_CANDIDATES)
    mailbox_columns, missing_mailbox = resolve_columns(
        read_column_names(connection, "mailboxes"), MAILBOX_COLUMN_CANDIDATES
    )
    missing = missing_message + missing_subject + missing_address + missing_mailbox
    if missing:
        raise EnvelopeSchemaError(
            "Apple Mail's envelope index has an unexpected column layout.\n\n"
            + "Unresolved: "
            + "; ".join(missing)
            + "\n\nThis usually means the schema changed in a macOS release that mailgrep has not been "
            "updated for. Please open an issue including the schema below.\n\n"
            + dump_schema(connection)
        )

    optional_message_columns, _ = resolve_columns(
        read_column_names(connection, "messages"), OPTIONAL_MESSAGE_COLUMN_CANDIDATES
    )
    message_columns.update(optional_message_columns)
    optional_address_columns, _ = resolve_columns(
        address_column_names, OPTIONAL_ADDRESS_COLUMN_CANDIDATES
    )
    address_columns.update(optional_address_columns)

    recipient_columns: dict[str, str] = {}
    has_recipients_table = "recipients" in tables
    if has_recipients_table:
        recipient_columns, missing_recipient = resolve_columns(
            read_column_names(connection, "recipients"), RECIPIENT_COLUMN_CANDIDATES
        )
        if missing_recipient:
            has_recipients_table = False
            recipient_columns = {}

    schema = EnvelopeSchema(
        message_columns=message_columns,
        subject_columns=subject_columns,
        address_columns=address_columns,
        mailbox_columns=mailbox_columns,
        recipient_columns=recipient_columns,
        has_recipients_table=has_recipients_table,
    )
    schema.timestamp_offset_seconds = detect_timestamp_offset(
        connection, message_columns["date_received"]
    )
    return schema


def to_datetime(raw_value, offset_seconds: int) -> datetime | None:
    if raw_value is None:
        return None
    try:
        seconds = float(raw_value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds + offset_seconds, tz=timezone.utc)


def to_store_timestamp(moment: datetime, offset_seconds: int) -> float:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp() - offset_seconds


@dataclass
class MessageRecord:
    message_id: int
    subject: str
    sender_address: str
    sender_name: str
    mailbox_url: str
    date_received: datetime | None
    date_sent: datetime | None
    is_read: bool | None
    is_flagged: bool | None
    is_deleted: bool | None
    conversation_id: int | None
    source: str = "index"

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "message_id": self.message_id,
            "subject": self.subject,
            "sender_address": self.sender_address,
            "sender_name": self.sender_name,
            "mailbox_url": self.mailbox_url,
            "date_received": self.date_received.isoformat() if self.date_received else None,
            "date_sent": self.date_sent.isoformat() if self.date_sent else None,
            "is_read": self.is_read,
            "is_flagged": self.is_flagged,
            "is_deleted": self.is_deleted,
            "conversation_id": self.conversation_id,
        }


@dataclass
class MessageFilter:
    sender: str | None = None
    recipient: str | None = None
    subject: str | None = None
    mailbox: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    unread_only: bool = False
    flagged_only: bool = False
    include_deleted: bool = False
    conversation_id: int | None = None
    message_ids: list[int] | None = None


def build_message_query(schema: EnvelopeSchema, message_filter: MessageFilter) -> tuple[str, list]:
    columns = schema.message_columns
    date_received_column = columns["date_received"]
    selected = [
        "messages.ROWID AS message_id",
        f'messages."{date_received_column}" AS date_received',
        "subjects_table.{} AS subject".format(schema.subject_columns["subject_text"]),
        "addresses_table.{} AS sender_address".format(schema.address_columns["address_text"]),
        "mailboxes_table.{} AS mailbox_url".format(schema.mailbox_columns["mailbox_url"]),
    ]
    display_name_column = schema.address_columns.get("display_name")
    selected.append(
        f"addresses_table.{display_name_column} AS sender_name" if display_name_column else "NULL AS sender_name"
    )
    subject_prefix_column = columns.get("subject_prefix")
    selected.append(
        f'messages."{subject_prefix_column}" AS subject_prefix' if subject_prefix_column else "NULL AS subject_prefix"
    )
    for logical_name, alias in (
        ("date_sent", "date_sent"),
        ("read_flag", "is_read"),
        ("flagged_flag", "is_flagged"),
        ("deleted_flag", "is_deleted"),
        ("conversation_reference", "conversation_id"),
    ):
        column = columns.get(logical_name)
        selected.append(f'messages."{column}" AS {alias}' if column else f"NULL AS {alias}")

    statement = [
        "SELECT " + ", ".join(selected),
        "FROM messages",
        'LEFT JOIN subjects AS subjects_table ON messages."{}" = subjects_table.ROWID'.format(
            columns["subject_reference"]
        ),
        'LEFT JOIN addresses AS addresses_table ON messages."{}" = addresses_table.ROWID'.format(
            columns["sender_reference"]
        ),
        'LEFT JOIN mailboxes AS mailboxes_table ON messages."{}" = mailboxes_table.ROWID'.format(
            columns["mailbox_reference"]
        ),
    ]

    conditions: list[str] = []
    parameters: list = []

    if message_filter.sender:
        address_column = schema.address_columns["address_text"]
        if display_name_column:
            conditions.append(
                f"(addresses_table.{address_column} LIKE ? OR addresses_table.{display_name_column} LIKE ?)"
            )
            parameters.extend([f"%{message_filter.sender}%"] * 2)
        else:
            conditions.append(f"addresses_table.{address_column} LIKE ?")
            parameters.append(f"%{message_filter.sender}%")

    if message_filter.subject:
        subject_text_column = schema.subject_columns["subject_text"]
        if subject_prefix_column:
            conditions.append(
                f'(COALESCE(messages."{subject_prefix_column}", "") || '
                f'COALESCE(subjects_table.{subject_text_column}, "")) LIKE ?'
            )
        else:
            conditions.append(f"subjects_table.{subject_text_column} LIKE ?")
        parameters.append(f"%{message_filter.subject}%")

    if message_filter.mailbox:
        conditions.append("mailboxes_table.{} LIKE ?".format(schema.mailbox_columns["mailbox_url"]))
        parameters.append(f"%{message_filter.mailbox}%")

    if message_filter.recipient and schema.has_recipients_table:
        recipient_message_column = schema.recipient_columns["message_reference"]
        recipient_address_column = schema.recipient_columns["address_reference"]
        address_column = schema.address_columns["address_text"]
        conditions.append(
            "EXISTS (SELECT 1 FROM recipients "
            f'JOIN addresses AS recipient_addresses ON recipients."{recipient_address_column}" = recipient_addresses.ROWID '
            f'WHERE recipients."{recipient_message_column}" = messages.ROWID '
            f"AND recipient_addresses.{address_column} LIKE ?)"
        )
        parameters.append(f"%{message_filter.recipient}%")

    if message_filter.since:
        conditions.append(f'messages."{date_received_column}" >= ?')
        parameters.append(to_store_timestamp(message_filter.since, schema.timestamp_offset_seconds))

    if message_filter.until:
        conditions.append(f'messages."{date_received_column}" <= ?')
        parameters.append(to_store_timestamp(message_filter.until, schema.timestamp_offset_seconds))

    read_column = columns.get("read_flag")
    if message_filter.unread_only and read_column:
        conditions.append(f'(messages."{read_column}" = 0 OR messages."{read_column}" IS NULL)')

    flagged_column = columns.get("flagged_flag")
    if message_filter.flagged_only and flagged_column:
        conditions.append(f'messages."{flagged_column}" = 1')

    deleted_column = columns.get("deleted_flag")
    if not message_filter.include_deleted and deleted_column:
        conditions.append(f'(messages."{deleted_column}" = 0 OR messages."{deleted_column}" IS NULL)')

    conversation_column = columns.get("conversation_reference")
    if message_filter.conversation_id is not None and conversation_column:
        conditions.append(f'messages."{conversation_column}" = ?')
        parameters.append(message_filter.conversation_id)

    if message_filter.message_ids:
        placeholders = ", ".join("?" for _ in message_filter.message_ids)
        conditions.append(f"messages.ROWID IN ({placeholders})")
        parameters.extend(message_filter.message_ids)

    if conditions:
        statement.append("WHERE " + " AND ".join(conditions))
    statement.append(f'ORDER BY messages."{date_received_column}" DESC')
    return "\n".join(statement), parameters


def combine_subject(row: sqlite3.Row) -> str:
    prefix = row["subject_prefix"] or ""
    subject = row["subject"] or ""
    return f"{prefix}{subject}"


def row_to_record(row: sqlite3.Row, offset_seconds: int) -> MessageRecord:
    return MessageRecord(
        message_id=int(row["message_id"]),
        subject=combine_subject(row),
        sender_address=row["sender_address"] or "",
        sender_name=row["sender_name"] or "",
        mailbox_url=row["mailbox_url"] or "",
        date_received=to_datetime(row["date_received"], offset_seconds),
        date_sent=to_datetime(row["date_sent"], offset_seconds),
        is_read=None if row["is_read"] is None else bool(row["is_read"]),
        is_flagged=None if row["is_flagged"] is None else bool(row["is_flagged"]),
        is_deleted=None if row["is_deleted"] is None else bool(row["is_deleted"]),
        conversation_id=None if row["conversation_id"] is None else int(row["conversation_id"]),
    )


def query_messages(
    connection: sqlite3.Connection,
    schema: EnvelopeSchema,
    message_filter: MessageFilter,
) -> Iterator[MessageRecord]:
    statement, parameters = build_message_query(schema, message_filter)
    for row in connection.execute(statement, parameters):
        yield row_to_record(row, schema.timestamp_offset_seconds)


def count_messages(connection: sqlite3.Connection, schema: EnvelopeSchema) -> int:
    row = connection.execute("SELECT COUNT(*) AS total FROM messages").fetchone()
    return int(row["total"]) if row else 0


def all_message_ids(connection: sqlite3.Connection) -> set[int]:
    return {int(row[0]) for row in connection.execute("SELECT ROWID FROM messages")}


def list_mailbox_urls(connection: sqlite3.Connection, schema: EnvelopeSchema) -> list[tuple[str, int]]:
    mailbox_url_column = schema.mailbox_columns["mailbox_url"]
    mailbox_reference = schema.message_columns["mailbox_reference"]
    statement = (
        f"SELECT mailboxes.{mailbox_url_column} AS url, COUNT(messages.ROWID) AS total "
        f'FROM mailboxes LEFT JOIN messages ON messages."{mailbox_reference}" = mailboxes.ROWID '
        f"GROUP BY mailboxes.ROWID ORDER BY url"
    )
    return [(row["url"] or "", int(row["total"])) for row in connection.execute(statement)]

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .accounts import group_accounts, mailbox_display_name
from .attachments import (
    AttachmentUnavailableError,
    extract_attachment,
    list_attachments,
)
from .doctor import build_report, render_report
from .emlx import read_emlx
from .envelope import (
    MessageFilter,
    MessageRecord,
    all_message_ids,
    introspect_schema,
    list_mailbox_urls,
    open_envelope_index,
    query_messages,
)
from .locator import build_locator
from .unindexed import scan_unindexed_messages
from .store import FullDiskAccessError, MailStoreError, default_mail_root, open_mail_store

PROGRAM_NAME = "mailgrep"
DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")


def parse_date(value: str) -> datetime:
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(value, date_format).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"unrecognised date: {value!r} (expected YYYY-MM-DD)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM_NAME,
        description="Read-only search and extraction for the local Apple Mail store.",
    )
    parser.add_argument("--mail-root", type=Path, default=None, help="override ~/Library/Mail")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="verify access, schema, coverage and freshness")
    doctor_parser.add_argument("--reindex", action="store_true", help="rebuild the message path cache")

    subparsers.add_parser("accounts", help="list accounts discovered in the local store")
    subparsers.add_parser("mailboxes", help="list mailboxes and message counts")

    search_parser = subparsers.add_parser("search", help="search messages")
    search_parser.add_argument("--from", dest="sender", help="match sender address or display name")
    search_parser.add_argument("--to", dest="recipient", help="match any recipient address")
    search_parser.add_argument("--subject", help="match subject text")
    search_parser.add_argument("--mailbox", help="match mailbox URL")
    search_parser.add_argument("--body", help="match decoded message body (reads every candidate file)")
    search_parser.add_argument("--since", type=parse_date, help="only messages received on or after this date")
    search_parser.add_argument("--until", type=parse_date, help="only messages received on or before this date")
    search_parser.add_argument("--unread", action="store_true", help="only unread messages")
    search_parser.add_argument("--flagged", action="store_true", help="only flagged messages")
    search_parser.add_argument("--include-deleted", action="store_true", help="include messages marked deleted")
    search_parser.add_argument("--limit", type=int, default=None, help="stop after this many matches")
    search_parser.add_argument(
        "--indexed-only",
        action="store_true",
        help="skip messages absent from Apple Mail's index (faster, but incomplete)",
    )

    read_parser = subparsers.add_parser("read", help="print one message as text")
    read_parser.add_argument("message_id", type=int)
    read_parser.add_argument("--headers-only", action="store_true")

    attachments_parser = subparsers.add_parser("attachments", help="list or extract attachments")
    attachments_parser.add_argument("message_id", type=int)
    attachments_parser.add_argument("--extract", type=Path, default=None, help="directory to copy bytes into")
    return parser


def resolve_mail_root(arguments: argparse.Namespace) -> Path:
    return arguments.mail_root or default_mail_root()


def emit(payload, text: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(text)


def run_doctor(arguments: argparse.Namespace) -> int:
    report = build_report(resolve_mail_root(arguments), force_rescan=arguments.reindex)
    emit(report, render_report(report), arguments.json)
    if report["access_state"] != "readable":
        return 2
    if not report.get("schema_ok", False):
        return 3
    if not report.get("coverage_complete", True):
        return 4
    return 0


def run_accounts(arguments: argparse.Namespace) -> int:
    mail_store = open_mail_store(resolve_mail_root(arguments))
    with open_envelope_index(mail_store.envelope_index_path) as connection:
        schema = introspect_schema(connection)
        accounts = group_accounts(list_mailbox_urls(connection, schema))
    payload = [account.as_dict() for account in accounts]
    text = "\n".join(
        f"{account.identifier}\t{account.mailbox_count} mailboxes\t{account.message_count} messages"
        for account in accounts
    )
    emit(payload, text or "no accounts found", arguments.json)
    return 0


def run_mailboxes(arguments: argparse.Namespace) -> int:
    mail_store = open_mail_store(resolve_mail_root(arguments))
    with open_envelope_index(mail_store.envelope_index_path) as connection:
        schema = introspect_schema(connection)
        mailbox_counts = list_mailbox_urls(connection, schema)
    payload = [
        {"url": url, "display_name": mailbox_display_name(url), "message_count": total}
        for url, total in mailbox_counts
    ]
    text = "\n".join(f"{total}\t{mailbox_display_name(url)}\t{url}" for url, total in mailbox_counts)
    emit(payload, text or "no mailboxes found", arguments.json)
    return 0


def format_record(record: MessageRecord) -> str:
    received = record.date_received.strftime("%Y-%m-%d %H:%M") if record.date_received else "unknown-date"
    sender = record.sender_address or record.sender_name or "unknown-sender"
    marker = "\t(unindexed)" if record.source == "disk" else ""
    return f"{record.message_id}\t{received}\t{sender}\t{record.subject or '(no subject)'}{marker}"


def build_filter(arguments: argparse.Namespace) -> MessageFilter:
    return MessageFilter(
        sender=arguments.sender,
        recipient=arguments.recipient,
        subject=arguments.subject,
        mailbox=arguments.mailbox,
        since=arguments.since,
        until=arguments.until,
        unread_only=arguments.unread,
        flagged_only=arguments.flagged,
        include_deleted=arguments.include_deleted,
    )


def run_search(arguments: argparse.Namespace) -> int:
    mail_store = open_mail_store(resolve_mail_root(arguments))
    message_filter = build_filter(arguments)
    matches: list[MessageRecord] = []
    scanned = 0
    unreadable: list[int] = []
    truncated = False
    body_needle = arguments.body.casefold() if arguments.body else None
    locator = build_locator(mail_store)

    with open_envelope_index(mail_store.envelope_index_path) as connection:
        schema = introspect_schema(connection)
        indexed_ids = all_message_ids(connection)
        for record in query_messages(connection, schema, message_filter):
            scanned += 1
            if body_needle is not None:
                path = locator.path_for(record.message_id)
                if path is None or not path.exists():
                    unreadable.append(record.message_id)
                    continue
                try:
                    body = read_emlx(path).text_body()
                except FullDiskAccessError:
                    raise
                except Exception:
                    unreadable.append(record.message_id)
                    continue
                if body_needle not in body.casefold():
                    continue
            matches.append(record)
            if arguments.limit is not None and len(matches) >= arguments.limit:
                truncated = True
                break

    unindexed_result = None
    if not arguments.indexed_only and not truncated:
        unindexed_result = scan_unindexed_messages(locator, indexed_ids, message_filter, body_needle)
        scanned += unindexed_result.scanned_count
        unreadable.extend(unindexed_result.unreadable_ids)
        for record in unindexed_result.records:
            matches.append(record)
            if arguments.limit is not None and len(matches) >= arguments.limit:
                truncated = True
                break

    disk_match_count = sum(1 for record in matches if record.source == "disk")
    summary_lines = [format_record(record) for record in matches]
    footer = [f"-- {len(matches)} matches, {scanned} messages examined"]
    if disk_match_count:
        footer.append(
            f"-- {disk_match_count} of those are not in Apple Mail's index and were read directly from disk"
        )
    if unreadable:
        footer.append(
            f"-- WARNING: {len(unreadable)} messages could not be read and were not searched "
            f"(sample ids: {unreadable[:10]}); run `{PROGRAM_NAME} doctor` for details"
        )
    if unindexed_result is not None and unindexed_result.skipped_for_unknowable_state:
        footer.append(
            f"-- WARNING: {unindexed_result.skipped_for_unknowable_state} unindexed messages were skipped because "
            "--unread and --flagged cannot be evaluated without Apple Mail's index"
        )
    if arguments.indexed_only:
        footer.append("-- NOTE: --indexed-only was set; messages absent from Apple Mail's index were not searched")
    if truncated:
        footer.append(f"-- WARNING: stopped at --limit {arguments.limit}; more matches may exist")

    payload = {
        "matches": [record.as_dict() for record in matches],
        "match_count": len(matches),
        "examined_count": scanned,
        "unindexed_match_count": disk_match_count,
        "unreadable_count": len(unreadable),
        "unreadable_sample": unreadable[:10],
        "skipped_unindexed_count": unindexed_result.skipped_for_unknowable_state if unindexed_result else 0,
        "indexed_only": arguments.indexed_only,
        "truncated_by_limit": truncated,
    }
    emit(payload, "\n".join(summary_lines + footer), arguments.json)
    return 0


def load_message(arguments: argparse.Namespace, message_id: int):
    mail_store = open_mail_store(resolve_mail_root(arguments))
    locator = build_locator(mail_store)
    path = locator.path_for(message_id)
    if path is None or not path.exists():
        locator = build_locator(mail_store, force_rescan=True)
        path = locator.path_for(message_id)
    if path is None or not path.exists():
        raise MailStoreError(
            f"no message file found for id {message_id}. "
            f"Run `{PROGRAM_NAME} doctor` to check index and disk coverage."
        )
    return read_emlx(path)


def run_read(arguments: argparse.Namespace) -> int:
    parsed = load_message(arguments, arguments.message_id)
    headers = {name: parsed.header(name) for name in ("Date", "From", "To", "Cc", "Subject")}
    attachments = list_attachments(parsed, arguments.message_id)
    body = "" if arguments.headers_only else parsed.text_body()
    payload = {
        "message_id": arguments.message_id,
        "path": str(parsed.path),
        "is_partial": parsed.is_partial,
        "headers": headers,
        "body": body,
        "attachments": [
            {
                "part_id": attachment.part_id,
                "filename": attachment.filename,
                "content_type": attachment.content_type,
                "source": attachment.source.value,
                "savable": attachment.savable,
            }
            for attachment in attachments
        ],
    }
    lines = [f"{name}: {value}" for name, value in headers.items() if value]
    if attachments:
        descriptions = ", ".join(f"{item.filename} [{item.source.value}]" for item in attachments)
        lines.append(f"Attachments: {descriptions}")
    if body:
        lines.append("")
        lines.append(body)
    emit(payload, "\n".join(lines), arguments.json)
    return 0


def run_attachments(arguments: argparse.Namespace) -> int:
    parsed = load_message(arguments, arguments.message_id)
    attachments = list_attachments(parsed, arguments.message_id)
    results = []
    lines = []
    exit_code = 0
    for attachment in attachments:
        entry = {
            "part_id": attachment.part_id,
            "filename": attachment.filename,
            "content_type": attachment.content_type,
            "source": attachment.source.value,
            "declared_size": attachment.declared_size,
            "savable": attachment.savable,
            "reason": attachment.reason,
            "extracted_to": None,
        }
        if arguments.extract is not None:
            if attachment.savable:
                try:
                    destination = extract_attachment(parsed, attachment, arguments.extract)
                    entry["extracted_to"] = str(destination)
                except AttachmentUnavailableError as error:
                    entry["reason"] = str(error)
                    exit_code = 5
            else:
                exit_code = 5
        results.append(entry)
        size = "unknown size" if attachment.declared_size is None else f"{attachment.declared_size} bytes"
        line = f"{attachment.part_id}\t{attachment.filename}\t{attachment.content_type}\t{attachment.source.value}\t{size}"
        if entry["extracted_to"]:
            line += f"\t-> {entry['extracted_to']}"
        elif attachment.reason:
            line += f"\t{attachment.reason}"
        lines.append(line)
    if not attachments:
        lines.append("no attachments")
    emit({"message_id": arguments.message_id, "attachments": results}, "\n".join(lines), arguments.json)
    return exit_code


COMMAND_HANDLERS = {
    "doctor": run_doctor,
    "accounts": run_accounts,
    "mailboxes": run_mailboxes,
    "search": run_search,
    "read": run_read,
    "attachments": run_attachments,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    handler = COMMAND_HANDLERS[arguments.command]
    try:
        return handler(arguments)
    except FullDiskAccessError as error:
        print(str(error), file=sys.stderr)
        return 2
    except MailStoreError as error:
        print(f"{PROGRAM_NAME}: {error}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

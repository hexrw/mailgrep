---
name: mailgrep
description: Use when the user asks you to find, read, search, quote, or extract attachments from email in the local Apple Mail app on macOS - searches Mail's on-disk store read-only, with no ability to send or modify mail
---

# Reading Apple Mail with mailgrep

`mailgrep` reads Apple Mail's local store on macOS: the `Envelope Index` SQLite database for
metadata, `.emlx` files for bodies. Read-only by construction — it cannot send, delete, move, or
flag anything.

## Always run doctor first in a session

```sh
mailgrep doctor
```

It resolves access, schema, coverage and freshness in one shot. Do not debug empty search results
without running it — the answer is almost always in its output.

Exit codes: `0` ok, `2` Full Disk Access missing, `3` schema unrecognised, `4` coverage incomplete.

## Searching

```sh
mailgrep search --from alec@example.com --since 2026-06-01
mailgrep search --subject contract --body "signed copy"
mailgrep search --to someone@example.com --mailbox INBOX --unread
mailgrep search --from vendor --json
```

`--from` matches address or display name. `--subject`, `--mailbox`, `--to` are substring matches.
`--body` decodes each candidate message, so it is slower than metadata filters — narrow with
`--from`/`--since` first when a mailbox is large.

Add `--json` whenever you need to parse the result rather than show it.

Search returns message ids. Use them with `read` and `attachments`:

```sh
mailgrep read 12345
mailgrep read 12345 --headers-only
mailgrep attachments 12345
mailgrep attachments 12345 --extract ./out
```

## Read the output footers — they carry the caveats

Every search prints a summary line and, when relevant, warnings. Report these to the user rather
than presenting results as complete when they aren't:

- `N of those are not in Apple Mail's index and were read directly from disk` — normal, usually
  drafts and outbox. Those rows are labelled `(unindexed)`.
- `WARNING: N messages could not be read` — those were **not** searched. Run `doctor`.
- `WARNING: stopped at --limit N` — more matches may exist. Do not describe the result as complete.
- `--unread/--flagged cannot be evaluated: their .emlx files carry no flags trailer` — a few
  unindexed messages were excluded from that specific filter.

If the user asks "find every email about X", do not pass `--limit`, and do not pass
`--indexed-only`.

## Attachments have four states

`inline` and `external` are savable. `not_downloaded` means Mail never fetched the bytes from the
server (the account's Download Attachments setting is Recent or None) — no local tool can retrieve
them, and the user must open the message in Mail once. `not_extractable` means declared but absent.

`mailgrep` refuses to write a 0-byte file rather than reporting a false success. If extraction exits
`5`, tell the user which state applies instead of retrying.

## Large attachments and images

Extract to a scratch directory, not into the user's project. For image or PDF attachments, delegate
the visual inspection to a subagent and keep only its text summary — raw image bytes consume a large
amount of context for something that is only needed once.

## Full Disk Access

If `doctor` exits `2`, the fix belongs to the user and cannot be scripted. `~/Library/Mail` is
TCC-protected, and the grant must go to the **application that launched the shell** (terminal,
editor, IDE), not to `mailgrep` itself — macOS attributes file access to the launching application.
`doctor` names the exact application. The user must then quit and reopen it, because TCC only applies
grants to newly launched processes.

## Freshness

Apple Mail does not fetch mail while it is closed. `doctor` reports whether Mail.app is running and
how long ago it last wrote to the store. macOS exposes no last-sync timestamp, so that write time is a
**lower bound** on freshness — never tell the user their mail is up to date on the strength of it.

If the user is looking for a message that just arrived and it isn't there, check `doctor` for
`Mail.app running: NO`. That is the likely answer. `mailgrep` cannot trigger a sync; the user must
open Mail.

Message ids are `ROWID`s and Apple Mail reassigns them when it rebuilds its index. Use them within a
task; never store them or quote them back to the user in a later session as stable references.

## Exchange accounts return nothing, not stale data

EWS/Exchange accounts do not store messages locally at all. If a user insists mail exists that
`mailgrep` cannot find, check the account type before assuming a bug — for Exchange there is nothing
on disk to read.

## Privacy

This reads the user's real mail. Quote only what the task needs, do not bulk-dump mailboxes into your
context, and do not copy message contents into files or commits unless the user asked for it.

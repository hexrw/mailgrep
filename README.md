# mailgrep

Read-only search and extraction for the local Apple Mail store on macOS.

`mailgrep` reads the files Apple Mail already keeps on your disk — the `Envelope Index` SQLite
database for metadata and `.emlx` files for message bodies. It never drives Mail.app, never speaks
IMAP, and contains no code path that can send, delete, move, or flag a message.

- **Read-only by construction.** Not a flag you can forget to pass; there is no write path.
- **Zero dependencies.** Python standard library only. Runs on macOS's stock `/usr/bin/python3`.
- **No silent gaps.** Every command reports what it examined, what it could not read, and what it
  skipped. Coverage is a tested property, not a hope.
- **Stays inside `~/Library/Mail`.** Deliberately does not read the macOS Accounts framework
  database or any other credential store.

## Install

```sh
git clone https://github.com/hexrw/mailgrep
cd mailgrep
pip install .
```

Or run it straight from the checkout with no install at all:

```sh
PYTHONPATH=src python3 -m mailgrep doctor
```

## Grant Full Disk Access

`~/Library/Mail` is protected by macOS privacy controls (TCC). Nothing works until you grant Full
Disk Access, and you must grant it to **the application that launches the process** — your terminal,
your editor, or whatever spawned the shell.

System Settings → Privacy & Security → Full Disk Access → add your terminal app, then quit and
reopen it completely. TCC only applies grants to newly launched processes.

Granting Full Disk Access to the `mailgrep` executable itself does nothing. macOS resolves what it
calls *responsible code*, which for a program run from a shell is the terminal application, not the
program. This trick worked until Apple closed it in early 2019.

`mailgrep doctor` detects a missing grant and names the exact application that needs it.

Full Disk Access is coarse: it grants the terminal read access to all TCC-protected data, not just
Mail. That is a real cost, and macOS offers no narrower per-application entitlement for mail data.
The only tighter alternative is installing a reader as a signed launchd agent with
`AssociatedBundleIdentifiers`, which `mailgrep` deliberately does not do.

## Usage

Start here. It verifies access, resolves the schema, audits coverage, and checks freshness:

```sh
mailgrep doctor
```

```
access:                 ok
version directory:      /Users/you/Library/Mail/V10
timestamp offset:       0s
messages in index:      8216
message files on disk:  8362
readable messages:      8216
coverage:               INCOMPLETE
  file but not indexed: 146 (sample ids: [2, 7, 11, 14, 17])
```

Search. Metadata filters run in SQLite; `--body` decodes each candidate message:

```sh
mailgrep search --from alec@example.com --since 2026-06-01
mailgrep search --subject contract --body "signed copy"
mailgrep search --to petr@example.com --mailbox INBOX --unread
mailgrep search --from vendor --json
```

Read one message, and list or extract its attachments:

```sh
mailgrep read 12345
mailgrep attachments 12345
mailgrep attachments 12345 --extract ./out
```

Every command takes `--json` for scripting, and `--mail-root` to point at a copied or archived store.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | success |
| 1 | usage or lookup error (e.g. unknown message id) |
| 2 | Full Disk Access missing, or no Mail store |
| 3 | envelope index schema not recognised |
| 4 | coverage incomplete (`doctor` only) |
| 5 | an attachment could not be extracted |

## Attachments have three states, and the difference matters

Apple Mail does not keep attachments inside the `.emlx`. It converts the file to `.partial.emlx`,
keeps the MIME structure including `Content-Disposition: filename`, strips the encoded body, and
writes the real bytes to a sibling `Attachments/<id>/<part>/<filename>` directory.

- `inline` — bytes are in the message body.
- `external` — body is empty, bytes are in the sibling `Attachments` directory.
- `not_downloaded` — bytes are on the server and were never fetched. Mail's per-account *Download
  Attachments* setting (All / Recent / None) decides this. No local tool can recover them; open the
  message in Mail once.
- `not_extractable` — declared but neither present nor marked as pending.

A parser that only reads the inline body gets an empty payload for the `external` and
`not_downloaded` cases. If it does not check, it writes a **0-byte file and reports success**.
`mailgrep` refuses to write empty output and tells you which state applies. This is regression-tested
against synthetic `.partial.emlx` fixtures.

## Coverage, and why `doctor` can say INCOMPLETE

`doctor` compares the set of message ids in the index against the set of `.emlx` files on disk, and
reports both directions:

- **indexed but no file** — the message is searchable by metadata but its body cannot be read.
- **file but not indexed** — the message exists on disk but Mail's index does not list it. In
  practice this is mostly `Drafts` and `Outbox`, which Mail stores differently.

`search` reads unindexed messages directly from disk so they still turn up, labelled `(unindexed)`.
Pass `--indexed-only` to skip them for speed; the output says so when you do. `--unread` and
`--flagged` cannot be evaluated for unindexed messages, so they are excluded and counted explicitly
rather than silently dropped.

## Notes on the store format

The `Envelope Index` schema is undocumented and Apple changes it between releases, so `mailgrep`
resolves table and column names at runtime via `PRAGMA table_info` rather than hardcoding them. When
resolution fails it aborts with the actual schema printed, instead of returning zero results.

The version directory is discovered by globbing `~/Library/Mail/V*` and picking the highest number.
`V10` is current from macOS 13 through macOS 26, and `V9` was macOS 12, but nothing is hardcoded.

**Timestamps are detected, not assumed.** Apple Mail's own history and most published writeups
describe these columns as Core Data / Cocoa seconds (epoch 2001-01-01, offset 978307200). On macOS 26
they are plain Unix seconds. `mailgrep` samples the newest value and picks the interpretation, so
both work; hardcoding the Cocoa offset puts every date off by 31 years. `doctor` prints the detected
offset.

Mail keeps the index in WAL mode, so `mailgrep` snapshot-copies the database together with its `-wal`
and `-shm` sidecars and opens the copy read-only. Reading the main database alone — or with
`immutable=1` — silently misses everything still sitting in the write-ahead log.

The local store is only as fresh as Mail's last sync, and Mail does not sync while it is closed.
`doctor` reports how long ago the index was touched and warns past 24 hours. Forcing a sync would
require an Automation grant, which `mailgrep` does not ask for.

## Development

```sh
python3 -m unittest discover -s tests -t tests
```

The suite runs entirely against synthetic fixtures generated in a temp directory — no real mail, no
Full Disk Access, no network. `tests/fixtures.py` builds a store with an `Envelope Index`, plain and
HTML messages, an inline attachment, a stripped `.partial.emlx` with external bytes, and a
never-downloaded attachment.

## License

MIT

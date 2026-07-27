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

One wrinkle worth knowing: `~/Library/Mail` is a protected directory, so a denial can surface as
*"no such file"* rather than a permission error. `mailgrep` reports that case as ambiguous — Mail was
never set up, **or** access is denied and macOS is hiding it — instead of asserting the store is
missing. Genuinely transient errors are reported as undetermined so you are never sent to change
privacy settings for an unrelated failure.

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
| 2 | Full Disk Access missing, or no Mail store, or access undetermined |
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

**Subjects are stored without their reply prefix.** `subjects.subject` holds `Faktura 260002` while
`Re: ` lives in a separate `messages.subject_prefix` column. `mailgrep` concatenates them, so
`--subject "Re: Faktura"` matches. Joining only the `subjects` table — which every documented query
does — silently misses every reply when you search across the prefix boundary.

**`messages.sender` joins to `addresses.ROWID` directly.** The schema also contains `senders` and
`sender_addresses` tables, and at least one implementation declares `sender REFERENCES
sender_addresses` in its DDL. On macOS 26 that indirection is not used for the sender address: the
direct join resolves every row. `senders` holds Apple-Intelligence contact bucketing instead.

**Message ids are `ROWID`s and are not stable.** Apple Mail can rebuild the envelope index and
reassign them. Use ids within a session; do not persist them. The RFC `Message-ID` header is the only
durable identifier.

The real schema on macOS 26 has ~55 tables, far more than any published description — including
`ews_folders`, `conversations`, `labels`, and several Apple-Intelligence categorization tables.
`mailgrep` reads only `messages`, `subjects`, `addresses`, `mailboxes` and `recipients`, and probes
for optional columns rather than requiring them.

**Message flags come from the `.emlx` plist trailer**, which is how `--unread` and `--flagged` work
for messages missing from the index. The bit layout is not contiguous: bit 0 read, 1 deleted,
2 answered, 3 encrypted, 4 flagged, 5 recent, 6 draft, 7 initial, 8 forwarded, 9 redirected, then
bits 10–15 attachment count, 16–22 priority, 23 signed, 24 junk, 25 not junk. Published orderings
that run the flags contiguously from bit 0 are wrong past bit 0.

Mail keeps the index in WAL mode, so `mailgrep` snapshot-copies the database together with its `-wal`
and `-shm` sidecars and opens the copy read-only. Reading the main database alone — or with
`immutable=1` — silently misses everything still sitting in the write-ahead log.

## Freshness cannot be measured, only bounded

Apple Mail does not fetch mail while it is closed — it holds an IMAP IDLE connection, which requires
the app to be running. There is no launchd agent doing it on Mail's behalf. So if Mail is shut, the
local store is stale by an unbounded amount.

**macOS exposes no last-sync timestamp.** There is no such column in the envelope index and no such
key in any plist under `MailData`. The `sync_state` column that does exist belongs to `ews_folders`,
is Exchange-only, and holds an opaque sync token rather than a time.

So `doctor` reports two facts instead of one guess: whether Mail.app is currently running, and how
long ago Mail last wrote to its store. Treat the second as a **lower bound**, not a guarantee — Mail
writes for local reasons (marking read, flagging, categorization), so a recent write does not prove a
server round-trip, and a quiet mailbox that synced seconds ago looks identical to one that has not
synced in a week.

Forcing a sync is possible via an AppleScript `synchronize` call, but that is an Apple Event and needs
a second TCC grant (Automation) on top of Full Disk Access — and that same grant permits sending and
deleting. `mailgrep` does not ask for it, which is why it stays single-grant and read-only.

## Exchange accounts are a hard limitation

EWS/Exchange accounts in Apple Mail do not materialise `.emlx` files; messages stay server-resident
and are fetched on demand. For those accounts a local reader returns **nothing**, not stale data.
`doctor` will show the mailboxes with low or zero message counts. IMAP, Gmail/Workspace, iCloud and
On My Mac accounts all store locally and work normally.

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

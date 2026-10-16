# notion-next-move

**Turn your mailbox into a per-customer "next action" board in Notion — read-only, evidence-first, agent-ready.**

Built for one-person sales teams and small teams who already run their
customers in Notion. Your mailbox stays untouched (read-only IMAP); your
customer facts stay in your own Notion pages; this tool only adds **evidence**
and **next-move tasks** on top.

```
your mailbox ──read-only IMAP──► scan ──► archive the original under the
(INBOX + Sent)                          customer's Notion page (readback-verified)
                                          │
                                          ▼
                                    next-move task in your Notion
                                    task database (dedupe + evidence link)
                                          │
        your reply lands in Sent ─────────┘
        → task is proposed complete, with evidence
```

The heavy machinery lives in deterministic Python; the judgement (what does
this customer need next) belongs to you or your AI agent. Nothing is ever
sent, deleted, moved or marked as read in your mailbox.

---

## What it actually does

| Step | Command | What happens |
|---|---|---|
| 1. Verify | `python -m nextmove doctor` | Checks config, secrets, read-only mailbox login, Notion token and schema mapping. Prints what it verified; never prints secrets. |
| 2. Look | `python -m nextmove check` | Dry scan: which mail is new, what would be archived. **Writes nothing.** |
| 3. Scan | `python -m nextmove scan` | Reads new mail read-only, archives originals under the customer's Notion page, advances checkpoints. Fails visibly; a failed fetch or write never advances. |
| 4. Suggest | `python -m nextmove suggest --input <run>/packet.json` | Proposes next-move tasks (one reply task per new customer message). No writes. |
| 5. Apply | `python -m nextmove apply --input <run>/suggestions.json [--commit]` | Validates and writes tasks into your database. Default is **dry-run**. |
| 6. Complete | `python -m nextmove complete --identity <key> --evidence ... --commit` | Marks a task done — only with a real evidence locator. |
| 7. Report | `python -m nextmove status` | Checkpoints, pending review items, open tasks. |

Why the split: steps 4–5 are where an AI agent plugs in. It reads the scan
packet, decides what each customer actually needs, and hands candidates back.
Or you do it yourself — the mechanical path needs no model at all.

## Guarantees you can verify

These are enforced in code and covered by the offline test suite
(`python tests/run_tests.py`, 37 checks, no network needed):

- **Read-only mail.** Every session is `EXAMINE`; only `BODY.PEEK`. No
  `STORE`/`DELETE`/`MOVE`/`COPY`/`EXPUNGE` command exists in the code, and the
  test suite asserts that plus the command trace against a fake server.
- **No message loss.** All UIDs are enumerated and cross-checked against the
  folder count. A checkpoint lives in your local state file; late-arriving
  old-dated mail still counts as new; a UIDVALIDITY change re-anchors by
  Message-ID and re-scans instead of losing or duplicating mail.
- **Idempotent.** Message-ID dedupe + a per-message marker appended to the
  page: re-running never duplicates evidence and never duplicates a task —
  even if you lose local state (the Notion side is checked too).
- **Honest failure.** A fetch failure, a rejected write, or an unreadable
  coverage never advances the checkpoint and never reports success.
- **No guessing.** A sender that does not resolve to a configured customer
  goes to a review queue (and optionally to a review page you control); it
  never creates, completes or advances a customer task.
- **Nothing destructive.** The Notion client has no delete/archive/move code
  path at all; the test suite asserts it.

## The task database

You bring your own Notion task database (or use
[`notion-template/`](notion-template/) to create one). Required: a **title**
and a **status** property. Optional but recommended: **任务标识** (a stable key
this tool writes so dedupe is exact), **来源链接** (URL — where the evidence
lives), **客户** (text), dates, **负责人** (people). Configure names in
`notion.config.json`; the tool reads your live schema and tells you if a name
does not exist instead of writing into the wrong property.

## Install

Any Python ≥ 3.10 (no dependencies — standard library only).

```bash
git clone https://github.com/<you>/notion-next-move.git
cd notion-next-move
cp config.example.json nextmove.config.json
# edit nextmove.config.json (mailbox host, Notion database id, customers)
python -m nextmove doctor
```

Secrets are **never** stored in the config:
- set `NEXTMOVE_IMAP_PASSWORD` and `NOTION_TOKEN` in your environment, or
- put `{"imap_password": "...", "notion_token": "..."}` in a local
  `secrets.json` next to the config (it is git-ignored).

If your provider (e.g. NetEase Enterprise / QQ / 163 / Gmail) requires an
app-specific password for IMAP, generate one in the web mailbox and use it
here. **Never** type it into a chat.

### macOS (first-time users)

See **[docs/MAC-SETUP.md](docs/MAC-SETUP.md)** — written for someone who has
never opened Terminal (that is exactly the person this was built for).

## First run

The default first run adopts a *baseline*: it records where your mailbox
currently ends and only picks up mail arriving from then on, so it never
imports years of history by accident. To import history instead, run
`python -m nextmove scan --baseline full` on the first run (still read-only;
only senders that resolve to a configured customer are archived).

## Plugging in an AI agent

The scan output (`packet.json`) is designed to be handed to an agent: new
messages with sender, subject, a trimmed preview and the evidence locator.
The agent (or you) writes `suggestions.json`; `apply` validates each candidate
mechanically before anything is written.

The repo ships an `AGENTS.md` that describes the contract an agent should
follow. See [AGENTS.md](AGENTS.md).

## What this is not

- It is **not** a CRM. Customer facts stay wherever you keep them (Notion
  pages/your own files). This tool does not ask you to restructure them.
- It does **not** send mail, draft replies, or change your customer's
  expectations. Every task is internal.
- It does **not** multi-account, multi-workspace or sync both ways. One
  mailbox, one task database.
- It does **not** decide business judgement for you. It prepares the evidence
  and the mechanics; you decide the next move.

## Requirements / limitations (honest list)

- One email account per config; protocol = IMAP over TLS.
- Folders must be identifiable by name (`INBOX`, `Sent`, or a localized name
  like `已发送` — the resolver handles modified-UTF-7 folder names).
- The tool reads only senders already listed in `customers`; everything else
  is queued for review. This is deliberate, but means an onboarding step: fill
  in your customer list.
- Notion API version `2025-09-03` (databases contain data sources). If your
  database holds several data sources, set `notion.task_data_source_id`.
- Verified end-to-end against a local fake IMAP server and a local fake
  Notion API. Real-provider specifics (your IMAP host, a client-specific
  auth-code) depend on your mailbox settings — `doctor` is the check.

## Tests

```bash
python tests/run_tests.py
```

No network, no credentials, no real data: a fake IMAP server (speaking real
IMAP to `imaplib`), a fake Notion API, and fully fictional customers in
reserved `.example` domains.

## License

MIT. See [LICENSE](LICENSE).

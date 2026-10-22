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
                                    judgement queue  ──►  your agent (or you)
                              state/judgment-request.json ──► judgment.json
                                          │
                                          ▼
                                    next-move task in your Notion task database
                                    (validated, deduped, evidence-linked)
```

**The split is deliberate.** Python does the mechanical, safety-critical work
(read-only mail, dedupe, checkpoints, validation, persistence). Deciding what
a customer actually needs next is *business judgement* — that is your agent's
job (or yours), expressed in a small JSON document the tool validates before
writing anything. The tool never invents a next action on its own.

---

## The daily run

One command, one complete loop:

```bash
python -m nextmove scan --apply-judgment
```

1. reads new mail read-only and archives each original under the customer's
   Notion page (a checkpoint advances only when everything in scope was
   processed and read back);
2. queues newly archived messages for judgement and writes
   `state/judgment-request.json`;
3. if `state/judgment.json` is waiting, validates and applies it **in the same
   run**.

An agent (or you) closes the loop between runs:

```bash
python -m nextmove judgment-request          # (re)write the request file
#  -> hand state/judgment-request.json to your agent; it writes state/judgment.json
python -m nextmove apply --judgment state/judgment.json --commit
```

If your agent runs *before* the next scan, the next `scan --apply-judgment`
picks the judgment up automatically. No vendor, model or orchestration
framework is assumed — the contract is two JSON files.

### What the request contains

The new messages (sender, subject, preview, evidence locator), the customer's
open tasks, and the decision schema. The agent writes decisions in **its own
words**: `create` a task (title / action / completion standard), `complete` a
task with evidence, `append` evidence to an existing task, or `hold` with a
reason. Everything is validated against the request before a single write:
a decision cannot invent a message, a task, a customer or an evidence
locator. See [AGENTS.md](AGENTS.md) for the full contract.

### No agent? The mechanical fallback still works

`suggest` + `apply --input` produce one "reply to this customer" task per
inbound message. Useful to try the plumbing; less useful than a real
judgement, which is the point.

## Commands

| Command | What it does | Writes? |
|---|---|---|
| `doctor` | Checks config, secrets, read-only mailbox login, Notion token and schema mapping. Never prints secrets. | no |
| `check` | Dry scan: which mail is new, what would be archived. | no |
| `scan [--apply-judgment]` | The daily chain above. | yes (commits) |
| `judgment-request` | (Re)writes the request file from the current queue. | file only |
| `apply --judgment <file> [--commit]` | Validates and persists a judgment document. | dry-run by default |
| `apply --input <file> [--commit]` | Mechanical fallback path. | dry-run by default |
| `suggest --input <packet>` | Mechanical task suggestions from a scan packet. | file only |
| `complete --identity … --evidence … --source … [--commit]` | Marks one task done with evidence. | dry-run by default |
| `status` | Checkpoints, judgement queue, review queue, open tasks. | no |

## Guarantees you can verify

Enforced in code and covered by the offline suite (`python tests/run_tests.py`,
66 checks, no network needed) plus `python tests/acceptance_daily_chain.py`
(27 checks driving the real CLI through seven runs):

- **Read-only mail.** Every session is `EXAMINE`; only `BODY.PEEK`. No
  `STORE`/`DELETE`/`MOVE`/`COPY`/`EXPUNGE` code path exists, and the suite
  asserts it against a fake server's command trace.
- **No message loss.** All UIDs enumerated and cross-checked against the
  folder count; late-arriving old-dated mail counts as new; a UIDVALIDITY
  change re-anchors by Message-ID and re-scans instead of losing or
  duplicating mail.
- **Idempotent.** Message-ID dedupe + a per-message marker on the page +
  a queue-based judgment guard: reruns never duplicate evidence, tasks or
  completed judgments.
- **Honest failure.** A fetch failure, a rejected write or unproven coverage
  never advances the checkpoint and never reports success. Unresolved mail
  (unknown sender) holds the checkpoint until it is resolved, and is reported.
- **Judgement boundaries.** A decision referencing a message that is not in
  the request, a task that is not in local state, or an evidence message id
  that was never archived is **rejected** — the Python side owns that check.
- **No destructive Notion operations.** The client has no delete/archive/move
  code path at all; the suite asserts it.
- **Human edits win.** A row the human edited *after* this tool's last write
  is never flipped to `Done` — it is reported as `protected` and left alone
  (see [Human-edit protection](#human-edit-protection)).
- **Unreadable input is never empty input.** `suggest` / `apply --input`
  refuse a file whose shape they do not recognise instead of reporting
  "0 suggestions" (see [Never "nothing to do"](#never-nothing-to-do)).

### Tiered dedupe (0.72 / 0.42 / 0.30)

A binary "duplicate or create" has two ways to be wrong, and both are bad:
too strict creates a near-duplicate of an existing row; too loose swallows
genuinely new work that merely resembles an old row. So a candidate gets one
of four verdicts:

| Verdict | When | What happens |
|---|---|---|
| `exact` | the stable identity key, or an identical normalized title | existing row is reused |
| `same` | similarity ≥ `refresh_ratio` (0.72) — or the customer has a **single** row and similarity ≥ `project_floor` (0.30) | existing row is reused |
| `near` | similarity ≥ `hold_ratio` (0.42) but below `refresh_ratio` | **held for a human**: nothing is created, nothing is overwritten, the run reports the resembling row and the score |
| `new` | below `hold_ratio` | a task is created |

Similarity is character-bigram **coverage of the longer title**, which is what
makes it usable on CJK text (a paraphrase that `difflib` scores ~0.4 lands at
0.5–0.9). It is measured against the longer side on purpose: the ported
pipeline divided by the *shorter* side, which let a stub row titled `Alpha`
score 1.0 against `Alpha Trading｜核价并回复` and swallow it — an existing
test now pins that.

Thresholds are yours to tune (`policy.dedupe`); `"mode": "exact"` restores the
old binary behaviour for anyone who wants it.

### Human-edit protection

Every write this tool makes to a task row records *when* it wrote
(`lastWriteAt` in the local ledger). Before a write that would change a
human-visible field (flipping a task to `Done`), the page's Notion
`last_edited_time` is compared with that stamp: if the human edited the row
after the tool's last write, the write is refused, reported as `protected`,
and the row is left as the human left it. Appending evidence is additive and
still allowed. When the comparison cannot be made, the page counts as
protected — absence of proof that we may write is not proof that we may.
Disable with `policy.protect_human_edits: false` (not recommended).

### Never "nothing to do"

Reading a file whose shape the tool does not recognise must never look like
an empty result: `suggest` on a foreign JSON reports `PACKET_UNRECOGNIZED`
and exits non-zero, `apply --input` reports `INPUT_UNRECOGNIZED`. This is the
generic form of a real failure seen in production — an extractor that grepped
for a producer's exact heading renamed it, extracted 0 items, and marked the
source consumed, so the real open item was lost for good.

### What "completion" does and does not mean — read this

The completion gate (`complete`, and `complete` decisions) checks that a
completion **claim is well-formed and traceable**:

- the task exists in the local ledger with a live Notion page;
- the note carries evidence text, a source locator, and a date or message id;
- the message id resolves to a message **this tool actually archived**, and
  the locator points at that archived page.

It does **not** verify that the claimed outcome really happened, that the
evidence proves it, or that the customer agrees. A task marked `Done` means
*"a traceable claim was recorded"* — the business judgement behind it stays
with you. The tool reports its own scope in the result
(`well_formed_claim_not_verified_outcome` /
`traceable_to_archived_source_not_business_truth`); do not read it as
"verified complete".

## The task database

You bring your own Notion task database (or use
[`notion-template/`](notion-template/) to create one). Required: a **title**
and a **status** property. Optional but recommended: **任务标识** (a stable key
this tool writes so dedupe is exact), **来源链接** (URL — where the evidence
lives), **客户** (text), dates, **负责人** (people). Configure names in
`notion.config.json`; the tool reads your live schema and refuses to write
when a name does not exist instead of guessing.

## Install

Any Python ≥ 3.10 (no dependencies — standard library only).

```bash
git clone https://github.com/quaner1234-cmd/notion-next-move.git
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
never opened Terminal. It includes the exact commands for the whole daily
chain and a copy-paste cron line.

## First run

The default first run adopts a *baseline*: it records where your mailbox
currently ends and only picks up mail arriving from then on, so it never
imports years of history by accident. To import history instead, run
`python -m nextmove scan --baseline full` on the first run (still read-only;
only senders that resolve to a configured customer are archived).

## Scheduling

Nothing is scheduled for you. One line is enough (adjust the time):

```bash
# run this first and copy the path it prints (cron does not read your shell
# profile, so the line must carry the exact interpreter path):
command -v python3
crontab -e
# every weekday at 08:30, full daily chain (replace the path with yours):
30 8 * * 1-5 cd "$HOME/Documents/notion-next-move" && mkdir -p logs && /usr/local/bin/python3 -m nextmove scan --apply-judgment >> logs/scan.log 2>&1
```

If you already run a sync task (any tool), keep it and just add the
`scan --apply-judgment` step to it — do not create a second scheduler.

## What this is not

- It is **not** a CRM, and it does not ask you to restructure your Notion.
- It does **not** send mail, draft replies, or change your customer's
  expectations. Every task is internal.
- It does **not** decide the next action for you — that is the agent/user
  side of the contract, by design.
- It does **not** multi-account, multi-workspace or sync both ways. One
  mailbox, one task database.
- A `Done` task is a recorded, traceable claim — not an independently
  verified outcome (see above).

## Requirements / limitations (honest list)

- One email account per config; protocol = IMAP over TLS.
- Folders must be identifiable by name (`INBOX`, `Sent`, or a localized name
  like `已发送` — the resolver handles modified-UTF-7 folder names).
- Only senders listed in `customers` are processed; everything else goes to
  the review queue. Deliberate, but it means an onboarding step.
- Notion API version `2025-09-03` (databases contain data sources). If your
  database holds several data sources, set `notion.task_data_source_id`.
- Verified end-to-end against a local fake IMAP server, a local fake Notion
  API, and one live Notion workspace. Real-provider specifics (your IMAP
  host, your client authorization code) depend on your mailbox — `doctor` is
  the check.

## Tests

```bash
python tests/run_tests.py                 # 66 offline unit/loop checks
python tests/acceptance_daily_chain.py    # 27 checks: 7 scheduled runs via the CLI
python tests/mutation_test.py             # proves the checks above can go red
```

No network, no credentials, no real data: a fake IMAP server (speaking real
IMAP to `imaplib`), a fake Notion API, and fully fictional customers in
reserved `.example` domains.

Every claim above is pinned by a test that fails when the behaviour is
broken — the tiered-dedupe and protection paths were checked by re-running
the suite with each defect reintroduced (revert the similarity formula,
drop the `near` tier, ignore the configured thresholds, skip the hold path,
blind the human-edit check, remove the completion gate, treat an
unrecognised packet as empty): all seven turn the suite red and the source
is restored byte-for-byte afterwards.

## License

MIT. See [LICENSE](LICENSE).

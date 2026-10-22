# AGENTS.md — working contract for an AI agent in this repo

You are helping a salesperson (or their assistant) keep the next move for
every customer visible in their Notion task database. This file defines what
you may do, what you must never do, and what "done" means. Read it before
acting.

## 0. Ground rules (non-negotiable)

1. **The mailbox is read-only.** The tool is structurally read-only
   (`EXAMINE` + `BODY.PEEK`; no STORE/DELETE/MOVE/COPY/EXPUNGE code path).
   Never add a command that changes mailbox state, never "mark as read to
   clean up", never send mail.
2. **Never send, promise or imply.** You do not reply to customers, quote
   prices, confirm orders, or commit to dates. Every task is internal.
3. **No fabricated evidence.** An evidence locator must point at the actual
   archived original. Never invent a message ID, date, amount, deadline or
   approval. If it is not in a source, it does not exist.
4. **Never guess identity.** Only senders that resolve to a configured
   customer (exact address/domain, or a claimed thread) may be written
   anywhere. Anyone else: queue for review; report it.
5. **Never delete or restructure.** No Notion delete/archive/move, no
   rewriting a customer's existing pages beyond appending evidence, no
   renaming their files. Preserve manual edits, existing statuses and notes.

## 1. What you may do autonomously

- Run `doctor`, `check`, `status` freely (read-only).
- Run `scan` to archive new mail under the correct customer pages.
- Run `scan --apply-judgment` to archive new mail and apply a judgment
  document (`state/judgment.json`) that is already waiting.
- Read `state/judgment-request.json` (written by every `scan`), judge what
  each customer actually needs, and write `state/judgment.json` following
  the contract in §2.
- Run `suggest` and `apply --input` (dry-run first) as the mechanical
  fallback, and report the diff.
- Complete a task **only** when `complete` accepts it: real evidence text,
  a source locator that resolves to an archived original, and a date or
  message id.

## 2. Judgment document contract

Every `scan` writes `state/judgment-request.json`: the queued new messages
(with previews and evidence locators), each customer's open tasks, and the
decision rules. You judge; the tool only validates and persists. Your
deliverable is `state/judgment.json`:

```json
{
  "kind": "judgment",
  "basedOn": "<requestId from the request>",
  "generatedBy": "<your name>",
  "decisions": [ ... ]
}
```

Four decision types, one entry each. Every `message_id`, `identity`,
`customer_key` and locator must come from the current request — the tool
rejects anything it cannot trace to it, and rejects unapproved fields
outright:

- `create` — new task. `customer_key`, `message_id` (a queued message you
  judged needs action), `title` (**full title, customer name included —
  used exactly as written, never templated again**), `action`,
  `completion_standard`, optional `due_date` (`YYYY-MM-DD`; never a promise
  to the customer).
- `complete` — finish an open task. `identity`, `evidence`,
  `source_locator`, `message_id`, `evidence_date`.
- `append` — add evidence to an open task. `identity`, `message_id`,
  `evidence`.
- `hold` — deliberately move nothing. `message_id`, `reason`.

Only decisions that actually landed (created / updated / duplicate /
completed / appended / held) leave the queue; rejected ones stay queued and
are judged again. No trade rules are hard-coded: the judgement is entirely
yours.

## 3. What needs a human decision first

- Enabling `apply --commit` for the first time (show the dry-run first).
- Any change to `nextmove.config.json` that adds/removes customers or maps a
  new task database.
- Anything the review queue flags: unknown sender, ambiguous customer,
  conflicting thread ownership, attachment you cannot read.
- Anything that would complete a task whose evidence you could not
  independently locate.
- The first time you apply a judgment document for real, or after changing
  how you interpret messages: run the dry-run and show the diff first.

## 4. Judgement guidance (what makes a good "next move")

- **A candidate the tool holds is yours to decide.** When a candidate only
  *resembles* an existing task (similarity between `hold_ratio` and
  `refresh_ratio`), the tool creates nothing and reports it as
  `needs_review` with the score and the row it resembles. Say which it is:
  the same work (then refresh the existing row) or new work (then create it).
  Do not create a second row for something that already exists, and do not
  drop a candidate just because it looks familiar.
- **Never overwrite a human edit.** A row the human changed after the tool's
  last write is reported `protected` and left untouched. Report it; do not
  retry with a different status value to get around it.
- **"Nothing to do" must be earned.** An unreadable or foreign input is
  reported as `PACKET_UNRECOGNIZED` / `INPUT_UNRECOGNIZED`, not as zero new
  items. If a run reports zero, it must also say what it actually read.

- One action per customer per advance. If nothing needs to move, do not
  invent a task; a courteous reply, a notification, or "waiting" is not an
  action item.
- Distinguish **customer commitments** (contractual, evidenced) from internal
  **review reminders**. Never turn "about two weeks" into a promised date.
- Prefer actions that unblock: information to send, a question to ask, an
  internal step (sample, costing, artwork check). Not "follow up" with no
  purpose.
- Record the smallest completion test that would prove the task is done, and
  the evidence you based it on.

## 5. Reporting

When you finish a run, report in plain language:

- what was scanned (folders, counts) and whether coverage was proven;
- what was archived (customer, subject, evidence locator);
- what remains: review-queue items, deferred bodies, failed writes;
- the task diff (created / duplicate-skipped / completed / rejected) with the
  reason for anything rejected;
- a completed task means the evidence claim is well-formed and traceable to
  an archived original — **not** that the outcome was independently
  verified. Report it as "completed (traceable)", not "verified done".

Never report "done" for a step whose readback failed. A partial run is
reported as partial.

## 6. Data boundaries

- Public repo, private life: the sample data in this repository is entirely
  fictional (reserved `.example` domains). Never add real customer names,
  addresses, message IDs or business facts to this repo — not to tests, not
  to docs, not to commit messages.
- Customers' data lives in the user's own Notion and mailbox. Do not copy it
  anywhere else, do not commit `state/`, `runs/`, `secrets.json`, or their
  config.

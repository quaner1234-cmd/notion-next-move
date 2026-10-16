# macOS setup — for a first-time user

This guide assumes you have **never used Terminal** before. Every step is
copy-paste. Total time: about 20 minutes. Nothing here sends mail or changes
your customers' data.

You need: a Mac, your work mailbox address, your Notion account, and the
person who set this up for you (call them "your helper") reachable for one
question at the end.

---

## Step 1 — Install Python (one time)

1. Open **https://www.python.org/downloads/macos/** in Safari.
2. Click the big yellow button (**Download Python 3.x**). Open the downloaded
   `.pkg` file and click through the installer (Continue → Agree → Install).
3. Check it worked: press `Cmd + Space`, type `Terminal`, press Return.
   In the window that opens, type (or paste):

   ```bash
   python3 --version
   ```

   You should see something like `Python 3.12.x`. If you see an error, redo
   step 1.

## Step 2 — Get the project

In the Terminal window, paste these two lines (one at a time, then Return):

```bash
cd ~/Documents
git clone https://github.com/quaner1234-cmd/notion-next-move.git
```

If it says `command not found: git`, a small window will pop up offering to
install "command line developer tools" — click **Install**, wait, then run
the `git clone` line again.

Then:

```bash
cd notion-next-move
```

## Step 3 — Your mailbox app password

Most providers do **not** accept your normal web password in mail programs.
You need an "app password" / "client authorization code":

1. Log into your mailbox in the browser (e.g. `mail.qiye.163.com`,
   `exmail.qq.com`, `mail.google.com`).
2. Find **Settings** → something like **POP3/IMAP/SMTP** or
   **Client authorization** or **App passwords**.
3. Turn IMAP on if it is off, and generate an app password / authorization
   code. Copy it — you only see it once.
4. Note your **IMAP server address** (your provider's help page lists it, e.g.
   `imap.qiye.163.com` for NetEase Enterprise, `imap.gmail.com` for Gmail).

> Keep this code private. This project never asks you to paste a password
> into the chat with your AI helper. If anyone asks you to, stop — that is
> not how this tool works.

## Step 4 — Fill in the two config files

In Terminal:

```bash
cp config.example.json nextmove.config.json
open -e nextmove.config.json
```

TextEdit opens. Fill in:

- `mailbox.host` — your IMAP server (from Step 3)
- `mailbox.user` — your full email address
- `notion.task_database_id` — the 32-character id from your Notion task
  database URL (see `notion-template/README.md` step 3; your helper may give
  you this)
- the `customers` list — each customer's name, their email addresses, and
  their Notion page id. Start with the two or three customers you email most.

Save (`Cmd + S`) and close TextEdit.

Now the secrets file. In Terminal:

```bash
cat > secrets.json <<'EOF'
{
  "imap_password": "PASTE_YOUR_APP_PASSWORD_HERE",
  "notion_token": "PASTE_YOUR_NOTION_TOKEN_HERE"
}
EOF
```

Then open it and replace both placeholders:

```bash
open -e secrets.json
```

Replace `PASTE_YOUR_APP_PASSWORD_HERE` with your app password and
`PASTE_YOUR_NOTION_TOKEN_HERE` with your Notion token, save (`Cmd + S`) and
close. **Straight quotes only** — if TextEdit turns `"` into curly quotes
(`“ ”`), the file breaks (the fix is in Troubleshooting below).

`secrets.json` is ignored by git and never shared.

Where does the Notion token come from? Your helper creates an integration at
`https://www.notion.so/my-integrations` and hands you the token — or walks
you through it. Same rule as the mail password: it stays on your machine.

## Step 5 — Check everything

```bash
python3 -m nextmove doctor
```

Read the output. It prints, line by line, whether it could log into your
mailbox (read-only), whether the Notion token works, and whether the
properties in your database match the config. **Any problem is described in
plain words** — fix what it names and run it again.

## Step 6 — Look before touching

```bash
python3 -m nextmove check
```

This writes **nothing**. It shows which messages are new and what would be
archived. If the customer names it shows are correct, continue.

## Step 7 — The first real run

```bash
python3 -m nextmove scan
```

This reads new mail (read-only), copies the originals under the right
customer's Notion page, and writes one file:

```
state/judgment-request.json
```

That file lists the new messages waiting for a decision. It does **not**
decide anything itself — the decision (what each customer needs next) is
made by your AI agent (or by you, or by your helper).

**If you have an AI agent** (e.g. Codex): give it that file and say:

> Read `state/judgment-request.json` and write your decisions to
> `state/judgment.json`, following the instructions inside the request.

The agent writes a small file like this (example, made-up customer):

```json
{
  "kind": "judgment",
  "basedOn": "sha256:the-requestId-from-the-request-file",
  "decisions": [
    {
      "decision": "create",
      "customer_key": "alpha",
      "message_id": "<the message-id from the request>",
      "title": "Alpha Trading｜核价 300 件并回复报价",
      "action": "与工厂核价后回复客户报价区间与交期",
      "completion_standard": "报价邮件已发出且客户已收到",
      "generatedBy": "your-agent-name"
    }
  ]
}
```

Then apply it:

```bash
python3 -m nextmove apply --judgment state/judgment.json --commit
```

(or just wait — the next daily run picks it up automatically, see Step 8).

**If you have no agent yet**, the mechanical fallback still works — it turns
each new customer message into one "reply to this customer" task:

```bash
# find the newest run folder and make suggestions from it
PKT="$(ls -td runs/*-scan | head -1)/packet.json"
python3 -m nextmove suggest --input "$PKT"
python3 -m nextmove apply --input "$(dirname "$PKT")/suggestions.json" --commit
```

Check Notion: the tasks should appear in your database with a source link.

## Step 8 — Make it regular (optional)

Once you are happy, let it run every morning — **one command does the whole
chain** (read mail → archive → apply any decision you or your agent
prepared):

```bash
python3 -m nextmove scan --apply-judgment
```

To schedule it, first find the exact path of your Python (cron does not know
your shortcuts):

```bash
command -v python3
```

It prints something like `/usr/local/bin/python3` — copy that exact text.
Then:

```bash
crontab -e
```

A text editor opens. Add this one line (replace the path with the one you
copied; adjust the time `8`/`30` if you like):

```
30 8 * * 1-5 cd "$HOME/Documents/notion-next-move" && mkdir -p logs && /usr/local/bin/python3 -m nextmove scan --apply-judgment >> logs/scan.log 2>&1
```

Save and close (in the default editor: press `Esc`, type `:wq`, Return).
That runs every weekday at 08:30. It does not touch mail or send anything.

To check it worked, the next day:

```bash
tail -5 logs/scan.log
```

If the file is empty or missing, the usual cause is the Python path — redo
the `command -v python3` step and fix the line. If you are not comfortable
editing that file, ask your helper — it is a one-line change.

## How to tell it worked

```bash
python3 -m nextmove status
```

Shows, in plain terms: what was scanned, what is waiting for a decision,
what is waiting for you to review (unknown senders), and the open tasks.
Plus: open Notion and look at your task database.

---

## Troubleshooting (the usual ones)

| Symptom | Meaning | Fix |
|---|---|---|
| `NO_IMAP_PASSWORD` | secrets.json is missing/typo'd, or the JSON quotes are the curly kind from TextEdit | Re-run step 4 with straight quotes `"` |
| `IMAP login/connect failed` | Wrong host, or you used the web password | Check Step 3 — it must be the **app password**, and the exact server address |
| `folder '...' not found or ambiguous` | Your provider names folders differently | Run the command it suggests (it lists the actual folder names) and put the right one in `sent_folder` |
| Notion complains about a property | The config name does not match the database | `doctor` prints both; change the config name or the Notion property |
| `JUDGMENT_INVALID` | The agent's judgment file has a problem (it says which decision and why) | Send the message back to the agent with the reason; nothing was written |
| A message sits in the judgment queue | No decision was written for it yet | Run `judgment-request`, give the file to your agent, then `apply --judgment … --commit` |

## What you must do yourself (nobody can do these for you)

- **Generate the mail app password** in your mailbox's web settings (Step 3).
  It never leaves your Mac.
- **Authorize the Notion integration** and **share the task database and the
  customer pages** with it (in Notion: page `···` menu → Connections).
  Without the share, nothing can be written.
- **Decide the sync time** (Step 8) — nothing is scheduled for you.
- **Review what the agent decided** before you trust it — especially the
  first week. The tool validates that a decision is *well-formed and
  traceable to an archived original*; it cannot know whether the action
  itself is the right business move. That part is you.

## What this tool will never do

- Read, change or delete mail it did not need (and everything it reads, it
  reads without marking as read).
- Send anything to your customers.
- Write to any Notion page that is not in your config's customer list.
- Mark a task done without a recorded evidence link to the original message.
- Print your passwords or token anywhere.

# macOS setup — for a first-time user

This guide assumes you have **never used Terminal** before. Every step is
copy-paste. Total time: about 15 minutes. Nothing here sends mail or changes
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
git clone https://github.com/<your-helper>/notion-next-move.git
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

Replace both placeholders between the quotes, save the command (Return runs
it). `secrets.json` is ignored by git and never shared.

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
customer's Notion page, and advances its own bookkeeping. Then:

```bash
python3 -m nextmove suggest --input state/../runs/<the newest folder>/packet.json
```

(Or ask your helper / AI agent to do step 6–7 — this is where a helper is
worth one message.)

Then review and apply:

```bash
python3 -m nextmove apply --input <the newest run folder>/suggestions.json
python3 -m nextmove apply --input <the newest run folder>/suggestions.json --commit
```

The first command is a **dry run** — it shows what would be created. Only
the second one writes. Check Notion: the tasks should appear in your
database with a source link.

## Step 8 — Make it regular (optional)

Once you are happy, you can let it run automatically every morning. Ask your
helper to add it to `crontab` (macOS's scheduler):

```bash
crontab -e
```

and add a line like:

```
30 8 * * 1-5 cd ~/Documents/notion-next-move && /usr/bin/python3 -m nextmove scan >> logs/scan.log 2>&1
```

That runs the read+archive step every weekday at 08:30. It does not touch
mail or send anything. If you are not comfortable editing that file, ask
your helper — it is a one-line change.

---

## Troubleshooting (the usual three)

| Symptom | Meaning | Fix |
|---|---|---|
| `NO_IMAP_PASSWORD` | secrets.json is missing/typo'd, or the JSON quotes are the curly kind from TextEdit | Re-run step 4 with straight quotes `"` |
| `IMAP login/connect failed` | Wrong host, or you used the web password | Check Step 3 — it must be the **app password**, and the exact server address |
| `folder '...' not found or ambiguous` | Your provider names folders differently | Run the command it suggests (it lists the actual folder names) and put the right one in `sent_folder` |
| Notion complains about a property | The config name does not match the database | `doctor` prints both; change the config name or the Notion property |

## What this tool will never do

- Read, change or delete mail it did not need (and everything it reads, it
  reads without marking as read).
- Send anything to your customers.
- Write to any Notion page that is not in your config's customer list.
- Print your passwords or token anywhere.

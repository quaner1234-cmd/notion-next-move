# Notion template — a task database this tool can drive

You have two options: keep your existing task database, or create one from
this template. The template is deliberately minimal; the tool adapts to your
own property names via `nextmove.config.json`.

## Option A — use the template (fastest)

1. In Notion, create a database (Table view is fine) with the properties
   below. Names in **bold** must match the example config; everything else is
   optional but recommended.
2. Connect your integration to it: open the database → `...` menu →
   **Connections** → add your integration. (The API can only see pages the
   integration is explicitly connected to.)
3. Copy the database id: it is the 32-character hex string in the database
   URL: `https://www.notion.so/<workspace>/<DATABASE_ID>?v=...`
4. Put it in `nextmove.config.json` → `notion.task_database_id`.

### Properties

| Property (example name) | Type | Role in config | Notes |
|---|---|---|---|
| **任务名称** (or `Name`) | Title | `properties.title` | Required. The task text. |
| **状态** | Status (or Select) | `properties.status` | Required. Values are mapped in `status_map`. Status type with options: 未开始 / 进行中 / 完成 / 已归档 (or Todo / In progress / Done / Archived). A plain Select works too. |
| 任务标识 | Text (rich text) | `properties.key` | Strongly recommended. The tool writes a stable key here; dedupe becomes exact. |
| 来源链接 | URL | `properties.source_locator` | Recommended. Where the evidence lives (the archived original). |
| 客户 | Text | `properties.customer` | Recommended. Human-readable customer name. |
| 负责人 | People | `properties.owner` | Optional. New tasks are assigned to `owner_user_id` when set. |
| 截止日期 | Date | `properties.due_date` | Optional. Only for a **customer-confirmed** commitment. |
| 内部复查日期 | Date | `properties.internal_review` | Optional. An internal reminder — never presented as a customer deadline. |
| 最后核验 | Date | `properties.last_checked` | Optional. Last time this task's source was actually checked. |

If a name differs in your database, either rename the property or change it
in the config — `doctor` tells you exactly which name it could not find.

### Status map

```json
"status_map": {
  "todo": "未开始",
  "in_progress": "进行中",
  "done": "完成",
  "archived": "已归档"
}
```

The tool **never invents** a status option: if `status_map.todo` names a
value that does not exist on a Status property, `doctor` and `apply` refuse
to run rather than creating a malformed page.

## Option B — keep your existing database

Point `notion.properties.*` at your own property names. Minimum viable:
title + status. The tool reads your live schema on every run and reports
mismatches instead of writing into the wrong field.

## Views worth creating (3 minutes, big payoff)

Once tasks start landing:

1. **Open tasks** — filter `状态 != 完成` and `状态 != 已归档`, sort by
   `截止日期` ascending then `内部复查日期`. This is your daily board.
2. **Needs review / stale** — filter `来源链接` is empty, or sort by
   `最后核验` descending to spot cards that have not been re-checked.
3. **Waiting on customer** — group by `客户`, filter `状态 = 进行中`. Useful
   when a customer asks "where are we?".

These are ordinary Notion views — nothing in this tool creates them, and
nothing breaks if you change them.

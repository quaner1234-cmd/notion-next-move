"""Configuration: one JSON file plus a local secrets file. No hidden state.

Secrets are never stored in the config file. They are resolved, in order, from:

  1. an environment variable (config keys `mailbox.password_env` /
     `notion.token_env`, e.g. exported in your shell profile), then
  2. a local secrets file (`secrets_file`, default `./secrets.json` next to the
     config) with keys `imap_password` and `notion_token`.

The CLI prints only whether a secret was found and where — never its value.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

REQUIRED_PROPERTY_ROLES = ("title", "status")
OPTIONAL_PROPERTY_ROLES = (
    "owner",
    "customer",
    "key",
    "due_date",
    "internal_review",
    "last_checked",
    "source_locator",
)

DEFAULTS = {
    "mailbox": {
        "account": "work",
        "host": "",
        "port": 993,
        "user": "",
        "password_env": "NEXTMOVE_IMAP_PASSWORD",
        "inbox_folder": "INBOX",
        "sent_folder": "Sent",
        "timeout_seconds": 30,
    },
    "notion": {
        "token_env": "NOTION_TOKEN",
        "notion_version": "2025-09-03",
        "task_database_id": "",
        "task_data_source_id": None,
        "owner_user_id": None,
        "properties": {role: None for role in REQUIRED_PROPERTY_ROLES + OPTIONAL_PROPERTY_ROLES},
        "status_map": {
            "todo": "Todo",
            "in_progress": "In progress",
            "done": "Done",
            "archived": "Archived",
        },
    },
    "customers": [],
    "policy": {
        "timezone": "Asia/Shanghai",
        "body_fetch_limit_per_run": 60,
        "title_template": "{customer}｜{action}",
        "state_dir": "state",
        "runs_dir": "runs",
        "secrets_file": "secrets.json",
    },
}


class ConfigError(Exception):
    """Raised when the configuration cannot be used at all."""


def _merge(defaults, value):
    out = copy.deepcopy(defaults)
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(out.get(key), dict) and isinstance(item, dict):
                out[key] = _merge(out[key], item)
            else:
                out[key] = item
    return out


def load(path):
    """Load and structurally validate a config file. Raises ConfigError."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a JSON object")
    config = _merge(DEFAULTS, raw)
    config["_path"] = str(path.resolve())
    config["_dir"] = str(path.resolve().parent)
    return config


def validate(config):
    """Return a list of human-readable problems. Empty list = usable."""
    problems = []
    mailbox = config.get("mailbox", {})
    if not mailbox.get("host"):
        problems.append("mailbox.host is empty")
    if not mailbox.get("user"):
        problems.append("mailbox.user is empty")
    if not str(mailbox.get("port", "")).isdigit():
        problems.append("mailbox.port must be a number")
    notion = config.get("notion", {})
    db_id = str(notion.get("task_database_id") or "").replace("-", "")
    if len(db_id) != 32 or any(c not in "0123456789abcdefABCDEF" for c in db_id):
        problems.append("notion.task_database_id is missing or not a 32-hex Notion id")
    properties = notion.get("properties") or {}
    for role in REQUIRED_PROPERTY_ROLES:
        if not properties.get(role):
            problems.append(f"notion.properties.{role} is required")
    customers = config.get("customers", [])
    if not isinstance(customers, list) or not customers:
        problems.append("customers is empty — at least one customer mapping is required")
    seen = set()
    for item in customers if isinstance(customers, list) else []:
        key = str(item.get("key") or "").strip()
        if not key:
            problems.append("a customer entry has no key")
        elif key in seen:
            problems.append(f"duplicate customer key: {key}")
        seen.add(key)
        if not str(item.get("display_name") or "").strip():
            problems.append(f"customer {key or '?'} has no display_name")
    policy = config.get("policy", {})
    if not str(policy.get("timezone") or "").strip():
        problems.append("policy.timezone is empty")
    return problems


def customers_by_key(config):
    out = {}
    for item in config.get("customers", []):
        key = str(item.get("key") or "").strip()
        if key:
            out[key] = item
    return out


def customer_for_address(config, address):
    """Resolve a customer key from an email address when it is unambiguous.

    Only exact address matches (or exact domain matches for non-public
    domains) count. Returns None when nothing matches — never guess.
    """
    address = (address or "").strip().lower()
    if "@" not in address:
        return None
    domain = address.rsplit("@", 1)[1]
    matches = []
    for item in config.get("customers", []):
        for known in item.get("email_addresses", []) or []:
            if str(known).strip().lower() == address:
                matches.append(item.get("key"))
        for known in item.get("email_domains", []) or []:
            if str(known).strip().lower() == domain:
                matches.append(item.get("key"))
    unique = [key for key in dict.fromkeys(matches) if key]
    return unique[0] if len(unique) == 1 else None


def resolve_secret(config, name):
    """Resolve 'imap_password' or 'notion_token'. Returns (value|None, source)."""
    if name == "imap_password":
        env_key = config["mailbox"].get("password_env") or "NEXTMOVE_IMAP_PASSWORD"
    elif name == "notion_token":
        env_key = config["notion"].get("token_env") or "NOTION_TOKEN"
    else:
        raise ConfigError(f"unknown secret name: {name}")
    value = os.environ.get(env_key)
    if value:
        return value, f"env:{env_key}"
    secrets_path = Path(config["_dir"]) / (config["policy"].get("secrets_file") or "secrets.json")
    if secrets_path.exists():
        try:
            data = json.loads(secrets_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None, f"file:{secrets_path} (invalid JSON)"
        value = data.get(name)
        if value:
            return str(value), f"file:{secrets_path}"
    return None, f"env:{env_key} or file:{secrets_path}"


def state_path(config):
    return Path(config["_dir"]) / (config["policy"].get("state_dir") or "state") / "state.json"


def runs_dir(config):
    return Path(config["_dir"]) / (config["policy"].get("runs_dir") or "runs")

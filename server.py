"""Telegram MCP server for Claude.

Two parts:
- bot_*  : a personal bot. It only talks to its owner (owner_chat_id); messages from anyone else are ignored.
- tg_*   : the owner's own account through Telethon, READ-ONLY (no send/edit/delete tools exist here).

Secrets live in config.json and account.session next to this file; both are created by setup.py.
"""
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
INBOX = BASE / "inbox.jsonl"
STATE = BASE / "state.json"
SESSION = BASE / "account"  # Telethon adds .session
DOWNLOADS = Path.home() / "Downloads" / "Telegram MCP"

mcp = MCPServer("telegram")

# httpx logs full request URLs, which contain the bot token: keep it quiet
import logging
for _name in ("httpx", "httpcore", "telethon"):
    logging.getLogger(_name).setLevel(logging.WARNING)


def cfg() -> dict:
    if not CONFIG.exists():
        raise ToolError("config.json not found: run setup.py first")
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "file"


def unique_path(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / safe_name(name)
    i = 1
    while p.exists():
        p = folder / f"{p.stem} ({i}){p.suffix}"
        i += 1
    return p


# ---------------------------------------------------------------- bot

def bot_api(method: str, **kw):
    c = cfg()
    token = c.get("bot_token")
    if not token:
        raise ToolError("bot is not configured: run setup.py")
    r = httpx.post(f"https://api.telegram.org/bot{token}/{method}", timeout=60, **kw)
    data = r.json()
    if not data.get("ok"):
        raise ToolError(f"Telegram error: {data.get('description')}")
    return data["result"]


def owner_id() -> int:
    oid = cfg().get("owner_chat_id")
    if not oid:
        raise ToolError("owner_chat_id missing: run setup.py")
    return int(oid)


def describe_bot_message(m: dict) -> dict:
    item = {
        "message_id": m["message_id"],
        "date": datetime.fromtimestamp(m["date"]).strftime("%Y-%m-%d %H:%M"),
        "text": m.get("text") or m.get("caption") or "",
    }
    fwd = m.get("forward_origin") or {}
    src = fwd.get("sender_user") or fwd.get("chat") or {}
    if src or fwd.get("sender_user_name"):
        item["forwarded_from"] = src.get("title") or " ".join(
            x for x in (src.get("first_name"), src.get("last_name")) if x
        ) or fwd.get("sender_user_name")
    files = []
    if m.get("photo"):
        best = m["photo"][-1]
        files.append({"type": "photo", "file_id": best["file_id"], "name": f"photo_{m['message_id']}.jpg"})
    for kind in ("document", "video", "audio", "voice"):
        if m.get(kind):
            f = m[kind]
            files.append({"type": kind, "file_id": f["file_id"],
                          "name": f.get("file_name") or f"{kind}_{m['message_id']}",
                          "size": f.get("file_size")})
    if files:
        item["files"] = files
    return item


HELP_TEXT = (
    "Я бот-помощник для Claude. Всё, что ты сюда пишешь или пересылаешь, Claude видит в своей сессии.\n\n"
    "Что можно делать:\n"
    "• Пересылать фото методичек, PDF, файлы: Claude их скачает и использует (до 20 МБ).\n"
    "• Писать заметки или задания: «сделай лабу 4 по физике».\n"
    "• Получать от Claude готовые отчёты и напоминания.\n\n"
    "Потом в Claude напиши, например: «возьми файлы из бота».\n\n"
    "Команды:\n"
    "/help: эта справка\n"
    "/status: сколько сообщений ждёт Claude\n\n"
    "Бот отвечает на команды только пока открыта сессия Claude, но сообщения не теряются: "
    "Telegram хранит их до 24 часов."
)
COMMANDS = [
    {"command": "help", "description": "Что умеет бот"},
    {"command": "status", "description": "Сколько сообщений ждёт Claude"},
]

_lock = threading.Lock()
_poller_alive = False


def _state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def _inbox_lines() -> list:
    return INBOX.read_text(encoding="utf-8").splitlines() if INBOX.exists() else []


def _reply(chat_id: int, text: str):
    try:
        bot_api("sendMessage", json={"chat_id": chat_id, "text": text})
    except Exception:
        pass


# ------------------------------------------------ update pipeline (middleware)
#
# Every incoming message goes through a chain: each middleware gets the context and `nxt`
# (the rest of the chain). It can handle the message and stop, or call nxt(ctx) to pass it on.
#
#   owner_only -> commands -> store_in_inbox
#
# The owner check sits first, so nothing after it ever sees a stranger's message.

class Ctx:
    def __init__(self, message: dict, owner: int, state: dict, inbox):
        self.message = message
        self.chat_id = message.get("chat", {}).get("id")
        self.text = (message.get("text") or "").strip()
        self.cmd = self.text.split()[0].split("@")[0].lower() if self.text.startswith("/") else ""
        self.owner = owner
        self.state = state
        self.inbox = inbox
        self.added = 0


Handler = Callable[[Ctx], None]
Middleware = Callable[[Ctx, Handler], None]


def owner_only(ctx: Ctx, nxt: Handler):
    """Access control: only the owner's messages go further."""
    if ctx.chat_id != ctx.owner:
        if ctx.cmd:
            _reply(ctx.chat_id, "Это приватный бот.")
        return
    nxt(ctx)


def commands(ctx: Ctx, nxt: Handler):
    """Bot commands are answered here and are not stored for Claude."""
    if ctx.cmd in ("/help", "/start"):
        _reply(ctx.chat_id, HELP_TEXT)
    elif ctx.cmd == "/status":
        waiting = len(_inbox_lines()) + ctx.added - ctx.state.get("seen", 0)
        _reply(ctx.chat_id, f"Ждут Claude: {max(waiting, 0)} сообщ.")
    else:
        nxt(ctx)


def store_in_inbox(ctx: Ctx):
    """Final handler: save the message for Claude (bot_inbox)."""
    ctx.inbox.write(json.dumps(describe_bot_message(ctx.message), ensure_ascii=False) + "\n")
    ctx.added += 1


MIDDLEWARES: list = [owner_only, commands]


def build_pipeline(middlewares: list, final: Handler) -> Handler:
    handler = final
    for mw in reversed(middlewares):
        handler = (lambda m, n: lambda ctx: m(ctx, n))(mw, handler)
    return handler


PIPELINE = build_pipeline(MIDDLEWARES, store_in_inbox)


def pull_updates(timeout: int = 0) -> int:
    """One getUpdates round: runs every message through PIPELINE. Returns stored count."""
    with _lock:
        offset = _state().get("offset", 0)
    updates = bot_api("getUpdates", json={"offset": offset, "timeout": timeout})
    oid = owner_id()
    added = 0
    with _lock:
        st = _state()
        with INBOX.open("a", encoding="utf-8") as f:
            for u in updates:
                offset = max(offset, u["update_id"] + 1)
                m = u.get("message")
                if not m:
                    continue
                ctx = Ctx(m, oid, st, f)
                ctx.added = added
                PIPELINE(ctx)
                added = ctx.added
        st["offset"] = offset
        STATE.write_text(json.dumps(st))
    return added


def _poll_forever():
    """Background long polling while the MCP server runs, so /help gets an instant answer."""
    global _poller_alive
    try:
        if not cfg().get("bot_token") or not cfg().get("owner_chat_id"):
            return
        bot_api("setMyCommands", json={"commands": COMMANDS})
    except Exception:
        return
    _poller_alive = True
    while True:
        try:
            pull_updates(timeout=25)
        except Exception:
            time.sleep(5)  # network error or another session is polling (409)


@mcp.tool()
def bot_inbox(limit: int = 20) -> str:
    """Return the last `limit` messages the owner sent/forwarded to the bot (text, forwarded-from,
    attached files with file_id) and how many are new since the last call. Use bot_download to save a file."""
    if not _poller_alive:
        pull_updates()
    with _lock:
        lines = _inbox_lines()
        st = _state()
        new = len(lines) - st.get("seen", 0)
        st["seen"] = len(lines)
        STATE.write_text(json.dumps(st))
    items = [json.loads(x) for x in lines[-limit:]]
    return json.dumps({"new": max(new, 0), "items": items}, ensure_ascii=False, indent=1)


@mcp.tool()
def bot_download(file_id: str, filename: str = "") -> str:
    """Download a file from the bot inbox (by file_id) to Downloads\\Telegram MCP. Returns the local path.
    Bot API limit: 20 MB per file."""
    info = bot_api("getFile", json={"file_id": file_id})
    token = cfg()["bot_token"]
    name = filename or Path(info["file_path"]).name
    dest = unique_path(DOWNLOADS, name)
    with httpx.stream("GET", f"https://api.telegram.org/file/bot{token}/{info['file_path']}", timeout=120) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    return str(dest)


@mcp.tool()
def bot_send_message(text: str) -> str:
    """Send a text message from the bot to the owner only (reminders, short reports)."""
    res = bot_api("sendMessage", json={"chat_id": owner_id(), "text": text[:4096]})
    return f"sent, message_id={res['message_id']}"


@mcp.tool()
def bot_send_file(path: str, caption: str = "") -> str:
    """Send a local file (e.g. a finished .docx report) from the bot to the owner only. Max 50 MB."""
    p = Path(path)
    if not p.is_file():
        raise ToolError(f"file not found: {path}")
    with p.open("rb") as f:
        res = bot_api("sendDocument", data={"chat_id": owner_id(), "caption": caption[:1024]},
                      files={"document": (p.name, f)})
    return f"sent {p.name}, message_id={res['message_id']}"


# ---------------------------------------------------------------- account (read-only)

_client = None


async def client():
    global _client
    if _client is None:
        from telethon import TelegramClient
        c = cfg()
        if not c.get("api_id"):
            raise ToolError("account is not configured: run setup.py")
        _client = TelegramClient(str(SESSION), int(c["api_id"]), c["api_hash"])
    if not _client.is_connected():
        await _client.connect()
    if not await _client.is_user_authorized():
        raise ToolError("account session is not logged in: run setup.py")
    return _client


async def resolve(chat: str):
    cl = await client()
    s = chat.strip()
    if re.fullmatch(r"-?\d+", s):
        return await cl.get_entity(int(s))
    if s.startswith("@") or "t.me/" in s:
        return await cl.get_entity(s)
    low = s.lower()
    async for d in cl.iter_dialogs():
        if low in (d.name or "").lower():
            return d.entity
    raise ToolError(f"chat not found: {chat}")


def fmt(m, names: dict) -> str:
    who = names.get(m.sender_id, str(m.sender_id or ""))
    media = ""
    if m.media:
        if m.photo:
            media = " [photo]"
        elif m.document:
            fname = next((a.file_name for a in m.document.attributes if hasattr(a, "file_name")), "")
            media = f" [file: {fname or m.document.mime_type}]"
        else:
            media = f" [{type(m.media).__name__}]"
    text = (m.message or "").replace("\n", " ⏎ ")
    return f"[{m.id}] {m.date.astimezone():%Y-%m-%d %H:%M} {who}: {text}{media}"


async def sender_names(msgs) -> dict:
    names = {}
    for m in msgs:
        if m.sender_id and m.sender_id not in names:
            s = m.sender
            names[m.sender_id] = (getattr(s, "title", None) or " ".join(
                x for x in (getattr(s, "first_name", None), getattr(s, "last_name", None)) if x)) if s else str(m.sender_id)
    return names


@mcp.tool()
async def tg_list_chats(limit: int = 30, query: str = "") -> str:
    """List the account's chats (id, name, unread count). `query` filters by name."""
    cl = await client()
    out = []
    async for d in cl.iter_dialogs(limit=None if query else limit):
        if query and query.lower() not in (d.name or "").lower():
            continue
        out.append(f"{d.id}\t{d.name}\tunread={d.unread_count}")
        if len(out) >= limit:
            break
    return "\n".join(out) or "no chats"


@mcp.tool()
async def tg_read(chat: str, limit: int = 30, search: str = "", before_id: int = 0) -> str:
    """Read recent messages from one chat (READ-ONLY, does not mark as read).
    chat = id, @username or part of the chat name. Optional text `search`; `before_id` pages back."""
    cl = await client()
    ent = await resolve(chat)
    msgs = [m async for m in cl.iter_messages(ent, limit=min(limit, 200), search=search or None,
                                               offset_id=before_id)]
    names = await sender_names(msgs)
    return "\n".join(fmt(m, names) for m in reversed(msgs)) or "no messages"


@mcp.tool()
async def tg_search(query: str, limit: int = 20) -> str:
    """Search all chats of the account for text (READ-ONLY)."""
    cl = await client()
    msgs = [m async for m in cl.iter_messages(None, search=query, limit=min(limit, 100))]
    names = await sender_names(msgs)
    lines = []
    for m in msgs:
        chat = await m.get_chat()
        title = getattr(chat, "title", None) or getattr(chat, "first_name", "") or ""
        lines.append(f"<{m.chat_id} {title}> " + fmt(m, names))
    return "\n".join(lines) or "nothing found"


@mcp.tool()
async def tg_download(chat: str, message_id: int) -> str:
    """Download the photo/file of one message to Downloads\\Telegram MCP. Returns the local path."""
    cl = await client()
    ent = await resolve(chat)
    m = await cl.get_messages(ent, ids=message_id)
    if not m or not m.media:
        raise ToolError("message has no media")
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    path = await cl.download_media(m, file=str(DOWNLOADS) + os.sep)
    return str(path)


if __name__ == "__main__":
    threading.Thread(target=_poll_forever, daemon=True).start()
    mcp.run()

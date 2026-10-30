# telegram-mcp

A small, self-hosted [MCP](https://modelcontextprotocol.io) server that gives Claude (or any MCP client) access to Telegram in two safe modes:

| Mode | What it does | Tools |
|---|---|---|
| **Personal bot** | Exchange files and notes with your own bot. The bot talks **only to its owner**; messages from anyone else are ignored. | `bot_inbox`, `bot_download`, `bot_send_message`, `bot_send_file` |
| **Your account, read-only** | Read and search your chats via [Telethon](https://github.com/LonamiWebs/Telethon). There are **no** send / edit / delete tools. Reading does not mark messages as read. | `tg_list_chats`, `tg_read`, `tg_search`, `tg_download` |

Both modes are optional: configure one or both.

## Use cases

- Forward photos of a lab manual to the bot → *"take the photos from the bot and write the report"*.
- *"What was posted in my study group chat this week?"*
- *"Send the finished report to my Telegram."*

## Install

Requires Python 3.10+.

```bash
git clone https://github.com/Spleps/telegram-mcp.git
cd telegram-mcp
pip install -r requirements.txt
```

## Configure

```bash
python setup.py
```

1. **Bot**: create one with [@BotFather](https://t.me/BotFather) (`/newbot`), paste the token, then send `/start` to your bot. That chat becomes the only owner.
2. **Account**: get `api_id` / `api_hash` at [my.telegram.org](https://my.telegram.org) → *API development tools*. Log in by scanning a QR code (Telegram → Settings → Devices → Link Desktop Device). If you use a cloud password (2FA), a small dialog asks for it.

Secrets are stored next to the script in `config.json` and `account.session`. On Windows the setup restricts both files to the current user. They are listed in `.gitignore`: **never share them**. The session file gives full access to your account.

## Register in Claude Code

```bash
claude mcp add --scope user telegram -- python /path/to/telegram-mcp/server.py
```

Start a new Claude session and the `bot_*` / `tg_*` tools appear. Downloaded files go to `~/Downloads/Telegram MCP`.

## Revoke access

- Account: Telegram → Settings → Devices → terminate the session.
- Bot: `/revoke` in @BotFather, then run `setup.py` again.

## Notes

- Bot API limits: download ≤ 20 MB, upload ≤ 50 MB.
- Setup prompts are in Russian.

## License

MIT

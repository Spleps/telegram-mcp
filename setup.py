"""Run once in a terminal:  py setup.py
Asks for the bot token and/or Telegram API keys and logs the account in.
Everything stays in this folder (config.json, account.session). Never share these files."""
import asyncio
import getpass
import json
import os
import subprocess
import time
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"


def load():
    return json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}


def save(c):
    CONFIG.write_text(json.dumps(c, indent=1), encoding="utf-8")
    lock(CONFIG)


def lock(p: Path):
    # only the current Windows user may read the file
    if os.name == "nt" and p.exists():
        subprocess.run(["icacls", str(p), "/inheritance:r", "/grant:r", f"{os.environ['USERNAME']}:F"],
                       capture_output=True)


def setup_bot(c):
    print("\n=== 1. Бот ===")
    if c.get("bot_token") and c.get("owner_chat_id"):
        print("Бот уже настроен, пропускаю.")
        return
    token = c.get("bot_token")
    if not token:
        print("В Telegram: @BotFather -> /newbot -> скопируй токен.")
        token = getpass.getpass("Токен бота (ввод скрыт, Enter = пропустить): ").strip()
    if not token:
        return
    r = httpx.get(f"https://api.telegram.org/bot{token}/getMe").json()
    if not r.get("ok"):
        print("Токен не подошёл:", r.get("description"))
        return
    name = r["result"]["username"]
    print(f"Бот @{name} найден. Теперь открой его в Telegram и отправь /start ...")
    for _ in range(60):
        ups = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates").json().get("result", [])
        for u in ups:
            m = u.get("message")
            if m and m.get("text", "").startswith("/start"):
                c["bot_token"] = token
                c["owner_chat_id"] = m["chat"]["id"]
                save(c)
                httpx.get(f"https://api.telegram.org/bot{token}/getUpdates",
                          params={"offset": u["update_id"] + 1})
                httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                           json={"chat_id": m["chat"]["id"], "text": "Готово, я подключён к Claude."})
                print("Бот привязан к тебе.")
                return
        time.sleep(2)
    print("Не дождался /start за 2 минуты, запусти setup ещё раз.")


async def setup_account(c):
    print("\n=== 2. Аккаунт (только чтение) ===")
    api_id, api_hash = str(c.get("api_id") or ""), c.get("api_hash") or ""
    if not api_id:
        print("Открой https://my.telegram.org -> API development tools -> создай приложение.")
        api_id = input("api_id (Enter = пропустить): ").strip()
        if not api_id:
            return
        api_hash = getpass.getpass("api_hash (ввод скрыт): ").strip()
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    client = TelegramClient(str(BASE / "account"), int(api_id), api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await qr_login(client, SessionPasswordNeededError)
    me = await client.get_me()
    await client.disconnect()
    c["api_id"], c["api_hash"] = int(api_id), api_hash
    save(c)
    lock(BASE / "account.session")
    print(f"Вошёл как {me.first_name}. Сессия сохранена.")


async def qr_login(client, SessionPasswordNeededError):
    import qrcode
    png = BASE / "login_qr.png"
    print("\nВход по QR-коду. На телефоне: Telegram -> Настройки -> Устройства -> Подключить устройство,")
    print("и наведи камеру на QR (он откроется картинкой; в терминале тоже есть копия).")
    try:
        qr = await client.qr_login()
    except SessionPasswordNeededError:
        await ask_password(client)
        return
    while True:
        try:
            png = Path(os.environ.get("TEMP", BASE)) / f"tg_login_qr_{int(time.time())}.png"
            qrcode.make(qr.url).save(str(png))
            os.startfile(png)
        except OSError as e:
            print("Картинку открыть не вышло, сканируй QR из терминала:", e)
        q = qrcode.QRCode(border=1)
        q.add_data(qr.url)
        q.print_ascii(invert=True)
        try:
            await qr.wait(timeout=60)
            break
        except asyncio.TimeoutError:
            print("QR устарел, показываю новый...")
            await qr.recreate()
        except SessionPasswordNeededError:
            await ask_password(client)
            break
    try:
        png.unlink(missing_ok=True)
    except OSError:
        pass


def gui_password(prompt: str) -> str:
    """Ask the password in a small Windows dialog (the app's terminal mangles key input)."""
    import tkinter as tk
    result = {"v": None}
    root = tk.Tk()
    root.title("Telegram: облачный пароль")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    tk.Label(root, text=prompt, font=("Segoe UI", 11)).pack(padx=16, pady=(14, 6))
    var = tk.StringVar()
    show = tk.BooleanVar(value=False)
    e = tk.Entry(root, textvariable=var, show="*", width=34, font=("Segoe UI", 12))
    e.pack(padx=16)
    tk.Checkbutton(root, text="Показать пароль", variable=show,
                   command=lambda: e.config(show="" if show.get() else "*")).pack(pady=4)

    def ok(_=None):
        result["v"] = var.get()
        root.destroy()

    tk.Button(root, text="OK", width=12, command=ok).pack(pady=(4, 14))
    e.bind("<Return>", ok)
    root.after(200, lambda: (root.focus_force(), e.focus_set()))
    root.mainloop()
    return result["v"] or ""


def masked_input(prompt: str) -> str:
    """Password input that echoes * per char, accepts pasted text, ignores stray control keys."""
    if os.name != "nt":
        return getpass.getpass(prompt)
    import msvcrt
    import sys
    sys.stdout.write(prompt)
    sys.stdout.flush()
    buf = []
    while True:
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            break
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\x16":  # Ctrl+V arrived as a key: read clipboard instead
            try:
                text = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                                      capture_output=True, text=True).stdout.rstrip("\r\n")
            except Exception:
                text = ""
            buf.extend(text)
            sys.stdout.write("*" * len(text))
        elif ch == "\b":
            if buf:
                buf.pop()
                sys.stdout.write("\b \b")
        elif ch in ("\x00", "\xe0"):
            msvcrt.getwch()  # skip arrow/function key code
        elif ch >= " ":
            buf.append(ch)
            sys.stdout.write("*")
        sys.stdout.flush()
    sys.stdout.write(f"  ({len(buf)} симв.)\n")
    return "".join(buf)


async def ask_password(client):
    from telethon.errors import PasswordHashInvalidError
    print("\nНужен облачный пароль Telegram: открылось окошко, введи его там.")
    for i in range(3):
        msg = "Облачный пароль Telegram:" if i == 0 else "Неверный пароль. Ещё раз (проверь раскладку):"
        try:
            pwd = gui_password(msg)
        except Exception:
            pwd = masked_input("Облачный пароль: ")
        if not pwd:
            continue
        try:
            await client.sign_in(password=pwd)
            return
        except PasswordHashInvalidError:
            print("Неверный пароль, попробуй ещё раз.")
    raise SystemExit("Пароль не подошёл 3 раза.")


def main():
    c = load()
    setup_bot(c)
    asyncio.run(setup_account(c))
    print("\nГотово. Перезапусти сессию Claude, чтобы он увидел инструменты Telegram.")


if __name__ == "__main__":
    main()

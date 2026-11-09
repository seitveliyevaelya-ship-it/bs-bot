"""Минимальный Telegram-бот для учёта счетов-фактур.

Только стандартная библиотека Python 3.9+ (pip не нужен).
Данные хранятся локально в invoices.db (SQLite) рядом со скриптом.
"""
import configparser
import csv
import io
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
# На Railway путь к базе задаётся переменной DB_PATH (например /data/invoices.db на Volume)
DB_PATH = Path(os.environ.get("DB_PATH") or BASE / "invoices.db")
CFG_PATH = BASE / "config.ini"

HELP = """Учёт счетов-фактур

/add вх|исх <номер> <ДД.ММ.ГГГГ> <сумма> <контрагент>
   пример: /add вх 125 09.11.2026 15000,50 ООО Ромашка
/list [N] — последние N записей (по умолч. 10)
/unpaid — неоплаченные
/paid <id> — отметить оплату
/del <id> — удалить запись
/find <текст> — поиск по номеру или контрагенту
/report [ГГГГ-ММ] — итоги за месяц (по умолч. текущий)
/export [ГГГГ-ММ] — выгрузка CSV (для сверки с 1С)
/myid — показать ваш Telegram ID"""

DIRS = {"вх": "in", "входящая": "in", "in": "in",
        "исх": "out", "исходящая": "out", "out": "out"}
DIR_LABEL = {"in": "вх", "out": "исх"}


# ---------- настройки ----------
def load_config():
    """Сначала переменные окружения (Railway), затем config.ini (локальный запуск)."""
    token = (os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    raw = os.environ.get("ALLOWED_USER_IDS")
    if CFG_PATH.exists():
        cfg = configparser.ConfigParser()
        cfg.read(CFG_PATH, encoding="utf-8")
        token = token or cfg.get("bot", "token", fallback="").strip()
        if raw is None:
            raw = cfg.get("bot", "allowed_user_ids", fallback="")
    if not token or token.startswith("ВСТАВЬТЕ"):
        sys.exit("Не указан токен: переменная BOT_TOKEN или token в config.ini.")
    allowed = {int(x) for x in (raw or "").replace(" ", "").split(",") if x}
    return token, allowed


def save_owner(user_id):
    if not CFG_PATH.exists():  # Railway: файла нет, ID надо вписать в переменную вручную
        print(f"Впишите в переменную ALLOWED_USER_IDS значение {user_id}, иначе после перезапуска доступ сбросится.")
        return
    lines = CFG_PATH.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("allowed_user_ids"):
            lines[i] = f"allowed_user_ids = {user_id}"
            break
    else:
        lines.append(f"allowed_user_ids = {user_id}")
    CFG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- база ----------
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        direction TEXT NOT NULL,          -- in / out
        number TEXT NOT NULL,
        date TEXT NOT NULL,               -- ГГГГ-ММ-ДД
        amount_cents INTEGER NOT NULL,
        counterparty TEXT NOT NULL,
        paid INTEGER NOT NULL DEFAULT 0,
        paid_at TEXT,
        created_at TEXT NOT NULL,
        created_by INTEGER)""")
    return con


def money(cents):
    s = f"{cents / 100:,.2f}".replace(",", " ").replace(".", ",")
    return s


def parse_amount(text):
    t = text.replace(" ", "").replace(",", ".")
    value = float(t)
    if value <= 0:
        raise ValueError
    return round(value * 100)


def fmt_row(r):
    mark = "✅" if r["paid"] else "⏳"
    d = datetime.strptime(r["date"], "%Y-%m-%d").strftime("%d.%m.%Y")
    return (f"{mark} #{r['id']} [{DIR_LABEL[r['direction']]}] №{r['number']} от {d} — "
            f"{money(r['amount_cents'])} — {r['counterparty']}")


def month_arg(args):
    if args:
        datetime.strptime(args[0], "%Y-%m")  # проверка формата
        return args[0]
    return datetime.now().strftime("%Y-%m")


# ---------- команды ----------
def cmd_add(args, user_id):
    if len(args) < 5:
        return "Формат: /add вх|исх <номер> <ДД.ММ.ГГГГ> <сумма> <контрагент>"
    direction = DIRS.get(args[0].lower())
    if not direction:
        return "Первым словом укажите вх или исх."
    number = args[1]
    try:
        date = datetime.strptime(args[2], "%d.%m.%Y").strftime("%Y-%m-%d")
    except ValueError:
        return "Дата должна быть в формате ДД.ММ.ГГГГ."
    try:
        cents = parse_amount(args[3])
    except ValueError:
        return "Сумма — число без пробелов, например 15000,50."
    counterparty = " ".join(args[4:])
    with db() as con:
        dup = con.execute("SELECT id FROM invoices WHERE direction=? AND number=? AND date=?",
                          (direction, number, date)).fetchone()
        cur = con.execute(
            "INSERT INTO invoices(direction, number, date, amount_cents, counterparty, created_at, created_by)"
            " VALUES (?,?,?,?,?,?,?)",
            (direction, number, date, cents, counterparty, datetime.now().isoformat(timespec="seconds"), user_id))
        row = con.execute("SELECT * FROM invoices WHERE id=?", (cur.lastrowid,)).fetchone()
    msg = "Добавлено:\n" + fmt_row(row)
    if dup:
        msg += f"\n⚠️ Похоже на дубль записи #{dup['id']} (тот же номер и дата)."
    return msg


def cmd_list(args, _):
    n = int(args[0]) if args and args[0].isdigit() else 10
    with db() as con:
        rows = con.execute("SELECT * FROM invoices ORDER BY date DESC, id DESC LIMIT ?", (n,)).fetchall()
    return "\n".join(fmt_row(r) for r in rows) or "Записей пока нет."


def cmd_unpaid(args, _):
    with db() as con:
        rows = con.execute("SELECT * FROM invoices WHERE paid=0 ORDER BY date").fetchall()
    if not rows:
        return "Неоплаченных нет."
    total = {"in": 0, "out": 0}
    for r in rows:
        total[r["direction"]] += r["amount_cents"]
    return ("\n".join(fmt_row(r) for r in rows) +
            f"\n\nИтого не оплачено: вх {money(total['in'])} · исх {money(total['out'])}")


def _by_id(args):
    if not args or not args[0].lstrip("#").isdigit():
        return None
    return int(args[0].lstrip("#"))


def cmd_paid(args, _):
    inv_id = _by_id(args)
    if inv_id is None:
        return "Формат: /paid <id>"
    with db() as con:
        cur = con.execute("UPDATE invoices SET paid=1, paid_at=? WHERE id=?",
                          (datetime.now().isoformat(timespec="seconds"), inv_id))
        if not cur.rowcount:
            return f"Запись #{inv_id} не найдена."
        row = con.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()
    return "Отмечено как оплачено:\n" + fmt_row(row)


def cmd_del(args, _):
    inv_id = _by_id(args)
    if inv_id is None:
        return "Формат: /del <id>"
    with db() as con:
        row = con.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()
        if not row:
            return f"Запись #{inv_id} не найдена."
        con.execute("DELETE FROM invoices WHERE id=?", (inv_id,))
    return "Удалено:\n" + fmt_row(row)


def cmd_find(args, _):
    if not args:
        return "Формат: /find <текст>"
    q = " ".join(args).lower()
    with db() as con:
        rows = con.execute("SELECT * FROM invoices ORDER BY date DESC").fetchall()
    found = [r for r in rows if q in r["number"].lower() or q in r["counterparty"].lower()]
    return "\n".join(fmt_row(r) for r in found[:30]) or "Ничего не найдено."


def cmd_report(args, _):
    try:
        month = month_arg(args)
    except ValueError:
        return "Формат: /report ГГГГ-ММ"
    with db() as con:
        rows = con.execute(
            "SELECT direction, paid, COUNT(*) c, SUM(amount_cents) s FROM invoices "
            "WHERE substr(date,1,7)=? GROUP BY direction, paid", (month,)).fetchall()
    if not rows:
        return f"За {month} записей нет."
    stat = {(r["direction"], r["paid"]): (r["c"], r["s"]) for r in rows}
    lines = [f"Итоги за {month}"]
    for d, title in (("in", "Входящие"), ("out", "Исходящие")):
        pc, ps = stat.get((d, 1), (0, 0))
        uc, us = stat.get((d, 0), (0, 0))
        lines.append(f"\n{title}: {pc + uc} шт., {money(ps + us)}")
        lines.append(f"  оплачено: {pc} шт., {money(ps)}")
        lines.append(f"  не оплачено: {uc} шт., {money(us)}")
    return "\n".join(lines)


def export_csv(args):
    month = month_arg(args) if args else None
    with db() as con:
        if month:
            rows = con.execute("SELECT * FROM invoices WHERE substr(date,1,7)=? ORDER BY date", (month,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM invoices ORDER BY date").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "вид", "номер", "дата", "сумма", "контрагент", "оплачено", "дата оплаты"])
    for r in rows:
        w.writerow([r["id"], DIR_LABEL[r["direction"]], r["number"],
                    datetime.strptime(r["date"], "%Y-%m-%d").strftime("%d.%m.%Y"),
                    f"{r['amount_cents'] / 100:.2f}".replace(".", ","), r["counterparty"],
                    "да" if r["paid"] else "нет", r["paid_at"] or ""])
    name = f"invoices_{month or 'all'}.csv"
    # utf-8-sig — чтобы Excel открыл кириллицу корректно
    return name, buf.getvalue().encode("utf-8-sig"), len(rows)


COMMANDS = {"/add": cmd_add, "/list": cmd_list, "/unpaid": cmd_unpaid, "/paid": cmd_paid,
            "/del": cmd_del, "/find": cmd_find, "/report": cmd_report}


# ---------- Telegram API ----------
class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, params=None, timeout=40):
        data = json.dumps(params or {}).encode("utf-8")
        req = urllib.request.Request(self.base + method, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def send(self, chat_id, text):
        for i in range(0, len(text), 4000):  # лимит Telegram ~4096 символов
            self.call("sendMessage", {"chat_id": chat_id, "text": text[i:i + 4000]})

    def send_document(self, chat_id, filename, content, caption=""):
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in (("chat_id", str(chat_id)), ("caption", caption)):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
                      f'filename="{filename}"\r\nContent-Type: text/csv\r\n\r\n').encode())
        parts.append(content + f"\r\n--{boundary}--\r\n".encode())
        req = urllib.request.Request(self.base + "sendDocument", data=b"".join(parts),
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))


def handle(tg, allowed, msg):
    chat_id = msg["chat"]["id"]
    user_id = msg.get("from", {}).get("id")
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()  # /add@MyBot -> /add
    args = parts[1:]

    if cmd == "/myid":
        tg.send(chat_id, f"Ваш Telegram ID: {user_id}")
        return
    if not allowed and user_id:
        # Первый написавший становится владельцем и сохраняется в config.ini
        allowed.add(user_id)
        save_owner(user_id)
        print(f"Владелец назначен: ID {user_id}")
        tg.send(chat_id, f"Вы назначены владельцем бота (ID {user_id}). Другим доступ закрыт.")
    if user_id not in allowed:
        print(f"Отказано в доступе: ID {user_id}")
        tg.send(chat_id, f"Доступ закрыт. Ваш ID: {user_id} — добавьте его в allowed_user_ids в config.ini.")
        return
    if cmd in ("/start", "/help"):
        tg.send(chat_id, HELP)
    elif cmd == "/export":
        try:
            name, content, n = export_csv(args)
        except ValueError:
            tg.send(chat_id, "Формат: /export [ГГГГ-ММ]")
            return
        tg.send_document(chat_id, name, content, caption=f"Записей: {n}")
    elif cmd in COMMANDS:
        tg.send(chat_id, COMMANDS[cmd](args, user_id))
    else:
        tg.send(chat_id, "Неизвестная команда. /help — список команд.")


def main():
    token, allowed = load_config()
    tg = Telegram(token)
    me = tg.call("getMe")["result"]
    print(f"Бот @{me['username']} запущен. Разрешённые ID: "
          f"{sorted(allowed) or 'нет — первый написавший станет владельцем'}")
    offset = 0
    while True:
        try:
            upd = tg.call("getUpdates", {"offset": offset, "timeout": 30})
            for u in upd.get("result", []):
                offset = u["update_id"] + 1
                if "message" in u:
                    try:
                        handle(tg, allowed, u["message"])
                    except Exception as e:  # одна ошибка не должна ронять бота
                        print("Ошибка обработки:", e)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            print("Сеть:", e, "— повтор через 5 с")
            time.sleep(5)
        except KeyboardInterrupt:
            print("Остановлено.")
            break


if __name__ == "__main__":
    main()

"""
Бот «Счета» (invoicebot).

Сидит в рабочем чате. Когда кто-то присылает файл (документ или фото),
бот скачивает его и отправляет на сервер bmw-names (POST /api/invoice_submit).
Сервер разбирает: счёт ли это, кто контрагент, сумма, дата — и, если счёт,
добавляет его в раздел «Счета» на сайте со статусом «на одобрении». Бот
отвечает в чат, что получилось.

Своего хранилища у бота нет — всё на сервере (единый источник правды).

Установка:
    pip install aiogram aiohttp

Переменные окружения (bot.env):
    BOT_TOKEN     — токен нового бота от @BotFather
    OWNER_IDS     — id владельца(ев) через запятую (кто привязывает чат: /here)
    API_BASE      — адрес сервера bmw-names (по умолчанию http://127.0.0.1:8090)
    API_PASSWORD  — тот же APP_PASSWORD, что у сервера bmw-names (для X-Auth)

ВАЖНО: чтобы бот видел файлы в группе, у него должен быть ВЫКЛЮЧЕН режим
приватности: @BotFather → /setprivacy → выбрать бота → Disable.

Локальный файл состояния: invoicebot_state.json (chat_id).

Запуск:
    python invoicebot.py
"""

import asyncio
import json
import logging
import os

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8090").rstrip("/")
API_PASSWORD = os.getenv("API_PASSWORD", "")
OWNER_IDS = {
    int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x
}
STATE_PATH = "invoicebot_state.json"

log = logging.getLogger(__name__)
router = Router()


# ---------- Локальное состояние ----------

def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(st: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def state_get(key, default=None):
    return load_state().get(key, default)


def state_set(key, value) -> None:
    st = load_state()
    st[key] = value
    save_state(st)


# ---------- Доступ (как у paybot): владелец + привязанный чат ----------

def is_owner(uid: int) -> bool:
    return uid in OWNER_IDS


def chat_bound(chat_id) -> bool:
    b = state_get("chat_id")
    return b is not None and str(chat_id) == str(b)


def msg_allowed(m: Message) -> bool:
    return is_owner(m.from_user.id) or chat_bound(m.chat.id)


# ---------- Отправка файла на сервер ----------

async def submit_file(data: bytes, filename: str, mime: str, by: str, chat: str) -> dict:
    form = aiohttp.FormData()
    form.add_field("file", data, filename=filename, content_type=mime)
    form.add_field("by", by)
    form.add_field("chat", chat)
    timeout = aiohttp.ClientTimeout(total=120)  # ИИ-разбор может быть небыстрым
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(f"{API_BASE}/api/invoice_submit", data=form,
                          headers={"X-Auth": API_PASSWORD}) as r:
            if r.status == 401:
                return {"error": "сервер отклонил пароль (API_PASSWORD)"}
            return await r.json(content_type=None)


# ---------- Хэндлеры ----------

@router.message(lambda m: not msg_allowed(m))
async def deny(message: Message) -> None:
    await message.answer(
        "Доступа нет. Этим ботом пользуются в общем чате, куда его добавил владелец.\n"
        f"Ваш id: {message.from_user.id}"
    )


@router.callback_query(lambda c: not (is_owner(c.from_user.id) or
                                      (c.message and chat_bound(c.message.chat.id))))
async def deny_cb(callback: CallbackQuery) -> None:
    await callback.answer("Доступа нет", show_alert=True)


@router.message(Command("start", "help"))
async def cmd_start(message: Message) -> None:
    chat = state_get("chat_id")
    where = "этот чат" if str(message.chat.id) == str(chat) else "ещё не выбран"
    await message.answer(
        "Бот «Счета».\n\n"
        "Присылайте в привязанный чат счета файлами (PDF, фото, Word/Excel) — "
        "я добавлю их в раздел «Счета» на сайте на одобрение. PDF и фото разбираю "
        "автоматически (контрагент, сумма, дата), остальное заполняется вручную.\n\n"
        "Команды:\n"
        "/here — привязать этот чат (только владелец)\n\n"
        f"Чат для счетов: {where}."
    )


@router.message(Command("here"))
async def cmd_here(message: Message) -> None:
    if not is_owner(message.from_user.id):
        await message.answer("Привязать чат может только владелец бота.")
        return
    state_set("chat_id", message.chat.id)
    await message.answer(
        "✅ Этот чат привязан. Присылайте сюда счета файлами — буду добавлять их на сайт."
    )


@router.message(F.document | F.photo)
async def on_file(message: Message, bot: Bot) -> None:
    if message.document:
        src = message.document
        filename = message.document.file_name or f"file_{message.message_id}"
        mime = message.document.mime_type or "application/octet-stream"
    else:  # фото
        src = message.photo[-1]  # самое крупное
        filename = f"photo_{message.message_id}.jpg"
        mime = "image/jpeg"

    try:
        bio = await bot.download(src)
        data = bio.read()
    except Exception as e:
        await message.reply(f"Не смог скачать файл: {e}")
        return

    u = message.from_user
    by = u.full_name or (f"@{u.username}" if u.username else str(u.id))
    chat = message.chat.title or message.chat.full_name or str(message.chat.id)

    note = await message.reply("⏳ Проверяю файл…")
    try:
        res = await submit_file(data, filename, mime, by, chat)
    except Exception as e:
        await note.edit_text(f"Ошибка обработки: {e}")
        return

    if res.get("error"):
        await note.edit_text(f"Ошибка: {res['error']}")
        return
    if res.get("is_invoice") is False:
        why = res.get("why") or ""
        await note.edit_text("Это не похоже на счёт — не добавил." +
                             (f"\n{why}" if why else ""))
        return

    inv = res.get("invoice") or {}
    if res.get("auto"):
        await note.edit_text(
            "🧾 Счёт добавлен на одобрение:\n"
            f"Контрагент: {inv.get('contractor') or '—'}\n"
            f"Сумма: {inv.get('amount') or '—'}\n"
            f"Дата: {inv.get('date') or '—'}\n\n"
            "Проверить и одобрить — в разделе «Счета» на сайте."
        )
    else:
        await note.edit_text(
            "📄 Файл добавлен в «Счета». Автоматически разобрать не вышло — "
            "заполните контрагента, сумму и дату на сайте."
        )


# ---------- Запуск ----------

async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN")
    if not OWNER_IDS:
        raise SystemExit("Не задан OWNER_IDS")
    if not API_PASSWORD:
        log.warning("API_PASSWORD пуст — если у сервера задан пароль, запросы будут 401")

    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(f"{API_BASE}/api/invoices",
                             headers={"X-Auth": API_PASSWORD}) as r:
                log.info("Связь с сервером bmw-names: %s (%s)",
                         "ок" if r.status == 200 else f"код {r.status}", API_BASE)
    except Exception as e:
        log.warning("Нет связи с сервером %s: %s (бот продолжит)", API_BASE, e)

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    log.info("Invoice bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

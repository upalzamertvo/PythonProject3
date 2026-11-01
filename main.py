"""
Бот-менеджер: продажа Telegram Stars (усиленная версия, несколько админов)
Установка: pip install "python-telegram-bot[socks]"
Запуск:    python main.py
"""

import logging
import logging.handlers
import json
import os
import asyncio
from dataclasses import dataclass, asdict
from typing import Dict, Optional, List, Set, Any
from collections import OrderedDict

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.error import (
    RetryAfter,
    TimedOut,
    NetworkError,
    BadRequest,
    Forbidden,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ──────────────────────────────────────────────
#  КОНФИГУРАЦИЯ
# ──────────────────────────────────────────────

BOT_TOKEN = "9251746216:AAFfQo5rIAJi69reSL_ipt8-dldz-8n290w"   # ← токен от @BotFather

# Несколько админов — впишите ID через запятую (узнать через @userinfobot)
ADMIN_IDS: Set[int] = {
    6330094840,    # ← админ 1
    8417748949,
    8810626479,    # ← админ 2
    # добавляйте сколько нужно
}

CURRENCY = "₽"

DEFAULT_PRICES = {
    50:  79,
    75:  120,
    100: 160,
    150: 240,
    200: 315,
    250: 390,
    300: 470,
    350: 550,
    500: 775,
}

PRICES_FILE = "prices.json"
DB_FILE = "orders.json"
LOG_FILE = "bot.log"

PROXY_URL: Optional[str] = None
# PROXY_URL = "socks5://IP:ПОРТ"

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0

# ──────────────────────────────────────────────
#  ЛОГИРОВАНИЕ (файл + консоль, ротация)
# ──────────────────────────────────────────────

_log_handlers = [
    logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8",
    ),
    logging.StreamHandler(),
]

logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO,
    handlers=_log_handlers,
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
#  АСИНХРОННЫЙ I/O (не блокирует event loop)
# ──────────────────────────────────────────────

def _write_json_sync(filepath: str, data: Any) -> None:
    tmp = filepath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, filepath)


def _read_json_sync(filepath: str) -> Any:
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


async def async_write_json(filepath: str, data: Any) -> None:
    await asyncio.to_thread(_write_json_sync, filepath, data)


async def async_read_json(filepath: str) -> Any:
    return await asyncio.to_thread(_read_json_sync, filepath)


# ──────────────────────────────────────────────
#  ХРАНЕНИЕ ЦЕН
# ──────────────────────────────────────────────

async def load_prices() -> Dict[int, int]:
    if os.path.exists(PRICES_FILE):
        try:
            data = await async_read_json(PRICES_FILE)
            return {int(k): int(v) for k, v in data.items()}
        except Exception as e:
            logger.warning("Не удалось загрузить цены: %s", e)
    return dict(DEFAULT_PRICES)


async def save_prices(prices: Dict[int, int]) -> None:
    data = {str(k): v for k, v in prices.items()}
    await async_write_json(PRICES_FILE, data)


# ──────────────────────────────────────────────
#  МОДЕЛИ ЗАКАЗОВ
# ──────────────────────────────────────────────

@dataclass
class Order:
    id: int
    user_id: int
    username: str
    stars: int
    amount_rub: float
    status: str = "new"
    qr_message_id: Optional[int] = None


async def load_orders() -> Dict[int, Order]:
    if os.path.exists(DB_FILE):
        try:
            data = await async_read_json(DB_FILE)
            return {int(k): Order(**v) for k, v in data.items()}
        except Exception as e:
            logger.warning("Не удалось загрузить заказы: %s", e)
    return {}


async def save_orders(orders: Dict[int, Order]) -> None:
    data = {k: asdict(v) for k, v in orders.items()}
    await async_write_json(DB_FILE, data)


# Глобальное состояние
STAR_PRICES: Dict[int, int] = {}
orders: Dict[int, Order] = {}
next_order_id: int = 1

admin_pending_qr: Dict[int, int] = {}

# Admin → User чат
admin_pending_chat: Dict[int, int] = {}      # admin_id -> user_id (ожидание первого сообщения)
active_admin_chats: Dict[int, int] = {}      # user_id -> admin_id (активный диалог)

support_msg_map: OrderedDict[int, int] = OrderedDict()
SUPPORT_MAP_MAX = 200

support_mode_users: Set[int] = set()

stats = {"orders_total": 0, "messages_total": 0, "errors_total": 0, "start_time": None}


def remember_support_msg(msg_id: int, user_id: int) -> None:
    support_msg_map[msg_id] = user_id
    support_msg_map.move_to_end(msg_id)
    while len(support_msg_map) > SUPPORT_MAP_MAX:
        support_msg_map.popitem(last=False)


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ──────────────────────────────────────────────
#  БЕЗОПАСНАЯ ОТПРАВКА (retry + flood wait)
# ──────────────────────────────────────────────

async def safe_send(bot, chat_id: int, text: str, parse_mode: str = "HTML",
                    reply_markup=None, **kwargs) -> Optional[Any]:
    for attempt in range(MAX_RETRIES):
        try:
            return await bot.send_message(
                chat_id=chat_id, text=text, parse_mode=parse_mode,
                reply_markup=reply_markup, **kwargs,
            )
        except RetryAfter as e:
            wait = e.retry_after + 1
            logger.warning("Flood wait %ss для chat_id=%s", wait, chat_id)
            await asyncio.sleep(wait)
        except TimedOut:
            logger.warning("Таймаут отправки (попытка %d/%d) chat_id=%s", attempt + 1, MAX_RETRIES, chat_id)
            await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
        except Forbidden as e:
            logger.warning("Доставка невозможна chat_id=%s: %s", chat_id, e)
            return None
        except BadRequest as e:
            logger.error("BadRequest chat_id=%s: %s", chat_id, e)
            if parse_mode:
                try:
                    return await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, **kwargs)
                except Exception:
                    pass
            return None
        except NetworkError as e:
            logger.warning("Сетевая ошибка (попытка %d/%d): %s", attempt + 1, MAX_RETRIES, e)
            await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
        except Exception as e:
            logger.error("Неожиданная ошибка отправки chat_id=%s: %s", chat_id, e)
            return None
    logger.error("Превышен лимит попыток отправки chat_id=%s", chat_id)
    return None


async def safe_send_photo(bot, chat_id: int, photo: str, caption: str = "",
                          parse_mode: str = "HTML", **kwargs) -> Optional[Any]:
    for attempt in range(MAX_RETRIES):
        try:
            return await bot.send_photo(
                chat_id=chat_id, photo=photo, caption=caption,
                parse_mode=parse_mode, **kwargs,
            )
        except RetryAfter as e:
            wait = e.retry_after + 1
            logger.warning("Flood wait %ss (photo) chat_id=%s", wait, chat_id)
            await asyncio.sleep(wait)
        except TimedOut:
            logger.warning("Таймаут фото (попытка %d/%d) chat_id=%s", attempt + 1, MAX_RETRIES, chat_id)
            await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
        except Forbidden as e:
            logger.warning("Доставка фото невозможна chat_id=%s: %s", chat_id, e)
            return None
        except BadRequest as e:
            logger.error("BadRequest (photo) chat_id=%s: %s", chat_id, e)
            if parse_mode:
                try:
                    return await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption, **kwargs)
                except Exception:
                    pass
            return None
        except NetworkError as e:
            logger.warning("Сетевая ошибка фото (попытка %d/%d): %s", attempt + 1, MAX_RETRIES, e)
            await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
        except Exception as e:
            logger.error("Неожиданная ошибка фото chat_id=%s: %s", chat_id, e)
            return None
    return None


async def safe_edit(query, text: str, parse_mode: str = "HTML",
                    reply_markup=None) -> bool:
    for attempt in range(MAX_RETRIES):
        try:
            await query.edit_message_text(text=text, parse_mode=parse_mode, reply_markup=reply_markup)
            return True
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TimedOut:
            await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                return True
            if parse_mode:
                try:
                    await query.edit_message_text(text=text, reply_markup=reply_markup)
                    return True
                except Exception:
                    pass
            logger.error("BadRequest edit: %s", e)
            return False
        except NetworkError as e:
            logger.warning("Сетевая ошибка edit (попытка %d/%d): %s", attempt + 1, MAX_RETRIES, e)
            await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
        except Exception as e:
            logger.error("Неожиданная ошибка edit: %s", e)
            return False
    return False


async def safe_edit_caption(query, caption: str, parse_mode: str = "HTML",
                            reply_markup=None) -> bool:
    for attempt in range(MAX_RETRIES):
        try:
            await query.edit_message_caption(caption=caption, parse_mode=parse_mode, reply_markup=reply_markup)
            return True
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TimedOut:
            await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                return True
            logger.error("BadRequest edit_caption: %s", e)
            return False
        except NetworkError as e:
            logger.warning("Сетевая ошибка edit_caption (попытка %d/%d): %s", attempt + 1, MAX_RETRIES, e)
            await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
        except Exception as e:
            logger.error("Неожиданная ошибка edit_caption: %s", e)
            return False
    return False


async def safe_answer(query, text: str = "", show_alert: bool = False) -> bool:
    for attempt in range(MAX_RETRIES):
        try:
            await query.answer(text=text, show_alert=show_alert)
            return True
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Exception:
            return False
    return False

# ──────────────────────────────────────────────
#  РАССЫЛКА АДМИНАМ
# ──────────────────────────────────────────────

async def notify_admins(bot, text: str, reply_markup=None) -> None:
    for admin_id in ADMIN_IDS:
        await safe_send(bot, admin_id, text, reply_markup=reply_markup)


# ──────────────────────────────────────────────
#  КЛАВИАТУРЫ — КЛИЕНТ
# ──────────────────────────────────────────────

def user_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["⭐ Заказать звёзды", "📋 Мои заказы"],
            ["📞 Поддержка", "ℹ️ О магазине"],
        ],
        resize_keyboard=True,
    )


def support_mode_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["⬅️ Выйти из поддержки"]], resize_keyboard=True)


def user_chat_kb() -> ReplyKeyboardMarkup:
    """Клавиатура клиента в чате с админом."""
    return ReplyKeyboardMarkup([["⬅️ В меню"]], resize_keyboard=True)


def stars_kb() -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    items = list(STAR_PRICES.items())
    for i in range(0, len(items), 2):
        row = []
        for stars, price in items[i:i + 2]:
            row.append(InlineKeyboardButton(f"{stars}⭐ — {price}{CURRENCY}", callback_data=f"order:{stars}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def support_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✉️ Написать в поддержку", callback_data="enter_support")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="back_to_menu")],
    ])


def user_paid_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Я оплатил(а)", callback_data=f"ipaid:{order_id}")],
    ])


def user_waiting_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏳ Ожидает подтверждения", callback_data="noop")],
    ])


# ──────────────────────────────────────────────
#  КЛАВИАТУРЫ — АДМИН
# ──────────────────────────────────────────────

def admin_order_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Отправить QR", callback_data=f"qr:{order_id}")],
        [InlineKeyboardButton("💬 Написать пользователю", callback_data=f"chat:{order_id}")],
        [InlineKeyboardButton("✅ Оплата получена", callback_data=f"paid:{order_id}")],
        [InlineKeyboardButton("🏁 Завершить", callback_data=f"done:{order_id}"),
         InlineKeyboardButton("❌ Отменить", callback_data=f"cancel:{order_id}")],
    ])


def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Цены", callback_data="admin_prices"),
         InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("📋 Активные заказы", callback_data="admin_orders")],
    ])


def admin_price_select_kb() -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    items = list(STAR_PRICES.items())
    for i in range(0, len(items), 2):
        row = []
        for stars, price in items[i:i + 2]:
            row.append(InlineKeyboardButton(f"{stars}⭐ — {price}{CURRENCY}", callback_data=f"chgprice:{stars}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin_back")])
    return InlineKeyboardMarkup(rows)


def admin_chat_kb() -> ReplyKeyboardMarkup:
    """Клавиатура админа в режиме чата с пользователем."""
    return ReplyKeyboardMarkup([["🔚 Завершить диалог"]], resize_keyboard=True)


# ──────────────────────────────────────────────
#  ТЕКСТЫ — КЛИЕНТ
# ──────────────────────────────────────────────

def support_text() -> str:
    return (
        "📞 <b>Поддержка</b>\n\n"
        "Напишите нам прямо здесь — ответ придёт в этот же чат.\n\n"
        "🕐 Работаем: 10:00–22:00 (МСК)\n"
        "⚡ Отвечаем за 5–10 минут\n\n"
        "Нажмите кнопку ниже 👇"
    )


def about_text() -> str:
    return (
        "ℹ️ <b>О магазине</b>\n\n"
        "⭐ Звёзды Telegram по выгодной цене\n"
        "💳 Оплата по QR-коду\n"
        "⚡ Начисление после оплаты\n\n"
        "<b>Как купить:</b>\n"
        "1️⃣ Выберите количество звёзд\n"
        "2️⃣ Дождитесь QR-код (до 10 минут)\n"
        "3️⃣ Оплатите — нажмите «Я оплатил(а)»\n"
        "4️⃣ Получите звёзды\n\n"
        "💡 Цена при оплате может быть ниже указанной"
    )


def qr_caption(order: Order) -> str:
    return (
        f"💳 <b>Оплата заказа #{order.id}</b>\n\n"
        f"⭐ {order.stars} звёзд\n"
        f"💰 <b>{order.amount_rub:g}{CURRENCY}</b>\n"
        f"💡 Цена при оплате может быть ниже\n\n"
        f"📱 <b>Отсканируйте QR-код в мобильном приложении вашего банка.</b>\n"
        f"Откройте приложение банка на телефоне → найдите функцию сканирования QR → наведите на код.\n\n"
        f"После оплаты нажмите кнопку ниже 👇"
    )


def qr_caption_paid(order: Order) -> str:
    return (
        f"💳 <b>Оплата заказа #{order.id}</b>\n\n"
        f"⭐ {order.stars} звёзд\n"
        f"💰 <b>{order.amount_rub:g}{CURRENCY}</b>\n\n"
        f"✅ Вы нажали «Я оплатил(а)».\n"
        f"Ожидайте подтверждения от администратора."
    )


# ──────────────────────────────────────────────
#  ТЕКСТЫ — АДМИН
# ──────────────────────────────────────────────

def admin_panel_text() -> str:
    return (
        "🔧 <b>Панель администратора</b>\n\n"
        "💰 <b>Цены</b> — изменить цену на звёзды\n"
        "📊 <b>Статистика</b> — заказы, аптайм, ошибки\n"
        "📋 <b>Заказы</b> — список активных заказов\n\n"
        "Заказы от клиентов приходят сюда автоматически.\n"
        "💬 Кнопка «Написать пользователю» — открыть чат с клиентом."
    )


def admin_order_text(order: Order) -> str:
    return (
        f"🔔 <b>Заказ #{order.id}</b>\n\n"
        f"👤 @{esc(order.username)} (ID: {order.user_id})\n"
        f"⭐ {order.stars} звёзд\n"
        f"💰 {order.amount_rub:g}{CURRENCY}\n\n"
        f"Выберите действие 👇"
    )


def admin_paid_claim_text(order: Order) -> str:
    return (
        f"💰 <b>Клиент нажал «Я оплатил(а)»</b>\n\n"
        f"Заказ #{order.id}\n"
        f"👤 @{esc(order.username)} (ID: {order.user_id})\n"
        f"⭐ {order.stars} звёзд\n"
        f"💰 {order.amount_rub:g}{CURRENCY}\n\n"
        f"Проверьте оплату и подтвердите:"
    )


def admin_stats_text() -> str:
    uptime = ""
    if stats["start_time"]:
        secs = int(asyncio.get_event_loop().time() - stats["start_time"])
        h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
        uptime = f"{h}ч {m}м {s}с"
    active = sum(1 for o in orders.values() if o.status in ("new", "qr_sent", "paid"))
    return (
        f"📊 <b>Статистика</b>\n\n"
        f"⏱ Работает: {uptime}\n"
        f"📦 Заказов всего: {stats['orders_total']}\n"
        f"🔄 Активных: {active}\n"
        f"💬 Сообщений: {stats['messages_total']}\n"
        f"❌ Ошибок: {stats['errors_total']}\n"
        f"👥 В поддержке: {len(support_mode_users)}\n"
        f"💬 Активных чатов: {len(active_admin_chats)}\n"
        f"👮 Админов: {len(ADMIN_IDS)}"
    )


def admin_orders_text() -> str:
    active = [o for o in orders.values() if o.status in ("new", "qr_sent", "paid")]
    if not active:
        return "📋 Активных заказов нет."
    status_emoji = {"new": "⏳", "qr_sent": "💳", "paid": "✅"}
    lines = ["📋 <b>Активные заказы</b>\n"]
    for o in active:
        lines.append(f"{status_emoji.get(o.status, '⏳')} #{o.id} — {o.stars}⭐ — {o.amount_rub:g}{CURRENCY} — @{esc(o.username)}")
    return "\n".join(lines)


# ──────────────────────────────────────────────
#  КОМАНДЫ
# ──────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    support_mode_users.discard(uid)
    # Если был активный чат — выходим из него
    if uid in active_admin_chats:
        active_admin_chats.pop(uid)
    if uid in ADMIN_IDS:
        context.user_data.pop("state", None)
        context.user_data.pop("chat_target", None)
    stats["messages_total"] += 1
    await update.message.reply_text(
        "⭐ <b>Магазин Telegram Stars</b>\n\n"
        "Купите звёзды по выгодной цене.\n"
        "💡 Цена при оплате может быть ниже указанной\n\n"
        "Выберите в меню ниже 👇",
        parse_mode="HTML",
        reply_markup=user_menu_kb(),
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Только для администратора.")
        return
    # Выходим из режима чата, если был
    context.user_data.pop("state", None)
    context.user_data.pop("chat_target", None)
    await update.message.reply_text(admin_panel_text(), parse_mode="HTML", reply_markup=admin_panel_kb())


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    await update.message.reply_text(admin_stats_text(), parse_mode="HTML")


async def handle_user_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    stats["messages_total"] += 1
    text = update.message.text
    user = update.effective_user
    uid = user.id

    try:
        # ── Админ вводит новую цену ──
        if uid in ADMIN_IDS and context.user_data.get("state") == "awaiting_new_price":
            stars = context.user_data.get("price_change_stars")
            try:
                new_price = int(text.strip())
                if new_price < 1:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("Введите число больше 0:")
                return
            old_price = STAR_PRICES.get(stars)
            STAR_PRICES[stars] = new_price
            await save_prices(STAR_PRICES)
            context.user_data.pop("state", None)
            context.user_data.pop("price_change_stars", None)
            await update.message.reply_text(
                f"✅ {stars}⭐: {old_price}{CURRENCY} → <b>{new_price}{CURRENCY}</b>",
                parse_mode="HTML",
            )
            return

        # ── Админ завершает чат с пользователем ──
        if uid in ADMIN_IDS and text == "🔚 Завершить диалог":
            target_user = context.user_data.get("chat_target")
            if target_user:
                active_admin_chats.pop(target_user, None)
                await safe_send(
                    context.bot, target_user,
                    "🔚 Диалог с поддержкой завершён.\n"
                    "Если понадобимся — пишите снова! 👋",
                    reply_markup=user_menu_kb(),
                )
            context.user_data.pop("state", None)
            context.user_data.pop("chat_target", None)
            await update.message.reply_text(
                "🔚 Диалог завершён.\n\n/admin — панель управления",
            )
            return

        # ── Админ пишет первое сообщение в чат пользователю ──
        if uid in ADMIN_IDS and context.user_data.get("state") == "awaiting_chat_message":
            target_user = context.user_data.get("chat_target")
            if not target_user:
                context.user_data.pop("state", None)
                context.user_data.pop("chat_target", None)
                await update.message.reply_text("⚠️ Ошибка: пользователь не найден.")
                return
            # Убираем пользователя из support_mode, чтобы не было конфликтов
            support_mode_users.discard(target_user)
            ok = await safe_send(
                context.bot, target_user,
                f"💬 <b>Поддержка:</b>\n\n{esc(text)}",
                reply_markup=user_chat_kb(),
            )
            if ok:
                active_admin_chats[target_user] = uid
                context.user_data["state"] = "in_chat_with_user"
                await update.message.reply_text(
                    "✅ Сообщение отправлено.\n"
                    "Продолжайте писать — все сообщения идут пользователю.\n"
                    "🔚 «Завершить диалог» — закончить чат.",
                    reply_markup=admin_chat_kb(),
                )
            else:
                context.user_data.pop("state", None)
                context.user_data.pop("chat_target", None)
                await update.message.reply_text("⚠️ Пользователь заблокировал бота.")
            return

        # ── Админ в активном чате — текст идёт пользователю ──
        if uid in ADMIN_IDS and context.user_data.get("state") == "in_chat_with_user":
            target_user = context.user_data.get("chat_target")
            if not target_user:
                context.user_data.pop("state", None)
                context.user_data.pop("chat_target", None)
                await update.message.reply_text("⚠️ Ошибка: пользователь не найден.")
                return
            ok = await safe_send(
                context.bot, target_user,
                f"💬 <b>Поддержка:</b>\n\n{esc(text)}",
                reply_markup=user_chat_kb(),
            )
            if ok:
                await update.message.reply_text("✅ Отправлено", reply_markup=admin_chat_kb())
            else:
                active_admin_chats.pop(target_user, None)
                context.user_data.pop("state", None)
                context.user_data.pop("chat_target", None)
                await update.message.reply_text("⚠️ Пользователь заблокировал бота. Чат завершён.")
            return

        # ── Админ отвечает reply-ом на сообщение поддержки ──
        if uid in ADMIN_IDS and update.message.reply_to_message:
            replied_id = update.message.reply_to_message.message_id
            if replied_id in support_msg_map:
                target_user = support_msg_map[replied_id]
                ok = await safe_send(
                    context.bot, target_user,
                    f"💬 <b>Поддержка:</b>\n\n{esc(text)}",
                    reply_markup=user_chat_kb() if target_user in active_admin_chats else None,
                )
                if ok:
                    # Если ещё не в активном чате — переводим в режим чата
                    if target_user not in active_admin_chats:
                        active_admin_chats[target_user] = uid
                        context.user_data["state"] = "in_chat_with_user"
                        context.user_data["chat_target"] = target_user
                    await update.message.reply_text(
                        "✅ Отправлено. Теперь можете писать без reply — сообщения идут пользователю.",
                        reply_markup=admin_chat_kb(),
                    )
                else:
                    await update.message.reply_text("⚠️ Пользователь заблокировал бота.")
                return

        # ── Админ пишет текст без reply и без активного чата ──
        if uid in ADMIN_IDS:
            await update.message.reply_text(
                "💡 Чтобы ответить клиенту — нажмите <b>reply</b> на его сообщение "
                "или откройте чат через «💬 Написать пользователю» на заказе.\n\n"
                "/admin — панель управления\n/stats — статистика",
                parse_mode="HTML",
            )
            return

        # ── Клиент в активном чате с админом ──
        if uid in active_admin_chats:
            if text == "⬅️ В меню":
                admin_id = active_admin_chats.pop(uid)
                await safe_send(
                    context.bot, admin_id,
                    f"ℹ️ Пользователь @{esc(user.username or 'без_username')} (ID: {uid}) вышел из чата.",
                )
                await update.message.reply_text(
                    "Вы вышли из чата с поддержкой.", reply_markup=user_menu_kb()
                )
                return
            admin_id = active_admin_chats[uid]
            uname = user.username or "без_username"
            sent = await safe_send(
                context.bot, admin_id,
                f"💬 <b>@{esc(uname)}</b> (ID: {uid}):\n\n{esc(text)}\n\n↩️ Reply, чтобы ответить",
                reply_markup=admin_chat_kb(),
            )
            if sent:
                remember_support_msg(sent.message_id, uid)
            await update.message.reply_text("✅ Отправлено.", reply_markup=user_chat_kb())
            return

        # ── Клиент в режиме поддержки ──
        if uid in support_mode_users:
            if text == "⬅️ Выйти из поддержки":
                support_mode_users.discard(uid)
                await update.message.reply_text("Вы вышли из поддержки.", reply_markup=user_menu_kb())
                return
            uname = user.username or "без_username"
            await notify_admins(
                context.bot,
                f"💬 <b>@{esc(uname)}</b> (ID: {uid}):\n\n{esc(text)}\n\n↩️ Reply, чтобы ответить",
            )
            await update.message.reply_text("✅ Отправлено. Ждите ответа.")
            return

        # ── Меню клиента ──
        if text == "⭐ Заказать звёзды":
            await update.message.reply_text(
                "⭐ <b>Выберите количество:</b>\n\n💡 Цена при оплате может быть ниже 👇",
                parse_mode="HTML", reply_markup=stars_kb(),
            )
        elif text == "📋 Мои заказы":
            user_orders = [o for o in orders.values() if o.user_id == uid]
            if not user_orders:
                await update.message.reply_text("Заказов пока нет.")
                return
            se = {"new": "⏳", "qr_sent": "💳", "paid": "✅", "completed": "🏁", "cancelled": "❌"}
            lines = [f"{se.get(o.status, '⏳')} #{o.id}: {o.stars}⭐ — {o.amount_rub:g}{CURRENCY}" for o in user_orders]
            await update.message.reply_text("\n".join(lines))
        elif text == "📞 Поддержка":
            await update.message.reply_text(support_text(), parse_mode="HTML", reply_markup=support_kb())
        elif text == "ℹ️ О магазине":
            await update.message.reply_text(about_text(), parse_mode="HTML")
        else:
            await update.message.reply_text("Используйте кнопки меню 👇", reply_markup=user_menu_kb())

    except Exception as e:
        logger.error("Ошибка в handle_user_text: %s", e, exc_info=True)
        stats["errors_total"] += 1
        try:
            await update.message.reply_text("⚠️ Ошибка. Попробуйте позже.")
        except Exception:
            pass


# ──────────────────────────────────────────────
#  CALLBACK-ОБРАБОТЧИКИ
# ──────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    stats["messages_total"] += 1
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    try:
        if data == "noop":
            await safe_answer(query)
            return

        if data == "back_to_menu":
            await safe_answer(query)
            await safe_edit(query, "⭐ Вы в главном меню 👇")
            return

        if data == "enter_support":
            support_mode_users.add(user_id)
            await safe_answer(query)
            await safe_edit(
                query,
                "📞 <b>Режим поддержки</b>\n\nПишите — админ ответит здесь же.\nДля выхода: «⬅️ Выйти из поддержки».",
                reply_markup=InlineKeyboardMarkup([]),
            )
            await safe_send(context.bot, query.message.chat_id, "Напишите ваше сообщение 👇", reply_markup=support_mode_kb())
            return

        # ── Клиент нажал «Я оплатил(а)» ──
        if data.startswith("ipaid:"):
            order_id = int(data.split(":", 1)[1])
            order = orders.get(order_id)
            if not order:
                await safe_answer(query, "Заказ не найден", show_alert=True)
                return
            if order.status in ("paid", "completed"):
                await safe_answer(query, "Заказ уже обработан", show_alert=True)
                return
            if order.status == "cancelled":
                await safe_answer(query, "Заказ отменён", show_alert=True)
                return
            await safe_edit_caption(query, qr_caption_paid(order), reply_markup=user_waiting_kb())
            await safe_answer(query, "Ожидайте подтверждения оплаты")
            await notify_admins(context.bot, admin_paid_claim_text(order), reply_markup=admin_order_kb(order_id))
            return

        # ── Клиент выбирает звёзды ──
        if data.startswith("order:"):
            stars = int(data.split(":", 1)[1])
            if stars not in STAR_PRICES:
                await safe_answer(query, "Недоступно", show_alert=True)
                return
            price = STAR_PRICES[stars]
            user = query.from_user
            global next_order_id
            order_id = next_order_id
            next_order_id += 1
            stats["orders_total"] += 1
            order = Order(id=order_id, user_id=user.id, username=user.username or "без_username", stars=stars, amount_rub=price)
            orders[order_id] = order
            await save_orders(orders)
            await safe_answer(query)
            await safe_edit(
                query,
                f"✅ <b>Заказ #{order_id}</b>\n\n⭐ {stars} звёзд\n💰 <b>{price}{CURRENCY}</b>\n💡 Цена может быть ниже\n\n⏳ QR-код придёт в течение <b>10 минут</b>",
            )
            await notify_admins(context.bot, admin_order_text(order), reply_markup=admin_order_kb(order_id))
            return

        # ── Дальше — только админ ──
        if user_id not in ADMIN_IDS:
            await safe_answer(query, "Нет доступа", show_alert=True)
            return

        if data == "admin_back":
            await safe_answer(query)
            await safe_edit(query, admin_panel_text(), reply_markup=admin_panel_kb())

        elif data == "admin_prices":
            await safe_answer(query)
            lines = [f"{s}⭐ — {p}{CURRENCY}" for s, p in STAR_PRICES.items()]
            await safe_edit(query, "💰 <b>Цены</b>\n\n" + "\n".join(lines) + "\n\nНажмите позицию для смены цены:", reply_markup=admin_price_select_kb())

        elif data == "admin_stats":
            await safe_answer(query)
            await safe_edit(query, admin_stats_text(), reply_markup=admin_panel_kb())

        elif data == "admin_orders":
            await safe_answer(query)
            await safe_edit(query, admin_orders_text(), reply_markup=admin_panel_kb())

        elif data.startswith("chgprice:"):
            stars = int(data.split(":", 1)[1])
            old_price = STAR_PRICES.get(stars, "?")
            context.user_data["state"] = "awaiting_new_price"
            context.user_data["price_change_stars"] = stars
            await safe_answer(query)
            await safe_edit(query, f"💰 <b>{stars}⭐</b>\nСейчас: {old_price}{CURRENCY}\n\nВведите новую цену:")

        elif data.startswith("chat:"):
            order_id = int(data.split(":", 1)[1])
            order = orders.get(order_id)
            if not order:
                await safe_answer(query, "Заказ не найден", show_alert=True)
                return
            context.user_data["state"] = "awaiting_chat_message"
            context.user_data["chat_target"] = order.user_id
            await safe_answer(query)
            await safe_send(
                context.bot, user_id,
                f"💬 <b>Чат с пользователем</b>\n"
                f"👤 @{esc(order.username)} (ID: {order.user_id})\n"
                f"⭐ Заказ #{order_id}: {order.stars}⭐ — {order.amount_rub:g}{CURRENCY}\n\n"
                f"Напишите сообщение — оно будет отправлено пользователю.\n"
                f"Кнопка «🔚 Завершить диалог» — закончить чат.",
                reply_markup=admin_chat_kb(),
            )

        elif data.startswith("qr:"):
            order_id = int(data.split(":", 1)[1])
            order = orders.get(order_id)
            if not order:
                await safe_answer(query, "Заказ не найден", show_alert=True)
                return
            admin_pending_qr[user_id] = order_id
            await safe_answer(query, "Отправьте фото с QR", show_alert=True)
            await safe_send(context.bot, user_id, f"📸 Отправьте фото QR для заказа #{order_id}\nПолучатель: @{esc(order.username)}")

        elif data.startswith("paid:"):
            order_id = int(data.split(":", 1)[1])
            order = orders.get(order_id)
            if not order:
                return
            order.status = "paid"
            await save_orders(orders)
            await safe_send(context.bot, order.user_id, f"✅ <b>Оплата получена!</b>\n\nЗаказ #{order.id}: {order.stars}⭐\n\nЗвёзды будут начислены скоро. Спасибо! 🫧")
            await safe_answer(query)
            await safe_edit(query, f"✅ Заказ #{order_id} оплачен.\nПодарите {order.stars}⭐ @{esc(order.username)}")

        elif data.startswith("done:"):
            order_id = int(data.split(":", 1)[1])
            order = orders.get(order_id)
            if not order:
                return
            order.status = "completed"
            await save_orders(orders)
            await safe_send(context.bot, order.user_id, f"🏁 Заказ #{order.id} завершён. Звёзды начислены! ⭐")
            await safe_answer(query)
            await safe_edit(query, f"🏁 Заказ #{order_id} завершён.")

        elif data.startswith("cancel:"):
            order_id = int(data.split(":", 1)[1])
            order = orders.get(order_id)
            if not order:
                return
            order.status = "cancelled"
            await save_orders(orders)
            await safe_send(context.bot, order.user_id, f"❌ Заказ #{order.id} отменён.")
            await safe_answer(query)
            await safe_edit(query, f"❌ Заказ #{order_id} отменён.")

    except Exception as e:
        logger.error("Ошибка в handle_callback: %s", e, exc_info=True)
        stats["errors_total"] += 1
        await safe_answer(query, "Ошибка", show_alert=True)


# ──────────────────────────────────────────────
#  ОБРАБОТКА ФОТО
# ──────────────────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    stats["messages_total"] += 1
    user = update.effective_user
    photo = update.message.photo[-1]
    caption = update.message.caption or ""

    try:
        # ── Админ отвечает фото на сообщение поддержки (reply) ──
        if user.id in ADMIN_IDS and update.message.reply_to_message:
            replied_id = update.message.reply_to_message.message_id
            if replied_id in support_msg_map:
                target_user = support_msg_map[replied_id]
                cap = f"💬 <b>Поддержка:</b>\n\n{esc(caption)}" if caption else "💬 <b>Поддержка</b>"
                ok = await safe_send_photo(context.bot, target_user, photo.file_id, cap,
                                           reply_markup=user_chat_kb() if target_user in active_admin_chats else None)
                if ok:
                    if target_user not in active_admin_chats:
                        active_admin_chats[target_user] = user.id
                        context.user_data["state"] = "in_chat_with_user"
                        context.user_data["chat_target"] = target_user
                    await update.message.reply_text("✅ Фото отправлено.", reply_markup=admin_chat_kb())
                else:
                    await update.message.reply_text("⚠️ Пользователь заблокировал бота.")
                return

        # ── Админ в режиме чата шлёт фото пользователю (без reply) ──
        if user.id in ADMIN_IDS and context.user_data.get("state") in ("awaiting_chat_message", "in_chat_with_user"):
            target_user = context.user_data.get("chat_target")
            if target_user:
                cap = f"💬 <b>Поддержка:</b>\n\n{esc(caption)}" if caption else "💬 <b>Поддержка</b>"
                ok = await safe_send_photo(context.bot, target_user, photo.file_id, cap, reply_markup=user_chat_kb())
                if ok:
                    if context.user_data.get("state") == "awaiting_chat_message":
                        active_admin_chats[target_user] = user.id
                        context.user_data["state"] = "in_chat_with_user"
                    await update.message.reply_text("✅ Фото отправлено.", reply_markup=admin_chat_kb())
                else:
                    await update.message.reply_text("⚠️ Пользователь заблокировал бота.")
                return

        # ── Клиент в активном чате шлёт фото ──
        if user.id in active_admin_chats:
            admin_id = active_admin_chats[user.id]
            uname = user.username or "без_username"
            cap = f"💬 <b>@{esc(uname)}</b> (ID: {user.id}):\n\n{esc(caption)}\n\n↩️ Reply, чтобы ответить" if caption else f"💬 <b>@{esc(uname)}</b> (ID: {user.id})\n\n↩️ Reply, чтобы ответить"
            sent = await safe_send_photo(context.bot, admin_id, photo.file_id, cap, reply_markup=admin_chat_kb())
            if sent:
                remember_support_msg(sent.message_id, user.id)
                await update.message.reply_text("✅ Фото отправлено.", reply_markup=user_chat_kb())
            else:
                await update.message.reply_text("⚠️ Ошибка. Попробуйте позже.")
            return

        # ── Клиент в режиме поддержки шлёт фото ──
        if user.id in support_mode_users:
            uname = user.username or "без_username"
            cap = f"💬 <b>@{esc(uname)}</b> (ID: {user.id}):\n\n{esc(caption)}\n\n↩️ Reply, чтобы ответить"
            sent = False
            for admin_id in ADMIN_IDS:
                result = await safe_send_photo(context.bot, admin_id, photo.file_id, cap)
                if result:
                    remember_support_msg(result.message_id, user.id)
                    sent = True
            if sent:
                await update.message.reply_text("✅ Фото отправлено в поддержку.")
            else:
                await update.message.reply_text("⚠️ Ошибка. Попробуйте позже.")
            return

        # ── Админ шлёт QR-код для заказа ──
        if user.id in ADMIN_IDS:
            order_id = admin_pending_qr.pop(user.id, None)
            if not order_id:
                await update.message.reply_text(
                    "💡 Сначала нажмите «📤 Отправить QR» на заказе.\n"
                    "Или reply на сообщение поддержки, чтобы ответить фото.\n"
                    "Или вы в режиме чата — фото уйдёт пользователю."
                )
                return
            order = orders.get(order_id)
            if not order:
                await update.message.reply_text("Заказ не найден.")
                return
            order.status = "qr_sent"
            await save_orders(orders)
            ok = await safe_send_photo(
                context.bot, order.user_id, photo.file_id,
                qr_caption(order),
                reply_markup=user_paid_kb(order.id),
            )
            if ok:
                await update.message.reply_text(f"✅ QR отправлен @{esc(order.username)}")
            else:
                await update.message.reply_text("⚠️ Не удалось отправить QR. Пользователь мог заблокировать бота.")

    except Exception as e:
        logger.error("Ошибка в handle_photo: %s", e, exc_info=True)
        stats["errors_total"] += 1
        try:
            await update.message.reply_text("⚠️ Ошибка. Попробуйте позже.")
        except Exception:
            pass


# ──────────────────────────────────────────────
#  ERROR HANDLER
# ──────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    stats["errors_total"] += 1
    error = context.error
    if isinstance(error, RetryAfter):
        logger.warning("Flood wait: %sс", error.retry_after)
    elif isinstance(error, TimedOut):
        logger.warning("Таймаут: %s", error)
    elif isinstance(error, NetworkError):
        logger.warning("Сетевая ошибка: %s", error)
    elif isinstance(error, Forbidden):
        logger.warning("Forbidden: %s", error)
    elif isinstance(error, BadRequest):
        logger.warning("BadRequest: %s", error)
    else:
        logger.error("Необработанная ошибка: %s", error, exc_info=error)


# ──────────────────────────────────────────────
#  ИНИЦИАЛИЗАЦИЯ И ЗАПУСК
# ──────────────────────────────────────────────

async def post_init(app) -> None:
    global STAR_PRICES, orders, next_order_id
    STAR_PRICES = await load_prices()
    orders = await load_orders()
    next_order_id = max(orders.keys()) + 1 if orders else 1
    stats["start_time"] = asyncio.get_event_loop().time()
    logger.info("Бот запущен. Заказов: %d. Цен: %d. Админов: %d", len(orders), len(STAR_PRICES), len(ADMIN_IDS))


def main() -> None:
    logger.info("Бот запускается...")

    if BOT_TOKEN == "ВСТАВЬТЕ_СЮДА_ВАШ_ТОКЕН":
        logger.error("⚠️  Вставьте токен бота в переменную BOT_TOKEN!")
        return

    if not ADMIN_IDS:
        logger.error("⚠️  Вставьте ID админов в ADMIN_IDS!")
        return

    builder = ApplicationBuilder().token(BOT_TOKEN)

    if PROXY_URL:
        builder = builder.proxy(PROXY_URL).get_updates_proxy(PROXY_URL)

    builder = (
        builder
        .connect_timeout(60.0)
        .read_timeout(60.0)
        .write_timeout(60.0)
        .pool_timeout(60.0)
        .concurrent_updates(10)
    )

    app = builder.build()
    app.post_init = post_init

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.add_error_handler(error_handler)

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        poll_interval=2.0,
        timeout=30,
    )


if __name__ == "__main__":
    main()

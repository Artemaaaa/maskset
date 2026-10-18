import asyncio
import os
import sqlite3
import re
from io import BytesIO
from aiogram.types import BufferedInputFile
from datetime import datetime
from html import escape

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from typing import Callable, Awaitable, Any
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ORDER_BOT_TOKEN = os.getenv("ORDER_BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
PAYMENT_DETAILS = os.getenv("PAYMENT_DETAILS", "Укажите реквизиты в файле .env")
SHOP_NAME = os.getenv("SHOP_NAME", "Maskset")
DB_PATH = os.getenv("DB_PATH", "shop.db")
MIN_ORDER_AMOUNT = 2000
FREE_DELIVERY_FROM = 2000
DELIVERY_FEE = 100

if not TOKEN:
    raise RuntimeError("Заполните BOT_TOKEN в переменных Railway")
if not ORDER_BOT_TOKEN:
    raise RuntimeError("Заполните ORDER_BOT_TOKEN — токен отдельного бота заказов")

bot = Bot(TOKEN)
dp = Dispatcher()
order_bot = Bot(ORDER_BOT_TOKEN)
order_dp = Dispatcher()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'Прочее',
            price REAL NOT NULL, stock INTEGER NOT NULL DEFAULT 0,
            description TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, username TEXT DEFAULT '',
            customer_name TEXT NOT NULL, phone TEXT NOT NULL,
            house TEXT NOT NULL, comment TEXT DEFAULT '',
            items TEXT NOT NULL, subtotal REAL NOT NULL, delivery REAL NOT NULL,
            total REAL NOT NULL, status TEXT NOT NULL DEFAULT 'Ожидает оплату',
            receipt_file_id TEXT DEFAULT '', created_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS order_admin_messages (
            order_id INTEGER NOT NULL,
            admin_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            PRIMARY KEY (order_id, admin_id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS broadcast_users (
            user_id INTEGER PRIMARY KEY,
            first_seen TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS user_access (
            user_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL CHECK(status IN ('approved','pending','rejected')),
            full_name TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            requested_at TEXT NOT NULL,
            reviewed_at TEXT DEFAULT '',
            reviewed_by INTEGER DEFAULT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS access_admin_messages (
            user_id INTEGER NOT NULL,
            admin_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            PRIMARY KEY(user_id,admin_id)
        )""")
        # Переносим в одобренные только тех, кто пользовался ботом ДО обновления.
        # Выполняется единожды; новые пользователи не получают доступ автоматически.
        migration = c.execute("SELECT value FROM app_meta WHERE key='access_migrated'").fetchone() if \
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='app_meta'").fetchone() else None
        if migration is None:
            c.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            c.execute("""INSERT OR IGNORE INTO user_access(user_id,status,requested_at)
                         SELECT user_id,'approved',first_seen FROM broadcast_users""")
            c.execute("""INSERT OR IGNORE INTO user_access(user_id,status,requested_at)
                         SELECT DISTINCT user_id,'approved',? FROM orders""",
                      (datetime.now().isoformat(timespec='seconds'),))
            c.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('access_migrated','1')")
        # Клиенты с ранее оформленными заказами тоже смогут получать рассылку.
        c.execute("""INSERT OR IGNORE INTO broadcast_users (user_id, first_seen)
                     SELECT DISTINCT user_id, ? FROM orders""", (datetime.now().isoformat(timespec="seconds"),))


class Broadcast(StatesGroup):
    content = State()
    confirm = State()


class BulkProducts(StatesGroup):
    input = State()
    confirm = State()


class EditProduct(StatesGroup):
    product_id = State()
    details = State()
    confirm = State()


class AccessManage(StatesGroup):
    user_id = State()


class Checkout(StatesGroup):
    name = State()
    house = State()
    comment = State()
    receipt = State()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


class TrackCustomerMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        chat = getattr(event, "chat", None)
        if chat is None:
            chat = getattr(getattr(event, "message", None), "chat", None)
        if user and not user.is_bot and chat and chat.type == "private":
            with db() as c:
                c.execute(
                    "INSERT OR IGNORE INTO broadcast_users (user_id, first_seen) VALUES (?, ?)",
                    (user.id, datetime.now().isoformat(timespec="seconds"))
                )
        return await handler(event, data)


def access_keyboard(user_id, status):
    rows = []
    if status != "approved":
        rows.append([InlineKeyboardButton(text="✅ Одобрить доступ", callback_data=f"access:approve:{user_id}")])
    if status != "rejected":
        rows.append([InlineKeyboardButton(text="⛔ Отклонить / заблокировать", callback_data=f"access:reject:{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def access_user_text(user_id, name, username, status):
    status_ru = {"approved": "Одобрен", "pending": "Ожидает одобрения", "rejected": "Отклонён"}
    return (f"👤 Пользователь Maskset\nИмя: {name or 'Не указано'}\n"
            f"Telegram: @{username}\n" if username else
            f"👤 Пользователь Maskset\nИмя: {name or 'Не указано'}\n") + \
           f"ID: {user_id}\nСтатус: {status_ru.get(status, status)}"


async def send_access_request(user_id, name, username):
    text = "🔔 Новая заявка на доступ\n" + access_user_text(user_id, name, username, "pending")
    for admin_id in ADMIN_IDS:
        try:
            sent = await order_bot.send_message(admin_id, text, reply_markup=access_keyboard(user_id, "pending"))
            with db() as c:
                c.execute("INSERT OR REPLACE INTO access_admin_messages(user_id,admin_id,message_id) VALUES(?,?,?)",
                          (user_id, admin_id, sent.message_id))
        except Exception as exc:
            print(f"Не удалось отправить заявку пользователя {user_id} администратору {admin_id}: {exc}")


class AccessGateMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        chat = getattr(event, "chat", None) or getattr(getattr(event, "message", None), "chat", None)
        if not user or user.is_bot or not chat or chat.type != "private" or is_admin(user.id):
            return await handler(event, data)
        with db() as c:
            row = c.execute("SELECT status FROM user_access WHERE user_id=?", (user.id,)).fetchone()
            new_request = row is None
            if new_request:
                c.execute("""INSERT OR IGNORE INTO user_access
                             (user_id,status,full_name,username,requested_at)
                             VALUES(?,?,?,?,?)""",
                          (user.id, "pending", user.full_name or "", user.username or "",
                           datetime.now().isoformat(timespec="seconds")))
                row = c.execute("SELECT status FROM user_access WHERE user_id=?", (user.id,)).fetchone()
        if new_request and row["status"] == "pending":
            await send_access_request(user.id, user.full_name or "", user.username or "")
        if row["status"] == "approved":
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer("Доступ к магазину ещё не разрешён" if row["status"] == "pending"
                               else "Доступ закрыт администратором", show_alert=True)
        elif isinstance(event, Message):
            if row["status"] == "pending":
                await event.answer("⏳ Ваша заявка отправлена администраторам Maskset. Ожидайте одобрения.")
            else:
                await event.answer("⛔ Доступ к Maskset отклонён. Повторная заявка недоступна. "
                                   "Только администратор может разрешить вход.")
        return None


dp.message.outer_middleware(AccessGateMiddleware())
dp.callback_query.outer_middleware(AccessGateMiddleware())

dp.message.middleware(TrackCustomerMiddleware())
dp.callback_query.middleware(TrackCustomerMiddleware())
broadcast_lock = asyncio.Lock()


def broadcast_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить товары списком", callback_data="bulk:new")],
        [InlineKeyboardButton(text="✏️ Изменить товар по ID", callback_data="admin:editproduct")],
        [InlineKeyboardButton(text="🗑 Удалить товар по ID", callback_data="admin:deleteproduct")],
        [InlineKeyboardButton(text="👥 Количество пользователей", callback_data="admin:users")],
        [InlineKeyboardButton(text="🔐 Управление доступом", callback_data="access:menu")],
        [InlineKeyboardButton(text="📦 Остатки", callback_data="admin:stock")],
        [InlineKeyboardButton(text="🛍 Каталог товаров", callback_data="admin:catalog")],
        [InlineKeyboardButton(text="📢 Рассылка всем пользователям", callback_data="broadcast:new")]
    ])


def broadcast_confirmation():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить всем", callback_data="broadcast:send")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast:cancel")]
    ])


def money(n):
    return f"{n:,.0f}".replace(",", " ") + " ₽"


def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 Каталог", callback_data="catalog")],
        [InlineKeyboardButton(text="🧺 Корзина", callback_data="cart"),
         InlineKeyboardButton(text="📦 Мои заказы", callback_data="myorders")],
        [InlineKeyboardButton(text="ℹ️ Доставка и оплата", callback_data="info")]
    ])



def categories_keyboard():
    with db() as c:
        categories = c.execute(
            "SELECT DISTINCT category FROM products WHERE active=1 AND stock>0 ORDER BY category"
        ).fetchall()
    rows = [[InlineKeyboardButton(text=str(row["category"])[:64], callback_data="category:" + str(row["category"])[:50])]
            for row in categories]
    if not rows:
        rows = [[InlineKeyboardButton(text="Пока нет товаров в наличии", callback_data="noop")]]
    rows.append([InlineKeyboardButton(text="🧺 Открыть корзину", callback_data="cart")])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def category_products_keyboard(category):
    with db() as c:
        products = c.execute(
            "SELECT * FROM products WHERE active=1 AND stock>0 AND category=? ORDER BY name", (category,)
        ).fetchall()
    rows = []
    for p in products:
        label = f"{p['name']} — {money(p['price'])} (ост. {p['stock']})"
        rows.append([InlineKeyboardButton(text=label[:64], callback_data=f"product:{p['id']}")])
    if not rows:
        rows = [[InlineKeyboardButton(text="В этой категории пока нет товаров", callback_data="noop")]]
    rows.append([InlineKeyboardButton(text="⬅️ Категории", callback_data="catalog")])
    rows.append([InlineKeyboardButton(text="🧺 Корзина", callback_data="cart")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def product_keyboard(product_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить в корзину", callback_data=f"add:{product_id}")],
        [InlineKeyboardButton(text="⬅️ Каталог", callback_data="catalog")]
    ])


# Корзина MVP хранится в памяти процесса; заказы и каталог — в SQLite.
carts = {}


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        f"Добро пожаловать в «{escape(SHOP_NAME)}»!\n\n"
        "Выберите товары в каталоге и оформите доставку прямо в Telegram.",
        reply_markup=main_keyboard()
    )


@dp.callback_query(F.data == "menu")
async def menu(call: CallbackQuery):
    await call.message.edit_text(f"«{escape(SHOP_NAME)}» — главное меню", reply_markup=main_keyboard())
    await call.answer()


@dp.callback_query(F.data == "noop")
async def noop(call: CallbackQuery):
    await call.answer("Товары скоро появятся", show_alert=True)


@dp.callback_query(F.data == "catalog")
async def catalog(call: CallbackQuery):
    await call.message.edit_text("🛍 Каталог Maskset\n\nВыберите категорию:", reply_markup=categories_keyboard())
    await call.answer()


@dp.callback_query(F.data.startswith("category:"))
async def show_category(call: CallbackQuery):
    category = call.data.split(":", 1)[1]
    await call.message.edit_text(
        f"🛍 Категория: {escape(category)}\n\nВыберите товар:",
        reply_markup=category_products_keyboard(category)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("product:"))
async def product(call: CallbackQuery):
    pid = int(call.data.split(":")[1])
    with db() as c:
        p = c.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        await call.answer("Товар не найден", show_alert=True)
        return
    text = f"<b>{escape(p['name'])}</b>\nКатегория: {escape(p['category'])}\nЦена: {money(p['price'])}\nВ наличии: {p['stock']}"
    if p["description"]:
        text += "\n\n" + escape(p["description"])
    await call.message.edit_text(text, reply_markup=product_keyboard(pid), parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data.startswith("add:"))
async def add_to_cart(call: CallbackQuery):
    pid = int(call.data.split(":")[1])
    with db() as c:
        p = c.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p or p["stock"] <= 0:
        await call.answer("Товара нет в наличии", show_alert=True)
        return
    cart = carts.setdefault(call.from_user.id, {})
    current = cart.get(pid, 0)
    if current >= p["stock"]:
        await call.answer("Больше нет в наличии", show_alert=True)
        return
    cart[pid] = current + 1
    await call.answer("Добавлено в корзину")


def get_cart(user_id):
    cart = carts.get(user_id, {})
    items, subtotal = [], 0
    with db() as c:
        for pid, qty in cart.items():
            p = c.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
            if p and p["stock"] > 0:
                qty = min(qty, p["stock"])
                if qty:
                    line = p["price"] * qty
                    items.append((p, qty, line))
                    subtotal += line
    return items, subtotal


@dp.callback_query(F.data == "cart")
async def show_cart(call: CallbackQuery):
    items, subtotal = get_cart(call.from_user.id)
    if not items:
        await call.message.edit_text("Корзина пока пуста.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛍 В каталог", callback_data="catalog")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")]
        ]))
        await call.answer()
        return
    delivery = 0 if subtotal >= FREE_DELIVERY_FROM else DELIVERY_FEE
    lines = ["🧺 <b>Ваша корзина</b>\n"]
    for p, qty, line in items:
        lines.append(f"• {escape(p['name'])} × {qty} — {money(line)}")
    lines += ["", f"Товары: {money(subtotal)}",
              f"Доставка: {'бесплатно' if delivery == 0 else money(delivery)}",
              f"<b>Итого: {money(subtotal + delivery)}</b>"]
    if subtotal < MIN_ORDER_AMOUNT:
        lines.append(f"\n⚠️ Минимальная сумма заказа — {money(MIN_ORDER_AMOUNT)}.")
        lines.append(f"Добавьте товаров ещё на {money(MIN_ORDER_AMOUNT - subtotal)}.")
    rows = []
    if subtotal >= MIN_ORDER_AMOUNT:
        rows.append([InlineKeyboardButton(text="✅ Оформить заказ", callback_data="checkout")])
    rows.extend([
        [InlineKeyboardButton(text="🛍 Продолжить покупки", callback_data="catalog")],
        [InlineKeyboardButton(text="🗑 Очистить корзину", callback_data="clearcart")]
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await call.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data == "clearcart")
async def clear_cart(call: CallbackQuery):
    carts.pop(call.from_user.id, None)
    await call.message.edit_text("Корзина очищена.", reply_markup=main_keyboard())
    await call.answer()


@dp.callback_query(F.data == "checkout")
async def checkout(call: CallbackQuery, state: FSMContext):
    items, subtotal = get_cart(call.from_user.id)
    if not items:
        await call.answer("Корзина пуста", show_alert=True)
        return
    if subtotal < MIN_ORDER_AMOUNT:
        await call.answer(
            f"Минимальный заказ {money(MIN_ORDER_AMOUNT)}. Добавьте ещё на {money(MIN_ORDER_AMOUNT - subtotal)}.",
            show_alert=True
        )
        return
    await state.set_state(Checkout.name)
    await call.message.answer("Как вас зовут?")
    await call.answer()


@dp.message(Checkout.name)
async def checkout_name(message: Message, state: FSMContext):
    await state.update_data(customer_name=message.text.strip()[:100])
    await state.set_state(Checkout.house)
    await message.answer("Укажите номер домика (и корпус/улицу, если нужно):")


@dp.message(Checkout.house)
async def checkout_house(message: Message, state: FSMContext):
    await state.update_data(house=message.text.strip()[:200])
    await state.set_state(Checkout.comment)
    await message.answer("Комментарий курьеру? Например, как найти домик. Если комментария нет, отправьте «-».")


@dp.message(Checkout.comment)
async def checkout_comment(message: Message, state: FSMContext):
    await state.update_data(comment="" if message.text.strip() == "-" else message.text.strip()[:500])
    data = await state.get_data()
    items, subtotal = get_cart(message.from_user.id)
    if not items:
        await state.clear()
        await message.answer("Корзина изменилась и теперь пуста. Начните заказ заново.", reply_markup=main_keyboard())
        return
    if subtotal < MIN_ORDER_AMOUNT:
        await state.clear()
        await message.answer(
            f"Минимальная сумма заказа — {money(MIN_ORDER_AMOUNT)}. "
            f"В корзине товаров на {money(subtotal)}. "
            f"Добавьте ещё на {money(MIN_ORDER_AMOUNT - subtotal)}.",
            reply_markup=main_keyboard()
        )
        return
    delivery = 0 if subtotal >= FREE_DELIVERY_FROM else DELIVERY_FEE
    total = subtotal + delivery
    item_lines = [f"{p['name']} × {q} = {money(line)}" for p, q, line in items]
    # Проверяем и списываем остатки в одной транзакции. Если что-то изменилось,
    # заказ не создаётся и ни один остаток не уменьшается.
    try:
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            for p, qty, _ in items:
                current = c.execute(
                    "SELECT price, stock, active FROM products WHERE id=?", (p["id"],)
                ).fetchone()
                if (not current or not current["active"] or current["stock"] < qty
                        or current["price"] != p["price"]):
                    raise ValueError("Товар или его цена/остаток изменились. Проверьте корзину.")
            cur = c.execute("""INSERT INTO orders
                (user_id, username, customer_name, phone, house, comment, items, subtotal, delivery, total, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (message.from_user.id, message.from_user.username or "", data["customer_name"], "",
                 data["house"], data.get("comment", ""), "\n".join(item_lines), subtotal, delivery, total,
                 "Ожидает оплату", datetime.now().isoformat(timespec="minutes")))
            order_id = cur.lastrowid
            for p, qty, _ in items:
                result = c.execute(
                    "UPDATE products SET stock=stock-? WHERE id=? AND stock>=? AND active=1",
                    (qty, p["id"], qty))
                if result.rowcount != 1:
                    raise ValueError("Недостаточно товара на складе. Проверьте корзину.")
    except ValueError as exc:
        await state.clear()
        await message.answer(str(exc) + "\nОткройте корзину заново.", reply_markup=main_keyboard())
        return
    carts.pop(message.from_user.id, None)
    await state.update_data(order_id=order_id, total=total)
    await state.set_state(Checkout.receipt)
    await message.answer(
        f"Заказ №{order_id} создан.\n\n{chr(10).join(item_lines)}\n\n"
        f"Товары: {money(subtotal)}\nДоставка: {'бесплатно' if delivery == 0 else money(delivery)}\n"
        f"Итого к оплате: {money(total)}\n\n"
        f"Переведите точную сумму по реквизитам:\n{escape(PAYMENT_DETAILS)}\n\n"
        "После перевода отправьте сюда скриншот чека. Заказ будет подтверждён после проверки администратором.",
        parse_mode="HTML"
    )


@dp.message(Checkout.receipt, F.photo)
async def receive_receipt(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    if not order_id:
        await state.clear()
        await message.answer("Не удалось определить заказ. Напишите /start.")
        return
    file_id = message.photo[-1].file_id
    with db() as c:
        c.execute("UPDATE orders SET receipt_file_id=?, status='Чек получен — проверка' WHERE id=? AND user_id=?",
                  (file_id, order_id, message.from_user.id))
    with db() as c:
        order = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if order:
        username = (order["username"] or "").strip().lstrip("@")
        contact_url = f"https://t.me/{username}" if username else f"tg://user?id={order['user_id']}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Связаться с покупателем", url=contact_url)],
            [InlineKeyboardButton(text="✅ Оплата подтверждена", callback_data=f"paid:{order_id}"),
             InlineKeyboardButton(text="❌ Отклонить чек", callback_data=f"reject:{order_id}")]
        ])
        caption = (f"🧾 Чек по заказу №{order_id}\n"
                   f"Покупатель: {order['customer_name']}\n"
                   f"Домик: {order['house']}\n"
                   f"Комментарий: {order['comment'] or 'Нет'}\n"
                   f"Сумма: {money(order['total'])}\n"
                   f"Статус: {order['status']}\n\n"
                   f"Состав заказа:\n{order['items']}")
        for admin_id in ADMIN_IDS:
            try:
                photo_bytes = BytesIO()
                await bot.download(file_id, destination=photo_bytes)

                sent = await order_bot.send_photo(
                    chat_id=admin_id,
                    photo=BufferedInputFile(
                        photo_bytes.getvalue(),
                        filename="receipt.jpg"
                    ),
                    caption=caption,
                    reply_markup=kb
                )
                with db() as c:
                    c.execute(
                        "INSERT OR REPLACE INTO order_admin_messages (order_id, admin_id, message_id) VALUES (?, ?, ?)",
                        (order_id, admin_id, sent.message_id)
                    )
            except Exception as exc:
                print(f"Ошибка отправки заказа: {exc}")
    await state.clear()
    await message.answer(f"Чек по заказу №{order_id} получен. Ожидайте подтверждения оплаты.", reply_markup=main_keyboard())


@dp.message(Checkout.receipt)
async def receipt_not_photo(message: Message):
    await message.answer("Пожалуйста, отправьте скриншот чека именно как фотографию.")


async def finish_payment_review(call: CallbackQuery, approved: bool):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return

    oid = int(call.data.split(":", 1)[1])
    new_status = "Оплачен — собирается" if approved else "Чек отклонён — требуется связь"
    # Условное обновление предотвращает повторное решение вторым администратором.
    with db() as c:
        cur = c.execute(
            "UPDATE orders SET status=? WHERE id=? AND status='Чек получен — проверка'",
            (new_status, oid)
        )
        changed = cur.rowcount > 0
        order = c.execute("SELECT user_id, username FROM orders WHERE id=?", (oid,)).fetchone()
        copies = c.execute(
            "SELECT admin_id, message_id FROM order_admin_messages WHERE order_id=?",
            (oid,)
        ).fetchall()

    if not changed:
        await call.answer("Этот заказ уже обработан", show_alert=True)
        return

    # Сохраняем кнопку связи с покупателем, удаляя только кнопки оплаты.
    if order:
        username = (order["username"] or "").strip().lstrip("@")
        contact_url = f"https://t.me/{username}" if username else f"tg://user?id={order['user_id']}"
        contact_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Связаться с покупателем", url=contact_url)]
        ])
    else:
        contact_keyboard = None

    for copy in copies:
        try:
            await order_bot.edit_message_reply_markup(
                chat_id=copy["admin_id"],
                message_id=copy["message_id"],
                reply_markup=contact_keyboard
            )
        except Exception as exc:
            print(f"Не удалось убрать кнопки оплаты заказа №{oid} у {copy['admin_id']}: {exc}")

    # На случай если копия не попала в таблицу (например, старый заказ).
    if not any(x["admin_id"] == call.from_user.id and
               x["message_id"] == call.message.message_id for x in copies):
        try:
            await call.message.edit_reply_markup(reply_markup=contact_keyboard)
        except Exception as exc:
            print(f"Не удалось убрать кнопки в текущем сообщении: {exc}")

    if order:
        try:
            notice = (
                f"✅ Оплата заказа №{oid} подтверждена! Мы начали собирать ваш заказ."
                if approved else
                f"❗ По чеку заказа №{oid} не удалось подтвердить оплату. "
                "Пожалуйста, свяжитесь с магазином."
            )
            await bot.send_message(order["user_id"], notice)
        except Exception as exc:
            print(f"Не удалось уведомить покупателя заказа №{oid}: {exc}")

    await call.answer("Оплата подтверждена" if approved else "Чек отклонён")


@order_dp.callback_query(F.data.startswith("paid:"))
async def admin_paid(call: CallbackQuery):
    await finish_payment_review(call, approved=True)


@order_dp.callback_query(F.data.startswith("reject:"))
async def admin_reject(call: CallbackQuery):
    await finish_payment_review(call, approved=False)


@dp.callback_query(F.data == "myorders")
async def my_orders(call: CallbackQuery):
    with db() as c:
        orders = c.execute("SELECT id, total, status, created_at FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10",
                           (call.from_user.id,)).fetchall()
    if not orders:
        text = "У вас пока нет заказов."
    else:
        text = "📦 <b>Ваши заказы</b>\n\n" + "\n".join(
            f"№{o['id']} · {money(o['total'])} · {escape(o['status'])} · {o['created_at'][:16]}" for o in orders)
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")]
    ]), parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data == "info")
async def info(call: CallbackQuery):
    await call.message.edit_text(
        f"🚚 Доставка по территории\nМинимальная сумма товаров для заказа — {money(MIN_ORDER_AMOUNT)}.\n"
        "Доставка бесплатная.\n\n"
        "💳 Оплата переводом на реквизиты магазина. После перевода отправьте скриншот чека в чат бота. "
        "Заказ считается оплаченным только после подтверждения администратором.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛍 Каталог", callback_data="catalog")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")]
        ])
    )
    await call.answer()


# Рассылка запускается только администраторами первого бота.
@dp.message(Command("broadcast"))
async def broadcast_command(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id) or message.chat.type != "private":
        return
    await state.set_state(Broadcast.content)
    await message.answer(
        "📢 Пришлите сообщение для рассылки: текст, фотографию или фото с подписью.\n"
        "После этого покажу предварительный просмотр.\n"
        "Для отмены отправьте /cancel."
    )


@dp.callback_query(F.data == "broadcast:new")
async def broadcast_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id) or call.message.chat.type != "private":
        await call.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(Broadcast.content)
    await call.message.answer("📢 Пришлите текст или фото с подписью для рассылки. Для отмены: /cancel")
    await call.answer()


@dp.message(Command("cancel"))
async def broadcast_cancel_command(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    current = await state.get_state()
    if current in (Broadcast.content.state, Broadcast.confirm.state):
        await state.clear()
        await message.answer("Рассылка отменена.")


@dp.message(Broadcast.content)
async def broadcast_content(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id) or message.chat.type != "private":
        return
    if not message.text and not message.photo:
        await message.answer("Можно отправить текст или фотографию с подписью.")
        return
    await state.update_data(source_chat=message.chat.id, source_message=message.message_id)
    await state.set_state(Broadcast.confirm)
    await message.answer("👀 Предпросмотр сообщения:")
    await bot.copy_message(chat_id=message.chat.id, from_chat_id=message.chat.id, message_id=message.message_id)
    with db() as c:
        count = c.execute("SELECT COUNT(*) FROM broadcast_users").fetchone()[0]
    await message.answer(
        f"Получателей в базе: {count}. Отправить это сообщение всем?",
        reply_markup=broadcast_confirmation()
    )


@dp.callback_query(F.data == "broadcast:cancel")
async def broadcast_cancel(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await call.message.edit_text("❌ Рассылка отменена.")
    await call.answer()


@dp.callback_query(F.data == "broadcast:send")
async def broadcast_send(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id) or call.message.chat.type != "private":
        await call.answer("Нет доступа", show_alert=True)
        return
    if await state.get_state() != Broadcast.confirm.state:
        await call.answer("Сначала подготовьте сообщение через /broadcast", show_alert=True)
        return
    if broadcast_lock.locked():
        await call.answer("Другая рассылка уже выполняется. Попробуйте позже.", show_alert=True)
        return
    async with broadcast_lock:
        draft = await state.get_data()
        await state.clear()
        await call.answer("Рассылка началась")
        await call.message.edit_text("📢 Рассылка выполняется…")
        with db() as c:
            recipients = [row[0] for row in c.execute("SELECT user_id FROM broadcast_users").fetchall()]
        sent = failed = 0
        for user_id in recipients:
            for attempt in range(2):
                try:
                    await bot.copy_message(
                        chat_id=user_id,
                        from_chat_id=draft["source_chat"],
                        message_id=draft["source_message"]
                    )
                    sent += 1
                    break
                except TelegramRetryAfter as exc:
                    if attempt == 0:
                        await asyncio.sleep(exc.retry_after + 1)
                        continue
                    failed += 1
                except (TelegramForbiddenError, TelegramBadRequest):
                    failed += 1
                    break
                except Exception as exc:
                    print(f"Ошибка рассылки пользователю {user_id}: {type(exc).__name__}")
                    failed += 1
                    break
            await asyncio.sleep(0.06)
        await call.message.edit_text(
            f"✅ Рассылка завершена.\nУспешно: {sent}\nНе доставлено: {failed}\nВсего: {len(recipients)}"
        )


# Массовое добавление: пробелы — для однословной категории,
# | — для названий и категорий с пробелами.
MAX_BULK_LINES = 70


def parse_bulk_line(line):
    if "|" in line:
        parts = [part.strip() for part in line.split("|")]
    else:
        parts = line.split()
        if len(parts) >= 4:
            parts = [" ".join(parts[:-3]), *parts[-3:]]
    if len(parts) != 4:
        raise ValueError("нужно 4 поля: название, категория, цена, остаток")
    name, category, price_raw, stock_raw = parts
    if not name or not category or len(name) > 150 or len(category) > 100:
        raise ValueError("название/категория пустые или слишком длинные")
    if not re.fullmatch(r"\d+(?:[.,]\d{1,2})?", price_raw):
        raise ValueError("цена должна быть числом, например 500 или 500,50")
    if not re.fullmatch(r"\d+", stock_raw):
        raise ValueError("остаток должен быть целым числом, например 12 (не I2)")
    price, stock = float(price_raw.replace(",", ".")), int(stock_raw)
    if price <= 0 or price > 10000000 or stock > 1000000:
        raise ValueError("цена должна быть больше нуля, остаток — от 0 до 1000000")
    return {"name": name, "category": category, "price": price, "stock": stock}


def bulk_buttons():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Добавить товары", callback_data="bulk:confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="bulk:cancel")]
    ])


async def bulk_begin(message, state):
    await state.set_state(BulkProducts.input)
    await message.answer(
        "➕ Пришлите товары одним сообщением, по одному на строку.\n\n"
        "Пельмени заморозка 500 12\nМолоко молочные 120 25\n\n"
        "Если категория или название из нескольких слов, используйте | :\n"
        "Пельмени домашние | Замороженные продукты | 500 | 12\n\n"
        "Остаток 0 допустим, но такой товар нельзя купить.\n"
        "Для отмены: /cancel"
    )


@dp.message(Command("bulk"))
async def bulk_command(message: Message, state: FSMContext):
    if is_admin(message.from_user.id):
        await bulk_begin(message, state)


@dp.callback_query(F.data == "bulk:new")
async def bulk_callback(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await bulk_begin(call.message, state)
    await call.answer()


@dp.message(BulkProducts.input)
async def bulk_receive(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.text and message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("Добавление отменено.")
        return
    if not message.text:
        await message.answer("Пришлите список текстом.")
        return
    lines = [x.strip() for x in message.text.splitlines() if x.strip()]
    if not lines or len(lines) > MAX_BULK_LINES:
        await message.answer(f"В одном сообщении должно быть от 1 до {MAX_BULK_LINES} строк.")
        return
    products, errors = [], []
    for index, line in enumerate(lines, 1):
        try:
            products.append(parse_bulk_line(line))
        except ValueError as exc:
            errors.append(f"Строка {index}: {exc}")
    if errors:
        await message.answer("❌ Ошибки. Ничего не добавлено:\n" + "\n".join(errors[:25]) +
                             "\nИсправьте список и отправьте его снова.")
        return
    keys = [(x["name"].casefold(), x["category"].casefold()) for x in products]
    repeated = {key for key in keys if keys.count(key) > 1}
    if repeated:
        await message.answer("❌ В списке есть повторяющиеся названия и категории. "
                             "Уберите повторы и отправьте список снова.")
        return
    with db() as c:
        existing = c.execute("SELECT id,name,category,active FROM products").fetchall()
    lookup = {(r["name"].strip().casefold(), r["category"].strip().casefold()): r for r in existing}
    dupes = [x for x in products if (x["name"].casefold(), x["category"].casefold()) in lookup]
    if dupes:
        warnings = []
        for x in dupes[:15]:
            old = lookup[(x["name"].casefold(), x["category"].casefold())]
            warnings.append(f"• {x['name']} / {x['category']} (ID {old['id']})")
        await message.answer(
            "⚠️ Эти товары уже есть в базе:\n" + "\n".join(warnings) +
            "\n\nОни НЕ будут добавлены повторно. "
            "Для изменения остатков используйте /пополнить ID количество."
        )
    fresh = [x for x in products if (x["name"].casefold(), x["category"].casefold()) not in lookup]
    if not fresh:
        await message.answer("Все позиции уже существуют. Новых товаров нет.")
        await state.clear()
        return
    await state.update_data(bulk_products=fresh)
    await state.set_state(BulkProducts.confirm)
    preview = [f"{i}. {x['name']} | {x['category']} | {money(x['price'])} | {x['stock']} шт."
               for i, x in enumerate(fresh, 1)]
    text = (f"📋 Проверка списка\nНовых товаров: {len(fresh)}\n"
            f"Пропущено дубликатов: {len(dupes)}\n\n" + "\n".join(preview))
    if len(text) > 3500:
        text = text[:3400] + "\n…\nПолный список сохранён до подтверждения."
    await message.answer(text, reply_markup=bulk_buttons())


@dp.callback_query(F.data == "bulk:cancel")
async def bulk_cancel(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("Добавление отменено.")
    await call.answer()


@dp.message(Command("cancel"), BulkProducts.input)
@dp.message(Command("cancel"), BulkProducts.confirm)
async def bulk_cancel_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Добавление отменено.")


@dp.callback_query(F.data == "bulk:confirm")
async def bulk_confirm(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    if await state.get_state() != BulkProducts.confirm.state:
        await call.answer("Этот список уже обработан или устарел", show_alert=True)
        return
    data = await state.get_data()
    products = data.get("bulk_products", [])
    if not products:
        await call.answer("Нет товаров для добавления", show_alert=True)
        return
    added, skipped = 0, 0
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        existing = {(r["name"].strip().casefold(), r["category"].strip().casefold())
                    for r in c.execute("SELECT name,category FROM products")}
        for x in products:
            key = (x["name"].casefold(), x["category"].casefold())
            if key in existing:
                skipped += 1
                continue
            c.execute("INSERT INTO products(name,category,price,stock,description,active) "
                      "VALUES(?,?,?,?,?,1)",
                      (x["name"], x["category"], x["price"], x["stock"], ""))
            existing.add(key)
            added += 1
    await state.clear()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(f"✅ Добавлено товаров: {added}\nПропущено дубликатов: {skipped}.")
    await call.answer()


async def send_stock(message):
    with db() as c:
        rows = c.execute("SELECT id,name,category,stock,active FROM products ORDER BY category,name").fetchall()
    if not rows:
        await message.answer("Каталог пока пуст.")
        return
    lines = [f"#{r['id']} {r['name']} ({r['category']}) — {r['stock']} шт." +
             (" [скрыт]" if not r["active"] else "") for r in rows]
    chunk = "📦 Остатки:\n"
    for line in lines:
        if len(chunk) + len(line) > 3500:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await message.answer(chunk)
    await message.answer("Пополнение: /пополнить ID количество\nПример: /пополнить 7 15")


@dp.message(Command("stock"))
async def stock_command(message: Message):
    if is_admin(message.from_user.id):
        await send_stock(message)


@dp.callback_query(F.data == "admin:stock")
async def stock_callback(call: CallbackQuery):
    if is_admin(call.from_user.id):
        await send_stock(call.message)
    await call.answer()


@dp.callback_query(F.data == "admin:catalog")
async def admin_catalog_callback(call: CallbackQuery):
    if is_admin(call.from_user.id):
        await list_products(call.message, verified=True)
    await call.answer()


@dp.message(F.text.regexp(r"^/(?:пополнить|replenish)(?:\s|$)"))
async def replenish(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Формат: /пополнить ID количество\nНапример: /пополнить 7 15")
        return
    pid, qty = int(parts[1]), int(parts[2])
    if qty <= 0 or qty > 1000000:
        await message.answer("Количество должно быть от 1 до 1000000.")
        return
    with db() as c:
        cur = c.execute("UPDATE products SET stock=stock+? WHERE id=?", (qty, pid))
        row = c.execute("SELECT name,stock,active FROM products WHERE id=?", (pid,)).fetchone()
    if not cur.rowcount:
        await message.answer("Товар с таким ID не найден.")
    else:
        await message.answer(f"✅ {row['name']}: добавлено {qty} шт., остаток {row['stock']} шт." +
                             ("\nТовар скрыт: /пополнить не включает его в каталог." if not row["active"] else ""))


# Редактирование и удаление товара по ID.
def product_edit_keyboard(pid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Название", callback_data=f"pe:field:{pid}:name"),
         InlineKeyboardButton(text="🗂 Категория", callback_data=f"pe:field:{pid}:category")],
        [InlineKeyboardButton(text="💰 Цена", callback_data=f"pe:field:{pid}:price"),
         InlineKeyboardButton(text="📦 Остаток", callback_data=f"pe:field:{pid}:stock")],
        [InlineKeyboardButton(text="🗑 Удалить навсегда", callback_data=f"pe:delete:{pid}")],
    ])


def product_summary(row):
    return (f"Товар #{row['id']}\n"
            f"Название: {row['name']}\nКатегория: {row['category']}\n"
            f"Цена: {money(row['price'])}\nОстаток: {row['stock']} шт.\n"
            f"Статус: {'в каталоге' if row['active'] else 'скрыт'}")


async def show_product_editor(message, state, pid):
    with db() as c:
        row = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    await state.clear()
    if row is None:
        await message.answer(f"Товар #{pid} не найден. Проверьте ID через /products.")
        return
    await message.answer(product_summary(row) + "\n\nВыберите поле:",
                         reply_markup=product_edit_keyboard(pid))


async def ask_product_id(message, state):
    await state.clear()
    await state.set_state(EditProduct.product_id)
    await message.answer("Введите числовой ID товара (его можно посмотреть в /products). "
                         "Отмена: /cancel")


@dp.message(Command("editproduct"))
async def edit_product_command(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    args = (message.text or "").split()
    if len(args) == 1:
        await ask_product_id(message, state)
    elif len(args) == 2 and args[1].isdigit():
        await show_product_editor(message, state, int(args[1]))
    else:
        await message.answer("Формат: /editproduct 7")


@dp.callback_query(F.data == "admin:editproduct")
async def edit_product_menu(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await ask_product_id(call.message, state)
    await call.answer()


@dp.message(EditProduct.product_id)
async def edit_product_get_id(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    value = (message.text or "").strip()
    if value == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
    elif value.isdigit():
        await show_product_editor(message, state, int(value))
    else:
        await message.answer("Введите ID цифрами, например 7. Отмена: /cancel")


@dp.callback_query(F.data.startswith("pe:field:"))
async def edit_product_select_field(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    try:
        _, _, pid_raw, field = call.data.split(":")
        pid = int(pid_raw)
    except (ValueError, TypeError):
        await call.answer("Неверная кнопка", show_alert=True)
        return
    if field not in ("name", "category", "price", "stock"):
        await call.answer("Неизвестное поле", show_alert=True)
        return
    with db() as c:
        row = c.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone()
    if row is None:
        await call.answer("Товар уже удалён", show_alert=True)
        return
    await state.clear()
    await state.update_data(edit_id=pid, edit_field=field)
    await state.set_state(EditProduct.details)
    labels = {"name": "название", "category": "категорию",
              "price": "цену в рублях", "stock": "остаток в штуках"}
    await call.message.answer(
        f"Товар #{pid}. Введите новое значение поля «{labels[field]}».\n"
        "Для отмены отправьте /cancel."
    )
    await call.answer()


@dp.message(EditProduct.details)
async def edit_product_details(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    value = (message.text or "").strip()
    if value == "/cancel":
        await state.clear()
        await message.answer("Редактирование отменено.")
        return
    data = await state.get_data()
    field, pid = data["edit_field"], data["edit_id"]
    try:
        if field in ("name", "category"):
            if not value or len(value) > (150 if field == "name" else 100):
                raise ValueError("Пустое или слишком длинное значение")
            new_value = value
        elif field == "price":
            if not re.fullmatch(r"\\d+(?:[.,]\\d{1,2})?", value):
                raise ValueError("Цена должна быть числом, например 550 или 550,50")
            new_value = float(value.replace(",", "."))
            if not 0 < new_value <= 10000000:
                raise ValueError("Цена должна быть больше нуля")
        else:
            if not re.fullmatch(r"\\d+", value):
                raise ValueError("Остаток должен быть целым числом от 0")
            new_value = int(value)
            if new_value > 1000000:
                raise ValueError("Слишком большой остаток")
    except ValueError as exc:
        await message.answer(f"Ошибка: {exc}. Повторите ввод или /cancel.")
        return
    with db() as c:
        row = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if row is None:
            await state.clear()
            await message.answer("Товар уже удалён.")
            return
        new_name = new_value if field == "name" else row["name"]
        new_category = new_value if field == "category" else row["category"]
        duplicate = c.execute(
            "SELECT id FROM products WHERE id<>? AND name=? COLLATE NOCASE "
            "AND category=? COLLATE NOCASE LIMIT 1",
            (pid, new_name, new_category)
        ).fetchone()
    if duplicate:
        await message.answer(
            f"⚠️ Такое название и категория уже у товара #{duplicate['id']}. "
            "Изменение не принято. Введите другое значение."
        )
        return
    await state.update_data(edit_value=new_value)
    await state.set_state(EditProduct.confirm)
    await message.answer(
        f"Товар #{pid}\nПоле: {field}\nБыло: {row[field]}\nСтанет: {new_value}\n"
        "Подтвердить изменение?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Сохранить", callback_data="pe:save"),
             InlineKeyboardButton(text="❌ Отмена", callback_data="pe:cancel")]
        ])
    )


@dp.callback_query(F.data == "pe:save")
async def edit_product_save(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    if await state.get_state() != EditProduct.confirm.state:
        await call.answer("Подтверждение устарело", show_alert=True)
        return
    data = await state.get_data()
    pid, field, value = data["edit_id"], data["edit_field"], data["edit_value"]
    with db() as c:
        row = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if row is None:
            await state.clear()
            await call.answer("Товар удалён", show_alert=True)
            return
        name = value if field == "name" else row["name"]
        category = value if field == "category" else row["category"]
        duplicate = c.execute(
            "SELECT id FROM products WHERE id<>? AND name=? COLLATE NOCASE "
            "AND category=? COLLATE NOCASE LIMIT 1",
            (pid, name, category)
        ).fetchone()
        if duplicate:
            await call.answer(f"Дубликат товара #{duplicate['id']}", show_alert=True)
            return
        c.execute(f"UPDATE products SET {field}=? WHERE id=?", (value, pid))
    await state.clear()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(f"✅ Товар #{pid} обновлён.")
    await show_product_editor(call.message, state, pid)
    await call.answer()


@dp.callback_query(F.data == "pe:cancel")
async def edit_product_cancel(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Отменено")


@dp.message(Command("deleteproduct"))
async def delete_product_forever_command(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    args = (message.text or "").split()
    if len(args) != 2 or not args[1].isdigit():
        await message.answer("Формат: /deleteproduct 7")
        return
    await request_delete_product(message, state, int(args[1]))


async def request_delete_product(message, state, pid):
    with db() as c:
        row = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if row is None:
        await message.answer(f"Товар #{pid} не найден.")
        return
    await state.clear()
    await state.update_data(delete_id=pid)
    await state.set_state(EditProduct.confirm)
    await message.answer(
        "⚠️ ПОЛНОЕ УДАЛЕНИЕ из базы. Восстановить товар можно только из резервной копии.\n\n"
        + product_summary(row) + "\n\nУдалить навсегда?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Да, удалить навсегда", callback_data="pe:deleteconfirm")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="pe:cancel")]
        ])
    )


@dp.callback_query(F.data.startswith("pe:delete:"))
async def delete_product_button(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    pid = int(call.data.rsplit(":", 1)[1])
    await request_delete_product(call.message, state, pid)
    await call.answer()


@dp.callback_query(F.data == "pe:deleteconfirm")
async def delete_product_confirm(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    data = await state.get_data()
    if await state.get_state() != EditProduct.confirm.state or "delete_id" not in data:
        await call.answer("Подтверждение устарело", show_alert=True)
        return
    pid = data["delete_id"]
    with db() as c:
        result = c.execute("DELETE FROM products WHERE id=?", (pid,))
    await state.clear()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(
        f"🗑 Товар #{pid} полностью удалён из базы." if result.rowcount
        else f"Товар #{pid} уже удалён."
    )
    await call.answer()


@dp.callback_query(F.data == "admin:deleteproduct")
async def admin_delete_product_menu(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await call.message.answer("Чтобы полностью удалить товар, отправьте /deleteproduct ID\n"
                              "Например: /deleteproduct 7. Перед удалением будет подтверждение.")
    await call.answer()


@dp.message(Command("users"))
async def admin_user_count(message: Message):
    if not is_admin(message.from_user.id):
        return
    with db() as c:
        count = c.execute("SELECT COUNT(*) FROM user_access").fetchone()[0]
    await message.answer(
        f"👥 Известных пользователей первого бота: {count}\n"
        "Учитываются одобренные, ожидающие и отклонённые. Это не число активных пользователей."
    )


@dp.callback_query(F.data == "admin:users")
async def admin_user_count_button(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    with db() as c:
        count = c.execute("SELECT COUNT(*) FROM user_access").fetchone()[0]
    await call.message.answer(f"👥 Известных пользователей: {count}\n"
                              "Включены одобренные, ожидающие и отклонённые пользователи.")
    await call.answer()


# Управление доступом из основного админ-бота.
def access_admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏳ Ожидают одобрения", callback_data="access:list:pending")],
        [InlineKeyboardButton(text="⛔ Отклонённые", callback_data="access:list:rejected")],
        [InlineKeyboardButton(text="✅ Одобренные", callback_data="access:list:approved")],
        [InlineKeyboardButton(text="🔎 Найти пользователя по ID", callback_data="access:lookup")],
    ])


async def show_access_menu(message):
    with db() as c:
        stats = dict(c.execute("SELECT status,COUNT(*) FROM user_access GROUP BY status").fetchall())
    await message.answer(
        "🔐 Управление доступом Maskset\n"
        f"⏳ Ожидают: {stats.get('pending', 0)}\n"
        f"⛔ Отклонены: {stats.get('rejected', 0)}\n"
        f"✅ Одобрены: {stats.get('approved', 0)}\n\n"
        "Выберите список или поиск по Telegram ID:", reply_markup=access_admin_menu()
    )


@dp.message(Command("access"))
async def access_command(message: Message):
    if is_admin(message.from_user.id):
        await show_access_menu(message)


@dp.callback_query(F.data == "access:menu")
async def access_menu_callback(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await show_access_menu(call.message)
    await call.answer()


@dp.callback_query(F.data.startswith("access:list:"))
async def access_list_callback(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    status = call.data.rsplit(":", 1)[1]
    if status not in ("approved", "pending", "rejected"):
        await call.answer("Неверный статус", show_alert=True)
        return
    with db() as c:
        rows = c.execute("SELECT * FROM user_access WHERE status=? ORDER BY requested_at DESC LIMIT 20",
                         (status,)).fetchall()
    if not rows:
        await call.message.answer("Пользователей в этой категории нет.")
    else:
        await call.message.answer(f"Показаны последние {len(rows)} пользователей (максимум 20).")
        for row in rows:
            await call.message.answer(
                access_user_text(row["user_id"], row["full_name"], row["username"], row["status"]),
                reply_markup=access_keyboard(row["user_id"], row["status"])
            )
    await call.answer()


@dp.callback_query(F.data == "access:lookup")
async def access_lookup_callback(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await state.set_state(AccessManage.user_id)
    await call.message.answer("Отправьте Telegram ID пользователя цифрами. Отмена: /cancel")
    await call.answer()


@dp.message(AccessManage.user_id)
async def access_lookup_id(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    value = (message.text or "").strip()
    if value == "/cancel":
        await state.clear()
        await message.answer("Поиск отменён.")
        return
    if not value.isdigit():
        await message.answer("Введите Telegram ID цифрами. Отмена: /cancel")
        return
    with db() as c:
        row = c.execute("SELECT * FROM user_access WHERE user_id=?", (int(value),)).fetchone()
    await state.clear()
    if not row:
        await message.answer("Пользователь не найден. Он должен хотя бы один раз написать боту.")
    else:
        await message.answer(
            access_user_text(row["user_id"], row["full_name"], row["username"], row["status"]),
            reply_markup=access_keyboard(row["user_id"], row["status"])
        )


async def process_access_decision(call: CallbackQuery, new_status: str):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    try:
        uid = int(call.data.rsplit(":", 1)[1])
    except ValueError:
        await call.answer("Неверный ID", show_alert=True)
        return
    if uid in ADMIN_IDS:
        await call.answer("Администраторов блокировать нельзя", show_alert=True)
        return
    with db() as c:
        old = c.execute("SELECT status FROM user_access WHERE user_id=?", (uid,)).fetchone()
        if old is None:
            await call.answer("Пользователь не найден", show_alert=True)
            return
        changed = c.execute(
            "UPDATE user_access SET status=?,reviewed_at=?,reviewed_by=? "
            "WHERE user_id=? AND status<>?",
            (new_status, datetime.now().isoformat(timespec="seconds"), call.from_user.id,
             uid, new_status)
        ).rowcount
        copies = c.execute("SELECT admin_id,message_id FROM access_admin_messages WHERE user_id=?",
                           (uid,)).fetchall()
    if not changed:
        await call.answer("Этот статус уже установлен", show_alert=True)
        return
    # Убираем старые кнопки у обоих администраторов.
    for copy in copies:
        try:
            await order_bot.edit_message_reply_markup(
                chat_id=copy["admin_id"], message_id=copy["message_id"], reply_markup=None)
        except Exception as exc:
            print(f"Не удалось обновить заявку {uid}: {exc}")
    # Если решение принято в главном боте, обновляем его сообщение тоже.
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except (TelegramBadRequest, AttributeError):
        pass
    if new_status == "rejected":
        carts.pop(uid, None)
    try:
        await bot.send_message(
            uid, "✅ Администратор одобрил вашу заявку! Откройте /start для входа в Maskset."
            if new_status == "approved" else
            "⛔ Администратор закрыл вам доступ к Maskset. Повторная заявка недоступна."
        )
    except Exception as exc:
        print(f"Не удалось уведомить пользователя {uid}: {exc}")
    await call.answer("Доступ разрешён" if new_status == "approved" else "Доступ закрыт")
    try:
        await call.message.answer(f"Пользователь {uid}: " +
                                  ("✅ доступ разрешён" if new_status == "approved" else "⛔ доступ закрыт"))
    except Exception:
        pass


@dp.callback_query(F.data.startswith("access:approve:"))
async def access_approve_main(call: CallbackQuery):
    await process_access_decision(call, "approved")


@dp.callback_query(F.data.startswith("access:reject:"))
async def access_reject_main(call: CallbackQuery):
    await process_access_decision(call, "rejected")


@order_dp.callback_query(F.data.startswith("access:approve:"))
async def access_approve_orders(call: CallbackQuery):
    await process_access_decision(call, "approved")


@order_dp.callback_query(F.data.startswith("access:reject:"))
async def access_reject_orders(call: CallbackQuery):
    await process_access_decision(call, "rejected")


# Админ-команды. Формат добавления: /addproduct Название|Категория|Цена|Остаток|Описание
@dp.message(Command("admin"))
async def admin_help(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "🛠 Команды администратора:\n"
        "/addproduct Название|Категория|Цена|Остаток|Описание\n"
        "/setprice ID Цена\n"
        "/setstock ID Количество\n"
        "/delproduct ID — скрыть товар\n"
        "/products — список товаров\n"
        "/orders — последние заказы\n"
        "/status ID Новый статус\n"
        "/broadcast — рассылка покупателям\n"
        "/bulk — добавить товары списком\n"
        "/пополнить ID количество — увеличить остаток\n"
        "/stock — остатки\n"
        "/access — управление доступом покупателей\n\n"
        "Пример: /addproduct Молоко 1л|Молочные продукты|99|20|Пастеризованное",
        reply_markup=broadcast_keyboard()
    )


@dp.message(Command("addproduct"))
async def add_product(message: Message):
    if not is_admin(message.from_user.id):
        return
    raw = message.text.partition(" ")[2]
    parts = [x.strip() for x in raw.split("|")]
    if len(parts) < 4:
        await message.answer("Формат: /addproduct Название|Категория|Цена|Остаток|Описание")
        return
    try:
        name, category = parts[0], parts[1]
        price, stock = float(parts[2].replace(",", ".")), int(parts[3])
        description = parts[4] if len(parts) > 4 else ""
        if price < 0 or stock < 0:
            raise ValueError()
        with db() as c:
            cur = c.execute("INSERT INTO products(name,category,price,stock,description) VALUES(?,?,?,?,?)",
                            (name, category, price, stock, description))
        await message.answer(f"Добавлен товар ID {cur.lastrowid}: {name}")
    except ValueError:
        await message.answer("Цена должна быть числом, остаток — целым неотрицательным числом.")


@dp.message(Command("setprice"))
async def set_price(message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        _, pid, price = message.text.split(maxsplit=2)
        with db() as c:
            c.execute("UPDATE products SET price=? WHERE id=?", (float(price.replace(",", ".")), int(pid)))
        await message.answer("Цена обновлена.")
    except Exception:
        await message.answer("Формат: /setprice ID Цена")


@dp.message(Command("setstock"))
async def set_stock(message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        _, pid, stock = message.text.split(maxsplit=2)
        with db() as c:
            c.execute("UPDATE products SET stock=? WHERE id=?", (max(0, int(stock)), int(pid)))
        await message.answer("Остаток обновлён.")
    except Exception:
        await message.answer("Формат: /setstock ID Количество")


@dp.message(Command("delproduct"))
async def delete_product(message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        pid = int(message.text.split()[1])
        with db() as c:
            c.execute("UPDATE products SET active=0 WHERE id=?", (pid,))
        await message.answer("Товар скрыт из каталога.")
    except Exception:
        await message.answer("Формат: /delproduct ID")


@dp.message(Command("products"))
async def list_products(message: Message, *, verified=False):
    if not verified and not is_admin(message.from_user.id):
        return
    with db() as c:
        rows = c.execute("SELECT id,name,category,price,stock,active FROM products ORDER BY id DESC LIMIT 100").fetchall()
    if not rows:
        await message.answer("Товаров пока нет.")
        return
    lines = [f"#{p['id']} {p['name']} | {p['category']} | {money(p['price'])} | остаток {p['stock']} | {'активен' if p['active'] else 'скрыт'}" for p in rows]
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) > 3500:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await message.answer(chunk)


@dp.message(Command("orders"))
async def list_orders(message: Message):
    if not is_admin(message.from_user.id):
        return
    with db() as c:
        rows = c.execute("SELECT id,customer_name,phone,house,total,status,created_at FROM orders ORDER BY id DESC LIMIT 30").fetchall()
    if not rows:
        await message.answer("Заказов пока нет.")
        return
    for o in rows:
        await message.answer(
            f"Заказ №{o['id']} · {money(o['total'])}\n{o['customer_name']}\n"
            f"Домик: {o['house']}\nСтатус: {o['status']}\n{o['created_at'][:16]}")


@dp.message(Command("status"))
async def set_order_status(message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        _, oid, *status_parts = message.text.split()
        status = " ".join(status_parts)
        if not status:
            raise ValueError()
        with db() as c:
            order = c.execute("SELECT user_id FROM orders WHERE id=?", (int(oid),)).fetchone()
            c.execute("UPDATE orders SET status=? WHERE id=?", (status, int(oid)))
        if order:
            await bot.send_message(order["user_id"], f"📦 Статус заказа №{oid}: {status}")
        await message.answer("Статус обновлён.")
    except Exception:
        await message.answer("Формат: /status ID Новый статус")


@order_dp.message(CommandStart())
async def order_bot_start(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Этот бот предназначен только для администраторов Maskset.")
        return
    await message.answer("Бот заказов Maskset подключён. Сюда будут приходить заказы со скриншотами оплаты.")


async def main():
    init_db()
    await asyncio.gather(dp.start_polling(bot), order_dp.start_polling(order_bot))


if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import os
import sqlite3
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
        c.execute("""CREATE TABLE IF NOT EXISTS broadcast_users (
            user_id INTEGER PRIMARY KEY,
            first_seen TEXT NOT NULL
        )""")
        # Клиенты с ранее оформленными заказами тоже смогут получать рассылку.
        c.execute("""INSERT OR IGNORE INTO broadcast_users (user_id, first_seen)
                     SELECT DISTINCT user_id, ? FROM orders""", (datetime.now().isoformat(timespec="seconds"),))


class Broadcast(StatesGroup):
    content = State()
    confirm = State()


class Checkout(StatesGroup):
    name = State()
    phone = State()
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


dp.message.middleware(TrackCustomerMiddleware())
dp.callback_query.middleware(TrackCustomerMiddleware())
broadcast_lock = asyncio.Lock()


def broadcast_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
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
    await state.set_state(Checkout.phone)
    await message.answer("Укажите номер телефона для связи:")


@dp.message(Checkout.phone)
async def checkout_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text.strip()[:40])
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
    with db() as c:
        cur = c.execute("""INSERT INTO orders
            (user_id, username, customer_name, phone, house, comment, items, subtotal, delivery, total, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (message.from_user.id, message.from_user.username or "", data["customer_name"], data["phone"],
             data["house"], data.get("comment", ""), "\n".join(item_lines), subtotal, delivery, total,
             "Ожидает оплату", datetime.now().isoformat(timespec="minutes")))
        order_id = cur.lastrowid
        for p, qty, _ in items:
            c.execute("UPDATE products SET stock=stock-? WHERE id=? AND stock>=?", (qty, p["id"], qty))
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
                   f"Телефон: {order['phone']}\n"
                   f"Домик: {order['house']}\n"
                   f"Сумма: {money(order['total'])}\n"
                   f"Статус: {order['status']}\n\n"
                   f"Состав заказа:\n{order['items']}")
        for admin_id in ADMIN_IDS:
            try:
                photo_bytes = BytesIO()
                await bot.download(file_id, destination=photo_bytes)

                await order_bot.send_photo(
                    chat_id=admin_id,
                    photo=BufferedInputFile(
                        photo_bytes.getvalue(),
                        filename="receipt.jpg"
                    ),
                    caption=caption,
                    reply_markup=kb
                )
            except Exception as exc:
                print(f"Ошибка отправки заказа: {exc}")
    await state.clear()
    await message.answer(f"Чек по заказу №{order_id} получен. Ожидайте подтверждения оплаты.", reply_markup=main_keyboard())


@dp.message(Checkout.receipt)
async def receipt_not_photo(message: Message):
    await message.answer("Пожалуйста, отправьте скриншот чека именно как фотографию.")


@order_dp.callback_query(F.data.startswith("paid:"))
async def admin_paid(call: CallbackQuery):
    oid = int(call.data.split(":")[1])
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    with db() as c:
        order = c.execute("SELECT user_id FROM orders WHERE id=?", (oid,)).fetchone()
        c.execute("UPDATE orders SET status='Оплачен — собирается' WHERE id=?", (oid,))
    if order:
        await bot.send_message(order["user_id"], f"✅ Оплата заказа №{oid} подтверждена! Мы начали собирать ваш заказ.")
    await call.message.edit_caption((call.message.caption or "") + "\n\n✅ Оплата подтверждена")
    await call.answer("Готово")


@order_dp.callback_query(F.data.startswith("reject:"))
async def admin_reject(call: CallbackQuery):
    oid = int(call.data.split(":")[1])
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    with db() as c:
        order = c.execute("SELECT user_id FROM orders WHERE id=?", (oid,)).fetchone()
        c.execute("UPDATE orders SET status='Чек отклонён — требуется связь' WHERE id=?", (oid,))
    if order:
        await bot.send_message(order["user_id"], f"❗ По чеку заказа №{oid} не удалось подтвердить оплату. Пожалуйста, свяжитесь с магазином или отправьте корректный чек.")
    await call.message.edit_caption((call.message.caption or "") + "\n\n❌ Чек отклонён")
    await call.answer("Чек отклонён")


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
        "/broadcast — рассылка покупателям\n\n"
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
async def list_products(message: Message):
    if not is_admin(message.from_user.id):
        return
    with db() as c:
        rows = c.execute("SELECT id,name,category,price,stock,active FROM products ORDER BY id DESC LIMIT 100").fetchall()
    if not rows:
        await message.answer("Товаров пока нет.")
        return
    await message.answer("\n".join(
        f"#{p['id']} {p['name']} | {p['category']} | {money(p['price'])} | остаток {p['stock']} | {'активен' if p['active'] else 'скрыт'}"
        for p in rows))


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
            f"Заказ №{o['id']} · {money(o['total'])}\n{o['customer_name']} · {o['phone']}\n"
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

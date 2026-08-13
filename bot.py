import asyncio
import logging
import os
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    ContentType,
)
from dotenv import load_dotenv

# ---------- НАСТРОЙКИ ----------

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DRIVERS_GROUP_ID = int(os.getenv("DRIVERS_GROUP_ID", "0"))
DB_PATH = os.path.join(os.path.dirname(__file__), "taxi.db")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ---------- БАЗА ДАННЫХ ----------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            client_name TEXT,
            pickup TEXT,
            destination TEXT,
            phone TEXT,
            status TEXT DEFAULT 'new',       -- new / taken / cancelled / done
            driver_id INTEGER,
            driver_name TEXT,
            group_message_id INTEGER,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def create_order(client_id, client_name, pickup, destination, phone):
    conn = db()
    cur = conn.execute(
        """INSERT INTO orders (client_id, client_name, pickup, destination, phone, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (client_id, client_name, pickup, destination, phone, datetime.now().isoformat()),
    )
    conn.commit()
    order_id = cur.lastrowid
    conn.close()
    return order_id


def get_order(order_id):
    conn = db()
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    return row


def take_order_atomic(order_id, driver_id, driver_name):
    """Атомарно назначает заказ водителю, только если он ещё свободен.
    Возвращает True, если именно этот водитель успел забрать заказ."""
    conn = db()
    cur = conn.execute(
        "UPDATE orders SET status='taken', driver_id=?, driver_name=? WHERE id=? AND status='new'",
        (driver_id, driver_name, order_id),
    )
    conn.commit()
    success = cur.rowcount == 1
    conn.close()
    return success


def set_group_message_id(order_id, message_id):
    conn = db()
    conn.execute("UPDATE orders SET group_message_id=? WHERE id=?", (message_id, order_id))
    conn.commit()
    conn.close()


def cancel_order(order_id, client_id):
    conn = db()
    cur = conn.execute(
        "UPDATE orders SET status='cancelled' WHERE id=? AND client_id=? AND status='new'",
        (order_id, client_id),
    )
    conn.commit()
    success = cur.rowcount == 1
    conn.close()
    return success


# ---------- FSM: СОСТОЯНИЯ ОФОРМЛЕНИЯ ЗАКАЗА ----------

class OrderForm(StatesGroup):
    pickup = State()
    destination = State()
    phone = State()
    confirm = State()


# ---------- КЛАВИАТУРЫ ----------

def main_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🚕 Заказать такси")]],
        resize_keyboard=True,
    )


def phone_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер телефона", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def confirm_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✅ Подтвердить"), KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def take_order_kb(order_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🚖 Взять заказ", callback_data=f"take:{order_id}")]]
    )


# ---------- КЛИЕНТСКИЙ FLOW ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Салем! Это бот заказа такси в Акшукуре.\n\n"
        "Нажми «🚕 Заказать такси», чтобы оформить заявку.",
        reply_markup=main_menu_kb(),
    )


@router.message(F.text == "🚕 Заказать такси")
async def start_order(message: Message, state: FSMContext):
    await state.set_state(OrderForm.pickup)
    await message.answer("Откуда забрать? (укажи адрес или ориентир)", reply_markup=ReplyKeyboardRemove())


@router.message(OrderForm.pickup)
async def get_pickup(message: Message, state: FSMContext):
    await state.update_data(pickup=message.text)
    await state.set_state(OrderForm.destination)
    await message.answer("Куда едем?")


@router.message(OrderForm.destination)
async def get_destination(message: Message, state: FSMContext):
    await state.update_data(destination=message.text)
    await state.set_state(OrderForm.phone)
    await message.answer(
        "Оставь номер телефона для связи (можно кнопкой ниже или написать вручную):",
        reply_markup=phone_kb(),
    )


@router.message(OrderForm.phone, F.content_type == ContentType.CONTACT)
async def get_phone_contact(message: Message, state: FSMContext):
    await state.update_data(phone=message.contact.phone_number)
    await ask_confirm(message, state)


@router.message(OrderForm.phone, F.text)
async def get_phone_text(message: Message, state: FSMContext):
    await state.update_data(phone=message.text)
    await ask_confirm(message, state)


async def ask_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.set_state(OrderForm.confirm)
    await message.answer(
        f"Проверь заказ:\n\n"
        f"📍 Откуда: {data['pickup']}\n"
        f"🏁 Куда: {data['destination']}\n"
        f"📞 Телефон: {data['phone']}\n\n"
        f"Всё верно?",
        reply_markup=confirm_kb(),
    )


@router.message(OrderForm.confirm, F.text == "✅ Подтвердить")
async def confirm_order(message: Message, state: FSMContext):
    data = await state.get_data()
    client_name = message.from_user.full_name
    order_id = create_order(
        client_id=message.from_user.id,
        client_name=client_name,
        pickup=data["pickup"],
        destination=data["destination"],
        phone=data["phone"],
    )
    await state.clear()
    await message.answer(
        f"Заказ №{order_id} создан ✅\nИщем свободного водителя, жди ответа.",
        reply_markup=main_menu_kb(),
    )

    group_text = (
        f"🚖 <b>Новый заказ №{order_id}</b>\n\n"
        f"📍 Откуда: {data['pickup']}\n"
        f"🏁 Куда: {data['destination']}\n"
        f"📞 Телефон: {data['phone']}\n"
        f"👤 Клиент: {client_name}"
    )
    sent = await bot.send_message(DRIVERS_GROUP_ID, group_text, reply_markup=take_order_kb(order_id))
    set_group_message_id(order_id, sent.message_id)


@router.message(OrderForm.confirm, F.text == "❌ Отмена")
async def cancel_order_form(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Заказ отменён.", reply_markup=main_menu_kb())


# ---------- ВОДИТЕЛЬСКИЙ FLOW (группа) ----------

@router.callback_query(F.data.startswith("take:"))
async def take_order_cb(callback: CallbackQuery):
    order_id = int(callback.data.split(":")[1])
    driver = callback.from_user
    driver_name = driver.full_name

    success = take_order_atomic(order_id, driver.id, driver_name)

    if not success:
        await callback.answer("Этот заказ уже забрал другой водитель.", show_alert=True)
        return

    order = get_order(order_id)

    # обновляем сообщение в группе — убираем кнопку, показываем кто взял
    new_text = (
        f"🚖 <b>Заказ №{order_id}</b> — забрал {driver_name}\n\n"
        f"📍 Откуда: {order['pickup']}\n"
        f"🏁 Куда: {order['destination']}\n"
        f"📞 Телефон: {order['phone']}\n"
        f"👤 Клиент: {order['client_name']}"
    )
    await callback.message.edit_text(new_text)
    await callback.answer("Заказ за тобой! Контакты клиента выше.")

    # уведомляем клиента
    try:
        await bot.send_message(
            order["client_id"],
            f"Твой заказ №{order_id} принял водитель {driver_name} 🚖\n"
            f"Он скоро свяжется с тобой по номеру {order['phone']}.",
        )
    except Exception as e:
        logging.warning(f"Не удалось уведомить клиента {order['client_id']}: {e}")


# ---------- СЛУЖЕБНЫЕ КОМАНДЫ ----------

@router.message(Command("myid"))
async def my_id(message: Message):
    # полезно для получения chat_id группы водителей при настройке
    await message.answer(f"chat_id: <code>{message.chat.id}</code>")


async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

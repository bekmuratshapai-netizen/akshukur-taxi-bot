import asyncio
import logging
import os
import sqlite3
from datetime import datetime, date

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
# Чат/группа модератора, куда падают анкеты водителей на проверку.
# Если не задан отдельно — используется тот же чат, что и DRIVERS_GROUP_ID.
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", str(DRIVERS_GROUP_ID)))
MIN_DRIVER_AGE = int(os.getenv("MIN_DRIVER_AGE", "21"))
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS drivers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL UNIQUE,
            full_name TEXT,
            birth_year INTEGER,
            phone TEXT,
            car_number TEXT,
            license_photo_id TEXT,       -- фото водительского удостоверения
            vehicle_photo_id TEXT,       -- фото техпаспорта / СТС
            status TEXT DEFAULT 'pending',   -- pending / approved / rejected
            admin_message_id INTEGER,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def create_driver_application(telegram_id, full_name, birth_year, phone, car_number,
                               license_photo_id, vehicle_photo_id):
    conn = db()
    cur = conn.execute(
        """INSERT INTO drivers
           (telegram_id, full_name, birth_year, phone, car_number,
            license_photo_id, vehicle_photo_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(telegram_id) DO UPDATE SET
               full_name=excluded.full_name,
               birth_year=excluded.birth_year,
               phone=excluded.phone,
               car_number=excluded.car_number,
               license_photo_id=excluded.license_photo_id,
               vehicle_photo_id=excluded.vehicle_photo_id,
               status='pending',
               created_at=excluded.created_at
        """,
        (telegram_id, full_name, birth_year, phone, car_number,
         license_photo_id, vehicle_photo_id, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return get_driver_by_telegram_id(telegram_id)


def get_driver_by_telegram_id(telegram_id):
    conn = db()
    row = conn.execute("SELECT * FROM drivers WHERE telegram_id = ?", (telegram_id,)).fetchone()
    conn.close()
    return row


def get_driver(driver_row_id):
    conn = db()
    row = conn.execute("SELECT * FROM drivers WHERE id = ?", (driver_row_id,)).fetchone()
    conn.close()
    return row


def set_driver_admin_message(driver_row_id, message_id):
    conn = db()
    conn.execute("UPDATE drivers SET admin_message_id=? WHERE id=?", (message_id, driver_row_id))
    conn.commit()
    conn.close()


def set_driver_status(driver_row_id, status):
    conn = db()
    conn.execute("UPDATE drivers SET status=? WHERE id=?", (status, driver_row_id))
    conn.commit()
    conn.close()


def is_driver_approved(telegram_id) -> bool:
    row = get_driver_by_telegram_id(telegram_id)
    return bool(row) and row["status"] == "approved"


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


class DriverForm(StatesGroup):
    full_name = State()
    birth_year = State()
    phone = State()
    car_number = State()
    license_photo = State()
    vehicle_photo = State()
    confirm = State()


# ---------- КЛАВИАТУРЫ ----------

def main_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚕 Заказать такси")],
            [KeyboardButton(text="🚗 Стать водителем")],
        ],
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


def driver_confirm_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✅ Отправить на проверку"), KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def driver_moderation_kb(driver_row_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"drv_ok:{driver_row_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"drv_no:{driver_row_id}"),
        ]]
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


# ---------- РЕГИСТРАЦИЯ ВОДИТЕЛЯ (анкета + модерация) ----------

@router.message(F.text == "🚗 Стать водителем")
async def start_driver_registration(message: Message, state: FSMContext):
    existing = get_driver_by_telegram_id(message.from_user.id)
    if existing and existing["status"] == "approved":
        await message.answer("Ты уже одобренный водитель ✅ Можешь принимать заказы в группе.")
        return
    if existing and existing["status"] == "pending":
        await message.answer("Твоя анкета уже на проверке у модератора, жди решения.")
        return

    await state.set_state(DriverForm.full_name)
    await message.answer(
        "Регистрация водителя.\n\nВведи своё ФИО полностью:",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(DriverForm.full_name)
async def driver_full_name(message: Message, state: FSMContext):
    await state.update_data(full_name=message.text)
    await state.set_state(DriverForm.birth_year)
    await message.answer("Укажи год своего рождения (например, 1990):")


@router.message(DriverForm.birth_year)
async def driver_birth_year(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("Год рождения должен быть числом, например 1990. Попробуй ещё раз:")
        return

    birth_year = int(text)
    current_year = date.today().year
    age = current_year - birth_year

    if age < MIN_DRIVER_AGE or age > 80:
        await message.answer(
            f"К сожалению, регистрация доступна с {MIN_DRIVER_AGE} лет. "
            f"По указанному году рождения тебе {age}. Если ошибся — введи год рождения ещё раз:"
        )
        return

    await state.update_data(birth_year=birth_year)
    await state.set_state(DriverForm.phone)
    await message.answer(
        "Укажи номер телефона для связи (кнопкой или вручную):",
        reply_markup=phone_kb(),
    )


@router.message(DriverForm.phone, F.content_type == ContentType.CONTACT)
async def driver_phone_contact(message: Message, state: FSMContext):
    await state.update_data(phone=message.contact.phone_number)
    await state.set_state(DriverForm.car_number)
    await message.answer("Укажи гос. номер машины (например, 123ABC02):", reply_markup=ReplyKeyboardRemove())


@router.message(DriverForm.phone, F.text)
async def driver_phone_text(message: Message, state: FSMContext):
    await state.update_data(phone=message.text)
    await state.set_state(DriverForm.car_number)
    await message.answer("Укажи гос. номер машины (например, 123ABC02):", reply_markup=ReplyKeyboardRemove())


@router.message(DriverForm.car_number)
async def driver_car_number(message: Message, state: FSMContext):
    await state.update_data(car_number=message.text)
    await state.set_state(DriverForm.license_photo)
    await message.answer("Пришли фото водительского удостоверения (лицевую сторону):")


@router.message(DriverForm.license_photo, F.content_type == ContentType.PHOTO)
async def driver_license_photo(message: Message, state: FSMContext):
    file_id = message.photo[-1].file_id
    await state.update_data(license_photo_id=file_id)
    await state.set_state(DriverForm.vehicle_photo)
    await message.answer("Теперь пришли фото техпаспорта (свидетельства о регистрации) автомобиля:")


@router.message(DriverForm.license_photo)
async def driver_license_photo_wrong(message: Message):
    await message.answer("Нужно именно фото. Пришли фото водительского удостоверения:")


@router.message(DriverForm.vehicle_photo, F.content_type == ContentType.PHOTO)
async def driver_vehicle_photo(message: Message, state: FSMContext):
    file_id = message.photo[-1].file_id
    await state.update_data(vehicle_photo_id=file_id)
    data = await state.get_data()
    await state.set_state(DriverForm.confirm)
    await message.answer(
        f"Проверь анкету:\n\n"
        f"👤 ФИО: {data['full_name']}\n"
        f"🎂 Год рождения: {data['birth_year']}\n"
        f"📞 Телефон: {data['phone']}\n"
        f"🚘 Номер машины: {data['car_number']}\n"
        f"📄 Фото ВУ и техпаспорта — приложены\n\n"
        f"Отправить на проверку модератору?",
        reply_markup=driver_confirm_kb(),
    )


@router.message(DriverForm.vehicle_photo)
async def driver_vehicle_photo_wrong(message: Message):
    await message.answer("Нужно именно фото. Пришли фото техпаспорта:")


@router.message(DriverForm.confirm, F.text == "✅ Отправить на проверку")
async def driver_confirm_send(message: Message, state: FSMContext):
    data = await state.get_data()
    driver_row = create_driver_application(
        telegram_id=message.from_user.id,
        full_name=data["full_name"],
        birth_year=data["birth_year"],
        phone=data["phone"],
        car_number=data["car_number"],
        license_photo_id=data["license_photo_id"],
        vehicle_photo_id=data["vehicle_photo_id"],
    )
    await state.clear()
    await message.answer(
        "Анкета отправлена на проверку ✅\nКак только модератор одобрит — сможешь принимать заказы.",
        reply_markup=main_menu_kb(),
    )

    age = date.today().year - data["birth_year"]
    caption = (
        f"🆕 <b>Анкета водителя</b> (id {driver_row['id']})\n\n"
        f"👤 ФИО: {data['full_name']}\n"
        f"🎂 Возраст: {age}\n"
        f"📞 Телефон: {data['phone']}\n"
        f"🚘 Номер машины: {data['car_number']}\n"
        f"🔗 Telegram: {message.from_user.mention_html()}"
    )

    # отправляем два фото и сообщение с кнопками модерации
    await bot.send_photo(ADMIN_CHAT_ID, data["license_photo_id"], caption="📄 Водительское удостоверение")
    sent = await bot.send_photo(
        ADMIN_CHAT_ID,
        data["vehicle_photo_id"],
        caption=caption,
        reply_markup=driver_moderation_kb(driver_row["id"]),
    )
    set_driver_admin_message(driver_row["id"], sent.message_id)


@router.message(DriverForm.confirm, F.text == "❌ Отмена")
async def driver_confirm_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Регистрация отменена.", reply_markup=main_menu_kb())


@router.callback_query(F.data.startswith("drv_ok:"))
async def driver_approve_cb(callback: CallbackQuery):
    driver_row_id = int(callback.data.split(":")[1])
    driver_row = get_driver(driver_row_id)
    if not driver_row:
        await callback.answer("Анкета не найдена.", show_alert=True)
        return

    set_driver_status(driver_row_id, "approved")
    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n✅ <b>ОДОБРЕНО</b>",
        reply_markup=None,
    )
    await callback.answer("Водитель одобрен")

    try:
        await bot.send_message(
            driver_row["telegram_id"],
            "Поздравляем! Твоя анкета одобрена ✅\nТеперь заходи в группу водителей и принимай заказы.",
        )
    except Exception as e:
        logging.warning(f"Не удалось уведомить водителя {driver_row['telegram_id']}: {e}")


@router.callback_query(F.data.startswith("drv_no:"))
async def driver_reject_cb(callback: CallbackQuery):
    driver_row_id = int(callback.data.split(":")[1])
    driver_row = get_driver(driver_row_id)
    if not driver_row:
        await callback.answer("Анкета не найдена.", show_alert=True)
        return

    set_driver_status(driver_row_id, "rejected")
    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n❌ <b>ОТКЛОНЕНО</b>",
        reply_markup=None,
    )
    await callback.answer("Водитель отклонён")

    try:
        await bot.send_message(
            driver_row["telegram_id"],
            "К сожалению, твоя анкета отклонена модератором. "
            "Если это ошибка — свяжись с организатором.",
        )
    except Exception as e:
        logging.warning(f"Не удалось уведомить водителя {driver_row['telegram_id']}: {e}")


# ---------- ВОДИТЕЛЬСКИЙ FLOW (группа) ----------

@router.callback_query(F.data.startswith("take:"))
async def take_order_cb(callback: CallbackQuery):
    order_id = int(callback.data.split(":")[1])
    driver = callback.from_user
    driver_name = driver.full_name

    if not is_driver_approved(driver.id):
        await callback.answer(
            "Ты ещё не зарегистрирован как проверенный водитель. "
            "Напиши боту в личку и пройди регистрацию.",
            show_alert=True,
        )
        return

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

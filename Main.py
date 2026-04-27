import asyncio
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from dotenv import load_dotenv
import sys
import os
sys.path.insert(0, '/data/data/com.termux/files/home/garant')

from database import Database
from blockchain import TONManager

# Загрузка конфигурации
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS").split(",")]
WALLET_MNEMONIC = os.getenv("WALLET_MNEMONIC")
TON_API_KEY = os.getenv("TON_API_KEY")
ESCROW_WALLET = os.getenv("ESCROW_WALLET")
DEFAULT_FEE_PERCENT = float(os.getenv("DEFAULT_FEE_PERCENT", 3.0))

# Инициализация
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db = Database()
ton = TONManager(WALLET_MNEMONIC, TON_API_KEY, ESCROW_WALLET)

# FSM состояния
class DealStates(StatesGroup):
    waiting_amount = State()
    waiting_seller_username = State()
    waiting_seller_wallet = State()
    waiting_description = State()

class WithdrawStates(StatesGroup):
    waiting_amount = State()

class DisputeStates(StatesGroup):
    waiting_reason = State()

# Клавиатуры
def main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="🛒 Создать сделку"))
    builder.add(KeyboardButton(text="💰 Баланс"), KeyboardButton(text="📊 Профиль"))
    builder.add(KeyboardButton(text="💎 Пополнить"), KeyboardButton(text="💸 Вывести"))
    builder.add(KeyboardButton(text="👥 Рефералы"), KeyboardButton(text="ℹ️ Помощь"))
    if user_id in ADMIN_IDS:
        builder.add(KeyboardButton(text="⚙️ Админ панель"))
    builder.adjust(1, 2, 2, 2, 1)
    return builder.as_markup(resize_keyboard=True)

def admin_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="📋 Активные сделки", callback_data="admin_active_deals"))
    builder.add(InlineKeyboardButton(text="⚠️ Диспуты", callback_data="admin_disputes"))
    builder.add(InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users"))
    builder.add(InlineKeyboardButton(text="💰 Баланс кошелька", callback_data="admin_wallet_balance"))
    builder.add(InlineKeyboardButton(text="⚙️ Комиссия", callback_data="admin_set_fee"))
    builder.add(InlineKeyboardButton(text="🛑 Стоп выплаты", callback_data="admin_stop_payouts"))
    builder.adjust(2)
    return builder.as_markup()

# ==================== ХЭНДЛЕРЫ ====================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user = db.get_user(message.from_user.id)
    if not user:
        ref_code = message.text.split()[-1] if len(message.text.split()) > 1 else None
        referred_by = None
        if ref_code and ref_code != "/start":
            # Поиск реферера по коду (упрощенно)
            with db.get_connection() as conn:
                ref_user = conn.execute("SELECT user_id FROM users WHERE referral_code = ?", (ref_code,)).fetchone()
                if ref_user:
                    referred_by = ref_user['user_id']
        user = db.create_user(message.from_user.id, message.from_user.username, message.from_user.first_name, referred_by)
    
    welcome = (
        f"🎮 <b>TON Garant Bot</b>\n\n"
        f"👤 {message.from_user.full_name}\n"
        f"🆔 ID: <code>{user['user_id']}</code>\n"
        f"⭐ Рейтинг: {user['rating']:.1f}\n"
        f"📊 Сделок: {user['total_deals']}\n\n"
        f"<i>Безопасные сделки в сети TON</i>"
    )
    await message.answer(welcome, reply_markup=main_keyboard(message.from_user.id), parse_mode="HTML")

@dp.message(F.text == "🛒 Создать сделку")
async def create_deal_start(message: types.Message, state: FSMContext):
    await message.answer("💰 Введите сумму сделки в TON (минимум 0.01):")
    await state.set_state(DealStates.waiting_amount)

@dp.message(DealStates.waiting_amount)
async def process_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
        if amount < 0.01:
            await message.answer("❌ Минимальная сумма: 0.01 TON")
            return
        await state.update_data(amount=amount)
        await message.answer("👤 Введите username продавца (без @):")
        await state.set_state(DealStates.waiting_seller_username)
    except ValueError:
        await message.answer("❌ Введите корректное число")

@dp.message(DealStates.waiting_seller_username)
async def process_seller_username(message: types.Message, state: FSMContext):
    seller_username = message.text.replace("@", "").strip()
    await state.update_data(seller_username=seller_username)
    await message.answer("🏦 Введите TON адрес кошелька продавца (UQ... или EQ...):")
    await state.set_state(DealStates.waiting_seller_wallet)

@dp.message(DealStates.waiting_seller_wallet)
async def process_seller_wallet(message: types.Message, state: FSMContext):
    wallet = message.text.strip()
    if not (wallet.startswith("UQ") or wallet.startswith("EQ")):
        await message.answer("❌ Неверный формат адреса. Должен начинаться с UQ или EQ")
        return
    await state.update_data(seller_wallet=wallet)
    await message.answer("📝 Описание сделки (или напишите 'нет'):")
    await state.set_state(DealStates.waiting_description)

@dp.message(DealStates.waiting_description)
async def process_description(message: types.Message, state: FSMContext):
    description = message.text if message.text.lower() != "нет" else ""
    data = await state.get_data()
    
    # Создаем продавца в БД если его нет (упрощенно)
    seller_username = data['seller_username']
    with db.get_connection() as conn:
        seller = conn.execute("SELECT user_id FROM users WHERE username = ?", (seller_username,)).fetchone()
        seller_id = seller['user_id'] if seller else -1  # В реальности нужно пригласить продавца в бота
    
    if seller_id == -1:
        await message.answer("⚠️ Продавец не зарегистрирован в боте. Попросите его запустить бота командой /start.")
        await state.clear()
        return
    
    # Создаем сделку (БЕСПЛАТНО)
    deal = db.create_deal(
        buyer_id=message.from_user.id,
        seller_id=seller_id,
        seller_username=seller_username,
        seller_wallet=data['seller_wallet'],
        amount_ton=data['amount'],
        description=description
    )
    
    # Генерируем ссылку на оплату
    total_ton = deal['total_nanotons'] / 1e9
    payment_link = ton.generate_payment_link(total_ton, deal['deal_id'])
    
    deal_info = (
        f"🤝 <b>Сделка создана!</b>\n\n"
        f"🆔 ID: <code>{deal['deal_id']}</code>\n"
        f"👤 Покупатель: @{message.from_user.username}\n"
        f"👤 Продавец: @{seller_username}\n"
        f"💰 Сумма: {deal['amount_nanotons'] / 1e9:.2f} TON\n"
        f"💳 Комиссия: {deal['fee_nanotons'] / 1e9:.2f} TON\n"
        f"💎 Итого к оплате: <b>{total_ton:.2f} TON</b>\n"
        f"📝 Описание: {description or 'Нет'}\n\n"
        f"⚠️ <i>Оплатите по кнопке ниже. Бот автоматически подтвердит платеж.</i>"
    )
    
    keyboard = InlineKeyboardBuilder()
    keyboard.add(InlineKeyboardButton(text="💳 Оплатить через Tonkeeper", url=payment_link))
    keyboard.add(InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"check_payment_{deal['deal_id']}"))
    keyboard.add(InlineKeyboardButton(text="❌ Отменить сделку", callback_data=f"cancel_deal_{deal['deal_id']}"))
    keyboard.adjust(1)
    
    await message.answer(deal_info, reply_markup=keyboard.as_markup(), parse_mode="HTML")
    await state.clear()

@dp.callback_query(F.data.startswith("check_payment_"))
async def check_payment(callback: types.CallbackQuery):
    deal_id = callback.data.split("_", 2)[2]
    deal = db.get_deal(deal_id)
    
    if not deal or deal['status'] != 'WAITING_PAYMENT':
        await callback.answer("Сделка уже обработана")
        return
    
    await callback.message.edit_text("🔍 Проверяю платеж в блокчейне...")
    
    success, tx_hash = await ton.check_incoming_transaction(deal_id, deal['total_nanotons'])
    
    if success:
        db.activate_deal(deal_id, tx_hash)
        await callback.message.edit_text(
            f"✅ <b>Оплата получена!</b>\n"
            f"Сделка <code>{deal_id}</code> активирована.\n"
            f"Ожидайте подтверждения от покупателя.",
            parse_mode="HTML"
        )
        # Уведомление продавцу (здесь нужен user_id продавца)
        try:
            await bot.send_message(
                deal['seller_id'],
                f"💰 <b>Новая сделка #{deal_id}</b>\n"
                f"Сумма: {deal['amount_nanotons'] / 1e9:.2f} TON\n"
                f"Оплата получена! Ожидайте подтверждения покупателя.",
                reply_markup=InlineKeyboardBuilder().add(
                    InlineKeyboardButton(text="✅ Я выполнил заказ", callback_data=f"seller_done_{deal_id}")
                ).as_markup(),
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Seller notify error: {e}")
    else:
        await callback.message.edit_text(
            f"⏳ <b>Платеж еще не найден</b>\n"
            f"Сделка: <code>{deal_id}</code>\n"
            f"Проверьте правильность суммы и комментария.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().add(
                InlineKeyboardButton(text="🔄 Проверить снова", callback_data=f"check_payment_{deal_id}")
            ).as_markup()
        )
    await callback.answer()

@dp.callback_query(F.data.startswith("cancel_deal_"))
async def cancel_deal(callback: types.CallbackQuery):
    deal_id = callback.data.split("_", 2)[2]
    db.cancel_expired_deal(deal_id)
    await callback.message.edit_text(f"❌ Сделка <code>{deal_id}</code> отменена.", parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("seller_done_"))
async def seller_done(callback: types.CallbackQuery):
    deal_id = callback.data.split("_", 2)[2]
    deal = db.get_deal(deal_id)
    if deal and deal['status'] == 'ACTIVE':
        db.confirm_deal(deal_id, callback.from_user.id)
        await callback.message.edit_text("✅ Вы подтвердили выполнение заказа. Ожидайте подтверждения покупателя.")
    else:
        await callback.answer("Сделка не активна")
    await callback.answer()

# ==================== АДМИН ПАНЕЛЬ ====================
@dp.message(F.text == "⚙️ Админ панель")
async def admin_panel(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Доступ запрещен")
        return
    await message.answer("⚙️ <b>Админ панель</b>", reply_markup=admin_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_active_deals")
async def admin_active_deals(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    
    with db.get_connection() as conn:
        deals = conn.execute("SELECT * FROM deals WHERE status IN ('ACTIVE', 'WAITING_PAYMENT')").fetchall()
    
    if not deals:
        await callback.message.edit_text("Нет активных сделок.")
        return
    
    text = "📋 <b>Активные сделки:</b>\n\n"
    for d in deals:
        text += f"🆔 <code>{d['deal_id']}</code>\n"
        text += f"Статус: {d['status']}\n"
        text += f"Сумма: {d['amount_nanotons'] / 1e9:.2f} TON\n"
        text += f"---\n"
    
    await callback.message.edit_text(text, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_disputes")
async def admin_disputes(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    
    with db.get_connection() as conn:
        disputes = conn.execute("SELECT * FROM deals WHERE status = 'DISPUTE'").fetchall()
    
    if not disputes:
        await callback.message.edit_text("Нет открытых споров.")
        return
    
    for d in disputes:
        text = (
            f"⚠️ <b>Спор #{d['deal_id']}</b>\n"
            f"Причина: {d['dispute_reason']}\n"
            f"Сумма: {d['amount_nanotons'] / 1e9:.2f} TON\n"
        )
        keyboard = InlineKeyboardBuilder()
        keyboard.add(InlineKeyboardButton(text="💰 Вернуть покупателю", callback_data=f"resolve_buyer_{d['deal_id']}"))
        keyboard.add(InlineKeyboardButton(text="🤝 Выплатить продавцу", callback_data=f"resolve_seller_{d['deal_id']}"))
        keyboard.adjust(1)
        await callback.message.answer(text, reply_markup=keyboard.as_markup(), parse_mode="HTML")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("resolve_"))
async def resolve_dispute(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    
    _, winner, deal_id = callback.data.split("_")
    deal = db.get_deal(deal_id)
    
    if not deal:
        await callback.answer("Сделка не найдена")
        return
    
    if winner == "buyer":
        recipient = deal['buyer_id']
        amount = deal['total_nanotons']
        db.resolve_dispute(deal_id, recipient)
        db.refund_buyer(deal_id)
        text = f"✅ Спор решен в пользу покупателя. Средства возвращены."
    else:
        recipient = deal['seller_id']
        amount = int(deal['amount_nanotons'] * 0.95)  # вычет комиссии
        db.resolve_dispute(deal_id, recipient)
        db.release_payment(deal_id)
        
        # Отправка средств продавцу
        wallet = db.get_user_wallet(recipient)
        if wallet:
            success, _ = await ton.send_payout(wallet, amount, f"Deal {deal_id} resolved")
            text = f"✅ Спор решен в пользу продавца. Средства отправлены."
        else:
            text = "⚠️ У продавца не указан адрес кошелька."
    
    await callback.message.edit_text(text)
    await callback.answer()

@dp.callback_query(F.data == "admin_wallet_balance")
async def admin_wallet_balance(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    
    balance = await ton.get_wallet_balance()
    balance_ton = balance / 1e9
    await callback.message.edit_text(f"💰 <b>Баланс кошелька:</b> {balance_ton:.4f} TON", parse_mode="HTML")
    await callback.answer()

# ==================== ФОНОВЫЕ ЗАДАЧИ ====================
async def check_expired_deals():
    """Проверка истекших сделок каждые 5 минут"""
    while True:
        try:
            expired = db.get_expired_deals()
            for deal in expired:
                db.cancel_expired_deal(deal['deal_id'])
                logging.info(f"Deal {deal['deal_id']} expired")
        except Exception as e:
            logging.error(f"Expired deals check error: {e}")
        await asyncio.sleep(300)

# ==================== ЗАПУСК ====================
async def main():
    logging.basicConfig(level=logging.INFO)
    
    # Запуск фоновых задач
    asyncio.create_task(check_expired_deals())
    
    logging.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

import os
import asyncio
import logging
import sqlite3
from typing import Dict, Any
from datetime import datetime
import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery

# Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных окружения!")

logging.basicConfig(level=logging.INFO)

# =====================================================================
# БАЗА ДАННЫХ
# =====================================================================
DB_NAME = "tracker_db.sqlite"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            target_id TEXT PRIMARY KEY,
            is_channel INTEGER,
            interval_min INTEGER DEFAULT 0,
            last_check TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id TEXT,
            asset TEXT,
            alert_type TEXT,
            target_price REAL,
            is_active INTEGER DEFAULT 1
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def db_execute(query, params=()):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(query, params)
    conn.commit()
    conn.close()

def db_fetchall(query, params=()):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(query, params)
    res = cursor.fetchall()
    conn.close()
    return res

# =====================================================================
# УЛУЧШЕННЫЙ РЕАЛЬНЫЙ API (Цена + Изменения за 24ч)
# =====================================================================
async def fetch_prices() -> Dict[str, Any]:
    # Дефолтная структура данных
    prices = {
        "GRAM": 7.30, "GRAM_change": 0.0,
        "USDT": 1.0, "USDT_change": 0.0,
        "BTC": 65000.0, "BTC_change": 0.0,
        "DOGE": 0.14, "DOGE_change": 0.0
    }
    
    # 1. Запрос BTC и DOGE с Binance 24hr Ticker API
    try:
        async with aiohttp.ClientSession() as session:
            url_binance = "https://api.binance.com/api/v3/ticker/24hr?symbols=[%22BTCUSDT%22,%22DOGEUSDT%22]"
            async with session.get(url_binance, timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data:
                        ticker = item['symbol'].replace('USDT', '')
                        prices[ticker] = float(item['lastPrice'])
                        prices[f"{ticker}_change"] = float(item['priceChangePercent'])
    except Exception as e:
        logging.error(f"Ошибка Binance API: {e}")

    # 2. Запрос TON (как GRAM) и USDT с CoinGecko API с параметром изменений за 24ч
    try:
        async with aiohttp.ClientSession() as session:
            url_gecko = "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network,tether&vs_currencies=usd&include_24hr_change=true"
            async with session.get(url_gecko, timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "the-open-network" in data:
                        prices["GRAM"] = float(data["the-open-network"].get("usd", prices["GRAM"]))
                        prices["GRAM_change"] = float(data["the-open-network"].get("usd_24h_change", 0.0))
                    if "tether" in data:
                        prices["USDT"] = float(data["tether"].get("usd", prices["USDT"]))
                        prices["USDT_change"] = float(data["tether"].get("usd_24h_change", 0.0))
    except Exception as e:
        logging.error(f"Ошибка CoinGecko API: {e}")

    return prices

# Вспомогательная функция для красивого вывода изменения цены
def format_change(change_val: float) -> str:
    emoji = "🟢 +" if change_val >= 0 else "🔴 "
    return f"{emoji}{change_val:.2f}%"

# Вспомогательная функция сборки текста прайса
def build_price_text(prices: dict) -> str:
    dt_now = datetime.now().strftime("%H:%M:%S")
    return (
        f"<b>📊 Актуальный прайс криптовалют</b>\n"
        f"<i>Обновлено в: {dt_now}</i>\n\n"
        f"💎 <b>GRAM (TON):</b> ${prices['GRAM']:.3f} | {format_change(prices['GRAM_change'])}\n"
        f"💵 <b>USDT:</b> ${prices['USDT']:.3f} | {format_change(prices['USDT_change'])}\n"
        f"🪙 <b>BTC:</b> ${prices['BTC']:,.2f} | {format_change(prices['BTC_change'])}\n"
        f"🐕 <b>DOGE:</b> ${prices['DOGE']:.4f} | {format_change(prices['DOGE_change'])}"
    )

# =====================================================================
# СОСТОЯНИЯ FSM И КЛАВИАТУРЫ
# =====================================================================
class BotStates(StatesGroup):
    waiting_for_channel = State()
    waiting_for_alert_price = State()

def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Показать курсы", callback_data="check_prices")],
        [InlineKeyboardButton(text="⚙️ Настройки уведомлений", callback_data="settings_main")],
        [InlineKeyboardButton(text="📢 Настройка Каналов", callback_data="channel_main")]
    ])

def prices_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить прайс", callback_data="refresh_prices")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main")]
    ])

def settings_menu(target_id: str, is_channel: bool):
    prefix = f"ch_{target_id}" if is_channel else "usr"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱ Интервал отчетов", callback_data=f"set_int_{prefix}")],
        [InlineKeyboardButton(text="🔔 Добавить Alert", callback_data=f"add_alert_{prefix}")],
        [InlineKeyboardButton(text="📜 Список моих алертов", callback_data=f"list_alerts_{prefix}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
    ])

# =====================================================================
# ХЕНДЛЕРЫ
# =====================================================================
router = Router()

@router.message(Command("start"))
async def cmd_start(message: Message):
    db_execute("INSERT OR IGNORE INTO settings (target_id, is_channel) VALUES (?, 0)", (str(message.from_user.id),))
    await message.answer("🎯 Добро пожаловать! Я трекер-бот. Слежу за рынком и отправляю алерты.\nПод именем <b>GRAM</b> выводится реальный курс <b>TON</b>.", reply_markup=main_menu(), parse_mode="HTML")

@router.callback_query(F.data == "back_to_main")
async def back_to_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Главное меню бота:", reply_markup=main_menu(), parse_mode="HTML")

# Вывод курсов
@router.callback_query(F.data == "check_prices")
async def check_prices_handler(call: CallbackQuery):
    prices = await fetch_prices()
    await call.message.edit_text(build_price_text(prices), reply_markup=prices_keyboard(), parse_mode="HTML")

# Кнопка мгновенного обновления прайса (Фича!)
@router.callback_query(F.data == "refresh_prices")
async def refresh_prices_handler(call: CallbackQuery):
    prices = await fetch_prices()
    # Чтобы телеграм не выдавал ошибку, если цена не изменилась за секунду:
    try:
        await call.message.edit_text(build_price_text(prices), reply_markup=prices_keyboard(), parse_mode="HTML")
        await call.answer("Прайс обновлен!")
    except Exception:
        await call.answer("Курс пока прежний", show_alert=False)

@router.callback_query(F.data == "settings_main")
async def settings_main(call: CallbackQuery):
    await call.message.edit_text("Настройки уведомлений для текущего чата:", reply_markup=settings_menu(str(call.from_user.id), False), parse_mode="HTML")

# Управление каналами
@router.callback_query(F.data == "channel_main")
async def channel_main(call: CallbackQuery):
    channels = db_fetchall("SELECT target_id FROM settings WHERE is_channel = 1")
    text = "📢 <b>Подключенные каналы:</b>\n\n"
    keyboard = []
    
    if channels:
        for ch in channels:
            text += f"• <code>{ch[0]}</code>\n"
            keyboard.append([InlineKeyboardButton(text=f"Настроить {ch[0]}", callback_data=f"manage_ch_{ch[0]}")])
    else:
        text += "У вас пока нет добавленных каналов."
        
    keyboard.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="add_new_channel")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")])
    
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")

@router.callback_query(F.data == "add_new_channel")
async def add_new_channel(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("Отправьте ID канала (начинается с -100...).\n<b>Важно:</b> Бот должен быть назначен Администратором вашего канала!", parse_mode="HTML")
    await state.set_state(BotStates.waiting_for_channel)

@router.message(BotStates.waiting_for_channel)
async def save_channel(message: Message, state: FSMContext):
    channel_id = None
    if message.forward_from_chat and message.forward_from_chat.type == "channel":
        channel_id = str(message.forward_from_chat.id)
    elif message.text.startswith("-100"):
        channel_id = message.text.strip()
        
    if channel_id:
        db_execute("INSERT OR IGNORE INTO settings (target_id, is_channel) VALUES (?, 1)", (channel_id,))
        await message.answer(f"✅ Канал <code>{channel_id}</code> успешно добавлен!", reply_markup=main_menu(), parse_mode="HTML")
        await state.clear()
    else:
        await message.answer("Неверный формат ID. Попробуйте еще раз или вернитесь в меню через /start.")

@router.callback_query(F.data.startswith("manage_ch_"))
async def manage_channel(call: CallbackQuery):
    ch_id = call.data.replace("manage_ch_", "")
    await call.message.edit_text(f"Настройки публикации для канала <code>{ch_id}</code>:", reply_markup=settings_menu(ch_id, True), parse_mode="HTML")

# Интервалы
@router.callback_query(F.data.startswith("set_int_"))
async def set_interval_options(call: CallbackQuery):
    target = call.data.replace("set_int_", "")
    keyboard = []
    intervals = [("Выкл ❌", 0), ("30 минут", 30), ("1 час", 60), ("4 часа", 240), ("12 часов", 720)]
    for text, val in intervals:
        keyboard.append([InlineKeyboardButton(text=text, callback_data=f"saveint_{val}_{target}")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")])
    await call.message.edit_text("Выберите периодичность автоматических отчетов по ценам:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@router.callback_query(F.data.startswith("saveint_"))
async def save_interval(call: CallbackQuery):
    parts = call.data.split("_")
    val = int(parts[1])
    target_type = parts[2]
    
    if target_type == "usr":
        target_id = str(call.from_user.id)
    else:
        target_id = "_".join(parts[3:])
        
    db_execute("UPDATE settings SET interval_min = ?, last_check = NULL WHERE target_id = ?", (val, target_id))
    await call.answer("Готово! Настройки сохранены", show_alert=True)
    await call.message.edit_text("✅ Изменения применились.", reply_markup=main_menu())

# Создание алертов
@router.callback_query(F.data.startswith("add_alert_"))
async def add_alert_select_coin(call: CallbackQuery, state: FSMContext):
    target = call.data.replace("add_alert_", "")
    keyboard = []
    for coin in ["GRAM", "USDT", "BTC", "DOGE"]:
        keyboard.append([
            InlineKeyboardButton(text=f"{coin} станет выше ↑", callback_data=f"coinalert_{coin}_up_{target}"),
            InlineKeyboardButton(text=f"{coin} станет ниже ↓", callback_data=f"coinalert_{coin}_down_{target}")
        ])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")])
    await call.message.edit_text("Выберите актив и направление триггера:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@router.callback_query(F.data.startswith("coinalert_"))
async def process_coin_alert(call: CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    coin = parts[1]
    alert_type = parts[2]
    target = "_".join(parts[3:])
    
    target_id = str(call.from_user.id) if target == "usr" else target
        
    await state.update_data(alert_target=target_id, alert_coin=coin, alert_type=alert_type)
    await call.message.edit_text(f"Введите пороговую цену для <b>{coin}</b> в USD (например, 7.55 или 63200):", parse_mode="HTML")
    await state.set_state(BotStates.waiting_for_alert_price)

@router.message(BotStates.waiting_for_alert_price)
async def save_alert_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Ошибка! Пожалуйста, отправьте число.")
        return
        
    data = await state.get_data()
    db_execute(
        "INSERT INTO alerts (target_id, asset, alert_type, target_price) VALUES (?, ?, ?, ?)",
        (data['alert_target'], data['alert_coin'], data['alert_type'], price)
    )
    arrow = "вырастет до" if data['alert_type'] == 'up' else "упадет до"
    await message.answer(f"🚀 Будильник заведен! Когда <b>{data['alert_coin']}</b> {arrow} <b>${price}</b>, я пришлю уведомление.", reply_markup=main_menu(), parse_mode="HTML")
    await state.clear()

@router.callback_query(F.data.startswith("list_alerts_"))
async def list_alerts(call: CallbackQuery):
    target = call.data.replace("list_alerts_", "")
    target_id = str(call.from_user.id) if target == "usr" else target
    
    alerts = db_fetchall("SELECT id, asset, alert_type, target_price FROM alerts WHERE target_id = ? AND is_active = 1", (target_id,))
    
    text = "📜 <b>Ваши активные алерты:</b>\n\n"
    keyboard = []
    if alerts:
        for aid, asset, atype, price in alerts:
            arrow = "🔼 выше" if atype == "up" else "🔽 ниже"
            text += f"• {asset} при цене {arrow} ${price}\n"
            keyboard.append([InlineKeyboardButton(text=f"❌ Снять {asset} ({arrow} ${price})", callback_data=f"delalert_{aid}")])
    else:
        text += "Активных триггеров на цену нет."
        
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")

@router.callback_query(F.data.startswith("delalert_"))
async def delete_alert(call: CallbackQuery):
    aid = call.data.split("_")[1]
    db_execute("DELETE FROM alerts WHERE id = ?", (aid,))
    await call.answer("Алерт удален")
    await call.message.edit_text("✅ Алерт успешно деактивирован.", reply_markup=main_menu())

# =====================================================================
# ФОНОВЫЙ ТРЕКЕР
# =====================================================================
async def price_tracker_worker(bot: Bot):
    while True:
        try:
            prices = await fetch_prices()
            now = datetime.now()
            
            # Проверка триггерных алертов
            alerts = db_fetchall("SELECT id, target_id, asset, alert_type, target_price FROM alerts WHERE is_active = 1")
            for aid, target_id, asset, alert_type, target_price in alerts:
                current_price = prices.get(asset, 0)
                triggered = False
                
                if alert_type == "up" and current_price >= target_price:
                    triggered = True
                    msg = f"🔔 <b>ALERT! Рынок растет!</b>\n📈 Актив <b>{asset}</b> пробил верхнюю цель: <b>${current_price:.4f}</b> (Цель: ${target_price})"
                elif alert_type == "down" and current_price <= target_price:
                    triggered = True
                    msg = f"🔔 <b>ALERT! Рынок падает!</b>\n📉 Актив <b>{asset}</b> опустился ниже цели: <b>${current_price:.4f}</b> (Цель: ${target_price})"
                    
                if triggered:
                    try:
                        await bot.send_message(chat_id=target_id, text=msg, parse_mode="HTML")
                        db_execute("UPDATE alerts SET is_active = 0 WHERE id = ?", (aid,)) 
                    except Exception as e:
                        logging.error(f"Не удалось отправить алерт: {e}")

            # Периодические отчеты
            settings = db_fetchall("SELECT target_id, interval_min, last_check FROM settings WHERE interval_min > 0")
            for target_id, interval, last_check in settings:
                should_send = False
                if not last_check:
                    should_send = True
                else:
                    last_check_dt = datetime.fromisoformat(last_check)
                    if (now - last_check_dt).total_seconds() / 60 >= interval:
                        should_send = True
                        
                if should_send:
                    try:
                        await bot.send_message(chat_id=target_id, text=build_price_text(prices), parse_mode="HTML")
                        db_execute("UPDATE settings SET last_check = ? WHERE target_id = ?", (now.isoformat(), target_id))
                    except Exception as e:
                        logging.error(f"Не удалось отправить отчет: {e}")

        except Exception as e:
            logging.error(f"Ошибка трекера: {e}")
            
        await asyncio.sleep(40) # Проверка цен каждые 40 секунд

# =====================================================================
# СТАРТ
# =====================================================================
async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    
    asyncio.create_task(price_tracker_worker(bot))
    
    print("Улучшенный бот успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

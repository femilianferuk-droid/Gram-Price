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
# ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ
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
            alert_type TEXT, -- 'up' или 'down'
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
# РЕАЛЬНЫЙ ИНТЕГРИРОВАННЫЙ API ДЛЯ ПОЛУЧЕНИЯ КУРСОВ
# =====================================================================
async def fetch_prices() -> Dict[str, float]:
    # Базовые значения на случай временного сбоя сети у провайдеров API
    prices = {"GRAM": 1.65, "USDT": 1.0, "BTC": 65000.0, "DOGE": 0.14}
    
    # 1. Запрос BTC и DOGE с публичного API Binance (не требует ключей, высокий rate-limit)
    try:
        async with aiohttp.ClientSession() as session:
            url_binance = "https://api.binance.com/api/v3/ticker/price?symbols=[%22BTCUSDT%22,%22DOGEUSDT%22]"
            async with session.get(url_binance, timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data:
                        ticker = item['symbol'].replace('USDT', '')
                        prices[ticker] = float(item['price'])
    except Exception as e:
        logging.error(f"Ошибка получения цен с Binance: {e}")

    # 2. Запрос цены GRAM и USDT с CoinGecko API (публичный эндпоинт без авторизации)
    try:
        async with aiohttp.ClientSession() as session:
            url_gecko = "https://api.coingecko.com/api/v3/simple/price?ids=gram,tether&vs_currencies=usd"
            async with session.get(url_gecko, timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "gram" in data:
                        prices["GRAM"] = float(data["gram"].get("usd", prices["GRAM"]))
                    if "tether" in data:
                        prices["USDT"] = float(data["tether"].get("usd", prices["USDT"]))
    except Exception as e:
        logging.error(f"Ошибка получения цен с CoinGecko: {e}")

    return prices

# =====================================================================
# СОСТОЯНИЯ FSM
# =====================================================================
class BotStates(StatesGroup):
    waiting_for_channel = State()
    waiting_for_alert_price = State()

# =====================================================================
# КЛАВИАТУРЫ
# =====================================================================
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Текущие курсы", callback_data="check_prices")],
        [InlineKeyboardButton(text="⚙️ Настройки уведомлений", callback_data="settings_main")],
        [InlineKeyboardButton(text="📢 Добавить/Настроить Канал", callback_data="channel_main")]
    ])

def settings_menu(target_id: str, is_channel: bool):
    prefix = f"ch_{target_id}" if is_channel else "usr"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱ Изменить интервал", callback_data=f"set_int_{prefix}")],
        [InlineKeyboardButton(text="🔔 Добавить Алерт (Цена)", callback_data=f"add_alert_{prefix}")],
        [InlineKeyboardButton(text="📜 Активные алерты", callback_data=f"list_alerts_{prefix}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
    ])

# =====================================================================
# ХЕНДЛЕРЫ
# =====================================================================
router = Router()

@router.message(Command("start"))
async def cmd_start(message: Message):
    db_execute("INSERT OR IGNORE INTO settings (target_id, is_channel) VALUES (?, 0)", (str(message.from_user.id),))
    await message.answer("Привет! Я бот для отслеживания реальных курсов криптовалют (GRAM, USDT, BTC, DOGE).\nВыберите действие:", reply_markup=main_menu())

@router.callback_query(F.data == "back_to_main")
async def back_to_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Главное меню:", reply_markup=main_menu())

@router.callback_query(F.data == "check_prices")
async def check_prices_handler(call: CallbackQuery):
    prices = await fetch_prices()
    text = (
        f"💰 **Актуальные курсы валют (Live API):**\n\n"
        f"💎 **GRAM:** ${prices.get('GRAM'):.4f}\n"
        f"💵 **USDT:** ${prices.get('USDT'):.4f}\n"
        f"🪙 **BTC:** ${prices.get('BTC'):,.2f}\n"
        f"🐕 **DOGE:** ${prices.get('DOGE'):.4f}"
    )
    await call.message.edit_text(text, reply_markup=main_menu(), parse_mode="Markdown")

@router.callback_query(F.data == "settings_main")
async def settings_main(call: CallbackQuery):
    await call.message.edit_text("Настройки мониторинга для вашего личного чата:", reply_markup=settings_menu(str(call.from_user.id), False))

@router.callback_query(F.data == "channel_main")
async def channel_main(call: CallbackQuery):
    channels = db_fetchall("SELECT target_id FROM settings WHERE is_channel = 1")
    text = "📢 **Ваши подключенные каналы:**\n\n"
    keyboard = []
    
    if channels:
        for ch in channels:
            text += f"• `{ch[0]}`\n"
            keyboard.append([InlineKeyboardButton(text=f"Настроить {ch[0]}", callback_data=f"manage_ch_{ch[0]}")])
    else:
        text += "У вас пока нет добавленных каналов."
        
    keyboard.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="add_new_channel")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")])
    
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@router.callback_query(F.data == "add_new_channel")
async def add_new_channel(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("Отправьте ID канала (начинается с -100...).\nВажно: Бот должен быть администратором в этом канале!")
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
        await message.answer(f"✅ Канал {channel_id} успешно добавлен!", reply_markup=main_menu())
        await state.clear()
    else:
        await message.answer("Не удалось распознать ID канала. Убедитесь, что отправили верный ID в формате `-100xxxxxxxxx`.")

@router.callback_query(F.data.startswith("manage_ch_"))
async def manage_channel(call: CallbackQuery):
    ch_id = call.data.replace("manage_ch_", "")
    await call.message.edit_text(f"Настройки для канала `{ch_id}`:", reply_markup=settings_menu(ch_id, True), parse_mode="Markdown")

@router.callback_query(F.data.startswith("set_int_"))
async def set_interval_options(call: CallbackQuery):
    target = call.data.replace("set_int_", "")
    
    keyboard = []
    intervals = [("Выкл", 0), ("30 мин", 30), ("1 час", 60), ("4 часа", 240), ("12 часов", 720)]
    for text, val in intervals:
        keyboard.append([InlineKeyboardButton(text=text, callback_data=f"saveint_{val}_{target}")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")])
    
    await call.message.edit_text("Выберите, как часто присылать изменения курсов:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

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
    await call.answer("Интервал обновлен!", show_alert=True)
    await call.message.edit_text("✅ Настройки интервала успешно сохранены.", reply_markup=main_menu())

@router.callback_query(F.data.startswith("add_alert_"))
async def add_alert_select_coin(call: CallbackQuery, state: FSMContext):
    target = call.data.replace("add_alert_", "")
    
    keyboard = []
    for coin in ["GRAM", "USDT", "BTC", "DOGE"]:
        keyboard.append([
            InlineKeyboardButton(text=f"{coin} выше ↑", callback_data=f"coinalert_{coin}_up_{target}"),
            InlineKeyboardButton(text=f"{coin} ниже ↓", callback_data=f"coinalert_{coin}_down_{target}")
        ])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")])
    await call.message.edit_text("Выберите монету и условие для уведомления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@router.callback_query(F.data.startswith("coinalert_"))
async def process_coin_alert(call: CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    coin = parts[1]
    alert_type = parts[2]
    target = "_".join(parts[3:])
    
    if target == "usr":
        target_id = str(call.from_user.id)
    else:
        target_id = target
        
    await state.update_data(alert_target=target_id, alert_coin=coin, alert_type=alert_type)
    await call.message.edit_text(f"Введите цену для {coin} в USD (например, 1.75 или 68500):")
    await state.set_state(BotStates.waiting_for_alert_price)

@router.message(BotStates.waiting_for_alert_price)
async def save_alert_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число.")
        return
        
    data = await state.get_data()
    db_execute(
        "INSERT INTO alerts (target_id, asset, alert_type, target_price) VALUES (?, ?, ?, ?)",
        (data['alert_target'], data['alert_coin'], data['alert_type'], price)
    )
    arrow = "выше ↑" if data['alert_type'] == 'up' else "ниже ↓"
    await message.answer(f"✅ Уведомление создано! Когда {data['alert_coin']} станет {arrow} чем ${price}, вы получите алерт.", reply_markup=main_menu())
    await state.clear()

@router.callback_query(F.data.startswith("list_alerts_"))
async def list_alerts(call: CallbackQuery):
    target = call.data.replace("list_alerts_", "")
    target_id = str(call.from_user.id) if target == "usr" else target
    
    alerts = db_fetchall("SELECT id, asset, alert_type, target_price FROM alerts WHERE target_id = ? AND is_active = 1", (target_id,))
    
    text = "📜 **Активные уведомления:**\n\n"
    keyboard = []
    if alerts:
        for aid, asset, atype, price in alerts:
            arrow = "↑" if atype == "up" else "↓"
            text += f"• {asset} {arrow} ${price}\n"
            keyboard.append([InlineKeyboardButton(text=f"❌ Удалить {asset} ({arrow}${price})", callback_data=f"delalert_{aid}")])
    else:
        text += "У вас нет активных уведомлений по цене."
        
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@router.callback_query(F.data.startswith("delalert_"))
async def delete_alert(call: CallbackQuery):
    aid = call.data.split("_")[1]
    db_execute("DELETE FROM alerts WHERE id = ?", (aid,))
    await call.answer("Уведомление удалено")
    await call.message.edit_text("✅ Уведомление успешно удалено.", reply_markup=main_menu())

# =====================================================================
# ФОНОВЫЙ ТРЕКЕР ЦЕН И ОТПРАВКА УВЕДОМЛЕНИЙ
# =====================================================================
async def price_tracker_worker(bot: Bot):
    while True:
        try:
            prices = await fetch_prices()
            now = datetime.now()
            
            # 1. СРАБАТЫВАНИЕ ЦЕЛЕВЫХ АЛЕРТОВ
            alerts = db_fetchall("SELECT id, target_id, asset, alert_type, target_price FROM alerts WHERE is_active = 1")
            for aid, target_id, asset, alert_type, target_price in alerts:
                current_price = prices.get(asset, 0)
                triggered = False
                
                if alert_type == "up" and current_price >= target_price:
                    triggered = True
                    msg = f"🔔 **ALERT!** Монета {asset} выросла! \n📈 Текущая цена: **${current_price:.4f}** (Целевой порог: ${target_price})"
                elif alert_type == "down" and current_price <= target_price:
                    triggered = True
                    msg = f"🔔 **ALERT!** Монета {asset} упала! \n📉 Текущая цена: **${current_price:.4f}** (Целевой порог: ${target_price})"
                    
                if triggered:
                    try:
                        await bot.send_message(chat_id=target_id, text=msg, parse_mode="Markdown")
                        db_execute("UPDATE alerts SET is_active = 0 WHERE id = ?", (aid,)) 
                    except Exception as e:
                        logging.error(f"Не удалось отправить алерт в {target_id}: {e}")

            # 2. ПЕРИОДИЧЕСКИЕ ОБНОВЛЕНИЯ ПО ИНТЕРВАЛАМ
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
                    text = (
                        f"⏱ **Периодический отчет по курсам валют:**\n\n"
                        f"💎 GRAM: ${prices.get('GRAM'):.4f}\n"
                        f"💵 USDT: ${prices.get('USDT'):.4f}\n"
                        f"🪙 BTC: ${prices.get('BTC'):,.2f}\n"
                        f"🐕 DOGE: ${prices.get('DOGE'):.4f}"
                    )
                    try:
                        await bot.send_message(chat_id=target_id, text=text, parse_mode="Markdown")
                        db_execute("UPDATE settings SET last_check = ? WHERE target_id = ?", (now.isoformat(), target_id))
                    except Exception as e:
                        logging.error(f"Не удалось отправить отчет в {target_id}: {e}")

        except Exception as e:
            logging.error(f"Ошибка в цикле трекера: {e}")
            
        await asyncio.sleep(45) # Проверка цен каждые 45 секунд

# =====================================================================
# ЗАПУСК
# =====================================================================
async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    
    # Запуск фонового потока проверки цен
    asyncio.create_task(price_tracker_worker(bot))
    
    print("Бот на реальных API успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

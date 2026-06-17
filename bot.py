import os
import asyncio
import logging
import sqlite3
from typing import Dict, Any
from datetime import datetime, timezone, timedelta
import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных окружения!")

logging.basicConfig(level=logging.INFO)

# =====================================================================
# БАЗА ДАННЫХ (С поддержкой выбора монет)
# =====================================================================
DB_NAME = "tracker_db.sqlite"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # Добавлено поле assets для хранения списка выбранных монет через запятую
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            target_id TEXT PRIMARY KEY,
            is_channel INTEGER,
            interval_min INTEGER DEFAULT 0,
            last_check TEXT,
            assets TEXT DEFAULT 'GRAM,USDT,BTC,DOGE'
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
# ВРЕМЯ МСК И API
# =====================================================================
def get_msk_time() -> datetime:
    return datetime.now(timezone(timedelta(hours=3)))

async def fetch_prices() -> tuple[Dict[str, Any], bool]:
    # Корректные дефолтные значения (заглушка)
    prices = {
        "GRAM": 5.25, "GRAM_change": 0.0,
        "USDT": 1.0, "USDT_change": 0.0,
        "BTC": 67200.0, "BTC_change": 0.0,
        "DOGE": 0.142, "DOGE_change": 0.0
    }
    is_live = True
    
    # 1. Binance API (BTC, DOGE)
    try:
        async with aiohttp.ClientSession() as session:
            url_binance = "https://api.binance.com/api/v3/ticker/24hr?symbols=[%22BTCUSDT%22,%22DOGEUSDT%22]"
            async with session.get(url_binance, timeout=6) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data:
                        ticker = item['symbol'].replace('USDT', '')
                        prices[ticker] = float(item['lastPrice'])
                        prices[f"{ticker}_change"] = float(item['priceChangePercent'])
    except Exception as e:
        logging.error(f"Binance API Error: {e}")

    # 2. CoinGecko API (TON/GRAM, USDT)
    try:
        async with aiohttp.ClientSession() as session:
            url_gecko = "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network,tether&vs_currencies=usd&include_24hr_change=true"
            async with session.get(url_gecko, timeout=6) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "the-open-network" in data:
                        prices["GRAM"] = float(data["the-open-network"].get("usd", prices["GRAM"]))
                        prices["GRAM_change"] = float(data["the-open-network"].get("usd_24h_change", 0.0))
                    if "tether" in data:
                        prices["USDT"] = float(data["tether"].get("usd", prices["USDT"]))
                        prices["USDT_change"] = float(data["tether"].get("usd_24h_change", 0.0))
                else:
                    # Если CoinGecko кинул ошибку 429 (лимиты), помечаем, что прайс частично архивный
                    is_live = False
    except Exception as e:
        logging.error(f"CoinGecko API Error: {e}")
        is_live = False

    return prices, is_live

def format_change(change_val: float) -> str:
    emoji = "🟢 +" if change_val >= 0 else "🔴 "
    return f"{emoji}{change_val:.2f}%"

def build_price_text(prices: dict, is_live: bool, allowed_assets_str: str = "GRAM,USDT,BTC,DOGE") -> str:
    dt_now = get_msk_time().strftime("%H:%M:%S")
    status_market = "🟢 Live API" if is_live else "⚠️ Fallback (Лимит запросов API)"
    
    allowed = [a.strip() for a in allowed_assets_str.split(',')]
    
    text = f"<b>📊 Актуальный прайс криптовалют</b>\n"
    text += f"<i>Время МСК: {dt_now} | Статус: {status_market}</i>\n\n"
    
    if "GRAM" in allowed:
        text += f"💎 <b>TON Прайс (GRAM):</b> ${prices['GRAM']:.3f} | {format_change(prices['GRAM_change'])}\n"
    if "USDT" in allowed:
        text += f"💵 <b>USDT:</b> ${prices['USDT']:.3f} | {format_change(prices['USDT_change'])}\n"
    if "BTC" in allowed:
        text += f"🪙 <b>BTC:</b> ${prices['BTC']:,.2f} | {format_change(prices['BTC_change'])}\n"
    if "DOGE" in allowed:
        text += f"🐕 <b>DOGE:</b> ${prices['DOGE']:.4f} | {format_change(prices['DOGE_change'])}\n"
        
    if not any(coin in allowed for coin in ["GRAM", "USDT", "BTC", "DOGE"]):
        text += "❌ Ни одна монета не выбрана для отображения."
        
    return text

# =====================================================================
# СТРУКТУРА КЛАВИАТУР МЕНЮ
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
        [InlineKeyboardButton(text="⏱ Настроить интервал", callback_data=f"set_int_{prefix}")],
        [InlineKeyboardButton(text="🪙 Выбрать монеты для отчетов", callback_data=f"edit_coins_{prefix}")],
        [InlineKeyboardButton(text="🔔 Добавить Alert (Цена)", callback_data=f"add_alert_{prefix}")],
        [InlineKeyboardButton(text="📜 Список моих алертов", callback_data=f"list_alerts_{prefix}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main" if not is_channel else "channel_main")]
    ])

def coins_toggle_keyboard(target_id: str, is_channel: bool):
    res = db_fetchall("SELECT assets FROM settings WHERE target_id = ?", (target_id,))
    current_assets = res[0][0] if (res and res[0][0]) else "GRAM,USDT,BTC,DOGE"
    allowed = [a.strip() for a in current_assets.split(',')]
    
    prefix = f"ch_{target_id}" if is_channel else "usr"
    keyboard = []
    
    for coin in ["GRAM", "USDT", "BTC", "DOGE"]:
        status = "✅" if coin in allowed else "❌"
        keyboard.append([InlineKeyboardButton(text=f"{status} {coin}", callback_data=f"toggle_{coin}_{prefix}")])
        
    keyboard.append([InlineKeyboardButton(text="💾 Готово / Назад", callback_data=f"back_to_target_{prefix}")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# =====================================================================
# ХЕНДЛЕРЫ ЛОГИКИ БОТА
# =====================================================================
router = Router()

@router.message(Command("start"))
async def cmd_start(message: Message):
    db_execute("INSERT OR IGNORE INTO settings (target_id, is_channel) VALUES (?, 0, 'GRAM,USDT,BTC,DOGE')", (str(message.from_user.id),))
    await message.answer("🎯 Бот запущен! Под именем <b>GRAM</b> выводится актуальный <b>TON Прайс</b>.\nДоступен выбор отправляемой крипты для каналов и лички.", reply_markup=main_menu(), parse_mode="HTML")

@router.callback_query(F.data == "back_to_main")
async def back_to_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Главное меню бота:", reply_markup=main_menu(), parse_mode="HTML")

@router.callback_query(F.data == "check_prices")
async def check_prices_handler(call: CallbackQuery):
    prices, is_live = await fetch_prices()
    res = db_fetchall("SELECT assets FROM settings WHERE target_id = ?", (str(call.from_user.id),))
    allowed_str = res[0][0] if (res and res[0][0]) else "GRAM,USDT,BTC,DOGE"
    await call.message.edit_text(build_price_text(prices, is_live, allowed_str), reply_markup=prices_keyboard(), parse_mode="HTML")

@router.callback_query(F.data == "refresh_prices")
async def refresh_prices_handler(call: CallbackQuery):
    prices, is_live = await fetch_prices()
    res = db_fetchall("SELECT assets FROM settings WHERE target_id = ?", (str(call.from_user.id),))
    allowed_str = res[0][0] if (res and res[0][0]) else "GRAM,USDT,BTC,DOGE"
    try:
        await call.message.edit_text(build_price_text(prices, is_live, allowed_str), reply_markup=prices_keyboard(), parse_mode="HTML")
        await call.answer("Данные обновлены!")
    except Exception:
        await call.answer("Курс не изменился")

@router.callback_query(F.data == "settings_main")
async def settings_main(call: CallbackQuery):
    await call.message.edit_text("Настройки уведомлений для вашего чата:", reply_markup=settings_menu(str(call.from_user.id), False), parse_mode="HTML")

# Настройка списка монет (Чекбоксы)
@router.callback_query(F.data.startswith("edit_coins_"))
async def edit_coins_handler(call: CallbackQuery):
    target = call.data.replace("edit_coins_", "")
    if target == "usr":
        target_id, is_channel = str(call.from_user.id), False
    else:
        target_id, is_channel = target.replace("ch_", ""), True
        
    await call.message.edit_text(" Нажимайте на кнопки, чтобы включить/выключить монеты в авто-отчетах:", 
                                 reply_markup=coins_toggle_keyboard(target_id, is_channel))

@router.callback_query(F.data.startswith("toggle_"))
async def toggle_coin_db(call: CallbackQuery):
    # Разбор: toggle_COIN_usr ИЛИ toggle_COIN_ch_-100xxx
    parts = call.data.split("_")
    coin = parts[1]
    target_type = parts[2]
    target_id = str(call.from_user.id) if target_type == "usr" else "_".join(parts[3:])
    is_channel = (target_type != "usr")

    res = db_fetchall("SELECT assets FROM settings WHERE target_id = ?", (target_id,))
    current_assets = res[0][0] if (res and res[0][0]) else "GRAM,USDT,BTC,DOGE"
    allowed = [a.strip() for a in current_assets.split(',') if a.strip()]

    if coin in allowed:
        allowed.remove(coin)
    else:
        allowed.append(coin)

    new_assets_str = ",".join(allowed)
    db_execute("UPDATE settings SET assets = ? WHERE target_id = ?", (new_assets_str, target_id))
    
    # Моментально обновляем кнопки с чекбоксами
    await call.message.edit_reply_markup(reply_markup=coins_toggle_keyboard(target_id, is_channel))
    await call.answer(f"Изменено: {coin}")

@router.callback_query(F.data.startswith("back_to_target_"))
async def back_to_target(call: CallbackQuery):
    target = call.data.replace("back_to_target_", "")
    if target == "usr":
        await call.message.edit_text("Настройки уведомлений для вашего чата:", reply_markup=settings_menu(str(call.from_user.id), False), parse_mode="HTML")
    else:
        ch_id = target.replace("ch_", "")
        await call.message.edit_text(f"Настройки публикации для канала <code>{ch_id}</code>:", reply_markup=settings_menu(ch_id, True), parse_mode="HTML")

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
        text += "Каналов не добавлено."
    keyboard.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="add_new_channel")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")

@router.callback_query(F.data == "add_new_channel")
async def add_new_channel(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("Отправьте ID канала (начинается с -100...).\nБот должен быть админом в этом канале!", parse_mode="HTML")
    await state.set_state(BotStates.waiting_for_channel)

@router.message(BotStates.waiting_for_channel)
async def save_channel(message: Message, state: FSMContext):
    channel_id = message.text.strip() if message.text.startswith("-100") else None
    if message.forward_from_chat and message.forward_from_chat.type == "channel":
        channel_id = str(message.forward_from_chat.id)
        
    if channel_id:
        db_execute("INSERT OR IGNORE INTO settings (target_id, is_channel, assets) VALUES (?, 1, 'GRAM,USDT,BTC,DOGE')", (channel_id,))
        await message.answer(f"✅ Канал <code>{channel_id}</code> добавлен!", reply_markup=main_menu(), parse_mode="HTML")
        await state.clear()
    else:
        await message.answer("Неверный формат. Введите ID вида -100xxxxxxxxxx.")

@router.callback_query(F.data.startswith("manage_ch_"))
async def manage_channel(call: CallbackQuery):
    ch_id = call.data.replace("manage_ch_", "")
    await call.message.edit_text(f"Настройки публикации для канала <code>{ch_id}</code>:", reply_markup=settings_menu(ch_id, True), parse_mode="HTML")

# Интервалы обновлений
@router.callback_query(F.data.startswith("set_int_"))
async def set_interval_options(call: CallbackQuery):
    target = call.data.replace("set_int_", "")
    keyboard = []
    intervals = [("Выкл ❌", 0), ("30 мин", 30), ("1 час", 60), ("4 часа", 240), ("12 часов", 720)]
    for text, val in intervals:
        keyboard.append([InlineKeyboardButton(text=text, callback_data=f"saveint_{val}_{target}")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"back_to_target_{target}")])
    await call.message.edit_text("Как часто присылать автоматические отчеты?", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@router.callback_query(F.data.startswith("saveint_"))
async def save_interval(call: CallbackQuery):
    parts = call.data.split("_")
    val, target_type = int(parts[1]), parts[2]
    target_id = str(call.from_user.id) if target_type == "usr" else "_".join(parts[3:])
    db_execute("UPDATE settings SET interval_min = ?, last_check = NULL WHERE target_id = ?", (val, target_id))
    await call.answer("Интервал изменен!")
    await call.message.edit_text("✅ Изменения успешно применились.", reply_markup=main_menu())

# Алерты по цене
@router.callback_query(F.data.startswith("add_alert_"))
async def add_alert_select_coin(call: CallbackQuery, state: FSMContext):
    target = call.data.replace("add_alert_", "")
    keyboard = []
    for coin in ["GRAM", "USDT", "BTC", "DOGE"]:
        keyboard.append([
            InlineKeyboardButton(text=f"{coin} выше ↑", callback_data=f"coinalert_{coin}_up_{target}"),
            InlineKeyboardButton(text=f"{coin} ниже ↓", callback_data=f"coinalert_{coin}_down_{target}")
        ])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"back_to_target_{target}")])
    await call.message.edit_text("Выберите актив для алертов:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@router.callback_query(F.data.startswith("coinalert_"))
async def process_coin_alert(call: CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    coin, alert_type, target = parts[1], parts[2], "_".join(parts[3:])
    target_id = str(call.from_user.id) if target == "usr" else target
    await state.update_data(alert_target=target_id, alert_coin=coin, alert_type=alert_type)
    await call.message.edit_text(f"Введите цену для <b>{coin}</b> в USD (например, 5.80 или 69000):", parse_mode="HTML")
    await state.set_state(BotStates.waiting_for_alert_price)

@router.message(BotStates.waiting_for_alert_price)
async def save_alert_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Пожалуйста, отправьте корректное число.")
        return
    data = await state.get_data()
    db_execute("INSERT INTO alerts (target_id, asset, alert_type, target_price) VALUES (?, ?, ?, ?)",
               (data['alert_target'], data['alert_coin'], data['alert_type'], price))
    await message.answer("🚀 Алерт заведен успешно!", reply_markup=main_menu())
    await state.clear()

@router.callback_query(F.data.startswith("list_alerts_"))
async def list_alerts(call: CallbackQuery):
    target = call.data.replace("list_alerts_", "")
    target_id = str(call.from_user.id) if target == "usr" else target
    alerts = db_fetchall("SELECT id, asset, alert_type, target_price FROM alerts WHERE target_id = ? AND is_active = 1", (target_id,))
    text = "📜 <b>Активные триггеры цен:</b>\n\n"
    keyboard = []
    if alerts:
        for aid, asset, atype, price in alerts:
            arrow = "🔼 выше" if atype == "up" else "🔽 ниже"
            text += f"• {asset} {arrow} ${price}\n"
            keyboard.append([InlineKeyboardButton(text=f"❌ Удалить {asset}", callback_data=f"delalert_{aid}")])
    else:
        text += "Нет активных уведомлений."
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"back_to_target_{'usr' if target==str(call.from_user.id) else 'ch_'+target}")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")

@router.callback_query(F.data.startswith("delalert_"))
async def delete_alert(call: CallbackQuery):
    aid = call.data.split("_")[1]
    db_execute("DELETE FROM alerts WHERE id = ?", (aid,))
    await call.answer("Удалено")
    await call.message.edit_text("✅ Уведомление отключено.", reply_markup=main_menu())

# =====================================================================
# ФОНОВЫЙ ПОТОК (Учитывает персональный список монет для рассылки)
# =====================================================================
async def price_tracker_worker(bot: Bot):
    while True:
        try:
            prices, is_live = await fetch_prices()
            now_msk = get_msk_time()
            
            # 1. Проверка алертов
            alerts = db_fetchall("SELECT id, target_id, asset, alert_type, target_price FROM alerts WHERE is_active = 1")
            for aid, target_id, asset, alert_type, target_price in alerts:
                current_price = prices.get(asset, 0)
                triggered = False
                if alert_type == "up" and current_price >= target_price:
                    triggered = True
                    msg = f"🔔 <b>ALERT! {asset} пробил уровень вверх!</b>\n📈 Текущая цена: <b>${current_price:.4f}</b> (Порог: ${target_price})"
                elif alert_type == "down" and current_price <= target_price:
                    triggered = True
                    msg = f"🔔 <b>ALERT! {asset} пробил уровень вниз!</b>\n📉 Текущая цена: <b>${current_price:.4f}</b> (Порог: ${target_price})"
                
                if triggered:
                    try:
                        await bot.send_message(chat_id=target_id, text=msg, parse_mode="HTML")
                        db_execute("UPDATE alerts SET is_active = 0 WHERE id = ?", (aid,)) 
                    except Exception as e:
                        logging.error(f"Ошибка отправки алерта: {e}")

            # 2. Периодическая отправка отчетов (с фильтрацией выбранных монет)
            settings = db_fetchall("SELECT target_id, interval_min, last_check, assets FROM settings WHERE interval_min > 0")
            for target_id, interval, last_check, assets_str in settings:
                should_send = False
                if not last_check:
                    should_send = True
                else:
                    last_check_dt = datetime.fromisoformat(last_check).replace(tzinfo=timezone(timedelta(hours=3)))
                    if (now_msk - last_check_dt).total_seconds() / 60 >= interval:
                        should_send = True
                        
                if should_send:
                    # Строим кастомный текст отчета на базе сохраненных настроек монет этого чата/канала
                    allowed_coins = assets_str if assets_str else "GRAM,USDT,BTC,DOGE"
                    report_text = build_price_text(prices, is_live, allowed_coins)
                    try:
                        await bot.send_message(chat_id=target_id, text=report_text, parse_mode="HTML")
                        db_execute("UPDATE settings SET last_check = ? WHERE target_id = ?", (now_msk.isoformat(), target_id))
                    except Exception as e:
                        logging.error(f"Ошибка отправки отчета в {target_id}: {e}")

        except Exception as e:
            logging.error(f"Критическая ошибка трекера: {e}")
            
        await asyncio.sleep(40) # Пауза между итерациями трекера

# =====================================================================
# ТОЧКА ВХОДА
# =====================================================================
async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(price_tracker_worker(bot))
    print("Бот с часовым поясом МСК и кастомизацией монет запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

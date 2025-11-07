#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SaleTest_full.py
Полный Telegram-бот + CryptoBot интеграция.
- Хранение заказов в SQLite
- Профиль пользователя (список заказов, отмена)
- Админ-панель (просмотр/смена статуса)
- Создание чеков в CryptoBot (выбор валюты: RUB, USD, EUR, USDT, TON, TRX, MATIC и т.д.)
- Обработка IPN от CryptoBot (webhook /cryptobot/ipn)
- Telegram webhook endpoint /<BOT_TOKEN> (для Render/Railway/Heroku)
- Генерация QR для pay_url
- Всегда полный код — не сокращаю.

Инструкция кратко:
1) Вставь BOT_TOKEN (от BotFather) и CRYPTOPAY_API_TOKEN (от CryptoBot).
2) Если используешь Render/Railway: выставь USE_WEBHOOK=1 и WEB_DOMAIN=https://твой-домен
3) Заливай на хостинг, перезапускай. На локали можно запустить в режиме polling (см. переменную RUN_LOCAL_POLLING).
"""

import os
import json
import sqlite3
import requests
import qrcode
import io
import time
from datetime import datetime
from typing import Optional, Dict, Any, List

from flask import Flask, request, jsonify, send_file
import telebot
from telebot import types

# ----------------------- НАСТРОЙКИ (вставь свои значения) -----------------------

# Telegram Bot token (от BotFather) — обязательно вставь свой рабочий токен
BOT_TOKEN = os.environ.get("BOT_TOKEN") or "8587164094:AAEcsW0oUMg1Hphbymdg3NHtH_Q25j7RyWo"

# CryptoBot API token (API key) — обязательно вставь
CRYPTOPAY_API_TOKEN = os.environ.get("CRYPTOPAY_API_TOKEN") or "484313:AA6FJU50A2cMhJas5ruR6PD15Jl5F1XMrN7"

# Админ Telegram ID (для уведомлений и админ-панели)
ADMIN_ID = int(os.environ.get("ADMIN_ID") or 1942740947)

# Домен приложения (для webhook CryptoBot callback и для Telegram webhook)
# Пример: https://my-app.onrender.com
WEB_DOMAIN = os.environ.get("WEB_DOMAIN") or ""  # <--- обязательно установи когда деплоишь

# Установи в 1 чтобы бот использовал Telegram webhook (для Render/Railway)
USE_WEBHOOK = os.environ.get("USE_WEBHOOK", "0") == "1"

# Локальный режим: если True, бот будет работать через polling (для локальной отладки)
RUN_LOCAL_POLLING = os.environ.get("RUN_LOCAL_POLLING", "0") == "1"

# Путь к базе данных SQLite
DB_FILE = os.environ.get("DB_FILE") or "sale_bot.db"

# Путь до файла логов IPN optionally (не обязателен)
IPN_LOG_FILE = os.environ.get("IPN_LOG_FILE") or "ipn_log.jsonl"

# CryptoBot API base
CRYPTO_API_BASE = "https://pay.crypt.bot/api"

# Список доступных валют/asset для создания инвойса (показываем пользователю)
# Пользователь выбирает одну из них — мы передаём это как 'asset' в createInvoice.
AVAILABLE_ASSETS = [
    ("RUB", "Российский рубль (RUB)"),
    ("USD", "Доллар США (USD)"),
    ("EUR", "Евро (EUR)"),
    ("USDT", "USDT (Tether)"),
    ("TON", "TON"),
    ("TRX", "TRON (TRX)"),
    ("MATIC", "Polygon (MATIC)")
]

# Предустановленные пакеты и цены (в рублях). При создании инвойса можно указать нужную asset.
OFFERS = {
    "sub": {"100": 100, "500": 400, "1000": 700},
    "view": {"1000": 50, "5000": 200, "10000": 350},
    "com": {"50": 150, "200": 500},
}
PRETTY = {"sub": "Подписчики", "view": "Просмотры", "com": "Комментарии"}

# OPTIONAL: функция конвертации: рубли -> выбранная валюта
# Примечание: реальную конвертацию обычно делает CryptoBot (или вы можете использовать курсы).
# Здесь для простоты мы используем фиксированный примерный обменный курс (демо).
EXAMPLE_RUB_TO_USD = 100.0  # 100 RUB = 1 USD (пример). При необходимости подключи API курса.
# -----------------------------------------------------------------------------

# ----------------------- Инициализация Flask и Bot -----------------------
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN)
# -----------------------------------------------------------------------------

# ----------------------- БД: SQLite helpers -----------------------
def get_db_conn():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        created_at TEXT
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        category TEXT,
        amount INTEGER,
        price REAL,
        currency TEXT,
        status TEXT,
        invoice_id TEXT,
        pay_url TEXT,
        created_at TEXT,
        updated_at TEXT
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS invoices_map (
        invoice_id TEXT PRIMARY KEY,
        chat_id INTEGER,
        order_id INTEGER,
        raw_payload TEXT,
        created_at TEXT
    );
    """)
    conn.commit()
    conn.close()

init_db()
# -----------------------------------------------------------------------------

# ----------------------- Утилиты для работы с БД -----------------------
def ensure_user(chat_id: int, message: Optional[telebot.types.Message]=None):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    if not row and message is not None:
        cur.execute("INSERT OR IGNORE INTO users (chat_id, username, first_name, last_name, created_at) VALUES (?, ?, ?, ?, ?)",
                    (chat_id, message.from_user.username if message.from_user else None,
                     message.from_user.first_name if message.from_user else None,
                     message.from_user.last_name if message.from_user else None,
                     datetime.utcnow().isoformat()))
        conn.commit()
    conn.close()

def create_order_record(chat_id: int, category: str, amount: int, price: float, currency: str) -> int:
    now = datetime.utcnow().isoformat()
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""INSERT INTO orders (chat_id, category, amount, price, currency, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (chat_id, category, amount, price, currency, "ожидает оплаты", now, now))
    order_id = cur.lastrowid
    conn.commit()
    conn.close()
    return order_id

def set_invoice_mapping(invoice_id: str, chat_id: int, order_id: int, raw_payload: Optional[str]=None):
    now = datetime.utcnow().isoformat()
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""INSERT OR REPLACE INTO invoices_map (invoice_id, chat_id, order_id, raw_payload, created_at)
                   VALUES (?, ?, ?, ?, ?)""", (invoice_id, chat_id, order_id, raw_payload, now))
    conn.commit()
    conn.close()

def update_order_invoice(order_id: int, invoice_id: Optional[str], pay_url: Optional[str]):
    now = datetime.utcnow().isoformat()
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE orders SET invoice_id = ?, pay_url = ?, updated_at = ? WHERE id = ?",
                (invoice_id, pay_url, now, order_id))
    conn.commit()
    conn.close()

def update_order_status_db(order_id: int, new_status: str):
    now = datetime.utcnow().isoformat()
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?", (new_status, now, order_id))
    conn.commit()
    conn.close()

def get_user_orders(chat_id: int) -> List[Dict[str, Any]]:
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE chat_id = ? ORDER BY id DESC", (chat_id,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def get_order_by_id(order_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    r = cur.fetchone()
    conn.close()
    return dict(r) if r else None

def get_invoice_map(invoice_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM invoices_map WHERE invoice_id = ?", (invoice_id,))
    r = cur.fetchone()
    conn.close()
    return dict(r) if r else None

# -----------------------------------------------------------------------------

# ----------------------- CryptoBot API helpers -----------------------
def create_cryptobot_invoice(amount_value: float, asset: str, payload: str, description: str, callback_url: Optional[str]=None) -> Dict[str, Any]:
    """
    Создаёт инвойс в CryptoBot.
    Вход:
      - amount_value: сумма в выбранной asset (строкой или числом). Например '1.23'
      - asset: строка из AVAILABLE_ASSETS кода, например 'TON', 'USDT', 'RUB', 'USD'
      - payload: наша доп.строка, например "<chat>_<order_id>"
      - description: описание
      - callback_url: url, куда CryptoBot пришлёт IPN (опционально)
    Возвращает JSON-ответ или {'error': True, ...}
    """
    url = f"{CRYPTO_API_BASE}/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTOPAY_API_TOKEN, "Content-Type": "application/json"}
    data = {
        "amount": str(amount_value),
        "asset": asset,
        "payload": str(payload),
        "description": description
    }
    if callback_url:
        data["callback"] = callback_url
    try:
        r = requests.post(url, json=data, headers=headers, timeout=20)
        try:
            j = r.json()
        except Exception:
            return {"error": True, "status_code": r.status_code, "body": r.text}
        if r.status_code not in (200, 201):
            return {"error": True, "status_code": r.status_code, "body": j}
        return j
    except Exception as e:
        return {"error": True, "exception": str(e)}

def get_invoice_info(invoice_id: str) -> Dict[str, Any]:
    url = f"{CRYPTO_API_BASE}/getInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTOPAY_API_TOKEN}
    try:
        r = requests.get(url, headers=headers, params={"invoiceId": invoice_id}, timeout=15)
        return r.json()
    except Exception as e:
        return {"error": True, "exception": str(e)}
# -----------------------------------------------------------------------------

# ----------------------- Helpers: QR generation -----------------------
def generate_qr_bytes(url: str) -> bytes:
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()

# -----------------------------------------------------------------------------

# ----------------------- Telegram UI helpers -----------------------
def main_menu_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("📈 Купить подписчиков", callback_data="menu_sub"))
    kb.add(types.InlineKeyboardButton("👁 Купить просмотры", callback_data="menu_view"))
    kb.add(types.InlineKeyboardButton("💬 Купить комментарии", callback_data="menu_com"))
    kb.add(types.InlineKeyboardButton("👤 Профиль", callback_data="profile"))
    if ADMIN_ID:
        kb.add(types.InlineKeyboardButton("🔐 Админ", callback_data="admin_panel"))
    return kb

def packages_markup(cat_key: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    for amt, price in OFFERS.get(cat_key, {}).items():
        kb.add(types.InlineKeyboardButton(f"{amt} — {price}₽", callback_data=f"order_{cat_key}_{amt}"))
    kb.add(types.InlineKeyboardButton("✏ Своя сумма", callback_data=f"custom_{cat_key}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
    return kb

def currency_selection_markup(order_ref: str) -> types.InlineKeyboardMarkup:
    """order_ref - any id that helps идентифицировать заказ (например chat_orderid)"""
    kb = types.InlineKeyboardMarkup(row_width=2)
    for code, desc in AVAILABLE_ASSETS:
        kb.add(types.InlineKeyboardButton(f"{code}", callback_data=f"pay_asset_{order_ref}_{code}"))
    # add back
    kb.add(types.InlineKeyboardButton("🔙 Отмена", callback_data="cancel_payment"))
    return kb

# -----------------------------------------------------------------------------

# ----------------------- Telegram handlers -----------------------
@bot.message_handler(commands=["start"])
def handler_start(message: types.Message):
    ensure_user(message.chat.id, message)
    bot.send_message(message.chat.id, "🧸 Добро пожаловать! Выберите услугу:", reply_markup=main_menu_keyboard())

@bot.callback_query_handler(func=lambda c: True)
def handler_callback(call: types.CallbackQuery):
    chat_id = call.message.chat.id
    data_call = call.data

    # МЕНЮ
    if data_call == "menu_sub":
        bot.edit_message_text("📈 Выберите пакет подписчиков:", chat_id, call.message.message_id, reply_markup=packages_markup("sub"))
        return
    if data_call == "menu_view":
        bot.edit_message_text("👁 Выберите пакет просмотров:", chat_id, call.message.message_id, reply_markup=packages_markup("view"))
        return
    if data_call == "menu_com":
        bot.edit_message_text("💬 Выберите пакет комментариев:", chat_id, call.message.message_id, reply_markup=packages_markup("com"))
        return
    if data_call == "back":
        bot.edit_message_text("🧸 Главное меню:", chat_id, call.message.message_id, reply_markup=main_menu_keyboard())
        return

    if data_call == "profile":
        show_profile(chat_id)
        return

    if data_call == "admin_panel":
        if chat_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "Нет прав")
            return
        show_admin_panel(chat_id)
        return

    # fixed package order
    if data_call.startswith("order_"):
        # format: order_{category}_{amt}
        try:
            _, category, amt = data_call.split("_", 2)
            amount = int(amt)
        except Exception:
            bot.answer_callback_query(call.id, "Ошибка в данных заказа")
            return
        # compute price in RUB from OFFERS
        price_rub = OFFERS.get(category, {}).get(str(amount), amount)
        # create local order with no invoice yet
        order_id = create_order_record(chat_id, category, amount, price_rub, "RUB")
        # ask user to choose currency/asset to pay
        order_ref = f"{chat_id}_{order_id}"
        bot.send_message(chat_id, f"Заказ создан #{order_id}. Выберите валюту для оплаты:", reply_markup=currency_selection_markup(order_ref))
        bot.answer_callback_query(call.id, "Заказ создан — выберите валюту")
        return

    # custom amount
    if data_call.startswith("custom_"):
        category = data_call.replace("custom_", "")
        max_offer = max(int(x) for x in OFFERS.get(category, {}).keys()) if OFFERS.get(category) else 0
        min_allowed = max_offer + 1 if max_offer else 1
        # save state in DB? we'll use a simple memory state per chat in this file: data_user_input
        data_user_input[chat_id] = {"waiting_custom": True, "category": category, "min_allowed": min_allowed}
        bot.send_message(chat_id, f"Введите количество для {PRETTY.get(category)} (целое, минимум {min_allowed}):")
        bot.answer_callback_query(call.id)
        return

    # cancel payment
    if data_call == "cancel_payment":
        bot.send_message(chat_id, "Оплата отменена.", reply_markup=main_menu_keyboard())
        bot.answer_callback_query(call.id)
        return

    # payment by asset selected
    if data_call.startswith("pay_asset_"):
        # format pay_asset_{chat}_{orderid}_{ASSET}
        try:
            _, order_ref, asset = data_call.split("_", 2)
        except Exception:
            # older format: pay_asset_{order_ref}_{asset}
            parts = data_call.split("_")
            if len(parts) >= 4:
                order_ref = parts[2]
                asset = parts[3]
            else:
                bot.answer_callback_query(call.id, "Bad pay data")
                return
        # order_ref was constructed as chat_orderid
        try:
            chat_str, orderid_str = order_ref.split("_", 1)
            order_chat = int(chat_str); order_id = int(orderid_str)
        except Exception:
            bot.answer_callback_query(call.id, "Неверная ссылка на заказ")
            return
        order = get_order_by_id(order_id)
        if not order:
            bot.answer_callback_query(call.id, "Заказ не найден")
            return
        # Price logic: if stored price is in RUB, convert to target asset amount for the invoice.
        # For demo: if asset in fiat (RUB, USD, EUR) we'll pass amount equal to price in that currency (assumes OFFERS price in RUB)
        price_rub = float(order["price"])
        # Convert example: if asset == USD -> price_usd = price_rub / EXAMPLE_RUB_TO_USD
        if asset.upper() == "USD":
            pay_amount = round(price_rub / EXAMPLE_RUB_TO_USD, 2)
        elif asset.upper() == "RUB":
            pay_amount = round(price_rub, 2)
        else:
            # For crypto (USDT, TON, TRX, MATIC) — we will attempt to pass price in that asset as approximate:
            # use a naive approach: price_in_asset = price_rub / example_rate. For real use, integrate FX provider.
            # We'll use same EXAMPLE_RUB_TO_USD conversion for USDT and for TON leave same formula (demo).
            if asset.upper() in ("USDT", "TON", "TRX", "MATIC"):
                pay_amount = round(price_rub / EXAMPLE_RUB_TO_USD, 6)  # smaller decimals
            else:
                pay_amount = round(price_rub, 2)
        # create invoice via CryptoBot
        order_uid = f"{order_chat}_{order_id}"
        description = f"Заказ #{order_id} {order['category']} {order['amount']}"
        callback_url = (WEB_DOMAIN.rstrip("/") + "/cryptobot/ipn") if WEB_DOMAIN else None
        resp = create_cryptobot_invoice(pay_amount, asset.upper(), order_uid, description, callback_url=callback_url)
        if isinstance(resp, dict) and resp.get("error"):
            bot.send_message(chat_id, f"Ошибка при создании чека: {resp}")
            bot.answer_callback_query(call.id, "Ошибка при создании чека")
            return
        # Extract pay_url and invoice id (response structure may vary — handle variants)
        invoice_id = None
        pay_url = None
        # CryptoBot may return { "invoiceId": "...", "pay_url": "..." } or nested in result
        if resp.get("invoiceId"):
            invoice_id = resp.get("invoiceId")
        elif resp.get("result") and isinstance(resp["result"], dict):
            invoice_id = resp["result"].get("invoice_id") or resp["result"].get("invoiceId") or resp["result"].get("id")
            pay_url = resp["result"].get("pay_url") or resp["result"].get("payment_url") or resp["result"].get("url")
        # try top-level
        if not pay_url:
            pay_url = resp.get("pay_url") or resp.get("payment_url") or resp.get("url") or resp.get("invoice_url")
        # fallback: resp might include link under 'link' etc.
        if not invoice_id and isinstance(resp, dict):
            for v in ("invoice_id", "id", "paymentId", "invoiceId"):
                if resp.get(v):
                    invoice_id = resp.get(v)
                    break
        # Save mapping and update order
        if invoice_id:
            set_invoice_mapping(str(invoice_id), order_chat, order_id, raw_payload=json.dumps(resp))
        update_order_invoice(order_id, invoice_id, pay_url)
        # Send pay_url and QR
        if pay_url:
            bot.send_message(order_chat, f"Ссылка для оплаты: {pay_url}\nПосле оплаты придёт уведомление в этот чат.")
            try:
                qr_bytes = generate_qr_bytes(pay_url)
                bot.send_photo(order_chat, qr_bytes)
            except Exception:
                pass
            bot.answer_callback_query(call.id, "Ссылка отправлена")
            return
        else:
            bot.send_message(order_chat, "Ссылка на оплату не получена. Обратись к администратору.")
            bot.answer_callback_query(call.id, "Ссылка не получена")
            return

    # admin manage actions
    if data_call.startswith("admin_manage_"):
        if chat_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "Нет прав")
            return
        try:
            _, _, user_id_str, order_id_str = data_call.split("_")
            uid = int(user_id_str); oid = int(order_id_str)
        except Exception:
            bot.answer_callback_query(call.id, "Bad admin data")
            return
        # show admin action buttons
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"admin_set_{uid}_{oid}_Подтверждено"))
        kb.add(types.InlineKeyboardButton("🕒 В процессе", callback_data=f"admin_set_{uid}_{oid}_В процессе"))
        kb.add(types.InlineKeyboardButton("❌ Отменить", callback_data=f"admin_set_{uid}_{oid}_Отменён"))
        bot.send_message(chat_id, f"Управление заказом {uid}#{oid}", reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data_call.startswith("admin_set_"):
        if chat_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "Нет прав")
            return
        try:
            # admin_set_{uid}_{oid}_{new_status}
            parts = data_call.split("_", 4)
            _, _, user_id, order_id_str, new_status = parts
            uid = str(user_id); oid = int(order_id_str)
        except Exception:
            bot.answer_callback_query(call.id, "Bad admin set")
            return
        # update in DB
        update_order_status_db(oid, new_status)
        # notify user
        try:
            bot.send_message(int(uid), f"🔔 Статус вашего заказа #{oid} изменён: {new_status}")
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Статус изменён")
        return

    # cancel order from user
    if data_call.startswith("cancel_"):
        try:
            _, order_id_str = data_call.split("_", 1)
            oid = int(order_id_str)
        except Exception:
            bot.answer_callback_query(call.id, "Bad cancel")
            return
        order = get_order_by_id(oid)
        if not order:
            bot.answer_callback_query(call.id, "Заказ не найден")
            return
        if order["status"] == "оплачен":
            bot.answer_callback_query(call.id, "Нельзя отменить оплаченный заказ")
            return
        update_order_status_db(oid, "Отменён")
        bot.edit_message_text("Заказ отменён ✅", call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id, "Заказ отменён")
        # notify admin
        try:
            bot.send_message(ADMIN_ID, f"Пользователь {chat_id} отменил заказ #{oid}")
        except Exception:
            pass
        return

    # fallback
    bot.answer_callback_query(call.id, "Неизвестная команда")

# -----------------------------------------
# In-memory user input state (simple)
data_user_input: Dict[int, Dict[str, Any]] = {}

@bot.message_handler(func=lambda m: True)
def handler_text(message: types.Message):
    chat_id = message.chat.id
    text = (message.text or "").strip()

    # If waiting for custom amount
    state = data_user_input.get(chat_id)
    if state and state.get("waiting_custom"):
        category = state.get("category")
        min_allowed = state.get("min_allowed", 1)
        if not text.isdigit():
            bot.send_message(chat_id, "Нужно ввести целое число. Попробуй снова.")
            return
        amount = int(text)
        if amount < min_allowed:
            bot.send_message(chat_id, f"Минимум для этой категории: {min_allowed}. Попробуй снова.")
            return
        # create order, similar to fixed package flow
        price_rub = round(amount * 1.0, 2)  # if you want, compute custom formula for price
        order_id = create_order_record(chat_id, category, amount, price_rub, "RUB")
        order_ref = f"{chat_id}_{order_id}"
        bot.send_message(chat_id, f"Заказ #{order_id} создан. Выберите валюту для оплаты:", reply_markup=currency_selection_markup(order_ref))
        data_user_input.pop(chat_id, None)
        return

    # profile commands
    if text.lower() in ("/profile", "профиль", "мои заказы"):
        show_profile(chat_id)
        return

    # admin command
    if text.lower().startswith("/admin") and chat_id == ADMIN_ID:
        show_admin_panel(chat_id)
        return

    # short fallback
    bot.send_message(chat_id, "Не понял. Наберите /start чтобы начать.", reply_markup=None)

# ----------------------- Profile and admin -----------------------
def show_profile(chat_id: int):
    ensure_user(chat_id)
    orders = get_user_orders(chat_id)
    if not orders:
        bot.send_message(chat_id, "📭 У вас пока нет заказов.", reply_markup=main_menu_keyboard())
        return
    text_lines = []
    kb = types.InlineKeyboardMarkup(row_width=1)
    for o in orders:
        text_lines.append(f"#{o['id']} | {PRETTY.get(o['category'], o['category'])} — {o['amount']} — {o['price']} {o['currency']} — {o['status']}")
        if o['status'] not in ("Отменён", "оплачен"):
            kb.add(types.InlineKeyboardButton(f"Отменить #{o['id']}", callback_data=f"cancel_{o['id']}"))
    bot.send_message(chat_id, "📋 Ваши заказы:\n\n" + "\n".join(text_lines), reply_markup=kb)

def show_admin_panel(chat_id: int):
    if chat_id != ADMIN_ID:
        bot.send_message(chat_id, "Нет прав")
        return
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 200")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        bot.send_message(chat_id, "Нет заказов")
        return
    text = "📋 Все заказы (последние 200):\n\n"
    kb = types.InlineKeyboardMarkup(row_width=1)
    for r in rows:
        text += f"UID {r['chat_id']} | #{r['id']} | {PRETTY.get(r['category'], r['category'])} {r['amount']} | {r['price']} {r['currency']} | {r['status']}\n"
        kb.add(types.InlineKeyboardButton(f"Управлять {r['chat_id']}#{r['id']}", callback_data=f"admin_manage_{r['chat_id']}_{r['id']}"))
    bot.send_message(chat_id, text, reply_markup=kb)

# ----------------------- Flask endpoints: CryptoBot IPN -----------------------
@app.route("/cryptobot/ipn", methods=["POST"])
def cryptobot_ipn():
    """
    Обработчик уведомлений от CryptoBot (IPN).
    CryptoBot обычно POST JSON с полями invoiceId, status, payload и др.
    Мы:
      - логируем payload
      - ищем invoiceId (или payload как order UID)
      - помечаем заказ в БД как оплачен если статус указывает на paid
    """
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "bad json"}), 400

    # log raw
    try:
        with open(IPN_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": datetime.utcnow().isoformat(), "payload": payload}, ensure_ascii=False) + "\n")
    except Exception:
        pass

    # try to find invoice id and status
    invoice_id = None
    for k in ("invoiceId", "invoice_id", "id", "paymentId", "payment_id"):
        if k in payload:
            invoice_id = str(payload[k])
            break

    status_field = None
    for k in ("status", "paymentStatus", "payment_status", "state"):
        if k in payload:
            status_field = payload[k]
            break

    order_uid = payload.get("payload") or payload.get("order") or payload.get("comment") or payload.get("merchant_order_id")

    # normalize status string
    st = str(status_field).lower() if status_field else ""

    paid_indicators = {"paid", "success", "finished", "confirmed", "complete"}
    if any(p in st for p in paid_indicators):
        # First try to map invoice_id -> order via invoices_map
        if invoice_id:
            mapping = get_invoice_map(invoice_id)
            if mapping:
                try:
                    chat_id = int(mapping["chat_id"])
                    order_id = int(mapping["order_id"])
                    update_order_status_db(order_id, "оплачен")
                    # notify user
                    try:
                        bot.send_message(chat_id, f"🔔 Платёж подтверждён. Заказ #{order_id} помечен как оплачен.")
                    except Exception:
                        pass
                    return jsonify({"ok": True}), 200
                except Exception:
                    pass
        # else try parse order_uid like "<chat>_<orderid>"
        if order_uid and isinstance(order_uid, str) and "_" in order_uid:
            try:
                parts = order_uid.split("_")
                chat = int(parts[0]); oid = int(parts[1])
                update_order_status_db(oid, "оплачен")
                try:
                    bot.send_message(chat, f"🔔 Платёж подтверждён. Заказ #{oid} помечен как оплачен.")
                except Exception:
                    pass
                return jsonify({"ok": True}), 200
            except Exception:
                pass

    # если пришёл другой статус — сохраняем в лог и возвращаем ok
    return jsonify({"ok": True}), 200

# ----------------------- Telegram webhook endpoint for hosting -----------------------
@app.route("/" + BOT_TOKEN, methods=["POST"])
def telegram_webhook_handler():
    # Telegram will POST updates here
    json_str = request.get_data().decode("utf-8")
    try:
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception as e:
        print("Error processing Telegram webhook:", e)
    return "OK", 200

@app.route("/", methods=["GET"])
def index():
    return "Bot is running", 200

# ----------------------- On-startup helpers -----------------------
def set_telegram_webhook_if_needed():
    if not USE_WEBHOOK:
        print("USE_WEBHOOK not set; skipping setting Telegram webhook.")
        return
    if not WEB_DOMAIN:
        print("WEB_DOMAIN not set; cannot set Telegram webhook.")
        return
    webhook_url = WEB_DOMAIN.rstrip("/") + "/" + BOT_TOKEN
    try:
        bot.remove_webhook()
        time.sleep(0.5)
        ok = bot.set_webhook(url=webhook_url)
        print("Set webhook to", webhook_url, "result:", ok)
    except Exception as e:
        print("Failed to set Telegram webhook:", e)

# -----------------------------------------------------------------------------
# ENTRYPOINT
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # If running locally in debugging, you may want polling mode instead of webhook.
    print("Starting SaleTest_full.py")
    print("DB file:", DB_FILE)
    init_db()
    if RUN_LOCAL_POLLING:
        print("Running in local polling mode.")
        # start Flask in background thread and polling in main thread (local dev)
        from threading import Thread
        t = Thread(target=lambda: app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False), daemon=True)
        t.start()
        print("Flask started on port 5000 locally; starting polling...")
        bot.infinity_polling(timeout=60, long_polling_timeout=20)
    else:
        # production-style: use webhook (hosting) OR still allow polling if USE_WEBHOOK is False
        set_telegram_webhook_if_needed()
        # run Flask app (hosting will use WSGI to run it; this block useful when running directly)
        print("Starting Flask (will handle Telegram webhook at /<BOT_TOKEN> and CryptoBot IPN at /cryptobot/ipn).")
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
# -----------------------------------------------------------------------------

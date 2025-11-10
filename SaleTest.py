# SaleTest.py
# Полный развёрнутый Telegram-бот + CryptoBot + SQLite + многопользовательская поддержка
# --------------------------------------------------------------------------
# Вставь свои значения:
#   BOT_TOKEN              - токен от @BotFather
#   CRYPTOPAY_API_TOKEN    - токен от @CryptoBot (если есть)
#   WEB_DOMAIN             - https://your-app.onrender.com (для webhook и IPN)
# Затем: pip install -r requirements.txt (pyTelegramBotAPI Flask requests qrcode pillow)
# Procfile (для Render/Railway): web: python3 SaleTest.py
# --------------------------------------------------------------------------

import os
import json
import sqlite3
import requests
import qrcode
import io
import time
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

from flask import Flask, request, jsonify, send_file
import telebot
from telebot import types

# ----------------------- НАСТРОЙКИ -----------------------
BOT_TOKEN = "8587164094:AAEcsW0oUMg1Hphbymdg3NHtH_Q25j7RyWo"

CRYPTOPAY_API_TOKEN = os.environ.get("CRYPTOPAY_API_TOKEN") or "484313:AA6FJU50A2cMhJas5ruR6PD15Jl5F1XMrN7"  # вставь свой Cryptobot API token, если хочешь
# Список юзернеймов внешней поддержки, разделённый запятой (показываются в меню)
SUPPORT_USERNAMES = os.environ.get("SUPPORT_USERNAMES") or "@Urikossan"
# Список admin IDs (по умолчанию один). Можно добавить агентов в БД.
DEFAULT_ADMIN_ID = int(os.environ.get("ADMIN_ID") or 1942740947)
WEB_DOMAIN = os.environ.get("WEB_DOMAIN") or "https://render-jj8d.onrender.com"  # например https://my-app.onrender.com
USE_WEBHOOK = os.environ.get("USE_WEBHOOK", "0") == "1"
RUN_LOCAL_POLLING = os.environ.get("RUN_LOCAL_POLLING", "0") == "1"
DB_FILE = os.environ.get("DB_FILE") or "sale_bot_full.db"
IPN_LOG_FILE = os.environ.get("IPN_LOG_FILE") or "ipn_log.jsonl"
CRYPTO_API_BASE = "https://pay.crypt.bot/api"
# Валюты
AVAILABLE_ASSETS = [
    ("RUB", "Рубль (RUB)"),
    ("USD", "Доллар (USD)"),
    ("EUR", "Евро (EUR)"),
    ("USDT", "USDT (Tether)"),
    ("TON", "TON"),
    ("TRX", "TRX"),
    ("MATIC", "MATIC"),
]
# Офферы (цены в рублях)
OFFERS = {
    "sub": {"100": 100, "500": 400, "1000": 700},
    "view": {"1000": 50, "5000": 200, "10000": 350},
    "com": {"50": 150, "200": 500},
}
PRETTY = {"sub": "Подписчики", "view": "Просмотры", "com": "Комментарии"}
# Примерный курс RUB->USD (демо), лучше подключить реальный курс при необходимости
EXAMPLE_RUB_TO_USD = 100.0
# -------------------------------------------------------------------

app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

# ----------------------- SQLite init -----------------------
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
    )
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
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS invoices_map (
        invoice_id TEXT PRIMARY KEY,
        chat_id INTEGER,
        order_id INTEGER,
        raw_payload TEXT,
        created_at TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS support_agents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_chat_id INTEGER UNIQUE,
        username TEXT,
        display_name TEXT,
        created_at TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS support_conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_chat_id INTEGER,
        title TEXT,
        status TEXT,
        created_at TEXT,
        updated_at TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS support_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conv_id INTEGER,
        from_chat_id INTEGER,
        to_chat_id INTEGER,
        direction TEXT,
        message_type TEXT,
        text TEXT,
        tg_message_id INTEGER,
        file_id TEXT,
        raw_payload TEXT,
        created_at TEXT
    )
    """)
    conn.commit()
    conn.close()

init_db()

# ----------------------- DB helpers -----------------------
def ensure_user(chat_id: int, message: Optional[telebot.types.Message]=None):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,))
    r = cur.fetchone()
    if not r and message is not None:
        cur.execute("INSERT OR IGNORE INTO users (chat_id, username, first_name, last_name, created_at) VALUES (?, ?, ?, ?, ?)",
                    (chat_id, getattr(message.from_user, "username", None),
                     getattr(message.from_user, "first_name", None),
                     getattr(message.from_user, "last_name", None),
                     datetime.utcnow().isoformat()))
        conn.commit()
    conn.close()

def create_order_record(chat_id:int, category:str, amount:int, price:float, currency:str) -> int:
    now = datetime.utcnow().isoformat()
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""INSERT INTO orders (chat_id, category, amount, price, currency, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (chat_id, category, amount, price, currency, "ожидает оплаты", now, now))
    oid = cur.lastrowid
    conn.commit()
    conn.close()
    return oid

def update_order_invoice(order_id:int, invoice_id:Optional[str], pay_url:Optional[str]):
    now = datetime.utcnow().isoformat()
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE orders SET invoice_id = ?, pay_url = ?, updated_at = ? WHERE id = ?", (invoice_id, pay_url, now, order_id))
    conn.commit()
    conn.close()

def update_order_status_db(order_id:int, new_status:str):
    now = datetime.utcnow().isoformat()
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?", (new_status, now, order_id))
    conn.commit()
    conn.close()

def set_invoice_mapping(invoice_id:str, chat_id:int, order_id:int, raw_payload:Optional[str]=None):
    now = datetime.utcnow().isoformat()
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO invoices_map (invoice_id, chat_id, order_id, raw_payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (invoice_id, chat_id, order_id, raw_payload, now))
    conn.commit()
    conn.close()

def get_invoice_map(invoice_id:str):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM invoices_map WHERE invoice_id = ?", (invoice_id,))
    r = cur.fetchone()
    conn.close()
    return dict(r) if r else None

def get_order_by_id(order_id:int):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    r = cur.fetchone()
    conn.close()
    return dict(r) if r else None

def get_user_orders(chat_id:int):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE chat_id = ? ORDER BY id DESC", (chat_id,))
    rows = [dict(x) for x in cur.fetchall()]
    conn.close()
    return rows

# Support helpers
def add_support_agent(admin_chat_id:int, username:Optional[str]=None, display_name:Optional[str]=None):
    now = datetime.utcnow().isoformat()
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO support_agents (admin_chat_id, username, display_name, created_at) VALUES (?, ?, ?, ?)",
                (admin_chat_id, username, display_name, now))
    conn.commit()
    conn.close()

def remove_support_agent(admin_chat_id:int):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM support_agents WHERE admin_chat_id = ?", (admin_chat_id,))
    conn.commit()
    conn.close()

def list_support_agents():
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM support_agents")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def create_support_conversation(user_chat_id:int, title:str="Запрос в поддержку"):
    now = datetime.utcnow().isoformat()
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO support_conversations (user_chat_id, title, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (user_chat_id, title, "open", now, now))
    conv_id = cur.lastrowid
    conn.commit()
    conn.close()
    return conv_id

def add_support_message(conv_id:int, from_chat:int, to_chat:int, direction:str, message_type:str, text:Optional[str]=None, tg_message_id:Optional[int]=None, file_id:Optional[str]=None, raw_payload:Optional[str]=None):
    now = datetime.utcnow().isoformat()
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""INSERT INTO support_messages (conv_id, from_chat_id, to_chat_id, direction, message_type, text, tg_message_id, file_id, raw_payload, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (conv_id, from_chat, to_chat, direction, message_type, text, tg_message_id, file_id, raw_payload, now))
    mid = cur.lastrowid
    cur.execute("UPDATE support_conversations SET updated_at = ? WHERE id = ?", (now, conv_id))
    conn.commit()
    conn.close()
    return mid

def get_or_create_open_conv_for_user(user_chat_id:int):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM support_conversations WHERE user_chat_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1", (user_chat_id,))
    r = cur.fetchone()
    if r:
        conv_id = r["id"]
    else:
        conv_id = create_support_conversation(user_chat_id)
    conn.close()
    return conv_id

# ----------------------- CryptoBot helpers -----------------------
def create_cryptobot_invoice(amount_value:float, asset:str, payload:str, description:str, callback_url:Optional[str]=None):
    url = CRYPTO_API_BASE + "/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTOPAY_API_TOKEN, "Content-Type": "application/json"}
    payload_body = {
        "amount": str(amount_value),
        "asset": asset,
        "payload": str(payload),
        "description": description
    }
    if callback_url:
        payload_body["callback"] = callback_url
    try:
        r = requests.post(url, json=payload_body, headers=headers, timeout=20)
        try:
            j = r.json()
        except Exception:
            return {"error": True, "status_code": r.status_code, "body": r.text}
        if r.status_code not in (200, 201):
            return {"error": True, "status_code": r.status_code, "body": j}
        return j
    except Exception as e:
        return {"error": True, "exception": str(e)}

def get_invoice_info(invoice_id:str):
    url = CRYPTO_API_BASE + "/getInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTOPAY_API_TOKEN}
    try:
        r = requests.get(url, headers=headers, params={"invoiceId": invoice_id}, timeout=15)
        return r.json()
    except Exception as e:
        return {"error": True, "exception": str(e)}

# ----------------------- QR helper -----------------------
def generate_qr_bytes(url:str) -> bytes:
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()

# ----------------------- UI helpers -----------------------
def main_menu_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("📈 Купить подписчиков", callback_data="menu_sub"))
    kb.add(types.InlineKeyboardButton("👁 Купить просмотры", callback_data="menu_view"))
    kb.add(types.InlineKeyboardButton("💬 Купить комментарии", callback_data="menu_com"))
    kb.add(types.InlineKeyboardButton("👤 Профиль", callback_data="profile"))
    # External support usernames (first displayed)
    if SUPPORT_USERNAMES:
        # show first username as clickable link button
        first = SUPPORT_USERNAMES.split(",")[0].strip()
        kb.add(types.InlineKeyboardButton(f"📨 Написать {first}", url=f"https://t.me/{first.lstrip('@')}"))
    # internal support via bot
    kb.add(types.InlineKeyboardButton("📞 Связаться с поддержкой (в боте)", callback_data="contact_support"))
    # admin shortcut (for owners)
    kb.add(types.InlineKeyboardButton("🔐 Админ", callback_data="admin_panel"))
    return kb

def packages_markup(cat_key):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for amt, price in OFFERS.get(cat_key, {}).items():
        kb.add(types.InlineKeyboardButton(f"{amt} — {price}₽", callback_data=f"order_{cat_key}_{amt}"))
    kb.add(types.InlineKeyboardButton("✏ Своя сумма", callback_data=f"custom_{cat_key}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
    return kb

def currency_selection_markup(order_ref:str):
    kb = types.InlineKeyboardMarkup(row_width=3)
    for code, desc in AVAILABLE_ASSETS:
        kb.add(types.InlineKeyboardButton(f"{code}", callback_data=f"pay_asset_{order_ref}_{code}"))
    kb.add(types.InlineKeyboardButton("🔙 Отмена", callback_data="cancel_payment"))
    return kb

# ----------------------- Bot handlers -----------------------
@bot.message_handler(commands=["start"])
def cmd_start(m):
    ensure_user(m.chat.id, m)
    bot.send_message(m.chat.id, "🧸 Добро пожаловать! Выберите услугу:", reply_markup=main_menu_keyboard())

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    cid = call.message.chat.id
    data_call = call.data

    if data_call == "menu_sub":
        bot.edit_message_text("📈 Выберите пакет подписчиков:", cid, call.message.message_id, reply_markup=packages_markup("sub"))
        return
    if data_call == "menu_view":
        bot.edit_message_text("👁 Выберите пакет просмотров:", cid, call.message.message_id, reply_markup=packages_markup("view"))
        return
    if data_call == "menu_com":
        bot.edit_message_text("💬 Выберите пакет комментариев:", cid, call.message.message_id, reply_markup=packages_markup("com"))
        return
    if data_call == "back":
        bot.edit_message_text("🧸 Главное меню:", cid, call.message.message_id, reply_markup=main_menu_keyboard())
        return
    if data_call == "profile":
        show_profile(cid)
        return
    if data_call == "contact_support":
        # start support conversation and forward to agents
        conv_id = get_or_create_open_conv_for_user(cid)
        bot.send_message(cid, "✉️ Напишите ваше сообщение для поддержки. Мы пересылаем его агентам поддержки.")
        # set user state
        user_state[cid] = {"awaiting_support_msg": True, "conv_id": conv_id}
        bot.answer_callback_query(call.id)
        return
    if data_call.startswith("order_"):
        try:
            _, category, amt = data_call.split("_", 2)
            amount = int(amt)
        except Exception:
            bot.answer_callback_query(call.id, "Ошибка в данных заказа.")
            return
        price_rub = OFFERS.get(category, {}).get(amt, amount)
        order_id = create_order_record(cid, category, amount, price_rub, "RUB")
        order_ref = f"{cid}_{order_id}"
        bot.send_message(cid, f"Заказ #{order_id} создан. Выберите валюту для оплаты:", reply_markup=currency_selection_markup(order_ref))
        bot.answer_callback_query(call.id, "Заказ создан.")
        return
    if data_call.startswith("custom_"):
        category = data_call.replace("custom_", "")
        max_offer = max((int(x) for x in OFFERS.get(category, {}).keys())) if OFFERS.get(category) else 0
        min_allowed = max_offer + 1 if max_offer else 1
        user_state[cid] = {"awaiting_custom_amount": True, "category": category, "min_allowed": min_allowed}
        bot.send_message(cid, f"Введите количество для {PRETTY.get(category)} (минимум {min_allowed}):")
        bot.answer_callback_query(call.id)
        return
    if data_call == "cancel_payment":
        bot.send_message(cid, "Оплата отменена.", reply_markup=main_menu_keyboard())
        bot.answer_callback_query(call.id)
        return
    if data_call.startswith("pay_asset_"):
        # format: pay_asset_{chat}_{orderid}_{ASSET}
        parts = data_call.split("_", 3)
        if len(parts) == 4:
            _, order_ref, asset = parts[0], parts[1], parts[3]
        else:
            # older format: pay_asset_{orderref}_{asset}
            try:
                _, _, order_ref, asset = parts
            except:
                bot.answer_callback_query(call.id, "Bad pay data")
                return
        # order_ref is like "chatid_orderid" or "chatid_orderid"
        if "_" in order_ref:
            try:
                chat_str, orderid_str = order_ref.split("_", 1)
                order_chat = int(chat_str); order_id = int(orderid_str)
            except:
                bot.answer_callback_query(call.id, "Неверный идентификатор заказа")
                return
        else:
            bot.answer_callback_query(call.id, "Неверный формат заказа")
            return
        order = get_order_by_id(order_id)
        if not order:
            bot.answer_callback_query(call.id, "Заказ не найден")
            return
        price_rub = float(order["price"])
        # convert to asset amount (demo logic)
        if asset.upper() == "USD":
            pay_amount = round(price_rub / EXAMPLE_RUB_TO_USD, 2)
        elif asset.upper() == "RUB":
            pay_amount = round(price_rub, 2)
        else:
            pay_amount = round(price_rub / EXAMPLE_RUB_TO_USD, 6)
        order_uid = f"{order_chat}_{order_id}"
        description = f"Заказ #{order_id} {order['category']} {order['amount']}"
        callback_url = (WEB_DOMAIN.rstrip("/") + "/cryptobot/ipn") if WEB_DOMAIN else None
        resp = create_cryptobot_invoice(pay_amount, asset.upper(), order_uid, description, callback_url=callback_url)
        if isinstance(resp, dict) and resp.get("error"):
            bot.send_message(order_chat, f"Ошибка при создании чека: {resp}")
            bot.answer_callback_query(call.id, "Ошибка при создании чека")
            return
        # extract invoice id and pay url (handle response variants)
        invoice_id = None
        pay_url = None
        if isinstance(resp, dict):
            invoice_id = resp.get("invoiceId") or resp.get("invoice_id") or (resp.get("result") and resp["result"].get("invoice_id")) or None
            pay_url = resp.get("pay_url") or resp.get("payment_url") or (resp.get("result") and resp["result"].get("pay_url")) or resp.get("url") or resp.get("invoice_url")
        if invoice_id:
            set_invoice_mapping(str(invoice_id), order_chat, order_id, raw_payload=json.dumps(resp, ensure_ascii=False))
        update_order_invoice(order_id, invoice_id, pay_url)
        if pay_url:
            bot.send_message(order_chat, f"Перейдите по ссылке для оплаты:\n{pay_url}")
            try:
                qr = generate_qr_bytes(pay_url)
                bot.send_photo(order_chat, qr)
            except Exception:
                pass
            bot.answer_callback_query(call.id, "Ссылка отправлена")
            return
        else:
            bot.send_message(order_chat, "Ссылка на оплату не получена. Обратитесь к администратору.")
            bot.answer_callback_query(call.id, "Ссылка не получена")
            return
    if data_call.startswith("cancel_"):
        try:
            _, oid = data_call.split("_", 1)
            oid = int(oid)
        except:
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
        # notify admins (agents)
        for agent in list_support_agents():
            try:
                bot.send_message(agent["admin_chat_id"], f"Пользователь {cid} отменил заказ #{oid}")
            except:
                pass
        return
    if data_call.startswith("admin_manage_"):
        if cid != DEFAULT_ADMIN_ID:
            bot.answer_callback_query(call.id, "Нет прав")
            return
        _, _, user_id, order_id = data_call.split("_")
        uid = int(user_id); oid = int(order_id)
        # show options
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"admin_set_{uid}_{oid}_Подтверждено"))
        kb.add(types.InlineKeyboardButton("🕒 В процессе", callback_data=f"admin_set_{uid}_{oid}_В процессе"))
        kb.add(types.InlineKeyboardButton("❌ Отменить", callback_data=f"admin_set_{uid}_{oid}_Отменён"))
        bot.send_message(cid, f"Управление заказом {uid}#{oid}", reply_markup=kb)
        bot.answer_callback_query(call.id)
        return
    if data_call.startswith("admin_set_"):
        if cid != DEFAULT_ADMIN_ID:
            bot.answer_callback_query(call.id, "Нет прав")
            return
        try:
            _, _, user_id, order_id_str, new_status = data_call.split("_", 4)
            uid = int(user_id); oid = int(order_id_str)
        except:
            bot.answer_callback_query(call.id, "Bad admin set")
            return
        update_order_status_db(oid, new_status)
        try:
            bot.send_message(uid, f"🔔 Статус вашего заказа #{oid} обновлён: {new_status}")
        except:
            pass
        bot.answer_callback_query(call.id, "Статус изменён")
        return
    # support agent reply action: admin clicks "reply_to_conv_{conv_id}_{user_chat}"
    if data_call.startswith("reply_to_conv_"):
        parts = data_call.split("_", 3)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Bad reply")
            return
        _, _, conv_id_s = parts[0], parts[1], parts[2]
        try:
            conv_id = int(conv_id_s)
        except:
            bot.answer_callback_query(call.id, "Bad conv id")
            return
        # set agent state: awaiting reply for conv
        agent_state[cid] = {"awaiting_reply_conv": conv_id}
        bot.send_message(cid, "Введите ответ, я отправлю его пользователю (можно прикрепить файлы).")
        bot.answer_callback_query(call.id)
        return

    bot.answer_callback_query(call.id, "Неизвестная команда.")

# In-memory states for users and agents (temporary)
user_state: Dict[int, Dict[str, Any]] = {}
agent_state: Dict[int, Dict[str, Any]] = {}

@bot.message_handler(content_types=['text', 'photo', 'document', 'audio', 'voice', 'video'])
def inbound_message(m):
    cid = m.chat.id
    text = getattr(m, "text", None)
    # if user is sending custom amount
    ustate = user_state.get(cid)
    if ustate and ustate.get("awaiting_custom_amount"):
        category = ustate["category"]
        min_allowed = ustate["min_allowed"]
        if not text or not text.isdigit():
            bot.send_message(cid, "Введите целое число.")
            return
        amount = int(text)
        if amount < min_allowed:
            bot.send_message(cid, f"Минимум {min_allowed}. Попробуйте ещё раз.")
            return
        price_rub = round(amount * 1.0, 2)
        order_id = create_order_record(cid, category, amount, price_rub, "RUB")
        user_state.pop(cid, None)
        order_ref = f"{cid}_{order_id}"
        bot.send_message(cid, f"Заказ #{order_id} создан. Выберите валюту для оплаты:", reply_markup=currency_selection_markup(order_ref))
        return
    # if user is sending support message
    ustate = user_state.get(cid)
    if ustate and ustate.get("awaiting_support_msg"):
        conv_id = ustate["conv_id"]
        # create support message and forward to all agents
        # detect file types
        file_id = None
        msg_type = "text"
        raw = None
        if m.content_type == "text":
            msg_text = m.text
        else:
            msg_text = f"<{m.content_type}>"
            msg_type = m.content_type
            # try get file id for photo/document/audio/voice
            if m.content_type == "photo":
                file_id = m.photo[-1].file_id
            elif m.content_type == "document":
                file_id = m.document.file_id
            elif m.content_type == "voice":
                file_id = m.voice.file_id
            elif m.content_type == "audio":
                file_id = m.audio.file_id
            elif m.content_type == "video":
                file_id = m.video.file_id
        add_support_message(conv_id, cid, None, "user->agents", msg_type, text=msg_text, file_id=file_id, raw_payload=None)
        user_state.pop(cid, None)
        # forward to agents
        agents = list_support_agents()
        if not agents:
            bot.send_message(cid, "Нет агентов поддержки. Попробуйте позже.")
            return
        for a in agents:
            aid = int(a["admin_chat_id"])
            try:
                if file_id:
                    # forward file
                    if msg_type == "photo":
                        bot.send_photo(aid, file_id, caption=f"Новый запрос от {cid} (conv {conv_id})\n{msg_text}")
                    elif msg_type == "document":
                        bot.send_document(aid, file_id, caption=f"Новый запрос от {cid} (conv {conv_id})\n{msg_text}")
                    elif msg_type == "voice":
                        bot.send_voice(aid, file_id, caption=f"Новый запрос от {cid} (conv {conv_id})")
                    else:
                        bot.send_message(aid, f"Новый запрос от {cid} (conv {conv_id})\n{msg_text}")
                else:
                    bot.send_message(aid, f"Новый запрос от {cid} (conv {conv_id})\n{msg_text}")
                # add button for agent to reply
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("Ответить", callback_data=f"reply_to_conv_{conv_id}"))
                kb.add(types.InlineKeyboardButton("Перейти к беседе", callback_data=f"open_conv_{conv_id}"))
                bot.send_message(aid, "Действия:", reply_markup=kb)
            except Exception:
                pass
        bot.send_message(cid, "✅ Ваше сообщение отправлено агентам поддержки. Ожидайте ответа.")
        return
    # if agent is replying (agent_state)
    astate = agent_state.get(cid)
    if astate and astate.get("awaiting_reply_conv"):
        conv_id = astate["awaiting_reply_conv"]
        # get conv info
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM support_conversations WHERE id = ?", (conv_id,))
        conv = cur.fetchone()
        conn.close()
        if not conv:
            bot.send_message(cid, "Разговор не найден.")
            agent_state.pop(cid, None)
            return
        user_chat = conv["user_chat_id"]
        # forward agent message to user (support via bot)
        file_id = None
        msg_type = "text"
        if m.content_type == "text":
            reply_text = m.text
            bot.send_message(user_chat, f"Ответ поддержки:\n{reply_text}")
            add_support_message(conv_id, cid, user_chat, "agent->user", "text", text=reply_text)
        else:
            msg_type = m.content_type
            if msg_type == "photo":
                file_id = m.photo[-1].file_id
                bot.send_photo(user_chat, file_id, caption="Ответ поддержки (фото)")
            elif msg_type == "document":
                file_id = m.document.file_id
                bot.send_document(user_chat, file_id, caption="Ответ поддержки (файл)")
            elif msg_type == "voice":
                file_id = m.voice.file_id
                bot.send_voice(user_chat, file_id, caption="Ответ поддержки (голос)")
            elif msg_type == "audio":
                file_id = m.audio.file_id
                bot.send_audio(user_chat, file_id, caption="Ответ поддержки (аудио)")
            add_support_message(conv_id, cid, user_chat, "agent->user", msg_type, file_id=file_id)
        bot.send_message(cid, "Ответ отправлен пользователю.")
        agent_state.pop(cid, None)
        return

    # general fallback
    bot.send_message(cid, "Не понял. Нажми /start для меню.")

# ----------------------- Profile and admin commands -----------------------
def show_profile(chat_id:int):
    ensure_user(chat_id)
    orders = get_user_orders(chat_id)
    if not orders:
        bot.send_message(chat_id, "📭 У вас пока нет заказов.", reply_markup=main_menu_keyboard())
        return
    text = "📋 Ваши заказы:\n\n"
    kb = types.InlineKeyboardMarkup(row_width=1)
    for o in orders:
        text += f"#{o['id']} | {PRETTY.get(o['category'],o['category'])} — {o['amount']} — {o['price']} {o['currency']} — {o['status']}\n"
        if o['status'] not in ("Отменён", "оплачен"):
            kb.add(types.InlineKeyboardButton(f"Отменить #{o['id']}", callback_data=f"cancel_{o['id']}"))
    bot.send_message(chat_id, text, reply_markup=kb)

def main_menu_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("📈 Купить подписчиков", callback_data="menu_sub"))
    kb.add(types.InlineKeyboardButton("👁 Купить просмотры", callback_data="menu_view"))
    kb.add(types.InlineKeyboardButton("💬 Купить комментарии", callback_data="menu_com"))
    kb.add(types.InlineKeyboardButton("👤 Профиль", callback_data="profile"))
    if SUPPORT_USERNAMES:
        first = SUPPORT_USERNAMES.split(",")[0].strip()
        kb.add(types.InlineKeyboardButton(f"📨 Написать {first}", url=f"https://t.me/{first.lstrip('@')}"))
    kb.add(types.InlineKeyboardButton("📞 Связаться с поддержкой (в боте)", callback_data="contact_support"))
    if DEFAULT_ADMIN_ID:
        kb.add(types.InlineKeyboardButton("🔐 Админ", callback_data="admin_panel"))
    return kb

# ----------------------- Flask endpoints -----------------------
@app.route("/cryptobot/ipn", methods=["POST"])
def cryptobot_ipn():
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "bad json"}), 400
    # log
    try:
        with open(IPN_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": datetime.utcnow().isoformat(), "payload": payload}, ensure_ascii=False) + "\n")
    except:
        pass
    invoice_id = None
    for k in ("invoiceId","invoice_id","id","paymentId","payment_id"):
        if k in payload:
            invoice_id = str(payload[k]); break
    status_field = None
    for k in ("status","paymentStatus","payment_status","state"):
        if k in payload:
            status_field = payload[k]; break
    order_uid = payload.get("payload") or payload.get("order") or payload.get("comment") or payload.get("merchant_order_id")
    st = str(status_field).lower() if status_field else ""
    paid_indicators = {"paid","success","finished","confirmed","complete"}
    if any(p in st for p in paid_indicators):
        if invoice_id:
            mapping = get_invoice_map(invoice_id)
            if mapping:
                try:
                    chat_id = int(mapping["chat_id"]); order_id = int(mapping["order_id"])
                    update_order_status_db(order_id, "оплачен")
                    try:
                        bot.send_message(chat_id, f"🔔 Платёж подтверждён. Заказ #{order_id} помечен как оплачен.")
                    except:
                        pass
                    return jsonify({"ok": True}), 200
                except:
                    pass
        if order_uid and isinstance(order_uid,str) and "_" in order_uid:
            try:
                parts = order_uid.split("_"); chat = int(parts[0]); oid = int(parts[1])
                update_order_status_db(oid, "оплачен")
                try:
                    bot.send_message(chat, f"🔔 Платёж подтверждён. Заказ #{oid} помечен оплачен.")
                except:
                    pass
                return jsonify({"ok": True}), 200
            except:
                pass
    return jsonify({"ok": True}), 200

@app.route("/" + BOT_TOKEN, methods=["POST"])
def telegram_webhook():
    json_str = request.get_data().decode("utf-8")
    try:
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception as e:
        print("Webhook error:", e)
    return "OK", 200

@app.route("/", methods=["GET"])
def index():
    return "Bot is running", 200

# ----------------------- Startup -----------------------
def set_telegram_webhook_if_needed():
    if not USE_WEBHOOK:
        print("USE_WEBHOOK not set; skipping Telegram webhook setup.")
        return
    if not WEB_DOMAIN:
        print("WEB_DOMAIN not set; cannot configure webhook.")
        return
    webhook_url = WEB_DOMAIN.rstrip("/") + "/" + BOT_TOKEN
    try:
        bot.remove_webhook()
        time.sleep(0.5)
        res = bot.set_webhook(url=webhook_url)
        print("Set Telegram webhook to:", webhook_url, "result:", res)
    except Exception as e:
        print("Failed set webhook:", e)

# Run
if __name__ == "__main__":
    print("Starting SaleTest full service")
    init_db()
    # Create default admin in support_agents if not exist
    if DEFAULT_ADMIN_ID:
        add_support_agent(DEFAULT_ADMIN_ID, username=None, display_name="Owner")
    if RUN_LOCAL_POLLING:
        from threading import Thread
        t = Thread(target=lambda: app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False), daemon=True)
        t.start()
        print("Local Flask started on 5000; starting polling...")
        bot.infinity_polling(timeout=60, long_polling_timeout=20)
    else:
        set_telegram_webhook_if_needed()
        print("Starting Flask app (production)...")
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)))

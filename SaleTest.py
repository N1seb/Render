#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SaleTest.py
Полный Telegram-бот с магазином, CryptoBot-интеграцией, IPN, и полноценной поддержкой с операторами (SQLite).
Сохраняет всё в SQLite, операторы и обращения не теряются при перезапуске.
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

from flask import Flask, request, jsonify
import telebot
from telebot import types

# -------------------------
# НАСТРОЙКИ (вставлены твои значения по умолчанию)
# Можно переопределить через ENV переменные на Render
# -------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN") or "8587164094:AAEcsW0oUMg1Hphbymdg3NHtH_Q25j7RyWo"
CRYPTOPAY_API_TOKEN = os.environ.get("CRYPTOPAY_API_TOKEN") or "484313:AA6FJU50A2cMhJas5ruR6PD15Jl5F1XMrN7"
WEB_DOMAIN = os.environ.get("WEB_DOMAIN") or "https://render-jj8d.onrender.com"
USE_WEBHOOK = os.environ.get("USE_WEBHOOK", "0") == "1"

# Админ (у тебя)
ADMIN_IDS = set([int(os.environ.get("ADMIN_ID") or 1942740947)])
# Операторы инициализируются в БД; можно добавить первоначального оператора здесь:
INITIAL_OPERATORS = [7771789412]  # как ты просил — только этот ID сейчас опер

# CryptoBot base
CRYPTO_API_BASE = "https://pay.crypt.bot/api"

# Валюты (для выбора)
AVAILABLE_ASSETS = [
    ("RUB", "Рубль"),
    ("USD", "Доллар"),
    ("USDT", "USDT"),
    ("TON", "TON"),
    ("TRX", "TRX"),
    ("MATIC", "MATIC")
]

# Офферы
OFFERS = {
    "sub": {"100": 100, "500": 400, "1000": 700},
    "view": {"1000": 50, "5000": 200, "10000": 350},
    "com": {"50": 150, "200": 500},
}
PRETTY = {"sub": "Подписчики", "view": "Просмотры", "com": "Комментарии"}

DB_FILE = os.environ.get("DB_FILE") or "salebot.sqlite"
IPN_LOG_FILE = "ipn_log.jsonl"

# -------------------------
# Инициализация бота и фласка
# -------------------------
bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
app = Flask(__name__)

# -------------------------
# SQLite - инициализация
# -------------------------
def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    # users
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        created_at TEXT
    )
    """)
    # orders
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
    # invoices map
    cur.execute("""
    CREATE TABLE IF NOT EXISTS invoices_map (
        invoice_id TEXT PRIMARY KEY,
        chat_id INTEGER,
        order_id INTEGER,
        raw_payload TEXT,
        created_at TEXT
    )
    """)
    # operators
    cur.execute("""
    CREATE TABLE IF NOT EXISTS operators (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER UNIQUE,
        username TEXT,
        display_name TEXT,
        created_at TEXT
    )
    """)
    # support requests
    cur.execute("""
    CREATE TABLE IF NOT EXISTS support_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_chat INTEGER,
        username TEXT,
        text TEXT,
        status TEXT,
        created_at TEXT
    )
    """)
    # support messages history (optional)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS support_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        req_id INTEGER,
        from_chat INTEGER,
        to_chat INTEGER,
        text TEXT,
        created_at TEXT
    )
    """)
    # notifications mapping (operator -> last notification message id to update)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS operator_notifications (
        operator_chat INTEGER PRIMARY KEY,
        message_id INTEGER,
        created_at TEXT
    )
    """)
    conn.commit()
    conn.close()

init_db()

# -------------------------
# DB helper functions
# -------------------------
def ensure_user(chat_id: int, message: Optional[telebot.types.Message] = None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,))
    if not cur.fetchone() and message is not None:
        cur.execute("INSERT INTO users (chat_id, username, first_name, last_name, created_at) VALUES (?, ?, ?, ?, ?)",
                    (chat_id,
                     getattr(message.from_user, "username", None),
                     getattr(message.from_user, "first_name", None),
                     getattr(message.from_user, "last_name", None),
                     datetime.utcnow().isoformat()))
        conn.commit()
    conn.close()

def add_operator(chat_id:int, username:Optional[str]=None, display_name:Optional[str]=None):
    conn = get_db()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute("INSERT OR IGNORE INTO operators (chat_id, username, display_name, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, username, display_name, now))
    conn.commit()
    conn.close()
    # ensure notification row exists
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO operator_notifications (operator_chat, message_id, created_at) VALUES (?, ?, ?)",
                (chat_id, None, now))
    conn.commit()
    conn.close()

def remove_operator(chat_id:int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM operators WHERE chat_id = ?", (chat_id,))
    cur.execute("DELETE FROM operator_notifications WHERE operator_chat = ?", (chat_id,))
    conn.commit()
    conn.close()

def list_operators():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM operators ORDER BY id")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def create_order_record(chat_id:int, category:str, amount:int, price:float, currency:str) -> int:
    now = datetime.utcnow().isoformat()
    conn = get_db()
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
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE orders SET invoice_id = ?, pay_url = ?, updated_at = ? WHERE id = ?", (invoice_id, pay_url, now, order_id))
    conn.commit()
    conn.close()

def set_invoice_mapping(invoice_id:str, chat_id:int, order_id:int, raw_payload:Optional[str]=None):
    now = datetime.utcnow().isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO invoices_map (invoice_id, chat_id, order_id, raw_payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (invoice_id, chat_id, order_id, json.dumps(raw_payload, ensure_ascii=False) if raw_payload else None, now))
    conn.commit()
    conn.close()

def get_open_requests_count() -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM support_requests WHERE status = 'open'")
    r = cur.fetchone()
    conn.close()
    return r["c"] if r else 0

def create_support_request(user_chat:int, username:str, text:str) -> int:
    now = datetime.utcnow().isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO support_requests (user_chat, username, text, status, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_chat, username, text, "open", now))
    rid = cur.lastrowid
    cur.execute("INSERT INTO support_messages (req_id, from_chat, to_chat, text, created_at) VALUES (?, ?, ?, ?, ?)",
                (rid, user_chat, None, text, now))
    conn.commit()
    conn.close()
    return rid

def get_open_requests(offset:int=0, limit:int=10):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM support_requests WHERE status = 'open' ORDER BY id ASC LIMIT ? OFFSET ?", (limit, offset))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def get_request_by_id(req_id:int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM support_requests WHERE id = ?", (req_id,))
    r = cur.fetchone()
    conn.close()
    return dict(r) if r else None

def close_request(req_id:int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE support_requests SET status = 'closed' WHERE id = ?", (req_id,))
    conn.commit()
    conn.close()

def add_support_message(req_id:int, from_chat:int, to_chat:int, text:str):
    now = datetime.utcnow().isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO support_messages (req_id, from_chat, to_chat, text, created_at) VALUES (?, ?, ?, ?, ?)",
                (req_id, from_chat, to_chat, text, now))
    conn.commit()
    conn.close()

# -------------------------
# CryptoBot helpers
# -------------------------
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

# -------------------------
# QR helper
# -------------------------
def generate_qr_bytes(url:str) -> bytes:
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()

# -------------------------
# UI helpers
# -------------------------
def main_menu_markup():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("📈 Купить подписчиков", callback_data="menu_sub"))
    kb.add(types.InlineKeyboardButton("👁 Купить просмотры", callback_data="menu_view"))
    kb.add(types.InlineKeyboardButton("💬 Купить комментарии", callback_data="menu_com"))
    # support buttons: external personal messages and in-bot
    kb.add(types.InlineKeyboardButton("В личных сообщениях", callback_data="support_personal"))
    kb.add(types.InlineKeyboardButton("Написать в поддержку (в боте)", callback_data="support_bot"))
    kb.add(types.InlineKeyboardButton("👤 Профиль", callback_data="profile"))
    return kb

def packages_markup(cat_key):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for amt, price in OFFERS.get(cat_key, {}).items():
        kb.add(types.InlineKeyboardButton(f"{amt} — {price}₽", callback_data=f"order_{cat_key}_{amt}"))
    kb.add(types.InlineKeyboardButton("🔢 Своя сумма", callback_data=f"custom_{cat_key}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
    return kb

def currency_selection_markup(order_ref:str):
    kb = types.InlineKeyboardMarkup(row_width=3)
    for code, _ in AVAILABLE_ASSETS:
        chat_str, order_str = order_ref.split("_", 1)
        kb.add(types.InlineKeyboardButton(f"{code}", callback_data=f"pay_asset_{chat_str}_{order_str}_{code}"))

    kb.add(types.InlineKeyboardButton("🔙 Отмена", callback_data="cancel_payment"))
    return kb


def convert_price_rub_to_asset(price_rub: float, asset: str) -> float:
    """
    Конвертируем RUB -> asset (USDT/TON/CRYPTO)
    через публичный API coingecko
    """
    asset = asset.upper()

    # если оплата в RUB — ничего не конвертируем
    if asset == "RUB":
        return round(price_rub, 2)

    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": "tether,toncoin",
                "vs_currencies": "rub"
            },
            timeout=10
        )
        j = r.json()

        if asset == "USDT":
            rub_price = j["tether"]["rub"]
            return round(price_rub / rub_price, 6)

        if asset == "TON":
            rub_price = j["toncoin"]["rub"]
            return round(price_rub / rub_price, 6)

        # если крипты нет в API — fallback
        return round(price_rub / 100, 6)

    except Exception:
        # если API упал — безопасный fallback
        return round(price_rub / 100, 6)


# -------------------------
# In-memory states (short-lived)
# -------------------------
user_state: Dict[int, Dict[str, Any]] = {}   # for custom amount, support message etc.
operator_state: Dict[int, Dict[str, Any]] = {}  # for when operator is replying to a specific req

# -------------------------
# Bot handlers
# -------------------------
@bot.message_handler(commands=['start'])
def cmd_start(m):
    ensure_user(m.chat.id, m)
    bot.send_message(m.chat.id, "🧸 Добро пожаловать в магазин!", reply_markup=main_menu_markup())

@bot.callback_query_handler(func=lambda call: True)
def cb_all(call):
    data = call.data
    cid = call.message.chat.id
    # Menu navigation
    if data == "menu_sub" or data == "menu_view" or data == "menu_com":
        if data == "menu_sub":
            bot.edit_message_text("📈 Пакеты подписчиков:", cid, call.message.message_id, reply_markup=packages_markup("sub"))
        elif data == "menu_view":
            bot.edit_message_text("👁 Пакеты просмотров:", cid, call.message.message_id, reply_markup=packages_markup("view"))
        else:
            bot.edit_message_text("💬 Пакеты комментариев:", cid, call.message.message_id, reply_markup=packages_markup("com"))
        return

    if data == "back":
        bot.edit_message_text("🧸 Главное меню:", cid, call.message.message_id, reply_markup=main_menu_markup())
        return

    # support personal
    if data == "support_personal":
        # show simple instructions (no username)
        bot.edit_message_text("📨 Чтобы написать оператору в личные сообщения — найдите его в Telegram по его юзернейму.\n\nЕсли хотите написать через бота, нажмите «Написать в поддержку (в боте)».", cid, call.message.message_id, reply_markup=None)
        return

    # support via bot
    if data == "support_bot":
        conv_id = None
        # request the message from user
        user_state[cid] = {"awaiting_support_msg": True}
        bot.send_message(cid, "✉️ Напишите своё сообщение для поддержки. Оно будет отправлено операторам.")
        return

    # ordering fixed packages
    if data.startswith("order_"):
        try:
            _, category, amt = data.split("_", 2)
            amount = int(amt)
        except:
            bot.answer_callback_query(call.id, "Неверный формат заказа")
            return
        # ask for link next
        user_state[cid] = {"awaiting_link_for_order": True, "order_category": category, "order_amount": amount}
        bot.send_message(cid, f"Вы выбрали {PRETTY.get(category)} — {amount}. Отправьте ссылку на канал/пост (начинается с http...):")
        bot.answer_callback_query(call.id, "Введите ссылку для заказа")
        return

    # custom amount
    if data.startswith("custom_"):
        category = data.replace("custom_", "")
        max_offer = 0
        if OFFERS.get(category):
            max_offer = max(int(x) for x in OFFERS[category].keys())
        min_allowed = max_offer + 1 if max_offer else 1
        user_state[cid] = {"awaiting_custom_amount": True, "category": category, "min_allowed": min_allowed}
        bot.send_message(cid, f"Введите количество для {PRETTY.get(category)} (минимум {min_allowed}):")
        bot.answer_callback_query(call.id)
        return

    # currency pay button: format pay_asset_{chatid}_{orderid}_{ASSET}
    if data.startswith("pay_asset_"):
        try:
            _, chat_str, orderid_str, asset = data.split("_", 3)
            order_chat = int(chat_str)
            order_id = int(orderid_str)
        except:
            bot.answer_callback_query(call.id, "Неверный формат заказа")
            return

        # получить заказ
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            bot.answer_callback_query(call.id, "Заказ не найден")
            return

        price_rub = float(row["price"])
        pay_amount = convert_price_rub_to_asset(price_rub, asset.upper())

        order_uid = f"{order_chat}_{order_id}"
        description = f"Заказ #{order_id} {row['category']}"
        callback_url = (WEB_DOMAIN.rstrip("/") + "/cryptobot/ipn") if WEB_DOMAIN else None

        resp = create_cryptobot_invoice(pay_amount, asset.upper(), order_uid, description, callback_url=callback_url)

        if isinstance(resp, dict) and resp.get("error"):
            bot.send_message(order_chat, f"Ошибка при создании чека: {resp}")
            bot.answer_callback_query(call.id, "Ошибка")
            return

        invoice_id = resp.get("invoiceId") or resp.get("invoice_id") or resp.get("id")
        pay_url = (
            resp.get("pay_url")
            or resp.get("payment_url")
            or (resp.get("result", {}).get("pay_url") if isinstance(resp, dict) else None)
        )

        if invoice_id:
            set_invoice_mapping(str(invoice_id), order_chat, order_id, raw_payload=resp)
            update_order_invoice(order_id, str(invoice_id), pay_url)

        qr_bytes = generate_qr_bytes(pay_url)
        bot.send_photo(order_chat, qr_bytes, caption=f"💳 Оплата заказа #{order_id} через {asset.upper()}\nСсылка: {pay_url}")
        bot.answer_callback_query(call.id, "Счёт создан, проверьте сообщения")
        return

        # order_ref is like "chatid-orderid" or "chatid_orderid" — we will use chatid_orderid created below
# currency pay button: format pay_asset_{chatid}_{orderid}_{ASSET}
if data.startswith("pay_asset_"):
    try:
        _, chat_str, orderid_str, asset = data.split("_", 3)
        order_chat = int(chat_str)
        order_id = int(orderid_str)
    except:
        bot.answer_callback_query(call.id, "Неверный формат заказа")
        return

            try:
                order_chat = int(chat_str); order_id = int(orderid_str)
            except:
                bot.answer_callback_query(call.id, "Неверный идентификатор заказа")
                return
        else:
            bot.answer_callback_query(call.id, "Неверный формат заказа")
            return
        # fetch order
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            bot.answer_callback_query(call.id, "Заказ не найден")
            return
        price_rub = float(row["price"])
        # quick conversion demo (RUB->USD) - you can replace with real rates if needed
        EXAMPLE_RUB_TO_USD = 100.0
        pay_amount = convert_price_rub_to_asset(price_rub, asset.upper())

        order_uid = f"{order_chat}_{order_id}"
        description = f"Заказ #{order_id} {row['category']}"
        callback_url = (WEB_DOMAIN.rstrip("/") + "/cryptobot/ipn") if WEB_DOMAIN else None
        resp = create_cryptobot_invoice(pay_amount, asset.upper(), order_uid, description, callback_url=callback_url)
        if isinstance(resp, dict) and resp.get("error"):
            bot.send_message(order_chat, f"Ошибка при создании чека: {resp}")
            bot.answer_callback_query(call.id, "Ошибка")
            return
        # get invoice id and url
        invoice_id = resp.get("invoiceId") or resp.get("invoice_id") or resp.get("id") or None
        pay_url = resp.get("pay_url") or resp.get("payment_url") or resp.get("result", {}).get("pay_url") if isinstance(resp, dict) else None
        if invoice_id:
            set_invoice_mapping(str(invoice_id), order_chat, order_id, raw_payload=resp)
        update_order_invoice(order_id, invoice_id, pay_url)
        if pay_url:
            bot.send_message(order_chat, f"Перейдите для оплаты: {pay_url}")
            try:
                qr = generate_qr_bytes(pay_url)
                bot.send_photo(order_chat, qr)
            except:
                pass
            bot.answer_callback_query(call.id, "Ссылка отправлена")
            return
        else:
            bot.send_message(order_chat, "Не удалось получить ссылку на оплату. Обратитесь к администратору.")
            bot.answer_callback_query(call.id, "Ошибка получения ссылки")
            return

    if data == "cancel_payment":
        bot.send_message(cid, "Оплата отменена.", reply_markup=main_menu_markup())
        bot.answer_callback_query(call.id)
        return

    # profile show
    if data == "profile":
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE chat_id = ? ORDER BY id DESC", (cid,))
        rows = cur.fetchall()
        conn.close()
        if not rows:
            bot.send_message(cid, "У вас нет заказов.", reply_markup=main_menu_markup())
            return
        txt = "📋 Ваши заказы:\n\n"
        kb = types.InlineKeyboardMarkup(row_width=1)
        for r in rows:
            txt += f"#{r['id']} | {PRETTY.get(r['category'], r['category'])} {r['amount']} — {r['price']}{r['currency']} — {r['status']}\n"
            if r['status'] not in ("оплачен", "Отменён", "closed"):
                kb.add(types.InlineKeyboardButton(f"Отменить #{r['id']}", callback_data=f"cancel_{r['id']}"))
        bot.send_message(cid, txt, reply_markup=kb)
        return

    if data.startswith("cancel_"):
        try:
            _, oid = data.split("_",1)
            oid = int(oid)
        except:
            bot.answer_callback_query(call.id, "Ошибка отмены")
            return
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE id = ?", (oid,))
        r = cur.fetchone()
        if not r:
            bot.answer_callback_query(call.id, "Заказ не найден")
            conn.close()
            return
        if r["status"] == "оплачен":
            bot.answer_callback_query(call.id, "Нельзя отменить оплаченный заказ")
            conn.close()
            return
        cur.execute("UPDATE orders SET status = ? WHERE id = ?", ("Отменён", oid))
        conn.commit(); conn.close()
        bot.answer_callback_query(call.id, "Заказ отменён")
        bot.send_message(cid, f"Заказ #{oid} отменён.")
        return

    # --- Support operators actions: notification button pressed ---
    # open requests menu: callback open_requests_page_{page}
    if data.startswith("open_requests_page_"):
        try:
            page = int(data.split("_")[-1])
        except:
            page = 1
        show_requests_page(call.message.chat.id, page, call.message)
        bot.answer_callback_query(call.id)
        return

    # operator clicked a specific request: req_{id}
    if data.startswith("req_"):
        try:
            req_id = int(data.split("_")[1])
        except:
            bot.answer_callback_query(call.id, "Bad request id")
            return
        req = get_request_by_id(req_id)
        if not req or req["status"] != "open":
            bot.answer_callback_query(call.id, "Обращение не найдено или уже закрыто")
            return
        # show full text and reply button
        text = f"📨 Обращение #{req['id']}\nОт: {req['username']} (id {req['user_chat']})\n\n{req['text']}\n\n[Нажмите Ответить, чтобы отправить ответ и закрыть обращение]"
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("Ответить", callback_data=f"reply_req_{req_id}"))
        kb.add(types.InlineKeyboardButton("Назад к списку", callback_data="open_requests_page_1"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("reply_req_"):
        try:
            req_id = int(data.split("_")[2])
        except:
            bot.answer_callback_query(call.id, "Bad conv id")
            return
        # set operator state awaiting reply to this request
        operator_state[call.from_user.id] = {"awaiting_reply_for": req_id, "message_id": call.message.message_id}
        bot.send_message(call.from_user.id, "Введите ответ для пользователя (сообщение будет отправлено и обращение закроется):")
        bot.answer_callback_query(call.id)
        return

    bot.answer_callback_query(call.id, "Неизвестная команда.")

# -------------------------
# Support: utilities for operator notifications & pagination
# -------------------------
def notify_all_operators_new_request():
    # For each operator, either send new notification message or update existing one
    ops = list_operators()
    total = get_open_requests_count()
    if total == 0:
        return
    for op in ops:
        op_chat = op["chat_id"]
        # try to get existing notification message id
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT message_id FROM operator_notifications WHERE operator_chat = ?", (op_chat,))
        r = cur.fetchone()
        conn.close()
        notif_text = f"🔔 У вас новое обращение ({total} всего)"
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("Перейти к обращениям", callback_data=f"open_requests_page_1"))
        try:
            if r and r["message_id"]:
                # edit existing
                bot.edit_message_text(notif_text, op_chat, r["message_id"], reply_markup=kb)
            else:
                sent = bot.send_message(op_chat, notif_text, reply_markup=kb)
                # store message id
                conn = get_db()
                cur = conn.cursor()
                cur.execute("INSERT OR REPLACE INTO operator_notifications (operator_chat, message_id, created_at) VALUES (?, ?, ?)",
                            (op_chat, sent.message_id, datetime.utcnow().isoformat()))
                conn.commit()
                conn.close()
        except Exception as e:
            # operator might not have started bot or blocked -> ignore
            # ensure stored message_id removed so future sends try again
            try:
                conn = get_db()
                cur = conn.cursor()
                cur.execute("INSERT OR REPLACE INTO operator_notifications (operator_chat, message_id, created_at) VALUES (?, ?, ?)",
                            (op_chat, None, datetime.utcnow().isoformat()))
                conn.commit()
                conn.close()
            except:
                pass

def show_requests_page(operator_chat:int, page:int, message_reference=None):
    per_page = 3
    offset = (page-1) * per_page
    rows = get_open_requests(offset=offset, limit=per_page)
    total = get_open_requests_count()
    total_pages = (total + per_page - 1) // per_page if total else 1
    # build keyboard with requests
    kb = types.InlineKeyboardMarkup(row_width=1)
    for r in rows:
        uname = r["username"] or f"id{r['user_chat']}"
        kb.add(types.InlineKeyboardButton(f"{r['id']} | {uname}", callback_data=f"req_{r['id']}"))
    # navigation
    nav = types.InlineKeyboardMarkup(row_width=3)
    items = []
    prev_page = page-1 if page>1 else 1
    next_page = page+1 if page<total_pages else total_pages
    kb_nav = types.InlineKeyboardMarkup(row_width=3)
    kb_nav.add(types.InlineKeyboardButton("◀️", callback_data=f"open_requests_page_{prev_page}"),
               types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data=f"noop_page"),
               types.InlineKeyboardButton("▶️", callback_data=f"open_requests_page_{next_page}"))
    # send or edit
    txt = f"📂 Обращения — страница {page}/{total_pages}\nВсего открытых: {total}\n\nНажмите на обращение, чтобы открыть его."
    try:
        if message_reference:
            bot.edit_message_text(txt, operator_chat, message_reference.message_id, reply_markup=kb)
            # send navigation separately
            bot.send_message(operator_chat, "Навигация:", reply_markup=kb_nav)
        else:
            bot.send_message(operator_chat, txt, reply_markup=kb)
            bot.send_message(operator_chat, "Навигация:", reply_markup=kb_nav)
    except Exception as e:
        # if edit fails, send new
        try:
            bot.send_message(operator_chat, txt, reply_markup=kb)
            bot.send_message(operator_chat, "Навигация:", reply_markup=kb_nav)
        except:
            pass

# -------------------------
# Message handlers: text (orders, support, operator replies)
# -------------------------
@bot.message_handler(content_types=['text'])
def handle_text(m):
    cid = m.chat.id
    text = m.text.strip()

    # Admin commands: add/remove/check operators (only admin)
    if text.startswith("/add_operator"):
        if m.from_user.id not in ADMIN_IDS:
            bot.reply_to(m, "Нет прав.")
            return
        parts = text.split()
        if len(parts) != 2:
            bot.reply_to(m, "Использование: /add_operator <chat_id>")
            return
        try:
            new_id = int(parts[1])
        except:
            bot.reply_to(m, "Bad id")
            return
        add_operator(new_id, username=None, display_name=None)
        bot.reply_to(m, f"Добавлен оператор {new_id}")
        return

    if text.startswith("/operator_remove"):
        if m.from_user.id not in ADMIN_IDS:
            bot.reply_to(m, "Нет прав.")
            return
        parts = text.split()
        if len(parts) != 2:
            bot.reply_to(m, "Использование: /operator_remove <chat_id>")
            return
        try:
            rem = int(parts[1])
        except:
            bot.reply_to(m, "Bad id")
            return
        remove_operator(rem)
        bot.reply_to(m, f"Удалён оператор {rem}")
        return

    if text.startswith("/operators_check"):
        if m.from_user.id not in ADMIN_IDS:
            bot.reply_to(m, "Нет прав.")
            return
        ops = list_operators()
        if not ops:
            bot.reply_to(m, "Нет операторов.")
            return
        s = "Операторы:\n"
        for o in ops:
            s += f"- id: {o['chat_id']}, username: {o['username'] or '—'}, name: {o['display_name'] or '—'}\n"
        bot.reply_to(m, s)
        return

    # admin panel /sadm (only admin)
    if text.startswith("/sadm"):
        if m.from_user.id not in ADMIN_IDS:
            bot.reply_to(m, "Нет прав.")
            return
        # show last 10 orders
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, chat_id, category, amount, price, currency, status FROM orders ORDER BY id DESC LIMIT 10")
        rows = cur.fetchall()
        conn.close()
        if not rows:
            bot.reply_to(m, "Заказов нет.")
            return
        txt = "Последние 10 заказов:\n\n"
        for r in rows:
            txt += f"#{r['id']} uid:{r['chat_id']} {PRETTY.get(r['category'],r['category'])} {r['amount']} — {r['price']}{r['currency']} — {r['status']}\n"
        bot.reply_to(m, txt)
        return

    # If user awaiting custom amount
    state = user_state.get(cid)
    if state and state.get("awaiting_custom_amount"):
        if not text.isdigit():
            bot.reply_to(m, "Введите целое число.")
            return
        amount = int(text)
        if amount < state.get("min_allowed",1):
            bot.reply_to(m, f"Минимум {state.get('min_allowed')}")
            return
        # create order record price=amount * 1 (demo)
        price = float(amount)  # simple mapping, replace with your pricing logic
        order_id = create_order_record(cid, state["category"], amount, price, "RUB")
        user_state.pop(cid, None)
        bot.reply_to(m, f"Заказ #{order_id} создан. Выберите валюту для оплаты.", reply_markup=currency_selection_markup(f"{cid}_{order_id}"))
        return

    # If user awaiting link for order
    if state and state.get("awaiting_link_for_order"):
        link = text
        if not (link.startswith("http://") or link.startswith("https://")):
            bot.reply_to(m, "Неверная ссылка. Должна начинаться с http/https")
            return
        category = state["order_category"]
        amount = state["order_amount"]
        price = float(amount)  # demo
        order_id = create_order_record(cid, category, amount, price, "RUB")
        user_state.pop(cid, None)
        bot.reply_to(m, f"Заказ #{order_id} создан. Выберите валюту для оплаты.", reply_markup=currency_selection_markup(f"{cid}_{order_id}"))
        return

    # If user awaiting support message
    if state and state.get("awaiting_support_msg"):
        user_state.pop(cid, None)
        uname = m.from_user.username or f"id{cid}"
        rid = create_support_request(cid, uname, text)
        bot.reply_to(m, "✅ Ваше обращение отправлено. Ожидайте ответа.")
        # notify operators
        notify_all_operators_new_request()
        return

    # Operator replying to a request: operator_state holds awaiting_reply_for
    opstate = operator_state.get(cid)
    if opstate and opstate.get("awaiting_reply_for"):
        req_id = opstate["awaiting_reply_for"]
        # send message to user and close request
        req = get_request_by_id(req_id)
        if not req:
            bot.reply_to(m, "Обращение не найдено.")
            operator_state.pop(cid, None)
            return
        # send reply text to original user
        reply_text = text
        try:
            bot.send_message(req["user_chat"], f"💬 Ответ от поддержки:\n{reply_text}")
        except Exception as e:
            pass
        add_support_message(req_id, cid, req["user_chat"], reply_text)
        close_request(req_id)
        bot.reply_to(m, "Ответ отправлен и обращение закрыто.")
        operator_state.pop(cid, None)
        # update notifications for operators (counts)
        notify_all_operators_new_request()
        return

    # fallback
    bot.send_message(cid, "Не понял. Нажми /start для меню.")

# -------------------------
# Flask endpoints: CryptoBot IPN and Telegram webhook
# -------------------------
@app.route("/cryptobot/ipn", methods=["POST"])
def cryptobot_ipn():
    try:
        payload = request.get_json(force=True)
    except:
        return jsonify({"ok": False, "error": "bad json"}), 400
    # log payload
    try:
        with open(IPN_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": datetime.utcnow().isoformat(), "payload": payload}, ensure_ascii=False) + "\n")
    except:
        pass
    invoice_id = None
    for k in ("invoiceId","invoice_id","id"):
        if k in payload:
            invoice_id = str(payload[k]); break
    status_field = payload.get("status") or payload.get("paymentStatus") or payload.get("state")
    paid_indicators = {"paid","success","confirmed","finished","complete"}
    st = str(status_field).lower() if status_field else ""
    if any(p in st for p in paid_indicators):
        # try mapping by invoice id
        if invoice_id:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT * FROM invoices_map WHERE invoice_id = ?", (invoice_id,))
            r = cur.fetchone()
            conn.close()
            if r:
                try:
                    oid = int(r["order_id"])
                    # update orders table
                    conn = get_db()
                    cur = conn.cursor()
                    cur.execute("UPDATE orders SET status = ? WHERE id = ?", ("оплачен", oid))
                    conn.commit(); conn.close()
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
    return "Bot OK", 200

# -------------------------
# Startup: set webhook if needed
# -------------------------
def set_telegram_webhook():
    if not USE_WEBHOOK:
        print("USE_WEBHOOK not set; skipping Telegram webhook setup.")
        return
    if not WEB_DOMAIN:
        print("WEB_DOMAIN empty; cannot set webhook.")
        return
    webhook_url = WEB_DOMAIN.rstrip("/") + "/" + BOT_TOKEN
    try:
        bot.remove_webhook()
        time.sleep(0.5)
        res = bot.set_webhook(url=webhook_url)
        print("Webhook set:", webhook_url, "result:", res)
    except Exception as e:
        print("Failed set webhook:", e)

# -------------------------
# Ensure initial operators exist in DB
# -------------------------
for op in INITIAL_OPERATORS:
    add_operator(op)

# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    print("Starting SaleTest service")
    init_db()
    if USE_WEBHOOK:
        set_telegram_webhook()
        print("Running Flask (webhook mode)...")
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
    else:
        # for local testing
        from threading import Thread
        t = Thread(target=lambda: app.run(host="0.0.0.0", port=5000), daemon=True)
        t.start()
        print("Running polling (local)...")
        bot.infinity_polling(timeout=60, long_polling_timeout=20)

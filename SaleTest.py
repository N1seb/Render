#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SaleTest_full.py
Полный Telegram-бот: магазин + корзина + CryptoBot (USDT/TON/TRX) + операторы + поддержка.
SQLite хранение всех данных (корзина, операторы, заказы, привязка invoice -> заказ(ы)).
"""

import os
import json
import sqlite3
import requests
import qrcode
import io
import time
import traceback
from datetime import datetime
from threading import Thread
from typing import Optional, Dict, Any, List

from flask import Flask, request, jsonify
import telebot
from telebot import types

# -------------------------
# CONFIG (можно переопределять через ENV)
# -------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN") or "8587164094:AAEcsW0oUMg1Hphbymdg3NHtH_Q25j7RyWo"
CRYPTOPAY_API_TOKEN = os.environ.get("CRYPTOPAY_API_TOKEN") or "484313:AA6FJU50A2cMhJas5ruR6PD15Jl5F1XMrN7"
WEB_DOMAIN = os.environ.get("WEB_DOMAIN") or "https://render-jj8d.onrender.com"
USE_WEBHOOK = os.environ.get("USE_WEBHOOK", "0") == "1"
ADMIN_IDS = set([int(os.environ.get("ADMIN_ID") or 1942740947)])
INITIAL_OPERATORS = [7771789412]  # initial operator(s)

CRYPTO_API_BASE = "https://pay.crypt.bot/api"

DB_FILE = os.environ.get("DB_FILE") or "salebot.sqlite"
IPN_LOG_FILE = "ipn_log.jsonl"

# Allowed assets only (per request)
AVAILABLE_ASSETS = ["USDT", "TON", "TRX"]

# Social networks and available services (prices in USD).
# You said you pre-multiplied/adjusted — сюда можно подставить твой итоговый прайс.
# Сейчас это демонстрационный прайс (пример). Изменяй как нужно.
SERVICES_BY_SOCIAL = {
    "Instagram": {
        "sub": {"label": "Подписчики", "price_usd": 5.0},
        "view": {"label": "Просмотры", "price_usd": 1.0},
        "com": {"label": "Комментарии", "price_usd": 7.0},
        "react": {"label": "Реакции", "price_usd": 0.6},
    },
    "TikTok": {
        "sub": {"label": "Подписчики", "price_usd": 4.0},
        "view": {"label": "Просмотры", "price_usd": 0.6},
        "com": {"label": "Комментарии", "price_usd": 6.0},
        "react": {"label": "Реакции", "price_usd": 0.5},
    },
    "YouTube": {
        "sub": {"label": "Подписчики", "price_usd": 6.0},
        "view": {"label": "Просмотры", "price_usd": 1.2},
        "com": {"label": "Комментарии", "price_usd": 8.0},
        "react": {"label": "Реакции", "price_usd": 0.8},
    },
    "Telegram": {
        "sub": {"label": "Подписчики (члены)", "price_usd": 3.0},
        "view": {"label": "Просмотры (репосты)", "price_usd": 0.8},
        "com": {"label": "Комментарии (в чатах)", "price_usd": 5.0},
    },
    "Facebook": {
        "sub": {"label": "Подписчики", "price_usd": 4.5},
        "view": {"label": "Просмотры", "price_usd": 1.0},
        "com": {"label": "Комментарии", "price_usd": 6.5},
        "react": {"label": "Реакции", "price_usd": 0.7},
    },
    "Twitter/X": {
        "sub": {"label": "Подписчики", "price_usd": 3.5},
        "view": {"label": "Просмотры", "price_usd": 0.7},
        "com": {"label": "Комментарии", "price_usd": 5.5},
        "react": {"label": "Реакции", "price_usd": 0.4},
    }
}

# Pretty names
PRETTY_SOCIAL = {
    "Instagram": "Instagram",
    "TikTok": "TikTok",
    "YouTube": "YouTube",
    "Telegram": "Telegram",
    "Facebook": "Facebook",
    "Twitter/X": "Twitter/X",
}

# -------------------------
# Sanity
# -------------------------
if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise ValueError("BOT_TOKEN must be set and contain ':'")

# -------------------------
# Init bot & flask
# -------------------------
bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
app = Flask(__name__)

# -------------------------
# DB init
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
    )""")
    # orders (finalized or awaiting payment)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        social TEXT,
        service TEXT,
        amount INTEGER,
        price_usd REAL,
        currency TEXT,
        status TEXT,
        invoice_id TEXT,
        pay_url TEXT,
        link TEXT,
        created_at TEXT,
        updated_at TEXT
    )""")
    # invoices mapping (invoice -> list of order ids as JSON)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS invoices_map (
        invoice_id TEXT PRIMARY KEY,
        chat_id INTEGER,
        order_ids TEXT,
        raw_payload TEXT,
        created_at TEXT
    )""")
    # cart (temporary / persistent)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS cart (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        social TEXT,
        service TEXT,
        amount INTEGER,
        price_usd REAL,
        link TEXT,
        created_at TEXT
    )""")
    # operators
    cur.execute("""
    CREATE TABLE IF NOT EXISTS operators (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER UNIQUE,
        username TEXT,
        display_name TEXT,
        created_at TEXT
    )""")
    # support
    cur.execute("""
    CREATE TABLE IF NOT EXISTS support_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_chat INTEGER,
        username TEXT,
        text TEXT,
        status TEXT,
        created_at TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS support_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        req_id INTEGER,
        from_chat INTEGER,
        to_chat INTEGER,
        text TEXT,
        created_at TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS operator_notifications (
        operator_chat INTEGER PRIMARY KEY,
        message_id INTEGER,
        created_at TEXT
    )""")
    conn.commit()
    conn.close()

init_db()

# -------------------------
# DB helpers
# -------------------------
def ensure_user(chat_id: int, message: Optional[telebot.types.Message] = None):
    conn = get_db(); cur = conn.cursor()
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
    conn = get_db(); cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute("INSERT OR IGNORE INTO operators (chat_id, username, display_name, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, username, display_name, now))
    conn.commit(); conn.close()
    # ensure notification row exists
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO operator_notifications (operator_chat, message_id, created_at) VALUES (?, ?, ?)",
                (chat_id, None, now))
    conn.commit(); conn.close()

def remove_operator(chat_id:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM operators WHERE chat_id = ?", (chat_id,))
    cur.execute("DELETE FROM operator_notifications WHERE operator_chat = ?", (chat_id,))
    conn.commit(); conn.close()

def list_operators():
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM operators ORDER BY id")
    rows = [dict(r) for r in cur.fetchall()]; conn.close(); return rows

def add_to_cart(chat_id:int, social:str, service:str, amount:int, price_usd:float, link:str):
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO cart (chat_id, social, service, amount, price_usd, link, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, social, service, amount, price_usd, link, now))
    conn.commit(); oid = cur.lastrowid; conn.close(); return oid

def get_cart(chat_id:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM cart WHERE chat_id = ? ORDER BY id", (chat_id,))
    rows = [dict(r) for r in cur.fetchall()]; conn.close(); return rows

def remove_cart_item(cart_id:int, chat_id:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM cart WHERE id = ? AND chat_id = ?", (cart_id, chat_id))
    conn.commit(); conn.close()

def clear_cart(chat_id:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM cart WHERE chat_id = ?", (chat_id,))
    conn.commit(); conn.close()

def create_order_record(chat_id:int, social:str, service:str, amount:int, price_usd:float, currency:str="USD", link:Optional[str]=None, status:str="awaiting_payment"):
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("""INSERT INTO orders (chat_id, social, service, amount, price_usd, currency, status, invoice_id, pay_url, link, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (chat_id, social, service, amount, price_usd, currency, status, None, None, link, now, now))
    oid = cur.lastrowid; conn.commit(); conn.close(); return oid

def update_order_invoice(order_id:int, invoice_id:Optional[str], pay_url:Optional[str]):
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE orders SET invoice_id = ?, pay_url = ?, updated_at = ? WHERE id = ?", (invoice_id, pay_url, now, order_id))
    conn.commit(); conn.close()

def set_invoice_mapping(invoice_id:str, chat_id:int, order_ids:List[int], raw_payload:Optional[Any]=None):
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO invoices_map (invoice_id, chat_id, order_ids, raw_payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (invoice_id, chat_id, json.dumps(order_ids, ensure_ascii=False), json.dumps(raw_payload, ensure_ascii=False) if raw_payload else None, now))
    conn.commit(); conn.close()

def get_invoice_mapping(invoice_id:str):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM invoices_map WHERE invoice_id = ?", (invoice_id,))
    r = cur.fetchone(); conn.close()
    return dict(r) if r else None

def mark_orders_paid(order_ids:List[int]):
    conn = get_db(); cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    for oid in order_ids:
        cur.execute("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?", ("оплачен", now, oid))
    conn.commit(); conn.close()

# Support functions (same as before)
def get_open_requests_count() -> int:
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM support_requests WHERE status = 'open'")
    r = cur.fetchone(); conn.close()
    return r["c"] if r else 0

def create_support_request(user_chat:int, username:str, text:str)->int:
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO support_requests (user_chat, username, text, status, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_chat, username, text, "open", now))
    rid = cur.lastrowid
    cur.execute("INSERT INTO support_messages (req_id, from_chat, to_chat, text, created_at) VALUES (?, ?, ?, ?, ?)",
                (rid, user_chat, None, text, now))
    conn.commit(); conn.close(); return rid

def get_open_requests(offset:int=0, limit:int=10):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM support_requests WHERE status = 'open' ORDER BY id ASC LIMIT ? OFFSET ?", (limit, offset))
    rows = [dict(r) for r in cur.fetchall()]; conn.close(); return rows

def get_request_by_id(req_id:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM support_requests WHERE id = ?", (req_id,))
    r = cur.fetchone(); conn.close(); return dict(r) if r else None

def close_request(req_id:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE support_requests SET status = 'closed' WHERE id = ?", (req_id,))
    conn.commit(); conn.close()

def add_support_message(req_id:int, from_chat:int, to_chat:int, text:str):
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO support_messages (req_id, from_chat, to_chat, text, created_at) VALUES (?, ?, ?, ?, ?)",
                (req_id, from_chat, to_chat, text, now))
    conn.commit(); conn.close()

# -------------------------
# CryptoBot API helpers
# -------------------------
def create_cryptobot_invoice(amount_value:float, asset:str, payload:str, description:str, callback_url:Optional[str]=None) -> dict:
    url = CRYPTO_API_BASE + "/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTOPAY_API_TOKEN, "Content-Type": "application/json"}
    body = {"amount": str(amount_value), "asset": asset, "payload": str(payload), "description": description}
    if callback_url:
        body["callback"] = callback_url
    try:
        r = requests.post(url, json=body, headers=headers, timeout=20)
        try:
            j = r.json()
        except Exception:
            return {"error": True, "status_code": r.status_code, "body": r.text}
        if r.status_code not in (200,201):
            return {"error": True, "status_code": r.status_code, "body": j}
        return j
    except Exception as e:
        return {"error": True, "exception": str(e)}

def get_invoice_info(invoice_id:str) -> dict:
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
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return buf.read()

# -------------------------
# Conversion helpers (USD base)
# -------------------------
def convert_price_usd_to_asset(price_usd: float, asset: str) -> float:
    asset = asset.upper()
    if asset == "USDT":
        return round(price_usd, 6)
    try:
        if asset == "TON":
            r = requests.get("https://api.coingecko.com/api/v3/simple/price", params={"ids": "toncoin", "vs_currencies": "usd"}, timeout=8)
            j = r.json(); ton_usd = float(j["toncoin"]["usd"]); return round(price_usd / ton_usd, 6)
        if asset == "TRX":
            r = requests.get("https://api.coingecko.com/api/v3/simple/price", params={"ids": "tron", "vs_currencies": "usd"}, timeout=8)
            j = r.json(); trx_usd = float(j["tron"]["usd"]); return round(price_usd / trx_usd, 6)
        return round(price_usd, 6)
    except Exception:
        return round(price_usd, 6)

# -------------------------
# UI helpers
# -------------------------
def main_menu_markup():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🛒 Купить услуги", callback_data="buy_services"))
    kb.add(types.InlineKeyboardButton("🧾 Корзина", callback_data="view_cart"))
    kb.add(types.InlineKeyboardButton("📨 Поддержка (в боте)", callback_data="support_bot"))
    kb.add(types.InlineKeyboardButton("👤 Профиль", callback_data="profile"))
    return kb

def social_menu_markup():
    kb = types.InlineKeyboardMarkup(row_width=2)
    for soc in SERVICES_BY_SOCIAL.keys():
        kb.add(types.InlineKeyboardButton(PRETTY_SOCIAL.get(soc, soc), callback_data=f"social_{soc}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_main"))
    return kb

def services_markup_for_social(social:str):
    kb = types.InlineKeyboardMarkup(row_width=1)
    services = SERVICES_BY_SOCIAL.get(social, {})
    for svc_key, svc in services.items():
        label = svc.get("label", svc_key)
        price = svc.get("price_usd", 0.0)
        kb.add(types.InlineKeyboardButton(f"{label} — ${price}", callback_data=f"svc_{social}_{svc_key}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="buy_services"))
    return kb

def currency_selection_markup_for_order(chat_id:int, order_id:int):
    kb = types.InlineKeyboardMarkup(row_width=3)
    for asset in AVAILABLE_ASSETS:
        kb.add(types.InlineKeyboardButton(asset, callback_data=f"pay_asset_{chat_id}_{order_id}_{asset}"))
    kb.add(types.InlineKeyboardButton("🔙 Отмена", callback_data="cancel_payment"))
    return kb

def cart_view_markup(chat_id:int):
    kb = types.InlineKeyboardMarkup(row_width=1)
    items = get_cart(chat_id)
    for it in items:
        kb.add(types.InlineKeyboardButton(f"Удалить #{it['id']} {it['social']} {SERVICES_BY_SOCIAL.get(it['social'], {}).get(it['service'], {}).get('label',it['service'])} x{it['amount']}", callback_data=f"cart_remove_{it['id']}"))
    if items:
        kb.add(types.InlineKeyboardButton("Оплатить всё", callback_data=f"cart_pay_{chat_id}"))
        kb.add(types.InlineKeyboardButton("Очистить корзину", callback_data=f"cart_clear_{chat_id}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_main"))
    return kb

# -------------------------
# In-memory states
# -------------------------
user_state: Dict[int, Dict[str, Any]] = {}   # for awaiting input flows
operator_state: Dict[int, Dict[str, Any]] = {}  # operator replies

# Ensure initial operators
for op in INITIAL_OPERATORS:
    try:
        add_operator(op)
    except Exception:
        pass

# -------------------------
# Bot handlers
# -------------------------
@bot.message_handler(commands=['start'])
def cmd_start(m):
    ensure_user(m.chat.id, m)
    bot.send_message(m.chat.id, "🧸 Добро пожаловать! Выберите действие:", reply_markup=main_menu_markup())

@bot.callback_query_handler(func=lambda c: True)
def cb_all(call):
    try:
        data = call.data
        cid = call.message.chat.id

        # main flow
        if data == "buy_services":
            bot.edit_message_text("Выберите соцсеть:", cid, call.message.message_id, reply_markup=social_menu_markup())
            return

        if data == "back_main":
            bot.edit_message_text("Главное меню:", cid, call.message.message_id, reply_markup=main_menu_markup())
            return

        # social chosen
        if data.startswith("social_"):
            soc = data.split("_",1)[1]
            bot.edit_message_text(f"Услуги для {PRETTY_SOCIAL.get(soc,soc)}:", cid, call.message.message_id, reply_markup=services_markup_for_social(soc))
            return

        # service selected -> ask amount then link
        if data.startswith("svc_"):
            # svc_<social>_<svc_key>
            _, social, svc_key = data.split("_",2)
            svc = SERVICES_BY_SOCIAL.get(social, {}).get(svc_key)
            if not svc:
                bot.answer_callback_query(call.id, "Услуга не найдена")
                return
            # ask amount
            user_state[cid] = {"awaiting_amount_for": {"social": social, "service": svc_key}}
            bot.send_message(cid, f"Вы выбрали {PRETTY_SOCIAL.get(social,social)} - {svc.get('label')}. Введите количество (целое число):")
            bot.answer_callback_query(call.id)
            return

        # view cart
        if data == "view_cart":
            items = get_cart(cid)
            if not items:
                bot.answer_callback_query(call.id, "Корзина пуста")
                bot.send_message(cid, "Ваша корзина пуста.", reply_markup=main_menu_markup())
                return
            # build text
            text = "🧾 Ваша корзина:\n\n"
            total = 0.0
            for it in items:
                svc_label = SERVICES_BY_SOCIAL.get(it['social'], {}).get(it['service'], {}).get('label', it['service'])
                text += f"#{it['id']} {it['social']} — {svc_label} x{it['amount']} — ${it['price_usd']:.2f}\nСсылка: {it['link']}\n\n"
                total += float(it['price_usd'])
            text += f"Всего: ${total:.2f}\n\nВыберите действие:"
            bot.edit_message_text(text, cid, call.message.message_id, reply_markup=cart_view_markup(cid))
            return

        # remove cart item
        if data.startswith("cart_remove_"):
            try:
                cart_id = int(data.split("_")[-1])
            except:
                bot.answer_callback_query(call.id, "Ошибка")
                return
            remove_cart_item(cart_id, cid)
            bot.answer_callback_query(call.id, "Позиция удалена")
            # refresh cart view
            items = get_cart(cid)
            if not items:
                bot.send_message(cid, "Корзина пуста.", reply_markup=main_menu_markup())
            else:
                text = "🧾 Ваша корзина:\n\n"
                total = 0.0
                for it in items:
                    svc_label = SERVICES_BY_SOCIAL.get(it['social'], {}).get(it['service'], {}).get('label', it['service'])
                    text += f"#{it['id']} {it['social']} — {svc_label} x{it['amount']} — ${it['price_usd']:.2f}\nСсылка: {it['link']}\n\n"
                    total += float(it['price_usd'])
                text += f"Всего: ${total:.2f}\n\nВыберите действие:"
                try:
                    bot.edit_message_text(text, cid, call.message.message_id, reply_markup=cart_view_markup(cid))
                except Exception:
                    bot.send_message(cid, text, reply_markup=cart_view_markup(cid))
            return

        # clear cart
        if data.startswith("cart_clear_"):
            clear_cart(cid)
            bot.answer_callback_query(call.id, "Корзина очищена")
            bot.send_message(cid, "Корзина очищена.", reply_markup=main_menu_markup())
            return

        # pay cart
        if data.startswith("cart_pay_"):
            # show asset selection and create interim orders for each cart item
            items = get_cart(cid)
            if not items:
                bot.answer_callback_query(call.id, "Корзина пуста")
                return
            # create orders (status awaiting_payment) and collect ids
            order_ids = []
            total_usd = 0.0
            for it in items:
                oid = create_order_record(cid, it['social'], it['service'], it['amount'], float(it['price_usd']), "USD", link=it['link'], status="awaiting_payment")
                order_ids.append(oid)
                total_usd += float(it['price_usd'])
            # clear cart (moved to orders)
            clear_cart(cid)
            # create a temporary "cart reference" id: join order ids
            # offer asset choices to user to pay total_usd
            kb = types.InlineKeyboardMarkup(row_width=3)
            for asset in AVAILABLE_ASSETS:
                kb.add(types.InlineKeyboardButton(asset, callback_data=f"pay_cart_{cid}_{'-'.join(map(str,order_ids))}_{asset}"))
            kb.add(types.InlineKeyboardButton("🔙 Отмена", callback_data="back_main"))
            bot.send_message(cid, f"Сумма для оплаты: ${total_usd:.2f}. Выберите валюту:", reply_markup=kb)
            bot.answer_callback_query(call.id)
            return

        # pay single order (asset button) - format pay_asset_<chat>_<order>_<asset>
        if data.startswith("pay_asset_"):
            bot.answer_callback_query(call.id)
            payload = data.replace("pay_asset_", "", 1)
            parts = payload.split("_")
            if len(parts) < 3:
                bot.send_message(cid, "Ошибка: недостаточно данных для оплаты.")
                return
            try:
                order_chat = int(parts[0]); order_id = int(parts[1]); asset = parts[2]
            except:
                bot.send_message(cid, "Ошибка: неверные данные заказа.")
                return
            # load order
            conn = get_db(); cur = conn.cursor()
            cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
            row = cur.fetchone(); conn.close()
            if not row:
                bot.send_message(cid, "Заказ не найден.")
                return
            price_usd = float(row["price_usd"])
            pay_amount = convert_price_usd_to_asset(price_usd, asset.upper())
            order_uid = f"order_{order_chat}_{order_id}"
            description = f"Оплата заказа #{order_id}"
            callback_url = WEB_DOMAIN.rstrip("/") + "/cryptobot/ipn"
            resp = create_cryptobot_invoice(pay_amount, asset.upper(), order_uid, description, callback_url=callback_url)
            if isinstance(resp, dict) and resp.get("error"):
                bot.send_message(order_chat, f"Ошибка создания счета: {resp}")
                return
            invoice_id = resp.get("invoiceId") or resp.get("invoice_id") or resp.get("id")
            pay_url = resp.get("pay_url") or resp.get("payment_url") or (resp.get("result",{}).get("pay_url") if isinstance(resp, dict) else None)
            if invoice_id:
                set_invoice_mapping(str(invoice_id), order_chat, [order_id], raw_payload=resp)
                update_order_invoice(order_id, str(invoice_id), pay_url)
            if pay_url:
                try:
                    qr = generate_qr_bytes(pay_url)
                    bot.send_photo(order_chat, qr, caption=f"💳 Оплата заказа #{order_id} через {asset.upper()}\nСсылка: {pay_url}")
                except Exception:
                    bot.send_message(order_chat, f"💳 Оплата: {pay_url}")
            else:
                bot.send_message(order_chat, "Не удалось получить ссылку на оплату. Обратитесь в поддержку.")
            return

        # pay cart callback: pay_cart_<chat>_<orderids joined by -> e.g. 12-13-14>_<asset>
        if data.startswith("pay_cart_"):
            bot.answer_callback_query(call.id)
            try:
                _, rest = data.split("pay_cart_",1)
                parts = rest.split("_")
                chat_str = parts[0]
                orderids_part = parts[1]
                asset = parts[2]
            except Exception:
                bot.send_message(cid, "Ошибка оплаты корзины.")
                return
            try:
                order_chat = int(chat_str)
            except:
                bot.send_message(cid, "Ошибка: некорректный chat id."); return
            order_ids = []
            for s in orderids_part.split("-"):
                try:
                    order_ids.append(int(s))
                except:
                    pass
            if not order_ids:
                bot.send_message(cid, "Нет заказов для оплаты"); return
            # compute total USD
            conn = get_db(); cur = conn.cursor()
            cur.execute("SELECT id, price_usd FROM orders WHERE id IN ({seq})".format(seq=",".join("?"*len(order_ids))), order_ids)
            rows = cur.fetchall(); conn.close()
            if not rows:
                bot.send_message(cid, "Заказы не найдены"); return
            total_usd = sum(float(r["price_usd"]) for r in rows)
            pay_amount = convert_price_usd_to_asset(total_usd, asset.upper())
            # create single invoice with payload referencing multiple orders: payload "cart_{chat}_{ts}"
            ts = int(time.time())
            payload = f"cart_{order_chat}_{ts}"
            description = f"Оплата корзины {order_chat} ({len(order_ids)} поз.)"
            callback_url = WEB_DOMAIN.rstrip("/") + "/cryptobot/ipn"
            resp = create_cryptobot_invoice(pay_amount, asset.upper(), payload, description, callback_url=callback_url)
            if isinstance(resp, dict) and resp.get("error"):
                bot.send_message(order_chat, f"Ошибка создания счета: {resp}"); return
            invoice_id = resp.get("invoiceId") or resp.get("invoice_id") or resp.get("id")
            pay_url = resp.get("pay_url") or resp.get("payment_url") or (resp.get("result",{}).get("pay_url") if isinstance(resp, dict) else None)
            if invoice_id:
                set_invoice_mapping(str(invoice_id), order_chat, order_ids, raw_payload=resp)
                # update each order
                for oid in order_ids:
                    update_order_invoice(oid, str(invoice_id), pay_url)
            if pay_url:
                try:
                    qr = generate_qr_bytes(pay_url); bot.send_photo(order_chat, qr, caption=f"💳 Оплата корзины через {asset}\nСсылка: {pay_url}")
                except:
                    bot.send_message(order_chat, f"💳 Оплата: {pay_url}")
            else:
                bot.send_message(order_chat, "Не удалось получить ссылку на оплату.")
            return

        # profile: show orders + cart link
        if data == "profile":
            # show cart summary + orders
            items = get_cart(cid)
            txt = "👤 Профиль\n\n"
            if items:
                txt += "🧾 В корзине:\n"
                for it in items:
                    lbl = SERVICES_BY_SOCIAL.get(it['social'], {}).get(it['service'], {}).get('label', it['service'])
                    txt += f"- #{it['id']} {it['social']} {lbl} x{it['amount']} — ${it['price_usd']:.2f}\n"
            else:
                txt += "Корзина пуста.\n"
            # also show recent orders
            conn = get_db(); cur = conn.cursor()
            cur.execute("SELECT * FROM orders WHERE chat_id = ? ORDER BY id DESC LIMIT 10", (cid,))
            rows = cur.fetchall(); conn.close()
            if rows:
                txt += "\n📦 Последние заказы:\n"
                for r in rows:
                    txt += f"#{r['id']} {r['social']} {SERVICES_BY_SOCIAL.get(r['social'],{}).get(r['service'],{}).get('label', r['service'])} x{r['amount']} — ${r['price_usd']:.2f} — {r['status']}\n"
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton("Перейти в корзину", callback_data="view_cart"))
            kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_main"))
            bot.send_message(cid, txt, reply_markup=kb)
            return

        # support via bot
        if data == "support_bot":
            user_state[cid] = {"awaiting_support_msg": True}
            bot.send_message(cid, "✉️ Напишите ваше сообщение для поддержки. Оно будет отправлено операторам.")
            return

        # default
        bot.answer_callback_query(call.id, "Неизвестная команда.")
    except Exception:
        traceback.print_exc()
        try:
            bot.answer_callback_query(call.id, "Ошибка")
        except:
            pass

# -------------------------
# Notifications for operators
# -------------------------
def _store_operator_notification(op_chat:int, msg_id:Optional[int]):
    conn = get_db(); cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute("INSERT OR REPLACE INTO operator_notifications (operator_chat, message_id, created_at) VALUES (?, ?, ?)",
                (op_chat, msg_id, now))
    conn.commit(); conn.close()

def notify_all_operators_new_request():
    ops = list_operators()
    total = get_open_requests_count()
    for op in ops:
        op_chat = op["chat_id"]
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT message_id FROM operator_notifications WHERE operator_chat = ?", (op_chat,))
        r = cur.fetchone(); conn.close()
        notif_text = f"🔔 У вас новое обращение ({total} всего)"
        kb = types.InlineKeyboardMarkup(); kb.add(types.InlineKeyboardButton("Перейти к обращениям", callback_data=f"open_requests_page_1"))
        try:
            if r and r["message_id"]:
                try:
                    bot.edit_message_text(notif_text, op_chat, r["message_id"], reply_markup=kb)
                except Exception:
                    sent = bot.send_message(op_chat, notif_text, reply_markup=kb); _store_operator_notification(op_chat, sent.message_id)
            else:
                sent = bot.send_message(op_chat, notif_text, reply_markup=kb); _store_operator_notification(op_chat, sent.message_id)
        except Exception:
            _store_operator_notification(op_chat, None)

def show_requests_page(operator_chat:int, page:int, message_reference=None):
    per_page = 5
    offset = (page-1) * per_page
    rows = get_open_requests(offset=offset, limit=per_page)
    total = get_open_requests_count()
    total_pages = (total + per_page - 1)//per_page if total else 1
    txt = f"📂 Обращения — страница {page}/{total_pages}\nВсего открытых: {total}\n\nНажмите на обращение, чтобы открыть его."
    kb = types.InlineKeyboardMarkup(row_width=1)
    for r in rows:
        uname = r["username"] or f"id{r['user_chat']}"
        kb.add(types.InlineKeyboardButton(f"{r['id']} | {uname}", callback_data=f"req_{r['id']}"))
    nav = types.InlineKeyboardMarkup(row_width=3)
    prev_page = page-1 if page>1 else 1
    next_page = page+1 if page<total_pages else total_pages
    nav.add(types.InlineKeyboardButton("◀️", callback_data=f"open_requests_page_{prev_page}"),
            types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"),
            types.InlineKeyboardButton("▶️", callback_data=f"open_requests_page_{next_page}"))
    try:
        if message_reference:
            bot.edit_message_text(txt, operator_chat, message_reference.message_id, reply_markup=kb)
            bot.send_message(operator_chat, "Навигация:", reply_markup=nav)
        else:
            bot.send_message(operator_chat, txt, reply_markup=kb)
            bot.send_message(operator_chat, "Навигация:", reply_markup=nav)
    except Exception:
        try:
            bot.send_message(operator_chat, txt, reply_markup=kb)
            bot.send_message(operator_chat, "Навигация:", reply_markup=nav)
        except Exception:
            pass

# -------------------------
# Text handler: numbers, link, admin commands, operator replies
# -------------------------
@bot.message_handler(content_types=['text'])
def handle_text(m):
    try:
        cid = m.chat.id
        text = m.text.strip()
        ensure_user(cid, m)

        # ADMIN commands
        if text.startswith("/add_operator"):
            if m.from_user.id not in ADMIN_IDS:
                bot.reply_to(m, "Нет прав."); return
            parts = text.split()
            if len(parts) != 2:
                bot.reply_to(m, "Использование: /add_operator <chat_id>"); return
            try:
                new_id = int(parts[1])
            except:
                bot.reply_to(m, "Bad id"); return
            add_operator(new_id); bot.reply_to(m, f"Добавлен оператор {new_id}"); return

        if text.startswith("/operator_remove"):
            if m.from_user.id not in ADMIN_IDS:
                bot.reply_to(m, "Нет прав."); return
            parts = text.split()
            if len(parts) != 2:
                bot.reply_to(m, "Использование: /operator_remove <chat_id>"); return
            try:
                rem = int(parts[1])
            except:
                bot.reply_to(m, "Bad id"); return
            remove_operator(rem); bot.reply_to(m, f"Удалён оператор {rem}"); return

        if text.startswith("/operators_check"):
            if m.from_user.id not in ADMIN_IDS:
                bot.reply_to(m, "Нет прав."); return
            ops = list_operators()
            if not ops:
                bot.reply_to(m, "Нет операторов."); return
            s = "Операторы:\n"
            for o in ops:
                s += f"- id: {o['chat_id']}, username: {o['username'] or '—'}, name: {o['display_name'] or '—'}\n"
            bot.reply_to(m, s); return

        if text.startswith("/sadm"):
            if m.from_user.id not in ADMIN_IDS:
                bot.reply_to(m, "Нет прав."); return
            conn = get_db(); cur = conn.cursor()
            cur.execute("SELECT id, chat_id, social, service, amount, price_usd, status, link FROM orders ORDER BY id DESC LIMIT 100")
            rows = cur.fetchall(); conn.close()
            if not rows:
                bot.reply_to(m, "Заказов нет."); return
            txt = "Последние заказы:\n\n"
            for r in rows:
                svc_label = SERVICES_BY_SOCIAL.get(r['social'], {}).get(r['service'], {}).get('label', r['service'])
                txt += f"#{r['id']} uid:{r['chat_id']} {r['social']} {svc_label} x{r['amount']} — ${r['price_usd']:.2f} — {r['status']}\nСсылка: {r['link']}\n\n"
            bot.reply_to(m, txt); return

        # Flow states: awaiting amount for chosen service
        state = user_state.get(cid)
        if state and state.get("awaiting_amount_for"):
            # expecting integer amount
            if not text.isdigit():
                bot.reply_to(m, "Введите целое число."); return
            amount = int(text)
            info = state["awaiting_amount_for"]
            social = info["social"]; svc_key = info["service"]
            svc_def = SERVICES_BY_SOCIAL.get(social, {}).get(svc_key)
            if not svc_def:
                bot.reply_to(m, "Ошибка услуги."); user_state.pop(cid,None); return
            # compute price (simple: amount * base_unit_price). Here using price_usd per unit from svc_def
            # Many services are priced per a chunk (e.g., per 1000 views). For demo we multiply price_usd by amount/1
            # Adjust logic as needed.
            base_price = float(svc_def.get("price_usd", 0.0))
            # For typical cases amount may be in units; we'll compute total = base_price * (amount / 1)
            total_price = round(base_price * amount, 2)
            # store in user_state and ask link
            user_state[cid] = {"awaiting_link_for_cart": True, "pending_item": {"social": social, "service": svc_key, "amount": amount, "price_usd": total_price}}
            bot.send_message(cid, f"Итого: ${total_price:.2f}. Введите ссылку (http/https) для выполнения услуги:")
            return

        # awaiting link (for cart add or for single fixed package ordering)
        if state and state.get("awaiting_link_for_cart"):
            link = text
            if not (link.startswith("http://") or link.startswith("https://")):
                bot.reply_to(m, "Неверная ссылка. Должна начинаться с http/https"); return
            pending = state["pending_item"]
            add_to_cart(cid, pending["social"], pending["service"], pending["amount"], pending["price_usd"], link)
            user_state.pop(cid, None)
            kb = types.InlineKeyboardMarkup(); kb.add(types.InlineKeyboardButton("Перейти в корзину", callback_data="view_cart")); kb.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_main"))
            bot.reply_to(m, f"✅ Позиция добавлена в корзину: {pending['social']} {SERVICES_BY_SOCIAL.get(pending['social'],{}).get(pending['service'],{}).get('label',pending['service'])} x{pending['amount']} — ${pending['price_usd']:.2f}", reply_markup=kb)
            return

        # awaiting support msg
        if state and state.get("awaiting_support_msg"):
            user_state.pop(cid, None)
            uname = m.from_user.username or f"id{cid}"
            rid = create_support_request(cid, uname, text)
            bot.reply_to(m, "✅ Ваше обращение отправлено. Ожидайте ответа.")
            notify_all_operators_new_request()
            return

        # operator replying to request
        opstate = operator_state.get(cid)
        if opstate and opstate.get("awaiting_reply_for"):
            req_id = opstate["awaiting_reply_for"]
            req = get_request_by_id(req_id)
            if not req:
                bot.reply_to(m, "Обращение не найдено."); operator_state.pop(cid,None); return
            reply_text = text
            try:
                bot.send_message(req["user_chat"], f"💬 Ответ от поддержки:\n{reply_text}")
            except Exception:
                pass
            add_support_message(req_id, cid, req["user_chat"], reply_text)
            close_request(req_id)
            bot.reply_to(m, "Ответ отправлен и обращение закрыто.")
            operator_state.pop(cid,None)
            notify_all_operators_new_request()
            return

        # fallback
        bot.send_message(cid, "Не понял. Нажми /start для меню.")
    except Exception:
        traceback.print_exc()
        try:
            bot.reply_to(m, "Внутренняя ошибка.")
        except:
            pass

# -------------------------
# Flask endpoints: IPN + webhook
# -------------------------
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
    except Exception:
        pass
    invoice_id = None
    for k in ("invoiceId","invoice_id","id"):
        if k in payload:
            invoice_id = str(payload[k]); break
    status_field = payload.get("status") or payload.get("paymentStatus") or payload.get("state")
    paid_indicators = {"paid","success","confirmed","finished","complete"}
    st = str(status_field).lower() if status_field else ""
    if invoice_id and any(p in st for p in paid_indicators):
        mapping = get_invoice_mapping(invoice_id)
        if mapping:
            try:
                order_ids = json.loads(mapping["order_ids"])
                mark_orders_paid(order_ids)
                # notify user(s)
                try:
                    chatid = int(mapping["chat_id"])
                    bot.send_message(chatid, f"✅ Оплата получена. Заказы: {', '.join(map(str,order_ids))}. Спасибо!")
                except Exception:
                    pass
            except Exception:
                traceback.print_exc()
    return jsonify({"ok": True}), 200

# Telegram webhook endpoint
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
# Webhook setter
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
        bot.remove_webhook(); time.sleep(0.5)
        res = bot.set_webhook(url=webhook_url)
        print("Webhook set:", webhook_url, "result:", res)
    except Exception as e:
        print("Failed set webhook:", e)

# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    print("Starting SaleTest_full service")
    init_db()
    if USE_WEBHOOK:
        set_telegram_webhook()
        port = int(os.environ.get("PORT", 5000))
        print("Running Flask (webhook mode) on port", port)
        app.run(host="0.0.0.0", port=port)
    else:
        # run flask in thread and polling
        t = Thread(target=lambda: app.run(host="0.0.0.0", port=5000), daemon=True)
        t.start()
        print("Running polling (local)...")
        bot.infinity_polling(timeout=60, long_polling_timeout=20)

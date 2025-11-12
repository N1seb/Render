#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SaleTest_full.py
Telegram shop bot with:
 - social networks menus
 - services per network
 - cart (add/remove/view/pay)
 - CryptoBot integration (USDT, TON, TRX)
 - operators/support system
 - SQLite persistence
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
from typing import Optional, Dict, Any, List
from threading import Thread

from flask import Flask, request, jsonify
import telebot
from telebot import types

# -------------------------
# CONFIG (ENV overrides recommended)
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

# Only allowed assets
AVAILABLE_ASSETS = ["USDT", "TON", "TRX"]

# -------------------------
# PRICE LIST (USD base)
# You told me you already multiplied prices yourself — replace values here with your final per-unit prices.
# Structure:
# PRICE_LIST = {
#   "TikTok": {
#       "views": {"unit": 1000, "price_usd_for_unit": 5.0, "min":1000},
#       "subs": {"unit": 1, "price_usd_for_unit": 0.005, "min":10},
#       ...
#   }, ...
# }
# unit: how many real items correspond to 'one unit' used in "price description"
# price_usd_for_unit: price in USD for that unit (you can set it to your multiplied final price)
# min: minimal allowed quantity in real items (for views might be 1000, for others 10)
# Example values are placeholders — **replace** with your actual final prices.
# -------------------------
PRICE_LIST = {
    "TikTok": {
        "views": {"unit": 1000, "price_usd_for_unit": 5.0, "min": 1000, "label": "Просмотры"},
        "subs": {"unit": 1, "price_usd_for_unit": 0.005, "min": 10, "label": "Подписчики"},
        "comments": {"unit": 1, "price_usd_for_unit": 0.03, "min": 10, "label": "Комментарии"},
        "reactions": {"unit": 1, "price_usd_for_unit": 0.01, "min": 10, "label": "Реакции"},
    },
    "YouTube": {
        "views": {"unit": 1000, "price_usd_for_unit": 4.0, "min": 1000, "label": "Просмотры"},
        "subs": {"unit": 1, "price_usd_for_unit": 0.02, "min": 10, "label": "Подписчики"},
        "comments": {"unit": 1, "price_usd_for_unit": 0.05, "min": 10, "label": "Комментарии"},
    },
    "Instagram": {
        "likes": {"unit": 1, "price_usd_for_unit": 0.01, "min": 10, "label": "Лайки"},
        "subs": {"unit": 1, "price_usd_for_unit": 0.02, "min": 10, "label": "Подписчики"},
        "comments": {"unit": 1, "price_usd_for_unit": 0.05, "min": 10, "label": "Комментарии"},
    },
    "Telegram": {
        "subs": {"unit": 1, "price_usd_for_unit": 0.02, "min": 10, "label": "Подписчики"},
        "views": {"unit": 1, "price_usd_for_unit": 0.001, "min": 10, "label": "Просмотры"},
    },
    "Facebook": {
        "likes": {"unit": 1, "price_usd_for_unit": 0.01, "min": 10, "label": "Лайки"},
        "subs": {"unit": 1, "price_usd_for_unit": 0.02, "min": 10, "label": "Подписчики"},
    },
    "Twitter/X": {
        "likes": {"unit": 1, "price_usd_for_unit": 0.005, "min": 10, "label": "Лайки"},
        "subs": {"unit": 1, "price_usd_for_unit": 0.02, "min": 10, "label": "Подписчики"},
        "retweets": {"unit": 1, "price_usd_for_unit": 0.03, "min": 10, "label": "Репосты/Ретвиты"},
    }
}

# Map friendly list for menus
SOCIALS_ORDER = ["TikTok", "YouTube", "Instagram", "Telegram", "Facebook", "Twitter/X"]

# -------------------------
# INIT
# -------------------------
if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise ValueError("BOT_TOKEN must be set and contain a colon (:)")

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
    conn = get_db(); cur = conn.cursor()
    # users
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        created_at TEXT
    )""")
    # carts and cart items
    cur.execute("""
    CREATE TABLE IF NOT EXISTS carts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        created_at TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS cart_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cart_id INTEGER,
        social TEXT,
        service_key TEXT,
        quantity INTEGER,
        link TEXT,
        price_usd REAL,
        created_at TEXT
    )""")
    # orders (finalized payments) - each payment becomes an order group
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        total_usd REAL,
        asset TEXT,
        invoice_id TEXT,
        pay_url TEXT,
        status TEXT,
        created_at TEXT,
        updated_at TEXT
    )""")
    # order items (snapshot of cart_items at payment)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS order_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        social TEXT,
        service_key TEXT,
        quantity INTEGER,
        link TEXT,
        price_usd REAL
    )""")
    # invoice mapping for IPN
    cur.execute("""
    CREATE TABLE IF NOT EXISTS invoices_map (
        invoice_id TEXT PRIMARY KEY,
        order_id INTEGER,
        raw_payload TEXT,
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
    # operator notification storage
    cur.execute("""
    CREATE TABLE IF NOT EXISTS operator_notifications (
        operator_chat INTEGER PRIMARY KEY,
        message_id INTEGER,
        created_at TEXT
    )""")
    conn.commit(); conn.close()

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

def get_or_create_cart(chat_id:int) -> int:
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id FROM carts WHERE chat_id = ? ORDER BY id DESC LIMIT 1", (chat_id,))
    r = cur.fetchone()
    if r:
        cid = r["id"]
    else:
        now = datetime.utcnow().isoformat()
        cur.execute("INSERT INTO carts (chat_id, created_at) VALUES (?, ?)", (chat_id, now))
        cid = cur.lastrowid
        conn.commit()
    conn.close()
    return cid

def add_item_to_cart(cart_id:int, social:str, service_key:str, quantity:int, link:str, price_usd:float):
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("""INSERT INTO cart_items (cart_id, social, service_key, quantity, link, price_usd, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""", (cart_id, social, service_key, quantity, link, price_usd, now))
    conn.commit(); conn.close()

def list_cart_items(cart_id:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM cart_items WHERE cart_id = ?", (cart_id,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def remove_cart_item(item_id:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM cart_items WHERE id = ?", (item_id,))
    conn.commit(); conn.close()

def clear_cart(cart_id:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM cart_items WHERE cart_id = ?", (cart_id,))
    conn.commit(); conn.close()

def create_order_from_cart(cart_id:int, chat_id:int, asset:str, invoice_id:Optional[str], pay_url:Optional[str]) -> int:
    items = list_cart_items(cart_id)
    if not items:
        raise ValueError("empty cart")
    total = sum(float(it["price_usd"]) for it in items)
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("""INSERT INTO orders (chat_id, total_usd, asset, invoice_id, pay_url, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (chat_id, total, asset, invoice_id, pay_url, "awaiting_payment", now, now))
    oid = cur.lastrowid
    for it in items:
        cur.execute("""INSERT INTO order_items (order_id, social, service_key, quantity, link, price_usd)
                       VALUES (?, ?, ?, ?, ?, ?)""", (oid, it["social"], it["service_key"], it["quantity"], it["link"], it["price_usd"]))
    conn.commit(); conn.close()
    return oid

def update_order_payment(order_id:int, invoice_id:Optional[str], pay_url:Optional[str]):
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE orders SET invoice_id = ?, pay_url = ?, updated_at = ? WHERE id = ?", (invoice_id, pay_url, now, order_id))
    conn.commit(); conn.close()

def mark_order_paid(order_id:int):
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?", ("paid", now, order_id))
    conn.commit(); conn.close()

def set_invoice_map(invoice_id:str, order_id:int, raw_payload:Optional[Any]=None):
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO invoices_map (invoice_id, order_id, raw_payload, created_at) VALUES (?, ?, ?, ?)",
                (invoice_id, order_id, json.dumps(raw_payload, ensure_ascii=False) if raw_payload else None, now))
    conn.commit(); conn.close()

def get_order_by_invoice(invoice_id:str):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM invoices_map WHERE invoice_id = ?", (invoice_id,))
    r = cur.fetchone()
    conn.close()
    return dict(r) if r else None

# operator helpers
def add_operator(chat_id:int, username:Optional[str]=None, display_name:Optional[str]=None):
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
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
    rows = [dict(r) for r in cur.fetchall()]; conn.close()
    return rows

# support helpers
def get_open_requests(offset:int=0, limit:int=20):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM support_requests WHERE status = 'open' ORDER BY id ASC LIMIT ? OFFSET ?", (limit, offset))
    rows = [dict(r) for r in cur.fetchall()]; conn.close()
    return rows

def create_support_request(user_chat:int, username:str, text:str) -> int:
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO support_requests (user_chat, username, text, status, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_chat, username, text, "open", now))
    rid = cur.lastrowid
    cur.execute("INSERT INTO support_messages (req_id, from_chat, to_chat, text, created_at) VALUES (?, ?, ?, ?, ?)",
                (rid, user_chat, None, text, now))
    conn.commit(); conn.close()
    return rid

def get_request_by_id(req_id:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM support_requests WHERE id = ?", (req_id,))
    r = cur.fetchone(); conn.close()
    return dict(r) if r else None

def close_request(req_id:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE support_requests SET status = ? WHERE id = ?", ("closed", req_id))
    conn.commit(); conn.close()

def add_support_message(req_id:int, from_chat:int, to_chat:int, text:str):
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO support_messages (req_id, from_chat, to_chat, text, created_at) VALUES (?, ?, ?, ?, ?)",
                (req_id, from_chat, to_chat, text, now))
    conn.commit(); conn.close()

# operator notifications helper
def _store_operator_notification(op_chat:int, msg_id:Optional[int]):
    conn = get_db(); cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute("INSERT OR REPLACE INTO operator_notifications (operator_chat, message_id, created_at) VALUES (?, ?, ?)",
                (op_chat, msg_id, now))
    conn.commit(); conn.close()

def notify_all_operators_new_request():
    ops = list_operators()
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM support_requests WHERE status = 'open'")
    r = cur.fetchone(); conn.close()
    total = r["c"] if r else 0
    for op in ops:
        op_chat = op["chat_id"]
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT message_id FROM operator_notifications WHERE operator_chat = ?", (op_chat,))
        r = cur.fetchone(); conn.close()
        notif_text = f"🔔 У вас новое обращение ({total} всего)"
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("Перейти к обращениям", callback_data=f"open_requests_page_1"))
        try:
            if r and r["message_id"]:
                try:
                    bot.edit_message_text(notif_text, op_chat, r["message_id"], reply_markup=kb)
                except Exception:
                    sent = bot.send_message(op_chat, notif_text, reply_markup=kb)
                    _store_operator_notification(op_chat, sent.message_id)
            else:
                sent = bot.send_message(op_chat, notif_text, reply_markup=kb)
                _store_operator_notification(op_chat, sent.message_id)
        except Exception:
            _store_operator_notification(op_chat, None)

# -------------------------
# CryptoBot helpers
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
# Conversion helpers USD -> asset
# -------------------------
def convert_usd_to_asset(amount_usd:float, asset:str) -> float:
    asset = asset.upper()
    if asset == "USDT":
        # assume 1:1
        return round(amount_usd, 6)
    try:
        # use CoinGecko
        mapping = {
            "TON": "toncoin",
            "TRX": "tron"
        }
        if asset in mapping:
            rid = mapping[asset]
            r = requests.get("https://api.coingecko.com/api/v3/simple/price", params={"ids": rid, "vs_currencies": "usd"}, timeout=8)
            j = r.json()
            price_usd = float(j[rid]["usd"])
            return round(amount_usd / price_usd, 6)
    except Exception:
        pass
    return round(amount_usd, 6)

# -------------------------
# UI helpers (menus)
# -------------------------
def main_menu_markup():
    kb = types.InlineKeyboardMarkup(row_width=1)
    for s in SOCIALS_ORDER:
        kb.add(types.InlineKeyboardButton(s, callback_data=f"social_{s}"))
    kb.add(types.InlineKeyboardButton("🧾 Корзина / Профиль", callback_data="profile_cart"))
    kb.add(types.InlineKeyboardButton("✉️ Поддержка (в боте)", callback_data="support_bot"))
    return kb

def services_markup_for_social(social:str):
    services = PRICE_LIST.get(social, {})
    kb = types.InlineKeyboardMarkup(row_width=1)
    for key, meta in services.items():
        label = meta.get("label") or key
        unit = meta.get("unit",1)
        price = meta.get("price_usd_for_unit", 0.0)
        price_descr = f"${price} за {unit}" if unit and unit != 1 else f"${price} за 1"
        kb.add(types.InlineKeyboardButton(f"{label} — {price_descr}", callback_data=f"service_{social}_{key}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_main"))
    return kb

def profile_cart_markup(chat_id:int):
    kb = types.InlineKeyboardMarkup(row_width=1)
    cart_id = get_or_create_cart(chat_id)
    items = list_cart_items(cart_id)
    if items:
        kb.add(types.InlineKeyboardButton("Оплатить корзину", callback_data=f"pay_cart_{chat_id}_{cart_id}"))
    kb.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_main"))
    return kb

def cart_item_row_markup(cart_id:int, item_id:int):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("❌ Удалить", callback_data=f"cart_remove_{cart_id}_{item_id}"))
    kb.add(types.InlineKeyboardButton("🔙 Корзина", callback_data=f"profile_cart"))
    return kb

# -------------------------
# In-memory user states for multi-step flows
# -------------------------
user_state: Dict[int, Dict[str, Any]] = {}
operator_state: Dict[int, Dict[str, Any]] = {}

# -------------------------
# Ensure initial operators exist
# -------------------------
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
    bot.send_message(m.chat.id, "🧸 Добро пожаловать в магазин! Выберите соцсеть:", reply_markup=main_menu_markup())

@bot.callback_query_handler(func=lambda call: True)
def cb_all(call):
    try:
        data = call.data
        caller = call.from_user
        cid = call.message.chat.id if call.message else caller.id

        # Social selection
        if data.startswith("social_"):
            social = data.split("social_",1)[1]
            if social not in PRICE_LIST:
                bot.answer_callback_query(call.id, "Сеть не поддерживается")
                return
            bot.edit_message_text(f"📡 {social} — услуги:", cid, call.message.message_id, reply_markup=services_markup_for_social(social))
            return

        # back to main
        if data == "back_main":
            bot.edit_message_text("🧸 Главное меню:", cid, call.message.message_id, reply_markup=main_menu_markup())
            return

        # service chosen -> ask quantity and link
        if data.startswith("service_"):
            # format: service_<social>_<key>
            _, rest = data.split("service_",1)
            parts = rest.split("_")
            social = parts[0]
            service_key = "_".join(parts[1:])  # in case service key has underscores
            meta = PRICE_LIST.get(social, {}).get(service_key)
            if not meta:
                bot.answer_callback_query(call.id, "Услуга не найдена")
                return
            # store state for user: awaiting quantity then link
            # we also show price hint like "$X за unit"
            unit = meta.get("unit",1)
            price_unit = meta.get("price_usd_for_unit", 0.0)
            hint = f"${price_unit} за {unit}" if unit != 1 else f"${price_unit} за 1"
            minq = meta.get("min", 1)
            user_state[caller.id] = {"awaiting_qty_for": True, "social": social, "service_key": service_key, "min": minq, "unit": unit, "price_unit": price_unit}
            bot.send_message(caller.id, f"Вы выбрали {social} — {meta.get('label')}. Цена: {hint}. Введите количество (минимум {minq}):")
            bot.answer_callback_query(call.id)
            return

        # profile / cart
        if data == "profile_cart":
            cart_id = get_or_create_cart(cid)
            items = list_cart_items(cart_id)
            if not items:
                bot.edit_message_text("🧾 Ваша корзина пуста.", cid, call.message.message_id, reply_markup=main_menu_markup())
                return
            # build list text
            txt = "🧾 Ваша корзина:\n\n"
            total = 0.0
            for it in items:
                txt += f"#{it['id']} | {it['social']} — {PRICE_LIST[it['social']][it['service_key']]['label']} x{it['quantity']} | link: {it['link']} | ${it['price_usd']:.2f}\n"
                total += float(it['price_usd'])
            txt += f"\n💰 Итого: ${total:.2f}"
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton("Оплатить корзину", callback_data=f"pay_cart_{cid}_{cart_id}"))
            for it in items:
                kb.add(types.InlineKeyboardButton(f"❌ Удалить #{it['id']}", callback_data=f"cart_remove_{cart_id}_{it['id']}"))
            kb.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_main"))
            bot.edit_message_text(txt, cid, call.message.message_id, reply_markup=kb)
            return

        # remove cart item
        if data.startswith("cart_remove_"):
            try:
                _, cart_str, item_str = data.split("_",2)
                cart_id = int(cart_str); item_id = int(item_str)
            except:
                bot.answer_callback_query(call.id, "Неверные данные")
                return
            remove_cart_item(item_id)
            bot.answer_callback_query(call.id, "Позиция удалена")
            # refresh profile/cart view
            cart_id = get_or_create_cart(cid)
            items = list_cart_items(cart_id)
            if not items:
                bot.send_message(cid, "🧾 Корзина пуста.", reply_markup=main_menu_markup())
                return
            txt = "🧾 Ваша корзина:\n\n"; total = 0.0
            for it in items:
                txt += f"#{it['id']} | {it['social']} — {PRICE_LIST[it['social']][it['service_key']]['label']} x{it['quantity']} | link: {it['link']} | ${it['price_usd']:.2f}\n"
                total += float(it['price_usd'])
            txt += f"\n💰 Итого: ${total:.2f}"
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton("Оплатить корзину", callback_data=f"pay_cart_{cid}_{cart_id}"))
            kb.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_main"))
            bot.send_message(cid, txt, reply_markup=kb)
            return

        # pay cart
        if data.startswith("pay_cart_"):
            # format pay_cart_<chatid>_<cartid>
            try:
                _, chat_str, cart_str = data.split("_",2)
                chatid = int(chat_str); cartid = int(cart_str)
            except:
                bot.answer_callback_query(call.id, "Неправильный формат")
                return
            # load cart items
            items = list_cart_items(cartid)
            if not items:
                bot.answer_callback_query(call.id, "Корзина пуста")
                return
            # compute total USD
            total_usd = sum(float(it["price_usd"]) for it in items)
            # present asset selection inline
            kb = types.InlineKeyboardMarkup(row_width=3)
            for asset in AVAILABLE_ASSETS:
                kb.add(types.InlineKeyboardButton(asset, callback_data=f"pay_asset_cart_{chatid}_{cartid}_{asset}"))
            kb.add(types.InlineKeyboardButton("🔙 Отмена", callback_data="profile_cart"))
            bot.send_message(chatid, f"Итого: ${total_usd:.2f}. Выберите валюту для оплаты:", reply_markup=kb)
            bot.answer_callback_query(call.id)
            return

        # currency pay for cart: pay_asset_cart_<chatid>_<cartid>_<asset>
        if data.startswith("pay_asset_cart_"):
            bot.answer_callback_query(call.id)
            payload = data.replace("pay_asset_cart_", "", 1)
            parts = payload.split("_")
            if len(parts) < 3:
                bot.answer_callback_query(call.id, "Ошибка данных")
                return
            chat_str = parts[0]; cart_str = parts[1]; asset = parts[2]
            if not chat_str.isdigit() or not cart_str.isdigit():
                bot.answer_callback_query(call.id, "Ошибка данных")
                return
            chatid = int(chat_str); cartid = int(cart_str)
            items = list_cart_items(cartid)
            if not items:
                bot.send_message(chatid, "Корзина пуста"); return
            total_usd = sum(float(it["price_usd"]) for it in items)
            pay_amount = convert_usd_to_asset(total_usd, asset.upper())
            order_uid = f"cart_{chatid}_{cartid}_{int(time.time())}"
            description = f"Оплата корзины пользователя {chatid}, items {len(items)}"
            callback_url = WEB_DOMAIN.rstrip("/") + "/cryptobot/ipn" if WEB_DOMAIN else None
            resp = create_cryptobot_invoice(pay_amount, asset.upper(), order_uid, description, callback_url=callback_url)
            if isinstance(resp, dict) and resp.get("error"):
                bot.send_message(chatid, f"Ошибка создания счёта: {resp}")
                return
            invoice_id = resp.get("invoiceId") or resp.get("invoice_id") or resp.get("id")
            pay_url = resp.get("pay_url") or resp.get("payment_url") or (resp.get("result", {}).get("pay_url") if isinstance(resp, dict) else None)
            # create order record (awaiting payment) and map invoice
            order_id = create_order_from_cart(cartid, chatid, asset.upper(), invoice_id, pay_url)
            if invoice_id:
                set_invoice_map(str(invoice_id), order_id, raw_payload=resp)
                update_order_payment(order_id, invoice_id, pay_url)
            # send payment link/qr
            if pay_url:
                try:
                    qr = generate_qr_bytes(pay_url)
                    bot.send_photo(chatid, qr, caption=f"Оплатите заказ #{order_id} через {asset.upper()}\nСсылка: {pay_url}")
                except Exception:
                    bot.send_message(chatid, f"Оплатите заказ #{order_id}: {pay_url}")
            else:
                bot.send_message(chatid, "Не удалось получить ссылку на оплату. Свяжитесь с поддержкой.")
            return

        # Support via bot
        if data == "support_bot":
            user_state[cid] = {"awaiting_support_msg": True}
            bot.send_message(cid, "✉️ Введите сообщение для поддержки. Операторы увидят уведомление.")
            bot.answer_callback_query(call.id)
            return

        # operator: list requests page
        if data.startswith("open_requests_page_"):
            try:
                page = int(data.split("_")[-1])
            except:
                page = 1
            show_requests_page(call.from_user.id, page, call.message)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("req_"):
            try:
                req_id = int(data.split("_")[1])
            except:
                bot.answer_callback_query(call.id, "Bad request id"); return
            req = get_request_by_id(req_id)
            if not req or req["status"] != "open":
                bot.answer_callback_query(call.id, "Обращение не найдено или закрыто"); return
            text = f"📨 Обращение #{req['id']}\nОт: {req['username']} (id {req['user_chat']})\n\n{req['text']}\n\nНажмите Ответить, чтобы отправить ответ и закрыть обращение."
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton("Ответить", callback_data=f"reply_req_{req['id']}"))
            kb.add(types.InlineKeyboardButton("Назад к списку", callback_data="open_requests_page_1"))
            try:
                bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)
            except Exception:
                bot.send_message(call.message.chat.id, text, reply_markup=kb)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("reply_req_"):
            try:
                req_id = int(data.split("_")[-1])
            except:
                bot.answer_callback_query(call.id, "Bad conv id"); return
            # only operators
            if call.from_user.id not in [o["chat_id"] for o in list_operators()]:
                bot.answer_callback_query(call.id, "У вас нет прав оператора"); return
            operator_state[call.from_user.id] = {"awaiting_reply_for": req_id, "message_id": call.message.message_id}
            bot.send_message(call.from_user.id, "Введите ответ для пользователя. После отправки обращение закроется.")
            bot.answer_callback_query(call.id)
            return

        bot.answer_callback_query(call.id, "Неизвестная команда.")
    except Exception:
        traceback.print_exc()
        try:
            bot.answer_callback_query(call.id, "Ошибка обработки")
        except:
            pass

# -------------------------
# Requests page for operators
# -------------------------
def show_requests_page(operator_chat:int, page:int, message_reference=None):
    per_page = 5
    offset = (page-1)*per_page
    rows = get_open_requests(offset=offset, limit=per_page)
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM support_requests WHERE status = 'open'"); r = cur.fetchone(); conn.close()
    total = r["c"] if r else 0
    total_pages = (total + per_page -1)//per_page if total else 1
    txt = f"📂 Обращения — страница {page}/{total_pages}\nВсего открытых: {total}\n\nНажмите на обращение, чтобы открыть его."
    kb = types.InlineKeyboardMarkup(row_width=1)
    for rr in rows:
        uname = rr["username"] or f"id{rr['user_chat']}"
        kb.add(types.InlineKeyboardButton(f"{rr['id']} | {uname}", callback_data=f"req_{rr['id']}"))
    # nav
    nav = types.InlineKeyboardMarkup(row_width=3)
    prev_p = page-1 if page>1 else 1
    next_p = page+1 if page<total_pages else total_pages
    nav.add(types.InlineKeyboardButton("◀️", callback_data=f"open_requests_page_{prev_p}"),
            types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"),
            types.InlineKeyboardButton("▶️", callback_data=f"open_requests_page_{next_p}"))
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
        except:
            pass

# -------------------------
# Text handler (quantity, link, cart, support, admin commands)
# -------------------------
@bot.message_handler(content_types=['text'])
def handle_text(m):
    try:
        cid = m.chat.id
        text = m.text.strip()
        ensure_user(cid, m)

        # Admin commands: manage operators and /sadm
        if text.startswith("/add_operator"):
            if m.from_user.id not in ADMIN_IDS:
                bot.reply_to(m, "Нет прав."); return
            parts = text.split()
            if len(parts) != 2:
                bot.reply_to(m, "Использование: /add_operator <chat_id>"); return
            try:
                nid = int(parts[1])
            except:
                bot.reply_to(m, "Bad id"); return
            add_operator(nid)
            bot.reply_to(m, f"Добавлен оператор {nid}"); return

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
            remove_operator(rem)
            bot.reply_to(m, f"Удалён оператор {rem}"); return

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
            cur.execute("SELECT id, chat_id, total_usd, asset, status, created_at FROM orders ORDER BY id DESC LIMIT 50")
            rows = cur.fetchall(); conn.close()
            if not rows:
                bot.reply_to(m, "Заказов нет."); return
            txt = "Последние заказы:\n\n"
            for r in rows:
                txt += f"#{r['id']} uid:{r['chat_id']} ${r['total_usd']:.2f} asset:{r['asset']} status:{r['status']} at:{r['created_at']}\n"
                # list items
                conn = get_db(); cur = conn.cursor()
                cur.execute("SELECT * FROM order_items WHERE order_id = ?", (r["id"],))
                items = cur.fetchall(); conn.close()
                for it in items:
                    txt += f"   - {it['social']} | {PRICE_LIST[it['social']][it['service_key']]['label']} x{it['quantity']} | link:{it['link']} | ${it['price_usd']:.2f}\n"
            bot.reply_to(m, txt); return

        # States
        state = user_state.get(cid)
        # awaiting quantity
        if state and state.get("awaiting_qty_for"):
            if not text.isdigit():
                bot.reply_to(m, "Введите целое число."); return
            qty = int(text)
            minq = state.get("min",1)
            if qty < minq:
                bot.reply_to(m, f"Минимум {minq}"); return
            # calculate price: price_usd_for_unit * (qty/unit)
            unit = state.get("unit",1)
            price_unit = float(state.get("price_unit",0.0))
            # price = price_unit * (qty / unit)
            price = price_unit * (qty / unit)
            price = round(price, 2)
            # now ask for link (some services may require link)
            user_state[cid] = {"awaiting_link_for": True, "social": state["social"], "service_key": state["service_key"], "quantity": qty, "price_usd": price}
            bot.reply_to(m, f"Цена: ${price:.2f}. Теперь отправьте ссылку на пост/канал/аккаунт (http/https):")
            return

        # awaiting link
        if state and state.get("awaiting_link_for"):
            link = text
            if not (link.startswith("http://") or link.startswith("https://")):
                bot.reply_to(m, "Неверная ссылка. Должна начинаться с http/https"); return
            social = state["social"]; service_key = state["service_key"]; qty = state["quantity"]; price = float(state["price_usd"])
            # add to cart
            cart_id = get_or_create_cart(cid)
            add_item_to_cart(cart_id, social, service_key, qty, link, price)
            user_state.pop(cid, None)
            bot.reply_to(m, f"✅ Товар добавлен в корзину. Цена: ${price:.2f}\nПерейти в корзину можно через '🧾 Корзина / Профиль' в меню.", reply_markup=main_menu_markup())
            return

        # awaiting support
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
                bot.reply_to(m, "Обращение не найдено."); operator_state.pop(cid, None); return
            reply_text = text
            try:
                bot.send_message(req["user_chat"], f"💬 Ответ от поддержки:\n{reply_text}")
            except Exception:
                pass
            add_support_message(req_id, cid, req["user_chat"], reply_text)
            close_request(req_id)
            bot.reply_to(m, "Ответ отправлен и обращение закрыто.")
            operator_state.pop(cid, None)
            notify_all_operators_new_request()
            return

        # default fallback
        bot.send_message(cid, "Не понял. Нажми /start для меню.")
    except Exception:
        traceback.print_exc()
        try:
            bot.reply_to(m, "Внутренняя ошибка.")
        except:
            pass

# -------------------------
# Flask endpoints: CryptoBot IPN and Telegram webhook
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
    except:
        pass
    # find invoice id and status
    invoice_id = None
    for k in ("invoiceId","invoice_id","id"):
        if k in payload:
            invoice_id = str(payload[k]); break
    status_field = payload.get("status") or payload.get("paymentStatus") or payload.get("state")
    paid_indicators = {"paid","success","confirmed","finished","complete"}
    st = str(status_field).lower() if status_field else ""
    if any(p in st for p in paid_indicators) and invoice_id:
        # find order via invoices_map
        row = get_order_by_invoice(invoice_id)
        if row:
            order_id = int(row["order_id"])
            mark_order_paid(order_id)
            # notify buyer
            conn = get_db(); cur = conn.cursor()
            cur.execute("SELECT chat_id FROM orders WHERE id = ?", (order_id,))
            rr = cur.fetchone(); conn.close()
            if rr:
                try:
                    bot.send_message(rr["chat_id"], f"✅ Оплата получена за заказ #{order_id}. Спасибо! В ближайшее время начнём выполнение.")
                except Exception:
                    pass
    return jsonify({"ok": True}), 200

# webhook endpoint
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

# webhook setter
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
    # ensure initial operators persistently
    for op in INITIAL_OPERATORS:
        try:
            add_operator(op)
        except:
            pass
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

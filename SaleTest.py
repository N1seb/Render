#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SaleTest_full.py
Полный рабочий Telegram-бот — магазин + корзина + CryptoBot + операторы + поддержка.
Валюты: USDT, TON, TRX
Хранение: SQLite
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
# CONFIG — настрой перед запуском или задавай через ENV
# -------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN") or "8587164094:AAEcsW0oUMg1Hphbymdg3NHtH_Q25j7RyWo"
CRYPTOPAY_API_TOKEN = os.environ.get("CRYPTOPAY_API_TOKEN") or "484313:AA6FJU50A2cMhJas5ruR6PD15Jl5F1XMrN7"
WEB_DOMAIN = os.environ.get("WEB_DOMAIN") or "https://render-jj8d.onrender.com"
USE_WEBHOOK = os.environ.get("USE_WEBHOOK", "0") == "1"
ADMIN_IDS = set([int(os.environ.get("ADMIN_ID") or 1942740947)])
INITIAL_OPERATORS = [7771789412]  # добавь сюда операторов, которые сразу есть

CRYPTO_API_BASE = "https://pay.crypt.bot/api"

DB_FILE = os.environ.get("DB_FILE") or "salebot_full.sqlite"
IPN_LOG_FILE = os.environ.get("IPN_LOG_FILE") or "ipn_log.jsonl"

# Валюты, которые мы поддерживаем для оплаты
AVAILABLE_ASSETS = ["USDT", "TON", "TRX"]

# Соцсети и доступные услуги — ПРАЙС: ты говорил, что у тебя уже готовый прайс.
# Здесь шаблон: 'social' -> { 'service_key': { 'title': str, 'min': int, 'unit': int, 'price_usd_per_unit': float } }
# price_usd_per_unit — цена за единицу в USD. (ты говорил, уже перемножил — просто вставь свои значения)
SERVICES = {
    "Instagram": {
        "sub": {"title": "Подписчики", "min": 10, "unit": 1, "price_usd_per_unit": 0.01},
        "view": {"title": "Просмотры", "min": 1000, "unit": 1000, "price_usd_per_unit": 5.0},  # пример: $5 за 1000
        "com": {"title": "Комментарии", "min": 10, "unit": 1, "price_usd_per_unit": 0.02},
        "like": {"title": "Лайки/Реакции", "min": 10, "unit": 1, "price_usd_per_unit": 0.01},
    },
    "TikTok": {
        "sub": {"title": "Подписчики", "min": 10, "unit": 1, "price_usd_per_unit": 0.015},
        "view": {"title": "Просмотры", "min": 1000, "unit": 1000, "price_usd_per_unit": 6.0},
        "like": {"title": "Лайки", "min": 10, "unit": 1, "price_usd_per_unit": 0.02},
    },
    "YouTube": {
        "sub": {"title": "Подписчики", "min": 10, "unit": 1, "price_usd_per_unit": 0.05},
        "view": {"title": "Просмотры", "min": 1000, "unit": 1000, "price_usd_per_unit": 3.0},
        "com": {"title": "Комментарии", "min": 5, "unit": 1, "price_usd_per_unit": 0.2},
    },
    "Telegram": {
        "sub": {"title": "Подписчики (канал)", "min": 10, "unit": 1, "price_usd_per_unit": 0.02},
        "view": {"title": "Просмотры", "min": 10, "unit": 1, "price_usd_per_unit": 0.01},
    },
    "Facebook": {
        "sub": {"title": "Подписчики", "min": 10, "unit": 1, "price_usd_per_unit": 0.03},
        "like": {"title": "Реакции", "min": 10, "unit": 1, "price_usd_per_unit": 0.02},
    },
    "X": {  # Twitter/X
        "sub": {"title": "Подписчики", "min": 10, "unit": 1, "price_usd_per_unit": 0.02},
        "retweet": {"title": "Ретвиты/репосты", "min": 5, "unit": 1, "price_usd_per_unit": 0.1},
    }
}

# Текстовые отображения
PRETTY_SOCIALS = list(SERVICES.keys())

# -------------------------
# sanity check
# -------------------------
if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise ValueError("BOT_TOKEN must be set and contain a colon (:)")

if not CRYPTOPAY_API_TOKEN or ":" not in CRYPTOPAY_API_TOKEN:
    # CryptoBot token may also contain colon pattern like "<botid>:<token>", but some tokens may be simple. We'll warn only.
    pass

# -------------------------
# Init bot + flask
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
    # orders — finished individual orders (single-item) and also used for paid items
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        social TEXT,
        service_key TEXT,
        amount INTEGER,
        price_usd REAL,
        link TEXT,
        status TEXT,
        invoice_id TEXT,
        pay_url TEXT,
        created_at TEXT,
        updated_at TEXT
    )""")
    # carts — one cart per user (open)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS carts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        status TEXT, -- open, paid, cancelled
        created_at TEXT,
        updated_at TEXT
    )""")
    # cart items
    cur.execute("""
    CREATE TABLE IF NOT EXISTS cart_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cart_id INTEGER,
        social TEXT,
        service_key TEXT,
        amount INTEGER,
        price_usd REAL,
        link TEXT,
        created_at TEXT
    )""")
    # invoices_map
    cur.execute("""
    CREATE TABLE IF NOT EXISTS invoices_map (
        invoice_id TEXT PRIMARY KEY,
        chat_id INTEGER,
        order_id INTEGER,
        cart_id INTEGER,
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
    # operator notifications (store last message id so we can edit)
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

def get_or_create_cart(chat_id:int) -> int:
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id FROM carts WHERE chat_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1", (chat_id,))
    r = cur.fetchone()
    if r:
        cid = r["id"]
    else:
        now = datetime.utcnow().isoformat()
        cur.execute("INSERT INTO carts (chat_id, status, created_at, updated_at) VALUES (?, ?, ?, ?)", (chat_id, "open", now, now))
        cid = cur.lastrowid
        conn.commit()
    conn.close()
    return cid

def add_item_to_cart(cart_id:int, social:str, service_key:str, amount:int, link:str, price_usd:float):
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO cart_items (cart_id, social, service_key, amount, price_usd, link, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cart_id, social, service_key, amount, price_usd, link, now))
    cur.execute("UPDATE carts SET updated_at = ? WHERE id = ?", (now, cart_id))
    conn.commit(); conn.close()

def get_cart_items(cart_id:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM cart_items WHERE cart_id = ?", (cart_id,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close(); return rows

def remove_cart_item(item_id:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM cart_items WHERE id = ?", (item_id,))
    conn.commit(); conn.close()

def clear_cart(cart_id:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM cart_items WHERE cart_id = ?", (cart_id,))
    cur.execute("UPDATE carts SET status = ?, updated_at = ? WHERE id = ?", ("cancelled", datetime.utcnow().isoformat(), cart_id))
    conn.commit(); conn.close()

def mark_cart_paid(cart_id:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE carts SET status = ?, updated_at = ? WHERE id = ?", ("paid", datetime.utcnow().isoformat(), cart_id))
    conn.commit(); conn.close()

def create_order_from_cart_item(chat_id:int, cart_item:dict, status="awaiting_payment"):
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("""INSERT INTO orders (chat_id, social, service_key, amount, price_usd, link, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (chat_id, cart_item["social"], cart_item["service_key"], cart_item["amount"], cart_item["price_usd"],
                 cart_item["link"], status, now, now))
    oid = cur.lastrowid
    conn.commit(); conn.close()
    return oid

def create_single_order(chat_id:int, social:str, service_key:str, amount:int, price_usd:float, link:str, status="awaiting_payment"):
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("""INSERT INTO orders (chat_id, social, service_key, amount, price_usd, link, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (chat_id, social, service_key, amount, price_usd, link, status, now, now))
    oid = cur.lastrowid
    conn.commit(); conn.close()
    return oid

def update_order_invoice(order_id:int, invoice_id:Optional[str], pay_url:Optional[str]):
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE orders SET invoice_id = ?, pay_url = ?, updated_at = ? WHERE id = ?", (invoice_id, pay_url, now, order_id))
    conn.commit(); conn.close()

def set_invoice_mapping(invoice_id:str, chat_id:int, order_id:Optional[int]=None, cart_id:Optional[int]=None, raw_payload:Optional[Any]=None):
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO invoices_map (invoice_id, chat_id, order_id, cart_id, raw_payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (invoice_id, chat_id, order_id, cart_id, json.dumps(raw_payload, ensure_ascii=False) if raw_payload else None, now))
    conn.commit(); conn.close()

def list_operators():
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM operators ORDER BY id")
    rows = [dict(r) for r in cur.fetchall()]; conn.close(); return rows

def add_operator(chat_id:int, username:Optional[str]=None, display_name:Optional[str]=None):
    conn = get_db(); cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute("INSERT OR IGNORE INTO operators (chat_id, username, display_name, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, username, display_name, now))
    conn.commit(); conn.close()
    # ensure notification entry
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO operator_notifications (operator_chat, message_id, created_at) VALUES (?, ?, ?)",
                (chat_id, None, now))
    conn.commit(); conn.close()

def remove_operator(chat_id:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM operators WHERE chat_id = ?", (chat_id,))
    cur.execute("DELETE FROM operator_notifications WHERE operator_chat = ?", (chat_id,))
    conn.commit(); conn.close()

def get_open_requests_count() -> int:
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM support_requests WHERE status = 'open'")
    r = cur.fetchone(); conn.close()
    return r["c"] if r else 0

def create_support_request(user_chat:int, username:str, text:str) -> int:
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
        if r.status_code not in (200, 201):
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
            r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                             params={"ids": "toncoin", "vs_currencies": "usd"}, timeout=8)
            j = r.json()
            ton_usd = float(j["toncoin"]["usd"])
            return round(price_usd / ton_usd, 6)
        if asset == "TRX":
            r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                             params={"ids": "tron", "vs_currencies": "usd"}, timeout=8)
            j = r.json()
            trx_usd = float(j["tron"]["usd"])
            return round(price_usd / trx_usd, 6)
        return round(price_usd, 6)
    except Exception:
        return round(price_usd, 6)

# -------------------------
# UI helpers
# -------------------------
def main_menu_markup():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🛒 Магазин", callback_data="menu_shop"))
    kb.add(types.InlineKeyboardButton("🧾 Корзина / Профиль", callback_data="profile"))
    kb.add(types.InlineKeyboardButton("✉️ Поддержка (в боте)", callback_data="support_bot"))
    kb.add(types.InlineKeyboardButton("📞 Поддержка (личные)", callback_data="support_personal"))
    return kb

def shop_socials_markup():
    kb = types.InlineKeyboardMarkup(row_width=2)
    for s in PRETTY_SOCIALS:
        kb.add(types.InlineKeyboardButton(s, callback_data=f"shop_social_{s}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_main"))
    return kb

def services_markup_for_social(social):
    kb = types.InlineKeyboardMarkup(row_width=1)
    services = SERVICES.get(social, {})
    for key, info in services.items():
        # show short price example: price per unit (unit explained)
        unit = info.get("unit",1)
        price = info.get("price_usd_per_unit", 0.0)
        if unit > 1:
            label = f"{info['title']} — ${price} / {unit}"
        else:
            label = f"{info['title']} — ${price} / 1"
        kb.add(types.InlineKeyboardButton(label, callback_data=f"service_{social}_{key}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="shop_socials"))
    return kb

def currency_selection_markup_for_order(chat_id:int, order_ref:str):
    # order_ref for single order: "order_<chatid>_<orderid>"
    kb = types.InlineKeyboardMarkup(row_width=3)
    for asset in AVAILABLE_ASSETS:
        kb.add(types.InlineKeyboardButton(asset, callback_data=f"pay_asset_{order_ref}_{asset}"))
    kb.add(types.InlineKeyboardButton("🔙 Отмена", callback_data="cancel_payment"))
    return kb

def currency_selection_markup_for_cart(chat_id:int, cart_id:int):
    kb = types.InlineKeyboardMarkup(row_width=3)
    for asset in AVAILABLE_ASSETS:
        kb.add(types.InlineKeyboardButton(asset, callback_data=f"pay_cart_{chat_id}_{cart_id}_{asset}"))
    kb.add(types.InlineKeyboardButton("🔙 Отмена", callback_data="cancel_payment"))
    return kb

# -------------------------
# In-memory states (short-lived)
# -------------------------
user_state: Dict[int, Dict[str, Any]] = {}   # for awaiting qty/link/support etc.
operator_state: Dict[int, Dict[str, Any]] = {}

# -------------------------
# Ensure initial operators
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
    bot.send_message(m.chat.id, "🧸 Добро пожаловать! Выберите действие:", reply_markup=main_menu_markup())

# (часть продолжается далее...)
@bot.callback_query_handler(func=lambda c: True)
def cb_all(call):
    try:
        data = call.data
        uid = call.from_user.id
        cid = call.message.chat.id

        # Main menu
        if data == "menu_shop":
            bot.edit_message_text("Выберите соцсеть:", cid, call.message.message_id, reply_markup=shop_socials_markup())
            return
        if data == "back_main":
            bot.edit_message_text("Главное меню:", cid, call.message.message_id, reply_markup=main_menu_markup())
            return

        # support personal (just instruction)
        if data == "support_personal":
            bot.edit_message_text("📨 Нужна помощь в личных сообщениях? Найдите оператора по его юзернейму или нажмите 'Поддержка (в боте)' чтобы написать через бота.", cid, call.message.message_id)
            return

        # support via bot
        if data == "support_bot":
            user_state[cid] = {"awaiting_support_msg": True}
            bot.send_message(cid, "✉️ Напишите ваше сообщение для поддержки. Оно будет отправлено операторам.")
            return

        # shop social selected
        if data.startswith("shop_social_"):
            social = data.replace("shop_social_", "", 1)
            if social not in SERVICES:
                bot.answer_callback_query(call.id, "Ошибка: неизвестная соцсеть")
                return
            bot.edit_message_text(f"Услуги для {social}:", cid, call.message.message_id, reply_markup=services_markup_for_social(social))
            return

        # service selected (start qty flow)
        if data.startswith("service_"):
            _, social, key = data.split("_", 2)
            svc = SERVICES.get(social, {}).get(key)
            if not svc:
                bot.answer_callback_query(call.id, "Сервис не найден")
                return
            min_allowed = svc.get("min",1)
            unit = svc.get("unit",1)
            price_unit = svc.get("price_usd_per_unit", 0.0)
            user_state[cid] = {"awaiting_qty_for": True, "social": social, "service_key": key,
                               "min": min_allowed, "unit": unit, "price_unit": price_unit}
            bot.send_message(cid, f"Вы выбрали: {svc['title']} для {social}.\nМинимум: {min_allowed}. Введите количество (целое число):")
            bot.answer_callback_query(call.id)
            return

        # profile/cart
        if data == "profile":
            cart_id = get_or_create_cart(cid)
            items = get_cart_items(cart_id)
            txt = "🧾 Ваша корзина:\n\n"
            total = 0.0
            kb = types.InlineKeyboardMarkup(row_width=1)
            if items:
                for it in items:
                    txt += f"#{it['id']} | {it['social']} — {SERVICES[it['social']][it['service_key']]['title']} x{it['amount']} — ${it['price_usd']:.2f}\nLink: {it['link']}\n\n"
                    kb.add(types.InlineKeyboardButton(f"Удалить #{it['id']}", callback_data=f"cart_remove_{it['id']}"))
                    total += float(it['price_usd'])
                txt += f"Итого: ${total:.2f}\n\n"
                kb.add(types.InlineKeyboardButton("Оплатить корзину", callback_data=f"cart_pay_{cart_id}"))
                kb.add(types.InlineKeyboardButton("Очистить корзину", callback_data=f"cart_clear_{cart_id}"))
            else:
                txt += "Корзина пуста.\n\n"
            conn = get_db(); cur = conn.cursor()
            cur.execute("SELECT * FROM orders WHERE chat_id = ? ORDER BY id DESC LIMIT 10", (cid,))
            rows = cur.fetchall(); conn.close()
            if rows:
                txt += "\n📋 Последние заказы:\n"
                for r in rows:
                    txt += f"#{r['id']} | {r['social']} {SERVICES[r['social']][r['service_key']]['title']} x{r['amount']} — ${r['price_usd']:.2f} — {r['status']}\n"
            bot.send_message(cid, txt, reply_markup=kb)
            bot.answer_callback_query(call.id)
            return

        # cart remove item
        if data.startswith("cart_remove_"):
            try:
                item_id = int(data.split("_")[-1])
            except:
                bot.answer_callback_query(call.id, "Ошибка удаления")
                return
            remove_cart_item(item_id)
            bot.answer_callback_query(call.id, "Удалено")
            try:
                bot.edit_message_text("Элемент удалён. Откройте Корзина/Профиль снова.", cid, call.message.message_id)
            except Exception:
                pass
            return

        # cart clear
        if data.startswith("cart_clear_"):
            try:
                cart_id = int(data.split("_")[-1])
            except:
                bot.answer_callback_query(call.id, "Ошибка")
                return
            clear_cart(cart_id)
            bot.answer_callback_query(call.id, "Корзина очищена")
            try:
                bot.edit_message_text("Корзина очищена. Откройте Корзина/Профиль снова.", cid, call.message.message_id)
            except Exception:
                pass
            return

        # cart pay selected
        if data.startswith("cart_pay_"):
            try:
                cart_id = int(data.split("_")[-1])
            except:
                bot.answer_callback_query(call.id, "Ошибка")
                return
            items = get_cart_items(cart_id)
            if not items:
                bot.answer_callback_query(call.id, "Корзина пуста")
                return
            total = sum(float(it['price_usd']) for it in items)
            bot.send_message(cid, f"Сумма к оплате: ${total:.2f}. Выберите валюту:", reply_markup=currency_selection_markup_for_cart(cid, cart_id))
            bot.answer_callback_query(call.id)
            return

        # pay cart in asset
        if data.startswith("pay_cart_"):
            parts = data.split("_", 3)
            if len(parts) != 4:
                bot.answer_callback_query(call.id, "Неверные данные оплаты"); return
            _, chat_str, cartid_str, asset = parts
            if not chat_str.isdigit() or not cartid_str.isdigit():
                bot.answer_callback_query(call.id, "Неправильные идентификаторы"); return
            order_chat = int(chat_str); cart_id = int(cartid_str)
            items = get_cart_items(cart_id)
            if not items:
                bot.answer_callback_query(call.id, "Корзина пуста"); return
            total_usd = sum(float(it['price_usd']) for it in items)
            pay_amount = convert_price_usd_to_asset(total_usd, asset.upper())
            order_uid = f"cart_{order_chat}_{cart_id}_{int(time.time())}"
            description = f"Оплата корзины #{cart_id} пользователем {order_chat}"
            callback_url = WEB_DOMAIN.rstrip("/") + "/cryptobot/ipn"
            resp = create_cryptobot_invoice(pay_amount, asset.upper(), order_uid, description, callback_url=callback_url)
            if isinstance(resp, dict) and resp.get("error"):
                bot.send_message(order_chat, f"Ошибка при создании чека: {resp}")
                bot.answer_callback_query(call.id, "Ошибка")
                return
            invoice_id = resp.get("invoiceId") or resp.get("id")
            pay_url = resp.get("pay_url") or resp.get("payment_url") or (resp.get("result",{}).get("pay_url") if isinstance(resp, dict) else None)
            if invoice_id:
                set_invoice_mapping(str(invoice_id), order_chat, order_id=None, cart_id=cart_id, raw_payload=resp)
            if pay_url:
                try:
                    qr = generate_qr_bytes(pay_url)
                    bot.send_photo(order_chat, qr, caption=f"💳 Оплата корзины #{cart_id} через {asset.upper()}\nСсылка: {pay_url}")
                except Exception:
                    bot.send_message(order_chat, f"💳 Оплата корзины: {pay_url}")
            else:
                bot.send_message(order_chat, "Не удалось получить ссылку на оплату.")
            bot.answer_callback_query(call.id, "Счёт создан, проверьте сообщения")
            return

        # cancel payment
        if data == "cancel_payment":
            bot.answer_callback_query(call.id, "Оплата отменена")
            try:
                bot.send_message(cid, "Оплата отменена.", reply_markup=main_menu_markup())
            except:
                pass
            return

    except Exception:
        traceback.print_exc()
        try:
            bot.answer_callback_query(call.id, "Ошибка обработки")
        except:
            pass
# -------------------------
# Support notifications / listing
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

def show_requests_page(operator_chat:int, page:int, message_reference=None):
    per_page = 5
    offset = (page-1) * per_page
    rows = get_open_requests(offset=offset, limit=per_page)
    total = get_open_requests_count()
    total_pages = (total + per_page - 1) // per_page if total else 1
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
# Text handler
# -------------------------
@bot.message_handler(content_types=['text'])
def handle_text(m):
    try:
        cid = m.chat.id
        text = m.text.strip()
        ensure_user(cid, m)

        # Admin commands
        if text.startswith("/add_operator"):
            if m.from_user.id not in ADMIN_IDS:
                bot.reply_to(m, "Нет прав."); return
            parts = text.split()
            if len(parts) != 2:
                bot.reply_to(m, "Использование: /add_operator <chat_id>"); return
            new_id = int(parts[1])
            add_operator(new_id, username=None, display_name=None)
            bot.reply_to(m, f"Добавлен оператор {new_id}"); return

        if text.startswith("/operator_remove"):
            if m.from_user.id not in ADMIN_IDS:
                bot.reply_to(m, "Нет прав."); return
            parts = text.split()
            if len(parts) != 2:
                bot.reply_to(m, "Использование: /operator_remove <chat_id>"); return
            rem = int(parts[1])
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

        # Support & reply
        state = user_state.get(cid)
        if state and state.get("awaiting_support_msg"):
            user_state.pop(cid, None)
            uname = m.from_user.username or f"id{cid}"
            rid = create_support_request(cid, uname, text)
            bot.reply_to(m, "✅ Ваше обращение отправлено. Ожидайте ответа.")
            notify_all_operators_new_request()
            return

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

        bot.send_message(cid, "Не понял. Нажми /start для меню.")
    except Exception:
        traceback.print_exc()
        bot.reply_to(m, "Ошибка обработки.")

# -------------------------
# Flask endpoints
# -------------------------
@app.route("/cryptobot/ipn", methods=["POST"])
def cryptobot_ipn():
    try:
        payload = request.get_json(force=True)
    except:
        return jsonify({"ok": False, "error": "bad json"}), 400
    try:
        with open(IPN_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": datetime.utcnow().isoformat(), "payload": payload}, ensure_ascii=False) + "\n")
    except:
        pass

    invoice_id = str(payload.get("invoiceId") or payload.get("id") or "")
    status = str(payload.get("status") or payload.get("paymentStatus") or "").lower()
    if any(x in status for x in ["paid", "success", "confirmed", "finished"]):
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT * FROM invoices_map WHERE invoice_id = ?", (invoice_id,))
        r = cur.fetchone()
        if r:
            if r["order_id"]:
                cur.execute("UPDATE orders SET status = ? WHERE id = ?", ("оплачен", r["order_id"]))
                conn.commit()
                try:
                    bot.send_message(r["chat_id"], f"✅ Оплата получена за заказ #{r['order_id']}. Спасибо!")
                except:
                    pass
            elif r["cart_id"]:
                cart_id = int(r["cart_id"])
                cur.execute("SELECT * FROM cart_items WHERE cart_id = ?", (cart_id,))
                items = [dict(x) for x in cur.fetchall()]
                for it in items:
                    create_order_from_cart_item(r["chat_id"], it, status="оплачен")
                mark_cart_paid(cart_id)
                conn.commit()
                try:
                    bot.send_message(r["chat_id"], f"✅ Оплата получена за корзину #{cart_id}. Заказы созданы.")
                except:
                    pass
        conn.close()
    return jsonify({"ok": True}), 200

@app.route("/" + BOT_TOKEN, methods=["POST"])
def telegram_webhook():
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200

@app.route("/", methods=["GET"])
def index():
    return "Bot OK", 200

# -------------------------
# Startup
# -------------------------
if __name__ == "__main__":
    print("🚀 Starting SaleTest full bot")
    init_db()
    if USE_WEBHOOK:
        set_telegram_webhook()
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
    else:
        Thread(target=lambda: app.run(host="0.0.0.0", port=5000), daemon=True).start()
        bot.infinity_polling(timeout=60, long_polling_timeout=20)

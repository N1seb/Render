#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SaleTest_full_part1.py — часть 1/2
Первая половина полнофункционального Telegram-магазина:
- соцсети -> услуги
- корзина (несколько товаров в корзине, оплата всей корзины)
- надёжная разборка callback-данных (чтобы не было "Неверный формат заказа")
- SQLite-хранилище
- поддержка/операторы/админка
В части 2/2 будет остальная логика оплаты, IPN, вебхуки и запуск.
"""

from __future__ import annotations

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
from typing import Optional, Dict, Any, List, Tuple

from flask import Flask, request, jsonify
import telebot
from telebot import types

# -------------------------
# CONFIG - изменяйте через ENV
# -------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN") or "8587164094:AAEcsW0oUMg1Hphbymdg3NHtH_Q25j7RyWo"
CRYPTOPAY_API_TOKEN = os.environ.get("CRYPTOPAY_API_TOKEN") or "484313:AA6FJU50A2cMhJas5ruR6PD15Jl5F1XMrN7"
WEB_DOMAIN = os.environ.get("WEB_DOMAIN") or "https://render-jj8d.onrender.com"
USE_WEBHOOK = os.environ.get("USE_WEBHOOK", "0") == "1"

ADMIN_IDS = set([int(os.environ.get("ADMIN_ID") or 1942740947)])
INITIAL_OPERATORS = [7771789412]

CRYPTO_API_BASE = "https://pay.crypt.bot/api"

DB_FILE = os.environ.get("DB_FILE") or "salebot_full.sqlite"
IPN_LOG_FILE = os.environ.get("IPN_LOG_FILE") or "ipn_log.jsonl"

# Поддерживаемые валюты (как просили)
AVAILABLE_ASSETS = ["USDT", "TON", "TRX"]

# -------------------------
# SERVICES / PRICE TEMPLATE
# -------------------------
# Вы предоставили, что прайс-лист уже умножен вами — сюда вставляйте реальные числа.
# Формат: SERVICES[social][service_key] = {
#    "title": str,
#    "min": int,           <- минимальное количество
#    "unit": int,          <- единица подсчёта (например, views может быть unit=1000)
#    "price_usd_per_unit": float  <- цена за единицу (unit)
# }
# Вы говорили, что уже умножили цены — оставляю вам возможность редактировать значения.
SERVICES: Dict[str, Dict[str, Dict[str, Any]]] = {
    "Instagram": {
        "sub":  {"title": "Подписчики", "min": 10,   "unit": 1,    "price_usd_per_unit": 0.10},
        "view": {"title": "Просмотры",  "min": 1000, "unit": 1000, "price_usd_per_unit": 5.00},  # $5 за 1000
        "com":  {"title": "Комментарии", "min": 10,   "unit": 1,    "price_usd_per_unit": 0.20},
        "like": {"title": "Лайки",      "min": 10,   "unit": 1,    "price_usd_per_unit": 0.05},
    },
    "TikTok": {
        "sub":  {"title": "Подписчики", "min": 10,   "unit": 1,    "price_usd_per_unit": 0.12},
        "view": {"title": "Просмотры",  "min": 1000, "unit": 1000, "price_usd_per_unit": 6.00},
        "like": {"title": "Лайки",      "min": 10,   "unit": 1,    "price_usd_per_unit": 0.06},
    },
    "YouTube": {
        "sub":  {"title": "Подписчики", "min": 10,   "unit": 1,    "price_usd_per_unit": 0.50},
        "view": {"title": "Просмотры",  "min": 1000, "unit": 1000, "price_usd_per_unit": 3.00},
        "com":  {"title": "Комментарии", "min": 5,    "unit": 1,    "price_usd_per_unit": 0.30},
    },
    "Telegram": {
        "sub":  {"title": "Подписчики (канал)", "min": 10, "unit": 1, "price_usd_per_unit": 0.08},
        "view": {"title": "Просмотры",           "min": 10, "unit": 1, "price_usd_per_unit": 0.02},
    },
    "Facebook": {
        "sub":  {"title": "Подписчики", "min": 10, "unit": 1, "price_usd_per_unit": 0.09},
        "like": {"title": "Реакции",    "min": 10, "unit": 1, "price_usd_per_unit": 0.05},
    },
    "Twitter/X": {
        "sub":     {"title": "Подписчики", "min": 10, "unit": 1, "price_usd_per_unit": 0.07},
        "retweet": {"title": "Репосты/ретвиты", "min": 5, "unit": 1, "price_usd_per_unit": 0.50},
    }
}

# список хороших отображений соцсетей
PRETTY_SOCIALS = list(SERVICES.keys())

# -------------------------
# Sanity checks
# -------------------------
if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN must be set and contain a colon (:)")

# -------------------------
# Bot and Flask init
# -------------------------
bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
app = Flask(__name__)

# -------------------------
# DB: init and helpers
# -------------------------
def get_db() -> sqlite3.Connection:
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
    # orders - completed entries or single-item orders
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
    # carts - one active open cart per user
    cur.execute("""
    CREATE TABLE IF NOT EXISTS carts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        status TEXT, -- open, paid, cancelled
        created_at TEXT,
        updated_at TEXT
    )""")
    # cart_items
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
    # operator notifications for editing messages
    cur.execute("""
    CREATE TABLE IF NOT EXISTS operator_notifications (
        operator_chat INTEGER PRIMARY KEY,
        message_id INTEGER,
        created_at TEXT
    )""")
    conn.commit()
    conn.close()

# ensure DB exists
init_db()

# -------------------------
# DB utility functions
# -------------------------
def ensure_user(chat_id: int, message: Optional[telebot.types.Message] = None):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,))
    if not cur.fetchone() and message is not None:
        now = datetime.utcnow().isoformat()
        cur.execute("INSERT INTO users (chat_id, username, first_name, last_name, created_at) VALUES (?, ?, ?, ?, ?)",
                    (chat_id,
                     getattr(message.from_user, "username", None),
                     getattr(message.from_user, "first_name", None),
                     getattr(message.from_user, "last_name", None),
                     now))
        conn.commit()
    conn.close()

def get_or_create_cart(chat_id: int) -> int:
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

def add_item_to_cart(cart_id: int, social: str, service_key: str, amount: int, link: str, price_usd: float) -> int:
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO cart_items (cart_id, social, service_key, amount, price_usd, link, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cart_id, social, service_key, amount, price_usd, link, now))
    cur.execute("UPDATE carts SET updated_at = ? WHERE id = ?", (now, cart_id))
    conn.commit()
    item_id = cur.lastrowid
    conn.close()
    return item_id

def get_cart_items(cart_id: int) -> List[Dict[str, Any]]:
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM cart_items WHERE cart_id = ?", (cart_id,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def remove_cart_item(item_id: int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM cart_items WHERE id = ?", (item_id,))
    conn.commit(); conn.close()

def clear_cart(cart_id: int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM cart_items WHERE cart_id = ?", (cart_id,))
    cur.execute("UPDATE carts SET status = ?, updated_at = ? WHERE id = ?", ("cancelled", datetime.utcnow().isoformat(), cart_id))
    conn.commit(); conn.close()

def mark_cart_paid(cart_id: int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE carts SET status = ?, updated_at = ? WHERE id = ?", ("paid", datetime.utcnow().isoformat(), cart_id))
    conn.commit(); conn.close()

def create_order_from_cart_item(chat_id: int, cart_item: dict, status: str = "awaiting_payment") -> int:
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("""INSERT INTO orders (chat_id, social, service_key, amount, price_usd, link, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (chat_id, cart_item["social"], cart_item["service_key"], cart_item["amount"], cart_item["price_usd"],
                 cart_item["link"], status, now, now))
    oid = cur.lastrowid
    conn.commit(); conn.close()
    return oid

def create_single_order(chat_id:int, social:str, service_key:str, amount:int, price_usd:float, link:str, status:str="awaiting_payment") -> int:
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

def set_invoice_mapping(invoice_id: str, chat_id: int, order_id: Optional[int] = None, cart_id: Optional[int] = None, raw_payload: Optional[Any] = None):
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO invoices_map (invoice_id, chat_id, order_id, cart_id, raw_payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (invoice_id, chat_id, order_id, cart_id, json.dumps(raw_payload, ensure_ascii=False) if raw_payload else None, now))
    conn.commit(); conn.close()

def list_operators() -> List[Dict[str, Any]]:
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM operators ORDER BY id")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close(); return rows

def add_operator(chat_id:int, username:Optional[str]=None, display_name:Optional[str]=None):
    conn = get_db(); cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute("INSERT OR IGNORE INTO operators (chat_id, username, display_name, created_at) VALUES (?, ?, ?, ?)", (chat_id, username, display_name, now))
    conn.commit(); conn.close()
    # ensure notification row
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO operator_notifications (operator_chat, message_id, created_at) VALUES (?, ?, ?)", (chat_id, None, now))
    conn.commit(); conn.close()

def remove_operator(chat_id:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM operators WHERE chat_id = ?", (chat_id,))
    cur.execute("DELETE FROM operator_notifications WHERE operator_chat = ?", (chat_id,))
    conn.commit(); conn.close()

def get_open_requests_count() -> int:
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM support_requests WHERE status = 'open'")
    r = cur.fetchone(); conn.close(); return r["c"] if r else 0

def create_support_request(user_chat:int, username:str, text:str) -> int:
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO support_requests (user_chat, username, text, status, created_at) VALUES (?, ?, ?, ?, ?)", (user_chat, username, text, "open", now))
    rid = cur.lastrowid
    cur.execute("INSERT INTO support_messages (req_id, from_chat, to_chat, text, created_at) VALUES (?, ?, ?, ?, ?)", (rid, user_chat, None, text, now))
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
    cur.execute("INSERT INTO support_messages (req_id, from_chat, to_chat, text, created_at) VALUES (?, ?, ?, ?, ?)", (req_id, from_chat, to_chat, text, now))
    conn.commit(); conn.close()

# -------------------------
# CryptoBot helpers (part 1)
# -------------------------
def create_cryptobot_invoice(amount_value: float, asset: str, payload: str, description: str, callback_url: Optional[str] = None) -> dict:
    """
    Создаёт инвойс через CryptoBot API.
    Возвращает dict (успех — JSON ответа API, иначе {"error": True, ...})
    """
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

def get_invoice_info(invoice_id: str) -> dict:
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
def generate_qr_bytes(url: str) -> bytes:
    img = qrcode.make(url)
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return buf.read()

# -------------------------
# Price conversion helpers
# -------------------------
def convert_price_usd_to_asset(price_usd: float, asset: str) -> float:
    asset = asset.upper()
    # USDT ~ 1 USD
    if asset == "USDT":
        return round(price_usd, 6)
    try:
        if asset == "TON":
            r = requests.get("https://api.coingecko.com/api/v3/simple/price", params={"ids": "toncoin", "vs_currencies": "usd"}, timeout=8)
            j = r.json()
            ton_usd = float(j["toncoin"]["usd"])
            return round(price_usd / ton_usd, 6)
        if asset == "TRX":
            r = requests.get("https://api.coingecko.com/api/v3/simple/price", params={"ids": "tron", "vs_currencies": "usd"}, timeout=8)
            j = r.json()
            trx_usd = float(j["tron"]["usd"])
            return round(price_usd / trx_usd, 6)
        return round(price_usd, 6)
    except Exception:
        return round(price_usd, 6)

# -------------------------
# UI helpers (markup builders)
# -------------------------
def main_menu_markup() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🛒 Магазин", callback_data="menu_shop"))
    kb.add(types.InlineKeyboardButton("🧾 Корзина / Профиль", callback_data="profile"))
    kb.add(types.InlineKeyboardButton("✉️ Поддержка (в боте)", callback_data="support_bot"))
    kb.add(types.InlineKeyboardButton("📞 Поддержка (лично)", callback_data="support_personal"))
    return kb

def shop_socials_markup() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    for s in PRETTY_SOCIALS:
        kb.add(types.InlineKeyboardButton(s, callback_data=f"shop_social::{s}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_main"))
    return kb

def services_markup_for_social(social: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    services = SERVICES.get(social, {})
    for key, info in services.items():
        unit = info.get("unit", 1)
        price = info.get("price_usd_per_unit", 0.0)
        if unit > 1:
            label = f"{info['title']} — ${price:.2f} / {unit}"
        else:
            label = f"{info['title']} — ${price:.2f} / 1"
        # use :: separator to prevent accidental splits by underscores in names
        kb.add(types.InlineKeyboardButton(label, callback_data=f"service::{social}::{key}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="shop_socials"))
    return kb

def currency_selection_markup_for_cart(chat_id: int, cart_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=3)
    for asset in AVAILABLE_ASSETS:
        # pay_cart::<chatid>::<cartid>::<asset>
        kb.add(types.InlineKeyboardButton(asset, callback_data=f"pay_cart::{chat_id}::{cart_id}::{asset}"))
    kb.add(types.InlineKeyboardButton("🔙 Отмена", callback_data="cancel_payment"))
    return kb

def currency_selection_markup_for_order(chat_id: int, order_ref: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=3)
    for asset in AVAILABLE_ASSETS:
        kb.add(types.InlineKeyboardButton(asset, callback_data=f"pay_order::{order_ref}::{asset}"))
    kb.add(types.InlineKeyboardButton("🔙 Отмена", callback_data="cancel_payment"))
    return kb

# -------------------------
# In-memory short states
# -------------------------
user_state: Dict[int, Dict[str, Any]] = {}
operator_state: Dict[int, Dict[str, Any]] = {}

# Ensure initial operators
for op in INITIAL_OPERATORS:
    try:
        add_operator(op)
    except Exception:
        pass

# -------------------------
# Bot handlers - messages & callbacks (start of main flow)
# -------------------------
@bot.message_handler(commands=['start'])
def cmd_start(m: telebot.types.Message):
    ensure_user(m.chat.id, m)
    bot.send_message(m.chat.id, "🧸 Добро пожаловать! Выберите действие:", reply_markup=main_menu_markup())

@bot.callback_query_handler(func=lambda c: True)
def cb_all(call: telebot.types.CallbackQuery):
    """
    Главный обработчик callback'ов.
    Используем '::' как разделитель в callback_data, чтобы избежать проблем с '_' в названиях.
    """
    try:
        data = call.data or ""
        cid = call.message.chat.id
        uid = call.from_user.id

        # navigation
        if data == "menu_shop":
            bot.edit_message_text("Выберите соцсеть:", cid, call.message.message_id, reply_markup=shop_socials_markup())
            return
        if data == "back_main":
            bot.edit_message_text("Главное меню:", cid, call.message.message_id, reply_markup=main_menu_markup())
            return
        if data == "shop_socials":
            bot.edit_message_text("Выберите соцсеть:", cid, call.message.message_id, reply_markup=shop_socials_markup())
            return

        # support instructions
        if data == "support_personal":
            bot.edit_message_text("📨 Чтобы получить помощь в личных сообщениях — найдите оператора в Telegram.\n\nЕсли хотите написать через бота — нажмите «Поддержка (в боте)».", cid, call.message.message_id)
            return

        if data == "support_bot":
            user_state[cid] = {"awaiting_support_msg": True}
            bot.send_message(cid, "✉️ Напишите ваше сообщение для поддержки. Оно будет отправлено операторам.")
            bot.answer_callback_query(call.id)
            return

        # shop_social::<SocialName>
        if data.startswith("shop_social::"):
            social = data.split("::", 1)[1]
            if social not in SERVICES:
                bot.answer_callback_query(call.id, "Ошибка: неизвестная соцсеть")
                return
            bot.edit_message_text(f"Услуги для {social}:", cid, call.message.message_id, reply_markup=services_markup_for_social(social))
            return

        # service::<social>::<key>
        if data.startswith("service::"):
            parts = data.split("::")
            if len(parts) != 3:
                bot.answer_callback_query(call.id, "Неверные данные сервиса")
                return
            _, social, key = parts
            svc = SERVICES.get(social, {}).get(key)
            if not svc:
                bot.answer_callback_query(call.id, "Сервис не найден")
                return
            # prepare state for quantity input
            min_allowed = int(svc.get("min", 1))
            unit = int(svc.get("unit", 1))
            price_unit = float(svc.get("price_usd_per_unit", 0.0))
            user_state[cid] = {"awaiting_qty_for": True, "social": social, "service_key": key,
                               "min": min_allowed, "unit": unit, "price_unit": price_unit}
            bot.send_message(cid, f"Вы выбрали: {svc['title']} ({social}). Минимум: {min_allowed}. Введите количество (целое число):")
            bot.answer_callback_query(call.id)
            return

        # profile / cart
        if data == "profile":
            cart_id = get_or_create_cart(cid)
            items = get_cart_items(cart_id)
            txt_lines = []
            total = 0.0
            kb = types.InlineKeyboardMarkup(row_width=1)
            txt_lines.append("🧾 Ваша корзина:\n")
            if items:
                for it in items:
                    title = SERVICES[it['social']][it['service_key']]['title']
                    txt_lines.append(f"#{it['id']} | {it['social']} — {title} x{it['amount']} — ${float(it['price_usd']):.2f}\nLink: {it['link']}\n")
                    kb.add(types.InlineKeyboardButton(f"Удалить #{it['id']}", callback_data=f"cart_remove::{it['id']}"))
                    total += float(it['price_usd'])
                txt_lines.append(f"\nИтого: ${total:.2f}\n")
                kb.add(types.InlineKeyboardButton("Оплатить корзину", callback_data=f"cart_pay::{cart_id}"))
                kb.add(types.InlineKeyboardButton("Очистить корзину", callback_data=f"cart_clear::{cart_id}"))
            else:
                txt_lines.append("Корзина пуста.\n")
            # user orders (recent)
            conn = get_db(); cur = conn.cursor()
            cur.execute("SELECT * FROM orders WHERE chat_id = ? ORDER BY id DESC LIMIT 10", (cid,))
            rows = cur.fetchall(); conn.close()
            if rows:
                txt_lines.append("\n📋 Последние заказы:\n")
                for r in rows:
                    title = SERVICES[r['social']][r['service_key']]['title']
                    txt_lines.append(f"#{r['id']} | {r['social']} {title} x{r['amount']} — ${r['price_usd']:.2f} — {r['status']}\n")
            bot.send_message(cid, "\n".join(txt_lines), reply_markup=kb)
            bot.answer_callback_query(call.id)
            return

        # cart_remove::<item_id>
        if data.startswith("cart_remove::"):
            parts = data.split("::")
            try:
                item_id = int(parts[1])
            except Exception:
                bot.answer_callback_query(call.id, "Ошибка удаления")
                return
            remove_cart_item(item_id)
            bot.answer_callback_query(call.id, "Элемент удалён")
            # don't try to reconstruct the whole cart here — user can open Корзина снова
            try:
                bot.edit_message_text("Элемент удалён. Откройте '🧾 Корзина / Профиль' снова.", cid, call.message.message_id)
            except Exception:
                pass
            return

        # cart_clear::<cart_id>
        if data.startswith("cart_clear::"):
            parts = data.split("::")
            try:
                cart_id = int(parts[1])
            except Exception:
                bot.answer_callback_query(call.id, "Ошибка")
                return
            clear_cart(cart_id)
            bot.answer_callback_query(call.id, "Корзина очищена")
            try:
                bot.edit_message_text("Корзина очищена. Откройте '🧾 Корзина / Профиль' снова.", cid, call.message.message_id)
            except Exception:
                pass
            return

        # cart_pay::<cart_id> -> show currency options
        if data.startswith("cart_pay::"):
            parts = data.split("::")
            try:
                cart_id = int(parts[1])
            except Exception:
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

        # pay_cart::<chatid>::<cartid>::<asset>
        if data.startswith("pay_cart::"):
            # will handle in part 2 (creating invoice + mapping); here only sanity-parse and reply fast
            parts = data.split("::")
            if len(parts) != 4:
                bot.answer_callback_query(call.id, "Неверные данные оплаты")
                return
            _, chat_str, cartid_str, asset = parts
            if not chat_str.isdigit() or not cartid_str.isdigit():
                bot.answer_callback_query(call.id, "Неправильные идентификаторы")
                return
            order_chat = int(chat_str)
            cart_id = int(cartid_str)
            items = get_cart_items(cart_id)
            if not items:
                bot.answer_callback_query(call.id, "Корзина пуста")
                return
            total_usd = sum(float(it['price_usd']) for it in items)
            # We'll call create invoice and send link in part2; but show immediate ack here
            bot.answer_callback_query(call.id, "Создаю счёт, проверьте личные сообщения...")
            # delegate to payment function that will be implemented in part 2 via helper (we can also call here if part2 is sent)
            # To avoid blocking: create a background thread to call payment function (part2)
            Thread(target=handle_cart_payment_background, args=(order_chat, cart_id, asset), daemon=True).start()
            return

        # pay_order::<order_ref>::<asset> - pay single order
        if data.startswith("pay_order::"):
            parts = data.split("::")
            # order_ref expected format: order_<chatid>_<orderid>
            if len(parts) != 3:
                bot.answer_callback_query(call.id, "Неверный формат")
                return
            _, order_ref, asset = parts
            # parse order_ref
            ref_parts = order_ref.split("_")
            if len(ref_parts) < 3:
                bot.answer_callback_query(call.id, "Неверный формат заказа")
                return
            try:
                # last two parts are chatid and orderid
                order_chat = int(ref_parts[-2])
                order_id = int(ref_parts[-1])
            except Exception:
                bot.answer_callback_query(call.id, "Неверный формат заказа")
                return
            # spawn background payment (actual invoice creation in part2)
            bot.answer_callback_query(call.id, "Создаю счёт, проверьте личные сообщения...")
            Thread(target=handle_single_order_payment_background, args=(order_chat, order_id, asset), daemon=True).start()
            return

        # cancel_payment
        if data == "cancel_payment":
            bot.answer_callback_query(call.id, "Оплата отменена")
            try:
                bot.send_message(cid, "Оплата отменена.", reply_markup=main_menu_markup())
            except Exception:
                pass
            return

        # operator support navigation
        if data.startswith("open_requests_page::"):
            try:
                page = int(data.split("::")[-1])
            except Exception:
                page = 1
            show_requests_page(call.from_user.id, page, call.message)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("req::"):
            try:
                req_id = int(data.split("::")[1])
            except Exception:
                bot.answer_callback_query(call.id, "Bad request id"); return
            req = get_request_by_id(req_id)
            if not req or req["status"] != "open":
                bot.answer_callback_query(call.id, "Обращение не найдено или закрыто"); return
            text = f"📨 Обращение #{req['id']}\nОт: {req['username']} (id {req['user_chat']})\n\n{req['text']}\n\nНажмите Ответить, чтобы отправить ответ и закрыть обращение."
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton("Ответить", callback_data=f"reply_req::{req['id']}"))
            kb.add(types.InlineKeyboardButton("Назад к списку", callback_data="open_requests_page::1"))
            try:
                bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)
            except Exception:
                bot.send_message(call.message.chat.id, text, reply_markup=kb)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("reply_req::"):
            try:
                req_id = int(data.split("::")[-1])
            except:
                bot.answer_callback_query(call.id, "Bad conv id"); return
            if call.from_user.id not in [o["chat_id"] for o in list_operators()]:
                bot.answer_callback_query(call.id, "У вас нет прав оператора"); return
            operator_state[call.from_user.id] = {"awaiting_reply_for": req_id, "message_id": call.message.message_id}
            bot.send_message(call.from_user.id, "Введите ответ для пользователя (сообщение будет отправлено и обращение закроется):")
            bot.answer_callback_query(call.id)
            return

        # unknown
        bot.answer_callback_query(call.id, "Неизвестная команда.")
    except Exception:
        traceback.print_exc()
        try:
            bot.answer_callback_query(call.id, "Ошибка обработки")
        except Exception:
            pass

# -------------------------
# Background payment stubs (will be implemented in part 2)
# -------------------------
def handle_cart_payment_background(order_chat: int, cart_id: int, asset: str):
    """
    Заглушка: реальная логика создания инвойса и отправки ссылки на оплату
    будет реализована в части 2. Здесь — мы аккуратно:
    - считаем total
    - создаём invoice через create_cryptobot_invoice (part2 will call it)
    - сохраняем mapping
    - отправляем QR/link
    """
    try:
        items = get_cart_items(cart_id)
        if not items:
            try:
                bot.send_message(order_chat, "Корзина пуста.")
            except:
                pass
            return
        total_usd = sum(float(it['price_usd']) for it in items)
        # convert -> amount in asset
        pay_amount = convert_price_usd_to_asset(total_usd, asset.upper())
        order_uid = f"cart_{order_chat}_{cart_id}_{int(time.time())}"
        description = f"Оплата корзины #{cart_id} пользователем {order_chat}"
        callback_url = WEB_DOMAIN.rstrip("/") + "/cryptobot/ipn"
        # call API - safe wrapper
        resp = create_cryptobot_invoice(pay_amount, asset.upper(), order_uid, description, callback_url=callback_url)
        if isinstance(resp, dict) and resp.get("error"):
            try:
                bot.send_message(order_chat, f"Ошибка при создании чека: {resp}")
            except:
                pass
            return
        invoice_id = resp.get("invoiceId") or resp.get("id") or resp.get("invoice_id")
        pay_url = resp.get("pay_url") or resp.get("payment_url") or (resp.get("result", {}).get("pay_url") if isinstance(resp, dict) else None)
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
    except Exception:
        traceback.print_exc()
        try:
            bot.send_message(order_chat, "Ошибка при создании оплаты.")
        except:
            pass

def handle_single_order_payment_background(order_chat: int, order_id: int, asset: str):
    """
    Создаёт инвойс для одного заказа (order_id). Логика — аналогична корзине.
    """
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        r = cur.fetchone(); conn.close()
        if not r:
            try:
                bot.send_message(order_chat, "Заказ не найден.")
            except:
                pass
            return
        total_usd = float(r["price_usd"])
        pay_amount = convert_price_usd_to_asset(total_usd, asset.upper())
        order_uid = f"order_{order_chat}_{order_id}_{int(time.time())}"
        description = f"Оплата заказа #{order_id}"
        callback_url = WEB_DOMAIN.rstrip("/") + "/cryptobot/ipn"
        resp = create_cryptobot_invoice(pay_amount, asset.upper(), order_uid, description, callback_url=callback_url)
        if isinstance(resp, dict) and resp.get("error"):
            try:
                bot.send_message(order_chat, f"Ошибка при создании чека: {resp}")
            except:
                pass
            return
        invoice_id = resp.get("invoiceId") or resp.get("id") or resp.get("invoice_id")
        pay_url = resp.get("pay_url") or resp.get("payment_url") or (resp.get("result", {}).get("pay_url") if isinstance(resp, dict) else None)
        if invoice_id:
            set_invoice_mapping(str(invoice_id), order_chat, order_id=order_id, cart_id=None, raw_payload=resp)
            update_order_invoice(order_id, str(invoice_id), pay_url)
        if pay_url:
            try:
                qr = generate_qr_bytes(pay_url)
                bot.send_photo(order_chat, qr, caption=f"💳 Оплата заказа #{order_id} через {asset.upper()}\nСсылка: {pay_url}")
            except Exception:
                bot.send_message(order_chat, f"💳 Оплата заказа: {pay_url}")
        else:
            bot.send_message(order_chat, "Не удалось получить ссылку на оплату.")
    except Exception:
        traceback.print_exc()
        try:
            bot.send_message(order_chat, "Ошибка при создании оплаты.")
        except:
            pass

# -------------------------
# Support notifications & pagination
# -------------------------
def _store_operator_notification(op_chat:int, msg_id:Optional[int]):
    conn = get_db(); cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute("INSERT OR REPLACE INTO operator_notifications (operator_chat, message_id, created_at) VALUES (?, ?, ?)", (op_chat, msg_id, now))
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
        kb.add(types.InlineKeyboardButton("Перейти к обращениям", callback_data=f"open_requests_page::1"))
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
        kb.add(types.InlineKeyboardButton(f"{r['id']} | {uname}", callback_data=f"req::{r['id']}"))
    nav = types.InlineKeyboardMarkup(row_width=3)
    prev_page = page-1 if page>1 else 1
    next_page = page+1 if page<total_pages else total_pages
    nav.add(types.InlineKeyboardButton("◀️", callback_data=f"open_requests_page::{prev_page}"),
            types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"),
            types.InlineKeyboardButton("▶️", callback_data=f"open_requests_page::{next_page}"))
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
# Text message handler (orders, support, admin)
# -------------------------
@bot.message_handler(content_types=['text'])
def handle_text(m: telebot.types.Message):
    try:
        cid = m.chat.id
        text = (m.text or "").strip()
        ensure_user(cid, m)

        # ADMIN: add operator
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

        # ADMIN: remove operator
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

        # ADMIN: list operators
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

        # ADMIN: show recent orders
        if text.startswith("/sadm"):
            if m.from_user.id not in ADMIN_IDS:
                bot.reply_to(m, "Нет прав.")
                return
            conn = get_db(); cur = conn.cursor()
            cur.execute("SELECT id, chat_id, social, service_key, amount, price_usd, status FROM orders ORDER BY id DESC LIMIT 200")
            rows = cur.fetchall(); conn.close()
            if not rows:
                bot.reply_to(m, "Заказов нет.")
                return
            txt = "Последние заказы:\n\n"
            for r in rows:
                svc_title = SERVICES.get(r["social"],{}).get(r["service_key"],{}).get("title", r["service_key"])
                txt += f"#{r['id']} uid:{r['chat_id']} {r['social']} {svc_title} x{r['amount']} — ${r['price_usd']:.2f} — {r['status']}\n"
            bot.reply_to(m, txt)
            return

        # USER: states
        state = user_state.get(cid)

        # awaiting quantity
        if state and state.get("awaiting_qty_for"):
            if not text.isdigit():
                bot.reply_to(m, "Введите целое число.")
                return
            qty = int(text)
            minq = state.get("min", 1)
            if qty < minq:
                bot.reply_to(m, f"Минимум {minq}")
                return
            unit = state.get("unit", 1)
            price_unit = float(state.get("price_unit", 0.0))
            price = price_unit * (qty / unit)
            price = round(price, 2)
            # ask link
            user_state[cid] = {"awaiting_link_for": True, "social": state["social"], "service_key": state["service_key"], "quantity": qty, "price_usd": price}
            bot.reply_to(m, f"Цена: ${price:.2f}. Теперь отправьте ссылку на аккаунт/пост/канал (http/https):")
            return

        # awaiting link
        if state and state.get("awaiting_link_for"):
            link = text
            if not (link.startswith("http://") or link.startswith("https://")):
                bot.reply_to(m, "Неверная ссылка. Должна начинаться с http/https")
                return
            social = state["social"]; service_key = state["service_key"]; qty = state["quantity"]; price = float(state["price_usd"])
            cart_id = get_or_create_cart(cid)
            add_item_to_cart(cart_id, social, service_key, qty, link, price)
            user_state.pop(cid, None)
            bot.reply_to(m, f"✅ Товар добавлен в корзину. Цена: ${price:.2f}\nПерейти в неё можно через '🧾 Корзина / Профиль'", reply_markup=main_menu_markup())
            return

        # awaiting support message
        if state and state.get("awaiting_support_msg"):
            user_state.pop(cid, None)
            uname = m.from_user.username or f"id{cid}"
            rid = create_support_request(cid, uname, text)
            bot.reply_to(m, "✅ Ваше обращение отправлено. Ожидайте ответа.")
            notify_all_operators_new_request()
            return

        # operator reply flow
        opstate = operator_state.get(cid)
        if opstate and opstate.get("awaiting_reply_for"):
            req_id = opstate["awaiting_reply_for"]
            req = get_request_by_id(req_id)
            if not req:
                bot.reply_to(m, "Обращение не найдено.")
                operator_state.pop(cid, None)
                return
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

        # fallback
        bot.send_message(cid, "Не понял. Нажми /start для меню.")
    except Exception:
        traceback.print_exc()
        try:
            bot.reply_to(m, "Внутренняя ошибка.")
        except:
            pass

# -------------------------
# End of part 1/2
# -------------------------
# В part 2/2 будут:
# - обработка webhook / IPN / подтверждение оплаты (cryptobot_ipn)
# - завершение логики создания инвойсов (если требуется дополнительная устойчивость)
# - запуск (webhook/polling) и мелкие фиксы
#
# Отправляю вторую часть сейчас же по вашему запросу.
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SaleTest_full_part2.py — часть 2/2
Продолжение кода Telegram-магазина:
- CryptoBot IPN обработчик (авто-зачёт оплаты)
- логика завершения заказов/корзин после оплаты
- завершение Flask webhook
- запуск бота (webhook / polling)
"""

import os
import json
import traceback
from datetime import datetime
from flask import request, jsonify

# Flask и bot уже объявлены в части 1
# Используем их напрямую

# -------------------------
# Logging utility
# -------------------------
def log_ipn_event(data: dict):
    try:
        with open(IPN_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": datetime.utcnow().isoformat(), "data": data}, ensure_ascii=False) + "\n")
    except Exception:
        traceback.print_exc()


# -------------------------
# Payment confirmation (IPN endpoint)
# -------------------------
@app.route("/cryptobot/ipn", methods=["POST"])
def cryptobot_ipn():
    """
    Обработчик уведомлений от CryptoBot.
    Приходит JSON с данными инвойса и статусом.
    """
    try:
        payload = request.get_json(force=True)
        log_ipn_event(payload)

        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "Invalid JSON"}), 400

        status = payload.get("status")
        invoice_id = str(payload.get("invoice_id") or payload.get("invoiceId") or "")
        amount = float(payload.get("amount", 0))
        asset = payload.get("asset")
        payload_str = str(payload.get("payload", ""))

        # Нам важны invoice_id и status == "paid"
        if not invoice_id:
            return jsonify({"ok": False, "error": "Missing invoice_id"}), 400

        if status and status.lower() == "paid":
            # Найдём invoice_id в базе
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT * FROM invoices_map WHERE invoice_id = ?", (invoice_id,))
            inv = cur.fetchone()
            conn.close()

            if not inv:
                return jsonify({"ok": False, "error": "Unknown invoice"}), 404

            chat_id = inv["chat_id"]
            order_id = inv["order_id"]
            cart_id = inv["cart_id"]

            # если это корзина — закрываем все позиции
            if cart_id:
                try:
                    items = get_cart_items(cart_id)
                    for it in items:
                        create_order_from_cart_item(chat_id, it, status="paid")
                    mark_cart_paid(cart_id)
                    try:
                        bot.send_message(chat_id, f"✅ Оплата корзины #{cart_id} на сумму {amount} {asset} получена!\nЗаказы будут выполнены в ближайшее время.", reply_markup=main_menu_markup())
                    except Exception:
                        pass
                except Exception:
                    traceback.print_exc()

            # если это одиночный заказ
            if order_id:
                conn = get_db()
                cur = conn.cursor()
                cur.execute("UPDATE orders SET status = 'paid', updated_at = ? WHERE id = ?", (datetime.utcnow().isoformat(), order_id))
                conn.commit()
                conn.close()
                try:
                    bot.send_message(chat_id, f"✅ Оплата заказа #{order_id} ({amount} {asset}) подтверждена!\nЗаказ принят в работу.", reply_markup=main_menu_markup())
                except Exception:
                    pass

        return jsonify({"ok": True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


# -------------------------
# Webhook route for Telegram updates
# -------------------------
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook_update():
    try:
        update = request.get_data().decode("utf-8")
        bot.process_new_updates([telebot.types.Update.de_json(update)])
    except Exception:
        traceback.print_exc()
    return "ok", 200


@app.route("/", methods=["GET", "HEAD"])
def index():
    return "SaleTest bot alive.", 200


# -------------------------
# Webhook / polling setup
# -------------------------
def setup_webhook():
    """Устанавливает webhook, если выбран режим webhook"""
    bot.remove_webhook()
    wh_url = f"{WEB_DOMAIN.rstrip('/')}/{BOT_TOKEN}"
    bot.set_webhook(url=wh_url)
    print(f"Webhook set to {wh_url}")


def run_polling():
    """Запуск в режиме polling (локально)"""
    print("Running in polling mode...")
    bot.remove_webhook()
    bot.infinity_polling(timeout=60, long_polling_timeout=20)


# -------------------------
# Flask launcher
# -------------------------
if __name__ == "__main__":
    try:
        if USE_WEBHOOK:
            setup_webhook()
            print("Starting Flask webhook server on port 10000...")
            app.run(host="0.0.0.0", port=10000)
        else:
            run_polling()
    except KeyboardInterrupt:
        print("Bot stopped manually.")
    except Exception as e:
        traceback.print_exc()
        print("Fatal error:", e)

# -------------------------
# END OF FULL PROJECT
# -------------------------

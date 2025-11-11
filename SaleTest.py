#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SaleTest.py - полный рабочий Telegram-магазин с CryptoBot интеграцией и операторами.
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
from typing import Optional, Dict, Any, List
from threading import Thread

from flask import Flask, request, jsonify
import telebot
from telebot import types

# ------------------------- 
# КОНФИГ (можно переопределять через ENV)
# -------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN") or "8587164094:AAEcsW0oUMg1Hphbymdg3NHtH_Q25j7RyWo"
CRYPTOPAY_API_TOKEN = os.environ.get("CRYPTOPAY_API_TOKEN") or "484313:AAwPvRj3LLlT0vDY4LSFh0Vt2gIUCqlQfWw"
WEB_DOMAIN = os.environ.get("WEB_DOMAIN") or "https://render-jj8d.onrender.com"
USE_WEBHOOK = os.environ.get("USE_WEBHOOK", "0") == "1"
ADMIN_IDS = set([int(os.environ.get("ADMIN_ID") or 1942740947)])
INITIAL_OPERATORS = [7771789412]  # initial operator list

CRYPTO_API_BASE = "https://pay.crypt.bot/api"

DB_FILE = os.environ.get("DB_FILE") or "salebot.sqlite"
IPN_LOG_FILE = "ipn_log.jsonl"

# Only allowed assets per your request
AVAILABLE_ASSETS = ["USDT", "TON", "TRX"]

# Offers expressed in USD base (you wanted final conversion to USD)
OFFERS_USD = {
    "sub": {"100": 1.0, "500": 4.0, "1000": 7.0},
    "view": {"1000": 0.5, "5000": 2.0, "10000": 3.5},
    "com": {"50": 1.5, "200": 5.0}
}
PRETTY = {"sub": "Подписчики", "view": "Просмотры", "com": "Комментарии"}

# -------------------------
# Инициализация
# -------------------------
# simple token sanity check
if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise ValueError("BOT_TOKEN must be set and contain a colon (:).")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
app = Flask(__name__)

# -------------------------
# БД: инициализация и хелперы
# -------------------------
def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        created_at TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        category TEXT,
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
    cur.execute("""
    CREATE TABLE IF NOT EXISTS invoices_map (
        invoice_id TEXT PRIMARY KEY,
        chat_id INTEGER,
        order_id INTEGER,
        raw_payload TEXT,
        created_at TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS operators (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER UNIQUE,
        username TEXT,
        display_name TEXT,
        created_at TEXT
    )""")
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
# DB helper functions
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
    rows = [dict(r) for r in cur.fetchall()]
    conn.close(); return rows

def create_order_record(chat_id:int, category:str, amount:int, price_usd:float, currency:str="USD", link:Optional[str]=None)->int:
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("""INSERT INTO orders (chat_id, category, amount, price_usd, currency, status, invoice_id, pay_url, link, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (chat_id, category, amount, price_usd, currency, "awaiting_payment", None, None, link, now, now))
    oid = cur.lastrowid
    conn.commit(); conn.close(); return oid

def update_order_invoice(order_id:int, invoice_id:Optional[str], pay_url:Optional[str]):
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE orders SET invoice_id = ?, pay_url = ?, updated_at = ? WHERE id = ?", (invoice_id, pay_url, now, order_id))
    conn.commit(); conn.close()

def set_invoice_mapping(invoice_id:str, chat_id:int, order_id:int, raw_payload:Optional[Any]=None):
    now = datetime.utcnow().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO invoices_map (invoice_id, chat_id, order_id, raw_payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (invoice_id, chat_id, order_id, json.dumps(raw_payload, ensure_ascii=False) if raw_payload else None, now))
    conn.commit(); conn.close()

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
    rows = [dict(r) for r in cur.fetchall()]; conn.close()
    return rows

def get_request_by_id(req_id:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM support_requests WHERE id = ?", (req_id,))
    r = cur.fetchone(); conn.close()
    return dict(r) if r else None

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
    """
    Calls CryptoBot createInvoice API. Returns parsed JSON or dict(error=True,...)
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
        # fallback: return USD amount
        return round(price_usd, 6)
    except Exception:
        return round(price_usd, 6)

# -------------------------
# UI helpers
# -------------------------
def main_menu_markup():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("📈 Купить подписчиков", callback_data="menu_sub"))
    kb.add(types.InlineKeyboardButton("👁 Купить просмотры", callback_data="menu_view"))
    kb.add(types.InlineKeyboardButton("💬 Купить комментарии", callback_data="menu_com"))
    kb.add(types.InlineKeyboardButton("В личных сообщениях", callback_data="support_personal"))
    kb.add(types.InlineKeyboardButton("Написать в поддержку (в боте)", callback_data="support_bot"))
    kb.add(types.InlineKeyboardButton("👤 Профиль", callback_data="profile"))
    return kb

def packages_markup(cat_key:str):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for amt, price in OFFERS_USD.get(cat_key, {}).items():
        kb.add(types.InlineKeyboardButton(f"{amt} — ${price}", callback_data=f"order_{cat_key}_{amt}"))
    kb.add(types.InlineKeyboardButton("🔢 Своя сумма", callback_data=f"custom_{cat_key}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
    return kb

def currency_selection_markup(chat_id:int, order_id:int):
    kb = types.InlineKeyboardMarkup(row_width=3)
    for asset in AVAILABLE_ASSETS:
        # callback format: pay_asset_<chatid>_<orderid>_<asset>
        kb.add(types.InlineKeyboardButton(asset, callback_data=f"pay_asset_{chat_id}_{order_id}_{asset}"))
    kb.add(types.InlineKeyboardButton("🔙 Отмена", callback_data="cancel_payment"))
    return kb

# -------------------------
# In-memory short states
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
    bot.send_message(m.chat.id, "🧸 Добро пожаловать! Выберите действие:", reply_markup=main_menu_markup())

@bot.callback_query_handler(func=lambda call: True)
def cb_all(call):
    try:
        data = call.data
        cid = call.message.chat.id

        # Main menu navigation
        if data in ("menu_sub", "menu_view", "menu_com"):
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

        # Support personal
        if data == "support_personal":
            bot.edit_message_text("📨 Чтобы связаться в личных сообщениях — найдите оператора в Telegram.\n\nЕсли хотите написать через бота — нажмите «Написать в поддержку (в боте)».", cid, call.message.message_id)
            return

        # Support via bot
        if data == "support_bot":
            user_state[cid] = {"awaiting_support_msg": True}
            bot.send_message(cid, "✉️ Напишите ваше сообщение для поддержки. Оно будет отправлено операторам.")
            return

        # Fixed package ordering
        if data.startswith("order_"):
            try:
                _, category, amt = data.split("_", 2)
                amount = int(amt)
            except Exception:
                bot.answer_callback_query(call.id, "Неверный формат заказа")
                return
            user_state[cid] = {"awaiting_link_for_order": True, "order_category": category, "order_amount": amount}
            bot.send_message(cid, f"Вы выбрали {PRETTY.get(category)} — {amount}. Отправьте ссылку на канал/пост (http/https):")
            bot.answer_callback_query(call.id, "Введите ссылку для заказа")
            return

        # Custom amount
        if data.startswith("custom_"):
            category = data.replace("custom_", "")
            max_offer = 0
            if OFFERS_USD.get(category):
                max_offer = max(int(x) for x in OFFERS_USD[category].keys())
            min_allowed = max_offer + 1 if max_offer else 1
            user_state[cid] = {"awaiting_custom_amount": True, "category": category, "min_allowed": min_allowed}
            bot.send_message(cid, f"Введите количество для {PRETTY.get(category)} (минимум {min_allowed}):")
            bot.answer_callback_query(call.id)
            return

        # Cancel payment
        if data == "cancel_payment":
            bot.send_message(cid, "Оплата отменена.", reply_markup=main_menu_markup())
            bot.answer_callback_query(call.id)
            return

        # Profile
        if data == "profile":
            conn = get_db(); cur = conn.cursor()
            cur.execute("SELECT * FROM orders WHERE chat_id = ? ORDER BY id DESC", (cid,))
            rows = cur.fetchall(); conn.close()
            if not rows:
                bot.send_message(cid, "У вас нет заказов.", reply_markup=main_menu_markup())
                return
            txt = "📋 Ваши заказы:\n\n"
            kb = types.InlineKeyboardMarkup(row_width=1)
            for r in rows:
                txt += f"#{r['id']} | {PRETTY.get(r['category'], r['category'])} {r['amount']} — ${r['price_usd']:.2f} — {r['status']}\n"
                if r['status'] not in ("оплачен", "closed"):
                    kb.add(types.InlineKeyboardButton(f"Отменить #{r['id']}", callback_data=f"cancel_{r['id']}"))
            bot.send_message(cid, txt, reply_markup=kb)
            return

        # Cancel order
        if data.startswith("cancel_"):
            try:
                _, oid = data.split("_",1)
                oid = int(oid)
            except:
                bot.answer_callback_query(call.id, "Ошибка отмены")
                return
            conn = get_db(); cur = conn.cursor()
            cur.execute("SELECT * FROM orders WHERE id = ?", (oid,))
            r = cur.fetchone()
            if not r:
                bot.answer_callback_query(call.id, "Заказ не найден"); conn.close(); return
            if r["status"] == "оплачен":
                bot.answer_callback_query(call.id, "Нельзя отменить оплаченный заказ"); conn.close(); return
            cur.execute("UPDATE orders SET status = ? WHERE id = ?", ("Отменён", oid))
            conn.commit(); conn.close()
            bot.answer_callback_query(call.id, "Заказ отменён")
            bot.send_message(call.message.chat.id, f"Заказ #{oid} отменён.")
            return

        # open requests pagination (for operators)
        if data.startswith("open_requests_page_"):
            try:
                page = int(data.split("_")[-1])
            except:
                page = 1
            show_requests_page(call.from_user.id, page, call.message)
            bot.answer_callback_query(call.id)
            return

        # open specific request
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

        # reply to request (operator)
        if data.startswith("reply_req_"):
            try:
                # format: reply_req_<reqid>
                parts = data.split("_")
                req_id = int(parts[-1])
            except:
                bot.answer_callback_query(call.id, "Bad conv id"); return
            # only operators allowed
            if call.from_user.id not in [o["chat_id"] for o in list_operators()]:
                bot.answer_callback_query(call.id, "У вас нет прав оператора"); return
            operator_state[call.from_user.id] = {"awaiting_reply_for": req_id, "message_id": call.message.message_id}
            bot.send_message(call.from_user.id, "Введите ответ для пользователя (сообщение будет отправлено и обращение закроется):")
            bot.answer_callback_query(call.id)
            return

        # --- currency pay button: pay_asset_chatid_orderid_asset
        if data.startswith("pay_asset_"):
            # confirm callback quickly
            bot.answer_callback_query(call.id)

            # Убираем префикс
            payload = data.replace("pay_asset_", "", 1)

            # Разбираем безопасно (независимо от количества _)
            parts = payload.split("_")
            if len(parts) < 3:
                bot.send_message(cid, "Ошибка: не хватает данных для оплаты")
                return

            chat_str = parts[0]
            orderid_str = parts[1]
            asset = parts[2]

            # проверяем числа
            if not chat_str.isdigit() or not orderid_str.isdigit():
                bot.send_message(cid, "Ошибка: неверные данные заказа")
                return

            order_chat = int(chat_str)
            order_id = int(orderid_str)

            # Загружаем заказ из БД
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
            row = cur.fetchone()
            conn.close()

            if not row:
                bot.send_message(cid, "Заказ не найден")
                return

            price_usd = float(row["price_usd"])
            pay_amount = convert_price_usd_to_asset(price_usd, asset.upper())

            order_uid = f"{order_chat}_{order_id}"
            description = f"Заказ #{order_id} {row['category']}"
            callback_url = WEB_DOMAIN.rstrip("/") + "/cryptobot/ipn"

            resp = create_cryptobot_invoice(pay_amount, asset.upper(), order_uid, description, callback_url=callback_url)

            invoice_id = (resp.get("invoiceId")
                          or resp.get("invoice_id")
                          or resp.get("id"))

            pay_url = (
                resp.get("pay_url")
                or resp.get("payment_url")
                or (resp.get("result", {}).get("pay_url") if isinstance(resp, dict) else None)
            )

            if invoice_id:
                set_invoice_mapping(str(invoice_id), order_chat, order_id, raw_payload=resp)
                update_order_invoice(order_id, str(invoice_id), pay_url)

            if pay_url:
                try:
                    qr_bytes = generate_qr_bytes(pay_url)
                    bot.send_photo(order_chat, qr_bytes, caption=f"💳 Оплата заказа #{order_id} через {asset.upper()}\nСсылка: {pay_url}")
                except Exception:
                    bot.send_message(order_chat, f"💳 Оплата: {pay_url}")
            else:
                bot.send_message(order_chat, "Ошибка: нет ссылки на оплату")
            return

        # Unknown or unhandled callback
        bot.answer_callback_query(call.id, "Неизвестная команда.")
    except Exception as e:
        try:
            traceback.print_exc()
            bot.answer_callback_query(call.id, "Ошибка обработки команды")
        except:
            pass

# -------------------------
# Support notifications & request listing
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
    # navigation
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
# Text messages: orders, support, admin operations, operator replies
# -------------------------
@bot.message_handler(content_types=['text'])
def handle_text(m):
    try:
        cid = m.chat.id
        text = m.text.strip()
        ensure_user(cid, m)

        # Admin-only commands
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
                bot.reply_to(m, "Bad id"); return
            add_operator(new_id, username=None, display_name=None)
            bot.reply_to(m, f"Добавлен оператор {new_id}"); return

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
            cur.execute("SELECT id, chat_id, category, amount, price_usd, currency, status FROM orders ORDER BY id DESC LIMIT 50")
            rows = cur.fetchall(); conn.close()
            if not rows:
                bot.reply_to(m, "Заказов нет."); return
            txt = "Последние заказы:\n\n"
            for r in rows:
                txt += f"#{r['id']} uid:{r['chat_id']} {PRETTY.get(r['category'],r['category'])} {r['amount']} — ${r['price_usd']:.2f} — {r['status']}\n"
            bot.reply_to(m, txt); return

        # User flow states
        state = user_state.get(cid)

        # awaiting custom amount
        if state and state.get("awaiting_custom_amount"):
            if not text.isdigit():
                bot.reply_to(m, "Введите целое число."); return
            amount = int(text)
            if amount < state.get("min_allowed",1):
                bot.reply_to(m, f"Минимум {state.get('min_allowed')}"); return
            base_unit_price = 0.01
            price_usd = round(amount * base_unit_price, 2)
            order_id = create_order_record(cid, state["category"], amount, price_usd, "USD")
            user_state.pop(cid, None)
            bot.reply_to(m, f"Заказ #{order_id} создан на ${price_usd:.2f}. Выберите валюту для оплаты.", reply_markup=currency_selection_markup(cid, order_id))
            return

        # awaiting link for fixed package
        if state and state.get("awaiting_link_for_order"):
            link = text
            if not (link.startswith("http://") or link.startswith("https://")):
                bot.reply_to(m, "Неверная ссылка. Должна начинаться с http/https"); return
            category = state["order_category"]; amount = state["order_amount"]
            price_usd = float(OFFERS_USD.get(category, {}).get(str(amount), amount * 0.01))
            order_id = create_order_record(cid, category, amount, price_usd, "USD", link=link)
            user_state.pop(cid, None)
            bot.reply_to(m, f"Заказ #{order_id} создан на ${price_usd:.2f}. Выберите валюту для оплаты.", reply_markup=currency_selection_markup(cid, order_id))
            return

        # awaiting support message
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

        # fallback
        bot.send_message(cid, "Не понял. Нажми /start для меню.")
    except Exception:
        traceback.print_exc()
        try:
            bot.reply_to(m, "Внутренняя ошибка.")
        except:
            pass

# -------------------------
# Flask endpoints: IPN & webhook
# -------------------------
@app.route("/cryptobot/ipn", methods=["POST"])
def cryptobot_ipn():
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "bad json"}), 400

    # log payload
    try:
        with open(IPN_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": datetime.utcnow().isoformat(), "payload": payload}, ensure_ascii=False) + "\n")
    except Exception:
        pass

    invoice_id = None
    for k in ("invoiceId","invoice_id","id"):
        if k in payload:
            invoice_id = str(payload[k]); break

    # check payment status
    status_field = payload.get("status") or payload.get("paymentStatus") or payload.get("state")
    paid_indicators = {"paid","success","confirmed","finished","complete"}
    st = str(status_field).lower() if status_field else ""
    if any(p in st for p in paid_indicators):
        if invoice_id:
            conn = get_db(); cur = conn.cursor()
            cur.execute("SELECT * FROM invoices_map WHERE invoice_id = ?", (invoice_id,))
            r = cur.fetchone()
            if r:
                try:
                    oid = int(r["order_id"])
                    cur.execute("UPDATE orders SET status = ? WHERE id = ?", ("оплачен", oid))
                    conn.commit()
                    # notify user
                    try:
                        cur2 = conn.cursor()
                        cur2.execute("SELECT chat_id FROM orders WHERE id = ?", (oid,))
                        rr = cur2.fetchone()
                        if rr:
                            chatid = rr["chat_id"]
                            try:
                                bot.send_message(chatid, f"✅ Оплата получена за заказ #{oid}. В ближайшее время мы начнём работу.")
                            except Exception:
                                pass
                    except Exception:
                        pass
                except Exception:
                    pass
            conn.close()
    return jsonify({"ok": True}), 200

# Telegram webhook endpoint (optional)
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
# Webhook setup helper
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
# Run (polling or webhook)
# -------------------------
if __name__ == "__main__":
    print("Starting SaleTest service")
    init_db()
    if USE_WEBHOOK:
        set_telegram_webhook()
        port = int(os.environ.get("PORT", 5000))
        print("Running Flask (webhook mode) on port", port)
        app.run(host="0.0.0.0", port=port)
    else:
        # run flask in separate thread and polling for bot updates
        t = Thread(target=lambda: app.run(host="0.0.0.0", port=5000), daemon=True)
        t.start()
        print("Running polling (local)...")
        bot.infinity_polling(timeout=60, long_polling_timeout=20)

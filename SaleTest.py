# bot_with_cryptobot_ton.py
"""
Полный Telegram-бот с приёмом оплат через CryptoBot (чек-API).
- ПРИНИМАЕМ в конечной валюте TON (asset = "TON")
- Создаём чек через CryptoBot API, отправляем pay_url + QR пользователю
- Принимаем webhook (IPN) от CryptoBot и отмечаем заказ как 'оплачен'
- Профиль пользователя (просмотр заказов, отмена)
- Админ-панель (просмотр всех заказов, изменение статуса)
- Хранение данных в data.json
Requirements:
pip install pyTelegramBotAPI Flask requests qrcode[pil]
Запуск: python bot_with_cryptobot_ton.py
Перед запуском: укажи PUBLIC_WEBHOOK_URL (https://.../cryptobot/ipn) — можно через ngrok
"""

import os
import json
import threading
import requests
import qrcode
from io import BytesIO
from flask import Flask, request, jsonify, abort

import telebot
from telebot import types

# ----------------------- КОНФИГУРАЦИЯ -----------------------
# Твой Telegram бот токен (оставил тот, что был ранее)
BOT_TOKEN = "8587164094:AAEcsW0oUMg1Hphbymdg3NHtH_Q25j7RyWo"

# --- НОВЫЙ CryptoBot API Token (вставлен) ---
CRYPTOPAY_API_TOKEN = "484313:AA6FJU50A2cMhJas5ruR6PD15Jl5F1XMrN7"

# Публичный URL (где доступен Flask app). Пример: https://abcd1234.ngrok.io
# Укажи свой публичный URL, который проксирует Flask.
PUBLIC_WEBHOOK_URL = os.environ.get("PUBLIC_WEBHOOK_URL") or "https://<YOUR_NGROK_OR_DOMAIN>/cryptobot/ipn"

# Админ id (куда приходят уведомления и кто администрирует)
ADMIN_ID = 1942740947  # замени, если нужно

# Путь до файла с данными
DATA_FILE = "data.json"

# Мы принимаем и конвертируем ВСЕ платежи в TON на стороне CryptoBot (asset = "TON")
TARGET_ASSET = "TON"

# Офферы (пакеты) — формат: ключ категории -> {amount_str: price_rub}
OFFERS = {
    "sub": {"100": 100, "500": 400, "1000": 700},
    "view": {"1000": 50, "5000": 200, "10000": 350},
    "com": {"50": 150, "200": 500},
}
PRETTY = {"sub": "Подписчики", "view": "Просмотры", "com": "Комментарии"}

# CryptoBot API endpoints
CRYPTO_API_BASE = "https://pay.crypt.bot/api"

# ----------------------- ИНИЦИАЛИЗАЦИЯ -----------------------
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# загружаем/создаём data.json
if os.path.exists(DATA_FILE):
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
else:
    data = {"users": {}, "invoices": {}, "user_state": {}}
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ----------------------- УТИЛИТЫ -----------------------
def register_user(chat_id):
    cid = str(chat_id)
    if cid not in data["users"]:
        data["users"][cid] = {"orders": []}
        save_data()

def add_order(chat_id, category_key, amount):
    """
    Добавляем заказ и возвращаем order_id (в рамках пользователя)
    """
    register_user(chat_id)
    cid = str(chat_id)
    order = {
        "id": len(data["users"][cid]["orders"]) + 1,
        "category": category_key,
        "category_name": PRETTY.get(category_key, category_key),
        "amount": amount,
        "status": "ожидает оплаты",
        "invoice_id": None,
        "pay_url": None,
        "created_at": None
    }
    data["users"][cid]["orders"].append(order)
    save_data()
    return order["id"]

def set_invoice_for_order(chat_id, order_id, invoice_id, pay_url):
    cid = str(chat_id)
    for o in data["users"][cid]["orders"]:
        if o["id"] == order_id:
            o["invoice_id"] = invoice_id
            o["pay_url"] = pay_url
            save_data()
            return True
    return False

def update_order_status(chat_id, order_id, new_status):
    cid = str(chat_id)
    for o in data["users"][cid]["orders"]:
        if o["id"] == order_id:
            o["status"] = new_status
            save_data()
            return True
    return False

# ----------------------- CryptoBot API helpers -----------------------
def create_cryptobot_invoice(amount_value, asset_target, order_uid, description, callback_url=None):
    """
    Создаёт чек в CryptoBot (через /createInvoice). Возвращает dict ответа.
    Заголовок: Crypto-Pay-API-Token
    Тело: { amount: "1.23", asset: "TON", callback: "...", payload: "...", description: "..." }
    """
    url = CRYPTO_API_BASE + "/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTOPAY_API_TOKEN, "Content-Type": "application/json"}
    payload = {
        "amount": str(amount_value),
        "asset": asset_target,
        "payload": str(order_uid),
        "description": description
    }
    if callback_url:
        payload["callback"] = callback_url
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=20)
        if r.status_code not in (200, 201):
            return {"error": True, "status_code": r.status_code, "body": r.text}
        return r.json()
    except Exception as e:
        return {"error": True, "exception": str(e)}

def get_invoice_status(invoice_id):
    """
    Запрос статуса инвойса (fallback проверка).
    Endpoint: /getInvoice?invoiceId=...
    """
    url = CRYPTO_API_BASE + "/getInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTOPAY_API_TOKEN}
    params = {"invoiceId": invoice_id}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        if r.status_code not in (200, 201):
            return {"error": True, "status_code": r.status_code, "body": r.text}
        return r.json()
    except Exception as e:
        return {"error": True, "exception": str(e)}

# ----------------------- Вебхук от CryptoBot (IPN) -----------------------
@app.route("/cryptobot/ipn", methods=["POST"])
def cryptobot_ipn():
    """
    CryptoBot will POST JSON describing invoice status.
    Пример тела (вариативно): { "invoiceId": "...", "status": "PAID", "payload": "...", ... }
    """
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "bad json"}), 400

    # логируем полезные поля
    invoice_id = None
    for key in ("invoiceId", "invoice_id", "id", "paymentId", "invoiceID", "payment_id"):
        if key in payload:
            invoice_id = payload[key]
            break

    status_field = None
    for key in ("status", "paymentStatus", "payment_status", "state"):
        if key in payload:
            status_field = payload[key]
            break

    custom_payload = payload.get("payload") or payload.get("order") or payload.get("comment") or payload.get("merchant_order_id")

    if not invoice_id and custom_payload:
        for inv, rec in data.get("invoices", {}).items():
            if str(rec.get("order_uid")) == str(custom_payload):
                invoice_id = inv
                break

    if invoice_id:
        data.setdefault("invoices", {})[str(invoice_id)] = {"payload": payload}
        save_data()

    st = None
    if status_field:
        st = str(status_field).lower()

    paid_indicators = {"paid", "success", "finished", "confirmed", "complete"}
    if st and any(p in st for p in paid_indicators):
        rec = data.get("invoices", {}).get(str(invoice_id))
        if rec and rec.get("chat_id") and rec.get("order_id"):
            chat_id = rec["chat_id"]
            order_id = rec["order_id"]
            update_order_status(chat_id, order_id, "оплачен")
            try:
                bot.send_message(chat_id, f"🔔 Платёж подтверждён. Заказ #{order_id} помечен как оплачен.")
            except Exception:
                pass
            return jsonify({"ok": True}), 200
        else:
            order_uid = custom_payload
            if order_uid and isinstance(order_uid, str) and "_" in order_uid:
                try:
                    parts = order_uid.split("_")
                    chat = int(parts[0])
                    oid = int(parts[1])
                    update_order_status(chat, oid, "оплачен")
                    try:
                        bot.send_message(chat, f"🔔 Платёж подтверждён. Заказ #{oid} помечен как оплачен.")
                    except Exception:
                        pass
                    return jsonify({"ok": True}), 200
                except Exception:
                    pass
    return jsonify({"ok": True}), 200

# ----------------------- Telegram bot: UI и логика -----------------------

def main_menu_inline():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("📈 Купить подписчиков", callback_data="menu_sub"))
    kb.add(types.InlineKeyboardButton("👁 Купить просмотры", callback_data="menu_view"))
    kb.add(types.InlineKeyboardButton("💬 Купить комментарии", callback_data="menu_com"))
    kb.add(types.InlineKeyboardButton("👤 Профиль", callback_data="profile"))
    kb.add(types.InlineKeyboardButton("🔐 Админ", callback_data="admin_panel"))
    return kb

def packages_markup(cat_key):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for amt, price in OFFERS.get(cat_key, {}).items():
        kb.add(types.InlineKeyboardButton(f"{amt} — {price}₽", callback_data=f"order_{cat_key}_{amt}"))
    kb.add(types.InlineKeyboardButton("✏ Своя сумма", callback_data=f"custom_{cat_key}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
    return kb

@bot.message_handler(commands=['start'])
def cmd_start(m):
    register_user(m.chat.id)
    bot.send_message(m.chat.id, "🧸 Добро пожаловать! Выбери услугу:", reply_markup=main_menu_inline())

@bot.callback_query_handler(func=lambda c: True)
def callback_handler(call):
    cid = call.message.chat.id
    data_call = call.data

    # Меню навигации
    if data_call == "menu_sub":
        bot.edit_message_text("📈 Выбери пакет подписчиков:", cid, call.message.message_id, reply_markup=packages_markup("sub"))
        return
    if data_call == "menu_view":
        bot.edit_message_text("👁 Выбери пакет просмотров:", cid, call.message.message_id, reply_markup=packages_markup("view"))
        return
    if data_call == "menu_com":
        bot.edit_message_text("💬 Выбери пакет комментариев:", cid, call.message.message_id, reply_markup=packages_markup("com"))
        return
    if data_call == "back":
        bot.edit_message_text("🧸 Выбери услугу:", cid, call.message.message_id, reply_markup=main_menu_inline())
        return

    # профиль
    if data_call == "profile":
        show_profile(cid)
        return

    # админ панель (видна всем, но доступна только ADMIN_ID)
    if data_call == "admin_panel":
        if cid != ADMIN_ID:
            bot.answer_callback_query(call.id, "Нет прав")
            return
        show_admin_panel(cid)
        return

    # фиксированный пакет: создаём заказ и создаём чек
    if data_call.startswith("order_"):
        _, category, amt_str = data_call.split("_", 2)
        amount = int(amt_str)
        # создаём запись заказа
        order_id = add_order(cid, category, amount)
        # Формула цены: возьмём price_rub из OFFERS и конвертируем в USD для выставления в чеке.
        price_rub = OFFERS.get(category, {}).get(amt_str, None)
        if price_rub is None:
            price_rub = amount  # fallback
        # Для demo: 100 RUB = 1 USD
        price_usd = round(price_rub / 100.0, 2)
        order_uid = f"{cid}_{order_id}"
        callback_url = PUBLIC_WEBHOOK_URL
        description = f"Заказ #{order_id} {PRETTY.get(category)} {amount}"
        resp = create_cryptobot_invoice(price_usd, TARGET_ASSET, order_uid, description, callback_url=callback_url)
        if isinstance(resp, dict) and resp.get("error"):
            bot.send_message(cid, "Ошибка при создании чека. Сообщи админу.")
            bot.send_message(ADMIN_ID, f"CryptoBot create error: {resp}")
            return
        invoice_id = resp.get("invoiceId") or resp.get("invoice_id") or resp.get("id") or resp.get("paymentId")
        pay_url = resp.get("pay_url") or resp.get("payment_url") or resp.get("url") or resp.get("invoice_url") or resp.get("paymentLink")
        if invoice_id:
            data.setdefault("invoices", {})[str(invoice_id)] = {"chat_id": cid, "order_id": order_id, "payload": order_uid}
            save_data()
        set_invoice_for_order(cid, order_id, invoice_id, pay_url)
        if pay_url:
            bot.send_message(cid, f"Перейдите по ссылке для оплаты (поддерживается много валют):\n{pay_url}\nПосле оплаты чек автоматически отметится как оплачен.")
            try:
                img_buf = BytesIO()
                qrcode.make(pay_url).save(img_buf, format="PNG")
                img_buf.seek(0)
                bot.send_photo(cid, img_buf)
            except Exception:
                pass
        else:
            bot.send_message(cid, "Ссылка на оплату не получена. Обратись к администратору.")
        bot.answer_callback_query(call.id, "Счёт создан и отправлен.")
        return

    # custom создание: пользователь вводит своё количество
    if data_call.startswith("custom_"):
        category = data_call.replace("custom_", "")
        offers = OFFERS.get(category, {})
        if not offers:
            bot.answer_callback_query(call.id, "Нет предложений")
            return
        max_offer = max(int(x) for x in offers.keys())
        min_allowed = max_offer + 1
        data.setdefault("user_state", {})[str(cid)] = {"waiting_custom": True, "category": category, "min_allowed": min_allowed}
        save_data()
        bot.send_message(cid, f"✏ Введите количество для {PRETTY.get(category)} (целое, минимум {min_allowed}):")
        bot.answer_callback_query(call.id)
        return

    bot.answer_callback_query(call.id, "Неизвестная команда.")

# ----------------------- обработка текстовых сообщений (ввод custom amounts, profile команды) -----------------------
@bot.message_handler(func=lambda m: True)
def on_text(m):
    cid = m.chat.id
    text = (m.text or "").strip()

    # если юзер в режиме ввода custom
    ustate = data.get("user_state", {}).get(str(cid))
    if ustate and ustate.get("waiting_custom"):
        if not text.isdigit():
            bot.send_message(cid, "❗ Нужно ввести только целое число. Попробуй ещё раз.")
            return
        amount = int(text)
        min_allowed = ustate.get("min_allowed", 1)
        if amount < min_allowed:
            bot.send_message(cid, f"❗ Минимум для этой категории — {min_allowed}. Введи число >= {min_allowed}.")
            return
        category = ustate["category"]
        order_id = add_order(cid, category, amount)
        price_usd = round(amount * 0.01, 2)
        order_uid = f"{cid}_{order_id}"
        resp = create_cryptobot_invoice(price_usd, TARGET_ASSET, order_uid, f"Заказ #{order_id} {PRETTY.get(category)}", callback_url=PUBLIC_WEBHOOK_URL)
        if isinstance(resp, dict) and resp.get("error"):
            bot.send_message(cid, "Ошибка при создании чека. Сообщи админу.")
            bot.send_message(ADMIN_ID, f"CryptoBot create error: {resp}")
            data["user_state"].pop(str(cid), None)
            save_data()
            return
        invoice_id = resp.get("invoiceId") or resp.get("invoice_id") or resp.get("id") or resp.get("paymentId")
        pay_url = resp.get("pay_url") or resp.get("payment_url") or resp.get("url") or resp.get("invoice_url")
        if invoice_id:
            data.setdefault("invoices", {})[str(invoice_id)] = {"chat_id": cid, "order_id": order_id, "payload": order_uid}
            save_data()
        set_invoice_for_order(cid, order_id, invoice_id, pay_url)
        data["user_state"].pop(str(cid), None)
        save_data()
        if pay_url:
            bot.send_message(cid, f"Перейдите по ссылке для оплаты: {pay_url}")
            try:
                buf = BytesIO()
                qrcode.make(pay_url).save(buf, format="PNG")
                buf.seek(0)
                bot.send_photo(cid, buf)
            except Exception:
                pass
            bot.send_message(cid, "После оплаты бот автоматически отметит заказ как оплачен.")
        else:
            bot.send_message(cid, "Ссылка на оплату не получена. Сообщи админу.")
        return

    # профиль
    if text.lower() in ("/profile", "профиль", "👤 профиль"):
        show_profile(cid)
        return

    # admin show
    if text.lower() in ("/admin",) and cid == ADMIN_ID:
        show_admin_panel(cid)
        return

    # fallback
    bot.send_message(cid, "Не понял. Нажми /start чтобы вернуться в меню.", reply_markup=None)

# ----------------------- Профиль и админ панель -----------------------
def show_profile(chat_id):
    register_user(chat_id)
    cid = str(chat_id)
    orders = data["users"][cid]["orders"]
    if not orders:
        bot.send_message(chat_id, "📭 У вас пока нет заказов.", reply_markup=main_menu_inline())
        return
    text = "📋 Ваши заказы:\n\n"
    kb = types.InlineKeyboardMarkup(row_width=1)
    for o in orders:
        text += f"#{o['id']} | {o['category_name']} — {o['amount']} шт. | {o['status']}\n"
        if o["status"] not in ("Отменён", "оплачен"):
            kb.add(types.InlineKeyboardButton(f"Отменить #{o['id']}", callback_data=f"cancel_{o['id']}"))
    bot.send_message(chat_id, text, reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel_"))
def cancel_order_callback(call):
    cid = str(call.message.chat.id)
    idx = int(call.data.split("_")[1])
    orders = data["users"].get(cid, {}).get("orders", [])
    for o in orders:
        if o["id"] == idx:
            if o["status"] == "оплачен":
                bot.answer_callback_query(call.id, "Нельзя отменить уже оплаченный заказ.")
                return
            o["status"] = "Отменён"
            save_data()
            bot.answer_callback_query(call.id, "Заказ отменён")
            bot.edit_message_text("Заказ отменён ✅", call.message.chat.id, call.message.message_id)
            bot.send_message(ADMIN_ID, f"Пользователь {cid} отменил заказ #{idx}")
            return
    bot.answer_callback_query(call.id, "Заказ не найден")

def show_admin_panel(chat_id):
    if chat_id != ADMIN_ID:
        bot.send_message(chat_id, "Недостаточно прав.")
        return
    text = "📋 Все заказы:\n\n"
    kb = types.InlineKeyboardMarkup(row_width=1)
    any_orders = False
    for uid, udata in data["users"].items():
        for o in udata["orders"]:
            any_orders = True
            text += f"User {uid} | #{o['id']} | {o['category_name']} {o['amount']} | {o['status']}\n"
            kb.add(types.InlineKeyboardButton(f"Управлять (UID {uid} #{o['id']})", callback_data=f"admin_manage_{uid}_{o['id']}"))
    if not any_orders:
        bot.send_message(chat_id, "Заказов нет.")
    else:
        bot.send_message(chat_id, text, reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_manage_"))
def admin_manage(call):
    if call.message.chat.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Нет прав")
        return
    _, _, user_id, order_id = call.data.split("_")
    user_id = str(user_id); order_id = int(order_id)
    o = None
    try:
        o = next(x for x in data["users"][user_id]["orders"] if x["id"] == order_id)
    except Exception:
        bot.answer_callback_query(call.id, "Заказ не найден")
        return
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"admin_set_{user_id}_{order_id}_Подтверждено"))
    kb.add(types.InlineKeyboardButton("🕒 В процессе", callback_data=f"admin_set_{user_id}_{order_id}_В процессе"))
    kb.add(types.InlineKeyboardButton("❌ Отменить", callback_data=f"admin_set_{user_id}_{order_id}_Отменён"))
    bot.send_message(call.message.chat.id, f"Управление заказом {user_id} #{order_id}\nТекущий статус: {o['status']}", reply_markup=kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_set_"))
def admin_set(call):
    if call.message.chat.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Нет прав")
        return
    parts = call.data.split("_", 4)
    if len(parts) != 5:
        bot.answer_callback_query(call.id, "Bad data")
        return
    _, _, user_id, order_id_str, new_status = parts
    user_id = str(user_id); order_idx = int(order_id_str)
    try:
        for o in data["users"][user_id]["orders"]:
            if o["id"] == order_idx:
                o["status"] = new_status
                save_data()
                bot.send_message(ADMIN_ID, f"Статус заказа {user_id}#{order_idx} изменён на {new_status}")
                try:
                    bot.send_message(int(user_id), f"🔔 Статус вашего заказа #{order_idx} обновлён: {new_status}")
                except Exception:
                    pass
                bot.answer_callback_query(call.id, "Статус изменён")
                return
    except Exception as e:
        bot.answer_callback_query(call.id, f"Ошибка: {e}")

# ----------------------- Запуск Flask + Bot -----------------------
def run_flask():
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

def run_bot():
    bot.infinity_polling(timeout=60, long_polling_timeout=20)

if __name__ == "__main__":
    print("Запускаю Flask (IPN) и Telegram бот...")
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    run_bot()

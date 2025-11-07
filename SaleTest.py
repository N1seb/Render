# full_salebot.py
"""
Полный рабочий Telegram-бот с интеграцией CryptoBot (приём оплаты в TON),
профилем пользователя, историей заказов, админ-панелью, хранением данных в data.json,
созданием чеков в CryptoBot + обработкой IPN (webhook) через Flask.

Запуск:
- На локальной машине: нужен публичный адрес (ngrok) для IPN, либо запускай в режиме polling.
- На Render/Railway: webhook mode (Flask обработает POST от CryptoBot и Telegram).
- Перед запуском: установить зависимости:
    pip install pyTelegramBotAPI Flask requests qrcode[pil]
"""

import os
import json
import threading
import requests
import qrcode
from io import BytesIO
from flask import Flask, request, jsonify
import telebot
from telebot import types
import time
from datetime import datetime

# ----------------------- КОНФИГУРАЦИЯ -----------------------
# 1) Токен Telegram-бота (BotFather) — обязательно проверь.
BOT_TOKEN = "PUT_YOUR_BOTFATHER_TOKEN_HERE"  # <-- ВСТАВЬ СЮДА токен от @BotFather

# 2) Токен CryptoBot (Crypto Pay API token)
CRYPTOPAY_API_TOKEN = "PUT_YOUR_CRYPTOPAY_TOKEN_HERE"  # <-- ВСТАВЬ СЮДА токен от @CryptoBot

# 3) Публичный URL для приёма IPN от CryptoBot (например https://your-app.onrender.com/cryptobot/ipn)
#    На Render/Railway это будет домен твоего сервиса + /cryptobot/ipn
PUBLIC_WEBHOOK_URL = os.environ.get("PUBLIC_WEBHOOK_URL") or "https://<YOUR-PROJECT>.onrender.com/cryptobot/ipn"

# Админ ID (твой Telegram ID) — уведомления о заказах
ADMIN_ID = 1942740947  # замени на свой ID если нужно

# Путь до файла с данными
DATA_FILE = "data.json"

# Валюта, в которую будут конвертироваться все платежи (вывод для тебя)
TARGET_ASSET = "TON"

# Предустановленные пакеты (показываем пользователю, кнопки)
OFFERS = {
    "sub": {"100": 100, "500": 400, "1000": 700},
    "view": {"1000": 50, "5000": 200, "10000": 350},
    "com": {"50": 150, "200": 500},
}
PRETTY = {"sub": "Подписчики", "view": "Просмотры", "com": "Комментарии"}

CRYPTO_API_BASE = "https://pay.crypt.bot/api"

# ----------------------- ИНИЦИАЛИЗАЦИЯ -----------------------
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# Загружаем данные или создаём структуру
if os.path.exists(DATA_FILE):
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {"users": {}, "invoices": {}, "user_state": {}}
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
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
        "created_at": datetime.utcnow().isoformat()
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

    # payload / custom field (мы передаем order_uid в поле 'payload')
    custom_payload = payload.get("payload") or payload.get("order") or payload.get("comment") or payload.get("merchant_order_id")

    # Если нет invoice_id — попытаемся найти по payload
    if not invoice_id and custom_payload:
        for inv, rec in data.get("invoices", {}).items():
            if str(rec.get("order_uid")) == str(custom_payload):
                invoice_id = inv
                break

    # Сохраним сам входящий payload для логов
    if invoice_id:
        data.setdefault("invoices", {})[str(invoice_id)] = {"payload": payload}
        save_data()

    # Нормализация статуса
    st = None
    if status_field:
        st = str(status_field).lower()

    paid_indicators = {"paid", "success", "finished", "confirmed", "complete"}
    if st and any(p in st for p in paid_indicators):
        # отмечаем заказ как оплачен, если найдём mapping
        # Найдём mapping invoice_id -> chat_id, order_id
        rec = data.get("invoices", {}).get(str(invoice_id))
        if rec and rec.get("chat_id") and rec.get("order_id"):
            chat_id = rec["chat_id"]
            order_id = rec["order_id"]
            update_order_status(chat_id, order_id, "оплачен")
            # уведомление пользователю
            try:
                bot.send_message(chat_id, f"🔔 Платёж подтверждён. Заказ #{order_id} помечен как оплачен.")
            except Exception:
                pass
            return jsonify({"ok": True}), 200
        else:
            # пытаемся найти по payload (order_uid)
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
    # другие статусы просто логируем
    return jsonify({"ok": True}), 200

# ----------------------- Telegram bot: UI и логика -----------------------

def main_menu_inline():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("📈 Купить подписчиков", callback_data="menu_sub"))
    kb.add(types.InlineKeyboardButton("👁 Купить просмотры", callback_data="menu_view"))
    kb.add(types.InlineKeyboardButton("💬 Купить комментарии", callback_data="menu_com"))
    kb.add(types.InlineKeyboardButton("👤 Профиль", callback_data="profile"))
    if str(ADMIN_ID):
        # показываем админ кнопку только если задан ADMIN_ID (для безопасного доступа)
        kb.add(types.InlineKeyboardButton("🔐 Админ", callback_data="admin_panel"))
    return kb

def packages_markup(cat_key):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for amt, price in OFFERS.get(cat_key, {}).items():
        # убираем лишние единицы (точное число) — отображаем просто amt
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
        bot.edit_message_text("💬 Выбери пакет комментариев:", cid, call.message.chat.id, call.message.message_id, reply_markup=packages_markup("com"))
        # Note: fallback in case of api differences
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
        try:
            _, category, amt_str = data_call.split("_", 2)
        except ValueError:
            bot.answer_callback_query(call.id, "Неверные данные")
            return
        amount = int(amt_str)
        order_id = add_order(cid, category, amount)
        # Формула цены: берём price_rub из OFFERS
        price_rub = OFFERS.get(category, {}).get(amt_str, None)
        if price_rub is None:
            price_rub = amount  # fallback
        # конвертация RUB -> USD: ставим примерный курс 100 RUB = 1 USD (подставь реальный)
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
        # запомним ожидание ввода
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
            # очистим state
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
            # уведомим админа
            try:
                bot.send_message(ADMIN_ID, f"Пользователь {cid} отменил заказ #{idx}")
            except Exception:
                pass
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
                try:
                    bot.send_message(ADMIN_ID, f"Статус заказа {user_id}#{order_idx} изменён на {new_status}")
                except Exception:
                    pass
                try:
                    bot.send_message(int(user_id), f"🔔 Статус вашего заказа #{order_idx} обновлён: {new_status}")
                except Exception:
                    pass
                bot.answer_callback_query(call.id, "Статус изменён")
                return
    except Exception as e:
        bot.answer_callback_query(call.id, f"Ошибка: {e}")

# ----------------------- Запуск Flask + Bot (для локальной отладки через polling)
# На продакшне (Render/Railway) лучше использовать webhook mode для Telegram.
def run_flask():
    # Flask слушает PUBLIC_WEBHOOK_URL path /cryptobot/ipn
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

def run_bot_polling():
    bot.infinity_polling(timeout=60, long_polling_timeout=20)

# Если хочешь - можно запустить и Flask, и polling в отдельных потоках (удобно для локальной отладки),
# но на хостингах (Render/Railway) используем webhook подход для Telegram — см. ниже.
if __name__ == "__main__" and os.environ.get("RUN_MODE", "local") == "local":
    print("Запускаю Flask (IPN) и Telegram бот (polling)...")
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    run_bot_polling()

# ----------------------- Webhook mode support (для Render/Heroku/Railway)
# Если переменная окружения USE_WEBHOOK="1", мы экспортируем Flask endpoints для Telegram webhook:
#   /<BOT_TOKEN> - Telegram will POST updates here
#   /cryptobot/ipn - CryptoBot IPN handler (implemented above)
#
# Для Render: установи ENV USE_WEBHOOK=1 и PUBLIC_WEBHOOK_URL = https://your-app.onrender.com/cryptobot/ipn
if os.environ.get("USE_WEBHOOK") == "1":
    @app.route('/' + BOT_TOKEN, methods=['POST'])
    def telegram_webhook():
        json_str = request.get_data().decode('UTF-8')
        try:
            update = telebot.types.Update.de_json(json_str)
            bot.process_new_updates([update])
        except Exception as e:
            # лог ошибок
            print("Webhook error:", e)
        return "OK", 200

    # on startup, set webhook for Telegram to point to /<BOT_TOKEN>
    def set_telegram_webhook():
        # compose webhook URL
        domain = os.environ.get("WEB_DOMAIN")  # expected e.g. https://my-app.onrender.com
        if not domain:
            print("WEB_DOMAIN not set, webhook not configured.")
            return
        webhook_url = domain.rstrip("/") + "/" + BOT_TOKEN
        try:
            bot.remove_webhook()
            time.sleep(0.5)
            bot.set_webhook(url=webhook_url)
            print("Telegram webhook set to:", webhook_url)
        except Exception as e:
            print("Failed to set telegram webhook:", e)

    # Set webhook when starting Flask via WSGI environment (call manually in the entrypoint)
    # You can call set_telegram_webhook() from your start script.

# ========================================================================
# END OF FULL FILE
# ========================================================================

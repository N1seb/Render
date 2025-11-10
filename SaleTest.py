# SaleTest.py — Telegram бот с оплатой через CryptoBot TON
# Полностью адаптирован для Render (Flask + pyTelegramBotAPI)

import os
import json
import threading
import requests
import qrcode
from io import BytesIO
from flask import Flask, request, jsonify

import telebot
from telebot import types

# ----------------------- КОНФИГ -----------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CRYPTOPAY_API_TOKEN = os.environ.get("CRYPTOPAY_API_TOKEN")
PUBLIC_WEBHOOK_URL = os.environ.get("PUBLIC_WEBHOOK_URL") or "https://<YOUR_DOMAIN>.onrender.com/cryptobot/ipn"
ADMIN_ID = int(os.environ.get("ADMIN_ID", "1942740947"))

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN is missing or invalid! Set it in Render Environment Variables.")

if not CRYPTOPAY_API_TOKEN:
    raise ValueError("❌ CRYPTOPAY_API_TOKEN is missing! Set it in Render Environment Variables.")

DATA_FILE = "data.json"
TARGET_ASSET = "TON"

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
    register_user(chat_id)
    cid = str(chat_id)
    order = {
        "id": len(data["users"][cid]["orders"]) + 1,
        "category": category_key,
        "category_name": PRETTY.get(category_key, category_key),
        "amount": amount,
        "status": "ожидает оплаты",
        "invoice_id": None,
        "pay_url": None
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

# ----------------------- CryptoBot API -----------------------
def create_cryptobot_invoice(amount_value, asset_target, order_uid, description, callback_url=None):
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
        return r.json()
    except Exception as e:
        return {"error": True, "exception": str(e)}

# ----------------------- Вебхук CryptoBot -----------------------
@app.route("/cryptobot/ipn", methods=["POST"])
def cryptobot_ipn():
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "bad json"}), 400

    invoice_id = payload.get("invoiceId") or payload.get("id")
    status_field = payload.get("status")
    custom_payload = payload.get("payload")

    if not invoice_id:
        return jsonify({"ok": False, "error": "no invoice"}), 400

    data.setdefault("invoices", {})[str(invoice_id)] = {"payload": payload}
    save_data()

    if status_field and str(status_field).lower() in {"paid", "success", "confirmed"}:
        try:
            parts = str(custom_payload).split("_")
            chat_id = int(parts[0])
            order_id = int(parts[1])
            update_order_status(chat_id, order_id, "оплачен")
            bot.send_message(chat_id, f"✅ Платёж подтверждён! Заказ #{order_id} помечен как оплачен.")
        except Exception as e:
            print("Webhook error:", e)
    return jsonify({"ok": True}), 200

# ----------------------- Telegram логика -----------------------
def main_menu_inline():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("📈 Подписчики", callback_data="menu_sub"))
    kb.add(types.InlineKeyboardButton("👁 Просмотры", callback_data="menu_view"))
    kb.add(types.InlineKeyboardButton("💬 Комментарии", callback_data="menu_com"))
    kb.add(types.InlineKeyboardButton("👤 Профиль", callback_data="profile"))
    kb.add(types.InlineKeyboardButton("🔐 Админ", callback_data="admin_panel"))
    return kb

def packages_markup(cat_key):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for amt, price in OFFERS.get(cat_key, {}).items():
        kb.add(types.InlineKeyboardButton(f"{amt} — {price}₽", callback_data=f"order_{cat_key}_{amt}"))
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
    if data_call == "profile":
        show_profile(cid)
        return
    if data_call == "admin_panel":
        if cid != ADMIN_ID:
            bot.answer_callback_query(call.id, "Нет прав")
            return
        show_admin_panel(cid)
        return

    if data_call.startswith("order_"):
        _, category, amt_str = data_call.split("_", 2)
        amount = int(amt_str)
        order_id = add_order(cid, category, amount)
        price_rub = OFFERS.get(category, {}).get(amt_str, amount)
        price_usd = round(price_rub / 100.0, 2)
        order_uid = f"{cid}_{order_id}"
        resp = create_cryptobot_invoice(price_usd, TARGET_ASSET, order_uid, f"Заказ #{order_id} {PRETTY.get(category)}", callback_url=PUBLIC_WEBHOOK_URL)

        if isinstance(resp, dict) and resp.get("ok") is False:
            bot.send_message(cid, "Ошибка при создании чека.")
            return

        pay_url = resp.get("result", {}).get("pay_url") or resp.get("pay_url")
        if pay_url:
            bot.send_message(cid, f"Перейдите по ссылке для оплаты:\n{pay_url}")
            img_buf = BytesIO()
            qrcode.make(pay_url).save(img_buf, format="PNG")
            img_buf.seek(0)
            bot.send_photo(cid, img_buf)
        else:
            bot.send_message(cid, "Не удалось получить ссылку на оплату.")
        return

# ----------------------- Профиль и админ -----------------------
def show_profile(chat_id):
    register_user(chat_id)
    cid = str(chat_id)
    orders = data["users"][cid]["orders"]
    if not orders:
        bot.send_message(chat_id, "📭 У вас пока нет заказов.")
        return
    text = "📋 Ваши заказы:\n\n"
    for o in orders:
        text += f"#{o['id']} | {o['category_name']} — {o['amount']} шт. | {o['status']}\n"
    bot.send_message(chat_id, text)

def show_admin_panel(chat_id):
    if chat_id != ADMIN_ID:
        bot.send_message(chat_id, "Недостаточно прав.")
        return
    text = "📋 Все заказы:\n\n"
    for uid, udata in data["users"].items():
        for o in udata["orders"]:
            text += f"User {uid} | #{o['id']} | {o['category_name']} {o['amount']} | {o['status']}\n"
    bot.send_message(chat_id, text)

# ----------------------- Запуск -----------------------
def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)

def run_bot():
    bot.infinity_polling(timeout=60, long_polling_timeout=20)

if __name__ == "__main__":
    print("🚀 Запускаю Flask (IPN) и Telegram-бота...")
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()

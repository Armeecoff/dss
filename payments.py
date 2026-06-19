import os
import re
import uuid
import logging
from typing import Optional

from yookassa import Payment, Configuration
from telegram import Update, LabeledPrice, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

import database as db

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email.strip()))

def setup_yookassa():
    shop_id = os.getenv("YOOKASSA_SHOP_ID", "")
    secret_key = os.getenv("YOOKASSA_SECRET_KEY", "")
    if shop_id and secret_key:
        Configuration.account_id = shop_id
        Configuration.secret_key = secret_key
        return True
    return False

async def create_yookassa_payment(user_id: int, plan_id: int, email: str) -> Optional[str]:
    plan = await db.get_plan(plan_id)
    if not plan or not plan.get("price_rub"):
        return None
    if not setup_yookassa():
        return None

    bot_username = os.getenv("BOT_USERNAME", "bot")
    try:
        idempotence_key = str(uuid.uuid4())
        payment = Payment.create({
            "amount": {
                "value": f"{plan['price_rub']:.2f}",
                "currency": "RUB"
            },
            "confirmation": {
                "type": "redirect",
                "return_url": f"https://t.me/{bot_username}"
            },
            "capture": True,
            "description": f"Подписка «{plan['name']}» на {plan['duration_days']} дней",
            "receipt": {
                "customer": {
                    "email": email
                },
                "items": [
                    {
                        "description": f"Подписка «{plan['name']}» — {plan['duration_days']} дней",
                        "quantity": "1.00",
                        "amount": {
                            "value": f"{plan['price_rub']:.2f}",
                            "currency": "RUB"
                        },
                        "vat_code": "1",
                        "payment_mode": "full_payment",
                        "payment_subject": "service"
                    }
                ]
            },
            "metadata": {
                "user_id": str(user_id),
                "plan_id": str(plan_id)
            }
        }, idempotence_key)

        await db.save_pending_payment(
            payment.id, user_id, plan_id,
            float(plan["price_rub"]), "RUB"
        )
        return payment.confirmation.confirmation_url
    except Exception as e:
        logger.error(f"YooKassa error: {e}")
        return None

async def check_yookassa_payment(payment_id: str) -> bool:
    if not setup_yookassa():
        return False
    try:
        payment = Payment.find_one(payment_id)
        return payment.status == "succeeded"
    except Exception as e:
        logger.error(f"YooKassa check error: {e}")
        return False

async def send_stars_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_id: int):
    plan = await db.get_plan(plan_id)
    if not plan or not plan.get("price_stars"):
        if update.callback_query:
            await update.callback_query.answer("Оплата звёздами недоступна для этого тарифа.", show_alert=True)
        return

    title = f"Подписка «{plan['name']}»"
    description = f"Доступ к боту-архивариусу на {plan['duration_days']} дней"
    payload = f"sub_{plan_id}_{update.effective_user.id}"
    prices = [LabeledPrice(label=title, amount=plan["price_stars"])]

    await context.bot.send_invoice(
        chat_id=update.effective_user.id,
        title=title,
        description=description,
        payload=payload,
        currency="XTR",
        prices=prices
    )

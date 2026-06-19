import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
import database as db

logger = logging.getLogger(__name__)

async def is_admin(user_id: int) -> bool:
    admins = await db.get_admin_ids()
    return user_id in admins

def main_admin_keyboard(sub_enabled: bool) -> InlineKeyboardMarkup:
    sub_btn = "🔴 Выкл. подписку" if sub_enabled else "🟢 Вкл. подписку"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(sub_btn, callback_data="admin_toggle_sub")],
        [InlineKeyboardButton("📋 Тарифные планы", callback_data="admin_plans")],
        [InlineKeyboardButton("➕ Добавить тариф", callback_data="admin_add_plan")],
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("👑 Управление админами", callback_data="admin_manage_admins")],
        [InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast")],
    ])

async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sub_enabled = await db.is_subscription_enabled()
    status_text = "🟢 Включена" if sub_enabled else "🔴 Выключена (бесплатный доступ)"
    text = (
        "⚙️ <b>Панель администратора</b>\n\n"
        f"💳 Подписка: {status_text}\n\n"
        "Выберите действие:"
    )
    kb = main_admin_keyboard(sub_enabled)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_admin(user_id):
        await update.message.reply_text("❌ У вас нет прав администратора.")
        return
    await show_admin_panel(update, context)

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    if not await is_admin(user_id):
        await query.answer("❌ Нет прав.", show_alert=True)
        return

    data = query.data
    await query.answer()

    if data == "admin_back":
        await show_admin_panel(update, context)

    elif data == "admin_toggle_sub":
        current = await db.is_subscription_enabled()
        await db.set_setting("subscription_enabled", "0" if current else "1")
        new_state = not current
        msg = "🟢 Подписка <b>включена</b>. Доступ — только по подписке." if new_state else "🔴 Подписка <b>выключена</b>. Все пользователи имеют бесплатный доступ."
        await query.edit_message_text(msg, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="admin_back")]]))

    elif data == "admin_plans":
        await show_plans_list(update, context)

    elif data == "admin_add_plan":
        context.user_data["admin_state"] = "waiting_plan_name"
        await query.edit_message_text(
            "➕ <b>Добавление тарифа</b>\n\nВведите <b>название</b> тарифа (например: «7 дней», «Месяц», «Год»):",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin_cancel_input")]]))

    elif data == "admin_stats":
        stats = await db.get_stats()
        text = (
            "📊 <b>Статистика бота</b>\n\n"
            f"👤 Всего пользователей: <b>{stats['total_users']}</b>\n"
            f"✅ Активных подписок: <b>{stats['active_subs']}</b>\n"
            f"💾 Сохранено медиа: <b>{stats['total_saved']}</b>\n"
            f"💳 Успешных платежей: <b>{stats['total_payments']}</b>\n"
        )
        await query.edit_message_text(text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="admin_back")]]))

    elif data == "admin_manage_admins":
        admins = await db.get_admin_ids()
        text = "👑 <b>Администраторы</b>\n\n"
        if admins:
            text += "\n".join(f"• <code>{a}</code>" for a in admins)
        else:
            text += "Список пуст."
        text += "\n\nЧтобы добавить/удалить админа — введите его User ID:"
        context.user_data["admin_state"] = "waiting_admin_id"
        await query.edit_message_text(text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin_cancel_input")]]))

    elif data == "admin_broadcast":
        context.user_data["admin_state"] = "waiting_broadcast"
        await query.edit_message_text(
            "📢 <b>Рассылка</b>\n\nВведите текст сообщения для рассылки всем пользователям с подпиской:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin_cancel_input")]]))

    elif data == "admin_cancel_input":
        context.user_data.pop("admin_state", None)
        context.user_data.pop("new_plan", None)
        await show_admin_panel(update, context)

    elif data.startswith("admin_del_plan_"):
        plan_id = int(data.split("_")[-1])
        await db.delete_plan(plan_id)
        await query.edit_message_text("✅ Тариф удалён.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="admin_plans")]]))

    elif data.startswith("admin_toggle_plan_"):
        plan_id = int(data.split("_")[-1])
        await db.toggle_plan(plan_id)
        await show_plans_list(update, context)

async def show_plans_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    plans = await db.get_plans(active_only=False)
    text = "📋 <b>Тарифные планы</b>\n\n"
    buttons = []
    if plans:
        for p in plans:
            status = "✅" if p["is_active"] else "❌"
            rub = f"{p['price_rub']}₽" if p.get("price_rub") else "—"
            stars = f"{p['price_stars']}⭐" if p.get("price_stars") else "—"
            text += f"{status} <b>{p['name']}</b> | {p['duration_days']} дн. | {rub} / {stars}\n"
            buttons.append([
                InlineKeyboardButton(
                    f"{'🔴 Откл' if p['is_active'] else '🟢 Вкл'} {p['name']}",
                    callback_data=f"admin_toggle_plan_{p['id']}"
                ),
                InlineKeyboardButton("🗑 Удалить", callback_data=f"admin_del_plan_{p['id']}")
            ])
    else:
        text += "Тарифов нет."
    buttons.append([InlineKeyboardButton("➕ Добавить тариф", callback_data="admin_add_plan")])
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_back")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    if not await is_admin(user_id):
        return False

    state = context.user_data.get("admin_state")
    if not state:
        return False

    text = update.message.text.strip()

    if state == "waiting_plan_name":
        context.user_data["new_plan"] = {"name": text}
        context.user_data["admin_state"] = "waiting_plan_days"
        await update.message.reply_text(
            f"✅ Название: <b>{text}</b>\n\nТеперь введите количество <b>дней</b> подписки (например: 7, 30, 365):",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin_cancel_input")]]))
        return True

    elif state == "waiting_plan_days":
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text("❌ Введите корректное число дней (например: 7, 30, 365).")
            return True
        context.user_data["new_plan"]["days"] = int(text)
        context.user_data["admin_state"] = "waiting_plan_price_rub"
        await update.message.reply_text(
            "Введите цену в <b>рублях</b> (например: 199.99).\nЕсли оплата рублями не нужна — введите <code>0</code>:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin_cancel_input")]]))
        return True

    elif state == "waiting_plan_price_rub":
        try:
            price_rub = float(text.replace(",", "."))
            context.user_data["new_plan"]["price_rub"] = price_rub if price_rub > 0 else None
        except ValueError:
            await update.message.reply_text("❌ Введите число, например: 199.99 или 0.")
            return True
        context.user_data["admin_state"] = "waiting_plan_price_stars"
        await update.message.reply_text(
            "Введите цену в <b>Telegram Stars</b> (целое число, например: 50).\nЕсли не нужна — введите <code>0</code>:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin_cancel_input")]]))
        return True

    elif state == "waiting_plan_price_stars":
        if not text.isdigit():
            await update.message.reply_text("❌ Введите целое число, например: 50 или 0.")
            return True
        price_stars = int(text)
        plan_data = context.user_data.get("new_plan", {})
        plan_id = await db.add_plan(
            name=plan_data.get("name", "Тариф"),
            duration_days=plan_data.get("days", 30),
            price_rub=plan_data.get("price_rub"),
            price_stars=price_stars if price_stars > 0 else None
        )
        context.user_data.pop("admin_state", None)
        context.user_data.pop("new_plan", None)
        rub_str = f"{plan_data.get('price_rub')}₽" if plan_data.get("price_rub") else "нет"
        stars_str = f"{price_stars}⭐" if price_stars > 0 else "нет"
        await update.message.reply_text(
            f"✅ <b>Тариф добавлен!</b>\n\n"
            f"📌 Название: {plan_data.get('name')}\n"
            f"📅 Длительность: {plan_data.get('days')} дней\n"
            f"💰 Цена: {rub_str} / {stars_str}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 К тарифам", callback_data="admin_plans"),
                                                InlineKeyboardButton("◀️ В меню", callback_data="admin_back")]]))
        return True

    elif state == "waiting_admin_id":
        if not text.isdigit():
            await update.message.reply_text("❌ Введите числовой User ID.")
            return True
        target_id = int(text)
        admins = await db.get_admin_ids()
        if target_id in admins:
            await db.remove_admin(target_id)
            msg = f"✅ Пользователь <code>{target_id}</code> удалён из администраторов."
        else:
            await db.add_admin(target_id)
            msg = f"✅ Пользователь <code>{target_id}</code> добавлен как администратор."
        context.user_data.pop("admin_state", None)
        await update.message.reply_text(msg, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ В меню", callback_data="admin_back")]]))
        return True

    elif state == "waiting_broadcast":
        users = await db.get_all_users()
        count = 0
        for uid in users:
            try:
                await context.bot.send_message(uid, f"📢 <b>Сообщение от администратора:</b>\n\n{text}", parse_mode="HTML")
                count += 1
            except Exception:
                pass
        context.user_data.pop("admin_state", None)
        await update.message.reply_text(f"✅ Сообщение отправлено {count} пользователям.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ В меню", callback_data="admin_back")]]))
        return True

    return False

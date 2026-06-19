import os
import io
import logging
from datetime import datetime
from typing import Optional

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, Message, User
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, PreCheckoutQueryHandler,
    TypeHandler, filters, ContextTypes
)

import database as db
import admin as adm
import payments as pay

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.DEBUG
)
# Шумные библиотеки — глушим, наш код (bot, __main__) остаётся на DEBUG
for _noisy in ("httpx", "httpcore", "telegram", "apscheduler",
               "aiosqlite", "asyncio", "urllib3", "charset_normalizer"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📊 Мой статус", "💳 Купить подписку"],
        ["📖 Инструкция", "ℹ️ Помощь"],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите действие..."
)


# ─── TypeHandlers для всех бизнес-апдейтов (TypeHandler надёжнее MessageHandler) ──

class _BusinessConnectionHandler(TypeHandler):
    def __init__(self, callback):
        super().__init__(Update, callback)

    def check_update(self, update: object) -> bool:
        return isinstance(update, Update) and bool(getattr(update, "business_connection", None))


class _BusinessMessageHandler(TypeHandler):
    def __init__(self, callback):
        super().__init__(Update, callback)

    def check_update(self, update: object) -> bool:
        return isinstance(update, Update) and bool(getattr(update, "business_message", None))


class _EditedBusinessMessageHandler(TypeHandler):
    def __init__(self, callback):
        super().__init__(Update, callback)

    def check_update(self, update: object) -> bool:
        return isinstance(update, Update) and bool(getattr(update, "edited_business_message", None))


class _DeletedBusinessMessagesHandler(TypeHandler):
    def __init__(self, callback):
        super().__init__(Update, callback)

    def check_update(self, update: object) -> bool:
        return isinstance(update, Update) and bool(getattr(update, "deleted_business_messages", None))


# ─── Вспомогательные функции ──────────────────────────────────────────────────

def get_sender_name(user: Optional[User]) -> str:
    if not user:
        return "Неизвестный"
    parts = [user.first_name or ""]
    if user.last_name:
        parts.append(user.last_name)
    name = " ".join(parts).strip() or "Без имени"
    if user.username:
        name += f" (@{user.username})"
    return name


async def resolve_owner_id(context, connection_id: str) -> Optional[int]:
    """
    Получает owner_id для connection_id.
    Сначала из локального кэша (БД), затем через API Telegram.
    Это критично: вызов get_business_connection при каждом сообщении ненадёжен.
    """
    # 1. Быстрый путь — из кэша
    cached = await db.get_connection_owner(connection_id)
    if cached:
        return cached

    # 2. Медленный путь — API
    try:
        conn = await context.bot.get_business_connection(connection_id)
        owner_id = conn.user.id
        # Сохраняем в кэш на будущее
        await db.store_business_connection(connection_id, owner_id, conn.is_enabled)
        logger.info(f"Resolved & cached: connection_id={connection_id} → owner={owner_id}")
        return owner_id
    except Exception as e:
        logger.error(f"get_business_connection({connection_id}) failed: {e}")
        return None


async def check_access(user_id: int) -> bool:
    if not await db.is_subscription_enabled():
        return True
    if await adm.is_admin(user_id):
        return True
    return await db.has_active_subscription(user_id)


def extract_media_from_reply(reply_msg) -> tuple:
    """
    Извлекает медиа из reply_to_message (PTB Message или dict).
    Telegram НЕ присылает view-once как отдельный business_message —
    они приходят только в reply_to_message с has_protected_content=True.
    Возвращает (media_type, file_id, sender_id, sender_name).
    """
    if not reply_msg:
        return None, None, None, None

    # Проверяем is_protected
    if isinstance(reply_msg, dict):
        protected = reply_msg.get("has_protected_content") or reply_msg.get("has_media_spoiler")
    else:
        protected = (
            bool(getattr(reply_msg, "has_protected_content", False))
            or bool(getattr(reply_msg, "has_media_spoiler", False))
        )

    if not protected:
        logger.debug("[REPLY] reply_to_message не защищённое — пропуск")
        return None, None, None, None

    # Извлекаем медиа
    media_type = file_id = None
    if isinstance(reply_msg, dict):
        if reply_msg.get("voice"):
            media_type, file_id = "voice", reply_msg["voice"]["file_id"]
        elif reply_msg.get("video_note"):
            media_type, file_id = "video_note", reply_msg["video_note"]["file_id"]
        elif reply_msg.get("photo"):
            photos = reply_msg["photo"]
            media_type, file_id = "photo", photos[-1]["file_id"]
        elif reply_msg.get("video"):
            media_type, file_id = "video", reply_msg["video"]["file_id"]
        elif reply_msg.get("audio"):
            media_type, file_id = "audio", reply_msg["audio"]["file_id"]
        elif reply_msg.get("document"):
            media_type, file_id = "document", reply_msg["document"]["file_id"]
        elif reply_msg.get("animation"):
            media_type, file_id = "animation", reply_msg["animation"]["file_id"]
        # sender
        frm = reply_msg.get("from") or {}
        sender_id = frm.get("id")
        fn = frm.get("first_name", "")
        ln = frm.get("last_name", "")
        un = frm.get("username", "")
        sender_name = (f"{fn} {ln}".strip() or un or str(sender_id or "?"))
        if un:
            sender_name += f" (@{un})"
    else:
        media_type, file_id = extract_media(reply_msg)
        sender_id = reply_msg.from_user.id if reply_msg.from_user else None
        sender_name = get_sender_name(reply_msg.from_user)

    if not file_id:
        logger.debug("[REPLY] reply_to_message защищённое, но медиа не найдено")
        return None, None, None, None

    logger.info(
        f"[REPLY] Найдено view-once медиа в reply_to_message: "
        f"type={media_type} file={file_id[:12]} sender={sender_name!r}"
    )
    return media_type, file_id, sender_id, sender_name


def extract_media(message: Message):
    """Возвращает (media_type, file_id). file_id=None для текстовых сообщений."""
    # Логируем какие медиа-поля НЕ None для диагностики
    present = []
    if message.voice:       present.append(f"voice({message.voice.file_id[:12]})")
    if message.video_note:  present.append(f"video_note({message.video_note.file_id[:12]})")
    if message.photo:       present.append(f"photo[{len(message.photo)}]({message.photo[-1].file_id[:12]})")
    if message.video:       present.append(f"video({message.video.file_id[:12]})")
    if message.audio:       present.append(f"audio({message.audio.file_id[:12]})")
    if message.document:    present.append(f"document({message.document.file_id[:12]})")
    if message.sticker:     present.append(f"sticker({message.sticker.file_id[:12]})")
    if message.animation:   present.append(f"animation({message.animation.file_id[:12]})")
    if message.text:        present.append(f"text({repr(message.text[:20])})")
    if message.caption:     present.append(f"caption({repr(message.caption[:20])})")
    # Флаги
    spoiler = getattr(message, "has_media_spoiler", None)
    protected = getattr(message, "has_protected_content", None)
    if spoiler:   present.append("HAS_SPOILER")
    if protected: present.append("HAS_PROTECTED")
    logger.debug(f"[extract_media] msg_id={message.message_id} fields={present}")

    if message.voice:
        return "voice", message.voice.file_id
    if message.video_note:
        return "video_note", message.video_note.file_id
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        return "video", message.video.file_id
    if message.audio:
        return "audio", message.audio.file_id
    if message.document:
        return "document", message.document.file_id
    if message.sticker:
        return "sticker", message.sticker.file_id
    if message.animation:
        return "animation", message.animation.file_id
    if message.text or message.caption:
        return "text", None
    return None, None


def chat_title(chat) -> str:
    return (getattr(chat, "title", None)
            or getattr(chat, "first_name", None)
            or str(chat.id))


# ─── Пересылка контента владельцу ────────────────────────────────────────────

async def forward_to_owner(
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
    event: str,
    sender_name: str,
    chat_name: str,
    media_type: Optional[str],
    file_id: Optional[str],
    text: Optional[str],
    caption: Optional[str],
    orig_text: Optional[str] = None,
    orig_caption: Optional[str] = None,
):
    icons = {"deleted": "🗑", "edited": "✏️", "once": "📸", "media": "📥"}
    labels = {"deleted": "Удалённое", "edited": "Изменённое", "once": "Одноразовое", "media": "Новое медиа"}
    icon = icons.get(event, "📨")
    label = labels.get(event, event)

    header = (
        f"{icon} <b>{label} сообщение</b>\n"
        f"👤 {sender_name}\n"
        f"💬 {chat_name}"
    )

    # Изменённый текст
    if event == "edited" and media_type == "text":
        was = (orig_text or "—")[:400]
        now = (text or "—")[:400]
        try:
            await context.bot.send_message(
                owner_id,
                f"{header}\n\n"
                f"📝 <b>Было:</b>\n<i>{was}</i>\n\n"
                f"✏️ <b>Стало:</b>\n<i>{now}</i>",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"forward edited text → {owner_id}: {e}")
        return

    note = header
    if event == "edited" and (orig_caption or orig_text):
        was = (orig_caption or orig_text or "")[:200]
        note += f"\n📝 <b>Было подпись:</b> <i>{was}</i>"
    if event in ("deleted", "once", "media") and (text or caption):
        body = (text or caption or "")[:300]
        note += f"\n\n💬 <i>{body}</i>"

    logger.info(
        f"[FWD] event={event} media_type={media_type} "
        f"file_id={'YES:'+file_id[:12] if file_id else 'NO'} → owner={owner_id}"
    )

    # Ошибки Telegram при которых нужно скачать файл и переотправить
    _RESTRICTED_ERRORS = (
        "selfdestructingphoto",
        "File_reference_expired",
        "wrong file identifier",
        "FILE_REFERENCE_EXPIRED",
    )

    async def _send_by_type(fid_or_bio):
        """Отправить медиа — file_id (str) или BytesIO."""
        if media_type == "photo":
            await context.bot.send_photo(owner_id, fid_or_bio, caption=note, parse_mode="HTML")
        elif media_type == "video":
            await context.bot.send_video(owner_id, fid_or_bio, caption=note, parse_mode="HTML")
        elif media_type == "voice":
            await context.bot.send_voice(owner_id, fid_or_bio, caption=note, parse_mode="HTML")
        elif media_type == "video_note":
            await context.bot.send_video_note(owner_id, fid_or_bio)
            await context.bot.send_message(owner_id, note, parse_mode="HTML")
        elif media_type == "audio":
            await context.bot.send_audio(owner_id, fid_or_bio, caption=note, parse_mode="HTML")
        elif media_type == "document":
            await context.bot.send_document(owner_id, fid_or_bio, caption=note, parse_mode="HTML")
        elif media_type == "animation":
            await context.bot.send_animation(owner_id, fid_or_bio, caption=note, parse_mode="HTML")
        elif media_type == "sticker":
            await context.bot.send_sticker(owner_id, fid_or_bio)
            await context.bot.send_message(owner_id, note, parse_mode="HTML")
        else:
            await context.bot.send_message(owner_id, note, parse_mode="HTML")

    async def _try_download_resend():
        """Скачиваем файл и переотправляем как BytesIO (обход ограничений selfdestructing)."""
        logger.info(f"[FWD] Пробуем скачать и переотправить {media_type} file_id={file_id[:12]}")
        tg_file = await context.bot.get_file(file_id)
        data = await tg_file.download_as_bytearray()
        ext_map = {
            "photo": "jpg", "video": "mp4", "voice": "ogg",
            "video_note": "mp4", "audio": "mp3", "document": "bin",
            "animation": "mp4", "sticker": "webp",
        }
        bio = io.BytesIO(bytes(data))
        bio.name = f"media.{ext_map.get(media_type, 'bin')}"
        await _send_by_type(bio)
        logger.info(f"[FWD] ✅ Скачан и переотправлен {media_type} ({len(data)} bytes) → {owner_id}")

    if not file_id:
        try:
            await context.bot.send_message(owner_id, note, parse_mode="HTML")
            logger.debug(f"[FWD] sent text-only → {owner_id}")
        except Exception as e:
            logger.error(f"[FWD] send_message failed: {e}")
        return

    # Шаг 1 — попытка прямой отправки по file_id
    try:
        await _send_by_type(file_id)
        logger.debug(f"[FWD] sent {media_type} direct → {owner_id}")
        return
    except Exception as e:
        err_str = str(e)
        if any(r in err_str for r in _RESTRICTED_ERRORS):
            logger.warning(f"[FWD] Ограничение Telegram ({err_str}) — пробуем скачать файл")
        else:
            logger.error(f"[FWD] send {media_type} direct failed (неизвестная ошибка): {e}")
            # Для неизвестных ошибок тоже пробуем скачать
            logger.warning(f"[FWD] Пробуем скачать как запасной вариант")

    # Шаг 2 — скачать файл и переотправить
    try:
        await _try_download_resend()
        return
    except Exception as e2:
        logger.error(f"[FWD] download+resend failed для {media_type}: {e2}")

    # Шаг 3 — fallback: текстовое уведомление
    media_labels = {
        "photo": "фото 📸", "video": "видео 🎥", "voice": "голосовое 🎙",
        "video_note": "кружочек ⭕", "audio": "аудио 🎵",
        "document": "файл 📄", "sticker": "стикер", "animation": "gif",
    }
    mlabel = media_labels.get(media_type, media_type)
    fallback = (
        f"{note}\n\n"
        f"⚠️ <i>Telegram не позволил переслать {mlabel} напрямую.\n"
        f"Файл зашифрован как одноразовый и недоступен для пересылки.</i>"
    )
    try:
        await context.bot.send_message(owner_id, fallback, parse_mode="HTML")
        logger.info(f"[FWD] Отправлено текстовое уведомление вместо {media_type}")
    except Exception as e3:
        logger.error(f"[FWD] fallback message failed: {e3}")


# ─── Хендлеры бизнес-режима ───────────────────────────────────────────────────

async def on_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Срабатывает когда пользователь подключает/отключает бота в Business."""
    conn = update.business_connection
    owner_id = conn.user.id

    # Проверяем права
    rights = conn.rights
    rights_info = {}
    if rights:
        rights_info = {
            "can_reply":               getattr(rights, "can_reply", False),
            "can_read_messages":       getattr(rights, "can_read_messages", False),
            "can_delete_sent":         getattr(rights, "can_delete_sent_messages", False),
            "can_delete_all":          getattr(rights, "can_delete_all_messages", False),
        }

    logger.info(
        f"BusinessConnection: id={conn.id} user={owner_id} "
        f"enabled={conn.is_enabled} rights={rights_info}"
    )

    # Всегда сохраняем в кэш
    await db.store_business_connection(conn.id, owner_id, conn.is_enabled)

    if conn.is_enabled:
        # Собираем список активных прав
        rights_lines = []
        can_read = rights_info.get("can_read_messages", False) if rights else False
        if rights:
            if rights_info.get("can_reply"):            rights_lines.append("✅ Отвечать от имени аккаунта")
            if rights_info.get("can_read_messages"):    rights_lines.append("✅ Читать сообщения")
            if rights_info.get("can_delete_sent"):      rights_lines.append("✅ Удалять свои сообщения")
            if rights_info.get("can_delete_all"):       rights_lines.append("✅ Удалять все сообщения")

        rights_text = "\n".join(rights_lines) if rights_lines else "ℹ️ Права не заданы явно (стандартный режим)"

        # Предупреждение если нет нужных прав
        warning = ""
        if not can_read and rights:
            warning = (
                "\n\n⚠️ <b>Внимание!</b> Право <b>«Читать сообщения»</b> не выдано.\n"
                "Для перехвата удалённых и изменённых сообщений отключите бота и подключите снова, "
                "выдав это разрешение."
            )

        try:
            await context.bot.send_message(
                owner_id,
                "✅ <b>Бот подключён к вашему бизнес-аккаунту!</b>\n\n"
                "Теперь я буду:\n"
                "🗑 Перехватывать удалённые сообщения\n"
                "✏️ Сохранять изменённые сообщения\n"
                "📸 Фиксировать одноразовые фото и видео\n"
                "🎙 Ловить голосовые сразу при получении\n"
                "⭕ Ловить кружочки сразу при получении\n\n"
                f"<b>Выданные права:</b>\n{rights_text}"
                f"{warning}\n\n"
                "Всё будет приходить сюда 👆",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"on_business_connection notify: {e}")
    else:
        try:
            await context.bot.send_message(
                owner_id,
                "❌ Бот отключён от вашего бизнес-аккаунта.\n"
                "Чтобы снова включить — подключите через Настройки → Business → Чат-боты.",
            )
        except Exception as e:
            logger.error(f"on_business_connection disable notify: {e}")


async def on_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Каждое сообщение в подключённом бизнес-чате.
    Кэшируем ВСЕ сообщения. Голосовые и кружочки — пересылаем сразу же.
    """
    msg = update.business_message
    if not msg:
        return

    connection_id = getattr(msg, "business_connection_id", None)
    if not connection_id:
        logger.warning(f"business_message без connection_id: msg_id={msg.message_id}")
        return

    owner_id = await resolve_owner_id(context, connection_id)
    if not owner_id:
        logger.error(f"Не удалось определить owner для connection_id={connection_id}")
        return

    media_type, file_id = extract_media(msg)
    sender_name = get_sender_name(msg.from_user)

    is_once = (
        bool(getattr(msg, "has_media_spoiler", False))
        or bool(getattr(msg, "has_protected_content", False))
    )

    logger.info(
        f"[BIZ MSG] id={msg.message_id} chat={msg.chat.id} conn={connection_id[:12]} "
        f"type={media_type} file={'YES:'+file_id[:12] if file_id else 'NO'} "
        f"is_once={is_once} owner={owner_id}"
    )
    logger.debug(
        f"[BIZ MSG DETAIL] sender={sender_name!r} "
        f"has_media_spoiler={getattr(msg,'has_media_spoiler',None)} "
        f"has_protected={getattr(msg,'has_protected_content',None)} "
        f"text={msg.text!r:.40} caption={msg.caption!r:.40}"
    )

    # Кэшируем каждое сообщение (нужно для восстановления при удалении)
    await db.cache_business_message(
        connection_id=connection_id,
        chat_id=msg.chat.id,
        message_id=msg.message_id,
        owner_id=owner_id,
        sender_id=msg.from_user.id if msg.from_user else None,
        sender_name=sender_name,
        media_type=media_type,
        file_id=file_id,
        text=msg.text,
        caption=msg.caption,
    )
    logger.debug(f"[BIZ MSG] Закэшировано: msg_id={msg.message_id}")

    if not await check_access(owner_id):
        logger.info(f"[BIZ MSG] owner={owner_id} нет доступа (нет подписки) — пропуск пересылки")
        return

    cname = chat_title(msg.chat)

    # Всё медиа пересылаем НЕМЕДЛЕННО:
    # - голосовые/кружочки: эфемерны — владелец может пропустить
    # - одноразовые: исчезают после просмотра
    # - любое другое медиа: может быть удалено — нужна копия сразу
    # Текстовые сообщения НЕ пересылаем (лишний спам), только кэшируем для случая удаления
    if file_id:
        event_type = "once" if (is_once or media_type in ("voice", "video_note")) else "media"
        logger.info(f"[BIZ MSG] ▶ Пересылаем {media_type} event={event_type} → owner={owner_id}")
        try:
            await db.save_media_event(
                owner_id, msg.chat.id,
                msg.from_user.id if msg.from_user else None,
                sender_name, media_type, file_id, msg.caption, event_type
            )
            logger.debug(f"[BIZ MSG] save_media_event OK")
        except Exception as e:
            logger.error(f"[BIZ MSG] save_media_event FAILED: {e}")
        try:
            await forward_to_owner(
                context, owner_id, event_type,
                sender_name, cname,
                media_type, file_id, msg.text, msg.caption
            )
            logger.info(f"[BIZ MSG] ✅ Переслано {media_type} → owner={owner_id}")
        except Exception as e:
            logger.error(f"[BIZ MSG] forward_to_owner FAILED: {e}", exc_info=True)
    else:
        logger.debug(f"[BIZ MSG] Нет медиа (type={media_type}) — только кэш, не пересылаем")

    # ── Перехват view-once медиа из reply_to_message ─────────────────────────
    # Telegram НЕ присылает view-once фото/видео как отдельный business_message
    # (нет отдельного update), но они появляются в reply_to_message
    # следующего сообщения с has_protected_content=True.
    reply = msg.reply_to_message
    if reply:
        logger.debug(
            f"[REPLY] msg_id={msg.message_id} has reply → "
            f"reply_id={reply.message_id} "
            f"protected={getattr(reply,'has_protected_content',None)} "
            f"spoiler={getattr(reply,'has_media_spoiler',None)}"
        )
        r_type, r_fid, r_sid, r_sname = extract_media_from_reply(reply)
        if r_fid and r_type:
            reply_msg_id = reply.message_id
            # Дедупликация: не пересылаем одно и то же view-once дважды
            already = await db.get_cached_message(connection_id, msg.chat.id, reply_msg_id)
            if already:
                logger.debug(f"[REPLY ONCE] Уже в кэше msg_id={reply_msg_id} — пропуск")
            else:
                logger.info(
                    f"[REPLY ONCE] ⚡ Перехвачено view-once {r_type} "
                    f"из reply_to msg_id={reply_msg_id} → owner={owner_id}"
                )
                # Кэшируем под ID оригинального view-once сообщения
                await db.cache_business_message(
                    connection_id=connection_id,
                    chat_id=msg.chat.id,
                    message_id=reply_msg_id,
                    owner_id=owner_id,
                    sender_id=r_sid,
                    sender_name=r_sname,
                    media_type=r_type,
                    file_id=r_fid,
                    text=None,
                    caption=None,
                )
                if await check_access(owner_id):
                    try:
                        await db.save_media_event(
                            owner_id, msg.chat.id, r_sid,
                            r_sname, r_type, r_fid, None, "once"
                        )
                        await forward_to_owner(
                            context, owner_id, "once",
                            r_sname, cname,
                            r_type, r_fid, None, None
                        )
                        logger.info(
                            f"[REPLY ONCE] ✅ Переслано view-once {r_type} → owner={owner_id}"
                        )
                    except Exception as e:
                        logger.error(f"[REPLY ONCE] forward failed: {e}", exc_info=True)


async def on_edited_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сообщение отредактировано в подключённом бизнес-чате."""
    msg = update.edited_business_message
    if not msg:
        return

    connection_id = getattr(msg, "business_connection_id", None)
    if not connection_id:
        return

    owner_id = await resolve_owner_id(context, connection_id)
    if not owner_id:
        return

    if not await check_access(owner_id):
        return

    # Берём оригинал из кэша
    cached = await db.get_cached_message(connection_id, msg.chat.id, msg.message_id)
    orig_text = cached.get("text") if cached else None
    orig_caption = cached.get("caption") if cached else None

    media_type, file_id = extract_media(msg)
    sender_name = get_sender_name(msg.from_user)
    cname = chat_title(msg.chat)

    logger.info(f"[EDITED BIZ] id={msg.message_id} chat={msg.chat.id} owner={owner_id}")

    await forward_to_owner(
        context, owner_id, "edited",
        sender_name, cname,
        media_type, file_id, msg.text, msg.caption,
        orig_text=orig_text, orig_caption=orig_caption
    )

    # Обновляем кэш
    await db.update_cached_message_text(
        connection_id, msg.chat.id, msg.message_id, msg.text, msg.caption
    )


async def on_deleted_business_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Сообщения удалены из подключённого бизнес-чата.
    Telegram присылает только ID — контент берём из кэша.
    Медиа было уже переслано при получении, поэтому просто уведомляем об удалении.
    """
    info = update.deleted_business_messages
    if not info:
        return

    connection_id = info.business_connection_id
    owner_id = await resolve_owner_id(context, connection_id)
    if not owner_id:
        logger.error(f"[DELETED] Нет owner для connection_id={connection_id}")
        return

    if not await check_access(owner_id):
        return

    cname = chat_title(info.chat)
    logger.info(f"[DELETED BIZ] ids={info.message_ids} chat={info.chat.id} owner={owner_id}")

    found = 0
    for msg_id in info.message_ids:
        cached = await db.get_cached_message(connection_id, info.chat.id, msg_id)
        if not cached:
            logger.warning(f"[DELETED] Нет в кэше: msg_id={msg_id} chat={info.chat.id}")
            continue
        found += 1
        sender_name = cached.get("sender_name") or "Неизвестный"
        media_type = cached.get("media_type")
        file_id = cached.get("file_id")
        text = cached.get("text")
        caption = cached.get("caption")

        if file_id and media_type and media_type != "text":
            # Медиа уже было переслано при получении → только уведомляем об удалении
            await db.save_media_event(
                owner_id, info.chat.id, cached.get("sender_id"),
                sender_name, media_type, file_id, caption, "deleted"
            )
            media_labels = {
                "photo": "фото", "video": "видео", "voice": "голосовое",
                "video_note": "кружочек", "audio": "аудио",
                "document": "файл", "sticker": "стикер", "animation": "gif"
            }
            mlabel = media_labels.get(media_type, media_type)
            note = (
                f"🗑 <b>Удалено: {mlabel}</b>\n"
                f"👤 {sender_name}\n"
                f"💬 {cname}\n"
                f"<i>(содержимое уже было переслано при получении ↑)</i>"
            )
            if caption:
                note += f"\n\n📝 <i>{caption[:200]}</i>"
            try:
                await context.bot.send_message(owner_id, note, parse_mode="HTML")
            except Exception as e:
                logger.error(f"[DELETED] media notice: {e}")
        else:
            # Текстовое сообщение — пересылаем содержимое (оно не было переслано ранее)
            await forward_to_owner(
                context, owner_id, "deleted",
                sender_name, cname,
                media_type, file_id, text, caption
            )

    if found == 0:
        count = len(info.message_ids)
        try:
            await context.bot.send_message(
                owner_id,
                f"🗑 <b>Удалено {count} сообщ.</b> в чате <b>{cname}</b>\n"
                f"<i>Содержимого нет в кэше — сообщения поступили до запуска или до подключения бота.</i>",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"[DELETED] notice: {e}")


# ─── Пользовательские команды ─────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sub_enabled = await db.is_subscription_enabled()
    has_access = await check_access(user.id)

    bot_info = await context.bot.get_me()
    bot_username = f"@{bot_info.username}"

    inline_buttons = [
        [InlineKeyboardButton(f"📋 {bot_username}", callback_data="send_username")],
        [InlineKeyboardButton("📖 Инструкция по подключению", callback_data="instructions")],
    ]

    if sub_enabled and not has_access:
        inline_buttons.append([InlineKeyboardButton("💳 Купить подписку", callback_data="buy_sub")])
    elif has_access and sub_enabled:
        sub = await db.get_user_subscription(user.id)
        if sub:
            expires = datetime.fromisoformat(sub["expires_at"])
            inline_buttons.insert(0, [InlineKeyboardButton(
                f"✅ Подписка до {expires.strftime('%d.%m.%Y')}",
                callback_data="sub_info"
            )])

    await update.message.reply_text(
        f"🛡 Привет, {user.first_name}! Я — твой личный архивариус.\n\n"
        "Что я умею:\n"
        "🗑 Перехватываю <b>удалённые и изменённые</b> сообщения\n"
        "📸 Сохраняю <b>одноразовые фото и видео</b> мгновенно\n"
        "🎙 Фиксирую <b>голосовые</b> сразу при получении\n"
        "⭕ Ловлю <b>кружочки</b> сразу при получении\n\n"
        "Подключение занимает 30 секунд ↓",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_buttons)
    )
    await update.message.reply_text(
        "Используй кнопки меню 👇",
        reply_markup=MAIN_KEYBOARD
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_info = await context.bot.get_me()
    await update.message.reply_text(
        "ℹ️ <b>Помощь</b>\n\n"
        "/start — главное меню\n"
        "/status — статус подписки\n"
        "/subscribe — купить подписку\n"
        "/admin — панель администратора\n\n"
        "<b>Как подключить бота:</b>\n"
        "Настройки → Telegram Business → Чат-боты\n"
        f"→ введите <code>@{bot_info.username}</code> → Подключить\n\n"
        "<b>Что перехватывается:</b>\n"
        "• 🗑 Удалённые сообщения (восстанавливаем из кэша)\n"
        "• ✏️ Изменённые (было → стало)\n"
        "• 🎙 Голосовые — сразу при получении\n"
        "• ⭕ Кружочки — сразу при получении\n"
        "• 📸 Одноразовые фото/видео — сразу\n\n"
        "⚡ <i>Бот кэширует все входящие сообщения автоматически</i>",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sub_enabled = await db.is_subscription_enabled()

    if not sub_enabled:
        await update.message.reply_text(
            "✅ Подписка не требуется — бот работает <b>бесплатно</b> для всех!",
            parse_mode="HTML", reply_markup=MAIN_KEYBOARD
        )
        return

    if await adm.is_admin(user.id):
        await update.message.reply_text(
            "👑 У вас <b>безлимитный доступ</b> как у администратора.",
            parse_mode="HTML", reply_markup=MAIN_KEYBOARD
        )
        return

    sub = await db.get_user_subscription(user.id)
    if not sub:
        await update.message.reply_text(
            "❌ У вас нет активной подписки.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("💳 Купить подписку", callback_data="buy_sub")]]
            )
        )
        return

    expires = datetime.fromisoformat(sub["expires_at"])
    is_active = expires > datetime.now()
    days_left = max((expires - datetime.now()).days, 0)
    plan = await db.get_plan(sub["plan_id"]) if sub.get("plan_id") else None
    plan_name = plan["name"] if plan else "Неизвестный тариф"

    text = (
        f"💳 <b>Статус подписки</b>\n\n"
        f"📌 Тариф: {plan_name}\n"
        f"📅 До: {expires.strftime('%d.%m.%Y %H:%M')}\n"
        f"⏳ Осталось: {days_left} дн.\n"
        f"{'✅' if is_active else '❌'} {'Активна' if is_active else 'Истекла'}\n"
        f"💰 Способ: {sub.get('payment_method', '—')}"
    )
    buttons = []
    if not is_active or days_left < 7:
        buttons.append([InlineKeyboardButton("🔄 Продлить", callback_data="buy_sub")])
    await update.message.reply_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else MAIN_KEYBOARD
    )


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_plans(update, context, via_command=True)


async def show_plans(update: Update, context: ContextTypes.DEFAULT_TYPE, via_command=False):
    plans = await db.get_plans(active_only=True)
    if not plans:
        text = "❌ Тарифные планы пока не настроены. Обратитесь к администратору."
        if via_command:
            await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)
        else:
            await update.callback_query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Назад", callback_data="main_menu")]]
                )
            )
        return

    text = "💳 <b>Выберите тарифный план:</b>\n\n"
    buttons = []
    for p in plans:
        rub = f"{p['price_rub']}₽" if p.get("price_rub") else None
        stars = f"{p['price_stars']}⭐" if p.get("price_stars") else None
        price_str = " / ".join(filter(None, [rub, stars]))
        text += f"📌 <b>{p['name']}</b> — {p['duration_days']} дн. | {price_str}\n"
        row = []
        if p.get("price_rub"):
            row.append(InlineKeyboardButton(f"💳 {rub}", callback_data=f"pay_rub_{p['id']}"))
        if p.get("price_stars"):
            row.append(InlineKeyboardButton(f"⭐ {stars}", callback_data=f"pay_stars_{p['id']}"))
        if row:
            buttons.append(row)
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="main_menu")])

    if via_command:
        await update.message.reply_text(text, parse_mode="HTML",
                                        reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.callback_query.edit_message_text(text, parse_mode="HTML",
                                                      reply_markup=InlineKeyboardMarkup(buttons))


# ─── Оплата ───────────────────────────────────────────────────────────────────

async def _request_email(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_id: int):
    context.user_data["pending_plan_id"] = plan_id
    context.user_data["user_state"] = "waiting_email"
    email = await db.get_user_email(update.effective_user.id)
    if email:
        context.user_data["known_email"] = email
        plan = await db.get_plan(plan_id)
        await update.callback_query.edit_message_text(
            f"📧 <b>Email для чека (54-ФЗ)</b>\n\n"
            f"Тариф: <b>{plan['name']}</b> — {plan['price_rub']}₽\n\n"
            f"Сохранённый email: <code>{email}</code>\n\n"
            f"Использовать этот адрес?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Использовать", callback_data=f"confirm_email_{plan_id}")],
                [InlineKeyboardButton("✏️ Ввести другой", callback_data=f"change_email_{plan_id}")],
                [InlineKeyboardButton("◀️ Назад", callback_data="buy_sub")],
            ])
        )
    else:
        await update.callback_query.edit_message_text(
            "📧 <b>Введите email для чека</b>\n\n"
            "По закону 54-ФЗ при онлайн-оплате необходимо отправить электронный чек.\n"
            "Введите email — чек придёт на этот адрес (сохраним для следующих покупок):",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Отмена", callback_data="buy_sub")]]
            )
        )


async def _do_rub_payment(context, user_id: int, plan_id: int,
                          email: str, chat_id: int, message_id: int = None):
    url = await pay.create_yookassa_payment(user_id, plan_id, email)
    plan = await db.get_plan(plan_id)
    if not url:
        text = "❌ Оплата через ЮКассу временно недоступна.\nОбратитесь к администратору."
        buttons = []
        if plan and plan.get("price_stars"):
            buttons.append([InlineKeyboardButton("⭐ Оплатить звёздами",
                                                 callback_data=f"pay_stars_{plan_id}")])
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="buy_sub")])
        try:
            await context.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                                reply_markup=InlineKeyboardMarkup(buttons))
        except Exception:
            await context.bot.send_message(chat_id, text,
                                           reply_markup=InlineKeyboardMarkup(buttons))
        return

    text = (
        f"💳 <b>Оплата через ЮКассу</b>\n\n"
        f"Тариф: <b>{plan['name']}</b>\n"
        f"Сумма: <b>{plan['price_rub']}₽</b>\n"
        f"Чек → <code>{email}</code>\n\n"
        f"Нажмите «Перейти к оплате», оплатите и вернитесь сюда."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Перейти к оплате", url=url)],
        [InlineKeyboardButton("✅ Проверить оплату", callback_data=f"check_pay_{plan_id}")],
        [InlineKeyboardButton("◀️ Назад", callback_data="buy_sub")],
    ])
    try:
        await context.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                            parse_mode="HTML", reply_markup=kb)
    except Exception:
        await context.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)


# ─── Обработчик кнопок ────────────────────────────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data.startswith("admin_"):
        await adm.admin_callback(update, context)
        return

    await query.answer()

    if data == "main_menu":
        await query.delete_message()
        await start_command(update, context)

    elif data == "send_username":
        bot_info = await context.bot.get_me()
        await context.bot.send_message(
            update.effective_user.id,
            f"<code>@{bot_info.username}</code>\n\n"
            f"Нажмите и удерживайте, чтобы скопировать 👆",
            parse_mode="HTML"
        )

    elif data == "instructions":
        bot_info = await context.bot.get_me()
        await query.edit_message_text(
            "📖 <b>Инструкция по подключению</b>\n\n"
            "1. Откройте <b>Настройки Telegram</b>\n"
            "2. Перейдите в <b>Telegram Business</b>\n"
            "3. Выберите <b>Чат-боты</b>\n"
            f"4. Введите <code>@{bot_info.username}</code>\n"
            "5. Нажмите <b>Подключить</b> и выберите чаты\n\n"
            "✅ Готово! Бот сразу начнёт перехватывать сообщения.\n\n"
            "⚡ <i>Подключение занимает ~30 секунд</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Назад", callback_data="main_menu")]]
            )
        )

    elif data == "buy_sub":
        await show_plans(update, context)

    elif data == "sub_info":
        pass

    elif data.startswith("pay_rub_"):
        plan_id = int(data.split("_")[-1])
        await _request_email(update, context, plan_id)

    elif data.startswith("confirm_email_"):
        plan_id = int(data.split("_")[-1])
        email = context.user_data.get("known_email", "")
        if not email:
            await _request_email(update, context, plan_id)
            return
        context.user_data.pop("user_state", None)
        await _do_rub_payment(
            context, query.from_user.id, plan_id, email,
            query.message.chat_id, query.message.message_id
        )

    elif data.startswith("change_email_"):
        plan_id = int(data.split("_")[-1])
        context.user_data["pending_plan_id"] = plan_id
        context.user_data["user_state"] = "waiting_email"
        context.user_data.pop("known_email", None)
        await query.edit_message_text(
            "📧 Введите новый email для чека:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Отмена", callback_data="buy_sub")]]
            )
        )

    elif data.startswith("pay_stars_"):
        plan_id = int(data.split("_")[-1])
        await query.delete_message()
        await pay.send_stars_invoice(update, context, plan_id)

    elif data.startswith("check_pay_"):
        plan_id = int(data.split("_")[-1])
        user_id = query.from_user.id
        row = await db.get_last_pending_payment(user_id, plan_id)
        if not row:
            await query.answer("❌ Платёж не найден.", show_alert=True)
            return
        confirmed = await pay.check_yookassa_payment(row["payment_id"])
        if confirmed:
            await db.complete_payment(row["payment_id"])
            await db.grant_subscription(user_id, plan_id, "ЮКасса")
            plan = await db.get_plan(plan_id)
            sub = await db.get_user_subscription(user_id)
            expires = datetime.fromisoformat(sub["expires_at"])
            await query.edit_message_text(
                f"✅ <b>Подписка активирована!</b>\n\n"
                f"Тариф: <b>{plan['name']}</b>\n"
                f"Действует до: <b>{expires.strftime('%d.%m.%Y')}</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]]
                )
            )
        else:
            await query.answer(
                "⏳ Оплата ещё не поступила. Подождите и попробуйте снова.",
                show_alert=True
            )


# ─── Оплата через Stars ───────────────────────────────────────────────────────

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    payload = update.message.successful_payment.invoice_payload
    parts = payload.split("_")
    if len(parts) >= 3 and parts[0] == "sub":
        plan_id = int(parts[1])
        await db.grant_subscription(user.id, plan_id, "Telegram Stars")
        plan = await db.get_plan(plan_id)
        sub = await db.get_user_subscription(user.id)
        expires = datetime.fromisoformat(sub["expires_at"])
        await update.message.reply_text(
            f"✅ <b>Подписка активирована!</b>\n\n"
            f"Тариф: <b>{plan['name']}</b>\n"
            f"Действует до: <b>{expires.strftime('%d.%m.%Y')}</b>\n\n"
            f"Спасибо за покупку! ⭐",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD
        )


# ─── Обработчик обычных личных сообщений ─────────────────────────────────────

async def general_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat = update.effective_chat
    if not chat or chat.type != "private":
        return

    # Мульти-шаговые формы администратора
    if await adm.handle_admin_input(update, context):
        return

    text = (update.message.text or "").strip()
    state = context.user_data.get("user_state")

    # Email для ЮКассы
    if state == "waiting_email":
        email = text
        if not pay.is_valid_email(email):
            await update.message.reply_text(
                "❌ Некорректный email. Введите адрес в формате <b>name@domain.ru</b>:",
                parse_mode="HTML"
            )
            return
        plan_id = context.user_data.get("pending_plan_id")
        if not plan_id:
            context.user_data.pop("user_state", None)
            return
        await db.save_user_email(update.effective_user.id, email)
        context.user_data.pop("user_state", None)
        context.user_data.pop("pending_plan_id", None)
        tmp = await update.message.reply_text("⏳ Создаю ссылку на оплату...")
        await _do_rub_payment(
            context, update.effective_user.id, plan_id, email,
            update.effective_chat.id, tmp.message_id
        )
        return

    # Reply-клавиатура
    if text == "📊 Мой статус":
        await status_command(update, context)
    elif text == "💳 Купить подписку":
        await subscribe_command(update, context)
    elif text == "📖 Инструкция":
        bot_info = await context.bot.get_me()
        await update.message.reply_text(
            "📖 <b>Инструкция по подключению</b>\n\n"
            "1. Откройте <b>Настройки Telegram</b>\n"
            "2. Перейдите в <b>Telegram Business</b>\n"
            "3. Выберите <b>Чат-боты</b>\n"
            f"4. Введите <code>@{bot_info.username}</code>\n"
            "5. Нажмите <b>Подключить</b> и выберите нужные чаты\n\n"
            "✅ После подключения бот сразу начнёт работать.",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD
        )
    elif text == "ℹ️ Помощь":
        await help_command(update, context)


# ─── Debug: логирует ВСЕ входящие апдейты (для диагностики) ──────────────────

async def _debug_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fields = []
    if update.message:                   fields.append("message")
    if update.edited_message:            fields.append("edited_message")
    if update.business_message:          fields.append("business_message")
    if update.edited_business_message:   fields.append("edited_business_message")
    if update.deleted_business_messages: fields.append("deleted_business_messages")
    if update.business_connection:       fields.append("business_connection")
    if update.callback_query:            fields.append("callback_query")
    if update.pre_checkout_query:        fields.append("pre_checkout_query")
    if not fields:                       fields.append("UNKNOWN")
    logger.info(f"[DBG] update_id={update.update_id} types={fields}")

    # Полный дамп бизнес-сообщения для диагностики одноразовых медиа
    msg_obj = (update.business_message or update.edited_business_message)
    if msg_obj:
        try:
            raw = msg_obj.to_dict()
            # Убираем шумные поля, оставляем только важные
            keep = {k: v for k, v in raw.items() if k not in ("from", "chat", "entities")}
            logger.info(f"[DBG MSG DICT] id={msg_obj.message_id} raw={keep}")
        except Exception as e:
            logger.warning(f"[DBG] to_dict failed: {e}")

    # Дамп deleted_business_messages
    if update.deleted_business_messages:
        d = update.deleted_business_messages
        logger.info(
            f"[DBG DELETED] conn={d.business_connection_id} "
            f"chat={d.chat.id} msg_ids={d.message_ids}"
        )


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"[ERROR] update={update!r}", exc_info=context.error)


# ─── Сборка приложения ────────────────────────────────────────────────────────

def build_application():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise ValueError("BOT_TOKEN не задан!")

    app = ApplicationBuilder().token(token).build()

    # Глобальный обработчик ошибок
    app.add_error_handler(_error_handler)

    # Debug: логирует КАЖДЫЙ апдейт (группа -1 = до всего остального)
    app.add_handler(TypeHandler(Update, _debug_all_updates), group=-1)

    # Команды
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("subscribe", subscribe_command))
    app.add_handler(CommandHandler("admin", adm.admin_command))

    # Оплата
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    # ── Бизнес-апдейты: используем TypeHandler для ВСЕХ типов ──────────────
    # TypeHandler надёжнее MessageHandler для не-Message апдейтов.
    # Группа 0 — наивысший приоритет, порядок важен.

    app.add_handler(_BusinessConnectionHandler(on_business_connection), group=0)
    app.add_handler(_BusinessMessageHandler(on_business_message), group=0)
    app.add_handler(_EditedBusinessMessageHandler(on_edited_business_message), group=0)
    app.add_handler(_DeletedBusinessMessagesHandler(on_deleted_business_messages), group=0)

    # ── Обычные личные сообщения (группа 1) ────────────────────────────────
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            general_message_handler
        ),
        group=1
    )

    return app

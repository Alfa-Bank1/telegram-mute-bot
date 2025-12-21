import os
import logging
import json
import re
import time
import random
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters
)
from groq import Groq

# === КОНФИГУРАЦИЯ ===
ADMIN_USER_IDS = [int(x.strip()) for x in os.getenv("ADMIN_USER_ID", "").split(",") if x.strip()]
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Файлы данных (в /tmp — Render позволяет писать туда)
USERS_FILE = "/tmp/users_cache.json"
MUTED_FILE = "/tmp/invisible_mutes.json"
LAST_ADMIN_MSG_FILE = "/tmp/last_admin_message.json"  # НОВЫЙ ФАЙЛ

# Запрещённые темы (семья, религия, национальность)
FORBIDDEN_TOPICS = [
    "мам", "пап", "родител", "семь", "жена", "муж", "ребён", "ребен", "сын", "дочь",
    "бог", "аллах", "исус", "христ", "религ", "мечеть", "церков", "молитв", "вера", "атеизм",
    "наци", "рас", "этнос", "рус", "украин", "белорус", "казан", "татар", "евре", "немец",
    "американ", "китаец", "япон", "черн", "бел", "мусульман", "христиан", "будд", "инду",
    "родин", "патриот", "граждан", "россия", "украина", "сша", "кита", "германи", "франци", "создатель"
]

# Разрешённые пользователи для агрессивных ответов
ALLOWED_USER_IDS = {8462839381, 6370704218, 7038529593, 527497822, 8180038585, 8349016341, 5372063362, 6194116904, 1645451702}

# Хранилище активных отложенных задач (по чату и пользователю)
pending_replies = {}  # {(chat_id, user_id): {"task": task, "message_id": id}}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- ФАЙЛОВЫЕ УТИЛИТЫ ---
def load_data(filename, default):
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки {filename}: {e}")
    return default

def save_data(filename, data):
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения {filename}: {e}")

def load_users():
    return load_data(USERS_FILE, {})

def save_users(data):
    save_data(USERS_FILE, data)

def load_muted_users():
    raw = load_data(MUTED_FILE, {})
    try:
        return {(int(k.split(':')[0]), int(k.split(':')[1])): v for k, v in raw.items()}
    except Exception as e:
        logger.error(f"Ошибка парсинга muted_users: {e}")
        return {}

def save_muted_users(muted_dict):
    serializable = {f"{chat}:{user}": expiry for (chat, user), expiry in muted_dict.items()}
    save_data(MUTED_FILE, serializable)

def load_last_admin_msg():
    return load_data(LAST_ADMIN_MSG_FILE, {})

def save_last_admin_msg(data):
    save_data(LAST_ADMIN_MSG_FILE, data)

# --- ПРОВЕРКА НА ЗАПРЕЩЁННЫЕ ТЕМЫ ---
def contains_forbidden_topic(text: str) -> bool:
    text_low = text.lower()
    return any(word in text_low for word in FORBIDDEN_TOPICS)

# --- ОТЛАДКА: /clear ---
async def debug_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return
    files_to_remove = [USERS_FILE, MUTED_FILE, LAST_ADMIN_MSG_FILE]
    removed = []
    for f in files_to_remove:
        if os.path.exists(f):
            try:
                os.remove(f)
                removed.append(f)
            except Exception as e:
                logger.error(f"Не удалось удалить {f}: {e}")
    msg = "🧹 Удалены файлы кэша." if removed else "✅ Нет файлов для удаления."
    await update.message.reply_text(msg)

# --- ПОЛУЧЕНИЕ СПИСКА ГРУПП ---
async def get_bot_groups(context: ContextTypes.DEFAULT_TYPE):
    groups = []
    cache = load_users()
    for chat_id_str in list(cache.keys()):
        try:
            chat_id = int(chat_id_str)
            chat = await context.bot.get_chat(chat_id)
            if chat.type in ("group", "supergroup"):
                title = chat.title or f"Группа {chat_id}"
                groups.append((chat_id, title))
        except Exception as e:
            logger.warning(f"Чат {chat_id_str} недоступен: {e}")
            cache.pop(chat_id_str, None)
            save_users(cache)
    return groups

# --- СБРОС СОСТОЯНИЯ ---
def clear_state(context: ContextTypes.DEFAULT_TYPE):
    keys = ["mode", "target_chat_id", "target_chat_title", "mute_user_id", "mute_user_name"]
    for k in keys:
        context.user_data.pop(k, None)

# --- КНОПКИ ---
def back_button():
    return InlineKeyboardButton("← Назад", callback_data="back")

# --- /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return
    clear_state(context)
    await update.message.reply_text(
        "🛡️ Панель администратора",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Выбрать группу", callback_data="select_group")]])
    )

# --- ЛАЙК НА ПОСЛЕДНЕЕ СООБЩЕНИЕ АДМИНА (ИСПРАВЛЕНО) ---
async def like_my_last_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = context.user_data.get("target_chat_id")
    user_id = update.effective_user.id

    if not chat_id:
        await query.edit_message_text("❌ Группа не выбрана.")
        return

    last_admin = load_last_admin_msg()
    chat_id_str = str(chat_id)
    user_id_str = str(user_id)

    if chat_id_str not in last_admin or user_id_str not in last_admin[chat_id_str]:
        await query.edit_message_text("📭 Ваше последнее сообщение в этой группе не найдено.")
        return

    target_message_id = last_admin[chat_id_str][user_id_str]["message_id"]

    try:
        await context.bot.set_message_reaction(
            chat_id=chat_id,
            message_id=target_message_id,
            reaction=["👍"],
            is_big=False
        )
        await query.edit_message_text("✅ Лайк 👍 поставлен на ваше последнее сообщение!")
    except Exception as e:
        error = str(e)
        if "not a member" in error:
            text = "❌ Бот не в группе."
        elif "message not found" in error:
            text = "❌ Сообщение удалено или слишком старое."
        elif "can't set reaction" in error:
            text = "❌ У бота нет прав на реакции в этой группе."
        else:
            text = f"❌ Ошибка: {error[:100]}"
        await query.edit_message_text(text)

# --- ОБРАБОТЧИК КНОПОК ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("unmute:"):
        try:
            _, chat_id_str, user_id_str = data.split(":")
            chat_id = int(chat_id_str)
            user_id = int(user_id_str)
        except:
            await query.edit_message_text("❌ Неверные данные.")
            return

        muted = load_muted_users()
        key = (chat_id, user_id)
        if key in muted:
            del muted[key]
            save_muted_users(muted)
            await query.edit_message_text("🔓 Мут снят!")
        else:
            await query.edit_message_text("ℹ️ Пользователь не в муте.")
        return

    if data == "like_my_last":
        await like_my_last_message(update, context)
        return

    if data == "select_group":
        groups = await get_bot_groups(context)
        if not groups:
            await query.edit_message_text("📭 Бот не состоит ни в одной группе.")
            return
        keyboard = [
            [InlineKeyboardButton(title, callback_data=f"group:{chat_id}")]
            for chat_id, title in groups
        ]
        await query.edit_message_text(
            "👥 Выберите группу:",
            reply_markup=InlineKeyboardMarkup(keyboard + [[back_button()]])
        )

    elif data.startswith("group:"):
        chat_id = int(data.split(":", 1)[1])
        try:
            chat = await context.bot.get_chat(chat_id)
            title = chat.title or str(chat_id)
        except:
            await query.edit_message_text("❌ Группа недоступна.")
            return
        context.user_data["target_chat_id"] = chat_id
        context.user_data["target_chat_title"] = title
        context.user_data["mode"] = None
        keyboard = [
            [InlineKeyboardButton("Написать сообщение от бота", callback_data="mode:send")],
            [InlineKeyboardButton("Невидимый мут пользователя", callback_data="mode:mutelist")],
            [InlineKeyboardButton("Лайк на моё сообщение", callback_data="like_my_last")],
            [back_button()]
        ]
        await query.edit_message_text(
            f"✅ Выбрана группа: {title}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "back":
        clear_state(context)
        await query.edit_message_text(
            "🛡️ Панель администратора",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Выбрать группу", callback_data="select_group")]])
        )

    elif data == "mode:send":
        if "target_chat_id" not in context.user_data:
            await query.edit_message_text("❌ Группа не выбрана.")
            return
        context.user_data["mode"] = "send_message"
        await query.edit_message_text(
            "✏️ Режим: отправка сообщений от бота.\nВсё, что вы напишете — уйдёт в группу.",
            reply_markup=InlineKeyboardMarkup([[back_button()]])
        )

    elif data == "mode:mutelist":
        chat_id = context.user_data.get("target_chat_id")
        if not chat_id:
            await query.edit_message_text("❌ Группа не выбрана.")
            return
        cache = load_users()
        chat_id_str = str(chat_id)
        users = cache.get(chat_id_str, {})
        if not users:
            await query.edit_message_text("📭 В группе никто не писал.")
            return
        keyboard = []
        for user_id_str, user in users.items():
            full_name = (user["first_name"] + " " + user["last_name"]).strip()
            display_name = full_name if full_name else (f"@{user['username']}" if user['username'] else f"ID{user['id']}")
            keyboard.append([InlineKeyboardButton(display_name, callback_data=f"muteuser:{user_id_str}")])
        keyboard.append([back_button()])
        await query.edit_message_text("👥 Выберите пользователя для мута:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("muteuser:"):
        user_id_str = data.split(":", 1)[1]
        chat_id = context.user_data.get("target_chat_id")
        if not chat_id:
            await query.edit_message_text("❌ Группа не выбрана.")
            return
        cache = load_users()
        chat_id_str = str(chat_id)
        user = cache[chat_id_str][user_id_str]
        user_id = int(user_id_str)
        bot = await context.bot.get_me()
        if user_id == update.effective_user.id or user_id == bot.id:
            await query.edit_message_text("❌ Нельзя замутить себя или бота.")
            return
        full_name = (user["first_name"] + " " + user["last_name"]).strip()
        name = full_name if full_name else (f"@{user['username']}" if user['username'] else f"ID{user['id']}")
        context.user_data["mute_user_id"] = user_id
        context.user_data["mute_user_name"] = name
        durations = [
            ("1 мин", 60),
            ("5 мин", 300),
            ("10 мин", 600),
            ("1 ч", 3600),
            ("3 ч", 10800),
            ("12 ч", 43200),
            ("24 ч", 86400),
            ("Год", 31536000),
        ]
        keyboard = [
            [InlineKeyboardButton(f"Мут на {label}", callback_data=f"mutetime:{sec}")]
            for label, sec in durations
        ]
        keyboard.append([back_button()])
        await query.edit_message_text(
            f"⏳ Пользователь: {name}\nВыберите длительность:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("mutetime:"):
        seconds = int(data.split(":", 1)[1])
        chat_id = context.user_data.get("target_chat_id")
        user_id = context.user_data.get("mute_user_id")
        name = context.user_data.get("mute_user_name")
        if not all([chat_id, user_id, name]):
            await query.edit_message_text("❌ Данные устарели.")
            return

        expiry = time.time() + seconds
        muted = load_muted_users()
        muted[(chat_id, user_id)] = expiry
        save_muted_users(muted)

        async def auto_unmute():
            await asyncio.sleep(seconds)
            current = load_muted_users()
            key = (chat_id, user_id)
            if key in current and time.time() >= current[key] - 2:
                del current[key]
                save_muted_users(current)
                logger.info(f"Авто-размут: {user_id} в {chat_id}")

        asyncio.create_task(auto_unmute())
        if seconds == 31536000:
            dur_text = "Год"
        elif seconds >= 3600:
            dur_text = f"{seconds // 3600} ч"
        elif seconds >= 60:
            dur_text = f"{seconds // 60} мин"
        else:
            dur_text = f"{seconds} сек"

        keyboard = [[InlineKeyboardButton("Убрать мут", callback_data=f"unmute:{chat_id}:{user_id}")]]
        await query.edit_message_text(
            f"✅ {name} *невидимо замучен* на {dur_text}!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# --- ОБРАБОТЧИК ЛИЧНЫХ СООБЩЕНИЙ ОТ АДМИНА (НЕ ПЕРЕСЛАННЫХ) ---
async def admin_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return

    if context.user_data.get("mode") != "send_message":
        return

    chat_id = context.user_data.get("target_chat_id")
    if not chat_id:
        await update.message.reply_text("❌ Целевая группа не выбрана. Начните с /start.")
        return

    msg = update.effective_message
    try:
        if msg.text:
            await context.bot.send_message(chat_id=chat_id, text=msg.text)
        elif msg.voice:
            await context.bot.send_voice(chat_id=chat_id, voice=msg.voice.file_id)
        elif msg.photo:
            await context.bot.send_photo(chat_id=chat_id, photo=msg.photo[-1].file_id)
        elif msg.video:
            await context.bot.send_video(chat_id=chat_id, video=msg.video.file_id)
        elif msg.document:
            await context.bot.send_document(chat_id=chat_id, document=msg.document.file_id)
        elif msg.audio:
            await context.bot.send_audio(chat_id=chat_id, audio=msg.audio.file_id)
        elif msg.sticker:
            await context.bot.send_sticker(chat_id=chat_id, sticker=msg.sticker.file_id)
        else:
            await update.message.reply_text("⚠️ Тип сообщения не поддерживается.")
            return

    except Exception as e:
        logger.error(f"Ошибка отправки в группу {chat_id}: {repr(e)}")
        err = str(e)
        if "migrated" in err and "new chat id" in err:
            new_id_match = re.search(r"New chat id: (-\d+)", err)
            new_id = new_id_match.group(1) if new_id_match else "неизвестен"
            text = f"❌ Группа мигрировала. Новый ID: {new_id}. Обновите выбор группы."
        elif "bot is not a member" in err or "chat not found" in err:
            text = "❌ Бот не состоит в группе или группа недоступна."
        elif "can't send messages" in err:
            text = "❌ У бота нет прав на отправку сообщений в группе."
        elif "bot was blocked" in err:
            text = "❌ Бот заблокирован в группе."
        else:
            text = f"❌ Ошибка: {err[:100]}"
        await update.message.reply_text(text)

# --- РЕАКЦИИ НА ПЕРЕСЛАННЫЕ СООБЩЕНИЯ (ОСТАВЛЕНО ДЛЯ СОВМЕСТИМОСТИ) ---
async def handle_forwarded_to_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return

    msg = update.effective_message
    if not msg or not msg.forward_from_chat:
        return

    original_chat = msg.forward_from_chat
    original_message_id = msg.forward_from_message_id

    if not original_chat or not original_message_id:
        await msg.reply_text("❌ Не удалось определить исходное сообщение.")
        return

    reaction = "👍"

    try:
        await context.bot.set_message_reaction(
            chat_id=original_chat.id,
            message_id=original_message_id,
            reaction=[reaction],
            is_big=False
        )
        await msg.reply_text(f"✅ Лайк `{reaction}` поставлен на сообщение в чате {original_chat.title or original_chat.id}.")
    except Exception as e:
        error_text = str(e)
        if "bot was blocked" in error_text:
            feedback = "❌ Бот заблокирован в чате."
        elif "not a member" in error_text or "chat not found" in error_text:
            feedback = "❌ Бот не состоит в чате."
        elif "message to react not found" in error_text:
            feedback = "❌ Сообщение удалено или недоступно."
        elif "can't set reaction" in error_text:
            feedback = "❌ У бота нет прав на реакции в этом чате."
        else:
            feedback = f"❌ Ошибка: {error_text[:150]}"
        await msg.reply_text(feedback)

# --- ФУНКЦИЯ БЕЗОПАСНОЙ ГЕНЕРАЦИИ ---
async def safe_generate_aggressive_reply(text: str) -> str | None:
    while True:
        reply = await generate_aggressive_reply(text)
        if reply is None:
            return None
        if not contains_forbidden_topic(reply):
            return reply
        for _ in range(2):
            reply = await generate_aggressive_reply(text)
            if reply and not contains_forbidden_topic(reply):
                return reply
        return random.choice([
            "Я твой рот шатал чепух🤣",
            "Продолжаешь базарить, значит 🫵петушок!",
            "Бл ты🫵 такой попуск🤣",
            "Это воздух ОЧКОШНИКА"
        ])

# --- ОБРАБОТЧИК ГРУПП ---
async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message

    if msg.migrate_to_chat_id:
        old_id = str(msg.chat.id)
        new_id = str(msg.migrate_to_chat_id)
        cache = load_users()
        if old_id in cache:
            cache[new_id] = cache.pop(old_id)
            save_users(cache)
            logger.info(f"Группа мигрировала: {old_id} → {new_id}")
        return

    if chat.type not in ("group", "supergroup") or user.is_bot or user.id == context.bot.id:
        return

    cache = load_users()
    chat_id_str = str(chat.id)
    if chat_id_str not in cache:
        cache[chat_id_str] = {}
    cache[chat_id_str][str(user.id)] = {
        "id": user.id,
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "username": user.username or "",
    }
    save_users(cache)

    # === Сохраняем последнее сообщение админа в группе ===
    if user.id in ADMIN_USER_IDS and (msg.text or msg.caption or msg.photo or msg.video or msg.document):
        last_admin = load_last_admin_msg()
        chat_id_str = str(chat.id)
        if chat_id_str not in last_admin:
            last_admin[chat_id_str] = {}
        last_admin[chat_id_str][str(user.id)] = {
            "message_id": msg.message_id,
            "timestamp": time.time()
        }
        save_last_admin_msg(last_admin)

    muted = load_muted_users()
    key = (chat.id, user.id)
    is_muted = key in muted and time.time() < muted[key]

    if is_muted:
        try:
            await msg.delete()
        except:
            pass

        # === Пользователь в муте: ответ через 10 сек, БЕЗ отметки ===
        async def delayed_reply_muted():
            await asyncio.sleep(10)
            if user.id in ALLOWED_USER_IDS:
                replies = [
                    "🫵Геи",
                    "Жопу закрой щенк😂",
                    "Кто эту шерсть сюда пустил?",
                    "Часотка🫵",
                    "Поддерживаешь геев, значит пидр🫵"
                ]
                reply_text = random.choice(replies)
            else:
                name = (user.first_name or user.username or f"ID{user.id}")
                fake_text = f"{name} пишет в муте"
                reply_text = await safe_generate_aggressive_reply(fake_text)
                if not reply_text:
                    reply_text = random.choice([
                        "Шкура, сиди в муте! 🫵",
                        "Петушок, мут не кончился! 🤣",
                        "Чмо, сиди тихо! 🤫",
                        "Гей, ты в муте! Не пизди! 🫵",
                        "Не доказал! Значит 🫵петушок!",
                    ])

            try:
                await context.bot.send_message(chat_id=chat.id, text=reply_text)
            except:
                pass

            if time.time() >= muted[key]:
                del muted[key]
                save_muted_users(muted)

        # Отмена предыдущей задачи (если есть)
        task_key = (chat.id, user.id)
        if task_key in pending_replies:
            pending_replies[task_key]["task"].cancel()
        pending_replies[task_key] = {"task": asyncio.create_task(delayed_reply_muted()), "message_id": msg.message_id}
        return

    # === Обычный пользователь ИЗ СПИСКА ALLOWED_USER_IDS ===
    if user.id in ALLOWED_USER_IDS:
        text = (msg.text or msg.caption or "").strip()
        if not text or contains_forbidden_topic(text):
            return

        # Отмена предыдущей задачи (если есть)
        task_key = (chat.id, user.id)
        if task_key in pending_replies:
            pending_replies[task_key]["task"].cancel()

        async def delayed_reply_normal():
            await asyncio.sleep(10)
            # Убедимся, что задача не была отменена
            if task_key in pending_replies and pending_replies[task_key]["task"].done():
                return
            reply_text = await safe_generate_aggressive_reply(text)
            if reply_text:
                target_msg_id = pending_replies.get(task_key, {}).get("message_id", msg.message_id)
                try:
                    await context.bot.send_message(
                        chat_id=chat.id,
                        text=reply_text,
                        reply_to_message_id=target_msg_id
                    )
                except:
                    pass
            pending_replies.pop(task_key, None)

        new_task = asyncio.create_task(delayed_reply_normal())
        pending_replies[task_key] = {"task": new_task, "message_id": msg.message_id}

# --- ГЕНЕРАЦИЯ АГРЕССИВНОГО ОТВЕТА ЧЕРЕЗ GROQ ---
async def generate_aggressive_reply(text: str) -> str | None:
    prompt = (
        f"Пользователь написал: {text}\n"
        "Ты — агрессивный тролль в чате. Ответь ОДНОЙ законченной фразой без уважения, грубо.\n"
        "Используй резкие слова: шкура, петушок, пидор, шерсть, гей, часотка, попуск, пидрила, дебил, мудила.\n"
        "Можно эмодзи: 🤣, 🫵\n"
        "Фраза должна быть полной, без обрезанных слов, без многоточий, без обрывков.\n"
        "Не упоминай семью, религию или национальность.\n"
        "Ответ:"
    )

    if not GROQ_API_KEY:
        return random.choice([
            "Эта шмара Поддерживает геев🤣",
            "Поддержал за яйца геев, значит 🫵петушок!",
            "Очкошник ты че забыл тут?",
            "Тебя по кругу уже давно пустили тут, запись гч есть же, дятел! Ты сказал, что раком встал да + на бутылке прыгал! Ле какой ты хитровыебаный🤣"
        ])

    try:
        client = Groq(api_key=GROQ_API_KEY)

        loop = asyncio.get_event_loop()
        chat_completion = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.1-8b-instant",
                temperature=1.4,
                max_tokens=50,
                top_p=0.93
            )
        )
        reply = chat_completion.choices[0].message.content.strip()

        if not reply:
            return None

        # Удаляем только потенциально вредоносные символы, но не обрезаем слова
        reply = re.sub(r'[^\w\sа-яА-ЯёЁ.,!?—–\-\"\'\(\)\[\]{}:;…🤣🫵]', '', reply)
        reply = re.sub(r'\s+', ' ', reply).strip()

        # Проверка на наличие «агрессивных» слов
        lower = reply.lower()
        if not any(w in lower for w in ["шкура", "петушок", "пидор", "часотка", "гей", "шерсть", "попуск", "пидрила", "мудила", "дебил"]):
            return None

        return reply

    except Exception as e:
        logger.error(f"Groq error: {e}")
        return random.choice([
            "Очко закрой пес!",
            "Не доказал! Значит 🫵петушок!",
            "🫵 шалава местная",
            "Ты че, на ментовской помойке вырос, шерсть?",
            "Ле какой ты заднеприводный пидрила поганый🤣"
        ])

# === ЗАПУСК (WEBHOOK) ===
def main():
    if not BOT_TOKEN:
        raise RuntimeError("❌ BOT_TOKEN не задан в переменных окружения!")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", debug_clear))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Обработка обычных сообщений админа (не пересланных)
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.User(user_id=ADMIN_USER_IDS) & ~filters.FORWARDED,
            admin_private_message
        ),
        group=1
    )

    # Обработка пересланных сообщений (оставлено на случай использования)
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.User(user_id=ADMIN_USER_IDS) & filters.FORWARDED,
            handle_forwarded_to_bot
        ),
        group=2
    )

    # Обработка сообщений в группах
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, handle_group_message),
        group=0
    )

    RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
    if not RENDER_EXTERNAL_URL:
        raise RuntimeError("❌ RENDER_EXTERNAL_URL не задан!")

    webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/{BOT_TOKEN}"

    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        url_path=BOT_TOKEN,
        webhook_url=webhook_url
    )

if __name__ == "__main__":
    main()
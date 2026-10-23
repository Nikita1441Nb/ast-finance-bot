import os
import re
import json
import base64
import asyncio
from datetime import datetime

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

from config import (
    TELEGRAM_BOT_TOKEN,
    SUPER_ADMINS,
    USERS_FILE,
    DATA_DIR,
    logger,
    get_current_time,
)
from database import (
    init_db,
    get_db,
    get_user_state,
    set_user_state,
    clear_user_state,
    get_user_mode,
    set_user_mode,
    clear_user_history,
    add_user_rule,
    get_user_rules,
    clear_user_rules,
    cancel_waiting_edits,
    create_draft,
    get_draft,
    get_waiting_edit_draft,
    update_draft,
    delete_draft,
)
from parsers import (
    decode_text_bytes,
    parse_excel_xlsx,
    parse_excel_xls,
    parse_pdf,
    generate_excel_bytes,
)
from ai_services import (
    parse_reminder_time_via_claude,
    extract_document_via_claude,
    ask_financial_analyst,
)

def get_user_info(username: str):
    uname_clean = (username or "").lstrip("@").strip().lower()
    if not USERS_FILE.exists():
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            f.write("Администратор : @admin : Админ, Руководитель\n")

    if uname_clean in SUPER_ADMINS:
        return {"name": "Администратор", "username": uname_clean, "desc": "Главный админ", "is_admin": True}

    with open(USERS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            parts = [p.strip() for p in line.split(":", 2)]
            if len(parts) >= 2:
                name = parts[0]
                file_uname = parts[1].lstrip("@").strip().lower()
                desc = parts[2] if len(parts) > 2 else "Сотрудник"
                if file_uname == uname_clean:
                    is_admin = ("админ" in desc.lower()) or (uname_clean in SUPER_ADMINS)
                    return {"name": name, "username": uname_clean, "desc": desc, "is_admin": is_admin}
    return None

def find_chat_id_by_target(target_str: str):
    target_clean = (target_str or "").lstrip("@").strip().lower()
    mapped_username = None

    if USERS_FILE.exists():
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                parts = [p.strip() for p in line.split(":", 2)]
                if len(parts) >= 2:
                    name = parts[0].strip().lower()
                    uname = parts[1].lstrip("@").strip().lower()
                    if target_clean in (name, uname):
                        mapped_username = uname
                        break

    search_targets = [target_clean]
    if mapped_username:
        search_targets.append(mapped_username)

    with get_db() as conn:
        for t in search_targets:
            row = conn.execute(
                "SELECT user_id FROM telegram_users WHERE LOWER(username) = ? OR LOWER(first_name) = ? LIMIT 1",
                (t, t),
            ).fetchone()
            if row:
                return row["user_id"]
    return None

def get_admin_keyboard():
    kb = [
        [KeyboardButton("📊 Финансовая сводка"), KeyboardButton("📥 Выгрузить Excel")],
        [KeyboardButton("📋 Последние записи"), KeyboardButton("📩 Создать Сообщение")],
        [KeyboardButton("⚙️ Управление персоналом"), KeyboardButton("🔄 Сбросить контекст")],
        [KeyboardButton("👷 Режим Сотрудника")],
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True, is_persistent=True)

def get_worker_keyboard(is_admin: bool = False):
    kb = [[KeyboardButton("📩 Создать Сообщение")]]
    if is_admin:
        kb.append([KeyboardButton("👑 Режим Администратора")])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True, is_persistent=True)

def get_draft_keyboard(draft_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Сохранить в базу", callback_data=f"confirm_ok:{draft_id}"),
            InlineKeyboardButton("✏️ Исправить", callback_data=f"confirm_edit:{draft_id}"),
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"confirm_cancel:{draft_id}")],
    ])

async def safe_reply(message, text: str, reply_markup=None):
    try:
        return await message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    except BadRequest:
        return await message.reply_text(text, reply_markup=reply_markup)

async def safe_edit(query, text: str, reply_markup=None):
    try:
        return await query.edit_message_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    except BadRequest:
        return await query.edit_message_text(text, reply_markup=reply_markup)

async def send_long_message(message, text: str, max_len: int = 4000):
    if len(text) <= max_len:
        await safe_reply(message, text)
        return

    chunks = []
    current_chunk = ""
    for line in text.split("\n"):
        if len(current_chunk) + len(line) + 1 > max_len:
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = line
            else:
                chunks.append(line[:max_len])
                current_chunk = line[max_len:]
        else:
            current_chunk = f"{current_chunk}\n{line}" if current_chunk else line

    if current_chunk:
        chunks.append(current_chunk)

    for chunk in chunks:
        await safe_reply(message, chunk)

async def reminder_scheduler(app: Application):
    while True:
        try:
            now_str = get_current_time().strftime("%Y-%m-%d %H:%M")
            with get_db() as conn:
                due_records = conn.execute(
                    "SELECT id, creator_id, creator_name, target_user, remind_text, remind_time FROM reminders WHERE is_sent = 0 AND remind_time <= ?",
                    (now_str,),
                ).fetchall()

            for rec in due_records:
                target_chat_id = find_chat_id_by_target(rec["target_user"])
                alert_text = (
                    f"📩 **Сообщение!**\n\n"
                    f"👤 **Кому:** {rec['target_user']}\n"
                    f"📝 **Суть:** {rec['remind_text']}\n"
                    f"✍️ **От кого:** {rec['creator_name']}"
                )

                if target_chat_id:
                    try:
                        await app.bot.send_message(chat_id=target_chat_id, text=alert_text, parse_mode="Markdown")
                    except Exception as err:
                        logger.error(f"Ошибка доставки напоминания #{rec['id']}: {err}")
                else:
                    try:
                        await app.bot.send_message(
                            chat_id=rec["creator_id"],
                            text=f"⚠️ Не удалось доставить адресату **{rec['target_user']}** (бот не запущен):\n\n{alert_text}",
                            parse_mode="Markdown",
                        )
                    except Exception as err:
                        logger.error(f"Ошибка уведомления создателя #{rec['id']}: {err}")

                with get_db() as conn:
                    conn.execute("UPDATE reminders SET is_sent = 1 WHERE id = ?", (rec["id"],))

        except Exception as e:
            logger.error(f"Ошибка в цикле reminder_scheduler: {e}")

        await asyncio.sleep(30)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uinfo = get_user_info(user.username)
    if not uinfo:
        await safe_reply(update.message, f"⛔ Доступ запрещен. ID: {user.id}")
        return

    cancel_waiting_edits(user.id)
    with get_db() as conn:
        conn.execute(
            """INSERT INTO telegram_users (user_id, username, first_name) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET username = excluded.username, first_name = excluded.first_name""",
            (user.id, (user.username or "").lower(), uinfo["name"]),
        )

    mode = "admin" if uinfo["is_admin"] else "worker"
    set_user_mode(user.id, mode)
    kb = get_admin_keyboard() if uinfo["is_admin"] else get_worker_keyboard(is_admin=False)

    await safe_reply(
        update.message,
        f"👋 Здравствуйте, {uinfo['name']}!\nРежим: **{'Администратор' if uinfo['is_admin'] else 'Сотрудник'}**.",
        reply_markup=kb,
    )

async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uinfo = get_user_info(user.username)
    if not uinfo or not uinfo["is_admin"]:
        return

    if not context.args:
        await safe_reply(update.message, "ℹ️ Укажите ID записи. Пример: /del 3")
        return

    try:
        tx_id = int(context.args[0])
    except ValueError:
        return

    with get_db() as conn:
        record = conn.execute("SELECT id FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        if not record:
            await safe_reply(update.message, f"⚠️ Запись #{tx_id} не найдена.")
            return
        conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))

    await safe_reply(update.message, f"🗑 Запись #{tx_id} удалена.")

async def send_last_records(update: Update):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, company, counterparty, amount FROM transactions ORDER BY id DESC LIMIT 5"
        ).fetchall()

    if not rows:
        await update.message.reply_text("В базе пока нет записей.")
        return

    await safe_reply(update.message, "📋 **Последние записи:**")
    for r in rows:
        msg = f"🔹 **ID #{r['id']}**\n{r['company']} -> {r['counterparty']}\nСумма: **{r['amount']:,.0f} ₸**"
        del_kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🗑 Удалить #{r['id']}", callback_data=f"del_tx_{r['id']}")]])
        await safe_reply(update.message, msg, reply_markup=del_kb)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    uinfo = get_user_info(user.username)
    if not uinfo or not uinfo["is_admin"]:
        return

    data = query.data

    if data == "staff_menu":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Список сотрудников", callback_data="staff_list")],
            [InlineKeyboardButton("➕ Добавить сотрудника", callback_data="staff_add")],
            [InlineKeyboardButton("❌ Удалить сотрудника", callback_data="staff_delete_select")],
            [InlineKeyboardButton("🔙 Закрыть", callback_data="cancel_action")],
        ])
        await safe_edit(query, "⚙️ **Управление персоналом**\nВыберите действие:", reply_markup=kb)
        return

    if data == "staff_list":
        content = "Файл пуст."
        if USERS_FILE.exists():
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                content = f.read()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="staff_menu")]])
        await safe_edit(query, f"👥 **Список сотрудников:**\n```text\n{content}\n```", reply_markup=kb)
        return

    if data == "staff_add":
        set_user_state(user.id, step="awaiting_name")
        await safe_edit(query, "👤 Введите **Имя** нового сотрудника (например: Айдар):")
        return

    if data == "staff_delete_select":
        inline_kb = []
        if USERS_FILE.exists():
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or ":" not in stripped:
                        continue
                    parts = [p.strip() for p in stripped.split(":", 2)]
                    name, uname = parts[0], parts[1]
                    if uname.lstrip("@").lower() in SUPER_ADMINS:
                        continue
                    inline_kb.append([InlineKeyboardButton(f"🗑 {name} ({uname})", callback_data=f"del_user_{uname}")])
        inline_kb.append([InlineKeyboardButton("🔙 Назад", callback_data="staff_menu")])
        await safe_edit(query, "👥 **Выберите сотрудника для удаления:**", reply_markup=InlineKeyboardMarkup(inline_kb))
        return

    if data.startswith("del_user_"):
        target_uname = data.replace("del_user_", "").lstrip("@").strip().lower()
        lines = []
        found = False
        deleted_line = ""

        if USERS_FILE.exists():
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or ":" not in stripped:
                        lines.append(line)
                        continue
                    parts = [p.strip() for p in stripped.split(":", 2)]
                    file_uname = parts[1].lstrip("@").strip().lower()
                    if file_uname == target_uname:
                        found = True
                        deleted_line = stripped
                    else:
                        lines.append(line)

        if found:
            with open(USERS_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines)
            await safe_edit(query, f"✅ **Сотрудник удален!**\n{deleted_line}")
        else:
            await safe_edit(query, f"⚠️ Пользователь @{target_uname} не найден.")
        return

    if data.startswith("del_tx_"):
        tx_id = int(data.replace("del_tx_", ""))
        with get_db() as conn:
            conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
        await safe_edit(query, f"🗑 Запись **ID #{tx_id}** удалена.")
        return

    if data == "cancel_action":
        await safe_edit(query, "❌ Отменено.")
        return

    if ":" in data:
        action, draft_id_str = data.split(":", 1)
        draft_id = int(draft_id_str)
    else:
        return

    draft = get_draft(draft_id)
    if not draft:
        await safe_edit(query, "Черновик устарел.")
        return

    if action == "confirm_ok":
        parsed = draft["data"]
        draft_uinfo = draft["uinfo"]
        items = parsed.get("items", [])

        with get_db() as conn:
            conn.execute(
                """INSERT INTO summaries (added_by_name, title, formatted_summary, raw_json, created_at)
                   VALUES (?, ?, ?, ?, datetime('now', 'localtime'))""",
                (draft_uinfo["name"], parsed.get("summary_title", "Отчет"), parsed.get("readable_card", ""), json.dumps(parsed, ensure_ascii=False)),
            )
            for item in items:
                conn.execute(
                    """INSERT INTO transactions 
                       (added_by_username, added_by_name, doc_date, doc_type, company, counterparty, operation_type, amount, details, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))""",
                    (
                        draft_uinfo["username"],
                        draft_uinfo["name"],
                        item.get("doc_date", ""),
                        item.get("doc_type", ""),
                        item.get("company", ""),
                        item.get("counterparty", ""),
                        item.get("operation_type", ""),
                        float(item.get("amount") or 0),
                        item.get("details", ""),
                    ),
                )
            conn.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))

        await safe_edit(
            query,
            f"✅ **Успешно записано в базу данных SQLite!**\n\n"
            f"Таблица: `transactions` ({len(items)} записей)\n"
            f"Таблица: `summaries` (1 сводка)\n\n"
            f"{parsed.get('readable_card', '')}",
        )

    elif action == "confirm_edit":
        cancel_waiting_edits(user.id)
        update_draft(draft_id, waiting_edit=True)
        await safe_reply(query.message, "✏️ Напишите исправление текстом:")

    elif action == "confirm_cancel":
        delete_draft(draft_id)
        await safe_edit(query, "❌ Отменено.")

async def process_incoming_doc(update: Update, context: ContextTypes.DEFAULT_TYPE, uinfo: dict, content_blocks: list):
    user_id = update.effective_user.id
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        parsed = await extract_document_via_claude(content_blocks)
        items = parsed.get("items", [])
        total_items_amount = sum(float(item.get("amount") or 0.0) for item in items)
        audit_note = f"\n\n🔍 **Контрольная сверка:** строк: {len(items)}, общая сумма: **{total_items_amount:,.0f} ₸**"

        draft_id = create_draft(user_id, uinfo, parsed)
        card_text = f"📋 **Проверьте данные:**\n\n{parsed.get('readable_card', '')}{audit_note}\n\n❓ **Сохранить в базу?**"
        await safe_reply(update.message, card_text, reply_markup=get_draft_keyboard(draft_id))
    except Exception as e:
        logger.error(f"Ошибка распознавания данных: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Ошибка распознавания данных. Проверьте формат документа.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uinfo = get_user_info(user.username)
    if not uinfo:
        return

    text = update.message.text.strip()
    state = get_user_state(user.id)
    back_kb = get_admin_keyboard() if uinfo["is_admin"] else get_worker_keyboard(is_admin=False)

    if state:
        if text.lower() in ["отмена", "/cancel"]:
            clear_user_state(user.id)
            await safe_reply(update.message, "❌ Действие отменено.", reply_markup=back_kb)
            return

        if uinfo["is_admin"] and state["step"] == "awaiting_name":
            set_user_state(user.id, step="awaiting_username", temp_name=text)
            await safe_reply(update.message, "🔗 Введите **Telegram Username** сотрудника (например: @username):")
            return

        elif uinfo["is_admin"] and state["step"] == "awaiting_username":
            uname = text if text.startswith("@") else f"@{text}"
            set_user_state(user.id, step="awaiting_role", temp_name=state["temp_name"], temp_username=uname)
            await safe_reply(update.message, "📝 Введите **Роль / Описание** сотрудника (например: Менеджер):")
            return

        elif uinfo["is_admin"] and state["step"] == "awaiting_role":
            name, uname, role = state["temp_name"], state["temp_username"], text
            clear_user_state(user.id)
            new_line = f"{name} : {uname} : {role}"
            with open(USERS_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n{new_line}")
            await safe_reply(update.message, f"✅ **Сотрудник успешно добавлен!**\n{new_line}", reply_markup=back_kb)
            return

        elif state["step"] == "remind_target":
            cleaned_target = text.strip().lower()
            target_value = user.username or uinfo["name"] if cleaned_target in ["мне", "себе", "я", "сам", "сама"] else text
            set_user_state(user.id, step="remind_text", temp_target=target_value)
            await safe_reply(update.message, f"📝 Что именно нужно напомнить для **{target_value}**?")
            return

        elif state["step"] == "remind_text":
            set_user_state(user.id, step="remind_time", temp_target=state["temp_target"], temp_remind_text=text)
            await safe_reply(
                update.message,
                "🗓 Когда отправить Сообщение?\nФормат свободный (`прямо сейчас`, `завтра в 11:30`, `через 45 минут`):",
            )
            return

        elif state["step"] == "remind_time":
            target = state["temp_target"]
            remind_msg = state["temp_remind_text"]
            clear_user_state(user.id)

            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
            parsed_dt = await parse_reminder_time_via_claude(text)
            target_chat_id = find_chat_id_by_target(target)

            alert_text = (
                f"📩 **Сообщение!**\n\n"
                f"👤 **Кому:** {target}\n"
                f"📝 **Суть:** {remind_msg}\n"
                f"✍️ **От кого:** {uinfo['name']}"
            )

            if parsed_dt.get("send_now"):
                sent_to_target = False
                if target_chat_id:
                    try:
                        await context.bot.send_message(chat_id=target_chat_id, text=alert_text, parse_mode="Markdown")
                        sent_to_target = True
                    except Exception as e:
                        logger.error(f"Не удалось отправить пользователю: {e}")

                if sent_to_target:
                    await safe_reply(update.message, f"⚡️ Сообщение отправлено пользователю **{target}**!", reply_markup=back_kb)
                else:
                    await safe_reply(
                        update.message,
                        f"⚠️ Пользователь **{target}** еще не запускал бота.\nСообщение:\n\n{alert_text}",
                        reply_markup=back_kb,
                    )
                return

            sched_time = parsed_dt.get("scheduled_datetime")
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO reminders (creator_id, creator_name, target_user, remind_text, remind_time) VALUES (?, ?, ?, ?, ?)",
                    (user.id, uinfo["name"], target, remind_msg, sched_time),
                )

            status_note = "" if target_chat_id else "\n⚠️ _(Адресат еще не нажал /start — уведомление придет вам, если он не активирует бота)_"
            await safe_reply(
                update.message,
                f"✅ **Сообщение запланировано!**\n\n"
                f"👤 **Кому:** {target}\n"
                f"📝 **Суть:** {remind_msg}\n"
                f"📩 **Срок:** {sched_time} ({parsed_dt.get('readable_time', '')}){status_note}",
                reply_markup=back_kb,
            )
            return

    clean_cmd = text.strip().lower()
    if clean_cmd in ["новая задача", "/new", "сбросить контекст", "🔄 сбросить контекст"]:
        cancel_waiting_edits(user.id)
        clear_user_history(user.id)
        clear_user_state(user.id)
        await safe_reply(update.message, "🆕 **Контекст очищен.** Диалог начат с чистого листа.", reply_markup=back_kb)
        return

    if clean_cmd in ["отменить", "отмена"]:
        cancel_waiting_edits(user.id)
        clear_user_state(user.id)
        await safe_reply(update.message, "❌ Текущее действие отменено.", reply_markup=back_kb)
        return

    if clean_cmd.startswith("сохранить правило:") or clean_cmd.startswith("запомни:"):
        rule_content = text.split(":", 1)[1].strip()
        if rule_content:
            success = add_user_rule(user.id, rule_content)
            msg = f"💾 **Правило сохранено:**\n«{rule_content}»" if success else "⚠️ Ошибка записи в базу данных."
            await safe_reply(update.message, msg)
        else:
            await safe_reply(update.message, "ℹ️ Укажите текст правила после двоеточия.")
        return

    if clean_cmd in ["мои правила", "правила"]:
        rules = get_user_rules(user.id)
        if not rules:
            await safe_reply(update.message, "У вас нет сохранённых правил.\nДобавить: `Сохранить правило: <текст>`")
        else:
            formatted_rules = "\n".join([f"{idx+1}. {r['text']} (ID: {r['id']})" for idx, r in enumerate(rules)])
            await safe_reply(update.message, f"📋 **Ваши сохранённые правила:**\n{formatted_rules}")
        return

    if clean_cmd in ["очистить правила"]:
        cnt = clear_user_rules(user.id)
        await safe_reply(update.message, f"🗑 Удалено сохранённых правил: {cnt}.")
        return

    if text == "📩 Создать Сообщение":
        cancel_waiting_edits(user.id)
        set_user_state(user.id, step="remind_target")
        await safe_reply(update.message, "👤 Кому адресовано Сообщение? Введите имя или @username:")
        return

    if uinfo["is_admin"]:
        if text == "👷 Режим Сотрудника":
            cancel_waiting_edits(user.id)
            set_user_mode(user.id, "worker")
            await update.message.reply_text("👷 Включен режим сотрудника.", reply_markup=get_worker_keyboard(is_admin=True))
            return
        elif text == "👑 Режим Администратора":
            cancel_waiting_edits(user.id)
            set_user_mode(user.id, "admin")
            await update.message.reply_text("👑 Включен режим администратора.", reply_markup=get_admin_keyboard())
            return
        elif text == "📋 Последние записи":
            cancel_waiting_edits(user.id)
            await send_last_records(update)
            return
        elif text == "📊 Финансовая сводка":
            cancel_waiting_edits(user.id)
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
            ans = await ask_financial_analyst(user.id, "Сделай полную финансовую сводку.")
            await send_long_message(update.message, ans)
            return
        elif text == "📥 Выгрузить Excel":
            cancel_waiting_edits(user.id)
            bio = await asyncio.to_thread(generate_excel_bytes)
            await update.message.reply_document(document=bio, filename="Финансы_AST_GROUP.xlsx")
            return
        elif text == "⚙️ Управление персоналом":
            cancel_waiting_edits(user.id)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 Список сотрудников", callback_data="staff_list")],
                [InlineKeyboardButton("➕ Добавить сотрудника", callback_data="staff_add")],
                [InlineKeyboardButton("❌ Удалить сотрудника", callback_data="staff_delete_select")],
                [InlineKeyboardButton("🔙 Закрыть", callback_data="cancel_action")],
            ])
            await safe_reply(update.message, "⚙️ **Управление персоналом**\nВыберите действие:", reply_markup=kb)
            return

    waiting_draft = get_waiting_edit_draft(user.id)
    if waiting_draft:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        try:
            updated = await extract_document_via_claude([], previous_draft=waiting_draft["data"], user_correction=text)
            update_draft(waiting_draft["id"], parsed_data=updated, waiting_edit=False)
            await safe_reply(
                update.message,
                f"📋 **Обновлено:**\n\n{updated.get('readable_card', '')}\n\n❓ **Сохраняем?**",
                reply_markup=get_draft_keyboard(waiting_draft["id"]),
            )
        except Exception:
            await update.message.reply_text("⚠️ Не удалось применить правку.")
        return

    mode = get_user_mode(user.id, uinfo["is_admin"])
    if mode == "admin":
        lower_text = text.lower()
        add_triggers = ["добавь", "запиши", "внеси", "зафиксируй", "сохрани", "свободных"]
        if any(w in lower_text for w in add_triggers) and not text.startswith("📊"):
            await process_incoming_doc(update, context, uinfo, [{"type": "text", "text": f"Фиксация операции: {text}"}])
            return

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        ans = await ask_financial_analyst(user.id, text)
        await send_long_message(update.message, ans)
        return

    await process_incoming_doc(update, context, uinfo, [{"type": "text", "text": text}])

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uinfo = get_user_info(user.username)
    if not uinfo:
        return
    photo_file = await update.message.photo[-1].get_file()
    b = bytes(await photo_file.download_as_bytearray())

    timestamp = get_current_time().strftime("%Y%m%d_%H%M%S")
    uname = (user.username or str(user.id)).lstrip("@")
    file_path = DATA_DIR / f"{timestamp}_{uname}_{photo_file.file_unique_id}.jpg"
    try:
        await asyncio.to_thread(file_path.write_bytes, b)
    except Exception as e:
        logger.error(f"Ошибка сохранения фото на диск: {e}")

    b64 = base64.b64encode(b).decode("utf-8")
    caption = update.message.caption or "Распознай чек."
    await process_incoming_doc(update, context, uinfo, [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
        {"type": "text", "text": caption},
    ])

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uinfo = get_user_info(user.username)
    if not uinfo:
        return

    doc = update.message.document
    original_name = doc.file_name or "document"
    fname = original_name.lower()
    fobj = await doc.get_file()
    b = bytes(await fobj.download_as_bytearray())

    timestamp = get_current_time().strftime("%Y%m%d_%H%M%S")
    uname = (user.username or str(user.id)).lstrip("@")
    clean_original_name = re.sub(r"[^\w\.-]", "_", original_name)
    save_path = DATA_DIR / f"{timestamp}_{uname}_{clean_original_name}"
    try:
        await asyncio.to_thread(save_path.write_bytes, b)
    except Exception as e:
        logger.error(f"Ошибка сохранения документа на диск: {e}")

    caption = update.message.caption or f"Распознай документ {doc.file_name}"

    if fname.endswith((".jpg", ".jpeg", ".png", ".webp")):
        b64 = base64.b64encode(b).decode("utf-8")
        await process_incoming_doc(update, context, uinfo, [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text", "text": caption},
        ])
        return

    try:
        if fname.endswith(".xlsx"):
            extracted = await asyncio.to_thread(parse_excel_xlsx, b)
        elif fname.endswith(".xls"):
            extracted = await asyncio.to_thread(parse_excel_xls, b)
        elif fname.endswith(".pdf"):
            extracted = await asyncio.to_thread(parse_pdf, b)
        elif fname.endswith((".txt", ".csv")):
            extracted = await asyncio.to_thread(decode_text_bytes, b)
        else:
            await update.message.reply_text("Формат не поддерживается.")
            return
    except Exception:
        await update.message.reply_text("⚠️ Не удалось прочитать файл.")
        return

    await process_incoming_doc(update, context, uinfo, [{"type": "text", "text": f"Файл {doc.file_name}:\n{extracted}"}])

def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN не задан в .env файле.")
        return
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY не задан в .env файле.")
        return

    init_db()
    logger.info("Запуск бота AST GROUP...")
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .read_timeout(90)
        .write_timeout(90)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("del", delete_command))
    app.add_handler(CommandHandler("records", lambda u, c: send_last_records(u)))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    async def post_init(application: Application):
        asyncio.create_task(reminder_scheduler(application))

    app.post_init = post_init
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
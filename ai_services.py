import json
import asyncio
from datetime import datetime
from anthropic import AsyncAnthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, get_current_time
from database import get_db, get_user_rules, get_recent_history, save_chat_message
from parsers import get_weather_forecast

claude_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY, max_retries=3)

WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
}

WEATHER_TOOL = {
    "name": "get_weather_forecast",
    "description": "Получает актуальный прогноз погоды для указанного города.",
    "input_schema": {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "Название города, например Астана или Алматы"}
        },
        "required": ["city"],
    },
}

PARSE_DATE_TOOL = {
    "name": "parse_datetime",
    "description": "Парсит текстовое описание даты и времени в точный формат YYYY-MM-DD HH:MM или определяет флаг немедленной отправки.",
    "input_schema": {
        "type": "object",
        "properties": {
            "send_now": {
                "type": "boolean",
                "description": "True, если пользователь хочет отправить сообщение сразу / прямо сейчас.",
            },
            "scheduled_datetime": {
                "type": "string",
                "description": "Дата и время в формате 'YYYY-MM-DD HH:MM'.",
            },
            "readable_time": {
                "type": "string",
                "description": "Понятное человеку описание срока ('через 15 минут', 'завтра в 14:00').",
            },
        },
        "required": ["send_now", "scheduled_datetime", "readable_time"],
    },
}

EXTRACTION_TOOL = {
    "name": "record_financial_document",
    "description": "Извлекает структурированные финансовые операции из документа, чека, таблицы или текста.",
    "input_schema": {
        "type": "object",
        "properties": {
            "readable_card": {
                "type": "string",
                "description": "Краткая наглядная сводка операции с суммами, датами, контрагентом и эмодзи.",
            },
            "summary_title": {
                "type": "string",
                "description": "Короткое название операции или документа.",
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "doc_date": {"type": "string", "description": "Дата документа в формате ДД.ММ.ГГГГ"},
                        "doc_type": {"type": "string", "description": "Чек / Счет / Выписка / Акт / Корректировка / Остаток"},
                        "company": {"type": "string", "description": "AS GROUP / ИП БУХLINE / АСТ / АСБ / Kaspi / Свободные средства"},
                        "counterparty": {"type": "string", "description": "Контрагент или основание операции"},
                        "operation_type": {
                            "type": "string",
                            "enum": ["Приход", "Расход", "Свободные средства", "Остаток", "Корректировка"],
                        },
                        "amount": {"type": "number", "description": "Сумма операции числом (положительное)"},
                        "details": {"type": "string", "description": "Назначение платежа или детали"},
                    },
                    "required": ["doc_date", "doc_type", "company", "counterparty", "operation_type", "amount", "details"],
                },
            },
        },
        "required": ["readable_card", "summary_title", "items"],
    },
}

SYSTEM_EXTRACTOR = (
    "Ты — финансовый контролер компании AST GROUP (компании группы: AS GROUP, ИП БУХLINE, АСТ, АСБ).\n"
    "Проанализируй присланный документ, изображение чека, таблицу или текст.\n"
    "Обязательно вызови инструмент record_financial_document и заполни все поля максимально точно."
)

async def parse_reminder_time_via_claude(user_input: str) -> dict:
    now_dt = get_current_time()
    now_str = now_dt.strftime("%Y-%m-%d %H:%M (%A, Asia/Almaty UTC+5)")
    system_prompt = (
        f"Ты — парсер даты и времени. Текущее время сервера: {now_str}.\n"
        "Переведи фразу пользователя о сроке в точную целевую дату формата YYYY-MM-DD HH:MM.\n"
        "Обязательно вызови инструмент parse_datetime."
    )
    resp = await claude_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=500,
        system=system_prompt,
        tools=[PARSE_DATE_TOOL],
        tool_choice={"type": "tool", "name": "parse_datetime"},
        messages=[{"role": "user", "content": user_input}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "parse_datetime":
            return block.input
    return {"send_now": False, "scheduled_datetime": now_dt.strftime("%Y-%m-%d %H:%M"), "readable_time": user_input}

async def extract_document_via_claude(messages_content: list, previous_draft: dict = None, user_correction: str = None) -> dict:
    today_str = get_current_time().strftime("%d.%m.%Y")
    system_with_date = f"{SYSTEM_EXTRACTOR}\nСегодняшняя дата: {today_str}."

    if previous_draft and user_correction:
        prompt = (
            f"Предыдущие распознанные данные (JSON):\n{json.dumps(previous_draft, ensure_ascii=False)}\n\n"
            f"Правка от пользователя: {user_correction}\n"
            "Пересчитай данные, обнови readable_card и вызови инструмент record_financial_document."
        )
        content = [{"type": "text", "text": prompt}]
    else:
        content = messages_content

    resp = await claude_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=3000,
        system=system_with_date,
        tools=[EXTRACTION_TOOL],
        tool_choice={"type": "tool", "name": "record_financial_document"},
        messages=[{"role": "user", "content": content}],
    )

    for block in resp.content:
        if block.type == "tool_use" and block.name == "record_financial_document":
            return block.input

    raise ValueError("Модель не вернула ожидаемую структуру данных.")

async def ask_financial_analyst(user_id: int, question: str) -> str:
    financial_keywords = ["баланс", "сводка", "деньги", "транзакц", "остаток", "расход", "приход", "касса", "отчет", "выручка", "kzt", "тенге"]
    needs_finance_data = any(kw in question.lower() for kw in financial_keywords)

    context_str = ""
    if needs_finance_data:
        with get_db() as conn:
            total_count = conn.execute("SELECT COUNT(*) as cnt FROM transactions").fetchone()["cnt"]
            totals_by_type = conn.execute(
                "SELECT operation_type, SUM(amount) as total FROM transactions GROUP BY operation_type"
            ).fetchall()
            txs = conn.execute(
                """SELECT id, doc_date, doc_type, company, counterparty, operation_type, amount, details
                   FROM transactions ORDER BY id DESC LIMIT 100"""
            ).fetchall()

        sum_initial = sum(float(r["total"] or 0.0) for r in totals_by_type if r["operation_type"] in ("Остаток", "Свободные средства"))
        sum_income = sum(float(r["total"] or 0.0) for r in totals_by_type if r["operation_type"] == "Приход")
        sum_expense = sum(float(r["total"] or 0.0) for r in totals_by_type if r["operation_type"] == "Расход")
        exact_balance = (sum_initial + sum_income) - sum_expense

        context_str = (
            f"ФИНАНСОВЫЙ КОНТЕКСТ БАЗЫ (Всего записей: {total_count}):\n"
            f"Баланс: {exact_balance:,.0f} KZT | Приход: {sum_income:,.0f} KZT | Расход: {sum_expense:,.0f} KZT\n"
            "ПОСЛЕДНИЕ ОПЕРАЦИИ:\n"
        )
        for t in reversed(txs):
            context_str += f"- #{t['id']} | {t['doc_date']} | {t['company']} -> {t['counterparty']} | {t['operation_type']}: {t['amount']:,.0f} KZT ({t['details']})\n"
        context_str += "\n"

    user_rules = get_user_rules(user_id)
    rules_block = ""
    if user_rules:
        rules_block = "ПОЛЬЗОВАТЕЛЬСКИЕ ПРАВИЛА:\n" + "\n".join(f"- {r['text']}" for r in user_rules) + "\n\n"

    now_dt = get_current_time()
    today_str = now_dt.strftime("%d.%m.%Y %H:%M (%A, Asia/Almaty, UTC+5)")
    system_prompt = (
        f"Ты — корпоративный ассистент компании AST GROUP. Системное время: {today_str}.\n"
        f"{rules_block}"
        "Правила ответа:\n"
        "1. Не смешивай контекст разных задач. Если запрос не касается финансов, не упоминай счета и балансы.\n"
        "2. Для внешних данных используй web_search или специализированные инструменты (погода). Если данные не найдены, прямо скажи об этом.\n"
        "3. При расчётах проверяй формулы и явно показывай контрольную сумму."
    )

    history = get_recent_history(user_id, limit=6)
    messages_payload = [{"role": h["role"], "content": h["content"]} for h in history]
    messages_payload.append({"role": "user", "content": f"{context_str}\n\nЗапрос: {question}"})

    current_messages = list(messages_payload)

    for _ in range(3):
        resp = await claude_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=3000,
            system=system_prompt,
            tools=[WEB_SEARCH_TOOL, WEATHER_TOOL],
            messages=current_messages,
        )

        if resp.stop_reason == "tool_use":
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use" and block.name == "get_weather_forecast":
                    target_city = block.input.get("city", "Астана")
                    res_text = await asyncio.to_thread(get_weather_forecast, target_city)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(res_text),
                    })

            if not tool_results:
                break

            current_messages.append({"role": "assistant", "content": resp.content})
            current_messages.append({"role": "user", "content": tool_results})
        else:
            break

    texts = [b.text for b in resp.content if getattr(b, "type", "") == "text" and hasattr(b, "text")]
    answer = "\n".join(texts).strip() or "Не удалось сформировать ответ. Попробуйте уточнить запрос."

    save_chat_message(user_id, "user", question)
    save_chat_message(user_id, "assistant", answer)
    return answer
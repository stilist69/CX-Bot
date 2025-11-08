import os
import re
import json
import random
import asyncio
from typing import Callable, Type, Iterable, Dict, Any, List, Tuple, Optional
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
import uvicorn

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, ApplicationBuilder, MessageHandler, CommandHandler,
    ConversationHandler, ContextTypes, PicklePersistence, filters
)
from telegram.error import TimedOut, RetryAfter, NetworkError

# Optional Google Sheets logging
try:
    import gspread
    from gspread.exceptions import APIError
    HAS_GS = True
except Exception:
    HAS_GS = False
    class APIError(Exception):
        pass

# ==========================
# Helpers & configuration
# ==========================

# Normalize "Завершити" input of any form (emoji, spaces, case)
def is_exit(text: str) -> bool:
    t = (text or "").casefold().strip()
    t = t.replace("🔚", "").strip()
    return t.endswith("завершити")

# Buttons & keyboards
ROLE_BUTTONS = {"👩‍💼 Керівник", "🦷 Лікар", "💬 Адміністратор"}
ABC_BUTTONS  = {"A", "B", "C"}
EXIT_TEXTS   = {"🔚 Завершити", "Завершити"}

ROLE_KB = ReplyKeyboardMarkup(
    [["👩‍💼 Керівник"], ["🦷 Лікар"], ["💬 Адміністратор"], ["🔚 Завершити"]],
    resize_keyboard=True
)
ABC_KB = ReplyKeyboardMarkup(
    [["A", "B", "C"], ["🔚 Завершити"]],
    resize_keyboard=True
)

# Conversation states
CHOOSING_ROLE, ASKING = range(2)

# Environment & required settings
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")

APP_BASE_URL = os.getenv("APP_BASE_URL")  # optional; needed for set_webhook
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET is required")

# Google Sheets env (optional)
GS_CREDENTIALS_JSON = os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON")  # JSON string for a Service Account
GS_SPREADSHEET_KEY = os.getenv("GOOGLE_SHEETS_SPREADSHEET_KEY")    # target spreadsheet id
GS_WORKSHEET_NAME  = os.getenv("GOOGLE_SHEETS_WORKSHEET_NAME", "Logs")

# Fixed retry strategy (with respect to RetryAfter)
class RetryConfig:
    def __init__(
        self,
        attempts: int = 5,
        delays: List[float] = None,
        jitter: float = 0.0,
        retry_on: Iterable[Type[BaseException]] = (TimedOut, RetryAfter, NetworkError, APIError),
    ):
        self.attempts = attempts
        self.delays = delays or [1.0, 2.0, 2.0, 3.0, 5.0]
        self.jitter = jitter
        self.retry_on = tuple(retry_on)

TG_RETRY = RetryConfig()

async def retry_async(func: Callable, *, cfg: RetryConfig, **kwargs):
    attempt = 0
    while True:
        try:
            return await func(**kwargs)
        except cfg.retry_on as e:  # type: ignore
            attempt += 1
            if attempt >= cfg.attempts:
                raise
            # Respect RetryAfter exact delay
            if isinstance(e, RetryAfter) and getattr(e, "retry_after", None):
                await asyncio.sleep(float(e.retry_after))
            else:
                delay = cfg.delays[min(attempt-1, len(cfg.delays)-1)]
                if cfg.jitter:
                    delay += random.uniform(0, cfg.jitter)
                await asyncio.sleep(delay)

# Safe reply wrapper with retries
async def safe_reply(message, *, text: str, reply_markup=None):
    async def _send():
        return await message.reply_text(text=text, reply_markup=reply_markup)
    return await retry_async(_send, cfg=TG_RETRY)

# ==========================
# Questions (keep 5 per role)
# ==========================

# TODO: Replace these with your real questions.
# Each item is a tuple: (question_text, correct_option) where correct_option is 'A'/'B'/'C'.
QUESTIONS: Dict[str, List[Tuple[str, str]]] = {
    "Керівник": [
        ('Як ви дізнаєтесь, що пацієнт залишився задоволеним?

A) Якщо не скаржився — значить, усе добре.
B) Ми періодично запитуємо відгуки.
C) Лікарі самі бачать, коли пацієнт задоволений.', "B"),
        ('Як часто ви обговорюєте сервіс із командою?

A) Раз на рік на загальних зборах.
B) Коли з’являються проблеми.
C) Регулярно, як частину роботи.', "C"),
        ('Що для вас важливіше: нові пацієнти чи повторні?

A) Головне — потік нових.
B) Повторні — бо це показник довіри.
C) Обидва варіанти однакові.', "B"),
        ('Коли востаннє ви проходили шлях пацієнта особисто (дзвінок, запис, прийом)?

A) Ніколи.
B) Давно, але колись робив(-ла).
C) Роблю це регулярно.', "C"),
        ('Як ви реагуєте на скаргу?

A) Захищаю команду — вони стараються.
B) Розбираюсь спокійно, шукаю, що можна покращити.
C) Ігнорую, якщо пацієнт «важкий».', "B"),
    ],
    "Лікар": [
        ('Як ви пояснюєте пацієнту план лікування?

A) Стисло — без деталей.
B) Детально, простою мовою, показую приклади.
C) Лише тоді, коли питає.', "B"),
        ('Що ви робите, якщо пацієнт нервує?

A) Продовжую працювати — час дорогоцінний.
B) Роблю паузу, пояснюю, що буде далі.
C) Прошу адміністратора/асистента заспокоїти.', "B"),
        ('Як ви передаєте інформацію адміністратору після прийому?

A) Усно, коли є час.
B) Через нотатку або у CRM.
C) Не передаю — він сам розбереться.', "B"),
        ('Що ви робите, якщо пацієнт відмовляється від лікування?

A) Пропоную дешевший варіант.
B) Запитую, що саме викликає сумнів.
C) Просто фіксую відмову.', "B"),
        ('Як ви ставитесь до відгуків пацієнтів?

A) Не читаю — зайве нервування.
B) Читаю і думаю, як покращити комунікацію.
C) Вважаю, що більшість пишуть емоційно.', "B"),
    ],
    "Адміністратор": [
        ('Як ви вітаєте пацієнта, якщо він запізнився?

A) Роблю зауваження — це ж правила.
B) Спокійно вітаю, пояснюю, що ми все одно приймемо.
C) Ігнорую ситуацію, щоб не псувати настрій.', "B"),
        ('Якщо лікар затримується — що ви робите?

A) Кажу «чекайте».
B) Повідомляю, скільки часу орієнтовно чекати, і пропоную воду/каву.
C) А що я можу зробити? Це не моя зона відповідальності.', "B"),
        ('Як ви реагуєте на скаргу?

A) Переадресовую керівнику.
B) Спокійно вислуховую, дякую за відгук і передаю далі.
C) Виправдовую колегу.', "B"),
        ('Коли телефонуєте пацієнту після лікування, що ви кажете?

A) «Як себе почуваєте? Усе добре?»
B) «Ми нагадуємо про наступний візит.»
C) Не телефоную — якщо треба, сам подзвонить.', "A"),
        ('Як завершуєте розмову по телефону?

A) «До побачення.»
B) «Гарного дня, чекаємо вас.»
C) Просто кладу слухавку. Розмова ж завершена.', "B"),
    ],
}

# ==========================
# Google Sheets logger
# ==========================

def _open_worksheet():
    if not HAS_GS:
        return None
    if not (GS_CREDENTIALS_JSON and GS_SPREADSHEET_KEY):
        return None
    try:
        creds = json.loads(GS_CREDENTIALS_JSON)
        gc = gspread.service_account_from_dict(creds)
        sh = gc.open_by_key(GS_SPREADSHEET_KEY)
        try:
            ws = sh.worksheet(GS_WORKSHEET_NAME)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(GS_WORKSHEET_NAME, rows=1000, cols=20)
        return ws
    except Exception:
        return None

async def log_result_async(user_id: int, role: str, correct: int, total: int):
    ws = await asyncio.to_thread(_open_worksheet)
    if not ws:
        return
    row = [datetime.utcnow().isoformat(), str(user_id), role, str(correct), str(total)]
    async def _append():
        return await asyncio.to_thread(ws.append_row, row, value_input_option="RAW")
    try:
        await retry_async(_append, cfg=TG_RETRY)
    except Exception:
        # swallow logging errors
        pass

# ==========================
# Bot logic
# ==========================

app = FastAPI(title="CX Bot")

class WebhookModel(BaseModel):
    update_id: Optional[int] = None

# Anti-duplicate helper for handlers
def _dedupe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    uid = getattr(update, "update_id", None)
    if uid is None:
        return None
    last = context.user_data.get("last_update_id")
    if last == uid:
        # return current state without sending anything
        if "role" not in context.user_data or "i" not in context.user_data:
            return CHOOSING_ROLE
        return ASKING
    context.user_data["last_update_id"] = uid
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Clear state and show roles
    context.user_data.clear()
    await safe_reply(update.message,
        text="Оберіть роль, щоб почати 👇",
        reply_markup=ROLE_KB)
    return CHOOSING_ROLE

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = _dedupe(update, context)
    if d is not None:
        return d
    context.user_data.clear()
    await safe_reply(update.message,
        text="Готово. Можете пройти мікроаудит ще раз — просто оберіть роль нижче 👇",
        reply_markup=ROLE_KB)
    return CHOOSING_ROLE

async def choose_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = _dedupe(update, context)
    if d is not None:
        return d
    text = (update.message.text or "").strip()

    # Exit anytime by button
    if text in EXIT_TEXTS or is_exit(text):
        return await cancel(update, context)

    # Only accept our role buttons
    if text not in ROLE_BUTTONS:
        await safe_reply(update.message,
            text="Оберіть роль, щоб почати 👇",
            reply_markup=ROLE_KB)
        return CHOOSING_ROLE

    # Normalize to role key
    if "Керівник" in text:
        role = "Керівник"
    elif "Лікар" in text:
        role = "Лікар"
    else:
        role = "Адміністратор"

    context.user_data["role"] = role
    context.user_data["i"] = 0
    context.user_data["errors"] = 0

    # Ask first question
    q, _ = QUESTIONS[role][0]
    await safe_reply(update.message, text=q, reply_markup=ABC_KB)
    return ASKING

async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = _dedupe(update, context)
    if d is not None:
        return d

    if "role" not in context.user_data or "i" not in context.user_data:
        await safe_reply(update.message,
            text="Спочатку оберіть роль 👇",
            reply_markup=ROLE_KB)
        return CHOOSING_ROLE

    text = (update.message.text or "").strip()

    # Exit anytime
    if text in EXIT_TEXTS or is_exit(text):
        return await cancel(update, context)

    # Accept only A/B/C from buttons
    if text not in ABC_BUTTONS:
        await safe_reply(update.message,
            text="Будь ласка, оберіть A, B або C 👇",
            reply_markup=ABC_KB)
        return ASKING

    role = context.user_data["role"]
    i = context.user_data["i"]
    total = 5  # fixed number of questions by design
    correct_option = QUESTIONS[role][i][1]
    if text != correct_option:
        context.user_data["errors"] += 1

    i += 1
    context.user_data["i"] = i

    if i < total:
        q, _ = QUESTIONS[role][i]
        await safe_reply(update.message, text=q, reply_markup=ABC_KB)
        return ASKING

    # Finish
    correct = total - context.user_data["errors"]
    result_text = (
        "Є сильні сторони і моменти, які можуть зіпсувати враження пацієнтів. "
        "Я можу показати, як це виглядає їх очима.\n\n"
        f"✅ Ви відповіли правильно на {correct} із {total}.\n\n"
        "Напишіть мені в особисті: @PavelZolottsev — підкажу, як швидко підтягнути сервіс.\n\n"
        "Хочете пройти тест у іншій ролі?"
    )
    await safe_reply(update.message, text=result_text, reply_markup=ROLE_KB)

    # async log (don't block UX)
    try:
        asyncio.create_task(log_result_async(update.effective_user.id, role, correct, total))
    except Exception:
        pass

    # Reset to choose role
    context.user_data.clear()
    return CHOOSING_ROLE

async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = _dedupe(update, context)
    if d is not None:
        return d
    text = (update.message.text or "").strip()

    # Exit universally
    if text in EXIT_TEXTS or is_exit(text):
        return await cancel(update, context)

    # If not in scenario -> show roles and wait for button
    if "role" not in context.user_data or "i" not in context.user_data:
        await safe_reply(update.message,
            text="Оберіть роль, щоб почати 👇",
            reply_markup=ROLE_KB)
        return CHOOSING_ROLE

    # If in scenario -> ask for A/B/C and wait for button
    await safe_reply(update.message,
        text="Будь ласка, оберіть A, B або C 👇",
        reply_markup=ABC_KB)
    return ASKING

# ==========================
# FastAPI + PTB integration
# ==========================

persistence = PicklePersistence(filepath="/tmp/cxbot_state.pickle")
application: Application = ApplicationBuilder().token(BOT_TOKEN).persistence(persistence).build()

conv = ConversationHandler(
    entry_points=[CommandHandler("start", start)],
    states={
        CHOOSING_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_role)],
        ASKING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_answer)],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
    name="cxbot",
    persistent=True,
)

application.add_handler(conv)
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback))

@app.get("/", response_class=PlainTextResponse)
def health():
    return "ok"

@app.get("/set_webhook", response_class=PlainTextResponse)
async def set_webhook(secret: str):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    if not APP_BASE_URL:
        raise HTTPException(status_code=400, detail="APP_BASE_URL not set")
    url = f"{APP_BASE_URL}/webhook/{WEBHOOK_SECRET}"
    await retry_async(application.bot.set_webhook, url=url, cfg=TG_RETRY)
    return f"set_webhook {url}"

@app.post(f"/webhook/{{secret}}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.initialize()
    await application.process_update(update)
    return PlainTextResponse("ok")

if __name__ == "__main__":
    # For local run only (dev)
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)

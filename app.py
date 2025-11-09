# -*- coding: utf-8 -*-
import os
import json
import random
import asyncio
from typing import Callable, Iterable, Type, Dict, List, Tuple
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
import uvicorn

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, ApplicationBuilder, MessageHandler, CommandHandler,
    ConversationHandler, ContextTypes, PicklePersistence, filters
)
from telegram.error import TimedOut, RetryAfter, NetworkError

# ---- Google Sheets logging ----
try:
    import gspread
    from gspread.exceptions import APIError as GSAPIError, WorksheetNotFound
    HAS_GS = True
except Exception:
    HAS_GS = False
    class GSAPIError(Exception): ...
    class WorksheetNotFound(Exception): ...

# ==========================
# Env (as before)
# ==========================
BOT_TOKEN = os.getenv("BOT_TOKEN")  # required by runtime
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "set-a-secret")  # kept for parity (not used in URL)
APP_BASE_URL = os.getenv("APP_BASE_URL")  # e.g. https://your-app.run.app

SHEET_ID = os.getenv("SHEET_ID")  # spreadsheet key
GCP_SERVICE_ACCOUNT = os.getenv("GCP_SERVICE_ACCOUNT")  # JSON string of service account
CONTACT_USERNAME = os.getenv("CONTACT_USERNAME", "")  # e.g. stilist69 (без @)

# ==========================
# Retry helper (fixed delays)
# ==========================
class RetryConfig:
    def __init__(self,
                 attempts: int = 5,
                 delays: List[float] = None,
                 jitter: float = 0.0,
                 retry_on: Iterable[Type[BaseException]] = (TimedOut, RetryAfter, NetworkError, GSAPIError)):
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
            if isinstance(e, RetryAfter) and getattr(e, "retry_after", None):
                await asyncio.sleep(float(e.retry_after))
            else:
                delay = cfg.delays[min(attempt - 1, len(cfg.delays) - 1)]
                if cfg.jitter:
                    delay += random.uniform(0, cfg.jitter)
                await asyncio.sleep(delay)

async def safe_reply(message, *, text: str, reply_markup=None):
    async def _send():
        return await message.reply_text(text=text, reply_markup=reply_markup)
    return await retry_async(_send, cfg=TG_RETRY)

# ==========================
# Keyboards & constants
# ==========================
ROLE_KB = ReplyKeyboardMarkup(
    [["👩‍💼 Керівник"],
     ["🦷 Лікар"],
     ["💬 Адміністратор"],
     ["🔚 Завершити"]],
    resize_keyboard=True
)
ABC_KB = ReplyKeyboardMarkup(
    [["A", "B", "C"], ["🔚 Завершити"]],
    resize_keyboard=True
)

ROLE_BUTTONS = {"👩‍💼 Керівник", "🦷 Лікар", "💬 Адміністратор"}
ABC_BUTTONS = {"A", "B", "C"}
EXIT_BUTTONS = {"🔚 Завершити", "Завершити"}

CHOOSING_ROLE, ASKING = range(2)

def _cta_suffix() -> str:
    handle = (CONTACT_USERNAME or "").lstrip("@")
    return f"\n\nНапишіть мені в особисті: @{handle} — підкажу, як швидко підтягнути сервіс." if handle else ""

def is_exit(text: str) -> bool:
    t = (text or "").casefold().strip()
    t = t.replace("🔚", "").strip()
    return t.endswith("завершити")

# ==========================
# Questions (preserved wording)
# ==========================
def qfmt(q, a, b, c):
    return f"{q}\n\nA) {a}\nB) {b}\nC) {c}"

QUESTIONS: Dict[str, List[Tuple[str, str]]] = {
    "Керівник": [
        (qfmt("Як ви дізнаєтесь, що пацієнт залишився задоволеним?",
              "Якщо не скаржився — значить, усе добре.",
              "Ми періодично запитуємо відгуки.",
              "Лікарі самі бачать, коли пацієнт задоволений."), "B"),
        (qfmt("Як часто ви обговорюєте сервіс із командою?",
              "Раз на рік на загальних зборах.",
              "Коли з’являються проблеми.",
              "Регулярно, як частину роботи."), "C"),
        (qfmt("Що для вас важливіше: нові пацієнти чи повторні?",
              "Головне — потік нових.",
              "Повторні — бо це показник довіри.",
              "Обидва варіанти однакові."), "B"),
        (qfmt("Коли востаннє ви проходили шлях пацієнта особисто (дзвінок, запис, прийом)?",
              "Ніколи.",
              "Давно, але колись робив(-ла).",
              "Роблю це регулярно."), "C"),
        (qfmt("Як ви реагуєте на скаргу?",
              "Захищаю команду — вони стараються.",
              "Розбираюсь спокійно, шукаю, що можна покращити.",
              "Ігнорую, якщо пацієнт «важкий»."), "B"),
    ],
    "Лікар": [
        (qfmt("Як ви пояснюєте пацієнту план лікування?",
              "Стисло — без деталей.",
              "Детально, простою мовою, показую приклади.",
              "Лише тоді, коли питає."), "B"),
        (qfmt("Що ви робите, якщо пацієнт нервує?",
              "Продовжую працювати — час дорогоцінний.",
              "Роблю паузу, пояснюю, що буде далі.",
              "Прошу адміністратора/асистента заспокоїти."), "B"),
        (qfmt("Як ви передаєте інформацію адміністратору після прийому?",
              "Усно, коли є час.",
              "Через нотатку або у CRM.",
              "Не передаю — він сам розбереться."), "B"),
        (qfmt("Що ви робите, якщо пацієнт відмовляється від лікування?",
              "Пропоную дешевший варіант.",
              "Запитую, що саме викликає сумнів.",
              "Просто фіксую відмову."), "B"),
        (qfmt("Як ви ставитесь до відгуків пацієнтів?",
              "Не читаю — зайве нервування.",
              "Читаю і думаю, як покращити комунікацію.",
              "Вважаю, що більшість пишуть емоційно."), "B"),
    ],
    "Адміністратор": [
        (qfmt("Як ви вітаєте пацієнта, якщо він запізнився?",
              "Роблю зауваження — це ж правила.",
              "Спокійно вітаю, пояснюю, що ми все одно приймемо.",
              "Ігнорую ситуацію, щоб не псувати настрій."), "B"),
        (qfmt("Якщо лікар затримується — що ви робите?",
              "Кажу «чекайте».",
              "Повідомляю, скільки часу орієнтовно чекати, і пропоную воду/каву.",
              "А що я можу зробити? Це не моя зона відповідальності."), "B"),
        (qfmt("Як ви реагуєте на скаргу?",
              "Переадресовую керівнику.",
              "Спокійно вислуховую, дякую за відгук і передаю далі.",
              "Виправдовую колегу."), "B"),
        (qfmt("Коли телефонуєте пацієнту після лікування, що ви кажете?",
              "«Як себе почуваєте? Усе добре?»",
              "«Ми нагадуємо про наступний візит.»",
              "Не телефоную — якщо треба, сам подзвонить."), "A"),
        (qfmt("Як завершуєте розмову по телефону?",
              "«До побачення.»",
              "«Гарного дня, чекаємо вас.»",
              "Просто кладу слухавку. Розмова ж завершена."), "B"),
    ],
}

# ==========================
# Sheets helpers
# ==========================
def _open_worksheet():
    """Open worksheet STAT (or configured name). Returns gspread worksheet or None."""
    if not HAS_GS:
        return None
    if not (SHEET_ID and (GCP_SERVICE_ACCOUNT or os.path.isfile("credentials.json"))):
        return None
    try:
        if GCP_SERVICE_ACCOUNT:
            creds = json.loads(GCP_SERVICE_ACCOUNT)
            gc = gspread.service_account_from_dict(creds)
        else:
            gc = gspread.service_account(filename="credentials.json")
        sh = gc.open_by_key(SHEET_ID)
        name = os.getenv("GOOGLE_SHEETS_WORKSHEET_NAME", "STAT")
        try:
            ws = sh.worksheet(name)
        except WorksheetNotFound:
            ws = sh.add_worksheet(name, rows=1000, cols=20)
        return ws
    except Exception:
        return None

async def log_result_async(user_id: int, username: str, role: str, correct: int, errors: int):
    ws = await asyncio.to_thread(_open_worksheet)
    if not ws:
        return
    row = [datetime.utcnow().isoformat(), str(user_id), username or "", role, str(correct), str(errors)]
    async def _append():
        return await asyncio.to_thread(ws.append_row, row, value_input_option="RAW")
    try:
        await retry_async(_append, cfg=TG_RETRY)
    except Exception:
        pass

# ==========================
# Bot logic
# ==========================
app = FastAPI(title="CX Bot")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await safe_reply(update.message,
        text="Оберіть роль, щоб почати 👇",
        reply_markup=ROLE_KB)
    return CHOOSING_ROLE

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await safe_reply(update.message,
        text="Сесію завершено. Щоб почати заново — оберіть роль нижче 👇",
        reply_markup=ROLE_KB)
    return CHOOSING_ROLE

async def choose_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if txt in EXIT_BUTTONS or is_exit(txt):
        return await cancel(update, context)
    if txt not in ROLE_BUTTONS:
        await safe_reply(update.message, text="Оберіть роль, щоб почати 👇", reply_markup=ROLE_KB)
        return CHOOSING_ROLE

    role = "Керівник" if "Керівник" in txt else ("Лікар" if "Лікар" in txt else "Адміністратор")
    context.user_data["role"] = role
    context.user_data["i"] = 0
    context.user_data["errors"] = 0

    q, _ = QUESTIONS[role][0]
    await safe_reply(update.message, text=q, reply_markup=ABC_KB)
    return ASKING

async def ask_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    role = context.user_data["role"]
    i = context.user_data["i"]
    q, _ = QUESTIONS[role][i]
    await safe_reply(update.message, text=q, reply_markup=ABC_KB)
    return ASKING

async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "role" not in context.user_data or "i" not in context.user_data:
        await safe_reply(update.message, text="Спочатку оберіть роль 👇", reply_markup=ROLE_KB)
        return CHOOSING_ROLE

    txt = (update.message.text or "").strip()
    if txt in EXIT_BUTTONS or is_exit(txt):
        return await cancel(update, context)

    if txt not in ABC_BUTTONS:
        await safe_reply(update.message, text="Будь ласка, оберіть A, B або C 👇", reply_markup=ABC_KB)
        return ASKING

    role = context.user_data["role"]
    i = context.user_data["i"]
    correct_letter = QUESTIONS[role][i][1]
    if txt != correct_letter:
        context.user_data["errors"] += 1

    context.user_data["i"] = i + 1
    if context.user_data["i"] < 5:
        return await ask_next(update, context)

    # Final message (unchanged style)
    correct_count = 5 - context.user_data["errors"]
    msg = ("Є сильні сторони і моменти, які можуть зіпсувати враження пацієнтів. Я можу показати, як це виглядає їх очима."
           if context.user_data["errors"] >= 2 else
           "У Вас добрий рівень розуміння клієнтського досвіду. Ви відчуваєте, що сервіс — це більше, ніж просто послуга.")
    final_text = f"{msg}\n\n✅ Ви відповіли правильно на {correct_count} із 5.{_cta_suffix()}\n\nХочете пройти тест у іншій ролі?"

    await safe_reply(update.message, text=final_text, reply_markup=ROLE_KB)

    # Async log
    try:
        user = update.effective_user
        asyncio.create_task(log_result_async(user.id, user.username, role, correct_count, context.user_data['errors']))
    except Exception:
        pass

    context.user_data.clear()
    return CHOOSING_ROLE

async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if txt in EXIT_BUTTONS or is_exit(txt):
        return await cancel(update, context)

    if "role" not in context.user_data or "i" not in context.user_data:
        await safe_reply(update.message, text="Оберіть роль, щоб почати 👇", reply_markup=ROLE_KB)
        return CHOOSING_ROLE

    await safe_reply(update.message, text="Будь ласка, оберіть A, B або C 👇", reply_markup=ABC_KB)
    return ASKING

# ==========================
# FastAPI + PTB
# ==========================
persistence = PicklePersistence(filepath="/tmp/cxbot_state.pickle")
_token = BOT_TOKEN or "000:TEST_DUMMY_TOKEN"  # allows CI checks without secret envs
application: Application = ApplicationBuilder().token(_token).persistence(persistence).build()

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

app = FastAPI(title="CX Bot")

@app.get("/", response_class=PlainTextResponse)
def health():
    return "ok"

@app.get("/set_webhook", response_class=PlainTextResponse)
async def set_webhook():
    if not (APP_BASE_URL and BOT_TOKEN):
        raise HTTPException(status_code=500, detail="APP_BASE_URL or BOT_TOKEN not set")
    url = f"{APP_BASE_URL}/webhook"  # no secret in URL
    await retry_async(application.bot.set_webhook, url=url, cfg=TG_RETRY)
    return f"set_webhook {url}"

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.initialize()
    await application.process_update(update)
    return PlainTextResponse("ok")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)

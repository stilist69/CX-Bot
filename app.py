import os
import json
import asyncio
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

import gspread

# --- Env ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "set-a-secret")
APP_BASE_URL = os.getenv("APP_BASE_URL")  # e.g. https://your-app.koyeb.app
SHEET_ID = os.getenv("SHEET_ID")
GCP_SERVICE_ACCOUNT = os.getenv("GCP_SERVICE_ACCOUNT")  # JSON string

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not SHEET_ID:
    raise RuntimeError("SHEET_ID is required")
if not GCP_SERVICE_ACCOUNT:
    raise RuntimeError("GCP_SERVICE_ACCOUNT JSON is required")

# --- Google Sheets client ---
def _gs_client():
    sa_info = json.loads(GCP_SERVICE_ACCOUNT)
    gc = gspread.service_account_from_dict(sa_info)
    return gc

def log_to_sheet(role: str, correct: int, errors: int, chat_id: str, username: str|None):
    try:
        gc = _gs_client()
        sh = gc.open_by_key(SHEET_ID)
        ws = sh.worksheet("STAT")
        result = "improve" if errors >= 2 else "strong"
        ws.append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            str(chat_id),
            username or "",
            role,
            str(correct),
            str(errors),
            result
        ], value_input_option="USER_ENTERED")
    except Exception as e:
        print("Sheet logging error:", e, flush=True)

# --- Bot content (from blueprint) ---
QUESTIONS = {
    "kerivnyk": [
        {"q":"Як ви дізнаєтесь, що пацієнт залишився задоволеним?","options":["A. Якщо не скаржився — значить, усе добре.","B. Ми періодично запитуємо відгуки.","C. Лікарі самі бачать, коли пацієнт задоволений."],"correct":"B"},
        {"q":"Як часто ви обговорюєте сервіс із командою?","options":["A. Раз на рік на загальних зборах.","B. Коли з’являються проблеми.","C. Регулярно, як частину роботи."],"correct":"C"},
        {"q":"Що для вас важливіше: нові пацієнти чи повторні?","options":["A. Головне — потік нових.","B. Повторні — бо це показник довіри.","C. Обидва варіанти однакові."],"correct":"B"},
        {"q":"Коли востаннє ви проходили шлях пацієнта особисто (дзвінок, запис, прийом)?","options":["A. Ніколи.","B. Давно, але колись робив(-ла).","C. Роблю це регулярно."],"correct":"C"},
        {"q":"Як ви реагуєте на скаргу?","options":["A. Захищаю команду — вони стараються.","B. Розбираюсь спокійно, шукаю, що можна покращити.","C. Ігнорую, якщо пацієнт «важкий»."],"correct":"B"}
    ],
    "likar": [
        {"q":"Як ви пояснюєте пацієнту план лікування?","options":["A. Стисло — без деталей.","B. Детально, простою мовою, показую приклади.","C. Лише тоді, коли питає."],"correct":"B"},
        {"q":"Що ви робите, якщо пацієнт нервує?","options":["A. Продовжую працювати — час дорогоцінний.","B. Роблю паузу, пояснюю, що буде далі.","C. Прошу адміністратора/асистента заспокоїти."],"correct":"B"},
        {"q":"Як ви передаєте інформацію адміністратору після прийому?","options":["A. Усно, коли є час.","B. Через нотатку або у CRM.","C. Не передаю — він сам розбереться."],"correct":"B"},
        {"q":"Що ви робите, якщо пацієнт відмовляється від лікування?","options":["A. Пропоную дешевший варіант.","B. Запитую, що саме викликає сумнів.","C. Просто фіксую відмову."],"correct":"B"},
        {"q":"Як ви ставитесь до відгуків пацієнтів?","options":["A. Не читаю — зайве нервування.","B. Читаю і думаю, як покращити комунікацію.","C. Вважаю, що більшість пишуть емоційно."],"correct":"B"}
    ],
    "admin": [
        {"q":"Як ви вітаєте пацієнта, якщо він запізнився?","options":["A. Роблю зауваження — це ж правила.","B. Спокійно вітаю, пояснюю, що ми все одно приймемо.","C. Ігнорую ситуацію, щоб не псувати настрій."],"correct":"B"},
        {"q":"Якщо лікар затримується — що ви робите?","options":["A. Кажу «чекайте».","B. Повідомляю, скільки часу орієнтовно чекати, і пропоную воду/каву.","C. А що я можу зробити? Це не моя зона відповідальності."],"correct":"B"},
        {"q":"Як ви реагуєте на скаргу?","options":["A. Переадресовую керівнику.","B. Спокійно вислуховую, дякую за відгук і передаю далі.","C. Виправдовую колегу."],"correct":"B"},
        {"q":"Коли телефонуєте пацієнту після лікування, що ви кажете?","options":["A. «Як себе почуваєте? Усе добре?»","B. «Ми нагадуємо про наступний візит.»","C. Не телефоную — якщо треба, сам подзвонить."],"correct":"A"},
        {"q":"Як завершуєте розмову по телефону?","options":["A. «До побачення.»","B. «Гарного дня, чекаємо вас.»","C. Просто кладу слухавку. Розмова ж завершена."],"correct":"B"}
    ]
}

CHOOSING_ROLE, ASKING = range(2)
ROLE_KB = ReplyKeyboardMarkup([["👩‍💼 Керівник","🦷 Лікар","💬 Адміністратор"],["🔚 Завершити"]], resize_keyboard=True)
ABC_KB  = ReplyKeyboardMarkup([["A","B","C"]], resize_keyboard=True)

def role_code_from_text(text: str) -> str:
    if "Керівник" in text: return "kerivnyk"
    if "Лікар" in text: return "likar"
    return "admin"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Привіт! Я — CX Bot.\n"
        "Допоможу тобі побачити клініку очима пацієнтів.\n"
        "Це короткий тест із 5 запитань. Відповідай чесно — тут не буває «поганих» результатів.\n\n"
        "Обери свою роль 👇", reply_markup=ROLE_KB
    )
    return CHOOSING_ROLE

async def choose_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "🔚 Завершити":
        await update.message.reply_text("Гаразд. Побачимось!", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    role = role_code_from_text(text)
    context.user_data["role"] = role
    context.user_data["i"] = 0
    context.user_data["errors"] = 0
    return await ask_next(update, context)

async def ask_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    role = context.user_data["role"]
    i = context.user_data["i"]
    q = QUESTIONS[role][i]
    body = f"{q['q']}\n\nA) {q['options'][0]}\nB) {q['options'][1]}\nC) {q['options'][2]}"
    await update.message.reply_text(body, reply_markup=ABC_KB)
    return ASKING

async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    role = context.user_data["role"]
    i = context.user_data["i"]
    answer = (update.message.text or "").strip().upper()
    if answer not in ("A","B","C"):
        await update.message.reply_text("Будь ласка, обери лише A, B або C 🙂", reply_markup=ABC_KB)
        return ASKING

    correct = QUESTIONS[role][i]["correct"]
    if answer != correct:
        context.user_data["errors"] += 1
    context.user_data["i"] += 1

    if context.user_data["i"] < 5:
        return await ask_next(update, context)

    correct_count = 5 - context.user_data["errors"]
    msg = ("Є сильні сторони і моменти, які можуть зіпсувати враження пацієнтів. Я можу показати, як це виглядає їх очима."
           if context.user_data["errors"] >= 2 else
           "У вас добрий рівень розуміння клієнтського досвіду. Ви відчуваєте, що сервіс — це більше, ніж просто послуга.\nХочете побачити, як ваша клініка виглядає очима пацієнтів?")
    await update.message.reply_text(
        f"{msg}\n\n✅ Ви відповіли правильно на {correct_count} із 5.\n\nХочете пройти тест у іншій ролі?",
        reply_markup=ROLE_KB
    )

    # Log to sheets
    chat = update.effective_user
    log_to_sheet(role, correct_count, context.user_data["errors"], chat.id, chat.username)

    return CHOOSING_ROLE

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Скасовано. До зустрічі!", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# --- FastAPI + PTB integration ---
app = FastAPI(title="CX Bot")

persistence = PicklePersistence(filepath="/tmp/cxbot_state.pickle")
application: Application = ApplicationBuilder().token(BOT_TOKEN).persistence(persistence).build()

conv = ConversationHandler(
    entry_points=[CommandHandler("start", start)],
    states={
        CHOOSING_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_role)],
        ASKING:        [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_answer)],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
    name="cxbot", persistent=True,
)
application.add_handler(conv)

class WebhookModel(BaseModel):
    update_id: int | None = None

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
    await application.bot.set_webhook(url)
    return f"set_webhook {url}"

@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}

@app.on_event("startup")
async def on_startup():
    await application.initialize()
    await application.start()

@app.on_event("shutdown")
async def on_shutdown():
    await application.stop()
    await application.shutdown()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

# -*- coding: utf-8 -*-
import os
import json
import random
import time
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

# ---------- Google Sheets (optional, via ENV) ----------
try:
    import gspread
    from gspread.exceptions import APIError as GSAPIError, WorksheetNotFound
    HAS_GS = True
except Exception:
    HAS_GS = False
    class GSAPIError(Exception): ...
    class WorksheetNotFound(Exception): ...

# ---------- ENV ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_BASE_URL = os.getenv("APP_BASE_URL")                # e.g. https://your-app.run.app
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")        # optional; not enforced

SHEET_ID = os.getenv("SHEET_ID")
GCP_SERVICE_ACCOUNT = os.getenv("GCP_SERVICE_ACCOUNT")  # JSON string of credentials
WORKSHEET_NAME = os.getenv("GOOGLE_SHEETS_WORKSHEET_NAME", "STAT")
CONTACT_USERNAME = os.getenv("CONTACT_USERNAME", "")    # stilist69 (без @)

# ---------- Retry helper ----------
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

# ---------- Keyboards ----------
ROLE_KB = ReplyKeyboardMarkup(
    [["👩‍💼 Керівник"], ["🦷 Лікар"], ["💬 Адміністратор"], ["🔚 Завершити"]],
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
    h = (CONTACT_USERNAME or "").lstrip("@")
    return f"\n\nНапишіть мені в особисті: @{h} — підкажу, як швидко підтягнути сервіс." if h else ""

def is_exit(text: str) -> bool:
    t = (text or "").casefold().strip()
    t = t.replace("🔚", "").strip()
    return t.endswith("завершити")

# ---------- Questions ----------
def qfmt(q, a, b, c):
    ZW = "\u200b"  # невидимий символ
    return f"{q}\n\nA) {a}\n{ZW}\nB) {b}\n{ZW}\nC) {c}"

QUESTIONS: Dict[str, List[Tuple[str, str]]] = {
    "Керівник": [
        (
            qfmt(
                "Як ви вимірюєте якість сервісу в клініці?",
                "Основні орієнтири – фінансовий результат і завантаженість графіка; окремо сервіс не рахуємо.",
                "Комбінуємо фінансові показники, повторні візити і структуровані відгуки пацієнтів.",
                "Покладаємось на відчуття команди: що кажуть лікарі й адміністратори про реакцію пацієнтів.",
            ),
            "B",
        ),
        (
            qfmt(
                "Як ви працюєте з відгуками пацієнтів?",
                "Розбираємо відгуки точково: якщо з'явився яскравий негатив або конфлікт, проговорюємо його й реагуємо адресно.",
                "Маємо процес: збір, аналіз, пріоритизація змін, зворотний зв'язок пацієнту і команді.",
                "Частину відгуків збираємо, передаємо відповідальним, але системного аналізу й плану змін поки немає.",
            ),
            "B",
        ),
        (
            qfmt(
                "Як часто ви переглядаєте сервісні стандарти або скрипти?",
                "Маємо базові домовленості, але без прописаних стандартів – кожен трохи адаптує під себе.",
                "Оновлюємо, коли бачимо, що з'являється багато скарг або просідають показники.",
                "Планово переглядаємо, тестуємо зміни на практиці і навчаємо команду.",
            ),
            "C",
        ),
        (
            qfmt(
                "Що для вас ключовий сигнал ризику в клієнтському досвіді?",
                "Різке падіння завантаженості лікарів, особливо поза сезонними коливаннями.",
                "Зростає частка скасувань і no-show та падає частка повторних візитів.",
                "Зменшується активність пацієнтів у комунікації та зворотному зв'язку, менше відгуків і рекомендацій.",
            ),
            "B",
        ),
        (
            qfmt(
                "Як ви впроваджуєте зміни, пов'язані з сервісом?",
                "Озвучуємо нові правила на загальній зустрічі, далі очікуємо, що команда підхопить.",
                "Тестуємо на невеликій групі, даємо чітку інструкцію, тренуємо, заміряємо ефект.",
                "Спершу обговорюємо ідею з командою, дивимось на готовність, а вже потім поступово переходимо до змін без чітких етапів.",
            ),
            "B",
        ),
    ],

    "Лікар": [
        (
            qfmt(
                "Як ви починаєте консультацію з новим пацієнтом?",
                "Коротко вітаюся і переходжу до клінічних питань, щоб не втрачати час.",
                "Коротко знайомлюсь, з'ясовую очікування, попередній досвід лікування і рівень тривоги.",
                "Уточнюю головну скаргу і запит пацієнта, далі вже в процесі розмови розкриваємо інші деталі.",
            ),
            "B",
        ),
        (
            qfmt(
                "Як ви пояснюєте план лікування?",
                "Пояснюю основний план простими словами, показую схему і орієнтовну суму, деталі залишаю на етап лікування.",
                "Пояснюю варіанти з плюсами і мінусами, ризики, терміни, вартість простими словами, перевіряю розуміння.",
                "Даю пацієнту матеріали (брошура, посилання) і пропоную обговорити питання, якщо щось залишиться незрозумілим.",
            ),
            "B",
        ),
        (
            qfmt(
                "Як реагуєте на страх або тривогу пацієнта в кріслі?",
                "Підтримую пацієнта словами, намагаюся працювати швидше й акуратніше, щоб скоріше зняти напругу.",
                "Уточнюю, чого саме боїться, пояснюю кроки, домовляюсь про стоп-сигнал, даю час адаптуватися.",
                "Пропоную додаткові методи – заспокійливі, седацію чи перенесення візиту, якщо бачу сильний страх.",
            ),
            "B",
        ),
        (
            qfmt(
                "Що ви робите після завершення складного лікування?",
                "Коротко дякую за довіру, відповідаю на запитання, якщо виникли, і передаю пацієнта адміністратору.",
                "Підсумовую зроблене, повторюю ключові рекомендації, з'ясовую, чи є питання, домовляюсь про наступний контакт.",
                "Усні рекомендації даю мінімально, з акцентом на тому, що повний перелік пацієнт отримає в письмовому вигляді від адміністратора.",
            ),
            "B",
        ),
        (
            qfmt(
                "Як ви реагуєте на скаргу, що стосується сервісу, але не якості лікування?",
                "Вислуховую і прошу вирішити це питання на рівні адміністратора чи керівника, бо це їхня зона відповідальності.",
                "Вислуховую, визнаю дискомфорт пацієнта, пояснюю, що передам інформацію керівнику або адміну і проконтролюю, щоб ситуація не повторилась.",
                "Пояснюю, чому процес організований саме так, і пропоную пацієнту залишити офіційний відгук, якщо щось не влаштовує.",
            ),
            "B",
        ),
    ],

    "Адміністратор": [
        (
            qfmt(
                "Як ви починаєте телефонну розмову з новим пацієнтом?",
                "Коротко представляю клініку і питаю, як можу допомогти, без детального вивчення історії.",
                "Представляюся, уточнюю, як до людини звертатися, коротко виявляю запит і очікування.",
                "Дізнаюся, з яким запитом звертається пацієнт, і одразу пропоную найближчі вікна в графіку.",
            ),
            "B",
        ),
        (
            qfmt(
                "Як ви працюєте з перенесеннями або скасуваннями візитів?",
                "Уточнюю, чи зручно перенести на іншу дату, але якщо пацієнту складно – просто скасовую без додаткових розмов.",
                "Уточнюю причину, пропоную найближчу альтернативу, нагадую про важливість лікування.",
                "Записую скасування і прошу пацієнта самостійно звернутися, коли йому буде зручно, без пропозиції альтернатив.",
            ),
            "B",
        ),
        (
            qfmt(
                "Що ви робите, якщо пацієнт чекає довше, ніж обіцяли?",
                "Слідкую за часом і, якщо затримка не критична, просто інколи озвучую, що лікар трохи затримується.",
                "Попереджаю про затримку, орієнтовний час очікування, пропоную воду або каву, за потреби перенесення.",
                "Коли пацієнт заходить до кабінету, обов'язково вибачаюся за затримку і коротко пояснюю причину, без додаткових дій.",
            ),
            "B",
        ),
        (
            qfmt(
                "Як ви завершуєте візит на рецепції?",
                "Озвучую суму, нагадую про наступні кроки (наприклад, контрольний візит), але без детального підсумку прийому.",
                "Підсумовую, що сьогодні було зроблено, нагадую ключові рекомендації лікаря, узгоджую наступний візит.",
                "Видаю чек і, за потреби, друковані рекомендації, відповідаю на запитання, якщо вони виникають.",
            ),
            "B",
        ),
        (
            qfmt(
                "Як ви фіксуєте і передаєте запити або зауваження пацієнтів команді?",
                "Фіксую ключові моменти в особистих нотатках чи месенджері й передаю їх відповідній людині усно або в чаті.",
                "Фіксую ключові деталі в CRM або картці пацієнта і передаю відповідальній особі, повертаюсь із зворотним зв'язком до пацієнта, якщо обіцяла.",
                "Передаю команді тільки ті зауваження, які повторюються або виглядають серйозними, дрібні відмічаю для себе.",
            ),
            "B",
        ),
    ],
}
# ---------- Sheets helpers ----------
def _open_worksheet():
    if not HAS_GS or not SHEET_ID:
        return None
    try:
        if GCP_SERVICE_ACCOUNT:
            creds = json.loads(GCP_SERVICE_ACCOUNT)
            gc = gspread.service_account_from_dict(creds)
        elif os.path.isfile("credentials.json"):
            gc = gspread.service_account(filename="credentials.json")
        else:
            return None
        sh = gc.open_by_key(SHEET_ID)
        try:
            ws = sh.worksheet(WORKSHEET_NAME)
        except WorksheetNotFound:
            ws = sh.add_worksheet(WORKSHEET_NAME, rows=1000, cols=20)
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

# ---------- Bot logic ----------
app = FastAPI(title="CX Bot")

@app.on_event("startup")
async def _startup():
    await application.initialize()
    if application.job_queue:
        application.job_queue.start()
    print("PTB application initialized")

@app.on_event("shutdown")
async def _shutdown():
    if application.job_queue:
        application.job_queue.stop()
    await application.shutdown()
    print("PTB application shutdown")

def _dedupe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = getattr(update, "update_id", None)
    if uid is None:
        return False
    last = context.user_data.get("_last_update_id")
    if last == uid:
        return True
    context.user_data["_last_update_id"] = uid
    return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _dedupe(update, context):  # захист від повторів
        return CHOOSING_ROLE

    context.user_data.clear()
    welcome = (
        "Привіт! Я — CX Bot.\n"
        "Допоможу Вам побачити клініку очима пацієнтів.\n"
        "Це короткий тест із 5 запитань. Відповідайте чесно — тут не буває «поганих» результатів.\n\n"
        "Оберіть свою роль 👇"
    )
    await safe_reply(update.message, text=welcome, reply_markup=ROLE_KB)
    return CHOOSING_ROLE

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _dedupe(update, context):
        return CHOOSING_ROLE

    context.user_data.clear()
    await safe_reply(update.message, text="Готово. Можете пройти мікроаудит ще раз — просто оберіть роль нижче 👇", reply_markup=ROLE_KB)
    return CHOOSING_ROLE

async def choose_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _dedupe(update, context):
        return CHOOSING_ROLE

    txt = (update.message.text or "").strip()
    if txt in EXIT_BUTTONS or is_exit(txt):
        return await cancel(update, context)
    if txt not in ROLE_BUTTONS:
        return await start(update, context)
    role = "Керівник" if "Керівник" in txt else ("Лікар" if "Лікар" in txt else "Адміністратор")
    context.user_data["role"] = role
    context.user_data["i"] = 0
    context.user_data["errors"] = 0
    context.user_data.pop("last_hint_ts", None)
    q, _ = QUESTIONS[role][0]
    await safe_reply(update.message, text=q, reply_markup=ABC_KB)
    return ASKING

async def ask_again(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _dedupe(update, context):
        return ASKING
    now = time.time()
    last = context.user_data.get("last_hint_ts", 0.0)
    if now - last >= 2.0:
        await safe_reply(update.message, text="Будь ласка, оберіть A, B або C 👇", reply_markup=ABC_KB)
        context.user_data["last_hint_ts"] = now
    return ASKING

async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _dedupe(update, context):
        return ASKING
    if "role" not in context.user_data or "i" not in context.user_data:
        return await start(update, context)

    txt = (update.message.text or "").strip()
    if txt in EXIT_BUTTONS or is_exit(txt):
        return await cancel(update, context)
    if txt not in ABC_BUTTONS:
        return await ask_again(update, context)

    role = context.user_data["role"]
    i = context.user_data["i"]
    correct_letter = QUESTIONS[role][i][1]
    if txt != correct_letter:
        context.user_data["errors"] += 1

    context.user_data["i"] = i + 1
    if context.user_data["i"] < 5:
        q, _ = QUESTIONS[role][context.user_data["i"]]
        await safe_reply(update.message, text=q, reply_markup=ABC_KB)
        return ASKING

    # Final message unchanged (+ optional CTA)
    correct_count = 5 - context.user_data["errors"]
    msg = ("Є сильні сторони і моменти, які можуть зіпсувати враження пацієнтів. Я можу показати, як це виглядає їх очима."
           if context.user_data["errors"] >= 2 else
           "У Вас добрий рівень розуміння клієнтського досвіду. Ви відчуваєте, що сервіс — це більше, ніж просто послуга.")
    final_text = f"{msg}\n\n✅ Ви відповіли правильно на {correct_count} із 5.{_cta_suffix()}\n\nХочете пройти тест у іншій ролі?"
    await safe_reply(update.message, text=final_text, reply_markup=ROLE_KB)

    try:
        user = update.effective_user
        await log_result_async(user.id, user.username, role, correct_count, context.user_data['errors'])
    except Exception:
        pass

    context.user_data.clear()
    # Завершуємо розмову: далі будь-яке натискання спрацює як новий вхід через entry_points
    return ConversationHandler.END

# ---------- FastAPI + PTB ----------
persistence = PicklePersistence(filepath="/tmp/cxbot_state.pickle")
_token = BOT_TOKEN or "000:TEST_DUMMY_TOKEN"
application: Application = ApplicationBuilder().token(_token).persistence(persistence).build()

# Strict per-state handlers (no global TEXT handler)
exit_handler = MessageHandler(filters.Regex(r"^(🔚 Завершити|Завершити)$"), cancel)
role_handler = MessageHandler(filters.Regex(r"^(👩‍💼 Керівник|🦷 Лікар|💬 Адміністратор)$"), choose_role)
abc_handler  = MessageHandler(filters.Regex(r"^(A|B|C)$"), handle_answer)
fallback_role = MessageHandler(filters.TEXT & ~filters.COMMAND, start)
fallback_asking = MessageHandler(filters.TEXT & ~filters.COMMAND, ask_again)

conv = ConversationHandler(
    entry_points=[
        CommandHandler("start", start),
        exit_handler,   # "🔚 Завершити" теж може стати входом
        role_handler,   # натиснув роль – можна стартувати навіть з нуля
        abc_handler,    # навіть якщо тисне A/B/C зі старої клавіатури
    ],
    states={
        CHOOSING_ROLE: [exit_handler, role_handler, fallback_role],
        ASKING:        [exit_handler, abc_handler,  fallback_asking],
    },
    fallbacks=[exit_handler],
    name="cxbot",
    persistent=True,
)
application.add_handler(conv)

@app.get("/", response_class=PlainTextResponse)
def health():
    return "ok"

@app.get("/set_webhook", response_class=PlainTextResponse)
async def set_webhook():
    if not (APP_BASE_URL and BOT_TOKEN):
        raise HTTPException(status_code=500, detail="APP_BASE_URL or BOT_TOKEN not set")
    url = f"{APP_BASE_URL}/webhook/{WEBHOOK_SECRET}" if WEBHOOK_SECRET else f"{APP_BASE_URL}/webhook"
    await retry_async(application.bot.set_webhook, url=url, cfg=TG_RETRY)
    return f"set_webhook {url}"

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    print("Webhook update:", data.get("update_id"), "message:", data.get("message", {}).get("text"))
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return PlainTextResponse("ok")

@app.post("/webhook/{_secret}")
async def telegram_webhook_secret(_secret: str, request: Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return PlainTextResponse("ok")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    # ГОЛОВНЕ: модуль і об'єкт правильні
    uvicorn.run("app:app", host="0.0.0.0", port=port)

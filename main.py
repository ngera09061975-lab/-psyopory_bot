"""
Бот Надежды Герасимовой — Лид-магнит + Прогрев + Оплата + Автовыдача доступа
Stack: aiogram 3.27, APScheduler, SQLite, python-dotenv

КАК ПОЛЬЗОВАТЬСЯ:
1. Не меняйте ничего в этом файле, если не уверены
2. Тексты прогрева меняются в переменной TOUCH_TEXTS ниже (поиск по Ctrl+F)
3. Аудио кладите в папку audio/ с именами: lead_magnet.mp3, touch_2.mp3, touch_4.mp3
"""
import os
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    FSInputFile, LabeledPrice, PreCheckoutQuery
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from dotenv import load_dotenv

load_dotenv()

# ─── НАСТРОЙКИ (берутся из файла .env) ─────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
CHANNEL_LINK = os.getenv("CHANNEL_LINK")
COURSE_CHANNEL_ID = os.getenv("COURSE_CHANNEL_ID")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN")
COURSE_PRICE = int(os.getenv("COURSE_PRICE", "300000"))

AUDIO_DIR = Path("audio")
LEAD_MAGNET_AUDIO = AUDIO_DIR / "lead_magnet.mp3"

# ─── ЛОГИ ───────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── БАЗА ДАННЫХ ───────────────────────────────────────
DB_PATH = Path("data/bot.db")
DB_PATH.parent.mkdir(exist_ok=True)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at TEXT,
            subscribed INTEGER DEFAULT 0,
            got_lead_magnet INTEGER DEFAULT 0,
            touch_count INTEGER DEFAULT 0,
            last_touch_at TEXT,
            paid INTEGER DEFAULT 0,
            paid_at TEXT,
            invite_link_sent INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def add_user(user_id: int, username: str, first_name: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO users (user_id, username, first_name, joined_at) VALUES (?, ?, ?, ?)",
        (user_id, username, first_name, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def set_subscribed(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET subscribed = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def set_lead_magnet(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET got_lead_magnet = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def increment_touch(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE users SET touch_count = touch_count + 1, last_touch_at = ? WHERE user_id = ?",
        (datetime.now().isoformat(), user_id),
    )
    conn.commit()
    conn.close()


def set_paid(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE users SET paid = 1, paid_at = ? WHERE user_id = ?",
        (datetime.now().isoformat(), user_id),
    )
    conn.commit()
    conn.close()


def set_invite_sent(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET invite_link_sent = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_user(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row


# ─── БОТ ────────────────────────────────────────────────
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# ─── СОСТОЯНИЯ (шаги воронки) ─────────────────────────
class UserFlow(StatesGroup):
    welcome = State()
    subscribe_gate = State()
    lead_magnet = State()
    warming = State()
    payment = State()
    finished = State()


# ─── КНОПКИ ────────────────────────────────────────────
def kb_subscribe():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="🎧 Забрать практику «Согласие с собой»", callback_data="check_sub")]
    ])


def kb_buy_course():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💜 Купить «Карту тревоги» — 3000 ₽", callback_data="buy_course")],
        [InlineKeyboardButton(text="🎁 Записаться на бесплатную диагностику", callback_data="free_diag")]
    ])


# ─── ОБРАБОТЧИКИ ────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    add_user(user_id, message.from_user.username, message.from_user.first_name)
    row = get_user(user_id)
    if row and row[8] == 1:
        await message.answer("У тебя уже есть доступ к «Карте тревоги» 💜")
        await send_course_access(user_id)
        return
    await state.set_state(UserFlow.welcome)
    await message.answer(
        "Ты на месте 💜\n\n"
        "Меня зовут Надежда Герасимова, я психолог. Уже 16 лет помогаю людям, "
        "которые живут с тревогой и паническими атаками, снова почувствовать опору внутри себя.\n\n"
        "Через минуту у тебя будет практика «Согласие с собой». 10 минут тишины и внутреннего равновесия, "
        "к которым можно возвращаться в любой момент, когда накрывает.\n\n"
        "Перед этим один короткий шаг 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Перейти к подписке →", callback_data="to_subscribe")]
        ])
    )


@dp.callback_query(F.data == "to_subscribe")
async def to_subscribe(call: CallbackQuery, state: FSMContext):
    await state.set_state(UserFlow.subscribe_gate)
    await call.message.answer(
        "Я веду терапевтичный канал, где делюсь практиками, разборами и поддержкой "
        "для тех, кто устал тревожиться.\n\n"
        "Подпишись, чтобы быть рядом, и сразу забирай практику 💜",
        reply_markup=kb_subscribe(),
    )
    await call.answer()


@dp.callback_query(F.data == "check_sub")
async def check_subscription(call: CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        if member.status in ("member", "administrator", "creator"):
            set_subscribed(user_id)
            await state.set_state(UserFlow.lead_magnet)
            await send_lead_magnet(call.message.chat.id)
        else:
            await call.answer("Сначала подпишись на канал, пожалуйста 💜", show_alert=True)
    except Exception as e:
        logger.error(f"Ошибка проверки подписки: {e}")
        await call.answer("Не удалось проверить подписку. Попробуй ещё раз.", show_alert=True)


async def send_lead_magnet(chat_id: int):
    await bot.send_message(
        chat_id,
        "Держи 🤍\n\n"
        "🎧 «Согласие с собой» — практика четырёх опор · 10 минут тишины внутри"
    )
    if LEAD_MAGNET_AUDIO.exists():
        audio = FSInputFile(LEAD_MAGNET_AUDIO)
        await bot.send_audio(chat_id, audio)
    else:
        await bot.send_message(chat_id, "[здесь будет аудиофайл — положи его в папку audio/lead_magnet.mp3]")
    await bot.send_message(
        chat_id,
        "Найди место, где тебя минут 15 никто не потревожит. Надень наушники. "
        "И просто позволь себе эти 10 минут, ничего не нужно делать, только слушать.\n\n"
        "После напиши мне сюда одним словом, как ты 🤍 Мне правда важно."
    )
    schedule_warming(chat_id)


# ═══════════════════════════════════════════════════════
#  ТЕКСТЫ ПРОГРЕВА — МЕНЯЙТЕ ИХ ЗДЕСЬ
#  Чтобы найти: нажмите Ctrl+F и введите TOUCH_TEXTS
# ═══════════════════════════════════════════════════════

TOUCH_TEXTS = {
    1: (
        "Ну как ты?\n\n"
        "Если получилось выделить эти десять минут только на себя — это уже не мелочь. "
        "Знаешь, сколько женщин так и не находят их годами? Всё для всех, а для себя — потом, когда-нибудь, не сейчас.\n\n"
        "А ты нашла. Сегодня.\n\n"
        "Побудь пока просто с этим. Ничего не надо делать. Мы только начали."
    ),
    2: (
        "Привет. Это снова Надежда.\n\n"
        "Хочу сегодня сказать тебе одну вещь. Простую, но она в своё время многое для меня перевернула.\n\n"
        "Тревога — это не ты.\n\n"
        "Смотри. Мы привыкли говорить: «я тревожная», «я такая уродилась». "
        "И живём с этим, как с приговором. А приговор — это же навсегда.\n\n"
        "Но тревога — это не характер. Это состояние. То, во что ты входишь — и из чего можно выйти. "
        "Она приходит и уходит. У неё есть начало и есть конец. А раз так — значит, это не ты.\n\n"
        "Представь охранника, которого когда-то давно поставили на пост. Сказали: стой, смотри в оба. "
        "И он стоит. Год стоит, три, десять. Вокруг давно всё спокойно, а он всё держит оборону, "
        "потому что приказ никто не отменил.\n\n"
        "Вот твоя тревога — это такой охранник. Слишком преданный. Слишком старательный. "
        "Он не враг тебе — наоборот, он тебя когда-то спас. Просто до сих пор не понял, что война давно кончилась.\n\n"
        "И знаешь, что самое важное? Раз это не ты, а вот такая отдельная твоя часть — с ней можно познакомиться. "
        "Разглядеть её. Понять, чего она боится. И потихоньку договориться. Не воевать с собой, а разобраться.\n\n"
        "Вот с этого всё и начинается. С того, чтобы перестать быть своей тревогой — и начать на неё смотреть.\n\n"
        "Ладно. Побудь пока с этой мыслью. Она важнее, чем кажется."
    ),
    3: (
        "Вчера я говорила, что тревога — это не ты, а отдельное состояние.\n\n"
        "А теперь — самое интересное. У этого состояния есть устройство. Его можно разобрать по частям, как механизм.\n\n"
        "Их шесть.\n\n"
        "Кто ты в тревоге — там ведь выходит вперёд будто и не взрослая ты, а кто-то помладше, поиспуганнее. "
        "Что ты в этот момент чувствуешь — и это почти никогда не один только страх, там целый клубок. "
        "Что делаешь и чего, наоборот, не делаешь. Во что начинаешь верить — про себя, про людей, про мир. "
        "В каких ситуациях тревога включается. И рядом с кем становится громче, а рядом с кем — отпускает.\n\n"
        "Шесть частей. И вот что происходит, когда ты видишь их все сразу: туман расходится.\n\n"
        "Пока тревога — это сплошное «мне просто плохо и я не понимаю почему» — с ней ничего не сделать. "
        "Не ухватить. А когда она разложена на понятные детали — всё, она уже не туман. Она карта. "
        "А по карте, в отличие от тумана, можно идти.\n\n"
        "Именно это мы и делаем в моём практикуме — «Карта тревоги». Но до него ещё дойдём, не спешу."
    ),
    4: (
        "Привет, это Надежда.\n\n"
        "Расскажу тебе сегодня про одну женщину. Историй таких у меня за шестнадцать лет — сотни, "
        "так что имя не важно, да и узнаешь ты в ней, скорее всего, не её.\n\n"
        "Пришла ко мне женщина. Сильная. Знаешь, из тех, на ком всё держится — дом, дети, работа, стареющие родители, "
        "ещё и подруги все со своими бедами к ней же. Кремень. Со стороны — да у неё всё под контролем, железная леди.\n\n"
        "А приходит и говорит: со мной что-то не так. По ночам накрывает так, что сердце выскакивает и дышать нечем. "
        "Днём держусь, а ночью разваливаюсь. И главное — стыдно. Вроде взрослая баба, чего это я.\n\n"
        "Мы начали разбираться. И знаешь, что вылезло? Что вот эта вся её сила, весь этот контроль, всё «я сама справлюсь» — "
        "это не взрослая женщина. Это маленькая девочка. Лет шести. Которая когда-то очень рано поняла: рассчитывать не на кого, "
        "надо тянуть самой, иначе рухнет. И с тех пор — тянет. Всю жизнь. За двоих, за пятерых, за всех.\n\n"
        "И вот эта девочка внутри — она страшно устала. Просто ей нельзя было признаться в этом даже себе. "
        "Потому что если признаешься, если на секунду отпустишь — а вдруг всё развалится?\n\n"
        "И когда эта женщина впервые её там, внутри, увидела — эту свою маленькую, замученную, "
        "которая до сих пор держит на себе весь мир, — она заплакала. Первый раз за очень долгое время. "
        "Не от горя. От того, что наконец-то поняла, кто там, за всем этим напряжением, стоит.\n\n"
        "Это был поворотный момент. Потому что тревога перестала быть врагом, с которым надо бороться. "
        "Она стала девочкой, которую надо наконец пожалеть и разгрузить.\n\n"
        "Я это к чему. Если ты сейчас узнала в этом хоть немного себя — знай: с тобой не «что-то не так». "
        "Просто внутри есть та, что давно устала. И ей нужна не борьба. Ей нужна ты — взрослая, тёплая, на её стороне.\n\n"
        "И этому можно научиться. Правда можно."
    ),
    5: (
        "Все эти дни мы с тобой потихоньку шли к одному.\n\n"
        "Ты уже знаешь: тревога — это не ты. У неё есть устройство, шесть частей. "
        "И внутри почти всегда — та самая маленькая, которой когда-то было страшно и одиноко.\n\n"
        "Так вот, всё это можно собрать. Разложить свою тревогу по полочкам и наконец увидеть её целиком — свою, а не «как в книжках».\n\n"
        "Для этого я и сделала практикум «Карта тревоги».\n\n"
        "Четыре урока. Не лекции — мы там не теорию слушаем, а прямо по шагам собираем твою личную карту. "
        "Ты своими глазами увидишь ту маленькую внутри, услышишь свои дежурные фразы, найдёшь свои триггеры и поймёшь, "
        "рядом с кем тебе легче, а рядом с кем тяжелее.\n\n"
        "Внутри:\n"
        "— четыре урока-практики, проходишь в своём темпе;\n"
        "— «Дневник наблюдения за тревогой» — чтобы замечать её уже в жизни, а не только на уроке;\n"
        "— доступ остаётся у тебя, возвращаться можно сколько угодно.\n\n"
        "К концу ты будешь знать свою тревогу в лицо. А то, что знаешь в лицо, уже не пугает так, как безымянное."
    ),
    6: (
        "Знаешь, что выматывает в тревоге сильнее всего?\n\n"
        "Не сама тревога даже. А то, что не понимаешь, что с тобой. "
        "Вот это «мне плохо, а почему — не ясно». Живёшь как в тумане и не знаешь, с какой стороны к себе подойти. "
        "Это и высасывает силы.\n\n"
        "«Карта тревоги» именно туман и убирает.\n\n"
        "Я не буду обещать тебе, что после практикума ты станешь спокойной и безмятежной за один вечер. "
        "Так не бывает, и я не хочу тебе врать. Но одно точно: ты перестанешь быть заложницей непонятного. "
        "Ты начнёшь видеть. А то, что видишь, уже не управляет тобой само по себе, исподтишка.\n\n"
        "Если внутри есть отклик — не откладывай на «потом разберусь». "
        "Ты же знаешь, как это бывает: «потом» тихо превращается в «никогда», а тревога тем временем спокойно продолжает жить твою жизнь за тебя.\n\n"
        "И что бы ты ни решила — помни: ты не тревога. Тревога — не ты. Это просто часть тебя, которой очень нужна твоя забота."
    ),
}

# Аудио для касаний: положите файлы в папку audio/
# Если файла нет — бот отправит только текст, ничего не сломается
TOUCH_AUDIO = {
    2: AUDIO_DIR / "touch_2.mp3",
    4: AUDIO_DIR / "touch_4.mp3",
}


def schedule_warming(user_id: int):
    """Планируем 6 касаний: 2 раза в день, утро/день"""
    now = datetime.now()
    t1 = now + timedelta(hours=2, minutes=30)
    t2 = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0)
    t3 = (now + timedelta(days=2)).replace(hour=14, minute=0, second=0)
    t4 = (now + timedelta(days=3)).replace(hour=10, minute=0, second=0)
    t5 = (now + timedelta(days=4)).replace(hour=14, minute=0, second=0)
    t6 = (now + timedelta(days=6)).replace(hour=10, minute=0, second=0)

    times = [t1, t2, t3, t4, t5, t6]
    for i, t in enumerate(times, 1):
        scheduler.add_job(
            send_touch,
            trigger=DateTrigger(run_date=t),
            args=(user_id, i),
            id=f"touch_{user_id}_{i}",
            replace_existing=True,
        )
    logger.info(f"Запланировано 6 касаний для user_id={user_id}")


async def send_touch(user_id: int, touch_num: int):
    row = get_user(user_id)
    if not row or row[6] >= touch_num or row[8] == 1:
        return
    text = TOUCH_TEXTS.get(touch_num, "")
    if not text:
        return
    increment_touch(user_id)
    if touch_num >= 5:
        await bot.send_message(user_id, text, reply_markup=kb_buy_course())
    else:
        await bot.send_message(user_id, text)
    audio_path = TOUCH_AUDIO.get(touch_num)
    if audio_path and audio_path.exists():
        await bot.send_audio(user_id, FSInputFile(audio_path))
    logger.info(f"Касание {touch_num} отправлено user_id={user_id}")


# ─── ОПЛАТА ─────────────────────────────────────────────

@dp.callback_query(F.data == "buy_course")
async def send_invoice_handler(call: CallbackQuery):
    user_id = call.from_user.id
    prices = [LabeledPrice(label="Карта тревоги", amount=COURSE_PRICE)]
    await bot.send_invoice(
        chat_id=user_id,
        title="Практикум «Карта тревоги»",
        description="4 урока-практики + Дневник наблюдения за тревогой. Доступ навсегда.",
        payload=f"course_{user_id}",
        provider_token=PAYMENT_PROVIDER_TOKEN,
        currency="RUB",
        prices=prices,
        start_parameter="buy_course",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить 3000 ₽", pay=True)],
            [InlineKeyboardButton(text="🎁 Сначала бесплатная диагностика", callback_data="free_diag")]
        ])
    )
    await call.answer()


@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout.id, ok=True)


@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    user_id = message.from_user.id
    set_paid(user_id)
    await message.answer(
        "Оплата прошла успешно! 💜\n\n"
        "Сейчас вышлю тебе доступ к закрытому каналу с практикумом «Карта тревоги». "
        "Переходи по ссылке — она одноразовая и действует 1 час."
    )
    await send_course_access(user_id)
    if ADMIN_ID:
        await bot.send_message(
            ADMIN_ID,
            f"💰 Новая покупка!\n"
            f"Пользователь: {message.from_user.full_name} (@{message.from_user.username})\n"
            f"ID: {user_id}"
        )


async def send_course_access(user_id: int):
    try:
        invite_link = await bot.create_chat_invite_link(
            chat_id=COURSE_CHANNEL_ID,
            expire_date=datetime.now() + timedelta(hours=1),
            member_limit=1
        )
        set_invite_sent(user_id)
        await bot.send_message(
            user_id,
            f"Вот твоя ссылка 👇\n\n"
            f"{invite_link.invite_link}\n\n"
            f"Нажми, чтобы войти в канал с курсом. Если не успеешь за час — напиши мне, вышлю новую."
        )
    except Exception as e:
        logger.error(f"Ошибка создания ссылки: {e}")
        await bot.send_message(
            user_id,
            "Не удалось создать ссылку автоматически. Я свяжусь с тобой вручную в течение часа 💜"
        )
        if ADMIN_ID:
            await bot.send_message(ADMIN_ID, f"⚠️ Ошибка выдачи доступа user_id={user_id}: {e}")


@dp.callback_query(F.data == "free_diag")
async def free_diagnostic(call: CallbackQuery):
    await call.message.answer(
        "Спасибо за интерес 💜\n\n"
        "Я свяжусь с тобой в ближайшие 24 часа, чтобы договориться о времени "
        "бесплатной диагностики (видеозвон, 20-30 минут).\n\n"
        "Напиши, пожалуйста, удобное время и свой контакт (Telegram/телефон), если хочешь."
    )
    await call.answer()


@dp.message(F.text)
async def any_text(message: Message, state: FSMContext):
    current_state = await state.get_state()
    user_id = message.from_user.id
    if current_state == UserFlow.lead_magnet.state:
        await message.answer(
            "Спасибо, что поделилась 🤍\n\n"
            "Я читаю каждое сообщение. В ближайшие дни я буду присылать тебе "
            "небольшие тёплые послания — то, что помогает моим клиенткам. "
            "Без спама, только тогда, когда это может быть важно."
        )
        await state.set_state(UserFlow.warming)
        set_lead_magnet(user_id)
    else:
        await message.answer(
            "Я с тобой 💜 Если хочешь что-то спросить или поделиться — пиши. "
            "Я отвечаю лично, хоть и не мгновенно."
        )


# ─── ЗАПУСК ────────────────────────────────────────────

async def main():
    init_db()
    scheduler.start()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

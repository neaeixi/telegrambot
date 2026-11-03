"""
مُذكّر الصلاة للمجموعات — الرياض
================================
• تذكير تلقائي بالصلوات الخمس حسب توقيت الرياض (تقويم أم القرى) + الوتر 10 م.
• منشن لجميع الأعضاء المسجّلين لدى البوت (على دفعات).
• أزرار تأكيد (صليت / سأصلي الآن) مع قائمة حيّة بمن أكّد.
• بعد 30 دقيقة: تنبيه في المجموعة ثم 15 رسالة خاصة (واحدة كل دقيقة)
  لكل عضو لم يضغط «صليت».
• /times مواقيت اليوم — /members حالة التسجيل — /test تجربة فورية.

المتطلبات:
    pip install "python-telegram-bot[job-queue]>=21" httpx tzdata
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from zoneinfo import ZoneInfo

import httpx
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    User,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================== الإعدادات ==============================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DB_PATH = os.environ.get("DB_PATH", "prayer_bot_v2.db")

CALC_METHOD = 4  # أم القرى
CITY = "Riyadh"
COUNTRY = "SA"
TZ = ZoneInfo("Asia/Riyadh")

WITR_TIME = dtime(22, 0)
CATCHUP_WINDOW_SECONDS = 600

FOLLOWUP_AFTER_MIN = int(os.environ.get("FOLLOWUP_AFTER_MIN", 30))      # بدء المتابعة
REPEAT_COUNT = int(os.environ.get("REPEAT_COUNT", 15))                  # عدد الرسائل الخاصة
REPEAT_INTERVAL_SEC = int(os.environ.get("REPEAT_INTERVAL_SEC", 60))    # الفاصل بينها
RECORD_MAX_AGE_MIN = 240
MENTION_CHUNK = 5            # عدد المنشن في كل رسالة (ليصل الإشعار للجميع)
STOP_ON_SOON = False         # True = زر «سأصلي الآن» يوقف الرسائل الخاصة أيضًا

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("prayer-bot")

# ============================== النصوص ==============================
PRAYERS = ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]
SCHEDULE = PRAYERS + ["Witr"]
PRAYER_NAMES = {
    "Fajr": "الفجر", "Dhuhr": "الظهر", "Asr": "العصر",
    "Maghrib": "المغرب", "Isha": "العشاء", "Witr": "الوتر",
}
PRAYER_ICONS = {
    "Fajr": "🌅", "Sunrise": "🌄", "Dhuhr": "☀️", "Asr": "🌤",
    "Maghrib": "🌇", "Isha": "🌃", "Witr": "🌙",
}
HADITHS_GENERAL = [
    "قال ﷺ: «أحبُّ الأعمال إلى الله الصلاةُ على وقتها»",
    "قال ﷺ: «الصَّلَوَاتُ الخَمْسُ، وَالجُمُعَةُ إِلَى الجُمُعَةِ، كَفَّارَةٌ لِمَا بَيْنَهُنَّ مَا لَمْ تُغْشَ الكَبَائِرُ»",
]
HADITHS_BY_PRAYER = {
    "Fajr": [
        "قال ﷺ: «رَكْعَتَا الفَجْرِ خَيْرٌ مِنَ الدُّنْيَا وَمَا فِيهَا»",
        "قال ﷺ: «من صلّى البَردين دخل الجنة» (الفجر والعصر)",
        "قال ﷺ: «مَنْ صَلَّى الصُّبْحَ فَهُوَ فِي ذِمَّةِ اللَّهِ»",
    ],
    "Asr": [
        "قال ﷺ: «من صلّى البَردين دخل الجنة» (الفجر والعصر)",
        "قال ﷺ: «الَّذِي تَفُوتُهُ صَلَاةُ العَصْرِ كَأَنَّمَا وُتِرَ أَهْلَهُ وَمَالَهُ»",
    ],
    "Isha": ["قال ﷺ: «مَنْ صَلَّى العِشَاءَ فِي جَمَاعَةٍ فَكَأَنَّمَا قَامَ نِصْفَ اللَّيْلِ»"],
    "Witr": [
        "قال ﷺ: «إنَّ اللَّهَ وِتْرٌ يُحِبُّ الوِتْرَ فَأَوْتِرُوا»",
        "قال ﷺ: «اجْعَلُوا آخِرَ صَلَاتِكُمْ بِاللَّيْلِ وِتْرًا»",
    ],
}
AR_WEEKDAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
AR_MONTHS = [
    "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
    "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر",
]
LINE = "━━━━━━━━━━━━━━━━━━"
_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def ar_digits(s) -> str:
    return str(s).translate(_DIGITS)


def fmt_time(t: dtime) -> str:
    h = t.hour % 12 or 12
    return f"{ar_digits(f'{h}:{t.minute:02d}')} {'ص' if t.hour < 12 else 'م'}"


def fmt_duration(delta: timedelta) -> str:
    total = max(int(delta.total_seconds() // 60), 0)
    h, m = divmod(total, 60)
    if h and m:
        return f"{ar_digits(h)} س و {ar_digits(m)} د"
    if h:
        return f"{ar_digits(h)} س"
    return f"{ar_digits(m)} د"


def now_riyadh() -> datetime:
    return datetime.now(TZ)


# ============================== قاعدة البيانات ==============================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS chats (chat_id INTEGER PRIMARY KEY, active INTEGER DEFAULT 1)")
        c.execute(
            """CREATE TABLE IF NOT EXISTS prayer_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL, prayer TEXT NOT NULL, day TEXT NOT NULL,
                sent_at TEXT NOT NULL, message_id INTEGER, body TEXT NOT NULL,
                followup_sent INTEGER DEFAULT 0, repeat_started INTEGER DEFAULT 0,
                repeat_count INTEGER DEFAULT 0, last_repeat_at TEXT, last_repeat_msg INTEGER,
                closed INTEGER DEFAULT 0,
                UNIQUE(chat_id, prayer, day)
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS responses (
                record_id INTEGER NOT NULL, user_id INTEGER NOT NULL, user_name TEXT,
                status TEXT NOT NULL, updated_at TEXT,
                PRIMARY KEY (record_id, user_id)
            )"""
        )
        # أعضاء المجموعات الذين رآهم البوت
        c.execute(
            """CREATE TABLE IF NOT EXISTS members (
                chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, name TEXT,
                PRIMARY KEY (chat_id, user_id)
            )"""
        )
        # من فعّل الرسائل الخاصة مع البوت
        c.execute(
            """CREATE TABLE IF NOT EXISTS dm_users (
                user_id INTEGER PRIMARY KEY, name TEXT, active INTEGER DEFAULT 1
            )"""
        )
        # عدّاد الرسائل الخاصة لكل شخص في كل تذكير
        c.execute(
            """CREATE TABLE IF NOT EXISTS dm_progress (
                record_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                count INTEGER DEFAULT 0, last_at TEXT,
                PRIMARY KEY (record_id, user_id)
            )"""
        )


def set_chat_active(chat_id: int, active: bool) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO chats(chat_id, active) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET active=excluded.active",
            (chat_id, 1 if active else 0),
        )


_known: dict[tuple[int, int], str] = {}


def remember_member(chat_id: int, user: User) -> None:
    if user.is_bot:
        return
    if _known.get((chat_id, user.id)) == user.first_name:
        return
    _known[(chat_id, user.id)] = user.first_name
    with db() as c:
        c.execute(
            "INSERT INTO members(chat_id, user_id, name) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET name=excluded.name",
            (chat_id, user.id, user.first_name),
        )


def forget_member(chat_id: int, user_id: int) -> None:
    _known.pop((chat_id, user_id), None)
    with db() as c:
        c.execute("DELETE FROM members WHERE chat_id=? AND user_id=?", (chat_id, user_id))


def set_dm_user(user: User, active: bool = True) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO dm_users(user_id, name, active) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, active=excluded.active",
            (user.id, user.first_name, 1 if active else 0),
        )


# ============================== مواقيت الصلاة ==============================
_cache: dict[str, dict] = {}
_fail_until: datetime | None = None


async def fetch_timings(d: date) -> dict:
    key = d.isoformat()
    if key in _cache:
        return _cache[key]
    url = f"https://api.aladhan.com/v1/timingsByCity/{d.strftime('%d-%m-%Y')}"
    params = {"city": CITY, "country": COUNTRY, "method": CALC_METHOD}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()["data"]

    times: dict[str, dtime] = {}
    for k in PRAYERS + ["Sunrise"]:
        hh, mm = data["timings"][k][:5].split(":")
        times[k] = dtime(int(hh), int(mm))
    times["Witr"] = WITR_TIME
    h = data["date"]["hijri"]
    hijri = f"{ar_digits(h['day'])} {h['month']['ar']} {ar_digits(h['year'])} هـ"
    result = {"times": times, "hijri": hijri}
    if len(_cache) > 10:
        _cache.clear()
    _cache[key] = result
    return result


async def fetch_timings_retry(d: date, attempts: int = 3) -> dict:
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await fetch_timings(d)
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("Timings fetch failed (%s/%s): %s", i + 1, attempts, e)
    raise last  # type: ignore[misc]


# ============================== أدوات مساعدة ==============================
_admin_cache: dict[int, tuple[float, list]] = {}
_titles: dict[int, str] = {}


async def get_admins(bot, chat_id: int) -> list:
    cached = _admin_cache.get(chat_id)
    if cached and time.monotonic() - cached[0] < 600:
        return cached[1]
    try:
        members = await bot.get_chat_administrators(chat_id)
        admins = [m.user for m in members if not m.user.is_bot]
    except Exception as e:  # noqa: BLE001
        log.warning("get_chat_administrators failed for %s: %s", chat_id, e)
        return cached[1] if cached else []
    _admin_cache[chat_id] = (time.monotonic(), admins)
    return admins


async def get_members(bot, chat_id: int) -> list[tuple[int, str]]:
    """كل من يعرفه البوت في المجموعة: المسجّلون + المشرفون."""
    result: dict[int, str] = {}
    with db() as c:
        for r in c.execute("SELECT user_id, name FROM members WHERE chat_id=?", (chat_id,)):
            result[r["user_id"]] = r["name"] or "عضو"
    for a in await get_admins(bot, chat_id):
        result[a.id] = a.first_name
    return list(result.items())


async def chat_title(bot, chat_id: int) -> str:
    if chat_id in _titles:
        return _titles[chat_id]
    try:
        title = (await bot.get_chat(chat_id)).title or "المجموعة"
    except Exception:  # noqa: BLE001
        title = "المجموعة"
    _titles[chat_id] = title
    return title


def mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>'


def dm_link(bot_username: str) -> str:
    return f"https://t.me/{bot_username}?start=dm"


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return True
    try:
        m = await context.bot.get_chat_member(chat.id, update.effective_user.id)
        return m.status in ("administrator", "creator")
    except Exception:  # noqa: BLE001
        return False


async def send_mention_batches(
    bot, chat_id: int, members: list[tuple[int, str]], header: str = "",
    reply_to: int | None = None, markup: InlineKeyboardMarkup | None = None,
) -> None:
    """منشن للأعضاء على دفعات صغيرة ليصل الإشعار للجميع."""
    chunks = [members[i:i + MENTION_CHUNK] for i in range(0, len(members), MENTION_CHUNK)] or [[]]
    for i, chunk in enumerate(chunks):
        ments = " ".join(mention(uid, name) for uid, name in chunk)
        if i == 0:
            text = header + (("\n" if header and ments else "") + ments)
        else:
            text = "👥 " + ments
        if not text.strip():
            continue
        try:
            await bot.send_message(
                chat_id, text, parse_mode=ParseMode.HTML,
                reply_to_message_id=reply_to, allow_sending_without_reply=True,
                reply_markup=markup if i == 0 else None,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("mention batch failed: %s", e)
        if i < len(chunks) - 1:
            await asyncio.sleep(1)


# ============================== الأزرار والعرض ==============================
def group_keyboard(record_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🟢 صليت", callback_data=f"p:{record_id}:prayed"),
            InlineKeyboardButton("⏳ سأصلي الآن", callback_data=f"p:{record_id}:soon"),
        ]]
    )


def render_status(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return ""
    prayed = [r for r in rows if r["status"] == "prayed"]
    soon = [r for r in rows if r["status"] == "soon"]
    parts = [f"\n{LINE}"]
    if prayed:
        names = "\n".join(f"   ▫️ {mention(r['user_id'], r['user_name'])}" for r in prayed)
        parts.append(f"✅ <b>أدّوا الصلاة ({ar_digits(len(prayed))})</b>\n{names}")
    if soon:
        names = "\n".join(f"   ▫️ {mention(r['user_id'], r['user_name'])}" for r in soon)
        parts.append(f"⏳ <b>سيصلّون الآن ({ar_digits(len(soon))})</b>\n{names}")
    return "\n".join(parts)


# ============================== الأوامر ==============================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    me = context.bot.username

    if chat.type == ChatType.PRIVATE:
        set_dm_user(update.effective_user, True)
        await update.message.reply_text(
            "✅ <b>تم تفعيل الرسائل الخاصة</b>\n"
            f"{LINE}\n"
            "سأذكّرك هنا بالصلاة إن لم تؤكّد أداءها في المجموعة بعد "
            f"{ar_digits(FOLLOWUP_AFTER_MIN)} دقيقة.\n\n"
            "تقبّل الله طاعتك 🤍\n"
            "▫️ /times — مواقيت اليوم",
            parse_mode=ParseMode.HTML,
        )
        return

    if not await is_admin(update, context):
        await update.message.reply_text("⚠️ هذا الأمر متاح لمشرفي المجموعة فقط.")
        return

    set_chat_active(chat.id, True)
    remember_member(chat.id, update.effective_user)
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ سجّلني في التذكير", callback_data="reg")],
            [InlineKeyboardButton("📩 فعّل الرسائل الخاصة", url=dm_link(me))],
        ]
    )
    await update.message.reply_text(
        "🟢 <b>تم تفعيل المُذكّر في المجموعة</b>\n"
        f"{LINE}\n"
        "▫️ تذكير بالصلوات الخمس بتوقيت الرياض مع منشن للأعضاء.\n"
        "▫️ تذكير بصلاة الوتر الساعة ١٠:٠٠ م.\n"
        f"▫️ بعد {ar_digits(FOLLOWUP_AFTER_MIN)} دقيقة: {ar_digits(REPEAT_COUNT)} رسالة خاصة "
        "لمن لم يؤكّد.\n\n"
        "👇 <b>على كل عضو:</b>\n"
        "١) الضغط على «سجّلني في التذكير».\n"
        "٢) الضغط على «فعّل الرسائل الخاصة» ثم Start.\n\n"
        "📋 /times — المواقيت   👥 /members — حالة التسجيل   ⛔ /stop — إيقاف",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        return
    if not await is_admin(update, context):
        await update.message.reply_text("⚠️ هذا الأمر متاح لمشرفي المجموعة فقط.")
        return
    set_chat_active(update.effective_chat.id, False)
    await update.message.reply_text("⛔ تم إيقاف التذكير في هذه المجموعة.\nلإعادة التفعيل أرسل /start")


async def cmd_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE or not await is_admin(update, context):
        return
    members = await get_members(context.bot, chat.id)
    ids = [m[0] for m in members]
    dm_ok = 0
    if ids:
        with db() as c:
            q = ",".join("?" * len(ids))
            dm_ok = c.execute(
                f"SELECT COUNT(*) FROM dm_users WHERE active=1 AND user_id IN ({q})", ids
            ).fetchone()[0]
    await update.message.reply_text(
        "👥 <b>حالة التسجيل</b>\n"
        f"{LINE}\n"
        f"▫️ أعضاء يعرفهم البوت: <b>{ar_digits(len(members))}</b>\n"
        f"▫️ فعّلوا الرسائل الخاصة: <b>{ar_digits(dm_ok)}</b>\n"
        f"▫️ لم يفعّلوها: <b>{ar_digits(len(members) - dm_ok)}</b>\n\n"
        "<i>البوت يتعرّف على العضو عند كتابته في المجموعة أو ضغطه على أي زر.</i>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_times(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = now_riyadh()
    try:
        day = await fetch_timings_retry(now.date())
    except Exception:  # noqa: BLE001
        await update.message.reply_text("⚠️ تعذّر جلب المواقيت حاليًا، حاول بعد قليل.")
        return

    times = day["times"]
    greg = (
        f"{AR_WEEKDAYS[now.weekday()]}، {ar_digits(now.day)} "
        f"{AR_MONTHS[now.month - 1]} {ar_digits(now.year)} م"
    )
    nxt_name, nxt_dt = None, None
    for p in PRAYERS:
        dt = datetime.combine(now.date(), times[p], tzinfo=TZ)
        if dt > now:
            nxt_name, nxt_dt = p, dt
            break
    if nxt_dt is None:
        try:
            tm = await fetch_timings_retry(now.date() + timedelta(days=1))
            nxt_name = "Fajr"
            nxt_dt = datetime.combine(now.date() + timedelta(days=1), tm["times"]["Fajr"], tzinfo=TZ)
        except Exception:  # noqa: BLE001
            pass

    rows = []
    for k in ["Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha"]:
        label = "الشروق" if k == "Sunrise" else PRAYER_NAMES[k]
        if k == "Dhuhr" and now.weekday() == 4:
            label = "الظهر (الجمعة)"
        mark = " ◀️" if k == nxt_name else ""
        rows.append(f"{PRAYER_ICONS[k]} <b>{label}</b>  ·  {fmt_time(times[k])}{mark}")
    rows.append(f"{PRAYER_ICONS['Witr']} <b>الوتر</b>  ·  {fmt_time(WITR_TIME)}")

    text = (
        f"🕌 <b>مواقيت الصلاة — الرياض</b>\n🗓 {greg}\n🌙 {day['hijri']}\n"
        f"{LINE}\n" + "\n".join(rows) + f"\n{LINE}\n"
    )
    if nxt_dt:
        text += f"⏭ <b>الصلاة القادمة:</b> {PRAYER_NAMES[nxt_name]} بعد {fmt_duration(nxt_dt - now)}\n"
    text += "<i>التقويم: أم القرى</i>"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تذكير تجريبي فوري (للمشرفين)."""
    if update.effective_chat.type == ChatType.PRIVATE or not await is_admin(update, context):
        return
    now = now_riyadh()
    try:
        day = await fetch_timings_retry(now.date())
    except Exception:  # noqa: BLE001
        await update.message.reply_text("⚠️ تعذّر جلب المواقيت حاليًا.")
        return
    await send_reminder(
        context, update.effective_chat.id, "Dhuhr", day["times"]["Dhuhr"], now,
        day_key=f"test-{int(time.time())}",
    )


# ============================== تتبّع الأعضاء ==============================
async def on_group_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, msg = update.effective_chat, update.effective_message
    if not chat or chat.type == ChatType.PRIVATE:
        return
    if update.effective_user:
        remember_member(chat.id, update.effective_user)
    if msg:
        for u in msg.new_chat_members or []:
            remember_member(chat.id, u)
        if msg.left_chat_member:
            forget_member(chat.id, msg.left_chat_member.id)


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member
    if not cmu:
        return
    user = cmu.new_chat_member.user
    if cmu.new_chat_member.status in ("member", "administrator", "creator", "restricted"):
        remember_member(cmu.chat.id, user)
    elif cmu.new_chat_member.status in ("left", "kicked"):
        forget_member(cmu.chat.id, user.id)


async def on_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    remember_member(q.message.chat.id, q.from_user)
    with db() as c:
        dm = c.execute(
            "SELECT 1 FROM dm_users WHERE user_id=? AND active=1", (q.from_user.id,)
        ).fetchone()
    msg = "تم تسجيلك في التذكير ✅"
    if not dm:
        msg += "\n\nلتصلك الرسائل الخاصة اضغط زر «📩 فعّل الرسائل الخاصة» ثم Start."
    await q.answer(msg, show_alert=True)


# ============================== ضغط الأزرار ==============================
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        _, rid, status = q.data.split(":")
        record_id = int(rid)
    except ValueError:
        await q.answer()
        return
    if status not in ("prayed", "soon"):
        await q.answer()
        return

    user = q.from_user
    with db() as c:
        rec = c.execute("SELECT * FROM prayer_records WHERE id=?", (record_id,)).fetchone()
        if not rec:
            await q.answer("انتهت صلاحية هذا التذكير.", show_alert=True)
            return
        existing = c.execute(
            "SELECT status FROM responses WHERE record_id=? AND user_id=?", (record_id, user.id)
        ).fetchone()
        if existing and existing["status"] == "prayed":
            await q.answer("سبق أن أكّدت أداء الصلاة. تقبّل الله منك 🤍")
            return
        if existing and existing["status"] == status:
            await q.answer("تم تسجيل إجابتك مسبقًا.")
            return
        c.execute(
            "INSERT INTO responses(record_id, user_id, user_name, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(record_id, user_id) DO UPDATE SET "
            "status=excluded.status, user_name=excluded.user_name, updated_at=excluded.updated_at",
            (record_id, user.id, user.first_name, status, datetime.now(timezone.utc).isoformat()),
        )
        rows = c.execute(
            "SELECT * FROM responses WHERE record_id=? ORDER BY updated_at", (record_id,)
        ).fetchall()

    remember_member(rec["chat_id"], user)
    await q.answer(
        "تقبّل الله طاعتك 🤍" if status == "prayed" else "بارك الله فيك، بادر بالصلاة ⏳"
    )

    # تحديث رسالة المجموعة
    if rec["message_id"]:
        try:
            await context.bot.edit_message_text(
                chat_id=rec["chat_id"], message_id=rec["message_id"],
                text=rec["body"] + render_status(rows),
                parse_mode=ParseMode.HTML, reply_markup=group_keyboard(record_id),
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                log.warning("edit failed: %s", e)

    # لو الضغط من الخاص: حدّث رسالة الخاص نفسها
    if q.message and q.message.chat.type == ChatType.PRIVATE:
        if status == "prayed":
            text, kb = "✅ تم تسجيلك: أدّيت الصلاة. تقبّل الله منك 🤍", None
        else:
            text, kb = "⏳ تم تسجيل أنك ستصلي الآن. لا تنسَ تأكيد «صليت» بعد الانتهاء.", group_keyboard(record_id)
        try:
            await q.edit_message_text(text, reply_markup=kb)
        except Exception:  # noqa: BLE001
            pass


# ============================== المجدول: التذكيرات ==============================
def build_reminder_body(key: str, t: dtime, now: datetime) -> str:
    hadith = random.choice(HADITHS_BY_PRAYER.get(key) or HADITHS_GENERAL)
    if key == "Witr":
        title = "🌙 <b>تذكير بصلاة الوتر وقيام الليل</b>"
    else:
        name = PRAYER_NAMES[key]
        if key == "Dhuhr" and now.weekday() == 4:
            name = "الجمعة"
        title = f"🕌 <b>حان الآن موعد صلاة {name}</b>"
    return (
        f"{title}\n📍 الرياض  ·  🕰 {fmt_time(t)}\n{LINE}\n💬 {hadith}\n{LINE}\n"
        "👇 <b>اختر حالتك عبر الأزرار:</b>"
    )


async def send_reminder(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, key: str, t: dtime,
    now: datetime, day_key: str | None = None,
) -> None:
    day_key = day_key or now.strftime("%Y-%m-%d")
    with db() as c:
        if c.execute(
            "SELECT 1 FROM prayer_records WHERE chat_id=? AND prayer=? AND day=?",
            (chat_id, key, day_key),
        ).fetchone():
            return

    body = build_reminder_body(key, t, now)
    with db() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO prayer_records(chat_id, prayer, day, sent_at, body) VALUES (?, ?, ?, ?, ?)",
            (chat_id, key, day_key, datetime.now(timezone.utc).isoformat(), body),
        )
        if cur.rowcount == 0:
            return
        record_id = cur.lastrowid

    try:
        msg = await context.bot.send_message(
            chat_id, body, parse_mode=ParseMode.HTML, reply_markup=group_keyboard(record_id)
        )
    except Forbidden:
        log.warning("Bot removed from chat %s — deactivating.", chat_id)
        set_chat_active(chat_id, False)
        with db() as c:
            c.execute("DELETE FROM prayer_records WHERE id=?", (record_id,))
        return
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to send reminder to %s: %s", chat_id, e)
        with db() as c:
            c.execute("DELETE FROM prayer_records WHERE id=?", (record_id,))
        return

    with db() as c:
        c.execute("UPDATE prayer_records SET message_id=? WHERE id=?", (msg.message_id, record_id))
    log.info("Reminder %s sent to %s", key, chat_id)

    # منشن للجميع (على دفعات) كرد على رسالة التذكير
    members = await get_members(context.bot, chat_id)
    await send_mention_batches(
        context.bot, chat_id, members, header="📣 <b>نداء للجميع:</b>", reply_to=msg.message_id
    )


async def tick_prayer_reminders(context: ContextTypes.DEFAULT_TYPE):
    global _fail_until
    now = now_riyadh()
    if _fail_until and now < _fail_until:
        return
    try:
        day = await fetch_timings(now.date())
    except Exception as e:  # noqa: BLE001
        log.warning("Timings unavailable, retry in 2 min: %s", e)
        _fail_until = now + timedelta(minutes=2)
        return

    due = []
    for key in SCHEDULE:
        t = day["times"][key]
        elapsed = (now - datetime.combine(now.date(), t, tzinfo=TZ)).total_seconds()
        if 0 <= elapsed < CATCHUP_WINDOW_SECONDS:
            due.append((key, t))
    if not due:
        return

    with db() as c:
        chats = [r["chat_id"] for r in c.execute("SELECT chat_id FROM chats WHERE active=1")]
    for chat_id in chats:
        for key, t in due:
            await send_reminder(context, chat_id, key, t, now)


# ============================== المجدول: المتابعة ==============================
async def send_dms(context, rec, pending: list[tuple[int, str]], now_utc: datetime) -> bool:
    """يرسل الرسائل الخاصة المستحقة. يعيد True إن بقي أحد يستحق رسائل إضافية."""
    rec_id = rec["id"]
    pname = PRAYER_NAMES.get(rec["prayer"], "الصلاة")
    title = html.escape(await chat_title(context.bot, rec["chat_id"]))
    remaining = False

    for uid, _name in pending:
        with db() as c:
            u = c.execute("SELECT 1 FROM dm_users WHERE user_id=? AND active=1", (uid,)).fetchone()
            prog = c.execute(
                "SELECT count, last_at FROM dm_progress WHERE record_id=? AND user_id=?", (rec_id, uid)
            ).fetchone()
        if not u:
            continue
        count = prog["count"] if prog else 0
        if count >= REPEAT_COUNT:
            continue
        remaining = True
        if prog and prog["last_at"]:
            since = (now_utc - datetime.fromisoformat(prog["last_at"])).total_seconds()
            if since < REPEAT_INTERVAL_SEC:
                continue

        n = count + 1
        text = (
            f"🔔 <b>تذكير بصلاة {pname}</b> ({ar_digits(n)}/{ar_digits(REPEAT_COUNT)})\n"
            f"{LINE}\n"
            f"لم تؤكّد أداء الصلاة بعد في «{title}».\n"
            "بادر بالصلاة ثم اضغط «صليت» 🤍"
        )
        try:
            await context.bot.send_message(
                uid, text, parse_mode=ParseMode.HTML, reply_markup=group_keyboard(rec_id)
            )
        except Forbidden:
            with db() as c:
                c.execute("UPDATE dm_users SET active=0 WHERE user_id=?", (uid,))
            continue
        except Exception as e:  # noqa: BLE001
            log.warning("DM to %s failed: %s", uid, e)
            continue

        with db() as c:
            c.execute(
                "INSERT INTO dm_progress(record_id, user_id, count, last_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(record_id, user_id) DO UPDATE SET count=excluded.count, last_at=excluded.last_at",
                (rec_id, uid, n, now_utc.isoformat()),
            )
    return remaining


async def process_record(context: ContextTypes.DEFAULT_TYPE, rec, now_utc: datetime) -> None:
    rec_id, chat_id = rec["id"], rec["chat_id"]
    elapsed = (now_utc - datetime.fromisoformat(rec["sent_at"])).total_seconds() / 60
    members = await get_members(context.bot, chat_id)

    done_statuses = ("prayed", "soon") if STOP_ON_SOON else ("prayed",)
    ph = ",".join("?" * len(done_statuses))
    with db() as c:
        confirmed = {
            r[0] for r in c.execute(
                f"SELECT user_id FROM responses WHERE record_id=? AND status IN ({ph})",
                (rec_id, *done_statuses),
            )
        }
    pending = [m for m in members if m[0] not in confirmed]

    def close():
        with db() as c:
            c.execute("UPDATE prayer_records SET closed=1 WHERE id=?", (rec_id,))

    if not pending or elapsed > RECORD_MAX_AGE_MIN:
        close()
        return
    if elapsed < FOLLOWUP_AFTER_MIN:
        return

    me = context.bot.username

    # 1) تنبيه المجموعة (مرة واحدة)
    if not rec["followup_sent"]:
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("📩 فعّل الرسائل الخاصة", url=dm_link(me))]]
        )
        header = (
            f"⚠️ <b>تنبيه</b>\nمضت {ar_digits(FOLLOWUP_AFTER_MIN)} دقيقة ولم يؤكّد أداء الصلاة:\n"
            f"<i>سيصلكم {ar_digits(REPEAT_COUNT)} تذكير خاص — إن لم تصلكم اضغطوا الزر أدناه.</i>"
        )
        await send_mention_batches(
            context.bot, chat_id, pending, header=header,
            reply_to=rec["message_id"], markup=kb,
        )
        with db() as c:
            c.execute("UPDATE prayer_records SET followup_sent=1 WHERE id=?", (rec_id,))
        return

    # 2) الرسائل الخاصة
    remaining = await send_dms(context, rec, pending, now_utc)

    # 3) التنبيه الختامي بعد انتهاء كل الرسائل
    if not remaining:
        await send_mention_batches(
            context.bot, chat_id, pending,
            header=(
                "📢 <b>تنبيه عام</b>\nانتهت مهلة التذكيرات دون تأكيد من:\n"
                "<i>نسأل الله أن يعيننا على المحافظة على الصلاة في وقتها 🤍</i>"
            ),
            reply_to=rec["message_id"],
        )
        close()


async def tick_followups(context: ContextTypes.DEFAULT_TYPE):
    now_utc = datetime.now(timezone.utc)
    with db() as c:
        records = c.execute("SELECT * FROM prayer_records WHERE closed=0").fetchall()
    for rec in records:
        try:
            await process_record(context, rec, now_utc)
        except Exception:  # noqa: BLE001
            log.exception("process_record failed (id=%s)", rec["id"])


# ============================== التشغيل ==============================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=context.error)


async def post_init(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("times", "مواقيت الصلاة اليوم — الرياض"),
            BotCommand("start", "تفعيل التذكير / الرسائل الخاصة"),
            BotCommand("members", "حالة تسجيل الأعضاء"),
            BotCommand("stop", "إيقاف التذكير"),
        ]
    )


def main():
    if not BOT_TOKEN:
        sys.exit("❌ لم يتم ضبط BOT_TOKEN.")
    init_db()

    app = (
        Application.builder().token(BOT_TOKEN)
        .connect_timeout(30.0).read_timeout(30.0).write_timeout(30.0)
        .post_init(post_init).build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("times", cmd_times))
    app.add_handler(CommandHandler("members", cmd_members))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^p:"))
    app.add_handler(CallbackQueryHandler(on_register, pattern=r"^reg$"))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, on_group_activity), group=1)
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(tick_prayer_reminders, interval=30, first=5)
    app.job_queue.run_repeating(tick_followups, interval=10, first=15)

    log.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
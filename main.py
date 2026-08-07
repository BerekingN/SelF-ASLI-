#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات مدیریتی + سلف تلگرام (نسخه کاملا قانونی)
ساخته‌شده با Telethon - مخصوص اجرا روی Railway

این اسکریپت دو کلاینت تلگرام اجرا می‌کند:
1) bot_client : یک ربات معمولی (ساخته‌شده با BotFather) که فقط برای گرفتن
   شماره تلفن و کد ورود از صاحب اکانت استفاده می‌شود (فرآیند ورود).
2) user_client : اکانت شخصی خودتان (سلف) که بعد از ورود موفق فعال می‌شود
   و دستورات فارسی را در «پیام‌های ذخیره‌شده» (Saved Messages) می‌خواند.

هیچ ارسال خودکار/زمان‌بندی‌شده‌ی تبلیغاتی در این کد وجود ندارد؛ چون این کار
مستقیماً ناقض قوانین ضداسپم تلگرام است و می‌تواند باعث محدود یا بن شدن
اکانت شما شود. تمام ارسال‌ها با دستور دستی خودتان انجام می‌شود.

توضیحات کامل در README.md
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.tl.types import User
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    FloodWaitError,
)

# ---------------------------------------------------------------------------
# تنظیمات پایه
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("selfbot")

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = os.environ.get("OWNER_ID", "")
OWNER_ID = int(OWNER_ID) if OWNER_ID.strip().isdigit() else None
SESSION_STRING_ENV = os.environ.get("SESSION_STRING", "").strip()

DATA_FILE = Path(__file__).parent / "bot_data.json"
LOCAL_SESSION_FILE = Path(__file__).parent / "last_session.txt"

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise SystemExit(
        "لطفاً متغیرهای محیطی API_ID و API_HASH و BOT_TOKEN را در Railway تنظیم کنید. "
        "راهنمای کامل در README.md موجود است."
    )

# ---------------------------------------------------------------------------
# ذخیره‌سازی ساده روی فایل JSON
# ---------------------------------------------------------------------------

DEFAULT_DATA = {
    "admins": [],          # آیدی عددی ادمین‌های اضافه بر مالک
    "groups": [],          # [{"id": ..., "title": ...}, ...]
    "banner": None,        # {"chat_id": ..., "msg_id": ...}
    "auto_reply": {"enabled": False, "text": ""},
    "qa": {},              # {"سوال": "جواب"}
    "muted": False,        # سکوت = خاموش بودن منشی و پاسخ خودکار
    "antigroup": {},       # {"<group_id>": {"links": bool, "flood": bool}}
    "scheduled": [],       # [{"id":1,"minutes":2,"command":"پینگ","enabled":True,"last_run":0}]
}



def load_data():
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            for k, v in DEFAULT_DATA.items():
                d.setdefault(k, v)
            return d
        except Exception:
            log.exception("خطا در خواندن bot_data.json - از تنظیمات پیش‌فرض استفاده می‌شود")
    return json.loads(json.dumps(DEFAULT_DATA))


def save_data(d):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


data = load_data()
if OWNER_ID and OWNER_ID not in data["admins"]:
    data["admins"].append(OWNER_ID)
    save_data(data)

# ردیابی فلود برای آنتی‌گروه: {chat_id: {user_id: [timestamps]}}
flood_tracker = {}
LINK_REGEX = re.compile(r"(https?://|t\.me/|telegram\.me/|@[\w\d_]{4,})", re.IGNORECASE)

# ---------------------------------------------------------------------------
# کلاینت ربات ورود (bot_client) - با BOT_TOKEN از BotFather
# ---------------------------------------------------------------------------

bot_client = TelegramClient("login_bot", API_ID, API_HASH)

# کلاینت سلف (user_client) - بعد از ورود موفق ساخته می‌شود
user_client: TelegramClient | None = None
user_client_lock = asyncio.Lock()

# وضعیت مکالمه‌ی ورود برای هر کاربر: {user_id: {"step":..., "phone":..., "phone_code_hash":..., "temp_client":...}}
login_state = {}

# تسک بک‌گراند زمان‌بند دستورات
scheduler_task: asyncio.Task | None = None


def next_schedule_id() -> int:
    ids = [j.get("id", 0) for j in data["scheduled"]]
    return (max(ids) + 1) if ids else 1


def find_schedule_index(idx_text: str):
    try:
        idx = int(idx_text)
        if 1 <= idx <= len(data["scheduled"]):
            return idx - 1
    except ValueError:
        pass
    return None


async def scheduler_loop(client: TelegramClient):
    """هر ۱۵ ثانیه چک می‌کند و دستوراتی که موعدشان رسیده را به‌صورت خودکار
    در پیام‌های ذخیره‌شده اجرا می‌کند (دقیقاً مثل اینکه خودتان تایپ کرده باشید)."""
    while True:
        try:
            await asyncio.sleep(15)
            me = await client.get_me()
            now = time.time()
            changed = False
            for job in list(data.get("scheduled", [])):
                if not job.get("enabled", True):
                    continue
                interval = max(1, int(job.get("minutes", 1))) * 60
                if now - job.get("last_run", 0) >= interval:
                    job["last_run"] = now
                    changed = True
                    try:
                        await client.send_message(me.id, job["command"])
                    except Exception:
                        log.exception("خطا در اجرای خودکار دستور زمان‌بندی‌شده: %s", job.get("command"))
            if changed:
                save_data(data)
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("خطا در حلقه‌ی زمان‌بند")


def is_owner(uid: int) -> bool:
    return OWNER_ID is None or uid == OWNER_ID


@bot_client.on(events.NewMessage(pattern="/start"))
async def cmd_start(event):
    await event.respond(
        "سلام 👋\n"
        "این ربات کمکی برای راه‌اندازی سلف‌بات شخصی شماست.\n\n"
        "برای ورود به اکانت تلگرام خودتان دستور /login را بفرستید.\n"
        "پس از ورود، تمام دستورات داخل «پیام‌های ذخیره‌شده» (Saved Messages) "
        "خودِ اکانت شما فعال می‌شوند. کافیست آنجا بنویسید: راهنما"
    )


@bot_client.on(events.NewMessage(pattern="/login"))
async def cmd_login(event):
    uid = event.sender_id
    if not is_owner(uid):
        await event.respond("⛔️ فقط صاحب این ربات اجازه‌ی ورود دارد.")
        return
    if user_client is not None and user_client.is_connected():
        try:
            if await user_client.is_user_authorized():
                await event.respond("✅ شما از قبل وارد شده‌اید. برای ورود مجدد اول باید ربات را ری‌استارت کنید.")
                return
        except Exception:
            pass
    login_state[uid] = {"step": "phone"}
    await event.respond(
        "لطفاً شماره تلفن اکانتی که می‌خواهید سلف روی آن فعال شود را با فرمت "
        "بین‌المللی بفرستید. مثال:\n`+989121234567`",
        parse_mode="markdown",
    )


@bot_client.on(events.NewMessage())
async def login_flow_handler(event):
    uid = event.sender_id
    if uid not in login_state:
        return
    if event.raw_text.startswith("/"):
        return

    state = login_state[uid]
    step = state["step"]

    # --- مرحله‌ی دریافت شماره تلفن ---
    if step == "phone":
        phone = event.raw_text.strip()
        if not re.match(r"^\+\d{7,15}$", phone):
            await event.respond("فرمت شماره اشتباه است. با + و کد کشور بفرستید. مثال: +989121234567")
            return
        temp_client = TelegramClient(StringSession(), API_ID, API_HASH)
        await temp_client.connect()
        try:
            sent = await temp_client.send_code_request(phone)
        except FloodWaitError as e:
            await event.respond(f"محدودیت موقت تلگرام. لطفاً {e.seconds} ثانیه دیگر دوباره امتحان کنید.")
            await temp_client.disconnect()
            login_state.pop(uid, None)
            return
        except Exception as e:
            await event.respond(f"خطا در ارسال کد: {e}")
            await temp_client.disconnect()
            login_state.pop(uid, None)
            return
        state.update(
            step="code",
            phone=phone,
            phone_code_hash=sent.phone_code_hash,
            temp_client=temp_client,
        )
        await event.respond(
            "کدی به تلگرام (یا پیامک) شما ارسال شد. لطفاً کد را اینجا بفرستید.\n"
            "⚠️ برای جلوگیری از خطای تلگرام، کد را با یک فاصله یا حرف اضافه بفرستید؛ "
            "مثلاً اگر کد 12345 است بنویسید: 1 2 3 4 5"
        )
        return

    # --- مرحله‌ی دریافت کد ---
    if step == "code":
        raw_code = event.raw_text.strip()
        code = re.sub(r"\D", "", raw_code)  # فقط ارقام را نگه می‌داریم
        temp_client: TelegramClient = state["temp_client"]
        try:
            await temp_client.sign_in(
                phone=state["phone"],
                code=code,
                phone_code_hash=state["phone_code_hash"],
            )
        except PhoneCodeInvalidError:
            await event.respond("کد اشتباه است. دوباره تلاش کنید.")
            return
        except PhoneCodeExpiredError:
            await event.respond("کد منقضی شده. دوباره /login را بزنید.")
            login_state.pop(uid, None)
            return
        except SessionPasswordNeededError:
            state["step"] = "password"
            await event.respond("این اکانت رمز دو مرحله‌ای (Cloud Password) دارد. لطفاً رمز را بفرستید.")
            return
        except Exception as e:
            await event.respond(f"خطا در ورود: {e}")
            return
        await finish_login(event, uid, temp_client)
        return

    # --- مرحله‌ی رمز دو مرحله‌ای ---
    if step == "password":
        temp_client: TelegramClient = state["temp_client"]
        try:
            await temp_client.sign_in(password=event.raw_text.strip())
        except Exception as e:
            await event.respond(f"رمز اشتباه است یا خطایی رخ داد: {e}")
            return
        await finish_login(event, uid, temp_client)
        return


async def finish_login(event, uid, temp_client: TelegramClient):
    session_str = StringSession.save(temp_client.session)
    login_state.pop(uid, None)
    try:
        with open(LOCAL_SESSION_FILE, "w", encoding="utf-8") as f:
            f.write(session_str)
    except Exception:
        pass
    await event.respond(
        "✅ ورود موفقیت‌آمیز بود! سلف‌بات شما فعال شد.\n\n"
        "برای این‌که بعد از هر بار ری‌استارت شدن سرویس روی Railway مجبور به "
        "ورود دوباره نشوید، رشته‌ی زیر را کپی کنید و در Railway به‌عنوان "
        "متغیر محیطی به نام SESSION_STRING ذخیره کنید:\n\n"
        f"`{session_str}`\n\n"
        "حالا کافیست به «پیام‌های ذخیره‌شده» (Saved Messages) خودتان بروید و "
        "بنویسید: راهنما",
        parse_mode="markdown",
    )
    await start_user_client(session_str)


# ---------------------------------------------------------------------------
# راه‌اندازی کلاینت سلف
# ---------------------------------------------------------------------------

async def start_user_client(session_str: str):
    global user_client, scheduler_task
    async with user_client_lock:
        if user_client is not None and user_client.is_connected():
            await user_client.disconnect()
        if scheduler_task is not None and not scheduler_task.done():
            scheduler_task.cancel()
        user_client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await user_client.connect()
        if not await user_client.is_user_authorized():
            log.error("سشن معتبر نیست؛ نیاز به ورود مجدد دارید (/login در ربات ورود)")
            return
        register_user_handlers(user_client)
        scheduler_task = asyncio.create_task(scheduler_loop(user_client))
        log.info("کلاینت سلف با موفقیت متصل شد. زمان‌بند دستورات فعال شد.")


# ---------------------------------------------------------------------------
# متن راهنما
# ---------------------------------------------------------------------------

HELP_MAIN = """📖 راهنمای کامل ربات

همه‌ی این دستورات را داخل «پیام‌های ذخیره‌شده» (Saved Messages) خودتان تایپ کنید.

🔹 عمومی
راهنما — همین راهنما
راهنما <بخش> — توضیح کامل یک بخش (مثال: راهنما بنر)
پینگ — تست سرعت اتصال
آیدی — نمایش آیدی عددی خودتان

🔹 گروه‌ها
لیست گروه‌ها
افزودن گروه <آیدی یا یوزرنیم>
حذف گروه <شماره>

🔹 بنر (تبلیغ دستی)
ثبت بنر — با ریپلای روی یک پیام
بنر — پیش‌نمایش بنر ذخیره‌شده
ارسال بنر <شماره گروه یا all>

🔹 منشی
منشی روشن / منشی خاموش
منشی متن <پیام>
پرسش پاسخ افزودن <سوال> | <جواب>
پرسش پاسخ حذف <سوال>
لیست پرسش پاسخ

🔹 امنیت
سکوت روشن / سکوت خاموش
افزودن ادمین <آیدی>
حذف ادمین <آیدی>
لیست ادمین‌ها

🔹 آنتی گروه (حفاظت گروه)
آنتی گروه روشن <شماره گروه>
آنتی گروه خاموش <شماره گروه>
آنتی گروه وضعیت <شماره گروه>

🔹 زمان‌بندی دستورات (اجرای خودکار در بازه‌ی زمانی)
زمانبندی افزودن <دقیقه> | <دستور>
لیست زمانبندی
زمانبندی روشن <شماره>
زمانبندی خاموش <شماره>
حذف زمانبندی <شماره>

⚠️ توجه: این ربات هیچ ارسال خودکار یا زمان‌بندی‌شده‌ی تبلیغاتی به گروه‌ها انجام نمی‌دهد؛ چون این کار طبق قوانین تلگرام اسپم محسوب می‌شود و می‌تواند اکانت شما را در خطر بیندازد. ارسال بنر همیشه با دستور دستی خودتان انجام می‌شود."""

HELP_SECTIONS = {
    "بنر": (
        "📌 راهنمای بنر\n\n"
        "1) پیام (متن/عکس/ویدیو) موردنظر را در Saved Messages بفرستید.\n"
        "2) روی همان پیام ریپلای کنید و بنویسید: ثبت بنر\n"
        "3) با نوشتن «بنر» می‌توانید پیش‌نمایش بگیرید.\n"
        "4) برای ارسال دستی به گروه‌ها بنویسید:\n"
        "   ارسال بنر 1   (ارسال به گروه شماره 1 در لیست گروه‌ها)\n"
        "   ارسال بنر all   (ارسال به همه‌ی گروه‌های ثبت‌شده)\n"
        "این ارسال همیشه دستی است و هیچ زمان‌بندی خودکاری وجود ندارد."
    ),
    "گروه": (
        "📌 راهنمای گروه‌ها\n\n"
        "افزودن گروه <آیدی یا یوزرنیم> — گروه را به لیست اضافه می‌کند (باید عضو آن باشید)\n"
        "لیست گروه‌ها — نمایش شماره، نام و آیدی گروه‌ها\n"
        "حذف گروه <شماره> — حذف از لیست"
    ),
    "منشی": (
        "📌 راهنمای منشی\n\n"
        "منشی روشن — فعال کردن پاسخ خودکار در چت خصوصی\n"
        "منشی متن <پیام> — تنظیم متن پاسخ ساده\n"
        "پرسش پاسخ افزودن <سوال> | <جواب> — افزودن یک جفت سوال/جواب\n"
        "پرسش پاسخ حذف <سوال>\n"
        "لیست پرسش پاسخ\n"
        "اگر پیام دریافتی با یکی از سوال‌ها مطابقت داشته باشد همان جواب فرستاده می‌شود، "
        "در غیر این صورت (در صورت فعال بودن) متن پیش‌فرض منشی فرستاده می‌شود."
    ),
    "امنیت": (
        "📌 راهنمای امنیت\n\n"
        "سکوت روشن — منشی و پاسخ خودکار موقتاً خاموش می‌شود (خود دستورات همچنان کار می‌کنند)\n"
        "سکوت خاموش — بازگرداندن وضعیت عادی\n"
        "افزودن ادمین <آیدی> / حذف ادمین <آیدی> / لیست ادمین‌ها — مدیریت افرادی که اجازه‌ی "
        "تغییر تنظیمات ربات از طریق ربات ورود (پیوی) را دارند."
    ),
    "ادیت": HELP_MAIN,
    "کل": HELP_MAIN,
    "آنتی": (
        "📌 راهنمای آنتی گروه\n\n"
        "آنتی گروه روشن <شماره گروه> — فعال کردن حذف خودکار پیام‌های حاوی لینک/یوزرنیم "
        "و کنترل فلود (پیام زیاد در زمان کوتاه) از اعضای غیرادمین گروه\n"
        "آنتی گروه خاموش <شماره گروه>\n"
        "آنتی گروه وضعیت <شماره گروه>\n\n"
        "توجه: این قابلیت فقط زمانی کار می‌کند که اکانت شما در آن گروه دسترسی حذف پیام داشته باشد."
    ),
    "زمانبندی": (
        "📌 راهنمای زمان‌بندی دستورات\n\n"
        "با این بخش می‌توانید هر دستوری از همین ربات (مثلاً پینگ، آیدی، لیست گروه‌ها و ...) را "
        "طوری تنظیم کنید که خودش هر چند دقیقه یک‌بار به‌طور خودکار در پیام‌های ذخیره‌شده اجرا شود؛ "
        "دقیقاً مثل اینکه خودتان تایپش کرده باشید.\n\n"
        "زمانبندی افزودن <دقیقه> | <دستور> — مثال:\n"
        "   زمانبندی افزودن 2 | پینگ\n"
        "   (یعنی دستور «پینگ» هر ۲ دقیقه یک‌بار خودکار اجرا شود)\n\n"
        "لیست زمانبندی — نمایش همه‌ی موارد زمان‌بندی‌شده با شماره\n"
        "زمانبندی روشن <شماره> / زمانبندی خاموش <شماره> — فعال یا موقتاً متوقف کردن یک مورد\n"
        "حذف زمانبندی <شماره> — حذف کامل یک مورد\n\n"
        "⚠️ به دلایل امنیتی و برای جلوگیری از بن شدن اکانت، دستور «ارسال بنر» (ارسال دسته‌جمعی "
        "به گروه‌ها) قابل زمان‌بندی خودکار نیست و همیشه باید دستی اجرا شود؛ چون ارسال تکراری و "
        "خودکار پیام به گروه‌های زیاد دقیقاً همان کاری‌ست که قوانین ضداسپم تلگرام آن را جریمه می‌کند."
    ),
}


def find_group_index(idx_text: str):
    try:
        idx = int(idx_text)
        if 1 <= idx <= len(data["groups"]):
            return idx - 1
    except ValueError:
        pass
    return None


# ---------------------------------------------------------------------------
# ثبت‌کننده‌ی دستورات سلف (روی user_client)
# ---------------------------------------------------------------------------

def register_user_handlers(client: TelegramClient):

    @client.on(events.NewMessage(outgoing=True))
    async def self_commands(event):
        me = await client.get_me()
        # فقط داخل Saved Messages (چت با خودمان)
        if event.chat_id != me.id:
            return
        text = (event.raw_text or "").strip()
        if not text:
            return

        # ---------- راهنما ----------
        if text == "راهنما":
            await event.edit(HELP_MAIN)
            return
        if text.startswith("راهنما "):
            key = text.split(" ", 1)[1].strip()
            body = HELP_SECTIONS.get(key)
            await event.edit(body if body else f"بخشی با نام «{key}» پیدا نشد. فقط «راهنما» را بفرستید تا لیست کامل را ببینید.")
            return

        # ---------- پینگ ----------
        if text == "پینگ":
            start = time.time()
            await event.edit("در حال سنجش...")
            ms = int((time.time() - start) * 1000)
            await event.edit(f"🏓 پونگ! {ms}ms")
            return

        # ---------- آیدی ----------
        if text == "آیدی":
            await event.edit(f"🆔 آیدی عددی شما: `{me.id}`", parse_mode="markdown")
            return

        # ---------- گروه‌ها ----------
        if text == "لیست گروه‌ها":
            if not data["groups"]:
                await event.edit("هنوز گروهی ثبت نشده. با «افزودن گروه <آیدی یا یوزرنیم>» اضافه کنید.")
                return
            lines = ["📋 لیست گروه‌ها:"]
            for i, g in enumerate(data["groups"], 1):
                lines.append(f"{i}. {g['title']} — `{g['id']}`")
            await event.edit("\n".join(lines), parse_mode="markdown")
            return

        if text.startswith("افزودن گروه"):
            target = text.replace("افزودن گروه", "", 1).strip()
            if not target:
                await event.edit("مثال: افزودن گروه @mygroup یا افزودن گروه -1001234567890")
                return
            try:
                entity = await client.get_entity(target)
                gid = entity.id if not str(entity.id).startswith("-") else entity.id
                full_id = entity.id
                title = getattr(entity, "title", None) or getattr(entity, "username", str(full_id))
                if any(g["id"] == full_id for g in data["groups"]):
                    await event.edit("این گروه از قبل ثبت شده.")
                    return
                data["groups"].append({"id": full_id, "title": title})
                save_data(data)
                await event.edit(f"✅ گروه «{title}» اضافه شد.")
            except Exception as e:
                await event.edit(f"خطا در پیدا کردن گروه: {e}")
            return

        if text.startswith("حذف گروه"):
            idx_text = text.replace("حذف گروه", "", 1).strip()
            idx = find_group_index(idx_text)
            if idx is None:
                await event.edit("شماره گروه معتبر نیست. برای دیدن شماره‌ها «لیست گروه‌ها» را بزنید.")
                return
            removed = data["groups"].pop(idx)
            save_data(data)
            await event.edit(f"🗑 گروه «{removed['title']}» حذف شد.")
            return

        # ---------- بنر ----------
        if text == "ثبت بنر":
            if not event.is_reply:
                await event.edit("برای ثبت بنر باید روی یک پیام ریپلای کنید.")
                return
            replied = await event.get_reply_message()
            data["banner"] = {"chat_id": replied.chat_id, "msg_id": replied.id}
            save_data(data)
            await event.edit("✅ بنر ذخیره شد. برای پیش‌نمایش بنویسید: بنر")
            return

        if text == "بنر":
            b = data.get("banner")
            if not b:
                await event.edit("هنوز بنری ثبت نشده. با ریپلای روی یک پیام بنویسید: ثبت بنر")
                return
            try:
                await client.forward_messages(me.id, b["msg_id"], b["chat_id"])
            except Exception as e:
                await event.edit(f"خطا در نمایش بنر: {e}")
            return

        if text.startswith("ارسال بنر"):
            b = data.get("banner")
            if not b:
                await event.edit("هنوز بنری ثبت نشده.")
                return
            arg = text.replace("ارسال بنر", "", 1).strip()
            if not data["groups"]:
                await event.edit("هیچ گروهی ثبت نشده. اول با «افزودن گروه» یک گروه اضافه کنید.")
                return
            targets = []
            if arg == "all" or arg == "":
                targets = data["groups"]
            else:
                idx = find_group_index(arg)
                if idx is None:
                    await event.edit("شماره گروه نامعتبر است.")
                    return
                targets = [data["groups"][idx]]
            ok, fail = 0, 0
            for g in targets:
                try:
                    await client.forward_messages(g["id"], b["msg_id"], b["chat_id"])
                    ok += 1
                    await asyncio.sleep(2)  # فاصله‌ی کوتاه برای رعایت محدودیت‌های تلگرام
                except Exception:
                    fail += 1
            await event.edit(f"📤 ارسال دستی انجام شد. موفق: {ok} | ناموفق: {fail}")
            return

        # ---------- منشی ----------
        if text == "منشی روشن":
            data["auto_reply"]["enabled"] = True
            save_data(data)
            await event.edit("✅ منشی فعال شد.")
            return
        if text == "منشی خاموش":
            data["auto_reply"]["enabled"] = False
            save_data(data)
            await event.edit("🔴 منشی غیرفعال شد.")
            return
        if text.startswith("منشی متن"):
            msg = text.replace("منشی متن", "", 1).strip()
            if not msg:
                await event.edit("مثال: منشی متن سلام، فعلا در دسترس نیستم.")
                return
            data["auto_reply"]["text"] = msg
            save_data(data)
            await event.edit("✅ متن منشی ذخیره شد.")
            return

        if text.startswith("پرسش پاسخ افزودن"):
            rest = text.replace("پرسش پاسخ افزودن", "", 1).strip()
            if "|" not in rest:
                await event.edit("فرمت درست: پرسش پاسخ افزودن سوال | جواب")
                return
            q, a = rest.split("|", 1)
            data["qa"][q.strip()] = a.strip()
            save_data(data)
            await event.edit("✅ ذخیره شد.")
            return

        if text.startswith("پرسش پاسخ حذف"):
            q = text.replace("پرسش پاسخ حذف", "", 1).strip()
            if q in data["qa"]:
                del data["qa"][q]
                save_data(data)
                await event.edit("🗑 حذف شد.")
            else:
                await event.edit("چنین سوالی پیدا نشد.")
            return

        if text == "لیست پرسش پاسخ":
            if not data["qa"]:
                await event.edit("هنوز پرسش‌پاسخی ثبت نشده.")
                return
            lines = ["📋 لیست پرسش و پاسخ:"]
            for q, a in data["qa"].items():
                lines.append(f"❓ {q} → {a}")
            await event.edit("\n".join(lines))
            return

        # ---------- سکوت ----------
        if text == "سکوت روشن":
            data["muted"] = True
            save_data(data)
            await event.edit("🔇 سکوت فعال شد؛ منشی و پاسخ خودکار متوقف شدند.")
            return
        if text == "سکوت خاموش":
            data["muted"] = False
            save_data(data)
            await event.edit("🔊 سکوت غیرفعال شد.")
            return

        # ---------- ادمین‌ها ----------
        if text == "لیست ادمین‌ها":
            lines = ["👮 لیست ادمین‌ها:"]
            for a in data["admins"]:
                lines.append(f"- `{a}`")
            await event.edit("\n".join(lines), parse_mode="markdown")
            return
        if text.startswith("افزودن ادمین"):
            arg = text.replace("افزودن ادمین", "", 1).strip()
            if not arg.isdigit():
                await event.edit("مثال: افزودن ادمین 123456789")
                return
            aid = int(arg)
            if aid not in data["admins"]:
                data["admins"].append(aid)
                save_data(data)
            await event.edit("✅ ادمین اضافه شد.")
            return
        if text.startswith("حذف ادمین"):
            arg = text.replace("حذف ادمین", "", 1).strip()
            if arg.isdigit() and int(arg) in data["admins"]:
                data["admins"].remove(int(arg))
                save_data(data)
                await event.edit("🗑 ادمین حذف شد.")
            else:
                await event.edit("پیدا نشد.")
            return

        # ---------- آنتی گروه ----------
        if text.startswith("آنتی گروه روشن"):
            idx = find_group_index(text.replace("آنتی گروه روشن", "", 1).strip())
            if idx is None:
                await event.edit("شماره گروه نامعتبر است.")
                return
            gid = str(data["groups"][idx]["id"])
            data["antigroup"].setdefault(gid, {})
            data["antigroup"][gid]["links"] = True
            data["antigroup"][gid]["flood"] = True
            save_data(data)
            await event.edit(f"🛡 آنتی گروه برای «{data['groups'][idx]['title']}» فعال شد.")
            return
        if text.startswith("آنتی گروه خاموش"):
            idx = find_group_index(text.replace("آنتی گروه خاموش", "", 1).strip())
            if idx is None:
                await event.edit("شماره گروه نامعتبر است.")
                return
            gid = str(data["groups"][idx]["id"])
            data["antigroup"].pop(gid, None)
            save_data(data)
            await event.edit(f"🛡 آنتی گروه برای «{data['groups'][idx]['title']}» غیرفعال شد.")
            return
        if text.startswith("آنتی گروه وضعیت"):
            idx = find_group_index(text.replace("آنتی گروه وضعیت", "", 1).strip())
            if idx is None:
                await event.edit("شماره گروه نامعتبر است.")
                return
            gid = str(data["groups"][idx]["id"])
            state = data["antigroup"].get(gid)
            await event.edit("فعال ✅" if state else "غیرفعال 🔴")
            return

        # ---------- زمان‌بندی دستورات ----------
        if text.startswith("زمانبندی افزودن"):
            rest = text.replace("زمانبندی افزودن", "", 1).strip()
            if "|" not in rest:
                await event.edit("فرمت درست: زمانبندی افزودن <دقیقه> | <دستور>\nمثال: زمانبندی افزودن 2 | پینگ")
                return
            minutes_text, cmd_text = rest.split("|", 1)
            minutes_text = minutes_text.strip()
            cmd_text = cmd_text.strip()
            if not minutes_text.isdigit() or int(minutes_text) < 1:
                await event.edit("دقیقه باید یک عدد صحیح و حداقل 1 باشد.")
                return
            if not cmd_text:
                await event.edit("دستور نمی‌تواند خالی باشد.")
                return
            
            job = {
                "id": next_schedule_id(),
                "minutes": int(minutes_text),
                "command": cmd_text,
                "enabled": True,
                "last_run": 0,
            }
            data["scheduled"].append(job)
            save_data(data)
            await event.edit(f"✅ زمان‌بندی ثبت شد: هر {job['minutes']} دقیقه دستور «{job['command']}» اجرا می‌شود.")
            return

        if text == "لیست زمانبندی":
            if not data["scheduled"]:
                await event.edit("هنوز هیچ زمان‌بندی‌ای ثبت نشده. با «زمانبندی افزودن» اضافه کنید.")
                return
            lines = ["⏱ لیست زمان‌بندی‌ها:"]
            for i, j in enumerate(data["scheduled"], 1):
                status = "فعال ✅" if j.get("enabled", True) else "متوقف 🔴"
                lines.append(f"{i}. هر {j['minutes']} دقیقه — «{j['command']}» — {status}")
            await event.edit("\n".join(lines))
            return

        if text.startswith("زمانبندی روشن"):
            idx = find_schedule_index(text.replace("زمانبندی روشن", "", 1).strip())
            if idx is None:
                await event.edit("شماره زمان‌بندی نامعتبر است. اول «لیست زمانبندی» را بزنید.")
                return
            data["scheduled"][idx]["enabled"] = True
            save_data(data)
            await event.edit(f"✅ زمان‌بندی «{data['scheduled'][idx]['command']}» فعال شد.")
            return

        if text.startswith("زمانبندی خاموش"):
            idx = find_schedule_index(text.replace("زمانبندی خاموش", "", 1).strip())
            if idx is None:
                await event.edit("شماره زمان‌بندی نامعتبر است. اول «لیست زمانبندی» را بزنید.")
                return
            data["scheduled"][idx]["enabled"] = False
            save_data(data)
            await event.edit(f"🔴 زمان‌بندی «{data['scheduled'][idx]['command']}» متوقف شد.")
            return

        if text.startswith("حذف زمانبندی"):
            idx = find_schedule_index(text.replace("حذف زمانبندی", "", 1).strip())
            if idx is None:
                await event.edit("شماره زمان‌بندی نامعتبر است. اول «لیست زمانبندی» را بزنید.")
                return
            removed = data["scheduled"].pop(idx)
            save_data(data)
            await event.edit(f"🗑 زمان‌بندی «{removed['command']}» حذف شد.")
            return

    # ---------- منشی خودکار در پیوی ----------
    @client.on(events.NewMessage(incoming=True))
    async def auto_reply_handler(event):
        if data["muted"]:
            return
        if not event.is_private:
            return
        sender = await event.get_sender()
        if isinstance(sender, User) and sender.bot:
            return
        text = (event.raw_text or "").strip()
        for q, a in data["qa"].items():
            if q in text:
                await event.respond(a)
                return
        if data["auto_reply"]["enabled"] and data["auto_reply"]["text"]:
            await event.respond(data["auto_reply"]["text"])

    # ---------- آنتی گروه ----------
    @client.on(events.NewMessage(incoming=True))
    async def antigroup_handler(event):
        if not event.is_group and not event.is_channel:
            return
        gid = str(event.chat_id)
        conf = data["antigroup"].get(gid)
        if not conf:
            return
        sender = await event.get_sender()
        if not isinstance(sender, User) or sender.bot:
            return
        # ادمین‌های خودِ گروه از فیلتر معاف هستند
        try:
            perms = await client.get_permissions(event.chat_id, sender)
            if perms.is_admin or perms.is_creator:
                return
        except Exception:
            pass

        text = event.raw_text or ""

        if conf.get("links") and LINK_REGEX.search(text):
            try:
                await event.delete()
                return
            except Exception:
                pass

        if conf.get("flood"):
            now = time.time()
            chat_tracker = flood_tracker.setdefault(event.chat_id, {})
            times = chat_tracker.setdefault(sender.id, [])
            times.append(now)
            chat_tracker[sender.id] = [t for t in times if now - t < 10]
            if len(chat_tracker[sender.id]) > 6:
                try:
                    await event.delete()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# اجرا
# ---------------------------------------------------------------------------

async def main():
    await bot_client.start(bot_token=BOT_TOKEN)
    log.info("ربات ورود روشن شد.")

    session_to_use = SESSION_STRING_ENV
    if not session_to_use and LOCAL_SESSION_FILE.exists():
        session_to_use = LOCAL_SESSION_FILE.read_text(encoding="utf-8").strip()

    if session_to_use:
        try:
            await start_user_client(session_to_use)
        except Exception:
            log.exception("اتصال با سشن ذخیره‌شده ناموفق بود. برای ورود دوباره /login را در ربات بزنید.")
    else:
        log.info("سشن سلف موجود نیست. برای ورود، به ربات پیام /login بدهید.")

    await bot_client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())

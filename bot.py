import io
import os
import re
import sqlite3
import logging
import zipfile
from functools import wraps

from anthropic import AsyncAnthropic
from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
MODEL = os.environ.get("MODEL", "claude-sonnet-5-5")
DB_PATH = os.environ.get("DB_PATH", "bot.db")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("figo")
client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

STACKS = {
    "web": "HTML + CSS + JavaScript (static website, index.html entry)",
    "python": "Python 3 (with requirements.txt and main.py)",
    "android": "Native Android app in Kotlin (Gradle project, Jetpack Compose)",
}

SYSTEM_PROMPT = """You are an expert software engineer that builds complete, working projects.
Rules:
- Output a SHORT explanation first (3-5 lines max) in the requested language.
- Then output EVERY file you create or change in exactly this format:
=== FILE: path/to/file.ext ===
<full file content>
=== END FILE ===
- Always give the FULL content of each file you output, never partial snippets.
- Do not wrap files in markdown code fences.
- Only output files that are new or changed.
- Keep the project small, clean and runnable."""

FILE_RE = re.compile(r"=== FILE: (.+?) ===\n(.*?)\n=== END FILE ===", re.DOTALL)


# ---------------------------------------------------------------- database
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                lang TEXT DEFAULT 'ar',
                stack TEXT DEFAULT 'web',
                current INTEGER
            );
            CREATE TABLE IF NOT EXISTS projects(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT,
                stack TEXT,
                created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS files(
                project_id INTEGER,
                path TEXT,
                content TEXT,
                PRIMARY KEY(project_id, path)
            );
            CREATE TABLE IF NOT EXISTS allowed(user_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
            """
        )


def get_user(uid):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()


def get_kv(k, default=None):
    with db() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default


def set_kv(k, v):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, v))


def project_files(pid):
    with db() as c:
        rows = c.execute("SELECT path, content FROM files WHERE project_id=?", (pid,)).fetchall()
        return {r["path"]: r["content"] for r in rows}


def save_files(pid, files, replace=False):
    with db() as c:
        if replace:
            c.execute("DELETE FROM files WHERE project_id=?", (pid,))
        for path, content in files.items():
            c.execute(
                "INSERT OR REPLACE INTO files(project_id,path,content) VALUES(?,?,?)",
                (pid, path, content),
            )


def current_project(uid):
    u = get_user(uid)
    if not u["current"]:
        return None
    with db() as c:
        return c.execute(
            "SELECT * FROM projects WHERE id=? AND user_id=?", (u["current"], uid)
        ).fetchone()


# ---------------------------------------------------------------- helpers
async def send(update: Update, text: str):
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i : i + 4000])


def guarded(fn):
    @wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if uid != OWNER_ID and get_kv("mode", "public") == "private":
            with db() as c:
                ok = c.execute("SELECT 1 FROM allowed WHERE user_id=?", (uid,)).fetchone()
            if not ok:
                await send(update, "⛔ البوت خاص حاليًا.")
                return
        get_user(uid)
        return await fn(update, context)

    return wrapper


async def generate(uid, instruction, files, stack, lang):
    language = "Arabic" if lang == "ar" else "English"
    existing = ""
    for path, content in files.items():
        existing += f"=== FILE: {path} ===\n{content}\n=== END FILE ===\n"
    prompt = (
        f"Tech stack: {STACKS[stack]}\n"
        f"Reply language: {language}\n\n"
        f"Current project files:\n{existing or '(none yet)'}\n\n"
        f"Task: {instruction}"
    )
    msg = await client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    new_files = {p.strip(): c for p, c in FILE_RE.findall(text)}
    explanation = FILE_RE.sub("", text).strip()[:1000]
    return explanation, new_files


async def run_task(update, context, instruction, replace=False, new_name=None):
    uid = update.effective_user.id
    user = get_user(uid)
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    await send(update, "⏳ جاري العمل، قد يستغرق دقيقة...")
    try:
        if new_name:
            with db() as c:
                cur = c.execute(
                    "INSERT INTO projects(user_id,name,stack) VALUES(?,?,?)",
                    (uid, new_name[:60], user["stack"]),
                )
                pid = cur.lastrowid
                c.execute("UPDATE users SET current=? WHERE user_id=?", (pid, uid))
            old_files, stack = {}, user["stack"]
        else:
            proj = current_project(uid)
            pid, stack = proj["id"], proj["stack"]
            old_files = project_files(pid)
        explanation, files = await generate(uid, instruction, old_files, stack, user["lang"])
    except Exception as e:
        log.exception("generation failed")
        await send(update, f"❌ حدث خطأ: {e}")
        return
    if not files:
        await send(update, "⚠️ لم يتم إنتاج ملفات. جرّب وصفًا أوضح.\n\n" + explanation)
        return
    save_files(pid, files, replace=replace)
    listing = "\n".join(f"• {p}" for p in files)
    await send(update, f"✅ تم.\n\n{explanation}\n\nالملفات:\n{listing}\n\nاستخدم /download لتنزيل المشروع.")


# ---------------------------------------------------------------- commands
@guarded
async def start(update, context):
    await send(
        update,
        "👋 أهلًا! أنا بوت بناء المشاريع بالذكاء الاصطناعي.\n"
        "ابدأ بـ: /new وصف فكرتك\nمثال: /new موقع لمطعم مع قائمة طعام\n\n/help لكل الأوامر.",
    )


@guarded
async def help_cmd(update, context):
    await send(
        update,
        "/new <فكرة> — إنشاء مشروع من فكرة\n"
        "/build <web|python|android> — إعادة بناء المشروع بتقنية أخرى\n"
        "/edit <طلب> — تعديل المشروع الحالي\n"
        "/fix <وصف الخطأ> — إصلاح خطأ\n"
        "/apk — حالة إنشاء APK\n"
        "/download — تنزيل المشروع ZIP\n"
        "/projects [رقم] — عرض مشاريعك أو التبديل لمشروع\n"
        "/reset — حذف المشروع الحالي\n"
        "/settings [lang ar|en] [stack web|python|android]\n"
        "/owner — إعدادات المالك",
    )


@guarded
async def new(update, context):
    idea = " ".join(context.args)
    if not idea:
        await send(update, "اكتب فكرتك بعد الأمر، مثال:\n/new تطبيق لإدارة المهام")
        return
    await run_task(update, context, f"Create a new project for this idea: {idea}", new_name=idea)


@guarded
async def build(update, context):
    uid = update.effective_user.id
    proj = current_project(uid)
    if not proj:
        await send(update, "لا يوجد مشروع حالي. ابدأ بـ /new")
        return
    if not context.args or context.args[0] not in STACKS:
        await send(update, "اختر تقنية: /build web أو /build python أو /build android")
        return
    stack = context.args[0]
    with db() as c:
        c.execute("UPDATE projects SET stack=? WHERE id=?", (stack, proj["id"]))
    await run_task(
        update,
        context,
        f"Rewrite the whole project using the new stack ({STACKS[stack]}), keeping the same features.",
        replace=True,
    )


@guarded
async def edit(update, context):
    if not current_project(update.effective_user.id):
        await send(update, "لا يوجد مشروع حالي. ابدأ بـ /new")
        return
    text = " ".join(context.args)
    if not text:
        await send(update, "اكتب التعديل المطلوب، مثال:\n/edit غيّر اللون الرئيسي إلى الأزرق")
        return
    await run_task(update, context, f"Modify the project: {text}")


@guarded
async def fix(update, context):
    if not current_project(update.effective_user.id):
        await send(update, "لا يوجد مشروع حالي. ابدأ بـ /new")
        return
    text = " ".join(context.args)
    if not text:
        await send(update, "الصق رسالة الخطأ بعد الأمر:\n/fix <الخطأ>")
        return
    await run_task(update, context, f"Fix this error/bug in the project: {text}")


@guarded
async def apk(update, context):
    await send(
        update,
        "📦 بناء APK غير مفعّل بعد. يحتاج سيرفر فيه Android SDK أو GitHub Actions.\n"
        "حاليًا استخدم /download لتنزيل المشروع وابنِه من Android Studio.",
    )


@guarded
async def download(update, context):
    proj = current_project(update.effective_user.id)
    if not proj:
        await send(update, "لا يوجد مشروع حالي.")
        return
    files = project_files(proj["id"])
    if not files:
        await send(update, "المشروع فارغ.")
        return
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, content in files.items():
            z.writestr(path, content)
    buf.seek(0)
    buf.name = f"project_{proj['id']}.zip"
    await update.message.reply_document(buf, caption=proj["name"])


@guarded
async def projects(update, context):
    uid = update.effective_user.id
    if context.args and context.args[0].isdigit():
        pid = int(context.args[0])
        with db() as c:
            ok = c.execute("SELECT 1 FROM projects WHERE id=? AND user_id=?", (pid, uid)).fetchone()
            if ok:
                c.execute("UPDATE users SET current=? WHERE user_id=?", (pid, uid))
        await send(update, f"✅ المشروع الحالي: {pid}" if ok else "❌ مشروع غير موجود.")
        return
    with db() as c:
        rows = c.execute(
            "SELECT id,name,stack FROM projects WHERE user_id=? ORDER BY id DESC LIMIT 20", (uid,)
        ).fetchall()
    if not rows:
        await send(update, "لا توجد مشاريع بعد. ابدأ بـ /new")
        return
    cur = get_user(uid)["current"]
    lines = [f"{'👉 ' if r['id'] == cur else ''}{r['id']} — {r['name']} [{r['stack']}]" for r in rows]
    await send(update, "مشاريعك:\n" + "\n".join(lines) + "\n\nللتبديل: /projects <رقم>")


@guarded
async def reset(update, context):
    uid = update.effective_user.id
    proj = current_project(uid)
    if not proj:
        await send(update, "لا يوجد مشروع حالي.")
        return
    with db() as c:
        c.execute("DELETE FROM files WHERE project_id=?", (proj["id"],))
        c.execute("DELETE FROM projects WHERE id=?", (proj["id"],))
        c.execute("UPDATE users SET current=NULL WHERE user_id=?", (uid,))
    await send(update, "🗑 تم حذف المشروع الحالي.")


@guarded
async def settings(update, context):
    uid = update.effective_user.id
    a = context.args
    if len(a) >= 2:
        key, val = a[0], a[1]
        if key == "lang" and val in ("ar", "en"):
            with db() as c:
                c.execute("UPDATE users SET lang=? WHERE user_id=?", (val, uid))
        elif key == "stack" and val in STACKS:
            with db() as c:
                c.execute("UPDATE users SET stack=? WHERE user_id=?", (val, uid))
        else:
            await send(update, "قيمة غير صحيحة.")
            return
    u = get_user(uid)
    await send(
        update,
        f"⚙️ الإعدادات:\nاللغة: {u['lang']}\nالتقنية الافتراضية: {u['stack']}\n\n"
        "تغيير: /settings lang en\n/settings stack python",
    )


async def owner(update, context):
    uid = update.effective_user.id
    if uid != OWNER_ID:
        await send(update, "⛔ للمالك فقط.")
        return
    a = context.args
    if a and a[0] in ("private", "public"):
        set_kv("mode", a[0])
    elif len(a) == 2 and a[0] == "allow" and a[1].isdigit():
        with db() as c:
            c.execute("INSERT OR IGNORE INTO allowed(user_id) VALUES(?)", (int(a[1]),))
    elif len(a) == 2 and a[0] == "block" and a[1].isdigit():
        with db() as c:
            c.execute("DELETE FROM allowed WHERE user_id=?", (int(a[1]),))
    with db() as c:
        users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        projs = c.execute("SELECT COUNT(*) n FROM projects").fetchone()["n"]
    await send(
        update,
        f"👑 المالك\nالمستخدمون: {users}\nالمشاريع: {projs}\nالوضع: {get_kv('mode', 'public')}\n\n"
        "/owner private | public\n/owner allow <id> | block <id>",
    )


async def post_init(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("start", "بدء استخدام البوت"),
            BotCommand("help", "عرض جميع الأوامر"),
            BotCommand("new", "إنشاء مشروع من فكرة"),
            BotCommand("build", "بناء المشروع أو تغيير تقنيته"),
            BotCommand("edit", "تعديل المشروع الحالي"),
            BotCommand("fix", "إصلاح خطأ في المشروع"),
            BotCommand("apk", "عرض حالة إنشاء APK"),
            BotCommand("download", "تنزيل المشروع بصيغة ZIP"),
            BotCommand("reset", "حذف المشروع الحالي"),
            BotCommand("settings", "إعداد اللغة والتقنية"),
            BotCommand("projects", "عرض مشاريعك"),
            BotCommand("owner", "إعدادات المالك"),
        ]
    )


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).post_init(post_init).build()
    handlers = {
        "start": start, "help": help_cmd, "new": new, "build": build, "edit": edit,
        "fix": fix, "apk": apk, "download": download, "reset": reset,
        "settings": settings, "projects": projects, "owner": owner,
    }
    for name, fn in handlers.items():
        app.add_handler(CommandHandler(name, fn))
    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()

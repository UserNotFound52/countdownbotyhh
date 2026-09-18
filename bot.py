import datetime
import logging
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_FILE = "countdowns.db"

# 设置目标时区（默认 UTC+8，适应北京/马来西亚时间）
LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=8))

# --- 防休眠 Web 服务器 ---

class HealthCheckHandler(BaseHTTPRequestHandler):
    """响应 Render 和外部 Ping 的健康检查请求，防止服务休眠"""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Bot is alive and running!")

    def log_message(self, format, *args):
        # 禁用默认日志，避免控制台被保活请求刷屏
        return

def start_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"防休眠 HTTP 服务已开启，监听端口: {port}")

# --- 数据库操作 ---

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS countdowns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            target_date TEXT NOT NULL,
            notified INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def save_countdown(chat_id: int, name: str, target_date: datetime.date) -> int:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO countdowns (chat_id, name, target_date) VALUES (?, ?, ?)",
        (chat_id, name, target_date.isoformat()),
    )
    conn.commit()
    c_id = cursor.lastrowid
    conn.close()
    return c_id

def get_active_countdowns(chat_id: int = None):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    if chat_id:
        cursor.execute(
            "SELECT id, chat_id, name, target_date FROM countdowns WHERE chat_id = ? AND notified = 0 ORDER BY target_date ASC",
            (chat_id,),
        )
    else:
        cursor.execute(
            "SELECT id, chat_id, name, target_date FROM countdowns WHERE notified = 0 ORDER BY target_date ASC"
        )
    rows = cursor.fetchall()
    conn.close()
    return rows

def mark_as_notified(countdown_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE countdowns SET notified = 1 WHERE id = ?", (countdown_id,))
    conn.commit()
    conn.close()

# --- 每天 06:00 准时播报 ---

async def daily_broadcast(context: ContextTypes.DEFAULT_TYPE):
    active_countdowns = get_active_countdowns()
    if not active_countdowns:
        return

    now_date = datetime.datetime.now(LOCAL_TZ).date()

    for c_id, chat_id, name, target_date_str in active_countdowns:
        target_date = datetime.date.fromisoformat(target_date_str)
        days_left = (target_date - now_date).days

        if days_left < 0:
            mark_as_notified(c_id)
            continue

        msg = (
            f"⏰ {name}\n"
            f"📅 Target: {target_date}\n"
            f"🗓️ {days_left} day(s) remaining"
        )

        try:
            await context.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            logger.error(f"发送播报给群组 {chat_id} 失败: {e}")

        if days_left == 0:
            mark_as_notified(c_id)

# --- 指令处理 ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Countdown Bot Ready!**\n\n"
        "Use `/set <Name> <YYYY-MM-DD>` to add a countdown.",
        parse_mode="Markdown",
    )

async def set_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ Usage: `/set <Name> <YYYY-MM-DD>`\nExample: `/set PSPM 1 2026-10-29`",
            parse_mode="Markdown",
        )
        return

    date_str = context.args[-1]
    name = " ".join(context.args[:-1])

    try:
        target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        await update.message.reply_text("❌ Invalid date format! Use `YYYY-MM-DD`.", parse_mode="Markdown")
        return

    now_date = datetime.datetime.now(LOCAL_TZ).date()
    if target_date < now_date:
        await update.message.reply_text("⚠️ Target date cannot be in the past!")
        return

    chat_id = update.effective_chat.id
    save_countdown(chat_id, name, target_date)

    days_left = (target_date - now_date).days

    preview_msg = (
        f"✅ **Countdown set successfully!**\n\n"
        f"⏰ {name}\n"
        f"📅 Target: {target_date}\n"
        f"🗓️ {days_left} day(s) remaining"
    )
    await update.message.reply_text(preview_msg)

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    active_countdowns = get_active_countdowns(chat_id)

    if not active_countdowns:
        await update.message.reply_text("ℹ️ No active countdowns found.")
        return

    now_date = datetime.datetime.now(LOCAL_TZ).date()
    msg = "📋 **Active Countdowns:**\n\n"

    for c_id, _, name, target_date_str in active_countdowns:
        target_date = datetime.date.fromisoformat(target_date_str)
        days_left = (target_date - now_date).days
        msg += f"⏰ **{name}** — `{target_date}` ({days_left} day(s) remaining)\n"

    await update.message.reply_text(msg, parse_mode="Markdown")

# --- 启动配置 ---

async def post_init(application: Application):
    init_db()

    # 每天 06:00 准时播报
    target_time = datetime.time(hour=6, minute=0, second=0, tzinfo=LOCAL_TZ)
    application.job_queue.run_daily(daily_broadcast, time=target_time)

def main():
    # 开启防休眠 Web 服务
    start_health_check_server()

    TOKEN = "8821535562:AAF30ZPTWkDlJGs_ioqDVBiYQs5hWr36tF8"  # ⚠️ 替换为你的真实 Token

    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("set", set_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("test", daily_broadcast))  # 方便手动测试

    logger.info("Bot 运行中...")
    app.run_polling()

if __name__ == "__main__":
    main()

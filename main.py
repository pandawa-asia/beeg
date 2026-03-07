#!/usr/bin/env python3
import re
import subprocess
import os
import time
import json
from pathlib import Path
from urllib.parse import urlparse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import asyncio
import queue
import threading
import socket
import urllib3
from datetime import datetime
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn
from telegram.error import BadRequest

# ─────────────────────────────────────────────
# ⚙️  KONFIGURASI
# ─────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN tidak ditemukan! Isi di .env: BOT_TOKEN=token_kamu")

ALLOWED_USER_IDS: set = set(
    int(uid.strip())
    for uid in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if uid.strip().isdigit()
)

MAX_RETRIES           = int(os.getenv("MAX_RETRIES", 3))
TIMEOUT               = int(os.getenv("TIMEOUT", 30))
MAX_QUEUE_SIZE        = int(os.getenv("MAX_QUEUE_SIZE", 100))
MAX_WORKERS           = int(os.getenv("MAX_WORKERS", 5))
STATE_FILE            = os.getenv("STATE_FILE", "bot_state.json")
CLEANUP_DAYS          = int(os.getenv("CLEANUP_DAYS", 7))
PROGRESS_THROTTLE_SEC = float(os.getenv("PROGRESS_THROTTLE", 3))

DOWNLOAD_DIR_DEFAULT  = "AsianGirl"
DOWNLOAD_DIR: str     = os.getenv("DOWNLOAD_DIR", "")

# ─────────────────────────────────────────────
# 🔧  Patch IPv4
# ─────────────────────────────────────────────
_orig_getaddrinfo = socket.getaddrinfo
def _patched_getaddrinfo(*args, **kwargs):
    return [r for r in _orig_getaddrinfo(*args, **kwargs) if r[0] == socket.AF_INET]
socket.getaddrinfo = _patched_getaddrinfo

https_pool = urllib3.PoolManager(
    retries=urllib3.Retry(total=3, backoff_factor=2),
    timeout=urllib3.Timeout(connect=30.0, read=30.0),
    maxsize=10, block=False
)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─────────────────────────────────────────────
# 📦  State global
# ─────────────────────────────────────────────
download_queue = queue.Queue(maxsize=MAX_QUEUE_SIZE)

download_stats = {
    'success': 0, 'failed': 0, 'total': 0,
    'queue_size': 0, 'start_time': datetime.now()
}
stats_lock = threading.Lock()

active_chats: dict     = {}
active_chats_lock      = threading.Lock()
telegram_loop          = None

processed_links: set   = set()
processed_lock         = threading.Lock()

active_downloads: dict = {}
downloads_lock         = threading.Lock()

# Riwayat folder per chat — {str(chat_id): ["FolderA", "FolderB", ...]}
folder_history: dict   = {}
folder_history_lock    = threading.Lock()
MAX_FOLDER_HISTORY     = 3

# Link menunggu konfirmasi folder — {chat_id: {...}}
pending_links: dict    = {}
pending_links_lock     = threading.Lock()

# ─────────────────────────────────────────────
# 💾  Persistensi
# ─────────────────────────────────────────────
def save_state():
    try:
        with processed_lock, stats_lock, folder_history_lock:
            data = {
                "processed_links": list(processed_links),
                "download_dir": DOWNLOAD_DIR,
                "folder_history": folder_history,
                "stats": {k: v for k, v in download_stats.items() if k != "start_time"}
            }
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[WARN] Gagal save state: {e}")

def load_state() -> str:
    if not os.path.exists(STATE_FILE):
        return ""
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        with processed_lock:
            processed_links.update(data.get("processed_links", []))
        with folder_history_lock:
            folder_history.update(data.get("folder_history", {}))
        saved = data.get("stats", {})
        with stats_lock:
            for k in ("success", "failed", "total"):
                if k in saved:
                    download_stats[k] = saved[k]
        saved_dir = data.get("download_dir", "")
        print(f"[STATE] Dimuat: {len(processed_links)} link, dir='{saved_dir}'")
        return saved_dir
    except Exception as e:
        print(f"[WARN] Gagal load state: {e}")
        return ""

def apply_download_dir(new_dir: str, chat_id=None):
    """Terapkan folder baru secara global dan catat ke riwayat."""
    global DOWNLOAD_DIR
    DOWNLOAD_DIR = new_dir
    Path(DOWNLOAD_DIR).mkdir(exist_ok=True)
    if chat_id and new_dir != DOWNLOAD_DIR_DEFAULT:
        with folder_history_lock:
            history = folder_history.setdefault(str(chat_id), [])
            if new_dir in history:
                history.remove(new_dir)
            history.insert(0, new_dir)
            folder_history[str(chat_id)] = history[:MAX_FOLDER_HISTORY]
    save_state()
    print(f"[FOLDER] Diset: '{DOWNLOAD_DIR}'")

def get_folder_history(chat_id) -> list:
    with folder_history_lock:
        return list(folder_history.get(str(chat_id), []))

# ─────────────────────────────────────────────
# 🧹  Cleanup
# ─────────────────────────────────────────────
def cleanup_old_files():
    if not DOWNLOAD_DIR:
        return
    cutoff = time.time() - (CLEANUP_DAYS * 86400)
    removed = 0
    for f in Path(DOWNLOAD_DIR).glob("*.mp4"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except Exception:
            pass
    if removed:
        print(f"[CLEANUP] {removed} file dihapus (>{CLEANUP_DAYS} hari)")

# ─────────────────────────────────────────────
# 🎨  Rich Progress
# ─────────────────────────────────────────────
progress_bar = Progress(
    SpinnerColumn(),
    TextColumn("[progress.description]{task.description}"),
    BarColumn(), DownloadColumn(),
    TransferSpeedColumn(), TimeRemainingColumn(),
    expand=True
)
SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
def get_spinner(i): return SPINNER_FRAMES[i % len(SPINNER_FRAMES)]

# ─────────────────────────────────────────────
# 🛠️  Helper
# ─────────────────────────────────────────────
def sanitize_filename(fn: str) -> str:
    return " ".join(re.sub(r'[\\/*?:"<>|]', "", fn)[:200].split()).strip()

def sanitize_foldername(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def extract_filename_from_url(url: str) -> str:
    try:
        fn = os.path.basename(urlparse(url).path)
        if fn.endswith('.mp4'):
            fn = sanitize_filename(fn)
            return fn if fn.lower().endswith('.mp4') else fn + '.mp4'
    except Exception:
        pass
    return f"video_{int(time.time())}.mp4"

def get_file_size_mb(fp: str) -> float:
    try:    return os.path.getsize(fp) / (1024 * 1024)
    except: return 0.0

def format_duration(s: float) -> str:
    h, m, sec = int(s // 3600), int((s % 3600) // 60), int(s % 60)
    if h:  return f"{h}j {m}m {sec}d"
    if m:  return f"{m}m {sec}d"
    return f"{sec}d"

# ─────────────────────────────────────────────
# 🌐  Dynamic Referer detector
# ─────────────────────────────────────────────
def get_referer(url: str) -> str:
    CDN_REFERER_MAP = {
        "xhpingcdn.com":   "https://xhamster.com/",
        "xhamster.com":    "https://xhamster.com/",
        "phncdn.com":      "https://www.pornhub.com/",
        "pornhub.com":     "https://www.pornhub.com/",
        "beeg.com":        "https://beeg.com/",
        "beegcdn.com":     "https://beeg.com/",
        "xvideos-cdn.com": "https://www.xvideos.com/",
        "xvideos.com":     "https://www.xvideos.com/",
        "xnxx-cdn.com":    "https://www.xnxx.com/",
        "xnxx.com":        "https://www.xnxx.com/",
        "rdtcdn.com":      "https://www.redtube.com/",
        "redtube.com":     "https://www.redtube.com/",
        "spankbang.com":   "https://spankbang.com/",
        "eporner.com":     "https://www.eporner.com/",
        "tnaflix.com":     "https://www.tnaflix.com/",
    }
    try:
        host = urlparse(url).hostname or ""
        for domain, referer in CDN_REFERER_MAP.items():
            if host == domain or host.endswith("." + domain):
                return referer
        # Fallback: pakai origin domain URL itu sendiri
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.hostname}/"
    except Exception:
        return "https://www.google.com/"


# ─────────────────────────────────────────────
# 📥  Download engine
# ─────────────────────────────────────────────
def download_video(url, filename, folder, chat_id=None, retry=0, message_id=None):
    if retry >= MAX_RETRIES:
        return False, None, "Maksimal retry tercapai"
    if retry > 0:
        time.sleep(2 ** retry)

    filepath = os.path.join(folder, filename)
    command = [
        'yt-dlp', '-4', '-f', 'best',
        '--no-warnings', '--no-check-certificates',
        '--progress-template',
        '[download] %(progress._percent_str)s at %(progress._speed_str)s '
        'ETA %(progress._eta_hms)s '
        '(frag %(progress._fragment_index)d/%(progress._fragment_count)d)',
        '--add-header', 'User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        '--add-header', f'Referer:{get_referer(url)}',   # ← dinamis
        '--newline', '-o', filepath, url
    ]

    try:
        start_time = time.time()
        task_id = progress_bar.add_task(
            f"[cyan]Worker-{threading.current_thread().name[-1]} [white]{filename}",
            total=100
        )
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, bufsize=1
        )

        last_step = -1
        last_tg   = 0.0
        spinner   = 0

        for line in process.stdout:
            line = line.strip()
            if not line or '[download]' not in line or '%' not in line:
                continue
            m = re.search(r'(\d+(?:\.\d+)?)%', line)
            if not m:
                continue

            pct  = float(m.group(1))
            progress_bar.update(task_id, completed=pct)

            step = int(pct // 10)
            now  = time.time()
            if step > last_step and (now - last_tg) >= PROGRESS_THROTTLE_SEC:
                last_step = step
                last_tg   = now
                sm    = re.search(r'at\s+([\d.]+\s*\w+/s)', line)
                speed = sm.group(1).strip() if sm else "0 B/s"
                em    = re.search(r'ETA\s+(\d+:\d+|\w+)', line)
                eta   = em.group(1) if em else "??"
                bar   = "█" * int(pct / 4) + "░" * (25 - int(pct / 4))
                send_message_sync(
                    chat_id,
                    f"{get_spinner(spinner)} *Downloading...*\n"
                    f"📁 `{filename}`\n"
                    f"💾 Folder: `{folder}`\n\n"
                    f"{bar}\n"
                    f"`{pct:.0f}%` • `{speed}` • ETA `{eta}`",
                    message_id=message_id
                )
                spinner += 1

        process.wait()
        progress_bar.remove_task(task_id)
        elapsed = time.time() - start_time

        if process.returncode == 0 and os.path.exists(filepath) and os.path.getsize(filepath) > 1024:
            save_state()
            return True, filepath, f"✅ {get_file_size_mb(filepath):.2f} MB • {format_duration(elapsed)}"

        if os.path.exists(filepath):
            os.remove(filepath)
        return download_video(url, filename, folder, chat_id, retry + 1, message_id=message_id)

    except Exception as e:
        print(f"[ERROR] {e}")
        if os.path.exists(filepath):
            os.remove(filepath)
        return download_video(url, filename, folder, chat_id, retry + 1, message_id=message_id)

# ─────────────────────────────────────────────
# 📨  Telegram messaging
# ─────────────────────────────────────────────
async def send_telegram_message(chat_id, text, message_id=None, reply_markup=None):
    try:
        with active_chats_lock:
            app = active_chats.get(chat_id)
        if app:
            if message_id:
                return await app.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id,
                    text=text, parse_mode='Markdown', reply_markup=reply_markup
                )
            return await app.bot.send_message(
                chat_id=chat_id, text=text,
                parse_mode='Markdown', reply_markup=reply_markup
            )
    except Exception as e:
        print(f"[ERROR] Kirim: {e}")
    return None

def send_message_sync(chat_id, text, message_id=None, reply_markup=None):
    try:
        if telegram_loop and telegram_loop.is_running():
            f = asyncio.run_coroutine_threadsafe(
                send_telegram_message(chat_id, text, message_id, reply_markup),
                telegram_loop
            )
            return f.result(timeout=10)
    except Exception as e:
        print(f"[ERROR] Sync: {e}")
    return None

# ─────────────────────────────────────────────
# 👷  Worker — folder ikut di setiap item queue
# ─────────────────────────────────────────────
def worker():
    while True:
        item = download_queue.get()
        if item is None:
            break

        url, custom_name, chat_id, folder = item

        with stats_lock:
            download_stats['queue_size'] = download_queue.qsize()

        filename = custom_name or extract_filename_from_url(url)

        with downloads_lock:
            active_downloads.setdefault(chat_id, []).append(filename)

        msg = send_message_sync(
            chat_id,
            f"⏬ *Memulai download...*\n"
            f"📁 `{filename}`\n"
            f"💾 Folder: `{folder}`\n"
            f"📊 Antrian: {download_queue.qsize()}"
        )
        message_id = msg.message_id if msg else None

        success, path, status = download_video(
            url, filename, folder, chat_id, message_id=message_id
        )

        with downloads_lock:
            lst = active_downloads.get(chat_id, [])
            if filename in lst:
                lst.remove(filename)

        with stats_lock:
            if success:
                download_stats['success'] += 1
                send_message_sync(
                    chat_id,
                    f"✅ *Download Berhasil!*\n"
                    f"📁 `{filename}`\n"
                    f"💾 `{folder}`\n"
                    f"ℹ️ {status}",
                    message_id=message_id
                )
            else:
                download_stats['failed'] += 1
                send_message_sync(
                    chat_id,
                    f"❌ *Download Gagal*\n"
                    f"📁 `{filename}`\n"
                    f"⚠️ {status}",
                    message_id=message_id
                )
            download_stats['queue_size'] = download_queue.qsize()

        save_state()
        download_queue.task_done()

def worker_wrapper(wid):
    while True:
        try:
            worker()
        except Exception as e:
            print(f"  ⚠️ Worker-{wid} crash: {e} — restart 5s...")
            time.sleep(5)

# ─────────────────────────────────────────────
# 🔐  Auth decorator
# ─────────────────────────────────────────────
def require_auth(handler):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if ALLOWED_USER_IDS:
            uid = update.effective_user.id if update.effective_user else None
            if uid not in ALLOWED_USER_IDS:
                if update.message:
                    await update.message.reply_text("⛔ Akses ditolak.")
                elif update.callback_query:
                    await update.callback_query.answer("⛔ Akses ditolak.", show_alert=True)
                return
        return await handler(update, context)
    wrapper.__name__ = handler.__name__
    return wrapper

# ─────────────────────────────────────────────
# ⌨️  Keyboards
# ─────────────────────────────────────────────
def get_main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📊 Statistik"),    KeyboardButton("📋 Antrian")],
        [KeyboardButton("📁 Ganti Folder"), KeyboardButton("🗑️ Clear Cache")],
        [KeyboardButton("❓ Bantuan")]
    ], resize_keyboard=True)

def get_inline_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Statistik",    callback_data="stats"),
         InlineKeyboardButton("📋 Antrian",      callback_data="queue")],
        [InlineKeyboardButton("📁 Ganti Folder", callback_data="change_folder"),
         InlineKeyboardButton("❓ Help",          callback_data="help")],
        [InlineKeyboardButton("🔄 Refresh",      callback_data="refresh")]
    ])

def get_stats_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="stats"),
         InlineKeyboardButton("🗑️ Clear",  callback_data="clear_stats")],
        [InlineKeyboardButton("🔙 Menu",    callback_data="menu")]
    ])

def get_queue_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="queue")],
        [InlineKeyboardButton("🔙 Menu",    callback_data="menu")]
    ])

def build_folder_choice_keyboard(chat_id) -> InlineKeyboardMarkup:
    """
    Keyboard pilihan folder — muncul setiap kali link dikirim.
    Urutan:
      1. Folder aktif saat ini
      2. Riwayat folder (maks 3, skip duplikat & default)
      3. Folder default (jika belum tampil)
      4. Buat folder baru
    """
    rows  = []
    shown = set()

    # ── 1. Folder aktif ──
    if DOWNLOAD_DIR:
        icon  = "🏠" if DOWNLOAD_DIR == DOWNLOAD_DIR_DEFAULT else "📂"
        rows.append([InlineKeyboardButton(
            f"{icon} Lanjutkan  ·  {DOWNLOAD_DIR}",
            callback_data=f"dlf_use|{DOWNLOAD_DIR}"
        )])
        shown.add(DOWNLOAD_DIR)

    # ── 2. Riwayat ──
    for h in get_folder_history(chat_id):
        if h not in shown and h != DOWNLOAD_DIR_DEFAULT:
            rows.append([InlineKeyboardButton(
                f"🕒 {h}", callback_data=f"dlf_use|{h}"
            )])
            shown.add(h)

    # ── 3. Default (jika belum tampil) ──
    if DOWNLOAD_DIR_DEFAULT not in shown:
        rows.append([InlineKeyboardButton(
            f"🏠 Default  ·  {DOWNLOAD_DIR_DEFAULT}",
            callback_data=f"dlf_use|{DOWNLOAD_DIR_DEFAULT}"
        )])

    # ── 4. Buat folder baru ──
    rows.append([InlineKeyboardButton(
        "✏️ Folder baru…", callback_data="dlf_new"
    )])

    return InlineKeyboardMarkup(rows)

def build_global_folder_keyboard() -> InlineKeyboardMarkup:
    """Keyboard untuk /folder — ubah folder aktif secara global."""
    rows  = []
    shown = set()

    if DOWNLOAD_DIR:
        rows.append([InlineKeyboardButton(
            f"✅ Tetap pakai  ·  {DOWNLOAD_DIR}",
            callback_data=f"gf_use|{DOWNLOAD_DIR}"
        )])
        shown.add(DOWNLOAD_DIR)

    if DOWNLOAD_DIR_DEFAULT not in shown:
        rows.append([InlineKeyboardButton(
            f"🏠 Default  ·  {DOWNLOAD_DIR_DEFAULT}",
            callback_data=f"gf_use|{DOWNLOAD_DIR_DEFAULT}"
        )])

    rows.append([InlineKeyboardButton(
        "✏️ Ketik nama folder baru", callback_data="gf_new"
    )])
    return InlineKeyboardMarkup(rows)

# ─────────────────────────────────────────────
# 🛎️  Queue helper
# ─────────────────────────────────────────────
def queue_links_to_download(links: list, chat_id: int, folder: str):
    added = dupes = 0
    full  = False
    for link in links:
        with processed_lock:
            if link in processed_links:
                dupes += 1
                continue
            processed_links.add(link)
        try:
            download_queue.put((link, None, chat_id, folder), timeout=1)
            added += 1
            with stats_lock:
                download_stats['total'] += 1
        except queue.Full:
            full = True
            break
    return added, dupes, full

def format_queue_result(added: int, dupes: int, full: bool, folder: str) -> str:
    result = (
        f"✅ *{added} link* dimasukkan antrian\n"
        f"💾 Folder: `{folder}`\n"
    )
    if dupes: result += f"⏭️ Duplikat: {dupes}\n"
    if full:  result += "⚠️ Beberapa link gagal (antrian penuh)\n"
    result += f"📊 Posisi antrian: {download_queue.qsize()}"
    return result

# ─────────────────────────────────────────────
# 🤖  Command handlers
# ─────────────────────────────────────────────
@require_auth
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with active_chats_lock:
        active_chats[chat_id] = context.application

    folder_info = f"`{DOWNLOAD_DIR}`" if DOWNLOAD_DIR else "_belum diset_"
    await update.message.reply_text(
        "🎬 *Video Downloader Bot*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🚀 *Bot siap digunakan!*\n\n"
        "📥 *Cara Menggunakan:*\n"
        "1️⃣ Kirim link video\n"
        "2️⃣ Pilih folder tujuan download\n"
        "3️⃣ Tunggu notifikasi selesai\n\n"
        f"📁 *Folder aktif:* {folder_info}\n\n"
        "Kirim link atau pilih menu! 👇",
        parse_mode='Markdown',
        reply_markup=get_main_keyboard()
    )
    await update.message.reply_text(
        "📌 *Quick Menu:*",
        parse_mode='Markdown',
        reply_markup=get_inline_menu_keyboard()
    )

@require_auth
async def folder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ubah folder aktif global — /folder atau tombol."""
    chat_id = update.effective_chat.id
    with active_chats_lock:
        active_chats[chat_id] = context.application

    folder_info = f"`{DOWNLOAD_DIR}`" if DOWNLOAD_DIR else "_belum diset_"
    text = (
        "📁 *Ganti Folder Aktif*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📌 Saat ini: {folder_info}\n\n"
        "Pilih folder atau buat baru:"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text, parse_mode='Markdown',
            reply_markup=build_global_folder_keyboard()
        )
    else:
        await update.message.reply_text(
            text, parse_mode='Markdown',
            reply_markup=build_global_folder_keyboard()
        )

@require_auth
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with stats_lock:
        uptime = datetime.now() - download_stats['start_time']
        total  = download_stats['success'] + download_stats['failed']
        rate   = (download_stats['success'] / total * 100) if total else 0
        text   = (
            "📊 *STATISTIK DOWNLOAD*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"✅ *Berhasil:* {download_stats['success']}\n"
            f"❌ *Gagal:* {download_stats['failed']}\n"
            f"📈 *Total:* {total}\n"
            f"🎯 *Success Rate:* {rate:.1f}%\n\n"
            f"📋 *Antrian:* {download_stats['queue_size']}\n"
            f"👷 *Workers:* {MAX_WORKERS}\n"
            f"📁 *Folder:* `{DOWNLOAD_DIR or 'belum diset'}`\n"
            f"🔗 *Cache Links:* {len(processed_links)}\n\n"
            f"⏱️ *Uptime:* {format_duration(uptime.total_seconds())}\n"
            f"🕐 *Start:* {download_stats['start_time'].strftime('%d/%m %H:%M')}"
        )
    if update.callback_query:
        try:
            await update.callback_query.answer("Statistik diperbarui!")
            await update.callback_query.edit_message_text(
                text, parse_mode='Markdown', reply_markup=get_stats_keyboard()
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise
    else:
        await update.message.reply_text(
            text, parse_mode='Markdown', reply_markup=get_stats_keyboard()
        )

@require_auth
async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    qs   = download_queue.qsize()
    acts = []
    with downloads_lock:
        for files in active_downloads.values():
            acts.extend(files[:3])

    text = (
        "📋 *STATUS ANTRIAN*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 *Total Antrian:* {qs}\n"
        f"👷 *Worker Aktif:* {MAX_WORKERS}\n"
        f"📊 *Kapasitas:* {MAX_QUEUE_SIZE}\n"
        f"📈 *Slot Tersisa:* {MAX_QUEUE_SIZE - qs}\n\n"
    )
    if acts:
        text += "⚡ *Sedang Diproses:*\n"
        for i, f in enumerate(acts[:3], 1):
            s = f[:30] + "..." if len(f) > 30 else f
            text += f"{i}. `{s}`\n"
    else:
        text += "✨ *Tidak ada download aktif*"

    if update.callback_query:
        try:
            await update.callback_query.answer("Antrian diperbarui!")
            await update.callback_query.edit_message_text(
                text, parse_mode='Markdown', reply_markup=get_queue_keyboard()
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise
    else:
        await update.message.reply_text(
            text, parse_mode='Markdown', reply_markup=get_queue_keyboard()
        )

@require_auth
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ *PANDUAN PENGGUNAAN*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "*📝 Commands:*\n"
        "`/start`  - Mulai bot\n"
        "`/stats`  - Statistik\n"
        "`/queue`  - Info antrian\n"
        "`/folder` - Ganti folder aktif\n"
        "`/clear`  - Clear cache link\n"
        "`/help`   - Panduan ini\n\n"
        "*🎯 Cara Pakai:*\n"
        "1. Kirim link video ke chat\n"
        "2. Bot tanya mau simpan ke folder mana:\n"
        "   📂 Lanjutkan di folder aktif\n"
        "   🕒 Pilih dari riwayat folder\n"
        "   🏠 Pilih folder default\n"
        "   ✏️ Ketik nama folder baru\n"
        "3. Download mulai otomatis\n\n"
        "*⚙️ Spesifikasi:*\n"
        f"• Folder aktif : `{DOWNLOAD_DIR or 'belum diset'}`\n"
        f"• Max antrian  : {MAX_QUEUE_SIZE}\n"
        f"• Workers      : {MAX_WORKERS}\n"
        f"• Retry        : {MAX_RETRIES}x\n"
        f"• Cleanup      : >{CLEANUP_DAYS} hari"
    )
    kb = [[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]
    if update.callback_query:
        try:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb)
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise
    else:
        await update.message.reply_text(
            text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb)
        )

@require_auth
async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with processed_lock:
        count = len(processed_links)
        processed_links.clear()
    save_state()
    text = (
        "🗑️ *CACHE CLEARED*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"✅ {count} link dihapus dari cache\n\n"
        "Link bisa di-download ulang sekarang!"
    )
    kb = [[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]
    if update.callback_query:
        await update.callback_query.answer("Cache dibersihkan!")
        await update.callback_query.edit_message_text(
            text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb)
        )
    else:
        await update.message.reply_text(
            text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb)
        )

async def clear_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with stats_lock:
        download_stats.update({
            'success': 0, 'failed': 0, 'total': 0,
            'start_time': datetime.now()
        })
    save_state()
    await update.callback_query.answer("Statistik direset!")
    await stats_command(update, context)

# ─────────────────────────────────────────────
# 🔘  Callback handler
# ─────────────────────────────────────────────
@require_auth
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    data    = query.data
    chat_id = update.effective_chat.id

    # ── Pilih folder untuk download link yang pending ──
    if data.startswith("dlf_use|"):
        chosen = data.split("|", 1)[1]
        with pending_links_lock:
            pending = pending_links.pop(chat_id, None)
        if not pending:
            await query.answer("⚠️ Sesi habis, kirim link lagi.")
            await query.edit_message_text("⚠️ Sesi habis. Silakan kirim link lagi.")
            return
        links = pending.get("links", [])
        apply_download_dir(chosen, chat_id)
        added, dupes, full = queue_links_to_download(links, chat_id, chosen)
        await query.answer(f"✅ Folder: {chosen}")
        await query.edit_message_text(
            format_queue_result(added, dupes, full, chosen),
            parse_mode='Markdown'
        )

    elif data == "dlf_new":
        # User mau ketik folder baru untuk link ini
        with pending_links_lock:
            if chat_id in pending_links:
                pending_links[chat_id]["waiting_custom"] = True
        await query.answer()
        await query.edit_message_text(
            "✏️ *Ketik nama folder baru:*\n\n"
            "Contoh: `VideoKoleksi`, `Film_2024`\n\n"
            "⚠️ Hindari: `/ \\ : * ? \" < > |`",
            parse_mode='Markdown'
        )

    # ── Ubah folder aktif global ──
    elif data.startswith("gf_use|"):
        chosen = data.split("|", 1)[1]
        apply_download_dir(chosen, chat_id)
        await query.answer(f"✅ Folder: {chosen}")
        await query.edit_message_text(
            f"✅ *Folder Aktif Diubah*\n\n"
            f"📁 `{chosen}`\n\n"
            "Folder ini akan muncul sebagai pilihan pertama saat kirim link.",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Menu", callback_data="menu")]
            ])
        )

    elif data == "gf_new":
        with pending_links_lock:
            pending_links[chat_id] = {
                "links": [], "waiting_custom": True, "global_folder": True
            }
        await query.answer()
        await query.edit_message_text(
            "✏️ *Ketik nama folder baru:*\n\n"
            "Contoh: `VideoKoleksi`, `Film_2024`\n\n"
            "⚠️ Hindari: `/ \\ : * ? \" < > |`",
            parse_mode='Markdown'
        )

    # ── Navigasi menu ──
    elif data == "stats":           await stats_command(update, context)
    elif data == "queue":           await queue_command(update, context)
    elif data == "help":            await help_command(update, context)
    elif data == "clear_cache":     await clear_command(update, context)
    elif data == "clear_stats":     await clear_stats_callback(update, context)
    elif data == "change_folder":   await folder_command(update, context)
    elif data == "menu":
        await query.answer()
        folder_info = f"`{DOWNLOAD_DIR}`" if DOWNLOAD_DIR else "_belum diset_"
        await query.edit_message_text(
            "🎬 *Video Downloader Bot*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📁 Folder aktif: {folder_info}\n\n"
            "Pilih menu atau kirim link! 👇",
            parse_mode='Markdown',
            reply_markup=get_inline_menu_keyboard()
        )
    elif data == "refresh":
        await query.answer("Menu diperbarui!")
        await query.edit_message_reply_markup(reply_markup=get_inline_menu_keyboard())
    else:
        await query.answer()

# ─────────────────────────────────────────────
# 💬  Text message handler
# ─────────────────────────────────────────────
@require_auth
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    text    = update.message.text.strip()

    with active_chats_lock:
        active_chats[chat_id] = context.application

    # ── Cek apakah menunggu input nama folder custom ──
    waiting_custom  = False
    is_global_setup = False
    with pending_links_lock:
        pend = pending_links.get(chat_id, {})
        waiting_custom  = pend.get("waiting_custom", False)
        is_global_setup = pend.get("global_folder", False)

    if waiting_custom:
        folder_name = sanitize_foldername(text)
        if not folder_name:
            await update.message.reply_text(
                "❌ *Nama folder tidak valid.* Coba lagi.",
                parse_mode='Markdown'
            )
            return

        if is_global_setup:
            # Ubah folder aktif global
            apply_download_dir(folder_name, chat_id)
            with pending_links_lock:
                pending_links.pop(chat_id, None)
            await update.message.reply_text(
                f"✅ *Folder Aktif Diubah*\n\n"
                f"📁 `{DOWNLOAD_DIR}`\n\n"
                "Folder ini akan muncul sebagai pilihan pertama saat kirim link.",
                parse_mode='Markdown',
                reply_markup=get_main_keyboard()
            )
        else:
            # Lanjutkan download ke folder baru yang diketik
            with pending_links_lock:
                pend = pending_links.pop(chat_id, {})
            links = pend.get("links", [])
            apply_download_dir(folder_name, chat_id)
            added, dupes, full = queue_links_to_download(links, chat_id, folder_name)
            await update.message.reply_text(
                format_queue_result(added, dupes, full, folder_name),
                parse_mode='Markdown'
            )
        return

    # ── Tombol keyboard utama ──
    if text == "📊 Statistik":
        await stats_command(update, context); return
    elif text == "📋 Antrian":
        await queue_command(update, context); return
    elif text == "📁 Ganti Folder":
        await folder_command(update, context); return
    elif text == "🗑️ Clear Cache":
        await clear_command(update, context); return
    elif text == "❓ Bantuan":
        await help_command(update, context); return

    # ── Deteksi link ──
    links = re.findall(
        r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+',
        text
    )

    if not links:
        await update.message.reply_text(
            "❌ *Tidak Ada Link*\n\n"
            "Tidak ditemukan link yang valid.\n\n"
            "Contoh:\n"
            "• `https://example.com/video.mp4`\n"
            "• `https://example.com/stream.m3u8`",
            parse_mode='Markdown'
        )
        return

    # ── Simpan link & tanya folder ──
    with pending_links_lock:
        pending_links[chat_id] = {
            "links": links,
            "waiting_custom": False,
            "global_folder": False
        }

    count   = len(links)
    preview = f"`{links[0][:60]}{'...' if len(links[0]) > 60 else ''}`"
    suffix  = f" *(+{count - 1} lainnya)*" if count > 1 else ""

    await update.message.reply_text(
        f"🔗 *{count} link diterima*{suffix}\n"
        f"{preview}\n\n"
        "📁 *Simpan ke folder mana?*",
        parse_mode='Markdown',
        reply_markup=build_folder_choice_keyboard(chat_id)
    )

# ─────────────────────────────────────────────
# 🚀  Entry point
# ─────────────────────────────────────────────
def run_bot():
    global DOWNLOAD_DIR

    saved_dir = load_state()
    if not DOWNLOAD_DIR and saved_dir:
        DOWNLOAD_DIR = saved_dir
        print(f"[FOLDER] Dimuat dari state: '{DOWNLOAD_DIR}'")

    if DOWNLOAD_DIR:
        Path(DOWNLOAD_DIR).mkdir(exist_ok=True)
        cleanup_old_files()

    progress_bar.start()

    print(f"🚀 Starting {MAX_WORKERS} workers...")
    for i in range(MAX_WORKERS):
        t = threading.Thread(
            target=worker_wrapper, args=(i + 1,),
            daemon=True, name=f"Worker-{i + 1}"
        )
        t.start()
        print(f"  ✅ Worker-{i + 1} started")

    try:
        app = (
            Application.builder().token(BOT_TOKEN)
            .read_timeout(30).write_timeout(30)
            .connect_timeout(30).pool_timeout(30)
            .build()
        )

        app.add_handler(CommandHandler("start",  start_command))
        app.add_handler(CommandHandler("stats",  stats_command))
        app.add_handler(CommandHandler("queue",  queue_command))
        app.add_handler(CommandHandler("folder", folder_command))
        app.add_handler(CommandHandler("help",   help_command))
        app.add_handler(CommandHandler("clear",  clear_command))
        app.add_handler(CallbackQueryHandler(button_callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

        async def main_bot():
            global telegram_loop
            telegram_loop = asyncio.get_running_loop()
            await app.initialize()
            await app.start()

            print("\n" + "=" * 50)
            print("✅ BOT RUNNING SUCCESSFULLY!")
            print("=" * 50)
            print(f"📁 Folder     : {DOWNLOAD_DIR or '(belum diset)'}")
            print(f"📊 Workers    : {MAX_WORKERS}")
            print(f"📦 Max queue  : {MAX_QUEUE_SIZE}")
            print(f"⏱️ Timeout    : {TIMEOUT}s")
            print(f"🔄 Retries    : {MAX_RETRIES}x")
            print(f"🔐 Auth users : {len(ALLOWED_USER_IDS) if ALLOWED_USER_IDS else 'SEMUA'}")
            print("=" * 50 + "\n")

            try:
                await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
                while True:
                    await asyncio.sleep(3600)
            except KeyboardInterrupt:
                print("\n⚠️ Shutdown...")
            except Exception as e:
                print(f"❌ Polling: {e}")
            finally:
                save_state()
                try:
                    await app.updater.stop()
                    await app.stop()
                    await app.shutdown()
                    print("✅ Bot stopped cleanly")
                except Exception as e:
                    print(f"⚠️ {e}")

        asyncio.run(main_bot())

    except KeyboardInterrupt:
        print("\n⚠️ Dihentikan (Ctrl+C)")
    except Exception as e:
        print(f"❌ [CRITICAL] {e}")
        import traceback
        traceback.print_exc()
    finally:
        save_state()
        progress_bar.stop()

if __name__ == "__main__":
    print("🎬 Video Downloader Bot Starting...")
    print("=" * 50)
    run_bot()
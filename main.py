#!/usr/bin/env python3
import re
import subprocess
import os
import time
import json
from pathlib import Path
from urllib.parse import urlparse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
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

DOWNLOAD_DIR_DEFAULT  = "Downloads/AsianGirl"
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

# Riwayat folder per chat
folder_history: dict   = {}
folder_history_lock    = threading.Lock()
MAX_FOLDER_HISTORY     = 3

# Link menunggu konfirmasi folder — {chat_id: {...}}
pending_links: dict    = {}
pending_links_lock     = threading.Lock()

# Mode hapus link dari cache — {chat_id: True}
remove_mode: dict      = {}
remove_mode_lock       = threading.Lock()

# Tracking URL yang pernah gagal — untuk koreksi stats saat retry sukses
failed_urls: set       = set()
failed_urls_lock       = threading.Lock()

# Shutdown signal untuk graceful cleanup
shutdown_event         = threading.Event()
log_file               = "bot_activity.log"

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
# 🎨  Rich Progress & Logging
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

def log_activity(msg: str):
    """Log activity ke file dan console"""
    timestamp = datetime.now().strftime("%d/%m %H:%M:%S")
    log_msg = f"[{timestamp}] {msg}"
    print(log_msg)
    try:
        with open(log_file, "a") as f:
            f.write(log_msg + "\n")
    except: pass

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
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.hostname}/"
    except Exception:
        return "https://www.google.com/"

# ─────────────────────────────────────────────
# 📥  Download engine
# ─────────────────────────────────────────────
def download_video(url, filename, folder, chat_id=None, retry=0, message_id=None):
    if shutdown_event.is_set():
        return False, None, "Bot sedang shutdown"
    
    if retry >= MAX_RETRIES:
        with failed_urls_lock:
            failed_urls.add(url)
        log_activity(f"[FINAL_FAIL] {url[:80]}... setelah {MAX_RETRIES} retry")
        return False, None, "Maksimal retry tercapai"
    
    if retry > 0:
        wait_time = min(2 ** retry, 30)  # Cap at 30s
        log_activity(f"[RETRY {retry}] {url[:80]}... tunggu {wait_time}s")
        time.sleep(wait_time)

    filepath = os.path.join(folder, filename)
    command = [
        'yt-dlp', '-4', '-f', 'best',
        '--no-warnings', '--no-check-certificates',
        '--progress-template',
        '[download] %(progress._percent_str)s at %(progress._speed_str)s '
        'ETA %(progress._eta_hms)s '
        '(frag %(progress._fragment_index)d/%(progress._fragment_count)d)',
        '--add-header', 'User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        '--add-header', f'Referer:{get_referer(url)}',
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

    except subprocess.TimeoutExpired:
        log_activity(f"[TIMEOUT] {url[:80]}... ({TIMEOUT}s)")
        if os.path.exists(filepath):
            os.remove(filepath)
        return download_video(url, filename, folder, chat_id, retry + 1, message_id=message_id)
    except Exception as e:
        log_activity(f"[ERROR] {url[:80]}... {str(e)[:80]}")
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
# 👷  Worker
# ─────────────────────────────────────────────
def worker():
    while not shutdown_event.is_set():
        try:
            item = download_queue.get(timeout=2)
            if item is None:
                break
        except queue.Empty:
            continue
        except Exception as e:
            log_activity(f"[WORKER_ERROR] {str(e)[:100]}")
            continue

        try:
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
                    # Jika ini retry dari yang pernah gagal, kurangi failed
                    with failed_urls_lock:
                        if url in failed_urls:
                            failed_urls.discard(url)
                            download_stats['failed'] = max(0, download_stats['failed'] - 1)
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
                    # Catat URL gagal & hapus dari cache agar bisa di-retry
                    with processed_lock:
                        processed_links.discard(url)
                    with failed_urls_lock:
                        failed_urls.add(url)
                    send_message_sync(
                        chat_id,
                        f"❌ *Download Gagal*\n"
                        f"📁 `{filename}`\n"
                        f"⚠️ {status}\n\n"
                        f"🔁 Link sudah dihapus dari cache, kirim ulang untuk retry.",
                        message_id=message_id
                    )
                download_stats['queue_size'] = download_queue.qsize()

            save_state()
        finally:
            download_queue.task_done()

def worker_wrapper(wid):
    while not shutdown_event.is_set():
        try:
            worker()
        except Exception as e:
            if not shutdown_event.is_set():
                log_activity(f"  ⚠️ Worker-{wid} crash: {e} — restart 5s...")
                time.sleep(5)
    log_activity(f"✅ Worker-{wid} stopped gracefully")

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
def get_main_inline_keyboard():
    """Inline menu utama."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Statistik",    callback_data="stats"),
         InlineKeyboardButton("📋 Antrian",      callback_data="queue")],
        [InlineKeyboardButton("📁 Ganti Folder", callback_data="change_folder"),
         InlineKeyboardButton("🗑️ Kelola Cache", callback_data="cache_menu")],
        [InlineKeyboardButton("❓ Bantuan",       callback_data="help"),
         InlineKeyboardButton("🔄 Refresh",      callback_data="refresh_menu")]
    ])

def get_cache_menu_keyboard():
    """Sub-menu kelola cache."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ Hapus Semua Cache",    callback_data="clear_all_cache")],
        [InlineKeyboardButton("✂️ Hapus Link Tertentu",  callback_data="remove_link")],
        [InlineKeyboardButton("🔙 Kembali",              callback_data="menu")]
    ])

def get_stats_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh",     callback_data="stats"),
         InlineKeyboardButton("🗑️ Reset Stats", callback_data="clear_stats")],
        [InlineKeyboardButton("🔙 Menu Utama",  callback_data="menu")]
    ])

def get_queue_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh",    callback_data="queue")],
        [InlineKeyboardButton("🔙 Menu Utama", callback_data="menu")]
    ])

def get_back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Menu Utama", callback_data="menu")]
    ])

def build_folder_choice_keyboard(chat_id) -> InlineKeyboardMarkup:
    rows  = []
    shown = set()

    if DOWNLOAD_DIR:
        icon = "🏠" if DOWNLOAD_DIR == DOWNLOAD_DIR_DEFAULT else "📂"
        rows.append([InlineKeyboardButton(
            f"{icon} Lanjutkan  ·  {DOWNLOAD_DIR}",
            callback_data=f"dlf_use|{DOWNLOAD_DIR}"
        )])
        shown.add(DOWNLOAD_DIR)

    for h in get_folder_history(chat_id):
        if h not in shown and h != DOWNLOAD_DIR_DEFAULT:
            rows.append([InlineKeyboardButton(
                f"🕒 {h}", callback_data=f"dlf_use|{h}"
            )])
            shown.add(h)

    if DOWNLOAD_DIR_DEFAULT not in shown:
        rows.append([InlineKeyboardButton(
            f"🏠 Default  ·  {DOWNLOAD_DIR_DEFAULT}",
            callback_data=f"dlf_use|{DOWNLOAD_DIR_DEFAULT}"
        )])

    rows.append([InlineKeyboardButton("✏️ Folder baru…", callback_data="dlf_new")])
    return InlineKeyboardMarkup(rows)

def build_global_folder_keyboard() -> InlineKeyboardMarkup:
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

    rows.append([InlineKeyboardButton("✏️ Ketik nama folder baru", callback_data="gf_new")])
    rows.append([InlineKeyboardButton("🔙 Kembali", callback_data="menu")])
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
            with stats_lock, failed_urls_lock:
                # Hanya +total jika bukan retry dari link yang pernah gagal
                if link not in failed_urls:
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
    if dupes: result += f"⏭️ Duplikat dilewati: {dupes}\n"
    if full:  result += "⚠️ Beberapa link gagal (antrian penuh)\n"
    result += f"📊 Posisi antrian: {download_queue.qsize()}"
    return result

# ─────────────────────────────────────────────
# 📄  Page builders
# ─────────────────────────────────────────────
def build_home_text() -> str:
    folder_info = f"`{DOWNLOAD_DIR}`" if DOWNLOAD_DIR else "_belum diset_"
    with stats_lock:
        qs = download_stats['queue_size']
    return (
        "🎬 *Video Downloader Bot*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📁 *Folder aktif:* {folder_info}\n"
        f"📋 *Antrian:* {qs} item\n\n"
        "Kirim link video kapan saja,\natau pilih menu di bawah 👇"
    )

def build_stats_text() -> str:
    with stats_lock:
        uptime = datetime.now() - download_stats['start_time']
        total  = download_stats['success'] + download_stats['failed']
        rate   = (download_stats['success'] / total * 100) if total else 0
        return (
            "📊 *STATISTIK DOWNLOAD*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"✅ *Berhasil:*    {download_stats['success']}\n"
            f"❌ *Gagal:*       {download_stats['failed']}\n"
            f"📈 *Total:*       {total}\n"
            f"🎯 *Sukses Rate:* {rate:.1f}%\n\n"
            f"📋 *Antrian:*     {download_stats['queue_size']}\n"
            f"👷 *Workers:*     {MAX_WORKERS}\n"
            f"📁 *Folder:*      `{DOWNLOAD_DIR or 'belum diset'}`\n"
            f"🔗 *Cache Links:* {len(processed_links)}\n\n"
            f"⏱️ *Uptime:*      {format_duration(uptime.total_seconds())}\n"
            f"🕐 *Start:*       {download_stats['start_time'].strftime('%d/%m %H:%M')}"
        )

def build_queue_text() -> str:
    qs   = download_queue.qsize()
    acts = []
    with downloads_lock:
        for files in active_downloads.values():
            acts.extend(files[:5])
    text = (
        "📋 *STATUS ANTRIAN*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 *Total Antrian:* {qs}\n"
        f"👷 *Worker Aktif:*  {MAX_WORKERS}\n"
        f"📊 *Kapasitas:*     {MAX_QUEUE_SIZE}\n"
        f"📈 *Slot Tersisa:*  {MAX_QUEUE_SIZE - qs}\n\n"
    )
    if acts:
        text += "⚡ *Sedang Diproses:*\n"
        for i, f in enumerate(acts[:5], 1):
            s = f[:35] + "…" if len(f) > 35 else f
            text += f"`{i}.` `{s}`\n"
    else:
        text += "✨ _Tidak ada download aktif_"
    return text

def build_help_text() -> str:
    return (
        "❓ *PANDUAN PENGGUNAAN*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "*📥 Download Video:*\n"
        "Kirim link video ke chat ini.\n"
        "Bot akan tanya folder tujuan, lalu download otomatis.\n\n"
        "*📁 Folder:*\n"
        "• Pilih folder aktif yang sudah ada\n"
        "• Pilih dari riwayat folder terakhir\n"
        "• Atau ketik nama folder baru\n\n"
        "*🗑️ Kelola Cache:*\n"
        "• *Hapus Semua* — semua link bisa download ulang\n"
        "• *Hapus Link Tertentu* — kirim link yang ingin dihapus\n"
        "• Link yang *gagal* otomatis dihapus dari cache\n\n"
        "*⚙️ Spesifikasi:*\n"
        f"• Folder aktif : `{DOWNLOAD_DIR or 'belum diset'}`\n"
        f"• Max antrian  : {MAX_QUEUE_SIZE}\n"
        f"• Workers      : {MAX_WORKERS}\n"
        f"• Retry        : {MAX_RETRIES}×\n"
        f"• Auto-cleanup : >{CLEANUP_DAYS} hari"
    )

def build_cache_menu_text() -> str:
    with processed_lock:
        count = len(processed_links)
    return (
        "🗑️ *KELOLA CACHE LINK*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 *Link di cache:* {count}\n\n"
        "Cache mencegah link yang sama didownload dua kali.\n"
        "Hapus jika ingin download ulang link lama.\n\n"
        "Pilih aksi:"
    )

# ─────────────────────────────────────────────
# 🤖  Handlers
# ─────────────────────────────────────────────
@require_auth
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point — satu-satunya command."""
    chat_id = update.effective_chat.id
    with active_chats_lock:
        active_chats[chat_id] = context.application

    # Hapus reply keyboard lama yang tersisa di Telegram client
    rm_msg = await update.message.reply_text("...", reply_markup=ReplyKeyboardRemove())
    await rm_msg.delete()

    await update.message.reply_text(
        build_home_text(),
        parse_mode='Markdown',
        reply_markup=get_main_inline_keyboard()
    )

# ─────────────────────────────────────────────
# 🔘  Callback — semua inline button
# ─────────────────────────────────────────────
@require_auth
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    data    = query.data
    chat_id = update.effective_chat.id

    # ── Menu utama ──
    if data in ("menu", "refresh_menu"):
        await query.answer("Diperbarui!" if data == "refresh_menu" else "")
        try:
            await query.edit_message_text(
                build_home_text(), parse_mode='Markdown',
                reply_markup=get_main_inline_keyboard()
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise

    # ── Statistik ──
    elif data == "stats":
        try:
            await query.answer("Diperbarui!")
            await query.edit_message_text(
                build_stats_text(), parse_mode='Markdown',
                reply_markup=get_stats_keyboard()
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise

    elif data == "clear_stats":
        with stats_lock:
            download_stats.update({'success': 0, 'failed': 0, 'total': 0, 'start_time': datetime.now()})
        save_state()
        await query.answer("Statistik direset!")
        try:
            await query.edit_message_text(
                build_stats_text(), parse_mode='Markdown',
                reply_markup=get_stats_keyboard()
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise

    # ── Antrian ──
    elif data == "queue":
        try:
            await query.answer("Diperbarui!")
            await query.edit_message_text(
                build_queue_text(), parse_mode='Markdown',
                reply_markup=get_queue_keyboard()
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise

    # ── Bantuan ──
    elif data == "help":
        try:
            await query.answer()
            await query.edit_message_text(
                build_help_text(), parse_mode='Markdown',
                reply_markup=get_back_keyboard()
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise

    # ── Cache menu ──
    elif data == "cache_menu":
        await query.answer()
        try:
            await query.edit_message_text(
                build_cache_menu_text(), parse_mode='Markdown',
                reply_markup=get_cache_menu_keyboard()
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise

    elif data == "clear_all_cache":
        with processed_lock:
            count = len(processed_links)
            processed_links.clear()
        save_state()
        await query.answer(f"✅ {count} link dihapus!")
        await query.edit_message_text(
            f"🗑️ *Cache Dibersihkan*\n\n"
            f"✅ {count} link dihapus dari cache.\n\n"
            "Semua link bisa didownload ulang sekarang!",
            parse_mode='Markdown',
            reply_markup=get_back_keyboard()
        )

    elif data == "remove_link":
        with remove_mode_lock:
            remove_mode[chat_id] = True
        await query.answer()
        await query.edit_message_text(
            "✂️ *Hapus Link Tertentu dari Cache*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Kirim link yang ingin dihapus dari cache.\n"
            "Bisa kirim beberapa link sekaligus (satu per baris).\n\n"
            "_Tekan Selesai untuk keluar dari mode ini._",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Selesai", callback_data="remove_cancel")]
            ])
        )

    elif data == "remove_cancel":
        with remove_mode_lock:
            remove_mode.pop(chat_id, None)
        await query.answer("Selesai.")
        await query.edit_message_text(
            build_cache_menu_text(), parse_mode='Markdown',
            reply_markup=get_cache_menu_keyboard()
        )

    # ── Folder untuk link pending ──
    elif data.startswith("dlf_use|"):
        chosen = data.split("|", 1)[1]
        with pending_links_lock:
            pending = pending_links.pop(chat_id, None)
        if not pending:
            await query.answer("⚠️ Sesi habis, kirim link lagi.")
            await query.edit_message_text(
                "⚠️ Sesi habis. Silakan kirim link lagi.",
                reply_markup=get_back_keyboard()
            )
            return
        links = pending.get("links", [])
        apply_download_dir(chosen, chat_id)
        added, dupes, full = queue_links_to_download(links, chat_id, chosen)
        await query.answer(f"✅ {added} link masuk antrian")
        await query.edit_message_text(
            format_queue_result(added, dupes, full, chosen),
            parse_mode='Markdown'
        )

    elif data == "dlf_new":
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

    # ── Folder global ──
    elif data == "change_folder":
        folder_info = f"`{DOWNLOAD_DIR}`" if DOWNLOAD_DIR else "_belum diset_"
        await query.answer()
        await query.edit_message_text(
            f"📁 *Ganti Folder Aktif*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📌 Saat ini: {folder_info}\n\n"
            "Pilih folder atau buat baru:",
            parse_mode='Markdown',
            reply_markup=build_global_folder_keyboard()
        )

    elif data.startswith("gf_use|"):
        chosen = data.split("|", 1)[1]
        apply_download_dir(chosen, chat_id)
        await query.answer(f"✅ Folder: {chosen}")
        await query.edit_message_text(
            f"✅ *Folder Aktif Diubah*\n\n"
            f"📁 `{chosen}`\n\n"
            "Folder ini menjadi pilihan pertama saat kirim link.",
            parse_mode='Markdown',
            reply_markup=get_back_keyboard()
        )

    elif data == "gf_new":
        with pending_links_lock:
            pending_links[chat_id] = {"links": [], "waiting_custom": True, "global_folder": True}
        await query.answer()
        await query.edit_message_text(
            "✏️ *Ketik nama folder baru:*\n\n"
            "Contoh: `VideoKoleksi`, `Film_2024`\n\n"
            "⚠️ Hindari: `/ \\ : * ? \" < > |`",
            parse_mode='Markdown'
        )

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

    # ── Mode hapus link tertentu ──
    in_remove_mode = False
    with remove_mode_lock:
        in_remove_mode = remove_mode.get(chat_id, False)

    if in_remove_mode:
        links = re.findall(
            r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+',
            text
        )
        if not links:
            await update.message.reply_text(
                "❌ Tidak ada link yang ditemukan.\n"
                "Kirim link yang ingin dihapus, atau tekan *Selesai*.",
                parse_mode='Markdown'
            )
            return

        removed = []
        not_found = []
        with processed_lock:
            for link in links:
                if link in processed_links:
                    processed_links.discard(link)
                    removed.append(link)
                else:
                    not_found.append(link)
        save_state()

        lines = ["✂️ *Hasil Hapus Cache*\n━━━━━━━━━━━━━━━━━━━━\n"]
        if removed:
            lines.append(f"✅ *{len(removed)} link dihapus:*")
            for l in removed:
                lines.append(f"• `{l[:60]}{'…' if len(l) > 60 else ''}`")
        if not_found:
            lines.append(f"\n⚠️ *{len(not_found)} tidak ada di cache:*")
            for l in not_found:
                lines.append(f"• `{l[:60]}{'…' if len(l) > 60 else ''}`")
        lines.append("\n_Kirim link lain, atau tekan Selesai._")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Selesai", callback_data="remove_cancel")]
            ])
        )
        return

    # ── Mode input nama folder custom ──
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
            apply_download_dir(folder_name, chat_id)
            with pending_links_lock:
                pending_links.pop(chat_id, None)
            await update.message.reply_text(
                f"✅ *Folder Aktif Diubah*\n\n"
                f"📁 `{DOWNLOAD_DIR}`\n\n"
                "Folder ini menjadi pilihan pertama saat kirim link.",
                parse_mode='Markdown',
                reply_markup=get_back_keyboard()
            )
        else:
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

    # ── Deteksi link video ──
    links = re.findall(
        r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+',
        text
    )

    if not links:
        await update.message.reply_text(
            "❌ *Tidak ada link yang valid.*\n\n"
            "Kirim link video, atau pilih menu di bawah.",
            parse_mode='Markdown'
        )
        return

    # Simpan link & tanya folder tujuan
    with pending_links_lock:
        pending_links[chat_id] = {"links": links, "waiting_custom": False, "global_folder": False}

    count   = len(links)
    preview = f"`{links[0][:60]}{'…' if len(links[0]) > 60 else ''}`"
    suffix  = f" _( +{count - 1} lainnya)_" if count > 1 else ""

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

        # Hanya /start — semua fitur lain via menu tombol
        app.add_handler(CommandHandler("start", start_handler))
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
        shutdown_event.set()
        print("🛑 Menunggu workers shutdown...")
        time.sleep(2)
        save_state()
        progress_bar.stop()
        log_activity("[SHUTDOWN] Bot stopped")

if __name__ == "__main__":
    print("🎬 Video Downloader Bot Starting...")
    print("=" * 50)
    run_bot()
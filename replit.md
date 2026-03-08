# Telegram Video Downloader Bot - Dokumentasi

## 📋 Deskripsi
Bot Telegram yang download video dari berbagai platform (XHamster, Pornhub, XVideos, XNXX, RedTube, Beeg, Eporner, TnaFlix, SpankBang) menggunakan `yt-dlp`.

## 🏗️ Arsitektur

### Core Components:
- **Multi-worker threading**: 16 workers concurrent untuk download parallel
- **Queue system**: Max 100 item antrian dengan graceful overflow handling
- **Persistent state**: `bot_state.json` menyimpan link cache, folder history, dan statistik
- **Dynamic referer detection**: Otomatis set Referer header sesuai platform
- **Folder management**: User bisa pilih/buat folder, history 3 terakhir tersimpan
- **Progress tracking**: Real-time updates via Telegram + Rich progress bars

## 📁 Folder Structure

```
project/
├── main.py
├── replit.md
├── .gitignore (ignores Downloads/ & bot_state.json)
├── Downloads/           ← Semua download ada sini
│   ├── .gitkeep        ← Folder tetap tercatat di git
│   ├── AsianGirl/      ← Default folder
│   ├── MyFolder/       ← Custom folders user
│   └── Videos2024/
├── bot_state.json      ← Persistent state (ignored)
└── bot_activity.log    ← Activity log (ignored)
```

⚠️ **PENTING:** `Downloads/` tidak akan ter-commit ke GitHub — hanya `.gitkeep` supaya folder tersimpan.

### Fitur:

#### 1. Download Video
- Kirim URL video → bot tanya folder → otomatis download
- Support multiple URL sekaligus
- Duplicate detection (cache)
- Retry mechanism (max 3x dengan exponential backoff)

#### 2. Folder Management
- Pilih folder aktif, buat custom folder
- History riwayat 3 folder terakhir per user
- Folder otomatis created jika belum ada

#### 3. Cache Management
- Link yang sudah download tidak didownload lagi
- User bisa hapus link tertentu atau clear semua cache
- Failed URLs otomatis dihapus untuk bisa di-retry

#### 4. Monitoring
- Statistik: success/failed count, success rate, uptime
- Queue monitoring: size, slot tersisa
- Active downloads tracking
- Activity logging ke `bot_activity.log`

## 🔧 Konfigurasi

## 🔄 Migration dari Old Folder Structure

Jika ada folder lama (`AsianGirl/`, `Gattouz/`, dll):
- Folder lama tetap ada dan masih berfungsi
- Setelah next restart, bot akan switch ke `Downloads/AsianGirl` (default baru)
- Bisa manual pindah dengan `/start` → pilih folder atau ganti ke `Downloads/FolderName`

**Best practice:**
- Kirim `/start` untuk reset ke default baru
- Atau pilih "Folder baru" dan ketik `Downloads/NamaFolder`
- Folder akan auto-created di dalam `Downloads/`

### Environment Variables:
```
BOT_TOKEN=<your_bot_token>           # Required: Telegram Bot API token
ALLOWED_USER_IDS=<uid1>,<uid2>,... # Comma-separated user IDs (optional, blank = allow all)
MAX_WORKERS=16                       # Worker threads (default: 5)
MAX_QUEUE_SIZE=100                   # Queue capacity (default: 100)
MAX_RETRIES=3                        # Retry attempts (default: 3)
TIMEOUT=30                           # Download timeout seconds (default: 30)
CLEANUP_DAYS=7                       # Auto-delete files older than N days
PROGRESS_THROTTLE=3                  # Progress update interval (seconds)
```

### Files:
- `main.py` - Entry point & bot logic
- `bot_state.json` - Persistent state (auto-created)
- `bot_activity.log` - Activity logging (auto-created)

## 📈 Performance Optimizations

### Recent Improvements (v2):
1. **Graceful Shutdown**: Workers stop cleanly on bot shutdown
2. **Better Error Handling**: Detailed logging untuk debugging
3. **Timeout Protection**: Workers timeout dari queue dengan 2s grace period
4. **Failed URL Tracking**: Prevent retry of permanently broken URLs
5. **Activity Logging**: All events logged ke file untuk monitoring
6. **Exponential Backoff**: Retry delay capped at 30s max

### Key Performance Features:
- Non-blocking queue gets dengan timeout
- Thread-safe operations via locks
- Efficient state persistence (only when needed)
- IPv4-only socket connections (faster)
- Connection pooling via urllib3

## 🚀 Running the Bot

```bash
python3 main.py
```

Bot akan:
1. Load state dari `bot_state.json`
2. Start 16 worker threads
3. Connect ke Telegram API
4. Log activity ke `bot_activity.log`
5. Listen untuk user messages

## 🔐 Security

- User ID whitelist (optional)
- Auth decorator pada semua handlers
- No sensitive data di logs
- Graceful error handling

## 📊 Monitoring

Check `bot_activity.log` untuk:
- Download attempts & results
- Worker crashes & restarts
- Timeout events
- Retry attempts
- Final failures
- Shutdown events

## 🛠️ Troubleshooting

### Bot not starting:
- Check `BOT_TOKEN` secret is set
- Check logs untuk error message

### Workers not downloading:
- Check folder permissions
- Check `DOWNLOAD_DIR` is writable
- Check internet connection

### Queue stuck:
- Bot logs retry attempts di `bot_activity.log`
- Worker auto-restarts every 5s jika crash
- Failed URLs di-remove dari cache untuk allow retry

## 📝 Implementation Notes

- Menggunakan `python-telegram-bot` 22.6
- Video extraction via `yt-dlp` 2026.3+
- Progress display via `rich` library
- Threading-based worker pool (bukan asyncio untuk blocking yt-dlp calls)

# 🚀 Setup Guide - Telegram Video Downloader Bot

## ✅ Sudah Siap Pakai!

Bot sudah di-setup di Replit environment dengan best practices untuk GitHub.

## 📁 Folder Structure (Safe from Git)

```
├── Downloads/              ← Semua video download di sini
│   ├── .gitkeep           ← File ini jaga folder tetap tercatat
│   ├── AsianGirl/         ← Default folder
│   └── [custom folders]/  ← User-created folders
├── bot_state.json         ← Persistent state (ignored by git)
├── bot_activity.log       ← Activity log (ignored by git)
├── .gitignore             ← Sudah configured
└── main.py                ← Bot code
```

## 🔒 Git Security

✅ `Downloads/` **TIDAK** akan ter-commit ke GitHub
✅ Hanya `.gitkeep` (empty file) yang tersimpan, jadi folder ada di git

### Apa yang TIDAK ter-commit:
- Video files (mp4, mkv, dll)
- Custom folders
- `bot_state.json` (state lama)
- `bot_activity.log` (logs)

### Apa yang TER-commit:
- `main.py`
- `replit.md`
- `Downloads/.gitkeep` (marker)
- Source code

## 🎯 Cara Pakai

1. **Kirim URL video ke bot:**
   ```
   https://example.com/video.mp4
   ```

2. **Bot tanya folder tujuan:**
   - Pilih default (`Downloads/AsianGirl`)
   - Pilih dari history
   - Buat folder baru (otomatis di `Downloads/`)

3. **Bot download otomatis:**
   - Progress real-time di Telegram
   - Retry sampai 3x jika gagal
   - File tersimpan di `Downloads/[folder]/`

## 📊 Monitoring

Check activity log:
```bash
tail -f bot_activity.log
```

Check stats di Telegram: `/start` → "📊 Statistik"

## 🔄 Migrasi dari Old Folder

Jika ada folder lama (`AsianGirl/`, `Gattouz/`, dll di root):
- Tetap ada dan berfungsi
- Bisa manually move ke `Downloads/` jika mau
- Default baru adalah `Downloads/AsianGirl`

## ✨ Status

- ✅ Bot: Running
- ✅ Workers: 16 concurrent
- ✅ Logging: Active
- ✅ GitHub Safe: Yes
- ✅ Error Handling: Robust
- ✅ Auto-Retry: 3x with backoff

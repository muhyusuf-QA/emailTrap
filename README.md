# Mailpit Service

Folder ini sekarang dipakai untuk menjalankan stack lokal ringan untuk flow auth/register FE:

- Mailpit untuk inbox OTP
- `asklumia-lite` untuk backend compatibility layer di `8001`
- `asklumia-lite-auth` untuk auth/refresh compatibility layer di `8003`

Tujuannya supaya repo FE bisa menjalankan flow register/login/forgot-password tanpa lagi bergantung ke repo `asklumia-service`.

## Yang disiapkan

- SMTP Mailpit di `127.0.0.1:1025`
- Web UI Mailpit di `http://127.0.0.1:8025`
- Hanya recipient dengan domain `@e2e.asklumia.co` yang diterima
- API compatibility service di `http://127.0.0.1:8001`
- Auth compatibility service di `http://127.0.0.1:8003`
- Data, binary, PID, dan log disimpan lokal di folder `.mailpit/`
- State backend lokal disimpan di `.asklumia-lite/`

## Cara pakai

### Start Mailpit saja

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Start-Mailpit.ps1
```

### Start backend auth stack lokal

Perintah ini otomatis memastikan Mailpit hidup terlebih dahulu:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Start-AsklumiaLite.ps1
```

Perintah lain:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Get-MailpitStatus.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\Stop-Mailpit.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\Install-Mailpit.ps1 -Force
powershell -ExecutionPolicy Bypass -File .\scripts\Get-AsklumiaLiteStatus.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\Stop-AsklumiaLite.ps1
```

Saat `Start-Mailpit.ps1` dijalankan pertama kali, script akan:

1. Mengambil release Mailpit Windows terbaru dari GitHub.
2. Mengunduh `mailpit.exe` ke `.mailpit/bin/`.
3. Menyalakan Mailpit di background.

Saat `Start-AsklumiaLite.ps1` dijalankan, script akan:

1. Memastikan Mailpit aktif.
2. Menyalakan service `8001` dan `8003`.
3. Menyimpan state user/session lokal di `.asklumia-lite/data/state.json`.

## Konfigurasi FE

Atur repo FE atau backend lokal agar mengirim email ke:

```text
SMTP host: 127.0.0.1
SMTP port: 1025
```

Recipient yang tidak berakhiran `@e2e.asklumia.co` akan ditolak oleh Mailpit pada level SMTP.

Env FE yang relevan:

```env
NEXT_PUBLIC_BASE_API_URL=http://127.0.0.1:8001
NEXT_PUBLIC_AUTH_API_URL=http://127.0.0.1:8003
MAILPIT_API_URL=http://127.0.0.1:8025/api/v1
REGISTER_API_URL=http://127.0.0.1:8001
REGISTER_EMAIL_DOMAIN=e2e.asklumia.co
```

Inbox hasil kirim bisa dilihat di:

```text
http://127.0.0.1:8025
```

## Scope `asklumia-lite`

Compatibility layer ini sengaja hanya menutup flow yang dipakai FE auth E2E:

- `POST /auth/guest`
- `POST /auth/email/available`
- `POST /auth/email/register`
- `POST /auth/email/register/verify`
- `POST /auth/email/register/resend`
- `POST /auth/email/login`
- `POST /auth/email/forgot`
- `POST /auth/email/forgot/verify`
- `POST /auth/email/forgot/update`
- `GET /auth/profile`
- `PATCH /auth/profile`
- `POST /auth/refresh` di port `8003`
- `GET /health` di port `8001` dan `8003`

Flow chat, research, billing, dan endpoint backend lain belum direplikasi di repo ini.

## Ubah konfigurasi port atau host

Edit [mailpit.settings.json](./mailpit.settings.json).

Default sekarang bind ke `127.0.0.1` agar aman untuk lokal. Kalau FE kamu jalan di Docker/WSL dan tidak bisa reach host ini, ubah:

```json
{
  "listen": "0.0.0.0:8025",
  "smtp": "0.0.0.0:1025"
}
```

## Lokasi file runtime

- Binary: `.mailpit/bin/mailpit.exe`
- Database: `.mailpit/data/mailpit.db`
- Log stdout: `.mailpit/logs/mailpit.stdout.log`
- Log stderr: `.mailpit/logs/mailpit.stderr.log`
- Backend local state: `.asklumia-lite/data/state.json`
- Backend log stdout: `.asklumia-lite/logs/asklumia-lite.stdout.log`
- Backend log stderr: `.asklumia-lite/logs/asklumia-lite.stderr.log`

# Panduan Deploy YouTube Clipper ke VPS

## 1. Beli VPS di Hetzner (Paling Murah)

1. Buka https://hetzner.com/cloud
2. Daftar akun
3. Buat project baru → **Add Server**:
   - Location: **Helsinki** atau **Singapore**
   - Image: **Ubuntu 22.04**
   - Type: **CX22** (2 CPU, 4GB RAM) — €3.79/bulan ≈ Rp 65.000
   - Tambah SSH Key (opsional) atau pakai password
4. Klik **Create & Buy**
5. Catat IP address VPS kamu

---

## 2. Beli Domain (Opsional tapi Disarankan)

- **Niagahoster** / **IDwebhost** / **Namecheap** — mulai Rp 50.000/tahun
- Setelah beli, arahkan DNS A record ke IP VPS:
  ```
  A  @          → IP_VPS_KAMU
  A  www        → IP_VPS_KAMU
  ```

---

## 3. Upload File ke VPS

Dari terminal Mac kamu:
```bash
# Zip folder CLIPPER
cd ~/Desktop/CLAUDE
zip -r clipper.zip CLIPPER/ --exclude "*.pyc" --exclude "__pycache__/*" --exclude "downloads/*" --exclude "clips/*"

# Upload ke VPS (ganti IP_VPS dengan IP asli)
scp clipper.zip root@IP_VPS:/root/

# Masuk ke VPS
ssh root@IP_VPS

# Unzip
unzip clipper.zip
cd CLIPPER
```

---

## 4. Jalankan Script Deploy

```bash
# Di dalam VPS:
chmod +x deploy.sh
nano deploy.sh   # <-- Ganti DOMAIN="yourdomain.com" dengan domain kamu

bash deploy.sh
```

Script otomatis akan:
- Install Python, ffmpeg, nginx
- Setup aplikasi sebagai service (auto-start)
- Konfigurasi nginx sebagai reverse proxy

---

## 5. Isi API Key

```bash
nano /var/www/clipper/.env
```

Isi dengan key asli dari Midtrans & Stripe, lalu:
```bash
systemctl restart clipper
```

---

## 6. Pasang SSL (HTTPS) — Gratis

```bash
apt install certbot python3-certbot-nginx -y
certbot --nginx -d yourdomain.com -d www.yourdomain.com
```

Ikuti instruksi, pilih **redirect HTTP ke HTTPS**.

---

## 7. Update Webhook di Midtrans & Stripe

Setelah domain aktif dengan HTTPS:

**Midtrans:**
- Dashboard → Settings → Configuration
- Payment Notification URL: `https://yourdomain.com/api/pay/midtrans/webhook`

**Stripe:**
- Dashboard → Developers → Webhooks → Add endpoint
- URL: `https://yourdomain.com/api/pay/stripe/webhook`
- Events: `checkout.session.completed`

---

## Perintah Berguna di VPS

```bash
# Cek status aplikasi
systemctl status clipper

# Restart aplikasi
systemctl restart clipper

# Lihat log real-time
journalctl -u clipper -f

# Update aplikasi (setelah ada perubahan)
cp -r ~/CLIPPER/* /var/www/clipper/
systemctl restart clipper
```

---

## Estimasi Biaya Bulanan

| Item | Biaya |
|------|-------|
| VPS Hetzner CX22 | Rp 65.000 |
| Domain .com | Rp 4.000 (Rp 50.000/tahun) |
| SSL | Gratis (Let's Encrypt) |
| **Total** | **~Rp 69.000/bulan** |

Balik modal jika ada **1 pelanggan Pro** (Rp 99.000/bulan) 🎉

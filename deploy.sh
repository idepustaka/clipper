#!/bin/bash
# ============================================================
# Script Deploy YouTube Clipper ke VPS (Ubuntu 22.04)
# Jalankan: bash deploy.sh
# ============================================================

set -e

APP_DIR="/var/www/clipper"
DOMAIN="yourdomain.com"   # <-- GANTI dengan domain kamu
APP_USER="clipper"

echo "===== [1/7] Update sistem ====="
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv nginx ffmpeg git ufw

echo "===== [2/7] Buat user & folder ====="
id -u $APP_USER &>/dev/null || useradd -m -s /bin/bash $APP_USER
mkdir -p $APP_DIR
chown $APP_USER:$APP_USER $APP_DIR

echo "===== [3/7] Copy file aplikasi ====="
# Asumsi script ini dijalankan dari folder CLIPPER
cp -r . $APP_DIR/
chown -R $APP_USER:$APP_USER $APP_DIR

echo "===== [4/7] Install Python dependencies ====="
cd $APP_DIR
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

echo "===== [5/7] Setup systemd service ====="
cat > /etc/systemd/system/clipper.service << EOF
[Unit]
Description=YouTube Clipper
After=network.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/gunicorn -c gunicorn.conf.py app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable clipper
systemctl restart clipper

echo "===== [6/7] Setup Nginx ====="
cat > /etc/nginx/sites-available/clipper << EOF
server {
    listen 80;
    server_name $DOMAIN www.$DOMAIN;

    client_max_body_size 500M;

    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_read_timeout 300s;
    }

    location /api/download/ {
        proxy_pass http://127.0.0.1:5001;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
}
EOF

ln -sf /etc/nginx/sites-available/clipper /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

echo "===== [7/7] Setup firewall ====="
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable

echo ""
echo "✅ Deploy selesai!"
echo "   Akses: http://$DOMAIN"
echo ""
echo "⚠️  Langkah selanjutnya:"
echo "   1. Edit $APP_DIR/.env — isi Midtrans & Stripe key"
echo "   2. Install SSL: apt install certbot python3-certbot-nginx && certbot --nginx -d $DOMAIN"
echo "   3. Update APP_URL di .env ke https://$DOMAIN"
echo "   4. Restart: systemctl restart clipper"

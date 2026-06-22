import os
import re
import struct
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import LoginManager, current_user, login_required

import imageio_ffmpeg
import yt_dlp

from config import Config
from models import TIERS, Subscription, User, db
from auth import auth
from payments import pay

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "auth.login"

@login_manager.user_loader
def load_user(uid):
    return User.query.get(int(uid))

app.register_blueprint(auth)
app.register_blueprint(pay)

with app.app_context():
    db.create_all()

BASE_DIR = Path(__file__).parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
CLIPS_DIR     = BASE_DIR / "clips"
DOWNLOAD_DIR.mkdir(exist_ok=True)
CLIPS_DIR.mkdir(exist_ok=True)

import shutil
FFMPEG_PATH = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()

YDL_BASE = {
    "quiet": True,
    "no_warnings": True,
    "extractor_args": {"youtube": {"player_client": ["android"]}},
    "socket_timeout": 60,
    "retries": 10,
    "fragment_retries": 10,
    "retry_sleep_functions": {"http": lambda n: 3 * n},
}

jobs = {}


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if not current_user.is_authenticated:
        return render_template("landing.html", user=current_user)
    return render_template("index.html", user=current_user, tiers=TIERS)

@app.route("/pricing")
def pricing():
    return render_template("pricing.html", tiers=TIERS, user=current_user)

@app.route("/dashboard")
@login_required
def dashboard():
    subs = Subscription.query.filter_by(user_id=current_user.id).order_by(Subscription.created_at.desc()).limit(10).all()
    return render_template("dashboard.html", user=current_user, tiers=TIERS, subs=subs)

@app.route("/admin")
@login_required
def admin():
    if current_user.email != "idepustaka@gmail.com":
        return redirect(url_for("index"))
    users = User.query.order_by(User.created_at.desc()).all()
    subs  = Subscription.query.order_by(Subscription.created_at.desc()).all()
    admin_id = User.query.filter_by(email="idepustaka@gmail.com").with_entities(User.id).scalar()
    stats = {
        "total_users":    User.query.filter(User.id != admin_id).count(),
        "free_users":     User.query.filter(User.id != admin_id, User.tier == "free").count(),
        "pro_users":      User.query.filter(User.id != admin_id, User.tier == "pro").count(),
        "business_users": User.query.filter(User.id != admin_id, User.tier == "business").count(),
        "total_subs":     Subscription.query.filter(Subscription.user_id != admin_id).count(),
        "active_subs":    Subscription.query.filter(Subscription.user_id != admin_id, Subscription.status == "active").count(),
        "mrr": (User.query.filter(User.id != admin_id, User.tier == "pro").count() * 99000 +
                User.query.filter(User.id != admin_id, User.tier == "business").count() * 299000),
    }

    now = datetime.now(timezone.utc)

    # Data per hari bulan berjalan
    today = now.replace(hour=23, minute=59, second=59, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days_in_month = (today.day)
    daily = []
    for d in range(1, days_in_month + 1):
        day_start = month_start.replace(day=d, hour=0, minute=0, second=0, microsecond=0)
        day_end   = day_start + timedelta(days=1)
        new_users = User.query.filter(User.created_at >= day_start, User.created_at < day_end).count()
        admin_id  = User.query.filter_by(email="idepustaka@gmail.com").with_entities(User.id).scalar()
        up_pro    = Subscription.query.filter(Subscription.tier == "pro",      Subscription.status == "active", Subscription.user_id != admin_id, Subscription.created_at >= day_start, Subscription.created_at < day_end).count()
        up_biz    = Subscription.query.filter(Subscription.tier == "business", Subscription.status == "active", Subscription.user_id != admin_id, Subscription.created_at >= day_start, Subscription.created_at < day_end).count()
        daily.append({"tanggal": day_start.strftime("%-d %b %Y"), "users": new_users, "pro": up_pro, "business": up_biz})

    total_users_month = sum(r["users"] for r in daily)
    total_pro_month   = sum(r["pro"]   for r in daily)
    total_biz_month   = sum(r["business"] for r in daily)
    total_omzet_month = total_pro_month * 99000 + total_biz_month * 299000

    # Pending subscription per user (untuk info nominal di tabel pengguna)
    pending_subs = {s.user_id: s for s in Subscription.query.filter_by(status="pending", gateway="manual").all()}

    return render_template("admin.html", users=users, subs=subs, stats=stats, daily=daily,
                           total_users_month=total_users_month, total_pro_month=total_pro_month,
                           total_biz_month=total_biz_month, total_omzet_month=total_omzet_month,
                           pending_subs=pending_subs)


@app.route("/admin/stats")
@login_required
def admin_stats():
    if current_user.email != "idepustaka@gmail.com":
        return jsonify({"error": "Unauthorized"}), 403
    from_str = request.args.get("from", "")
    to_str = request.args.get("to", "")
    try:
        from_dt = datetime.strptime(from_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        to_dt = datetime.strptime(to_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
    except ValueError:
        return jsonify({"error": "Format tanggal salah"}), 400
    users = User.query.filter(User.created_at >= from_dt, User.created_at <= to_dt).count()
    pro = Subscription.query.filter(Subscription.tier == "pro", Subscription.created_at >= from_dt, Subscription.created_at <= to_dt).count()
    business = Subscription.query.filter(Subscription.tier == "business", Subscription.created_at >= from_dt, Subscription.created_at <= to_dt).count()
    omzet = pro * 99000 + business * 299000
    return jsonify({"users": users, "pro": pro, "business": business, "omzet": omzet})


@app.route("/admin/activate", methods=["POST"])
@login_required
def admin_activate():
    if current_user.email != "idepustaka@gmail.com":
        return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    user_id = data.get("user_id")
    tier = data.get("tier")
    if tier not in ("free", "pro", "business"):
        return jsonify({"error": "Tier tidak valid"}), 400
    user = User.query.get(user_id)
    if not user:
        return jsonify({"error": "User tidak ditemukan"}), 404
    user.tier = tier
    user.clips_used = 0
    user.quota_exhausted_at = None
    user.quota_reminder_count = 0
    user.expired_at = None
    user.expired_reminder_count = 0
    # Cancel semua pending subscription user ini
    for ps in Subscription.query.filter_by(user_id=user.id, status="pending").all():
        ps.status = "cancelled"
    if tier != "free":
        sub = Subscription(
            user_id=user.id, gateway="manual", order_id=f"MANUAL-{tier.upper()}-{uuid.uuid4().hex[:8].upper()}",
            tier=tier, amount=99000 if tier == "pro" else 299000, currency="IDR", status="active",
            valid_until=datetime.now(timezone.utc) + timedelta(days=30),
        )
        db.session.add(sub)
    db.session.commit()

    # Kirim notif WA ke user saat paket diaktifkan
    fonnte_token = app.config.get("FONNTE_TOKEN", "")
    if user.phone and fonnte_token and tier != "free":
        from auth import send_wa
        if tier == "pro":
            msg = (
                f"Halo {user.name}! 🎉\n\n"
                f"Paket *Pro* kamu sudah aktif!\n"
                f"Kamu bisa download *30 clip per bulan* mulai sekarang.\n\n"
                f"👉 https://youtubeclipper.asia"
            )
        else:
            msg = (
                f"Halo {user.name}! 🎉\n\n"
                f"Paket *Business* kamu sudah aktif!\n"
                f"Kamu bisa download clip *unlimited* mulai sekarang.\n\n"
                f"👉 https://youtubeclipper.asia"
            )
        threading.Thread(target=send_wa, args=(user.phone, msg, fonnte_token), daemon=True).start()

    return jsonify({"ok": True})


@app.route("/api/request-upgrade", methods=["POST"])
@login_required
def request_upgrade():
    data = request.json or {}
    tier = data.get("tier")
    if tier not in ("pro", "business"):
        return jsonify({"error": "Tier tidak valid"}), 400

    # Batalkan pending lama milik user ini
    old = Subscription.query.filter_by(user_id=current_user.id, status="pending", gateway="manual").all()
    for o in old:
        o.status = "cancelled"
    db.session.commit()

    # Generate kode unik 3 digit yang belum dipakai user lain yang pending
    import random
    used_codes = {s.unique_code for s in Subscription.query.filter_by(status="pending").all() if s.unique_code}
    attempts = 0
    while attempts < 100:
        code = random.randint(100, 999)
        if code not in used_codes:
            break
        attempts += 1

    base_price = 99000 if tier == "pro" else 299000
    amount = base_price + code

    sub = Subscription(
        user_id=current_user.id,
        gateway="manual",
        order_id=f"MANUAL-{tier.upper()}-{uuid.uuid4().hex[:8].upper()}",
        tier=tier,
        amount=amount,
        unique_code=code,
        currency="IDR",
        status="pending",
        unique_code_expired_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    db.session.add(sub)
    db.session.commit()

    # Kirim WA ke user berisi info transfer
    fonnte_token = app.config.get("FONNTE_TOKEN", "")
    if current_user.phone and fonnte_token:
        from auth import send_wa
        tier_name = "Pro" if tier == "pro" else "Business"
        msg = (
            f"Halo {current_user.name}! 🎬\n\n"
            f"Permintaan upgrade *{tier_name}* kamu sudah diterima.\n\n"
            f"Silakan transfer ke:\n"
            f"🏦 BCA *0372470966*\n"
            f"a/n IDE PUSTAKA SETIAWAN\n\n"
            f"Nominal: *Rp {amount:,}*\n"
            f"(termasuk kode unik *{code}*)\n\n"
            f"⏰ Berlaku 24 jam. Setelah transfer, tunggu konfirmasi aktivasi dari kami.\n\n"
            f"Info: https://wa.me/6282137481104"
        )
        threading.Thread(target=send_wa, args=(current_user.phone, msg, fonnte_token), daemon=True).start()

    # Notif ke admin
    if fonnte_token:
        from auth import send_wa
        admin_msg = (
            f"💰 *Request Upgrade Baru!*\n\n"
            f"User: {current_user.name}\n"
            f"Email: {current_user.email}\n"
            f"Paket: {tier.capitalize()}\n"
            f"Nominal: Rp {amount:,}\n"
            f"Kode unik: *{code}*"
        )
        threading.Thread(target=send_wa, args=("82137481104", admin_msg, fonnte_token), daemon=True).start()

    return jsonify({"ok": True, "amount": amount, "code": code, "tier": tier})


@app.route("/api/checkout/mayar", methods=["POST"])
@login_required
def checkout_mayar():
    import requests as http_req
    data = request.json or {}
    tier = data.get("tier")
    if tier not in ("pro", "business"):
        return jsonify({"error": "Tier tidak valid"}), 400

    price = 99000 if tier == "pro" else 299000
    tier_name = "Pro" if tier == "pro" else "Business"
    mayar_key = app.config.get("MAYAR_API_KEY", "")
    if not mayar_key:
        return jsonify({"error": "Payment gateway belum dikonfigurasi"}), 500

    order_id = f"MAYAR-{tier.upper()}-{uuid.uuid4().hex[:8].upper()}"

    payload = {
        "amount": price,
        "description": f"YouTube Clipper {tier_name} — 1 bulan",
        "referenceId": order_id,
        "name": current_user.name,
        "email": current_user.email,
        "phone": current_user.phone or "",
        "redirectUrl": "https://youtubeclipper.asia/dashboard",
        "expiredAt": (datetime.now(timezone.utc) + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    resp = http_req.post(
        "https://api.mayar.id/hl/v1/payment/create",
        headers={"Authorization": f"Bearer {mayar_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )

    if resp.status_code != 200:
        app.logger.error(f"Mayar error {resp.status_code}: {resp.text}")
        return jsonify({"error": f"Mayar: {resp.status_code} - {resp.text[:200]}"}), 500

    result = resp.json()
    payment_url = result.get("data", {}).get("link") or result.get("data", {}).get("paymentUrl") or result.get("data", {}).get("url")
    if not payment_url:
        return jsonify({"error": "Link pembayaran tidak ditemukan"}), 500

    # Simpan sebagai pending subscription
    sub = Subscription(
        user_id=current_user.id,
        gateway="mayar",
        order_id=order_id,
        tier=tier,
        amount=price,
        currency="IDR",
        status="pending",
        valid_until=datetime.now(timezone.utc) + timedelta(days=30),
    )
    db.session.add(sub)
    db.session.commit()

    return jsonify({"ok": True, "url": payment_url})


@app.route("/webhook/mayar", methods=["POST"])
def webhook_mayar():
    data = request.json or {}
    status = data.get("status") or data.get("paymentStatus") or ""
    order_id = data.get("referenceId") or data.get("reference_id") or ""

    if status.lower() not in ("paid", "success", "completed"):
        return jsonify({"ok": True})

    sub = Subscription.query.filter_by(order_id=order_id).first()
    if not sub or sub.status == "active":
        return jsonify({"ok": True})

    sub.status = "active"
    sub.valid_until = datetime.now(timezone.utc) + timedelta(days=30)
    user = User.query.get(sub.user_id)
    if user:
        user.tier = sub.tier
        user.clips_used = 0
        user.quota_exhausted_at = None
        user.quota_reminder_count = 0
        user.expired_at = None
        user.expired_reminder_count = 0
        # Cancel pending lainnya
        for ps in Subscription.query.filter_by(user_id=user.id, status="pending").all():
            ps.status = "cancelled"
    db.session.commit()

    # WA notif ke user
    fonnte_token = app.config.get("FONNTE_TOKEN", "")
    if user and user.phone and fonnte_token:
        from auth import send_wa
        tier_name = "Pro" if sub.tier == "pro" else "Business"
        msg = (
            f"Halo {user.name}! 🎉\n\n"
            f"Pembayaran kamu berhasil!\n"
            f"Paket *{tier_name}* sudah aktif.\n\n"
            f"👉 https://youtubeclipper.asia"
        )
        threading.Thread(target=send_wa, args=(user.phone, msg, fonnte_token), daemon=True).start()

    # WA notif ke admin
    if fonnte_token:
        from auth import send_wa
        admin_msg = (
            f"💰 *Pembayaran Masuk (Mayar)!*\n\n"
            f"User: {user.name if user else '-'}\n"
            f"Email: {user.email if user else '-'}\n"
            f"Paket: {sub.tier.capitalize()}\n"
            f"Nominal: Rp {sub.amount:,}"
        )
        threading.Thread(target=send_wa, args=("82137481104", admin_msg, fonnte_token), daemon=True).start()

    return jsonify({"ok": True})


@app.route("/admin/delete-user", methods=["POST"])
@login_required
def admin_delete_user():
    if current_user.email != "idepustaka@gmail.com":
        return jsonify({"error": "Unauthorized"}), 403
    user_id = request.json.get("user_id")
    user = User.query.get(user_id)
    if not user:
        return jsonify({"error": "User tidak ditemukan"}), 404
    Subscription.query.filter_by(user_id=user.id).delete()
    db.session.delete(user)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/payment/success")
@login_required
def payment_success():
    order_id = request.args.get("order_id", "")
    sub = Subscription.query.filter_by(order_id=order_id, user_id=current_user.id).first()
    return render_template("payment_success.html", user=current_user, sub=sub)


# ── Search ────────────────────────────────────────────────────────────────────

@app.route("/api/search")
@login_required
def search_videos():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Query kosong"}), 400

    ydl_opts = {**YDL_BASE, "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch10:{query}", download=False)

    results = []
    for e in (info.get("entries") or []):
        if not e:
            continue
        results.append({
            "id": e.get("id", ""),
            "title": e.get("title", ""),
            "url": f"https://www.youtube.com/watch?v={e.get('id','')}",
            "thumbnail": e.get("thumbnail") or f"https://i.ytimg.com/vi/{e.get('id','')}/mqdefault.jpg",
            "duration": e.get("duration"),
            "view_count": e.get("view_count"),
            "channel": e.get("channel") or e.get("uploader", ""),
        })
    results.sort(key=lambda x: x.get("view_count") or 0, reverse=True)
    return jsonify(results)


# ── Video info ────────────────────────────────────────────────────────────────

@app.route("/api/info")
@login_required
def video_info():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL kosong"}), 400

    ydl_opts = {**YDL_BASE, "skip_download": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    chapters = info.get("chapters") or []
    duration = info.get("duration", 0)
    if not chapters:
        t, idx = 0, 1
        while t < duration:
            end = min(t + 60, duration)
            chapters.append({"title": f"Segmen {idx}", "start_time": t, "end_time": end})
            t, idx = end, idx + 1

    return jsonify({"title": info.get("title",""), "duration": duration,
                    "thumbnail": info.get("thumbnail",""), "chapters": chapters})


# ── Clip job ──────────────────────────────────────────────────────────────────

def run_clip_job(job_id, url, segments, user_id):
    try:
        jobs[job_id]["status"] = "downloading"
        jobs[job_id]["progress"] = 5

        raw_path = DOWNLOAD_DIR / f"{job_id}.%(ext)s"
        ydl_opts = {
            **YDL_BASE,
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "outtmpl": str(raw_path),
            "ffmpeg_location": str(Path(FFMPEG_PATH).parent),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        downloaded = list(DOWNLOAD_DIR.glob(f"{job_id}.*"))
        if not downloaded:
            raise FileNotFoundError("File download tidak ditemukan")
        source = downloaded[0]

        jobs[job_id]["status"] = "clipping"
        jobs[job_id]["progress"] = 60
        jobs[job_id]["clips"] = []

        total = len(segments)
        for i, seg in enumerate(segments):
            safe_title = re.sub(r"[^\w\-]", "_", seg.get("title", f"clip_{i+1}"))
            filename = f"{job_id}_{i+1:02d}_{safe_title}.mp4"
            output_path = CLIPS_DIR / filename

            cmd = [
                FFMPEG_PATH, "-y",
                "-ss", str(seg["start_time"]),
                "-i", str(source),
                "-to", str(seg["end_time"] - seg["start_time"]),
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
                str(output_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg error: {result.stderr[-500:]}")

            jobs[job_id]["clips"].append({
                "filename": filename,
                "title": seg.get("title", f"Clip {i+1}"),
                "start": seg["start_time"],
                "end": seg["end_time"],
                "size": output_path.stat().st_size,
            })
            jobs[job_id]["progress"] = 60 + int(38 * (i + 1) / total)

        # Kuota dipotong saat download, bukan saat clip dibuat

        source.unlink(missing_ok=True)
        jobs[job_id]["status"] = "done"
        jobs[job_id]["progress"] = 100

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        for f in DOWNLOAD_DIR.glob(f"{job_id}.*"):
            f.unlink(missing_ok=True)


@app.route("/api/clip", methods=["POST"])
@login_required
def start_clip():
    data     = request.json
    url      = (data.get("url") or "").strip()
    segments = data.get("segments") or []

    if not url:
        return jsonify({"error": "URL wajib diisi"}), 400
    if not segments:
        return jsonify({"error": "Tidak ada segmen yang dipilih"}), 400

    # Cek kuota
    if not current_user.can_clip(len(segments)):
        limit = current_user.clips_limit()
        return jsonify({"error": f"Kuota habis! Tier {current_user.tier} hanya {limit} clip/bulan. Upgrade untuk melanjutkan."}), 403

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "queued", "progress": 0, "clips": []}

    t = threading.Thread(target=run_clip_job, args=(job_id, url, segments, current_user.id), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


# ── Goal detection ────────────────────────────────────────────────────────────

def detect_goals_worker(job_id, url, before_sec, after_sec, user_id):
    try:
        jobs[job_id]["status"] = "downloading"
        jobs[job_id]["progress"] = 5

        raw_path = DOWNLOAD_DIR / f"{job_id}.%(ext)s"
        ydl_opts = {
            **YDL_BASE,
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "outtmpl": str(raw_path),
            "ffmpeg_location": str(Path(FFMPEG_PATH).parent),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        downloaded = list(DOWNLOAD_DIR.glob(f"{job_id}.*"))
        if not downloaded:
            raise FileNotFoundError("File download tidak ditemukan")
        source = downloaded[0]

        jobs[job_id]["status"] = "analyzing"
        jobs[job_id]["progress"] = 55

        audio_raw = DOWNLOAD_DIR / f"{job_id}_audio.raw"
        subprocess.run([FFMPEG_PATH, "-y", "-i", str(source),
                        "-ac", "1", "-ar", "100", "-f", "f32le", str(audio_raw)],
                       capture_output=True, check=True)

        data = audio_raw.read_bytes()
        n = len(data) // 4
        samples = struct.unpack(f"{n}f", data)
        audio_raw.unlink(missing_ok=True)

        rms_per_sec = []
        for i in range(0, n, 100):
            chunk = samples[i:i+100]
            if not chunk:
                break
            rms_per_sec.append((sum(x*x for x in chunk) / len(chunk)) ** 0.5)

        duration_sec = len(rms_per_sec)
        smoothed = []
        for i in range(len(rms_per_sec)):
            w = rms_per_sec[max(0,i-1):i+2]
            smoothed.append(sum(w)/len(w))

        mean_rms = sum(smoothed) / len(smoothed)
        std_rms  = (sum((x-mean_rms)**2 for x in smoothed) / len(smoothed)) ** 0.5
        threshold = mean_rms + 1.8 * std_rms

        spikes, last_spike = [], -30
        for i, val in enumerate(smoothed):
            if val > threshold and (i - last_spike) > 30:
                spikes.append(i)
                last_spike = i

        jobs[job_id]["progress"] = 75
        jobs[job_id]["status"] = "clipping"
        jobs[job_id]["clips"] = []

        if not spikes:
            source.unlink(missing_ok=True)
            jobs[job_id]["status"] = "done"
            jobs[job_id]["progress"] = 100
            jobs[job_id]["message"] = "Tidak ada momen gol terdeteksi."
            return

        # Cek kuota
        with app.app_context():
            user = User.query.get(user_id)
            if user and not user.can_clip(len(spikes)):
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = f"Kuota tidak cukup untuk {len(spikes)} clip. Upgrade akun."
                source.unlink(missing_ok=True)
                return

        for i, spike_sec in enumerate(spikes):
            clip_start = max(0, spike_sec - before_sec)
            clip_end   = min(duration_sec, spike_sec + after_sec)
            filename   = f"{job_id}_gol_{i+1:02d}.mp4"
            output_path = CLIPS_DIR / filename

            cmd = [
                FFMPEG_PATH, "-y",
                "-ss", str(clip_start),
                "-i", str(source),
                "-to", str(clip_end - clip_start),
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
                str(output_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                continue

            jobs[job_id]["clips"].append({
                "filename": filename,
                "title": f"Gol #{i+1} (~{spike_sec//60}:{spike_sec%60:02d})",
                "start": clip_start, "end": clip_end,
                "size": output_path.stat().st_size,
            })
            jobs[job_id]["progress"] = 75 + int(23 * (i+1) / len(spikes))

        source.unlink(missing_ok=True)
        jobs[job_id]["status"] = "done"
        jobs[job_id]["progress"] = 100

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        for f in DOWNLOAD_DIR.glob(f"{job_id}.*"):
            f.unlink(missing_ok=True)


@app.route("/api/detect-goals", methods=["POST"])
@login_required
def detect_goals():
    data       = request.json
    url        = (data.get("url") or "").strip()
    before_sec = int(data.get("before_sec", 30))
    after_sec  = int(data.get("after_sec", 15))

    if not url:
        return jsonify({"error": "URL wajib diisi"}), 400
    if not current_user.can_clip(1):
        return jsonify({"error": "Kuota habis! Upgrade akun untuk melanjutkan."}), 403

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "queued", "progress": 0, "clips": [], "mode": "goals"}

    t = threading.Thread(target=detect_goals_worker,
                         args=(job_id, url, before_sec, after_sec, current_user.id), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


# ── Status & Files ────────────────────────────────────────────────────────────

@app.route("/api/status/<job_id>")
@login_required
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan"}), 404
    return jsonify(job)

@app.route("/api/user/me")
@login_required
def user_me():
    return jsonify({
        "name": current_user.name,
        "email": current_user.email,
        "tier": current_user.tier,
        "tier_name": current_user.tier_info()["name"],
        "clips_used": current_user.clips_used,
        "clips_limit": current_user.clips_limit(),
        "remaining": current_user.remaining_clips(),
    })

def _check_subscription_expired(user):
    """Turunkan tier ke free jika langganan sudah expired."""
    if user.tier in ("pro", "business"):
        active_sub = Subscription.query.filter_by(
            user_id=user.id, status="active"
        ).order_by(Subscription.created_at.desc()).first()
        if active_sub and active_sub.valid_until:
            now = datetime.now(timezone.utc)
            valid = active_sub.valid_until
            if valid.tzinfo is None:
                valid = valid.replace(tzinfo=timezone.utc)
            if now > valid:
                active_sub.status = "expired"
                user.tier = "free"
                user.clips_used = 9999  # blokir akses free sampai perpanjang
                user.expired_at = datetime.now(timezone.utc)
                user.expired_reminder_count = 0
                db.session.commit()
                # Kirim notif WA expired
                fonnte_token = app.config.get("FONNTE_TOKEN", "")
                if user.phone and fonnte_token:
                    from auth import send_wa
                    msg = (
                        f"Halo {user.name}! 😔\n\n"
                        f"Paket *{active_sub.tier.capitalize()}* kamu sudah berakhir.\n\n"
                        f"Perpanjang sekarang agar bisa download clip lagi!\n"
                        f"👉 https://youtubeclipper.asia/pricing"
                    )
                    threading.Thread(target=send_wa, args=(user.phone, msg, fonnte_token), daemon=True).start()


@app.route("/api/download/<filename>")
@login_required
def download_clip(filename):
    path = CLIPS_DIR / filename
    if not path.exists():
        return jsonify({"error": "File tidak ditemukan"}), 404
    if current_user.email != "idepustaka@gmail.com":
        _check_subscription_expired(current_user)
        if not current_user.can_clip():
            return jsonify({"error": "Kuota clip habis atau paket sudah berakhir. Upgrade untuk melanjutkan."}), 403
        current_user.clips_used += 1
        # Set quota_exhausted_at saat kuota habis
        if not current_user.can_clip() and not current_user.quota_exhausted_at:
            current_user.quota_exhausted_at = datetime.now(timezone.utc)
            current_user.quota_reminder_count = 0
        db.session.commit()
        # Kirim WA jika sisa kuota tinggal 5
        remaining = current_user.remaining_clips()
        if remaining == 5 and current_user.phone:
            fonnte_token = app.config.get("FONNTE_TOKEN", "")
            if fonnte_token:
                from auth import send_wa
                msg = (
                    f"Halo {current_user.name}! ⚠️\n\n"
                    f"Kuota clip kamu tinggal *5 lagi* bulan ini.\n\n"
                    f"Upgrade ke Business untuk clip *unlimited*!\n"
                    f"👉 https://youtubeclipper.asia/pricing"
                )
                threading.Thread(target=send_wa, args=(current_user.phone, msg, fonnte_token), daemon=True).start()
    return send_file(path, as_attachment=True)

@app.route("/api/clips")
@login_required
def list_clips():
    files = [{"name": f.name, "size": f.stat().st_size}
             for f in sorted(CLIPS_DIR.glob("*.mp4"), key=lambda x: -x.stat().st_mtime)]
    return jsonify(files)

@app.route("/api/clips/<filename>", methods=["DELETE"])
@login_required
def delete_clip(filename):
    path = CLIPS_DIR / filename
    if path.exists():
        path.unlink()
    return jsonify({"ok": True})


REMINDER_DAYS = [1, 3, 7, 14, 30]
REMINDER_BATCH_SIZE = 50   # kirim max 50 WA per menit
REMINDER_BATCH_DELAY = 60  # detik jeda antar batch

def _send_reminder(user, msg, fonnte_token):
    from auth import send_wa
    send_wa(user.phone, msg, fonnte_token)

def _send_batch(queue, fonnte_token):
    """Kirim WA bertahap, 50 pesan per menit."""
    from auth import send_wa
    for i, (phone, msg) in enumerate(queue):
        if i > 0 and i % REMINDER_BATCH_SIZE == 0:
            time.sleep(REMINDER_BATCH_DELAY)
        try:
            send_wa(phone, msg, fonnte_token)
        except Exception:
            pass

def _reminder_job():
    """Kirim WA pengingat expired & kuota habis. Jalan setiap hari."""
    while True:
        try:
            with app.app_context():
                fonnte_token = app.config.get("FONNTE_TOKEN", "")
                if not fonnte_token:
                    time.sleep(86400)
                    continue

                now = datetime.now(timezone.utc)
                queue = []  # antrian (phone, msg)

                # Pengingat 3 hari sebelum expired
                subs = Subscription.query.filter_by(status="active").all()
                for sub in subs:
                    if not sub.valid_until:
                        continue
                    valid = sub.valid_until
                    if valid.tzinfo is None:
                        valid = valid.replace(tzinfo=timezone.utc)
                    if (valid - now).days == 3:
                        user = User.query.get(sub.user_id)
                        if user and user.phone:
                            msg = (
                                f"Halo {user.name}! ⏰\n\n"
                                f"Paket *{sub.tier.capitalize()}* kamu akan berakhir dalam *3 hari*.\n\n"
                                f"Perpanjang sekarang agar tidak terputus!\n"
                                f"👉 https://youtubeclipper.asia/pricing"
                            )
                            queue.append((user.phone, msg))

                # Reminder kuota habis (free & pro)
                users = User.query.filter(User.quota_exhausted_at.isnot(None)).all()
                for user in users:
                    if not user.phone:
                        continue
                    if user.quota_reminder_count >= len(REMINDER_DAYS):
                        continue
                    exhausted = user.quota_exhausted_at
                    if exhausted.tzinfo is None:
                        exhausted = exhausted.replace(tzinfo=timezone.utc)
                    days_since = (now - exhausted).days
                    target_day = REMINDER_DAYS[user.quota_reminder_count]
                    if days_since >= target_day:
                        if user.tier == "free":
                            msg = (
                                f"Halo {user.name}! 🎬\n\n"
                                f"Kuota *5 clip gratis* kamu sudah habis bulan ini.\n\n"
                                f"Upgrade ke *Pro* (30 clip/bln) atau *Business* (unlimited) untuk terus download!\n"
                                f"👉 https://youtubeclipper.asia/pricing"
                            )
                        else:
                            msg = (
                                f"Halo {user.name}! 🎬\n\n"
                                f"Kuota *30 clip Pro* kamu sudah habis bulan ini.\n\n"
                                f"Upgrade ke *Business* untuk clip *unlimited*!\n"
                                f"👉 https://youtubeclipper.asia/pricing"
                            )
                        queue.append((user.phone, msg))
                        user.quota_reminder_count += 1
                        db.session.commit()

                # Reminder expired (pro & business)
                expired_users = User.query.filter(User.expired_at.isnot(None)).all()
                for user in expired_users:
                    if not user.phone:
                        continue
                    if user.expired_reminder_count >= len(REMINDER_DAYS):
                        continue
                    expired = user.expired_at
                    if expired.tzinfo is None:
                        expired = expired.replace(tzinfo=timezone.utc)
                    days_since = (now - expired).days
                    target_day = REMINDER_DAYS[user.expired_reminder_count]
                    if days_since >= target_day:
                        last_sub = Subscription.query.filter_by(user_id=user.id).order_by(Subscription.created_at.desc()).first()
                        last_tier = last_sub.tier if last_sub else "pro"
                        if last_tier == "business":
                            msg = (
                                f"Halo {user.name}! ⚠️\n\n"
                                f"Paket *Business* kamu sudah berakhir.\n\n"
                                f"Perpanjang *Business* (unlimited) atau turun ke *Pro* (30 clip/bln).\n"
                                f"👉 https://youtubeclipper.asia/pricing"
                            )
                        else:
                            msg = (
                                f"Halo {user.name}! ⚠️\n\n"
                                f"Paket *Pro* kamu sudah berakhir.\n\n"
                                f"Perpanjang *Pro* (30 clip/bln) atau upgrade ke *Business* (unlimited)!\n"
                                f"👉 https://youtubeclipper.asia/pricing"
                            )
                        queue.append((user.phone, msg))
                        user.expired_reminder_count += 1
                        db.session.commit()

                # Kirim semua WA bertahap (50 per menit)
                if queue:
                    threading.Thread(target=_send_batch, args=(queue, fonnte_token), daemon=True).start()

        except Exception:
            pass
        time.sleep(86400)  # cek setiap 24 jam


threading.Thread(target=_reminder_job, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=True, port=port)

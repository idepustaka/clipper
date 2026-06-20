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
    stats = {
        "total_users":    User.query.count(),
        "free_users":     User.query.filter_by(tier="free").count(),
        "pro_users":      User.query.filter_by(tier="pro").count(),
        "business_users": User.query.filter_by(tier="business").count(),
        "total_subs":     Subscription.query.count(),
        "active_subs":    Subscription.query.filter_by(status="active").count(),
        "mrr": (User.query.filter_by(tier="pro").count() * 99000 +
                User.query.filter_by(tier="business").count() * 299000),
    }

    now = datetime.now(timezone.utc)

    # Data 30 hari terakhir untuk grafik
    chart_labels = []
    chart_users = []
    chart_pro = []
    chart_business = []
    for i in range(29, -1, -1):
        day = now - timedelta(days=i)
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        chart_labels.append(day.strftime("%d/%m"))
        chart_users.append(User.query.filter(User.created_at >= day_start, User.created_at < day_end).count())
        chart_pro.append(Subscription.query.filter(Subscription.tier=="pro", Subscription.created_at >= day_start, Subscription.created_at < day_end).count())
        chart_business.append(Subscription.query.filter(Subscription.tier=="business", Subscription.created_at >= day_start, Subscription.created_at < day_end).count())

    # Rekap harian/bulanan/tahunan
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    rekap = {
        "hari": {
            "users": User.query.filter(User.created_at >= today).count(),
            "pro": Subscription.query.filter(Subscription.tier=="pro", Subscription.created_at >= today).count(),
            "business": Subscription.query.filter(Subscription.tier=="business", Subscription.created_at >= today).count(),
        },
        "bulan": {
            "users": User.query.filter(User.created_at >= month_start).count(),
            "pro": Subscription.query.filter(Subscription.tier=="pro", Subscription.created_at >= month_start).count(),
            "business": Subscription.query.filter(Subscription.tier=="business", Subscription.created_at >= month_start).count(),
        },
        "tahun": {
            "users": User.query.filter(User.created_at >= year_start).count(),
            "pro": Subscription.query.filter(Subscription.tier=="pro", Subscription.created_at >= year_start).count(),
            "business": Subscription.query.filter(Subscription.tier=="business", Subscription.created_at >= year_start).count(),
        },
    }

    return render_template("admin.html", users=users, subs=subs, stats=stats,
                           rekap=rekap, chart_labels=chart_labels,
                           chart_users=chart_users, chart_pro=chart_pro, chart_business=chart_business)


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


def _reminder_job():
    """Kirim WA pengingat 3 hari sebelum langganan expired. Jalan setiap hari."""
    while True:
        try:
            with app.app_context():
                fonnte_token = app.config.get("FONNTE_TOKEN", "")
                if fonnte_token:
                    now = datetime.now(timezone.utc)
                    soon = now + timedelta(days=3)
                    subs = Subscription.query.filter_by(status="active").all()
                    for sub in subs:
                        if not sub.valid_until:
                            continue
                        valid = sub.valid_until
                        if valid.tzinfo is None:
                            valid = valid.replace(tzinfo=timezone.utc)
                        days_left = (valid - now).days
                        if days_left == 3:
                            user = User.query.get(sub.user_id)
                            if user and user.phone:
                                from auth import send_wa
                                msg = (
                                    f"Halo {user.name}! ⏰\n\n"
                                    f"Paket *{sub.tier.capitalize()}* kamu akan berakhir dalam *3 hari*.\n\n"
                                    f"Perpanjang sekarang agar tidak terputus!\n"
                                    f"👉 https://youtubeclipper.asia/pricing"
                                )
                                send_wa(user.phone, msg, fonnte_token)
        except Exception:
            pass
        time.sleep(86400)  # cek setiap 24 jam


threading.Thread(target=_reminder_job, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=True, port=port)

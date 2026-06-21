import threading
import time
import random
import requests as http_requests

from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from flask_login import login_required, login_user, logout_user, current_user

from models import User, db

auth = Blueprint("auth", __name__)

# Rate limiting: {ip: [timestamp, ...]}
_login_attempts = {}
MAX_ATTEMPTS = 5
BLOCK_SECONDS = 15 * 60  # 15 menit

def _check_rate_limit(ip):
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < BLOCK_SECONDS]
    if len(attempts) >= MAX_ATTEMPTS:
        return False, int(BLOCK_SECONDS - (now - attempts[0]))
    return True, 0

def _record_attempt(ip):
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < BLOCK_SECONDS]
    attempts.append(now)
    _login_attempts[ip] = attempts

def _clear_attempts(ip):
    _login_attempts.pop(ip, None)


def send_wa(phone, message, token):
    try:
        phone = phone.strip().replace("+", "").replace("-", "").replace(" ", "")
        if phone.startswith("0"):
            phone = "62" + phone[1:]
        http_requests.post(
            "https://api.fonnte.com/send",
            headers={"Authorization": token},
            data={"target": phone, "message": message},
            timeout=10,
        )
    except Exception:
        pass


@auth.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        data = request.json or request.form
        name  = (data.get("name") or "").strip()
        email = (data.get("email") or "").strip().lower()
        phone = (data.get("phone") or "").strip()
        pw    = data.get("password") or ""

        if not name or not email or not pw:
            return jsonify({"error": "Semua field wajib diisi"}), 400
        if len(pw) < 6:
            return jsonify({"error": "Password minimal 6 karakter"}), 400
        if User.query.filter_by(email=email).first():
            return jsonify({"error": "Email sudah terdaftar"}), 400

        user = User(name=name, email=email, phone=phone)
        user.set_password(pw)
        db.session.add(user)
        db.session.commit()
        login_user(user, remember=True)

        # Kirim WA selamat datang jika ada nomor HP
        from flask import current_app
        fonnte_token = current_app.config.get("FONNTE_TOKEN", "")
        if phone and fonnte_token:
            msg = (
                f"Halo {name}! 👋\n\n"
                f"Selamat datang di *YouTube Clipper*! 🎬✂️\n\n"
                f"Akun kamu sudah aktif dengan *5 clip gratis* per bulan.\n\n"
                f"Mulai clip video favorit kamu sekarang!\n"
                f"👉 https://youtubeclipper.asia"
            )
            threading.Thread(target=send_wa, args=(phone, msg, fonnte_token), daemon=True).start()

        # Notif ke admin
        if fonnte_token:
            admin_phone = "82137481104"
            admin_msg = (
                f"👤 *User Baru Daftar!*\n\n"
                f"Nama: {name}\n"
                f"Email: {email}\n"
                f"No. HP: {phone if phone else '-'}"
            )
            threading.Thread(target=send_wa, args=(admin_phone, admin_msg, fonnte_token), daemon=True).start()

        return jsonify({"ok": True, "redirect": "/"})
    return render_template("register.html")


@auth.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        ip = request.remote_addr
        allowed, wait = _check_rate_limit(ip)
        if not allowed:
            return jsonify({"error": f"Terlalu banyak percobaan login. Coba lagi dalam {wait // 60} menit."}), 429
        data  = request.json or request.form
        email = (data.get("email") or "").strip().lower()
        pw    = data.get("password") or ""
        user  = User.query.filter_by(email=email).first()
        if not user or not user.check_password(pw):
            _record_attempt(ip)
            return jsonify({"error": "Email atau password salah"}), 401
        _clear_attempts(ip)
        login_user(user, remember=True)
        return jsonify({"ok": True, "redirect": "/"})
    return render_template("login.html")


@auth.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


# OTP store: {phone_normalized: (otp, expired_at)}
_otp_store = {}

@auth.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("forgot_password.html")
    data = request.json or {}
    phone = (data.get("phone") or "").strip().replace("+", "").replace("-", "").replace(" ", "")
    if phone.startswith("0"):
        phone = "62" + phone[1:]
    if not phone:
        return jsonify({"error": "Nomor HP wajib diisi"}), 400
    user = User.query.filter(User.phone.like(f"%{phone[-9:]}")).first()
    if not user:
        return jsonify({"error": "Nomor HP tidak ditemukan"}), 404
    otp = str(random.randint(100000, 999999))
    _otp_store[phone] = (otp, time.time() + 600)  # valid 10 menit
    from flask import current_app
    fonnte_token = current_app.config.get("FONNTE_TOKEN", "")
    if fonnte_token:
        msg = f"Kode OTP reset password YouTube Clipper kamu:\n\n*{otp}*\n\nKode berlaku 10 menit. Jangan berikan ke siapapun!"
        threading.Thread(target=send_wa, args=(phone, msg, fonnte_token), daemon=True).start()
    return jsonify({"ok": True})


@auth.route("/reset-password", methods=["POST"])
def reset_password():
    data = request.json or {}
    phone = (data.get("phone") or "").strip().replace("+", "").replace("-", "").replace(" ", "")
    if phone.startswith("0"):
        phone = "62" + phone[1:]
    otp = data.get("otp", "").strip()
    pw = data.get("password", "")
    if not phone or not otp or not pw:
        return jsonify({"error": "Data tidak lengkap"}), 400
    if len(pw) < 6:
        return jsonify({"error": "Password minimal 6 karakter"}), 400
    stored = _otp_store.get(phone)
    if not stored:
        return jsonify({"error": "Kode OTP tidak valid"}), 400
    stored_otp, expired_at = stored
    if time.time() > expired_at:
        _otp_store.pop(phone, None)
        return jsonify({"error": "Kode OTP sudah kadaluarsa"}), 400
    if otp != stored_otp:
        return jsonify({"error": "Kode OTP salah"}), 400
    user = User.query.filter(User.phone.like(f"%{phone[-9:]}")).first()
    if not user:
        return jsonify({"error": "User tidak ditemukan"}), 404
    user.set_password(pw)
    db.session.commit()
    _otp_store.pop(phone, None)
    return jsonify({"ok": True})

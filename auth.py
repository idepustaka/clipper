import threading
import requests as http_requests

from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from flask_login import login_required, login_user, logout_user, current_user

from models import User, db

auth = Blueprint("auth", __name__)


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
                f"👉 http://103.13.207.57"
            )
            threading.Thread(target=send_wa, args=(phone, msg, fonnte_token), daemon=True).start()

        return jsonify({"ok": True, "redirect": "/"})
    return render_template("register.html")


@auth.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        data  = request.json or request.form
        email = (data.get("email") or "").strip().lower()
        pw    = data.get("password") or ""
        user  = User.query.filter_by(email=email).first()
        if not user or not user.check_password(pw):
            return jsonify({"error": "Email atau password salah"}), 401
        login_user(user, remember=True)
        return jsonify({"ok": True, "redirect": "/"})
    return render_template("login.html")


@auth.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))

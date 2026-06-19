from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from flask_login import login_required, login_user, logout_user, current_user

from models import User, db

auth = Blueprint("auth", __name__)


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

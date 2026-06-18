import base64
import uuid
from datetime import datetime, timedelta, timezone

import midtransclient
import requests as http_requests
import stripe
from flask import Blueprint, current_app, jsonify, request

from models import TIERS, Subscription, User, db

pay = Blueprint("pay", __name__)


# ── Xendit ────────────────────────────────────────────────────────────────────

@pay.route("/api/pay/xendit/create", methods=["POST"])
def xendit_create():
    from flask_login import current_user
    if not current_user.is_authenticated:
        return jsonify({"error": "Login diperlukan"}), 401

    data = request.json
    tier = data.get("tier")
    if tier not in ("pro", "business"):
        return jsonify({"error": "Tier tidak valid"}), 400

    cfg = current_app.config
    if not cfg["XENDIT_SECRET_KEY"]:
        return jsonify({"error": "Xendit belum dikonfigurasi"}), 503

    order_id = f"CLIP-{tier.upper()}-{uuid.uuid4().hex[:8].upper()}"
    amount = TIERS[tier]["price_idr"]

    auth = base64.b64encode(f"{cfg['XENDIT_SECRET_KEY']}:".encode()).decode()
    resp = http_requests.post(
        "https://api.xendit.co/v2/invoices",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
        json={
            "external_id": order_id,
            "amount": amount,
            "currency": "IDR",
            "description": f"YouTubeClipper {TIERS[tier]['name']} - 1 Bulan",
            "payer_email": current_user.email,
            "success_redirect_url": cfg["APP_URL"] + "/payment/success?order_id=" + order_id,
            "failure_redirect_url": cfg["APP_URL"] + "/pricing",
        },
        timeout=30,
    )

    if resp.status_code != 200:
        return jsonify({"error": "Gagal membuat invoice Xendit"}), 500

    invoice = resp.json()

    sub = Subscription(
        user_id=current_user.id,
        gateway="xendit",
        order_id=order_id,
        tier=tier,
        amount=amount,
        currency="IDR",
        status="pending",
    )
    db.session.add(sub)
    db.session.commit()

    return jsonify({"checkout_url": invoice["invoice_url"], "order_id": order_id})


@pay.route("/api/pay/xendit/webhook", methods=["POST"])
def xendit_webhook():
    data = request.json or {}
    # Verify Xendit webhook token
    cfg = current_app.config
    webhook_token = request.headers.get("x-callback-token", "")
    if cfg.get("XENDIT_WEBHOOK_TOKEN") and webhook_token != cfg["XENDIT_WEBHOOK_TOKEN"]:
        return jsonify({"ok": False}), 403

    external_id = data.get("external_id", "")
    status = data.get("status", "")

    sub = Subscription.query.filter_by(order_id=external_id).first()
    if not sub:
        return jsonify({"ok": False}), 404

    if status == "PAID":
        _activate_subscription(sub)
    elif status in ("EXPIRED", "FAILED"):
        sub.status = "expired"
        db.session.commit()

    return jsonify({"ok": True})


def get_midtrans():
    cfg = current_app.config
    return midtransclient.Snap(
        is_production=cfg["MIDTRANS_IS_PROD"],
        server_key=cfg["MIDTRANS_SERVER_KEY"],
    )


# ── Midtrans ──────────────────────────────────────────────────────────────────

@pay.route("/api/pay/midtrans/create", methods=["POST"])
def midtrans_create():
    from flask_login import current_user
    if not current_user.is_authenticated:
        return jsonify({"error": "Login diperlukan"}), 401

    data = request.json
    tier = data.get("tier")
    if tier not in ("pro", "business"):
        return jsonify({"error": "Tier tidak valid"}), 400

    cfg = current_app.config
    if not cfg["MIDTRANS_SERVER_KEY"]:
        return jsonify({"error": "Midtrans belum dikonfigurasi. Masukkan API key di .env"}), 503

    order_id = f"CLIP-{tier.upper()}-{uuid.uuid4().hex[:8].upper()}"
    amount = TIERS[tier]["price_idr"]

    transaction = get_midtrans().create_transaction({
        "transaction_details": {"order_id": order_id, "gross_amount": amount},
        "customer_details": {"email": current_user.email, "first_name": current_user.name},
        "item_details": [{"id": tier, "price": amount, "quantity": 1, "name": f"YouTubeClipper {TIERS[tier]['name']} - 1 Bulan"}],
    })

    sub = Subscription(
        user_id=current_user.id,
        gateway="midtrans",
        order_id=order_id,
        tier=tier,
        amount=amount,
        currency="IDR",
        status="pending",
    )
    db.session.add(sub)
    db.session.commit()

    return jsonify({"token": transaction["token"], "redirect_url": transaction["redirect_url"], "order_id": order_id})


@pay.route("/api/pay/midtrans/webhook", methods=["POST"])
def midtrans_webhook():
    data = request.json or {}
    order_id = data.get("order_id", "")
    status = data.get("transaction_status", "")
    fraud = data.get("fraud_status", "")

    sub = Subscription.query.filter_by(order_id=order_id).first()
    if not sub:
        return jsonify({"ok": False}), 404

    if status in ("capture", "settlement") and fraud in ("accept", ""):
        _activate_subscription(sub)
    elif status in ("deny", "cancel", "expire", "failure"):
        sub.status = "expired"
        db.session.commit()

    return jsonify({"ok": True})


# ── Stripe ────────────────────────────────────────────────────────────────────

@pay.route("/api/pay/stripe/create", methods=["POST"])
def stripe_create():
    from flask_login import current_user
    if not current_user.is_authenticated:
        return jsonify({"error": "Login diperlukan"}), 401

    data = request.json
    tier = data.get("tier")
    if tier not in ("pro", "business"):
        return jsonify({"error": "Tier tidak valid"}), 400

    cfg = current_app.config
    if not cfg["STRIPE_SECRET_KEY"]:
        return jsonify({"error": "Stripe belum dikonfigurasi. Masukkan API key di .env"}), 503

    stripe.api_key = cfg["STRIPE_SECRET_KEY"]
    order_id = f"CLIP-STRIPE-{tier.upper()}-{uuid.uuid4().hex[:8].upper()}"
    amount_cents = TIERS[tier]["price_usd"] * 100

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price_data": {
            "currency": "usd",
            "unit_amount": amount_cents,
            "product_data": {"name": f"YouTubeClipper {TIERS[tier]['name']} - 1 Bulan"},
        }, "quantity": 1}],
        mode="payment",
        customer_email=current_user.email,
        metadata={"order_id": order_id, "user_id": current_user.id, "tier": tier},
        success_url=cfg["APP_URL"] + "/payment/success?order_id=" + order_id,
        cancel_url=cfg["APP_URL"] + "/pricing",
    )

    sub = Subscription(
        user_id=current_user.id,
        gateway="stripe",
        order_id=order_id,
        tier=tier,
        amount=amount_cents,
        currency="USD",
        status="pending",
    )
    db.session.add(sub)
    db.session.commit()

    return jsonify({"checkout_url": session.url, "order_id": order_id})


@pay.route("/api/pay/stripe/webhook", methods=["POST"])
def stripe_webhook():
    cfg = current_app.config
    stripe.api_key = cfg["STRIPE_SECRET_KEY"]
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, cfg["STRIPE_WEBHOOK_SECRET"])
    except Exception:
        return jsonify({"ok": False}), 400

    if event["type"] == "checkout.session.completed":
        meta = event["data"]["object"].get("metadata", {})
        order_id = meta.get("order_id", "")
        sub = Subscription.query.filter_by(order_id=order_id).first()
        if sub:
            _activate_subscription(sub)

    return jsonify({"ok": True})


# ── Helper ────────────────────────────────────────────────────────────────────

def _activate_subscription(sub):
    sub.status = "active"
    sub.valid_until = datetime.now(timezone.utc) + timedelta(days=30)
    user = User.query.get(sub.user_id)
    if user:
        user.tier = sub.tier
        user.clips_used = 0
        user.cycle_start = datetime.now(timezone.utc)
    db.session.commit()

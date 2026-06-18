from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

TIERS = {
    "free":     {"name": "Gratis",   "clips_per_month": 5,   "price_idr": 0,      "price_usd": 0},
    "pro":      {"name": "Pro",      "clips_per_month": 100,  "price_idr": 99000,  "price_usd": 6},
    "business": {"name": "Business", "clips_per_month": None, "price_idr": 299000, "price_usd": 18},
}


class User(UserMixin, db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    name          = db.Column(db.String(80), nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    tier          = db.Column(db.String(20), default="free")
    clips_used    = db.Column(db.Integer, default=0)
    cycle_start   = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    created_at    = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    subscriptions = db.relationship("Subscription", backref="user", lazy=True)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    def clips_limit(self):
        return TIERS[self.tier]["clips_per_month"]

    def can_clip(self, n=1):
        limit = self.clips_limit()
        if limit is None:
            return True
        return (self.clips_used + n) <= limit

    def remaining_clips(self):
        limit = self.clips_limit()
        if limit is None:
            return "∞"
        return max(0, limit - self.clips_used)

    def tier_info(self):
        return TIERS.get(self.tier, TIERS["free"])


class Subscription(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    gateway         = db.Column(db.String(20))   # "midtrans" | "stripe"
    order_id        = db.Column(db.String(100), unique=True)
    tier            = db.Column(db.String(20))
    status          = db.Column(db.String(20), default="pending")  # pending|active|expired|cancelled
    amount          = db.Column(db.Integer)      # dalam rupiah atau sen USD
    currency        = db.Column(db.String(5), default="IDR")
    valid_until     = db.Column(db.DateTime)
    created_at      = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

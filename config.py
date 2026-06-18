import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "ganti-ini-dengan-key-rahasia-panjang")
    SQLALCHEMY_DATABASE_URI = "sqlite:///clipper.db"
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Midtrans
    MIDTRANS_SERVER_KEY  = os.environ.get("MIDTRANS_SERVER_KEY", "")
    MIDTRANS_CLIENT_KEY  = os.environ.get("MIDTRANS_CLIENT_KEY", "")
    MIDTRANS_IS_PROD     = os.environ.get("MIDTRANS_IS_PROD", "false").lower() == "true"

    # Stripe
    STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY", "")
    STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
    STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    # App
    APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:5001")

import os


class Config:
    database_url = os.getenv("DATABASE_URL", "sqlite:///social_listening.db")
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    SQLALCHEMY_DATABASE_URI = database_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JWT_SECRET = os.getenv("JWT_SECRET", "")
    ADMIN_USER = os.getenv("ADMIN_USER", "admin")
    ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
    X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "")
    MONTHLY_BUDGET_USD = float(os.getenv("MONTHLY_BUDGET_USD", "50"))
    POST_PRICE_USD = float(os.getenv("POST_PRICE_USD", "0.005"))
    USER_PRICE_USD = float(os.getenv("USER_PRICE_USD", "0.010"))
    ALERT_THRESHOLDS = [int(value) for value in os.getenv("ALERT_THRESHOLDS", "70,85,95").split(",")]
    CORS_ORIGIN = os.getenv("CORS_ORIGIN", "*")

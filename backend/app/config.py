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
    CAPTURE_MAX_POSTS = int(os.getenv("CAPTURE_MAX_POSTS", "1000"))
    SNAPSHOT_INTERVAL_MINUTES = int(os.getenv("SNAPSHOT_INTERVAL_MINUTES", "360"))
    SNAPSHOT_MAX_POSTS = int(os.getenv("SNAPSHOT_MAX_POSTS", "200"))
    AI_CLASSIFICATION_ENABLED = os.getenv("AI_CLASSIFICATION_ENABLED", "true").lower() in ("1", "true", "yes", "sim")
    AI_MODEL = os.getenv("AI_MODEL", "claude-opus-5-5")
    AI_EFFORT = os.getenv("AI_EFFORT", "low")
    AI_CONTEXT = os.getenv("AI_CONTEXT", "")
    AI_BATCH_SIZE = int(os.getenv("AI_BATCH_SIZE", "20"))
    AI_MAX_POSTS_PER_RUN = int(os.getenv("AI_MAX_POSTS_PER_RUN", "100"))
    AI_INTERVAL_MINUTES = int(os.getenv("AI_INTERVAL_MINUTES", "10"))
    AI_INPUT_PRICE_PER_MTOK = float(os.getenv("AI_INPUT_PRICE_PER_MTOK", "4.0"))
    AI_OUTPUT_PRICE_PER_MTOK = float(os.getenv("AI_OUTPUT_PRICE_PER_MTOK", "20.0"))
    REPORT_AI_EFFORT = os.getenv("REPORT_AI_EFFORT", "high")
    REPORT_MAX_POSTS = int(os.getenv("REPORT_MAX_POSTS", "300"))
    REPORT_CLASSIFY_LIMIT = int(os.getenv("REPORT_CLASSIFY_LIMIT", "400"))
    ALERT_THRESHOLDS = [int(value) for value in os.getenv("ALERT_THRESHOLDS", "70,85,95").split(",")]
    CORS_ORIGIN = os.getenv("CORS_ORIGIN", "*")

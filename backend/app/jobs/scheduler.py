import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import text

from app import create_app
from app.extensions import db
from app.models import Capture, MonitoringRule, SystemLog
from app.services import upsert_posts
from app.x_client import XAPIError, XClient

logging.basicConfig(level=logging.INFO)
app = create_app()


def record_error(source, exc):
    with app.app_context():
        db.session.add(SystemLog(level="ERROR", source=source, message=str(exc)[:1000]))
        db.session.commit()


def run_rule(rule_id):
    with app.app_context():
        rule = db.session.get(MonitoringRule, rule_id)
        if not rule or rule.status != "active":
            return
        try:
            token = app.config["X_BEARER_TOKEN"]
            if not token:
                raise XAPIError("X_BEARER_TOKEN não está configurado")
            client = XClient(token)
            result = client.search_recent(rule.query, since_id=rule.since_id, max_results=100)
            tweets = result.get("data") or []
            created, existing = upsert_posts(tweets, result.get("includes"), rule_id=rule.id,
                                             endpoint="search_recent", price=app.config["POST_PRICE_USD"])
            meta = result.get("meta") or {}
            rule.since_id = meta.get("newest_id") or rule.since_id
            rule.last_run_at = datetime.now(timezone.utc)
            rule.last_error = None
            db.session.add(SystemLog(level="INFO", source="radar", message=f"Regra {rule.name}: {created} novos, {existing} existentes"))
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            current = db.session.get(MonitoringRule, rule_id)
            if current:
                current.last_error = str(exc)[:1000]
                current.last_run_at = datetime.now(timezone.utc)
                db.session.add(SystemLog(level="ERROR", source=f"rule:{rule_id}", message=str(exc)[:1000]))
                db.session.commit()


def dispatch_rules():
    with app.app_context():
        now = datetime.now(timezone.utc)
        rules = MonitoringRule.query.filter_by(status="active").all()
        due_ids = [rule.id for rule in rules if not rule.last_run_at or rule.last_run_at + timedelta(minutes=rule.interval_minutes) <= now]
    for rule_id in due_ids:
        run_rule(rule_id)


def process_captures():
    with app.app_context():
        token = app.config["X_BEARER_TOKEN"]
        if not token:
            return
        capture = Capture.query.filter_by(status="queued").order_by(Capture.created_at).with_for_update(skip_locked=True).first()
        if not capture:
            return
        capture.status = "running"
        db.session.commit()
        capture_id, post_id = capture.id, capture.root_post_id
        try:
            client = XClient(token)
            original_result = client.get_post(post_id)
            original = original_result.get("data")
            if not original:
                raise XAPIError("Post não encontrado ou indisponível")
            root_id = original.get("conversation_id") or post_id
            collected = [original]
            includes = {"users": list(original_result.get("includes", {}).get("users", []))}
            replies = 0
            quotes = 0
            search_token = None
            while True:
                search = client.search_recent(f"conversation_id:{root_id}", next_token=search_token)
                batch = search.get("data") or []
                collected.extend(batch)
                includes["users"].extend(search.get("includes", {}).get("users", []))
                replies += sum(1 for item in batch if item.get("id") != post_id)
                search_token = (search.get("meta") or {}).get("next_token")
                if not search_token or len(collected) >= 500:
                    break
            quote_token = None
            while True:
                quoted = client.get_quotes(post_id, pagination_token=quote_token)
                batch = quoted.get("data") or []
                collected.extend(batch)
                includes["users"].extend(quoted.get("includes", {}).get("users", []))
                quotes += len(batch)
                quote_token = (quoted.get("meta") or {}).get("next_token")
                if not quote_token or len(collected) >= 1000:
                    break
            unique_posts = {str(item["id"]): item for item in collected if item.get("id")}
            upsert_posts(list(unique_posts.values()), includes, capture_id=capture_id,
                         endpoint="capture", price=app.config["POST_PRICE_USD"])
            with app.app_context():
                current = db.session.get(Capture, capture_id)
                if current:
                    current.status = "partial" if search_token or quote_token else "done"
                    current.replies_found = replies
                    current.quotes_found = quotes
                    current.error = "Limite de posts atingido; resultados podem estar incompletos." if search_token or quote_token else None
                    current.finished_at = datetime.now(timezone.utc)
                    db.session.commit()
        except Exception as exc:
            db.session.rollback()
            current = db.session.get(Capture, capture_id)
            if current:
                current.status = "error"
                current.error = str(exc)[:1000]
                current.finished_at = datetime.now(timezone.utc)
                db.session.add(SystemLog(level="ERROR", source=f"capture:{capture_id}", message=str(exc)[:1000]))
                db.session.commit()

def acquire_lock():
    if db.engine.dialect.name == "postgresql":
        acquired = db.session.execute(text("SELECT pg_try_advisory_lock(741852963)" )).scalar()
        db.session.commit()
        if not acquired:
            raise RuntimeError("Já existe uma instância ativa do worker")


def main():
    with app.app_context():
        db.create_all()
        acquire_lock()
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(dispatch_rules, "interval", minutes=1, id="dispatch_rules", max_instances=1, coalesce=True)
    scheduler.add_job(process_captures, "interval", seconds=15, id="process_captures", max_instances=1, coalesce=True)
    scheduler.start()


if __name__ == "__main__":
    main()

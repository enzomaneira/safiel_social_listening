import logging
import os
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
            result = XClient(token).get_post(post_id)
            tweets = result.get("data")
            if not tweets:
                raise XAPIError("Post não encontrado ou indisponível")
            upsert_posts([tweets], result.get("includes"), capture_id=capture_id,
                         endpoint="tweet_lookup", price=app.config["POST_PRICE_USD"])
            current = db.session.get(Capture, capture_id)
            current.status = "partial"
            current.error = "MVP: post raiz capturado. Coleta de replies e quotes será processada em uma próxima versão."
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

import logging
import os
import time
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import text

from app import ai_classifier, create_app, create_tables, instagram, reports
from app.extensions import db
from app.models import AIClassification, AISignal, Capture, IGCapture, MonitoringRule, Report, SystemLog, XPost
from app.services import month_spend, record_usage, take_snapshot, upsert_posts
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
            limit = max(10, rule.max_results_per_run or 100)
            fetched = created = existing = pages = 0
            newest_id = next_token = None
            # Pagina a Recent Search até esgotar os resultados ou atingir o limite da regra.
            while True:
                result = client.search_recent(rule.query, since_id=rule.since_id, next_token=next_token,
                                              max_results=min(100, limit - fetched))
                tweets = result.get("data") or []
                meta = result.get("meta") or {}
                newest_id = newest_id or meta.get("newest_id")
                page_created, page_existing = upsert_posts(tweets, result.get("includes"), rule_id=rule.id,
                                                           endpoint="search_recent", price=app.config["POST_PRICE_USD"],
                                                           user_price=app.config["USER_PRICE_USD"])
                created, existing, fetched, pages = created + page_created, existing + page_existing, fetched + len(tweets), pages + 1
                next_token = meta.get("next_token")
                if not next_token or fetched >= limit:
                    break
            rule.since_id = newest_id or rule.since_id
            rule.last_run_at = datetime.now(timezone.utc)
            rule.last_error = None
            message = f"Regra {rule.name}: {created} novos, {existing} existentes ({pages} página(s))"
            if next_token:
                message += f"; limite de {limit} posts por execução atingido, posts mais antigos desta janela não foram coletados"
            logging.info("Regra %s - %s", rule.id, message)
            db.session.add(SystemLog(level="WARNING" if next_token else "INFO", source="radar", message=message[:1000]))
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            current = db.session.get(MonitoringRule, rule_id)
            if current:
                current.last_error = str(exc)[:1000]
                current.last_run_at = datetime.now(timezone.utc)
                logging.error("Regra %s falhou: %s", rule_id, exc)
                db.session.add(SystemLog(level="ERROR", source=f"rule:{rule_id}", message=str(exc)[:1000]))
                db.session.commit()


def enforce_budget():
    """Ao atingir o orçamento mensal, suspende as regras não prioritárias; abaixo dele, reativa-as."""
    budget = app.config["MONTHLY_BUDGET_USD"]
    if not budget:
        return
    spend = month_spend()
    if spend >= budget:
        rules = db.session.query(MonitoringRule).filter(MonitoringRule.status == "active", MonitoringRule.priority != 1).all()
        for rule in rules:
            rule.status = "suspended_budget"
        if rules:
            message = (f"Orçamento mensal atingido (US$ {spend:.2f} de US$ {budget:.2f}): "
                       f"{len(rules)} regra(s) não prioritária(s) suspensa(s); regras de prioridade alta continuam ativas")
            logging.warning(message)
            db.session.add(SystemLog(level="WARNING", source="budget", message=message))
    else:
        rules = db.session.query(MonitoringRule).filter_by(status="suspended_budget").all()
        for rule in rules:
            rule.status = "active"
        if rules:
            message = f"Gasto abaixo do orçamento (US$ {spend:.2f} de US$ {budget:.2f}): {len(rules)} regra(s) reativada(s)"
            logging.info(message)
            db.session.add(SystemLog(level="INFO", source="budget", message=message))
    db.session.commit()


def dispatch_rules():
    with app.app_context():
        enforce_budget()
        now = datetime.now(timezone.utc)
        rules = db.session.query(MonitoringRule).filter_by(status="active").all()
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
            max_posts = max(10, app.config["CAPTURE_MAX_POSTS"])
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
                if not search_token or len(collected) >= max_posts // 2:
                    break
            quote_token = None
            while True:
                quoted = client.get_quotes(post_id, pagination_token=quote_token)
                batch = quoted.get("data") or []
                collected.extend(batch)
                includes["users"].extend(quoted.get("includes", {}).get("users", []))
                quotes += len(batch)
                quote_token = (quoted.get("meta") or {}).get("next_token")
                if not quote_token or len(collected) >= max_posts:
                    break
            unique_posts = {str(item["id"]): item for item in collected if item.get("id")}
            upsert_posts(list(unique_posts.values()), includes, capture_id=capture_id,
                         endpoint="capture", price=app.config["POST_PRICE_USD"], user_price=app.config["USER_PRICE_USD"])
            logging.info("Captura %s: %s respostas, %s quotes, %s posts salvos", capture_id, replies, quotes, len(unique_posts))
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
                logging.error("Captura %s falhou: %s", capture_id, exc)
                db.session.add(SystemLog(level="ERROR", source=f"capture:{capture_id}", message=str(exc)[:1000]))
                db.session.commit()


def process_ig_captures():
    """Coleta os comentários de um post da conta do Instagram conectada."""
    with app.app_context():
        capture = IGCapture.query.filter_by(status="queued").order_by(IGCapture.created_at).with_for_update(skip_locked=True).first()
        if not capture:
            return
        capture.status = "running"
        db.session.commit()
        capture_id = capture.id
        try:
            client = instagram.load_client(app.config)
            comments, replies = instagram.run_capture(capture_id, client, max(1, app.config["INSTAGRAM_CAPTURE_MAX_COMMENTS"]))
            message = f"Captura Instagram {capture_id}: {comments} comentários e {replies} respostas salvos"
            logging.info(message)
            db.session.add(SystemLog(level="INFO", source=f"ig_capture:{capture_id}", message=message))
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            current = db.session.get(IGCapture, capture_id)
            if current:
                current.status = "error"
                current.error = str(exc)[:1000]
                current.finished_at = datetime.now(timezone.utc)
                logging.error("Captura Instagram %s falhou: %s", capture_id, exc)
                db.session.add(SystemLog(level="ERROR", source=f"ig_capture:{capture_id}", message=str(exc)[:1000]))
                db.session.commit()


def snapshot_relevant_posts():
    """Atualiza as métricas dos posts marcados como relevantes e guarda o histórico."""
    with app.app_context():
        token = app.config["X_BEARER_TOKEN"]
        budget = app.config["MONTHLY_BUDGET_USD"]
        if not token:
            return
        if budget and month_spend() >= budget:
            logging.warning("Snapshots de métricas adiados: orçamento mensal atingido")
            return
        posts = (db.session.query(XPost).filter(XPost.is_relevant.is_(True))
                 .order_by(XPost.created_at.desc()).limit(app.config["SNAPSHOT_MAX_POSTS"]).all())
        if not posts:
            return
        by_id = {post.post_id: post for post in posts}
        ids = list(by_id)
        updated = 0
        try:
            client = XClient(token)
            for start in range(0, len(ids), 100):
                chunk = ids[start:start + 100]
                data = client.lookup_posts(chunk).get("data") or []
                for item in data:
                    post = by_id.get(str(item.get("id")))
                    metrics = item.get("public_metrics") or {}
                    if not post:
                        continue
                    post.like_count = metrics.get("like_count", post.like_count)
                    post.reply_count = metrics.get("reply_count", post.reply_count)
                    post.repost_count = metrics.get("retweet_count", post.repost_count)
                    post.quote_count = metrics.get("quote_count", post.quote_count)
                    post.bookmark_count = metrics.get("bookmark_count", post.bookmark_count)
                    post.impression_count = metrics.get("impression_count", post.impression_count)
                    post.last_updated_at = datetime.now(timezone.utc)
                    take_snapshot(post)
                    updated += 1
                record_usage("metrics_snapshot", len(data), len(data) * app.config["POST_PRICE_USD"])
                db.session.commit()
            message = f"Snapshots de métricas: {updated} de {len(ids)} posts relevantes atualizados"
            logging.info(message)
            db.session.add(SystemLog(level="INFO", source="snapshots", message=message))
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logging.error("Snapshots de métricas falharam: %s", exc)
            db.session.add(SystemLog(level="ERROR", source="snapshots", message=str(exc)[:1000]))
            db.session.commit()


def ai_available():
    budget = app.config["MONTHLY_BUDGET_USD"]
    return (app.config["AI_CLASSIFICATION_ENABLED"] and ai_classifier.is_configured()
            and not (budget and month_spend() >= budget))


def classify_and_save(posts):
    """Classifica posts com IA e grava classificação + sinais. Retorna (classificados, recusados, custo, avisos)."""
    parent_ids = {post.in_reply_to_post_id or post.quoted_post_id for post in posts} - {None}
    parents = {post.post_id: post.text for post in db.session.query(XPost).filter(XPost.post_id.in_(parent_ids)).all()} if parent_ids else {}
    outcomes, cost, warnings = ai_classifier.classify_posts(posts, app.config, parents)
    classified_at = datetime.now(timezone.utc)
    refused = 0
    for post_id, fields in outcomes:
        if fields is None:
            refused += 1
            db.session.merge(AIClassification(post_id=post_id, model=ai_classifier.REFUSED_MODEL_LABEL, classified_at=classified_at))
            continue
        fields = dict(fields)
        signals = {key: fields.pop(key) for key in ai_classifier.SIGNAL_FIELDS}
        db.session.merge(AIClassification(post_id=post_id, model=app.config["AI_MODEL"], classified_at=classified_at, **fields))
        db.session.merge(AISignal(post_id=post_id, **signals))
    record_usage("ai_classification", len(outcomes), cost)
    db.session.commit()
    return len(outcomes) - refused, refused, cost, warnings


def unclassified_query():
    return (db.session.query(XPost).outerjoin(AIClassification, AIClassification.post_id == XPost.post_id)
            .filter(AIClassification.post_id.is_(None), XPost.post_type != "repost", XPost.text.isnot(None)))


def classify_pending_posts():
    """Classifica com IA os posts ainda sem análise (sentimento, posicionamento, narrativa...)."""
    with app.app_context():
        if not app.config["AI_CLASSIFICATION_ENABLED"] or not ai_classifier.is_configured():
            return
        if not ai_available():
            logging.warning("Classificação por IA adiada: orçamento mensal atingido")
            return
        posts = unclassified_query().order_by(XPost.created_at.desc()).limit(app.config["AI_MAX_POSTS_PER_RUN"]).all()
        if not posts:
            return
        try:
            classified, refused, cost, warnings = classify_and_save(posts)
        except Exception as exc:
            db.session.rollback()
            logging.error("Classificação por IA falhou: %s", exc)
            db.session.add(SystemLog(level="ERROR", source="ai", message=str(exc)[:1000]))
            db.session.commit()
            return
        message = f"Classificação por IA: {classified} posts classificados, {refused} recusados (US$ {cost:.4f})"
        if warnings:
            message += f"; {len(warnings)} sem resposta válida (serão tentados de novo)"
        logging.info(message)
        db.session.add(SystemLog(level="INFO", source="ai", message=message))
        db.session.commit()


def report_posts_query(params):
    start = datetime.fromisoformat(params["date_from"]).replace(tzinfo=reports.BRT)
    end = datetime.fromisoformat(params["date_to"]).replace(tzinfo=reports.BRT) + timedelta(days=1)
    query = db.session.query(XPost).filter(XPost.created_at >= start, XPost.created_at < end)
    if params.get("rule_id"):
        query = query.filter(XPost.source_rule_id == params["rule_id"])
    return query


def process_reports():
    """Gera os relatórios na fila: classifica o que faltar, calcula indicadores, escreve a análise e monta o .docx."""
    with app.app_context():
        report = Report.query.filter_by(status="queued").order_by(Report.created_at).with_for_update(skip_locked=True).first()
        if not report:
            return
        report.status = "running"
        db.session.commit()
        report_id, params, title = report.id, dict(report.params or {}), report.title
        try:
            cost, notice = 0.0, None
            use_ai = ai_available()
            if use_ai:
                pending = (unclassified_query().filter(XPost.post_id.in_(report_posts_query(params).with_entities(XPost.post_id)))
                           .limit(app.config["REPORT_CLASSIFY_LIMIT"]).all())
                if pending:
                    cost += classify_and_save(pending)[2]
                    db.session.expire_all()
            posts = report_posts_query(params).all()
            if not posts:
                raise RuntimeError("Nenhum post encontrado no período e na regra selecionados")
            metrics = reports.compute_metrics(posts)
            corpus = reports.corpus_for_ai(posts, app.config["REPORT_MAX_POSTS"])
            analysis = None
            if not use_ai:
                notice = ("Análise qualitativa não gerada: a IA está desativada, sem chave configurada ou o orçamento mensal "
                          "foi atingido. O relatório contém só os indicadores.")
            elif not corpus:
                notice = "Nenhum post do recorte foi classificado como pertinente ao tema; não há base para a análise qualitativa."
            else:
                analysis, analysis_cost, notice = reports.write_analysis(metrics, corpus, params, app.config)
                cost += analysis_cost
                record_usage("ai_report", len(corpus), analysis_cost)
            document = reports.build_docx(title, params, metrics, analysis, corpus, notice)
            current = db.session.get(Report, report_id)
            current.status, current.docx, current.cost_usd = "done", document, round(cost, 4)
            current.summary = {"metrics": metrics, "notice": notice,
                               "executive_summary": analysis["executive_summary"] if analysis else None,
                               "central_reading": analysis["central_reading"] if analysis else None}
            current.finished_at = datetime.now(timezone.utc)
            logging.info("Relatório %s gerado: %s posts, núcleo %s, US$ %.4f", report_id, metrics["total"], metrics["core"], cost)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            current = db.session.get(Report, report_id)
            current.status, current.error, current.finished_at = "error", str(exc)[:1000], datetime.now(timezone.utc)
            logging.error("Relatório %s falhou: %s", report_id, exc)
            db.session.add(SystemLog(level="ERROR", source=f"report:{report_id}", message=str(exc)[:1000]))
            db.session.commit()


def acquire_lock(timeout=None, interval=5):
    # Durante um redeploy o Railway mantém o container antigo rodando por alguns minutos;
    # em vez de encerrar na hora, espera o lock ser liberado.
    if db.engine.dialect.name != "postgresql":
        return
    timeout = int(os.getenv("WORKER_LOCK_WAIT_SECONDS", "600")) if timeout is None else timeout
    deadline = time.monotonic() + timeout
    while True:
        acquired = db.session.execute(text("SELECT pg_try_advisory_lock(741852963)")).scalar()
        db.session.commit()
        if acquired:
            logging.info("Lock do worker adquirido")
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Já existe uma instância ativa do worker (aguardou {timeout}s)")
        logging.warning("Outra instância do worker ainda está ativa; nova tentativa em %ss", interval)
        time.sleep(interval)


def main():
    with app.app_context():
        create_tables()
        acquire_lock()
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(dispatch_rules, "interval", minutes=1, id="dispatch_rules", max_instances=1, coalesce=True)
    scheduler.add_job(process_captures, "interval", seconds=15, id="process_captures", max_instances=1, coalesce=True)
    scheduler.add_job(process_ig_captures, "interval", seconds=15, id="process_ig_captures", max_instances=1, coalesce=True)
    scheduler.add_job(process_reports, "interval", seconds=20, id="process_reports", max_instances=1, coalesce=True)
    scheduler.add_job(classify_pending_posts, "interval", minutes=max(1, app.config["AI_INTERVAL_MINUTES"]),
                      id="classify_pending_posts", max_instances=1, coalesce=True)
    scheduler.add_job(snapshot_relevant_posts, "interval", minutes=max(15, app.config["SNAPSHOT_INTERVAL_MINUTES"]),
                      id="snapshot_relevant_posts", max_instances=1, coalesce=True)
    scheduler.start()


if __name__ == "__main__":
    main()

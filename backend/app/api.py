import csv
import io
import re
import time
from datetime import date, datetime, timedelta, timezone
from functools import wraps

import bcrypt
import jwt
from flask import Blueprint, current_app, jsonify, request, send_file
from sqlalchemy import func, or_

from .extensions import db
from .models import AIClassification, ApiUsage, Capture, MonitoringRule, SystemLog, XAuthor, XMetricSnapshot, XPost
from .services import month_spend, take_snapshot
from .x_client import XAPIError, XClient

api = Blueprint("api", __name__)


def require_auth(fn):
    @wraps(fn)
    def decorated(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        token = header.removeprefix("Bearer ")
        try:
            if not token:
                raise ValueError("missing token")
            jwt.decode(token, current_app.config["JWT_SECRET"], algorithms=["HS256"])
        except (jwt.PyJWTError, ValueError):
            return jsonify({"error": "Não autenticado"}), 401
        return fn(*args, **kwargs)
    return decorated


def serialize_rule(rule):
    return {"id": rule.id, "name": rule.name, "query": rule.query, "group": rule.grp,
            "priority": rule.priority, "interval_minutes": rule.interval_minutes,
            "max_results_per_run": rule.max_results_per_run, "status": rule.status,
            "since_id": rule.since_id, "last_run_at": rule.last_run_at.isoformat() if rule.last_run_at else None,
            "last_error": rule.last_error}


def serialize_post(post):
    author = post.author
    return {"post_id": post.post_id, "text": post.text, "author_id": post.author_id,
            "author_username": author.username if author else None, "author_name": author.name if author else None,
            "created_at": post.created_at.isoformat() if post.created_at else None,
            "post_url": post.post_url, "post_type": post.post_type, "is_relevant": post.is_relevant,
            "like_count": post.like_count, "reply_count": post.reply_count,
            "repost_count": post.repost_count, "quote_count": post.quote_count,
            "impression_count": post.impression_count, "bookmark_count": post.bookmark_count,
            "conversation_id": post.conversation_id, "in_reply_to_post_id": post.in_reply_to_post_id,
            "quoted_post_id": post.quoted_post_id}


@api.post("/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    password_hash = current_app.config["ADMIN_PASSWORD_HASH"]
    try:
        valid_password = bcrypt.checkpw(data.get("password", "").encode(), password_hash.encode())
    except (ValueError, TypeError):
        valid_password = False
    if data.get("username") != current_app.config["ADMIN_USER"] or not valid_password:
        return jsonify({"error": "Usuário ou senha inválidos"}), 401
    if not current_app.config["JWT_SECRET"]:
        return jsonify({"error": "JWT_SECRET não configurado"}), 500
    token = jwt.encode({"sub": data["username"], "exp": datetime.now(timezone.utc) + timedelta(hours=12)},
                       current_app.config["JWT_SECRET"], algorithm="HS256")
    return jsonify({"token": token})


@api.get("/rules")
@require_auth
def list_rules():
    return jsonify([serialize_rule(rule) for rule in db.session.query(MonitoringRule).order_by(MonitoringRule.id.desc()).all()])


@api.post("/rules")
@require_auth
def create_rule():
    data = request.get_json(silent=True) or {}
    name, query = str(data.get("name", "")).strip(), str(data.get("query", "")).strip()
    if not name or not query or len(query) > 512:
        return jsonify({"error": "Nome e query são obrigatórios; query deve ter até 512 caracteres"}), 400
    rule = MonitoringRule(name=name, query=query, grp=data.get("group"), priority=int(data.get("priority", 2)),
                          interval_minutes=max(1, int(data.get("interval_minutes", 30))),
                          max_results_per_run=min(5000, max(1, int(data.get("max_results_per_run", 500)))))
    db.session.add(rule)
    db.session.commit()
    return jsonify(serialize_rule(rule)), 201


@api.patch("/rules/<int:rule_id>")
@require_auth
def update_rule(rule_id):
    rule = db.session.get(MonitoringRule, rule_id)
    if not rule:
        return jsonify({"error": "Regra não encontrada"}), 404
    data = request.get_json(silent=True) or {}
    budget = current_app.config["MONTHLY_BUDGET_USD"]
    priority = int(data.get("priority", rule.priority or 2))
    if data.get("status") == "active" and priority != 1 and budget and month_spend() >= budget:
        return jsonify({"error": "Orçamento mensal atingido: só regras de prioridade alta podem ser ativadas. "
                                 "Aumente o orçamento ou mude a prioridade da regra para Alta."}), 400
    for key, field in (("name", "name"), ("query", "query"), ("group", "grp"), ("priority", "priority"),
                       ("interval_minutes", "interval_minutes"), ("max_results_per_run", "max_results_per_run"),
                       ("status", "status")):
        if key in data:
            setattr(rule, field, data[key])
    db.session.commit()
    return jsonify(serialize_rule(rule))


@api.delete("/rules/<int:rule_id>")
@require_auth
def delete_rule(rule_id):
    rule = db.session.get(MonitoringRule, rule_id)
    if not rule:
        return jsonify({"error": "Regra não encontrada"}), 404
    XPost.query.filter_by(source_rule_id=rule.id).update({"source_rule_id": None})
    db.session.delete(rule)
    db.session.commit()
    return "", 204


@api.post("/rules/<int:rule_id>/run")
@require_auth
def run_rule(rule_id):
    rule = db.session.get(MonitoringRule, rule_id)
    if not rule:
        return jsonify({"error": "Regra não encontrada"}), 404
    if rule.status == "suspended_budget":
        return jsonify({"error": "Regra suspensa porque o orçamento mensal foi atingido"}), 400
    if rule.status != "active":
        return jsonify({"error": "Ative a regra para executá-la"}), 400
    # A coleta roda no worker: executá-la aqui podia passar do timeout do gunicorn
    # quando a API do X pede para aguardar (429). Sem last_run_at, o dispatch_rules
    # executa a regra no próximo ciclo (até 1 minuto).
    rule.last_run_at = None
    db.session.commit()
    return jsonify({"message": "Execução agendada: o worker vai coletar esta regra em até 1 minuto.",
                    "rule": serialize_rule(rule)}), 202


@api.post("/captures")
@require_auth
def create_capture():
    data = request.get_json(silent=True) or {}
    source_url = str(data.get("url", "")).strip()
    match = re.search(r"(?:x|twitter)\.com/\w+/status/(\d+)", source_url, flags=re.IGNORECASE)
    if not match:
        return jsonify({"error": "URL do X inválida"}), 400
    capture = Capture(source_url=source_url, root_post_id=match.group(1))
    db.session.add(capture)
    db.session.commit()
    return jsonify({"id": capture.id, "status": capture.status, "warning": "A captura pode ser incompleta conforme disponibilidade e acesso da API."}), 202


@api.get("/captures")
@require_auth
def list_captures():
    items = Capture.query.order_by(Capture.created_at.desc()).limit(100).all()
    return jsonify([{"id": c.id, "source_url": c.source_url, "root_post_id": c.root_post_id, "status": c.status,
                    "replies_found": c.replies_found, "quotes_found": c.quotes_found, "error": c.error,
                    "created_at": c.created_at.isoformat() if c.created_at else None} for c in items])


@api.get("/captures/<int:capture_id>")
@require_auth
def get_capture(capture_id):
    c = db.session.get(Capture, capture_id)
    if not c:
        return jsonify({"error": "Captura não encontrada"}), 404
    return jsonify({"id": c.id, "source_url": c.source_url, "root_post_id": c.root_post_id, "status": c.status,
                    "replies_found": c.replies_found, "quotes_found": c.quotes_found, "error": c.error,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                    "finished_at": c.finished_at.isoformat() if c.finished_at else None})


def capture_scope(capture):
    """Posts de uma captura: o post raiz, toda a conversa dele e os quotes do post."""
    root = db.session.get(XPost, capture.root_post_id)
    conversation_id = (root.conversation_id if root else None) or capture.root_post_id
    return conversation_id, or_(XPost.post_id == capture.root_post_id, XPost.conversation_id == conversation_id,
                                XPost.quoted_post_id == capture.root_post_id)


@api.get("/captures/<int:capture_id>/tree")
@require_auth
def capture_tree(capture_id):
    capture = db.session.get(Capture, capture_id)
    if not capture:
        return jsonify({"error": "Captura não encontrada"}), 404
    conversation_id, scope = capture_scope(capture)
    posts = XPost.query.filter(scope).order_by(XPost.created_at.asc()).limit(2000).all()
    conversation = [post for post in posts if post.conversation_id == conversation_id or post.post_id == conversation_id]
    quotes = [post for post in posts if post.quoted_post_id == capture.root_post_id and post not in conversation]
    by_id = {post.post_id: post for post in conversation}
    root_id = conversation_id if conversation_id in by_id else capture.root_post_id
    children = {}
    for post in conversation:
        if post.post_id == root_id:
            continue
        parent = post.in_reply_to_post_id if post.in_reply_to_post_id in by_id else root_id
        children.setdefault(parent, []).append(post)
    # Percorre a árvore em profundidade, reconstruída por referenced_tweets (replied_to).
    ordered, stack, seen = [], ([(by_id[root_id], 0)] if root_id in by_id else []), set()
    while stack:
        post, depth = stack.pop()
        if post.post_id in seen:
            continue
        seen.add(post.post_id)
        item = serialize_post(post)
        item.update(depth=depth, parent_missing=bool(post.in_reply_to_post_id and post.in_reply_to_post_id not in by_id
                                                        and post.post_id != root_id))
        ordered.append(item)
        stack.extend((child, depth + 1) for child in reversed(children.get(post.post_id, [])))
    ordered.extend(dict(serialize_post(post), depth=1, parent_missing=True) for post in conversation if post.post_id not in seen)
    return jsonify({"capture_id": capture.id, "root_post_id": capture.root_post_id, "conversation_id": conversation_id,
                    "root_found": root_id in by_id, "conversation": ordered,
                    "quotes": [serialize_post(post) for post in quotes]})


def filtered_posts():
    query = XPost.query
    capture_id = request.args.get("capture_id", type=int)
    if capture_id:
        capture = db.session.get(Capture, capture_id)
        query = query.filter(capture_scope(capture)[1]) if capture else query.filter(False)
    text = request.args.get("q")
    if text:
        query = query.filter(XPost.text.ilike(f"%{text[:200]}%"))
    author = request.args.get("author")
    if author:
        author = author.strip().removeprefix("@")[:100]
        query = query.filter(or_(XPost.author_id == author, XPost.author.has(or_(
            func.lower(XAuthor.username) == author.lower(), XAuthor.name.ilike(f"%{author}%")))))
    if request.args.get("rule_id", type=int):
        query = query.filter(XPost.source_rule_id == request.args.get("rule_id", type=int))
    if request.args.get("type") in ("original", "reply", "quote", "repost"):
        query = query.filter(XPost.post_type == request.args["type"])
    relevant = request.args.get("relevant")
    if relevant in ("true", "false"):
        query = query.filter(XPost.is_relevant.is_(relevant == "true"))
    for param, operator in (("from", "ge"), ("to", "le")):
        raw = request.args.get(param)
        if raw:
            try:
                value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            if operator == "ge":
                query = query.filter(XPost.created_at >= value)
            elif len(raw) == 10:
                # Só a data (AAAA-MM-DD): inclui o dia inteiro.
                query = query.filter(XPost.created_at < value + timedelta(days=1))
            else:
                query = query.filter(XPost.created_at <= value)
    return query


@api.get("/posts")
@require_auth
def list_posts():
    page = max(1, request.args.get("page", 1, type=int))
    page_size = min(200, max(1, request.args.get("page_size", 25, type=int)))
    query = filtered_posts().order_by(XPost.created_at.desc())
    pagination = query.paginate(page=page, per_page=page_size, error_out=False)
    return jsonify({"items": [serialize_post(post) for post in pagination.items], "page": page,
                    "page_size": page_size, "total": pagination.total, "pages": pagination.pages})


@api.patch("/posts/<post_id>")
@require_auth
def update_post(post_id):
    post = db.session.get(XPost, post_id)
    if not post:
        return jsonify({"error": "Post não encontrado"}), 404
    data = request.get_json(silent=True) or {}
    if "is_relevant" not in data or not isinstance(data["is_relevant"], bool):
        return jsonify({"error": "is_relevant boolean é obrigatório"}), 400
    if data["is_relevant"] and not post.is_relevant:
        take_snapshot(post)
    post.is_relevant = data["is_relevant"]
    db.session.commit()
    return jsonify(serialize_post(post))


@api.get("/posts/<post_id>/snapshots")
@require_auth
def post_snapshots(post_id):
    rows = (XMetricSnapshot.query.filter_by(post_id=post_id).order_by(XMetricSnapshot.captured_at.desc()).limit(200).all())
    return jsonify([{"captured_at": row.captured_at.isoformat() if row.captured_at else None, "like_count": row.like_count,
                     "reply_count": row.reply_count, "repost_count": row.repost_count, "quote_count": row.quote_count,
                     "impression_count": row.impression_count} for row in rows])


@api.delete("/posts/<post_id>")
@require_auth
def delete_post(post_id):
    post = db.session.get(XPost, post_id)
    if not post:
        return jsonify({"error": "Post não encontrado"}), 404
    XMetricSnapshot.query.filter_by(post_id=post_id).delete()
    AIClassification.query.filter_by(post_id=post_id).delete()
    db.session.delete(post)
    db.session.add(SystemLog(level="INFO", source="posts", message=f"Post {post_id} removido pelo painel"))
    db.session.commit()
    return "", 204


@api.get("/stats")
@require_auth
def stats():
    now = datetime.now(timezone.utc)
    counts = {}
    for hours, label in ((6, "6h"), (12, "12h"), (24, "24h"), (168, "7d")):
        counts[label] = XPost.query.filter(XPost.created_at >= now - timedelta(hours=hours)).count()
    return jsonify({"posts": counts, "rules": {"active": db.session.query(MonitoringRule).filter_by(status="active").count(),
                    "total": db.session.query(MonitoringRule).count()}, "latest_post_at": db.session.query(func.max(XPost.created_at)).scalar().isoformat()
                    if db.session.query(func.max(XPost.created_at)).scalar() else None})


_x_usage_cache = {"at": 0.0, "value": None}


def official_x_usage():
    """Consumo oficial da X API (GET /2/usage/tweets), em cache por 10 minutos."""
    if time.monotonic() - _x_usage_cache["at"] < 600:
        return _x_usage_cache["value"]
    token = current_app.config["X_BEARER_TOKEN"]
    if not token:
        value = {"error": "X_BEARER_TOKEN não configurado"}
    else:
        try:
            data = XClient(token, max_attempts=1, timeout=8).get_usage().get("data") or {}
            daily = (data.get("daily_project_usage") or {}).get("usage") or []
            value = {"project_usage": int(data["project_usage"]) if data.get("project_usage") is not None else None,
                     "project_cap": int(data["project_cap"]) if data.get("project_cap") is not None else None,
                     "cap_reset_day": data.get("cap_reset_day"),
                     "daily": [{"date": str(item.get("date", ""))[:10], "usage": int(item.get("usage") or 0)} for item in daily]}
        except (XAPIError, ValueError, KeyError, TypeError) as exc:
            value = {"error": str(exc)[:300]}
    _x_usage_cache.update(at=time.monotonic(), value=value)
    return value


@api.get("/usage")
@require_auth
def usage():
    first_day = datetime.now(timezone.utc).date().replace(day=1)
    rows = ApiUsage.query.filter(ApiUsage.date >= first_day).order_by(ApiUsage.date.desc()).all()
    spend = month_spend()
    budget = current_app.config["MONTHLY_BUDGET_USD"]
    pct = round((spend / budget) * 100, 1) if budget else 0
    suspended = db.session.query(MonitoringRule).filter_by(status="suspended_budget").count()
    return jsonify({"month_spend_usd": spend, "budget_usd": budget, "percent": pct, "suspended_rules": suspended,
                    "prices": {"post_usd": current_app.config["POST_PRICE_USD"], "user_usd": current_app.config["USER_PRICE_USD"]},
                    "alerts": [threshold for threshold in current_app.config["ALERT_THRESHOLDS"] if pct >= threshold],
                    "x_usage": official_x_usage(),
                    "by_day": [{"date": row.date.isoformat(), "endpoint": row.endpoint, "requests": row.requests,
                                "resources_read": row.resources_read, "est_cost_usd": float(row.est_cost_usd or 0)} for row in rows]})


@api.get("/status")
@require_auth
def status():
    errors = SystemLog.query.filter(SystemLog.level.in_(["ERROR", "CRITICAL"])).order_by(SystemLog.ts.desc()).limit(10).all()
    latest = db.session.query(func.max(MonitoringRule.last_run_at)).scalar()
    return jsonify({"status": "ok", "last_run_at": latest.isoformat() if latest else None,
                    "recent_errors": [{"ts": log.ts.isoformat(), "source": log.source, "message": log.message} for log in errors]})


EXPORT_FIELDS = ["post_id", "created_at", "author_id", "author_username", "author_name", "text", "post_url", "post_type",
                 "is_relevant", "language", "conversation_id", "in_reply_to_post_id", "quoted_post_id",
                 "like_count", "reply_count", "repost_count", "quote_count", "bookmark_count", "impression_count",
                 "hashtags", "mentions", "urls", "source_rule_id", "source_capture_id", "first_collected_at"]


def export_value(post, field):
    if field in ("author_username", "author_name"):
        return getattr(post.author, field.removeprefix("author_"), None) if post.author else None
    value = getattr(post, field)
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if item)
    if isinstance(value, datetime):
        # Excel não aceita datas com fuso; exporta em UTC sem tzinfo.
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    return value


@api.get("/export")
@require_auth
def export_posts():
    file_format = request.args.get("format", "csv").lower()
    if file_format not in ("csv", "xlsx"):
        return jsonify({"error": "format deve ser csv ou xlsx"}), 400
    posts = filtered_posts().order_by(XPost.created_at.desc()).limit(10000).all()
    capture_id = request.args.get("capture_id", type=int)
    basename = f"captura-{capture_id}" if capture_id else "posts"
    output = io.BytesIO()
    if file_format == "csv":
        text_stream = io.StringIO()
        # Ponto e vírgula: separador que o Excel em português espera ao abrir um CSV.
        writer = csv.writer(text_stream, delimiter=";")
        writer.writerow(EXPORT_FIELDS)
        for post in posts:
            row = []
            for field in EXPORT_FIELDS:
                value = export_value(post, field)
                row.append(value.strftime("%Y-%m-%d %H:%M:%S") if isinstance(value, datetime) else value)
            writer.writerow(row)
        output.write(text_stream.getvalue().encode("utf-8-sig"))
        mime, filename = "text/csv", f"{basename}.csv"
    else:
        from openpyxl import Workbook
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Posts"
        sheet.append(EXPORT_FIELDS)
        for post in posts:
            sheet.append([export_value(post, field) for field in EXPORT_FIELDS])
        sheet.freeze_panes = "A2"
        workbook.save(output)
        mime, filename = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"{basename}.xlsx"
    output.seek(0)
    return send_file(output, mimetype=mime, as_attachment=True, download_name=filename)

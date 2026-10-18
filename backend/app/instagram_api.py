import csv
import io
import importlib.util

from flask import Blueprint, current_app, jsonify, request, send_file
from sqlalchemy import func, or_

from .api import require_auth
from .extensions import db
from .instagram import iso, parse_shortcode, serialize_capture, serialize_comment, serialize_media
from .models import IGCapture, IGComment, IGMedia

ig_api = Blueprint("instagram", __name__)

EXPORT_FIELDS = ["comment_id", "parent_id", "tipo", "username", "text", "created_at", "like_count", "hidden"]


@ig_api.get("/status")
@require_auth
def status():
    return jsonify({"configured": bool(current_app.config["INSTAGRAM_ACCESS_TOKEN"]),
                    "client_available": importlib.util.find_spec(f"{__package__}.ig_client") is not None})


@ig_api.post("/captures")
@require_auth
def create_capture():
    data = request.get_json(silent=True) or {}
    source_url = str(data.get("url", "")).strip()
    shortcode = parse_shortcode(source_url)
    if not shortcode:
        return jsonify({"error": "Link do Instagram inválido. Use o link de um post ou reel, como https://www.instagram.com/p/ABC123/"}), 400
    capture = IGCapture(source_url=source_url, shortcode=shortcode)
    db.session.add(capture)
    db.session.commit()
    return jsonify(serialize_capture(capture)), 202


@ig_api.get("/captures")
@require_auth
def list_captures():
    items = IGCapture.query.order_by(IGCapture.created_at.desc()).limit(100).all()
    return jsonify([serialize_capture(capture) for capture in items])


@ig_api.get("/captures/<int:capture_id>")
@require_auth
def get_capture(capture_id):
    capture = db.session.get(IGCapture, capture_id)
    if not capture:
        return jsonify({"error": "Captura não encontrada"}), 404
    return jsonify(serialize_capture(capture))


def stored_counts(media_ids):
    if not media_ids:
        return {}
    rows = (db.session.query(IGComment.media_id, func.count(IGComment.comment_id))
            .filter(IGComment.media_id.in_(media_ids)).group_by(IGComment.media_id).all())
    return dict(rows)


@ig_api.get("/media")
@require_auth
def list_media():
    items = IGMedia.query.order_by(IGMedia.created_at.desc()).limit(200).all()
    counts = stored_counts([media.media_id for media in items])
    return jsonify([serialize_media(media, counts.get(media.media_id, 0)) for media in items])


@ig_api.get("/media/<media_id>")
@require_auth
def get_media(media_id):
    media = db.session.get(IGMedia, media_id)
    if not media:
        return jsonify({"error": "Post não encontrado"}), 404
    return jsonify(serialize_media(media, stored_counts([media_id]).get(media_id, 0)))


def sorted_comments(query, sort):
    if sort == "oldest":
        return query.order_by(IGComment.created_at.asc())
    if sort == "likes":
        return query.order_by(func.coalesce(IGComment.like_count, 0).desc(), IGComment.created_at.desc())
    return query.order_by(IGComment.created_at.desc())


@ig_api.get("/media/<media_id>/comments")
@require_auth
def list_comments(media_id):
    if not db.session.get(IGMedia, media_id):
        return jsonify({"error": "Post não encontrado"}), 404
    page = max(1, request.args.get("page", 1, type=int))
    page_size = min(200, max(1, request.args.get("page_size", 50, type=int)))
    sort = request.args.get("sort", "recent")
    text = (request.args.get("q") or "").strip()[:200]
    base = IGComment.query.filter(IGComment.media_id == media_id)
    if text:
        # Com busca, a lista é plana: comentários e respostas que contêm o termo.
        query = base.filter(or_(IGComment.text.ilike(f"%{text}%"), IGComment.username.ilike(f"%{text.lstrip('@')}%")))
        pagination = sorted_comments(query, sort).paginate(page=page, per_page=page_size, error_out=False)
        items = [serialize_comment(comment) for comment in pagination.items]
    else:
        # Sem busca, os comentários de primeiro nível vêm paginados com as respostas aninhadas.
        query = base.filter(IGComment.parent_id.is_(None))
        pagination = sorted_comments(query, sort).paginate(page=page, per_page=page_size, error_out=False)
        ids = [comment.comment_id for comment in pagination.items]
        replies = {}
        if ids:
            for reply in base.filter(IGComment.parent_id.in_(ids)).order_by(IGComment.created_at.asc()).all():
                replies.setdefault(reply.parent_id, []).append(serialize_comment(reply))
        items = [{**serialize_comment(comment), "replies": replies.get(comment.comment_id, [])} for comment in pagination.items]
    return jsonify({"items": items, "page": page, "page_size": page_size, "total": pagination.total,
                    "pages": pagination.pages, "threaded": not text})


@ig_api.get("/media/<media_id>/summary")
@require_auth
def summary(media_id):
    if not db.session.get(IGMedia, media_id):
        return jsonify({"error": "Post não encontrado"}), 404
    base = db.session.query(IGComment).filter(IGComment.media_id == media_id)
    total = base.count()
    replies = base.filter(IGComment.parent_id.isnot(None)).count()
    commenters = base.with_entities(func.count(func.distinct(IGComment.username))).scalar() or 0
    comment_likes = base.with_entities(func.coalesce(func.sum(IGComment.like_count), 0)).scalar() or 0
    top = (base.with_entities(IGComment.username, func.count(IGComment.comment_id).label("total"),
                              func.coalesce(func.sum(IGComment.like_count), 0))
           .filter(IGComment.username.isnot(None)).group_by(IGComment.username)
           .order_by(func.count(IGComment.comment_id).desc()).limit(10).all())
    day = func.date(IGComment.created_at)
    per_day = base.with_entities(day, func.count(IGComment.comment_id)).filter(IGComment.created_at.isnot(None)).group_by(day).order_by(day).all()
    most_liked = base.order_by(func.coalesce(IGComment.like_count, 0).desc()).limit(5).all()
    first = base.with_entities(func.min(IGComment.created_at), func.max(IGComment.created_at)).first()
    return jsonify({"total": total, "comments": total - replies, "replies": replies, "commenters": commenters,
                    "comment_likes": int(comment_likes),
                    "top_commenters": [{"username": name, "comments": count, "likes": int(likes)} for name, count, likes in top],
                    "per_day": [{"date": str(value), "comments": count} for value, count in per_day],
                    "most_liked": [serialize_comment(comment) for comment in most_liked if comment.like_count],
                    "first_comment_at": iso(first[0]) if first else None, "last_comment_at": iso(first[1]) if first else None})


def export_row(comment):
    return [comment.comment_id, comment.parent_id, "resposta" if comment.parent_id else "comentário", comment.username,
            comment.text, comment.created_at.strftime("%Y-%m-%d %H:%M:%S") if comment.created_at else None,
            comment.like_count, comment.hidden]


@ig_api.get("/media/<media_id>/export")
@require_auth
def export_comments(media_id):
    media = db.session.get(IGMedia, media_id)
    if not media:
        return jsonify({"error": "Post não encontrado"}), 404
    file_format = request.args.get("format", "csv").lower()
    if file_format not in ("csv", "xlsx"):
        return jsonify({"error": "format deve ser csv ou xlsx"}), 400
    comments = IGComment.query.filter_by(media_id=media_id).order_by(IGComment.created_at.asc()).all()
    basename = f"instagram-{media.shortcode or media_id}-comentarios"
    output = io.BytesIO()
    if file_format == "csv":
        text_stream = io.StringIO()
        # Ponto e vírgula: separador que o Excel em português espera ao abrir um CSV.
        writer = csv.writer(text_stream, delimiter=";")
        writer.writerow(EXPORT_FIELDS)
        writer.writerows(export_row(comment) for comment in comments)
        output.write(text_stream.getvalue().encode("utf-8-sig"))
        mime, filename = "text/csv", f"{basename}.csv"
    else:
        from openpyxl import Workbook
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Comentários"
        sheet.append(EXPORT_FIELDS)
        for comment in comments:
            sheet.append(export_row(comment))
        sheet.freeze_panes = "A2"
        workbook.save(output)
        mime, filename = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"{basename}.xlsx"
    output.seek(0)
    return send_file(output, mimetype=mime, as_attachment=True, download_name=filename)

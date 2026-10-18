"""Captura de comentários dos posts da conta do Instagram conectada.

A comunicação com a Graph API fica em ``app/ig_client.py``, que deve expor:

    class IGClient:
        def __init__(self, token, base, user_id): ...
        def find_media(self, shortcode) -> dict | None
            # mídia da conta conectada com esse shortcode (campos: id, shortcode, permalink, username,
            # caption, media_type, media_url, thumbnail_url, timestamp, like_count, comments_count)
        def iter_comments(self, media_id) -> Iterable[dict]
            # todos os comentários e respostas da mídia, já paginados (campos: id, text, timestamp,
            # username, from {id, username}, like_count, hidden, parent_id nas respostas)

Qualquer exceção levantada pelo cliente vira o erro exibido na captura.
"""
import re
from datetime import datetime, timezone

from .extensions import db
from .models import IGCapture, IGComment, IGMedia
from .services import record_usage

SHORTCODE_RE = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:p|reels?|tv)/([A-Za-z0-9_-]+)", re.IGNORECASE)
SAVE_BATCH = 200


def parse_shortcode(url):
    match = SHORTCODE_RE.search(url or "")
    return match.group(1) if match else None


def load_client(config):
    if not config["INSTAGRAM_ACCESS_TOKEN"]:
        raise RuntimeError("INSTAGRAM_ACCESS_TOKEN não está configurado")
    try:
        from .ig_client import IGClient
    except ImportError as exc:
        raise RuntimeError("Cliente da API do Instagram (backend/app/ig_client.py) não encontrado") from exc
    return IGClient(config["INSTAGRAM_ACCESS_TOKEN"], config["INSTAGRAM_API_BASE"], config["INSTAGRAM_USER_ID"])


def parse_time(value):
    if not value:
        return None
    # A Graph API devolve "2026-10-18T12:34:56+0000"; o fromisoformat só aceita o offset com dois-pontos.
    value = value.replace("Z", "+00:00")
    if len(value) > 5 and value[-5] in "+-" and value[-3] != ":":
        value = f"{value[:-2]}:{value[-2:]}"
    return datetime.fromisoformat(value)


def save_media(item):
    now = datetime.now(timezone.utc)
    media = db.session.get(IGMedia, str(item["id"])) or IGMedia(media_id=str(item["id"]), first_collected_at=now)
    media.shortcode = item.get("shortcode") or media.shortcode
    media.permalink = item.get("permalink") or media.permalink
    media.username = item.get("username") or media.username
    media.caption = item.get("caption")
    media.media_type = item.get("media_type")
    media.media_url = item.get("media_url") or media.media_url
    media.thumbnail_url = item.get("thumbnail_url") or media.thumbnail_url
    media.created_at = parse_time(item.get("timestamp")) or media.created_at
    media.like_count = item.get("like_count")
    media.comments_count = item.get("comments_count")
    media.last_updated_at = now
    media.raw_payload = item
    db.session.add(media)
    return media


def save_comment(media_id, item, capture_id):
    now = datetime.now(timezone.utc)
    comment = db.session.get(IGComment, str(item["id"])) or IGComment(comment_id=str(item["id"]), first_collected_at=now)
    author = item.get("from") or {}
    comment.media_id = media_id
    comment.parent_id = str(item["parent_id"]) if item.get("parent_id") else None
    comment.author_id = author.get("id") or comment.author_id
    comment.username = item.get("username") or author.get("username") or comment.username
    comment.text = item.get("text")
    comment.created_at = parse_time(item.get("timestamp")) or comment.created_at
    comment.like_count = item.get("like_count")
    comment.hidden = item.get("hidden")
    comment.source_capture_id = capture_id
    comment.last_updated_at = now
    comment.raw_payload = item
    db.session.add(comment)
    return comment


def run_capture(capture_id, client, max_comments):
    """Busca o post e todos os comentários; atualiza o progresso da captura a cada lote salvo."""
    capture = db.session.get(IGCapture, capture_id)
    item = client.find_media(capture.shortcode)
    if not item:
        raise RuntimeError("Post não encontrado na conta conectada. A API do Instagram só libera os comentários "
                           "de posts da própria conta.")
    media = save_media(item)
    capture.media_id = media.media_id
    db.session.commit()
    top_level = replies = 0
    truncated = False
    for index, raw in enumerate(client.iter_comments(media.media_id), start=1):
        if index > max_comments:
            truncated = True
            break
        comment = save_comment(media.media_id, raw, capture_id)
        if comment.parent_id:
            replies += 1
        else:
            top_level += 1
        if index % SAVE_BATCH == 0:
            capture.comments_found, capture.replies_found = top_level, replies
            db.session.commit()
    media.last_captured_at = datetime.now(timezone.utc)
    capture.comments_found, capture.replies_found = top_level, replies
    capture.status = "partial" if truncated else "done"
    capture.error = f"Limite de {max_comments} comentários por captura atingido." if truncated else None
    capture.finished_at = datetime.now(timezone.utc)
    record_usage("instagram_comments", top_level + replies, 0)
    db.session.commit()
    return top_level, replies


def iso(value):
    return value.isoformat() if value else None


def serialize_capture(capture):
    return {"id": capture.id, "source_url": capture.source_url, "shortcode": capture.shortcode, "media_id": capture.media_id,
            "status": capture.status, "comments_found": capture.comments_found, "replies_found": capture.replies_found,
            "error": capture.error, "created_at": iso(capture.created_at), "finished_at": iso(capture.finished_at)}


def serialize_media(media, stored=None):
    return {"media_id": media.media_id, "shortcode": media.shortcode, "permalink": media.permalink, "username": media.username,
            "caption": media.caption, "media_type": media.media_type, "media_url": media.media_url,
            "thumbnail_url": media.thumbnail_url, "created_at": iso(media.created_at), "like_count": media.like_count,
            "comments_count": media.comments_count, "stored_comments": stored, "last_captured_at": iso(media.last_captured_at)}


def serialize_comment(comment):
    return {"comment_id": comment.comment_id, "parent_id": comment.parent_id, "username": comment.username,
            "text": comment.text, "created_at": iso(comment.created_at), "like_count": comment.like_count,
            "hidden": comment.hidden}

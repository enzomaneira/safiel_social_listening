from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from .extensions import db
from .models import ApiUsage, XAuthor, XPost
from .normalizer import normalize


def month_spend():
    """Custo estimado acumulado no mês corrente (UTC)."""
    first_day = datetime.now(timezone.utc).date().replace(day=1)
    return float(db.session.query(func.coalesce(func.sum(ApiUsage.est_cost_usd), 0)).filter(ApiUsage.date >= first_day).scalar())


def upsert_posts(tweets, includes=None, rule_id=None, capture_id=None, endpoint="unknown", price=0.005, user_price=0.0):
    includes = includes or {}
    users = {user["id"]: user for user in includes.get("users", [])}
    authors_to_store = dict(users)
    for tweet in tweets:
        author_id = tweet.get("author_id")
        if author_id and author_id not in authors_to_store:
            authors_to_store[author_id] = {"id": author_id}
    for user in authors_to_store.values():
        metrics = user.get("public_metrics") or {}
        existing = db.session.get(XAuthor, user["id"])
        values = {"author_id": user["id"], "username": user.get("username"), "name": user.get("name"),
                  "verified": user.get("verified"), "followers_count": metrics.get("followers_count"),
                  "last_updated_at": datetime.now(timezone.utc)}
        if existing:
            for key, value in values.items():
                if key != "author_id" and value is not None:
                    setattr(existing, key, value)
        else:
            db.session.add(XAuthor(**values))
    db.session.flush()
    rows = [normalize(tweet, users, rule_id, capture_id) for tweet in tweets]
    ids = [row["post_id"] for row in rows]
    existing_ids = set(db.session.query(XPost.post_id).filter(XPost.post_id.in_(ids)).all()) if ids else set()
    existing_ids = {post_id for (post_id,) in existing_ids}
    if rows:
        statement = insert(XPost).values(rows)
        update_columns = ("conversation_id", "parent_post_id", "author_id", "text", "created_at", "language", "post_url",
                          "post_type", "is_reply", "is_quote", "is_repost", "quoted_post_id", "in_reply_to_post_id",
                          "like_count", "reply_count", "repost_count", "quote_count", "bookmark_count", "impression_count",
                          "hashtags", "mentions", "urls", "last_updated_at", "raw_payload")
        statement = statement.on_conflict_do_update(index_elements=["post_id"],
                    set_={column: getattr(statement.excluded, column) for column in update_columns})
        db.session.execute(statement)
    now = datetime.now(timezone.utc).date()
    usage = db.session.get(ApiUsage, (now, endpoint))
    n = len(tweets)
    # A X API cobra por recurso retornado: posts e também os usuários expandidos em includes.
    cost = n * price + len(users) * user_price
    if usage:
        usage.requests += 1
        usage.resources_read += n
        usage.est_cost_usd = float(usage.est_cost_usd or 0) + cost
    else:
        db.session.add(ApiUsage(date=now, endpoint=endpoint, requests=1, resources_read=n, est_cost_usd=cost))
    db.session.commit()
    return len(ids) - len(existing_ids), len(existing_ids)

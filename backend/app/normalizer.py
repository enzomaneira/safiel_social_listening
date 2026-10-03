from datetime import datetime, timezone


def normalize(tweet, users_by_id=None, rule_id=None, capture_id=None):
    users_by_id = users_by_id or {}
    metrics = tweet.get("public_metrics") or {}
    entities = tweet.get("entities") or {}
    refs = tweet.get("referenced_tweets") or []
    ref_by_type = {ref.get("type"): ref.get("id") for ref in refs}
    post_type = "reply" if "replied_to" in ref_by_type else "quote" if "quoted" in ref_by_type else "repost" if "retweeted" in ref_by_type else "original"
    author = users_by_id.get(tweet.get("author_id"), {})
    created_at = tweet.get("created_at")
    if created_at:
        created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    post_id = str(tweet["id"])
    username = author.get("username") or tweet.get("author_id") or "i"
    return {
        "post_id": post_id, "conversation_id": tweet.get("conversation_id"),
        "parent_post_id": ref_by_type.get("replied_to"), "author_id": tweet.get("author_id"),
        "text": tweet.get("text"), "created_at": created_at, "language": tweet.get("lang"),
        "post_url": f"https://x.com/{username}/status/{post_id}", "post_type": post_type,
        "is_reply": post_type == "reply", "is_quote": post_type == "quote", "is_repost": post_type == "repost",
        "quoted_post_id": ref_by_type.get("quoted"), "in_reply_to_post_id": ref_by_type.get("replied_to"),
        "like_count": metrics.get("like_count"), "reply_count": metrics.get("reply_count"),
        "repost_count": metrics.get("retweet_count"), "quote_count": metrics.get("quote_count"),
        "bookmark_count": metrics.get("bookmark_count"), "impression_count": metrics.get("impression_count"),
        "hashtags": [item.get("tag") for item in entities.get("hashtags", [])],
        "mentions": [item.get("username") for item in entities.get("mentions", [])],
        "urls": [item.get("url") for item in entities.get("urls", [])],
        "source_rule_id": rule_id, "source_capture_id": capture_id,
        "last_updated_at": datetime.now(timezone.utc), "raw_payload": tweet,
    }

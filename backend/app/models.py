from datetime import datetime, timezone

from .extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class XAuthor(db.Model):
    __tablename__ = "x_authors"
    author_id = db.Column(db.String, primary_key=True)
    username = db.Column(db.String)
    name = db.Column(db.String)
    verified = db.Column(db.Boolean)
    followers_count = db.Column(db.Integer)
    is_relevant = db.Column(db.Boolean, nullable=False, default=False)
    last_updated_at = db.Column(db.DateTime(timezone=True), default=utcnow)


class MonitoringRule(db.Model):
    __tablename__ = "monitoring_rules"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, nullable=False)
    query = db.Column(db.Text, nullable=False)
    grp = db.Column(db.String)
    priority = db.Column(db.Integer, default=2)
    interval_minutes = db.Column(db.Integer, default=30)
    max_results_per_run = db.Column(db.Integer, default=500)
    status = db.Column(db.String, default="active")
    since_id = db.Column(db.String)
    last_run_at = db.Column(db.DateTime(timezone=True))
    last_error = db.Column(db.Text)


class Capture(db.Model):
    __tablename__ = "captures"
    id = db.Column(db.Integer, primary_key=True)
    source_url = db.Column(db.Text)
    root_post_id = db.Column(db.String)
    status = db.Column(db.String, default="queued")
    replies_found = db.Column(db.Integer, default=0)
    quotes_found = db.Column(db.Integer, default=0)
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    finished_at = db.Column(db.DateTime(timezone=True))


class XPost(db.Model):
    __tablename__ = "x_posts"
    post_id = db.Column(db.String, primary_key=True)
    conversation_id = db.Column(db.String, index=True)
    parent_post_id = db.Column(db.String)
    author_id = db.Column(db.String, db.ForeignKey("x_authors.author_id"), index=True)
    text = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), index=True)
    language = db.Column(db.String)
    post_url = db.Column(db.Text)
    post_type = db.Column(db.String)
    is_reply = db.Column(db.Boolean, default=False)
    is_quote = db.Column(db.Boolean, default=False)
    is_repost = db.Column(db.Boolean, default=False)
    quoted_post_id = db.Column(db.String)
    in_reply_to_post_id = db.Column(db.String)
    like_count = db.Column(db.Integer)
    reply_count = db.Column(db.Integer)
    repost_count = db.Column(db.Integer)
    quote_count = db.Column(db.Integer)
    bookmark_count = db.Column(db.Integer)
    impression_count = db.Column(db.Integer)
    hashtags = db.Column(db.JSON)
    mentions = db.Column(db.JSON)
    urls = db.Column(db.JSON)
    source_rule_id = db.Column(db.Integer, db.ForeignKey("monitoring_rules.id"), index=True)
    source_capture_id = db.Column(db.Integer, db.ForeignKey("captures.id"))
    is_relevant = db.Column(db.Boolean, default=False, nullable=False)
    first_collected_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    last_updated_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    raw_payload = db.Column(db.JSON)


class XMetricSnapshot(db.Model):
    __tablename__ = "x_metric_snapshots"
    id = db.Column(db.BigInteger, primary_key=True)
    post_id = db.Column(db.String, db.ForeignKey("x_posts.post_id"), index=True)
    captured_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    like_count = db.Column(db.Integer)
    reply_count = db.Column(db.Integer)
    repost_count = db.Column(db.Integer)
    quote_count = db.Column(db.Integer)
    impression_count = db.Column(db.Integer)


class ApiUsage(db.Model):
    __tablename__ = "api_usage"
    date = db.Column(db.Date, primary_key=True)
    endpoint = db.Column(db.String, primary_key=True)
    requests = db.Column(db.Integer, default=0)
    resources_read = db.Column(db.Integer, default=0)
    est_cost_usd = db.Column(db.Numeric(10, 4), default=0)


class SystemLog(db.Model):
    __tablename__ = "system_logs"
    id = db.Column(db.BigInteger, primary_key=True)
    ts = db.Column(db.DateTime(timezone=True), default=utcnow, index=True)
    level = db.Column(db.String)
    source = db.Column(db.String)
    message = db.Column(db.Text)
    context = db.Column(db.JSON)


class AIClassification(db.Model):
    __tablename__ = "ai_classifications"
    post_id = db.Column(db.String, db.ForeignKey("x_posts.post_id"), primary_key=True)
    sentiment = db.Column(db.String)
    stance = db.Column(db.String)
    topic = db.Column(db.String)
    target = db.Column(db.String)
    narrative = db.Column(db.Text)
    irony = db.Column(db.Boolean)
    relevance_score = db.Column(db.Integer)
    classified_at = db.Column(db.DateTime(timezone=True))
    model = db.Column(db.String)

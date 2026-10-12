import time

import requests


class XAPIError(RuntimeError):
    pass


class XClient:
    BASE = "https://api.x.com/2"
    FIELDS = {
        "tweet.fields": "created_at,conversation_id,author_id,public_metrics,referenced_tweets,in_reply_to_user_id,lang,entities,text",
        "expansions": "author_id",
        "user.fields": "username,name,verified,public_metrics",
    }

    def __init__(self, token, max_attempts=4, timeout=20):
        self.max_attempts, self.timeout = max_attempts, timeout
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def get(self, path, params=None):
        last = self.max_attempts - 1
        for attempt in range(self.max_attempts):
            try:
                response = self.session.get(f"{self.BASE}{path}", params=params, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == last:
                    raise XAPIError("Falha de conexão com a API do X") from exc
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 429:
                if attempt == last:
                    raise XAPIError("Limite de requisições da API do X atingido")
                try:
                    wait = int(response.headers.get("retry-after", 2 ** attempt * 5))
                except ValueError:
                    wait = 2 ** attempt * 5
                time.sleep(min(wait, 60))
                continue
            if response.status_code in (401, 403):
                raise XAPIError(f"Credencial ou permissão da API inválida ({response.status_code}): {response.text[:300]}")
            if not response.ok:
                raise XAPIError(f"Erro da API do X ({response.status_code}): {response.text[:500]}")
            return response.json()
        raise XAPIError("Tentativas esgotadas")

    def search_recent(self, query, since_id=None, next_token=None, max_results=100):
        params = {**self.FIELDS, "query": query, "max_results": max(10, min(max_results, 100))}
        if since_id:
            params["since_id"] = since_id
        if next_token:
            params["next_token"] = next_token
        return self.get("/tweets/search/recent", params)

    def get_post(self, post_id):
        return self.get(f"/tweets/{post_id}", self.FIELDS)

    def lookup_posts(self, post_ids):
        """Busca até 100 posts por ID (só métricas públicas)."""
        return self.get("/tweets", {"ids": ",".join(post_ids), "tweet.fields": "public_metrics"})

    def get_usage(self, days=30):
        params = {"days": days, "usage.fields": "cap_reset_day,daily_project_usage,project_cap,project_usage"}
        return self.get("/usage/tweets", params)

    def get_quotes(self, post_id, pagination_token=None):
        params = {**self.FIELDS, "max_results": 100}
        if pagination_token:
            params["pagination_token"] = pagination_token
        return self.get(f"/tweets/{post_id}/quote_tweets", params)

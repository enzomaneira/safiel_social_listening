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

    def __init__(self, token):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def get(self, path, params=None):
        for attempt in range(4):
            try:
                response = self.session.get(f"{self.BASE}{path}", params=params, timeout=20)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == 3:
                    raise XAPIError("Falha de conexão com a API do X") from exc
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 429:
                if attempt == 3:
                    raise XAPIError("Limite de requisições da API do X atingido")
                try:
                    wait = int(response.headers.get("retry-after", 2 ** attempt * 5))
                except ValueError:
                    wait = 2 ** attempt * 5
                time.sleep(min(wait, 60))
                continue
            if response.status_code in (401, 403):
                raise XAPIError(f"Credencial ou permissão da API inválida ({response.status_code})")
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

    def get_quotes(self, post_id, pagination_token=None):
        params = {**self.FIELDS, "max_results": 100}
        if pagination_token:
            params["pagination_token"] = pagination_token
        return self.get(f"/tweets/{post_id}/quote_tweets", params)

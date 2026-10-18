# backend/app/ig_client.py

from __future__ import annotations

import random
import time
from typing import Dict, Generator, Iterable, Optional

import requests


DEFAULT_TIMEOUT = 20
DEFAULT_SCAN_LIMIT = 1000

MEDIA_FIELDS = ",".join(
    [
        "id",
        "shortcode",
        "permalink",
        "username",
        "caption",
        "media_type",
        "media_url",
        "thumbnail_url",
        "timestamp",
        "like_count",
        "comments_count",
    ]
)

COMMENT_FIELDS_FULL = ",".join(
    [
        "id",
        "text",
        "timestamp",
        "username",
        "from",
        "like_count",
        "hidden",
    ]
)

COMMENT_FIELDS_FALLBACK = ",".join(
    [
        "id",
        "text",
        "timestamp",
        "username",
        "like_count",
    ]
)

RATE_LIMIT_CODES = {4, 17, 32, 613}


class IGClient:
    def __init__(self, token: str, base: str, user_id: str, scan_limit: int = DEFAULT_SCAN_LIMIT):
        self.token = token
        self.base = base.rstrip("/")
        self.user_id = user_id
        self.scan_limit = scan_limit

    # =========================================================
    # Public API
    # =========================================================

    def find_media(self, shortcode: str) -> Optional[dict]:
        limit = self.scan_limit

        url = f"{self.base}/{self.user_id}/media"

        params = {
            "fields": MEDIA_FIELDS,
            "limit": 50,
        }

        scanned = 0

        while url and scanned < limit:
            data = self._get(url, params=params)

            for media in data.get("data", []):
                scanned += 1

                media_shortcode = media.get("shortcode")
                permalink = media.get("permalink", "")

                if (
                    media_shortcode == shortcode
                    or f"/{shortcode}/" in permalink
                ):
                    return media

                if scanned >= limit:
                    break

            url = self._paging_next(data)
            params = None

        return None

    def iter_comments(self, media_id: str) -> Iterable[dict]:
        url = f"{self.base}/{media_id}/comments"

        yield from self._iter_comments_with_fields(
            url=url,
            fields=COMMENT_FIELDS_FULL,
            fallback=True,
        )

    # =========================================================
    # Internal iterators
    # =========================================================

    def _iter_comments_with_fields(
        self,
        url: str,
        fields: str,
        fallback: bool,
    ) -> Generator[dict, None, None]:
        # As respostas vêm junto com cada comentário (expansão de campos); /replies só é
        # chamado quando um comentário tem mais respostas do que cabem nessa primeira página.
        params = {
            "fields": f"{fields},replies.limit(50){{{fields}}}",
            "limit": 50,
        }

        try:
            while url:
                data = self._get(url, params=params)

                for comment in data.get("data", []):
                    replies = comment.pop("replies", None) or {}
                    yield comment

                    comment_id = comment.get("id")
                    if not comment_id:
                        continue

                    for reply in replies.get("data", []):
                        reply["parent_id"] = comment_id
                        yield reply

                    next_replies = self._paging_next(replies)
                    if next_replies:
                        yield from self._iter_replies(
                            comment_id=comment_id,
                            parent_id=comment_id,
                            fields=fields,
                            url=next_replies,
                        )

                url = self._paging_next(data)
                params = None

        except Exception as exc:
            if (
                fallback
                and self._is_field_error(str(exc))
                and fields != COMMENT_FIELDS_FALLBACK
            ):
                yield from self._iter_comments_with_fields(
                    url=url,
                    fields=COMMENT_FIELDS_FALLBACK,
                    fallback=False,
                )
                return

            raise

    def _iter_replies(
        self,
        comment_id: str,
        parent_id: str,
        fields: str,
        url: Optional[str] = None,
    ) -> Generator[dict, None, None]:
        # Com url (o paging.next das respostas), os campos e o cursor já vêm na própria URL.
        params = None if url else {
            "fields": fields,
            "limit": 50,
        }
        url = url or f"{self.base}/{comment_id}/replies"

        try:
            while url:
                data = self._get(url, params=params)

                for reply in data.get("data", []):
                    reply["parent_id"] = parent_id
                    yield reply

                url = self._paging_next(data)
                params = None

        except Exception as exc:
            if (
                fields != COMMENT_FIELDS_FALLBACK
                and self._is_field_error(str(exc))
            ):
                yield from self._iter_replies(
                    comment_id=comment_id,
                    parent_id=parent_id,
                    fields=COMMENT_FIELDS_FALLBACK,
                )
                return

            raise

    # =========================================================
    # HTTP
    # =========================================================

    def _get(self, url: str, params: Optional[Dict] = None) -> dict:
        retries = 5
        backoff = 2

        for attempt in range(retries):
            try:
                final_params = dict(params or {})

                # As URLs de paging.next já trazem o token.
                if "access_token" not in final_params and "access_token=" not in url:
                    final_params["access_token"] = self.token

                response = requests.get(
                    url,
                    params=final_params,
                    timeout=DEFAULT_TIMEOUT,
                )

                if response.status_code == 429:
                    if attempt == retries - 1:
                        raise Exception(
                            "Limite de requisições da API do Instagram atingido."
                        )

                    self._sleep(attempt, backoff)
                    continue

                data = response.json()

                if response.status_code >= 400 or "error" in data:
                    error = data.get("error", {})

                    code = error.get("code")
                    message = error.get("message", "Erro desconhecido")
                    error_type = error.get("type", "")

                    if code in RATE_LIMIT_CODES:
                        if attempt == retries - 1:
                            raise Exception(
                                "Limite de requisições da API do Instagram atingido."
                            )

                        self._sleep(attempt, backoff)
                        continue

                    raise self._build_api_error(
                        code=code,
                        message=message,
                        error_type=error_type,
                    )

                return data

            # "from None" nos três casos abaixo: a exceção original do requests traz a URL com o token.
            except requests.Timeout:
                if attempt == retries - 1:
                    raise Exception(
                        "Tempo limite excedido ao acessar a API do Instagram."
                    ) from None

                self._sleep(attempt, backoff)

            except requests.ConnectionError:
                if attempt == retries - 1:
                    raise Exception(
                        "Falha de rede ao acessar a API do Instagram."
                    ) from None

                self._sleep(attempt, backoff)

            except requests.RequestException:
                if attempt == retries - 1:
                    raise Exception(
                        "Erro ao acessar a API do Instagram."
                    ) from None

                self._sleep(attempt, backoff)

        raise Exception("Falha ao acessar a API do Instagram.")

    # =========================================================
    # Helpers
    # =========================================================

    def _paging_next(self, data: dict) -> Optional[str]:
        paging = data.get("paging") or {}
        next_url = paging.get("next")

        if not next_url:
            return None

        return next_url

    def _sleep(self, attempt: int, base: int) -> None:
        seconds = min(60, (base ** attempt) + random.uniform(0, 1))
        time.sleep(seconds)

    def _is_field_error(self, message: str) -> bool:
        msg = (message or "").lower()

        return (
            "nonexisting field" in msg
            or "tried accessing nonexisting field" in msg
            or "campo" in msg
        )

    def _build_api_error(
        self,
        code: Optional[int],
        message: str,
        error_type: str,
    ) -> Exception:
        msg = (message or "").lower()

        if code == 190:
            return Exception(
                "Token do Instagram inválido ou expirado."
            )

        # Antes do teste de permissão: para um ID inexistente o Meta responde
        # "Unsupported get request... missing permissions".
        if "unsupported get request" in msg:
            return Exception(
                "Post não encontrado na conta conectada."
            )

        if "permission" in msg or "permissions" in msg:
            return Exception(
                "O token não possui permissões suficientes para acessar comentários do Instagram."
            )

        if "media" in msg and "not found" in msg:
            return Exception(
                "Post não encontrado na conta conectada."
            )

        clean_message = self._sanitize(message)

        return Exception(
            f"Erro da API do Instagram: {clean_message}"
        )

    def _sanitize(self, text: str) -> str:
        if not text:
            return ""

        sanitized = text.replace(self.token, "[TOKEN_REMOVIDO]")

        return sanitized

"""Авторизация Oura (OAuth2) — доступ к данным кольца.

Сделано по образцу google_auth.py: тот же поток, та же таблица
oauth_tokens, ключ строки — 'oura'.

Авторизация проходит один раз через браузер: открываешь /oura/auth у
своего бота, жмёшь «разрешить» — токен сохраняется и дальше обновляется
сам.

Почему OAuth2, а не простой токен: Oura закрыла личные токены (Personal
Access Tokens) в декабре 2025, новые не выдаёт. Остался только этот путь.
"""

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from assistant import config
from assistant.db import supabase

logger = logging.getLogger(__name__)

AUTH_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"

# Ключ строки в oauth_tokens — рядом с 'google' и 'google_personal'.
KEY = "oura"


def get_auth_url() -> str:
    """Ссылка, по которой Катя разрешает доступ к своим данным кольца."""
    params = {
        "client_id": config.OURA_CLIENT_ID,
        "redirect_uri": config.OURA_REDIRECT_URI,
        "response_type": "code",
        "scope": config.OURA_SCOPES,
        "state": KEY,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str) -> None:
    """Обменять код (из callback) на токены и сохранить их."""
    resp = httpx.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.OURA_REDIRECT_URI,
        "client_id": config.OURA_CLIENT_ID,
        "client_secret": config.OURA_CLIENT_SECRET,
    }, timeout=15)
    resp.raise_for_status()
    tokens = resp.json()
    _store(tokens["access_token"], tokens.get("refresh_token", ""),
           tokens.get("expires_in", 86400))


def _store(access_token: str, refresh_token: str, expires_in: int) -> None:
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    data = {"key": KEY, "access_token": access_token, "expires_at": expires_at}
    if refresh_token:
        data["refresh_token"] = refresh_token
    supabase.table("oauth_tokens").upsert(data, on_conflict="key").execute()


def _refresh(refresh_token: str) -> str | None:
    try:
        resp = httpx.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": config.OURA_CLIENT_ID,
            "client_secret": config.OURA_CLIENT_SECRET,
        }, timeout=15)
        resp.raise_for_status()
        tokens = resp.json()
        _store(tokens["access_token"], tokens.get("refresh_token", refresh_token),
               tokens.get("expires_in", 86400))
        return tokens["access_token"]
    except Exception:
        logger.exception("не удалось обновить токен Oura")
        return None


def is_authorized() -> bool:
    """Подключено ли кольцо вообще."""
    try:
        rows = supabase.table("oauth_tokens").select("key").eq("key", KEY).limit(1).execute().data
        return bool(rows)
    except Exception:
        logger.exception("не смогла проверить авторизацию Oura")
        return False


def get_access_token() -> str | None:
    """Действующий access token (обновляет сам, если истёк). None — если не авторизован."""
    rows = supabase.table("oauth_tokens").select("*").eq("key", KEY).limit(1).execute().data
    if not rows:
        return None
    tok = rows[0]
    expires_at = str(tok.get("expires_at", ""))
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= exp:
                return _refresh(tok["refresh_token"])
        except (ValueError, KeyError):
            pass
    return tok.get("access_token")

"""Общая авторизация Google (OAuth2) — её используют и Gmail, и Calendar.

Токены хранятся в таблице oauth_tokens (есть в schema.sql). Авторизация
проходит один раз через браузер: открываешь /google/auth у своего бота,
жмёшь «разрешить» — токен сохраняется и дальше обновляется сам.

Подробно — docs/04-add-gmail.md (там же общий шаг авторизации).
"""

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from assistant import config
from assistant.db import supabase

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# Какие права просим — зависит от включённых модулей.
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GCAL_SCOPE = "https://www.googleapis.com/auth/calendar.events"
# Чтобы видеть ВСЕ календари (личный + рабочий), а не только основной,
# нужно право прочитать их перечень. Только перечень — на чтение.
GCAL_LIST_SCOPE = "https://www.googleapis.com/auth/calendar.calendarlist.readonly"


# Можно подключить два гугл-аккаунта: рабочий и личный. Почтовый ящик, в
# отличие от календаря, расшарить нельзя — поэтому для второго ящика нужен
# отдельный токен. Рабочий — ключ по умолчанию, его трогать нельзя: на нём
# уже висит календарь.
WORK = "google"
PERSONAL = "google_personal"
ACCOUNTS = (WORK, PERSONAL)


def _scopes() -> list[str]:
    scopes = []
    if config.ENABLE_GMAIL:
        scopes.append(GMAIL_SCOPE)
    if config.ENABLE_GCAL:
        scopes.append(GCAL_SCOPE)
        scopes.append(GCAL_LIST_SCOPE)
    return scopes


def get_auth_url(account: str = WORK) -> str:
    """Ссылка, по которой пользователь разрешает доступ (открывается в браузере).

    account уезжает в Google как state и возвращается обратно в callback —
    так мы понимаем, какому из двух ящиков принадлежит выданный токен.
    """
    params = {
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": config.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(_scopes()),
        "access_type": "offline",      # чтобы получить refresh_token
        "prompt": "consent",
        "state": account,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str, account: str = WORK) -> None:
    """Обменять код (из callback) на токены и сохранить их."""
    resp = httpx.post(TOKEN_URL, data={
        "code": code,
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "redirect_uri": config.GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=15)
    resp.raise_for_status()
    tokens = resp.json()
    _store(tokens["access_token"], tokens.get("refresh_token", ""),
           tokens.get("expires_in", 3600), account)


def _store(access_token: str, refresh_token: str, expires_in: int,
           account: str = WORK) -> None:
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    data = {"key": account, "access_token": access_token, "expires_at": expires_at}
    if refresh_token:  # Google не всегда возвращает refresh_token при обновлении
        data["refresh_token"] = refresh_token
    supabase.table("oauth_tokens").upsert(data, on_conflict="key").execute()


def _refresh(refresh_token: str, account: str = WORK) -> str | None:
    try:
        resp = httpx.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
        }, timeout=15)
        resp.raise_for_status()
        tokens = resp.json()
        _store(tokens["access_token"], tokens.get("refresh_token", refresh_token),
               tokens.get("expires_in", 3600), account)
        return tokens["access_token"]
    except Exception:
        logger.exception("не удалось обновить токен Google (%s)", account)
        return None


def authorized_accounts() -> list[str]:
    """Какие из аккаунтов реально подключены. Пустой список — ни одного."""
    try:
        rows = supabase.table("oauth_tokens").select("key").in_("key", list(ACCOUNTS)).execute().data
        found = {r.get("key") for r in rows}
        return [a for a in ACCOUNTS if a in found]  # порядок: сначала рабочий
    except Exception:
        logger.exception("не удалось прочитать список аккаунтов")
        return [WORK]


def get_access_token(account: str = WORK) -> str | None:
    """Действующий access token (обновляет сам, если истёк). None — если не авторизован."""
    rows = supabase.table("oauth_tokens").select("*").eq("key", account).limit(1).execute().data
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
                return _refresh(tok["refresh_token"], account)
        except (ValueError, KeyError):
            pass
    return tok.get("access_token")

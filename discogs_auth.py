"""Bounded Discogs identity validation with distinct, credential-safe errors."""
import requests


class DiscogsAuthError(RuntimeError):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def _missing_file(exc):
    pending, seen = [exc], set()
    while pending:
        item = pending.pop()
        if not isinstance(item, BaseException) or id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, FileNotFoundError):
            return True
        pending.extend(item.args)
        pending.extend((item.__cause__, item.__context__, getattr(item, "reason", None)))
    return False


def validate_token(token, user_agent="FlipsideFlow/1.0"):
    token = token.strip()
    if not token:
        raise DiscogsAuthError("Enter your Discogs personal access token.", 400)
    try:
        response = requests.get("https://api.discogs.com/oauth/identity",
                                headers={"Authorization": "Discogs token=" + token,
                                         "User-Agent": user_agent or "FlipsideFlow/1.0"},
                                timeout=(5, 10), allow_redirects=False)
    except requests.exceptions.SSLError:
        raise DiscogsAuthError("Could not establish a secure connection to Discogs. Check the Mac's date and network, and use the latest FlipsideFlow build. Your token was not validated.", 503) from None
    except requests.exceptions.Timeout:
        raise DiscogsAuthError("Discogs timed out. Check your connection and try again; your token was not validated.", 504) from None
    except (requests.exceptions.RequestException, OSError) as exc:
        if _missing_file(exc):
            raise DiscogsAuthError("FlipsideFlow is missing a local HTTPS support file. Install the latest app build and restart it. This is an app error, not an invalid token.", 503) from None
        raise DiscogsAuthError("Could not connect to Discogs. Check your internet connection or proxy and try again; your token was not validated.", 503) from None
    if response.status_code == 401:
        raise DiscogsAuthError("Discogs rejected this token. Copy a personal access token from Discogs Settings → Developers and try again.", 400)
    if response.status_code == 403:
        raise DiscogsAuthError("Discogs denied access. Check your token/account and whether your network can access the Discogs API.", 403)
    if response.status_code == 429:
        raise DiscogsAuthError("Discogs request limit reached. Wait a minute and try again.", 429)
    if response.status_code != 200:
        raise DiscogsAuthError("Discogs is temporarily unavailable. Try again later; your token was not validated.", 502)
    try:
        data = response.json()
        username = data.get("username") if isinstance(data, dict) else None
        if not isinstance(username, str) or not username:
            raise ValueError()
    except ValueError:
        raise DiscogsAuthError("Discogs returned an unexpected response. Try again later; your token was not validated.", 502) from None
    return username

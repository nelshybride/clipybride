"""Publication automatique des clips (YouTube, TikTok).

Tout reste sur la machine : les identifiants d'application et les jetons OAuth
vivent dans `backend/data/publish.json` et ne sont jamais renvoyés au navigateur.
Aucune bibliothèque externe : `urllib` suffit pour OAuth et l'envoi des vidéos.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DATA = Path(__file__).resolve().parent / "data"
OUTPUTS = DATA / "outputs"
CONFIG = DATA / "publish.json"

# Points d'entrée officiels (surchargeables pour les tests).
YOUTUBE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
YOUTUBE_TOKEN = "https://oauth2.googleapis.com/token"
YOUTUBE_UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.upload"

TIKTOK_AUTH = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_INIT = "https://open.tiktokapis.com/v2/post/publish/video/init/"
TIKTOK_SCOPE = "user.info.basic,video.publish"

PROVIDERS = ("youtube", "tiktok")
LABELS = {"youtube": "YouTube", "tiktok": "TikTok"}
ENV_KEYS = {
    "youtube": ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET"),
    "tiktok": ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"),
}

CFG_LOCK = threading.Lock()
PENDING: dict[str, str] = {}          # state OAuth -> fournisseur
PUBLISH: dict[str, dict] = {}          # job_id -> état de publication
PUBLISH_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Configuration locale
# --------------------------------------------------------------------------- #

def _load() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def _provider_cfg(provider: str) -> dict:
    return _load().get("providers", {}).get(provider, {})


def credentials(provider: str) -> tuple[str, str]:
    """Client id/secret : variables d'environnement d'abord, puis fichier local."""
    key_id, key_secret = ENV_KEYS[provider]
    cid = os.environ.get(key_id, "").strip()
    secret = os.environ.get(key_secret, "").strip()
    if cid and secret:
        return cid, secret
    data = _provider_cfg(provider)
    return (data.get("client_id") or "").strip(), (data.get("client_secret") or "").strip()


def set_credentials(provider: str, client_id: str, client_secret: str) -> None:
    with CFG_LOCK:
        cfg = _load()
        entry = cfg.setdefault("providers", {}).setdefault(provider, {})
        entry["client_id"] = client_id.strip()
        entry["client_secret"] = client_secret.strip()
        _save(cfg)


def clear_credentials(provider: str) -> None:
    with CFG_LOCK:
        cfg = _load()
        cfg.setdefault("providers", {}).pop(provider, None)
        _save(cfg)
    with PUBLISH_LOCK:
        PENDING.clear()


def _store_token(provider: str, payload: dict) -> None:
    with CFG_LOCK:
        cfg = _load()
        entry = cfg.setdefault("providers", {}).setdefault(provider, {})
        previous = entry.get("token") or {}
        entry["token"] = {
            "access_token": payload.get("access_token"),
            "refresh_token": payload.get("refresh_token") or previous.get("refresh_token"),
            "expires_at": time.time() + float(payload.get("expires_in") or 3600) - 60,
            "open_id": payload.get("open_id") or previous.get("open_id"),
        }
        _save(cfg)


def _token_data(provider: str) -> dict:
    return _provider_cfg(provider).get("token") or {}


def is_connected(provider: str) -> bool:
    data = _token_data(provider)
    return bool(data.get("access_token") or data.get("refresh_token"))


def status() -> dict:
    providers = {}
    for provider in PROVIDERS:
        cid, secret = credentials(provider)
        providers[provider] = {
            "label": LABELS[provider],
            "configured": bool(cid and secret),
            "connected": is_connected(provider),
        }
    return {"providers": providers}


# --------------------------------------------------------------------------- #
# OAuth (redirection loopback, adaptée à une app de bureau)
# --------------------------------------------------------------------------- #

def build_auth_url(provider: str, redirect_uri: str) -> str:
    cid, secret = credentials(provider)
    if not (cid and secret):
        raise RuntimeError(f"{LABELS[provider]} n'est pas configuré : ajoute le Client ID "
                           "et le Client Secret de ton application.")
    state = uuid.uuid4().hex
    PENDING[state] = provider
    if provider == "youtube":
        params = {
            "client_id": cid,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": YOUTUBE_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return f"{YOUTUBE_AUTH}?{urlencode(params)}"
    params = {
        "client_key": cid,
        "scope": TIKTOK_SCOPE,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{TIKTOK_AUTH}?{urlencode(params)}"


def finish_auth(code: str, state: str, redirect_uri: str) -> str:
    provider = PENDING.pop(state, None)
    if not provider:
        raise RuntimeError("Session d'autorisation inconnue ou expirée.")
    cid, secret = credentials(provider)
    if provider == "youtube":
        payload = _post_form(YOUTUBE_TOKEN, {
            "code": code,
            "client_id": cid,
            "client_secret": secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
    else:
        payload = _post_form(TIKTOK_TOKEN, {
            "code": code,
            "client_key": cid,
            "client_secret": secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        })
    if not payload.get("access_token"):
        raise RuntimeError(f"{LABELS[provider]} n'a pas renvoyé de jeton d'accès.")
    _store_token(provider, payload)
    return provider


def _refresh(provider: str) -> str:
    data = _token_data(provider)
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(f"{LABELS[provider]} doit être reconnecté : le jeton a expiré.")
    cid, secret = credentials(provider)
    if provider == "youtube":
        payload = _post_form(YOUTUBE_TOKEN, {
            "client_id": cid,
            "client_secret": secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })
    else:
        payload = _post_form(TIKTOK_TOKEN, {
            "client_key": cid,
            "client_secret": secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })
    if not payload.get("access_token"):
        raise RuntimeError(f"Jeton {LABELS[provider]} refusé ({payload.get('error') or 'inconnu'}).")
    payload.setdefault("refresh_token", refresh_token)
    _store_token(provider, payload)
    return payload["access_token"]


def access_token(provider: str) -> str:
    data = _token_data(provider)
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"{LABELS[provider]} n'est pas connecté.")
    if float(data.get("expires_at") or 0) > time.time() + 30:
        return token
    return _refresh(provider)


# --------------------------------------------------------------------------- #
# HTTP minimal
# --------------------------------------------------------------------------- #

def _json_response(request: Request, timeout: float) -> tuple[int, dict, dict]:
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            try:
                payload = json.loads(body or "{}")
            except Exception:
                payload = {"raw": body}
            return response.status, payload, dict(response.headers)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(body).get("error_description") or json.loads(body).get("error") or body
        except Exception:
            detail = body
        raise RuntimeError(f"HTTP {exc.code} : {str(detail)[:300]}")
    except URLError as exc:
        raise RuntimeError(f"Serveur injoignable : {exc.reason}")


def _post_form(url: str, fields: dict) -> dict:
    request = Request(url, data=urlencode(fields).encode(),
                      headers={"Content-Type": "application/x-www-form-urlencoded"},
                      method="POST")
    _, payload, _ = _json_response(request, 60)
    return payload


# --------------------------------------------------------------------------- #
# Envoi des vidéos
# --------------------------------------------------------------------------- #

def _youtube_upload(path: Path, title: str, description: str, privacy: str, token: str) -> dict:
    size = path.stat().st_size
    meta = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "categoryId": "20",
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    start = Request(
        f"{YOUTUBE_UPLOAD}?uploadType=resumable&part=snippet,status",
        data=json.dumps(meta).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "video/mp4",
        },
    )
    _, _, headers = _json_response(start, 120)
    session_url = headers.get("Location") or headers.get("location")
    if not session_url:
        raise RuntimeError("YouTube n'a pas renvoyé d'URL d'envoi.")

    with path.open("rb") as handle:
        upload = Request(session_url, data=handle.read(), method="PUT",
                         headers={"Authorization": f"Bearer {token}",
                                  "Content-Type": "video/mp4",
                                  "Content-Length": str(size)})
        _, payload, _ = _json_response(upload, 1800)
    video_id = payload.get("id")
    if not video_id:
        raise RuntimeError("YouTube n'a pas renvoyé d'identifiant de vidéo.")
    return {"id": video_id, "url": f"https://www.youtube.com/shorts/{video_id}"}


def _tiktok_upload(path: Path, title: str, token: str) -> dict:
    size = path.stat().st_size
    single = size <= 64_000_000
    chunk = size if single else 10_000_000
    total = 1 if single else (size + chunk - 1) // chunk
    body = {
        "post_info": {
            "title": title[:2200],
            "privacy_level": "SELF_ONLY",
            "disable_comment": False,
            "disable_duet": False,
            "disable_stitch": False,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": chunk,
            "total_chunk_count": total,
        },
    }
    request = Request(TIKTOK_INIT, data=json.dumps(body).encode(), method="POST",
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json; charset=UTF-8"})
    _, payload, _ = _json_response(request, 120)
    error = payload.get("error") or {}
    if error.get("code") not in (None, "ok"):
        raise RuntimeError(f"TikTok : {error.get('message') or error.get('code')}")
    data = payload.get("data") or {}
    upload_url, publish_id = data.get("upload_url"), data.get("publish_id")
    if not (upload_url and publish_id):
        raise RuntimeError("TikTok n'a pas renvoyé d'URL d'envoi.")

    with path.open("rb") as handle:
        index = 0
        while index < total:
            blob = handle.read(chunk)
            if not blob:
                break
            first = index * chunk
            last = first + len(blob) - 1
            put = Request(upload_url, data=blob, method="PUT",
                          headers={"Authorization": f"Bearer {token}",
                                   "Content-Type": "video/mp4",
                                   "Content-Length": str(len(blob)),
                                   "Content-Range": f"bytes {first}-{last}/{size}"})
            _json_response(put, 600)
            index += 1
    return {"id": publish_id, "url": None}


# --------------------------------------------------------------------------- #
# File de publication d'une tâche
# --------------------------------------------------------------------------- #

def _clip_title(clip: dict) -> str:
    start = float(clip.get("start") or 0)
    return f"Extrait {int(start // 60)}:{int(start % 60):02d} · {int(float(clip.get('duration') or 0))} s"


def _clip_description(clip: dict) -> str:
    return (f"{_clip_title(clip)}\n\nGénéré avec ClipStudio.\n"
            "#shorts #clips #stream")


def _publish_one(provider: str, job_dir: Path, clip: dict, privacy: str) -> dict:
    token = access_token(provider)
    path = job_dir / clip["name"]
    if not path.exists():
        raise RuntimeError("Le fichier du clip a disparu (stockage purgé ?).")
    title = _clip_title(clip)
    if provider == "youtube":
        return _youtube_upload(path, title, _clip_description(clip), privacy, token)
    return _tiktok_upload(path, _clip_description(clip), token)


def _run_publish(job_id: str, provider: str, privacy: str, clips: list[dict]) -> None:
    job_dir = OUTPUTS / job_id
    for clip in clips:
        with PUBLISH_LOCK:
            entry = next((c for c in PUBLISH[job_id]["clips"] if c["name"] == clip["name"]), None)
            if entry:
                entry["state"] = "envoi"
        try:
            result = _publish_one(provider, job_dir, clip, privacy)
            with PUBLISH_LOCK:
                entry = next(c for c in PUBLISH[job_id]["clips"] if c["name"] == clip["name"])
                entry.update(state="publié", url=result.get("url"), id=result.get("id"))
        except Exception as exc:  # noqa: BLE001 - remonté dans l'état du job
            with PUBLISH_LOCK:
                entry = next(c for c in PUBLISH[job_id]["clips"] if c["name"] == clip["name"])
                entry.update(state="échec", error=str(exc))
    with PUBLISH_LOCK:
        job = PUBLISH[job_id]
        failed = [c for c in job["clips"] if c["state"] == "échec"]
        job["status"] = "partiel" if failed else "terminé"
        job["error"] = failed[0]["error"] if failed else None


def start_job(job_id: str, provider: str, privacy: str, clips: list[dict]) -> dict:
    if provider not in PROVIDERS:
        raise RuntimeError("Plateforme de publication inconnue.")
    if not clips:
        raise RuntimeError("Aucun clip à publier.")
    if not is_connected(provider):
        raise RuntimeError(f"{LABELS[provider]} n'est pas connecté.")
    entries = [{"name": clip["name"], "state": "en attente", "url": None, "error": None}
               for clip in clips]
    with PUBLISH_LOCK:
        PUBLISH[job_id] = {"status": "en cours", "provider": provider, "privacy": privacy,
                          "clips": entries, "error": None}
    threading.Thread(target=_run_publish, args=(job_id, provider, privacy, clips),
                     name=f"publish-{provider}", daemon=True).start()
    return job_status(job_id)


def job_status(job_id: str) -> dict | None:
    with PUBLISH_LOCK:
        job = PUBLISH.get(job_id)
        return json.loads(json.dumps(job)) if job else None

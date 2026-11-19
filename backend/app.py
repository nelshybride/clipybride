"""ClipStudio — API locale (FastAPI) + interface studio servie en statique.

Lancement : python -m uvicorn app:app --app-dir backend --port 8000
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import os
import sys
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlencode, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from html import escape as html_escape
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl
from starlette.concurrency import run_in_threadpool

import publish as publish_mod

IS_VERCEL = bool(os.environ.get("VERCEL"))

ROOT = Path(__file__).resolve().parent          # backend/
PROJECT = ROOT.parent                           # clipstudio/
DATA = Path("/tmp/clipstudio") if IS_VERCEL else (ROOT / "data")
UPLOADS = DATA / "uploads"
OUTPUTS = DATA / "outputs"
THUMBS = DATA / "thumbs"
FRONTEND = PROJECT / "frontend"
RUNTIME_BIN = PROJECT / ".runtime" / "ffmpeg" / "bin"

for _dir in (DATA, UPLOADS, OUTPUTS, THUMBS):
    _dir.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD = 3 * 1024 * 1024 * 1024
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
ALLOWED_URL_HOSTS = ("youtube.com", "youtu.be", "twitch.tv", "kick.com")
CPU = os.cpu_count() or 2

# Sous-titres : modèle Whisper local, téléchargé une seule fois dans le projet.
MODELS = (DATA / "models") if IS_VERCEL else (PROJECT / ".runtime" / "models")
CACHE = DATA / "cache"
WHISPER_MODEL_SIZE = os.environ.get("CLIPSTUDIO_WHISPER_MODEL", "base")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# Cam + gameplay : deux bandes empilées 1080x960 = 1080x1920.
BAND_W, BAND_H = 1080, 960
CORNER_CHOICES = ("top-left", "top-right", "bottom-left", "bottom-right")
CAM_SCALES = (0.18, 0.22, 0.26, 0.30, 0.34, 0.38, 0.44)

TWITCH_API = "https://api.twitch.tv/helix"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_CONFIG = DATA / "twitch.json"
TWITCH_CHANNELS = DATA / "channels.json"

app = FastAPI(title="ClipStudio", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


class UrlRequest(BaseModel):
    url: HttpUrl


class CamBox(BaseModel):
    """Zone caméra libre, en fractions de la frame source (0..1)."""
    x: float
    y: float
    w: float


class ProcessRequest(BaseModel):
    source_id: str
    clip_count: int = 5
    clip_length: int = 35
    aspect: str = "9:16"
    subtitles: bool = False
    subtitle_pos: str = "bottom"  # bottom | middle | top
    subtitle_y: float | None = None  # centre du bloc de sous-titres, 0 (haut) .. 1 (bas)
    layout: str = "fill"          # fill | split (caméra en haut, jeu en bas)
    cam_corner: str = "auto"      # auto | top-left | top-right | bottom-left | bottom-right
    cam_scale: float = 0.30       # largeur de la caméra, en fraction de la frame
    cam_box: CamBox | None = None  # zone placée à la main, prioritaire sur le coin


class TwitchCredentials(BaseModel):
    client_id: str
    client_secret: str


class ChannelRequest(BaseModel):
    login: str


class TwitchClipImport(BaseModel):
    url: HttpUrl
    title: str | None = None


class PublishCredentials(BaseModel):
    client_id: str
    client_secret: str


class PublishRequest(BaseModel):
    provider: str = "youtube"
    privacy: str = "private"


class PurgeRequest(BaseModel):
    targets: list[str] = ["outputs", "uploads", "thumbs"]
    older_than_days: float | None = None


# --------------------------------------------------------------------------- #
# Outils externes
# --------------------------------------------------------------------------- #

def run(cmd, timeout=None, cwd=None) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                                timeout=timeout, cwd=str(cwd) if cwd else None)
    except FileNotFoundError:
        raise RuntimeError(f"Outil manquant : « {cmd[0]} ». Il devrait être dans .runtime/ffmpeg/bin.")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"« {cmd[0]} » a dépassé le temps imparti.")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Commande échouée").strip()
        raise RuntimeError(detail[-1200:])
    return result.stdout


def probe(path: Path) -> dict:
    """Durée + résolution en un seul appel ffprobe (import rapide)."""
    out = run(["ffprobe", "-v", "error", "-probesize", "5M", "-analyzeduration", "5M",
               "-select_streams", "v:0",
               "-show_entries", "stream=width,height:format=duration",
               "-of", "json", str(path)], timeout=120)
    try:
        data = json.loads(out or "{}")
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (ValueError, TypeError):
        raise RuntimeError("Durée illisible : le fichier n'est pas une vidéo exploitable.")
    if duration <= 0:
        raise RuntimeError("Durée de la vidéo invalide.")
    stream = (data.get("streams") or [{}])[0]
    return {"duration": duration,
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0)}


def duration_of(path: Path) -> float:
    return probe(path)["duration"]


def make_poster(src: Path, dest: Path, at: float) -> bool:
    """Extrait une vignette JPEG (deux essais pour les très courtes vidéos)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    for seek in (max(0.0, at), 0.0):
        dest.unlink(missing_ok=True)
        try:
            run(["ffmpeg", "-y", "-ss", f"{seek:.3f}", "-i", str(src), "-frames:v", "1",
                 "-vf", "scale=640:-2", "-q:v", "4", str(dest)], timeout=90)
        except RuntimeError:
            continue
        if dest.exists() and dest.stat().st_size > 0:
            return True
    return False


def is_supported_video_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in ALLOWED_URL_HOSTS)


def safe_id(value: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise HTTPException(400, "Identifiant invalide")
    matches = [p for p in UPLOADS.glob(value + ".*") if p.is_file()]
    if not matches:
        raise HTTPException(404, "Vidéo introuvable : réimporte-la.")
    return matches[0]


# --------------------------------------------------------------------------- #
# Transcription / sélection des moments
# --------------------------------------------------------------------------- #

_whisper_model = None
_whisper_lock = threading.Lock()


def module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _whisper_available() -> bool:
    return module_available("faster_whisper")


def get_whisper_model():
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel
            MODELS.mkdir(parents=True, exist_ok=True)
            _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu",
                                          compute_type="int8", download_root=str(MODELS))
        return _whisper_model


def decode_audio(path: Path):
    """Audio mono 16 kHz float32, décodé par ffmpeg."""
    import numpy as np
    result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
                             "-vn", "-f", "f32le", "-acodec", "pcm_f32le",
                             "-ac", "1", "-ar", "16000", "-"], capture_output=True)
    if result.returncode != 0 or not result.stdout:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(detail[-400:] or "Audio illisible.")
    return np.frombuffer(result.stdout, dtype=np.float32)


def transcribe_source(path: Path, cache_key: str):
    """Transcrit la piste audio de la vidéo, avec cache disque par source."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / f"{cache_key}.json"
    if cached.exists():
        try:
            data = json.loads(cached.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass

    model = get_whisper_model()
    segments, _info = model.transcribe(decode_audio(path), vad_filter=True,
                                      condition_on_previous_text=False)
    result = []
    for seg in segments:
        text = " ".join((seg.text or "").split())
        if text:
            result.append({"start": round(float(seg.start), 3),
                           "end": round(float(seg.end), 3), "text": text})
    try:
        cached.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return result


def transcribe_segments(path: Path):
    if not _whisper_available():
        return None
    try:
        return transcribe_source(path, path.stem) or None
    except Exception:
        return None


def choose_moments(path: Path, total: float, count: int, length: float, transcript=None):
    """Retourne (débuts triés, méthode). Heuristique locale, aucune IA distante."""
    length = max(1.0, min(float(length), total))
    if transcript is None:
        transcript = transcribe_segments(path)
    if transcript:
        keywords = ("incroyable", "énorme", "attends", "quoi", "non", "wow", "let's go",
                    "impossible", "mort", "dingue", "regarde", "oh", "yes", "putain")
        ranked = []
        for seg in transcript:
            center = (seg["start"] + seg["end"]) / 2
            text = seg["text"].lower()
            score = min(len(text) / 90, 1.5)
            score += sum(1 for w in keywords if w in text) * 0.7
            score += min(text.count("!") * 0.25 + text.count("?") * 0.15, 0.8)
            start = max(0.0, min(total - length, center - length / 2))
            ranked.append((score, start))
        ranked.sort(reverse=True)
        selected = []
        for _, start in ranked:
            if all(abs(start - old) > length * 0.8 for old in selected):
                selected.append(start)
            if len(selected) >= count:
                break
        if selected:
            return sorted(selected), "transcription"

    if total <= length:
        return [0.0], "répartition temporelle"
    usable = total - length
    if count == 1:
        return [usable / 2], "répartition temporelle"
    min_gap = max(2.0, length * 0.25)
    starts = []
    for i in range(count):
        start = usable * i / (count - 1)
        if all(abs(start - old) >= min_gap for old in starts):
            starts.append(start)
    return (starts or [0.0]), "répartition temporelle"


# --------------------------------------------------------------------------- #
# Tâches de génération (asynchrones, avec progression)
# --------------------------------------------------------------------------- #

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOB_READY: dict[str, dict[int, dict]] = {}
EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clipstudio-job")


def job_update(job_id: str, **fields):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update(fields)


# --------------------------------------------------------------------------- #
# Sous-titres : fichier ASS incrusté à l'encodage
# --------------------------------------------------------------------------- #

def _ass_time(value: float) -> str:
    value = max(0.0, value)
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    seconds = value % 60
    return f"{hours}:{minutes:02d}:{seconds:05.2f}"


def _ass_escape(text: str) -> str:
    return (text.replace("\\", " ").replace("{", "(").replace("}", ")")
                .replace("\r", " ").replace("\n", " "))


def split_caption(text: str, max_chars: int = 40) -> list[str]:
    """Coupe une phrase en lignes courtes, lisibles sur mobile."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        if current and len(current) + 1 + len(word) > max_chars:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [text]


def write_ass(dest: Path, segments: list[dict], clip_start: float, clip_end: float,
              width: int, height: int, margin: int, position: str = "bottom",
              center_y: float | None = None) -> None:
    """Écrit les sous-titres de la fenêtre du clip, recalés à zéro."""
    font_size = max(26, round(width * 0.052))
    outline = max(3, round(font_size * 0.09))
    side = round(width * 0.06)
    alignment, margin_v = {"top": (8, margin), "middle": (5, 0)}.get(position, (2, margin))
    override = ""
    if center_y is not None:
        y = max(0.02, min(0.98, float(center_y)))
        alignment, margin_v = 5, 0
        override = f"{{\\an5\\pos({width // 2},{round(y * height)})}}"
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Clip,Arial,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&HA0000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},1,{alignment},{side},{side},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for segment in segments:
        start = max(float(segment.get("start", 0)), clip_start)
        end = min(float(segment.get("end", 0)), clip_end)
        if end - start < 0.2:
            continue
        text = " ".join(str(segment.get("text") or "").split())
        if not text:
            continue
        words = split_caption(text)
        if len(words) > 2:
            middle = start + (end - start) / 2
            blocks = [(" ".join(words[:len(words) // 2]), start, middle),
                      (" ".join(words[len(words) // 2:]), middle, end)]
        else:
            blocks = [("\\N".join(_ass_escape(w) for w in words), start, end)]
        for block_text, block_start, block_end in blocks:
            lines.append(f"Dialogue: 0,{_ass_time(block_start - clip_start)},"
                         f"{_ass_time(block_end - clip_start)},Clip,,0,0,0,,{override}{block_text}")

    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Cadrage : caméra en haut, gameplay en bas
# --------------------------------------------------------------------------- #

def _even(value: float) -> int:
    return max(2, int(round(value)) // 2 * 2)


def _fit_ratio(fw: int, fh: int, ratio: float) -> tuple[int, int]:
    if fw / fh > ratio:
        return _even(fh * ratio), _even(fh)
    return _even(fw), _even(fw / ratio)


def cam_rect(fw: int, fh: int, corner: str, scale: float, ratio: float) -> dict:
    """Zone caméra : bloc du coin demandé, agrandi au ratio de la bande."""
    cam_w, cam_h = fw * scale, fh * scale
    if cam_w / max(cam_h, 1) > ratio:
        width, height = cam_w, cam_w / ratio
    else:
        width, height = cam_h * ratio, cam_h
    width, height = min(width, fw), min(height, fh)
    x = 0 if corner.endswith("left") else fw - width
    y = 0 if corner.startswith("top") else fh - height
    width, height = _even(width), _even(height)
    x = max(0, min(_even(x), fw - width))
    y = max(0, min(_even(y), fh - height))
    return {"x": x, "y": y, "w": width, "h": height}


def box_rect(fw: int, fh: int, x: float, y: float, w: float) -> dict:
    """Zone caméra placée librement : origine et largeur relatives, hauteur au ratio."""
    width = max(0.05, min(1.0, float(w))) * fw
    height = width / (BAND_W / BAND_H)
    if height > fh:
        height = float(fh)
        width = height * (BAND_W / BAND_H)
    width, height = _even(width), _even(height)
    x_px = max(0, min(_even(max(0.0, min(1.0, float(x))) * fw), fw - width))
    y_px = max(0, min(_even(max(0.0, min(1.0, float(y))) * fh), fh - height))
    return {"x": x_px, "y": y_px, "w": width, "h": height}


def gameplay_rect(fw: int, fh: int, ratio: float, cam: dict | None) -> dict:
    """Plus grand cadre de ratio `ratio` qui évite le plus possible la caméra."""
    width, height = _fit_ratio(fw, fh, ratio)
    best = None
    steps = 12
    for ix in range(steps + 1):
        for iy in range(steps + 1):
            x = min(_even((fw - width) * ix / steps), fw - width)
            y = min(_even((fh - height) * iy / steps), fh - height)
            overlap = 0.0
            if cam:
                ox = max(0, min(x + width, cam["x"] + cam["w"]) - max(x, cam["x"]))
                oy = max(0, min(y + height, cam["y"] + cam["h"]) - max(y, cam["y"]))
                overlap = (ox * oy) / max(1, cam["w"] * cam["h"])
            drift = (abs(x + width / 2 - fw / 2) / fw) + (abs(y + height / 2 - fh / 2) / fh)
            score = overlap * 2.5 + drift * 0.30
            if best is None or score < best[0]:
                best = (score, x, y)
    return {"x": best[1], "y": best[2], "w": width, "h": height}


def build_graph(fw: int, fh: int, aspect: str, cam: dict | None,
                ass_name: str | None = None) -> tuple[str, str]:
    """(-filter_complex, label de sortie). `cam` = zone caméra en pixels source."""
    steps: list[str] = []
    if aspect == "16:9":
        steps.append("[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
                     "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1[base]")
    elif cam:
        game = gameplay_rect(fw, fh, BAND_W / BAND_H, cam)
        steps.append("[0:v]split=2[sv0][sv1]")
        steps.append(f"[sv0]crop={cam['w']}:{cam['h']}:{cam['x']}:{cam['y']},"
                     f"scale={BAND_W}:{BAND_H},setsar=1[up]")
        steps.append(f"[sv1]crop={game['w']}:{game['h']}:{game['x']}:{game['y']},"
                     f"scale={BAND_W}:{BAND_H},setsar=1[down]")
        steps.append("[up][down]vstack=inputs=2[base]")
    else:
        steps.append("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                     "crop=1080:1920,setsar=1[base]")

    label = "[base]"
    if ass_name:
        steps.append(f"[base]ass={ass_name}[subbed]")
        label = "[subbed]"
    return ";".join(steps), label


def detect_cam(src: Path, total: float, strict: bool = True) -> dict | None:
    """Repère une incrustation caméra, via ses bords rectangulaires."""
    import numpy as np

    width = 160
    frames = []
    for index in range(12):
        at = max(0.0, min(total - 0.05, total * (index + 0.5) / 12))
        result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{at:.2f}",
                                 "-i", str(src), "-frames:v", "1",
                                 "-vf", f"scale={width}:-2,format=gray",
                                 "-f", "rawvideo", "-"], capture_output=True)
        if result.returncode == 0 and result.stdout:
            frames.append(np.frombuffer(result.stdout, dtype=np.uint8))
    if len(frames) < 4:
        return None

    height = len(frames[0]) // width
    if height < 20:
        return None
    stack = np.stack([f[:width * height].reshape(height, width).astype(np.float32)
                      for f in frames])
    mean = stack.mean(axis=0)
    gx = np.abs(np.diff(mean, axis=1))
    gy = np.abs(np.diff(mean, axis=0))
    grad = np.zeros((height, width), dtype=np.float32)
    grad[:, :-1] += gx
    grad[:, 1:] += gx
    grad[:-1, :] += gy
    grad[1:, :] += gy
    baseline = float(np.median(grad)) + 1e-3

    def column_strength(x: int, y0: int, h: int) -> float:
        lo, hi = max(0, x - 2), min(width, x + 3)
        if hi <= lo or h <= 0:
            return 0.0
        return float(grad[y0:y0 + h, lo:hi].max(axis=1).mean())

    def row_strength(y: int, x0: int, w: int) -> float:
        lo, hi = max(0, y - 2), min(height, y + 3)
        if hi <= lo or w <= 0:
            return 0.0
        return float(grad[lo:hi, x0:x0 + w].max(axis=0).mean())

    scored = []
    for corner in CORNER_CHOICES:
        left = corner.endswith("left")
        top = corner.startswith("top")
        best = None
        for scale in CAM_SCALES:
            cam_w, cam_h = max(8, round(width * scale)), max(6, round(height * scale))
            cam_w, cam_h = min(cam_w, width), min(cam_h, height)
            x0 = 0 if left else width - cam_w
            y0 = 0 if top else height - cam_h
            edge_x = x0 + cam_w - 1 if left else x0
            edge_y = y0 + cam_h - 1 if top else y0
            score = (column_strength(edge_x, y0, cam_h)
                     + row_strength(edge_y, x0, cam_w)) / 2 / baseline
            if best is None or score > best[0]:
                best = (score, scale, cam_w, cam_h, x0, y0)
        scored.append((best[0], corner, best))

    scored.sort(key=lambda item: item[0], reverse=True)
    score, corner, (_, scale, cam_w, cam_h, x0, y0) = scored[0]
    runner_up = scored[1][0]
    confidence = score / max(runner_up, 0.01)
    confident = score >= 2.0 and confidence >= 1.8
    if strict and not confident:
        return None
    return {"corner": corner, "score": round(score, 2), "confidence": round(confidence, 2),
            "scale": scale, "confident": confident,
            "rect": {"x": round(x0 / width, 4), "y": round(y0 / height, 4),
                     "w": round(cam_w / width, 4), "h": round(cam_h / height, 4)}}


def render_clip(src: Path, out: Path, start: float, length: float, graph: str,
                label: str, threads: int, workdir: Path):
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{length:.3f}",
         "-filter_complex", graph, "-map", label, "-map", "0:a?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
         "-pix_fmt", "yuv420p", "-threads", str(threads),
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)],
        timeout=2400, cwd=workdir)


def run_job(job_id: str, src: Path, total: float, count: int, length: int, aspect: str,
            options: dict | None = None):
    options = options or {}
    job_dir = OUTPUTS / job_id
    try:
        job_update(job_id, status="running", progress=0.04, message="Analyse de la vidéo…")
        job_dir.mkdir(parents=True, exist_ok=True)

        info = probe(src)
        fw, fh = info["width"], info["height"]
        notes: list[str] = []

        subtitles = bool(options.get("subtitles"))
        transcript = None
        if subtitles and not _whisper_available():
            subtitles = False
            notes.append("Sous-titres indisponibles : lance setup.ps1 pour installer "
                         "la reconnaissance vocale.")
        if subtitles:
            job_update(job_id, progress=0.08, message="Transcription de l'audio…")
            try:
                transcript = transcribe_source(src, options.get("source_id") or src.stem)
            except Exception as exc:  # noqa: BLE001
                subtitles = False
                notes.append(f"Transcription impossible ({str(exc)[:140]}).")

        cam = None
        layout = options.get("layout") or "fill"
        if layout == "split" and aspect == "9:16":
            box = options.get("cam_box")
            if box:
                cam = box_rect(fw, fh, box["x"], box["y"], box["w"])
            else:
                corner = options.get("cam_corner") or "auto"
                if corner == "auto":
                    job_update(job_id, progress=0.12, message="Détection de la caméra…")
                    detected = detect_cam(src, total)
                    corner = detected["corner"] if detected else "bottom-right"
                    if not detected:
                        notes.append("Aucune caméra détectée : coin bas-droite par défaut, "
                                     "vérifie l'aperçu.")
                cam = cam_rect(fw, fh, corner, float(options.get("cam_scale") or 0.30),
                               BAND_W / BAND_H)
        elif layout == "split":
            notes.append("« Cam + gameplay » est réservé au format vertical 9:16.")

        starts, method = choose_moments(src, total, count, length, transcript)
        if len(starts) < count:
            notes.append(f"{len(starts)} clip(s) possible(s) sur {count} demandé(s) avec "
                         f"{length}s par clip : la vidéo est trop courte pour en placer "
                         "davantage.")
        job_update(job_id, progress=0.18, method=method, note=" ".join(notes) or None,
                   message=f"{len(starts)} passage(s) détecté(s) · {method}")

        workers = max(1, min(4, len(starts), max(1, CPU // 2)))
        threads = max(1, CPU // workers)
        done = itertools.count(1)

        def publish_clip(index: int, entry: dict, **fields):
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if job is None:
                    return
                slots = JOB_READY.setdefault(job_id, {})
                slots[index] = entry
                job["clips"] = [slots[k] for k in sorted(slots)]
                job.update(fields)

        subtitle_pos = options.get("subtitle_pos")
        if subtitle_pos not in ("top", "middle", "bottom"):
            subtitle_pos = "bottom"
        subtitle_y = options.get("subtitle_y")
        if subtitle_y is not None:
            try:
                subtitle_y = max(0.02, min(0.98, float(subtitle_y)))
            except (TypeError, ValueError):
                subtitle_y = None

        def one(index: int, start: float):
            clip_len = round(min(length, max(0.5, total - start)), 3)
            ass_name = None
            if subtitles and transcript:
                ass_name = f"clip_{index:02d}.ass"
                if aspect == "9:16":
                    width, height = 1080, 1920
                    margin = 160 if subtitle_pos == "top" else 250
                else:
                    width, height = 1920, 1080
                    margin = 60
                write_ass(job_dir / ass_name, transcript, start, start + clip_len,
                          width, height, margin, subtitle_pos, subtitle_y)

            graph, label = build_graph(fw, fh, aspect, cam, ass_name)
            out = job_dir / f"clip_{index:02d}.mp4"
            render_clip(src, out, start, clip_len, graph, label, threads, job_dir)
            make_poster(out, job_dir / f"clip_{index:02d}.jpg", min(1.0, max(0.1, clip_len / 3)))
            n = next(done)
            entry = {"name": out.name, "start": round(start, 1),
                     "duration": round(clip_len, 1),
                     "subtitles": bool(ass_name), "split": bool(cam),
                     "url": f"/media/{job_id}/{out.name}",
                     "poster": f"/media/{job_id}/clip_{index:02d}.jpg"}
            publish_clip(index, entry,
                         progress=min(0.94, 0.18 + 0.76 * n / max(1, len(starts))),
                         message=f"{n}/{len(starts)} clip(s) prêt(s) · encodage en cours")
            return entry

        job_update(job_id, progress=0.2, message=f"Encodage de {len(starts)} clip(s)…")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda item: one(item[0], item[1]), enumerate(starts, 1)))
        with JOBS_LOCK:
            slots = JOB_READY.get(job_id, {})
            clips = [slots[k] for k in sorted(slots)]

        job_update(job_id, progress=0.96, message="Création de l'archive ZIP…")
        with zipfile.ZipFile(job_dir / "clips.zip", "w", zipfile.ZIP_DEFLATED) as z:
            for clip in clips:
                z.write(job_dir / clip["name"], clip["name"])

        job_update(job_id, status="done", progress=1.0, message="Clips prêts",
                   clips=clips, zip_url=f"/media/{job_id}/clips.zip")
        JOB_READY.pop(job_id, None)
    except Exception as exc:  # noqa: BLE001
        JOB_READY.pop(job_id, None)
        shutil.rmtree(job_dir, ignore_errors=True)
        job_update(job_id, status="error", progress=1.0,
                   message=str(exc) or "Traitement impossible", error=str(exc))


# --------------------------------------------------------------------------- #
# Twitch (Helix, app access token)
# --------------------------------------------------------------------------- #

_twitch_token = {"value": None, "expires": 0.0}
_twitch_lock = threading.Lock()


def twitch_credentials():
    """Identifiants : variables d'environnement prioritaires, sinon fichier local."""
    cid = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    if cid and secret:
        return cid, secret, "variables d'environnement"
    if TWITCH_CONFIG.exists():
        try:
            saved = json.loads(TWITCH_CONFIG.read_text(encoding="utf-8"))
        except Exception:
            saved = {}
        cid = (saved.get("client_id") or "").strip()
        secret = (saved.get("client_secret") or "").strip()
        if cid and secret:
            return cid, secret, "fichier local"
    return None, None, None


def http_json(url, data=None, headers=None, timeout=30):
    body = urlencode(data).encode() if data else None
    request_headers = dict(headers or {})
    if body:
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=body, headers=request_headers,
                                     method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", "replace")
        try:
            message = json.loads(payload).get("message") or payload
        except Exception:
            message = payload
        raise RuntimeError(f"Twitch {exc.code} : {str(message)[:200]}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Twitch injoignable : {exc.reason}")


def twitch_token(force=False):
    cid, secret, _ = twitch_credentials()
    if not cid or not secret:
        raise RuntimeError("Identifiants Twitch absents.")
    with _twitch_lock:
        if not force and _twitch_token["value"] and _twitch_token["expires"] > time.time() + 60:
            return _twitch_token["value"]
        data = http_json(TWITCH_TOKEN_URL, {"client_id": cid, "client_secret": secret,
                                            "grant_type": "client_credentials"})
        token = data.get("access_token")
        if not token:
            raise RuntimeError("Twitch n'a pas renvoyé de jeton d'accès.")
        _twitch_token.update(value=token,
                             expires=time.time() + float(data.get("expires_in") or 3600))
        return token


def twitch_get(path, params):
    cid, _, _ = twitch_credentials()
    url = f"{TWITCH_API}/{path}?{urlencode(params)}"
    for attempt in (1, 2):
        token = twitch_token(force=(attempt == 2))
        try:
            return http_json(url, headers={"Client-Id": cid,
                                           "Authorization": f"Bearer {token}"})
        except RuntimeError as exc:
            if attempt == 2 or "401" not in str(exc):
                raise
    raise RuntimeError("Twitch : authentification impossible.")


def load_channels():
    try:
        items = json.loads(TWITCH_CHANNELS.read_text(encoding="utf-8"))
        return items if isinstance(items, list) else []
    except Exception:
        return []


def save_channels(items):
    TWITCH_CHANNELS.parent.mkdir(parents=True, exist_ok=True)
    TWITCH_CHANNELS.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                               encoding="utf-8")


def normalize_login(value):
    login = (value or "").strip().lower()
    login = re.sub(r"^https?://(www\.|m\.)?twitch\.tv/", "", login)
    login = login.split("?")[0].split("/")[0].strip().lstrip("@")
    if not re.fullmatch(r"[a-z0-9_]{3,25}", login):
        raise HTTPException(400, "Nom de chaîne invalide.")
    return login


def clip_mp4_url(thumbnail):
    """Twitch expose la miniature ; le MP4 est la même URL sans le suffixe -preview."""
    if not thumbnail:
        return None
    return re.sub(r"-preview-\d+x\d+\.[a-z]+$", ".mp4", thumbnail)


# --------------------------------------------------------------------------- #
# Stockage local
# --------------------------------------------------------------------------- #

def dir_stats(path: Path) -> dict:
    total = 0
    files = 0
    if not path.exists():
        return {"bytes": 0, "files": 0}
    for item in path.rglob("*"):
        if item.is_file():
            files += 1
            total += item.stat().st_size
    return {"bytes": total, "files": files}


def storage_snapshot() -> dict:
    return {"uploads": dir_stats(UPLOADS), "outputs": dir_stats(OUTPUTS),
            "thumbs": dir_stats(THUMBS)}


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "vercel": IS_VERCEL,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
        "yt_dlp": module_available("yt_dlp"),
        "whisper": _whisper_available(),
    }


@app.post("/api/upload")
async def upload_video(request: Request):
    """Upload en flux brut : pas de fichier temporaire, écriture unique."""
    UPLOADS.mkdir(parents=True, exist_ok=True)
    THUMBS.mkdir(parents=True, exist_ok=True)
    raw_name = unquote(request.headers.get("x-filename", "video.mp4"))
    filename = Path(raw_name).name or "video.mp4"
    ext = Path(filename).suffix.lower()
    if ext not in VIDEO_EXTS:
        raise HTTPException(400, "Format non pris en charge (MP4, MOV, MKV, WebM, M4V).")

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD:
        raise HTTPException(413, "Fichier trop volumineux (limite 3 Go).")

    source_id = uuid.uuid4().hex
    dest = UPLOADS / f"{source_id}{ext}"
    size = 0
    buffer = bytearray()
    try:
        with dest.open("wb") as handle:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "Fichier trop volumineux (limite 3 Go).")
                buffer += chunk
                if len(buffer) >= 4 * 1024 * 1024:
                    await run_in_threadpool(handle.write, bytes(buffer))
                    buffer.clear()
            if buffer:
                await run_in_threadpool(handle.write, bytes(buffer))

        if size == 0:
            raise HTTPException(400, "Fichier vide.")

        info = await run_in_threadpool(probe, dest)
        poster_ok = await run_in_threadpool(make_poster, dest, THUMBS / f"{source_id}.jpg", 1.0)
    except Exception:
        dest.unlink(missing_ok=True)
        (THUMBS / f"{source_id}.jpg").unlink(missing_ok=True)
        raise

    return {"source_id": source_id, "filename": filename,
            "duration": info["duration"], "width": info["width"], "height": info["height"],
            "size": size, "video_url": f"/sources/{dest.name}",
            "poster": f"/thumbs/{source_id}.jpg" if poster_ok else None}


@app.post("/api/url")
def import_url(req: UrlRequest):
    UPLOADS.mkdir(parents=True, exist_ok=True)
    THUMBS.mkdir(parents=True, exist_ok=True)
    url = str(req.url)
    if not is_supported_video_url(url):
        raise HTTPException(400, "Seules les URL YouTube, Twitch et Kick sont acceptées.")
    source_id = uuid.uuid4().hex
    try:
        run([sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings",
             "-f", "bv*+ba/b", "--merge-output-format", "mp4",
             "-o", str(UPLOADS / source_id) + ".%(ext)s", url], timeout=3600)
        candidates = [p for p in UPLOADS.glob(source_id + ".*") if p.is_file()]
        if not candidates:
            raise RuntimeError("Aucun fichier téléchargé.")
        downloaded = candidates[0]
        dest = UPLOADS / f"{source_id}{(downloaded.suffix or '.mp4').lower()}"
        downloaded.replace(dest)
        info = probe(dest)
        poster_ok = make_poster(dest, THUMBS / f"{source_id}.jpg", 1.0)
        return {"source_id": source_id, "filename": url,
                "duration": info["duration"], "width": info["width"], "height": info["height"],
                "size": dest.stat().st_size, "video_url": f"/sources/{dest.name}",
                "poster": f"/thumbs/{source_id}.jpg" if poster_ok else None}
    except Exception as exc:
        for leftover in UPLOADS.glob(source_id + "*"):
            leftover.unlink(missing_ok=True)
        raise HTTPException(400, f"Import impossible : {exc}")


@app.post("/api/process")
def process(req: ProcessRequest):
    src = safe_id(req.source_id)
    count = max(1, min(20, req.clip_count))
    length = max(5, min(180, req.clip_length))
    if req.aspect not in ("9:16", "16:9"):
        raise HTTPException(400, "Format invalide")
    if req.layout not in ("fill", "split"):
        raise HTTPException(400, "Recadrage invalide (fill ou split).")
    if req.cam_corner not in ("auto",) + CORNER_CHOICES:
        raise HTTPException(400, "Position de caméra invalide.")
    try:
        total = duration_of(src)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    if total < 2:
        raise HTTPException(400, "Vidéo trop courte (minimum 2 secondes).")

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"id": job_id, "status": "queued", "progress": 0.0,
                        "message": "En attente…", "method": None, "note": None, "clips": [],
                        "zip_url": None, "error": None}
    cam_box = None
    if req.cam_box:
        cam_box = {"x": max(0.0, min(1.0, float(req.cam_box.x))),
                   "y": max(0.0, min(1.0, float(req.cam_box.y))),
                   "w": max(0.05, min(1.0, float(req.cam_box.w)))}
    options = {"subtitles": bool(req.subtitles), "subtitle_pos": req.subtitle_pos,
               "subtitle_y": req.subtitle_y, "layout": req.layout,
               "cam_corner": req.cam_corner, "cam_box": cam_box,
               "cam_scale": max(0.15, min(0.50, float(req.cam_scale))),
               "source_id": req.source_id}
    EXECUTOR.submit(run_job, job_id, src, total, count, length, req.aspect, options)
    return {"job_id": job_id}


@app.get("/api/publish/status")
def publish_status():
    return publish_mod.status()


@app.post("/api/publish/credentials/{provider}")
def publish_set_credentials(provider: str, req: PublishCredentials):
    if provider not in publish_mod.PROVIDERS:
        raise HTTPException(400, "Plateforme inconnue (youtube ou tiktok).")
    if not req.client_id.strip() or not req.client_secret.strip():
        raise HTTPException(400, "Client ID et Client Secret sont requis.")
    publish_mod.set_credentials(provider, req.client_id, req.client_secret)
    return publish_mod.status()


@app.delete("/api/publish/credentials/{provider}")
def publish_clear_credentials(provider: str):
    if provider not in publish_mod.PROVIDERS:
        raise HTTPException(400, "Plateforme inconnue (youtube ou tiktok).")
    publish_mod.clear_credentials(provider)
    return publish_mod.status()


def _oauth_redirect(request: Request, provider: str) -> str:
    return str(request.base_url).rstrip("/") + f"/api/oauth/{provider}/callback"


@app.post("/api/publish/auth/{provider}")
def publish_auth(provider: str, request: Request):
    if provider not in publish_mod.PROVIDERS:
        raise HTTPException(400, "Plateforme inconnue (youtube ou tiktok).")
    redirect = _oauth_redirect(request, provider)
    try:
        url = publish_mod.build_auth_url(provider, redirect)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    return {"url": url, "redirect_uri": redirect}


@app.get("/api/oauth/{provider}/callback", response_class=HTMLResponse)
def oauth_callback(request: Request, provider: str, code: str = "", state: str = "",
                   error: str = ""):
    redirect = _oauth_redirect(request, provider)
    if error:
        message, ok = f"Autorisation refusée : {html_escape(error)}", False
    elif not code or not state:
        message, ok = "Réponse incomplète de la plateforme.", False
    else:
        try:
            publish_mod.finish_auth(code, state, redirect)
            message, ok = f"{publish_mod.LABELS[provider]} connecté. Tu peux fermer cet onglet.", True
        except Exception as exc:  # noqa: BLE001
            message, ok = f"Connexion impossible : {html_escape(str(exc))}", False
    color = "#3ddc97" if ok else "#ff6b6b"
    return HTMLResponse(f"""<!doctype html><html lang="fr"><meta charset="utf-8">
<title>ClipStudio</title>
<body style="margin:0;height:100vh;display:grid;place-items:center;background:#0b0d12;
color:#e8ecf5;font:15px/1.6 system-ui,sans-serif">
<div style="text-align:center;max-width:520px;padding:24px">
<p style="font-size:17px;border-left:3px solid {color};padding-left:14px;text-align:left">{message}</p>
<p style="color:#8b93a7">Reviens dans ClipStudio.</p></div></body></html>""")


@app.post("/api/publish/{job_id}")
def publish_job(job_id: str, req: PublishRequest):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(400, "Identifiant invalide")
    if req.privacy not in ("private", "unlisted", "public"):
        raise HTTPException(400, "Visibilité invalide (private, unlisted ou public).")
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        clips = list(job.get("clips") or []) if job else []
    if not clips:
        raise HTTPException(404, "Aucun clip à publier : génère d'abord les clips.")
    try:
        return publish_mod.start_job(job_id, req.provider, req.privacy, clips)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/publish/{job_id}")
def publish_job_status(job_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(400, "Identifiant invalide")
    return publish_mod.job_status(job_id) or {"status": "inconnu", "clips": []}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(400, "Identifiant invalide")
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Tâche introuvable")
        return dict(job)


@app.get("/api/cam/{source_id}")
def cam_probe(source_id: str):
    """Détection : `detected` si sûre, sinon `candidate` comme point de départ."""
    src = safe_id(source_id)
    info = probe(src)
    best = detect_cam(src, info["duration"], strict=False)
    detected = best if best and best.get("confident") else None
    return {"detected": detected, "candidate": best,
            "width": info["width"], "height": info["height"]}


@app.get("/api/frame/{source_id}")
def source_frame(source_id: str, at: float = 1.0):
    """Image de la source (non recadrée) : support du cadre caméra déplaçable."""
    src = safe_id(source_id)
    info = probe(src)
    seek = max(0.0, min(float(at), max(0.0, info["duration"] - 0.2)))
    result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{seek:.2f}",
                             "-i", str(src), "-frames:v", "1",
                             "-vf", "scale=640:-2", "-q:v", "4",
                             "-f", "image2", "-vcodec", "mjpeg", "-"],
                            capture_output=True)
    if result.returncode != 0 or not result.stdout:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise HTTPException(400, f"Image impossible : {detail[-300:]}")
    return Response(content=result.stdout, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/preview/{source_id}")
def preview_frame(source_id: str, layout: str = "fill", corner: str = "auto",
                  scale: float = 0.30, x: float | None = None,
                  y: float | None = None, w: float | None = None, at: float = 1.0):
    """Image d'aperçu du cadrage, sans encodage complet."""
    src = safe_id(source_id)
    info = probe(src)
    cam = None
    if layout == "split":
        if x is not None and y is not None and w is not None:
            cam = box_rect(info["width"], info["height"], x, y, w)
        else:
            resolved = corner if corner in CORNER_CHOICES else "auto"
            if resolved == "auto":
                best = detect_cam(src, info["duration"], strict=False)
                resolved = best["corner"] if best else "bottom-right"
            cam = cam_rect(info["width"], info["height"], resolved,
                           max(0.15, min(0.50, float(scale))), BAND_W / BAND_H)

    graph, label = build_graph(info["width"], info["height"], "9:16", cam)
    seek = max(0.0, min(float(at), max(0.0, info["duration"] - 0.2)))
    result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{seek:.2f}",
                             "-i", str(src), "-frames:v", "1",
                             "-filter_complex", graph, "-map", label,
                             "-q:v", "4", "-f", "image2", "-vcodec", "mjpeg", "-"],
                            capture_output=True)
    if result.returncode != 0 or not result.stdout:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise HTTPException(400, f"Aperçu impossible : {detail[-300:]}")
    return Response(content=result.stdout, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/twitch/status")
def twitch_status():
    cid, _, source = twitch_credentials()
    valid = False
    error = None
    if cid:
        try:
            twitch_token()
            valid = True
        except Exception as exc:
            error = str(exc)
    return {"configured": bool(cid), "source": source, "valid": valid,
            "error": error, "channels": load_channels()}


@app.post("/api/twitch/credentials")
def twitch_set_credentials(req: TwitchCredentials):
    cid, secret = req.client_id.strip(), req.client_secret.strip()
    if not cid or not secret:
        raise HTTPException(400, "Client ID et Client Secret sont requis.")
    try:
        data = http_json(TWITCH_TOKEN_URL, {"client_id": cid, "client_secret": secret,
                                            "grant_type": "client_credentials"})
    except RuntimeError as exc:
        raise HTTPException(400, f"Identifiants refusés — {exc}")
    TWITCH_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    TWITCH_CONFIG.write_text(json.dumps({"client_id": cid, "client_secret": secret}, indent=2),
                             encoding="utf-8")
    with _twitch_lock:
        _twitch_token.update(value=data.get("access_token"),
                             expires=time.time() + float(data.get("expires_in") or 3600))
    return {"configured": True, "valid": True}


@app.delete("/api/twitch/credentials")
def twitch_clear_credentials():
    TWITCH_CONFIG.unlink(missing_ok=True)
    with _twitch_lock:
        _twitch_token.update(value=None, expires=0.0)
    return {"configured": bool(twitch_credentials()[0])}


@app.post("/api/twitch/channels")
def twitch_add_channel(req: ChannelRequest):
    login = normalize_login(req.login)
    channels = load_channels()
    if any(c.get("login") == login for c in channels):
        raise HTTPException(409, "Cette chaîne est déjà ajoutée.")
    try:
        data = twitch_get("users", {"login": login})
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))
    users = data.get("data") or []
    if not users:
        raise HTTPException(404, "Chaîne Twitch introuvable.")
    user = users[0]
    channel = {"id": user["id"], "login": user["login"],
               "name": user.get("display_name") or user["login"],
               "avatar": user.get("profile_image_url")}
    channels.append(channel)
    save_channels(channels)
    return channel


@app.delete("/api/twitch/channels/{login}")
def twitch_remove_channel(login: str):
    login = login.strip().lower()
    channels = load_channels()
    remaining = [c for c in channels if c.get("login") != login]
    if len(remaining) == len(channels):
        raise HTTPException(404, "Chaîne absente de la liste.")
    save_channels(remaining)
    return {"channels": remaining}


@app.get("/api/twitch/clips/{login}")
def twitch_clips(login: str, period: str = "week", limit: int = 24):
    channels = load_channels()
    channel = next((c for c in channels if c.get("login") == login.lower()), None)
    if not channel:
        raise HTTPException(404, "Ajoute d'abord cette chaîne.")
    params = {"broadcaster_id": channel["id"], "first": str(max(1, min(50, limit)))}
    window = {"day": 1, "week": 7, "month": 31}.get(period)
    if window:
        start = datetime.now(timezone.utc) - timedelta(days=window)
        params["started_at"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        data = twitch_get("clips", params)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))
    clips = []
    for item in data.get("data") or []:
        clips.append({"id": item.get("id"),
                      "title": item.get("title") or "Clip",
                      "creator": item.get("creator_name"),
                      "views": item.get("view_count") or 0,
                      "duration": item.get("duration") or 0,
                      "created_at": item.get("created_at"),
                      "thumbnail": item.get("thumbnail_url"),
                      "page": item.get("url"),
                      "mp4": clip_mp4_url(item.get("thumbnail_url"))})
    return {"channel": channel, "clips": clips, "period": period}


@app.post("/api/twitch/import")
def twitch_import(req: TwitchClipImport):
    UPLOADS.mkdir(parents=True, exist_ok=True)
    THUMBS.mkdir(parents=True, exist_ok=True)
    url = str(req.url)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "twitch.tv" or host.endswith(".twitch.tv")):
        raise HTTPException(400, "Lien .mp4 Twitch attendu.")
    source_id = uuid.uuid4().hex
    dest = UPLOADS / f"{source_id}.mp4"
    try:
        with urllib.request.urlopen(url, timeout=180) as response, dest.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        if dest.stat().st_size == 0:
            raise RuntimeError("Téléchargement vide.")
        info = probe(dest)
        poster_ok = make_poster(dest, THUMBS / f"{source_id}.jpg", 1.0)
        return {"source_id": source_id, "filename": req.title or "Clip Twitch",
                "duration": info["duration"], "width": info["width"],
                "height": info["height"], "size": dest.stat().st_size,
                "video_url": f"/sources/{dest.name}",
                "poster": f"/thumbs/{source_id}.jpg" if poster_ok else None}
    except Exception as exc:
        dest.unlink(missing_ok=True)
        (THUMBS / f"{source_id}.jpg").unlink(missing_ok=True)
        raise HTTPException(400, f"Import du clip impossible : {exc}")


@app.get("/api/storage")
def storage_usage():
    return storage_snapshot()


@app.post("/api/storage/purge")
def storage_purge(req: PurgeRequest):
    with JOBS_LOCK:
        busy = any(job["status"] in ("queued", "running") for job in JOBS.values())
    if busy:
        raise HTTPException(409, "Une génération est en cours : attends la fin avant de purger.")
    folders = {"uploads": UPLOADS, "outputs": OUTPUTS, "thumbs": THUMBS}
    targets = [name for name in req.targets if name in folders]
    if not targets:
        raise HTTPException(400, "Aucune cible valide (uploads, outputs, thumbs).")
    cutoff = time.time() - req.older_than_days * 86400 if req.older_than_days else None
    freed = 0
    deleted = 0
    for name in targets:
        if not folders[name].exists():
            continue
        for item in list(folders[name].iterdir()):
            try:
                if cutoff and item.stat().st_mtime > cutoff:
                    continue
                if item.is_dir():
                    size = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    size = item.stat().st_size
                    item.unlink(missing_ok=True)
                freed += size
                deleted += 1
            except FileNotFoundError:
                continue
    return {"freed_bytes": freed, "deleted": deleted, "storage": storage_snapshot()}


@app.middleware("http")
async def no_cache_frontend(request, call_next):
    """HTML/CSS/JS changent souvent : le navigateur doit revalider à chaque fois."""
    response = await call_next(request)
    if request.url.path in ("/", "/index.html", "/styles.css", "/app.js"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# Fichiers produits (les StaticFiles gèrent le Range, donc la lecture/seek vidéo).
app.mount("/media", StaticFiles(directory=OUTPUTS, check_dir=False), name="media")
app.mount("/thumbs", StaticFiles(directory=THUMBS, check_dir=False), name="thumbs")
app.mount("/sources", StaticFiles(directory=UPLOADS, check_dir=False), name="sources")
if FRONTEND.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
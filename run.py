"""Lanceur ClipStudio : verifie l'environnement, choisit un port libre, ouvre le navigateur.

En cas d'echec, un fichier diagnostic.txt est ecrit dans le dossier du projet.

Utilisation :
    .runtime\\python\\python.exe run.py              lance le serveur
    .runtime\\python\\python.exe run.py --diag       ecrit diagnostic.txt et quitte
    .runtime\\python\\python.exe run.py --no-browser lance sans ouvrir le navigateur
"""

from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
RUNTIME = ROOT / ".runtime"
DIAG = ROOT / "diagnostic.txt"

MIN_PYTHON = (3, 10)
REQUIRED_MODULES = ("fastapi", "uvicorn", "yt_dlp")
OPTIONAL_MODULES = ("faster_whisper",)
PORT_START = 8000
PORT_TRIES = 25
BROWSER_TIMEOUT = 60

# Pas de proxy pour les appels locaux : un proxy d'entreprise ferait echouer
# le test de disponibilite alors que le serveur tourne tres bien.
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# --------------------------------------------------------------------------- #
# Diagnostic
# --------------------------------------------------------------------------- #
def _safe(func, default="?"):
    try:
        return func()
    except Exception as exc:  # noqa: BLE001 - le diagnostic ne doit jamais planter
        return f"{type(exc).__name__}: {exc}"


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _module_version(name: str) -> str:
    try:
        module = importlib.import_module(name)
        return str(getattr(module, "__version__", "present"))
    except Exception as exc:  # noqa: BLE001
        return f"import impossible ({type(exc).__name__}: {exc})"


def _pip_version() -> str:
    try:
        result = subprocess.run([sys.executable, "-m", "pip", "--version"],
                                capture_output=True, text=True, errors="replace", timeout=30)
        if result.returncode == 0:
            return result.stdout.strip().splitlines()[0]
        return f"erreur: {result.stderr.strip()[:200]}"
    except Exception as exc:  # noqa: BLE001
        return f"indisponible ({exc})"


def _which(exe: str) -> str | None:
    bundled = RUNTIME / "ffmpeg" / "bin" / f"{exe}.exe"
    if bundled.exists():
        return str(bundled)
    found = shutil.which(exe)
    return found


def _exe_version(path: str) -> str:
    try:
        result = subprocess.run([path, "-version"], capture_output=True, text=True,
                                errors="replace", timeout=20)
        first = (result.stdout or result.stderr).strip().splitlines()
        return first[0][:120] if first else "pas de sortie"
    except Exception as exc:  # noqa: BLE001
        return f"ne s'execute pas ({exc})"


def _listening_ports(start: int = PORT_START, count: int = PORT_TRIES) -> dict[int, str | None]:
    """Ports deja occupes dans la plage, avec le PID quand c'est possible."""
    busy: dict[int, str | None] = {}
    for port in range(start, start + count):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.2)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            busy[port] = None
        finally:
            sock.close()

    if busy and os.name == "nt":
        try:
            # text/errors=replace : netstat peut contenir des octets non cp1252
            # sur un Windows non anglais, et l'etat est traduit (ECOUTE/LISTENING).
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                                 text=True, errors="replace", timeout=10).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) < 5:
                    continue
                try:
                    port = int(parts[1].rsplit(":", 1)[1])
                except (ValueError, IndexError):
                    continue
                if port in busy and parts[4].isdigit():
                    busy[port] = parts[4]
        except Exception:  # noqa: BLE001
            pass

    return busy


def _writable(directory: Path) -> str:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".clipstudio_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return "OK"
    except Exception as exc:  # noqa: BLE001
        return f"IMPOSSIBLE ({type(exc).__name__}: {exc})"


def build_diagnostic(reason: str, detail: str = "") -> str:
    now = dt.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    lines: list[str] = [
        "=" * 70,
        "CLIPSTUDIO - DIAGNOSTIC DE DEMARRAGE",
        f"Genere le : {now}",
        f"Raison    : {reason}",
        "=" * 70,
        "",
        "--- SYSTEME ---",
        f"OS                : {_safe(platform.platform)}",
        f"Architecture      : {_safe(platform.machine)}",
        f"Version Windows   : {_safe(lambda: platform.win32_ver()[0] or '?')}",
        "",
        "--- PYTHON ---",
        f"Version           : {sys.version.replace(chr(10), ' ')}",
        f"Executable        : {sys.executable}",
        f"Version minimale  : {MIN_PYTHON[0]}.{MIN_PYTHON[1]} "
        f"({'OK' if sys.version_info >= MIN_PYTHON else 'TROP ANCIENNE'})",
        f"Encodage sortie   : {getattr(sys.stdout, 'encoding', '?')}",
        "",
        "--- PROJET ---",
        f"Dossier projet    : {ROOT}",
        f"Dossier courant   : {_safe(os.getcwd)}",
    ]

    for label, path in (
        ("run.py", ROOT / "run.py"),
        ("start.bat", ROOT / "start.bat"),
        ("setup.ps1", ROOT / "setup.ps1"),
        ("LISEZMOI.txt", ROOT / "LISEZMOI.txt"),
        ("backend/app.py", BACKEND / "app.py"),
        ("backend/requirements.txt", BACKEND / "requirements.txt"),
        ("frontend/index.html", ROOT / "frontend" / "index.html"),
        ("frontend/app.js", ROOT / "frontend" / "app.js"),
        ("frontend/styles.css", ROOT / "frontend" / "styles.css"),
        (".runtime/python/python.exe", RUNTIME / "python" / "python.exe"),
        (".runtime/ffmpeg/bin/ffmpeg.exe", RUNTIME / "ffmpeg" / "bin" / "ffmpeg.exe"),
        (".runtime/ffmpeg/bin/ffprobe.exe", RUNTIME / "ffmpeg" / "bin" / "ffprobe.exe"),
    ):
        lines.append(f"{label:32}: {'OK' if path.exists() else 'MANQUANT'}")

    lines += ["", "--- DEPENDANCES PYTHON ---"]
    missing: list[str] = []
    for module in REQUIRED_MODULES:
        if _module_present(module):
            lines.append(f"{module:32}: OK ({_module_version(module)})")
        else:
            missing.append(module)
            lines.append(f"{module:32}: MANQUANT")
    for module in OPTIONAL_MODULES:
        state = "present" if _module_present(module) else "absent (optionnel, normal)"
        lines.append(f"{module:32}: {state}")
    lines.append(f"Modules manquants : {', '.join(missing) if missing else 'aucun'}")
    lines.append(f"pip               : {_safe(_pip_version)}")

    lines += ["", "--- FFMPEG ---"]
    for exe in ("ffmpeg", "ffprobe"):
        path = _which(exe)
        lines.append(f"{exe:32}: {path or 'INTROUVABLE'}")
        if path:
            lines.append(f"{'  version':32}: {_safe(lambda p=path: _exe_version(p))}")

    lines += ["", f"--- PORTS {PORT_START}-{PORT_START + PORT_TRIES - 1} ---"]
    busy = _listening_ports()
    if busy:
        for port, pid in sorted(busy.items()):
            lines.append(f"port {port:<6}                 : OCCUPE (pid {pid or '?'})")
    else:
        lines.append("aucun port occupe dans la plage")

    lines += ["", "--- ECRITURE DISQUE ---"]
    for sub in ("backend/data", "backend/data/uploads", "backend/data/outputs",
                "backend/data/thumbs"):
        lines.append(f"{sub:32}: {_safe(lambda p=BACKEND / sub.replace('/', os.sep): _writable(p))}")
    usage = _safe(lambda: shutil.disk_usage(ROOT))
    if isinstance(usage, tuple) or hasattr(usage, "free"):
        lines.append(f"Espace libre          : {usage.free // (1024 ** 3)} Go "  # type: ignore[union-attr]
                     f"sur {usage.total // (1024 ** 3)} Go")

    lines += ["", "--- ENVIRONNEMENT ---"]
    path_value = os.environ.get("PATH", "")
    lines.append(f"PATH (600 premiers)   : {path_value[:600]}")
    for key in ("CLIPSTUDIO_NO_BROWSER", "PYTHONUTF8", "PYTHONPATH", "HTTP_PROXY",
                "HTTPS_PROXY", "NO_PROXY"):
        if key in os.environ:
            lines.append(f"{key:22}: {os.environ[key][:200]}")

    if detail.strip():
        lines += ["", "--- DETAIL DE L'ERREUR ---", detail.strip()]

    lines += ["", "=" * 70, "Fin du diagnostic.", ""]
    # \n seulement : le mode texte de Python ajoute deja le \r\n sous Windows.
    return "\n".join(lines)


def write_diagnostic(reason: str, detail: str = "") -> Path:
    path = DIAG
    try:
        # utf-8-sig : accents lisibles dans le Bloc-notes Windows.
        path.write_text(build_diagnostic(reason, detail), encoding="utf-8-sig")
    except Exception as exc:  # noqa: BLE001
        fallback = RUNTIME / "diagnostic.txt"
        try:
            fallback.write_text(build_diagnostic(reason, detail + f"\n{exc}"),
                                encoding="utf-8-sig")
            return fallback
        except Exception:  # noqa: BLE001
            return path
    return path


def fail(message: str, detail: str = "") -> None:
    print()
    print(f"[ClipStudio] ECHEC : {message}")
    path = write_diagnostic(message, detail)
    print()
    print("[ClipStudio] Un fichier de diagnostic a ete cree :")
    print(f"             {path}")
    print("             Envoie-le tel quel : il contient tout ce qu'il faut pour corriger.")
    print()
    if sys.stdin is not None and sys.stdin.isatty():
        try:
            input("Appuie sur Entree pour fermer...")
        except (EOFError, KeyboardInterrupt):
            pass
    raise SystemExit(1)


# --------------------------------------------------------------------------- #
# Demarrage
# --------------------------------------------------------------------------- #
def free_port(start: int = PORT_START, tries: int = PORT_TRIES) -> int | None:
    for port in range(start, start + tries):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.2)
        try:
            sock.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            sock.close()
    return None


def ensure_dependencies() -> None:
    if all(_module_present(module) for module in REQUIRED_MODULES):
        return

    print("[ClipStudio] Dependances Python manquantes, installation (internet requis)...")
    requirements = BACKEND / "requirements.txt"
    if not requirements.exists():
        fail("backend/requirements.txt est introuvable : le dossier du projet est incomplet.")

    result = subprocess.run([sys.executable, "-m", "pip", "install",
                             "--disable-pip-version-check", "-q",
                             "--no-warn-script-location", "-r", str(requirements)],
                            capture_output=True, text=True, errors="replace")
    if result.returncode != 0:
        fail("Installation des dependances impossible.",
             "pip a renvoye une erreur :\n" + (result.stderr or "")[-2000:])

    still_missing = [m for m in REQUIRED_MODULES if not _module_present(m)]
    if still_missing:
        fail("Dependances toujours absentes apres installation : " + ", ".join(still_missing),
             "Supprime le dossier .runtime\\python puis relance start.bat.")


def server_ready(url: str, timeout: float = 2.0) -> bool:
    try:
        with _LOCAL_OPENER.open(f"{url}/api/health", timeout=timeout) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 - pas encore pret
        return False


def open_browser_when_ready(url: str, timeout: int = BROWSER_TIMEOUT) -> bool:
    """Ouvre le navigateur seulement quand le serveur repond vraiment.

    C'est ce qui evite la page « site inaccessible » : ouvrir trop tot donne
    une erreur de connexion, et l'utilisateur croit que tout est casse.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if server_ready(url):
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass
            print(f"[ClipStudio] Page ouverte : {url}")
            return True
        time.sleep(0.3)
    print("[ClipStudio] Le serveur repond trop lentement, ouverture du navigateur annulee.")
    print(f"             Ouvre cette adresse a la main : {url}")
    return False


def ffmpeg_ready() -> bool:
    return _which("ffmpeg") is not None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if "--diag" in args:
        path = write_diagnostic("Diagnostic demande manuellement (--diag).")
        print(f"[ClipStudio] Diagnostic ecrit : {path}")
        return 0

    no_browser = "--no-browser" in args or os.environ.get("CLIPSTUDIO_NO_BROWSER") == "1"

    if sys.version_info < MIN_PYTHON:
        fail(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} ou plus recent est requis "
             f"(version actuelle : {sys.version.split()[0]}).")
    if not (BACKEND / "app.py").exists():
        fail("backend/app.py est introuvable : lance start.bat depuis le dossier du projet.")
    if not (ROOT / "frontend" / "index.html").exists():
        fail("frontend/index.html est introuvable : le dossier du projet est incomplet.")

    ensure_dependencies()
    sys.path.insert(0, str(BACKEND))

    try:
        import app as backend_app  # noqa: F401
    except Exception:  # noqa: BLE001
        fail("Le serveur ClipStudio n'a pas pu etre charge.", traceback.format_exc())

    import uvicorn

    port = free_port()
    if port is None:
        fail(f"Aucun port libre entre {PORT_START} et {PORT_START + PORT_TRIES - 1}.",
             "Ports occupes : " + ", ".join(str(p) for p in _listening_ports()))

    url = f"http://127.0.0.1:{port}"

    if not ffmpeg_ready():
        print("[ClipStudio] Attention : ffmpeg est introuvable.")
        print("             L'import fonctionnera, la decoupe echouera.")
        print("             Relance setup.ps1 pour l'installer.")

    print()
    print(f"[ClipStudio] Adresse : {url}")
    print("             Garde cette fenetre ouverte. Ctrl+C pour arreter.")
    print()

    if not no_browser:
        threading.Thread(target=open_browser_when_ready, args=(url,), daemon=True).start()

    try:
        uvicorn.run(backend_app.app, host="127.0.0.1", port=port, log_level="info")
    except KeyboardInterrupt:
        print("\n[ClipStudio] Serveur arrete.")
        return 0
    except Exception:  # noqa: BLE001
        fail("Le serveur s'est arrete sur une erreur inattendue.", traceback.format_exc())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

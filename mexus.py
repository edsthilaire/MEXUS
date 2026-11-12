#!/usr/bin/env python3
"""
MEXUS Server Manager

Descarga, configura e inicia servidores de Minecraft Java: Paper, Fabric,
Vanilla y Forge. Sin dependencias externas (solo biblioteca estandar).

Uso rapido:
    python mexus.py                       # menu interactivo
    python mexus.py install               # descarga lo configurado
    python mexus.py start                 # inicia el servidor
    python mexus.py versions --type paper
    python mexus.py doctor                # diagnostico (Java + enlaces)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

if sys.version_info < (3, 9):  # pragma: no cover
    sys.exit("MEXUS necesita Python 3.9 o superior.")

# Evita que un caracter raro tire el programa en consolas antiguas.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

# --------------------------------------------------------------------------- #
# Constantes
# --------------------------------------------------------------------------- #

APP_NAME = "MEXUS Server Manager"
APP_VERSION = "3.1.0"
REPO_URL = "https://github.com/edsthilaire/mexus"
USER_AGENT = f"MEXUS-Server-Manager/{APP_VERSION} (+{REPO_URL})"

CONFIG_FILE = "mexus_config.json"
SERVER_JAR = "server.jar"
FORGE_INSTALLER = "forge-installer.jar"
BACKUP_DIR = "backups"
REQUEST_TIMEOUT = 30

# Fuentes de descarga (revisadas en noviembre de 2026).
# PaperMC apago la API v2 (api.papermc.io/v2); ahora es "Fill v3".
PAPER_API = "https://fill.papermc.io/v3/projects/paper"
FABRIC_META = "https://meta.fabricmc.net/v2"
MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"

SERVER_INFO: dict[str, str] = {
    "paper": "Plugins y buen rendimiento (recomendado)",
    "fabric": "Mods ligeros y rapidos",
    "vanilla": "Servidor oficial de Mojang",
    "forge": "Mods clasicos (Forge)",
}
SERVER_TYPES: tuple[str, ...] = tuple(SERVER_INFO)

RAM_PATTERN = re.compile(r"^[1-9][0-9]*[MGmg]$")
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
RELEASE_PATTERN = re.compile(r"^\d+(?:\.\d+){1,2}$")


# --------------------------------------------------------------------------- #
# Interfaz de terminal
# --------------------------------------------------------------------------- #


def _enable_windows_ansi() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # noqa: BLE001
        return False


def _supports_unicode() -> bool:
    encoding = (sys.stdout.encoding or "ascii").lower()
    try:
        "╭─╮│╰╯●✔✘▲❯█░".encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


_SYMBOLS_UNICODE = {
    "ok": "✔", "err": "✘", "warn": "▲", "info": "●", "prompt": "❯",
    "full": "█", "empty": "░", "tl": "╭", "tr": "╮", "bl": "╰", "br": "╯",
    "h": "─", "v": "│", "dash": "—",
}
_SYMBOLS_ASCII = {
    "ok": "+", "err": "x", "warn": "!", "info": "*", "prompt": ">",
    "full": "#", "empty": "-", "tl": "+", "tr": "+", "bl": "+", "br": "+",
    "h": "-", "v": "|", "dash": "-",
}

_LETTERS = [
    r"""
 __  __ 
|  \/  |
| |\/| |
| |  | |
|_|  |_|
""",
    r"""
 _____ 
| ____|
|  _|  
| |___ 
|_____|
""",
    r"""
__  __
\ \/ /
 \  / 
 /  \ 
/_/\_\
""",
    r"""
 _   _ 
| | | |
| | | |
| |_| |
 \___/ 
""",
    r"""
 ____  
/ ___| 
\___ \ 
 ___) |
|____/ 
""",
]


def _build_logo() -> list[str]:
    blocks = []
    for letter in _LETTERS:
        rows = letter.strip("\n").split("\n")
        width = max(len(row) for row in rows)
        blocks.append([row.ljust(width) for row in rows])
    return [" ".join(block[i] for block in blocks) for i in range(5)]


class UI:
    """Colores, paneles, barras de progreso y mensajes."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"

    COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ and _enable_windows_ansi()
    SYM = _SYMBOLS_UNICODE if _supports_unicode() else _SYMBOLS_ASCII

    # -- basicos ----------------------------------------------------------- #

    @classmethod
    def c(cls, text: str, *codes: str) -> str:
        if not cls.COLOR or not codes:
            return text
        return "".join(codes) + text + cls.RESET

    @classmethod
    def dim(cls, text: str) -> str:
        return cls.c(text, cls.DIM)

    @staticmethod
    def vlen(text: str) -> int:
        return len(ANSI_PATTERN.sub("", text))

    @classmethod
    def clear(cls) -> None:
        if sys.stdout.isatty():
            os.system("cls" if os.name == "nt" else "clear")

    @staticmethod
    def width() -> int:
        return shutil.get_terminal_size((100, 24)).columns

    # -- mensajes ---------------------------------------------------------- #

    @classmethod
    def success(cls, message: str) -> None:
        print(f"{cls.c(cls.SYM['ok'], cls.GREEN, cls.BOLD)} {message}")

    @classmethod
    def error(cls, message: str) -> None:
        print(f"{cls.c(cls.SYM['err'], cls.RED, cls.BOLD)} {message}")

    @classmethod
    def warning(cls, message: str) -> None:
        print(f"{cls.c(cls.SYM['warn'], cls.YELLOW, cls.BOLD)} {message}")

    @classmethod
    def info(cls, message: str) -> None:
        print(f"{cls.c(cls.SYM['info'], cls.CYAN, cls.BOLD)} {message}")

    @classmethod
    def heading(cls, message: str) -> None:
        print()
        print(cls.c(message, cls.CYAN, cls.BOLD))
        print(cls.dim(cls.SYM["h"] * min(cls.vlen(message) + 6, cls.width() - 1)))

    @classmethod
    def ask(cls, prompt: str, default: str = "") -> str:
        suffix = f" {cls.dim('[' + default + ']')}" if default else ""
        answer = input(f"{prompt}{suffix} {cls.c(cls.SYM['prompt'], cls.CYAN, cls.BOLD)} ").strip()
        return answer or default

    # -- paneles ----------------------------------------------------------- #

    @classmethod
    def panel(
        cls,
        lines: Iterable[str],
        title: str = "",
        color: str | None = None,
        min_width: int = 52,
    ) -> str:
        color = color or cls.CYAN
        sym = cls.SYM
        body = list(lines)
        inner = max([min_width, cls.vlen(title) + 2, *(cls.vlen(line) for line in body)])

        if title:
            label = f" {title} "
            fill = sym["h"] * (inner + 2 - 1 - cls.vlen(label))
            top = cls.c(sym["tl"] + sym["h"], color) + cls.c(label, cls.BOLD, color) + cls.c(fill + sym["tr"], color)
        else:
            top = cls.c(sym["tl"] + sym["h"] * (inner + 2) + sym["tr"], color)

        rows = []
        for line in body:
            pad = " " * (inner - cls.vlen(line))
            rows.append(f"{cls.c(sym['v'], color)} {line}{pad} {cls.c(sym['v'], color)}")
        bottom = cls.c(sym["bl"] + sym["h"] * (inner + 2) + sym["br"], color)
        return "\n".join([top, *rows, bottom])

    @classmethod
    def banner(cls) -> str:
        palette = (cls.CYAN, cls.CYAN, cls.BLUE, cls.BLUE, cls.MAGENTA)
        logo = [cls.c(row, cls.BOLD, palette[i]) for i, row in enumerate(_build_logo())]
        subtitle = cls.c("Minecraft Server Manager", cls.WHITE, cls.BOLD)
        tagline = cls.dim(f"v{APP_VERSION}  {cls.SYM['dash']}  Paper · Fabric · Vanilla · Forge")
        return "\n".join(["", *("  " + row for row in logo), "", f"  {subtitle}", f"  {tagline}"])


class Progress:
    """Barra de progreso para descargas (sin dependencias)."""

    def __init__(self, total: int, label: str) -> None:
        self.total = total
        self.label = label
        self.done = 0
        self.started = time.monotonic()
        self.last_draw = 0.0
        self.live = sys.stdout.isatty()

    def update(self, amount: int) -> None:
        self.done += amount
        now = time.monotonic()
        if self.live and now - self.last_draw >= 0.1:
            self.last_draw = now
            self.render()

    def render(self) -> None:
        mb = self.done / 1_048_576
        speed = mb / max(time.monotonic() - self.started, 1e-6)
        if self.total:
            frac = min(self.done / self.total, 1.0)
            width = max(10, min(30, UI.width() - 55))
            filled = int(width * frac)
            bar = UI.SYM["full"] * filled + UI.SYM["empty"] * (width - filled)
            text = (
                f"  {UI.c(bar, UI.CYAN)} {frac * 100:5.1f}%  "
                f"{mb:.1f}/{self.total / 1_048_576:.1f} MB  {speed:.1f} MB/s"
            )
        else:
            text = f"  {mb:.1f} MB  {speed:.1f} MB/s"
        sys.stdout.write("\r" + text + "   ")
        sys.stdout.flush()

    def finish(self) -> None:
        if self.live:
            self.render()
            print()
        else:
            print(f"  {self.label}: {self.done / 1_048_576:.1f} MB")


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #


def normalize_ram(value: str) -> str:
    value = value.strip().upper()
    if not RAM_PATTERN.match(value):
        raise ValueError("Usa un valor como 2G, 4096M o 8G.")
    return value


def ram_to_mb(value: str) -> int:
    value = normalize_ram(value)
    amount = int(value[:-1])
    return amount * 1024 if value.endswith("G") else amount


def version_key(value: str) -> tuple[int, int, int, tuple[int, ...]]:
    """Orden numerico: sirve para 1.21.4, 26.1.2 y Forge (1.21.4-54.1.13)."""
    minecraft_part, _, build_part = value.partition("-")
    parts = [int(p) for p in re.findall(r"\d+", minecraft_part)[:3]]
    while len(parts) < 3:
        parts.append(0)
    build = tuple(int(p) for p in re.findall(r"\d+", build_part)[:4])
    return (parts[0], parts[1], parts[2], build)


def required_java(mc_version: str) -> int | None:
    """Version minima de Java segun la version de Minecraft."""
    nums = [int(n) for n in re.findall(r"\d+", mc_version.partition("-")[0])]
    if not nums:
        return None
    if nums[0] >= 26:  # nueva numeracion 26.x -> Java 25
        return 25
    if nums[0] != 1 or len(nums) < 2:
        return None
    minor = nums[1]
    patch = nums[2] if len(nums) > 2 else 0
    if minor >= 21 or (minor == 20 and patch >= 5):
        return 21
    if minor >= 18:
        return 17
    if minor == 17:
        return 16
    return 8


def parse_java_major(text: str) -> int | None:
    match = re.search(r'version "(\d+)(?:\.(\d+))?', text)
    if match:
        major = int(match.group(1))
        if major == 1 and match.group(2):  # 1.8.0_xxx -> 8
            major = int(match.group(2))
        return major
    match = re.search(r"\b(?:openjdk|java)\s+(\d+)", text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def yes(value: str) -> bool:
    return value.strip().lower() in {"s", "si", "sí", "y", "yes"}


def short_path(path: Path, limit: int = 46) -> str:
    text = str(path)
    return text if len(text) <= limit else "…" + text[-(limit - 1):]


def network_error_message(url: str, exc: Exception) -> str:
    host = urlparse(url).netloc or url
    if isinstance(exc, HTTPError):
        if exc.code == 404:
            return f"{host} no encontró el recurso (HTTP 404). Revisa que la versión exista."
        if exc.code in (401, 403):
            return f"{host} rechazó la petición (HTTP {exc.code})."
        if exc.code == 429:
            return f"{host} recibió demasiadas peticiones (HTTP 429). Espera un momento."
        return f"{host} respondió con HTTP {exc.code}. Intenta de nuevo más tarde."
    if isinstance(exc, URLError):
        if isinstance(exc.reason, socket.gaierror):
            return f"No se pudo resolver {host}. Revisa tu internet o DNS."
        return f"No se pudo conectar con {host}: {exc.reason}"
    return f"No se pudo conectar con {host}: {exc}"


# --------------------------------------------------------------------------- #
# Configuracion
# --------------------------------------------------------------------------- #


class Config:
    DEFAULTS: dict[str, Any] = {
        "type": "paper",
        "version": "",
        "ram_min": "2G",
        "ram_max": "4G",
        "java_path": "java",
        "extra_jvm_args": "",
        "auto_backup": True,
        "backup_keep": 5,
        "installed_type": "",
        "installed_version": "",
    }

    def __init__(self, path: Path | str = CONFIG_FILE) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = dict(self.DEFAULTS)
        self.load()

    def load(self) -> None:
        self.data = dict(self.DEFAULTS)
        if not self.path.exists():
            return
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            UI.warning(f"No se pudo leer {self.path.name}. Se usará configuración nueva.")
            return
        if not isinstance(saved, dict):
            return
        for key, value in saved.items():
            default = self.DEFAULTS.get(key)
            if key in self.DEFAULTS and type(value) is type(default):
                self.data[key] = value
        for key in ("ram_min", "ram_max"):
            if not RAM_PATTERN.match(self.get_str(key)):
                self.data[key] = self.DEFAULTS[key]

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def get_str(self, key: str, default: str = "") -> str:
        value = self.data.get(key)
        return value if isinstance(value, str) else default

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.data.get(key)
        return value if isinstance(value, bool) else default

    def get_int(self, key: str, default: int = 0) -> int:
        value = self.data.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else default

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def update(self, **values: Any) -> None:
        self.data.update(values)
        self.save()

    def is_ready(self) -> bool:
        return self.get_str("type") in SERVER_TYPES and bool(self.get_str("version").strip())

    def mark_installed(self) -> None:
        self.update(installed_type=self.get_str("type"), installed_version=self.get_str("version"))

    def install_matches_config(self) -> bool:
        return (
            self.get_str("installed_type") == self.get_str("type")
            and self.get_str("installed_version") == self.get_str("version")
        )


# --------------------------------------------------------------------------- #
# Red
# --------------------------------------------------------------------------- #


class HttpClient:
    RETRY_CODES = (429, 500, 502, 503, 504)

    def __init__(self, retries: int = 3) -> None:
        self.retries = retries
        # PaperMC (Fill v3) exige un User-Agent identificable.
        self.headers = {"User-Agent": USER_AGENT, "Accept": "application/json, */*"}

    def _open(self, url: str, timeout: int = REQUEST_TIMEOUT, retries: int | None = None):
        attempts = retries or self.retries
        error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return urlopen(Request(url, headers=self.headers), timeout=timeout)
            except HTTPError as exc:
                error = exc
                if exc.code in self.RETRY_CODES and attempt < attempts:
                    time.sleep(attempt)
                    continue
                break
            except (URLError, TimeoutError, OSError) as exc:
                error = exc
                if attempt < attempts:
                    time.sleep(attempt)
                    continue
        assert error is not None
        raise RuntimeError(network_error_message(url, error)) from error

    def json(self, url: str) -> Any:
        with self._open(url) as response:
            payload = response.read().decode("utf-8")
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{urlparse(url).netloc} devolvió una respuesta no válida.") from exc

    def text(self, url: str) -> str:
        with self._open(url) as response:
            return response.read().decode("utf-8")

    def ping(self, url: str) -> tuple[bool, int, str]:
        started = time.monotonic()
        try:
            with self._open(url, timeout=10, retries=1) as response:
                response.read(1)
            return True, int((time.monotonic() - started) * 1000), "OK"
        except RuntimeError as exc:
            return False, int((time.monotonic() - started) * 1000), str(exc)

    def download(
        self,
        url: str,
        target: Path,
        *,
        label: str = "",
        sha256: str | None = None,
        sha1: str | None = None,
    ) -> Path:
        target = Path(target)
        part = target.with_name(target.name + ".part")
        part.unlink(missing_ok=True)

        hasher = hashlib.sha256() if sha256 else (hashlib.sha1() if sha1 else None)  # noqa: S324
        expected = (sha256 or sha1 or "").lower()

        try:
            with self._open(url) as response, part.open("wb") as handle:
                total = int(response.headers.get("Content-Length") or 0)
                bar = Progress(total, label or target.name)
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    if hasher:
                        hasher.update(chunk)
                    bar.update(len(chunk))
                bar.finish()
        except BaseException:
            part.unlink(missing_ok=True)
            raise

        if part.stat().st_size == 0:
            part.unlink(missing_ok=True)
            raise RuntimeError("La descarga llegó vacía.")
        if hasher and hasher.hexdigest().lower() != expected:
            part.unlink(missing_ok=True)
            algo = "SHA-256" if sha256 else "SHA-1"
            raise RuntimeError(f"La verificación {algo} falló: el archivo descargado está corrupto.")

        part.replace(target)
        return target


# --------------------------------------------------------------------------- #
# Descargas por tipo de servidor
# --------------------------------------------------------------------------- #


@dataclass
class Artifact:
    url: str
    filename: str
    sha256: str | None = None
    sha1: str | None = None
    sha1_url: str | None = None  # checksum opcional (Forge)
    note: str = ""


def _flatten_paper_versions(raw: Any) -> list[str]:
    """Fill v3 agrupa versiones: {"26.1": ["26.1.2", ...], "1.21": [...]}."""
    if isinstance(raw, dict):
        items: list[Any] = [v for group in raw.values() if isinstance(group, list) for v in group]
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    return [v for v in items if isinstance(v, str)]


class ServerDownloader:
    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient()

    # -- listados ---------------------------------------------------------- #

    def get_versions(self, server_type: str, include_unstable: bool = False) -> list[str]:
        server_type = server_type.lower()
        versions: list[str]

        if server_type == "paper":
            data = self.http.json(PAPER_API)
            raw = data.get("versions") if isinstance(data, dict) else None
            versions = _flatten_paper_versions(raw)
            if not include_unstable:
                versions = [v for v in versions if RELEASE_PATTERN.match(v)]

        elif server_type == "fabric":
            data = self.http.json(f"{FABRIC_META}/versions/game")
            versions = [
                item["version"]
                for item in data
                if isinstance(item, dict)
                and isinstance(item.get("version"), str)
                and (include_unstable or item.get("stable") is True)
            ]

        elif server_type == "vanilla":
            data = self.http.json(MOJANG_MANIFEST)
            versions = [
                item["id"]
                for item in data.get("versions", [])
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and (include_unstable or item.get("type") == "release")
            ]

        elif server_type == "forge":
            root = ElementTree.fromstring(self.http.text(f"{FORGE_MAVEN}/maven-metadata.xml"))
            versions = [
                node.text
                for node in root.findall("./versioning/versions/version")
                if node.text and "-" in node.text
            ]

        else:
            raise ValueError(f"Tipo de servidor no soportado: {server_type}")

        return sorted(set(versions), key=version_key, reverse=True)

    @staticmethod
    def forge_latest_per_minecraft(versions: Iterable[str]) -> list[str]:
        """Una entrada por version de Minecraft: la build de Forge mas nueva."""
        best: dict[str, str] = {}
        for version in versions:
            mc = version.partition("-")[0]
            if mc not in best or version_key(version) > version_key(best[mc]):
                best[mc] = version
        return sorted(best.values(), key=version_key, reverse=True)

    def resolve_version(self, server_type: str, version: str) -> str:
        """Convierte 'latest' (o '1.21.4' en Forge) en una version exacta."""
        server_type = server_type.lower()
        wanted = version.strip()

        if wanted.lower() in {"", "latest", "ultima", "última"}:
            versions = self.get_versions(server_type)
            if server_type == "forge":
                versions = self.forge_latest_per_minecraft(versions)
            if not versions:
                raise RuntimeError(f"No se encontraron versiones de {server_type}.")
            return versions[0]

        if server_type == "forge" and "-" not in wanted:
            candidates = [v for v in self.get_versions("forge") if v.partition("-")[0] == wanted]
            if not candidates:
                raise RuntimeError(f"Forge no tiene builds para Minecraft {wanted}.")
            return max(candidates, key=version_key)

        return wanted

    # -- planes de descarga ------------------------------------------------ #

    def plan(self, server_type: str, version: str) -> Artifact:
        server_type = server_type.lower()
        if server_type == "paper":
            return self._plan_paper(version)
        if server_type == "fabric":
            return self._plan_fabric(version)
        if server_type == "vanilla":
            return self._plan_vanilla(version)
        if server_type == "forge":
            return self._plan_forge(version)
        raise ValueError(f"Tipo de servidor no soportado: {server_type}")

    def _plan_paper(self, version: str) -> Artifact:
        data = self.http.json(f"{PAPER_API}/versions/{version}/builds")
        builds = data.get("builds") if isinstance(data, dict) else data
        builds = [b for b in (builds or []) if isinstance(b, dict) and isinstance(b.get("downloads"), dict)]
        if not builds:
            raise RuntimeError(f"Paper no tiene builds para {version}.")

        stable = [b for b in builds if str(b.get("channel", "")).upper() == "STABLE"]
        build = max(stable or builds, key=lambda b: int(b.get("id") or 0))
        channel = str(build.get("channel", "?")).upper()

        downloads = build["downloads"]
        entry = downloads.get("server:default") or next(iter(downloads.values()))
        url = entry.get("url")
        if not isinstance(url, str):
            raise RuntimeError(f"La build {build.get('id')} de Paper no trae enlace de descarga.")
        checksums = entry.get("checksums") or {}

        note = f"build {build.get('id')} ({channel})"
        if not stable:
            UI.warning(f"Paper {version} aún no tiene builds STABLE; se usa la más reciente ({channel}).")
        return Artifact(url, SERVER_JAR, sha256=checksums.get("sha256"), note=note)

    def _plan_fabric(self, version: str) -> Artifact:
        loaders = self.http.json(f"{FABRIC_META}/versions/loader/{version}")
        entries = [x["loader"] for x in loaders if isinstance(x, dict) and isinstance(x.get("loader"), dict)]
        if not entries:
            raise RuntimeError(f"Fabric no soporta Minecraft {version}.")
        loader = next((e for e in entries if e.get("stable")), entries[0])["version"]

        installers = self.http.json(f"{FABRIC_META}/versions/installer")
        installers = [i for i in installers if isinstance(i, dict) and isinstance(i.get("version"), str)]
        if not installers:
            raise RuntimeError("Fabric no devolvió versiones del instalador.")
        installer = next((i for i in installers if i.get("stable")), installers[0])["version"]

        # La URL del server.jar necesita las TRES versiones: juego/loader/installer.
        url = f"{FABRIC_META}/versions/loader/{version}/{loader}/{installer}/server/jar"
        return Artifact(url, SERVER_JAR, note=f"loader {loader}, installer {installer}")

    def _plan_vanilla(self, version: str) -> Artifact:
        manifest = self.http.json(MOJANG_MANIFEST)
        info = next((v for v in manifest.get("versions", []) if isinstance(v, dict) and v.get("id") == version), None)
        if not info or not isinstance(info.get("url"), str):
            raise RuntimeError(f"No se encontró Vanilla {version}.")
        package = self.http.json(info["url"])
        server = (package.get("downloads") or {}).get("server")
        if not isinstance(server, dict) or not isinstance(server.get("url"), str):
            raise RuntimeError(f"Vanilla {version} no publica server.jar.")
        return Artifact(server["url"], SERVER_JAR, sha1=server.get("sha1"))

    def _plan_forge(self, version: str) -> Artifact:
        url = f"{FORGE_MAVEN}/{version}/forge-{version}-installer.jar"
        return Artifact(url, FORGE_INSTALLER, sha1_url=url + ".sha1")

    def download(self, server_type: str, version: str, root: Path) -> Path:
        artifact = self.plan(server_type, version)
        sha1 = artifact.sha1
        if not sha1 and artifact.sha1_url:
            try:  # verificacion "mejor esfuerzo": si no hay checksum, se continúa
                sha1 = self.http.text(artifact.sha1_url).split()[0].strip()
            except (RuntimeError, IndexError):
                sha1 = None

        suffix = f"  {UI.dim('(' + artifact.note + ')')}" if artifact.note else ""
        UI.info(f"Descargando {server_type} {version}{suffix}")
        return self.http.download(
            artifact.url,
            Path(root) / artifact.filename,
            label=artifact.filename,
            sha256=artifact.sha256,
            sha1=sha1,
        )


# --------------------------------------------------------------------------- #
# Administrador del servidor
# --------------------------------------------------------------------------- #


class ServerManager:
    BACKUP_FILES = (
        "server.properties", "whitelist.json", "ops.json",
        "banned-ips.json", "banned-players.json",
    )

    def __init__(self, root: Path | str = ".", downloader: ServerDownloader | None = None) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = Config(self.root / CONFIG_FILE)
        self.downloader = downloader or ServerDownloader()
        self.process: subprocess.Popen[bytes] | None = None

    # -- rutas ------------------------------------------------------------- #

    @property
    def server_jar(self) -> Path:
        return self.root / SERVER_JAR

    @property
    def forge_installer(self) -> Path:
        return self.root / FORGE_INSTALLER

    def minecraft_version(self) -> str:
        return self.config.get_str("version").partition("-")[0]

    def forge_args_file(self) -> Path:
        name = "win_args.txt" if os.name == "nt" else "unix_args.txt"
        return self.root / "libraries" / "net" / "minecraftforge" / "forge" / self.config.get_str("version") / name

    def files_present(self) -> bool:
        if self.config.get_str("type") == "forge":
            return (
                self.forge_args_file().exists()
                or (self.root / "run.bat").exists()
                or (self.root / "run.sh").exists()
                or bool(self.legacy_forge_jars())
            )
        return self.server_jar.exists()

    def legacy_forge_jars(self) -> list[Path]:
        return sorted(p for p in self.root.glob("forge-*.jar") if "installer" not in p.name)

    # -- Java -------------------------------------------------------------- #

    def java_executable(self) -> str | None:
        java = self.config.get_str("java_path", "java")
        if shutil.which(java) or Path(java).exists():
            return java
        return None

    def java_version(self) -> tuple[int | None, str]:
        java = self.java_executable()
        if java is None:
            return None, ""
        try:
            done = subprocess.run(
                [java, "-version"], cwd=self.root, capture_output=True, text=True, timeout=20
            )
        except (OSError, subprocess.SubprocessError):
            return None, ""
        output = (done.stderr or done.stdout).strip()
        first = output.splitlines()[0] if output else ""
        return parse_java_major(output), first

    def check_java(self, quiet: bool = False) -> bool:
        needed = required_java(self.minecraft_version()) if self.config.is_ready() else None
        if self.java_executable() is None:
            UI.error("No se encontró Java.")
            self._java_hint(needed)
            return False

        major, banner = self.java_version()
        if not quiet and banner:
            UI.info(banner)
        if major is None:
            UI.warning("No pude leer la versión de Java; se intentará continuar.")
            return True
        if needed and major < needed:
            UI.error(f"Minecraft {self.minecraft_version()} necesita Java {needed}+, pero tienes Java {major}.")
            self._java_hint(needed)
            return False
        return True

    @staticmethod
    def _java_hint(needed: int | None) -> None:
        version = needed or 25
        print(f"  Instala Java {version}: https://adoptium.net/temurin/releases/?version={version}")
        print("  Si ya lo tienes instalado, indica su ruta con: python mexus.py config --java RUTA")

    # -- instalacion ------------------------------------------------------- #

    def ensure_eula(self) -> bool:
        eula = self.root / "eula.txt"
        if eula.exists() and "eula=true" in eula.read_text(encoding="utf-8", errors="ignore").lower():
            return True
        UI.warning("Debes aceptar la EULA de Minecraft para iniciar el servidor.")
        print(f"  {UI.dim('https://aka.ms/MinecraftEULA')}")
        if not yes(UI.ask("¿Aceptas la EULA? (s/N)")):
            UI.error("No se inició el servidor porque la EULA no fue aceptada.")
            return False
        eula.write_text("eula=true\n", encoding="utf-8")
        UI.success("EULA aceptada (eula.txt).")
        return True

    def jvm_args(self) -> list[str]:
        extra = self.config.get_str("extra_jvm_args").strip()
        return [
            f"-Xms{self.config.get_str('ram_min')}",
            f"-Xmx{self.config.get_str('ram_max')}",
            "-XX:+UseG1GC",
            *(shlex.split(extra, posix=os.name != "nt") if extra else []),
        ]

    def write_forge_jvm_args(self) -> None:
        (self.root / "user_jvm_args.txt").write_text("\n".join(self.jvm_args()) + "\n", encoding="utf-8")

    def install_forge(self) -> bool:
        java = self.java_executable()
        if java is None:
            UI.error("Forge necesita Java para instalarse.")
            return False

        self.downloader.download("forge", self.config.get_str("version"), self.root)
        UI.info("Instalando Forge (puede tardar unos minutos)...")
        done = subprocess.run([java, "-jar", str(self.forge_installer), "--installServer"], cwd=self.root)
        if done.returncode != 0:
            UI.error("Forge no se pudo instalar.")
            return False

        self.forge_installer.unlink(missing_ok=True)
        (self.root / f"{FORGE_INSTALLER}.log").unlink(missing_ok=True)
        self.write_forge_jvm_args()
        self.config.mark_installed()
        UI.success("Forge instalado correctamente.")
        return True

    def ensure_server_files(self, force: bool = False) -> bool:
        self.config.load()
        if not self.config.is_ready():
            UI.error("Falta configurar tipo y versión del servidor.")
            return False

        server_type = self.config.get_str("type")
        try:
            resolved = self.downloader.resolve_version(server_type, self.config.get_str("version"))
            if resolved != self.config.get_str("version"):
                UI.info(f"Versión resuelta: {resolved}")
                self.config.update(version=resolved, installed_type="", installed_version="")

            up_to_date = self.files_present() and self.config.install_matches_config() and not force
            if up_to_date:
                if server_type == "forge":
                    self.write_forge_jvm_args()
                return True

            if server_type == "forge":
                return self.install_forge()

            self.downloader.download(server_type, self.config.get_str("version"), self.root)
            self.config.mark_installed()
            UI.success(f"{server_type} {self.config.get_str('version')} listo.")
            return True
        except Exception as exc:  # noqa: BLE001
            UI.error(f"No se pudo preparar el servidor: {exc}")
            return False

    # -- ejecucion --------------------------------------------------------- #

    def build_command(self) -> list[str]:
        server_type = self.config.get_str("type")
        java = self.config.get_str("java_path", "java")

        if server_type == "forge":
            self.write_forge_jvm_args()
            args_file = self.forge_args_file()
            if args_file.exists():  # Forge moderno (1.17+)
                return [java, "@user_jvm_args.txt", f"@{args_file.relative_to(self.root).as_posix()}", "nogui"]
            legacy = self.legacy_forge_jars()
            if legacy:  # Forge antiguo
                return [java, *self.jvm_args(), "-jar", legacy[-1].name, "nogui"]
            if os.name == "nt" and (self.root / "run.bat").exists():
                return ["cmd.exe", "/c", "run.bat", "nogui"]
            if (self.root / "run.sh").exists():
                return ["sh", "run.sh", "nogui"]
            raise RuntimeError("No se encontraron los archivos de arranque de Forge.")

        return [java, *self.jvm_args(), "-jar", SERVER_JAR, "nogui"]

    def start(self) -> bool:
        self.config.load()
        if not self.config.is_ready():
            UI.error("Primero configura el tipo y la versión del servidor.")
            return False
        if not self.check_java():
            return False
        if not self.ensure_server_files():
            return False
        if not self.ensure_eula():
            return False
        if self.config.get_bool("auto_backup", True):
            self.create_backup(automatic=True)

        try:
            command = self.build_command()
        except RuntimeError as exc:
            UI.error(str(exc))
            return False

        UI.success(
            f"Iniciando {self.config.get_str('type')} {self.config.get_str('version')} "
            f"({self.config.get_str('ram_min')}–{self.config.get_str('ram_max')} de RAM)"
        )
        UI.info("La consola del servidor es esta misma ventana. Escribe 'stop' o pulsa Ctrl+C para apagar.")
        print()

        interrupted = False
        try:
            # stdin se hereda: puedes escribir comandos directamente al servidor.
            self.process = subprocess.Popen(command, cwd=self.root)
            self.process.wait()
        except KeyboardInterrupt:
            interrupted = True
            print()
            UI.warning("Ctrl+C detectado: esperando a que el servidor guarde el mundo...")
        except OSError as exc:
            UI.error(f"No se pudo iniciar el servidor: {exc}")
            return False
        finally:
            self.stop_gracefully()

        code = self.process.returncode if self.process else 1
        print()
        if interrupted or code == 0:
            UI.success("Servidor detenido.")
            return True
        UI.error(f"El servidor terminó con código {code}. Revisa los mensajes de arriba.")
        return False

    def stop_gracefully(self, timeout: int = 60) -> None:
        proc = self.process
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            UI.warning("El servidor no se detuvo a tiempo. Cerrando el proceso...")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    # -- backups ----------------------------------------------------------- #

    def level_name(self) -> str:
        props = self.root / "server.properties"
        if props.exists():
            for line in props.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("level-name="):
                    return line.split("=", 1)[1].strip() or "world"
        return "world"

    def create_backup(self, automatic: bool = False) -> Path | None:
        level = self.level_name()
        names = [level, f"{level}_nether", f"{level}_the_end", *self.BACKUP_FILES]
        existing = [self.root / n for n in names if (self.root / n).exists()]
        if not existing:
            if not automatic:
                UI.info("Todavía no hay mundo ni archivos de servidor para respaldar.")
            return None

        backups = self.root / BACKUP_DIR
        backups.mkdir(exist_ok=True)
        prefix = "auto" if automatic else "manual"
        stamp = f"{prefix}-{datetime.now():%Y%m%d-%H%M%S}"
        path = backups / f"{stamp}.zip"
        counter = 2
        while path.exists():
            path = backups / f"{stamp}_{counter}.zip"
            counter += 1

        UI.info(f"Creando backup: {path.name}")
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in existing:
                if item.is_dir():
                    for child in item.rglob("*"):
                        if child.is_file():
                            archive.write(child, child.relative_to(self.root))
                else:
                    archive.write(item, item.relative_to(self.root))

        size = path.stat().st_size / 1_048_576
        UI.success(f"Backup guardado: {path.name} ({size:.1f} MB)")
        self.prune_backups()
        return path

    def prune_backups(self) -> None:
        keep = self.config.get_int("backup_keep", 5)
        if keep <= 0:
            return
        # Se ordena por fecha de modificacion (no por nombre) para borrar siempre los mas viejos.
        autos = sorted((self.root / BACKUP_DIR).glob("auto-*.zip"), key=lambda p: p.stat().st_mtime_ns)
        for old in autos[:-keep]:
            old.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Pantallas
# --------------------------------------------------------------------------- #


def print_status(manager: ServerManager) -> None:
    config = manager.config
    ready = config.is_ready()
    badge = UI.c(f"{UI.SYM['ok']} Configurado", UI.GREEN, UI.BOLD) if ready else UI.c(
        f"{UI.SYM['warn']} Falta configurar", UI.YELLOW, UI.BOLD)

    server = f"{config.get_str('type')} {config.get_str('version') or '-'}"
    if ready and manager.files_present() and config.install_matches_config():
        installed = UI.c("instalado", UI.GREEN)
    elif ready:
        installed = UI.c("se descargará al iniciar", UI.YELLOW)
    else:
        installed = UI.dim("-")

    label = lambda text: UI.dim(f"{text:<10}")  # noqa: E731
    backups = (
        f"activados (conserva {config.get_int('backup_keep', 5)})"
        if config.get_bool("auto_backup", True) else "desactivados"
    )
    lines = [
        f"{label('Estado')}{badge}",
        f"{label('Servidor')}{UI.c(server, UI.WHITE, UI.BOLD)}  {installed}",
        f"{label('RAM')}{config.get_str('ram_min')} – {config.get_str('ram_max')}",
        f"{label('Java')}{config.get_str('java_path')}",
        f"{label('Backups')}{backups}",
        f"{label('Carpeta')}{short_path(manager.root)}",
    ]
    print(UI.panel(lines, title="Servidor"))


def pause() -> None:
    try:
        input(UI.dim("\nPresiona Enter para continuar..."))
    except EOFError:
        pass


def print_columns(items: list[str], columns: int = 4) -> None:
    width = max((len(v) for v in items), default=8) + 6
    for start in range(0, len(items), columns):
        row = items[start:start + columns]
        print("  " + "".join(
            f"{UI.c(f'{start + i + 1:>2}', UI.CYAN)} {v:<{width - 3}}" for i, v in enumerate(row)
        ))


def choose_server_type(manager: ServerManager) -> None:
    config = manager.config
    UI.heading("Tipo de servidor")
    for index, (name, desc) in enumerate(SERVER_INFO.items(), start=1):
        print(f"  {UI.c(str(index), UI.CYAN, UI.BOLD)}  {name:<8} {UI.dim(desc)}")
    print()

    current = config.get_str("type", "paper")
    raw = UI.ask("Tipo (número o nombre)", default=current).lower()
    if raw.isdigit():
        index = int(raw) - 1
        if not 0 <= index < len(SERVER_TYPES):
            UI.error("Número inválido.")
            return
        server_type = SERVER_TYPES[index]
    else:
        server_type = raw
    if server_type not in SERVER_TYPES:
        UI.error("Tipo inválido. Usa: " + ", ".join(SERVER_TYPES))
        return

    versions: list[str] = []
    try:
        UI.info("Consultando versiones disponibles...")
        versions = manager.downloader.get_versions(server_type)
        if server_type == "forge":
            versions = manager.downloader.forge_latest_per_minecraft(versions)
    except Exception as exc:  # noqa: BLE001
        UI.warning(str(exc))
        UI.info("Puedes escribir la versión a mano (por ejemplo 1.21.4 o 26.1.2).")

    default = versions[0] if versions else ""
    if versions:
        UI.heading("Versiones recientes")
        print_columns(versions[:24])
        print(UI.dim("\n  Escribe un número, una versión exacta o 'latest'. Enter = la más reciente."))

    raw_version = UI.ask("Versión", default=default)
    if not raw_version:
        UI.error("La versión no puede estar vacía.")
        return
    if raw_version.isdigit() and versions:
        index = int(raw_version) - 1
        if not 0 <= index < len(versions):
            UI.error("Número de versión inválido.")
            return
        version = versions[index]
    else:
        version = raw_version

    try:
        version = manager.downloader.resolve_version(server_type, version)
    except Exception as exc:  # noqa: BLE001
        UI.warning(str(exc))

    config.update(type=server_type, version=version, installed_type="", installed_version="")
    UI.success(f"Configurado: {server_type} {version}")
    needed = required_java(version.partition("-")[0])
    if needed:
        UI.info(f"Esta versión necesita Java {needed} o superior.")


def configure_ram(manager: ServerManager) -> None:
    config = manager.config
    UI.heading("Memoria RAM")
    print(f"  {UI.c('1', UI.CYAN, UI.BOLD)}  2G – 4G   {UI.dim('servidor pequeño')}")
    print(f"  {UI.c('2', UI.CYAN, UI.BOLD)}  4G – 8G   {UI.dim('servidor mediano')}")
    print(f"  {UI.c('3', UI.CYAN, UI.BOLD)}  Personalizado")
    print()
    choice = UI.ask("Opción")
    try:
        if choice == "1":
            ram_min, ram_max = "2G", "4G"
        elif choice == "2":
            ram_min, ram_max = "4G", "8G"
        elif choice == "3":
            ram_min = normalize_ram(UI.ask("RAM mínima (ej. 2G)"))
            ram_max = normalize_ram(UI.ask("RAM máxima (ej. 4G)"))
        else:
            UI.error("Opción inválida.")
            return
        if ram_to_mb(ram_min) > ram_to_mb(ram_max):
            UI.error("La RAM mínima no puede ser mayor que la máxima.")
            return
    except ValueError as exc:
        UI.error(str(exc))
        return
    config.update(ram_min=ram_min, ram_max=ram_max)
    UI.success(f"RAM configurada: {ram_min} – {ram_max}")


def configure_java(manager: ServerManager) -> None:
    current = manager.config.get_str("java_path", "java")
    value = UI.ask("Ruta de Java", default=current)
    manager.config.set("java_path", value)
    UI.success(f"Java configurado: {value}")
    manager.check_java()


def toggle_backup(manager: ServerManager) -> None:
    new_value = not manager.config.get_bool("auto_backup", True)
    manager.config.set("auto_backup", new_value)
    UI.success(f"Backup automático: {'activado' if new_value else 'desactivado'}")


def doctor(manager: ServerManager) -> bool:
    UI.heading("Diagnóstico")
    ok_all = True
    sym = UI.SYM

    def line(ok: bool, label: str, detail: str = "") -> None:
        nonlocal ok_all
        ok_all = ok_all and ok
        mark = UI.c(sym["ok"], UI.GREEN, UI.BOLD) if ok else UI.c(sym["err"], UI.RED, UI.BOLD)
        print(f"  {mark} {label:<18}{UI.dim(detail)}")

    line(True, "Python", sys.version.split()[0])
    line(True, "Carpeta", short_path(manager.root, 60))

    major, banner = manager.java_version()
    needed = required_java(manager.minecraft_version()) if manager.config.is_ready() else None
    if manager.java_executable() is None:
        line(False, "Java", "no encontrado")
    elif needed and major is not None and major < needed:
        line(False, "Java", f"v{major}, pero esta versión pide {needed}+")
    else:
        line(True, "Java", banner or "detectado")

    print()
    for name, url in (
        ("Paper (Fill v3)", PAPER_API),
        ("Fabric Meta", f"{FABRIC_META}/versions/installer"),
        ("Mojang", MOJANG_MANIFEST),
        ("Forge Maven", f"{FORGE_MAVEN}/maven-metadata.xml"),
    ):
        ok, ms, detail = manager.downloader.http.ping(url)
        line(ok, name, f"{ms} ms" if ok else detail)

    print()
    if manager.config.is_ready():
        UI.success("Configuración lista.")
    else:
        UI.warning("Configura tipo y versión antes de iniciar.")
    return ok_all


MENU = (
    ("1", "Iniciar servidor", "descarga lo que falte"),
    ("2", "Cambiar tipo / versión", ""),
    ("3", "Configurar RAM", ""),
    ("4", "Instalar / actualizar archivos", "fuerza una descarga nueva"),
    ("5", "Crear backup manual", ""),
    ("6", "Cambiar ruta de Java", ""),
    ("7", "Backup automático on/off", ""),
    ("8", "Diagnóstico", "Java y enlaces de descarga"),
    ("0", "Salir", ""),
)


def interactive_menu(manager: ServerManager) -> None:
    actions = {
        "1": manager.start,
        "2": lambda: choose_server_type(manager),
        "3": lambda: configure_ram(manager),
        "4": lambda: manager.ensure_server_files(force=True),
        "5": lambda: manager.create_backup(automatic=False),
        "6": lambda: configure_java(manager),
        "7": lambda: toggle_backup(manager),
        "8": lambda: doctor(manager),
    }
    while True:
        manager.config.load()
        UI.clear()
        print(UI.banner())
        print()
        print_status(manager)
        print()
        for key, label, hint in MENU:
            print(f"  {UI.c(key, UI.CYAN, UI.BOLD)}  {label}  {UI.dim(hint)}".rstrip())
        print()

        try:
            choice = UI.ask("Selecciona una opción")
            print()
            if choice == "0":
                UI.success("Hasta luego.")
                return
            action = actions.get(choice)
            if action is None:
                UI.error("Opción inválida.")
                time.sleep(1)
                continue
            action()
            pause()
        except (EOFError, KeyboardInterrupt):
            print()
            return


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mexus.py",
        description="Descarga, configura e inicia servidores de Minecraft Java.",
    )
    parser.add_argument("-d", "--dir", default=".", help="carpeta del servidor (por defecto: la actual)")
    parser.add_argument("--no-color", action="store_true", help="desactiva los colores")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("start", help="inicia el servidor configurado")
    install = sub.add_parser("install", help="descarga e instala los archivos del servidor")
    install.add_argument("--force", action="store_true", help="descarga de nuevo aunque ya exista")
    sub.add_parser("update", help="descarga de nuevo la última build de la versión configurada")
    sub.add_parser("backup", help="crea un backup manual")
    sub.add_parser("status", help="muestra la configuración actual")
    sub.add_parser("doctor", help="revisa Java, configuración y enlaces de descarga")

    versions = sub.add_parser("versions", help="muestra las versiones disponibles")
    versions.add_argument("--type", choices=SERVER_TYPES, default="paper")
    versions.add_argument("--limit", type=int, default=25)
    versions.add_argument("--all", action="store_true", help="incluye snapshots / todas las builds")

    config = sub.add_parser("config", help="actualiza la configuración")
    config.add_argument("--type", choices=SERVER_TYPES)
    config.add_argument("--version", help="versión exacta o 'latest'")
    config.add_argument("--ram-min")
    config.add_argument("--ram-max")
    config.add_argument("--java")
    config.add_argument("--jvm-args", help="argumentos JVM extra, entre comillas")
    config.add_argument("--auto-backup", choices=("on", "off"))
    config.add_argument("--backup-keep", type=int, help="cuántos backups automáticos conservar")
    return parser


def apply_config_args(manager: ServerManager, args: argparse.Namespace) -> int:
    config = manager.config
    updates: dict[str, Any] = {}

    new_type = args.type or config.get_str("type")
    if args.type:
        updates["type"] = args.type
    if args.version:
        updates["version"] = manager.downloader.resolve_version(new_type, args.version)
    if args.type or args.version:
        updates["installed_type"] = ""
        updates["installed_version"] = ""

    ram_min = normalize_ram(args.ram_min) if args.ram_min else config.get_str("ram_min")
    ram_max = normalize_ram(args.ram_max) if args.ram_max else config.get_str("ram_max")
    if args.ram_min or args.ram_max:
        if ram_to_mb(ram_min) > ram_to_mb(ram_max):
            UI.error("La RAM mínima no puede ser mayor que la máxima.")
            return 1
        updates.update(ram_min=ram_min, ram_max=ram_max)

    if args.java:
        updates["java_path"] = args.java
    if args.jvm_args is not None:
        updates["extra_jvm_args"] = args.jvm_args
    if args.auto_backup:
        updates["auto_backup"] = args.auto_backup == "on"
    if args.backup_keep is not None:
        updates["backup_keep"] = max(args.backup_keep, 0)

    if updates:
        config.update(**updates)
        UI.success("Configuración actualizada.")
    print_status(manager)
    return 0


def run_cli(args: argparse.Namespace) -> int:
    manager = ServerManager(args.dir)
    command = args.command

    if command == "start":
        return 0 if manager.start() else 1
    if command == "install":
        return 0 if manager.ensure_server_files(force=args.force) else 1
    if command == "update":
        return 0 if manager.ensure_server_files(force=True) else 1
    if command == "backup":
        manager.create_backup(automatic=False)
        return 0
    if command == "status":
        print_status(manager)
        return 0
    if command == "doctor":
        return 0 if doctor(manager) else 1
    if command == "versions":
        versions = manager.downloader.get_versions(args.type, include_unstable=args.all)
        if args.type == "forge" and not args.all:
            versions = manager.downloader.forge_latest_per_minecraft(versions)
        for version in versions[: max(args.limit, 1)]:
            print(version)
        return 0
    if command == "config":
        return apply_config_args(manager, args)

    interactive_menu(manager)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.no_color:
        UI.COLOR = False
    try:
        return run_cli(args)
    except KeyboardInterrupt:
        print()
        UI.warning("Operación cancelada.")
        return 130
    except Exception as exc:  # noqa: BLE001
        UI.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

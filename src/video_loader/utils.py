from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlparse


INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str, fallback: str = "download") -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("_", name.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or fallback


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def filename_from_url(url: str, fallback: str = "download.bin") -> str:
    parsed = urlparse(url)
    name = unquote(Path(parsed.path).name)
    return safe_filename(name, fallback)


def parse_headers(text: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"请求头格式无效：{raw_line}")
        key, value = line.split(":", 1)
        headers[key.strip()] = value.strip()
    return headers


def parse_cookies(text: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for chunk in text.replace("\n", ";").split(";"):
        item = chunk.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"Cookie 格式无效：{item}")
        key, value = item.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies

"""网页嗅探：给一个网页地址，找出里面真正的 m3u8 视频地址和可选清晰度。

两种入口，按代价从小到大：
1. 直接抓页面 HTML（走指纹伪装会话），扫描 m3u8 链接，必要时跟进一层 iframe。
2. 页面被 Cloudflare 之类挡住、或者地址是播放器运行时才拼出来的，就启动本机 Chrome
   抓 Network 请求（见 services/browser_capture.py）。
"""
from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import urljoin, urlparse

from video_loader.models import DownloadTask, HttpSession, LogCallback, SniffResult, StreamVariant, label_for_resolution
from video_loader.services.browser_capture import capture_media_urls
from video_loader.services.http_client import build_session, default_referer, first_header


ABSOLUTE_M3U8_PATTERN = re.compile(r"""https?://[^\s"'<>()\\]+?\.m3u8[^\s"'<>()\\]*""", re.IGNORECASE)
QUOTED_URL_PATTERN = re.compile(r"""["'`]([^\s"'`<>()]*\.m3u8[^\s"'`<>()]*)["'`]""", re.IGNORECASE)
# 没有引号的相对路径（href=video.m3u8、src=hls/master.m3u8）；lookbehind 避免从绝对 URL 里截出相对路径。
BARE_URL_PATTERN = re.compile(
    r"""(?<![\w/.%:-])([A-Za-z0-9_%.\-]+(?:/[A-Za-z0-9_%.\-]+)*\.m3u8(?:\?[^\s"'<>()]*)?)""",
    re.IGNORECASE,
)
IFRAME_PATTERN = re.compile(r"""<iframe[^>]+src=["']([^"']+)["']""", re.IGNORECASE)
TITLE_PATTERN = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
STREAM_INF_PATTERN = re.compile(r"^#EXT-X-STREAM-INF:(?P<attrs>.*)$", re.IGNORECASE)
ATTRIBUTE_PATTERN = re.compile(r"""([A-Za-z0-9-]+)=("[^"]*"|[^,]*)""")
QUALITY_DIR_PATTERN = re.compile(r"^(?P<label>\d{3,4}p)$", re.IGNORECASE)
CHALLENGE_MARKERS = (
    "just a moment",
    "challenges.cloudflare.com",
    "cf-chl",
    "checking your browser",
    "attention required",
    "请稍候",
)

MAX_PLAYLIST_PROBES = 8
MAX_IFRAMES = 3


def unescape_url_text(text: str) -> str:
    """页面里的 URL 常被 JS 转义：https:\\/\\/host\\/a.m3u8。"""
    return text.replace("\\/", "/").replace("&amp;", "&").replace("\\u002F", "/").replace("\\u002f", "/")


def find_m3u8_urls(text: str, base_url: str) -> list[str]:
    """从任意文本（HTML / JS / JSON）里找出 m3u8 地址，保持出现顺序并去重。"""
    if not text:
        return []
    normalized = unescape_url_text(text)
    candidates: list[str] = []

    for raw in ABSOLUTE_M3U8_PATTERN.findall(normalized):
        candidates.append(raw)
    for raw in QUOTED_URL_PATTERN.findall(normalized):
        if raw.startswith("http://") or raw.startswith("https://"):
            continue
        candidates.append(urljoin(base_url, raw))
    for raw in BARE_URL_PATTERN.findall(normalized):
        if raw.startswith("http://") or raw.startswith("https://"):
            continue
        candidates.append(urljoin(base_url, raw))

    seen: set[str] = set()
    urls: list[str] = []
    for candidate in candidates:
        url = candidate.strip().rstrip("\\")
        if not urlparse(url).path.lower().endswith(".m3u8"):
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def parse_master_playlist(text: str, base_url: str) -> list[StreamVariant]:
    """解析主播放列表中的多档清晰度；媒体播放列表（没有 EXT-X-STREAM-INF）返回空列表。"""
    lines = text.splitlines()
    variants: list[StreamVariant] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        match = STREAM_INF_PATTERN.match(line)
        if not match:
            index += 1
            continue

        attributes = {key.upper(): value.strip().strip('"') for key, value in ATTRIBUTE_PATTERN.findall(match.group("attrs"))}
        uri = ""
        probe = index + 1
        while probe < len(lines):
            candidate = lines[probe].strip()
            if candidate and not candidate.startswith("#"):
                uri = candidate
                break
            probe += 1
        if not uri:
            index += 1
            continue

        resolution = attributes.get("RESOLUTION", "")
        variants.append(
            StreamVariant(
                url=urljoin(base_url, uri),
                label=label_for_resolution(resolution),
                resolution=resolution,
                bandwidth=_to_int(attributes.get("BANDWIDTH", "")),
                average_bandwidth=_to_int(attributes.get("AVERAGE-BANDWIDTH", "")),
                codecs=attributes.get("CODECS", ""),
                source=base_url,
            )
        )
        index = probe + 1
    return variants


def _to_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def playlist_quality_label(url: str) -> str:
    """从 .../480p/video.m3u8 这样的路径里取出 480p。"""
    parts = [part for part in urlparse(url).path.split("/") if part]
    for part in reversed(parts[:-1]):
        match = QUALITY_DIR_PATTERN.match(part)
        if match:
            return match.group("label").lower()
    return ""


def looks_like_challenge(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


def page_title(text: str) -> str:
    match = TITLE_PATTERN.search(text or "")
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def sort_variants(variants: list[StreamVariant]) -> list[StreamVariant]:
    """清晰度从高到低，界面里第一项就是最佳画质。"""
    return sorted(variants, key=lambda item: (item.height, item.bandwidth, item.average_bandwidth), reverse=True)


def describe_variants(variants: list[StreamVariant]) -> list[tuple[str, StreamVariant]]:
    """生成界面下拉框用的标签（重名时加序号），返回 (标签, 流)。"""
    labelled: list[tuple[str, StreamVariant]] = []
    used: dict[str, int] = {}
    for variant in variants:
        base = variant.describe()
        used[base] = used.get(base, 0) + 1
        label = base if used[base] == 1 else f"{base} #{used[base]}"
        labelled.append((label, variant))
    return labelled


def pick_variant(variants: list[StreamVariant], preferred: str = "") -> StreamVariant | None:
    """按偏好挑一档：先精确匹配标签（1080p），再匹配分辨率里的高度，最后取最高画质。"""
    if not variants:
        return None
    ordered = sort_variants(variants)
    wanted = (preferred or "").strip().lower()
    if wanted:
        for variant in ordered:
            if variant.quality_key.lower() == wanted or variant.label.lower() == wanted:
                return variant
        digits = re.search(r"\d{3,4}", wanted)
        if digits:
            for variant in ordered:
                if str(variant.height) == digits.group(0):
                    return variant
    return ordered[0]


def _fetch_text(session: HttpSession, url: str, task: DownloadTask, log_callback: LogCallback) -> str:
    response = session.request(
        "GET",
        url,
        headers=task.headers,
        cookies=task.cookies,
        timeout=task.timeout,
        verify=task.verify_ssl,
    )
    status = getattr(response, "status_code", 0)
    if status and status >= 400:
        raise RuntimeError(f"HTTP {status}")
    return getattr(response, "text", "") or ""


def collect_variants(
    playlists: list[str],
    session: HttpSession,
    task: DownloadTask,
    log_callback: LogCallback,
) -> tuple[list[StreamVariant], list[str]]:
    """把命中的播放列表展开成清晰度列表。"""
    variants: list[StreamVariant] = []
    notes: list[str] = []
    for playlist in playlists[:MAX_PLAYLIST_PROBES]:
        try:
            text = _fetch_text(session, playlist, task, log_callback)
        except Exception as exc:
            notes.append(f"读取播放列表失败：{playlist}（{exc}）")
            continue
        try:
            children = parse_master_playlist(text, playlist)
        except Exception as exc:  # 解析异常不应该让整个嗅探失败
            notes.append(f"解析播放列表失败：{playlist}（{exc}）")
            continue
        if children:
            log_callback(f"播放列表 {playlist} 包含 {len(children)} 档清晰度。")
            variants.extend(children)
        else:
            label = playlist_quality_label(playlist) or "默认"
            log_callback(f"播放列表 {playlist} 是单档流，按 {label} 处理。")
            variants.append(StreamVariant(url=playlist, label=label, source=playlist))

    deduped: list[StreamVariant] = []
    seen: set[str] = set()
    for variant in sort_variants(variants):
        if variant.url in seen:
            continue
        seen.add(variant.url)
        deduped.append(variant)
    if not deduped:
        notes.append("命中的播放列表里没有可用的清晰度。")
    return deduped, notes


def sniff_page(
    url: str,
    task: DownloadTask,
    log_callback: LogCallback,
    *,
    use_browser: bool | None = None,
) -> SniffResult:
    """嗅探页面里的 m3u8；找不到且允许时改走浏览器抓包。"""
    result = SniffResult(page_url=url)
    allow_browser = task.use_browser if use_browser is None else use_browser
    notes: list[str] = []
    playlists: list[str] = []
    html = ""

    # CDN 普遍做防盗链：无论页面还是播放列表，都带上「来源页面」作为 Referer。
    # 用户自己在请求头里写了 Referer 就以用户的为准。
    referer = first_header(task.headers, "Referer") or default_referer(url)
    probe_task = replace(task, referer=referer)
    result.referer = referer
    if referer:
        log_callback(f"抓取时使用 Referer：{referer}")

    session = build_session(probe_task, log_callback)
    try:
        log_callback(f"正在抓取页面：{url}")
        try:
            html = _fetch_text(session, url, probe_task, log_callback)
            result.page_title = page_title(html)
        except Exception as exc:
            notes.append(f"直接抓取页面失败：{exc}")
        challenge = looks_like_challenge(html)
        playlists = find_m3u8_urls(html, url)
        if playlists:
            log_callback(f"页面源码里找到 {len(playlists)} 个 m3u8 地址。")
        else:
            playlists = _scan_iframes(html, url, session, probe_task, log_callback, notes)
            if playlists:
                log_callback(f"在 iframe 里找到 {len(playlists)} 个 m3u8 地址。")

        if not playlists and allow_browser:
            notes.append("页面源码里没有 m3u8，改用浏览器抓包。")
            log_callback("页面源码里没有 m3u8，改用浏览器抓包...")
            capture = capture_media_urls(url, timeout=max(task.timeout * 2, 45), log_callback=log_callback)
            notes.extend(capture.notes)
            for found in capture.urls:
                if found not in playlists:
                    playlists.append(found)
            if not result.page_title:
                result.page_title = capture.title

        if not playlists:
            hint = "该页面疑似被 Cloudflare 等防护拦截" if challenge else "页面源码和网络请求里都没有出现 m3u8"
            raise RuntimeError(
                f"没有找到 m3u8 视频地址（{hint}）。\n"
                "可以尝试：① 勾选「浏览器抓包」后重试；② 在浏览器里播放视频，把 m3u8 链接直接粘到 HLS 模式下载。"
            )

        variants, variant_notes = collect_variants(playlists, session, probe_task, log_callback)
        notes.extend(variant_notes)
        result.playlists = playlists
        result.variants = variants
        result.notes = notes
        return result
    finally:
        try:
            session.close()
        except Exception:
            pass


def _scan_iframes(
    html: str,
    base_url: str,
    session: HttpSession,
    task: DownloadTask,
    log_callback: LogCallback,
    notes: list[str],
) -> list[str]:
    found: list[str] = []
    for raw_src in IFRAME_PATTERN.findall(html or "")[:MAX_IFRAMES]:
        frame_url = urljoin(base_url, unescape_url_text(raw_src))
        try:
            frame_html = _fetch_text(session, frame_url, task, log_callback)
        except Exception as exc:
            notes.append(f"读取 iframe 失败：{frame_url}（{exc}）")
            continue
        for candidate in find_m3u8_urls(frame_html, frame_url):
            if candidate not in found:
                found.append(candidate)
        if found:
            break
    return found

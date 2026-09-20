from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Event, Lock, local
from typing import TYPE_CHECKING, BinaryIO, Mapping, Protocol, Sequence

from video_loader.models import (
    DownloadResult,
    DownloadTask,
    HttpSession,
    LogCallback,
    ProgressCallback,
    worker_count,
)
from video_loader.services.http_client import build_session, uses_curl_cffi
from video_loader.utils import unique_path

if TYPE_CHECKING:
    import requests


# 下载中的内容写在 <正式名字>.part，只有完整下完才原子改名成正式名字。
# 于是"正式名字存在"就等价于"这个文件是完整的"，重跑时可以放心复用。
PART_SUFFIX = ".part"
CHUNK_SIZE = 1024 * 256


class Downloader(Protocol):
    def download(
        self,
        task: DownloadTask,
        progress_callback: ProgressCallback,
        log_callback: LogCallback,
        cancel_event: Event,
    ) -> DownloadResult:
        ...


def part_path(target: Path) -> Path:
    """文件下载中的临时名字（`.part` 结尾，不会被 ffmpeg 合并进去）。"""
    return target.with_name(target.name + PART_SUFFIX)


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def resumable_target(path: Path) -> Path:
    """单文件下载挑目标名：有半截文件就沿用它续下，否则照旧避让同名文件。"""
    if part_path(path).exists() and not path.exists():
        return path
    return unique_path(path)


def request_with_retries(
    session: HttpSession,
    method: str,
    url: str,
    task: DownloadTask,
    log_callback: LogCallback,
    *,
    stream: bool = False,
    content_callback: object = None,
    before_attempt: object = None,
    extra_headers: Mapping[str, str] | None = None,
    accept_status: tuple[int, ...] = (),
) -> requests.Response:
    """发一次请求并返回响应；失败按 task.retries 重试。

    - ``content_callback``：curl_cffi 的 WRITEFUNCTION 落盘回调（见 download_one）；
    - ``before_attempt``：每次尝试前调用，让回调路径重试时把文件写回开头；
    - ``extra_headers``：本次请求额外的请求头（例如断点续传的 Range）；
    - ``accept_status``：这些状态码不当作错误（例如 416 交由调用方判断"是否已下满"）。
    """
    last_error: Exception | None = None
    attempts = max(1, task.retries + 1)
    headers = dict(task.headers if extra_headers is None else {**task.headers, **extra_headers})
    for attempt in range(1, attempts + 1):
        if before_attempt is not None:
            before_attempt()  # type: ignore[operator]
        try:
            response = session.request(
                method,
                url,
                headers=headers,
                cookies=task.cookies,
                timeout=task.timeout,
                verify=task.verify_ssl,
                stream=stream,
                **({"content_callback": content_callback} if content_callback is not None else {}),
            )
            if response.status_code in accept_status:
                return response
            response.raise_for_status()
            return response
        except Exception as exc:  # requests raises several concrete exception types.
            last_error = exc
            if attempt < attempts:
                log_callback(f"重试 {attempt}/{task.retries}：{url}")
                time.sleep(min(2 * attempt, 5))
    raise RuntimeError(f"请求失败：{url}（{last_error}）")


class _FileSink:
    """curl_cffi 的落盘回调（WRITEFUNCTION）：边收边写，取消时抛错中止传输。

    重试前会调用 reset()：把文件截回开头，避免两次尝试的内容接在一起。
    """

    def __init__(self, file: BinaryIO, cancel_event: Event) -> None:
        self._file = file
        self._cancel_event = cancel_event
        self.written = 0

    def reset(self) -> None:
        self._file.seek(0)
        self._file.truncate()
        self.written = 0

    def __call__(self, chunk: bytes) -> int:
        if self._cancel_event.is_set():
            raise RuntimeError("下载已取消")
        self._file.write(chunk)
        self.written += len(chunk)
        return len(chunk)


def _total_from_content_range(value: str | None) -> int | None:
    """从 Content-Range 里取总长度：正常是 "bytes 100-999/1000"，416 时是 "bytes */1000"。"""
    if not value or "/" not in value:
        return None
    raw = value.rsplit("/", 1)[1].strip()
    return int(raw) if raw.isdigit() else None


def _expected_total(response: requests.Response, offset: int) -> int:
    """这份响应下完之后文件应该有多大（算进度百分比用）。"""
    total = _total_from_content_range(response.headers.get("content-range"))
    if total:
        return total
    length = int(response.headers.get("content-length") or 0)
    return offset + length if length else 0


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _plan_resume(
    session: HttpSession,
    url: str,
    target: Path,
    part: Path,
    task: DownloadTask,
    log_callback: LogCallback,
) -> tuple[int, "requests.Response | None"]:
    """开工前和磁盘、服务端对一次账，决定这次从哪继续。

    返回 ``(offset, response)``：offset 为 -1 表示"服务端确认已经下满，直接复用"；
    response 非空表示已经拿到一个 206 续传响应，调用方接着往后面追加即可。
    """
    if part.exists():
        offset = file_size(part)
    elif target.exists():
        # 可能是本程序上一次留下的完整文件，也可能是别处/旧版本写了一半的文件，
        # 所以不靠猜：用 Range 问服务端"从我这份的大小往后还有没有内容"。
        offset = file_size(target)
    else:
        return 0, None

    response = request_with_retries(
        session,
        "GET",
        url,
        task,
        log_callback,
        stream=True,
        extra_headers={"Range": f"bytes={offset}-"},
        accept_status=(416,),
    )
    status = int(response.status_code)

    if status == 416:  # 本地这份不比服务端少
        response.close()
        total = _total_from_content_range(response.headers.get("content-range"))
        if total is not None and offset == total:
            if part.exists():
                part.replace(target)
            return -1, None
        log_callback(
            f"本地缓存的片段大小对不上（本地 {offset} 字节 / 服务端 {total if total is not None else '未知'} 字节），重新下载。"
        )
    elif status == 206:
        if not part.exists() and target.exists():
            target.replace(part)  # 半截文件挪到 .part，维持"正式名字 = 完整"的约定
        return offset, response
    else:  # 200：对端忽略了 Range，返回的是整份内容
        log_callback("对端不支持 Range 续传，改为从头下载。")

    _discard(part)
    return 0, None


def _write_stream(
    response: requests.Response,
    part: Path,
    target: Path,
    offset: int,
    task: DownloadTask,
    cancel_event: Event,
    progress_callback: ProgressCallback | None,
    progress_start: float,
    progress_span: float,
) -> int:
    total = _expected_total(response, offset)
    written = offset
    try:
        with part.open("ab" if offset else "wb") as file:
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if cancel_event.is_set():
                    raise RuntimeError("下载已取消")
                if not chunk:
                    continue
                file.write(chunk)
                written += len(chunk)
                if progress_callback and total:
                    ratio = min(written / total, 1.0)
                    progress_callback(progress_start + ratio * progress_span, target.name)
    finally:
        # 没读完就退出（取消/异常）时关掉流，否则会话会被半截流占住，后续请求复用不了连接。
        response.close()
    return written - offset


def _write_with_content_callback(
    session: HttpSession,
    url: str,
    part: Path,
    task: DownloadTask,
    log_callback: LogCallback,
    cancel_event: Event,
) -> int:
    """全新下载的快路径：走会话自己的 curl 句柄，连接可以复用（见 download_one）。"""
    with part.open("wb") as file:
        sink = _FileSink(file, cancel_event)
        request_with_retries(
            session,
            "GET",
            url,
            task,
            log_callback,
            content_callback=sink,
            before_attempt=sink.reset,
        )
        return sink.written


def download_one(
    session: HttpSession,
    url: str,
    target: Path,
    task: DownloadTask,
    log_callback: LogCallback,
    cancel_event: Event,
    progress_callback: ProgressCallback | None = None,
    progress_start: float = 0.0,
    progress_span: float = 1.0,
    *,
    resume: bool | None = None,
) -> tuple[Path, int]:
    """把 url 落到 target，返回 (路径, 本次实际新写入的字节数)。

    断点续传的三条约定：

    1. 进行中的内容写 ``target.part``，完整下完才原子改名成 ``target``——所以正式名字
       存在就等于文件完整，重跑时可以直接复用；
    2. ``target``（或 ``.part``）已存在时先用 ``Range: bytes=<已有长度>-`` 问服务端：
       416 = 已经下满（复用）／206 = 本地那份其实是半截（挪成 .part 继续追加）／
       200 = 对端不支持 Range（丢掉本地那份从头下）；
    3. 中途失败或取消时保留 ``.part``，下次接着下；空文件不留。

    curl_cffi 的流式读取（stream=True）每次请求都会复制一份 curl 句柄，而复制品不继承连接
    缓存：实测 3 次请求 = 3 条 TCP 连接，也就是每个分片都要重做一次 TCP + TLS 握手。所以
    全新下载且不需要按字节报进度时（分片场景）走 content_callback 边收边写——这条路径用
    会话自己的句柄，同一线程内连接可以持续复用（实测 8 次请求 1 条连接）。需要先看状态码
    的续传请求则只能走流式读取，代价只是"取消后重跑"这一次。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    part = part_path(target)
    use_resume = task.resume if resume is None else resume

    if use_resume:
        offset, response = _plan_resume(session, url, target, part, task, log_callback)
    else:
        offset, response = 0, None

    if offset < 0:  # 服务端确认已经下满
        return target, 0

    try:
        if response is None and offset == 0 and progress_callback is None and uses_curl_cffi(session):
            _write_with_content_callback(session, url, part, task, log_callback, cancel_event)
        else:
            if response is None:
                response = request_with_retries(session, "GET", url, task, log_callback, stream=True)
            _write_stream(
                response,
                part,
                target,
                offset,
                task,
                cancel_event,
                progress_callback,
                progress_start,
                progress_span,
            )
    except Exception:
        if file_size(part) == 0:
            _discard(part)  # 什么都没下到，别留个空壳让下次多问一次 416
        raise

    written = file_size(part) - offset
    part.replace(target)
    return target, written


def download_many(
    items: Sequence[tuple[str, Path]],
    task: DownloadTask,
    log_callback: LogCallback,
    cancel_event: Event,
    progress_callback: ProgressCallback | None = None,
    *,
    progress_span: float = 0.9,
    label: str = "片段",
    resume: bool | None = None,
) -> list[Path]:
    """并发下载一批 URL，返回与 items 顺序一致的本地路径（合并前必须保持顺序）。

    每个线程复用一条会话：curl_cffi 的会话不是线程安全的，但同一线程内复用可以省掉每个分片
    一次会话初始化，配合 content_callback 还能复用同一条 TCP/TLS 连接。

    ``resume``（默认跟 task.resume）打开时用固定文件名，重跑同一个任务会复用已下好的分片、
    续下 `.part` 里的半截分片；关掉则是老行为——每次都给新名字，全部重下。
    """
    if not items:
        return []

    use_resume = task.resume if resume is None else resume
    targets = [path if use_resume else unique_path(path) for _url, path in items]
    planned = [(url, targets[index]) for index, (url, _path) in enumerate(items)]

    files: list[Path | None] = [None] * len(items)
    reusable = 0
    sessions: list[HttpSession] = []
    sessions_lock = Lock()
    thread_state = local()

    def session_for_this_thread() -> HttpSession:
        session = getattr(thread_state, "session", None)
        if session is None:
            # 第一条会话负责打印「已启用指纹伪装」这类一次性提示，其余线程安静创建。
            session = build_session(task, log_callback if not sessions else None)
            thread_state.session = session
            with sessions_lock:
                sessions.append(session)
        return session

    def fetch(index: int, url: str, path: Path) -> tuple[int, Path, int]:
        if cancel_event.is_set():
            raise RuntimeError("下载已取消")
        path.parent.mkdir(parents=True, exist_ok=True)
        result_path, written = download_one(
            session_for_this_thread(),
            url,
            path,
            task,
            log_callback,
            cancel_event,
            resume=use_resume,
        )
        return index, result_path, written

    completed = 0
    total = len(planned)
    workers = worker_count(task.concurrency)
    try:
        if total == 1:
            index, path, written = fetch(0, *planned[0])
            files[index] = path
            completed = 1
            reusable = 1 if written == 0 else 0
            if progress_callback:
                progress_callback(progress_span, "已下载 1/1")
        else:
            log_callback(f"并发 {workers} 路下载 {total} 个{label}。")
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(fetch, index, url, path) for index, (url, path) in enumerate(planned)]
                for future in as_completed(futures):
                    if cancel_event.is_set():
                        raise RuntimeError("下载已取消")
                    index, path, written = future.result()
                    files[index] = path
                    completed += 1
                    if written == 0:
                        reusable += 1
                    if progress_callback:
                        progress_callback(completed / max(total, 1) * progress_span, f"已下载 {completed}/{total}")
            if reusable and use_resume:
                log_callback(f"复用了 {reusable} 个已下好的{label}（断点续传）。")
    finally:
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass

    return [path for path in files if path is not None]

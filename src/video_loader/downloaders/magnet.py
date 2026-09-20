from __future__ import annotations

import re
from pathlib import Path
from threading import Event

from video_loader.models import DownloadResult, DownloadTask, LogCallback, ProgressCallback, worker_count
from video_loader.services.aria2 import Aria2Session, wait_for_gid
from video_loader.utils import safe_filename, unique_path

# 输入框里的注释行（以 # 开头）直接忽略，方便程序写入示例提示
COMMENT_PREFIX = "#"
BTIH_HEX = re.compile(r"xt=urn:btih:[0-9a-fA-F]{40}")
BTIH_BASE32 = re.compile(r"xt=urn:btih:[A-Za-z2-7]{32}")
BTMH = re.compile(r"xt=urn:btmh:")


def is_magnet_url(url: str) -> bool:
    return url.strip().lower().startswith("magnet:")


def split_magnet_lines(text: str) -> list[str]:
    """去掉空行和注释行。"""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith(COMMENT_PREFIX)
    ]


def magnet_error(line: str) -> str | None:
    """返回本地校验发现的错误；None 表示通过。"""
    if not is_magnet_url(line):
        return f"磁力模式只支持 magnet:? 链接，每行一个：{line[:80]}"
    if BTMH.search(line) or BTIH_HEX.search(line) or BTIH_BASE32.search(line):
        return None
    return f"磁力链接缺少有效的 btih 哈希（需要 40 位十六进制或 32 位 base32）：{line[:80]}"


class MagnetDownloader:
    def download(
        self,
        task: DownloadTask,
        progress_callback: ProgressCallback,
        log_callback: LogCallback,
        cancel_event: Event,
    ) -> DownloadResult:
        session: Aria2Session | None = None
        try:
            task.output_dir.mkdir(parents=True, exist_ok=True)
            magnet_lines = split_magnet_lines(task.url)
            if not magnet_lines:
                return DownloadResult(
                    False,
                    None,
                    "磁力链接下载失败",
                    ["请输入至少一个 magnet:? 链接（以 # 开头的行会被忽略）。"],
                )
            for line in magnet_lines:
                problem = magnet_error(line)
                if problem:
                    # 先做本地校验，否则错误要等 aria2 报 "No URI to download." 才暴露
                    return DownloadResult(False, None, "磁力链接下载失败", [problem])

            session = Aria2Session(task.output_dir, log_callback, max_concurrent_downloads=worker_count(task.concurrency))
            try:
                session.start()
                gids: list[str] = []
                for magnet in magnet_lines:
                    if cancel_event.is_set():
                        raise RuntimeError("下载已取消")
                    options = {"dir": str(task.output_dir)}
                    gid = session.call("aria2.addUri", [[magnet], options])
                    gids.append(str(gid))
                    log_callback(f"已加入磁力任务：{magnet[:96]}")

                files: list[Path] = []
                for index, gid in enumerate(gids, start=1):
                    log_callback(f"正在获取种子元信息并下载（{index}/{len(gids)}）...")
                    status = wait_for_gid(session, gid, cancel_event)
                    if status.get("status") == "error":
                        message = str(status.get("errorMessage") or status.get("error") or "aria2 报告错误")
                        return DownloadResult(False, None, "磁力链接下载失败", [message])
                    # wait_for_gid 会跟随磁力的 followedBy，返回的是真正下载文件的 GID 状态
                    for item in status.get("files") or []:
                        path = Path(str(item.get("path") or ""))
                        if path.is_file() and path.stat().st_size > 0:
                            files.append(path)

                if task.output_name and len(files) == 1:
                    output_name = safe_filename(task.output_name)
                    if not Path(output_name).suffix:
                        output_name = f"{output_name}.mkv"
                    renamed = unique_path(task.output_dir / output_name)
                    files[0].rename(renamed)
                    files = [renamed]

                if not files:
                    return DownloadResult(False, None, "磁力链接下载完成但没有生成文件", [])
                progress_callback(1.0, "磁力链接下载完成")
                return DownloadResult(True, files[0], f"已保存到 {', '.join(str(path) for path in files)}", [])
            finally:
                session.stop()
        except RuntimeError as exc:
            return DownloadResult(False, None, "磁力链接下载已取消" if "取消" in str(exc) else "磁力链接下载失败", [str(exc)])
        except Exception as exc:
            return DownloadResult(False, None, "磁力链接下载失败", [str(exc)])

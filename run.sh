#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "Missing virtual environment. Run:"
  echo "  python3 -m venv .venv"
  echo "  source .venv/bin/activate"
  echo "  pip install -r requirements.txt"
  exit 1
fi

log_path() {
  "$PYTHON" -c "import sys; sys.path.insert(0, '$ROOT_DIR/src'); from video_loader.services.app_log import log_path; print(log_path())"
}

# ./run.sh --background
# 前台启动时，关掉终端/会话就会把程序一起带走（SIGHUP），表现为"程序自己退了"。
# 这个模式用独立会话启动，并由一个 watcher 记录退出码/信号到日志文件。
if [[ "${1:-}" == "--background" ]]; then
  cd "$ROOT_DIR"
  nohup "$PYTHON" - "$ROOT_DIR" <<'PY' >/dev/null 2>&1 &
import os
import subprocess
import sys

root = sys.argv[1]
sys.path.insert(0, os.path.join(root, "src"))
from video_loader.services.app_log import log_line, log_path

child = subprocess.Popen([os.path.join(root, "run.sh")], cwd=root, start_new_session=True)
log_line(f"后台启动：PID={child.pid}（进程会话独立，关终端不会带走它）")
code = child.wait()
if code < 0:
    log_line(f"后台实例 PID={child.pid} 被信号结束：SIG{abs(code)}")
elif code > 128:
    log_line(f"后台实例 PID={child.pid} 被信号结束：SIG{code - 128}")
else:
    log_line(f"后台实例 PID={child.pid} 正常退出：exit={code}")
PY
  sleep 1
  echo "已后台启动（独立会话）。"
  echo "运行日志：$(log_path)"
  exit 0
fi

cd "$ROOT_DIR"
exec "$PYTHON" -m video_loader.app

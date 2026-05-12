"""로컬에서 Streamlit 대시보드를 띄운다. 8501이 점유 중이면 8502~8525 중 빈 포트를 고른다."""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def pick_port(lo: int = 8501, hi: int = 8525) -> int:
    # Streamlit은 기본적으로 모든 인터페이스에 바인딩한다.
    # 127.0.0.1만 검사하면 Windows에서 실제로는 점유인데 통과하는 경우가 있다.
    for port in range(lo, hi + 1):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("", port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    raise SystemExit(
        f"{lo}~{hi} 포트가 모두 사용 중입니다. "
        "다른 Streamlit·프로세스를 종료한 뒤 다시 실행해 주세요."
    )


def main() -> int:
    port = pick_port()
    url = f"http://localhost:{port}/"
    print(f"[INFO] 대시보드 주소: {url}")
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(ROOT / "webapp.py"),
        "--server.port",
        str(port),
        "--browser.gatherUsageStats",
        "false",
    ]
    return subprocess.call(cmd, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())

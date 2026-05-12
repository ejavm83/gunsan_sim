"""Streamlit Community Cloud 진입점(호환용 래퍼).

가능하면 Cloud 앱 설정의 Main file을 `webapp.py`로 두는 편이
기동·디버깅이 단순합니다. 이 파일은 기존 배포 URL 호환을 위해
`webapp.py`를 매 실행마다 다시 불러 옵니다.
"""

from __future__ import annotations

from pathlib import Path
import runpy

_ROOT = Path(__file__).resolve().parent
runpy.run_path(str(_ROOT / "webapp.py"), run_name="__main__")

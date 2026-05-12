"""Streamlit Community Cloud 진입점(호환용 래퍼).

Cloud 앱의 Main file이 이 파일일 때, 동일 프로세스에서 `webapp` 모듈을
로드해 대시보드를 띄운다. `runpy.run_path`는 일부 환경에서 기동이 멈출 수 있어
일반 import로 통일한다.

가능하면 Main file을 `webapp.py`로 두는 것도 권장한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_root_s = str(_ROOT)
if _root_s not in sys.path:
    sys.path.insert(0, _root_s)

import webapp  # noqa: E402, F401 — 모듈 로드 시 Streamlit UI가 등록된다.

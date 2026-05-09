"""군산 공장 하이브리드 공정 애니메이션.

`simulation.run_simulation()` 이 만들어낸 `Metrics.events` 시계열을 재생해서,
공장 평면도 위에 각 자원의 점유 상태와 버퍼 수량이 시간에 따라 변하는
모습을 GIF/MP4 로 저장한다.

사용법::

    from animate import render_factory_animation
    render_factory_animation(metrics, cfg, out_path="out/factory.gif")
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.animation import FuncAnimation, PillowWriter  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import FancyBboxPatch, Rectangle  # noqa: E402

from config import SimulationConfig  # noqa: E402
from metrics import Event, Metrics  # noqa: E402

_BUILD_INFO_TEXT = "개발자: (주) 지엠티 김길용 수석 | v0.0.2 (2026.05.09)"


# ---------------------------------------------------------------------------
# 1) 시간순 상태 재구성
# ---------------------------------------------------------------------------


@dataclass
class FactoryState:
    t: float = 0.0
    truck_inbound_active: int = 0  # 입구~출차 사이 트럭 수
    sort_queue: int = 0            # 하역 후 더미(트럭) 수
    press_queue: int = 0           # 선별 끝난 sub-pile 수
    pallet_buffer: int = 0         # 압착 후 파레트 수
    flake_buffer: int = 0          # 퓨플레이크 포대 수
    scr_buffer: int = 0            # SCR 코일 수
    truck_outbound_active: int = 0  # 출하 트럭 활성

    furnace_state: dict[int, str] = field(default_factory=dict)
    # "idle" | "charging" | "melting" | "casting"

    trucks_in_done: int = 0
    trucks_out_done: int = 0
    melt_batches_done: int = 0
    flake_units_total: int = 0
    scr_units_total: int = 0
    flake_dispatched_ton: float = 0.0
    scr_dispatched_ton: float = 0.0


def _interp_level(samples: list[tuple[float, int]], t: float) -> int:
    """누적 step 시계열에서 시각 t 의 값을 즉시 반환."""
    if not samples:
        return 0
    times = [s[0] for s in samples]
    idx = bisect_right(times, t) - 1
    if idx < 0:
        return 0
    return samples[idx][1]


def build_state_timeline(
    metrics: Metrics, horizon_min: int, step_min: float
) -> list[FactoryState]:
    """events 와 buffer_levels 를 활용해 step_min 간격의 스냅샷 시계열을 만든다."""

    sorted_events = sorted(metrics.events, key=lambda e: e.time_min)

    # 이벤트 누적용 상태 (스칼라 카운터들)
    counters = {
        "truck_inbound_active": 0,
        "sort_queue": 0,
        "press_queue": 0,
        "truck_outbound_active": 0,
        "trucks_in_done": 0,
        "trucks_out_done": 0,
        "melt_batches_done": 0,
        "flake_dispatched_ton": 0.0,
        "scr_dispatched_ton": 0.0,
    }
    furnace_state: dict[int, str] = {}

    snapshots: list[FactoryState] = []
    ev_idx = 0
    n_events = len(sorted_events)

    n_frames = int(np.ceil(horizon_min / step_min)) + 1
    for frame in range(n_frames):
        t = min(frame * step_min, horizon_min)
        while ev_idx < n_events and sorted_events[ev_idx].time_min <= t:
            ev = sorted_events[ev_idx]
            _apply_event(counters, furnace_state, ev)
            ev_idx += 1

        snap = FactoryState(
            t=t,
            truck_inbound_active=counters["truck_inbound_active"],
            sort_queue=counters["sort_queue"],
            press_queue=counters["press_queue"],
            pallet_buffer=_interp_level(metrics.pallet_buffer_levels, t),
            flake_buffer=_interp_level(metrics.flake_buffer_levels, t),
            scr_buffer=_interp_level(metrics.scr_buffer_levels, t),
            truck_outbound_active=counters["truck_outbound_active"],
            furnace_state=dict(furnace_state),
            trucks_in_done=counters["trucks_in_done"],
            trucks_out_done=counters["trucks_out_done"],
            melt_batches_done=counters["melt_batches_done"],
            flake_units_total=_interp_level(metrics.flake_buffer_levels, t)
            + int(counters["flake_dispatched_ton"]),
            scr_units_total=_interp_level(metrics.scr_buffer_levels, t)
            + int(counters["scr_dispatched_ton"] // 4),
            flake_dispatched_ton=counters["flake_dispatched_ton"],
            scr_dispatched_ton=counters["scr_dispatched_ton"],
        )
        snapshots.append(snap)
    return snapshots


def _apply_event(
    counters: dict, furnace_state: dict[int, str], ev: Event
) -> None:
    if ev.stage == "inbound":
        if ev.kind == "arrive":
            counters["truck_inbound_active"] += 1
        elif ev.kind == "unloaded":
            counters["sort_queue"] += 1
        elif ev.kind == "depart":
            counters["truck_inbound_active"] = max(
                0, counters["truck_inbound_active"] - 1
            )
            counters["trucks_in_done"] += 1
    elif ev.stage == "sorting":
        if ev.kind == "sort_done":
            counters["sort_queue"] = max(0, counters["sort_queue"] - 1)
            counters["press_queue"] += 8  # 트럭 1대 = 8 sub-pile
    elif ev.stage == "press":
        if ev.kind == "pallet_done":
            counters["press_queue"] = max(0, counters["press_queue"] - 1)
    elif ev.stage == "melting":
        fid = ev.detail.get("furnace")
        if fid is None:
            return
        if ev.kind == "batch_collected":
            furnace_state[fid] = "charging"
        elif ev.kind == "elevator_done":
            furnace_state[fid] = "charging"
        elif ev.kind == "melt_start":
            furnace_state[fid] = "melting"
        elif ev.kind == "melt_done":
            furnace_state[fid] = "casting"
        elif ev.kind == "batch_done":
            furnace_state[fid] = "idle"
            counters["melt_batches_done"] += 1
    elif ev.stage == "outbound":
        if ev.kind == "arrive":
            counters["truck_outbound_active"] += 1
        elif ev.kind == "depart":
            counters["truck_outbound_active"] = max(
                0, counters["truck_outbound_active"] - 1
            )
            counters["trucks_out_done"] += 1
            kind = ev.detail.get("load_kind")
            ton = ev.detail.get("ton", 0.0)
            if kind == "flake":
                counters["flake_dispatched_ton"] += ton
            elif kind == "scr":
                counters["scr_dispatched_ton"] += ton


# ---------------------------------------------------------------------------
# 2) 시각 요소 그리기
# ---------------------------------------------------------------------------


@dataclass
class Box:
    x: float
    y: float
    w: float
    h: float
    title: str
    base_color: str = "#dddddd"
    max_n: int = 1
    description: str = ""  # 마우스 오버 시 표시할 상세 설명


_FURNACE_COLOR = {
    "idle": "#cccccc",
    "charging": "#facc15",   # 노랑
    "melting": "#ef4444",    # 빨강 (12h 용해)
    "casting": "#22c55e",    # 초록 (주조)
}

# 반사로 상태별 상세 설명
_FURNACE_STATE_DESC = {
    "idle": "[대기 상태 (Idle)]\n반사로가 비어있는 대기 상태입니다.\n다음 배치 장입을 기다리고 있습니다.",
    "charging": "[장입 중 (Charging)]\n파레트를 엘리베이터로 반사로에 투입 중입니다.\n• 1배치 = 약 60 파레트 (30톤)\n• 장입 시간: 약 2~3시간",
    "melting": "[용해 중 (Melting)]\n스크랩을 고온으로 녹이는 중입니다.\n• 용해 온도: 1,100~1,200°C\n• 소요 시간: 약 12시간\n• 가스 버너로 가열",
    "casting": "[주조 중 (Casting)]\n용탕을 제품으로 주조하는 중입니다.\n• 퓨플레이크 라인: 50%\n• SCR 라인: 50%\n• 소요 시간: 약 8시간",
}


def _draw_box(
    ax,
    box: Box,
    *,
    n: Optional[int] = None,
    state_label: Optional[str] = None,
    fill_color: Optional[str] = None,
    text_color: str = "black",
) -> None:
    if fill_color is None:
        if n is not None and box.max_n > 0:
            ratio = min(1.0, n / box.max_n)
            fill_color = _level_color(ratio)
        else:
            fill_color = box.base_color

    patch = FancyBboxPatch(
        (box.x, box.y),
        box.w,
        box.h,
        boxstyle="round,pad=0.02,rounding_size=0.08",
        linewidth=1.0,
        edgecolor="#333333",
        facecolor=fill_color,
        alpha=0.85,
    )
    ax.add_patch(patch)

    ax.text(
        box.x + box.w / 2,
        box.y + box.h - 0.18,
        box.title,
        ha="center",
        va="top",
        fontsize=9,
        fontweight="bold",
        color=text_color,
    )

    label = state_label
    if label is None:
        if n is not None:
            label = f"{n} / {box.max_n}" if box.max_n > 1 else f"{n}"
        else:
            label = ""
    ax.text(
        box.x + box.w / 2,
        box.y + box.h / 2 - 0.05,
        label,
        ha="center",
        va="center",
        fontsize=12,
        fontweight="bold",
        color=text_color,
    )


def _level_color(ratio: float) -> str:
    """0~1 비율에 따라 청록 → 주황 → 빨강 색상을 반환."""
    if ratio < 0.5:
        # 청록 -> 노랑
        r = int(75 + (250 - 75) * (ratio / 0.5))
        g = int(192 + (220 - 192) * (ratio / 0.5))
        b = int(187 + (60 - 187) * (ratio / 0.5))
    else:
        # 노랑 -> 빨강
        x = (ratio - 0.5) / 0.5
        r = int(250 + (239 - 250) * x)
        g = int(220 + (68 - 220) * x)
        b = int(60 + (68 - 60) * x)
    return f"#{r:02x}{g:02x}{b:02x}"


def _draw_arrow(ax, x0: float, y0: float, x1: float, y1: float) -> None:
    ax.annotate(
        "",
        xy=(x1, y1),
        xytext=(x0, y0),
        arrowprops=dict(arrowstyle="->", color="#888888", lw=1.5),
    )


# ---------------------------------------------------------------------------
# 3) 한 프레임 그리기
# ---------------------------------------------------------------------------


_BOXES = {
    "inbound": Box(
        0.3, 4.5, 1.6, 1.0, "트럭 입고", max_n=4,
        description="[트럭 입고]\n"
                    "원료(스크랩) 운반 트럭이 공장에 도착하는 지점입니다.\n"
                    "• 최대 동시 대기: 4대\n"
                    "• 처리 흐름: 입고 → 1차 계근 → 하역장"
    ),
    "weigh_in": Box(
        2.1, 4.5, 1.0, 1.0, "1차 계근", max_n=1,
        description="[1차 계근]\n"
                    "입고 트럭의 총 중량을 측정합니다.\n"
                    "• 처리 용량: 1대/회\n"
                    "• 계근 시간: 약 3~5분\n"
                    "• 계근 후 하역장으로 이동"
    ),
    "unload": Box(
        3.3, 4.5, 1.4, 1.0, "하역장", max_n=2,
        description="[하역장]\n"
                    "트럭에서 스크랩 원료를 하역합니다.\n"
                    "• 동시 하역 가능: 2대\n"
                    "• 하역 시간: 약 20~30분/대\n"
                    "• 하역된 스크랩은 선별 대기 더미로 이동"
    ),
    "sort_yard": Box(
        5.0, 4.5, 1.5, 1.0, "선별 대기 더미", max_n=20,
        description="[선별 대기 더미]\n"
                    "하역된 스크랩이 선별 작업 전 대기하는 야적장입니다.\n"
                    "• 최대 더미 수: 20개 (트럭 20대분)\n"
                    "• 트럭 1대 = 1 더미 = 8 sub-pile\n"
                    "• 선별 작업: 등급별 분류 및 이물질 제거"
    ),
    "press": Box(
        6.7, 4.5, 1.4, 1.0, "압착 대기 큐", max_n=80,
        description="[압착 대기 큐]\n"
                    "선별 완료된 sub-pile이 압착 작업을 대기합니다.\n"
                    "• 최대 대기: 80개 sub-pile\n"
                    "• 트럭 1대 → 8 sub-pile 생성\n"
                    "• 압착기가 sub-pile을 파레트로 압축"
    ),
    "pallet": Box(
        8.3, 4.5, 1.7, 1.0, "파레트 버퍼", max_n=160,
        description="[파레트 버퍼]\n"
                    "압착 완료된 파레트가 용해 투입 전 대기합니다.\n"
                    "• 최대 적재: 160 파레트\n"
                    "• 파레트 1개 ≈ 500kg 스크랩\n"
                    "• 엘리베이터로 반사로에 투입"
    ),
    "elevator": Box(
        10.2, 4.5, 1.0, 1.0, "엘리베이터", max_n=1,
        description="[엘리베이터]\n"
                    "파레트를 반사로 투입구로 운반합니다.\n"
                    "• 운반 용량: 1 파레트/회\n"
                    "• 사이클 시간: 약 3~5분\n"
                    "• 1배치(30톤) = 약 60 파레트 투입"
    ),
    "furnace1": Box(
        11.4, 4.5, 1.7, 1.0, "반사로 1",
        description="[반사로 1]\n"
                    "스크랩을 용해하여 용탕을 만드는 대형 용해로입니다.\n"
                    "• 배치 용량: 30톤\n"
                    "• 용해 시간: 약 12시간\n"
                    "• 상태: idle(대기) → charging(장입) → melting(용해) → casting(주조)\n"
                    "• 용탕 온도: 약 1,100~1,200°C"
    ),
    "furnace2": Box(
        13.3, 4.5, 1.7, 1.0, "반사로 2",
        description="[반사로 2]\n"
                    "스크랩을 용해하여 용탕을 만드는 대형 용해로입니다.\n"
                    "• 배치 용량: 30톤\n"
                    "• 용해 시간: 약 12시간\n"
                    "• 상태: idle(대기) → charging(장입) → melting(용해) → casting(주조)\n"
                    "• 반사로 1과 교대로 운용하여 연속 생산"
    ),
    "flake_line": Box(
        11.4, 2.8, 1.7, 1.0, "퓨플레이크 라인",
        description="[퓨플레이크 라인]\n"
                    "용탕을 얇은 Cu 플레이크로 주조하는 라인입니다.\n"
                    "• 생산 비율: 배치당 약 50%\n"
                    "• 출력: 포대 단위 (25kg/포대)\n"
                    "• 주조 시간: 약 8시간/배치\n"
                    "• 용도: 전해 정련, 합금 제조용"
    ),
    "scr_line": Box(
        13.3, 2.8, 1.7, 1.0, "SCR 라인",
        description="[SCR 라인 (South Wire Rod)]\n"
                    "용탕을 SCR 코일 (전선용 구리봉)로 연속 주조합니다.\n"
                    "• 생산 비율: 배치당 약 50%\n"
                    "• 출력: 4톤 코일 단위\n"
                    "• 주조 시간: 약 8시간/배치\n"
                    "• 용도: 전선, 케이블 제조용"
    ),
    "flake_buf": Box(
        8.3, 2.8, 2.0, 1.0, "퓨플레이크 야적", max_n=100,
        description="[퓨플레이크 야적장]\n"
                    "생산된 Cu 플레이크 포대의 임시 보관 장소입니다.\n"
                    "• 최대 적재: 100 포대\n"
                    "• 포대 중량: 25kg/포대\n"
                    "• 버퍼가 가득 차면 생산 라인 정지\n"
                    "• 출하 트럭으로 반출"
    ),
    "scr_buf": Box(
        8.3, 1.3, 2.0, 1.0, "SCR 코일 야적", max_n=75,
        description="[SCR 코일 야적장]\n"
                    "생산된 SCR 코일의 임시 보관 장소입니다.\n"
                    "• 최대 적재: 75 코일\n"
                    "• 코일 중량: 4톤/코일\n"
                    "• 버퍼가 가득 차면 생산 라인 정지\n"
                    "• 출하 트럭으로 반출"
    ),
    "out_truck": Box(
        0.3, 2.0, 1.6, 1.0, "출하 트럭", max_n=4,
        description="[출하 트럭]\n"
                    "완제품을 고객에게 운송하는 트럭입니다.\n"
                    "• 최대 동시 대기: 4대\n"
                    "• 적재량: 약 20~25톤/대\n"
                    "• 처리 흐름: 상차 → 계근 → 출고"
    ),
    "weigh_out": Box(
        2.1, 2.0, 1.0, 1.0, "출하 계근", max_n=1,
        description="[출하 계근 (2차 계근)]\n"
                    "적재 완료된 출하 트럭의 중량을 측정합니다.\n"
                    "• 처리 용량: 1대/회\n"
                    "• 계근 시간: 약 3~5분\n"
                    "• 순중량 = 2차 계근 - 공차 중량"
    ),
    "out_load": Box(
        3.3, 2.0, 4.7, 1.0, "출하 상차", max_n=10,
        description="[출하 상차장]\n"
                    "완제품을 출하 트럭에 적재하는 장소입니다.\n"
                    "• 동시 상차 가능: 10대\n"
                    "• 상차 시간: 약 30~60분/대\n"
                    "• 퓨플레이크 또는 SCR 코일 적재\n"
                    "• 지게차/크레인 사용"
    ),
}


def _draw_factory(ax, snap: FactoryState, cfg: SimulationConfig) -> None:
    ax.clear()
    ax.set_xlim(0, 15.5)
    ax.set_ylim(0.5, 6.2)
    ax.set_aspect("equal")
    ax.axis("off")

    # 입고 라인
    _draw_box(ax, _BOXES["inbound"], n=snap.truck_inbound_active)
    _draw_box(ax, _BOXES["weigh_in"], n=min(snap.truck_inbound_active, 1))
    _draw_box(ax, _BOXES["unload"], n=min(snap.sort_queue, 2))
    _draw_box(ax, _BOXES["sort_yard"], n=snap.sort_queue)
    _draw_box(ax, _BOXES["press"], n=snap.press_queue)
    _draw_box(ax, _BOXES["pallet"], n=snap.pallet_buffer)
    _draw_box(ax, _BOXES["elevator"],
              n=1 if any(s == "charging" for s in snap.furnace_state.values()) else 0)

    # 반사로
    f1 = snap.furnace_state.get(1, "idle")
    f2 = snap.furnace_state.get(2, "idle")
    _draw_box(
        ax, _BOXES["furnace1"], state_label=f"F1: {f1}",
        fill_color=_FURNACE_COLOR.get(f1, "#cccccc"),
    )
    _draw_box(
        ax, _BOXES["furnace2"], state_label=f"F2: {f2}",
        fill_color=_FURNACE_COLOR.get(f2, "#cccccc"),
    )

    # 주조 라인
    flake_active = "casting" in (f1, f2)
    scr_active = flake_active
    _draw_box(
        ax, _BOXES["flake_line"],
        state_label="● 가동 중" if flake_active else "—",
        fill_color="#bfdbfe" if flake_active else "#eeeeee",
    )
    _draw_box(
        ax, _BOXES["scr_line"],
        state_label="● 가동 중" if scr_active else "—",
        fill_color="#fecaca" if scr_active else "#eeeeee",
    )

    # 완제품 야적장
    _draw_box(ax, _BOXES["flake_buf"], n=snap.flake_buffer)
    _draw_box(ax, _BOXES["scr_buf"], n=snap.scr_buffer)

    # 출하
    _draw_box(ax, _BOXES["out_truck"], n=snap.truck_outbound_active)
    _draw_box(ax, _BOXES["weigh_out"], n=min(snap.truck_outbound_active, 1))
    _draw_box(ax, _BOXES["out_load"], n=snap.truck_outbound_active)

    # 흐름 화살표 (라인 1: 입고 → 압착 → 파레트)
    flow_top = [
        ("inbound", "weigh_in"),
        ("weigh_in", "unload"),
        ("unload", "sort_yard"),
        ("sort_yard", "press"),
        ("press", "pallet"),
        ("pallet", "elevator"),
        ("elevator", "furnace1"),
    ]
    for a, b in flow_top:
        ba, bb = _BOXES[a], _BOXES[b]
        _draw_arrow(
            ax,
            ba.x + ba.w,
            ba.y + ba.h / 2,
            bb.x,
            bb.y + bb.h / 2,
        )

    # 반사로 → 주조 라인
    for fid in ("furnace1", "furnace2"):
        bf = _BOXES[fid]
        line = _BOXES["flake_line"] if fid == "furnace1" else _BOXES["scr_line"]
        _draw_arrow(
            ax,
            bf.x + bf.w / 2,
            bf.y,
            line.x + line.w / 2,
            line.y + line.h,
        )

    # 주조 → 야적
    _draw_arrow(
        ax,
        _BOXES["flake_line"].x,
        _BOXES["flake_line"].y + _BOXES["flake_line"].h / 2,
        _BOXES["flake_buf"].x + _BOXES["flake_buf"].w,
        _BOXES["flake_buf"].y + _BOXES["flake_buf"].h / 2,
    )
    _draw_arrow(
        ax,
        _BOXES["scr_line"].x,
        _BOXES["scr_line"].y + _BOXES["scr_line"].h / 2,
        _BOXES["scr_buf"].x + _BOXES["scr_buf"].w,
        _BOXES["scr_buf"].y + _BOXES["scr_buf"].h / 2,
    )

    # 야적 → 상차 → 계근 → 트럭
    flow_bottom = [
        ("flake_buf", "out_load"),
        ("scr_buf", "out_load"),
        ("out_load", "weigh_out"),
        ("weigh_out", "out_truck"),
    ]
    for a, b in flow_bottom:
        ba, bb = _BOXES[a], _BOXES[b]
        _draw_arrow(
            ax,
            ba.x,
            ba.y + ba.h / 2,
            bb.x + bb.w,
            bb.y + bb.h / 2,
        )

    # 상단 시계 표시
    days = int(snap.t // (24 * 60))
    hours = int((snap.t // 60) % 24)
    minutes = int(snap.t % 60)
    title = (
        f"군산 공장 하이브리드 공정  |  T = D{days}+{hours:02d}:{minutes:02d}  |  "
        f"입고 {snap.trucks_in_done}대 / 배치완료 {snap.melt_batches_done} / "
        f"출하 {snap.trucks_out_done}대 ({snap.flake_dispatched_ton:.0f} t flake + "
        f"{snap.scr_dispatched_ton:.0f} t scr)"
    )
    ax.set_title(title, fontsize=11)


def _draw_timeseries(
    ax_pallet,
    ax_flake,
    ax_scr,
    snapshots: list[FactoryState],
    current_t: float,
) -> None:
    ts_h = [s.t / 60 for s in snapshots]
    pallet = [s.pallet_buffer for s in snapshots]
    flake = [s.flake_buffer for s in snapshots]
    scr = [s.scr_buffer for s in snapshots]

    for ax, ys, title, color, ymax in [
        (ax_pallet, pallet, "Pallet buffer (cap=160)", "#2563eb", 160),
        (ax_flake, flake, "Cu-flake buffer (cap=100)", "#0ea5e9", 100),
        (ax_scr, scr, "SCR coil buffer (cap=75)", "#dc2626", 75),
    ]:
        ax.clear()
        ax.fill_between(ts_h, ys, color=color, alpha=0.2)
        ax.plot(ts_h, ys, color=color, lw=1.5)
        ax.axvline(current_t / 60, color="black", lw=1.0, alpha=0.6)
        ax.set_xlim(0, max(ts_h) if ts_h else 1)
        ax.set_ylim(0, ymax + 5)
        ax.set_xlabel("Time (h)", fontsize=8)
        ax.set_title(title, fontsize=9)
        ax.grid(True, alpha=0.3)


# ---------------------------------------------------------------------------
# 4) Public API
# ---------------------------------------------------------------------------


def render_factory_animation(
    metrics: Metrics,
    cfg: SimulationConfig,
    out_path: str | Path = "out/factory.gif",
    step_min: float = 60.0,
    fps: int = 12,
) -> Path:
    """공장 레이아웃 애니메이션을 GIF 로 저장한다.

    Parameters
    ----------
    step_min : 한 프레임이 실시간 몇 분에 해당하는지. 기본 60(=1시간/frame).
    fps : GIF 의 초당 프레임 수.
    """
    snapshots = build_state_timeline(metrics, cfg.sim_horizon_min, step_min)
    if not snapshots:
        raise RuntimeError("스냅샷이 비어있습니다")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 한글 폰트 설정 (윈도우 / mac / linux 우선순위)
    _setup_korean_font()

    fig = plt.figure(figsize=(16, 9))
    gs = fig.add_gridspec(
        nrows=4, ncols=3,
        height_ratios=[2.4, 2.4, 1.0, 1.2],
        hspace=0.35, wspace=0.25,
    )
    ax_factory = fig.add_subplot(gs[0:3, :])
    ax_pallet = fig.add_subplot(gs[3, 0])
    ax_flake = fig.add_subplot(gs[3, 1])
    ax_scr = fig.add_subplot(gs[3, 2])

    # 범례 (반사로 상태 색상)
    legend_handles = [
        Rectangle((0, 0), 1, 1, color=_FURNACE_COLOR[s], label=s)
        for s in ("idle", "charging", "melting", "casting")
    ]
    legend = fig.legend(
        legend_handles,
        [h.get_label() for h in legend_handles],
        loc="upper right",
        ncol=4,
        fontsize=9,
        frameon=False,
        bbox_to_anchor=(0.99, 0.98),
    )
    fig.text(
        0.995, 0.018, _BUILD_INFO_TEXT,
        ha="right", va="bottom", fontsize=8, color="#444444",
        zorder=20,
    )

    def update(frame_idx: int):
        snap = snapshots[frame_idx]
        _draw_factory(ax_factory, snap, cfg)
        _draw_timeseries(ax_pallet, ax_flake, ax_scr, snapshots, snap.t)
        return ()

    anim = FuncAnimation(
        fig,
        update,
        frames=len(snapshots),
        interval=1000 / fps,
        blit=False,
    )

    suffix = out_path.suffix.lower()
    if suffix == ".gif":
        writer = PillowWriter(fps=fps)
        anim.save(out_path, writer=writer, dpi=110)
    elif suffix in (".mp4", ".mov"):
        try:
            from matplotlib.animation import FFMpegWriter

            writer = FFMpegWriter(fps=fps, bitrate=2000)
            anim.save(out_path, writer=writer, dpi=110)
        except Exception as exc:
            raise RuntimeError(
                f"MP4 저장에 ffmpeg 가 필요합니다: {exc}\n"
                "GIF 로 저장하려면 확장자를 .gif 로 지정하세요."
            ) from exc
    else:
        raise ValueError(f"지원하지 않는 확장자: {suffix} (.gif/.mp4)")

    plt.close(fig)
    _ = legend  # 사용 표시
    return out_path


def _setup_korean_font() -> None:
    """matplotlib 에서 한글 깨짐을 방지."""
    import matplotlib.font_manager as fm

    candidates = [
        "Malgun Gothic",   # Windows 기본
        "AppleGothic",      # macOS
        "NanumGothic",      # 리눅스(나눔)
        "Noto Sans CJK KR",  # 리눅스(노토)
    ]
    available = {f.name for f in fm.fontManager.ttflist}
    for name in candidates:
        if name in available:
            plt.rcParams["font.family"] = name
            break
    plt.rcParams["axes.unicode_minus"] = False


# ---------------------------------------------------------------------------
# 5) 인터랙티브 뷰어 (마우스 오버 툴팁)
# ---------------------------------------------------------------------------


def _find_box_at(x: float, y: float) -> Optional[tuple[str, Box]]:
    """마우스 좌표에 해당하는 박스를 찾는다."""
    for name, box in _BOXES.items():
        if box.x <= x <= box.x + box.w and box.y <= y <= box.y + box.h:
            return (name, box)
    return None


def _get_tooltip_text(
    box_name: str, box: Box, snap: FactoryState
) -> str:
    """박스와 현재 상태에 따른 툴팁 텍스트를 생성한다."""
    base_desc = box.description if box.description else f"[{box.title}]"

    # 동적 상태 정보 추가
    status_lines = []

    if box_name == "inbound":
        status_lines.append(f"\n━━ 현재 상태 ━━")
        status_lines.append(f"• 대기 트럭: {snap.truck_inbound_active}대")
        status_lines.append(f"• 누적 입고: {snap.trucks_in_done}대")
    elif box_name == "weigh_in":
        status_lines.append(f"\n━━ 현재 상태 ━━")
        status_lines.append(f"• 계근 중: {min(snap.truck_inbound_active, 1)}대")
    elif box_name == "unload":
        status_lines.append(f"\n━━ 현재 상태 ━━")
        status_lines.append(f"• 하역 중: {min(snap.sort_queue, 2)}대")
    elif box_name == "sort_yard":
        status_lines.append(f"\n━━ 현재 상태 ━━")
        status_lines.append(f"• 대기 더미: {snap.sort_queue}개")
        status_lines.append(f"• 용량 사용: {snap.sort_queue}/{box.max_n} ({100*snap.sort_queue/box.max_n:.0f}%)")
    elif box_name == "press":
        status_lines.append(f"\n━━ 현재 상태 ━━")
        status_lines.append(f"• 대기 sub-pile: {snap.press_queue}개")
        status_lines.append(f"• 용량 사용: {snap.press_queue}/{box.max_n} ({100*snap.press_queue/box.max_n:.0f}%)")
    elif box_name == "pallet":
        status_lines.append(f"\n━━ 현재 상태 ━━")
        status_lines.append(f"• 적재 파레트: {snap.pallet_buffer}개")
        status_lines.append(f"• 용량 사용: {snap.pallet_buffer}/{box.max_n} ({100*snap.pallet_buffer/box.max_n:.0f}%)")
        status_lines.append(f"• 추정 중량: {snap.pallet_buffer * 0.5:.1f}톤")
    elif box_name == "elevator":
        is_active = any(s == "charging" for s in snap.furnace_state.values())
        status_lines.append(f"\n━━ 현재 상태 ━━")
        status_lines.append(f"• 작동 중: {'예' if is_active else '아니오'}")
    elif box_name == "furnace1":
        f_state = snap.furnace_state.get(1, "idle")
        status_lines.append(f"\n━━ 현재 상태 ━━")
        status_lines.append(f"• 상태: {f_state}")
        status_lines.append(_FURNACE_STATE_DESC.get(f_state, ""))
        status_lines.append(f"\n• 누적 배치 완료: {snap.melt_batches_done}회")
    elif box_name == "furnace2":
        f_state = snap.furnace_state.get(2, "idle")
        status_lines.append(f"\n━━ 현재 상태 ━━")
        status_lines.append(f"• 상태: {f_state}")
        status_lines.append(_FURNACE_STATE_DESC.get(f_state, ""))
        status_lines.append(f"\n• 누적 배치 완료: {snap.melt_batches_done}회")
    elif box_name == "flake_line":
        f1 = snap.furnace_state.get(1, "idle")
        f2 = snap.furnace_state.get(2, "idle")
        is_active = "casting" in (f1, f2)
        status_lines.append(f"\n━━ 현재 상태 ━━")
        status_lines.append(f"• 가동 중: {'예' if is_active else '아니오'}")
        status_lines.append(f"• 누적 생산: {snap.flake_units_total}포대")
    elif box_name == "scr_line":
        f1 = snap.furnace_state.get(1, "idle")
        f2 = snap.furnace_state.get(2, "idle")
        is_active = "casting" in (f1, f2)
        status_lines.append(f"\n━━ 현재 상태 ━━")
        status_lines.append(f"• 가동 중: {'예' if is_active else '아니오'}")
        status_lines.append(f"• 누적 생산: {snap.scr_units_total}코일")
    elif box_name == "flake_buf":
        status_lines.append(f"\n━━ 현재 상태 ━━")
        status_lines.append(f"• 적재량: {snap.flake_buffer}포대")
        status_lines.append(f"• 용량 사용: {snap.flake_buffer}/{box.max_n} ({100*snap.flake_buffer/box.max_n:.0f}%)")
        status_lines.append(f"• 추정 중량: {snap.flake_buffer * 0.025:.1f}톤")
        status_lines.append(f"• 누적 출하: {snap.flake_dispatched_ton:.1f}톤")
    elif box_name == "scr_buf":
        status_lines.append(f"\n━━ 현재 상태 ━━")
        status_lines.append(f"• 적재량: {snap.scr_buffer}코일")
        status_lines.append(f"• 용량 사용: {snap.scr_buffer}/{box.max_n} ({100*snap.scr_buffer/box.max_n:.0f}%)")
        status_lines.append(f"• 추정 중량: {snap.scr_buffer * 4:.1f}톤")
        status_lines.append(f"• 누적 출하: {snap.scr_dispatched_ton:.1f}톤")
    elif box_name == "out_truck":
        status_lines.append(f"\n━━ 현재 상태 ━━")
        status_lines.append(f"• 대기 트럭: {snap.truck_outbound_active}대")
        status_lines.append(f"• 누적 출하: {snap.trucks_out_done}대")
    elif box_name == "weigh_out":
        status_lines.append(f"\n━━ 현재 상태 ━━")
        status_lines.append(f"• 계근 중: {min(snap.truck_outbound_active, 1)}대")
    elif box_name == "out_load":
        status_lines.append(f"\n━━ 현재 상태 ━━")
        status_lines.append(f"• 상차 중: {snap.truck_outbound_active}대")

    return base_desc + "\n".join(status_lines)


class InteractiveViewer:
    """마우스 오버 툴팁을 지원하는 인터랙티브 뷰어."""

    def __init__(
        self,
        fig,
        ax_factory,
        snapshots: list[FactoryState],
        cfg: SimulationConfig,
    ):
        self.fig = fig
        self.ax_factory = ax_factory
        self.snapshots = snapshots
        self.cfg = cfg
        self.current_frame = 0
        self.tooltip_annotation = None
        self.last_box_name = None

        # 툴팁 어노테이션 생성
        self.tooltip_annotation = ax_factory.annotate(
            "",
            xy=(0, 0),
            xytext=(15, 15),
            textcoords="offset points",
            bbox=dict(
                boxstyle="round,pad=0.5",
                fc="#fffde7",
                ec="#fbc02d",
                alpha=0.95,
                linewidth=1.5,
            ),
            fontsize=8,
            ha="left",
            va="bottom",
            visible=False,
            zorder=100,
        )

        # 이벤트 연결
        self.fig.canvas.mpl_connect("motion_notify_event", self._on_mouse_move)

    def _on_mouse_move(self, event):
        """마우스 이동 이벤트 처리."""
        if event.inaxes != self.ax_factory:
            self._hide_tooltip()
            return

        result = _find_box_at(event.xdata, event.ydata)
        if result is None:
            self._hide_tooltip()
            return

        box_name, box = result

        # 같은 박스 위에 있으면 위치만 업데이트
        if box_name == self.last_box_name:
            self.tooltip_annotation.xy = (event.xdata, event.ydata)
            self.fig.canvas.draw_idle()
            return

        # 새로운 박스 - 툴팁 텍스트 업데이트
        self.last_box_name = box_name
        snap = self.snapshots[self.current_frame]
        tooltip_text = _get_tooltip_text(box_name, box, snap)

        self.tooltip_annotation.set_text(tooltip_text)
        self.tooltip_annotation.xy = (event.xdata, event.ydata)

        # 화면 경계 처리 - 오른쪽 끝이면 왼쪽으로 표시
        if event.xdata > 10:
            self.tooltip_annotation.set_ha("right")
            self.tooltip_annotation.xyann = (-15, 15)
        else:
            self.tooltip_annotation.set_ha("left")
            self.tooltip_annotation.xyann = (15, 15)

        self.tooltip_annotation.set_visible(True)
        self.fig.canvas.draw_idle()

    def _hide_tooltip(self):
        """툴팁 숨기기."""
        if self.tooltip_annotation.get_visible():
            self.tooltip_annotation.set_visible(False)
            self.last_box_name = None
            self.fig.canvas.draw_idle()

    def update_frame(self, frame_idx: int):
        """프레임 업데이트 시 호출."""
        self.current_frame = frame_idx
        # 현재 표시 중인 툴팁이 있으면 상태 정보 갱신
        if self.last_box_name is not None:
            result = _find_box_at(
                self.tooltip_annotation.xy[0],
                self.tooltip_annotation.xy[1],
            )
            if result:
                box_name, box = result
                snap = self.snapshots[frame_idx]
                tooltip_text = _get_tooltip_text(box_name, box, snap)
                self.tooltip_annotation.set_text(tooltip_text)


def show_interactive_factory(
    metrics: Metrics,
    cfg: SimulationConfig,
    step_min: float = 60.0,
    start_frame: int = 0,
) -> None:
    """인터랙티브 모드로 공장 시각화를 표시합니다.

    마우스를 각 공정 박스 위에 올리면 상세 설명과 현재 상태가 표시됩니다.
    키보드 좌/우 화살표로 프레임을 이동할 수 있습니다.

    Parameters
    ----------
    metrics : 시뮬레이션 결과 메트릭스
    cfg : 시뮬레이션 설정
    step_min : 한 프레임이 실시간 몇 분에 해당하는지
    start_frame : 시작 프레임 인덱스
    """
    # Agg 백엔드 해제하고 인터랙티브 백엔드 사용
    import matplotlib
    matplotlib.use("TkAgg")
    import matplotlib.pyplot as plt

    snapshots = build_state_timeline(metrics, cfg.sim_horizon_min, step_min)
    if not snapshots:
        raise RuntimeError("스냅샷이 비어있습니다")

    _setup_korean_font()

    fig = plt.figure(figsize=(16, 9))
    gs = fig.add_gridspec(
        nrows=4, ncols=3,
        height_ratios=[2.4, 2.4, 1.0, 1.2],
        hspace=0.35, wspace=0.25,
    )
    ax_factory = fig.add_subplot(gs[0:3, :])
    ax_pallet = fig.add_subplot(gs[3, 0])
    ax_flake = fig.add_subplot(gs[3, 1])
    ax_scr = fig.add_subplot(gs[3, 2])

    # 범례 생성 (상세 설명 포함)
    legend_handles = [
        Rectangle((0, 0), 1, 1, color=_FURNACE_COLOR[s], label=s)
        for s in ("idle", "charging", "melting", "casting")
    ]
    fig.legend(
        legend_handles,
        ["대기 (idle)", "장입 (charging)", "용해 (melting)", "주조 (casting)"],
        loc="upper right",
        ncol=4,
        fontsize=9,
        frameon=True,
        bbox_to_anchor=(0.99, 0.98),
        title="반사로 상태",
        title_fontsize=9,
    )

    # 인터랙티브 뷰어 생성
    viewer = InteractiveViewer(fig, ax_factory, snapshots, cfg)
    current_frame = [start_frame]  # 리스트로 감싸서 클로저에서 수정 가능하게

    def draw_frame(idx: int):
        idx = max(0, min(idx, len(snapshots) - 1))
        current_frame[0] = idx
        snap = snapshots[idx]
        _draw_factory(ax_factory, snap, cfg)
        _draw_timeseries(ax_pallet, ax_flake, ax_scr, snapshots, snap.t)
        viewer.update_frame(idx)
        fig.canvas.draw_idle()

    def on_key(event):
        if event.key == "right":
            draw_frame(current_frame[0] + 1)
        elif event.key == "left":
            draw_frame(current_frame[0] - 1)
        elif event.key == "home":
            draw_frame(0)
        elif event.key == "end":
            draw_frame(len(snapshots) - 1)
        elif event.key == "pageup":
            draw_frame(current_frame[0] + 10)
        elif event.key == "pagedown":
            draw_frame(current_frame[0] - 10)

    fig.canvas.mpl_connect("key_press_event", on_key)

    # 안내 텍스트
    fig.text(
        0.5, 0.01,
        "조작: ←/→ 프레임 이동 | PageUp/Down 10프레임 이동 | Home/End 처음/끝 | "
        "마우스 오버: 상세 설명 표시",
        ha="center", fontsize=9, color="#666666",
    )
    fig.text(
        0.995, 0.018, _BUILD_INFO_TEXT,
        ha="right", va="bottom", fontsize=8, color="#444444",
        zorder=20,
    )

    # 초기 프레임 그리기
    draw_frame(start_frame)

    plt.tight_layout(rect=[0, 0.06, 1, 0.97])
    plt.show()

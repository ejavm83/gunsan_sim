"""시뮬레이션 결과 시각화 모듈.

matplotlib 으로 다음 차트를 PNG 파일로 저장한다.

  1. 파레트 버퍼 / 완제품 버퍼 시계열 점유량
  2. 반사로(furnace) Gantt 차트 (이벤트 로그 기반)
  3. 누적 입고/출하 트럭 그래프
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import matplotlib

matplotlib.use("Agg")  # headless 환경 대응

import matplotlib.pyplot as plt  # noqa: E402

from metrics import Event, Metrics  # noqa: E402


def _step_xy(samples: Iterable[tuple[float, int]]) -> tuple[list[float], list[int]]:
    xs: list[float] = []
    ys: list[int] = []
    last_y = 0
    for x, y in samples:
        if xs:
            xs.append(x)
            ys.append(last_y)
        xs.append(x)
        ys.append(y)
        last_y = y
    return xs, ys


def plot_buffer_levels(metrics: Metrics, out_path: Path) -> None:
    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True)

    px, py = _step_xy(metrics.pallet_buffer_levels)
    axes[0].plot([t / 60 for t in px], py, color="tab:blue")
    axes[0].set_ylabel("Pallets in buffer")
    axes[0].set_title("Stage 2/3 - Pallet buffer occupancy")
    axes[0].grid(True, alpha=0.3)

    fx, fy = _step_xy(metrics.flake_buffer_levels)
    axes[1].plot([t / 60 for t in fx], fy, color="tab:cyan")
    axes[1].set_ylabel("Flake bags")
    axes[1].set_title("Stage 4/5 - Cu-flake buffer (blue product)")
    axes[1].grid(True, alpha=0.3)

    sx, sy = _step_xy(metrics.scr_buffer_levels)
    axes[2].plot([t / 60 for t in sx], sy, color="tab:red")
    axes[2].set_ylabel("SCR coils")
    axes[2].set_title("Stage 4/5 - SCR coil buffer (red product)")
    axes[2].set_xlabel("Time (hours)")
    axes[2].grid(True, alpha=0.3)

    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def plot_furnace_gantt(metrics: Metrics, out_path: Path) -> None:
    """이벤트 로그에서 furnace 별 melt_start/melt_done 구간을 추출해 Gantt 출력."""
    starts: dict[int, list[tuple[float, str]]] = {}
    intervals: dict[int, list[tuple[float, float, str]]] = {}

    for ev in metrics.events:
        if ev.stage != "melting":
            continue
        fid = ev.detail.get("furnace")
        if fid is None:
            continue
        if ev.kind in ("batch_collected", "elevator_done", "melt_start", "melt_done", "batch_done"):
            starts.setdefault(fid, []).append((ev.time_min, ev.kind))

    for fid, evs in starts.items():
        evs.sort()
        cur_label = None
        cur_start: float | None = None
        for t, kind in evs:
            if kind == "batch_collected":
                cur_label = "elevator+setup"
                cur_start = t
            elif kind == "melt_start" and cur_start is not None:
                intervals.setdefault(fid, []).append((cur_start, t, "elevator+setup"))
                cur_start = t
                cur_label = "melt"
            elif kind == "melt_done" and cur_start is not None:
                intervals.setdefault(fid, []).append((cur_start, t, "melt"))
                cur_start = t
                cur_label = "casting"
            elif kind == "batch_done" and cur_start is not None:
                intervals.setdefault(fid, []).append((cur_start, t, "casting"))
                cur_start = None
                cur_label = None

    color_map = {
        "elevator+setup": "tab:gray",
        "melt": "tab:orange",
        "casting": "tab:green",
    }

    fig, ax = plt.subplots(figsize=(11, 3.5))
    for i, fid in enumerate(sorted(intervals.keys())):
        for s, e, label in intervals[fid]:
            ax.barh(
                y=fid,
                width=(e - s) / 60,
                left=s / 60,
                color=color_map.get(label, "tab:blue"),
                edgecolor="black",
                linewidth=0.4,
            )
    ax.set_xlabel("Time (hours)")
    ax.set_ylabel("Furnace ID")
    ax.set_title("Reverberatory furnace utilization (gray=charge/setup, orange=melt 12h, green=casting)")
    ax.set_yticks(sorted(intervals.keys()))
    ax.grid(True, axis="x", alpha=0.3)

    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in color_map.values()]
    ax.legend(handles, list(color_map.keys()), loc="upper right")

    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def plot_truck_cumulative(metrics: Metrics, out_path: Path) -> None:
    in_arrive: list[float] = []
    in_depart: list[float] = []
    out_arrive: list[float] = []
    out_depart: list[float] = []
    for ev in metrics.events:
        if ev.stage == "inbound" and ev.kind == "arrive":
            in_arrive.append(ev.time_min)
        elif ev.stage == "inbound" and ev.kind == "depart":
            in_depart.append(ev.time_min)
        elif ev.stage == "outbound" and ev.kind == "arrive":
            out_arrive.append(ev.time_min)
        elif ev.stage == "outbound" and ev.kind == "depart":
            out_depart.append(ev.time_min)

    fig, ax = plt.subplots(figsize=(10, 4))
    for series, label, color in [
        (in_arrive, "Inbound arrive", "tab:blue"),
        (in_depart, "Inbound depart", "tab:cyan"),
        (out_arrive, "Outbound arrive", "tab:red"),
        (out_depart, "Outbound depart", "tab:orange"),
    ]:
        if not series:
            continue
        series_sorted = sorted(series)
        xs = [t / 60 for t in series_sorted]
        ys = list(range(1, len(xs) + 1))
        ax.plot(xs, ys, label=label, color=color)
    ax.set_xlabel("Time (hours)")
    ax.set_ylabel("Cumulative truck count")
    ax.set_title("Truck arrivals and departures (cumulative)")
    ax.legend()
    ax.grid(True, alpha=0.3)

    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def render_all(metrics: Metrics, out_dir: str | Path = "out") -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = [
        out / "buffer_levels.png",
        out / "furnace_gantt.png",
        out / "truck_cumulative.png",
    ]
    plot_buffer_levels(metrics, files[0])
    plot_furnace_gantt(metrics, files[1])
    plot_truck_cumulative(metrics, files[2])
    return files

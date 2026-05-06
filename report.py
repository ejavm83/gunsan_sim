"""인터랙티브 HTML 리포트 생성기.

`Metrics` 와 `SimulationConfig` 를 받아 Plotly 차트와 한국어 해설이 포함된
단일 HTML 파일을 생성한다. 별도 서버 없이 브라우저로 바로 열 수 있도록
plotly.js 는 CDN 으로 주입한다.

사용 예::

    from config import DEFAULT_CONFIG
    from simulation import run_simulation
    from report import generate_report

    metrics = run_simulation(DEFAULT_CONFIG)
    generate_report(metrics, DEFAULT_CONFIG, out_path="out/report.html")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any

import plotly.graph_objects as go

from config import SimulationConfig
from metrics import Event, Metrics


# ---------------------------------------------------------------------------
# 1) 자동 분석
# ---------------------------------------------------------------------------


@dataclass
class Analysis:
    summary: dict[str, Any]
    util: dict[str, float]              # 자원 가동률 (0~1)
    queue_stats: dict[str, dict[str, float]]  # 평균/최대/표준편차
    bottleneck: str
    bottleneck_reason: str
    insights: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    daily_throughput_ton: list[tuple[int, float, float]] = field(default_factory=list)
    # (day, flake_ton, scr_ton)


def _buffer_stats(samples: list[tuple[float, int]], horizon_min: int) -> dict[str, float]:
    """step 형 시계열의 시간 가중 평균/최대/표준편차."""
    if not samples:
        return {"avg": 0.0, "max": 0.0, "p95": 0.0}
    times = [s[0] for s in samples] + [horizon_min]
    values = [s[1] for s in samples]
    weighted_sum = 0.0
    total = 0.0
    bins: list[tuple[float, int]] = []  # (duration, value)
    for i in range(len(values)):
        dt = times[i + 1] - times[i]
        if dt <= 0:
            continue
        bins.append((dt, values[i]))
        weighted_sum += dt * values[i]
        total += dt
    avg = weighted_sum / total if total > 0 else 0.0
    max_v = max(values) if values else 0.0
    # 시간 가중 95-percentile 근사
    bins.sort(key=lambda b: b[1])
    cum = 0.0
    p95 = max_v
    for dt, v in bins:
        cum += dt
        if cum >= 0.95 * total:
            p95 = v
            break
    return {"avg": avg, "max": float(max_v), "p95": float(p95)}


def _per_day_throughput(metrics: Metrics, days: int) -> list[tuple[int, float, float]]:
    flake_per_day = [0.0] * days
    scr_per_day = [0.0] * days
    for ev in metrics.events:
        if ev.stage != "casting":
            continue
        if ev.kind == "flake_done":
            d = int(ev.time_min // (24 * 60))
            if 0 <= d < days:
                flake_per_day[d] += float(ev.detail.get("units", 0))
        elif ev.kind == "scr_done":
            d = int(ev.time_min // (24 * 60))
            if 0 <= d < days:
                scr_per_day[d] += float(ev.detail.get("units", 0)) * 4.0
    return [(d + 1, flake_per_day[d], scr_per_day[d]) for d in range(days)]


def analyze(metrics: Metrics, cfg: SimulationConfig) -> Analysis:
    summary = metrics.summary(cfg.sim_horizon_min)
    horizon = cfg.sim_horizon_min

    # ---- 자원 가동률 추정 ----
    sc = cfg.sorting
    mc = cfg.melting
    cc = cfg.casting

    # 압착기: pallet 1개 = 5 block × (5+1.5+2)분
    press_busy_per_pallet = sc.blocks_per_subpile * (
        sc.forklift_load_min + sc.press_min_per_block + sc.pallet_stack_min
    )
    press_util = (metrics.pallets_produced * press_busy_per_pallet) / max(
        horizon * sc.press_machines, 1
    )

    # 선별 워커: 트럭 1대 30분 (sub_piles_per_truck 단위 처리)
    sort_busy = metrics.truck_in_done * 30.0
    sort_util = sort_busy / max(horizon * sc.sorters, 1)

    # 반사로: 배치당 (사전준비+용해+홀딩+주조) 시간을 점유, 엘리베이터 시간은 별도
    casting_max = max(
        mc.batch_ton * cc.flake_ratio / cc.flake_unit_ton * cc.flake_min_per_unit,
        mc.batch_ton * cc.scr_ratio / cc.scr_unit_ton * cc.scr_min_per_unit,
    )
    furnace_busy_per_batch = mc.setup_min + mc.melting_min + cc.holding_setup_min + casting_max
    furnace_util = (metrics.batches_completed * furnace_busy_per_batch) / max(
        horizon * mc.furnace_count, 1
    )

    # 엘리베이터: 배치당 trips × cycle
    trips = mc.pallets_per_batch // mc.elevator_pallets_per_trip
    elevator_busy = metrics.batches_started * trips * mc.elevator_cycle_min
    elevator_util = elevator_busy / max(horizon * mc.elevator_count, 1)

    # 계근대: 입고/출고 × 10분 (in 5 + out 5)
    weigh_in_min = cfg.inbound.weigh_in_min + cfg.inbound.weigh_out_min
    weigh_out_min = cfg.outbound.weigh_in_min + cfg.outbound.weigh_out_min
    weigh_busy = (
        metrics.truck_in_done * weigh_in_min + metrics.truck_out_done * weigh_out_min
    )
    weigh_util = weigh_busy / max(horizon * cfg.inbound.weighbridge_count, 1)

    util = {
        "선별 워커": sort_util,
        "압착기": press_util,
        "엘리베이터": elevator_util,
        "반사로(평균)": furnace_util,
        "계근대": weigh_util,
    }

    # ---- 큐/버퍼 통계 ----
    queue_stats = {
        "파레트 버퍼": _buffer_stats(metrics.pallet_buffer_levels, horizon),
        "퓨플레이크 야적": _buffer_stats(metrics.flake_buffer_levels, horizon),
        "SCR 코일 야적": _buffer_stats(metrics.scr_buffer_levels, horizon),
    }

    # ---- 병목 식별 ----
    bottleneck_resource = max(util.items(), key=lambda kv: kv[1])
    bottleneck = bottleneck_resource[0]
    bottleneck_pct = bottleneck_resource[1] * 100
    bottleneck_reason = (
        f"가동률 {bottleneck_pct:.1f}% 로 가장 높습니다. "
        f"이 자원이 막혀 있는 동안 후속 공정이 대기합니다."
    )

    # ---- 인사이트 자동 생성 ----
    insights: list[str] = []
    recommendations: list[str] = []

    if press_util > 0.9:
        insights.append(
            f"압착기 가동률이 {press_util*100:.1f}% 로 사실상 풀가동 상태입니다. "
            f"하루 동안 trucks_per_day={cfg.inbound.trucks_per_day}대로 들어오는 "
            f"{cfg.inbound.trucks_per_day * cfg.inbound.payload_ton:.0f} t 의 입고 물량을 "
            f"소화하려면 압착 처리속도가 핵심입니다."
        )
        recommendations.append(
            "압착기를 1대 추가(=2대 병렬)하면 파레트 생성속도가 약 2배가 되어 "
            "용해 공정이 비로소 진짜 병목으로 드러나고, 두 반사로를 모두 활용할 수 있습니다."
        )
    elif press_util > 0.7:
        insights.append(
            f"압착기 가동률이 {press_util*100:.1f}% 로 안정 상태이지만 "
            "트럭 1대만 늘어도 빠르게 포화될 수 있는 구간입니다."
        )

    if furnace_util < 0.6:
        insights.append(
            f"반사로 평균 가동률이 {furnace_util*100:.1f}% 로 절반 수준입니다. "
            f"두 반사로 중 한쪽은 거의 노는 상태이며, 이는 앞단 압착에서 파레트가 "
            "충분히 빨리 만들어지지 못하기 때문입니다."
        )
    elif furnace_util > 0.85:
        insights.append(
            f"반사로 평균 가동률이 {furnace_util*100:.1f}% 로 거의 풀가동입니다. "
            f"용해/주조가 진짜 병목이며, CP-SAT 스케줄링으로도 메이크스팬 단축이 어렵습니다."
        )

    pallet_avg = queue_stats["파레트 버퍼"]["avg"]
    pallet_max = queue_stats["파레트 버퍼"]["max"]
    pallet_cap = sc.pallet_buffer_capacity
    if pallet_max >= pallet_cap * 0.9:
        insights.append(
            f"파레트 버퍼가 최대 {pallet_max:.0f}/{pallet_cap}개까지 차올랐습니다. "
            "용해가 막혀 있는 동안 압착 산출물이 정체되어 야적장이 한계에 가까워지는 신호입니다."
        )
    if pallet_avg > pallet_cap * 0.5:
        recommendations.append(
            f"파레트 버퍼 평균 점유가 {pallet_avg:.0f}개로 절반을 넘습니다. "
            "용해 일정을 더 자주 시작하거나(작은 배치), 야적장 면적을 늘리는 것을 검토하세요."
        )

    flake_avg = queue_stats["퓨플레이크 야적"]["avg"]
    scr_avg = queue_stats["SCR 코일 야적"]["avg"]
    if flake_avg < 5 and scr_avg < 5:
        insights.append(
            "완제품 야적장 평균 점유가 매우 낮습니다(< 5). 출하 트럭이 거의 만들어지자마자 가져가고 있어 "
            "제품 측 흐름은 원활합니다."
        )

    if metrics.truck_in_done < cfg.inbound.trucks_per_day * cfg.sim_days * 0.95:
        expected = cfg.inbound.trucks_per_day * cfg.sim_days
        insights.append(
            f"입고 트럭 {metrics.truck_in_done}대/{expected}대 처리 - "
            "horizon 내에 마지막 트럭이 출차 못한 케이스가 일부 있을 수 있습니다."
        )

    if cfg.melting.furnace_count >= 2 and furnace_util < 0.5:
        recommendations.append(
            "현재 반사로 두 대 중 한 대는 거의 사용되지 않고 있습니다. "
            "운영 비용 절감을 위해 한 대만 가동하면서 정기 점검에 활용하거나, "
            "앞단 처리량을 늘려 두 대 모두 가동하는 것을 검토하세요."
        )

    return Analysis(
        summary=summary,
        util=util,
        queue_stats=queue_stats,
        bottleneck=bottleneck,
        bottleneck_reason=bottleneck_reason,
        insights=insights,
        recommendations=recommendations,
        daily_throughput_ton=_per_day_throughput(metrics, cfg.sim_days),
    )


# ---------------------------------------------------------------------------
# 2) Plotly 차트
# ---------------------------------------------------------------------------


def _step_xy(samples: list[tuple[float, int]]) -> tuple[list[float], list[int]]:
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


def _fig_buffer_levels(metrics: Metrics, cfg: SimulationConfig) -> go.Figure:
    fig = go.Figure()
    for samples, name, color, cap in [
        (metrics.pallet_buffer_levels, "파레트 버퍼", "#2563eb",
         cfg.sorting.pallet_buffer_capacity),
        (metrics.flake_buffer_levels, "퓨플레이크 야적", "#0ea5e9",
         cfg.outbound.flake_buffer_unit),
        (metrics.scr_buffer_levels, "SCR 코일 야적", "#dc2626",
         cfg.outbound.scr_buffer_unit),
    ]:
        xs, ys = _step_xy(samples)
        fig.add_trace(go.Scatter(
            x=[t / 60 for t in xs], y=ys,
            mode="lines", name=f"{name} (max={cap})",
            line=dict(color=color, width=2),
            hovertemplate=f"{name}<br>시간 %{{x:.1f}}h<br>점유 %{{y}}<extra></extra>",
        ))
    fig.update_layout(
        title="버퍼/야적장 점유량 시계열",
        xaxis_title="시간 (시간)",
        yaxis_title="점유 개수",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        margin=dict(l=50, r=20, t=60, b=50),
        height=400,
        hovermode="x unified",
    )
    return fig


def _fig_furnace_gantt(metrics: Metrics) -> go.Figure:
    """이벤트 로그로부터 furnace 별 charging/melt/casting 구간을 추출."""
    intervals: dict[int, list[tuple[float, float, str]]] = {}
    starts: dict[int, list[tuple[float, str]]] = {}
    for ev in metrics.events:
        if ev.stage != "melting":
            continue
        fid = ev.detail.get("furnace")
        if fid is None:
            continue
        if ev.kind in ("batch_collected", "melt_start", "melt_done", "batch_done"):
            starts.setdefault(fid, []).append((ev.time_min, ev.kind))

    for fid, evs in starts.items():
        evs.sort()
        cur_label = None
        cur_start: float | None = None
        for t, kind in evs:
            if kind == "batch_collected":
                cur_label, cur_start = "장입+준비", t
            elif kind == "melt_start" and cur_start is not None:
                intervals.setdefault(fid, []).append((cur_start, t, "장입+준비"))
                cur_label, cur_start = "용해(12h)", t
            elif kind == "melt_done" and cur_start is not None:
                intervals.setdefault(fid, []).append((cur_start, t, "용해(12h)"))
                cur_label, cur_start = "주조", t
            elif kind == "batch_done" and cur_start is not None:
                intervals.setdefault(fid, []).append((cur_start, t, "주조"))
                cur_start = None
                cur_label = None

    color_map = {
        "장입+준비": "#9ca3af",
        "용해(12h)": "#ef4444",
        "주조": "#22c55e",
    }

    fig = go.Figure()
    legend_seen: set[str] = set()
    for fid in sorted(intervals.keys()):
        for s, e, label in intervals[fid]:
            show_legend = label not in legend_seen
            legend_seen.add(label)
            start_h = s / 60
            end_h = e / 60
            fig.add_trace(go.Bar(
                x=[end_h - start_h],
                y=[f"반사로 {fid}"],
                base=[start_h],
                customdata=[[end_h]],
                orientation="h",
                marker_color=color_map[label],
                name=label,
                legendgroup=label,
                showlegend=show_legend,
                hovertemplate=(
                    f"반사로 {fid} - {label}<br>"
                    "시작 %{base:.1f}h<br>"
                    "종료 %{customdata[0]:.1f}h<extra></extra>"
                ),
            ))
    fig.update_layout(
        title="반사로 배치 Gantt (회색=장입/준비, 빨강=12h 용해, 초록=주조)",
        xaxis_title="시간 (시간)",
        barmode="overlay",
        height=300,
        margin=dict(l=70, r=20, t=60, b=50),
    )
    return fig


def _fig_truck_cumulative(metrics: Metrics) -> go.Figure:
    series: dict[str, list[float]] = {
        "입고 도착": [], "입고 출차": [],
        "출하 도착": [], "출하 출차": [],
    }
    for ev in metrics.events:
        if ev.stage == "inbound":
            if ev.kind == "arrive":
                series["입고 도착"].append(ev.time_min)
            elif ev.kind == "depart":
                series["입고 출차"].append(ev.time_min)
        elif ev.stage == "outbound":
            if ev.kind == "arrive":
                series["출하 도착"].append(ev.time_min)
            elif ev.kind == "depart":
                series["출하 출차"].append(ev.time_min)

    color_map = {
        "입고 도착": "#1d4ed8",
        "입고 출차": "#60a5fa",
        "출하 도착": "#b91c1c",
        "출하 출차": "#fb923c",
    }
    fig = go.Figure()
    for name, ts in series.items():
        if not ts:
            continue
        ts_sorted = sorted(ts)
        xs = [t / 60 for t in ts_sorted]
        ys = list(range(1, len(xs) + 1))
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode="lines", name=name,
            line=dict(color=color_map[name], width=2),
            hovertemplate=f"{name}<br>%{{y}}대 @ %{{x:.1f}}h<extra></extra>",
        ))
    fig.update_layout(
        title="누적 트럭 도착/출차",
        xaxis_title="시간 (시간)",
        yaxis_title="누적 대수",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        height=400,
        margin=dict(l=50, r=20, t=60, b=50),
    )
    return fig


def _fig_utilization(util: dict[str, float]) -> go.Figure:
    names = list(util.keys())
    values = [v * 100 for v in util.values()]
    colors = ["#ef4444" if v >= 90 else "#facc15" if v >= 70 else "#22c55e" for v in values]
    fig = go.Figure(go.Bar(
        x=values, y=names, orientation="h",
        marker_color=colors,
        text=[f"{v:.1f}%" for v in values],
        textposition="outside",
        hovertemplate="%{y}<br>가동률 %{x:.1f}%<extra></extra>",
    ))
    fig.update_layout(
        title="자원 가동률 (90% 이상 빨강, 70% 이상 노랑, 그 미만 초록)",
        xaxis_title="가동률 (%)",
        xaxis=dict(range=[0, 110]),
        height=320,
        margin=dict(l=120, r=40, t=60, b=50),
    )
    return fig


def _fig_lead_time_hist(metrics: Metrics) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Histogram(
        x=metrics.truck_in_lead_times,
        name="입고 트럭 체류시간",
        nbinsx=30,
        marker_color="#1d4ed8",
        opacity=0.75,
    ))
    fig.add_trace(go.Histogram(
        x=metrics.truck_out_lead_times,
        name="출하 트럭 체류시간",
        nbinsx=30,
        marker_color="#dc2626",
        opacity=0.75,
    ))
    fig.update_layout(
        title="트럭 체류시간 분포 (분)",
        xaxis_title="체류시간 (분)",
        yaxis_title="대수",
        barmode="overlay",
        height=320,
        margin=dict(l=50, r=20, t=60, b=50),
    )
    return fig


def _fig_daily_throughput(daily: list[tuple[int, float, float]]) -> go.Figure:
    if not daily:
        return go.Figure()
    days = [d for d, _, _ in daily]
    flake = [f for _, f, _ in daily]
    scr = [s for _, _, s in daily]
    fig = go.Figure()
    fig.add_trace(go.Bar(name="퓨플레이크 (t)", x=days, y=flake, marker_color="#0ea5e9"))
    fig.add_trace(go.Bar(name="SCR 코일 (t)", x=days, y=scr, marker_color="#dc2626"))
    fig.update_layout(
        barmode="stack",
        title="일별 완제품 생산량 (t)",
        xaxis_title="일차",
        yaxis_title="생산량 (t)",
        height=320,
        margin=dict(l=50, r=20, t=60, b=50),
    )
    return fig


# ---------------------------------------------------------------------------
# 3) HTML 조립
# ---------------------------------------------------------------------------


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>군산 공장 하이브리드 공정 시뮬레이션 리포트</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
<style>
  :root {{
    --bg: #f6f7fb;
    --card: #ffffff;
    --border: #e5e7eb;
    --text: #1f2937;
    --muted: #6b7280;
    --accent: #2563eb;
    --warn: #f59e0b;
    --bad: #ef4444;
    --good: #10b981;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 0;
    font-family: -apple-system, "Segoe UI", "Apple SD Gothic Neo",
                 "Malgun Gothic", "Noto Sans KR", sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.55;
  }}
  header {{
    background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 100%);
    color: white;
    padding: 28px 36px;
  }}
  header h1 {{ margin: 0 0 6px; font-size: 24px; }}
  header .meta {{ opacity: 0.85; font-size: 14px; }}
  main {{ max-width: 1280px; margin: 0 auto; padding: 24px 24px 80px; }}
  section.card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 22px 26px;
    margin-bottom: 22px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }}
  h2 {{ margin: 0 0 14px; font-size: 18px; color: #111827; }}
  h2 .num {{ display: inline-block; width: 26px; height: 26px;
             background: var(--accent); color: white; border-radius: 50%;
             text-align: center; line-height: 26px; font-size: 14px;
             margin-right: 8px; }}
  .lead {{ color: var(--muted); margin: -4px 0 16px; font-size: 14px; }}

  .kpi-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
  }}
  .kpi {{
    background: #f9fafb; border: 1px solid var(--border);
    border-radius: 8px; padding: 14px 16px;
  }}
  .kpi .label {{ color: var(--muted); font-size: 12px; }}
  .kpi .value {{ font-size: 22px; font-weight: 700; color: var(--accent); }}
  .kpi .unit {{ color: var(--muted); font-size: 13px; margin-left: 4px; }}

  .insight {{
    background: #ecfeff; border-left: 4px solid #06b6d4;
    padding: 12px 16px; margin: 8px 0; border-radius: 4px;
  }}
  .recommend {{
    background: #fff7ed; border-left: 4px solid #f59e0b;
    padding: 12px 16px; margin: 8px 0; border-radius: 4px;
  }}
  .bottleneck {{
    background: #fef2f2; border-left: 4px solid var(--bad);
    padding: 14px 18px; border-radius: 4px;
  }}
  .bottleneck strong {{ color: var(--bad); }}

  table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
  th, td {{ border-bottom: 1px solid var(--border); padding: 8px 12px; text-align: left; }}
  th {{ background: #f3f4f6; font-weight: 600; }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}

  .flow {{
    display: flex; gap: 4px; align-items: stretch; flex-wrap: wrap;
    margin-top: 8px;
  }}
  .stage {{
    flex: 1 1 180px; background: #eff6ff; border: 1px solid #bfdbfe;
    border-radius: 8px; padding: 12px 14px; min-width: 170px;
  }}
  .stage .name {{ font-weight: 700; color: #1e3a8a; margin-bottom: 4px; }}
  .stage .desc {{ font-size: 12.5px; color: #1e40af; }}
  .stage.bottleneck-stage {{ background: #fef2f2; border-color: #fca5a5; }}
  .stage.bottleneck-stage .name {{ color: #991b1b; }}
  .stage.bottleneck-stage .desc {{ color: #7f1d1d; }}

  .legend {{ font-size: 12px; color: var(--muted); margin-top: 10px; }}
  details > summary {{ cursor: pointer; font-weight: 600; padding: 6px 0; }}

  footer {{
    color: var(--muted); font-size: 12px; padding: 24px 0; text-align: center;
  }}
</style>
</head>
<body>
<header>
  <h1>군산 공장 하이브리드 공정 시뮬레이션 리포트</h1>
  <div class="meta">
    시뮬레이션 기간 {sim_days}일 · 트럭/일 {trucks_per_day}대 · 반사로 {furnace_count}대
    · 압착기 {press_count}대 · 생성 시각 {generated_at}
  </div>
</header>
<main>

<section class="card">
  <h2><span class="num">1</span>핵심 지표 한눈에 보기</h2>
  <p class="lead">시뮬레이션 horizon({sim_horizon_h:.0f}시간) 동안 누적된 KPI 입니다.
  같은 입력에 대한 결과는 <code>--seed</code> 가 같다면 항상 동일합니다.</p>
  <div class="kpi-grid">
    {kpi_cards}
  </div>
</section>

<section class="card">
  <h2><span class="num">2</span>공정 흐름과 병목 진단</h2>
  <p class="lead">5단계 공정의 처리 시간/용량과 시뮬레이션이 식별한 병목 자원입니다.</p>
  <div class="bottleneck">
    <strong>식별된 병목: {bottleneck}</strong> — {bottleneck_reason}
  </div>
  <div class="flow">
    {stages_html}
  </div>
</section>

<section class="card">
  <h2><span class="num">3</span>자원 가동률</h2>
  <p class="lead">각 자원이 시뮬레이션 기간 중 실제로 일한 시간의 비율입니다.
  100%에 가까울수록 풀가동(=병목 가능성), 낮을수록 여유가 있다는 뜻입니다.</p>
  <div id="util_chart"></div>
</section>

<section class="card">
  <h2><span class="num">4</span>버퍼/야적장 점유 시계열</h2>
  <p class="lead">파레트 버퍼와 완제품 야적장이 얼마나 차 있었는지 시간순으로 보여줍니다.
  곡선이 max 라인에 자주 닿으면 그 단계에서 정체가 발생했다는 신호입니다.</p>
  <div id="buffer_chart"></div>
  <table style="margin-top:16px">
    <thead><tr>
      <th>버퍼</th><th class="num">평균 점유</th><th class="num">최대 점유</th>
      <th class="num">95퍼센타일</th>
    </tr></thead>
    <tbody>
      {queue_rows}
    </tbody>
  </table>
</section>

<section class="card">
  <h2><span class="num">5</span>반사로 배치 Gantt</h2>
  <p class="lead">두 반사로가 어떤 시각에 어떤 단계(장입·12시간 용해·주조)에
  있었는지 가로 막대로 표시합니다. 빨간 막대 12시간이 바로 문서가 언급한
  최대 병목 구간입니다.</p>
  <div id="gantt_chart"></div>
</section>

<section class="card">
  <h2><span class="num">6</span>트럭 흐름</h2>
  <p class="lead">왼쪽 곡선의 기울기가 가팔라지면 트럭이 한꺼번에 도착해 정체가 시작된 시점입니다.
  도착선과 출차선의 수직 차이가 동시 체류 트럭 수에 해당합니다.</p>
  <div id="truck_chart"></div>
  <div id="lead_chart"></div>
</section>

<section class="card">
  <h2><span class="num">7</span>일별 생산량</h2>
  <p class="lead">정상 가동에 도달하기까지의 워밍업 구간과 이후의 안정 가동량을 한눈에 비교할 수 있습니다.</p>
  <div id="daily_chart"></div>
</section>

<section class="card">
  <h2><span class="num">8</span>자동 인사이트와 권장사항</h2>
  <p class="lead">규칙 기반 분석이 찾아낸 관찰 포인트와 운영 권장 액션입니다.
  파라미터를 바꿔 다시 실행하면 권장사항도 따라 바뀝니다.</p>
  <h3 style="font-size:15px; margin-top:14px">관찰 포인트</h3>
  {insights_html}
  <h3 style="font-size:15px; margin-top:18px">권장 액션</h3>
  {recommendations_html}
</section>

<section class="card">
  <h2><span class="num">9</span>시뮬레이션 설정 요약</h2>
  <details open>
    <summary>주요 파라미터</summary>
    <table style="margin-top:8px">
      {config_rows}
    </table>
  </details>
</section>

</main>
<footer>
  Generated by gunsan_sim · SimPy · OR-Tools CP-SAT · Plotly
</footer>

<script>
  Plotly.newPlot("util_chart", {util_data}, {util_layout}, {{responsive: true}});
  Plotly.newPlot("buffer_chart", {buffer_data}, {buffer_layout}, {{responsive: true}});
  Plotly.newPlot("gantt_chart", {gantt_data}, {gantt_layout}, {{responsive: true}});
  Plotly.newPlot("truck_chart", {truck_data}, {truck_layout}, {{responsive: true}});
  Plotly.newPlot("lead_chart", {lead_data}, {lead_layout}, {{responsive: true}});
  Plotly.newPlot("daily_chart", {daily_data}, {daily_layout}, {{responsive: true}});
</script>
</body>
</html>
"""


_STAGES = [
    ("1. 입고/하역", "trucks_per_day=10대 × 20t · 1차 계근 5분 · 하역 20분 · 2차 계근 5분",
     "inbound"),
    ("2. 선별/압착", "트럭 1대 30분 정리 → 8 sub-pile → 0.5t × 8.5분 압착 → 파레트 2.5t",
     "press"),
    ("3. 장입/용해", "32파레트(80t) 모이면 엘리베이터 운반 → 2h 준비 → 12h 용해",
     "melting"),
    ("4. 하이브리드 주조", "퓨플레이크 1t/2.5분 + SCR 4t/10분, 비율 3:7",
     "casting"),
    ("5. 출하/야적", "빈 트럭 도착 → 22t 적재(1t flake / 4t SCR) → 2차 계근 → 출차",
     "outbound"),
]


def _kpi_cards_html(summary: dict[str, Any]) -> str:
    cards = [
        ("처리 트럭(입고)", summary["trucks_in_processed"], "대"),
        ("출하 트럭", summary["trucks_out_dispatched"], "대"),
        ("완료 배치", summary["melt_batches_completed"], "회"),
        ("퓨플레이크 생산", f"{summary['flake_ton']:.0f}", "t"),
        ("SCR 코일 생산", f"{summary['scr_ton']:.0f}", "t"),
        ("총 생산", f"{summary['total_product_ton']:.0f}", "t"),
        ("일평균 처리량", f"{summary['throughput_ton_per_day']:.1f}", "t/일"),
        ("입고 평균체류", f"{summary['avg_truck_in_lead_min']:.1f}", "분"),
        ("출하 평균체류", f"{summary['avg_truck_out_lead_min']:.1f}", "분"),
        ("배치 평균시간", f"{summary['avg_melt_batch_min']:.0f}", "분"),
    ]
    parts = []
    for label, value, unit in cards:
        parts.append(
            f'<div class="kpi"><div class="label">{escape(str(label))}</div>'
            f'<div class="value">{escape(str(value))}'
            f'<span class="unit">{escape(str(unit))}</span></div></div>'
        )
    return "\n".join(parts)


def _stages_html(bottleneck: str) -> str:
    bottleneck_key = ""
    for kw, key in [
        ("선별", "press"), ("압착", "press"),
        ("반사로", "melting"), ("엘리베이터", "melting"),
        ("계근", "inbound"),
    ]:
        if kw in bottleneck:
            bottleneck_key = key
            break
    parts = []
    for name, desc, key in _STAGES:
        cls = "stage bottleneck-stage" if key == bottleneck_key else "stage"
        parts.append(
            f'<div class="{cls}"><div class="name">{escape(name)}</div>'
            f'<div class="desc">{escape(desc)}</div></div>'
        )
    return "\n".join(parts)


def _queue_rows_html(queue_stats: dict[str, dict[str, float]]) -> str:
    parts = []
    for name, st in queue_stats.items():
        parts.append(
            f"<tr><td>{escape(name)}</td>"
            f"<td class='num'>{st['avg']:.1f}</td>"
            f"<td class='num'>{st['max']:.0f}</td>"
            f"<td class='num'>{st['p95']:.0f}</td></tr>"
        )
    return "\n".join(parts)


def _bullets(items: list[str], css_class: str) -> str:
    if not items:
        return f'<div class="{css_class}" style="opacity:0.6">'\
               '특별한 항목이 없습니다.</div>'
    return "\n".join(
        f'<div class="{css_class}">{escape(t)}</div>' for t in items
    )


def _config_rows(cfg: SimulationConfig) -> str:
    rows = [
        ("시뮬레이션 일수", cfg.sim_days, "일"),
        ("랜덤 시드", cfg.random_seed, ""),
        ("일 트럭 수", cfg.inbound.trucks_per_day, "대"),
        ("트럭 적재", cfg.inbound.payload_ton, "t"),
        ("계근대 수", cfg.inbound.weighbridge_count, ""),
        ("하역 베이", cfg.inbound.unloading_bays, ""),
        ("선별 워커", cfg.sorting.sorters, ""),
        ("압착기", cfg.sorting.press_machines, ""),
        ("파레트 버퍼 한도", cfg.sorting.pallet_buffer_capacity, "개"),
        ("배치 단위", cfg.melting.batch_ton, "t"),
        ("반사로 수", cfg.melting.furnace_count, ""),
        ("엘리베이터", cfg.melting.elevator_count, ""),
        ("용해 시간", cfg.melting.melting_min, "분"),
        ("flake/scr 비율", f"{cfg.casting.flake_ratio:.0%} : {cfg.casting.scr_ratio:.0%}", ""),
        ("출하 평균간격", cfg.outbound.empty_truck_interval_min, "분"),
    ]
    return "\n".join(
        f"<tr><th>{escape(name)}</th><td>{escape(str(value))}</td>"
        f"<td>{escape(unit)}</td></tr>"
        for name, value, unit in rows
    )


def _fig_to_json(fig: go.Figure) -> tuple[str, str]:
    """Plotly figure 를 (data_json, layout_json) 으로 분리해 반환."""
    spec = fig.to_dict()
    import json
    data_json = json.dumps(spec.get("data", []), default=str)
    layout_json = json.dumps(spec.get("layout", {}), default=str)
    return data_json, layout_json


def generate_report(
    metrics: Metrics,
    cfg: SimulationConfig,
    out_path: str | Path = "out/report.html",
) -> Path:
    """단일 HTML 리포트를 생성한다."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    analysis = analyze(metrics, cfg)

    util_data, util_layout = _fig_to_json(_fig_utilization(analysis.util))
    buffer_data, buffer_layout = _fig_to_json(_fig_buffer_levels(metrics, cfg))
    gantt_data, gantt_layout = _fig_to_json(_fig_furnace_gantt(metrics))
    truck_data, truck_layout = _fig_to_json(_fig_truck_cumulative(metrics))
    lead_data, lead_layout = _fig_to_json(_fig_lead_time_hist(metrics))
    daily_data, daily_layout = _fig_to_json(
        _fig_daily_throughput(analysis.daily_throughput_ton)
    )

    from datetime import datetime

    html = _HTML_TEMPLATE.format(
        sim_days=cfg.sim_days,
        sim_horizon_h=cfg.sim_horizon_min / 60,
        trucks_per_day=cfg.inbound.trucks_per_day,
        furnace_count=cfg.melting.furnace_count,
        press_count=cfg.sorting.press_machines,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        kpi_cards=_kpi_cards_html(analysis.summary),
        bottleneck=escape(analysis.bottleneck),
        bottleneck_reason=escape(analysis.bottleneck_reason),
        stages_html=_stages_html(analysis.bottleneck),
        queue_rows=_queue_rows_html(analysis.queue_stats),
        insights_html=_bullets(analysis.insights, "insight"),
        recommendations_html=_bullets(analysis.recommendations, "recommend"),
        config_rows=_config_rows(cfg),
        util_data=util_data, util_layout=util_layout,
        buffer_data=buffer_data, buffer_layout=buffer_layout,
        gantt_data=gantt_data, gantt_layout=gantt_layout,
        truck_data=truck_data, truck_layout=truck_layout,
        lead_data=lead_data, lead_layout=lead_layout,
        daily_data=daily_data, daily_layout=daily_layout,
    )

    out_path.write_text(html, encoding="utf-8")
    return out_path

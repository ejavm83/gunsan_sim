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

import re
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any

import pandas  # noqa: F401 — plotly 검증기가 pandas.Series 참조 전에 모듈 완료 보장

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
    truck_flow_insights: list[str] = field(default_factory=list)
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


def _truck_flow_insights(metrics: Metrics, cfg: SimulationConfig) -> list[str]:
    """누적 도착·출차와 체류시간을 규칙 기반으로 해설한다 (웹·HTML 공용)."""
    n_in_a = n_in_d = n_out_a = n_out_d = 0
    for ev in metrics.events:
        if ev.stage == "inbound":
            if ev.kind == "arrive":
                n_in_a += 1
            elif ev.kind == "depart":
                n_in_d += 1
        elif ev.stage == "outbound":
            if ev.kind == "arrive":
                n_out_a += 1
            elif ev.kind == "depart":
                n_out_d += 1

    if n_in_a == 0 and n_out_a == 0:
        return ["트럭 도착 이벤트가 없어 트럭 흐름을 해석할 수 없습니다."]

    bullets: list[str] = []
    gap_in = n_in_a - n_in_d
    gap_out = n_out_a - n_out_d
    thr_in = max(3, int(0.02 * max(n_in_a, 1)))

    if n_in_a > 0:
        if gap_in >= thr_in:
            bullets.append(
                f"입고는 누적 도착 {n_in_a}대 대비 출차 {n_in_d}대로, 출차가 {gap_in}대 적습니다. "
                "누적 곡선에서 입고 도착과 입고 출차가 벌어져 있으면 시뮬레이션 종료 시각까지 하역·계근을 "
                "끝내지 못한 트럭이 남았거나, 입고 페이스 대비 앞단 처리가 밀린 상태로 읽을 수 있습니다."
            )
        elif gap_in > 0:
            bullets.append(
                f"입고 누적 도착이 출차보다 {gap_in}대만큼 많습니다. 격차는 작지만, 막바지에 도착한 트럭이 "
                "아직 출차 기록 전일 수 있습니다."
            )
        else:
            bullets.append(
                f"입고 누적 도착 {n_in_a}대와 출차 {n_in_d}대가 맞아 떨어져, 미출차로 남은 입고 트럭은 "
                "없거나 매우 적습니다."
            )

    if n_out_a > 0:
        rel_out = gap_out / max(n_out_a, 1)
        if rel_out < 0.02 and n_out_d > 0:
            bullets.append(
                "출하는 누적 도착·출차가 거의 겹쳐 올라가면, 빈 트럭 방문 후 적재·계근을 마치고 "
                "곧바로 떠나는 리듬에 가깝다고 볼 수 있습니다."
            )
        elif gap_out >= max(2, int(0.02 * n_out_a)):
            bullets.append(
                f"출하 누적 도착이 출차보다 {gap_out}대 많습니다. 야적 재고 대기나 계근 대기로 "
                "출차가 늦게 잡힌 트럭이 남아 있을 수 있습니다."
            )

    tins = metrics.truck_in_lead_times
    touts = metrics.truck_out_lead_times
    if len(tins) >= 3 and len(touts) >= 3:
        med_i = float(median(tins))
        med_o = float(median(touts))
        if med_o > med_i * 1.4:
            bullets.append(
                f"체류시간 분포상 출하(중앙값 약 {med_o:.0f}분)가 입고(약 {med_i:.0f}분)보다 깁니다. "
                "완제품 확보·상차·설정된 빈 트럭 도착 간격의 영향이 큰 경우가 많습니다."
            )
        elif med_i > med_o * 1.4:
            bullets.append(
                f"체류시간 분포상 입고(중앙값 약 {med_i:.0f}분)가 출하(약 {med_o:.0f}분)보다 깁니다. "
                "계근·하역·선별 대기가 출하 적재 대기보다 상대적으로 길었습니다."
            )
        else:
            bullets.append(
                f"입·출하 체류시간 중앙값은 각각 약 {med_i:.0f}분·{med_o:.0f}분으로 비슷한 수준입니다."
            )

    if n_in_a > 0 and gap_in >= thr_in and len(tins) >= 5:
        med_i = float(median(tins))
        if med_i < 90.0:
            bullets.append(
                "참고: 누적 곡선상 입고 출차가 덜 따라오지만, 이미 처리가 끝난 트럭의 체류는 짧게 나타났습니다. "
                "말미에만 밀린 것인지, 간격이 계속 넓어지는지 시간 축을 따라가며 함께 보는 것이 좋습니다."
            )

    s = metrics.summary(cfg.sim_horizon_min)
    avg_i = float(s.get("avg_truck_in_lead_min", 0.0))
    avg_o = float(s.get("avg_truck_out_lead_min", 0.0))
    if avg_i > 60.0:
        bullets.append(
            f"입고 평균 체류는 약 {avg_i:.0f}분으로, 참고용 목표인 60분을 넘었습니다."
        )
    if avg_o > 90.0:
        bullets.append(
            f"출하 평균 체류는 약 {avg_o:.0f}분으로, 참고용 목표인 90분을 넘었습니다."
        )

    if len(bullets) > 8:
        bullets = bullets[:8]

    return bullets if bullets else ["트럭 흐름 데이터가 제한적이어서 추가 해설을 생략합니다."]


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
        "큐프레이크 야적": _buffer_stats(metrics.flake_buffer_levels, horizon),
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

    flake_avg = queue_stats["큐프레이크 야적"]["avg"]
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

    daily_throughput_ton = _per_day_throughput(metrics, cfg.sim_days)

    # ---- 추가 인사이트 (가동률·버퍼·일별·입출 균형) ----
    util_sorted = sorted(util.items(), key=lambda kv: kv[1], reverse=True)
    if len(util_sorted) >= 2:
        top_n, top_u = util_sorted[0]
        sec_u = util_sorted[1][1]
        if top_u - sec_u >= 0.22:
            insights.append(
                f"가동률 상위 자원은 **{top_n}** 약 {top_u * 100:.0f}%이고, "
                f"두 번째와 격차가 큽니다(약 {(top_u - sec_u) * 100:.0f}%p). "
                "개선 과제를 한 곳에 집중하면 체감 효과가 클 가능성이 있습니다."
            )

    if sort_util >= 0.85:
        insights.append(
            f"선별 워커 가동률이 {sort_util * 100:.1f}% 로 높습니다. "
            "입고 페이스가 유지되면 선별이 압착 앞단에서 대기를 만들 수 있습니다."
        )
    elif sort_util < 0.35 and cfg.inbound.trucks_per_day >= 5:
        insights.append(
            f"선별 워커 가동률이 {sort_util * 100:.1f}% 로 낮습니다. "
            "같은 입고 설정이면 압착·반사로 쪽이 더 바쁘거나, 입고 트럭 처리가 끝까지 못 미친 경우일 수 있습니다."
        )

    if elevator_util >= 0.82:
        insights.append(
            f"엘리베이터 가동률이 {elevator_util * 100:.1f}% 입니다. "
            "반사로 장입 페이스와 직결되며, 병목이 반사로일 때 함께 붙어 오르는 경우가 많습니다."
        )

    if weigh_util >= 0.88:
        insights.append(
            f"계근대 가동률이 {weigh_util * 100:.1f}% 로 높습니다. "
            "입·출하 트럭이 같은 계근 자원을 공유하므로, 피크 시간대에 겹치면 체류시간이 함께 늘 수 있습니다."
        )

    oc = cfg.outbound
    flake_cap_n = int(oc.flake_buffer_unit)
    scr_cap_n = int(oc.scr_buffer_unit)
    flake_max = queue_stats["큐프레이크 야적"]["max"]
    scr_max = queue_stats["SCR 코일 야적"]["max"]
    if flake_cap_n > 0 and flake_max >= flake_cap_n * 0.88:
        insights.append(
            f"큐프레이크 야적 최대 점유가 약 {flake_max:.0f}/{flake_cap_n}포대 수준으로, "
            "설정 상한에 가깝게 찬 구간이 있었습니다. 주조 속도 대비 출하가 못 따라가면 생산 정지 이벤트가 늘 수 있습니다."
        )
        recommendations.append(
            "큐프레이크 야적이 자주 찬다면 출하 빈 트럭 간격을 줄이거나, 야적·상차 인력을 검토해 보세요."
        )
    if scr_cap_n > 0 and scr_max >= scr_cap_n * 0.88:
        insights.append(
            f"SCR 코일 야적 최대 점유가 약 {scr_max:.0f}/{scr_cap_n}코일 근처까지 올라갔습니다. "
            "SCR 비중이 높을수록 출하 리듬이 생산 속도를 좌우합니다."
        )

    batches_done = int(summary.get("melt_batches_completed", 0))
    if batches_done == 0 and cfg.sim_days >= 1:
        insights.append(
            "완료된 반사로 배치가 0입니다. 파라미터(입고·압착·배치 톤수 등)가 과도하게 보수적이거나, "
            "시뮬 기간이 짧아 배치 한 사이클이 끝나기 전에 끊긴 것일 수 있습니다."
        )

    inbound_ton_cap = cfg.inbound.trucks_per_day * cfg.inbound.payload_ton * cfg.sim_days
    total_out = float(summary.get("total_product_ton", 0.0))
    if inbound_ton_cap > 0 and total_out > 0:
        ratio = total_out / inbound_ton_cap
        if ratio < 0.35:
            insights.append(
                f"기간 입고 상한(대략 {inbound_ton_cap:.0f}t) 대비 완제품 합계 약 {total_out:.0f}t로, "
                "스크랩→제품 전환율이 낮게 보입니다. 앞단 병목·배치 횟수·출하 제약을 함께 보는 것이 좋습니다."
            )
        elif ratio > 0.92:
            insights.append(
                f"입고 상한 대비 완제품 합이 약 {ratio * 100:.0f}% 수준으로, "
                "모델 전제 안에서는 원료 흐름이 제품 쪽으로 잘 넘어간 편입니다."
            )

    if len(daily_throughput_ton) >= 4:
        totals = [f + s for _, f, s in daily_throughput_ton]
        head_m = mean(totals[:2])
        tail_m = mean(totals[-2:])
        if head_m > 0 and tail_m / head_m < 0.72:
            insights.append(
                "일별 생산 합계가 앞 이틀보다 뒤 이틀에서 눈에 띄게 낮습니다. "
                "워밍업이 아니라면 출하·야적·반사로 리듬이 뒤로 갈수록 가라앉았을 가능성을 점검해 보세요."
            )
        elif tail_m > head_m * 1.15:
            insights.append(
                "일별 생산 합계가 시뮬 후반으로 갈수록 높아지는 모양입니다. "
                "초기에는 파이프라인이 채워지느라 낮았다가 정상 궤도에 올랐다고 읽을 수 있습니다."
            )

    avg_batch = float(summary.get("avg_melt_batch_min", 0.0))
    if batches_done > 0 and avg_batch > 0:
        nominal = (
            cfg.melting.setup_min + cfg.melting.melting_min
            + cfg.casting.holding_setup_min
            + max(
                cfg.melting.batch_ton * cfg.casting.flake_ratio / max(cfg.casting.flake_unit_ton, 1e-9)
                * cfg.casting.flake_min_per_unit,
                cfg.melting.batch_ton * cfg.casting.scr_ratio / max(cfg.casting.scr_unit_ton, 1e-9)
                * cfg.casting.scr_min_per_unit,
            )
        )
        if avg_batch > nominal * 1.12:
            insights.append(
                f"배치 평균 리드타임이 약 {avg_batch:.0f}분으로, "
                "설정상 한 사이클 분(대기·정체 포함)보다 깁니다. 대기열·버퍼·출하 간격의 영향이 섞여 있을 수 있습니다."
            )

    if (
        cfg.outbound.empty_truck_interval_min >= 120.0
        and scr_avg > flake_avg * 1.5
        and scr_max > flake_max
    ):
        insights.append(
            "출하 빈 트럭 평균 간격이 길게 설정된 상태에서 SCR 야적 평균·최대 점유가 큐프레이크보다 큽니다. "
            "SCR 비중·코일 단위가 크면 출하 리듬이 재고에 더 민감하게 나타날 수 있습니다."
        )

    truck_flow_insights = _truck_flow_insights(metrics, cfg)

    return Analysis(
        summary=summary,
        util=util,
        queue_stats=queue_stats,
        bottleneck=bottleneck,
        bottleneck_reason=bottleneck_reason,
        insights=insights,
        recommendations=recommendations,
        truck_flow_insights=truck_flow_insights,
        daily_throughput_ton=daily_throughput_ton,
    )


# ---------------------------------------------------------------------------
# 1b) 일반인용 결과 해석 (웹·HTML 리포트 공용)
# ---------------------------------------------------------------------------

_DYN_OPEN = "《수》"
_DYN_CLOSE = "《/수》"
_DYN_RE = re.compile(re.escape(_DYN_OPEN) + "(.*?)" + re.escape(_DYN_CLOSE), re.DOTALL)


def _dyn(text: str) -> str:
    """실행마다 바뀌는 숫자·짧은 이름 등. 후처리에서 시각적으로 강조된다."""
    return f"{_DYN_OPEN}{text}{_DYN_CLOSE}"


def _layperson_paragraph_to_rich_html(p: str) -> str:
    """《수》…《/수》는 강조 칩, 별표 두 개로 감싼 구간은 <strong>. 나머지는 escape."""
    metrics: list[str] = []

    def _metric_ph(m: re.Match[str]) -> str:
        metrics.append(m.group(1))
        return f"\x01M{len(metrics) - 1:03d}\x01"

    s = _DYN_RE.sub(_metric_ph, p)
    bolds: list[str] = []

    def _bold_ph(m: re.Match[str]) -> str:
        bolds.append(m.group(1))
        return f"\x01B{len(bolds) - 1:03d}\x01"

    _bd = "*" * 2
    s = re.sub(re.escape(_bd) + r"(.+?)" + re.escape(_bd), _bold_ph, s)
    s = escape(s)
    for i, inner in enumerate(metrics):
        chip = (
            '<span class="sim-metric-value" style="'
            "display:inline-block;color:#1e40af;font-weight:700;"
            "background:linear-gradient(180deg,#eef2ff 0%,#e0e7ff 100%);"
            "padding:0.08em 0.42em;border-radius:4px;border:1px solid #c7d2fe;"
            'font-variant-numeric:tabular-nums;line-height:1.35;white-space:nowrap;">'
            f"{escape(inner)}</span>"
        )
        s = s.replace(f"\x01M{i:03d}\x01", chip)
    for i, inner in enumerate(bolds):
        s = s.replace(f"\x01B{i:03d}\x01", "<strong>" + escape(inner) + "</strong>")
    return s


def _layperson_sections(
    metrics: Metrics, cfg: SimulationConfig, analysis: Analysis,
    *,
    include_rule_insights_sections: bool = True,
    event_log_total: int | None = None,
) -> list[tuple[str, list[str]]]:
    """섹션 제목과 문단 목록. 마크다운/HTML 양쪽에서 재사용."""
    s = analysis.summary
    qs = analysis.queue_stats
    horizon_h = cfg.sim_horizon_min / 60.0
    days = s.get("horizon_days", horizon_h / 24.0)
    expected_in = cfg.inbound.trucks_per_day * cfg.sim_days
    tin = int(s["trucks_in_processed"])
    tout = int(s["trucks_out_dispatched"])
    batches = int(s["melt_batches_completed"])
    total_ton = float(s["total_product_ton"])
    tpd = float(s["throughput_ton_per_day"])
    in_lead = float(s["avg_truck_in_lead_min"])
    out_lead = float(s["avg_truck_out_lead_min"])
    batch_min = float(s["avg_melt_batch_min"])

    pallet_st = qs["파레트 버퍼"]
    flake_st = qs["큐프레이크 야적"]
    scr_st = qs["SCR 코일 야적"]
    pallet_cap = cfg.sorting.pallet_buffer_capacity

    util_sorted = sorted(analysis.util.items(), key=lambda kv: kv[1], reverse=True)
    top_util = util_sorted[0] if util_sorted else ("(없음)", 0.0)

    sections: list[tuple[str, list[str]]] = []

    # 1. 한 줄 맥락
    p1: list[str] = [
        f"이 결과는 컴퓨터 안에서 {_dyn(f'{days:.1f}')}일(약 {_dyn(f'{horizon_h:.0f}')}시간) 동안 "
        "‘스크랩이 트럭으로 들어와 → 선별·압착으로 파레트가 되고 → 반사로에서 녹인 뒤 "
        "제품이 나와 → 다시 트럭으로 나간다’는 순서를 시간 흐름대로 흉내 낸 것입니다. "
        "실제 공장과 100% 같다고 보지 말고, 어디가 바쁜지·어디에 재고가 쌓이는지를 "
        "가늠하는 참고용 자료로 보면 됩니다.",
    ]
    sections.append(("이 시뮬레이션이 하는 일", p1))

    flake_pct = int(round(cfg.casting.flake_ratio * 100))
    scr_pct = max(0, 100 - flake_pct)
    run_ctx = [
        f"이번 실행에 넣은 주요 전제는 시뮬 {_dyn(str(cfg.sim_days))}일, 난수 시드 {_dyn(str(cfg.random_seed))}, "
        f"제품 비율 큐프레이크 {_dyn(str(flake_pct))}%·SCR {_dyn(str(scr_pct))}%, "
        f"입고 {_dyn(str(cfg.inbound.trucks_per_day))}대/일×{_dyn(str(int(cfg.inbound.payload_ton)))}t, "
        f"반사로 {_dyn(str(cfg.melting.furnace_count))}대·배치 {_dyn(str(int(cfg.melting.batch_ton)))}t, "
        f"빈 출하 트럭 평균 간격 {_dyn(str(int(cfg.outbound.empty_truck_interval_min)))}분입니다. "
        "뒤에 나오는 모든 수치는 이 조합으로 한 번 돌렸을 때의 결과입니다.",
    ]
    sections.append(("이번 실행에 들어간 조건", run_ctx))

    # 2. 숫자 읽기
    p2 = [
        f"입고 트럭은 {_dyn(str(tin))}대가 끝까지 처리되었습니다. "
        f"설정상 하루 {_dyn(str(cfg.inbound.trucks_per_day))}대 × {_dyn(str(cfg.sim_days))}일이면 "
        f"최대 약 {_dyn(str(expected_in))}대까지 도착할 수 있는 틀인데, "
        f"그중 {_dyn(str(tin))}대가 출차까지 끝났는지로 앞단이 밀렸는지를 첫 번째 가늠값으로 삼을 수 있습니다.",
        f"출하 트럭은 {_dyn(str(tout))}대가 완제품을 싣고 출고까지 마쳤습니다. "
        "입고 대수와 꼭 같을 필요는 없고, 빈 트럭이 얼마나 자주 오는지·야적에 물건이 "
        "있었는지에 따라 달라집니다.",
        f"반사로 배치는 {_dyn(str(batches))}번 완료되었습니다. 배치 한 번은 ‘정해진 양을 한 덩어리로 녹이고 주조한다’는 뜻으로, "
        "공장 생산의 큰 줄기를 나타냅니다.",
        f"완제품(큐프레이크 + SCR 코일)은 합쳐 약 {_dyn(f'{total_ton:.0f}')}톤이 만들어졌고, "
        f"기간으로 나누면 하루 평균 약 {_dyn(f'{tpd:.1f}')}톤/일입니다. "
        "‘하루에 고철이 얼마나 들어오는지’와 비교해 보면, 공장이 그 입고를 소화할 만큼 "
        "나오고 있는지 대략적인 균형을 볼 수 있습니다.",
    ]
    if batches == 0:
        p2.append(
            "이번 실행에서는 반사로 배치가 한 번도 끝나지 않았습니다. "
            "입고·압착이 너무 적거나, 설정이 막히는 조합인지 파라미터를 점검해 보세요."
        )
    elif batch_min > 0:
        p2.append(
            f"배치 하나가 끝날 때까지 걸린 평균 시간은 약 {_dyn(f'{batch_min:.0f}')}분 "
            f"({_dyn(f'{batch_min / 60:.1f}')}시간)입니다. 여기에는 장입·용해·주조가 모두 포함됩니다."
        )
    sections.append(("숫자로 보는 이번 실행", p2))

    p_chart = [
        "자원 가동률은 90% 이상이면 병목 후보, 70~90%는 주의, 그 아래는 여유로 읽는 편이 자연스럽습니다.",
        "버퍼·야적 시계열이 오랫동안 오르면 뒤단(출하·상차)에서 물건이 빠져나가느라 밀리는 모양일 수 있고, "
        "0 근처에 오래 붙어 있으면 앞단(입고·압착·반사로 자재) 공급이 느렸거나, 이미 생산이 한창 갔다가 마무리 구간일 수 있습니다.",
        "누적 트럭 선이 벌어지면 그 시점에 대기 중인 트럭이 많았을 가능성이 있고, 일별 생산 막대는 보통 맨 앞 1~2일이 안정화 전이라 들쭉날쭉할 수 있습니다.",
    ]
    sections.append(("그래프를 곁들여 볼 때", p_chart))

    # 3. 병목
    bn = analysis.bottleneck
    bn_plain = bn
    if "반사로" in bn:
        bn_plain = "반사로(용해·주조 쪽)"
    elif "압착" in bn:
        bn_plain = "압착기"
    elif "선별" in bn:
        bn_plain = "선별 작업"
    elif "계근" in bn:
        bn_plain = "계근대(트럭 계량)"
    elif "엘리베이터" in bn:
        bn_plain = "엘리베이터(반사로로 올리는 구간)"
    p3 = [
        "병목은 ‘물줄기가 가장 잘 안 나오는 좁은 구간’이라고 생각하면 됩니다. "
        "이번 실행에서 시스템이 가장 바빴다고 본 곳은 "
        f"{_dyn(bn)} 입니다. {analysis.bottleneck_reason}",
        f"비유하자면, 여러 차선 도로에서 {_dyn(bn_plain)} 차선만 거의 꽉 차 있고 "
        "그 앞뒤로 차가 줄 서 있는 그림에 가깝습니다. 여기를 넓히거나 나누면 "
        "전체 처리량이 달라질 가능성이 큽니다.",
    ]
    sections.append(("‘병목’이란 무엇인지, 이번엔 어디인지", p3))

    # 4. 가동률 막대
    melt_h = cfg.melting.melting_min / 60.0
    p4 = [
        "가동률 막대(HTML 리포트 등에 붙어 있다면)는 ‘그 기계나 작업장이 시뮬레이션 시간 중 얼마나 일했는지’ 비율입니다. "
        "90% 이상이면 사실상 쉴 틈이 거의 없다는 뜻이고, 낮으면 아직 여유가 있거나 "
        "앞단에서 물건이 덜 들어와서 놀고 있을 수도 있습니다.",
        f"이번에는 {_dyn(top_util[0])}이(가) 약 {_dyn(f'{top_util[1] * 100:.1f}')}%로 가장 높게 나왔습니다. "
        "막대 여러 개를 나란히 보면, 돈과 인력을 어디에 먼저 쓸지 우선순위를 잡는 데 도움이 됩니다.",
    ]
    hot = [f"{n} {_dyn(f'{u * 100:.1f}')}%" for n, u in util_sorted if u >= 0.9]
    warm = [f"{n} {_dyn(f'{u * 100:.1f}')}%" for n, u in util_sorted if 0.7 <= u < 0.9]
    if hot:
        p4.append(
            "**90% 이상(풀가동에 가까움):** " + " · ".join(hot) + " — 병목 후보로 먼저 살펴볼 만합니다."
        )
    if warm:
        p4.append(
            "**70~90%:** " + " · ".join(warm) + " — 아직 여유가 있다고 단정하긴 이르고, 부하가 조금만 늘어도 붉은 구간으로 넘어갈 수 있습니다."
        )
    if not hot and not warm and util_sorted:
        p4.append(
            "이번 실행에서는 모든 자원이 70% 미만으로 나와, 숫자만 놓고 보면 당장의 용량 여유는 비교적 큰 편입니다."
        )
    sections.append(("자원 가동률 막대그래프", p4))

    # 5. 버퍼
    p5 = [
        "파레트 버퍼는 압착 직후, 반사로에 넣기 전 파레트가 잠깐 머무는 ‘중간 선반’입니다. "
        "반사로가 배치 단위로 한꺼번에 가져가기 때문에, 곡선이 오랫동안 0에 가깝다는 것은 "
        "‘선반 위에 오래 쌓아 두지 않고 바로 이어서 쓴다’는 뜻일 수도 있고, "
        "입고가 끝난 뒤에는 새 파레트가 안 들어와서 비는 시간이 길어진 것일 수도 있습니다. "
        "‘버퍼 용량이 넉넉하다’와 ‘지금 비어 있다’는 다른 이야기입니다.",
        f"이번 실행에서 파레트 버퍼는 시간 가중 평균 약 {_dyn(format(pallet_st['avg'], '.1f'))}개, "
        f"최대 {_dyn(format(pallet_st['max'], '.0f'))}개까지 올라갔습니다(설정 상한 {_dyn(str(pallet_cap))}개).",
        "큐프레이크·SCR 야적은 완제품이 쌓이는 창고입니다. 출하 트럭 간격·상차 대기와 연결되어 "
        "곡선이 자주 오르내립니다. 파레트 버퍼와 모양이 다르더라도 이상하지 않습니다.",
        f"이번에는 큐프레이크 야적 평균 {_dyn(format(flake_st['avg'], '.1f'))}개, SCR 야적 평균 {_dyn(format(scr_st['avg'], '.1f'))}개 "
        f"(각 최대 {_dyn(format(flake_st['max'], '.0f'))}개 / {_dyn(format(scr_st['max'], '.0f'))}개) 수준으로 나타났습니다.",
        f"95% 구간에서는 파레트 버퍼가 대략 {_dyn(format(pallet_st['p95'], '.0f'))}개, "
        f"큐프레이크 야적 {_dyn(format(flake_st['p95'], '.0f'))}개, SCR 야적 {_dyn(format(scr_st['p95'], '.0f'))}개를 넘지 않았습니다. "
        "‘평소보다 얼마나 자주 꽉 찼는지’를 볼 때 최댓값과 함께 참고하면 좋습니다.",
    ]
    rem = int(s.get("pallets_remaining", 0))
    if rem > 0:
        p5.append(
            f"시뮬레이션 종료 시점에는 파레트 버퍼에 {_dyn(str(rem))}개가 남아 있었습니다. "
            "아직 반사로 배치로 들어가지 않은 중간 재고입니다."
        )
    sections.append(("버퍼·야적장 시계열 그래프", p5))

    # 6. 트럭·Gantt
    p6 = [
        "누적 트럭 그래프에서 입고 ‘도착’과 ‘출차’ 선이 벌어지면, 당시에 하역장이나 계근대에서 "
        "기다리는 트럭이 많았다는 뜻에 가깝습니다.",
        f"입고 트럭 평균 체류는 약 {_dyn(f'{in_lead:.0f}')}분, 출하 트럭은 약 {_dyn(f'{out_lead:.0f}')}분입니다. "
        "참고용으로 입고는 대략 60분·출하는 90분 안쪽이면 비교적 매끈한 편이라고 보면 됩니다.",
        f"반사로 Gantt에서 빨간 막대는 용해·정련 구간(설정상 약 {_dyn(f'{melt_h:.1f}')}시간)을 시각적으로 보여 줍니다. "
        "회색·초록은 장입·준비와 주조입니다. 흰 빈틈이 길면 그때는 반사로가 기다리거나 멈춘 구간일 수 있습니다.",
    ]
    p6.extend(analysis.truck_flow_insights)
    sections.append(("트럭 흐름·반사로 일정도", p6))

    dtt = analysis.daily_throughput_ton
    if dtt:
        totals = [f + s for _, f, s in dtt]
        n_days = len(totals)
        peak_i = max(range(n_days), key=lambda i: totals[i])
        peak_day = peak_i + 1
        mean_all = mean(totals)
        pdaily = [
            f"일별로 보면 {n_days}일 동안 하루 합계 생산(큐프레이크+SCR)이 "
            f"가장 컸던 날은 {_dyn(str(peak_day))}일차(약 {_dyn(f'{totals[peak_i]:.0f}')}t) 근처였습니다. "
            f"전체 평균은 약 {_dyn(f'{mean_all:.0f}')}t/일입니다.",
        ]
        if n_days >= 4:
            head = mean(totals[:2])
            tail = mean(totals[-2:])
            pdaily.append(
                f"맨 앞 이틀 평균은 약 {_dyn(f'{head:.0f}')}t/일, 마지막 이틀은 약 {_dyn(f'{tail:.0f}')}t/일입니다. "
                "앞부분이 뒤보다 확 낮으면 워밍업 구간일 가능성이 있고, "
                "처음부터 끝까지 들쭉날쭉하면 출하 리듬이나 반사로 배치가 매끈하지 않았다는 해석을 고려할 수 있습니다."
            )
        sections.append(("일별 생산 막대를 생각할 때", pdaily))

    # 7. 기록·한계
    evt_full = event_log_total if event_log_total is not None else len(metrics.events)
    evt_shown = len(metrics.events)
    p7 = [
        f"이번 실행에서는 시간순 사건이 {_dyn(format(evt_full, ','))}건 기록되었습니다. "
        "요약 지표와(있다면) 동봉한 그래프·표는 모두 이 기록을 압축한 것입니다.",
        "규칙으로 자동 생성하는 관찰 포인트·권장 문구는 있는 그대로의 힌트입니다. "
        "현장의 계약·날씨·설비 고장 등은 모델에 없으므로, 중요한 결정은 사람이 한 번 더 검토하는 것이 좋습니다.",
        "같은 설정이라도 난수 시드(출하 트럭 간격 등)를 바꾸면 곡선 모양은 달라질 수 있습니다. "
        "몇 번 돌려 보고 공통으로 나타나는 패턴을 보는 것이 안전합니다.",
    ]
    if event_log_total is not None and evt_full > evt_shown:
        p7.insert(
            1,
            f"**(웹 세션 한정)** 화면·다운로드용으로는 시간순 사건 중 **{evt_shown:,}**건만 "
            f"보관했습니다. (실행 전체 **{evt_full:,}**건.)",
        )
    if not include_rule_insights_sections:
        p7.append(
            "관찰 포인트와 권장 액션 문장의 전체 목록은 이 HTML 뒤쪽 "
            "‘**시뮬레이션 분석 결과 인사이트**’ 절에 따로 정리해 두었습니다."
        )
    sections.append(("기록의 크기와, 결과를 받아들일 때", p7))

    if include_rule_insights_sections and analysis.insights:
        sections.append(("규칙으로 찾은 관찰 포인트", list(analysis.insights)))
    if include_rule_insights_sections and analysis.recommendations:
        sections.append(("규칙으로 찾은 권장 액션", list(analysis.recommendations)))

    return sections


def _layperson_paragraph_to_plain_markdown(p: str) -> str:
    """《수》…《/수》 구간을 Markdown 굵게로 바꾼다. HTML 이스케이프 없음."""
    def _metric_md(m: re.Match[str]) -> str:
        return f"**{m.group(1)}**"

    return _DYN_RE.sub(_metric_md, p)


def layperson_interpretation_export_markdown(
    metrics: Metrics,
    cfg: SimulationConfig,
    analysis: Analysis,
    *,
    event_log_total: int | None = None,
) -> str:
    """파일·다운로드용: 일반인 해석을 순수 Markdown으로 만든다."""
    chunks: list[str] = []
    for title, paras in _layperson_sections(
        metrics, cfg, analysis, event_log_total=event_log_total
    ):
        chunks.append(f"#### {title}\n\n")
        for para in paras:
            chunks.append(_layperson_paragraph_to_plain_markdown(para))
            chunks.append("\n\n")
    return "".join(chunks).strip()


def layperson_interpretation_markdown(
    metrics: Metrics,
    cfg: SimulationConfig,
    analysis: Analysis,
    *,
    event_log_total: int | None = None,
) -> str:
    """Streamlit용: 섹션 제목은 Markdown, 본문은 수치 강조 HTML 인라인."""
    chunks: list[str] = []
    for title, paras in _layperson_sections(
        metrics, cfg, analysis, event_log_total=event_log_total
    ):
        chunks.append(f"#### {title}\n")
        for p in paras:
            chunks.append(_layperson_paragraph_to_rich_html(p))
            chunks.append("\n\n")
    return "".join(chunks).strip()


def layperson_interpretation_html(
    metrics: Metrics,
    cfg: SimulationConfig,
    analysis: Analysis,
    *,
    event_log_total: int | None = None,
) -> str:
    """HTML 리포트용. 단락별 escape + 《수》 구간 수치 강조."""
    parts: list[str] = []
    for title, paras in _layperson_sections(
        metrics,
        cfg,
        analysis,
        include_rule_insights_sections=False,
        event_log_total=event_log_total,
    ):
        parts.append(f'<h3 class="layperson-h3">{escape(title)}</h3>')
        for p in paras:
            parts.append(f"<p>{_layperson_paragraph_to_rich_html(p)}</p>")
    return "\n".join(parts)


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
        (metrics.flake_buffer_levels, "큐프레이크 야적", "#0ea5e9",
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


def _fig_furnace_gantt(metrics: Metrics, cfg: SimulationConfig) -> go.Figure:
    """이벤트 로그로부터 furnace 별 charging/melt/casting 구간을 추출."""
    melt_h = cfg.melting.melting_min / 60.0
    melt_label = f"용해·정련({melt_h:g}h)"
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
                cur_label, cur_start = melt_label, t
            elif kind == "melt_done" and cur_start is not None:
                intervals.setdefault(fid, []).append((cur_start, t, melt_label))
                cur_label, cur_start = "주조", t
            elif kind == "batch_done" and cur_start is not None:
                intervals.setdefault(fid, []).append((cur_start, t, "주조"))
                cur_start = None
                cur_label = None

    color_map = {
        "장입+준비": "#9ca3af",
        melt_label: "#ef4444",
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
        title=f"반사로 배치 Gantt (회색=장입/준비, 빨강={melt_label}, 초록=주조)",
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
    fig.add_trace(go.Bar(name="큐프레이크 (t)", x=days, y=flake, marker_color="#0ea5e9"))
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


def build_layperson_visual_figures(
    metrics: Metrics, cfg: SimulationConfig, analysis: Analysis
) -> dict[str, go.Figure]:
    """일반인 해석과 짝을 이루는 핵심 Plotly 차트 묶음 (웹·HTML 공용)."""
    return {
        "utilization": _fig_utilization(analysis.util),
        "buffers": _fig_buffer_levels(metrics, cfg),
        "trucks": _fig_truck_cumulative(metrics),
        "lead_times": _fig_lead_time_hist(metrics),
        "daily_output": _fig_daily_throughput(analysis.daily_throughput_ton),
        "furnace_gantt": _fig_furnace_gantt(metrics, cfg),
    }


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

  .layperson-section h3.layperson-h3 {{
    font-size: 15px; margin: 18px 0 8px; color: #1e3a8a;
  }}
  .layperson-section h3.layperson-h3:first-child {{ margin-top: 0; }}
  .layperson-section p {{
    margin: 0 0 11px; font-size: 14px; color: #374151;
  }}
  .layperson-section .sim-metric-value {{
    font-variant-numeric: tabular-nums;
  }}
  .layperson-chart-wrap {{
    margin: 10px 0 22px;
    padding: 14px 0 4px;
    border-top: 1px solid var(--border);
  }}
  .layperson-chart-wrap > .lead {{
    margin-top: 0;
  }}
  .layperson-chart-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
    gap: 18px;
    margin-top: 12px;
  }}
  .layperson-chart-grid > div {{
    min-height: 300px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: #fafafa;
    padding: 6px;
  }}

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

<section class="card layperson-section">
  <h2>일반인을 위한 상세 결과 해석</h2>
  <p class="lead">숫자·그래프를 처음 보는 분도 흐름을 따라갈 수 있도록, 이번 실행 결과를 풀어서 설명합니다.
  (규칙 기반 시뮬레이션 분석 결과 인사이트와 같이 참고용이며, 실제 투자·안전·계약 판단은 반드시 현장 검토가 필요합니다.)</p>
  <div class="layperson-chart-wrap">
    <h3 class="layperson-h3">이번 실행을 그래프로 먼저 보기</h3>
    <p class="lead">아래 그림은 뒤쪽 절(번호 3~7)과 같은 데이터입니다. 글 설명을 읽기 전에 어디가 붐비고 재고가 어떻게 움직였는지 감을 잡을 때 쓰면 됩니다.</p>
    <div class="layperson-chart-grid">
      <div id="lp_util"></div>
      <div id="lp_buffer"></div>
      <div id="lp_truck"></div>
      <div id="lp_lead"></div>
      <div id="lp_daily"></div>
      <div id="lp_gantt"></div>
    </div>
  </div>
  {layperson_html}
</section>

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
  <p class="lead">두 반사로가 어떤 시각에 어떤 단계(장입·용해·정련 병목·주조)에
  있었는지 가로 막대로 표시합니다. 긴 막대(약 13시간 전후)가 바로 문서가 언급한
  최대 병목 구간입니다.</p>
  <div id="gantt_chart"></div>
</section>

<section class="card">
  <h2><span class="num">6</span>트럭 흐름</h2>
  <p class="lead">왼쪽 곡선의 기울기가 가팔라지면 트럭이 한꺼번에 도착해 정체가 시작된 시점입니다.
  도착선과 출차선의 수직 차이가 동시 체류 트럭 수에 해당합니다.</p>
  <div id="truck_chart"></div>
  <div id="lead_chart"></div>
  <h3 style="font-size:15px; margin-top:22px">이 그래프에서 읽는 시사점</h3>
  <p class="lead" style="margin-top:4px">아래 문장은 이번 실행의 누적 도착·출차 건수와 체류시간 분포를 규칙으로 요약한 것입니다.</p>
  {truck_flow_insights_html}
</section>

<section class="card">
  <h2><span class="num">7</span>일별 생산량</h2>
  <p class="lead">정상 가동에 도달하기까지의 워밍업 구간과 이후의 안정 가동량을 한눈에 비교할 수 있습니다.</p>
  <div id="daily_chart"></div>
</section>

<section class="card">
  <h2><span class="num">8</span>시뮬레이션 분석 결과 인사이트</h2>
  <p class="lead">KPI·가동률·버퍼·일별 생산·트럭 이벤트에 규칙을 적용해 자동으로 뽑은 관찰 포인트와 권장 액션입니다.
  파라미터를 바꿔 다시 실행하면 내용도 함께 바뀝니다.</p>
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
  (function() {{
    const plotOpts = {{responsive: true}};
    const uD = {util_data}, uL = {util_layout};
    Plotly.newPlot("lp_util", uD, uL, plotOpts);
    Plotly.newPlot("util_chart", uD, uL, plotOpts);
    const bD = {buffer_data}, bL = {buffer_layout};
    Plotly.newPlot("lp_buffer", bD, bL, plotOpts);
    Plotly.newPlot("buffer_chart", bD, bL, plotOpts);
    const gD = {gantt_data}, gL = {gantt_layout};
    Plotly.newPlot("lp_gantt", gD, gL, plotOpts);
    Plotly.newPlot("gantt_chart", gD, gL, plotOpts);
    const tD = {truck_data}, tL = {truck_layout};
    Plotly.newPlot("lp_truck", tD, tL, plotOpts);
    Plotly.newPlot("truck_chart", tD, tL, plotOpts);
    const lD = {lead_data}, lL = {lead_layout};
    Plotly.newPlot("lp_lead", lD, lL, plotOpts);
    Plotly.newPlot("lead_chart", lD, lL, plotOpts);
    const dD = {daily_data}, dL = {daily_layout};
    Plotly.newPlot("lp_daily", dD, dL, plotOpts);
    Plotly.newPlot("daily_chart", dD, dL, plotOpts);
  }})();
</script>
</body>
</html>
"""


_STAGES = [
    ("1. 입고/하역", "10대×20t · 09~18시 도착(오전 80%) · 계근 각 5분 · 하역 20분",
     "inbound"),
    ("2. 선별/압착", "트럭 1대 30분 정리 → 8 sub-pile → 블록당 지게차+압착+적재 압착 → 파레트 2.5t",
     "press"),
    ("3. 장입/용해", "32파레트(80t) 모이면 엘리베이터(5분/왕복) → 2h 준비 → 병목 약 13h",
     "melting"),
    ("4. 하이브리드 주조", "큐플레이크 1t/3.1분 + SCR 4t/12.5분, 비율 2:8",
     "casting"),
    ("5. 출하/야적", "빈 트럭(오전 80% 편향) → 20t 적재(1t flake / 4t SCR) → 2차 계근 → 출차",
     "outbound"),
]


def _kpi_cards_html(summary: dict[str, Any]) -> str:
    cards = [
        ("처리 트럭(입고)", summary["trucks_in_processed"], "대"),
        ("출하 트럭", summary["trucks_out_dispatched"], "대"),
        ("완료 배치", summary["melt_batches_completed"], "회"),
        ("큐프레이크 생산", f"{summary['flake_ton']:.0f}", "t"),
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
    gantt_data, gantt_layout = _fig_to_json(_fig_furnace_gantt(metrics, cfg))
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
        layperson_html=layperson_interpretation_html(metrics, cfg, analysis),
        kpi_cards=_kpi_cards_html(analysis.summary),
        bottleneck=escape(analysis.bottleneck),
        bottleneck_reason=escape(analysis.bottleneck_reason),
        stages_html=_stages_html(analysis.bottleneck),
        queue_rows=_queue_rows_html(analysis.queue_stats),
        truck_flow_insights_html=_bullets(analysis.truck_flow_insights, "insight"),
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

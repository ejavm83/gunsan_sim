"""시뮬레이션 이벤트 로깅 및 KPI 산출.

각 공정 단계에서 발생하는 사건과 상태를 시간순으로 기록하고, 마지막에
처리량/체류시간/자원 활용률 등 핵심 KPI를 계산한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    time_min: float
    stage: str
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Metrics:
    events: list[Event] = field(default_factory=list)

    truck_in_done: int = 0
    truck_out_done: int = 0

    pallets_produced: int = 0
    pallets_consumed: int = 0

    batches_started: int = 0
    batches_completed: int = 0

    flake_units_produced: int = 0
    scr_units_produced: int = 0

    flake_dispatched_ton: float = 0.0
    scr_dispatched_ton: float = 0.0

    pallet_buffer_levels: list[tuple[float, int]] = field(default_factory=list)
    flake_buffer_levels: list[tuple[float, int]] = field(default_factory=list)
    scr_buffer_levels: list[tuple[float, int]] = field(default_factory=list)

    truck_in_lead_times: list[float] = field(default_factory=list)
    truck_out_lead_times: list[float] = field(default_factory=list)
    melt_batch_durations: list[float] = field(default_factory=list)

    # 입출 균형(WIP) 분석용 — 입고 누적 톤수와 시뮬 종료 시점의 공정 중간 잔량
    inbound_ton: float = 0.0
    wip_sort_queue_ton: float = 0.0          # 선별 대기 (하역 직후 트럭 더미)
    wip_press_queue_ton: float = 0.0         # 압착 대기 sub-pile
    wip_pallet_buffer_ton: float = 0.0       # 파레트 버퍼 잔량
    wip_melting_in_progress_ton: float = 0.0 # 시작했지만 미완료인 용해 배치
    wip_flake_buffer_ton: float = 0.0        # Flake 완제품 버퍼 잔량
    wip_scr_buffer_ton: float = 0.0          # SCR 완제품 버퍼 잔량

    # 트럭 만재 기준 톤수 (cfg 에서 복사, 적재율 등 KPI 계산에 사용)
    inbound_payload_ton: float = 0.0
    outbound_truck_capacity_ton: float = 0.0

    def log(self, time_min: float, stage: str, kind: str, **detail: Any) -> None:
        self.events.append(Event(time_min=time_min, stage=stage, kind=kind, detail=detail))

    # -- KPI 계산 -----------------------------------------------------------
    def summary(self, sim_horizon_min: int) -> dict[str, Any]:
        days = sim_horizon_min / (24 * 60)
        flake_ton = self.flake_units_produced * 1.0
        scr_ton = self.scr_units_produced * 4.0
        dispatched_ton = self.flake_dispatched_ton + self.scr_dispatched_ton
        wip_total_ton = (
            self.wip_sort_queue_ton
            + self.wip_press_queue_ton
            + self.wip_pallet_buffer_ton
            + self.wip_melting_in_progress_ton
            + self.wip_flake_buffer_ton
            + self.wip_scr_buffer_ton
        )
        # 미설명 손실 = 입고 - (출하 + 공정중 잔량). 이론상 0 이지만,
        # 시뮬 종료 시점에 진행 중인 트럭 적재/계근 등 미세한 시간 차이로
        # ±수십 t 정도 잔차가 남을 수 있다.
        unaccounted_ton = self.inbound_ton - dispatched_ton - wip_total_ton

        # 트럭/일·톤/일·트럭당 적재량·만재율 등 입출고 파생 지표
        trucks_in_per_day = self.truck_in_done / max(days, 1e-9)
        trucks_out_per_day = self.truck_out_done / max(days, 1e-9)
        inbound_ton_per_day = self.inbound_ton / max(days, 1e-9)
        dispatched_ton_per_day = dispatched_ton / max(days, 1e-9)
        avg_inbound_per_truck = self.inbound_ton / max(self.truck_in_done, 1)
        avg_outbound_per_truck = dispatched_ton / max(self.truck_out_done, 1)
        inbound_fill_rate = (
            avg_inbound_per_truck / self.inbound_payload_ton * 100.0
            if self.inbound_payload_ton > 0 else 0.0
        )
        outbound_fill_rate = (
            avg_outbound_per_truck / self.outbound_truck_capacity_ton * 100.0
            if self.outbound_truck_capacity_ton > 0 else 0.0
        )
        balance_pct = (
            dispatched_ton / self.inbound_ton * 100.0
            if self.inbound_ton > 0 else 0.0
        )
        wip_pct = (
            wip_total_ton / self.inbound_ton * 100.0
            if self.inbound_ton > 0 else 0.0
        )
        return {
            "horizon_days": days,
            "trucks_in_processed": self.truck_in_done,
            "trucks_out_dispatched": self.truck_out_done,
            "pallets_produced": self.pallets_produced,
            "pallets_consumed": self.pallets_consumed,
            "pallets_remaining": self.pallets_produced - self.pallets_consumed,
            "melt_batches_started": self.batches_started,
            "melt_batches_completed": self.batches_completed,
            "flake_units": self.flake_units_produced,
            "scr_units": self.scr_units_produced,
            "flake_ton": flake_ton,
            "scr_ton": scr_ton,
            "total_product_ton": flake_ton + scr_ton,
            "flake_dispatched_ton": self.flake_dispatched_ton,
            "scr_dispatched_ton": self.scr_dispatched_ton,
            "avg_truck_in_lead_min": _avg(self.truck_in_lead_times),
            "avg_truck_out_lead_min": _avg(self.truck_out_lead_times),
            "avg_melt_batch_min": _avg(self.melt_batch_durations),
            "throughput_ton_per_day": (flake_ton + scr_ton) / max(days, 1e-9),
            # 미처리(WIP) 분석
            "inbound_ton": self.inbound_ton,
            "dispatched_ton": dispatched_ton,
            "wip_sort_queue_ton": self.wip_sort_queue_ton,
            "wip_press_queue_ton": self.wip_press_queue_ton,
            "wip_pallet_buffer_ton": self.wip_pallet_buffer_ton,
            "wip_melting_in_progress_ton": self.wip_melting_in_progress_ton,
            "wip_flake_buffer_ton": self.wip_flake_buffer_ton,
            "wip_scr_buffer_ton": self.wip_scr_buffer_ton,
            "wip_total_ton": wip_total_ton,
            "unaccounted_ton": unaccounted_ton,
            # 입출고 파생 지표
            "trucks_in_per_day": trucks_in_per_day,
            "trucks_out_per_day": trucks_out_per_day,
            "inbound_ton_per_day": inbound_ton_per_day,
            "dispatched_ton_per_day": dispatched_ton_per_day,
            "avg_inbound_per_truck": avg_inbound_per_truck,
            "avg_outbound_per_truck": avg_outbound_per_truck,
            "inbound_fill_rate_pct": inbound_fill_rate,
            "outbound_fill_rate_pct": outbound_fill_rate,
            "inbound_payload_ton": self.inbound_payload_ton,
            "outbound_truck_capacity_ton": self.outbound_truck_capacity_ton,
            "balance_dispatched_pct": balance_pct,
            "wip_share_pct": wip_pct,
        }


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def format_summary(summary: dict[str, Any]) -> str:
    inbound_ton = summary.get("inbound_ton", 0.0)
    dispatched_ton = summary.get("dispatched_ton", 0.0)
    wip_total = summary.get("wip_total_ton", 0.0)
    unaccounted = summary.get("unaccounted_ton", 0.0)
    balance_pct = summary.get("balance_dispatched_pct", 0.0)
    wip_pct = summary.get("wip_share_pct", 0.0)
    trucks_in_per_day = summary.get("trucks_in_per_day", 0.0)
    trucks_out_per_day = summary.get("trucks_out_per_day", 0.0)
    inbound_ton_per_day = summary.get("inbound_ton_per_day", 0.0)
    dispatched_ton_per_day = summary.get("dispatched_ton_per_day", 0.0)
    avg_in_per_truck = summary.get("avg_inbound_per_truck", 0.0)
    avg_out_per_truck = summary.get("avg_outbound_per_truck", 0.0)
    in_fill = summary.get("inbound_fill_rate_pct", 0.0)
    out_fill = summary.get("outbound_fill_rate_pct", 0.0)
    in_cap = summary.get("inbound_payload_ton", 0.0)
    out_cap = summary.get("outbound_truck_capacity_ton", 0.0)

    # 가장 큰 WIP 항목 찾아서 병목 위치 추정용 텍스트 만들기
    wip_components: list[tuple[str, float]] = [
        ("선별 대기 (하역 직후)", summary.get("wip_sort_queue_ton", 0.0)),
        ("압착 대기 (sub-pile)", summary.get("wip_press_queue_ton", 0.0)),
        ("파레트 버퍼", summary.get("wip_pallet_buffer_ton", 0.0)),
        ("용해 진행 중", summary.get("wip_melting_in_progress_ton", 0.0)),
        ("Flake 야적장", summary.get("wip_flake_buffer_ton", 0.0)),
        ("SCR 야적장", summary.get("wip_scr_buffer_ton", 0.0)),
    ]
    top_wip_name, top_wip_ton = max(wip_components, key=lambda x: x[1])

    # 해석 가이드: 출하/입고 비율과 트럭 적재율을 보고 진단 멘트를 만든다.
    hints: list[str] = []
    if inbound_ton <= 0:
        hints.append("입고 누적이 0 입니다. 입고 트럭 설정 또는 시뮬 기간을 확인하세요.")
    else:
        if balance_pct < 60.0:
            hints.append(
                f"출하/입고 비율이 {balance_pct:.1f}% 로 낮습니다. "
                f"공정 중 잔량이 {wip_pct:.1f}% 쌓여 있고, "
                f"가장 큰 적체는 [{top_wip_name}] ({top_wip_ton:.0f} t) 입니다."
            )
        elif balance_pct < 90.0:
            hints.append(
                f"출하/입고 비율 {balance_pct:.1f}% — 시뮬 기간에 대부분이 빠져나갔지만 "
                f"공정 중 {wip_pct:.1f}% 가 잔류 중입니다. "
                f"가장 큰 잔류 위치: [{top_wip_name}] ({top_wip_ton:.0f} t)."
            )
        else:
            hints.append(
                f"출하/입고 비율 {balance_pct:.1f}% — 입출 물량이 거의 균형을 이룹니다."
            )

        if out_cap > 0:
            if out_fill < 80.0:
                hints.append(
                    f"출하 트럭 평균 적재가 만재의 {out_fill:.0f}% 수준입니다. "
                    "야적장 재고가 트럭 도착보다 느리게 채워지는 신호입니다 "
                    "(생산 능력 부족 또는 출하 도착 빈도 과다)."
                )
            elif out_fill > 98.0:
                hints.append(
                    f"출하 트럭이 거의 항상 만재({out_fill:.0f}%)로 나가고 있어, "
                    "야적장 재고는 충분히 확보되어 있습니다."
                )

        if in_cap > 0 and in_fill < 99.0:
            hints.append(
                f"입고 트럭 평균 적재가 만재의 {in_fill:.0f}% 입니다 "
                "(보통 입고는 만재로 들어오므로 100% 에 가까워야 정상)."
            )

    lines = [
        "=" * 60,
        "  군산 공장 하이브리드 공정 시뮬레이션 결과 요약",
        "=" * 60,
        f"  시뮬레이션 기간          : {summary['horizon_days']:.1f} 일",
        "-" * 60,
        "  [입고] 원료(스크랩) 도착",
        f"    · 처리 트럭             : {summary['trucks_in_processed']:>6} 대  ({trucks_in_per_day:.1f} 대/일)",
        f"    · 누적 입고 물량        : {inbound_ton:>8.1f} t  ({inbound_ton_per_day:.1f} t/일)",
        f"    · 트럭당 평균 적재       : {avg_in_per_truck:>8.1f} t  (만재 {in_cap:.0f} t · 적재율 {in_fill:.1f}%)",
        f"    · 평균 체류시간(분)     : {summary['avg_truck_in_lead_min']:>8.1f}",
        "-" * 60,
        f"  [선별/압착] 파레트 생성  : {summary['pallets_produced']:>6} 개",
        f"  [선별/압착] 파레트 소비  : {summary['pallets_consumed']:>6} 개",
        f"  [버퍼] 잔여 파레트       : {summary['pallets_remaining']:>6} 개",
        f"  [용해] 시작/완료 배치    : {summary['melt_batches_started']:>3} / {summary['melt_batches_completed']:<3}",
        f"  [용해] 평균 배치 시간(분): {summary['avg_melt_batch_min']:>8.1f}",
        f"  [생산] 큐프레이크        : {summary['flake_units']:>6} 포대 ({summary['flake_ton']:.1f} t)",
        f"  [생산] SCR 코일          : {summary['scr_units']:>6} 개  ({summary['scr_ton']:.1f} t)",
        f"  [생산] 합계              : {summary['total_product_ton']:>8.1f} t",
        "-" * 60,
        "  [출하] 완제품 반출",
        f"    · 처리 트럭             : {summary['trucks_out_dispatched']:>6} 대  ({trucks_out_per_day:.1f} 대/일)",
        f"    · 큐프레이크 출하       : {summary['flake_dispatched_ton']:>8.1f} t",
        f"    · SCR 출하              : {summary['scr_dispatched_ton']:>8.1f} t",
        f"    · 누적 출하 물량        : {dispatched_ton:>8.1f} t  ({dispatched_ton_per_day:.1f} t/일)",
        f"    · 트럭당 평균 적재       : {avg_out_per_truck:>8.1f} t  (만재 {out_cap:.0f} t · 적재율 {out_fill:.1f}%)",
        f"    · 평균 체류시간(분)     : {summary['avg_truck_out_lead_min']:>8.1f}",
        "-" * 60,
        f"  일평균 처리량            : {summary['throughput_ton_per_day']:>8.1f} t/일",
        "-" * 60,
        "  [균형] 입고 ↔ 출하 물량 비교",
        f"    · 입고 누적              : {inbound_ton:>8.1f} t  (트럭 {summary['trucks_in_processed']}대)",
        f"    · 출하 누적              : {dispatched_ton:>8.1f} t  (트럭 {summary['trucks_out_dispatched']}대)",
        f"    · 출하/입고 비율         : {balance_pct:>7.1f}%   (이론 100%, 손실 없는 공정)",
        f"    · 공정 중 잔량 (WIP) 합  : {wip_total:>8.1f} t  ({wip_pct:.1f}% of 입고)",
        f"        ↳ 선별 대기(하역 후) : {summary.get('wip_sort_queue_ton', 0.0):>8.1f} t",
        f"        ↳ 압착 대기(sub-pile): {summary.get('wip_press_queue_ton', 0.0):>8.1f} t",
        f"        ↳ 파레트 버퍼        : {summary.get('wip_pallet_buffer_ton', 0.0):>8.1f} t",
        f"        ↳ 용해 진행 중       : {summary.get('wip_melting_in_progress_ton', 0.0):>8.1f} t",
        f"        ↳ Flake 야적장       : {summary.get('wip_flake_buffer_ton', 0.0):>8.1f} t",
        f"        ↳ SCR 야적장         : {summary.get('wip_scr_buffer_ton', 0.0):>8.1f} t",
        f"    · 미설명 잔차 (적재 중)  : {unaccounted:>+8.1f} t",
        "-" * 60,
        "  [해석] 입출고 균형 진단",
    ]
    for hint in hints:
        # 80자 정도에서 줄바꿈해 보기 좋게 정렬
        prefix = "    • "
        wrap = 70
        words = hint.split()
        cur = ""
        for w in words:
            if len(cur) + 1 + len(w) > wrap:
                lines.append(prefix + cur)
                prefix = "      "
                cur = w
            else:
                cur = (cur + " " + w).strip()
        if cur:
            lines.append(prefix + cur)
    lines.extend([
        "    ※ 손실 없는 공정이므로 이론상  [입고 = 출하 + WIP].",
        "      [출하/입고 비율]이 낮으면 공정 안에 물량이 갇혀 있다는 뜻이고,",
        "      [출하 적재율]이 낮으면 트럭은 자주 오는데 실을 게 부족하다는 뜻이며,",
        "      가장 큰 WIP 항목이 그 시점의 1차 병목 위치입니다.",
        "=" * 60,
    ])
    return "\n".join(lines)

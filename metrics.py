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

    def log(self, time_min: float, stage: str, kind: str, **detail: Any) -> None:
        self.events.append(Event(time_min=time_min, stage=stage, kind=kind, detail=detail))

    # -- KPI 계산 -----------------------------------------------------------
    def summary(self, sim_horizon_min: int) -> dict[str, Any]:
        days = sim_horizon_min / (24 * 60)
        flake_ton = self.flake_units_produced * 1.0
        scr_ton = self.scr_units_produced * 4.0
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
        }


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def format_summary(summary: dict[str, Any]) -> str:
    lines = [
        "=" * 60,
        "  군산 공장 하이브리드 공정 시뮬레이션 결과 요약",
        "=" * 60,
        f"  시뮬레이션 기간          : {summary['horizon_days']:.1f} 일",
        "-" * 60,
        f"  [입고] 처리 트럭         : {summary['trucks_in_processed']:>6} 대",
        f"  [입고] 평균 체류시간(분) : {summary['avg_truck_in_lead_min']:>8.1f}",
        f"  [선별/압착] 파레트 생성  : {summary['pallets_produced']:>6} 개",
        f"  [선별/압착] 파레트 소비  : {summary['pallets_consumed']:>6} 개",
        f"  [버퍼] 잔여 파레트       : {summary['pallets_remaining']:>6} 개",
        f"  [용해] 시작/완료 배치    : {summary['melt_batches_started']:>3} / {summary['melt_batches_completed']:<3}",
        f"  [용해] 평균 배치 시간(분): {summary['avg_melt_batch_min']:>8.1f}",
        f"  [생산] 퓨플레이크        : {summary['flake_units']:>6} 포대 ({summary['flake_ton']:.1f} t)",
        f"  [생산] SCR 코일          : {summary['scr_units']:>6} 개  ({summary['scr_ton']:.1f} t)",
        f"  [생산] 합계              : {summary['total_product_ton']:>8.1f} t",
        f"  [출하] 트럭              : {summary['trucks_out_dispatched']:>6} 대",
        f"  [출하] 퓨플레이크 출하   : {summary['flake_dispatched_ton']:>8.1f} t",
        f"  [출하] SCR 출하          : {summary['scr_dispatched_ton']:>8.1f} t",
        f"  [출하] 평균 체류시간(분) : {summary['avg_truck_out_lead_min']:>8.1f}",
        "-" * 60,
        f"  일평균 처리량            : {summary['throughput_ton_per_day']:>8.1f} t/일",
        "=" * 60,
    ]
    return "\n".join(lines)

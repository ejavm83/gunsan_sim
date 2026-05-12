"""CP-SAT 기반 반사로 배치 스케줄 최적화.

`SimPy` 시뮬레이션은 파레트가 모이는 대로 `Resource(furnace, capacity=2)`가
선착순으로 배치를 잡아가며 진행한다. 그러나 실제 운영 입장에서는
"준비 가능한 N개의 배치를 두 반사로에 어떻게 배분해야 메이크스팬이
최소가 되는가" 라는 결정이 중요하다.

이 모듈은 OR-Tools CP-SAT 솔버를 사용해 다음 작업장 스케줄링 문제를
풀어 이론적 최적 메이크스팬을 산출한다.

  변수
    - 배치 i의 시작 시각  start[i]
    - 배치 i가 사용할 반사로 fid[i] ∈ {1, 2}
  제약
    - start[i] ≥ release[i] (32 파레트가 모인 시점)
    - 같은 반사로에 배정된 배치들은 처리시간 동안 겹치지 않음
  목적
    - max_i (start[i] + duration[i]) 최소화
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from simulation import inbound_arrivals_for_day

try:
    from ortools.sat.python import cp_model  # type: ignore
except ImportError as exc:  # pragma: no cover
    cp_model = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


@dataclass
class BatchSpec:
    batch_id: int
    release_min: int       # 32 파레트가 모이는 분
    duration_min: int      # 엘리베이터 + 준비 + 용해 + 주조 합계
    batch_ton: float = 80.0


@dataclass
class ScheduleEntry:
    batch_id: int
    furnace_id: int
    start_min: int
    end_min: int


@dataclass
class ScheduleResult:
    makespan_min: int
    entries: list[ScheduleEntry]
    status: str


def estimate_batch_duration(cfg) -> int:
    """`config.SimulationConfig` 기준 한 배치(=80 t)의 처리시간(분) 추정.

    엘리베이터 운반(파레트당 설정 사이클) + 사전 준비 + 용해 병목(8h+산화·슬래깅·환원) +
    홀딩로 셋업 + 주조(병렬, max(flake, scr) 시간) 의 합으로 계산한다.
    """
    mc = cfg.melting
    cc = cfg.casting

    elevator = (mc.pallets_per_batch // mc.elevator_pallets_per_trip) * mc.elevator_cycle_min
    flake_ton = mc.batch_ton * cc.flake_ratio
    scr_ton = mc.batch_ton * cc.scr_ratio
    flake_min = flake_ton / cc.flake_unit_ton * cc.flake_min_per_unit
    scr_min = scr_ton / cc.scr_unit_ton * cc.scr_min_per_unit
    casting = max(flake_min, scr_min)

    return int(round(elevator + mc.setup_min + mc.melting_min + cc.holding_setup_min + casting))


def estimate_batch_releases(cfg) -> list[int]:
    """선별/압착 단순 계산식으로 N번째 배치(32 파레트)가 모이는 분을 추정.

    - 트럭 도착: 09~18시, 오전 80% 균등(결정론적 균등 배치로 추정).
    - 트럭당 30분 선별 + 8 sub-pile × (지게차+압착+파레트적재)×5 분 압착 → 8 파레트.
    - 즉시 정상 가동된다고 가정하여 누적 파레트 수가 32의 배수가 되는 시점을 release 로 본다.
    """
    sc = cfg.sorting
    ic = cfg.inbound
    mc = cfg.melting

    cycle = sc.forklift_load_min + sc.press_min_per_block + sc.pallet_stack_min
    truck_proc = 30.0 + sc.sub_piles_per_truck * sc.blocks_per_subpile * cycle

    pallet_finish_times: list[float] = []
    last_press_done = 0.0
    for day in range(cfg.sim_days):
        for arrive in inbound_arrivals_for_day(day, ic, None):
            # 1차 계근 5분 + 하역 20분 = 25분 후 더미가 sort_queue 에 들어감
            dump_ready = arrive + ic.weigh_in_min + ic.unloading_min
            sort_done = max(dump_ready, last_press_done) + 30.0
            for sp in range(sc.sub_piles_per_truck):
                press_start = max(sort_done, last_press_done)
                press_done = press_start + sc.blocks_per_subpile * cycle
                pallet_finish_times.append(press_done)
                last_press_done = press_done
            # truck_proc 변수는 디버깅 용도로만 보존
            _ = truck_proc

    pallet_finish_times.sort()
    releases: list[int] = []
    for i in range(mc.pallets_per_batch - 1, len(pallet_finish_times),
                   mc.pallets_per_batch):
        releases.append(int(round(pallet_finish_times[i])))
    return releases


def solve_furnace_schedule(
    batches: list[BatchSpec],
    furnace_count: int = 2,
    horizon_min: Optional[int] = None,
    time_limit_sec: float = 10.0,
) -> ScheduleResult:
    """CP-SAT 로 두 반사로의 메이크스팬 최소화 스케줄을 푼다."""
    if cp_model is None:  # pragma: no cover
        raise RuntimeError(
            f"ortools 가 설치되지 않았습니다: {_IMPORT_ERROR}\n"
            "pip install ortools 후 다시 시도하세요."
        )

    if not batches:
        return ScheduleResult(makespan_min=0, entries=[], status="EMPTY")

    if horizon_min is None:
        horizon_min = max(b.release_min for b in batches) + sum(
            b.duration_min for b in batches
        )

    model = cp_model.CpModel()
    intervals_per_furnace: dict[int, list] = {f: [] for f in range(furnace_count)}
    starts: list = []
    presences: list[list] = []

    for b in batches:
        # 배치 i 가 반사로 f 에서 수행되는 optional interval 을 정의
        per_furnace_intervals = []
        per_furnace_present = []
        start = model.NewIntVar(b.release_min, horizon_min, f"start_{b.batch_id}")
        starts.append(start)
        for f in range(furnace_count):
            present = model.NewBoolVar(f"x_{b.batch_id}_{f}")
            interval = model.NewOptionalIntervalVar(
                start,
                b.duration_min,
                start + b.duration_min,
                present,
                f"itv_{b.batch_id}_{f}",
            )
            intervals_per_furnace[f].append(interval)
            per_furnace_intervals.append(interval)
            per_furnace_present.append(present)
        # 정확히 한 반사로에만 배정
        model.AddExactlyOne(per_furnace_present)
        presences.append(per_furnace_present)

    # 같은 반사로 내에서 시간 겹침 금지
    for f in range(furnace_count):
        if intervals_per_furnace[f]:
            model.AddNoOverlap(intervals_per_furnace[f])

    # 메이크스팬 변수
    makespan = model.NewIntVar(0, horizon_min, "makespan")
    end_vars = [s + b.duration_min for s, b in zip(starts, batches)]
    model.AddMaxEquality(makespan, end_vars)
    model.Minimize(makespan)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_sec
    status = solver.Solve(model)
    status_name = solver.StatusName(status)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return ScheduleResult(makespan_min=-1, entries=[], status=status_name)

    entries: list[ScheduleEntry] = []
    for b, start_var, present_vars in zip(batches, starts, presences):
        s_val = int(solver.Value(start_var))
        f_idx = -1
        for k, p in enumerate(present_vars):
            if solver.Value(p) == 1:
                f_idx = k
                break
        entries.append(
            ScheduleEntry(
                batch_id=b.batch_id,
                furnace_id=f_idx + 1,
                start_min=s_val,
                end_min=s_val + b.duration_min,
            )
        )
    entries.sort(key=lambda e: (e.furnace_id, e.start_min))
    return ScheduleResult(
        makespan_min=int(solver.Value(makespan)),
        entries=entries,
        status=status_name,
    )


def format_schedule(result: ScheduleResult) -> str:
    if not result.entries:
        return f"[CP-SAT] 스케줄 없음 (status={result.status})"

    lines = [
        "=" * 60,
        f"  CP-SAT 반사로 배치 스케줄 (status={result.status})",
        f"  최적 메이크스팬: {result.makespan_min} 분 ({result.makespan_min/60:.1f} h)",
        "-" * 60,
        f"  {'BatchID':>7}  {'Furnace':>7}  {'Start':>10}  {'End':>10}  {'Dur':>7}",
    ]
    for e in result.entries:
        lines.append(
            f"  {e.batch_id:>7}  {e.furnace_id:>7}  "
            f"{_hm(e.start_min):>10}  {_hm(e.end_min):>10}  "
            f"{e.end_min - e.start_min:>5} m"
        )
    lines.append("=" * 60)
    return "\n".join(lines)


def _hm(t_min: int) -> str:
    d = t_min // (24 * 60)
    h = (t_min // 60) % 24
    m = t_min % 60
    return f"D{d}+{h:02d}:{m:02d}"

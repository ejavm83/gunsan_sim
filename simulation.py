"""군산 공장 하이브리드 공정의 SimPy 이산사건 시뮬레이션.

전체 5단계 공정을 다음과 같이 모델링한다.

  1. 입고/하역  : 트럭 도착(09~18시, 오전 80%) → 1차 계근 → 하역장 → 2차 계근 → 출차
  2. 선별/압착  : 하역 더미 → 선별(30분/트럭) → 압착 사이클(지게차+90초+파레트적재/블록)
                  → 파레트 적재
  3. 장입/용해  : 파레트 32개(=80 t) 모이면 엘리베이터(5분/왕복)로 운반 → 사전 준비 →
                  용해 병목(8h+산화·슬래깅·환원) → 주조 라인으로 송출 (반사로 2개 병렬)
  4. 주조       : 큐플레이크(약 3.1분/1 t)와 SCR(약 12.5분/4 t)을 2:8 비율로 병렬 생산
  5. 출하       : 빈 트럭(오전 출하 80% 편향) → 1차 계근 → 상차(20 t 기준) → 2차 계근 → 출차

사용 예::

    from config import DEFAULT_CONFIG
    from simulation import run_simulation

    metrics = run_simulation(DEFAULT_CONFIG)
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Iterator

import simpy

from config import InboundConfig, OutboundConfig, SimulationConfig
from metrics import Metrics


def inbound_arrivals_for_day(
    day: int, ic: InboundConfig, rng: random.Random | None
) -> list[float]:
    """하루치 입고 트럭 도착 시각(분). rng 가 None 이면 구간 균등·결정론적 배치."""
    base = float(day * 24 * 60)
    ws, we = float(ic.arrival_window_start_min), float(ic.arrival_window_end_min)
    me = float(ic.morning_end_min)
    n = ic.trucks_per_day
    n_morn = max(0, min(n, int(round(n * ic.morning_arrival_fraction))))
    n_aft = n - n_morn
    times: list[float] = []
    if rng is None:
        for i in range(n_morn):
            span = max(me - ws, 1e-9)
            times.append(base + ws + (i + 0.5) / max(n_morn, 1) * span)
        for j in range(n_aft):
            span_a = max(we - me, 1e-9)
            times.append(base + me + (j + 0.5) / max(n_aft, 1) * span_a)
    else:
        for _ in range(n_morn):
            times.append(base + rng.uniform(ws, me))
        for _ in range(n_aft):
            times.append(base + rng.uniform(me, we))
    times.sort()
    return times


def next_outbound_arrival_min(env_now: float, rng: random.Random, oc: OutboundConfig) -> float:
    """출하 빈 트럭 다음 도착 시각(분).

    비균질 포아송 과정으로 모델링한다:

    * 도착은 매일 오전(08~12) / 오후(12~18) 두 시간창 안에서만 발생한다.
    * 두 창의 분당 도착률은 ``morning_dispatch_fraction`` 비중에 맞춰 분배해,
      시간창 전체의 일별 평균 도착 대수가
      ``(오전 길이 + 오후 길이) / empty_truck_interval_min`` 가 되도록 한다.
      예) 간격 60분·시간창 600분 → 일별 평균 10대 (오전 8 + 오후 2).
    * 현재 시각이 시간창 밖이거나 그날 두 창에서 표본이 모두 창 밖으로
      벗어나면 다음 날 오전 창 시작 시각으로 점프해 다시 표본을 뽑는다.
    """
    morning_start = float(oc.outbound_morning_start_min)
    morning_end = float(oc.outbound_morning_end_min)
    afternoon_start = float(oc.outbound_afternoon_start_min)
    afternoon_end = float(oc.outbound_arrival_window_end_min)
    morning_span = max(morning_end - morning_start, 0.0)
    afternoon_span = max(afternoon_end - afternoon_start, 0.0)
    full_span = morning_span + afternoon_span

    mean = max(float(oc.empty_truck_interval_min), 1e-6)
    if full_span <= 0.0:
        return float(env_now) + mean

    morning_fraction = min(max(float(oc.morning_dispatch_fraction), 0.0), 1.0)
    afternoon_fraction = 1.0 - morning_fraction
    daily_expected = full_span / mean
    morning_rate = (
        (daily_expected * morning_fraction) / morning_span if morning_span > 0 else 0.0
    )
    afternoon_rate = (
        (daily_expected * afternoon_fraction) / afternoon_span
        if afternoon_span > 0
        else 0.0
    )

    t = float(env_now)
    for _ in range(800):
        day_floor = int(t // 1440) * 1440.0
        windows = (
            (day_floor + morning_start, day_floor + morning_end, morning_rate),
            (day_floor + afternoon_start, day_floor + afternoon_end, afternoon_rate),
        )
        for lo, hi, rate in windows:
            if rate <= 0.0 or hi <= t:
                continue
            t_start = max(t, lo)
            cand = t_start + rng.expovariate(rate)
            if cand < hi:
                return cand
        t = day_floor + 1440.0 + morning_start

    return t


# ---------------------------------------------------------------------------
# 공장 모델
# ---------------------------------------------------------------------------


@dataclass
class TruckDump:
    truck_id: int
    payload_ton: float


@dataclass
class SubPile:
    truck_id: int
    sub_pile_id: int


@dataclass
class Pallet:
    pallet_id: int
    created_min: float


class GunsanFactory:
    """군산 공장 자원/큐 그래프와 5단계 공정 프로세스를 정의한다."""

    def __init__(
        self,
        env: simpy.Environment,
        cfg: SimulationConfig,
        metrics: Metrics,
        rng: random.Random,
    ) -> None:
        self.env = env
        self.cfg = cfg
        self.m = metrics
        self.rng = rng
        self._pallet_serial = 0

        ic, sc, mc, cc, oc = (
            cfg.inbound,
            cfg.sorting,
            cfg.melting,
            cfg.casting,
            cfg.outbound,
        )

        # 1) 계근대(공용) - 입고/출하 트럭이 같이 사용
        self.weighbridge = simpy.Resource(env, capacity=ic.weighbridge_count)
        self.unloading_bay = simpy.Resource(env, capacity=ic.unloading_bays)

        # 2) 선별/압착 자원 및 큐
        self.sorters = simpy.Resource(env, capacity=sc.sorters)
        self.press = simpy.Resource(env, capacity=sc.press_machines)
        self.sort_queue: simpy.Store = simpy.Store(env)
        self.press_queue: simpy.Store = simpy.Store(env)
        self.pallet_buffer: simpy.Store = simpy.Store(
            env, capacity=sc.pallet_buffer_capacity
        )

        # 3) 장입/용해
        self.elevator = simpy.Resource(env, capacity=mc.elevator_count)
        self.furnaces = simpy.Resource(env, capacity=mc.furnace_count)

        # 4) 주조 라인 (큐프레이크 / SCR)
        self.flake_line = simpy.Resource(env, capacity=cc.casting_lines_flake)
        self.scr_line = simpy.Resource(env, capacity=cc.casting_lines_scr)

        # 5) 완제품 버퍼
        self.flake_buffer: simpy.Store = simpy.Store(env, capacity=oc.flake_buffer_unit)
        self.scr_buffer: simpy.Store = simpy.Store(env, capacity=oc.scr_buffer_unit)

    # ---- 유틸 ------------------------------------------------------------
    def _pallet_id(self) -> int:
        self._pallet_serial += 1
        return self._pallet_serial

    def _record_pallet_level(self) -> None:
        self.m.pallet_buffer_levels.append(
            (self.env.now, len(self.pallet_buffer.items))
        )

    def _record_flake_level(self) -> None:
        self.m.flake_buffer_levels.append(
            (self.env.now, len(self.flake_buffer.items))
        )

    def _record_scr_level(self) -> None:
        self.m.scr_buffer_levels.append(
            (self.env.now, len(self.scr_buffer.items))
        )

    # ---- 1. 입고/하역 ----------------------------------------------------
    def truck_inbound_generator(self) -> Iterator[simpy.events.Event]:
        """매일 09~18시 사이 도착(오전 80% 균등)."""
        ic = self.cfg.inbound
        truck_id = 0
        for day in range(self.cfg.sim_days):
            for arrive_at in inbound_arrivals_for_day(day, ic, self.rng):
                delay = max(0.0, arrive_at - self.env.now)
                if delay:
                    yield self.env.timeout(delay)
                truck_id += 1
                self.env.process(self.truck_inbound(truck_id))

    def truck_inbound(self, truck_id: int) -> Iterator[simpy.events.Event]:
        ic = self.cfg.inbound
        t0 = self.env.now
        self.m.log(t0, "inbound", "arrive", truck=truck_id)

        # 1차 계근
        with self.weighbridge.request() as req:
            yield req
            yield self.env.timeout(ic.weigh_in_min)
        self.m.log(self.env.now, "inbound", "weigh_in", truck=truck_id)

        # 하역
        with self.unloading_bay.request() as req:
            yield req
            yield self.env.timeout(ic.unloading_min)
            dump = TruckDump(truck_id=truck_id, payload_ton=ic.payload_ton)
            yield self.sort_queue.put(dump)
            # 입고 톤수 누적: 하역이 끝나 더미가 공정 안으로 들어간 시점 기준
            self.m.inbound_ton += ic.payload_ton
        self.m.log(self.env.now, "inbound", "unloaded", truck=truck_id)

        # 2차 계근(영점)
        with self.weighbridge.request() as req:
            yield req
            yield self.env.timeout(ic.weigh_out_min)

        self.m.truck_in_done += 1
        self.m.truck_in_lead_times.append(self.env.now - t0)
        self.m.log(self.env.now, "inbound", "depart", truck=truck_id)

    # ---- 2. 선별 및 압착 -------------------------------------------------
    def sort_worker(self) -> Iterator[simpy.events.Event]:
        sc = self.cfg.sorting
        while True:
            dump: TruckDump = yield self.sort_queue.get()
            with self.sorters.request() as req:
                yield req
                # 트럭 1대 분(20 t) 정리에 30분 소요 -> 8개의 sub-pile 생성
                yield self.env.timeout(30.0)
            self.m.log(self.env.now, "sorting", "sort_done", truck=dump.truck_id)
            for i in range(sc.sub_piles_per_truck):
                yield self.press_queue.put(
                    SubPile(truck_id=dump.truck_id, sub_pile_id=i)
                )

    def press_worker(self) -> Iterator[simpy.events.Event]:
        sc = self.cfg.sorting
        cycle_min = sc.forklift_load_min + sc.press_min_per_block + sc.pallet_stack_min
        while True:
            sp: SubPile = yield self.press_queue.get()
            with self.press.request() as req:
                yield req
                # 0.5 t 블록 5개 = 파레트 1개 (= 2.5 t) 처리
                for _ in range(sc.blocks_per_subpile):
                    yield self.env.timeout(cycle_min)
            pallet = Pallet(pallet_id=self._pallet_id(), created_min=self.env.now)
            yield self.pallet_buffer.put(pallet)
            self.m.pallets_produced += 1
            self._record_pallet_level()
            self.m.log(
                self.env.now,
                "press",
                "pallet_done",
                pallet=pallet.pallet_id,
                buffer=len(self.pallet_buffer.items),
            )

    # ---- 3 & 4. 장입/용해/주조 ------------------------------------------
    def furnace_worker(self, furnace_id: int) -> Iterator[simpy.events.Event]:
        mc = self.cfg.melting
        cc = self.cfg.casting
        while True:
            # 32 파레트(= 80 t) 모일 때까지 대기
            collected: list[Pallet] = []
            for _ in range(mc.pallets_per_batch):
                pallet: Pallet = yield self.pallet_buffer.get()
                collected.append(pallet)
            self._record_pallet_level()
            t_collect = self.env.now
            self.m.batches_started += 1
            self.m.pallets_consumed += len(collected)
            self.m.log(
                t_collect,
                "melting",
                "batch_collected",
                furnace=furnace_id,
                pallets=len(collected),
            )

            # 엘리베이터 운반: 2 파레트 / 회 × 10분
            trips = mc.pallets_per_batch // mc.elevator_pallets_per_trip
            for trip in range(trips):
                with self.elevator.request() as req:
                    yield req
                    yield self.env.timeout(mc.elevator_cycle_min)
            self.m.log(
                self.env.now,
                "melting",
                "elevator_done",
                furnace=furnace_id,
                trips=trips,
            )

            # 반사로 점유: 사전준비 + 용해
            with self.furnaces.request() as req:
                yield req
                yield self.env.timeout(mc.setup_min)
                self.m.log(self.env.now, "melting", "melt_start", furnace=furnace_id)
                yield self.env.timeout(mc.melting_min)
                self.m.log(self.env.now, "melting", "melt_done", furnace=furnace_id)

                # 홀딩로 셋업
                yield self.env.timeout(cc.holding_setup_min)

                # 4. 라인 분기 (주조)
                yield from self.casting_run(furnace_id, mc.batch_ton)

            self.m.batches_completed += 1
            self.m.melt_batch_durations.append(self.env.now - t_collect)
            self.m.log(self.env.now, "melting", "batch_done", furnace=furnace_id)

    def casting_run(
        self, furnace_id: int, batch_ton: float
    ) -> Iterator[simpy.events.Event]:
        cc = self.cfg.casting
        flake_ton = batch_ton * cc.flake_ratio
        scr_ton = batch_ton * cc.scr_ratio
        flake_units = int(flake_ton / cc.flake_unit_ton)
        scr_units = int(scr_ton / cc.scr_unit_ton)

        flake_proc = self.env.process(self._cast_flake(furnace_id, flake_units))
        scr_proc = self.env.process(self._cast_scr(furnace_id, scr_units))

        # 두 라인의 병렬 생산을 모두 마치면 한 배치 종료
        yield self.env.all_of([flake_proc, scr_proc])

    def _cast_flake(
        self, furnace_id: int, units: int
    ) -> Iterator[simpy.events.Event]:
        cc = self.cfg.casting
        with self.flake_line.request() as req:
            yield req
            for _ in range(units):
                yield self.env.timeout(cc.flake_min_per_unit)
                if len(self.flake_buffer.items) >= self.flake_buffer.capacity:
                    self.m.log(self.env.now, "casting", "flake_buffer_full",
                               furnace=furnace_id)
                # 버퍼가 가득 차면 생산이 막혀 대기 - put 자체가 블록킹
                yield self.flake_buffer.put({"created_min": self.env.now})
                self.m.flake_units_produced += 1
                self._record_flake_level()
        self.m.log(self.env.now, "casting", "flake_done",
                   furnace=furnace_id, units=units)

    def _cast_scr(
        self, furnace_id: int, units: int
    ) -> Iterator[simpy.events.Event]:
        cc = self.cfg.casting
        with self.scr_line.request() as req:
            yield req
            for _ in range(units):
                yield self.env.timeout(cc.scr_min_per_unit)
                if len(self.scr_buffer.items) >= self.scr_buffer.capacity:
                    self.m.log(self.env.now, "casting", "scr_buffer_full",
                               furnace=furnace_id)
                yield self.scr_buffer.put({"created_min": self.env.now})
                self.m.scr_units_produced += 1
                self._record_scr_level()
        self.m.log(self.env.now, "casting", "scr_done",
                   furnace=furnace_id, units=units)

    # ---- 5. 출하 ---------------------------------------------------------
    def outbound_truck_generator(self) -> Iterator[simpy.events.Event]:
        oc = self.cfg.outbound
        cc = self.cfg.casting
        # 출하 시작 시각까지 대기
        if self.env.now < oc.outbound_start_min:
            yield self.env.timeout(oc.outbound_start_min - self.env.now)

        truck_id = 0
        while True:
            truck_id += 1
            nxt = next_outbound_arrival_min(self.env.now, self.rng, oc)
            delay = max(0.0, nxt - self.env.now)
            if delay:
                yield self.env.timeout(delay)

            # 주조 비율과 동일 확률로 트럭 적재 종류 결정
            load_kind = "flake" if self.rng.random() < cc.flake_ratio else "scr"
            self.env.process(self.outbound_truck(truck_id, load_kind))

    def outbound_truck(
        self, truck_id: int, load_kind: str
    ) -> Iterator[simpy.events.Event]:
        oc = self.cfg.outbound
        t0 = self.env.now
        self.m.log(t0, "outbound", "arrive", truck=truck_id, load_kind=load_kind)

        # 1차 계근(영점)
        with self.weighbridge.request() as req:
            yield req
            yield self.env.timeout(oc.weigh_in_min)

        # 적재 - flake 트럭은 1 t 포대 20개, scr 트럭은 4 t 코일 5개(20 t)
        if load_kind == "flake":
            target_units = int(oc.truck_capacity_ton // 1.0)
            buf = self.flake_buffer
            unit_ton = 1.0
        else:
            target_units = int(oc.truck_capacity_ton // 4.0)  # 5개 (= 20 t)
            buf = self.scr_buffer
            unit_ton = 4.0

        loaded_units = 0
        loaded_ton = 0.0
        # 최대 4시간 대기 - 그 사이 가용 재고만큼만 적재 후 출발
        deadline = self.env.now + 4 * 60
        while loaded_units < target_units and self.env.now < deadline:
            try:
                get_event = buf.get()
                timeout_event = self.env.timeout(deadline - self.env.now)
                result = yield self.env.any_of([get_event, timeout_event])
                if get_event in result:
                    loaded_units += 1
                    loaded_ton += unit_ton
                else:
                    get_event.cancel()
                    break
            except simpy.Interrupt:
                break

        if load_kind == "flake":
            self._record_flake_level()
        else:
            self._record_scr_level()

        if loaded_ton > 0:
            yield self.env.timeout(oc.loading_min_per_ton * loaded_ton)

        # 2차 계근(중량 검증)
        with self.weighbridge.request() as req:
            yield req
            yield self.env.timeout(oc.weigh_out_min)

        if load_kind == "flake":
            self.m.flake_dispatched_ton += loaded_ton
        else:
            self.m.scr_dispatched_ton += loaded_ton

        self.m.truck_out_done += 1
        self.m.truck_out_lead_times.append(self.env.now - t0)
        self.m.log(
            self.env.now,
            "outbound",
            "depart",
            truck=truck_id,
            load_kind=load_kind,
            ton=loaded_ton,
        )


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def run_simulation(
    cfg: SimulationConfig,
    progress: Callable[[str], None] | None = None,
    on_tick: Callable[[float, float, Metrics], None] | None = None,
    tick_steps: int = 100,
) -> Metrics:
    """주어진 설정으로 시뮬레이션을 실행하고 Metrics 객체를 반환한다.

    progress:
        호출될 때마다 한 줄 안내 문자열을 넘긴다. CLI·웹에서 실행 과정을
        사용자에게 보여 줄 때 사용한다. None 이면 호출하지 않는다.
    on_tick:
        ``(fraction, sim_time_min, metrics)`` 형태로 주기적으로 호출되어
        프로그래스 바(웹 ``st.progress``·CLI 한 줄 갱신 등)를 갱신할 수 있게 한다.
        ``fraction`` 은 0.0~1.0, ``sim_time_min`` 은 현재 가상 시각(분),
        ``metrics`` 는 누적 카운터를 그대로 노출한 객체다. ``None`` 이면
        기존처럼 ``env.run(until=horizon)`` 을 한 번에 돌린다.
    tick_steps:
        ``on_tick`` 이 주어진 경우 ``horizon`` 을 몇 단계로 쪼개 진행률을
        보고할지 결정한다. 큰 값일수록 갱신은 자주, 단계 사이 오버헤드는
        조금 늘어난다. 기본 100단계(1%) 정도면 화면 갱신이 매끄럽다.
    """

    def _p(msg: str) -> None:
        if progress is not None:
            progress(msg)

    horizon = cfg.sim_horizon_min
    _p(
        f"① 설정 적용: {cfg.sim_days}일치 · 시뮬 종료 시각 {horizon}분 "
        f"(약 {horizon / 60:.1f}시간) · 난수 시드 {cfg.random_seed}"
    )

    rng = random.Random(cfg.random_seed)
    env = simpy.Environment()
    metrics = Metrics()
    # 입출고 단위 톤수(만재 기준)는 cfg 에서 복사해 두어 KPI 계산에서 사용한다.
    metrics.inbound_payload_ton = cfg.inbound.payload_ton
    metrics.outbound_truck_capacity_ton = cfg.outbound.truck_capacity_ton
    factory = GunsanFactory(env, cfg, metrics, rng)
    _p("② SimPy 환경·공장(자원·큐·버퍼) 객체를 초기화했습니다.")

    # 5단계 워커 등록
    env.process(factory.truck_inbound_generator())
    for _ in range(cfg.sorting.sorters):
        env.process(factory.sort_worker())
    for _ in range(cfg.sorting.press_machines):
        env.process(factory.press_worker())
    for f in range(cfg.melting.furnace_count):
        env.process(factory.furnace_worker(furnace_id=f + 1))
    env.process(factory.outbound_truck_generator())
    _p(
        "③ 프로세스 등록: 입고 생성 · 선별 "
        f"×{cfg.sorting.sorters} · 압착 ×{cfg.sorting.press_machines} · "
        f"반사로 ×{cfg.melting.furnace_count} · 출하 생성"
    )

    _p(
        "④ env.run() — 0분부터 종료 시각까지 이산사건(트럭·파레트·배치·출하 등)을 "
        "시간순으로 처리합니다…"
    )
    if on_tick is None:
        env.run(until=horizon)
    else:
        # 진행률을 ``tick_steps`` 단계로 나누어 보고한다. 각 단계 사이에
        # ``env.run(until=t)`` 를 호출해 가상 시각을 ``t`` 까지 전진시키고,
        # 매 단계 끝에서 누적 카운터를 그대로 넘겨 외부에서 화면을 그릴 수
        # 있도록 한다.
        steps = max(1, int(tick_steps))
        try:
            on_tick(0.0, 0.0, metrics)
        except Exception:
            # 진행률 콜백에서 예외가 나도 시뮬 본진은 계속 돌아가야 한다.
            pass
        for i in range(1, steps + 1):
            t_next = horizon * i / steps
            env.run(until=t_next)
            try:
                on_tick(i / steps, float(env.now), metrics)
            except Exception:
                pass

    # 시뮬 종료 시점의 공정 중 잔량(WIP) 톤수를 metrics 에 기록.
    # - sort_queue: 하역 직후 더미 (트럭 1대 = pile_size_ton)
    # - press_queue: 선별 후 sub-pile (sub_pile_ton)
    # - pallet_buffer: 압착·적재 완료 파레트 (pallet_ton)
    # - 진행 중 배치: 시작했으나 미완료인 배치 (batch_ton). 엘리베이터/용해/주조
    #   중간 단계의 잔량을 단순화해서 한 덩어리로 잡는다.
    # - flake/scr_buffer: 완제품 야적장 잔량 (단위 톤수 × 잔여 단위 수)
    ic = cfg.inbound
    sc = cfg.sorting
    mc = cfg.melting
    cc = cfg.casting
    metrics.wip_sort_queue_ton = len(factory.sort_queue.items) * ic.pile_size_ton
    metrics.wip_press_queue_ton = len(factory.press_queue.items) * sc.sub_pile_ton
    metrics.wip_pallet_buffer_ton = len(factory.pallet_buffer.items) * sc.pallet_ton
    in_progress_batches = max(metrics.batches_started - metrics.batches_completed, 0)
    metrics.wip_melting_in_progress_ton = in_progress_batches * mc.batch_ton
    metrics.wip_flake_buffer_ton = len(factory.flake_buffer.items) * cc.flake_unit_ton
    metrics.wip_scr_buffer_ton = len(factory.scr_buffer.items) * cc.scr_unit_ton

    _p(
        f"⑤ 엔진 종료: 사건 로그 {len(metrics.events)}건 · "
        f"용해 완료 배치 {metrics.batches_completed}건 · "
        f"입고 트럭 {metrics.truck_in_done}대 처리"
    )
    return metrics

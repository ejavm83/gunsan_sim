"""군산 공장 하이브리드 공정의 SimPy 이산사건 시뮬레이션.

전체 5단계 공정을 다음과 같이 모델링한다.

  1. 입고/하역  : 트럭 도착 → 1차 계근 → 하역장 → 2차 계근 → 출차
  2. 선별/압착  : 하역 더미 → 선별(30분/트럭) → 압착(0.5 t × 8.5분) → 파레트 적재
  3. 장입/용해  : 파레트 32개(=80 t) 모이면 엘리베이터로 운반 → 사전 준비 →
                  12시간 용해 → 주조 라인으로 송출 (반사로 2개 병렬)
  4. 주조       : 같은 쇳물을 퓨플레이크 라인(2.5분/1 t)과 SCR 라인(10분/4 t)으로
                  3:7 비율 동시 생산
  5. 출하       : 빈 트럭 도착 → 1차 계근 → 상차 → 2차 계근 → 출차

사용 예::

    from config import DEFAULT_CONFIG
    from simulation import run_simulation

    metrics = run_simulation(DEFAULT_CONFIG)
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterator

import simpy

from config import SimulationConfig
from metrics import Metrics


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

        # 4) 주조 라인 (퓨플레이크 / SCR)
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
        """매일 오전 10시부터 1시간 간격으로 트럭을 도착시킨다."""
        ic = self.cfg.inbound
        truck_id = 0
        for day in range(self.cfg.sim_days):
            for k in range(ic.trucks_per_day):
                arrive_at = day * 24 * 60 + ic.first_arrival_min + k * ic.arrival_interval_min
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
            # 지수 분포로 도착 간격 모델링 (평균 90분)
            gap = self.rng.expovariate(1.0 / oc.empty_truck_interval_min)
            yield self.env.timeout(gap)

            # 30/70 비율로 트럭의 적재 종류를 결정
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

        # 적재 - flake 트럭은 1 t 포대 20개, scr 트럭은 4 t 코일 5개
        if load_kind == "flake":
            target_units = int(oc.truck_capacity_ton // 1.0)  # 22개 (≤ 22.5 t)
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


def run_simulation(cfg: SimulationConfig) -> Metrics:
    """주어진 설정으로 시뮬레이션을 실행하고 Metrics 객체를 반환한다."""
    rng = random.Random(cfg.random_seed)
    env = simpy.Environment()
    metrics = Metrics()
    factory = GunsanFactory(env, cfg, metrics, rng)

    # 5단계 워커 등록
    env.process(factory.truck_inbound_generator())
    for _ in range(cfg.sorting.sorters):
        env.process(factory.sort_worker())
    for _ in range(cfg.sorting.press_machines):
        env.process(factory.press_worker())
    for f in range(cfg.melting.furnace_count):
        env.process(factory.furnace_worker(furnace_id=f + 1))
    env.process(factory.outbound_truck_generator())

    env.run(until=cfg.sim_horizon_min)
    return metrics

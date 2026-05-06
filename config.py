"""군산 공장 하이브리드 공정 시뮬레이션 파라미터.

문서(`군산 공정 상세-김홍태보완.md`)의 1~5단계에 기재된 시간/용량 값을 그대로
파이썬 데이터 클래스로 옮긴 모듈이다. 모든 시간 단위는 *분(min)*, 모든 중량
단위는 *톤(t)* 으로 통일한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 1. 입고 / 하역 (Inbound)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InboundConfig:
    trucks_per_day: int = 10
    payload_ton: float = 20.0
    first_arrival_min: int = 10 * 60  # 오전 10시
    arrival_interval_min: int = 60     # 1시간 간격

    weighbridge_count: int = 1         # 1차/2차 계근 공용
    weigh_in_min: float = 5.0
    weigh_out_min: float = 5.0

    unloading_bays: int = 2            # 동시에 두 대 하역 가능
    unloading_min: float = 20.0
    pile_size_ton: float = 20.0        # 5x5 산더미 1개 = 트럭 1대 분


# ---------------------------------------------------------------------------
# 2. 선별 / 압착 (Sorting & Pressing)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SortingConfig:
    # 20 t 더미를 8개의 2.5 t 정리 더미로 만든다.
    sub_piles_per_truck: int = 8
    sub_pile_ton: float = 2.5
    sort_min_per_subpile: float = 30.0 / 8  # 30분 / 더미 8개 ≈ 3.75분
    sorters: int = 2                          # 선별 작업조

    # 0.5 t 단위로 압착기에 투입한다. 한 sub_pile = 5 block.
    block_ton: float = 0.5
    blocks_per_subpile: int = 5
    forklift_load_min: float = 5.0  # 지게차 0.5 t 적재
    press_min_per_block: float = 1.5  # 90초
    pallet_stack_min: float = 2.0   # 0.5 t 1개를 파레트에 적재

    press_machines: int = 1
    forklifts: int = 2

    # 파레트 1개 = 0.5 t × 5 = 2.5 t
    blocks_per_pallet: int = 5
    pallet_ton: float = 2.5
    pallet_buffer_capacity: int = 160  # 2일치 ≈ 400 t


# ---------------------------------------------------------------------------
# 3. 장입 / 용해 (Charging & Melting)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeltingConfig:
    # 80 t = 파레트 32개를 한 배치 기본 단위로 한다.
    batch_ton: float = 80.0
    pallets_per_batch: int = 32
    max_batch_ton: float = 200.0

    elevator_count: int = 1
    elevator_pallets_per_trip: int = 2  # 5 t/회
    elevator_cycle_min: float = 10.0

    setup_min: float = 120.0    # 사전 준비 2시간
    melting_min: float = 720.0  # 12시간 용해

    furnace_count: int = 2  # 반사로(소단지) 2개


# ---------------------------------------------------------------------------
# 4. 하이브리드 제품 생산 (Casting)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CastingConfig:
    holding_setup_min: float = 90.0  # 1~2시간 셋업 평균값

    # 비율: 퓨플레이크 3 : SCR 7
    flake_ratio: float = 0.3
    scr_ratio: float = 0.7

    # 퓨플레이크: 1 t 포대 / 2.5분
    flake_unit_ton: float = 1.0
    flake_min_per_unit: float = 2.5

    # SCR 코일: 4 t / 10분
    scr_unit_ton: float = 4.0
    scr_min_per_unit: float = 10.0

    casting_lines_flake: int = 1
    casting_lines_scr: int = 1


# ---------------------------------------------------------------------------
# 5. 완제품 야적 / 출하 (Outbound)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutboundConfig:
    flake_buffer_unit: int = 100  # 100 포대 = 100 t
    scr_buffer_unit: int = 75     # 75 코일 = 300 t

    truck_capacity_ton: float = 22.5  # 20~25 t 평균값
    weigh_in_min: float = 5.0
    weigh_out_min: float = 5.0
    loading_min_per_ton: float = 0.6  # 22.5 t 상차에 약 13~15분

    # 빈 트럭 도착 간격: 평균값(분)
    empty_truck_interval_min: float = 90.0
    outbound_start_min: int = 8 * 60  # 오전 8시부터 출하 시작


# ---------------------------------------------------------------------------
# 전체 묶음
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SimulationConfig:
    sim_days: int = 7
    random_seed: int = 42

    inbound: InboundConfig = field(default_factory=InboundConfig)
    sorting: SortingConfig = field(default_factory=SortingConfig)
    melting: MeltingConfig = field(default_factory=MeltingConfig)
    casting: CastingConfig = field(default_factory=CastingConfig)
    outbound: OutboundConfig = field(default_factory=OutboundConfig)

    @property
    def sim_horizon_min(self) -> int:
        return self.sim_days * 24 * 60


DEFAULT_CONFIG = SimulationConfig()

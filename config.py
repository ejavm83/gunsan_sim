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
    # 입고 시각: 09~18시 균등, 오전(기본 ~12시) 구간에 전체의 80% 배치 (소재 공장 운영 정보)
    arrival_window_start_min: int = 9 * 60
    arrival_window_end_min: int = 18 * 60
    morning_end_min: int = 12 * 60
    morning_arrival_fraction: float = 0.8
    # 하위 호환·추정식용: 고정 간격 모델을 쓰지 않지만 기본값 유지
    first_arrival_min: int = 9 * 60
    arrival_interval_min: int = 54  # 09~18h 균등 10대 시 대략 간격

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
    pallet_stack_min: float = 3.0   # 덩어리 1개 파레트 적재 약 3분

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
    elevator_cycle_min: float = 5.0  # 왕복 1사이클 약 5분

    setup_min: float = 120.0    # 사전 준비 2시간
    # 병목: 8h 용해 + 산화 30분 + 슬래깅 30분 + 환원 4시간 (신규 운영 기준)
    melting_min: float = 8 * 60 + 30 + 30 + 4 * 60  # 780

    furnace_count: int = 2  # 반사로(소단지) 2개


# ---------------------------------------------------------------------------
# 4. 하이브리드 제품 생산 (Casting)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CastingConfig:
    holding_setup_min: float = 90.0  # 1~2시간 셋업 평균값

    # 비율: 큐플레이크 20% : SCR 80%
    flake_ratio: float = 0.2
    scr_ratio: float = 0.8

    # 큐플레이크: 1 t / 약 3.1분
    flake_unit_ton: float = 1.0
    flake_min_per_unit: float = 3.1

    # SCR 코일: 4 t / 약 12.5분 (톤당 속도는 큐플레이크와 동일 수준)
    scr_unit_ton: float = 4.0
    scr_min_per_unit: float = 12.5

    casting_lines_flake: int = 1
    casting_lines_scr: int = 1


# ---------------------------------------------------------------------------
# 5. 완제품 야적 / 출하 (Outbound)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutboundConfig:
    flake_buffer_unit: int = 100  # 100 포대 = 100 t
    scr_buffer_unit: int = 75     # 75 코일 = 300 t

    truck_capacity_ton: float = 20.0  # 시뮬 기준 20 t
    weigh_in_min: float = 5.0
    weigh_out_min: float = 5.0
    loading_min_per_ton: float = 0.6  # 20 t 상차 약 12분 전후

    # 빈 트럭 도착 간격: 평균값(분); 오전(8~12시) 출하 비중 약 80%
    # 입고(10대/일 × 20t = 200t/일)와 출하 톤수가 균형을 이루도록 60분으로 잡았다.
    # (시간창 600분 / 60분 = 일별 평균 10대 → 200t/일)
    empty_truck_interval_min: float = 60.0
    outbound_start_min: int = 8 * 60  # 오전 8시부터 출하 시작
    morning_dispatch_fraction: float = 0.8
    outbound_morning_start_min: int = 8 * 60
    outbound_morning_end_min: int = 12 * 60
    outbound_afternoon_start_min: int = 12 * 60
    outbound_arrival_window_end_min: int = 18 * 60


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

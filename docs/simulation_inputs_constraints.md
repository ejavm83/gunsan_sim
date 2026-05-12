# 시뮬레이션 입력·제약·도출 항목

이 문서는 도메인 설명( `군산 공정 상세-김홍태보완.md` )과 실제 코드( `config.py`, `simulation.py`, `main.py`, `optimizer.py`, `webapp.py` )가 어떻게 대응하는지 정리한다. 시간 단위는 코드와 동일하게 분(min), 중량은 톤(t) 이다.

---

## 1. 입력이 어디서 오는가

| 출처 | 설명 |
|------|------|
| `config.py` / `DEFAULT_CONFIG` | 공정·용량·시간의 기본값. 상세 공정 문서의 1~5단계 수치를 데이터 클래스로 옮긴 것이며, SimPy 모델이 여기서 값을 읽는다. |
| `python main.py` | `--days`, `--seed`만 `SimulationConfig`를 덮어쓴다. 나머지는 전부 기본값. |
| `webapp.py` (Streamlit) | 사이드바에서 일부 파라미터만 조정하고, 실행 시 `SimulationConfig`를 `dataclasses.replace`로 조립한다. |
| `optimizer.py` + `main.py` | CP-SAT 비교 시, 시뮬 이벤트 로그에서 파레트 완료 시각을 모아 배치 release를 만들거나, 없으면 `estimate_batch_releases(cfg)`로 추정한다. 배치 길이는 `estimate_batch_duration(cfg)`로 산출한다. |

---

## 2. `SimulationConfig` 필드와 도메인 문서 대응

### 2.1 전역

| 필드 | 기본(예) | 의미 | 비고 |
|------|----------|------|------|
| `sim_days` | 7 | 시뮬레이션 일수 | `sim_horizon_min = sim_days × 24 × 60` 까지 실행 |
| `random_seed` | 42 | 난수 시드 | 출하 트럭 간격(지수분포), flake/scr 차종 등 |

### 2.2 입고·하역 `InboundConfig`

| 필드 | 기본(예) | 도메인 문서 요지 | 모델에서의 역할 |
|------|----------|------------------|-----------------|
| `trucks_per_day` | 10 | 하루 약 10대 | 일별 루프에서 도착 트럭 수 |
| `payload_ton` | 20 | 대당 약 20 t | 하역 후 더미 1개에 실리는 양 |
| `first_arrival_min` | 600 (10:00) | 오전 10시부터 도착 | 첫 트럭 시각 |
| `arrival_interval_min` | 60 | 1시간 간격 스케줄 | 몰림 방지용 결정론적 간격 |
| `weighbridge_count` | 1 | 계근대 | 입고·출하 공용 리소스 |
| `weigh_in_min` / `weigh_out_min` | 각 5 | 계근 소요 | 1차·2차 계근 |
| `unloading_bays` | 2 | 동시 하역 2대 | 하역 병렬도 |
| `unloading_min` | 20 | 하역 약 20분 | |
| `pile_size_ton` | 20 | 5×5 산더미 1개 ≈ 1대 분량 | 설정에만 존재, `simulation.py` 미사용 |

### 2.3 선별·압착 `SortingConfig`

| 필드 | 기본(예) | 도메인 문서 요지 | 모델에서의 역할 |
|------|----------|------------------|-----------------|
| `sub_piles_per_truck` | 8 | 20 t → 8개 더미 | 선별 후 `press_queue` 투입 개수 |
| `sub_pile_ton` | 2.5 | | 설명·균형용 |
| `sort_min_per_subpile` | 30/8 | 30분/트럭을 8등분한 값 | 참고용; 실제 선별 시간은 코드에서 30분 고정 |
| `sorters` | 2 | 선별 작업조 | `Resource` 용량 |
| `block_ton` | 0.5 | 지게차 0.5 t 단위 | |
| `blocks_per_subpile` | 5 | sub-pile당 5블록 → 파레트 1개 | |
| `forklift_load_min` | 5 | 지게차 투입 ~5분 | 압착 사이클에 합산 |
| `press_min_per_block` | 1.5 | 90초 압착 | |
| `pallet_stack_min` | 2 | 파레트 적재 | |
| `press_machines` | 1 | 압착기 | `Resource` 용량 |
| `forklifts` | 2 | | 설정만, 시뮬 미사용 |
| `pallet_ton` | 2.5 | 파레트 1개 2.5 t | 웹에서 `batch_ton / pallet_ton` → `pallets_per_batch` |
| `pallet_buffer_capacity` | 160 | 약 2일치 버퍼 | SimPy `Store` 최대 개수 |
| `blocks_per_pallet` | 5 | | |

압착 1 sub-pile 처리 시간(코드): 한 사이클 = `forklift_load_min + press_min_per_block + pallet_stack_min` 을 블록 수(`blocks_per_subpile`)만큼 반복.

### 2.4 장입·용해 `MeltingConfig`

| 필드 | 기본(예) | 도메인 문서 요지 | 모델에서의 역할 |
|------|----------|------------------|-----------------|
| `batch_ton` | 80 | 80 t = 파레트 32개 | 배치당 쇳물 양(주조 분배 기준) |
| `pallets_per_batch` | 32 | | 파레트 `get()` 횟수 = 배치 시작 조건 |
| `max_batch_ton` | 200 | 최대 200 t까지 가동 가능 | 설정만, 상한 검증·로직 없음 |
| `elevator_count` | 1 | | |
| `elevator_pallets_per_trip` | 2 | 2파레트(5 t)/회 | |
| `elevator_cycle_min` | 10 | 왕복 10분 | |
| `setup_min` | 120 | 사전 준비 ~2시간 | |
| `melting_min` | 720 | 12시간 용해 | 병목 구간 |
| `furnace_count` | 2 | 반사로 2기 | 동시 배치 처리 상한 |

### 2.5 주조 `CastingConfig`

| 필드 | 기본(예) | 도메인 문서 요지 | 모델에서의 역할 |
|------|----------|------------------|-----------------|
| `holding_setup_min` | 90 | 홀딩로 셋업 1~2시간 평균 | 용해 직후 대기 |
| `flake_ratio` / `scr_ratio` | 0.3 / 0.7 | 큐프레이크:SCR = 3:7 | 배치 톤 분배 후 단위 개수 계산 |
| `flake_unit_ton` | 1, `flake_min_per_unit` | 2.5분당 1 t | 큐프레이크 생산 속도 |
| `scr_unit_ton` | 4, `scr_min_per_unit` | 10분당 4 t | SCR 생산 속도 |
| `casting_lines_flake` / `casting_lines_scr` | 1 / 1 | | 라인별 `Resource`(기본 각 1) |

단위 개수: `int((batch_ton × 비율) / unit_ton)` 으로 버림 → 설정 비율과 실제 생산 중량에 소단차 가능.

### 2.6 출하 `OutboundConfig`

| 필드 | 기본(예) | 도메인 문서 요지 | 모델에서의 역할 |
|------|----------|------------------|-----------------|
| `flake_buffer_unit` | 100 | 100 포대 ≈ 100 t | flake `Store` 용량(개수) |
| `scr_buffer_unit` | 75 | 75코일 ≈ 300 t | SCR `Store` 용량(개수) |
| `truck_capacity_ton` | 22.5 | 20~25 t 평균 | 상차 목표 단위 산출 |
| `weigh_in_min` / `weigh_out_min` | 각 5 | | 출하 계근 |
| `loading_min_per_ton` | 0.6 | | `× 적재 완료 t` 로 상차 시간 |
| `empty_truck_interval_min` | 90 | | 빈 트럭 간격 지수분포 평균(분) |
| `outbound_start_min` | 480 (8:00) | | 출하 프로세스 시작 전 대기 |

---

## 3. 모델이 강제하는 제약·가정

### 3.1 자원(직렬·병렬)

- 계근대: 입고·출하 트럭이 동일 `weighbridge`를 공유한다.
- 하역 베이, 선별, 압착, 엘리베이터, 반사로, 큐프레이크 라인, SCR 라인: SimPy `Resource` 용량만큼만 동시 점유.

### 3.2 버퍼

- 파레트 버퍼: 용량 초과 시 `put` 블로킹 → 압착이 멈출 수 있다.
- 큐프레이크·SCR 버퍼: 가득 차면 생산 라인에서 `put` 대기 · 주조가 막힌다.

### 3.3 배치·반사로

- 반사로 워커는 `pallets_per_batch`개가 모일 때까지 파레트 버퍼에서 대기한다.
- 엘리베이터는 `pallets_per_batch // elevator_pallets_per_trip` 회로 단순 반복한다.
- `setup` → `melt` → `holding_setup` → `casting_run` 은 하나의 `with furnaces` 구간으로 묶여 있다.

### 3.4 출하

- 상차 대기 최대 4시간; 이후 가용 재고만 실고 출발.
- flake: `int(truck_capacity_ton // 1.0)` 포대 목표, SCR: `int(truck_capacity_ton // 4.0)` 코일 목표.
- 차종(flake vs scr)은 `random() < flake_ratio` 로 결정되어 주조 비율과 동일 확률을 쓴다.

### 3.5 코드 단순화·불일치

- 선별 시간은 설정의 `sort_min_per_subpile` 계열과 달리 30분 고정.
- `pile_size_ton`, `forklifts`, `max_batch_ton` 은 현재 시뮬 레이어에서 미사용.

---

## 4. 웹 대시보드(`webapp.py`)에서 바꿀 수 있는 항목

실행 버튼을 누를 때 다음만 사용자 입력으로 바뀌고, 나머지 시간·단위값은 `DEFAULT_CONFIG`를 그대로 따른다.

| 구분 | 조정 가능 필드 |
|------|----------------|
| 기본 | `sim_days` (`random_seed`는 UI 미노출, 기본값 유지) |
| 입고 | `trucks_per_day`, `payload_ton`, `unloading_bays` |
| 선별·압착 | `sorters`, `press_machines`, `pallet_buffer_capacity` |
| 용해·주조 | `furnace_count`, `batch_ton`, `flake_ratio`/`scr_ratio` |
| 출하 | `empty_truck_interval_min` |

배치와 파레트 수: 웹에서는 `pallets_per_batch = int(batch_ton / pallet_ton)` 으로 파레트 중량 2.5 t 를 고정 분모로 둔다( `DEFAULT_CONFIG.sorting.pallet_ton` ).

---

## 5. CP-SAT 보조 모듈(`optimizer.py`)

SimPy 본 시뮬과의 역할 구분·호출 시점은 [simpy_cpsat_overview.md](simpy_cpsat_overview.md) 에 Mermaid 그림과 함께 정리했다.

- 입력: `BatchSpec(release_min, duration_min)`, 반사로 대 수, 시간 제한.
- 제약: 각 배치는 한 대의 반사로만 사용, 같은 반사로에서는 기간 비중첩 불가, `start ≥ release`.
- `estimate_batch_duration`: 엘리베이터 + 준비 + 용해 + 홀딩 셋업 + `max`(큐프레이크 주조 시간, SCR 주조 시간) (SimPy는 라인별 병렬이지만 최적화 쪽은 한 덩어리 길이로 근사).
- `estimate_batch_releases`: 리소스 대기·버퍼 full 없이 선별·압착만 단순 파이프로 근사 → 시뮬과 다를 수 있음.

---

## 6. 관련 파일

- `군산 공정 상세-김홍태보완.md` — 공정 서술·가정의 근본
- `config.py` — 모든 기본 숫자
- `simulation.py` — SimPy 동작 상세
- `main.py` — CLI 및 CP-SAT 연동
- `execution_flow_sequence.puml` — CLI·SimPy 부팅·웹앱 순서 PlantUML 순차 다이어그램
- `simpy_cpsat_overview.md` — SimPy vs CP-SAT 관계·활용 시점 (Mermaid)

문서 수정 시 코드 기본값과 어긋나지 않도록 `config.py` 와 교차 검증할 것.

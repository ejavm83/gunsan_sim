# 군산 공장 하이브리드 공정 시뮬레이션

`군산 공정 상세-김홍태보완.md` 문서에 정의된 **5단계 하이브리드 공정**(스크랩
구리 입고 → 선별/압착 → 장입/용해 → 하이브리드 주조 → 야적/출하)을
이산사건 시뮬레이션으로 구현한 프로젝트이다. 시뮬레이션 엔진으로
[SimPy](https://simpy.readthedocs.io/) 를, 두 반사로의 배치 스케줄링 최적화에
[OR-Tools CP-SAT](https://developers.google.com/optimization) 를 사용한다.

## 디렉터리 구조

```
gunsan_sim/
├── config.py           # 모든 시간/용량 파라미터(문서 기준값) 정의
├── metrics.py          # 이벤트 로깅 및 KPI 산출
├── simulation.py       # SimPy 5단계 공정 모델
├── optimizer.py        # CP-SAT 반사로 스케줄 최적화
├── visualize.py        # matplotlib 정적 차트(버퍼 점유 / Gantt / 누적 트럭)
├── animate.py          # 공장 레이아웃 시간 흐름 애니메이션(GIF/MP4)
├── report.py           # 자동 해석 + 인터랙티브 HTML 리포트(Plotly)
├── webapp.py           # Streamlit 웹 대시보드 (브라우저에서 실행)
├── main.py             # CLI 실행 진입점
├── run_gunsan_sim.bat  # Windows 실행 메뉴
├── run_webapp.bat      # 웹 대시보드 실행 배치
├── requirements.txt
└── README.md
```

## 설치

```bash
pip install -r requirements.txt
```

## 실행

```bash
python main.py                                # 기본 7일 + CP-SAT + 정적 차트
python main.py --days 14                      # 14일치 실행
python main.py --no-plot                      # matplotlib 정적 차트 생략
python main.py --no-optimize                  # CP-SAT 비교 생략
python main.py --events events.csv            # 전체 이벤트 로그 CSV 저장
python main.py --animate                      # 애니메이션 → out/factory.gif
python main.py --animate out/factory.gif --frame-min 30 --fps 15
python main.py --report                       # 인터랙티브 HTML 리포트 → out/report.html
```

실행하면 콘솔에 KPI 요약 표가 출력되고, `out/` 디렉터리에 다음 산출물이
저장된다.

| 파일                       | 내용                                            |
| -------------------------- | ----------------------------------------------- |
| `out/buffer_levels.png`    | 파레트 / 퓨플레이크 / SCR 코일 버퍼 점유 시계열 |
| `out/furnace_gantt.png`    | 두 반사로의 배치 타임라인(준비/용해/주조 색상) |
| `out/truck_cumulative.png` | 입고/출하 트럭 누적 대수 그래프                 |
| `out/factory.gif`          | 공장 레이아웃 시간 흐름 애니메이션(`--animate`) |
| `out/report.html`          | 자동 해석 + 인터랙티브 차트가 포함된 HTML 리포트 (`--report`) |

### HTML 리포트 (`--report`)

`report.py` 의 `generate_report()` 가 `Metrics` 와 `SimulationConfig` 를
입력으로 받아 다음 9개 섹션을 가진 단일 HTML 파일을 만든다. plotly.js 는
CDN 으로 주입되어 별도 의존성 없이 브라우저에서 바로 열 수 있다.

1. **핵심 지표 한눈에 보기** : 처리 트럭, 완료 배치, 생산량, 평균 체류시간
   등 KPI 를 카드 형태로 표시.
2. **공정 흐름과 병목 진단** : 5단계 공정을 가로 카드로 보여주고, 자동
   분석이 식별한 병목 단계는 빨간색으로 강조.
3. **자원 가동률** : 선별/압착/엘리베이터/반사로/계근대 가동률을 막대그래프로.
   90% 이상 빨강 / 70% 이상 노랑 / 그 외 초록으로 색상 코딩.
4. **버퍼/야적장 점유 시계열** : Plotly 인터랙티브 차트. hover 로 시점별
   값 확인, 평균/최대/95p 통계 테이블 동반.
5. **반사로 배치 Gantt** : 두 반사로의 배치별 장입/12h용해/주조 구간을
   가로 막대로.
6. **트럭 흐름** : 누적 입출고 곡선과 체류시간 분포 히스토그램.
7. **일별 생산량** : 워밍업 → 안정 가동 비교용 일별 스택 막대.
8. **자동 인사이트와 권장사항** : 규칙 기반 분석이 발견한 관찰 포인트와
   운영 액션을 한국어 카드로. (예: "압착기 가동률 93% 로 풀가동" → "압착기를
   1대 추가하면 처리량 2배" 같은 권장)
9. **시뮬레이션 설정 요약** : 사용된 파라미터 표 (`<details>` 토글).

리포트는 파라미터를 바꿔 다시 실행하면 자동으로 새 인사이트와 권장사항을
생성하므로, **운영자가 코드를 들여다보지 않고도 시나리오 비교**를 할 수 있다.

### 웹 대시보드 (`webapp.py`)

Streamlit 기반의 인터랙티브 웹 대시보드로, **브라우저에서 파라미터를 조정하고
시뮬레이션을 실행한 뒤 결과를 즉시 확인**할 수 있다.

```bash
# 방법 1: 배치 파일 더블클릭
run_webapp.bat

# 방법 2: 명령어 실행
streamlit run webapp.py
# 또는
py -3 -m streamlit run webapp.py
```

실행하면 브라우저가 자동으로 열리고 `http://localhost:8501` 에서 대시보드를
사용할 수 있다. 주요 기능:

- **사이드바 파라미터 패널**: 시뮬레이션 일수, 트럭/일, 압착기 대수, 반사로
  대수, 퓨플레이크 비율 등 주요 파라미터를 슬라이더로 조정
- **원클릭 시뮬레이션**: "🚀 시뮬레이션 실행" 버튼 클릭으로 즉시 실행
- **KPI 카드**: 처리량, 완료 배치, 평균 체류시간 등 핵심 지표
- **병목 진단**: 가동률 기반 자동 병목 식별 및 5단계 공정 카드 강조
- **인터랙티브 차트**: 버퍼 시계열, 반사로 Gantt, 누적 트럭, 일별 생산량
  (Plotly - hover/zoom 지원)
- **자동 인사이트**: 관찰 포인트 및 권장 액션 자동 생성

파라미터를 바꿔가며 시뮬레이션을 반복 실행하면서 **최적의 설비 구성을
실험**할 수 있다.

### 공장 레이아웃 애니메이션

`--animate` 옵션은 `animate.render_factory_animation()` 으로 다음과 같은
GIF 를 만든다.

- **공장 평면도(상단 큰 영역)** : 입고 → 계근 → 하역 → 선별/압착 → 파레트
  버퍼 → 엘리베이터 → 반사로 1·2 → 주조 라인(퓨플레이크/SCR) → 완제품
  야적장 → 출하 트럭 흐름을 박스로 표시한다.
  - 큐/버퍼 박스 색상은 점유율(`현재 / 최대`)에 따라 청록 → 노랑 → 빨강으로
    변한다.
  - 반사로 박스 색상은 상태별로 회색(idle) / 노랑(charging) / 빨강(melting,
    12h) / 초록(casting) 으로 표시되어 12시간 용해 병목과 24시간 사이클이
    한눈에 들어온다.
- **하단 시계열 미니 차트 3개** : 파레트 버퍼 / 퓨플레이크 야적 / SCR 야적의
  현재 점유 시계열이 모두 표시되며, 검은색 세로선이 현재 시점을 가리킨다.
- 한 프레임은 기본 60분(`--frame-min`)에 해당하며, 7일 시뮬은 약 169
  프레임 → 12 fps 로 약 14초짜리 GIF (≈4–5 MB) 가 만들어진다.
- 확장자를 `.mp4` 로 지정하면 ffmpeg 가 있을 때 MP4 로도 저장한다.

## 모델링 요약

### 자원/큐 그래프

```
[truck_inbound] -> Resource(weighbridge, 1)
                -> Resource(unloading_bay, 2) -> Store(sort_queue)
[sort_worker]   <- Store(sort_queue)
                -> Resource(sorters, 2) -> Store(press_queue)
[press_worker]  <- Store(press_queue)
                -> Resource(press, 1) -> Store(pallet_buffer, cap=160)
[furnace_worker x2] <- Store(pallet_buffer)  # 32 파레트 = 1배치
                    -> Resource(elevator, 1)
                    -> Resource(furnaces, 2)
                    -> Resource(flake_line, 1) | Resource(scr_line, 1)
                    -> Store(flake_buffer, 100), Store(scr_buffer, 75)
[outbound_truck]    <- Store(flake_buffer) or Store(scr_buffer)
                    -> Resource(weighbridge, 1)
```

### 핵심 시간 파라미터

| 단계        | 값                                                              |
| ----------- | --------------------------------------------------------------- |
| 트럭 도착   | 매일 10:00 시작, 1시간 간격 × 10대 (각 20 t)                   |
| 1·2차 계근  | 5분 / 5분                                                       |
| 하역        | 20분 (베이 2개)                                                 |
| 선별        | 트럭 1대 분 30분 → 8 sub-pile(2.5 t)                           |
| 압착 사이클 | 0.5 t × (지게차 5분 + 압착 1.5분 + 적재 2분) = 8.5분           |
| 엘리베이터  | 2 파레트 / 회 × 10분 = 32 파레트 운반에 160분                  |
| 사전 준비   | 2시간                                                           |
| **용해**    | **12시간 (전체 병목)**                                          |
| 주조 셋업   | 1.5시간                                                         |
| 퓨플레이크  | 1 t / 2.5분 (전체 80 t 의 30 %)                                |
| SCR 코일    | 4 t / 10분 (전체 80 t 의 70 %)                                 |
| 출하 트럭   | 평균 90분 간격, 22.5 t 적재, 30:70 비율로 flake/scr 트럭 도착 |

### CP-SAT 최적화

`optimizer.solve_furnace_schedule` 는 다음 작업장 스케줄링 문제를 푼다.

- 변수: `start[i]`, `assigned[i, f]` (반사로 배정 boolean)
- 제약: `start[i] >= release[i]`, 같은 반사로 내 NoOverlap
- 목적: `min max_i (start[i] + duration[i])`

`main.py` 는 시뮬레이션의 실측 메이크스팬과 CP-SAT 의 이론 최적
메이크스팬을 함께 출력하여, 선착순(FIFO) 정책이 최적에서 얼마나 벗어나는지
비교할 수 있다.

## 결과 활용

- `metrics.events` 에 모든 사건이 시간순으로 저장되므로 별도 분석/대시보드
  연계가 쉽다.
- `main.py --events out/events.csv` 로 CSV 추출 후 Pandas/엑셀로
  드릴다운 가능하다.
- 파라미터 튜닝은 `config.py` 의 데이터클래스 값을 수정하거나 main 에서
  `dataclasses.replace` 로 일부만 바꿔 비교 실험할 수 있다.

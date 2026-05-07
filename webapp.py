"""군산 공장 하이브리드 공정 시뮬레이션 웹 대시보드.

Streamlit 기반 인터랙티브 대시보드로, 브라우저에서 파라미터를 조정하고
시뮬레이션을 실행한 뒤 결과를 즉시 확인할 수 있다.

실행 방법::

    streamlit run webapp.py
    # 또는
    py -3 -m streamlit run webapp.py
"""

from __future__ import annotations

import dataclasses
import time

import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from config import (
    DEFAULT_CONFIG,
    InboundConfig,
    SortingConfig,
    MeltingConfig,
    CastingConfig,
    OutboundConfig,
    SimulationConfig,
)
from simulation import run_simulation
from report import analyze, Analysis

_BUILD_INFO_TEXT = "(주) 지엠티 김길용 수석, v0.0.1 (2026.05.06)"


def _build_process_graphviz(
    trucks_per_day: int,
    payload_ton: float,
    flake_ratio: int,
    furnace_count: int,
    bottleneck: str | None = None,
) -> str:
    """가독성 중심 상세 공정 Graphviz."""
    total_inbound_ton = trucks_per_day * payload_ton
    flake_ton = total_inbound_ton * (flake_ratio / 100.0)
    scr_ton = max(total_inbound_ton - flake_ton, 0.0)
    bottleneck_text = bottleneck or ""
    is_press = "압착" in bottleneck_text
    is_furnace = "반사로" in bottleneck_text
    is_outbound = "출하" in bottleneck_text

    def style(is_bottle: bool, fill: str) -> str:
        border = "#dc2626" if is_bottle else "#5b6b7a"
        bg = "#fee2e2" if is_bottle else fill
        return f'shape=box style="rounded,filled" color="{border}" fillcolor="{bg}" penwidth=2'

    return f"""
digraph G {{
  rankdir=LR;
  graph [pad=0.2, nodesep=0.45, ranksep=0.55, bgcolor="white"];
  node [fontname="Malgun Gothic", fontsize=11, shape=box, style="rounded,filled", color="#5b6b7a", fillcolor="#eef6ff", penwidth=1.5];
  edge [color="#6b7280", penwidth=1.4, arrowsize=0.7];

  inbound   [label="트럭 입고\\n{trucks_per_day}대/일 · {total_inbound_ton:.0f}t/일"];
  weigh     [label="1차/2차 계근\\n각 5분"];
  unload    [label="하역\\n20분 · 베이 운영"];
  sorting   [label="선별\\n30분 · 8개 sub-pile"];
  press     [{style(is_press, "#eaf5ff")} label="압착/파레트\\n0.5t 사이클 8.5분\\n파레트 버퍼"];
  elevator  [label="엘리베이터\\n2파레트/10분"];
  furnace   [{style(is_furnace, "#fff4e8")} label="장입/용해\\n반사로 {furnace_count}대 · 12h"];
  casting   [label="하이브리드 주조\\nFlake {flake_ratio}% · SCR {100 - flake_ratio}%"];

  flake_yard [label="Flake 야적\\n{flake_ton:.0f}t", fillcolor="#e8f6ff"];
  scr_yard   [label="SCR 야적\\n{scr_ton:.0f}t", fillcolor="#ffeded"];
  flake_out  [{style(is_outbound, "#f4efff")} label="Flake 상차/출하\\n상차 → 계근 → 출고"];
  scr_out    [{style(is_outbound, "#f4efff")} label="SCR 상차/출하\\n상차 → 계근 → 출고"];

  inbound -> weigh -> unload -> sorting -> press -> elevator -> furnace -> casting;
  casting -> flake_yard -> flake_out;
  casting -> scr_yard -> scr_out;
}}
"""


def _build_detailed_process_figure(
    trucks_per_day: int,
    payload_ton: float,
    flake_ratio: int,
    furnace_count: int,
    bottleneck: str | None = None,
) -> go.Figure:
    """세부 공정(입고~출하) 라인 다이어그램."""
    total_inbound_ton = trucks_per_day * payload_ton
    flake_ton = total_inbound_ton * (flake_ratio / 100.0)
    scr_ton = max(total_inbound_ton - flake_ton, 0.0)
    bottleneck_text = bottleneck or ""

    nodes = [
        (0.07, 0.74, "트럭 입고", f"{trucks_per_day}대/일\n{total_inbound_ton:.0f}t/일", "#2563eb", False),
        (0.18, 0.74, "계근/하역", "계근 5분\n하역 20분", "#3b82f6", False),
        (0.29, 0.74, "선별", "30분\n8개 sub-pile", "#60a5fa", False),
        (0.40, 0.74, "압착/파레트", "0.5t 사이클 8.5분\n파레트 버퍼", "#0284c7", "압착" in bottleneck_text),
        (0.51, 0.74, "엘리베이터", "2파레트/10분", "#0ea5e9", False),
        (0.62, 0.74, "장입/용해", f"반사로 {furnace_count}대\n12h", "#f59e0b", "반사로" in bottleneck_text),
        (0.73, 0.74, "하이브리드 주조", f"Flake {flake_ratio}%\nSCR {100-flake_ratio}%", "#22c55e", False),
        (0.84, 0.58, "Flake 야적", f"{flake_ton:.0f}t", "#0ea5e9", False),
        (0.84, 0.36, "SCR 야적", f"{scr_ton:.0f}t", "#dc2626", False),
        (0.94, 0.58, "Flake 출하", "상차→계근→출고", "#6366f1", "출하" in bottleneck_text),
        (0.94, 0.36, "SCR 출하", "상차→계근→출고", "#7c3aed", "출하" in bottleneck_text),
    ]

    fig = go.Figure()
    shapes = []
    annotations = []

    for x, y, title, desc, color, is_bottle in nodes:
        fill = "#fee2e2" if is_bottle else "#eff6ff"
        border = "#dc2626" if is_bottle else color
        shapes.append(
            dict(
                type="rect",
                xref="paper",
                yref="paper",
                x0=x - 0.045,
                x1=x + 0.045,
                y0=y - 0.08,
                y1=y + 0.08,
                line=dict(color=border, width=2.5 if is_bottle else 1.6),
                fillcolor=fill,
            )
        )
        annotations.append(
            dict(
                x=x,
                y=y,
                xref="paper",
                yref="paper",
                text=f"<b>{title}</b><br><span style='font-size:10px'>{desc}</span>",
                showarrow=False,
                align="center",
                font=dict(size=11, color="#0f172a"),
            )
        )

    arrow_specs = [
        (0.135, 0.74, -70, 0), (0.245, 0.74, -70, 0), (0.355, 0.74, -70, 0),
        (0.465, 0.74, -70, 0), (0.575, 0.74, -70, 0), (0.685, 0.74, -70, 0),
        (0.79, 0.62, -55, 62), (0.79, 0.48, -55, -62),
        (0.895, 0.58, -55, 0), (0.895, 0.36, -55, 0),
    ]
    for x, y, ax, ay in arrow_specs:
        annotations.append(
            dict(
                x=x, y=y, xref="paper", yref="paper",
                ax=ax, ay=ay, axref="pixel", ayref="pixel",
                text="", showarrow=True, arrowhead=3, arrowsize=1.0, arrowwidth=1.8, arrowcolor="#64748b",
            )
        )

    annotations.append(
        dict(
            x=0.5, y=0.10, xref="paper", yref="paper", showarrow=False,
            text="상세 흐름: 입고 → 계근/하역 → 선별 → 압착/버퍼 → 엘리베이터 → 용해(12h) → 주조 → 야적 → 상차/출하",
            font=dict(size=12, color="#334155"),
        )
    )

    fig.update_layout(
        height=420,
        margin=dict(l=8, r=8, t=8, b=8),
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        paper_bgcolor="white",
        plot_bgcolor="white",
        shapes=shapes,
        annotations=annotations,
    )
    return fig


def _build_process_timeline_figure(
    batch_ton: float,
    flake_ratio: int,
) -> go.Figure:
    """멋진 대시보드형 타임라인: 누적 네온 라인 + 도넛 + 히트맵."""

    def scenario_steps(ratio: int) -> tuple[list[str], list[float], float, float]:
        flake_ton = batch_ton * (ratio / 100.0)
        scr_ton = max(batch_ton - flake_ton, 0.0)
        press_for_batch = (batch_ton / 0.5) * 8.5
        elevator = (batch_ton / 80.0) * 160.0
        flake_cast = (flake_ton / 1.0) * 2.5 if flake_ton > 0 else 0.0
        scr_cast = (scr_ton / 4.0) * 10.0 if scr_ton > 0 else 0.0
        branch_elapsed = max(flake_cast, scr_cast)
        labels = ["1차 계근", "하역", "2차 계근", "선별", "압착/파레트", "엘리베이터", "장입 준비", "용해(12h)", "주조 셋업", "분기 주조 완료"]
        durations = [5, 20, 5, 30, press_for_batch, elevator, 120, 720, 90, branch_elapsed]
        return labels, durations, flake_ton, scr_ton

    low_ratio = max(10, flake_ratio - 20)
    high_ratio = min(90, flake_ratio + 20)
    scenarios = [("저 Flake", low_ratio), ("기준", flake_ratio), ("고 Flake", high_ratio)]

    base_labels, base_durations_min, base_flake_ton, base_scr_ton = scenario_steps(flake_ratio)
    base_durations_h = [v / 60.0 for v in base_durations_min]
    base_total_h = sum(base_durations_h)
    cumulative = []
    cur = 0.0
    for d in base_durations_h:
        cur += d
        cumulative.append(cur)

    fig = make_subplots(
        rows=2,
        cols=2,
        specs=[[{"type": "xy", "colspan": 2}, None], [{"type": "domain"}, {"type": "xy"}]],
        vertical_spacing=0.25,
        horizontal_spacing=0.12,
        subplot_titles=("기준 시나리오: 공정 누적시간 네온 타임라인", "공정 시간 비중", "경우의 수별 누적시간 히트맵"),
    )

    fig.add_trace(
        go.Bar(
            x=base_labels,
            y=base_durations_h,
            marker_color="rgba(56,189,248,0.35)",
            text=[f"{v:.2f}h" for v in base_durations_h],
            textposition="inside",
            hovertemplate="%{x}<br>소요: %{y:.2f}시간<extra></extra>",
            showlegend=False,
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=base_labels,
            y=cumulative,
            mode="lines+markers+text",
            line=dict(color="#2563eb", width=4),
            marker=dict(size=10, color="#0ea5e9", line=dict(color="white", width=1)),
            text=[f"{v:.1f}h" for v in cumulative],
            textposition="top center",
            hovertemplate="%{x}<br>누적: %{y:.2f}시간<extra></extra>",
            showlegend=False,
        ),
        row=1,
        col=1,
    )

    fig.add_trace(
        go.Pie(
            labels=base_labels,
            values=base_durations_h,
            hole=0.58,
            sort=False,
            textinfo="percent",
            hovertemplate="%{label}<br>%{value:.2f}시간 (%{percent})<extra></extra>",
            marker=dict(line=dict(color="white", width=1)),
            showlegend=False,
        ),
        row=2,
        col=1,
    )

    heat_y = []
    heat_z = []
    for s_name, ratio in scenarios:
        labels, durations_min, _, _ = scenario_steps(ratio)
        cum_vals = []
        c = 0.0
        for d in durations_min:
            c += d / 60.0
            cum_vals.append(c)
        heat_y.append(f"{s_name} ({ratio}%)")
        heat_z.append(cum_vals)

    fig.add_trace(
        go.Heatmap(
            x=base_labels,
            y=heat_y,
            z=heat_z,
            colorscale="YlGnBu",
            colorbar=dict(title="누적시간(h)", len=0.8),
            hovertemplate="시나리오: %{y}<br>단계: %{x}<br>누적: %{z:.2f}시간<extra></extra>",
        ),
        row=2,
        col=2,
    )

    fig.update_layout(
        height=760,
        margin=dict(l=40, r=25, t=80, b=45),
        paper_bgcolor="white",
        plot_bgcolor="white",
        xaxis=dict(tickangle=-25),
        yaxis=dict(title="시간 (시간)", gridcolor="rgba(148,163,184,0.25)"),
        xaxis3=dict(tickangle=-25),
        yaxis3=dict(title="시나리오"),
    )
    fig.add_annotation(
        x=0.24,
        y=0.19,
        xref="paper",
        yref="paper",
        showarrow=False,
        text=f"<b>총 누적시간</b><br>{base_total_h:.2f}h",
        font=dict(size=14, color="#1e3a8a"),
    )
    fig.add_annotation(
        x=0.5,
        y=-0.08,
        xref="paper",
        yref="paper",
        showarrow=False,
        text=(
            f"기준 배치 {batch_ton:.0f}t | 기준 분기량 Flake {base_flake_ton:.1f}t, SCR {base_scr_ton:.1f}t "
            f"(분기 주조는 병렬 진행 기준으로 긴 쪽 시간 적용)"
        ),
        font=dict(size=12, color="#334155"),
    )
    return fig

# ---------------------------------------------------------------------------
# 페이지 설정
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="SCR공정 물류 시뮬레이션",
    page_icon="🏭",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# 접근 제어
# ---------------------------------------------------------------------------
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    st.title("🔒 접근 제한")
    st.write("비밀번호를 입력해야 대시보드에 접속할 수 있습니다.")
    password_input = st.text_input("비밀번호", type="password", placeholder="비밀번호 입력")
    if st.button("접속", type="primary"):
        if password_input == "6501":
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("비밀번호가 올바르지 않습니다.")
    st.stop()

# ---------------------------------------------------------------------------
# 사이드바 - 파라미터 입력
# ---------------------------------------------------------------------------

st.sidebar.title("🏭 시뮬레이션 파라미터")
st.sidebar.caption("ℹ️ 각 항목 위에 마우스를 올리면 상세 설명이 표시됩니다.")
run_button = st.sidebar.button(
    "🚀 시뮬레이션 실행", type="primary", use_container_width=True,
    help="""▶️ **시뮬레이션 실행**

설정한 파라미터로 이산사건 시뮬레이션을 실행합니다.

**실행 내용:**
1. SimPy 기반 DES 시뮬레이션 실행
2. 공정별 이벤트 로그 생성
3. KPI 및 병목 분석
4. 시각화 결과 생성

**예상 소요 시간:**
- 7일 시뮬레이션: 약 1~2초
- 30일 시뮬레이션: 약 3~5초

**결과 확인:**
- 핵심 지표 (KPI 카드)
- 병목 진단 및 권장사항
- 자원 가동률 차트
- 버퍼 시계열 그래프
- 반사로 Gantt 차트
- 트럭 흐름 분석"""
)
st.sidebar.caption("필요할 때 언제든 위 버튼으로 즉시 실행하세요.")

st.sidebar.header("1. 기본 설정")
st.sidebar.caption("시뮬레이션 실행 기간 설정")
sim_days = st.sidebar.slider(
    "시뮬레이션 일수", 1, 30, DEFAULT_CONFIG.sim_days,
    help="""📅 **시뮬레이션 기간 설정**

시뮬레이션을 실행할 총 일수를 지정합니다.

**권장 설정:**
- 단기 분석: 1~3일 (빠른 테스트)
- 중기 분석: 7~14일 (정상 상태 도달)
- 장기 분석: 14~30일 (통계적 신뢰성 확보)

**주의사항:**
- 일수가 길수록 시뮬레이션 실행 시간 증가
- 반사로 12시간 용해 주기 고려 시 최소 3일 권장
- 초기 warm-up 기간(1~2일) 후 정상 상태 도달"""
)
random_seed = DEFAULT_CONFIG.random_seed

st.sidebar.header("2. 입고/하역")
st.sidebar.caption("스크랩 원료 트럭 입고 및 하역 설정")
trucks_per_day = st.sidebar.slider(
    "일일 트럭 대수", 1, 20, DEFAULT_CONFIG.inbound.trucks_per_day,
    help="""🚛 **일일 스크랩 입고 트럭 대수**

하루 동안 공장에 도착하는 스크랩 원료 운반 트럭의 평균 대수입니다.

**입고 프로세스:**
1. 트럭 도착 → 1차 계근 (총중량 측정)
2. 하역장 이동 → 스크랩 하역 (20~30분)
3. 2차 계근 (공차 중량) → 출차

**일일 입고량 계산:**
- 총 입고량 = 트럭 대수 × 트럭당 적재량
- 예: 10대 × 20t = 200t/일

**파라미터 영향:**
- 증가 시: 일일 원료 공급량 증가, 하역장/선별 부하 증가
- 감소 시: 원료 부족으로 반사로 가동률 저하 가능

**실제 공장 고려사항:**
- 도착 시간은 균등 분포로 분산 가정
- 피크 시간대 하역장 혼잡 가능성"""
)
payload_ton = st.sidebar.slider(
    "트럭당 적재량 (t)", 10.0, 30.0, DEFAULT_CONFIG.inbound.payload_ton, 1.0,
    help="""⚖️ **트럭 1대당 스크랩 적재량 (톤)**

각 트럭이 운반하는 스크랩 원료의 평균 중량입니다.

**적재량 구성:**
- 총중량(1차 계근) - 공차중량(2차 계근) = 순 적재량
- 일반적인 범위: 15~25톤/대

**후속 공정 영향:**
- 트럭 1대 하역 → 1개 더미 생성
- 1개 더미 → 8개 sub-pile로 선별
- sub-pile → 파레트로 압착

**일일 처리량 계산:**
- 일일 입고량 = 트럭 대수 × 적재량
- 예: 10대 × 20t = 200t/일

**병목 관계:**
- 반사로 1배치 = 30~80톤 (설정에 따라)
- 입고량 > 처리량 시 야적장 재고 증가
- 입고량 < 처리량 시 반사로 유휴 발생"""
)
unloading_bays = st.sidebar.slider(
    "하역 베이 수", 1, 4, DEFAULT_CONFIG.inbound.unloading_bays,
    help="""🏗️ **하역장 동시 작업 가능 대수**

하역장에서 동시에 하역 작업을 수행할 수 있는 트럭 수입니다.

**하역 작업 상세:**
- 소요 시간: 약 20~30분/대
- 작업 내용: 덤프 트럭으로 스크랩 투하
- 결과물: 선별 대기 더미 1개/트럭

**용량 영향:**
- 1 베이: 순차 처리, 트럭 대기 시간 발생
- 2 베이: 병렬 처리, 처리량 2배
- 3+ 베이: 트럭 도착률 고려 필요

**병목 진단:**
- 하역 베이 가동률 > 90%: 병목 가능성
- 트럭 평균 대기시간 증가 시 베이 추가 검토

**설비 비용 고려:**
- 베이 추가는 초기 투자비 증가
- ROI 계산: 트럭 대기비용 vs 설비 투자비"""
)

st.sidebar.header("3. 선별/압착")
st.sidebar.caption("스크랩 등급 선별 및 파레트 압착 설정")
sorters = st.sidebar.slider(
    "선별 작업조 수", 1, 4, DEFAULT_CONFIG.sorting.sorters,
    help="""👷 **선별 작업조(팀) 수**

스크랩 원료를 등급별로 분류하고 이물질을 제거하는 작업조 수입니다.

**선별 작업 내용:**
- 스크랩 품질/등급 분류
- 비금속 이물질 제거 (플라스틱, 고무 등)
- 크기별 분류
- 소요 시간: 약 30분/더미

**작업 흐름:**
- 트럭 1대 하역 → 1개 더미
- 1개 더미 → 선별 후 8개 sub-pile
- sub-pile → 압착기로 이동

**용량 영향:**
- 작업조 증가 → 선별 처리량 증가
- 병렬 작업 가능 (각 조가 독립적으로 작업)

**인력 비용 고려:**
- 작업조당 2~3명 인력 필요
- 교대 근무 시 조 수 × 교대 수 인력 필요
- 선별 품질과 속도의 트레이드오프"""
)
press_machines = st.sidebar.slider(
    "압착기 대수", 1, 4, DEFAULT_CONFIG.sorting.press_machines,
    help="""🔧 **압착기 설비 대수**

선별된 sub-pile을 파레트로 압착하는 압착기의 대수입니다.

**압착 공정 상세:**
- 입력: 선별된 sub-pile (약 0.5톤)
- 출력: 압착 파레트 (규격화된 블록)
- 사이클 타임: 약 8.5분/파레트

**처리량 계산:**
- 1대 기준: 약 7 파레트/시간
- 일 처리량: 7 × 24 = 168 파레트/일 (연속 가동 시)

**병목 영향:**
- 압착기가 병목인 경우:
  - sub-pile 대기 큐 증가
  - 파레트 생성 지연
  - 반사로 장입 대기 발생

**투자 판단 기준:**
- 압착기 가동률 > 85%: 추가 투자 검토
- sub-pile 대기 큐 지속 증가: 병목 신호
- 파레트 버퍼 자주 비어있음: 압착기 부족"""
)
pallet_buffer_capacity = st.sidebar.slider(
    "파레트 버퍼 용량", 50, 300, DEFAULT_CONFIG.sorting.pallet_buffer_capacity, 10,
    help="""📦 **파레트 버퍼 최대 적재량**

압착 완료된 파레트가 반사로 장입 전까지 대기하는 버퍼의 용량입니다.

**버퍼 역할:**
- 압착 공정과 용해 공정 간 디커플링
- 공정 간 속도 차이 흡수
- 반사로 장입 대기열 역할

**용량 산정 기준:**
- 반사로 1배치 = 약 60 파레트 (30톤 기준)
- 최소 권장: 1배치 분량 (60~70개)
- 권장: 2배치 분량 (120~140개)

**용량 부족 시:**
- 버퍼 full → 압착기 정지
- 압착기 정지 → sub-pile 대기 증가
- 연쇄적 공정 지연 발생

**용량 과다 시:**
- 불필요한 공간 점유
- 재고 관리 비용 증가
- 파레트 품질 저하 가능 (장기 보관 시)

**모니터링 포인트:**
- 평균 점유율: 50~70% 적정
- 최대 점유율: 90% 이하 유지 권장"""
)

st.sidebar.header("4. 용해/주조")
st.sidebar.caption("반사로 용해 및 제품 주조 설정 (핵심 병목)")
furnace_count = st.sidebar.slider(
    "반사로 대수", 1, 3, DEFAULT_CONFIG.melting.furnace_count,
    help="""🔥 **반사로(Reverberatory Furnace) 대수**

스크랩을 고온으로 용해하여 용탕(molten copper)을 만드는 반사로의 대수입니다.

**반사로 운영 사이클:**
1. **장입 (Charging)**: 파레트 투입 (2~3시간)
2. **용해 (Melting)**: 고온 용해 (**12시간** - 병목!)
3. **주조 (Casting)**: 용탕을 제품으로 주조 (8시간)
4. **준비**: 다음 배치 준비

**총 사이클 타임: 약 22~24시간/배치**

**처리량 계산:**
- 1대: ~30톤/일 (24시간 사이클)
- 2대: ~60톤/일 (교대 운용)
- 3대: ~90톤/일 (연속 생산)

**병목 분석:**
- 12시간 용해는 공정의 **핵심 병목**
- 반사로 추가가 처리량 증대의 가장 직접적 방법
- 단, 대규모 설비 투자 필요

**에너지 비용:**
- 가스 버너 연료비 (주요 운영 비용)
- 용해 온도: 1,100~1,200°C
- 전력비: 버너, 제어 시스템 등"""
)
batch_ton = st.sidebar.slider(
    "배치 단위 (t)", 40.0, 200.0, DEFAULT_CONFIG.melting.batch_ton, 10.0,
    help="""⚗️ **반사로 1회 배치 용량 (톤)**

반사로에 한 번에 장입하여 용해하는 스크랩의 총 중량입니다.

**배치 구성:**
- 배치 톤수 ÷ 파레트당 중량 = 필요 파레트 수
- 예: 30톤 ÷ 0.5톤 = 60 파레트/배치

**배치 크기 영향:**

**작은 배치 (40~80톤):**
- 장점: 빠른 사이클, 유연한 운영
- 단점: 배치당 고정 시간 비중 증가

**큰 배치 (100~200톤):**
- 장점: 배치당 효율 증가
- 단점: 긴 사이클, 파레트 대기 시간 증가

**최적화 고려사항:**
- 파레트 버퍼 용량과의 균형
- 일일 입고량 대비 배치 횟수
- 주조 라인 용량과의 매칭

**실제 운영 팁:**
- 일 1~2배치가 일반적
- 야간 용해 → 주간 주조 패턴 고려"""
)
flake_ratio = st.sidebar.slider(
    "퓨플레이크 비율 (%)", 0, 100, int(DEFAULT_CONFIG.casting.flake_ratio * 100),
    help="""🥧 **주조 제품 비율: 퓨플레이크 vs SCR**

용탕을 주조할 때 퓨플레이크와 SCR 코일의 생산 비율입니다.

**제품 유형:**

**퓨플레이크 (Cu Flake):**
- 형태: 얇은 구리 플레이크/칩
- 단위: 25kg 포대
- 용도: 전해 정련, 합금 제조
- 주조 속도: 약 1톤/2.5분

**SCR 코일 (South Wire Rod):**
- 형태: 연속 주조 구리봉 (코일)
- 단위: 4톤/코일
- 용도: 전선, 케이블 제조
- 주조 속도: 약 4톤/10분

**비율 설정 가이드:**
- 50/50: 균형 생산 (기본값)
- 70/30: 퓨플레이크 수요 높을 때
- 30/70: SCR 수요 높을 때

**주의사항:**
- 두 라인 병렬 운영 (동시 주조)
- 야적장 용량 고려 필요
  - 퓨플레이크: 100포대 버퍼
  - SCR: 75코일 버퍼
- 출하 트럭 수요와 매칭 필요"""
)

st.sidebar.header("5. 출하")
st.sidebar.caption("완제품 야적 및 출하 트럭 설정")
empty_truck_interval = st.sidebar.slider(
    "출하 트럭 평균 간격 (분)", 30, 180, int(DEFAULT_CONFIG.outbound.empty_truck_interval_min),
    help="""🚚 **출하 빈 트럭 도착 평균 간격 (분)**

완제품(퓨플레이크/SCR)을 실어갈 빈 트럭이 도착하는 평균 시간 간격입니다.

**출하 프로세스:**
1. 빈 트럭 도착 → 대기열 진입
2. 야적장에서 제품 상차 (30~60분)
3. 2차 계근 (적재 중량 확인)
4. 출고

**도착 간격 모델:**
- **지수분포** 기반 확률적 도착
- 평균 간격 = 설정값
- 실제 간격은 확률적 변동 있음

**간격 설정 가이드:**

**짧은 간격 (30~60분):**
- 활발한 출하 수요
- 야적장 재고 빠르게 소진
- 트럭 대기 시간 발생 가능

**긴 간격 (120~180분):**
- 완만한 출하 수요
- 야적장 재고 누적 가능
- 야적장 용량 초과 주의

**균형 포인트:**
- 생산량 ≈ 출하량 유지
- 야적장 점유율 50~70% 목표
- 트럭 평균 대기시간 최소화"""
)

# ---------------------------------------------------------------------------
# 메인 - 결과 대시보드
# ---------------------------------------------------------------------------

st.title("🏭 SCR공정 물류 시뮬레이션")
st.markdown(
    f"""
    <div style="text-align:right; color:#6b7280; font-size:12px; margin-top:-8px;">
    {_BUILD_INFO_TEXT}
    </div>
    """,
    unsafe_allow_html=True,
)
st.markdown("""
스크랩 구리 입고 → 선별/압착 → 장입/용해(12h) → 하이브리드 주조 → 완제품 출하의
5단계 공정을 SimPy 이산사건 시뮬레이션으로 모델링합니다.
**왼쪽 사이드바에서 파라미터를 조정**한 뒤 **시뮬레이션 실행** 버튼을 누르세요.
""")
st.subheader("🧭 세부공정 프로세스")
st.graphviz_chart(
    _build_process_graphviz(
        trucks_per_day=trucks_per_day,
        payload_ton=payload_ton,
        flake_ratio=flake_ratio,
        furnace_count=furnace_count,
        bottleneck=None,
    ),
    use_container_width=True,
)
if run_button:
    # 설정 조립
    inbound = dataclasses.replace(
        DEFAULT_CONFIG.inbound,
        trucks_per_day=trucks_per_day,
        payload_ton=payload_ton,
        unloading_bays=unloading_bays,
    )
    sorting = dataclasses.replace(
        DEFAULT_CONFIG.sorting,
        sorters=sorters,
        press_machines=press_machines,
        pallet_buffer_capacity=pallet_buffer_capacity,
    )
    melting = dataclasses.replace(
        DEFAULT_CONFIG.melting,
        furnace_count=furnace_count,
        batch_ton=batch_ton,
        pallets_per_batch=int(batch_ton / DEFAULT_CONFIG.sorting.pallet_ton),
    )
    casting = dataclasses.replace(
        DEFAULT_CONFIG.casting,
        flake_ratio=flake_ratio / 100.0,
        scr_ratio=1.0 - flake_ratio / 100.0,
    )
    outbound = dataclasses.replace(
        DEFAULT_CONFIG.outbound,
        empty_truck_interval_min=float(empty_truck_interval),
    )
    cfg = SimulationConfig(
        sim_days=sim_days,
        random_seed=int(random_seed),
        inbound=inbound,
        sorting=sorting,
        melting=melting,
        casting=casting,
        outbound=outbound,
    )

    # 실행
    with st.spinner(f"🔄 {sim_days}일치 시뮬레이션 실행 중..."):
        t0 = time.perf_counter()
        metrics = run_simulation(cfg)
        elapsed = time.perf_counter() - t0

    st.success(f"✅ 시뮬레이션 완료 ({elapsed:.2f}초 소요)")

    # 분석
    analysis = analyze(metrics, cfg)
    summary = analysis.summary

    # ===== KPI 카드 =====
    st.header("📊 핵심 지표")
    cols = st.columns(5)
    with cols[0]:
        st.metric(
            "처리 트럭 (입고)", f"{summary['trucks_in_processed']} 대",
            help="시뮬레이션 기간 동안 입고 후 출차 완료된 트럭의 총 대수입니다. 1차 계근 → 하역 → 2차 계근 → 출차 과정을 완료한 트럭만 집계됩니다."
        )
    with cols[1]:
        st.metric(
            "출하 트럭", f"{summary['trucks_out_dispatched']} 대",
            help="완제품을 적재하고 출고된 출하 트럭의 총 대수입니다. 빈 트럭 도착 → 상차 → 계근 → 출고 과정을 완료한 트럭만 집계됩니다."
        )
    with cols[2]:
        st.metric(
            "완료 배치", f"{summary['melt_batches_completed']} 회",
            help="반사로에서 완료된 용해 배치의 총 횟수입니다. 1배치 = 장입 → 12시간 용해 → 주조 완료. 생산 능력의 핵심 지표입니다."
        )
    with cols[3]:
        st.metric(
            "총 생산량", f"{summary['total_product_ton']:.0f} t",
            help="퓨플레이크와 SCR 코일을 합한 총 생산량(톤)입니다. 출하된 제품 + 야적장 재고를 포함합니다."
        )
    with cols[4]:
        st.metric(
            "일평균 처리량", f"{summary['throughput_ton_per_day']:.1f} t/일",
            help="일평균 생산량 = 총 생산량 ÷ 시뮬레이션 일수. 공장의 실질적인 생산 능력을 나타내는 핵심 KPI입니다."
        )

    cols2 = st.columns(5)
    with cols2[0]:
        st.metric(
            "퓨플레이크", f"{summary['flake_ton']:.0f} t",
            help="생산된 Cu 플레이크의 총 중량(톤)입니다. 25kg 포대 단위로 생산되며, 전해 정련 및 합금 제조에 사용됩니다."
        )
    with cols2[1]:
        st.metric(
            "SCR 코일", f"{summary['scr_ton']:.0f} t",
            help="생산된 SCR(South Wire Rod) 코일의 총 중량(톤)입니다. 4톤/코일 단위로 생산되며, 전선/케이블 제조에 사용됩니다."
        )
    with cols2[2]:
        st.metric(
            "입고 평균체류", f"{summary['avg_truck_in_lead_min']:.1f} 분",
            help="입고 트럭의 평균 체류시간(도착~출차). 대기시간이 길면 하역장/계근대 병목을 의심해야 합니다. 목표: 60분 이내."
        )
    with cols2[3]:
        st.metric(
            "출하 평균체류", f"{summary['avg_truck_out_lead_min']:.1f} 분",
            help="출하 트럭의 평균 체류시간(도착~출고). 상차 대기, 제품 부족 등으로 지연될 수 있습니다. 목표: 90분 이내."
        )
    with cols2[4]:
        st.metric(
            "배치 평균시간", f"{summary['avg_melt_batch_min']:.0f} 분",
            help="반사로 1배치 완료에 걸리는 평균 시간(분). 장입(2~3h) + 용해(12h) + 주조(8h) ≈ 22~24시간이 정상입니다."
        )

    # ===== 병목 진단 =====
    st.header("🔍 병목 진단")
    with st.expander("ℹ️ 병목(Bottleneck)이란?", expanded=False):
        st.markdown("""
        **병목**은 전체 공정의 처리량을 제한하는 가장 느린 공정 단계입니다.
        
        - 병목 자원의 가동률이 가장 높음 (90% 이상)
        - 병목 앞단에 대기열/재고가 누적됨
        - 병목 개선이 전체 처리량 향상에 직결됨
        
        **일반적인 병목 순서:** 반사로 용해 > 압착기 > 하역장 > 선별
        """)
    st.error(f"**식별된 병목: {analysis.bottleneck}** — {analysis.bottleneck_reason}")
    st.graphviz_chart(
        _build_process_graphviz(
            trucks_per_day=trucks_per_day,
            payload_ton=payload_ton,
            flake_ratio=flake_ratio,
            furnace_count=furnace_count,
            bottleneck=analysis.bottleneck,
        ),
        use_container_width=True,
    )
    # 공정 흐름 카드
    stages = [
        ("1. 입고/하역", f"트럭 {trucks_per_day}대/일 × {payload_ton}t"),
        ("2. 선별/압착", f"작업조 {sorters}, 압착기 {press_machines}대"),
        ("3. 장입/용해", f"반사로 {furnace_count}대, {batch_ton}t/배치"),
        ("4. 하이브리드 주조", f"flake {flake_ratio}% / SCR {100-flake_ratio}%"),
        ("5. 출하/야적", f"평균 {empty_truck_interval}분 간격"),
    ]
    flow_cols = st.columns(5)
    for i, (name, desc) in enumerate(stages):
        with flow_cols[i]:
            is_bottleneck = "압착" in analysis.bottleneck and "압착" in name
            is_bottleneck = is_bottleneck or ("반사로" in analysis.bottleneck and "용해" in name)
            if is_bottleneck:
                st.markdown(
                    f"""<div style="background:#fef2f2; border:2px solid #ef4444;
                    border-radius:8px; padding:12px; text-align:center">
                    <b style="color:#991b1b">{name}</b><br>
                    <small style="color:#7f1d1d">{desc}</small></div>""",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f"""<div style="background:#eff6ff; border:1px solid #bfdbfe;
                    border-radius:8px; padding:12px; text-align:center">
                    <b style="color:#1e3a8a">{name}</b><br>
                    <small style="color:#1e40af">{desc}</small></div>""",
                    unsafe_allow_html=True,
                )

    # ===== 자원 가동률 =====
    st.header("⚙️ 자원 가동률")
    with st.expander("ℹ️ 가동률 해석 가이드", expanded=False):
        st.markdown("""
        **자원 가동률** = (실제 작업 시간 ÷ 가용 시간) × 100%
        
        | 가동률 | 상태 | 의미 |
        |--------|------|------|
        | 🔴 90%+ | 병목 | 해당 자원이 전체 처리량을 제한 중 |
        | 🟡 70~90% | 주의 | 부하가 높음, 모니터링 필요 |
        | 🟢 70% 미만 | 여유 | 충분한 여유 용량 보유 |
        
        **개선 우선순위:** 빨간색(병목) 자원부터 개선 검토
        """)
    util_names = list(analysis.util.keys())
    util_values = [v * 100 for v in analysis.util.values()]
    util_colors = ["#ef4444" if v >= 90 else "#facc15" if v >= 70 else "#22c55e" for v in util_values]

    fig_util = go.Figure(go.Bar(
        x=util_values, y=util_names, orientation="h",
        marker_color=util_colors,
        text=[f"{v:.1f}%" for v in util_values],
        textposition="outside",
    ))
    fig_util.update_layout(
        xaxis_title="가동률 (%)", xaxis=dict(range=[0, 110]),
        height=300, margin=dict(l=120, r=40, t=20, b=40),
    )
    st.plotly_chart(fig_util, use_container_width=True)
    st.caption("90% 이상 빨강 (병목), 70% 이상 노랑 (주의), 그 외 초록 (여유)")

    # ===== 버퍼 시계열 =====
    st.header("📈 버퍼/야적장 점유 시계열")
    with st.expander("ℹ️ 버퍼 시계열 해석 가이드", expanded=False):
        st.markdown("""
        **버퍼 점유 그래프**는 시간에 따른 재고 수준 변화를 보여줍니다.
        
        **건강한 패턴:**
        - 적정 수준(용량의 30~70%)에서 안정적 변동
        - 급격한 증가/감소 없이 완만한 변화
        
        **문제 징후:**
        - 📈 지속 상승: 후속 공정 병목 (출하 지연)
        - 📉 지속 하락/0: 선행 공정 병목 (공급 부족)
        - 🔺 용량 근접: 버퍼 풀 위험, 공정 정지 가능
        
        **버퍼 역할:**
        - 공정 간 속도 차이 흡수
        - 변동성 완충 (디커플링)
        - 적정 재고 유지로 연속 생산 보장
        """)

    def step_xy(samples):
        xs, ys = [], []
        last_y = 0
        for x, y in samples:
            if xs:
                xs.append(x)
                ys.append(last_y)
            xs.append(x)
            ys.append(y)
            last_y = y
        return xs, ys

    fig_buf = go.Figure()
    for samples, name, color in [
        (metrics.pallet_buffer_levels, "파레트 버퍼", "#2563eb"),
        (metrics.flake_buffer_levels, "퓨플레이크 야적", "#0ea5e9"),
        (metrics.scr_buffer_levels, "SCR 코일 야적", "#dc2626"),
    ]:
        xs, ys = step_xy(samples)
        fig_buf.add_trace(go.Scatter(
            x=[t / 60 for t in xs], y=ys,
            mode="lines", name=name,
            line=dict(color=color, width=2),
        ))
    fig_buf.update_layout(
        xaxis_title="시간 (시간)", yaxis_title="점유 개수",
        height=400, legend=dict(orientation="h", y=1.02),
        margin=dict(l=50, r=20, t=40, b=50),
    )
    st.plotly_chart(fig_buf, use_container_width=True)

    # 버퍼 통계 테이블
    buf_data = []
    for name, stats in analysis.queue_stats.items():
        buf_data.append({
            "버퍼": name,
            "평균 점유": f"{stats['avg']:.1f}",
            "최대 점유": f"{stats['max']:.0f}",
            "95퍼센타일": f"{stats['p95']:.0f}",
        })
    st.table(buf_data)

    # ===== 반사로 Gantt =====
    st.header("🔥 반사로 배치 Gantt")
    with st.expander("ℹ️ Gantt 차트 해석 가이드", expanded=False):
        st.markdown("""
        **반사로 Gantt 차트**는 각 반사로의 시간별 작업 상태를 보여줍니다.
        
        **색상 의미:**
        - ⬜ **회색 (장입+준비)**: 파레트 투입 및 가열 준비 (2~3시간)
        - 🟥 **빨강 (용해)**: 고온 용해 진행 중 (**12시간** - 핵심 병목!)
        - 🟩 **초록 (주조)**: 용탕을 제품으로 주조 (약 8시간)
        
        **효율적 운영 패턴:**
        - 반사로 간 작업이 서로 엇갈려 연속 생산
        - 1호기 용해 중 → 2호기 주조 (이상적)
        - 빈 구간(idle) 최소화
        
        **비효율 징후:**
        - 긴 idle 구간: 파레트 공급 부족
        - 배치 간 간격 넓음: 준비 지연
        """)

    # 이벤트에서 구간 추출
    intervals: dict[int, list] = {}
    starts: dict[int, list] = {}
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
        cur_label, cur_start = None, None
        for t, kind in evs:
            if kind == "batch_collected":
                cur_label, cur_start = "장입+준비", t
            elif kind == "melt_start" and cur_start is not None:
                intervals.setdefault(fid, []).append((cur_start, t, "장입+준비"))
                cur_label, cur_start = "용해(12h)", t
            elif kind == "melt_done" and cur_start is not None:
                intervals.setdefault(fid, []).append((cur_start, t, "용해(12h)"))
                cur_label, cur_start = "주조", t
            elif kind == "batch_done" and cur_start is not None:
                intervals.setdefault(fid, []).append((cur_start, t, "주조"))
                cur_start = None

    color_map = {"장입+준비": "#9ca3af", "용해(12h)": "#ef4444", "주조": "#22c55e"}
    fig_gantt = go.Figure()
    legend_seen = set()
    for fid in sorted(intervals.keys()):
        for s, e, label in intervals[fid]:
            show_legend = label not in legend_seen
            legend_seen.add(label)
            fig_gantt.add_trace(go.Bar(
                x=[(e - s) / 60], y=[f"반사로 {fid}"],
                base=[s / 60], orientation="h",
                marker_color=color_map[label],
                name=label, legendgroup=label, showlegend=show_legend,
            ))
    fig_gantt.update_layout(
        xaxis_title="시간 (시간)", barmode="overlay",
        height=250, margin=dict(l=80, r=20, t=20, b=40),
    )
    st.plotly_chart(fig_gantt, use_container_width=True)
    st.caption("회색=장입/준비, 빨강=12시간 용해 (병목), 초록=주조")

    # ===== 누적 트럭 =====
    st.header("🚛 트럭 흐름")
    with st.expander("ℹ️ 트럭 흐름 분석 가이드", expanded=False):
        st.markdown("""
        **누적 트럭 도착/출차 그래프:**
        - 선의 기울기 = 트럭 처리 속도
        - 도착선과 출차선 간격 = 체류 중인 트럭 수
        - 간격이 벌어지면 → 대기열 증가 (병목 징후)
        
        **체류시간 히스토그램:**
        - 중앙값 < 평균: 일부 트럭만 오래 대기 (정상)
        - 중앙값 ≈ 평균: 균일한 대기 시간
        - 긴 꼬리(오른쪽): 간헐적 심각한 지연 발생
        
        **목표 체류시간:**
        - 입고 트럭: 60분 이내
        - 출하 트럭: 90분 이내
        """)
    col_t1, col_t2 = st.columns(2)

    with col_t1:
        series = {"입고 도착": [], "입고 출차": [], "출하 도착": [], "출하 출차": []}
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

        color_truck = {"입고 도착": "#1d4ed8", "입고 출차": "#60a5fa",
                       "출하 도착": "#b91c1c", "출하 출차": "#fb923c"}
        fig_truck = go.Figure()
        for name, ts in series.items():
            if not ts:
                continue
            ts_sorted = sorted(ts)
            fig_truck.add_trace(go.Scatter(
                x=[t / 60 for t in ts_sorted],
                y=list(range(1, len(ts_sorted) + 1)),
                mode="lines", name=name, line=dict(color=color_truck[name], width=2),
            ))
        fig_truck.update_layout(
            title="누적 트럭 도착/출차",
            xaxis_title="시간 (시간)", yaxis_title="누적 대수",
            height=350, margin=dict(l=50, r=20, t=40, b=50),
        )
        st.plotly_chart(fig_truck, use_container_width=True)

    with col_t2:
        fig_lead = go.Figure()
        fig_lead.add_trace(go.Histogram(
            x=metrics.truck_in_lead_times, name="입고 체류시간",
            nbinsx=30, marker_color="#1d4ed8", opacity=0.7,
        ))
        fig_lead.add_trace(go.Histogram(
            x=metrics.truck_out_lead_times, name="출하 체류시간",
            nbinsx=30, marker_color="#dc2626", opacity=0.7,
        ))
        fig_lead.update_layout(
            title="트럭 체류시간 분포",
            xaxis_title="체류시간 (분)", yaxis_title="대수",
            barmode="overlay", height=350,
            margin=dict(l=50, r=20, t=40, b=50),
        )
        st.plotly_chart(fig_lead, use_container_width=True)

    # ===== 일별 생산량 =====
    st.header("📦 일별 생산량")
    with st.expander("ℹ️ 일별 생산량 해석 가이드", expanded=False):
        st.markdown("""
        **스택 바 차트**는 일별 퓨플레이크와 SCR 코일 생산량을 보여줍니다.
        
        **패턴 분석:**
        - **초기 1~2일**: Warm-up 기간 (정상 상태 미도달)
        - **3일차 이후**: 정상 상태 (Steady State) 기대
        - **일정한 막대 높이**: 안정적인 생산 (이상적)
        
        **변동 원인:**
        - 반사로 배치 완료 시점에 따른 자연 변동
        - 입고 변동 → 파레트 공급 변동
        - 출하 수요 변동 → 야적장 용량 영향
        
        **목표:**
        - 일평균 생산량 ≥ 일평균 입고량 × 0.9
        - 일별 변동계수(CV) < 20%
        """)
    if analysis.daily_throughput_ton:
        days_list = [d for d, _, _ in analysis.daily_throughput_ton]
        flake_list = [f for _, f, _ in analysis.daily_throughput_ton]
        scr_list = [s for _, _, s in analysis.daily_throughput_ton]
        fig_daily = go.Figure()
        fig_daily.add_trace(go.Bar(name="퓨플레이크 (t)", x=days_list, y=flake_list, marker_color="#0ea5e9"))
        fig_daily.add_trace(go.Bar(name="SCR 코일 (t)", x=days_list, y=scr_list, marker_color="#dc2626"))
        fig_daily.update_layout(
            barmode="stack", xaxis_title="일차", yaxis_title="생산량 (t)",
            height=350, margin=dict(l=50, r=20, t=20, b=50),
        )
        st.plotly_chart(fig_daily, use_container_width=True)

    # ===== 인사이트 & 권장 =====
    st.header("💡 자동 인사이트 & 권장사항")
    col_i, col_r = st.columns(2)
    with col_i:
        st.subheader("관찰 포인트")
        if analysis.insights:
            for ins in analysis.insights:
                st.info(ins)
        else:
            st.write("특별한 관찰 포인트가 없습니다.")
    with col_r:
        st.subheader("권장 액션")
        if analysis.recommendations:
            for rec in analysis.recommendations:
                st.warning(rec)
        else:
            st.write("특별한 권장 액션이 없습니다.")

    # ===== 설정 요약 =====
    with st.expander("⚙️ 시뮬레이션 설정 요약"):
        config_data = [
            ("시뮬레이션 일수", sim_days, "일"),
            ("랜덤 시드", random_seed, ""),
            ("일 트럭 수", trucks_per_day, "대"),
            ("트럭 적재", payload_ton, "t"),
            ("하역 베이", unloading_bays, ""),
            ("선별 워커", sorters, ""),
            ("압착기", press_machines, ""),
            ("파레트 버퍼", pallet_buffer_capacity, "개"),
            ("반사로", furnace_count, "대"),
            ("배치 단위", batch_ton, "t"),
            ("flake 비율", f"{flake_ratio}%", ""),
            ("출하 간격", empty_truck_interval, "분"),
        ]
        st.table([{"파라미터": n, "값": v, "단위": u} for n, v, u in config_data])

else:
    st.info("👈 왼쪽 사이드바에서 파라미터를 설정하고 **시뮬레이션 실행** 버튼을 누르세요.")

    # 탭으로 정보 구성
    tab1, tab2, tab3 = st.tabs(["📋 시뮬레이션 개요", "🏭 공정 상세", "🔬 방법론 및 라이브러리"])

    with tab1:
        st.markdown("""
        ### 시뮬레이션 개요

        | 단계 | 내용 |
        |------|------|
        | 1. 입고/하역 | 트럭 도착 → 1차 계근 → 하역(20분) → 2차 계근 → 출차 |
        | 2. 선별/압착 | 30분 정리 → 0.5t × 8.5분 압착 → 파레트(2.5t) 생성 |
        | 3. 장입/용해 | 32 파레트(80t) → 엘리베이터 → 2h 준비 → **12h 용해** |
        | 4. 주조 | 퓨플레이크(1t/2.5분) + SCR(4t/10분) 병렬 생산 |
        | 5. 출하 | 완제품 야적 → 빈 트럭 도착 → 상차 → 2차 계근 → 출차 |

        ### 기본값 기준 예상 결과
        - 일평균 입고: **200t** (10대 × 20t)
        - 12h 용해가 병목이면 일평균 처리량 약 **80t**/일
        - 압착기가 병목이면 파레트 생성이 지연되어 처리량 감소
        """)

    with tab2:
        st.markdown("## 군산 공장 하이브리드 공정 상세")
        st.markdown("""
        스크랩 구리 입고부터 완제품 출하까지 5단계에 이르는 군산 공장의 모든 상세 공정 흐름을 설명합니다.
        """)

        # 1. 스크랩 구리 입고 및 하역 공정
        st.markdown("---")
        st.markdown("### 1. 스크랩 구리 입고 및 하역 공정")
        st.markdown("""
        군산 공장의 하이브리드 공정 중 첫 번째 단계인 **스크랩 구리 입고 및 하역 공정**은 다음과 같은 순서로 진행됩니다.
        """)

        col1_1, col1_2 = st.columns([1, 1])
        with col1_1:
            st.markdown("""
            **① 트럭 입고 및 대기**
            - 하루에 약 **10대의 트럭**이 각각 **20톤의 스크랩 구리**를 싣고 공장에 도착
            - 순수한 구리뿐만 아니라 철이나 쓰레기 등 여러 이물질이 섞인 스크랩이 실림
            - 시뮬레이션에서는 병목 현상 방지를 위해 **오전 10시부터 1시간 간격**으로 도착하도록 스케줄링

            **② 1차 계근 및 중량 측정**
            - 공장 진입 시 계근대를 거쳐 적재된 스크랩 구리의 중량 측정
            - 서류상 물량(20톤)과 실제 중량 일치 여부 확인
            - 계근대 대기 및 측정 시간: **약 5분**
            """)
        with col1_2:
            st.markdown("""
            **③ 지정 공간 하역 작업 (쏟아 붓기)**
            - 1차 계근 후 지정된 하역 공간으로 이동하여 스크랩 구리를 바닥에 쏟아 붓기
            - 바닥에 부어진 스크랩은 **약 5×5 크기의 산더미 형태**로 쌓임
            - 하역 작업 시간: **약 20분**
            - 두 대 동시 하역 가능하도록 한 공간 추가 마련

            **④ 2차 계근(영점 확인) 및 빈 차 출차**
            - 하역 완료 후 다시 계근대를 거쳐 **적재 중량 0kg 확인**
            - 부당한 물량 반출 사고 방지 목적
            - 영점 확인 후 빈 트럭 출차, 계근 대기 시간: **약 5분**
            """)

        st.info("💡 계근 → 하역 → 재계근 → 출차의 과정이 하루 10대의 차량에 걸쳐 지속적으로 반복되면서 본격적인 공정의 원료가 수급됩니다.")

        # 2. 선별 및 압착 공정
        st.markdown("---")
        st.markdown("### 2. 선별 및 압착 공정")
        st.markdown("""
        두 번째 단계인 **선별 및 압착 공정**은 스크랩 구리의 불순물을 제거하고 
        다음 공정인 용해로 투입을 위해 부피를 압축하는 핵심 단계입니다.
        """)

        col2_1, col2_2 = st.columns([1, 1])
        with col2_1:
            st.markdown("""
            **① 이물질 선별 및 크기 축소 (정리 작업)**
            - 5×5 크기의 스크랩 더미에서 이물질(철, 쓰레기 등) 선별 제거
            - 구리 덩어리들을 **2×5 사이즈로 길게** 정리
            - 선별 및 정리 작업 시간: **약 30분**
            - 20톤의 구리에서 **총 8개의 더미** 생성

            **② 압착기 투입 대기**
            - 지게차를 이용해 **한 번에 0.5톤(500kg)** 분량씩 압착기에 투입
            - 지게차 준비 시간: **약 5분**
            """)
        with col2_2:
            st.markdown("""
            **③ 압착 작업 (프레스)**
            - 압착기가 스크랩 구리를 강하게 눌러 부피를 줄인 블록 형태로 제작
            - **0.5톤짜리 덩어리 1개** 압착 완성 시간: **90초**

            **④ 파레트 적재 및 버퍼 공간 보관**
            - 압착된 덩어리들을 지게차로 파레트 위에 적재
            - **적재 기준:** 0.5톤 × 5개 = **2.5톤/파레트**
            - 덩어리 1개 적재 시간: **약 2분**
            - 야적장 최대 용량: **2일치(파레트 160개)**
            """)

        # 3. 장입 및 용해 공정
        st.markdown("---")
        st.markdown("### 3. 장입 및 용해 공정 ⚠️ 핵심 병목")
        st.error("⚠️ **주의**: 이 공정은 전체 물류 흐름의 가장 핵심적인 병목(Bottleneck) 구간입니다.")
        st.markdown("""
        앞선 공정에서 완성된 파레트들이 버퍼 공간에 모두 모인 후부터 본격적으로 시작됩니다.
        """)

        col3_1, col3_2 = st.columns([1, 1])
        with col3_1:
            st.markdown("""
            **① 엘리베이터 운송 및 장입 (Charging)**
            - 버퍼의 2.5톤 파레트들을 **총 80톤(32개)까지** 용해로로 이동
            - **산업용 엘리베이터**(최대 5~10톤 하중)로 파레트 운송
            - 안전을 위해 **1회당 2개 파레트(5톤)만** 적재
            - 엘리베이터 왕복 1사이클: **약 10분**
            - 80톤 전체 운송에 수 시간 소요

            **② 사전 준비 작업**
            - 지게차로 파레트를 용해로(반사로)에 투입
            - 장비 셋업 및 구리 안정화 대기: **약 2시간**
            """)
        with col3_2:
            st.markdown("""
            **③ 12시간 용해 (Melting) - 최대 병목 지점**
            - 80톤의 스크랩 구리를 완전히 쇳물로 녹이는 시간: **정확히 12시간**
            - 이 시간 동안 새로운 물량 투입 불가
            - 앞단 공정 생산물은 버퍼에 대기
            - **전체 시뮬레이션의 가장 주요한 병목 기준 시간**

            **🔧 연속 생산 및 설비 유연성**
            - 공장에 반사로 **2개** 보유
            - 하나가 용해 중일 때 다른 반사로에 장입/예열 준비 가능
            - **최대 200톤**까지 연속 투입 가동 가능
            """)

        # 4. 하이브리드 제품 생산 공정
        st.markdown("---")
        st.markdown("### 4. 하이브리드 제품 생산 공정 (주조/압연/권취)")
        st.markdown("""
        군산 공장의 가장 큰 특징인 **'하이브리드 공정'**이 본격적으로 이루어지는 단계입니다.
        12시간 동안 끓인 쇳물을 **두 가지 형태의 제품**으로 뽑아냅니다.
        """)

        col4_1, col4_2 = st.columns([1, 1])
        with col4_1:
            st.markdown("""
            **① 생산 셋업 및 대기 (홀딩로 이동)**
            - 용해 완료 후 사전 준비(셋업): **약 1~2시간**
            - 쇳물을 용해로(반사로)에서 대기로(홀딩로)로 이동

            **② 라인 분기 (하이브리드 교차 생산)**
            - 동일한 쇳물에서 **2가지 제품 선택적 생산** 가능
            - 홀딩로 출구 경로 조절로 라인 선택
            - **생산 비율: 퓨플레이크 30% : SCR 코일 70%** (기본값)
            """)
        with col4_2:
            st.markdown("""
            **③ 퓨플레이크 (Cu-flake) 생산**
            - 끓는 쇳물에 찬물을 쏘아 순간 응축
            - 시리얼(콘프레이크)처럼 구리 조각 생성
            - 포대자루에 담아 포장
            - **생산 속도: 2.5분당 1톤(포대 1개)**

            **④ SCR 코일 (구리선) 생산**
            - 쇳물을 주조식으로 길게 뽑아 구리선 제작
            - 둥글게 감아 거대한 코일 형태로 제작
            - **생산 속도: 10분당 4톤(코일 1개)**
            """)

        st.success("""
        **📅 총 24시간 사이클 완성**
        - 장입 전 준비 (약 2시간) → 용해 (12시간) → 주조 셋업 및 실제 생산 (약 10시간)
        - 최대 200톤 분량의 쇳물을 두 가지 제품으로 전부 뽑아내는 실제 오퍼레이션 타임: **약 8시간**
        """)

        # 5. 완제품 야적 및 출하 공정
        st.markdown("---")
        st.markdown("### 5. 완제품 야적 및 출하 공정")
        st.markdown("""
        군산 공장 하이브리드 시뮬레이션의 마지막 단계입니다.
        """)

        col5_1, col5_2 = st.columns([1, 1])
        with col5_1:
            st.markdown("""
            **① 완제품 이동 및 버퍼(야적장) 보관**
            - 퓨플레이크 포대(1톤)와 SCR 코일(4톤)을 지게차로 야적장 이동
            - 시각적 구분: **퓨플레이크=파란색, SCR 코일=빨간색**
            - 야적장 용량: **약 2일치(총 400톤)**
              - 퓨플레이크: 100포대(100톤)
              - SCR 코일: 75개(300톤)

            **② 빈 트럭 입고 및 1차 계근 (영점 확인)**
            - 완제품 반출을 위한 빈 트럭 입고
            - 계근대에서 **적재 중량 0kg 확인**
            """)
        with col5_2:
            st.markdown("""
            **③ 지게차 상차 작업**
            - 영점 확인 후 야적장에서 완제품 상차
            - 트럭 1대당 **최대 20~25톤** 적재
            - 시뮬레이션에서 20톤 기준 출하 설정

            **④ 2차 계근 및 최종 출하**
            - 상차 완료 후 2차 중량 측정
            - 서류상 지정된 중량(20~25톤) 정확성 확인
            - 부당 반출 방지 최종 검증
            - 확인 완료 후 납품처로 출발
            """)

        st.info("🎉 이로써 스크랩 구리 입고부터 최종 완제품 출하까지 1~5단계에 이르는 군산 공장의 모든 상세 공정 흐름이 완료됩니다.")

        # 전체 공정 요약 표
        st.markdown("---")
        st.markdown("### 📊 전체 공정 요약")
        process_summary = [
            {"단계": "1. 입고/하역", "핵심 활동": "트럭 입고 → 계근 → 하역 → 출차", "주요 시간": "계근 5분, 하역 20분"},
            {"단계": "2. 선별/압착", "핵심 활동": "이물질 선별 → 압착 → 파레트 적재", "주요 시간": "선별 30분, 압착 90초/0.5톤"},
            {"단계": "3. 장입/용해", "핵심 활동": "엘리베이터 운송 → 장입 → 용해", "주요 시간": "준비 2시간, 용해 12시간 ⚠️"},
            {"단계": "4. 제품 생산", "핵심 활동": "홀딩로 이동 → 퓨플레이크/SCR 주조", "주요 시간": "셋업 1~2시간, 생산 8시간"},
            {"단계": "5. 야적/출하", "핵심 활동": "야적장 보관 → 상차 → 계근 → 출하", "주요 시간": "트럭당 20~25톤 출하"},
        ]
        st.table(process_summary)

    with tab3:
        st.markdown("## 시뮬레이션 방법론 및 사용 라이브러리")
        st.markdown("""
        본 시뮬레이션은 **학술적으로 검증된 방법론**과 **산업 표준 라이브러리**를 활용하여
        결과의 신뢰성과 재현성을 보장합니다.
        """)

        # SimPy 설명
        st.markdown("---")
        st.markdown("### 1. SimPy - 이산사건 시뮬레이션 (Discrete Event Simulation)")

        col_simpy1, col_simpy2 = st.columns([2, 1])
        with col_simpy1:
            st.markdown("""
            **SimPy**는 Python 기반 이산사건 시뮬레이션(DES) 프레임워크로,
            **2002년 최초 출시 이후 20년 이상** 학술 및 산업 분야에서 검증되었습니다.

            #### 학술적/산업적 신뢰성
            - **Google Scholar**: 수천 편의 학술 논문에서 인용
            - **적용 분야**: 제조업 공정, 물류/공급망, 의료 시스템, 통신 네트워크
            - **글로벌 기업**: Boeing, Toyota, DHL 등의 시뮬레이션 프로젝트에 활용

            #### 이산사건 시뮬레이션(DES)이란?
            연속 시간을 모사하지 않고 **이벤트 발생 시점**만 처리하여 계산 효율을 극대화하는 방법론입니다.
            제조업 공정 시뮬레이션의 **국제 표준 방법론**으로 인정받고 있습니다.
            """)
        with col_simpy2:
            st.markdown("""
            | 항목 | 내용 |
            |------|------|
            | 라이선스 | MIT (오픈소스) |
            | 버전 | 4.1+ |
            | 최초 출시 | 2002년 |
            | 유지보수 | 활발 (지속 업데이트) |
            """)

        with st.expander("💡 본 프로젝트에서 SimPy 활용 상세"):
            st.markdown("""
            ```python
            # 자원 경쟁 모델링 - 대기열 및 선착순 처리 자동 관리
            self.weighbridge = simpy.Resource(env, capacity=1)   # 계근대 1개
            self.furnaces = simpy.Resource(env, capacity=2)       # 반사로 2개

            # 버퍼 관리 - 용량 초과 시 생산 라인 자동 정지
            self.pallet_buffer = simpy.Store(env, capacity=160)   # 파레트 버퍼

            # 병렬 프로세스 - 퓨플레이크/SCR 동시 주조
            yield self.env.all_of([flake_proc, scr_proc])
            ```

            **모델링된 자원:**
            - 계근대(1개), 하역장(2개), 압착기(1개), 반사로(2개), 엘리베이터(1개)
            - 파레트 버퍼(160개), 퓨플레이크 야적(100포대), SCR 코일 야적(75코일)
            """)

        # CP-SAT 설명
        st.markdown("---")
        st.markdown("### 2. Google OR-Tools CP-SAT - 제약 만족 프로그래밍 최적화")

        col_cpsat1, col_cpsat2 = st.columns([2, 1])
        with col_cpsat1:
            st.markdown("""
            **CP-SAT (Constraint Programming - SAT Solver)**는 Google Research의
            Operations Research Team이 개발한 최적화 솔버입니다.

            #### 학술적/산업적 신뢰성
            - **MiniZinc Challenge**: 국제 제약 프로그래밍 경진대회에서 **지속적 상위권** 기록
            - **Google 내부 활용**: 자원 배분, 직원 스케줄링, 광고 최적화에 실전 적용
            - **학술 검증**: 수천 편의 논문에서 **벤치마크 솔버**로 활용

            #### 핵심 특징
            - **최적성 증명**: 최적해 발견 시 "더 나은 해가 없음"을 **수학적으로 증명**
            - **작업 스케줄링 특화**: `IntervalVar`, `NoOverlap` 등 스케줄링 전용 기능 제공
            """)
        with col_cpsat2:
            st.markdown("""
            | 항목 | 내용 |
            |------|------|
            | 개발사 | Google Research |
            | 라이선스 | Apache 2.0 (오픈소스) |
            | 버전 | 9.10+ |
            | 솔버 유형 | SAT + CP 하이브리드 |
            """)

        with st.expander("💡 본 프로젝트에서 CP-SAT 활용 상세"):
            st.markdown("""
            **반사로 배치 스케줄 최적화 문제:**

            ```python
            # 변수 정의
            start = model.NewIntVar(release_min, horizon_min, f"start_{batch_id}")

            # 제약 조건: 같은 반사로 내 작업 중첩 금지
            model.AddNoOverlap(intervals_per_furnace[f])

            # 목적 함수: 메이크스팬 최소화
            model.Minimize(makespan)
            ```

            **최적화 문제 구조:**
            - **변수**: 배치 시작 시각, 반사로 배정 (1 또는 2)
            - **제약**: 파레트 32개 준비 후 시작, 동일 반사로 작업 비중첩
            - **목적**: 전체 완료 시간(Makespan) 최소화

            **결과 해석:**
            - `OPTIMAL` 상태 시: 해당 메이크스팬이 **이론적 최선**임을 보장
            - `FEASIBLE` 상태 시: 실행 가능한 해이나 최적성 미증명
            """)

        # Matplotlib 설명
        st.markdown("---")
        st.markdown("### 3. Matplotlib - 시각화 및 애니메이션")

        st.markdown("""
        **Matplotlib**은 Python 시각화의 **사실상 표준(de facto standard)**으로,
        과학/공학 분야에서 가장 널리 사용되는 플로팅 라이브러리입니다.

        | 항목 | 내용 |
        |------|------|
        | 라이선스 | PSF License (Python Software Foundation) |
        | 버전 | 3.8+ |
        | 활용 | 공장 레이아웃 애니메이션, 버퍼 시계열 그래프, GIF/MP4 출력 |
        """)

        # Plotly / Streamlit 설명
        st.markdown("---")
        st.markdown("### 4. Plotly & Streamlit - 인터랙티브 대시보드")

        col_ui1, col_ui2 = st.columns(2)
        with col_ui1:
            st.markdown("""
            **Plotly**
            - 인터랙티브 차트 라이브러리
            - 줌, 팬, 호버 등 동적 기능 지원
            - 버퍼 시계열, Gantt 차트, 히스토그램 렌더링
            """)
        with col_ui2:
            st.markdown("""
            **Streamlit**
            - Python 기반 웹 앱 프레임워크
            - 데이터 과학/ML 대시보드에 최적화
            - 실시간 파라미터 조정 및 즉시 결과 확인
            """)

        # 방법론 요약
        st.markdown("---")
        st.markdown("### 📊 시뮬레이션 결과 신뢰성 요약")

        st.markdown("""
        | 구분 | 방법론 | 신뢰성 근거 |
        |------|--------|-------------|
        | **공정 시뮬레이션** | 이산사건 시뮬레이션 (DES) | 제조업 국제 표준, 20년+ 검증된 SimPy |
        | **스케줄 최적화** | 제약 만족 프로그래밍 (CP-SAT) | Google 개발, 국제 대회 검증, 최적성 수학적 증명 |
        | **불확실성 반영** | 지수분포 기반 확률 모델 | 도착 과정의 표준 확률 모델 (출하 트럭) |
        | **재현성** | 랜덤 시드 고정 | 동일 시드로 동일 결과 보장 |
        """)

        with st.expander("⚠️ 결과 해석 시 주의사항"):
            st.markdown("""
            1. **확정적 가정**: 작업 시간(용해 12시간, 압착 1.5분 등)은 고정값으로 모델링
               - 실제 변동성 반영 필요 시 확률 분포 적용 가능

            2. **단순화된 설비 모델**: 설비 고장, 유지보수 일정 미반영
               - 추후 확장 가능

            3. **시드 기반 재현성**: `random_seed` 고정으로 재현성 확보
               - 다른 시드로 반복 실험하여 통계적 신뢰구간 산출 권장

            4. **입력 데이터 의존성**: 파라미터 값의 정확도가 결과 품질에 직접 영향
               - 현장 데이터 기반 파라미터 검증 필요
            """)

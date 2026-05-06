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

# ---------------------------------------------------------------------------
# 페이지 설정
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="군산 공장 하이브리드 시뮬레이션",
    page_icon="🏭",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# 사이드바 - 파라미터 입력
# ---------------------------------------------------------------------------

st.sidebar.title("🏭 시뮬레이션 파라미터")

st.sidebar.header("1. 기본 설정")
sim_days = st.sidebar.slider("시뮬레이션 일수", 1, 30, DEFAULT_CONFIG.sim_days)
random_seed = st.sidebar.number_input("랜덤 시드", value=DEFAULT_CONFIG.random_seed)

st.sidebar.header("2. 입고/하역")
trucks_per_day = st.sidebar.slider(
    "일일 트럭 대수", 1, 20, DEFAULT_CONFIG.inbound.trucks_per_day
)
payload_ton = st.sidebar.slider(
    "트럭당 적재량 (t)", 10.0, 30.0, DEFAULT_CONFIG.inbound.payload_ton, 1.0
)
unloading_bays = st.sidebar.slider(
    "하역 베이 수", 1, 4, DEFAULT_CONFIG.inbound.unloading_bays
)

st.sidebar.header("3. 선별/압착")
sorters = st.sidebar.slider("선별 작업조 수", 1, 4, DEFAULT_CONFIG.sorting.sorters)
press_machines = st.sidebar.slider(
    "압착기 대수", 1, 4, DEFAULT_CONFIG.sorting.press_machines
)
pallet_buffer_capacity = st.sidebar.slider(
    "파레트 버퍼 용량", 50, 300, DEFAULT_CONFIG.sorting.pallet_buffer_capacity, 10
)

st.sidebar.header("4. 용해/주조")
furnace_count = st.sidebar.slider(
    "반사로 대수", 1, 3, DEFAULT_CONFIG.melting.furnace_count
)
batch_ton = st.sidebar.slider(
    "배치 단위 (t)", 40.0, 200.0, DEFAULT_CONFIG.melting.batch_ton, 10.0
)
flake_ratio = st.sidebar.slider(
    "퓨플레이크 비율 (%)", 0, 100, int(DEFAULT_CONFIG.casting.flake_ratio * 100)
)

st.sidebar.header("5. 출하")
empty_truck_interval = st.sidebar.slider(
    "출하 트럭 평균 간격 (분)", 30, 180, int(DEFAULT_CONFIG.outbound.empty_truck_interval_min)
)

# 실행 버튼
st.sidebar.markdown("---")
run_button = st.sidebar.button("🚀 시뮬레이션 실행", type="primary", use_container_width=True)

# ---------------------------------------------------------------------------
# 메인 - 결과 대시보드
# ---------------------------------------------------------------------------

st.title("🏭 군산 공장 하이브리드 공정 시뮬레이션")
st.markdown("""
스크랩 구리 입고 → 선별/압착 → 장입/용해(12h) → 하이브리드 주조 → 완제품 출하의
5단계 공정을 SimPy 이산사건 시뮬레이션으로 모델링합니다.
**왼쪽 사이드바에서 파라미터를 조정**한 뒤 **시뮬레이션 실행** 버튼을 누르세요.
""")

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
        st.metric("처리 트럭 (입고)", f"{summary['trucks_in_processed']} 대")
    with cols[1]:
        st.metric("출하 트럭", f"{summary['trucks_out_dispatched']} 대")
    with cols[2]:
        st.metric("완료 배치", f"{summary['melt_batches_completed']} 회")
    with cols[3]:
        st.metric("총 생산량", f"{summary['total_product_ton']:.0f} t")
    with cols[4]:
        st.metric("일평균 처리량", f"{summary['throughput_ton_per_day']:.1f} t/일")

    cols2 = st.columns(5)
    with cols2[0]:
        st.metric("퓨플레이크", f"{summary['flake_ton']:.0f} t")
    with cols2[1]:
        st.metric("SCR 코일", f"{summary['scr_ton']:.0f} t")
    with cols2[2]:
        st.metric("입고 평균체류", f"{summary['avg_truck_in_lead_min']:.1f} 분")
    with cols2[3]:
        st.metric("출하 평균체류", f"{summary['avg_truck_out_lead_min']:.1f} 분")
    with cols2[4]:
        st.metric("배치 평균시간", f"{summary['avg_melt_batch_min']:.0f} 분")

    # ===== 병목 진단 =====
    st.header("🔍 병목 진단")
    st.error(f"**식별된 병목: {analysis.bottleneck}** — {analysis.bottleneck_reason}")

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
    tab1, tab2 = st.tabs(["📋 시뮬레이션 개요", "🔬 방법론 및 라이브러리"])

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

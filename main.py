"""군산 공장 하이브리드 공정 시뮬레이션 실행 진입점.

사용법::

    python main.py                       # 기본 7일 시뮬레이션 + 차트 + CP-SAT 비교
    python main.py --days 14             # 14일치 시뮬레이션
    python main.py --no-plot             # 차트 생성 생략
    python main.py --no-optimize         # CP-SAT 최적화 비교 생략
    python main.py --events events.csv   # 이벤트 로그 CSV 저장
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import sys
from pathlib import Path

# Windows PowerShell 환경에서도 한글이 깨지지 않도록 stdout 을 UTF-8 로 재설정
try:  # Python 3.7+
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from config import DEFAULT_CONFIG, SimulationConfig
from metrics import Metrics, format_summary
from optimizer import (
    BatchSpec,
    estimate_batch_duration,
    estimate_batch_releases,
    format_schedule,
    solve_furnace_schedule,
)
from simulation import run_simulation


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gunsan factory hybrid process simulator")
    parser.add_argument("--days", type=int, default=DEFAULT_CONFIG.sim_days)
    parser.add_argument("--seed", type=int, default=DEFAULT_CONFIG.random_seed)
    parser.add_argument("--no-plot", action="store_true")
    parser.add_argument("--no-optimize", action="store_true")
    parser.add_argument("--out", type=str, default="out")
    parser.add_argument("--events", type=str, default=None,
                        help="이벤트 로그를 저장할 CSV 경로")
    parser.add_argument(
        "--animate",
        nargs="?",
        const="out/factory.gif",
        default=None,
        help="공장 레이아웃 애니메이션 파일 경로 (예: out/factory.gif). "
             "값을 안 주면 out/factory.gif 로 저장한다.",
    )
    parser.add_argument(
        "--frame-min",
        type=float,
        default=60.0,
        help="애니메이션 한 프레임이 시뮬 몇 분에 해당하는지 (기본 60)",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=12,
        help="애니메이션 fps (기본 12)",
    )
    parser.add_argument(
        "--report",
        nargs="?",
        const="out/report.html",
        default=None,
        help="결과 해석 HTML 리포트 생성 (예: out/report.html). "
             "값을 안 주면 out/report.html 로 저장한다.",
    )
    return parser.parse_args()


def write_events_csv(metrics: Metrics, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["time_min", "stage", "kind", "detail"])
        for ev in metrics.events:
            writer.writerow([
                f"{ev.time_min:.2f}",
                ev.stage,
                ev.kind,
                ";".join(f"{k}={v}" for k, v in ev.detail.items()),
            ])


def main() -> None:
    args = parse_args()
    cfg = dataclasses.replace(
        DEFAULT_CONFIG,
        sim_days=args.days,
        random_seed=args.seed,
    )

    print(
        f"[INFO] {args.days}일치 시뮬레이션을 시작합니다 "
        f"(가상 시간 horizon = {cfg.sim_horizon_min} min)."
    )
    print("[INFO] SimPy 엔진 단계(아래 순서대로 진행됩니다):")
    metrics = run_simulation(
        cfg,
        progress=lambda msg: print(f"       · {msg}"),
    )

    summary = metrics.summary(cfg.sim_horizon_min)
    print(format_summary(summary))

    if not args.no_optimize:
        print(
            "\n[INFO] SimPy 이후 단계: 반사로 배치에 대해 CP-SAT으로 이론 스케줄·"
            "메이크스팬을 계산하고 시뮬 실측과 비교합니다."
        )
        # CP-SAT 로 이상적 메이크스팬 산출
        duration = estimate_batch_duration(cfg)

        # 1차: 시뮬레이션 이벤트 로그에서 실제 32 파레트 누적 시점을 release 로 사용
        pallet_done_times: list[float] = sorted(
            ev.time_min
            for ev in metrics.events
            if ev.stage == "press" and ev.kind == "pallet_done"
        )
        pallets_per_batch = cfg.melting.pallets_per_batch
        sim_releases = [
            int(round(pallet_done_times[i]))
            for i in range(pallets_per_batch - 1, len(pallet_done_times),
                           pallets_per_batch)
        ]
        if sim_releases:
            # 시뮬레이션이 horizon 내에 실제로 완료한 배치 수만큼만 비교
            n_completed = max(metrics.batches_completed, 1)
            releases = sim_releases[:n_completed]
            print(
                f"\n[CP-SAT] release 시점은 시뮬레이션 실측 파레트 생성 시각 기준 "
                f"(완료 배치 {n_completed}개와 동일 조건 비교)"
            )
        else:
            # 트럭이 도착했더라도 horizon 내 32 파레트 못 모은 경우 추정값 사용
            all_releases = estimate_batch_releases(cfg)
            releases = [r for r in all_releases if r < cfg.sim_horizon_min]
            print("\n[CP-SAT] release 시점은 추정 모델 사용 (시뮬레이션 실측 미확보)")

        batches = [
            BatchSpec(batch_id=i + 1, release_min=r, duration_min=duration)
            for i, r in enumerate(releases)
        ]
        if not batches:
            print("[CP-SAT] 시뮬레이션 horizon 내 모일 수 있는 배치가 없습니다.")
        else:
            print(f"[CP-SAT] 입력 배치 수: {len(batches)}, "
                  f"배치당 처리시간: {duration} min "
                  f"({duration / 60:.1f} h)")
            release_gap_h = (
                (releases[-1] - releases[0]) / max(len(releases) - 1, 1) / 60
                if len(releases) > 1
                else 0.0
            )
            print(f"          첫 배치 release : "
                  f"{releases[0]} min ({releases[0] / 60:.1f} h)")
            print(f"          평균 release 간격: "
                  f"{release_gap_h:.1f} h "
                  f"(반사로 1대 처리시간 {duration / 60:.1f} h 와 비교)")
            schedule = solve_furnace_schedule(
                batches=batches,
                furnace_count=cfg.melting.furnace_count,
                time_limit_sec=10.0,
            )
            print(format_schedule(schedule))

            # 시뮬레이션 결과와 비교
            sim_makespan = 0.0
            for ev in metrics.events:
                if ev.stage == "melting" and ev.kind == "batch_done":
                    sim_makespan = max(sim_makespan, ev.time_min)
            if sim_makespan > 0:
                print(f"  시뮬레이션 실측 메이크스팬: {sim_makespan:.0f} 분 "
                      f"({sim_makespan / 60:.1f} h)")
                if schedule.makespan_min > 0:
                    gap = sim_makespan - schedule.makespan_min
                    print(f"  최적 대비 격차            : {gap:+.0f} 분 "
                          f"({gap / 60:+.1f} h)")

    if args.events:
        write_events_csv(metrics, Path(args.events))
        print(f"[INFO] 이벤트 로그 저장: {args.events}")

    if not args.no_plot:
        print("\n[INFO] matplotlib 정적 차트(buffer / Gantt / 트럭)를 생성합니다…")
        try:
            from visualize import render_all
        except ImportError as exc:
            print(f"[WARN] matplotlib 미설치 - 차트 생략: {exc}")
        else:
            files = render_all(metrics, out_dir=args.out)
            print(f"[INFO] 차트 저장 완료:")
            for p in files:
                print(f"   - {p}")

    if args.animate:
        try:
            from animate import render_factory_animation
        except ImportError as exc:
            print(f"[WARN] 애니메이션 의존성 부족 - 생략: {exc}")
        else:
            print(f"[INFO] 공장 레이아웃 애니메이션 생성 중 "
                  f"(frame={args.frame_min}min, fps={args.fps})...")
            out_path = render_factory_animation(
                metrics,
                cfg,
                out_path=args.animate,
                step_min=args.frame_min,
                fps=args.fps,
            )
            print(f"[INFO] 애니메이션 저장 완료: {out_path}")

    if args.report:
        try:
            from report import generate_report
        except ImportError as exc:
            print(f"[WARN] plotly 미설치 - 리포트 생략: {exc}")
        else:
            print(f"[INFO] HTML 리포트 생성 중...")
            out_path = generate_report(metrics, cfg, out_path=args.report)
            abs_path = out_path.resolve()
            print(f"[INFO] 리포트 저장 완료: {out_path}")
            print(f"        브라우저에서 열기: file:///{abs_path.as_posix()}")


if __name__ == "__main__":
    main()

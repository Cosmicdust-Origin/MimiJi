from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .appinfo import APP_NAME, APP_VERSION
from .core import JobConfig, JobPaths, load_json
from .jobs import JobRegistry
from .preflight import diagnose
from .providers import get_provider
from .subtitles import build_and_validate
from .transcriber import transcribe
from .translator import translate


def _add_job_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--video", type=Path, required=True, help="입력 영상 경로")
    parser.add_argument("--code", required=True, help="작품 제목(결과 파일 이름에 쓰임)")
    parser.add_argument("--output", type=Path, required=True, help="출력 폴더")
    parser.add_argument("--provider", choices=("codex", "claude"), default="codex")
    parser.add_argument("--model", help="번역 모델. 기본값은 제공자별로 결정됩니다.")
    parser.add_argument(
        "--reasoning-effort",
        choices=("default", "low", "medium", "high", "xhigh", "max"),
        default="default",
        help="번역 모델 사고 수준",
    )
    parser.add_argument(
        "--language-pair",
        choices=["ja-ko", "ko-ja"],
        default="ja-ko",
        help="ja-ko: 일본어 음성 → 한국어 자막, ko-ja: 한국어 음성 → 일본어 자막",
    )
    parser.add_argument(
        "--speed-mode",
        choices=("standard", "fast"),
        default="standard",
        help="Codex 번역 속도 모드. Claude는 standard만 지원합니다.",
    )
    parser.add_argument("--batch-size", type=int, default=240)
    parser.add_argument("--accelerator", choices=("auto", "cuda", "cpu"), default="auto")
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--compute-type", choices=("auto", "float16", "int8"), default="auto")
    parser.add_argument("--cpu-threads", type=int)
    parser.add_argument("--diarization", action="store_true", help="화자 분리를 사용합니다.")
    parser.add_argument("--diarization-speakers", type=int, help="예상 화자 수")
    parser.add_argument(
        "--diarization-alignment",
        choices=("basic", "precise"),
        default="basic",
    )
    parser.add_argument(
        "--diarization-device",
        choices=("auto", "cuda", "cpu"),
        default="auto",
    )
    parser.add_argument("--diarization-srt-labels", action="store_true")


def _config_from_args(args: argparse.Namespace) -> JobConfig:
    model = args.model or ("gpt-5.6-luna" if args.provider == "codex" else "sonnet")
    return JobConfig(
        video_path=args.video,
        code=args.code,
        output_dir=args.output,
        provider=args.provider,
        model=model,
        reasoning_effort=args.reasoning_effort,
        speed_mode=args.speed_mode,
        language_pair=args.language_pair,
        batch_size=args.batch_size,
        accelerator=args.accelerator,
        device_index=args.device_index,
        compute_type=args.compute_type,
        cpu_threads=args.cpu_threads,
        diarization_enabled=args.diarization,
        diarization_expected_speakers=args.diarization_speakers,
        diarization_alignment=args.diarization_alignment,
        diarization_device=args.diarization_device,
        diarization_srt_labels=args.diarization_srt_labels,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mimiji", description=f"{APP_NAME}: CUDA 우선 로컬 영상 자막 도구"
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("gui", help="단계형 GUI를 실행합니다.")

    diagnose_parser = subparsers.add_parser("diagnose", help="실행 환경을 진단합니다.")
    diagnose_parser.add_argument("--video", type=Path)

    login_parser = subparsers.add_parser("login", help="공식 CLI 로그인 창을 엽니다.")
    login_parser.add_argument("provider", choices=("codex", "claude"))

    status_parser = subparsers.add_parser("status", help="번역 CLI 로그인 상태를 확인합니다.")
    status_parser.add_argument("provider", choices=("codex", "claude"))

    jobs_parser = subparsers.add_parser("jobs", help="저장된 작업 목록을 표시합니다.")
    jobs_parser.add_argument("--all", action="store_true", help="완료된 작업도 표시합니다.")
    jobs_parser.add_argument("--state-dir", type=Path, help="작업 목록 저장 폴더를 지정합니다.")

    inspect_parser = subparsers.add_parser(
        "inspect-job", help="작업을 재개할 수 있는지 검사합니다."
    )
    inspect_parser.add_argument("job_id")
    inspect_parser.add_argument("--state-dir", type=Path)

    resume_parser = subparsers.add_parser("resume", help="저장된 작업을 이어서 진행합니다.")
    resume_parser.add_argument("job_id")
    resume_parser.add_argument("--state-dir", type=Path)
    resume_parser.add_argument(
        "--retry-held",
        action="store_true",
        help="보류된 번역 배치를 새 세션에서 다시 요청합니다.",
    )
    resume_parser.add_argument(
        "--parallel",
        type=int,
        default=3,
        help="동시에 처리할 번역 배치 수입니다(1~4, 기본 3).",
    )

    remove_parser = subparsers.add_parser(
        "remove-job", help="작업 데이터는 유지하고 목록에서만 제거합니다."
    )
    remove_parser.add_argument("job_id")
    remove_parser.add_argument("--state-dir", type=Path)

    for command, help_text in (
        ("transcribe", "CUDA 전사를 실행하거나 재개합니다."),
        ("translate", "직접 번역을 실행하거나 재개합니다."),
        ("build", "SRT를 생성하고 검증합니다."),
        ("run", "전사, 번역, 출력 단계를 순서대로 실행합니다."),
    ):
        job_parser = subparsers.add_parser(command, help=help_text)
        _add_job_arguments(job_parser)
        if command == "transcribe":
            job_parser.add_argument("--overwrite", action="store_true")
        if command in {"translate", "run"}:
            job_parser.add_argument(
                "--retry-held",
                action="store_true",
                help="보류된 번역 배치를 새 세션에서 다시 요청합니다.",
            )
            job_parser.add_argument(
                "--parallel",
                type=int,
                default=3,
                help="동시에 처리할 번역 배치 수입니다(1~4, 기본 3).",
            )
    return parser


def _print_progress(message: str) -> None:
    print(message, flush=True)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "gui"
    try:
        if command == "gui":
            from .qml_gui import run_gui

            return run_gui()
        if command == "diagnose":
            results = diagnose(args.video)
            print(json.dumps([item.to_dict() for item in results], ensure_ascii=False, indent=2))
            essentials_ok = all(item.ok for item in results if item.name not in {"codex", "claude"})
            provider_ok = any(item.ok for item in results if item.name in {"codex", "claude"})
            return 0 if essentials_ok and provider_ok else 1
        if command == "login":
            get_provider(args.provider).launch_login()
            print(f"{args.provider} 공식 로그인 창을 열었습니다.")
            return 0
        if command == "status":
            result = get_provider(args.provider).status()
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0 if result.ok else 1
        if command == "jobs":
            registry = JobRegistry(args.state_dir)
            jobs = registry.list_jobs(include_completed=args.all)
            print(
                json.dumps(
                    [item.to_summary() for item in jobs],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if command == "inspect-job":
            inspection = JobRegistry(args.state_dir).inspect_resume(args.job_id)
            print(json.dumps(inspection.to_dict(), ensure_ascii=False, indent=2))
            return 0 if inspection.can_resume else 1
        if command == "remove-job":
            removed = JobRegistry(args.state_dir).remove(args.job_id)
            print(
                f"{removed.code} 작업을 목록에서 제거했습니다. "
                "영상과 체크포인트 파일은 삭제하지 않았습니다."
            )
            return 0
        if command == "resume":
            registry = JobRegistry(args.state_dir)
            record = registry.get(args.job_id)
            if not record.incomplete:
                print(f"{record.code} 작업은 이미 완료되었습니다.")
                return 0
            inspection = registry.inspect_resume(args.job_id)
            if not inspection.can_resume:
                print(json.dumps(inspection.to_dict(), ensure_ascii=False, indent=2))
                return 1
            config = record.job_config()
            paths = JobPaths(config)
            try:
                manifest_data = load_json(paths.manifest)
                current_stage = str(manifest_data.get("stats", {}).get("current_stage", ""))
                status = str(manifest_data.get("status", ""))
                if not paths.raw.is_file() or current_stage == "transcription":
                    transcribe(config, progress=_print_progress)
                    registry.upsert(config)
                try:
                    decision_data = (
                        load_json(paths.decisions) if paths.decisions.is_file() else {}
                    )
                except (OSError, ValueError, json.JSONDecodeError):
                    decision_data = {}
                translation_incomplete = status in {
                    "TRANSLATING",
                    "TRANSLATION_BLOCKED",
                    "TRANSLATION_PARTIAL",
                }
                if (
                    not paths.decisions.is_file()
                    or not decision_data
                    or decision_data.get("partial")
                    or current_stage == "translation"
                    or translation_incomplete
                ):
                    translate(
                        config,
                        progress=_print_progress,
                        retry_held=args.retry_held,
                        parallel_batches=max(1, min(getattr(args, "parallel", 3), 4)),
                    )
                    registry.upsert(config)
                result = build_and_validate(config)
                print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
                return 0
            finally:
                registry.upsert(config)

        config = _config_from_args(args)
        registry = JobRegistry()
        registry.upsert(config)
        try:
            if command == "transcribe":
                print(transcribe(config, overwrite=args.overwrite, progress=_print_progress))
            elif command == "translate":
                decisions = translate(
                    config,
                    progress=_print_progress,
                    retry_held=args.retry_held,
                    parallel_batches=max(1, min(getattr(args, "parallel", 3), 4)),
                )
                print(f"번역 결정 {len(decisions)}개를 저장했습니다.")
            elif command == "build":
                print(
                    json.dumps(build_and_validate(config).to_dict(), ensure_ascii=False, indent=2)
                )
            elif command == "run":
                transcribe(config, progress=_print_progress)
                registry.upsert(config)
                translate(
                    config,
                    progress=_print_progress,
                    retry_held=args.retry_held,
                    parallel_batches=max(1, min(getattr(args, "parallel", 3), 4)),
                )
                registry.upsert(config)
                print(
                    json.dumps(build_and_validate(config).to_dict(), ensure_ascii=False, indent=2)
                )
            else:
                parser.error(f"알 수 없는 명령입니다: {command}")
            return 0
        finally:
            registry.upsert(config)
    except KeyboardInterrupt:
        print("사용자 요청으로 작업을 중단했습니다.", file=sys.stderr)
        return 130
    except BaseException as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

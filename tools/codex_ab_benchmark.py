"""Developer A/B benchmark: ephemeral `codex exec` per batch vs one resumed session.

Nothing is sent to Codex unless --live is given. Example:

    py -3.14 tools/codex_ab_benchmark.py --raw <job>/.subtitle_job/<CODE>_merged_raw_ja.json \
        --count 40 --batch-size 20 --out bench --live

Both arms use the same raw segments, prompts, model, reasoning effort, speed mode and
JSON Schema. Output: bench/ab_<timestamp>.json and .tsv with total time, time to first
event, token usage, repair count, raw_id validation and observed service tier
("unavailable" when the CLI does not report it).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from local_subtitle_tool.providers import CodexProvider, ProviderError  # noqa: E402
from local_subtitle_tool.translator import (  # noqa: E402
    TRANSLATION_SCHEMA,
    _make_prompt,
    _partition_items,
)


def run_arm(
    *,
    arm: str,
    segments: list[dict],
    batch_size: int,
    model: str,
    effort: str,
    speed: str,
    work: Path,
) -> list[dict]:
    provider = CodexProvider()
    schema = work / "schema.json"
    schema.write_text(json.dumps(TRANSLATION_SCHEMA, ensure_ascii=False), encoding="utf-8")
    session_id: str | None = None
    rows: list[dict] = []
    for index in range(0, len(segments), batch_size):
        ids = [int(item["id"]) for item in segments[index : index + batch_size]]
        prompt = _make_prompt(
            code="BENCH", all_segments=segments, target_ids=ids, context_size=8
        )
        started = time.monotonic()
        row: dict = {"arm": arm, "batch": index // batch_size, "targets": len(ids)}
        try:
            result = provider.run(
                prompt=prompt,
                model=model,
                reasoning_effort=effort,
                speed_mode=speed,
                schema_path=schema,
                output_path=work / f"{arm}_{index}.json",
                workdir=work,
                session_id=session_id if arm == "session" else None,
                persistent=arm == "session",
            )
            valid, problems = _partition_items(result.payload, ids)
            if arm == "session":
                session_id = result.session_id
            row.update(result.metrics)
            row.update(
                {
                    "outcome": "success",
                    "valid_ids": len(valid),
                    "invalid_or_missing": len(ids) - len(valid),
                    "problems": "; ".join(problems)[:300],
                }
            )
        except ProviderError as exc:
            row.update(exc.metrics)
            row.update({"outcome": exc.kind.value, "problems": str(exc)[:300]})
        row["wall_seconds"] = round(time.monotonic() - started, 3)
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--effort", default="xhigh")
    parser.add_argument("--speed", choices=("standard", "fast"), default="fast")
    parser.add_argument("--out", type=Path, default=Path("bench"))
    parser.add_argument("--live", action="store_true", help="실제로 Codex를 호출합니다.")
    args = parser.parse_args()

    raw = json.loads(args.raw.read_text(encoding="utf-8"))
    segments = sorted(raw.get("segments", []), key=lambda item: int(item["id"]))[: args.count]
    batches = (len(segments) + args.batch_size - 1) // args.batch_size
    print(
        f"계획: 세그먼트 {len(segments)}개, 배치 {batches}개 x 2 방식(ephemeral, session), "
        f"모델 {args.model}/{args.effort}/{args.speed}"
    )
    if not args.live:
        print("--live 없이 실행되어 실제 Codex 호출을 하지 않았습니다.")
        return 2
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    work = args.out / stamp
    work.mkdir(parents=True, exist_ok=True)
    rows = []
    for arm in ("ephemeral", "session"):
        rows += run_arm(
            arm=arm,
            segments=segments,
            batch_size=args.batch_size,
            model=args.model,
            effort=args.effort,
            speed=args.speed,
            work=work,
        )
    (args.out / f"ab_{stamp}.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    fields = sorted({key for row in rows for key in row})
    with (args.out / f"ab_{stamp}.tsv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    for arm in ("ephemeral", "session"):
        arm_rows = [row for row in rows if row["arm"] == arm]
        total = sum(row["wall_seconds"] for row in arm_rows)
        print(f"{arm}: 총 {total:.1f}초, 배치 {len(arm_rows)}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

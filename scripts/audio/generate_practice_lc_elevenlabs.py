#!/usr/bin/env python3
"""ATA 1.4.5 연습문제practice 듣기 생성기 — ElevenLabs.

`연습문제practice` 탭의 `nass+`와 `L/C` 행을 실시간으로 읽는다. 시트에
정식 ID가 없는 동안에는 행 번호를 포함한 안정적인 ID를 사용한다.

  nass+  -> practice_nassplus_r274.mp3
  L/C    -> practice_lc_r304.mp3

파일은 일반 제품 오디오와 같은 `audio/{U}/{id}.mp3` 경로에 저장·업로드한다.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
import time
import urllib.error
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
PRACTICE_TAB = "연습문제practice"
TARGET_TYPES = {"nass+", "l/c"}

_spec = importlib.util.spec_from_file_location(
    "nass_gen", SCRIPT_DIR / "generate_145_nass_elevenlabs.py"
)
nass_gen = importlib.util.module_from_spec(_spec)
sys.modules["nass_gen"] = nass_gen
_spec.loader.exec_module(nass_gen)


def practice_id(row: dict) -> str:
    explicit = nass_gen.row_value(row, "id").strip()
    if explicit:
        return explicit
    row_number = nass_gen.row_value(row, "_row_number")
    typ = nass_gen.normalize_type(nass_gen.row_value(row, "type"))
    prefix = "practice_nassplus" if typ == "nass+" else "practice_lc"
    return f"{prefix}_r{row_number}" if row_number else ""


def practice_lines(row: dict) -> list[str]:
    text = nass_gen.row_value(row, "tss", "arabic")
    lines = nass_gen.split_lines(text)
    typ = nass_gen.normalize_type(nass_gen.row_value(row, "type"))
    if typ != "nass+" or len(lines) != 1:
        return lines

    parts = [part.strip() for part in re.split(r"(?<=؟)", lines[0]) if part.strip()]
    if len(parts) >= 2:
        return [parts[0], " ".join(parts[1:])]

    parts = [part.strip() for part in re.split(r"(?<=[.!])\s*", lines[0]) if part.strip()]
    return parts if len(parts) >= 2 else lines


def collect_rows(units: set[str], ids: set[str], types: set[str]) -> list[dict]:
    normalized_units = {nass_gen.normalize_unit(value) for value in units if value}
    rows = []
    for row in nass_gen.read_sheet_csv(PRACTICE_TAB):
        typ = nass_gen.normalize_type(nass_gen.row_value(row, "type"))
        unit = nass_gen.normalize_unit(nass_gen.row_value(row, "U", "u"))
        rid = practice_id(row)
        if typ not in TARGET_TYPES or not unit or not rid:
            continue
        if normalized_units and unit not in normalized_units:
            continue
        if ids and rid not in ids:
            continue
        if types and typ not in types:
            continue
        row["_type"] = typ
        row["_unit"] = unit
        row["_id"] = rid
        rows.append(row)
    return rows


def build_inputs(row: dict, resolved: dict[str, str], args) -> tuple[list[dict], list[str]]:
    lines = practice_lines(row)
    genders = nass_gen.speaker_genders(row, lines)
    dialect = nass_gen.normalize_lahja(nass_gen.row_value(row, "lahja"))
    inputs, labels, used = [], [], set()
    for idx, line in enumerate(lines):
        if args.add_final_sukun:
            line = nass_gen.add_pause_sukun(line)
        if args.ta_marbuta_as_ha:
            line = nass_gen.ta_marbuta_pause_as_ha(line)
        gender = genders[idx]
        spec = nass_gen.select_voice_spec(
            gender,
            dialect,
            f"{row['_id']}:{idx}:{gender}:{dialect}",
            resolved,
            exclude=used,
        )
        used.add(spec.label)
        labels.append(f"{gender}:{spec.label}")
        inputs.append({
            "text": nass_gen.with_style_tags(line),
            "voice_id": resolved[spec.label],
        })
    return inputs, labels


def main() -> int:
    nass_gen.load_dotenv(SCRIPT_DIR / ".env")
    nass_gen.load_dotenv(PROJECT_ROOT / ".env")

    parser = argparse.ArgumentParser(description="연습문제practice 듣기 생성 (ElevenLabs)")
    parser.add_argument("--unit", action="append", default=[], help="유닛 필터. 반복 지정 가능.")
    parser.add_argument("--id", action="append", default=[], help="정확한 연습 오디오 ID.")
    parser.add_argument("--type", action="append", choices=["nass+", "l/c"], default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", "--force", action="store_true")
    parser.add_argument("--missing-only", action="store_true")
    parser.add_argument("--upload-existing", action="store_true")
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "audio"))
    parser.add_argument("--model", default=nass_gen.ELEVEN_MODEL)
    parser.add_argument("--output-format", default=nass_gen.ELEVEN_OUTPUT_FORMAT)
    parser.add_argument("--language-code", default=nass_gen.ELEVEN_LANGUAGE_CODE)
    parser.add_argument("--sleep", type=float, default=0.5)
    parser.add_argument("--add-final-sukun", action="store_true")
    parser.add_argument("--ta-marbuta-as-ha", action="store_true")
    args = parser.parse_args()

    if not args.unit and not args.id:
        print("유닛 또는 ID를 지정하세요. 예: --unit A010")
        return 2

    api_key = nass_gen.elevenlabs_api_key()
    if not api_key:
        print("ElevenLabs 키가 환경변수·.env·Keychain에 없습니다.")
        return 1

    rows = collect_rows(set(args.unit), set(args.id), set(args.type))
    output_root = Path(args.output_dir).expanduser()
    if not output_root.is_absolute():
        output_root = Path.cwd() / output_root

    token = "" if args.local_only else nass_gen.gcloud_token()
    existing_gcs = set() if args.local_only or not token else nass_gen.gcs_existing(
        token, {row["_unit"] for row in rows}
    )
    selected = []
    for row in rows:
        local_path = output_root / row["_unit"] / f"{row['_id']}.mp3"
        gcs_name = nass_gen.gcs_object_name(row["_unit"], f"{row['_id']}.mp3")
        row["_local_path"] = str(local_path)
        exists_local, exists_gcs = local_path.exists(), gcs_name in existing_gcs
        if args.upload_existing:
            if exists_local and (args.overwrite or not exists_gcs):
                selected.append(row)
        elif args.overwrite or not (exists_local or exists_gcs):
            selected.append(row)
        elif args.missing_only:
            continue
    rows = selected

    voices = nass_gen.fetch_eleven_voices(api_key)
    resolved = nass_gen.resolve_voice_ids(voices)
    print(f"sheet={nass_gen.SHEET_ID} tab={PRACTICE_TAB}")
    print(f"target=gs://{nass_gen.GCS_BUCKET}/{nass_gen.GCS_AUDIO_ROOT}/{{unit}}/")
    print(f"rows={len(rows)}")

    done = fail = 0
    for row in rows:
        unit, rid = row["_unit"], row["_id"]
        path = Path(row["_local_path"])
        try:
            if args.upload_existing:
                audio = path.read_bytes()
                labels = ["cached"]
                inputs = []
            else:
                inputs, labels = build_inputs(row, resolved, args)
                if not inputs:
                    raise RuntimeError("빈 TTS 입력")
                audio = b"" if args.dry_run else nass_gen.elevenlabs_dialogue(api_key, inputs, args)
            preview = " / ".join(item["text"] for item in inputs)
            print(f"- {unit}/{rid} row={row.get('_row_number')} type={row['_type']} voices={', '.join(labels)}")
            if preview:
                print(f"  {preview}")
            if args.dry_run:
                continue
            nass_gen.validate_mp3_bytes(audio)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not args.upload_existing:
                path.write_bytes(audio)
            if args.local_only:
                print(f"OK   {path}")
            else:
                if not token:
                    raise RuntimeError("GCS 업로드용 gcloud 인증이 없습니다.")
                url = nass_gen.upload_to_gcs(token, unit, f"{rid}.mp3", audio)
                print(f"OK   {url}")
            done += 1
            time.sleep(args.sleep)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            print(f"FAIL {unit}/{rid} HTTP {exc.code}: {body[:500]}")
            fail += 1
        except Exception as exc:
            print(f"FAIL {unit}/{rid} {type(exc).__name__}: {exc}")
            fail += 1
    print(f"done={done} fail={fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

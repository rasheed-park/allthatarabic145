#!/usr/bin/env python3
"""Generate ATA 1.4.5 learning audio from the unified Google Sheet.

Default use is deliberately narrow and safe:

  python3 scripts/audio/generate_145_audio.py --unit A001 --types rep,drill,expi,exp --dry-run
  python3 scripts/audio/generate_145_audio.py --unit A001 --types rep,drill,expi,exp --local-only

The app contract is:
  https://storage.googleapis.com/all-that-arabic-145/audio/{U}/{id}.wav
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import wave
from array import array
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

try:
    from google.api_core import exceptions as google_exceptions
    from google.cloud import texttospeech
except Exception:  # pragma: no cover - dry-run and upload-existing do not need TTS.
    google_exceptions = None
    texttospeech = None

try:
    from google.cloud import storage
except Exception:  # pragma: no cover - local-only generation can still work.
    storage = None

try:
    from google.auth import credentials as google_credentials
except Exception:  # pragma: no cover - dry-run does not need credentials.
    google_credentials = None


SHEET_ID = "1cFamlN6FjnIiRLTBl3OsAPbHTPiQYKJUio4caR-7Lm4"
DATA_TAB = "데이터시트sheet"
WORD_TAB = "단어장kalimat"
DEFAULT_TYPES = ("rep", "drill", "expi", "exp")
TYPE_ALIASES = {
    "dri": "drill",
    "dri+": "drill+",
    "kalimat": "kal",
    "kalimat+": "kal+",
    "nass": "nas",
    "nass+": "nas+",
}
DEFAULT_STATUSES = ("confirmed",)
DEFAULT_BUCKET = "all-that-arabic-145"
DEFAULT_VOICE_F = "ar-XA-Chirp3-HD-Zephyr"
# Male voice is intentionally conservative. Override with --voice-male once chosen.
DEFAULT_VOICE_M = "ar-XA-Chirp3-HD-Charon"
TARGET_SAMPLE_RATE = 24000
DEFAULT_GCLOUD_ACCOUNT = "rasheed.park@markazarabic.com"
DEFAULT_QUOTA_PROJECT = "all-that-arabic145"


class GcloudCliCredentials(google_credentials.Credentials if google_credentials else object):
    """Use the existing gcloud login for TTS and GCS without a second ADC login."""

    def __init__(self, account: str, quota_project: str):
        if google_credentials is None:
            raise RuntimeError("google-auth is unavailable")
        super().__init__()
        self.account = account
        self._quota_project_id = quota_project
        self.refresh(None)

    @property
    def quota_project_id(self) -> str:
        return self._quota_project_id

    def refresh(self, request) -> None:  # noqa: ARG002 - Google auth requires this signature.
        result = subprocess.run(
            ["gcloud", "auth", "print-access-token", "--account", self.account],
            check=False,
            capture_output=True,
            text=True,
        )
        token = result.stdout.strip()
        if result.returncode or not token:
            detail = result.stderr.strip() or "access token unavailable"
            raise RuntimeError(f"gcloud authentication failed for {self.account}: {detail}")
        self.token = token
        # gcloud access tokens last about one hour. Refresh before a long batch can expire.
        # google-auth's installed version compares naive UTC timestamps internally.
        self.expiry = datetime.utcnow() + timedelta(minutes=50)


@dataclass(frozen=True)
class AudioRow:
    unit: str
    typ: str
    status: str
    audio_id: str
    arabic: str
    tss: str
    note: str
    lahja: str
    voi: str
    css: str
    ref_ptn: str

    @property
    def tts_text(self) -> str:
        override = tss_override_text(self.tss)
        raw = override if override is not None else self.arabic
        return normalize_tts_text(raw)

    def voice_gender(self) -> str:
        voi = (self.voi or "").strip().upper()
        return "M" if voi == "M" else "F"


def normalize_tts_text(text: str) -> str:
    text = str(text or "")
    text = text.replace("*", "").replace("_", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


PASS_TSS_VALUES = {
    "✓",
    "✔",
    "✅",
    "☑",
    "통과",
    "ok",
    "pass",
    "passed",
    "o",
    "v",
    "완료",
}


def is_pass_tss(value: str) -> bool:
    value = str(value or "").strip()
    if not value:
        return False
    return value.lower() in PASS_TSS_VALUES


def has_arabic(text: str) -> bool:
    return bool(re.search(r"[\u0600-\u06ff]", str(text or "")))


def has_hangul(text: str) -> bool:
    return bool(re.search(r"[가-힣]", str(text or "")))


def tss_override_text(value: str) -> str | None:
    value = str(value or "").strip()
    if not value or is_pass_tss(value):
        return None
    if value.lower() in {"0", "1", "true", "false", "yes", "no"}:
        return None
    if value.lower().startswith("tts:"):
        return value.split(":", 1)[1].strip()
    if value.lower().startswith("tts="):
        return value.split("=", 1)[1].strip()
    if has_arabic(value) and not has_hangul(value):
        return value
    return None


def is_tss_review_memo(value: str) -> bool:
    value = str(value or "").strip()
    if not value or is_pass_tss(value):
        return False
    if tss_override_text(value) is not None:
        return False
    return True


def parse_gviz_json(raw: str) -> dict:
    match = re.search(r"setResponse\((.*)\);?\s*$", raw, re.S)
    if not match:
        raise ValueError("Google gviz response did not contain setResponse(...).")
    return json.loads(match.group(1))


def fetch_sheet_rows(sheet_id: str = SHEET_ID, tab: str = DATA_TAB) -> list[list[str]]:
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq"
        f"?tqx=out:json&sheet={urllib.parse.quote(tab)}&headers=1"
    )
    with urllib.request.urlopen(url, timeout=40) as response:
        payload = parse_gviz_json(response.read().decode("utf-8"))
    table = payload["table"]
    rows: list[list[str]] = []
    width = len(table.get("cols", []))
    for row in table.get("rows", []):
        values = []
        for cell in row.get("c", []):
            values.append("" if cell is None else str(cell.get("f", cell.get("v", ""))))
        values += [""] * (width - len(values))
        rows.append(values)
    return rows


def collect_audio_rows(
    unit: str,
    types: Iterable[str],
    statuses: Iterable[str],
    ids: set[str] | None = None,
) -> list[AudioRow]:
    unit = unit.upper()
    types_set = {TYPE_ALIASES.get(t.strip(), t.strip()) for t in types if t.strip()}
    statuses_set = {s.strip() for s in statuses if s.strip()}
    ids = ids or set()
    result: list[AudioRow] = []

    # 레퍼토리·드릴·지문은 데이터시트, 표현·단어는 단어장이 원본이다.
    # 같은 표현이 레거시 통합 탭에도 남아 있으므로 타입별 원본을 고정해
    # 중복 음원 생성과 오래된 문구 재생성을 막는다.
    source_tabs: dict[str, set[str]] = {}
    for row_type in types_set:
        tab = WORD_TAB if row_type in {"expi", "exp", "exp+", "kal", "kal+"} else DATA_TAB
        source_tabs.setdefault(tab, set()).add(row_type)

    for tab, tab_types in source_tabs.items():
        for values in fetch_sheet_rows(tab=tab):
            # Current data-source indexes:
            # C=0, U=1, ptn=2, arabic=3, korean=4, note=5, type=6, status=7,
            # id=8, lahja=9, voi=10, tss=11, css=12, ref_ptn=13.
            row_unit = values[1].strip()
            row_type = values[6].strip()
            status = values[7].strip()
            audio_id = values[8].strip()
            if row_unit != unit:
                continue
            if row_type not in tab_types:
                continue
            if statuses_set and status not in statuses_set:
                continue
            if ids and audio_id not in ids:
                continue
            result.append(
                AudioRow(
                    unit=row_unit,
                    typ=row_type,
                    status=status,
                    audio_id=audio_id,
                    arabic=values[3].strip(),
                    tss=values[11].strip(),
                    note=values[5].strip(),
                    lahja=values[9].strip(),
                    voi=values[10].strip(),
                    css=values[12].strip(),
                    ref_ptn=values[13].strip(),
                )
            )
    return result


def validate_rows(rows: list[AudioRow]) -> None:
    errors: list[str] = []
    seen: dict[tuple[str, str], AudioRow] = {}
    for row in rows:
        if not row.audio_id:
            errors.append(f"{row.unit} {row.typ} has empty id: {row.arabic!r}")
        if not row.tts_text:
            errors.append(f"{row.unit}/{row.audio_id} has empty TTS text")
        key = (row.unit, row.audio_id)
        if key in seen:
            errors.append(f"duplicate (U,id): {row.unit}/{row.audio_id}")
        seen[key] = row
        if row.voi and row.voi.strip().upper() not in {"M", "F"}:
            errors.append(f"{row.unit}/{row.audio_id} has unsupported voi={row.voi!r}")
        if is_tss_review_memo(row.tss):
            errors.append(f"{row.unit}/{row.audio_id} has tss review memo: {row.tss!r}")
    if errors:
        raise SystemExit("Validation failed:\n- " + "\n- ".join(errors))


def audio_target(output_dir: Path, row: AudioRow) -> Path:
    return output_dir / row.unit / f"{row.audio_id}.wav"


def synthesize_wav(
    client: texttospeech.TextToSpeechClient,
    row: AudioRow,
    target: Path,
    voice_f: str,
    voice_m: str,
    speaking_rate: float,
    retries: int = 4,
) -> None:
    if texttospeech is None or google_exceptions is None:
        raise RuntimeError(
            "Google Cloud Text-to-Speech dependencies are unavailable. "
            "Install google-cloud-texttospeech to synthesize audio."
        )
    voice_name = voice_m if row.voice_gender() == "M" else voice_f
    voice = texttospeech.VoiceSelectionParams(language_code="ar-XA", name=voice_name)
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.LINEAR16,
        sample_rate_hertz=TARGET_SAMPLE_RATE,
        speaking_rate=speaking_rate,
    )
    synthesis_input = texttospeech.SynthesisInput(text=row.tts_text)

    for attempt in range(retries):
        try:
            response = client.synthesize_speech(
                input=synthesis_input,
                voice=voice,
                audio_config=audio_config,
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(response.audio_content)
            return
        except (google_exceptions.TooManyRequests, google_exceptions.ServiceUnavailable, google_exceptions.InternalServerError) as exc:
            if attempt == retries - 1:
                raise
            wait = 1.5 * (2**attempt)
            print(f"retry {attempt + 1}/{retries - 1} for {row.audio_id}: {exc}; sleep {wait:.1f}s", file=sys.stderr)
            time.sleep(wait)


def add_fade_in(path: Path, milliseconds: int = 20) -> None:
    if milliseconds <= 0:
        return
    with wave.open(str(path), "rb") as wav:
        params = wav.getparams()
        frames = wav.readframes(wav.getnframes())
    if params.sampwidth != 2:
        return
    samples = array("h")
    samples.frombytes(frames)
    if sys.byteorder != "little":
        samples.byteswap()
    fade_samples = min(len(samples), int(params.framerate * milliseconds / 1000) * params.nchannels)
    if fade_samples <= 0:
        return
    for i in range(fade_samples):
        samples[i] = int(samples[i] * (i / fade_samples))
    if sys.byteorder != "little":
        samples.byteswap()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with wave.open(str(tmp_path), "wb") as out:
            out.setparams(params)
            out.writeframes(samples.tobytes())
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def inspect_wav(path: Path) -> dict:
    with wave.open(str(path), "rb") as wav:
        frames = wav.getnframes()
        rate = wav.getframerate()
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        duration = frames / float(rate or 1)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "path": str(path),
        "duration": round(duration, 3),
        "sample_rate": rate,
        "channels": channels,
        "sample_width": sample_width,
        "sha256": digest,
        "bytes": path.stat().st_size,
    }


def validate_wav_file(path: Path) -> None:
    meta = inspect_wav(path)
    errors = []
    if meta["duration"] <= 0:
        errors.append("zero duration")
    if meta["sample_rate"] != TARGET_SAMPLE_RATE:
        errors.append(f"sample_rate={meta['sample_rate']}")
    if meta["channels"] != 1:
        errors.append(f"channels={meta['channels']}")
    if meta["sample_width"] != 2:
        errors.append(f"sample_width={meta['sample_width']}")
    if errors:
        raise ValueError(f"{path} failed WAV validation: {', '.join(errors)}")


def upload_to_gcs(bucket_name: str, source: Path, row: AudioRow, overwrite: bool, credentials) -> str:
    if storage is None:
        raise RuntimeError("google-cloud-storage is unavailable")
    client = storage.Client(credentials=credentials)
    bucket = client.bucket(bucket_name)
    blob_name = f"audio/{row.unit}/{row.audio_id}.wav"
    blob = bucket.blob(blob_name)
    if_generation_match = None if overwrite else 0
    blob.upload_from_filename(
        str(source),
        content_type="audio/wav",
        if_generation_match=if_generation_match,
    )
    blob.cache_control = "public, max-age=3600"
    blob.patch()
    return f"https://storage.googleapis.com/{bucket_name}/{blob_name}"


def write_manifest(manifest_path: Path, rows: list[dict]) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "unit",
        "id",
        "type",
        "status",
        "voice",
        "text",
        "path",
        "duration",
        "sample_rate",
        "channels",
        "sample_width",
        "bytes",
        "sha256",
        "gcs_url",
    ]
    with manifest_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", required=True, help="Unit code such as A001. Required by design.")
    parser.add_argument("--types", default=",".join(DEFAULT_TYPES), help="Comma-separated types, e.g. rep,drill,expi,exp")
    parser.add_argument("--statuses", default=",".join(DEFAULT_STATUSES), help="Comma-separated statuses")
    parser.add_argument("--id", action="append", dest="ids", help="Limit to one id; repeatable")
    parser.add_argument("--dry-run", action="store_true", help="Print target rows without generating audio")
    parser.add_argument("--local-only", action="store_true", help="Generate local WAV files without uploading")
    parser.add_argument("--upload-existing", action="store_true", help="Upload existing local WAV files only")
    parser.add_argument("--upload", action="store_true", help="Generate missing local WAV files, then upload")
    parser.add_argument("--overwrite", action="store_true", help="Allow overwriting GCS objects")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--output-dir", default="audio")
    parser.add_argument("--manifest", default="audio/manifest_145_audio.csv")
    parser.add_argument("--voice-female", default=DEFAULT_VOICE_F)
    parser.add_argument("--voice-male", default=DEFAULT_VOICE_M)
    parser.add_argument("--gcloud-account", default=DEFAULT_GCLOUD_ACCOUNT,
                        help="Existing gcloud login used for both TTS and GCS; no ADC login required.")
    parser.add_argument("--quota-project", default=DEFAULT_QUOTA_PROJECT,
                        help="Google Cloud project billed for TTS requests.")
    parser.add_argument("--speaking-rate", type=float, default=1.0, help="TTS speaking rate, e.g. 0.7 for slow drills")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    modes = [args.dry_run, args.local_only, args.upload_existing, args.upload]
    if sum(bool(m) for m in modes) != 1:
        raise SystemExit("Choose exactly one mode: --dry-run, --local-only, --upload-existing, or --upload")

    rows = collect_audio_rows(
        unit=args.unit,
        types=args.types.split(","),
        statuses=args.statuses.split(","),
        ids=set(args.ids or []),
    )
    validate_rows(rows)
    if not rows:
        raise SystemExit("No matching rows.")

    output_dir = Path(args.output_dir)
    manifest_records: list[dict] = []
    if not (args.dry_run or args.upload_existing) and texttospeech is None:
        raise SystemExit(
            "Google Cloud Text-to-Speech dependencies are unavailable. "
            "Install google-cloud-texttospeech or use --dry-run/--upload-existing."
        )
    needs_cloud_credentials = args.upload or args.upload_existing or args.local_only
    credentials = GcloudCliCredentials(args.gcloud_account, args.quota_project) if needs_cloud_credentials else None
    tts_client = None if (args.dry_run or args.upload_existing) else texttospeech.TextToSpeechClient(credentials=credentials)

    print(f"rows={len(rows)} unit={args.unit.upper()} types={args.types} statuses={args.statuses}")
    for row in rows:
        voice = args.voice_male if row.voice_gender() == "M" else args.voice_female
        target = audio_target(output_dir, row)
        print(
            f"{row.unit}\t{row.typ}\t{row.status}\t{row.audio_id}\t{row.voice_gender()}\t"
            f"{voice}\trate={args.speaking_rate}\t{target}\t{row.tts_text}"
            + (" [tss:pass]" if is_pass_tss(row.tss) else "")
            + (" [tss:override]" if tss_override_text(row.tss) is not None else "")
            + (" [exists]" if target.exists() and not args.upload_existing else ""),
            flush=True,
        )
        if args.dry_run:
            continue

        if args.upload_existing:
            if not target.exists():
                raise SystemExit(f"missing local file for upload-existing: {target}")
        elif not target.exists() or args.upload:
            assert tts_client is not None
            synthesize_wav(tts_client, row, target, args.voice_female, args.voice_male, args.speaking_rate)
            add_fade_in(target)

        validate_wav_file(target)
        meta = inspect_wav(target)
        gcs_url = ""
        if args.upload or args.upload_existing:
            gcs_url = upload_to_gcs(args.bucket, target, row, overwrite=args.overwrite, credentials=credentials)
        manifest_records.append(
            {
                "unit": row.unit,
                "id": row.audio_id,
                "type": row.typ,
                "status": row.status,
                "voice": voice,
                "text": row.tts_text,
                "path": str(target),
                "gcs_url": gcs_url,
                **meta,
            }
        )

    if manifest_records:
        write_manifest(Path(args.manifest), manifest_records)
        print(f"manifest={args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

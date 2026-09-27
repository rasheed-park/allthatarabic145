#!/usr/bin/env python3
"""
ATA 1.4.5 nass audio generator using ElevenLabs.

Reads nass rows from the 1.4.5 `나쓰nass` tab, creates one dialogue MP3
per nass with ElevenLabs, writes local audio/{U}/{id}.mp3, and uploads
to gs://all-that-arabic-15/audio/{U}/{id}.mp3 (1.4.5 has its own bucket).

Speaker voice placement (M/F per line) is driven by the `seq` column
(e.g. "MF", "FM", "MM", "MFM"), one char per dialogue line. This makes
the audio one file per nass while still assigning correct-gender voices.

API keys must come from ELEVENLABS_API_KEY in the environment or from
archive/ata-audio-pipeline/.env. Do not hard-code keys in this file.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from io import StringIO
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent

SHEET_ID = os.getenv("SHEET_ID_145", "1cFamlN6FjnIiRLTBl3OsAPbHTPiQYKJUio4caR-7Lm4")
# 1.4.5 is a separate product with its own bucket. Use a 145-specific env
# var so the shared .env GCS_BUCKET_NAME (=14 bucket) does not override it.
GCS_BUCKET = os.getenv("GCS_BUCKET_NAME_145", "all-that-arabic-145")
GCS_AUDIO_ROOT = os.getenv("GCS_AUDIO_ROOT_145", "audio")
NASS_TAB = os.getenv("NASS_TAB_145", "데이터시트sheet")

ELEVEN_API_BASE = "https://api.elevenlabs.io"
ELEVEN_MODEL = os.getenv("ELEVENLABS_NASS_MODEL", "eleven_v3")
ELEVEN_OUTPUT_FORMAT = os.getenv("ELEVENLABS_NASS_OUTPUT_FORMAT", "mp3_44100_128")
ELEVEN_STYLE_TAGS = os.getenv("ELEVENLABS_NASS_STYLE_TAGS", "[naturally] [correctly]")
ELEVEN_LANGUAGE_CODE = os.getenv("ELEVENLABS_NASS_LANGUAGE_CODE", "ar")
ELEVENLABS_KEYCHAIN_SERVICE = "ATA145_ELEVENLABS_API_KEY"

TARGET_TYPES = {"nass", "nass+"}

VOWEL_MARKS = "ًٌٍَُِٰ"
SHADDA = "ّ"
SUKUN = "ْ"
ALL_DIACRITICS = VOWEL_MARKS + SHADDA + SUKUN
BOUNDARY_CHARS = r"\s،؛؟.,!?;:"


@dataclass(frozen=True)
class VoiceSpec:
    label: str
    aliases: tuple[str, ...]
    gender: str
    dialects: tuple[str, ...]
    weight: int = 1


VOICE_SPECS = [
    VoiceSpec("Faisal Ali", ("Faisal Ali", "Faisal"), "M", ("msa_gulf",), 5),
    VoiceSpec("Adeeb", ("Adeeb - Clear, Confident and Natural", "Adeeb"), "M", ("msa_gulf",), 1),
    VoiceSpec("Jeddawi", ("Jeddawi",), "M", ("msa_gulf",), 1),
    # Hijazi 제외 — 걸프 대사에 이집트풍으로 들린다는 피드백 (2026-07)
    VoiceSpec("Ghawi", ("Ghawi",), "M", ("msa_gulf",), 1),
    VoiceSpec("Hadi N", ("Hadi N", "Hadi"), "M", ("levant",), 1),
    VoiceSpec(
        "Heba Mansuri – Arabic Customer Care",
        ("Heba Mansuri – Arabic Customer Care", "Heba Mansuri - Arabic Customer Care"),
        "F",
        ("msa_gulf",),
        5,
    ),
    VoiceSpec("Salma", ("Salma - Friendly, Clear and Reassuring", "Salma"), "F", ("msa_gulf",), 3),
    # Noura 제외 — 목소리 부적합 피드백 (2026-07)
    VoiceSpec("Farah", ("Farah",), "F", ("levant",), 5),
    VoiceSpec(
        "Sara-soft, calm and gentle",
        ("Sara-soft, calm and gentle", "Sara soft calm and gentle", "Sara"),
        "F",
        ("levant",),
        1,
    ),
    VoiceSpec("Lina", ("Lina",), "F", ("levant",), 1),
    VoiceSpec("Laloosh-warm finance", ("Laloosh-warm finance", "Laloosh"), "F", ("levant",), 1),
    VoiceSpec("Refoush", ("Rafoush - Young Relatable Customer Care", "Rafoush", "Refoush"), "F", ("levant",), 1),
    VoiceSpec(
        "Maged Magdy",
        ("Maged Magdy - Calm, Natural and Balanced", "Maged Magdy"),
        "M",
        ("egypt",),
        1,
    ),
    VoiceSpec(
        "Heba-Egyptian",
        ("Heba - Soothing and Gracious", "Heba"),
        "F",
        ("egypt",),
        1,
    ),
]

# Use this when the automatic gender guess is too fuzzy.
SPEAKER_OVERRIDES = {
    "nass_lissa-nayma-indiki": "M,F",
    "nass_shu-sar-lesh": "M,F",
    "nass_khalas-ana-tala": "F,M",
    "nass_esh-hadha-lesh": "M,F",
    "nass_lahza-ana-nazla": "F,M",
    "nass_inta-fi-al-maqha": "F,M",
    "nass_shu-sar-shfij": "M,F",
    "nass_inti-juana-ktir": "M,F",
    "nass_uskut-shuwayy-allah": "F,M",
    "nass_iftah-ash-shubbak-allah": "F,M",
    "nass_iftah-as-sunduq-fih": "M,F",
    "nass_jib-li-qahwa": "F,M",
    "nass_imshi-ala-mahlik": "M,F",
    "nass_wish-amal-khalid": "M,M",
}

# Retry a repeatedly mispronounced dialogue with a different configured voice
# pair rather than reproducing the same deterministic voice selection.
VOICE_LABEL_OVERRIDES: dict[str, tuple[str, ...]] = {
    "nass_hal-huwa-amriikii-laa-huwa-kuurii": ("Faisal Ali", "Salma"),
}

# TTS-only overrides. Prefer sheet `tss` for durable content edits.
PRONUNCIATION_OVERRIDES: dict[str, str] = {
    "nass_ahlan-inti-raaniyaa-ah-anaa-raaniyaa": "أَهْلًا، إِنْتِ رَانِيَا؟\nأَهْ... أَنَا رَانِيَا. أَهْلَيْنْ.",
    "nass_shuu-hhaalich-kefik-al-hhamdu-lillaah": "شُو حَالِچ؟ كَيْفِك؟\nالحَمْدُ لِلّٰهِ، أَنَا زَيْنَة. وَإِنْتَ كَيْفَك؟",
    "nass_huwa-mahhmuud-wa-huwa-maahir-mumtaaz": "هُوَ مَحْمُودْ، وَهُوَ مَاهِرْ.\nمُمْتَازْ. هُوَ خَبِيرْ.",
    "nass_inti-jadiida-hinii-ii-anaa-jadiida": "إِنْتِ جَدِيدَهْ هِنِي؟\nإِي، أَنَا جَدِيدَهْ هِنِي. أَنَا نَادِيَهْ.",
    "nass_halaa-ahhmad-hinii-ii-huwa-hinii": "هَلَا، أَحْمَدْ هِنِي؟\nإِي، هُوَ هِنِي.",
    "nass_inti-mudiira-hinii-ii-anaa-mudiira": "إِنْتِ مُدِيرَهْ هِنِي؟\nإِي، أَنَا مُدِيرَهْ هِنِي. أَهْلًا وَسَهْلًا!",
    "nass_halaa-yaa-badr-halaa-wallaah-yaa": "هَلَا يَا بَدْرْ.\nهَلَا وَاللّٰه، يَا حَبِيبِي.",
    "nass_inta-kariim-laa-anaa-mahhmuud-wa": "إِنْتَ كَرِيمْ؟\nلَا، أَنَا مَحْمُودْ، وَكَرِيمْ هُنَاكْ.",
    "nass_hal-huwa-amriikii-laa-huwa-kuurii": "هَلْ هُوَ أَمْرِيكِيّ؟\nلَا، هُوَ كُورِيّ.",
    "nass_hal-hiya-zaynab-laa-laa-hiya": "هَلْ هِيَ زَيْنَبْ؟\nلَا لَا، هِيَ دِينَا. هِيَ جَدِيدَهْ هُنَا.",
    "nass_yallaa-yaa-hhabiibii-anaa-jaahiza-tamaam": "يَلَّا يَا حَبِيبِي. أَنَا جَاهِزَهْ.\nتَمَامْ، يَلَّا.",
    "nass_hal-anta-jaahiz-hayyaa-binaa-na3am": "هَلْ أَنْتَ جَاهِزْ؟ هَيَّا بِنَا.\nنَعَمْ، أَنَا جَاهِزْ. يَلَّا.",
}


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def elevenlabs_api_key() -> str:
    """Read the API key from the environment or the local macOS Keychain.

    The key is never stored in this repository or printed in logs.  One local
    Keychain entry makes later QA-triggered re-recordings non-interactive.
    """
    value = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if value:
        return value
    result = subprocess.run(
        ["security", "find-generic-password", "-s", ELEVENLABS_KEYCHAIN_SERVICE, "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def normalize_unit(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    upper = raw.upper()
    match = re.fullmatch(r"[AP]?0*(\d+)", upper)
    if match:
        # 1.4.5 uses 3-digit unit ids (A001, A002…) to match the sheet `u`
        # value and the app's audio path audio/{u}/{id}.mp3 and the img/ tree.
        return f"A{int(match.group(1)):03d}"
    return upper


def normalize_type(value: str) -> str:
    return (value or "").strip().lower()


def normalize_lahja(value: str) -> str:
    raw = (value or "").strip().lower()
    # Gulf takes priority over Levant: A02 greetings tagged "GLF, LEV"
    # and Egyptian labels: mixed labels use the Gulf voice pool when GLF is present.
    if any(token in raw for token in ("glf", "gulf", "khaleeji", "khaliji", "걸프", "사우디", "gcc")):
        return "msa_gulf"
    if any(token in raw for token in ("egy", "egypt", "이집트")):
        return "egypt"
    if any(token in raw for token in ("lev", "levant", "레반트", "요르단", "jordan")):
        return "levant"
    return "msa_gulf"


def normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def gcloud_token() -> str:
    result = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def rows_from_values(values: list[list[str]]) -> list[dict]:
    if not values:
        return []
    headers = [str(h).strip() for h in values[0]]
    rows = []
    for row_number, raw in enumerate(values[1:], start=2):
        row = {"_row_number": str(row_number)}
        for idx, header in enumerate(headers):
            if not header:
                continue
            row[header] = str(raw[idx]).strip() if idx < len(raw) else ""
        if any(value for key, value in row.items() if not key.startswith("_")):
            rows.append(row)
    return rows


def read_sheet_api(tab_name: str) -> list[dict]:
    token = gcloud_token()
    if not token:
        raise RuntimeError("gcloud auth token is required for Sheets API")
    range_name = f"{tab_name}!A1:AA"
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/"
        f"{urllib.parse.quote(range_name, safe='!')}"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return rows_from_values(data.get("values", []))


def read_sheet_csv(tab_name: str) -> list[dict]:
    try:
        return read_sheet_api(tab_name)
    except Exception as exc:
        print(f"! Sheets API failed, using CSV fallback: {exc}")
    url = (
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
        f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(tab_name)}&headers=1"
    )
    with urllib.request.urlopen(url, timeout=30) as resp:
        raw = resp.read().decode("utf-8-sig")
    rows = []
    for idx, row in enumerate(csv.DictReader(StringIO(raw)), start=2):
        row["_row_number"] = str(idx)
        rows.append(dict(row))
    return rows


def row_value(row: dict, *keys: str) -> str:
    lower = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        if key in row and str(row[key]).strip():
            return str(row[key]).strip()
        lk = key.lower()
        if lk in lower and str(lower[lk]).strip():
            return str(lower[lk]).strip()
    return ""


def slugify_note(note: str) -> str:
    raw = (note or "").lower()
    for old, new in (("*", ""), ("_", ""), ("'", ""), ("ʿ", "3"), ("ʾ", "2"), ("ʕ", "3"),
                     ("ā", "aa"), ("ī", "ii"), ("ū", "uu"), ("ē", "e"), ("ō", "o"),
                     ("ḥ", "hh"), ("ṣ", "ss"), ("ḍ", "dd"), ("ṭ", "tt"), ("ẓ", "zz")):
        raw = raw.replace(old, new)
    raw = raw.replace(".", "").replace(",", "").replace("!", "").replace("?", "")
    return re.sub(r"^-+|-+$", "", re.sub(r"[\s\-]+", "-", raw))


def get_row_id(row: dict) -> str:
    explicit = row_value(row, "id_nass", "id_words", "id", "#ref!")
    if explicit:
        return explicit
    typ = normalize_type(row_value(row, "type"))
    note = row_value(row, "note")
    slug = slugify_note(note)
    if typ in TARGET_TYPES and slug:
        return "nass_" + "-".join(slug.split("-")[:6])
    return ""


def is_tts_script_value(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return False
    return text.lower() not in {"0", "1", "true", "false"}


def strip_markup(text: str) -> str:
    text = re.sub(r"[*_]", "", text or "")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def add_pause_sukun(text: str) -> str:
    """Add pause sukun only to consonant endings, never long vowels."""
    long_vowel_finals = {"ا", "ى", "و", "ي"}
    word_pattern = re.compile(rf"(?:[ء-ي][{ALL_DIACRITICS}]*)+")
    final_pattern = re.compile(rf"([ء-ي])([{ALL_DIACRITICS}]*)$")

    def pause_word(match: re.Match[str]) -> str:
        word = match.group(0)
        final = final_pattern.search(word)
        if not final:
            return word
        base, marks = final.groups()
        if base in long_vowel_finals or SUKUN in marks or any(mark in marks for mark in VOWEL_MARKS):
            return word
        return word + SUKUN

    return word_pattern.sub(pause_word, text)


def ta_marbuta_pause_as_ha(text: str) -> str:
    return re.sub(
        rf"ة[{ALL_DIACRITICS}]*(?=[{BOUNDARY_CHARS}]|$)",
        f"ه{SUKUN}",
        text,
    )


def select_tts_text(row: dict) -> str:
    rid = get_row_id(row)
    if rid in PRONUNCIATION_OVERRIDES:
        return PRONUNCIATION_OVERRIDES[rid]
    for key in ("tss", "tts", "tts_script", "tts_arabic", "arabic"):
        value = row_value(row, key)
        if is_tts_script_value(value):
            return value
    return ""


def split_lines(text: str) -> list[str]:
    cleaned = strip_markup(text)
    lines = [line.strip() for line in cleaned.split("\n") if line.strip()]
    if len(lines) != 1:
        return lines
    # 데이터시트에서 두 발화가 줄바꿈 없이 붙어 있어도 질문표 뒤를
    # 분리한다. 그래야 voi=MF/MM 정보가 실제 두 목소리로 적용된다.
    parts = [part.strip() for part in re.split(r"(?<=؟)", lines[0]) if part.strip()]
    return [parts[0], " ".join(parts[1:])] if len(parts) >= 2 else lines


def looks_female_speaker(line: str) -> bool:
    female_tokens = (
        "أَنَا آسِفَة",
        "اَنَا آسِفَة",
        "آسِفَة",
        "تَعْبَانَة",
        "خَايْفَة",
        "زَعْلَانَة",
        "جُوعَانَة",
        "طَالْعَة",
        "نَازِلَة",
        "لَابْسَة",
        "رَايِحَة",
        "جَدِيدَة",
    )
    return any(token in line for token in female_tokens)


def looks_male_speaker(line: str) -> bool:
    male_tokens = ("أَنَا آسِف", "اَنَا آسِف", "آسِف")
    return any(token in line for token in male_tokens) and "آسِفَة" not in line


def sheet_speaker_genders(row: dict, count: int) -> list[str]:
    values: list[str] = []
    for idx in range(1, count + 1):
        raw = row_value(
            row,
            f"speaker{idx}",
            f"speaker_{idx}",
            f"speaker {idx}",
            f"speaker-{idx}",
        )
        value = raw.strip().upper()
        if value in {"M", "F"}:
            values.append(value)
    if not values:
        return []
    return [values[min(i, len(values) - 1)] for i in range(count)]


def seq_speaker_genders(row: dict, count: int) -> list[str]:
    """1.4.5: read the per-nass `seq` column (e.g. "MF", "FM", "MFM").

    Each M/F character maps to one dialogue line, in order. If seq has
    fewer entries than lines the last value repeats; extra entries are
    ignored. Non-M/F characters (spaces, commas) are skipped.
    """
    raw = row_value(row, "voi", "seq")
    values = [ch for ch in raw.upper() if ch in {"M", "F"}]
    if not values:
        return []
    return [values[min(i, len(values) - 1)] for i in range(count)]


def speaker_genders(row: dict, lines: list[str]) -> list[str]:
    seq_values = seq_speaker_genders(row, len(lines))
    if seq_values:
        return seq_values
    sheet_values = sheet_speaker_genders(row, len(lines))
    if sheet_values:
        return sheet_values
    rid = get_row_id(row)
    override = SPEAKER_OVERRIDES.get(rid)
    if override:
        values = [part.strip().upper() for part in override.split(",") if part.strip()]
        if values:
            return [values[min(i, len(values) - 1)] for i in range(len(lines))]
    result = []
    for idx, line in enumerate(lines):
        if looks_female_speaker(line):
            result.append("F")
        elif looks_male_speaker(line):
            result.append("M")
        else:
            result.append("M" if idx % 2 == 0 else "F")
    return result


def fetch_eleven_voices(api_key: str) -> list[dict]:
    req = urllib.request.Request(
        f"{ELEVEN_API_BASE}/v1/voices",
        headers={"xi-api-key": api_key},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data.get("voices", [])


def resolve_voice_ids(voices: list[dict]) -> dict[str, str]:
    by_name = {normalized_name(v.get("name", "")): v.get("voice_id", "") for v in voices}
    names = [(normalized_name(v.get("name", "")), v.get("voice_id", "")) for v in voices]
    resolved: dict[str, str] = {}
    for spec in VOICE_SPECS:
        voice_id = ""
        for alias in spec.aliases:
            key = normalized_name(alias)
            if key in by_name:
                voice_id = by_name[key]
                break
        if not voice_id:
            for alias in spec.aliases:
                key = normalized_name(alias)
                matches = [vid for name, vid in names if key and key in name]
                if len(matches) == 1:
                    voice_id = matches[0]
                    break
        if voice_id:
            resolved[spec.label] = voice_id
    return resolved


def print_voice_resolution(voices: list[dict], resolved: dict[str, str]) -> None:
    print("ElevenLabs voice mapping:")
    for spec in VOICE_SPECS:
        mark = "OK" if spec.label in resolved else "MISSING"
        print(f"- {mark:7} {spec.gender} {','.join(spec.dialects):9} weight={spec.weight} {spec.label}")
    if voices:
        print("\nAvailable account voices:")
        for voice in voices:
            print(f"- {voice.get('name', '')}")


def weighted_pick(specs: list[VoiceSpec], seed: str) -> VoiceSpec:
    total = sum(max(1, spec.weight) for spec in specs)
    value = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % total
    cursor = 0
    for spec in specs:
        cursor += max(1, spec.weight)
        if value < cursor:
            return spec
    return specs[-1]


def select_voice_spec(gender: str, dialect: str, seed: str, resolved: dict[str, str], exclude=None) -> VoiceSpec:
    candidates = [
        spec for spec in VOICE_SPECS
        if spec.gender == gender and dialect in spec.dialects and spec.label in resolved
    ]
    if not candidates:
        raise RuntimeError(f"No resolved ElevenLabs voice for gender={gender}, dialect={dialect}")
    # Avoid giving two speakers in the same dialogue the same voice (sounds like a monologue).
    if exclude:
        distinct = [spec for spec in candidates if spec.label not in exclude]
        if distinct:
            candidates = distinct
        else:
            # 일부 방언·성별 풀에는 계정 보이스가 하나뿐이다. 그 경우에도
            # 같은 사람이 문답하지 않도록 성별이 맞는 다른 아랍어 보이스를
            # 두 번째 화자 폴백으로 사용한다.
            fallback = [
                spec for spec in VOICE_SPECS
                if spec.gender == gender and spec.label in resolved and spec.label not in exclude
            ]
            if fallback:
                candidates = fallback
    return weighted_pick(candidates, seed)


def with_style_tags(line: str) -> str:
    tags = ELEVEN_STYLE_TAGS.strip()
    if not tags:
        return line
    if line.startswith(tags):
        return line
    return f"{tags} {line}"


def prepare_dialogue_inputs(row: dict, resolved: dict[str, str], args: argparse.Namespace) -> tuple[list[dict], list[str]]:
    raw_text = select_tts_text(row)
    lines = split_lines(raw_text)
    genders = speaker_genders(row, lines)
    dialect = normalize_lahja(row_value(row, "lahja"))
    inputs = []
    labels = []
    used = set()
    fixed_labels = VOICE_LABEL_OVERRIDES.get(get_row_id(row), ())
    for idx, line in enumerate(lines):
        if args.add_final_sukun:
            line = add_pause_sukun(line)
        if args.ta_marbuta_as_ha:
            line = ta_marbuta_pause_as_ha(line)
        gender = genders[idx]
        fixed_label = fixed_labels[idx] if idx < len(fixed_labels) else ""
        if fixed_label:
            matching = [spec for spec in VOICE_SPECS if spec.label == fixed_label and spec.gender == gender and dialect in spec.dialects and spec.label in resolved]
            if not matching:
                raise RuntimeError(f"Configured voice override is unavailable: {get_row_id(row)} line {idx + 1} {fixed_label}")
            spec = matching[0]
        else:
            spec = select_voice_spec(gender, dialect, f"{get_row_id(row)}:{idx}:{gender}:{dialect}", resolved, exclude=used)
        used.add(spec.label)
        labels.append(f"{gender}:{spec.label}")
        inputs.append({
            "text": with_style_tags(line),
            "voice_id": resolved[spec.label],
        })
    return inputs, labels


def elevenlabs_dialogue(api_key: str, inputs: list[dict], args: argparse.Namespace) -> bytes:
    query = urllib.parse.urlencode({"output_format": args.output_format})
    url = f"{ELEVEN_API_BASE}/v1/text-to-dialogue?{query}"
    payload = {
        "inputs": inputs,
        "model_id": args.model,
    }
    if args.language_code:
        payload["language_code"] = args.language_code
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read()


def validate_mp3_bytes(audio: bytes) -> None:
    """Reject empty, undecodable, or effectively silent ElevenLabs output."""
    if len(audio) < 1000:
        raise RuntimeError(f"ElevenLabs output is too small: {len(audio)} bytes")
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as handle:
        path = Path(handle.name)
        handle.write(audio)
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        duration = float(probe.stdout.strip() or 0)
        if probe.returncode or duration <= 0:
            raise RuntimeError(f"ElevenLabs MP3 is not decodable: duration={duration}")
        volume = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        match = re.search(r"max_volume:\s*(-?(?:inf|\d+(?:\.\d+)?)) dB", volume.stderr)
        peak = float("-inf") if match and match.group(1) == "-inf" else float(match.group(1)) if match else None
        if volume.returncode or peak is None or peak < -40:
            raise RuntimeError(f"ElevenLabs MP3 is silent or invalid: peak_dbfs={peak}")
    finally:
        path.unlink(missing_ok=True)


def gcs_object_name(unit: str, filename: str) -> str:
    return f"{GCS_AUDIO_ROOT}/{unit}/{filename}"


def gcs_existing(token: str, units: set[str]) -> set[str]:
    existing: set[str] = set()
    prefixes = [f"{GCS_AUDIO_ROOT}/{unit}/" for unit in sorted(units)] or [f"{GCS_AUDIO_ROOT}/"]
    for prefix in prefixes:
        url = (
            f"https://storage.googleapis.com/storage/v1/b/{GCS_BUCKET}/o"
            f"?prefix={urllib.parse.quote(prefix)}&fields=items(name)&maxResults=10000"
        )
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            existing.update(item["name"] for item in data.get("items", []))
        except Exception as exc:
            print(f"! GCS list failed for {prefix}: {exc}")
    return existing


def upload_to_gcs(token: str, unit: str, filename: str, audio_bytes: bytes) -> str:
    object_name = gcs_object_name(unit, filename)
    url = (
        f"https://storage.googleapis.com/upload/storage/v1/b/{GCS_BUCKET}/o"
        f"?uploadType=media&name={urllib.parse.quote(object_name, safe='')}"
    )
    req = urllib.request.Request(
        url,
        data=audio_bytes,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "audio/mpeg",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        resp.read()
    return f"https://storage.googleapis.com/{GCS_BUCKET}/{urllib.parse.quote(object_name)}"


def collect_rows(units: set[str], ids: set[str]) -> list[dict]:
    normalized_units = {normalize_unit(unit) for unit in units if unit}
    rows = []
    for row in read_sheet_csv(NASS_TAB):
        rid = get_row_id(row)
        row_unit = normalize_unit(row_value(row, "U", "u"))
        status = row_value(row, "status").lower()
        row_type = normalize_type(row_value(row, "type"))
        if not rid or not row_unit:
            continue
        # 1.4.5 content is authored as draft; accept both confirmed and draft.
        if status not in {"confirmed", "draft"} or row_type not in TARGET_TYPES:
            continue
        if normalized_units and row_unit not in normalized_units:
            continue
        if ids and rid not in ids:
            continue
        row["_id"] = rid
        row["_unit"] = row_unit
        row["_type"] = row_type
        rows.append(row)
    return rows


def fetch_feedback_items(url: str) -> dict[str, list[str]]:
    request = urllib.request.Request(url, headers={"User-Agent": "ATA-145-nass-generator/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    statuses = payload.get("statuses", {})
    feedback = payload.get("feedback", {})
    return {
        key: [str(item.get("body", "")).strip() for item in items if str(item.get("body", "")).strip()]
        for key, items in feedback.items()
        if statuses.get(key, {}).get("status") in {"needs-fix", "needs-revision"}
    }


def main() -> int:
    load_dotenv(SCRIPT_DIR / ".env")
    load_dotenv(PROJECT_ROOT / ".env")

    parser = argparse.ArgumentParser(description="Generate ATA 1.4.5 nass audio with ElevenLabs")
    parser.add_argument("--unit", action="append", default=[], help="Unit filter, e.g. A14. Repeatable.")
    parser.add_argument("--id", action="append", default=[], help="Specific id_nass filter. Repeatable.")
    parser.add_argument("--all-units", action="store_true", help="Allow processing every confirmed nass row.")
    parser.add_argument("--limit", type=int, default=0, help="Limit rows after filtering.")
    parser.add_argument("--model", default=ELEVEN_MODEL, help="ElevenLabs model id.")
    parser.add_argument("--output-format", default=ELEVEN_OUTPUT_FORMAT, help="ElevenLabs output format.")
    parser.add_argument("--language-code", default=ELEVEN_LANGUAGE_CODE, help="ElevenLabs language code.")
    parser.add_argument("--dry-run", action="store_true", help="Print targets without generating audio.")
    parser.add_argument("--missing-only", action="store_true", help="Only process files missing locally and on GCS.")
    parser.add_argument("--overwrite", action="store_true", help="Regenerate even when local/GCS file exists.")
    parser.add_argument("--upload-existing", action="store_true", help="Upload cached local mp3s to GCS without regenerating (no ElevenLabs credits).")
    parser.add_argument("--local-only", action="store_true", help="Do not upload to GCS.")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "audio"), help="Local audio root.")
    parser.add_argument("--sleep", type=float, default=0.5, help="Pause between API requests.")
    parser.add_argument("--list-voices", action="store_true", help="Print voice mapping and exit.")
    parser.add_argument("--add-final-sukun", action="store_true", help="TTS-only final sukun correction.")
    parser.add_argument("--ta-marbuta-as-ha", action="store_true", help="TTS-only pause ta marbuta correction.")
    parser.add_argument("--fail-unconfigured-voices", action="store_true", help="Treat unconfigured dialect/voice pools as failures.")
    parser.add_argument("--feedback-only", action="store_true", help="Generate only non-passed nass rows from the QA feedback service.")
    parser.add_argument("--feedback-api", default="https://ata145-qa-feedback.markazarabic.chatgpt.site/api/review", help="Read-only QA feedback API used by --feedback-only.")
    args = parser.parse_args()

    api_key = elevenlabs_api_key()
    if not api_key:
        print(f"ELEVENLabs key unavailable. Set ELEVENLABS_API_KEY or add the local Keychain service {ELEVENLABS_KEYCHAIN_SERVICE}.")
        return 1

    if not args.list_voices and not args.unit and not args.id and not args.all_units:
        print("Refusing to scan all units. Pass --unit, --id, or explicit --all-units.")
        return 2

    voices = fetch_eleven_voices(api_key)
    resolved = resolve_voice_ids(voices)
    if args.list_voices:
        print_voice_resolution(voices, resolved)
        return 0

    rows = collect_rows(set(args.unit), set(args.id))
    feedback_by_key = fetch_feedback_items(args.feedback_api) if args.feedback_only else {}
    if args.feedback_only:
        rows = [row for row in rows if f"{row['_unit']}/{row['_id']}" in feedback_by_key]
        for row in rows:
            comments = feedback_by_key[f"{row['_unit']}/{row['_id']}"]
            # The API returns newest feedback first. A precise sequence in the
            # latest comment must override older broad comments such as
            # "여자 목소리로 해야 함"; otherwise both keywords collapse the
            # whole dialogue to one gender on every re-recording.
            latest = comments[0] if comments else ""
            if "남자 목소리 다음에 여자 목소리" in latest:
                row["voi"] = "MF"
            elif "여자 목소리 다음에 남자 목소리" in latest:
                row["voi"] = "FM"
            elif "남자 목소리" in latest:
                row["voi"] = "M" * len(split_lines(select_tts_text(row)))
            elif "여자 목소리" in latest:
                row["voi"] = "F" * len(split_lines(select_tts_text(row)))
    if args.limit:
        rows = rows[:args.limit]

    output_root = Path(args.output_dir).expanduser()
    if not output_root.is_absolute():
        output_root = Path.cwd() / output_root

    units = {row["_unit"] for row in rows}
    token = "" if args.local_only else gcloud_token()
    existing_gcs = set() if args.local_only or not token else gcs_existing(token, units)

    filtered = []
    for row in rows:
        rid = row["_id"]
        unit = row["_unit"]
        filename = f"{rid}.mp3"
        local_path = output_root / unit / filename
        gcs_name = gcs_object_name(unit, filename)
        exists_local = local_path.exists()
        exists_gcs = gcs_name in existing_gcs
        row["_local_path"] = str(local_path)
        row["_exists_local"] = "1" if exists_local else ""
        row["_exists_gcs"] = "1" if exists_gcs else ""
        if args.upload_existing:
            # Upload already-generated local files without re-calling ElevenLabs.
            if exists_local and (args.overwrite or not exists_gcs):
                filtered.append(row)
            continue
        if args.missing_only and not args.overwrite and (exists_local or exists_gcs):
            continue
        if not args.overwrite and not args.missing_only and (exists_local or exists_gcs):
            continue
        filtered.append(row)
    rows = filtered

    print(f"sheet={SHEET_ID}")
    print(f"model={args.model}")
    print(f"output_format={args.output_format}")
    print(f"target=gs://{GCS_BUCKET}/{GCS_AUDIO_ROOT}/{{unit}}/")
    print(f"rows={len(rows)}")

    done = skip = fail = 0
    for row in rows:
        rid = row["_id"]
        unit = row["_unit"]
        filename = f"{rid}.mp3"
        local_path = Path(row["_local_path"])

        # Upload-existing mode: push the cached local mp3 straight to GCS,
        # no ElevenLabs call (no credits spent, audio byte-for-byte identical).
        if args.upload_existing:
            if not local_path.exists():
                print(f"SKIP {unit}/{rid} no local file")
                skip += 1
                continue
            if args.dry_run:
                print(f"- {unit}/{rid} would upload {local_path}")
                continue
            try:
                validate_mp3_bytes(local_path.read_bytes())
                if not token:
                    raise RuntimeError("gcloud auth token is required for GCS upload")
                url = upload_to_gcs(token, unit, filename, local_path.read_bytes())
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
            continue

        try:
            inputs, labels = prepare_dialogue_inputs(row, resolved, args)
        except Exception as exc:
            if not args.fail_unconfigured_voices and "voice" in str(exc).lower():
                print(f"SKIP {unit}/{rid} prepare: {exc}")
                skip += 1
                continue
            print(f"FAIL {unit}/{rid} prepare: {exc}")
            fail += 1
            continue

        preview = " / ".join(item["text"] for item in inputs)
        print(f"- {unit}/{rid} row={row.get('_row_number')} voices={', '.join(labels)}")
        comments = feedback_by_key.get(f"{unit}/{rid}", [])
        print(f"  {preview}" + (f" [feedback:{' | '.join(comments)}]" if comments else ""))

        if args.dry_run:
            continue
        if not inputs:
            print(f"SKIP {unit}/{rid} empty input")
            skip += 1
            continue

        try:
            audio = elevenlabs_dialogue(api_key, inputs, args)
            validate_mp3_bytes(audio)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(audio)
            if args.local_only:
                print(f"OK   {local_path}")
            else:
                if not token:
                    raise RuntimeError("gcloud auth token is required for GCS upload")
                url = upload_to_gcs(token, unit, filename, audio)
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

    print(f"done={done} skip={skip} fail={fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

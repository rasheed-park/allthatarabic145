#!/usr/bin/env python3
"""Export the ATA 1.4.5 review snapshot from the public Google Sheet.

The output is intentionally a static JSON file for GitHub Pages.  It contains
only fields needed for content review; no account credentials or API keys are
written to the deployed site.

Examples:
  python3 scripts/qa/export_qa_snapshot.py
  python3 scripts/qa/export_qa_snapshot.py --set-state A001/rep_baa needs-recheck --issue 42
  python3 scripts/qa/export_qa_snapshot.py --set-state A001/nass_example passed
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SHEET_ID = "1cFamlN6FjnIiRLTBl3OsAPbHTPiQYKJUio4caR-7Lm4"
TAB = "데이터시트sheet"
ROOT = Path(__file__).resolve().parents[2]
DATA_PATH = ROOT / "qa" / "data" / "review-data.json"
STATE_PATH = ROOT / "qa" / "data" / "review-state.json"
REVIEW_TYPES = {"exp", "exp+", "kal", "kal+", "rep", "drill", "drill+", "nass", "nass+"}
ACTIVE_SHEET_STATUSES = {"draft", "confirmed"}
STATE_ALIASES = {
    "new": "needs-review", "needs-review": "needs-review",
    "recheck": "needs-recheck", "needs-recheck": "needs-recheck",
    "feedback": "feedback", "working": "in-progress", "in-progress": "in-progress",
    "passed": "passed", "deleted": "deleted",
}
HASH_FIELDS = ("u", "ptn", "type", "sheetStatus", "arabic", "korean", "tss", "memo", "lahja", "voi", "css", "url")


def slugify_note(note: str) -> str:
    raw = (note or "").lower()
    for old, new in (("*", ""), ("_", ""), ("'", ""), ("ʿ", "3"), ("ʾ", "2"), ("ʕ", "3"),
                     ("ā", "aa"), ("ī", "ii"), ("ū", "uu"), ("ē", "e"), ("ō", "o"),
                     ("ḥ", "hh"), ("ṣ", "ss"), ("ḍ", "dd"), ("ṭ", "tt"), ("ẓ", "zz")):
        raw = raw.replace(old, new)
    raw = raw.replace(".", "").replace(",", "").replace("!", "").replace("?", "")
    return re.sub(r"^-+|-+$", "", re.sub(r"[\s\-]+", "-", raw))


def derived_audio_id(row: dict[str, str]) -> str:
    explicit = row.get("id", "").strip()
    if explicit:
        return explicit
    typ = row.get("type", "").strip().lower()
    note = row.get("note", "").strip()
    ptn = row.get("ptn", "").strip()
    if not typ or not note:
        return ""
    slug = slugify_note(note)
    base_type = typ.replace("+", "")
    if base_type == "ptn":
        return f"{ptn}_{slug}" if ptn else ""
    if base_type in {"nas", "nass"}:
        return "nass_" + "-".join(slug.split("-")[:6])
    kind = "kalimat" if base_type == "kal" else base_type
    return f"{kind}_{slug}" if slug else ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set-state", nargs=2, metavar=("U/ID", "STATE"), help="Persist an item state before exporting.")
    parser.add_argument("--issue", type=int, help="GitHub Issue number to save with --set-state.")
    parser.add_argument("--output", type=Path, default=DATA_PATH)
    return parser.parse_args()


def fetch_sheet() -> list[dict[str, str]]:
    url = (
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq"
        f"?tqx=out:json&sheet={urllib.parse.quote(TAB)}&headers=1"
    )
    with urllib.request.urlopen(url, timeout=40) as response:
        raw = response.read().decode("utf-8")
    match = re.search(r"setResponse\((.*)\);?\s*$", raw, re.S)
    if not match:
        raise RuntimeError("Google Sheet response did not include table data.")
    table = json.loads(match.group(1))["table"]
    headers = [str(column.get("label") or "").strip().lower() for column in table.get("cols", [])]
    rows: list[dict[str, str]] = []
    for source in table.get("rows", []):
        cells = source.get("c", [])
        row: dict[str, str] = {}
        for index, header in enumerate(headers):
            if not header:
                continue
            cell = cells[index] if index < len(cells) else None
            value = "" if cell is None else cell.get("f", cell.get("v", ""))
            row[header] = str(value).strip()
        if any(row.values()):
            # The sheet's ARRAYFORMULA ID column can be rendered as #REF!.
            # Keep the snapshot aligned with the audio generators by deriving
            # the same stable ID from type/note when the explicit ID is absent.
            if not row.get("id"):
                derived = derived_audio_id(row)
                if derived:
                    row["id"] = derived
            rows.append(row)
    return rows


def audio_candidates(row: dict[str, str]) -> list[str]:
    explicit = row.get("url", "")
    if explicit:
        return [explicit]
    unit, item_id = row["u"], row["id"]
    base = f"https://storage.googleapis.com/all-that-arabic-145/audio/{unit}/{item_id}"
    if row["type"].startswith("nass"):
        return [base + ".mp3", base + ".wav"]
    return [base + ".wav", base + ".mp3"]


def item_hash(record: dict[str, Any]) -> str:
    source = {field: record.get(field, "") for field in HASH_FIELDS}
    return hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def review_record(row: dict[str, str]) -> dict[str, Any]:
    record = {
        "key": f"{row['u']}/{row['id']}", "u": row["u"], "ptn": row.get("ptn", ""),
        "id": row["id"], "type": row["type"], "sheetStatus": row.get("status", ""),
        "arabic": row.get("arabic", ""), "korean": row.get("korean", ""),
        "tss": row.get("tss", ""), "memo": row.get("memo", ""),
        "lahja": row.get("lahja", ""), "voi": row.get("voi", ""), "css": row.get("css", ""),
        "url": row.get("url", ""), "audio": {"candidates": audio_candidates(row)},
    }
    record["itemHash"] = item_hash(record)
    return record


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def changed_fields(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    labels = {"arabic": "원문", "korean": "한국어", "tss": "TSS", "memo": "메모", "lahja": "방언", "voi": "화자", "url": "오디오 URL", "ptn": "패턴", "type": "타입", "sheetStatus": "시트 상태"}
    return [label for field, label in labels.items() if previous.get(field, "") != current.get(field, "")]


def compact_version(record: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(record.get(key, "")) for key in ("arabic", "korean", "tss", "memo", "lahja", "voi", "url", "audio")}


def update_state_file(args: argparse.Namespace, state: dict[str, Any]) -> None:
    if not args.set_state:
        return
    key, requested = args.set_state
    status = STATE_ALIASES.get(requested.strip().lower())
    if not status:
        choices = ", ".join(sorted(set(STATE_ALIASES.values())))
        raise SystemExit(f"Unknown review state {requested!r}. Use one of: {choices}")
    item = state.setdefault("items", {}).setdefault(key, {})
    item["state"] = status
    if args.issue is not None:
        item["issue"] = args.issue
    elif status == "passed":
        item.pop("issue", None)


def main() -> None:
    args = parse_args()
    state = load_json(STATE_PATH, {"version": 1, "items": {}})
    update_state_file(args, state)
    previous_data = load_json(args.output, {"records": []})
    previous_by_key = {record["key"]: record for record in previous_data.get("records", [])}
    rows = fetch_sheet()
    units = []
    for row in rows:
        if row.get("type") == "unit" and row.get("u"):
            units.append({"id": row["u"], "arabic": row.get("arabic", ""), "korean": row.get("korean", "")})
    records = []
    for row in rows:
        typ, sheet_status, item_id, unit = row.get("type", ""), row.get("status", ""), row.get("id", ""), row.get("u", "")
        if typ not in REVIEW_TYPES or sheet_status not in ACTIVE_SHEET_STATUSES or not unit or not item_id:
            continue
        record = review_record(row)
        previous = previous_by_key.get(record["key"])
        item_state = state.setdefault("items", {}).get(record["key"], {})
        if previous is None:
            review = {"state": item_state.get("state", "needs-review"), "changedFields": []}
        elif previous.get("itemHash") != record["itemHash"]:
            review = {"state": "needs-recheck", "changedFields": changed_fields(previous, record), "previous": compact_version(previous)}
        else:
            review = {"state": item_state.get("state", previous.get("review", {}).get("state", "needs-review")), "changedFields": previous.get("review", {}).get("changedFields", [])}
            if previous.get("review", {}).get("previous"):
                review["previous"] = previous["review"]["previous"]
        if item_state.get("issue") is not None:
            review["issue"] = item_state["issue"]
        if review["state"] == "passed":
            review["changedFields"] = []
            review.pop("previous", None)
        record["review"] = review
        records.append(record)
        state["items"][record["key"]] = {"state": review["state"], **({"issue": review["issue"]} if "issue" in review else {})}
    active_keys = {record["key"] for record in records}
    for key, item in state.get("items", {}).items():
        if key not in active_keys and item.get("state") == "passed":
            item["state"] = "deleted"
    # 시트가 콘텐츠의 정본 순서다. ID·타입 기준 재정렬을 하면
    # 표현 → 패턴 → 레퍼토리/드릴 → 지문이라는 제작·검수 순서가 깨진다.
    ordered_units = list({unit["id"]: unit for unit in units}.values())
    result = {"schemaVersion": 1, "generatedAt": datetime.now(timezone.utc).isoformat(), "source": {"sheetId": SHEET_ID, "tab": TAB}, "units": ordered_units, "records": records}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
        file.write("\n")
    with STATE_PATH.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
        file.write("\n")
    print(f"Wrote {len(records)} review records for {len(ordered_units)} units to {args.output}")


if __name__ == "__main__":
    main()

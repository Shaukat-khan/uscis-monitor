"""
US Citizenship Ready — civics data monitor
==========================================
Builds civics_data.json from public sources, diffs it against the last
committed snapshot, emails one consolidated alert if anything changed,
and writes the JSON so GitHub Actions can commit it back to the repo.

Required GitHub Secrets:
  GMAIL_USER, GMAIL_PASSWORD, NOTIFY_EMAIL
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import BytesIO
from typing import Any

import requests
from pypdf import PdfReader

PDF_URL = (
    "https://www.uscis.gov/sites/default/files/document/"
    "questions-and-answers/2025-Civics-Test-128-Questions-and-Answers.pdf"
)
TESTUPDATES_URL = "https://www.uscis.gov/citizenship/testupdates"
LEGISLATORS_URL = (
    "https://unitedstates.github.io/congress-legislators/legislators-current.json"
)
GOVERNORS_WIKI_URL = (
    "https://en.wikipedia.org/wiki/List_of_current_United_States_governors"
)
HOUSE_DIRECTORY_URL = "https://www.house.gov/representatives"
SENATE_DIRECTORY_URL = "https://www.senate.gov/senators/index.htm"
DATA_FILE = "civics_data.json"
UA = (
    "Mozilla/5.0 (compatible; USCitizenshipReadyMonitor/1.0; "
    "+https://github.com/Shaukat-khan/uscis-monitor)"
)

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", GMAIL_USER)

STATE_NAMES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
    "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ",
    "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}


def http_get(url: str, timeout: int = 45) -> requests.Response:
    print(f"GET {url}")
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    resp.raise_for_status()
    return resp


def load_json(path: str) -> dict[str, Any]:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path: str, data: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Wrote {path}")


def parse_questions(raw_text: str) -> dict[str, Any]:
    questions: dict[str, Any] = {}
    pattern = re.compile(r"\n(\d{1,3})\.\s+(.+?)(?=\n\d{1,3}\.\s|\Z)", re.DOTALL)
    matches = pattern.findall("\n" + raw_text)
    for num, body in matches:
        lines = [ln.strip() for ln in body.strip().splitlines() if ln.strip()]
        if not lines:
            continue
        question_text = lines[0]
        senior = "*" in question_text or "★" in question_text
        answers = []
        for line in lines[1:]:
            clean = line.lstrip("•●▪*-– ").strip()
            if clean and not clean.lower().startswith("visit uscis"):
                answers.append(clean)
        questions[num] = {
            "number": int(num),
            "question": question_text,
            "answers": answers,
            "senior": senior,
        }
    print(f"Parsed {len(questions)} civics questions")
    return questions


def fetch_civics_questions() -> tuple[dict[str, Any], list[int], str]:
    pdf_bytes = http_get(PDF_URL).content
    pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
    reader = PdfReader(BytesIO(pdf_bytes))
    pages = [page.extract_text() or "" for page in reader.pages]
    questions = parse_questions("\n".join(pages))
    seniors = sorted(
        q["number"] for q in questions.values() if q.get("senior")
    )
    return questions, seniors, pdf_sha


def _names_after(html: str, heading: str) -> list[str]:
    idx = html.lower().find(heading.lower())
    if idx < 0:
        return []
    chunk = html[idx : idx + 1800]
    # USCIS lists acceptable names as <li> items under each question.
    items = re.findall(r"<li[^>]*>(.*?)</li>", chunk, flags=re.I | re.S)
    names = []
    for item in items:
        text = re.sub(r"<[^>]+>", " ", item)
        text = re.sub(r"\s+", " ", text).strip(" .")
        if not text:
            continue
        if "visit " in text.lower() or "answers will vary" in text.lower():
            break
        if "question" in text.lower() and text[:3].isdigit():
            break
        names.append(text)
        if len(names) >= 6:
            break
    return names


def fetch_executive() -> dict[str, Any]:
    html = http_get(TESTUPDATES_URL).text
    # Prefer the 2025-test section when both 2008 and 2025 appear.
    section = html
    marker = "2025 Naturalization Civics Test"
    pos = html.find(marker)
    if pos >= 0:
        section = html[pos:]
    executive = {
        "president": _names_after(section, "President of the United States now"),
        "vice_president": _names_after(
            section, "Vice President of the United States now"
        ),
        "speaker_of_the_house": _names_after(
            section, "Speaker of the House of Representatives now"
        ),
        "chief_justice": _names_after(
            section, "Chief Justice of the United States now"
        ),
        "source": TESTUPDATES_URL,
    }
    print(
        "Executive:",
        {k: v for k, v in executive.items() if k != "source"},
    )
    return executive


def _official_name(entry: dict[str, Any]) -> str:
    name = entry.get("name") or {}
    official = name.get("official_full")
    if official:
        return official
    parts = [name.get("first"), name.get("middle"), name.get("last")]
    return " ".join(p for p in parts if p).strip()


def fetch_congress() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    data = http_get(LEGISLATORS_URL).json()
    senators: list[dict[str, Any]] = []
    reps: list[dict[str, Any]] = []
    for person in data:
        terms = person.get("terms") or []
        if not terms:
            continue
        current = terms[-1]
        if current.get("end") and current["end"] < datetime.now(timezone.utc).date().isoformat():
            continue
        rec = {
            "name": _official_name(person),
            "state": current.get("state"),
            "party": current.get("party"),
            "url": current.get("url"),
        }
        if current.get("type") == "sen":
            rec["class"] = current.get("class")
            senators.append(rec)
        elif current.get("type") == "rep":
            district = current.get("district")
            rec["district"] = "At-Large" if district in (0, "0", None) else str(district)
            reps.append(rec)
    senators.sort(key=lambda r: (r["state"] or "", r["name"]))
    reps.sort(key=lambda r: (r["state"] or "", r["district"], r["name"]))
    print(f"Congress: {len(senators)} senators, {len(reps)} representatives")
    return senators, reps


def fetch_governors() -> list[dict[str, Any]]:
    html = http_get(GOVERNORS_WIKI_URL).text
    # Rows look like: <td>Alabama</td> ... <td>Kay Ivey</td>
    governors: dict[str, str] = {}
    row_re = re.compile(
        r"<tr[^>]*>\s*<td[^>]*>.*?title=\"([^\"]+)\".*?</td>\s*"
        r"<td[^>]*>.*?</td>\s*"
        r"<td[^>]*>.*?title=\"([^\"]+)\".*?</td>",
        re.I | re.S,
    )
    for state_title, gov_title in row_re.findall(html):
        state_name = state_title.replace(" (state)", "").strip()
        code = STATE_NAMES.get(state_name)
        if not code:
            continue
        gov_name = re.sub(r" \(governor\)", "", gov_title, flags=re.I).strip()
        governors[code] = gov_name

    if len(governors) < 50:
        # Fallback: plain "Governor of X" titles in the same table.
        simple = re.findall(
            r"Governor of ([A-Za-z .]+).*?title=\"([^\"]+)\"",
            html,
            flags=re.I | re.S,
        )
        for state_name, gov_title in simple:
            code = STATE_NAMES.get(state_name.strip())
            if code and code not in governors:
                governors[code] = gov_title.strip()

    rows = [
        {"state": code, "name": governors[code], "state_name": name}
        for name, code in STATE_NAMES.items()
        if code in governors
    ]
    rows.sort(key=lambda r: r["state"])
    print(f"Governors parsed: {len(rows)}")
    if len(rows) < 50:
        print("WARNING: fewer than 50 governors parsed from Wikipedia")
    return rows


def canonicalize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: canonicalize(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [canonicalize(v) for v in obj]
    return obj


def _index_by(rows: list[dict[str, Any]], *keys: str) -> dict[str, dict[str, Any]]:
    out = {}
    for row in rows:
        key = "|".join(str(row.get(k, "")) for k in keys)
        out[key] = row
    return out


def diff_named(
    category: str,
    old_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
    key_fields: tuple[str, ...],
    label_fn,
) -> list[str]:
    old_map = _index_by(old_rows, *key_fields)
    new_map = _index_by(new_rows, *key_fields)
    changes = []
    for key in sorted(set(old_map) | set(new_map)):
        if key not in old_map:
            changes.append(f"{category}: added {label_fn(new_map[key])}")
        elif key not in new_map:
            changes.append(f"{category}: removed {label_fn(old_map[key])}")
        elif canonicalize(old_map[key]) != canonicalize(new_map[key]):
            changes.append(
                f"{category}: {label_fn(new_map[key])} changed "
                f"from {old_map[key].get('name')} to {new_map[key].get('name')}"
            )
    return changes


def find_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    changes: list[str] = []

    old_q = old.get("civics_questions") or {}
    new_q = new.get("civics_questions") or {}
    for key in sorted(set(old_q) | set(new_q), key=lambda x: int(x) if str(x).isdigit() else 0):
        if key not in old_q:
            changes.append(f"Question {key} added")
        elif key not in new_q:
            changes.append(f"Question {key} removed")
        elif canonicalize(old_q[key]) != canonicalize(new_q[key]):
            if old_q[key].get("question") != new_q[key].get("question"):
                changes.append(f"Question {key} text updated")
            else:
                changes.append(f"Question {key} answer updated")

    if canonicalize(old.get("seniors_questions")) != canonicalize(
        new.get("seniors_questions")
    ):
        changes.append("65+/seniors question list updated")

    old_ex = old.get("executive") or {}
    new_ex = new.get("executive") or {}
    labels = {
        "president": "President of the United States",
        "vice_president": "Vice President",
        "speaker_of_the_house": "Speaker of the House",
        "chief_justice": "Chief Justice of the Supreme Court",
    }
    for field, label in labels.items():
        if canonicalize(old_ex.get(field)) != canonicalize(new_ex.get(field)):
            changes.append(
                f"New {label}: {', '.join(new_ex.get(field) or []) or '(empty)'} "
                f"(was {', '.join(old_ex.get(field) or []) or '(empty)'})"
            )

    changes += diff_named(
        "governors",
        old.get("governors") or [],
        new.get("governors") or [],
        ("state",),
        lambda r: f"Governor of {r.get('state_name') or r.get('state')}",
    )
    changes += diff_named(
        "senators",
        old.get("senators") or [],
        new.get("senators") or [],
        ("state", "name"),
        lambda r: f"Senator {r.get('name')} ({r.get('state')})",
    )
    changes += diff_named(
        "representatives",
        old.get("representatives") or [],
        new.get("representatives") or [],
        ("state", "district"),
        lambda r: f"Representative {r.get('state')}-{r.get('district')} ({r.get('name')})",
    )
    return changes


def format_email(changes: list[str], new: dict[str, Any]) -> str:
    lines = [
        "US Citizenship Ready data update",
        "=" * 60,
        f"Generated: {new.get('generated_at')}",
        f"Total changes: {len(changes)}",
        "",
        "What changed:",
    ]
    for change in changes:
        lines.append(f"  - {change}")
    lines += [
        "",
        "Live JSON:",
        "https://raw.githubusercontent.com/Shaukat-khan/uscis-monitor/main/civics_data.json",
        "",
        f"Civics questions: {len(new.get('civics_questions') or {})}",
        f"Seniors questions: {len(new.get('seniors_questions') or [])}",
        f"Governors: {len(new.get('governors') or [])}",
        f"Senators: {len(new.get('senators') or [])}",
        f"Representatives: {len(new.get('representatives') or [])}",
        f"Executive: {new.get('executive')}",
    ]
    return "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    if not GMAIL_USER or not GMAIL_PASSWORD:
        print("No email credentials — printing alert:")
        print(subject)
        print(body)
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = NOTIFY_EMAIL
    msg.attach(MIMEText(body, "plain"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())
        print(f"Alert email sent to {NOTIFY_EMAIL}")
    except Exception as exc:
        print(f"Failed to send email: {exc}")
        print(body)


def build_dataset() -> dict[str, Any]:
    questions, seniors, pdf_sha = fetch_civics_questions()
    executive = fetch_executive()
    senators, representatives = fetch_congress()
    governors = fetch_governors()
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": {
            "civics_pdf": PDF_URL,
            "civics_pdf_sha256": pdf_sha,
            "testupdates": TESTUPDATES_URL,
            "legislators": LEGISLATORS_URL,
            "governors": GOVERNORS_WIKI_URL,
            "house": HOUSE_DIRECTORY_URL,
            "senate": SENATE_DIRECTORY_URL,
        },
        "civics_questions": questions,
        "seniors_questions": seniors,
        "executive": executive,
        "governors": governors,
        "senators": senators,
        "representatives": representatives,
    }


def maybe_simulate(data: dict[str, Any]) -> dict[str, Any]:
    clone = json.loads(json.dumps(data))
    for row in clone.get("governors") or []:
        if row.get("state") == "OH":
            row["name"] = "SIMULATED TEST GOVERNOR"
            break
    return clone


def main() -> int:
    print("=" * 60)
    print("US Citizenship Ready data monitor")
    print("=" * 60)
    try:
        current = build_dataset()
    except Exception as exc:
        send_email(
            "US Citizenship Ready monitor failed",
            f"Could not build civics_data.json.\n\nError: {exc}",
        )
        raise

    previous = load_json(DATA_FILE)
    simulate = os.environ.get("SIMULATE_CHANGE") == "1"
    if simulate:
        print("SIMULATE_CHANGE=1 — Ohio governor only, file stays real")
        simulated = maybe_simulate(current)
        changes = find_changes(current, simulated)
        body = format_email(changes, simulated)
        print(body)
        send_email(
            f"[SIMULATED] US Citizenship Ready data updated — {len(changes)} change(s)",
            body,
        )
        save_json(DATA_FILE, current)
        return 0

    changes = find_changes(previous, current) if previous else []
    save_json(DATA_FILE, current)

    if not previous:
        print("No previous snapshot — saved baseline. No email.")
        return 0

    if not changes:
        print("No category changes.")
        return 0

    print(f"{len(changes)} change(s) found")
    body = format_email(changes, current)
    print(body)
    send_email(
        f"US Citizenship Ready data updated — {len(changes)} change(s)",
        body,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

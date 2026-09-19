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


FOOTER_RE = re.compile(
    r"^(?:\d+\s+of\s+\d+|uscis\.gov(?:/citizenship)?/?|www\.uscis\.gov.*|"
    r"m-1778.*|128 civics questions.*)$",
    re.I,
)
VISIT_UPDATES_RE = re.compile(
    r"visit\s+uscis\.gov/citizenship/testupdates",
    re.I,
)
PAGE_NUM_QUESTION_RE = re.compile(r"^\d+\s+of\s+\d+$", re.I)

# PDF answers that only point at testupdates — fill from executive names.
CURRENT_OFFICIAL_QUESTIONS = {
    30: "speaker_of_the_house",
    38: "president",
    39: "vice_president",
    57: "chief_justice",
}


def _is_noise_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if FOOTER_RE.match(stripped):
        return True
    if PAGE_NUM_QUESTION_RE.match(stripped):
        return True
    if stripped.lower() in {"*", "•"}:
        return True
    return False


def _clean_pdf_text(raw_text: str) -> str:
    kept = []
    for line in raw_text.splitlines():
        if _is_noise_line(line):
            continue
        kept.append(line)
    return "\n".join(kept)


def parse_questions(raw_text: str) -> dict[str, Any]:
    """Split on real numbered questions (digit + period + capital), not page numbers."""
    cleaned = _clean_pdf_text(raw_text)
    pattern = re.compile(
        r"^(\d{1,3})\.\s+(?=[A-Z\"'])(.+?)(?=^\d{1,3}\.\s+(?=[A-Z\"'])|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    questions: dict[str, Any] = {}
    for num, body in pattern.findall(cleaned):
        n = int(num)
        if n < 1 or n > 128:
            continue
        lines = [ln.strip() for ln in body.strip().splitlines() if ln.strip()]
        if not lines:
            continue
        question_text = re.sub(r"\s+", " ", lines[0]).strip()
        senior = "*" in question_text or "★" in question_text
        answers: list[str] = []
        for line in lines[1:]:
            if _is_noise_line(line) or VISIT_UPDATES_RE.search(line):
                continue
            clean = line.lstrip("•●▪*-– ").strip()
            if not clean:
                continue
            answers.append(clean)
        questions[str(n)] = {
            "number": n,
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


def _looks_like_person_name(text: str) -> bool:
    t = text.strip()
    if not t or len(t) > 80:
        return False
    low = t.lower()
    if any(
        junk in low
        for junk in (
            "answers will vary",
            "visit ",
            "nine",
            "republican",
            "democrat",
            "party",
            "birth name",
            "governor of",
            "list of",
            "mayor of",
            "seal of",
            "flag of",
        )
    ):
        return False
    if re.search(r"\d", t):
        return False
    # Require at least a first + last, not a last-name-only USCIS shortcut.
    parts = [p for p in re.split(r"\s+", t) if p]
    return len(parts) >= 2


def _best_person_name(candidates: list[str]) -> str:
    people = [c.strip() for c in candidates if _looks_like_person_name(c)]
    if not people:
        return ""
    # Prefer the most complete form ("Donald J. Trump" over "Donald Trump").
    return max(people, key=lambda n: (len(n), n))


def _names_for_heading(html: str, heading_regex: str) -> str:
    """Take names from the list under one 2025-test heading, not the next question."""
    pattern = re.compile(
        heading_regex + r".*?(?:<ul[^>]*>(.*?)</ul>|(?:<li[^>]*>.*?</li>\s*)+)",
        re.I | re.S,
    )
    match = pattern.search(html)
    if not match:
        return ""
    block = match.group(0)
    # Stop before the next numbered civics heading if the regex overran.
    nxt = re.search(r"(?:<p[^>]*>)?\s*\d{1,3}\.\s+", block[20:], flags=re.I)
    if nxt:
        block = block[: 20 + nxt.start()]
    items = re.findall(r"<li[^>]*>(.*?)</li>", block, flags=re.I | re.S)
    names = []
    for item in items:
        text = re.sub(r"<[^>]+>", " ", item)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            names.append(text)
    return _best_person_name(names)


def fetch_executive() -> dict[str, Any]:
    html = http_get(TESTUPDATES_URL).text
    marker = "2025 Naturalization Civics Test"
    pos = html.find(marker)
    section = html[pos:] if pos >= 0 else html
    # Do not search for "President of the United States now" — that substring
    # also matches "Vice President of the United States now".
    executive = {
        "president": _names_for_heading(
            section,
            r"What is the name of the President of the United States now",
        ),
        "vice_president": _names_for_heading(
            section,
            r"What is the name of the Vice President of the United States now",
        ),
        "speaker_of_the_house": _names_for_heading(
            section,
            r"What is the name of the Speaker of the House of Representatives now",
        ),
        "chief_justice": _names_for_heading(
            section,
            r"Who is the Chief Justice of the United States now",
        ),
        "source": TESTUPDATES_URL,
    }
    print("Executive:", {k: v for k, v in executive.items() if k != "source"})
    return executive


def apply_current_officials(
    questions: dict[str, Any], executive: dict[str, Any]
) -> dict[str, Any]:
    for number, field in CURRENT_OFFICIAL_QUESTIONS.items():
        name = (executive.get(field) or "").strip()
        if not name:
            continue
        key = str(number)
        if key not in questions:
            continue
        questions[key]["answers"] = [name]
    return questions


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


SORTNAME_RE = re.compile(
    r"\{\{\s*sortname\s*\|([^}|]+)\|([^}|]+)(?:\|([^}]+))?\}\}",
    re.I,
)
GOVERNOR_OF_LINK_RE = re.compile(
    r"\[\[Governor of ([^|\]]+)\|([^\]]+)\]\]",
    re.I,
)
WIKI_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|([^\]]+))?\]\]")


def _sortname_display(first: str, last: str, extra: str | None) -> str:
    first, last = first.strip(), last.strip()
    extra = (extra or "").strip()
    extra_low = extra.lower()
    if extra and not extra_low.startswith("dab=") and extra_low not in (
        "nolink",
        "nolink=1",
    ):
        if _looks_like_person_name(extra):
            return extra
    return f"{first} {last}".strip()


def parse_governors_wikitext(wikitext: str) -> list[dict[str, Any]]:
    """Read person names from the state-governors table, not office page titles."""
    section = re.search(
        r"==\s*State governors\s*==(.*?)==\s*Territory governors\s*==",
        wikitext,
        flags=re.S | re.I,
    )
    body = section.group(1) if section else wikitext
    table = re.search(r"\{\|(.*?)\|\}", body, flags=re.S)
    if not table:
        return []
    governors: dict[str, str] = {}
    for raw_row in re.split(r"\n\|-", table.group(1)):
        state_code = None
        for _office, label in GOVERNOR_OF_LINK_RE.findall(raw_row):
            label = label.strip()
            if label in STATE_NAMES:
                state_code = STATE_NAMES[label]
                break
        if not state_code:
            continue
        name = ""
        sort_match = SORTNAME_RE.search(raw_row)
        if sort_match:
            name = _sortname_display(*sort_match.groups())
        if not _looks_like_person_name(name):
            for target, display in WIKI_LINK_RE.findall(raw_row):
                target = target.strip()
                if target.lower().startswith(
                    ("file:", "governor of", "list of", "category:")
                ):
                    continue
                candidate = (display or target).strip()
                if candidate in STATE_NAMES:
                    continue
                if _looks_like_person_name(candidate):
                    name = candidate
                    break
        if _looks_like_person_name(name):
            governors[state_code] = name

    rows = [
        {"state": code, "name": governors[code], "state_name": name}
        for name, code in STATE_NAMES.items()
        if code in governors
    ]
    rows.sort(key=lambda r: r["state"])
    return rows


def fetch_governors() -> list[dict[str, Any]]:
    api = "https://en.wikipedia.org/w/api.php"
    print(f"GET {api} (governors wikitext)")
    resp = requests.get(
        api,
        params={
            "action": "parse",
            "page": "List_of_current_United_States_governors",
            "prop": "wikitext",
            "format": "json",
            "redirects": 1,
        },
        headers={"User-Agent": UA},
        timeout=45,
    )
    resp.raise_for_status()
    wikitext = resp.json()["parse"]["wikitext"]["*"]
    rows = parse_governors_wikitext(wikitext)
    missing = [code for code in STATE_NAMES.values() if code not in {r["state"] for r in rows}]
    print(f"Governors: {len(rows)}")
    if missing:
        raise RuntimeError(
            f"Governor scrape returned {len(rows)} states, missing {missing}"
        )
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
                f"New {label}: {new_ex.get(field) or '(empty)'} "
                f"(was {old_ex.get(field) or '(empty)'})"
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
    questions = apply_current_officials(questions, executive)
    senators, representatives = fetch_congress()
    governors = fetch_governors()
    _assert_clean(questions, executive, governors)
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


def _assert_clean(
    questions: dict[str, Any],
    executive: dict[str, Any],
    governors: list[dict[str, Any]] | None = None,
) -> None:
    missing = [str(i) for i in range(1, 129) if str(i) not in questions]
    empty = [
        k
        for k in (str(i) for i in range(1, 129))
        if not (questions.get(k) or {}).get("answers")
    ]
    noisy = []
    for k, q in questions.items():
        joined = " ".join(q.get("answers") or [])
        if FOOTER_RE.search(joined) or " of 19" in joined:
            noisy.append(k)
        if "uscis.gov" in joined.lower() and int(k) in CURRENT_OFFICIAL_QUESTIONS:
            noisy.append(k)
    print(
        f"Validation: missing={missing or 'none'} "
        f"empty_answers={empty or 'none'} footer_noise={noisy or 'none'}"
    )
    for field in (
        "president",
        "vice_president",
        "speaker_of_the_house",
        "chief_justice",
    ):
        value = executive.get(field)
        print(f"Validation executive.{field}={value!r}")
        if not isinstance(value, str) or not value or "," in value:
            print(f"WARNING: executive.{field} should be a single name")
    if governors is not None:
        codes = [row.get("state") for row in governors]
        names = [row.get("name") or "" for row in governors]
        missing = [code for code in STATE_NAMES.values() if code not in codes]
        junk = [
            f"{row.get('state')}:{row.get('name')}"
            for row in governors
            if not _looks_like_person_name(str(row.get("name") or ""))
        ]
        print(f"Validation governors={len(governors)} missing={missing or 'none'}")
        if missing or junk or len(governors) != 50:
            raise RuntimeError(
                f"Governors invalid: count={len(governors)} missing={missing} junk={junk}"
            )
        if len(set(codes)) != 50 or len(set(names)) != 50:
            raise RuntimeError("Governors look duplicated or incomplete")


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

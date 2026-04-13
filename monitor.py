"""
USCIS Civics Test Monitor
=========================
Checks the official USCIS 128-question PDF every Monday.
If anything changed since last check, sends you an email
with exactly what is different.

Required GitHub Secrets:
  GMAIL_USER     — your Gmail address (e.g. you@gmail.com)
  GMAIL_PASSWORD — a Gmail App Password (not your real password)
  NOTIFY_EMAIL   — email address to send alerts to (can be same)
"""

import hashlib
import json
import os
import re
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from pypdf import PdfReader
from io import BytesIO

# ── Config ────────────────────────────────────────────────────
PDF_URL = (
    "https://www.uscis.gov/sites/default/files/document/"
    "questions-and-answers/2025-Civics-Test-128-Questions-and-Answers.pdf"
)
SNAPSHOT_FILE = "last_snapshot.json"

GMAIL_USER     = os.environ.get("GMAIL_USER", "")
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")
NOTIFY_EMAIL   = os.environ.get("NOTIFY_EMAIL", GMAIL_USER)


# ── Download PDF ──────────────────────────────────────────────
def download_pdf(url: str) -> bytes:
    print(f"Downloading PDF from {url} ...")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    print(f"Downloaded {len(resp.content):,} bytes")
    return resp.content


# ── Extract text from PDF ─────────────────────────────────────
def extract_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(BytesIO(pdf_bytes))
    pages = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages.append(text.strip())
    return "\n".join(pages)


# ── Parse questions and answers from raw text ─────────────────
def parse_questions(raw_text: str) -> dict:
    """
    Returns a dict like:
    {
      "1": {"question": "...", "answers": ["...", "..."]},
      ...
    }
    """
    questions = {}

    # Split on question numbers like "1.", "2.", ... "128."
    pattern = re.compile(r'\n(\d{1,3})\.\s+(.+?)(?=\n\d{1,3}\.\s|\Z)', re.DOTALL)
    matches = pattern.findall(raw_text)

    for num, body in matches:
        lines = [l.strip() for l in body.strip().splitlines() if l.strip()]
        if not lines:
            continue

        # First line is the question text
        question_text = lines[0]

        # Remaining lines starting with • are answers
        answers = []
        for line in lines[1:]:
            clean = line.lstrip("•●▪-– ").strip()
            if clean and not clean.startswith("Visit uscis"):
                answers.append(clean)

        questions[num] = {
            "question": question_text,
            "answers": answers,
        }

    print(f"Parsed {len(questions)} questions")
    return questions


# ── Hash the full PDF bytes for quick change detection ────────
def pdf_hash(pdf_bytes: bytes) -> str:
    return hashlib.sha256(pdf_bytes).hexdigest()


# ── Load / save snapshot ──────────────────────────────────────
def load_snapshot() -> dict:
    if os.path.exists(SNAPSHOT_FILE):
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_snapshot(data: dict):
    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Snapshot saved to {SNAPSHOT_FILE}")


# ── Compare old vs new ────────────────────────────────────────
def find_changes(old: dict, new: dict) -> list:
    changes = []

    all_keys = set(old.keys()) | set(new.keys())

    for key in sorted(all_keys, key=lambda x: int(x)):
        if key not in old:
            changes.append({
                "type": "NEW QUESTION",
                "number": key,
                "new": new[key],
            })
        elif key not in new:
            changes.append({
                "type": "REMOVED QUESTION",
                "number": key,
                "old": old[key],
            })
        else:
            old_q = old[key]
            new_q = new[key]
            diffs = []

            if old_q["question"].strip() != new_q["question"].strip():
                diffs.append(
                    f"  Question text changed:\n"
                    f"    OLD: {old_q['question']}\n"
                    f"    NEW: {new_q['question']}"
                )

            old_ans = set(a.lower() for a in old_q.get("answers", []))
            new_ans = set(a.lower() for a in new_q.get("answers", []))

            added   = new_ans - old_ans
            removed = old_ans - new_ans

            if added:
                diffs.append(
                    f"  Answers ADDED:\n"
                    + "\n".join(f"    + {a}" for a in sorted(added))
                )
            if removed:
                diffs.append(
                    f"  Answers REMOVED:\n"
                    + "\n".join(f"    - {a}" for a in sorted(removed))
                )

            if diffs:
                changes.append({
                    "type": "CHANGED",
                    "number": key,
                    "detail": "\n".join(diffs),
                })

    return changes


# ── Format email body ─────────────────────────────────────────
def format_email(changes: list) -> str:
    lines = [
        "⚠️  USCIS Civics Test PDF has changed — action required",
        "=" * 60,
        f"Total changes found: {len(changes)}",
        "",
    ]

    for c in changes:
        lines.append(f"[{c['type']}] Question #{c['number']}")
        if c["type"] == "NEW QUESTION":
            lines.append(f"  Question: {c['new']['question']}")
            lines.append(f"  Answers:  {', '.join(c['new']['answers'])}")
        elif c["type"] == "REMOVED QUESTION":
            lines.append(f"  Question: {c['old']['question']}")
        elif c["type"] == "CHANGED":
            lines.append(c["detail"])
        lines.append("")

    lines += [
        "=" * 60,
        "Next steps:",
        "1. Open your VSCode project",
        "2. Update lib/questions_data.dart with the changes above",
        "3. Bump version in pubspec.yaml",
        "4. Run: flutter build appbundle --release",
        "5. Upload to Google Play Console",
        "",
        "Official PDF link:",
        PDF_URL,
    ]

    return "\n".join(lines)


# ── Send email ────────────────────────────────────────────────
def send_email(subject: str, body: str):
    if not GMAIL_USER or not GMAIL_PASSWORD:
        print("No email credentials set — printing alert to console instead:")
        print(subject)
        print(body)
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())
        print(f"Alert email sent to {NOTIFY_EMAIL}")
    except Exception as e:
        print(f"Failed to send email: {e}")
        # Still print to GitHub Actions log
        print(body)


# ── Main ──────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("USCIS Civics Test Monitor")
    print("=" * 60)

    # Download current PDF
    try:
        pdf_bytes = download_pdf(PDF_URL)
    except Exception as e:
        print(f"ERROR downloading PDF: {e}")
        send_email(
            "⚠️ USCIS Monitor — Download Failed",
            f"Could not download the USCIS PDF.\n\nError: {e}\n\nURL: {PDF_URL}"
        )
        sys.exit(1)

    current_hash = pdf_hash(pdf_bytes)
    print(f"PDF hash: {current_hash[:16]}...")

    # Load previous snapshot
    snapshot = load_snapshot()
    last_hash = snapshot.get("hash", "")

    # Quick hash check — if identical, nothing changed
    if current_hash == last_hash:
        print("✅ No changes detected — PDF hash is identical. Nothing to do.")
        return

    print("⚠️  PDF hash changed — parsing content to find differences...")

    # Parse current PDF
    raw_text    = extract_text(pdf_bytes)
    current_qs  = parse_questions(raw_text)

    # Get previous questions
    previous_qs = snapshot.get("questions", {})

    if not previous_qs:
        print("No previous snapshot found — saving current as baseline.")
        save_snapshot({"hash": current_hash, "questions": current_qs})
        print("✅ Baseline saved. Next run will compare against this.")
        return

    # Compare
    changes = find_changes(previous_qs, current_qs)

    if not changes:
        print("✅ Hash changed but no question differences found (possibly formatting).")
        # Update hash so we don't re-alert on same non-change
        save_snapshot({"hash": current_hash, "questions": current_qs})
        return

    # Changes found — send alert
    print(f"🚨 {len(changes)} change(s) found!")
    body = format_email(changes)
    print(body)

    send_email(
        f"🚨 USCIS Civics Test Updated — {len(changes)} change(s) found",
        body,
    )

    # Save new snapshot
    save_snapshot({"hash": current_hash, "questions": current_qs})
    print("Done.")


if __name__ == "__main__":
    main()

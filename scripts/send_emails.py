#!/usr/bin/env python3
"""
Website HQ - Daily Outreach Sender
-----------------------------------
Reads "Ready" leads from the Notion "Main" data source, sends the
pre-written plain-text email for each one through Gmail, and writes the
result back to Notion (Status + Sent Date).

This script NEVER touches "Raw Data". It only talks to the "Main" and
"Settings" data sources, through a Notion integration token that is only
ever shared with Main/Later/Settings - never with Raw Data. That is a
deliberate safety boundary: this automated script can never run "Sync
Leads" or see/alter the raw lead list.

All configuration comes from environment variables (set via GitHub
Actions secrets/inputs - see .github/workflows/send-emails.yml and
README.md). Nothing is hardcoded here, so this file is safe to keep in a
public repo.
"""

import json
import os
import random
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import formataddr
from zoneinfo import ZoneInfo

NOTION_VERSION = "2025-09-03"
EASTERN = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Config (from environment)
# ---------------------------------------------------------------------------

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
MAIN_DATA_SOURCE_ID = os.environ.get("MAIN_DATA_SOURCE_ID", "")
SETTINGS_DATA_SOURCE_ID = os.environ.get("SETTINGS_DATA_SOURCE_ID", "")

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
SENDER_NAME = os.environ.get("SENDER_NAME", "Ayush")

# Optional overrides, mainly used for manual test runs (workflow_dispatch).
DRY_RUN_OVERRIDE = os.environ.get("DRY_RUN_OVERRIDE", "").strip().lower()  # "", "true", "false"
TEST_LIMIT = os.environ.get("TEST_LIMIT", "").strip()  # "" or an integer string
TEST_EMAIL_OVERRIDE = os.environ.get("TEST_EMAIL_OVERRIDE", "").strip()  # "" or an address
ALLOW_WHILE_PAUSED = os.environ.get("ALLOW_WHILE_PAUSED", "false").strip().lower() == "true"
SEND_DELAY_MIN_SECONDS = float(os.environ.get("SEND_DELAY_MIN_SECONDS", "40") or "40")
SEND_DELAY_MAX_SECONDS = float(os.environ.get("SEND_DELAY_MAX_SECONDS", "80") or "80")

IS_SCHEDULED = os.environ.get("GITHUB_EVENT_NAME", "") == "schedule"


# ---------------------------------------------------------------------------
# Notion helpers
# ---------------------------------------------------------------------------

def notion_request(method: str, path: str, body=None):
    url = f"https://api.notion.com/v1{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {NOTION_TOKEN}")
    req.add_header("Notion-Version", NOTION_VERSION)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        raise RuntimeError(f"Notion API error {e.code} on {method} {path}: {err_body}") from None


def _plain_text_from_rich(rich_list):
    return "".join(part.get("plain_text", "") for part in (rich_list or []))


def _title(prop):
    if not prop:
        return ""
    return _plain_text_from_rich(prop.get("title"))


def _rich_text(prop):
    if not prop:
        return ""
    return _plain_text_from_rich(prop.get("rich_text"))


def _email(prop):
    if not prop:
        return ""
    return prop.get("email") or ""


def get_settings():
    result = notion_request(
        "POST", f"/data_sources/{SETTINGS_DATA_SOURCE_ID}/query", {"page_size": 10}
    )
    rows = result.get("results", [])
    chosen = None
    for row in rows:
        props = row["properties"]
        if _title(props.get("Setting")) == "Current":
            chosen = props
            break
    if chosen is None and rows:
        chosen = rows[0]["properties"]
    if chosen is None:
        raise RuntimeError("Settings data source has no rows - add a 'Current' row first.")

    daily_limit_prop = chosen.get("Daily Limit", {})
    daily_limit = daily_limit_prop.get("number")
    return {
        "daily_limit": int(daily_limit) if daily_limit else 75,
        "dry_run": bool(chosen.get("Dry Run", {}).get("checkbox")),
        "pause_sending": bool(chosen.get("Pause Sending", {}).get("checkbox")),
        "send_days": [opt["name"] for opt in chosen.get("Send Days", {}).get("multi_select", [])],
    }


def query_ready_leads(limit: int):
    body = {
        "filter": {"property": "Status", "select": {"equals": "Ready"}},
        "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
        "page_size": min(max(limit, 1), 100),
    }
    result = notion_request("POST", f"/data_sources/{MAIN_DATA_SOURCE_ID}/query", body)
    leads = []
    for row in result.get("results", [])[:limit]:
        props = row["properties"]
        leads.append(
            {
                "page_id": row["id"],
                "business_name": _title(props.get("Business Name")),
                "email": _email(props.get("Email")),
                "subject": _rich_text(props.get("Email Subject")),
                "message": _rich_text(props.get("Email Message")),
            }
        )
    return leads


def update_lead_status(page_id: str, status: str, sent_date_iso: str = None):
    props = {"Status": {"select": {"name": status}}}
    if sent_date_iso:
        props["Sent Date"] = {"date": {"start": sent_date_iso}}
    notion_request("PATCH", f"/pages/{page_id}", {"properties": props})


# ---------------------------------------------------------------------------
# Email sending
# ---------------------------------------------------------------------------

def send_email(to_addr: str, subject: str, body: str):
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = formataddr((SENDER_NAME, GMAIL_ADDRESS))
    msg["To"] = to_addr

    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls(context=context)
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [to_addr], msg.as_string())


# ---------------------------------------------------------------------------
# Scheduling guards
# ---------------------------------------------------------------------------

SCHEDULE_CRON = os.environ.get("SCHEDULE_CRON", "").strip()

# Which US-Eastern UTC offset (in hours) each cron firing is meant for.
CRON_EXPECTED_OFFSET = {
    "30 13 * * 1-6": -4,  # 9:30 AM EDT
    "30 14 * * 1-6": -5,  # 9:30 AM EST
}


def in_send_time_window(now_et: datetime):
    """Return (ok, reason).

    The workflow fires twice a day (once for each possible UTC offset, to
    cover both EST and EDT). GitHub often starts scheduled runs many minutes
    - sometimes over an hour - late, so we must NOT demand an exact minute.
    Instead: the firing is valid only if it is the one whose UTC offset
    matches the real US-Eastern offset right now (so exactly one of the two
    firings ever sends per day), and it is still morning in the US.
    """
    expected = CRON_EXPECTED_OFFSET.get(SCHEDULE_CRON)
    if expected is not None:
        offset = now_et.utcoffset().total_seconds() / 3600
        if offset != expected:
            return False, "this is the other (daylight-saving) firing - nothing to do"
    if now_et.hour < 9:
        return False, "too early (before 9:00 AM US Eastern)"
    if now_et.hour >= 13:
        return False, "fired too late (after 1:00 PM US Eastern); skipping today's batch"
    return True, ""


def today_name(now_et: datetime) -> str:
    return now_et.strftime("%A")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def fail(message: str, code: int = 1):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


def main():
    missing = [
        name
        for name in ("NOTION_TOKEN", "MAIN_DATA_SOURCE_ID", "SETTINGS_DATA_SOURCE_ID", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")
        if not os.environ.get(name)
    ]
    if missing:
        fail(f"Missing required secrets/variables: {', '.join(missing)}")

    now_et = datetime.now(EASTERN)
    print(f"Current time (US Eastern): {now_et.isoformat()}")
    print(f"Triggered by: {os.environ.get('GITHUB_EVENT_NAME', 'unknown')}")

    settings = get_settings()
    print(f"Settings from Notion: {settings}")

    if IS_SCHEDULED:
        in_window, why = in_send_time_window(now_et)
        if not in_window:
            print(f"Not sending on this firing: {why}. Exiting.")
            # The late-firing case is a missed day, so make it loud (GitHub
            # emails failed runs). The other firing is the normal quiet no-op.
            sys.exit(1 if "too late" in why else 0)
        if today_name(now_et) not in settings["send_days"]:
            print(f"{today_name(now_et)} is not a configured send day. Exiting.")
            sys.exit(0)
        if settings["pause_sending"]:
            print("Pause Sending is ON in Notion Settings. Exiting without sending.")
            sys.exit(0)
    else:
        print("Manual run (workflow_dispatch) - skipping day/time window check.")
        if settings["pause_sending"] and not ALLOW_WHILE_PAUSED:
            fail(
                "Pause Sending is ON in Notion Settings. Turn it off, or re-run this "
                "workflow with 'ignore_pause' checked if you really want to run while paused."
            )

    dry_run = settings["dry_run"]
    if DRY_RUN_OVERRIDE == "true":
        dry_run = True
    elif DRY_RUN_OVERRIDE == "false":
        dry_run = False

    try:
        limit = int(TEST_LIMIT) if TEST_LIMIT else settings["daily_limit"]
    except ValueError:
        fail(f"TEST_LIMIT must be an integer, got: {TEST_LIMIT!r}")
    if limit <= 0:
        fail(f"Daily limit must be a positive number, got: {limit}")

    test_mode = bool(TEST_EMAIL_OVERRIDE)
    if test_mode:
        print(f"TEST MODE: every email in this run will be redirected to {TEST_EMAIL_OVERRIDE}")
        print("Lead statuses will NOT be changed in test mode, so no real leads are consumed.")

    print(f"Dry run: {dry_run} | Limit: {limit}")

    leads = query_ready_leads(limit)
    if not leads:
        print("No 'Ready' leads found in Main. Nothing to do.")
        sys.exit(0)

    print(f"Found {len(leads)} ready lead(s).")

    sent, failed, skipped_bad_email = 0, 0, 0

    for i, lead in enumerate(leads):
        label = lead["business_name"] or lead["page_id"]
        if not lead["email"]:
            print(f"[{label}] SKIPPED - no email on file.")
            skipped_bad_email += 1
            continue
        if not lead["subject"] or not lead["message"]:
            print(f"[{label}] SKIPPED - missing Email Subject or Email Message.")
            skipped_bad_email += 1
            continue

        recipient = TEST_EMAIL_OVERRIDE if test_mode else lead["email"]
        subject = f"[TEST] {lead['subject']}" if test_mode else lead["subject"]

        if dry_run:
            print(f"[DRY RUN] Would send to {recipient} (lead: {label}) | Subject: {subject}")
            sent += 1
        else:
            if not test_mode:
                update_lead_status(lead["page_id"], "Sending")
            try:
                send_email(recipient, subject, lead["message"])
            except Exception as exc:  # noqa: BLE001 - we want to log and keep going
                print(f"[{label}] FAILED to send to {recipient}: {exc}", file=sys.stderr)
                failed += 1
                if not test_mode:
                    update_lead_status(lead["page_id"], "Failed")
            else:
                print(f"[{label}] Sent to {recipient}.")
                sent += 1
                if not test_mode:
                    update_lead_status(
                        lead["page_id"], "Sent", now_et.astimezone(ZoneInfo("UTC")).isoformat()
                    )

        if i < len(leads) - 1 and not dry_run:
            delay = random.uniform(SEND_DELAY_MIN_SECONDS, SEND_DELAY_MAX_SECONDS)
            print(f"Waiting {delay:.0f}s before the next send...")
            time.sleep(delay)

    summary = (
        f"Done. Sent/would-send: {sent} | Failed: {failed} | "
        f"Skipped (bad data): {skipped_bad_email}"
    )
    print(summary)

    step_summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary_path:
        with open(step_summary_path, "a") as f:
            f.write(f"### Website HQ - Send run\n\n{summary}\n\n")
            f.write(f"- Dry run: {dry_run}\n- Test mode: {test_mode}\n- Limit: {limit}\n")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

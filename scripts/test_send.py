#!/usr/bin/env python3
"""
Website HQ - Fake-email delivery test
-------------------------------------
Sends FAKE outreach emails (made-up businesses, subject and message shaped
like the real ones) to the addresses typed into the temporary Notion table
"Test Recipients (Temp)". It uses the exact same Gmail sending code and the
same 40-80 second gaps as the real sender, so it simulates a real run.

It does NOT read or change any real lead in Main, and it never touches
Raw Data. The only Notion data it reads/writes is the temp recipients table
(Status and Result columns), through the same integration token.
"""

import os
import random
import sys
import time

# Reuse the real sender's helpers so the test goes through the same code path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import send_emails as real  # noqa: E402

RECIPIENTS_DATA_SOURCE_ID = os.environ.get("RECIPIENTS_DATA_SOURCE_ID", "").strip()
DEFAULT_PER_ADDRESS = int(os.environ.get("EMAILS_PER_ADDRESS", "5") or "5")

# Fake leads. Shape matches the real emails: greeting / opener with the flaw /
# pitch / Zoom offer / sign-off.
FAKE_LEADS = [
    ("Maple Ridge Roofing", "the phone number in your header is missing a digit, so tapping it to call goes nowhere",
     " There were a few more like that too, and every one of them is probably costing you a lead right now."),
    ("Harborview Landscaping", "your contact form says 'Message sent' but the page never actually submits anything",
     " A couple more things like that were scattered around the site, quietly costing you business every week they go unfixed."),
    ("Stonebridge Plumbing", "the 'Get a Quote' button on your homepage links to a page that no longer exists",
     " I noticed a few more like it as well, small issues that add up to real leads slipping away."),
    ("Bluebird Custom Cabinets", "the Facebook link in your footer is spelled wrong and opens an error page",
     " There were a few more like that too, and every one of them is probably costing you a lead right now."),
    ("Redwood Ridge Contracting", "your service-area page still lists a phone number from your old office",
     " A couple more things like that were scattered around the site, quietly costing you business every week they go unfixed."),
]

SUBJECT_STYLES = [
    "A thought on {b}'s website",
    "Website idea for {b}",
    "An idea for {b}'s website",
]


def build_email(n: int):
    """n is 1-based. Returns (subject, body)."""
    business, flaw, extra = FAKE_LEADS[(n - 1) % len(FAKE_LEADS)]
    subject = SUBJECT_STYLES[(n - 1) % len(SUBJECT_STYLES)].format(b=business)
    body = (
        f"Hi {business},\n\n"
        f"I was looking at your website and noticed {flaw}. "
        f"That can cost you enquiries from people who find you online.{extra}\n\n"
        "I build websites for businesses, and I'd be happy to make a free preview of an "
        'improved version for you. Just reply "yes" and I\'ll send it over, no strings attached.\n\n'
        "If you'd also like to talk it through, I'm happy to do a quick 10-minute Zoom call "
        "whenever suits you.\n\n"
        "Ayush"
    )
    return subject, body


def mask(addr: str) -> str:
    # Logs are public in a public repo, so never print a full address.
    local, _, domain = addr.partition("@")
    return f"{local[:2]}***@{domain[:1]}***"


def get_recipients():
    result = real.notion_request(
        "POST", f"/data_sources/{RECIPIENTS_DATA_SOURCE_ID}/query", {"page_size": 50}
    )
    out = []
    for row in result.get("results", []):
        props = row["properties"]
        addr = real._email(props.get("Email"))
        if not addr:
            continue
        n = props.get("Emails to send", {}).get("number")
        out.append({"page_id": row["id"], "email": addr, "count": int(n) if n else DEFAULT_PER_ADDRESS})
    return out


def set_row(page_id: str, status: str, result: str = None):
    props = {"Status": {"select": {"name": status}}}
    if result is not None:
        props["Result"] = {"rich_text": [{"type": "text", "text": {"content": result[:1900]}}]}
    real.notion_request("PATCH", f"/pages/{page_id}", {"properties": props})


def main():
    missing = [n for n in ("NOTION_TOKEN", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "RECIPIENTS_DATA_SOURCE_ID")
               if not os.environ.get(n)]
    if missing:
        real.fail(f"Missing required secrets/variables: {', '.join(missing)}")

    recipients = get_recipients()
    if not recipients:
        print("No email addresses found in the Test Recipients table. Nothing to do.")
        sys.exit(0)

    total = sum(r["count"] for r in recipients)
    print(f"{len(recipients)} recipient(s), {total} fake email(s) in total.")
    print(f"Gap between sends: {real.SEND_DELAY_MIN_SECONDS:.0f}-{real.SEND_DELAY_MAX_SECONDS:.0f}s")

    sent_total, failed_total, sent_so_far = 0, 0, 0
    for r in recipients:
        label = mask(r["email"])
        set_row(r["page_id"], "Sending")
        sent, failed, errors = 0, 0, []
        for n in range(1, r["count"] + 1):
            subject, body = build_email(n)
            subject = f"[TEST {n}/{r['count']}] {subject}"
            try:
                real.send_email(r["email"], subject, body)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f"#{n}: {type(exc).__name__}")
                print(f"[{label}] #{n} FAILED: {exc}", file=sys.stderr)
            else:
                sent += 1
                print(f"[{label}] #{n} sent.")
            sent_so_far += 1
            if sent_so_far < total:
                delay = random.uniform(real.SEND_DELAY_MIN_SECONDS, real.SEND_DELAY_MAX_SECONDS)
                print(f"Waiting {delay:.0f}s before the next send...")
                time.sleep(delay)
        sent_total += sent
        failed_total += failed
        summary = f"{sent} sent, {failed} failed" + (f" ({'; '.join(errors)})" if errors else "")
        set_row(r["page_id"], "Failed" if failed else "Done", summary)

    print(f"Done. Sent: {sent_total} | Failed: {failed_total}")
    if failed_total:
        sys.exit(1)


if __name__ == "__main__":
    main()

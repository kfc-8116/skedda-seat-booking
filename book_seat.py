#!/usr/bin/env python3
"""
Skedda Seat Booking Automation
Logs into Skedda via the web and books a seat using the internal API.
Tries seats in priority order — if the first choice is taken, falls back to the next.

Usage:
    python book_seat.py              # Book 12 days ahead (default)
    python book_seat.py --days 1     # Book tomorrow
    python book_seat.py --dry-run    # Show what would be booked without booking
    python book_seat.py --date 2026-04-01  # Book a specific date
"""

import argparse
import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).parent / ".env")

SKEDDA_URL    = os.getenv("SKEDDA_URL", "").rstrip("/")
SKEDDA_EMAIL  = os.getenv("SKEDDA_EMAIL", "")
SKEDDA_PASS   = os.getenv("SKEDDA_PASS", "")
BOOKING_START = os.getenv("BOOKING_START", "09:00")
BOOKING_END   = os.getenv("BOOKING_END",   "18:00")
DAYS_AHEAD    = int(os.getenv("DAYS_AHEAD", "12"))
BOOKING_DAYS  = set(int(d) for d in os.getenv("BOOKING_DAYS", "0,1,2,3,4").split(","))
VENUE_ID      = os.getenv("VENUE_ID", "202392")

# Priority-ordered seat list: "name:id,name:id,..."
# e.g. "GEMS 1:1281271,DOM 2:1273200,GEMS 5:1281275"
SEAT_PRIORITY_RAW = os.getenv("SEAT_PRIORITY", "")

# Parse into list of (name, space_id) tuples
SEAT_PRIORITY = []
for entry in SEAT_PRIORITY_RAW.split(","):
    entry = entry.strip()
    if ":" in entry:
        name, sid = entry.rsplit(":", 1)
        SEAT_PRIORITY.append((name.strip(), sid.strip()))

# Fallback: support old single-seat config
if not SEAT_PRIORITY:
    space_id = os.getenv("SPACE_ID", "")
    seat_name = os.getenv("SEAT_NAME", "")
    if space_id:
        SEAT_PRIORITY = [(seat_name, space_id)]

LOGIN_URL = "https://app.skedda.com/account/login"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DAY_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday",
             3: "Thursday", 4: "Friday", 5: "Saturday", 6: "Sunday"}


def resolve_target_date(days_ahead: int, explicit_date: str | None) -> date | None:
    if explicit_date:
        target = date.fromisoformat(explicit_date)
    else:
        target = date.today() + timedelta(days=days_ahead)

    if target.weekday() not in BOOKING_DAYS:
        log.info("Target date %s is a %s — not a booking day, skipping.",
                 target, DAY_NAMES[target.weekday()])
        return None
    return target


def validate_config() -> bool:
    missing = [k for k, v in {
        "SKEDDA_URL":   SKEDDA_URL,
        "SKEDDA_EMAIL": SKEDDA_EMAIL,
        "SKEDDA_PASS":  SKEDDA_PASS,
    }.items() if not v]
    if not SEAT_PRIORITY:
        missing.append("SEAT_PRIORITY")
    if missing:
        log.error("Missing required config: %s — fill in .env", ", ".join(missing))
        return False
    return True


# ---------------------------------------------------------------------------
# Core: API-based booking via authenticated Playwright session
# ---------------------------------------------------------------------------

def _try_book(page, space_id: str, seat_name: str, start_dt: str, end_dt: str,
              venueuser_id: str) -> str:
    """
    Attempt to book a single seat. Returns:
      "ok"       — booking created
      "conflict" — seat already taken
      "error"    — unexpected failure
    """
    result = page.evaluate("""async (params) => {
        const token = document.querySelector(
            'input[name="__RequestVerificationToken"]'
        )?.value;

        const resp = await fetch('/bookings', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Skedda-RequestVerificationToken': token || '',
            },
            body: JSON.stringify({
                booking: {
                    start: params.start,
                    end: params.end,
                    spaces: [params.spaceId],
                    venueuser: params.venueuserId,
                    venue: params.venueId,
                    title: null,
                    price: 0,
                    type: 1,
                    addOns: [],
                    attendees: null,
                    addConference: false,
                    syncToExternalCalendar: false,
                }
            }),
        });
        return { status: resp.status, body: await resp.text() };
    }""", {
        "start": start_dt,
        "end": end_dt,
        "spaceId": space_id,
        "venueuserId": venueuser_id,
        "venueId": VENUE_ID,
    })

    status = result["status"]
    body = result["body"]

    if status in (200, 201):
        return "ok"

    try:
        err = json.loads(body)
        detail = err.get("errors", [{}])[0].get("detail", body)
    except Exception:
        detail = body[:300]

    if "conflict" in detail.lower():
        log.info("  %s is taken: %s", seat_name, detail.split(".")[0])
        return "conflict"

    log.error("  %s booking failed (HTTP %s): %s", seat_name, status, detail)
    return "error"


def book_seat(target: date, dry_run: bool = False) -> bool:
    date_str = target.isoformat()
    start_dt = f"{date_str}T{BOOKING_START}:00"
    end_dt   = f"{date_str}T{BOOKING_END}:00"

    priority_str = " → ".join(name for name, _ in SEAT_PRIORITY)
    log.info("Booking for %s  %s–%s  priority: [%s]%s",
             target, BOOKING_START, BOOKING_END, priority_str,
             "  [DRY RUN]" if dry_run else "")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            # --- Login ---
            log.info("Logging in as %s ...", SKEDDA_EMAIL)
            page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)
            page.fill("#login-email", SKEDDA_EMAIL)
            page.fill("#login-password", SKEDDA_PASS)
            page.click("button[type='submit']")
            page.wait_for_timeout(5000)

            if "/login" in page.url:
                log.error("Login failed — still on login page. Check credentials.")
                return False
            log.info("Logged in. Redirected to %s", page.url)

            # --- Navigate to venue booking page to get CSRF token ---
            booking_page = f"{SKEDDA_URL}/booking"
            page.goto(booking_page, wait_until="networkidle", timeout=30_000)
            page.wait_for_timeout(2000)

            # --- Get venueuser ID ---
            venueuser_id = page.evaluate("""async () => {
                try {
                    const token = document.querySelector(
                        'input[name="__RequestVerificationToken"]'
                    )?.value;
                    const r = await fetch('/webs', {
                        headers: {
                            'X-Skedda-RequestVerificationToken': token || '',
                        },
                    });
                    const data = await r.json();
                    return data.venueusers?.[0]?.id || null;
                } catch { return null; }
            }""")

            if not venueuser_id:
                log.error("Could not determine venueuser ID.")
                return False
            log.info("Venueuser ID: %s", venueuser_id)

            if dry_run:
                for name, sid in SEAT_PRIORITY:
                    log.info("[DRY RUN] Would try: %s (space %s)", name, sid)
                return True

            # --- Try each seat in priority order ---
            for i, (name, sid) in enumerate(SEAT_PRIORITY, 1):
                log.info("Attempt %d/%d: %s (space %s)",
                         i, len(SEAT_PRIORITY), name, sid)

                outcome = _try_book(page, sid, name, start_dt, end_dt, venueuser_id)

                if outcome == "ok":
                    log.info("Booked %s on %s.", name, target)
                    return True
                elif outcome == "conflict":
                    continue  # try next seat
                else:
                    return False  # unexpected error, stop

            log.warning("All %d seats are taken on %s.", len(SEAT_PRIORITY), target)
            return True  # not a script error

        except Exception as exc:
            log.exception("Unexpected error: %s", exc)
            return False

        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Book a Skedda seat automatically.")
    parser.add_argument("--days",    type=int,  default=DAYS_AHEAD,
                        help=f"Days ahead to book (default: {DAYS_AHEAD})")
    parser.add_argument("--date",    type=str,  default=None,
                        help="Book a specific date (YYYY-MM-DD), overrides --days")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate without actually creating the booking")
    args = parser.parse_args()

    if not validate_config():
        sys.exit(1)

    target = resolve_target_date(args.days, args.date)
    if target is None:
        log.info("No booking needed today.")
        sys.exit(0)

    success = book_seat(target, dry_run=args.dry_run)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

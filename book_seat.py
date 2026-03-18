#!/usr/bin/env python3
"""
Skedda Seat Booking Automation
Logs into Skedda via the web and books a seat using the internal API.

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
SPACE_ID      = os.getenv("SPACE_ID", "")
SEAT_NAME     = os.getenv("SEAT_NAME", "")
BOOKING_START = os.getenv("BOOKING_START", "09:00")
BOOKING_END   = os.getenv("BOOKING_END",   "18:00")
DAYS_AHEAD    = int(os.getenv("DAYS_AHEAD", "12"))
BOOKING_DAYS  = set(int(d) for d in os.getenv("BOOKING_DAYS", "0,1,2,3,4").split(","))

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
        "SPACE_ID":     SPACE_ID,
    }.items() if not v]
    if missing:
        log.error("Missing required config: %s — fill in .env", ", ".join(missing))
        return False
    return True


# ---------------------------------------------------------------------------
# Core: API-based booking via authenticated Playwright session
# ---------------------------------------------------------------------------

def book_seat(target: date, dry_run: bool = False) -> bool:
    """
    1. Open a headless browser and log into Skedda.
    2. Navigate to the venue booking page (establishes cookies + CSRF).
    3. POST /bookings via in-page fetch to create the booking.
    """
    date_str = target.isoformat()
    start_dt = f"{date_str}T{BOOKING_START}:00"
    end_dt   = f"{date_str}T{BOOKING_END}:00"

    log.info("Booking %s (space %s) on %s  %s–%s%s",
             SEAT_NAME or "seat", SPACE_ID, target,
             BOOKING_START, BOOKING_END,
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

            # --- Get user info (venueuser ID) from /webs ---
            venueuser_id = page.evaluate("""() => {
                const el = document.querySelector('[data-venueuser-id]');
                return el ? el.getAttribute('data-venueuser-id') : null;
            }""")

            if not venueuser_id:
                # Fetch from /webs via intercepted data in Ember store
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
                log.info("[DRY RUN] Would book space %s (%s) on %s %s–%s. Stopping.",
                         SPACE_ID, SEAT_NAME, target, BOOKING_START, BOOKING_END)
                return True

            # --- Create booking via API ---
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
                "spaceId": SPACE_ID,
                "venueuserId": venueuser_id,
                "venueId": "202392",
            })

            status = result["status"]
            body = result["body"]

            if status in (200, 201):
                log.info("Booking created successfully for %s on %s.", SEAT_NAME, target)
                return True

            # Parse error
            try:
                err = json.loads(body)
                detail = err.get("errors", [{}])[0].get("detail", body)
            except Exception:
                detail = body[:300]

            if "conflict" in detail.lower():
                log.info("Seat %s on %s is already booked — nothing to do.", SEAT_NAME, target)
                return True  # not an error, just already taken
            else:
                log.error("Booking failed (HTTP %s): %s", status, detail)
                return False

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

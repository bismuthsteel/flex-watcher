"""
Athlon Flex showroom checker — GitHub Actions edition.

Runs once per invocation (cron triggers it), scrapes the showroom, and sends a
Telegram message for any NEW car that matches a filter in filters.yml.

State (the nulmeting baseline = car UUIDs already seen) lives in state.json,
which the workflow commits back to the repo so it survives between runs.

Env vars (set as GitHub Secrets):
    TG_BOT_TOKEN   required
    TG_CHAT_ID     required

Files:
    filters.yml    what to watch for (you edit this)
    state.json     baseline + seen UUIDs (managed automatically)
"""

from __future__ import annotations

import os
import re
import sys
import json
import html
import logging
from dataclasses import dataclass
from typing import Optional

import requests
import yaml

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TG_CHAT_ID", "")

FILTERS_PATH = os.getenv("FILTERS_PATH", "filters.yml")
STATE_PATH   = os.getenv("STATE_PATH", "state.json")

DEFAULT_SHOWROOM = "https://flex.athlon.com/app/showroom"
DEFAULT_FUEL     = "Elektrisch"
DEFAULT_MAX      = 60_000

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("check")

CAR_LINK_RE = re.compile(
    r"/app/showroom/(?P<brand>[^/]+)/(?P<model>[^/]+)/(?P<id>[0-9a-fA-F-]{16,})")

# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #

@dataclass
class Car:
    id: str
    brand: str
    model: str
    url: str
    fuel: Optional[str] = None
    fiscal_value: Optional[int] = None


def matches(car: Car, f: dict) -> bool:
    brand = str(f.get("brand", "*"))
    model = str(f.get("model", "*"))
    fuel  = str(f.get("fuel", DEFAULT_FUEL))
    mx    = int(f.get("max_value", DEFAULT_MAX))
    if brand != "*" and brand.lower() != car.brand.lower():
        return False
    if model != "*" and model.lower() not in car.model.lower():   # "contains"
        return False
    if car.fuel is not None and fuel.lower() not in car.fuel.lower():
        return False
    if car.fiscal_value is None:
        return False
    return car.fiscal_value < mx


# --------------------------------------------------------------------------- #
# State (nulmeting baseline)
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"baseline_done": False, "seen": []}
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        data.setdefault("baseline_done", False)
        data.setdefault("seen", [])
        return data
    except Exception:
        log.warning("state.json unreadable, starting fresh")
        return {"baseline_done": False, "seen": []}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def load_filters() -> dict:
    with open(FILTERS_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

def tg(method: str, **params):
    r = requests.post(f"{API}/{method}", json=params, timeout=60)
    data = r.json()
    if not data.get("ok"):
        log.error("Telegram %s failed: %s", method, data.get("description"))
    return data


def send(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing TG_BOT_TOKEN / TG_CHAT_ID — would send:\n%s", text)
        return
    tg("sendMessage", chat_id=CHAT_ID, text=text,
       parse_mode="HTML", disable_web_page_preview=False)


def alert(car: Car) -> None:
    val = f"€ {car.fiscal_value:,}".replace(",", ".") if car.fiscal_value else "?"
    send(f"🚗 <b>Nieuwe match!</b>\n"
         f"<b>{html.escape(car.brand)} {html.escape(car.model)}</b>\n\n"
         f"• Brandstof: <b>{html.escape(car.fuel or '?')}</b>\n"
         f"• Fiscale waarde: <b>{val}</b>\n\n"
         f'👉 <a href="{html.escape(car.url)}">Bekijken op Athlon</a>')


# --------------------------------------------------------------------------- #
# Scraper (Playwright)
# --------------------------------------------------------------------------- #

class Scraper:
    def __init__(self, showroom_url: str):
        self.showroom_url = showroom_url
        self._pw = self._browser = self._ctx = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._ctx = self._browser.new_context(
            locale="nl-NL",
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
        return self

    def __exit__(self, *exc):
        try:
            self._browser.close()
        finally:
            self._pw.stop()

    def _cookies(self, page):
        for sel in ('button:has-text("Accepteren")',
                    'button:has-text("Alles accepteren")',
                    'button:has-text("Alleen noodzakelijk")'):
            try:
                page.locator(sel).first.click(timeout=2500)
                return
            except Exception:
                continue

    def showroom(self) -> list[Car]:
        from playwright.sync_api import TimeoutError as PWTimeout
        page = self._ctx.new_page()
        try:
            page.goto(self.showroom_url, wait_until="networkidle", timeout=60_000)
            self._cookies(page)
            try:
                page.wait_for_selector('a[href*="/app/showroom/"]', timeout=20_000)
            except PWTimeout:
                log.warning("No car links appeared.")
            # If cars lazy-load on scroll, nudge the page a few times.
            for _ in range(6):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(800)
            hrefs = page.eval_on_selector_all(
                'a[href*="/app/showroom/"]',
                "els => els.map(e => e.getAttribute('href'))")
        finally:
            page.close()

        cars: dict[str, Car] = {}
        for href in hrefs or []:
            m = CAR_LINK_RE.search(href or "")
            if not m:
                continue
            cid = m.group("id")
            cars[cid] = Car(cid,
                            m.group("brand").replace("-", " ").strip(),
                            m.group("model").replace("-", " ").strip(),
                            _abs(href))
        log.info("Showroom: %d distinct cars found.", len(cars))
        return list(cars.values())

    def detail(self, car: Car) -> Car:
        from playwright.sync_api import TimeoutError as PWTimeout
        page = self._ctx.new_page()
        try:
            page.goto(car.url, wait_until="networkidle", timeout=45_000)
            self._cookies(page)
            try:
                page.wait_for_selector("text=Brandstofsoort", timeout=15_000)
            except PWTimeout:
                pass
            text = page.inner_text("body")
        finally:
            page.close()
        car.fuel = _field(text, "Brandstofsoort")
        car.fiscal_value = _euro(_field(text, "Fiscale waarde"))
        log.info("  detail %s %s -> fuel=%r value=%s",
                 car.brand, car.model, car.fuel, car.fiscal_value)
        return car


def _abs(href: str) -> str:
    if href.startswith("http"):
        return href
    return "https://flex.athlon.com" + (href if href.startswith("/") else "/" + href)

def _field(text: str, label: str) -> Optional[str]:
    m = re.search(rf"{re.escape(label)}\s*[:\n]?\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    return m.group(1).strip() if m else None

def _euro(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    cleaned = re.sub(r"[^\d,\.]", "", value).replace(".", "")
    if "," in cleaned:
        cleaned = cleaned.split(",")[0]
    return int(cleaned) if cleaned.isdigit() else None


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    cfg = load_filters()
    showroom_url = cfg.get("showroom_url", DEFAULT_SHOWROOM)
    filters = cfg.get("filters", [])
    state = load_state()
    seen = set(state["seen"])

    with Scraper(showroom_url) as s:
        cars = s.showroom()

        # SAFETY GUARD: a 0-car scrape means the site changed or blocked us.
        # Do NOT touch state, or the next run would treat everything as "new".
        if not cars:
            log.error("Scraped 0 cars — leaving state untouched and exiting.")
            return 1

        # --- nulmeting: first run records everything, alerts nothing --------
        if not state["baseline_done"]:
            state["seen"] = sorted({c.id for c in cars})
            state["baseline_done"] = True
            save_state(state)
            send(f"✅ <b>Nulmeting voltooid.</b>\nIk volg nu {len(cars)} auto's. "
                 f"Vanaf nu stuur ik alleen bericht bij <b>nieuwe</b> matches.")
            log.info("Baseline set with %d cars.", len(cars))
            return 0

        # --- normal run: evaluate only unseen cars --------------------------
        new = [c for c in cars if c.id not in seen]
        log.info("%d new car(s) since last run.", len(new))
        alerts = 0
        for c in new:
            s.detail(c)
            seen.add(c.id)
            if any(matches(c, f) for f in filters):
                alert(c)
                alerts += 1

    state["seen"] = sorted(seen)
    save_state(state)
    log.info("Done. new=%d alerts=%d", len(new), alerts)
    return 0


if __name__ == "__main__":
    sys.exit(main())

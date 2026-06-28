"""
Athlon Flex showroom checker — GitHub Actions edition.

Crawl structure (confirmed):
    showroom (aanbod)   /app/showroom?...           -> grid of MODELS
    model page          /app/showroom/Brand/Model    -> grid of CARS
    detail page         /app/showroom/Brand/Model/<uuid>  -> fuel + fiscale waarde

Each run: enumerate every car UUID, alert on NEW ones matching filters.yml.
State (nulmeting baseline) lives in state.json, committed back by the workflow.

Env (GitHub Secrets): TG_BOT_TOKEN, TG_CHAT_ID
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
from urllib.parse import urlsplit, urlunsplit

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

# /app/showroom/Brand/Model  (model link)  OR  .../Brand/Model/<uuid>  (car link)
SHOWROOM_PATH_RE = re.compile(
    r"/app/showroom/(?P<brand>[^/?#]+)/(?P<model>[^/?#]+)(?:/(?P<uuid>[0-9a-fA-F-]{16,}))?")


def norm(s: str) -> str:
    """Normalise 'AYGO_X' / 'AYGO-X' / 'aygo x' to a common form for matching."""
    return s.replace("_", " ").replace("-", " ").lower().strip()


def deslug(s: str) -> str:
    return s.replace("_", " ").replace("-", " ").strip()


def classify(href: str):
    """Return ('model', brand, model) | ('car', brand, model, uuid) | None."""
    m = SHOWROOM_PATH_RE.search(href or "")
    if not m:
        return None
    if m.group("uuid"):
        return ("car", m.group("brand"), m.group("model"), m.group("uuid"))
    return ("model", m.group("brand"), m.group("model"), None)


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
    if brand != "*" and norm(brand) != norm(car.brand):
        return False
    if model != "*" and norm(model) not in norm(car.model):     # "contains"
        return False
    if car.fuel is not None and fuel.lower() not in car.fuel.lower():
        return False
    if car.fiscal_value is None:
        return False
    return car.fiscal_value < mx


def model_of_interest(brand: str, model: str, filters: list[dict]) -> bool:
    """Should we descend into this model page? (coarse brand/model pre-filter)"""
    for f in filters:
        fb = str(f.get("brand", "*"))
        fm = str(f.get("model", "*"))
        if fb != "*" and norm(fb) != norm(brand):
            continue
        if fm != "*" and norm(fm) not in norm(model):
            continue
        return True
    return False


# --------------------------------------------------------------------------- #
# State + filters
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
# URL helpers
# --------------------------------------------------------------------------- #

def absolute(href: str) -> str:
    if href.startswith("http"):
        return href
    return "https://flex.athlon.com" + (href if href.startswith("/") else "/" + href)


def with_query(url: str, query: str) -> str:
    """Append the showroom's query string to a link that lacks one."""
    p = urlsplit(url)
    if p.query or not query:
        return url
    return urlunsplit((p.scheme, p.netloc, p.path, query, ""))


# --------------------------------------------------------------------------- #
# Scraper (Playwright)
# --------------------------------------------------------------------------- #

class Scraper:
    def __init__(self, showroom_url: str):
        self.showroom_url = showroom_url
        self.query = urlsplit(showroom_url).query
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

    def _links(self, page):
        # Nudge lazy-loaded grids, then read every showroom link on the page.
        for _ in range(6):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(700)
        return page.eval_on_selector_all(
            'a[href*="/app/showroom/"]',
            "els => els.map(e => e.getAttribute('href'))") or []

    def scrape_models(self) -> list[tuple[str, str, str]]:
        """Return [(brand, model, model_url), ...] from the aanbod grid."""
        from playwright.sync_api import TimeoutError as PWTimeout
        page = self._ctx.new_page()
        captured: list[tuple[str, str]] = []

        def _on_response(r):
            try:
                if "application/json" not in (r.headers.get("content-type", "") or ""):
                    return
                snip = ""
                if any(k in r.url.lower() for k in
                       ("showroom", "vehicle", "offer", "aanbod", "car",
                        "catalog", "inventory", "model", "lease", "stock")):
                    try:
                        snip = r.text()[:700]
                    except Exception:
                        snip = "(body unavailable)"
                captured.append((r.url, snip))
            except Exception:
                pass

        page.on("response", _on_response)
        try:
            page.goto(self.showroom_url, wait_until="networkidle", timeout=60_000)
            self._cookies(page)
            try:
                page.wait_for_selector('a[href*="/app/showroom/"]', timeout=20_000)
            except PWTimeout:
                pass
            hrefs = self._links(page)
            title, cur = page.title(), page.url
        finally:
            page.close()

        # Log backend JSON endpoints + a body snippet for the promising ones.
        # (Lets us switch from DOM-crawl to a fast direct API call.)
        for u, snip in captured[:20]:
            log.info("api seen: %s", u)
            if snip:
                log.info("   body: %s", snip.replace("\n", " "))

        models: dict[tuple[str, str], tuple[str, str, str]] = {}
        for href in hrefs:
            c = classify(href)
            if not c:
                continue
            kind, brand, model = c[0], c[1], c[2]
            # On the aanbod grid we want model links; car links also fine but rare here.
            url = with_query(absolute(href), self.query)
            models[(norm(brand), norm(model))] = (deslug(brand), deslug(model), url)

        if not models:
            body = ""
            log.warning("No model links found. title=%r final_url=%r", title, cur)
            low = (title or "").lower()
            if any(w in low for w in ("inloggen", "login", "aanmelden", "sign in")):
                log.warning(">>> Looks like a LOGIN wall — showroom may need auth.")
        log.info("Aanbod: %d distinct models found.", len(models))
        return list(models.values())

    def scrape_cars(self, model_url: str) -> list[Car]:
        """Return the individual cars (with uuid) listed on a model page."""
        from playwright.sync_api import TimeoutError as PWTimeout
        page = self._ctx.new_page()
        try:
            page.goto(model_url, wait_until="networkidle", timeout=45_000)
            self._cookies(page)
            try:
                page.wait_for_selector('a[href*="/app/showroom/"]', timeout=12_000)
            except PWTimeout:
                pass
            hrefs = self._links(page)
        finally:
            page.close()

        cars: dict[str, Car] = {}
        for href in hrefs:
            c = classify(href)
            if not c or c[0] != "car":
                continue
            _, brand, model, uuid = c
            cars[uuid] = Car(uuid, deslug(brand), deslug(model), absolute(href))
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
        models = s.scrape_models()

        # SAFETY GUARD: 0 models means the site changed / blocked / needs login.
        # Do NOT touch state, or the next run would treat everything as "new".
        if not models:
            log.error("Scraped 0 models — leaving state untouched and exiting.")
            return 1

        interesting = [m for m in models if model_of_interest(m[0], m[1], filters)]
        log.info("%d/%d models match your filters' brand/model; crawling those.",
                 len(interesting), len(models))

        cars: dict[str, Car] = {}
        for brand, model, url in interesting:
            for car in s.scrape_cars(url):
                cars[car.id] = car
        log.info("Total cars enumerated: %d", len(cars))

        # If brand/model matched models but we got no cars at all, treat as a
        # scrape failure (don't corrupt the baseline).
        if interesting and not cars:
            log.error("Models matched but 0 cars enumerated — exiting without state change.")
            return 1

        # --- nulmeting: first run records everything, alerts nothing --------
        if not state["baseline_done"]:
            state["seen"] = sorted(cars.keys())
            state["baseline_done"] = True
            save_state(state)
            send(f"✅ <b>Nulmeting voltooid.</b>\nIk volg nu {len(cars)} auto's. "
                 f"Vanaf nu stuur ik alleen bericht bij <b>nieuwe</b> matches.")
            log.info("Baseline set with %d cars.", len(cars))
            return 0

        # --- normal run: evaluate only unseen cars --------------------------
        new = [c for c in cars.values() if c.id not in seen]
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

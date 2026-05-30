"""
fb_friends_manager.py
Selenium-based Facebook Friends Manager using ChromeDriver.
Scrapes all friends from an active Facebook browser session
and exports them to a CSV with full audit columns.
"""

import csv
import time
import logging
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, fields, asdict
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, StaleElementReferenceException
)
from webdriver_manager.chrome import ChromeDriverManager

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("fb_friends_manager.log")],
)
log = logging.getLogger(__name__)

# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Friend:
    # Identity
    name: str
    profile_url: str
    profile_id: str          # uid or vanity slug extracted from URL
    mutual_friends: Optional[int] = None
    location: Optional[str] = None
    work: Optional[str] = None

    # Audit
    record_hash: str = ""           # SHA-256 of (name + profile_id)
    scraped_at_utc: str = ""        # ISO-8601 timestamp
    scrape_session_id: str = ""     # unique per run
    source_page: str = ""           # URL of friends list page scraped
    row_index: int = 0              # position in the scraped list
    is_duplicate: bool = False      # flagged if same profile_id seen twice

    def __post_init__(self):
        if not self.scraped_at_utc:
            self.scraped_at_utc = datetime.now(timezone.utc).isoformat()
        if not self.record_hash:
            raw = f"{self.name}|{self.profile_id}"
            self.record_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]

# ── ChromeDriver setup ────────────────────────────────────────────────────────

def build_driver(
    profile_dir: Optional[str] = None,
    headless: bool = False,
    chromedriver_path: Optional[str] = None,
) -> webdriver.Chrome:
    """
    Return a Chrome WebDriver.

    Args:
        profile_dir:      Path to an existing Chrome user-data-dir so
                          Facebook session cookies are already present.
        headless:         Run without a visible window (not recommended for FB).
        chromedriver_path: Explicit path to chromedriver binary. If None,
                           webdriver-manager auto-downloads the right version.
    """
    opts = Options()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("--window-size=1280,900")

    if headless:
        opts.add_argument("--headless=new")

    if profile_dir:
        opts.add_argument(f"--user-data-dir={profile_dir}")
        log.info("Using Chrome profile: %s", profile_dir)

    service = Service(
        chromedriver_path or ChromeDriverManager().install()
    )
    driver = webdriver.Chrome(service=service, options=opts)
    # Mask navigator.webdriver flag
    driver.execute_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )
    return driver

# ── Scraping helpers ──────────────────────────────────────────────────────────

FB_FRIENDS_URL = "https://www.facebook.com/{username}/friends"
SCROLL_PAUSE = 2.0
LOAD_TIMEOUT = 15


def _extract_uid(url: str) -> str:
    """Pull profile id or vanity name from a Facebook profile URL."""
    # handles both /profile.php?id=123 and /vanityname
    if "profile.php" in url:
        for part in url.split("?")[-1].split("&"):
            if part.startswith("id="):
                return part[3:]
    path = url.split("facebook.com/")[-1].split("?")[0].rstrip("/")
    return path or url


def _scroll_to_bottom(driver: webdriver.Chrome) -> None:
    """Scroll until no new content loads."""
    last_height = driver.execute_script("return document.body.scrollHeight")
    while True:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(SCROLL_PAUSE)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height
        log.debug("Scrolled, page height: %d", new_height)


def _parse_friend_cards(driver: webdriver.Chrome, session_id: str, source_url: str) -> list[Friend]:
    """
    Parse friend cards currently rendered in the DOM.
    Facebook renders friends as <a> tags inside a list; we target the
    canonical link + display name pattern that survives layout changes.
    """
    friends: list[Friend] = []
    seen_ids: set[str] = set()

    # Primary selector – works for most FB layouts
    selectors = [
        "div[data-pagelet='ProfileAppSection_0'] a[href*='facebook.com']",
        "ul[data-testid='friend-list'] a",
        "div.x1lq5wgf a[href*='facebook.com']",  # fallback
    ]

    anchors = []
    for sel in selectors:
        anchors = driver.find_elements(By.CSS_SELECTOR, sel)
        if anchors:
            log.debug("Matched selector: %s  (%d elements)", sel, len(anchors))
            break

    if not anchors:
        log.warning("No friend card anchors found – page structure may have changed.")
        return friends

    for idx, anchor in enumerate(anchors, start=1):
        try:
            href = anchor.get_attribute("href") or ""
            if "friends" in href or not href:
                continue  # skip nav links

            name = anchor.text.strip()
            if not name:
                # Try child span
                spans = anchor.find_elements(By.TAG_NAME, "span")
                name = next((s.text.strip() for s in spans if s.text.strip()), "")

            if not name:
                continue

            uid = _extract_uid(href)
            is_dup = uid in seen_ids
            seen_ids.add(uid)

            friend = Friend(
                name=name,
                profile_url=href.split("?")[0],
                profile_id=uid,
                row_index=idx,
                scrape_session_id=session_id,
                source_page=source_url,
                is_duplicate=is_dup,
            )
            friends.append(friend)

        except StaleElementReferenceException:
            log.debug("Stale element at index %d, skipping.", idx)

    return friends


def _enrich_friend(driver: webdriver.Chrome, friend: Friend) -> Friend:
    """
    Optionally visit each friend's profile to pull location & work.
    Disabled by default – enable via enrich=True in scrape_friends().
    """
    try:
        driver.get(friend.profile_url)
        wait = WebDriverWait(driver, 8)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))

        # Location
        try:
            loc_el = driver.find_element(
                By.XPATH,
                "//div[contains(@aria-label,'Lives in') or contains(@aria-label,'From')]//span",
            )
            friend.location = loc_el.text.strip()
        except NoSuchElementException:
            pass

        # Work
        try:
            work_el = driver.find_element(
                By.XPATH,
                "//div[contains(@aria-label,'Works at') or contains(@aria-label,'Studied at')]//span",
            )
            friend.work = work_el.text.strip()
        except NoSuchElementException:
            pass

        time.sleep(1)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not enrich %s: %s", friend.name, exc)

    return friend

# ── Main scraper ──────────────────────────────────────────────────────────────

def scrape_friends(
    username: str,
    driver: Optional[webdriver.Chrome] = None,
    profile_dir: Optional[str] = None,
    headless: bool = False,
    chromedriver_path: Optional[str] = None,
    enrich: bool = False,
) -> list[Friend]:
    """
    Scrape all friends for *username* from Facebook.

    Returns a list of Friend dataclass instances.

    Args:
        username:         Facebook username or 'me' for the logged-in account.
        driver:           Pass an existing driver (useful for testing).
        profile_dir:      Chrome profile directory with an active FB session.
        headless:         Headless Chrome mode.
        chromedriver_path: Explicit chromedriver path.
        enrich:           Visit each profile to pull location/work (slow).
    """
    own_driver = driver is None
    if own_driver:
        driver = build_driver(profile_dir, headless, chromedriver_path)

    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    url = FB_FRIENDS_URL.format(username=username)

    try:
        log.info("Opening %s", url)
        driver.get(url)

        # Wait for friends section
        try:
            WebDriverWait(driver, LOAD_TIMEOUT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='facebook.com']"))
            )
        except TimeoutException:
            log.error("Page did not load in %ds – are you logged in?", LOAD_TIMEOUT)
            return []

        log.info("Scrolling to load all friends…")
        _scroll_to_bottom(driver)

        log.info("Parsing friend cards…")
        friends = _parse_friend_cards(driver, session_id, url)
        log.info("Found %d friend entries (%d duplicates flagged).",
                 len(friends), sum(f.is_duplicate for f in friends))

        if enrich and friends:
            log.info("Enriching %d profiles (this may take a while)…", len(friends))
            friends = [_enrich_friend(driver, f) for f in friends]

    finally:
        if own_driver:
            driver.quit()

    return friends

# ── CSV export ────────────────────────────────────────────────────────────────

def export_csv(friends: list[Friend], output_path: str = "fb_friends.csv") -> str:
    """Write friends list to CSV and return the file path."""
    if not friends:
        log.warning("No friends to export.")
        return ""

    columns = [f.name for f in fields(Friend)]
    out = Path(output_path)

    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for friend in friends:
            writer.writerow(asdict(friend))

    log.info("Exported %d records → %s", len(friends), out.resolve())
    return str(out.resolve())

# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Facebook Friends Manager")
    parser.add_argument("username", nargs="?", default="me",
                        help="Facebook username (default: 'me' = logged-in user)")
    parser.add_argument("--output", "-o", default="fb_friends.csv",
                        help="Output CSV file path (default: fb_friends.csv)")
    parser.add_argument("--profile-dir", "-p", default=None,
                        help="Chrome user-data-dir with an active Facebook session")
    parser.add_argument("--chromedriver", default=None,
                        help="Path to chromedriver binary (auto-downloaded if omitted)")
    parser.add_argument("--headless", action="store_true",
                        help="Run Chrome in headless mode")
    parser.add_argument("--enrich", action="store_true",
                        help="Visit each profile to extract location & work info")
    args = parser.parse_args()

    friends_list = scrape_friends(
        username=args.username,
        profile_dir=args.profile_dir,
        headless=args.headless,
        chromedriver_path=args.chromedriver,
        enrich=args.enrich,
    )

    if friends_list:
        export_csv(friends_list, args.output)

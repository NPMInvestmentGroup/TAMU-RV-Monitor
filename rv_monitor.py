import json
import os
import re
import hashlib
from pathlib import Path
from typing import Dict, List, Tuple

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
DEBUG_DIR = ROOT / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

COMMON_NOISE = {
    "search", "select", "submit", "home", "parking", "transportation services",
    "texas a&m university", "university contacts", "privacy", "accessibility",
    "rv exchange instructions", "choose a game", "choose a location",
    "please select a game", "select one"
}

def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())

def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True))

def normalize_line(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s

def meaningful_lines(text: str) -> List[str]:
    lines = []
    for raw in text.splitlines():
        line = normalize_line(raw)
        if len(line) < 6:
            continue
        lower = line.lower()
        if lower in COMMON_NOISE:
            continue
        if any(noise == lower for noise in COMMON_NOISE):
            continue
        # Remove obvious static navigation/footer lines.
        if lower.startswith(("copyright", "skip to", "menu", "close menu")):
            continue
        lines.append(line)
    # Preserve order while de-duping.
    seen = set()
    out = []
    for line in lines:
        key = line.lower()
        if key not in seen:
            seen.add(key)
            out.append(line)
    return out

def telegram_send(message: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram secrets are not configured; alert would have been:")
        print(message)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True
    }, timeout=20)
    r.raise_for_status()

def get_selects(page):
    return page.locator("select").evaluate_all("""
    els => els.map((e, idx) => ({
      index: idx,
      id: e.id || "",
      name: e.name || "",
      options: Array.from(e.options).map(o => ({
        text: (o.textContent || "").trim(),
        value: o.value || ""
      }))
    }))
    """)

def choose_select(selects, kind: str, games: List[str]):
    best = None
    best_score = -1
    for s in selects:
        ident = (s["id"] + " " + s["name"]).lower()
        option_text = " | ".join(o["text"] for o in s["options"]).lower()
        score = 0
        if kind == "game":
            if "game" in ident: score += 5
            score += sum(2 for g in games if g.lower() in option_text)
            if any(x in option_text for x in ["missouri state", "arizona state", "kentucky", "tennessee"]):
                score += 3
        else:
            if any(x in ident for x in ["lot", "location", "park"]): score += 5
            if any(x in option_text for x in ["olsen", "equine", "penberthy", "lot", "aggie rv"]):
                score += 4
        if score > best_score:
            best_score = score
            best = s
    return best if best_score > 0 else None

def option_matches(options, target):
    t = target.lower()
    # Exact/contains first.
    for o in options:
        txt = o["text"].strip()
        if txt and (txt.lower() == t or t in txt.lower() or txt.lower() in t):
            return o
    # Flexible token match.
    tokens = [x for x in re.split(r"\W+", t) if len(x) > 2]
    for o in options:
        low = o["text"].lower()
        if tokens and all(tok in low for tok in tokens):
            return o
    return None

def click_search(page):
    candidates = [
        "input[type=submit]", "button[type=submit]", "button", "input[type=button]"
    ]
    for sel in candidates:
        loc = page.locator(sel)
        count = loc.count()
        for i in range(count):
            el = loc.nth(i)
            try:
                txt = ((el.get_attribute("value") or "") + " " + (el.inner_text() or "")).lower()
            except Exception:
                txt = (el.get_attribute("value") or "").lower()
            if any(k in txt for k in ["search", "view", "find", "submit"]):
                try:
                    el.click(timeout=5000)
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                return True
    return False

def snapshot(page, source_key: str, game: str, location: str = "") -> Tuple[str, List[str]]:
    # Main body text is more stable than raw HTML and works across both sites.
    body = page.locator("body").inner_text(timeout=10000)
    lines = meaningful_lines(body)

    # Keep lines most likely to represent actual search results/listings.
    resultish = []
    keys = [game.lower()]
    if location:
        keys.append(location.lower())
    listing_terms = [
        "rv", "lot", "space", "permit", "sale", "sell", "available",
        "reissue", "email", "phone", "$", "@"
    ]
    for line in lines:
        low = line.lower()
        if any(k in low for k in keys) or sum(term in low for term in listing_terms) >= 2:
            resultish.append(line)

    # If filtering was too aggressive, preserve a stable tail of meaningful content.
    if len(resultish) < 2:
        resultish = lines[-50:]

    # Save debug page text if requested.
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{source_key}_{game}_{location}")[:120]
    if CONFIG.get("debug"):
        (DEBUG_DIR / f"{safe}.txt").write_text("\n".join(lines))

    digest = hashlib.sha256("\n".join(resultish).encode()).hexdigest()
    return digest, resultish

def navigate_and_search(page, url: str, game: str, preferred_lots: List[str]):
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except PlaywrightTimeoutError:
        pass

    selects = get_selects(page)
    game_sel = choose_select(selects, "game", CONFIG["games"])
    loc_sel = choose_select(selects, "location", CONFIG["games"])

    # If no game dropdown exists, still snapshot the public page.
    if not game_sel:
        digest, lines = snapshot(page, "page", game)
        return [("All locations", digest, lines)]

    game_opt = option_matches(game_sel["options"], game)
    if not game_opt:
        print(f"\nCould not find game option matching: {game}")
        print("A&M game dropdown currently contains:")
        for opt in game_sel["options"]:
            print(f"  TEXT: {opt['text']!r} | VALUE: {opt['value']!r}")
        print("")
        return []

    game_locator = page.locator("select").nth(game_sel["index"])
    try:
        game_locator.select_option(value=game_opt["value"])
        page.wait_for_timeout(500)
        try:
            page.wait_for_load_state("networkidle", timeout=7000)
        except PlaywrightTimeoutError:
            pass
    except Exception as e:
        print("Game select error:", e)

    # Re-read selects after game selection because ASP.NET pages may re-render.
    selects = get_selects(page)
    loc_sel = choose_select(selects, "location", CONFIG["games"])

    outputs = []
    if loc_sel:
        location_options = [o for o in loc_sel["options"] if o["text"].strip() and
                            not any(x in o["text"].lower() for x in ["select", "choose", "please"])]
        if preferred_lots:
            filtered = []
            for lot in preferred_lots:
                m = option_matches(location_options, lot)
                if m and m not in filtered:
                    filtered.append(m)
            location_options = filtered

        # If there is an "all" option, use it. Otherwise check every lot.
        all_opt = next((o for o in location_options if "all" in o["text"].lower()), None)
        if all_opt:
            location_options = [all_opt]

        for loc_opt in location_options or [{"text": "All locations", "value": ""}]:
            # Reload cleanly for each location to avoid stateful postback surprises.
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=7000)
            except PlaywrightTimeoutError:
                pass
            sels = get_selects(page)
            gs = choose_select(sels, "game", CONFIG["games"])
            if gs:
                go = option_matches(gs["options"], game)
                if go:
                    page.locator("select").nth(gs["index"]).select_option(value=go["value"])
                    page.wait_for_timeout(500)
            sels = get_selects(page)
            ls = choose_select(sels, "location", CONFIG["games"])
            if ls:
                lo = option_matches(ls["options"], loc_opt["text"])
                if lo:
                    page.locator("select").nth(ls["index"]).select_option(value=lo["value"])
                    page.wait_for_timeout(300)

            click_search(page)
            digest, lines = snapshot(page, "search", game, loc_opt["text"])
            outputs.append((loc_opt["text"], digest, lines))
    else:
        click_search(page)
        digest, lines = snapshot(page, "search", game)
        outputs.append(("All locations", digest, lines))
    return outputs

def added_lines(old_lines: List[str], new_lines: List[str]) -> List[str]:
    old = {x.lower() for x in old_lines}
    additions = [x for x in new_lines if x.lower() not in old]
    # Prioritize likely listing details, but don't discard everything if site wording changed.
    strong = [x for x in additions if any(k in x.lower() for k in
              ["rv", "lot", "space", "permit", "sale", "available", "reissue", "@", "$"])]
    return strong[:12] if strong else additions[:8]

def main():
    global CONFIG
    CONFIG = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {"initialized": False, "checks": {}})
    first_run = not state.get("initialized", False)
    alerts = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (compatible; TAMU-RV-Availability-Watcher/1.0; personal-use)"
        )
        page = context.new_page()

        for source_key, source in CONFIG["sources"].items():
            print(f"Checking {source['name']}")
            for game in CONFIG["games"]:
                try:
                    results = navigate_and_search(
                        page, source["url"], game, CONFIG.get("preferred_lots", [])
                    )
                except Exception as e:
                    print(f"ERROR {source_key} / {game}: {e}")
                    continue

                for location, digest, lines in results:
                    key = f"{source_key}|{game}|{location}"
                    previous = state["checks"].get(key)
                    if previous and previous.get("digest") != digest:
                        additions = added_lines(previous.get("lines", []), lines)
                        if additions:
                            alerts.append({
                                "source": source["name"],
                                "game": game,
                                "location": location,
                                "url": source["url"],
                                "additions": additions
                            })
                    elif first_run and CONFIG.get("notify_on_first_run"):
                        alerts.append({
                            "source": source["name"],
                            "game": game,
                            "location": location,
                            "url": source["url"],
                            "additions": ["Initial monitor snapshot created."]
                        })

                    state["checks"][key] = {"digest": digest, "lines": lines}

        browser.close()

    state["initialized"] = True
    save_json(STATE_PATH, state)

    if alerts:
        for a in alerts:
            details = "\n".join(f"• {x}" for x in a["additions"][:10])
            msg = (
                "🚨 TEXAS A&M RV EXCHANGE CHANGE DETECTED\n\n"
                f"Game: {a['game']}\n"
                f"Source: {a['source']}\n"
                f"Lot/location: {a['location']}\n\n"
                f"New information:\n{details}\n\n"
                f"Open exchange:\n{a['url']}"
            )
            telegram_send(msg)
    else:
        print("No new RV exchange listings/changes detected.")

if __name__ == "__main__":
    CONFIG = {}
    main()

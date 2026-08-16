import hashlib
import json
import os
import re
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"

# Public pages
MY_PARKING_URL = "https://myparking.tamu.edu/rvx"
TWELFTH_MAN_URL = "https://transport.tamu.edu/parking/events/rvexchange.aspx"

def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()

def item_key(item):
    return hashlib.sha256(
        json.dumps(item, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()

def telegram_send(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID GitHub secret.")
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
        timeout=20,
    )
    r.raise_for_status()

def goto(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(1500)

def visible_text(page):
    return page.locator("body").inner_text(timeout=15000)

def click_matching(page, phrases):
    for phrase in phrases:
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=re.compile(re.escape(phrase), re.I))
                if loc.count():
                    loc.first.click(timeout=5000)
                    page.wait_for_timeout(1200)
                    return True
            except Exception:
                pass
        try:
            loc = page.get_by_text(phrase, exact=False)
            if loc.count():
                loc.first.click(timeout=5000)
                page.wait_for_timeout(1200)
                return True
        except Exception:
            pass
    return False

# ---------- MY PARKING / TRANSPORTATION EXCHANGE ----------

def reach_public_myparking_exchange(page):
    goto(page, MY_PARKING_URL)

    # IMPORTANT: "MY PARKING" text/header alone does NOT mean login is required.
    text = visible_text(page).lower()
    if "available permits" in text and ("view listings" in text or "no listings" in text):
        return True

    # Try public RV exchange navigation.
    click_matching(page, ["RV Exchange", "RV Football Exchange"])
    text = visible_text(page).lower()
    if "available permits" in text and ("view listings" in text or "no listings" in text):
        return True

    # Search public links for an RV exchange/claim destination.
    links = page.locator("a").evaluate_all("""
        els => els.map(a => ({
          text: (a.textContent || '').trim(),
          href: a.href || ''
        }))
    """)
    for link in links:
        hay = (link["text"] + " " + link["href"]).lower()
        if "rv" in hay and ("exchange" in hay or "permit" in hay):
            try:
                goto(page, link["href"])
                text = visible_text(page).lower()
                if "available permits" in text and ("view listings" in text or "no listings" in text):
                    return True
            except Exception:
                pass

    return False

def find_game_card_locator(page, game):
    """
    Find the smallest Material-UI card/container that contains the configured
    opponent plus Game # and VIEW LISTINGS / NO LISTINGS.
    """
    cards = page.locator("div")
    best = None
    best_len = None

    for i in range(min(cards.count(), 1200)):
        el = cards.nth(i)
        try:
            txt = clean(el.inner_text(timeout=250))
        except Exception:
            continue

        low = txt.lower()
        if game.lower() not in low or "game #" not in low:
            continue
        if "view listings" not in low and "no listings" not in low and "hide listings" not in low:
            continue
        # Avoid grabbing the whole page/container.
        if len(txt) > 800:
            continue

        if best is None or len(txt) < best_len:
            best = el
            best_len = len(txt)

    return best

def game_card_info(page, game):
    card = find_game_card_locator(page, game)
    if card is None:
        return None
    try:
        return {"text": clean(card.inner_text(timeout=1500))}
    except Exception:
        return None

def expand_myparking_game(page, game):
    """
    The /rvx page is a React/Material-UI app. Locate the Tennessee card first,
    then click the VIEW LISTINGS text/control inside that specific card.
    """
    card = find_game_card_locator(page, game)
    if card is None:
        print(f"MY_PARKING_CLICK | {game}: card not found")
        return False

    try:
        before = clean(card.inner_text(timeout=1500))
    except Exception:
        before = ""

    if "no listings" in before.lower():
        return True
    if "hide listings" in before.lower() or "space #" in before.lower():
        return True

    clicked = False

    # First try the exact text inside the game card.
    try:
        view = card.get_by_text("VIEW LISTINGS", exact=True)
        if view.count():
            view.first.scroll_into_view_if_needed()
            view.first.click(timeout=5000, force=True)
            clicked = True
    except Exception as exc:
        print(f"MY_PARKING_CLICK_TEXT_ERROR | {type(exc).__name__}: {exc}")

    # Fallback: locate a descendant containing the text and click its closest
    # button-like/clickable ancestor, then the element itself if necessary.
    if not clicked:
        try:
            clicked = card.evaluate("""
            (card) => {
              const norm = s => (s || '').replace(/\\s+/g,' ').trim().toUpperCase();
              const descendants = [...card.querySelectorAll('*')];
              const target = descendants.find(el => norm(el.textContent) === 'VIEW LISTINGS')
                         || descendants.find(el => norm(el.textContent).includes('VIEW LISTINGS'));
              if (!target) return false;

              let cur = target;
              for (let i=0; i<6 && cur && cur !== card.parentElement; i++, cur=cur.parentElement) {
                const tag = (cur.tagName || '').toLowerCase();
                const role = cur.getAttribute && cur.getAttribute('role');
                const style = window.getComputedStyle(cur);
                if (tag === 'button' || tag === 'a' || role === 'button' ||
                    style.cursor === 'pointer' || cur.onclick) {
                  cur.scrollIntoView({block:'center'});
                  cur.click();
                  return true;
                }
              }

              target.scrollIntoView({block:'center'});
              target.click();
              return true;
            }
            """)
        except Exception as exc:
            print(f"MY_PARKING_CLICK_JS_ERROR | {type(exc).__name__}: {exc}")

    if not clicked:
        print(f"MY_PARKING_CLICK | {game}: VIEW LISTINGS target not clicked")
        return False

    # Wait for the selected card to expose listing details.
    for _ in range(20):
        page.wait_for_timeout(400)
        info = game_card_info(page, game)
        if not info:
            continue
        low = info["text"].lower()
        if "space #" in low or "claim permit" in low or "hide listings" in low or "no listings" in low:
            print(f"MY_PARKING_CLICK | {game}: expanded")
            return True

    print(f"MY_PARKING_CLICK | {game}: click sent but expansion not confirmed")
    return False

def parse_myparking_card(game, text, allowed_lots):
    txt = clean(text)
    low = txt.lower()
    if "no listings" in low:
        return []

    # Typical visible listing:
    # AGGIE RV PARK  Space # C02  $225.00  CLAIM PERMIT
    pattern = re.compile(
        r"(?P<lot>[A-Za-z0-9 &'()./-]{2,100}?(?:RV\\s*Park|RV\\s*Lot|Lot\\s*[A-Za-z0-9-]+))"
        r"\\s+Space\\s*#\\s*(?P<space>[A-Za-z0-9-]+)"
        r".{0,80}?\\$\\s*(?P<price>\\d+(?:\\.\\d{2})?)",
        re.I
    )

    results, seen = [], set()
    for m in pattern.finditer(txt):
        lot = clean(m.group("lot"))
        space = clean(m.group("space"))
        price = clean(m.group("price"))
        if allowed_lots and not any(x.lower() in lot.lower() for x in allowed_lots):
            continue
        item = {"game": game, "lot": lot, "space": space, "price": price}
        k = item_key(item)
        if k not in seen:
            seen.add(k)
            results.append(item)

    # Fallback if the site wording around the lot changes.
    if not results and "space #" in low:
        spaces = re.findall(r"Space\\s*#\\s*([A-Za-z0-9-]+)", txt, re.I)
        prices = re.findall(r"\\$\\s*(\\d+(?:\\.\\d{2})?)", txt)
        for idx, space in enumerate(spaces):
            price = prices[idx] if idx < len(prices) else ""
            item = {"game": game, "lot": "RV Space", "space": space, "price": price}
            k = item_key(item)
            if k not in seen:
                seen.add(k)
                results.append(item)

    return results

def check_myparking(page, cfg):
    if not reach_public_myparking_exchange(page):
        return {
            "ok": False,
            "reason": "Could not reach the public My Parking AVAILABLE PERMITS / RV Exchange page.",
            "items": [],
        }

    results = []
    diagnostics = []
    for game in cfg.get("games_to_monitor", []):
        before = game_card_info(page, game)
        if not before:
            diagnostics.append(f"{game}: game card not found")
            continue

        if "view listings" in before["text"].lower():
            try:
                expanded = expand_myparking_game(page, game)
                if expanded:
                    page.wait_for_timeout(1200)
                else:
                    diagnostics.append(f"{game}: VIEW LISTINGS click did not expand")
            except Exception as exc:
                diagnostics.append(f"{game}: expansion error {type(exc).__name__}: {exc}")

        after = game_card_info(page, game)
        if not after:
            diagnostics.append(f"{game}: game card disappeared after expansion")
            continue

        diagnostics.append(f"{game}: {clean(after['text'])[:900]}")
        results.extend(
            parse_myparking_card(
                game,
                after["text"],
                cfg.get("lots_to_monitor", []),
            )
        )

    return {"ok": True, "reason": "", "items": results, "diagnostic": diagnostics}

# ---------- 12TH MAN EXCHANGE ----------

def click_agree(page):
    # Exact flow shown by the public page: I AGREE first.
    for name in ["I AGREE", "I Agree", "Agree"]:
        try:
            btn = page.get_by_role("button", name=re.compile(f"^{re.escape(name)}$", re.I))
            if btn.count():
                btn.first.click(timeout=5000)
                page.wait_for_timeout(1000)
                return True
        except Exception:
            pass

    try:
        loc = page.get_by_text("I AGREE", exact=False)
        if loc.count():
            loc.first.click(timeout=5000)
            page.wait_for_timeout(1000)
            return True
    except Exception:
        pass

    # It may already be agreed if the Game dropdown is visible.
    return page.locator("select").count() > 0

def select_game_dropdown(page, game):
    selects = page.locator("select")
    for i in range(selects.count()):
        sel = selects.nth(i)
        try:
            options = [clean(x) for x in sel.locator("option").all_text_contents()]
        except Exception:
            continue

        match = next((x for x in options if game.lower() in x.lower()), None)
        if match:
            sel.select_option(label=match)
            page.wait_for_timeout(1200)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                pass
            return True, options
    return False, []

def extract_12th_man_results(page, game, allowed_lots):
    # Ignore the VIEW/REMOVE and SELL forms entirely.
    # We only accept relatively small blocks containing the selected game plus
    # RV-lot/contact/listing-style data, and reject known form text.
    reject = [
        "view/remove postings",
        "search for my postings",
        "removal word/phrase",
        "postings submitted through this site",
        "sell first name",
        "please select a game",
    ]

    candidates = page.locator("tr, li, article, .card, .row, fieldset, table")
    results = []
    seen = set()

    for i in range(min(candidates.count(), 500)):
        try:
            txt = clean(candidates.nth(i).inner_text(timeout=500))
        except Exception:
            continue

        low = txt.lower()
        if not txt or len(txt) > 1600:
            continue
        if any(r in low for r in reject):
            continue
        if game.lower() not in low:
            continue

        lot_words = ["equine", "olsen", "lot r", "lot 74", "rv"]
        if allowed_lots:
            if not any(x.lower() in low for x in allowed_lots):
                continue
        elif not any(x in low for x in lot_words):
            continue

        # Require something that makes this look like an actual posting/result.
        posting_signals = [
            "$", "phone", "email", "@", "space", "available",
            "contact", "price", "asking"
        ]
        if not any(x in low for x in posting_signals):
            continue

        item = {"game": game, "detail": txt}
        k = item_key(item)
        if k not in seen:
            seen.add(k)
            results.append(item)

    # Prefer the smallest blocks so parent containers do not duplicate children.
    results.sort(key=lambda x: len(x["detail"]))
    filtered = []
    for item in results:
        if any(item["detail"] in old["detail"] for old in filtered):
            continue
        filtered.append(item)

    return filtered[:30]

def check_12th_man(page, cfg):
    goto(page, TWELFTH_MAN_URL)

    if not click_agree(page):
        return {
            "ok": False,
            "reason": "Could not click I AGREE and no Game dropdown appeared.",
            "items": [],
        }

    results = []
    diagnostics = []

    for game in cfg.get("games_to_monitor", []):
        ok, options = select_game_dropdown(page, game)
        if not ok:
            diagnostics.append(
                f"{game}: not found in Game dropdown. Options: {options}"
            )
            continue

        page.wait_for_timeout(1200)
        page_text = clean(visible_text(page))
        diagnostics.append(f"{game}: selected successfully; page excerpt: {page_text[:700]}")

        results.extend(
            extract_12th_man_results(
                page,
                game,
                cfg.get("lots_to_monitor", []),
            )
        )

    return {"ok": True, "reason": "", "items": results, "diagnostic": diagnostics}

# ---------- STATE / ALERTING ----------

def additions(previous_snapshot, current_snapshot):
    old = {item_key(x) for x in (previous_snapshot or {}).get("items", [])}
    return [x for x in current_snapshot.get("items", []) if item_key(x) not in old]

def describe(item):
    if "space" in item:
        price = f" — ${item['price']}" if item.get("price") else ""
        return (
            f"{item.get('game','')} — {item.get('lot','')} — "
            f"Space #{item.get('space','')}{price}"
        )
    return clean(item.get("detail", ""))[:700]

def main():
    cfg = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {"initialized": False, "sources": {}})
    old_sources = state.get("sources", {})
    current = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1200},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        if cfg.get("check_transportation_exchange", True):
            try:
                snap = check_myparking(page, cfg)
            except Exception as exc:
                snap = {
                    "ok": False,
                    "reason": f"{type(exc).__name__}: {exc}",
                    "items": [],
                }
            current["transportation"] = snap
            if snap["ok"]:
                print(
                    "SOURCE OK | My Parking RV Exchange | "
                    f"detected monitored listings: {len(snap['items'])}"
                )
                for x in snap["items"]:
                    print("ITEM:", describe(x))
                for d in snap.get("diagnostic", []):
                    print("DIAGNOSTIC:", d)
            else:
                print("SOURCE FAILED | My Parking RV Exchange |", snap["reason"])

        if cfg.get("check_12th_man_exchange", True):
            try:
                snap = check_12th_man(page, cfg)
            except Exception as exc:
                snap = {
                    "ok": False,
                    "reason": f"{type(exc).__name__}: {exc}",
                    "items": [],
                }
            current["12th_man"] = snap
            if snap["ok"]:
                print(
                    "SOURCE OK | 12th Man RV Space Exchange | "
                    f"detected monitored listings: {len(snap['items'])}"
                )
                for x in snap["items"]:
                    print("ITEM:", describe(x))
                for d in snap.get("diagnostic", []):
                    print("DIAGNOSTIC:", d)
            else:
                print("SOURCE FAILED | 12th Man RV Space Exchange |", snap["reason"])

        browser.close()

    first_run = not state.get("initialized", False)

    if first_run:
        print("BASELINE CREATED | existing listings recorded without alerting.")
    else:
        for source, snap in current.items():
            if not snap.get("ok"):
                if old_sources.get(source, {}).get("ok"):
                    telegram_send(
                        "⚠️ Aggie RV Monitor source problem\n\n"
                        f"{source}: {snap.get('reason', 'Unknown error')}"
                    )
                continue

            for item in additions(old_sources.get(source, {}), snap):
                telegram_send(
                    "🚨 AGGIE RV SPACE AVAILABLE\n\n"
                    f"{describe(item)}\n\n"
                    "Open the appropriate Texas A&M RV exchange now."
                )
                print("ALERT SENT:", describe(item))

    state["initialized"] = True
    state["sources"] = current
    save_json(STATE_PATH, state)
    print("MONITOR COMPLETE")

if __name__ == "__main__":
    main()

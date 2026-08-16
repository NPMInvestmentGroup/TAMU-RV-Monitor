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

MY_PARKING_URL = "https://myparking.tamu.edu/"
TWELFTH_MAN_URL = "https://transport.tamu.edu/parking/events/rvexchange.aspx"

def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()

def key_for(item):
    raw = json.dumps(item, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def telegram_send(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Telegram GitHub secrets are missing.")
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
        timeout=20,
    )
    r.raise_for_status()

def goto(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(1800)

def body_text(page):
    return page.locator("body").inner_text(timeout=15000)

def looks_like_login(page):
    text = body_text(page).lower()
    password_fields = page.locator('input[type="password"]').count()
    return password_fields > 0 or (
        ("netid" in text or "log in" in text or "login" in text)
        and "claim rv permit" not in text
        and "available permits" not in text
    )

def click_text_if_present(page, phrases):
    for phrase in phrases:
        loc = page.get_by_text(phrase, exact=False)
        if loc.count():
            try:
                loc.first.click(timeout=5000)
                page.wait_for_timeout(1200)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except PlaywrightTimeoutError:
                    pass
                return True
            except Exception:
                pass
    return False

def reach_rv_claim_page(page):
    goto(page, MY_PARKING_URL)

    text = body_text(page).lower()
    if "available permits" in text and ("rv" in text or "claim permit" in text):
        return True, ""

    if looks_like_login(page):
        return False, "My Parking presented a login page."

    click_text_if_present(page, ["RV Exchange"])
    text = body_text(page).lower()

    if "claim rv permit" in text:
        click_text_if_present(page, ["Claim RV Permit"])

    text = body_text(page).lower()
    if "available permits" in text or ("claim permit" in text and "game #" in text):
        return True, ""

    links = page.locator("a").evaluate_all("""
      els => els.map(a => ({
        text: (a.textContent || "").trim(),
        href: a.href || ""
      }))
    """)
    for link in links:
        hay = (link["text"] + " " + link["href"]).lower()
        if "rv" in hay and ("exchange" in hay or "permit" in hay):
            try:
                goto(page, link["href"])
                text = body_text(page).lower()
                if "available permits" in text or ("claim permit" in text and "game #" in text):
                    return True, ""
            except Exception:
                pass

    return False, "Could not reach the My Parking RV claim page."

def find_game_container(page, game):
    js = """
    (game) => {
      const norm = s => (s || '').replace(/\\s+/g,' ').trim().toLowerCase();
      const target = game.toLowerCase();
      const all = [...document.querySelectorAll('body *')];
      const hits = all.filter(el => norm(el.textContent).includes(target));
      let best = null;
      let bestLen = Infinity;
      for (const el of hits) {
        let cur = el;
        for (let i=0; i<8 && cur; i++, cur=cur.parentElement) {
          const t = norm(cur.innerText);
          if (t.includes(target) &&
              (t.includes('view listings') || t.includes('hide listings') ||
               t.includes('no listings') || t.includes('claim permit'))) {
            if (t.length < bestLen) {
              best = cur;
              bestLen = t.length;
            }
            break;
          }
        }
      }
      return best ? {text: best.innerText || ''} : null;
    }
    """
    return page.evaluate(js, game)

def expand_game(page, game):
    js = """
    (game) => {
      const norm = s => (s || '').replace(/\\s+/g,' ').trim().toLowerCase();
      const target = game.toLowerCase();
      const controls = [...document.querySelectorAll('button,a,[role="button"]')];
      for (const btn of controls) {
        if (!norm(btn.textContent).includes('view listings')) continue;
        let cur = btn;
        for (let i=0; i<8 && cur; i++, cur=cur.parentElement) {
          if (norm(cur.innerText).includes(target)) {
            btn.click();
            return true;
          }
        }
      }
      return false;
    }
    """
    try:
        clicked = page.evaluate(js, game)
        if clicked:
            page.wait_for_timeout(1200)
        return clicked
    except Exception:
        return False

def parse_myparking_game_text(game, text, allowed_lots):
    txt = clean(text)
    if "no listings" in txt.lower():
        return []

    pattern = re.compile(
        r"(?P<lot>[A-Za-z0-9 &'()./-]{3,60}?(?:RV Park|RV|Park|Lot\s*\d+[A-Za-z ]*))"
        r"\s+Space\s*#\s*(?P<space>[A-Za-z0-9-]+)"
        r"(?:\s+\$?(?P<price>\d+(?:\.\d{2})?))?",
        re.IGNORECASE
    )

    items, seen = [], set()
    for m in pattern.finditer(txt):
        lot = clean(m.group("lot"))
        space = clean(m.group("space"))
        price = clean(m.group("price") or "")
        if allowed_lots and not any(x.lower() in lot.lower() for x in allowed_lots):
            continue
        item = {"game": game, "lot": lot, "space": space, "price": price}
        k = key_for(item)
        if k not in seen:
            seen.add(k)
            items.append(item)

    if not items and "claim permit" in txt.lower() and "space #" in txt.lower():
        m = re.search(r"Space\s*#\s*([A-Za-z0-9-]+)", txt, re.I)
        if m:
            items.append({"game": game, "lot": "RV Space", "space": m.group(1), "price": ""})

    return items

def myparking_snapshot(page, cfg):
    ok, reason = reach_rv_claim_page(page)
    if not ok:
        return {"ok": False, "reason": reason, "items": []}

    games = cfg.get("games_to_monitor", [])
    lots = cfg.get("lots_to_monitor", [])
    items = []
    diagnostics = []

    for game in games:
        expand_game(page, game)
        container = find_game_container(page, game)
        if not container:
            diagnostics.append(f"{game}: game card not found")
            continue
        text = container["text"]
        diagnostics.append(f"{game}: {clean(text)[:500]}")
        items.extend(parse_myparking_game_text(game, text, lots))

    return {"ok": True, "reason": "", "items": items, "diagnostic": diagnostics}

def twelfth_man_snapshot(page, cfg):
    goto(page, TWELFTH_MAN_URL)
    games = cfg.get("games_to_monitor", [])
    lots = cfg.get("lots_to_monitor", [])
    items = []

    checks = page.locator('input[type="checkbox"]')
    for i in range(checks.count()):
        try:
            if not checks.nth(i).is_checked():
                checks.nth(i).check()
        except Exception:
            pass

    for game in games:
        selects = page.locator("select")
        for i in range(selects.count()):
            try:
                options = selects.nth(i).locator("option").all_text_contents()
            except Exception:
                continue
            match = next((x for x in options if game.lower() in clean(x).lower()), None)
            if match:
                try:
                    selects.nth(i).select_option(label=match)
                    page.wait_for_timeout(500)
                except Exception:
                    pass

        for label in ["Search", "Submit", "View", "Find"]:
            loc = page.get_by_text(label, exact=False)
            if loc.count():
                try:
                    loc.first.click(timeout=3000)
                    page.wait_for_timeout(1000)
                    break
                except Exception:
                    pass

        selectors = ["tr", "li", ".card", "article", "div"]
        candidates = []
        for sel in selectors:
            loc = page.locator(sel)
            count = min(loc.count(), 400)
            for i in range(count):
                try:
                    t = clean(loc.nth(i).inner_text(timeout=700))
                except Exception:
                    continue
                low = t.lower()
                if game.lower() not in low:
                    continue
                if not any(k in low for k in ("olsen", "equine", "lot r", "lot 74", "space", "@", "phone", "$")):
                    continue
                if len(t) > 1200:
                    continue
                if lots and not any(x.lower() in low for x in lots):
                    continue
                candidates.append(t)

        seen = set()
        for c in sorted(candidates, key=len):
            if c.lower() in seen:
                continue
            seen.add(c.lower())
            items.append({"game": game, "detail": c})
            if len(items) >= 30:
                break

    return {"ok": True, "reason": "", "items": items}

def new_items(old, new):
    old_keys = {key_for(x) for x in (old or {}).get("items", [])}
    return [x for x in new.get("items", []) if key_for(x) not in old_keys]

def describe(item):
    if "space" in item:
        price = f" — ${item['price']}" if item.get("price") else ""
        return f"{item.get('game','')} — {item.get('lot','')} — Space #{item.get('space','')}{price}"
    return clean(item.get("detail", ""))[:600]

def main():
    cfg = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {"initialized": False, "sources": {}})
    previous = state.get("sources", {})
    current = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (compatible; Aggie-RV-Availability-Watcher/3.0; personal-use)"
        )
        page = context.new_page()

        if cfg.get("check_transportation_exchange", True):
            try:
                snap = myparking_snapshot(page, cfg)
            except Exception as exc:
                snap = {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "items": []}
            current["transportation"] = snap
            if snap["ok"]:
                print(f"SOURCE OK | My Parking RV Exchange | detected monitored listings: {len(snap['items'])}")
                for item in snap["items"]:
                    print("  ITEM:", describe(item))
                for d in snap.get("diagnostic", []):
                    print("  DIAGNOSTIC:", d)
            else:
                print(f"SOURCE FAILED | My Parking RV Exchange | {snap['reason']}")

        if cfg.get("check_12th_man_exchange", True):
            try:
                snap = twelfth_man_snapshot(page, cfg)
            except Exception as exc:
                snap = {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "items": []}
            current["12th_man"] = snap
            if snap["ok"]:
                print(f"SOURCE OK | 12th Man RV Space Exchange | detected monitored listings: {len(snap['items'])}")
                for item in snap["items"][:20]:
                    print("  ITEM:", describe(item))
            else:
                print(f"SOURCE FAILED | 12th Man RV Space Exchange | {snap['reason']}")

        browser.close()

    first_run = not state.get("initialized", False)

    if not first_run:
        for source_key, snap in current.items():
            old = previous.get(source_key, {})
            if not snap.get("ok"):
                if old.get("ok"):
                    telegram_send(
                        "⚠️ Aggie RV Monitor source problem\n\n"
                        f"{source_key}: {snap.get('reason','Unknown error')}"
                    )
                continue

            additions = new_items(old, snap)
            for item in additions:
                telegram_send(
                    "🚨 AGGIE RV SPACE AVAILABLE\n\n"
                    f"{describe(item)}\n\n"
                    "Open the exchange now to claim/check the space."
                )
                print("ALERT SENT:", describe(item))

    state["initialized"] = True
    state["sources"] = current
    save_json(STATE_PATH, state)

    if first_run:
        print("BASELINE CREATED | existing listings were recorded without alerting.")
    else:
        print("MONITOR COMPLETE")

if __name__ == "__main__":
    main()

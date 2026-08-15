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
DEBUG_DIR = ROOT / "debug"

def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default

def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True))

def clean(text):
    return re.sub(r"\\s+", " ", (text or "")).strip()

def fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

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

def load_page(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(2500)

def get_selects(page):
    return page.locator("select").evaluate_all("""
      els => els.map((e, index) => ({
        index,
        id: e.id || "",
        name: e.name || "",
        aria: e.getAttribute("aria-label") || "",
        options: Array.from(e.options).map(o => ({
          text: (o.textContent || "").trim(),
          value: o.value || ""
        }))
      }))
    """)

def real_options(options):
    results = []
    for opt in options:
        text = clean(opt.get("text"))
        value = clean(opt.get("value"))
        low = text.lower()
        if not text:
            continue
        if "please select" in low or "choose a game" in low or "choose a location" in low:
            continue
        if value in ("", "0", "-1") and ("select" in low or "choose" in low):
            continue
        results.append({"text": text, "value": value})
    return results

def best_select(selects, kind):
    best = None
    best_score = 0
    for s in selects:
        ident = f"{s.get('id','')} {s.get('name','')} {s.get('aria','')}".lower()
        opts = " ".join(o.get("text","") for o in s.get("options", [])).lower()
        score = 0
        if kind == "game":
            if "game" in ident: score += 10
            if "select a game" in opts: score += 10
        else:
            if "location" in ident or "lot" in ident: score += 10
            if "select a location" in opts: score += 10
        if score > best_score:
            best_score, best = score, s
    return best

def body_lines(page):
    text = page.locator("body").inner_text(timeout=15000)
    seen, out = set(), []
    for raw in text.splitlines():
        line = clean(raw)
        if len(line) < 3:
            continue
        low = line.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(line)
    return out

def matches_filter(text, prefs):
    if not prefs:
        return True
    low = text.lower()
    return any(p.lower() in low for p in prefs)

def transportation_snapshot(page, source, cfg):
    load_page(page, source["url"])
    selects = get_selects(page)
    game_sel = best_select(selects, "game")
    if not game_sel:
        return {"ok": False, "reason": "Game dropdown not found.", "items": [], "diagnostic": {"selects": selects}}

    active_games = real_options(game_sel["options"])
    active_games = [g for g in active_games if matches_filter(g["text"], cfg.get("preferred_games", []))]
    items = []

    # On this public page, the game dropdown may contain only "Please select a game"
    # when no Transportation-managed RV permits are currently listed.
    for game in active_games:
        load_page(page, source["url"])
        current_selects = get_selects(page)
        current_game = best_select(current_selects, "game")
        if not current_game:
            continue

        game_control = page.locator("select").nth(current_game["index"])
        try:
            game_control.select_option(value=game["value"])
        except Exception:
            game_control.select_option(label=game["text"])
        page.wait_for_timeout(1200)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeoutError:
            pass

        after = get_selects(page)
        loc_sel = best_select(after, "location")
        locations = real_options(loc_sel["options"]) if loc_sel else []
        locations = [x for x in locations if matches_filter(x["text"], cfg.get("preferred_lots", []))]

        if locations:
            for loc in locations:
                items.append({"game": game["text"], "location": loc["text"]})
        else:
            items.append({"game": game["text"], "location": ""})

    return {
        "ok": True,
        "reason": "",
        "items": items,
        "diagnostic": {
            "all_game_options": game_sel["options"],
            "active_game_options": active_games,
            "all_selects": selects
        }
    }

def twelfth_man_snapshot(page, source, cfg):
    load_page(page, source["url"])
    lines = body_lines(page)

    candidates = []
    for line in lines:
        low = line.lower()
        score = 0
        if any(k in low for k in ("equine", "olsen", "lot r", "lot 74")): score += 2
        if any(k in low for k in ("available", "for sale", "space", "permit", "contact", "email")): score += 1
        if "@" in line or re.search(r"\\b\\d{3}[-.) ]+\\d{3}[- ]+\\d{4}\\b", line): score += 1
        if "this page contains listings" in low or "rv space exchange for 12th man lots" in low:
            score -= 3
        if score >= 2 and matches_filter(line, cfg.get("preferred_lots", [])):
            candidates.append(line)

    seen, unique = set(), []
    for x in candidates:
        key = x.lower()
        if key not in seen:
            seen.add(key)
            unique.append(x)

    sanitized = [x for x in lines if not any(k in x.lower() for k in ("copyright", "privacy", "accessibility"))]

    return {
        "ok": True,
        "reason": "",
        "items": [{"detail": x} for x in unique],
        "page_fingerprint": fingerprint(sanitized),
        "diagnostic": {"candidate_count": len(unique), "candidates": unique[:50]}
    }

def new_items(old, new):
    old_keys = {fingerprint(x) for x in (old or {}).get("items", [])}
    return [x for x in new.get("items", []) if fingerprint(x) not in old_keys]

def describe(item):
    game = clean(item.get("game"))
    location = clean(item.get("location"))
    detail = clean(item.get("detail"))
    if game and location:
        return f"{game} — {location}"
    return game or location or detail or "New RV listing detected"

def main():
    cfg = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {"initialized": False, "telegram_test_sent": False, "sources": {}})

    if cfg.get("telegram_test_once") and not state.get("telegram_test_sent"):
        telegram_send("✅ Aggie RV Alert is connected.\n\nGitHub Actions can successfully send notifications to this Telegram chat.")
        state["telegram_test_sent"] = True
        save_json(STATE_PATH, state)
        print("TELEGRAM OK | test notification sent")

    current = {}
    alerts = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (compatible; Aggie-RV-Availability-Watcher/2.0; personal-use)"
        )
        page = context.new_page()

        for key, source in cfg["sources"].items():
            try:
                if key == "transportation":
                    snap = transportation_snapshot(page, source, cfg)
                else:
                    snap = twelfth_man_snapshot(page, source, cfg)
            except Exception as exc:
                snap = {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "items": [], "diagnostic": {}}

            current[key] = snap

            if snap["ok"]:
                print(f"SOURCE OK | {source['name']} | detected items: {len(snap.get('items', []))}")
                if key == "transportation" and not snap.get("items"):
                    print("  No active game options. This is treated as a valid no-availability state.")
                for item in snap.get("items", [])[:20]:
                    print("  ITEM:", describe(item))
            else:
                print(f"SOURCE FAILED | {source['name']} | {snap['reason']}")

            if cfg.get("debug"):
                DEBUG_DIR.mkdir(exist_ok=True)
                (DEBUG_DIR / f"{key}.json").write_text(json.dumps(snap.get("diagnostic", {}), indent=2))

        browser.close()

    previous = state.get("sources", {})
    first_run = not state.get("initialized", False)

    for key, snap in current.items():
        source = cfg["sources"][key]
        old = previous.get(key, {})

        if not snap.get("ok"):
            if old.get("ok"):
                alerts.append(
                    f"⚠️ AGGIE RV MONITOR SOURCE ERROR\n\nSource: {source['name']}\nProblem: {snap.get('reason')}\n\nThe other source will continue to be checked."
                )
            continue

        if first_run or not old:
            continue

        additions = new_items(old, snap)

        page_changed = (
            key == "12th_man"
            and old.get("page_fingerprint")
            and snap.get("page_fingerprint")
            and old["page_fingerprint"] != snap["page_fingerprint"]
        )

        if additions:
            details = "\n".join("• " + describe(x) for x in additions[:10])
            alerts.append(
                f"🚨 TEXAS A&M RV AVAILABILITY DETECTED\n\nSource: {source['name']}\n\n{details}\n\nOpen exchange:\n{source['url']}"
            )
        elif page_changed:
            alerts.append(
                f"🚨 TEXAS A&M RV EXCHANGE CHANGED\n\nSource: {source['name']}\n\nThe public exchange page changed since the last check. Open it now to see whether a new RV space was posted.\n\n{source['url']}"
            )

    state["initialized"] = True
    state["sources"] = current
    save_json(STATE_PATH, state)

    for alert in alerts:
        telegram_send(alert)
        print("ALERT SENT")

    if not alerts:
        print("MONITOR OK | no new availability/change requiring an alert")

if __name__ == "__main__":
    main()

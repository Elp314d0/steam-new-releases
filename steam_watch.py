"""Watch Steam's newest demos and free-to-play games and send each new one to Telegram.

Runs on GitHub Actions every 3 hours. The ids already seen live in seen.json
(one list per source), which the workflow commits back to the repo after each run.
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# Base of the store search, newest first, English supported. The /search/results/
# endpoint returns the same rows as the page, as JSON (the HTML rows inside).
SEARCH_URL = (
    "https://store.steampowered.com/search/results/"
    "?query&start={start}&count=50&sort_by=Released_DESC&hwtype=0"
    "&supportedlang=english&infinite=1&cc=us&l=english&{filters}"
)
TAGS_URL = "https://store.steampowered.com/tagdata/populartags/english"
DETAILS_URL = "https://store.steampowered.com/api/appdetails?appids={appid}&filters=basic"

# What to watch. "filters" are extra search params; a source with several searches
# merges them so nothing slips through. "type" is what Steam's store API must report.
SOURCES = [
    {
        "key": "demo",
        "label": "DEMO",
        "type": "demo",
        "filters": ["category1=10"],                    # the Demos section
    },
    {
        "key": "f2p",
        "label": "FREE TO PLAY",
        "type": "game",
        "filters": [
            "category1=998&tags=113",                   # games tagged Free to Play
            "category1=998&maxprice=free",              # games that cost $0
        ],
    },
]

PAGES = 2  # 2 x 50 = the 100 newest per search; Steam gets ~50-70 demos and ~15 free games a day
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")
MAX_SEEN = 3000  # per source; keep the state file small, old items never come back to the top
MAX_TAGS = 7  # the search row carries the top tags, same ones the store shows

ROW_RE = re.compile(r'<a href="([^"]+)"[^>]*data-ds-itemkey="([^"]+)"(.*?)</a>', re.S)
TAGIDS_RE = re.compile(r'data-ds-tagids="\[([^\]]*)\]"')
TITLE_RE = re.compile(r'<span class="title">(.*?)</span>', re.S)
DATE_RE = re.compile(r'<div class="search_released[^"]*">(.*?)</div>', re.S)
PRICE_RE = re.compile(r'<div class="discount_final_price[^"]*">(.*?)</div>', re.S)
IMG_RE = re.compile(r'<div class="search_capsule"><img src="([^"]+)"')


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (steam-new-releases-watcher)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def text_of(regex, chunk):
    m = regex.search(chunk)
    return html.unescape(re.sub(r"\s+", " ", m.group(1)).strip()) if m else ""


def fetch_tag_names():
    """Map Steam tag ids to names, e.g. 19 -> 'Action'."""
    try:
        return {t["tagid"]: t["name"] for t in get_json(TAGS_URL)}
    except Exception as e:
        print(f"Could not load tag names: {e}")
        return {}


def release_date(game):
    """Sort key for merged searches; unknown formats ("Coming soon") go last."""
    try:
        return datetime.strptime(game["date"], "%b %d, %Y")
    except ValueError:
        return datetime.min


def fetch_search(filters, tag_names):
    """Return the newest items of one search as dicts, newest first."""
    games = []
    for page in range(PAGES):
        results = get_json(SEARCH_URL.format(start=page * 50, filters=filters))["results_html"]
        # The tag ids live inside the <a ...> opening tag, so search the whole row.
        for row in re.finditer(ROW_RE, results):
            url, key, body = row.groups()
            tag_ids = TAGIDS_RE.search(row.group(0))
            tags = []
            if tag_ids and tag_ids.group(1):
                tags = [tag_names[int(i)] for i in tag_ids.group(1).split(",") if int(i) in tag_names]
            img = IMG_RE.search(body)
            games.append({
                "id": key,                               # e.g. App_5516090
                "title": text_of(TITLE_RE, body),
                "date": text_of(DATE_RE, body),
                "price": text_of(PRICE_RE, body) or "-",
                "url": url.split("?")[0],                # drop Steam's tracking param
                "img": img.group(1) if img else "",      # small capsule, fallback only
                "tags": tags[:MAX_TAGS],
            })
    if not games:
        # Empty result usually means Steam changed its HTML; fail loudly instead of
        # silently saving an empty state.
        sys.exit(f"No games parsed from Steam ({filters}) - the page format may have changed.")
    return games


def fetch_source(source, tag_names):
    """Run every search of a source and merge them without duplicates, newest first."""
    merged, ids = [], set()
    for filters in source["filters"]:
        for game in fetch_search(filters, tag_names):
            if game["id"] not in ids:
                ids.add(game["id"])
                merged.append(game)
    # sorted() is stable, so games from the same day keep Steam's own order.
    return sorted(merged, key=release_date, reverse=True)


def fetch_details(game):
    """Return (type, header image) from the store API, e.g. ("demo", "https://.../header.jpg").

    The type is None when Steam doesn't answer; the image falls back to the small
    search capsule.
    """
    if game["id"].startswith("App_"):
        appid = game["id"][4:]
        try:
            data = get_json(DETAILS_URL.format(appid=appid))[appid]
            if data.get("success"):
                return data["data"].get("type"), data["data"].get("header_image") or game["img"]
        except Exception as e:
            print(f"No details for {game['title']}: {e}")
    return None, game["img"]


def load_seen():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE) as f:
        state = json.load(f)
    # Older versions stored a single list, and only watched demos.
    return {"demo": state} if isinstance(state, list) else state


def save_seen(state):
    with open(STATE_FILE, "w") as f:
        json.dump({k: v[:MAX_SEEN] for k, v in state.items()}, f, indent=0)


def build_caption(game, label):
    lines = [
        f"<b>[{label}]</b>",
        f"<b>{html.escape(game['title'])}</b>",
        f"{html.escape(game['date'])} · {html.escape(game['price'])}",
    ]
    if game["tags"]:
        lines.append("Tags: " + html.escape(", ".join(game["tags"])))
    lines.append(f'<a href="{game["url"]}">View on Steam</a>')
    return "\n".join(lines)


def telegram(method, params):
    token = os.environ["TELEGRAM_TOKEN"]
    params = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "parse_mode": "HTML", **params}
    data = urllib.parse.urlencode(params).encode()
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        with urllib.request.urlopen(url, data=data, timeout=30) as resp:
            return json.load(resp).get("ok", False)
    except urllib.error.HTTPError as e:
        print(f"Telegram {method} failed: {e.read().decode(errors='replace')}")
        return False


def send_text(text, dry_run):
    if dry_run:
        print(text + "\n")
    elif not telegram("sendMessage", {"text": text}):
        sys.exit("Telegram rejected the message.")


def send_game(game, label, photo, dry_run):
    caption = build_caption(game, label)
    if dry_run:
        print(photo)
        print(caption + "\n")
        return
    # If Telegram can't fetch the picture, still send the info as plain text.
    if not (photo and telegram("sendPhoto", {"photo": photo, "caption": caption})):
        send_text(caption, dry_run)
    time.sleep(1)  # stay well under Telegram's per-chat rate limit


def send_if_right_type(game, source, dry_run):
    """Send the game if Steam confirms its type. Returns True when it was sent."""
    kind, photo = fetch_details(game)
    # If Steam didn't say what it is, send it anyway rather than lose it.
    if kind and kind != source["type"]:
        print(f"Skipping {game['title']} ({kind}, expected {source['type']})")
        return False
    send_game(game, source["label"], photo, dry_run)
    return True


def main():
    dry_run = os.environ.get("DRY_RUN") == "1"
    # Manual test from the "Run workflow" button: send the newest N of each source,
    # without touching seen.json.
    test_count = int(os.environ.get("SEND_LATEST") or 0)
    tag_names = fetch_tag_names()
    state = load_seen()

    for source in SOURCES:
        games = fetch_source(source, tag_names)

        if test_count:
            picked = []
            for game in games[:test_count * 2]:          # room for a few skipped items
                kind, photo = fetch_details(game)
                if not kind or kind == source["type"]:
                    picked.append((game, photo))
                if len(picked) == test_count:
                    break
            # Oldest first, so the newest ends up at the bottom of the chat.
            for game, photo in reversed(picked):
                send_game(game, source["label"], photo, dry_run)
            print(f"Test: sent {len(picked)} {source['label']}.")
            continue

        seen = state.get(source["key"])
        if seen is None:
            # First run for this source: everything on the page is "old". Remember it
            # and say so, so we know it's being watched.
            print(f"First run for {source['label']}: storing {len(games)} as already seen.")
            send_text(f"Now watching new {source['label']} releases ({len(games)} tracked).", dry_run)
            state[source["key"]] = [g["id"] for g in games]
            save_seen(state)
            continue

        seen_set = set(seen)
        new_games = [g for g in games if g["id"] not in seen_set]
        print(f"{source['label']}: {len(games)} checked, {len(new_games)} new.")

        # Oldest first, so the newest ends up at the bottom of the chat.
        for game in reversed(new_games):
            send_if_right_type(game, source, dry_run)

        if new_games:
            # Save only after the messages went out, so a Telegram failure retries next run.
            state[source["key"]] = [g["id"] for g in new_games] + seen
            save_seen(state)


if __name__ == "__main__":
    main()

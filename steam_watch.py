"""Watch Steam's newest demos and send each one that just appeared to Telegram.

Runs on GitHub Actions every 3 hours. The list of games already seen lives in
seen.json, which the workflow commits back to the repo after each run.
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

# Same filters as the page:
# https://store.steampowered.com/search/?sort_by=Released_DESC&hwtype=0&category1=10&supportedlang=english
# The /search/results/ endpoint returns those results as JSON (the HTML rows inside).
SEARCH_URL = (
    "https://store.steampowered.com/search/results/"
    "?query&start={start}&count=50&sort_by=Released_DESC&hwtype=0"
    "&category1=10&supportedlang=english&infinite=1&cc=us&l=english"
)
TAGS_URL = "https://store.steampowered.com/tagdata/populartags/english"
DETAILS_URL = "https://store.steampowered.com/api/appdetails?appids={appid}&filters=basic"
PAGES = 2  # 2 x 50 = the 100 newest items; Steam gets ~50-70 demos a day, ~8 per 3 hours
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")
MAX_SEEN = 3000  # keep the state file small; old items never come back to the top
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


def fetch_games():
    """Return the newest items as dicts, newest first."""
    tag_names = fetch_tag_names()
    games = []
    for page in range(PAGES):
        results = get_json(SEARCH_URL.format(start=page * 50))["results_html"]
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
        sys.exit("No games parsed from Steam - the page format may have changed.")
    return games


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
        return None
    with open(STATE_FILE) as f:
        return json.load(f)


def save_seen(ids):
    with open(STATE_FILE, "w") as f:
        json.dump(ids[:MAX_SEEN], f, indent=0)


def build_caption(game):
    lines = [
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


def send_game(game, photo, dry_run):
    caption = build_caption(game)
    if dry_run:
        print(photo)
        print(caption + "\n")
        return
    # If Telegram can't fetch the picture, still send the info as plain text.
    if not (photo and telegram("sendPhoto", {"photo": photo, "caption": caption})):
        if not telegram("sendMessage", {"text": caption}):
            sys.exit("Telegram rejected the message.")
    time.sleep(1)  # stay well under Telegram's per-chat rate limit


def main():
    dry_run = os.environ.get("DRY_RUN") == "1"
    games = fetch_games()

    # Manual test from the "Run workflow" button: send the newest game, touch nothing.
    if os.environ.get("SEND_LATEST") == "true":
        send_game(games[0], fetch_details(games[0])[1], dry_run)
        return

    seen = load_seen()
    if seen is None:
        # First run: everything on the page is "old". Remember it and send a test
        # message so we know the bot setup works.
        print(f"First run: storing {len(games)} games as already seen.")
        text = (f"Steam watcher is running ({len(games)} games tracked). "
                "You'll get a message every time a new game shows up.")
        if dry_run:
            print(text)
        else:
            telegram("sendMessage", {"text": text})
        save_seen([g["id"] for g in games])
        return

    seen_set = set(seen)
    new_games = [g for g in games if g["id"] not in seen_set]
    print(f"{len(games)} games checked, {len(new_games)} new.")

    # Oldest first, so the newest ends up at the bottom of the chat.
    for game in reversed(new_games):
        kind, photo = fetch_details(game)
        # Only demos. If Steam didn't say what it is, send it anyway rather than lose it.
        if kind and kind != "demo":
            print(f"Skipping {game['title']} ({kind})")
            continue
        send_game(game, photo, dry_run)

    if new_games:
        # Save only after the messages went out, so a Telegram failure retries next run.
        save_seen([g["id"] for g in new_games] + seen)


if __name__ == "__main__":
    main()

"""Watch Steam's "newest releases" search and send the games that just appeared to Telegram.

Runs on GitHub Actions every 30 minutes. The list of games already seen lives in
seen.json, which the workflow commits back to the repo after each run.
"""

import html
import json
import os
import re
import sys
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
PAGES = 2  # 2 x 50 = the 100 newest items, plenty for a 30-minute window
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")
MAX_SEEN = 3000  # keep the state file small; old items never come back to the top
TELEGRAM_LIMIT = 4000  # Telegram caps a message at 4096 characters

ROW_RE = re.compile(r'<a href="([^"]+)"[^>]*data-ds-itemkey="([^"]+)"(.*?)</a>', re.S)
TITLE_RE = re.compile(r'<span class="title">(.*?)</span>', re.S)
DATE_RE = re.compile(r'<div class="search_released[^"]*">(.*?)</div>', re.S)
PRICE_RE = re.compile(r'<div class="discount_final_price[^"]*">(.*?)</div>', re.S)


def fetch_page(start):
    req = urllib.request.Request(
        SEARCH_URL.format(start=start),
        headers={"User-Agent": "Mozilla/5.0 (steam-new-releases-watcher)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["results_html"]


def text_of(regex, chunk):
    m = regex.search(chunk)
    return html.unescape(re.sub(r"\s+", " ", m.group(1)).strip()) if m else ""


def fetch_games():
    """Return the newest items as dicts, newest first."""
    games = []
    for page in range(PAGES):
        for url, key, body in ROW_RE.findall(fetch_page(page * 50)):
            games.append({
                "id": key,                               # e.g. App_5516090
                "title": text_of(TITLE_RE, body),
                "date": text_of(DATE_RE, body),
                "price": text_of(PRICE_RE, body) or "-",
                "url": url.split("?")[0],                # drop Steam's tracking param
            })
    if not games:
        # Empty result usually means Steam changed its HTML; fail loudly instead of
        # silently saving an empty state.
        sys.exit("No games parsed from Steam - the page format may have changed.")
    return games


def load_seen():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE) as f:
        return json.load(f)


def save_seen(ids):
    with open(STATE_FILE, "w") as f:
        json.dump(ids[:MAX_SEEN], f, indent=0)


def build_messages(new_games):
    """One line per game, split into as many messages as Telegram's size limit needs."""
    n = len(new_games)
    header = f"<b>Steam: {n} new game{'s' if n != 1 else ''}</b>\n\n"
    lines = [
        f'<a href="{g["url"]}">{html.escape(g["title"])}</a>\n'
        f'{html.escape(g["date"])} · {html.escape(g["price"])}\n'
        for g in new_games
    ]
    messages, current = [], header
    for line in lines:
        if len(current) + len(line) > TELEGRAM_LIMIT:
            messages.append(current)
            current = ""
        current += line + "\n"
    messages.append(current)
    return messages


def send_telegram(text):
    token = os.environ["TELEGRAM_TOKEN"]
    data = urllib.parse.urlencode({
        "chat_id": os.environ["TELEGRAM_CHAT_ID"],
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with urllib.request.urlopen(url, data=data, timeout=30) as resp:
        if not json.load(resp).get("ok"):
            sys.exit("Telegram rejected the message.")


def notify(messages, dry_run):
    for text in messages:
        if dry_run:
            print(text)
        else:
            send_telegram(text)


def main():
    dry_run = os.environ.get("DRY_RUN") == "1"
    games = fetch_games()
    seen = load_seen()

    if seen is None:
        # First run: everything on the page is "old". Remember it and send a test
        # message so we know the bot setup works.
        print(f"First run: storing {len(games)} games as already seen.")
        notify([f"Steam watcher is running ({len(games)} games tracked). "
                "You'll get a message every time a new game shows up."], dry_run)
        save_seen([g["id"] for g in games])
        return

    seen_set = set(seen)
    new_games = [g for g in games if g["id"] not in seen_set]
    print(f"{len(games)} games checked, {len(new_games)} new.")
    if not new_games:
        return

    notify(build_messages(new_games), dry_run)

    # Save only after the message went out, so a Telegram failure retries next run.
    save_seen([g["id"] for g in new_games] + seen)


if __name__ == "__main__":
    main()

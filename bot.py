"""X boost crew Slack bot: tracks shared X links, rocket replies, and /boostboard."""
import logging
import os
import re
import threading
import time

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from core import Store, extract_tweet_ids, has_rocket
from xapi import XClient, XError

log = logging.getLogger("boost-bot")

HANDLE = re.compile(r"^link\s+@?(\w{1,15})$", re.I)
MEDALS = {1: ":first_place_medal:", 2: ":second_place_medal:", 3: ":third_place_medal:"}
LEADERBOARD_SIZE = 20
REPLY_QUERY_CHUNK = 15  # keeps the search query under X's 512-char limit


def format_leaderboard(rows, name_of):
    rows = [r for r in rows if r["boosts"] or r["links"]][:LEADERBOARD_SIZE]
    if not rows:
        return "No boosts yet. Share an X link and reply to it with :rocket: to get on the board."
    lines = [":rocket: *Boost leaderboard*"]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"{MEDALS.get(i, f'{i}.')} *{name_of(r['user'])}* — "
            f"{r['boosts']} boost{'s' if r['boosts'] != 1 else ''} · "
            f"{r['links']} link{'s' if r['links'] != 1 else ''} shared · "
            f"{r['points']:g} pts"
        )
    lines.append("_Link your X account with `/boostboard link @handle` to earn reply points._")
    return "\n".join(lines)


def poll_once(store, x, track_days, full):
    """Refresh tweet counts; on a full pass also look for replies on X."""
    since = time.time() - track_days * 86400
    ids = store.tweets_to_poll(since, only_unseen=not full)
    for i in range(0, len(ids), 100):
        for tid, t in x.fetch_tweets(ids[i : i + 100]).items():
            store.record_tweet(tid, t["metrics"], t["author_id"], t["conversation_id"])
    if not full:
        return
    for tid, conversation_id, author, booster_ids in store.reply_targets(since):
        who = [author] + sorted(set(booster_ids) - {author})
        for i in range(0, len(who), REPLY_QUERY_CHUNK):
            store.add_replies(tid, x.fetch_replies(conversation_id, who[i : i + REPLY_QUERY_CHUNK]))


def poll_loop(store, x, wake, interval, track_days):
    """Full pass every `interval` seconds; a new share triggers a quick baseline fetch."""
    last_full = 0.0
    while True:
        full = time.time() - last_full >= interval
        try:
            poll_once(store, x, track_days, full)
        except XError as e:
            log.warning("poll failed, will retry next cycle: %s", e)
        except Exception:
            log.exception("poll crashed")
        if full:
            last_full = time.time()
        wake.wait(timeout=max(1.0, last_full + interval - time.time()))
        wake.clear()


def build_app(store, x, wake, channel=None, **app_kwargs):
    app = App(**app_kwargs)
    names = {}

    def name_of(user_id):
        if user_id not in names:
            try:
                profile = app.client.users_info(user=user_id)["user"]["profile"]
                names[user_id] = profile.get("display_name") or profile.get("real_name") or user_id
            except Exception:
                return f"<@{user_id}>"
        return names[user_id]

    @app.event("message")
    def on_message(event):
        if event.get("bot_id") or event.get("subtype") not in (None, "thread_broadcast", "file_share"):
            return
        if channel and event.get("channel") != channel:
            return
        user, ts, text = event.get("user"), event["ts"], event.get("text", "")
        thread_ts = event.get("thread_ts")
        if thread_ts and thread_ts != ts:  # a reply in a thread
            if has_rocket(text):
                store.add_boost(event["channel"], thread_ts, user)
            return
        tweet_ids = extract_tweet_ids(text)
        if tweet_ids:
            store.add_share(event["channel"], ts, user, tweet_ids)
            wake.set()

    @app.command("/boostboard")
    def stats(ack, respond, command):
        ack()
        text = (command.get("text") or "").strip()
        if not text:
            respond(response_type="in_channel", text=format_leaderboard(store.leaderboard(), name_of))
            return
        m = HANDLE.match(text)
        if not m:
            respond("Usage: `/boostboard` for the leaderboard, or `/boostboard link @your_x_handle`.")
            return
        try:
            found = x.lookup_user(m.group(1))
        except XError as e:
            log.warning("handle lookup failed: %s", e)
            respond("Couldn't reach X to check that handle. Try again in a bit.")
            return
        if not found:
            respond(f"No X account found for @{m.group(1)}.")
            return
        store.link_x_account(command["user_id"], *found)
        respond(f"Linked you to @{found[1]} on X. Replies there now count toward your score.")

    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = Store(os.environ.get("DB_PATH", "boost.db"))
    x = XClient(os.environ["X_BEARER_TOKEN"])
    wake = threading.Event()
    app = build_app(
        store, x, wake,
        channel=os.environ.get("BOOST_CHANNEL_ID") or None,
        token=os.environ["SLACK_BOT_TOKEN"],
    )
    threading.Thread(
        target=poll_loop,
        args=(store, x, wake, float(os.environ.get("POLL_MINUTES", "15")) * 60,
              float(os.environ.get("TRACK_DAYS", "7"))),
        daemon=True,
    ).start()
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()

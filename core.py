"""Parsing, storage and scoring for the boost bot. No Slack or network code here."""
import json
import re
import sqlite3
import threading
import time

# Points per engagement. "follow" is listed for completeness but X exposes no
# per-tweet follow data, so nothing is ever scored against it.
WEIGHTS = {
    "like": 0.5,
    "repost": 1.0,
    "follow": 4.0,
    "reply": 5.0,
    "share": 20.0,         # assumed for every rocket reply
    "author_reply": 49.0,  # tweet author replied to the booster's reply on X
}

TWEET_URL = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:twitter|x)\.com/\w+/status(?:es)?/(\d+)", re.I
)
ROCKET = re.compile(r":rocket:|\U0001F680")

SCHEMA = """
CREATE TABLE IF NOT EXISTS shares (
  channel TEXT NOT NULL, message_ts TEXT NOT NULL, tweet_id TEXT NOT NULL,
  user_id TEXT NOT NULL, shared_at REAL NOT NULL,
  PRIMARY KEY (channel, message_ts, tweet_id));
CREATE TABLE IF NOT EXISTS boosts (
  tweet_id TEXT NOT NULL, user_id TEXT NOT NULL, boosted_at REAL NOT NULL,
  PRIMARY KEY (tweet_id, user_id));
CREATE TABLE IF NOT EXISTS tweets (
  tweet_id TEXT PRIMARY KEY, first_shared_at REAL NOT NULL,
  author_id TEXT, conversation_id TEXT,
  baseline TEXT, latest TEXT, polled_at REAL);
CREATE TABLE IF NOT EXISTS x_links (
  slack_user TEXT PRIMARY KEY, x_user_id TEXT NOT NULL, x_handle TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS x_replies (
  reply_id TEXT PRIMARY KEY, tweet_id TEXT NOT NULL,
  author_id TEXT NOT NULL, in_reply_to TEXT);
"""


def extract_tweet_ids(text):
    """Unique tweet ids linked in a Slack message, in order of appearance."""
    return list(dict.fromkeys(TWEET_URL.findall(text or "")))


def has_rocket(text):
    return bool(ROCKET.search(text or ""))


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock, self.db:
            self.db.executescript(SCHEMA)

    def add_share(self, channel, message_ts, user_id, tweet_ids, now=None):
        now = now or time.time()
        with self.lock, self.db:
            for tid in tweet_ids:
                self.db.execute(
                    "INSERT OR IGNORE INTO shares VALUES (?,?,?,?,?)",
                    (channel, message_ts, tid, user_id, now),
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO tweets (tweet_id, first_shared_at) VALUES (?,?)",
                    (tid, now),
                )

    def add_boost(self, channel, thread_ts, user_id, now=None):
        """Record a rocket reply in a thread. Returns how many new boosts it created.

        A user gets one boost per tweet, and none for a link they shared themselves.
        """
        now = now or time.time()
        with self.lock, self.db:
            tweet_ids = [
                r[0]
                for r in self.db.execute(
                    """SELECT tweet_id FROM shares WHERE channel=? AND message_ts=?
                       AND tweet_id NOT IN (SELECT tweet_id FROM shares WHERE user_id=?)""",
                    (channel, thread_ts, user_id),
                )
            ]
            new = 0
            for tid in tweet_ids:
                new += self.db.execute(
                    "INSERT OR IGNORE INTO boosts VALUES (?,?,?)", (tid, user_id, now)
                ).rowcount
            return new

    def link_x_account(self, slack_user, x_user_id, x_handle):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO x_links VALUES (?,?,?)",
                (slack_user, x_user_id, x_handle),
            )

    def tweets_to_poll(self, since, only_unseen=False):
        q = "SELECT tweet_id FROM tweets WHERE first_shared_at >= ?"
        if only_unseen:
            q += " AND baseline IS NULL"
        with self.lock:
            return [r[0] for r in self.db.execute(q, (since,))]

    def record_tweet(self, tweet_id, metrics, author_id, conversation_id, now=None):
        """Save the latest counts; the first counts seen become the baseline."""
        blob = json.dumps(metrics)
        with self.lock, self.db:
            self.db.execute(
                """UPDATE tweets SET latest=?, baseline=COALESCE(baseline, ?),
                   author_id=?, conversation_id=?, polled_at=? WHERE tweet_id=?""",
                (blob, blob, author_id, conversation_id, now or time.time(), tweet_id),
            )

    def reply_targets(self, since):
        """Tweets worth searching for replies: [(tweet_id, conversation_id, author_id, [booster x ids])]."""
        with self.lock:
            rows = self.db.execute(
                """SELECT t.tweet_id, t.conversation_id, t.author_id, l.x_user_id
                   FROM tweets t JOIN boosts b ON b.tweet_id = t.tweet_id
                   JOIN x_links l ON l.slack_user = b.user_id
                   WHERE t.first_shared_at >= ? AND t.author_id IS NOT NULL""",
                (since,),
            ).fetchall()
        out = {}
        for tid, conv, author, xid in rows:
            out.setdefault((tid, conv or tid, author), []).append(xid)
        return [(*k, v) for k, v in out.items()]

    def add_replies(self, tweet_id, replies):
        """replies: iterable of (reply_id, author_id, in_reply_to_user_id)."""
        with self.lock, self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO x_replies VALUES (?,?,?,?)",
                [(rid, tweet_id, author, to) for rid, author, to in replies],
            )

    def leaderboard(self, weights=WEIGHTS):
        """Per-user stats, ranked by boost count (points break ties)."""
        with self.lock:
            tweets = {
                r[0]: (json.loads(r[1] or "{}"), json.loads(r[2] or "{}"), r[3])
                for r in self.db.execute(
                    "SELECT tweet_id, baseline, latest, author_id FROM tweets"
                )
            }
            boosts = self.db.execute("SELECT tweet_id, user_id FROM boosts").fetchall()
            links = dict(self.db.execute("SELECT slack_user, x_user_id FROM x_links"))
            shares = self.db.execute(
                "SELECT user_id, COUNT(DISTINCT tweet_id) FROM shares GROUP BY user_id"
            ).fetchall()
            replies = self.db.execute(
                "SELECT tweet_id, author_id, in_reply_to FROM x_replies"
            ).fetchall()

        stats = {}

        def row(user):
            return stats.setdefault(user, {"user": user, "boosts": 0, "links": 0, "points": 0.0})

        for user, n in shares:
            row(user)["links"] = n

        boosters = {}
        for tid, user in boosts:
            boosters.setdefault(tid, []).append(user)

        by_x_id = {}  # (tweet_id, booster's X id) -> slack user
        for tid, users in boosters.items():
            baseline, latest, author = tweets.get(tid, ({}, {}, None))
            # Likes and reposts gained since the link was shared, split evenly:
            # X does not say who liked or reposted.
            pool = sum(
                weights[k] * max(0, latest.get(k, 0) - baseline.get(k, 0))
                for k in ("like", "repost")
            )
            for user in users:
                r = row(user)
                r["boosts"] += 1
                r["points"] += weights["share"] + pool / len(users)
                xid = links.get(user)
                if xid and xid != author:
                    by_x_id[(tid, xid)] = user

        for tid, reply_author, to in replies:
            if (tid, reply_author) in by_x_id:
                row(by_x_id[(tid, reply_author)])["points"] += weights["reply"]
            elif reply_author == tweets.get(tid, (0, 0, None))[2] and (tid, to) in by_x_id:
                row(by_x_id[(tid, to)])["points"] += weights["author_reply"]

        return sorted(stats.values(), key=lambda r: (-r["boosts"], -r["points"], -r["links"]))

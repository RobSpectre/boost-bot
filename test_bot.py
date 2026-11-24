import time
import unittest

from core import Store, extract_tweet_ids, has_rocket


class ParsingTest(unittest.TestCase):
    def test_extracts_ids_from_slack_formatted_links(self):
        text = "check <https://x.com/alice/status/111?s=20> and <https://twitter.com/bob/status/222|this> and https://x.com/alice/status/111"
        self.assertEqual(extract_tweet_ids(text), ["111", "222"])
        self.assertEqual(extract_tweet_ids("https://x.com/alice"), [])

    def test_rocket(self):
        self.assertTrue(has_rocket("done :rocket:"))
        self.assertTrue(has_rocket("\U0001F680"))
        self.assertFalse(has_rocket("done :tada:"))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.s.add_share("C1", "100.1", "sharer", ["111"])

    def test_boost_once_per_tweet_and_not_own_link(self):
        self.assertEqual(self.s.add_boost("C1", "100.1", "ann"), 1)
        self.assertEqual(self.s.add_boost("C1", "100.1", "ann"), 0)
        self.assertEqual(self.s.add_boost("C1", "100.1", "sharer"), 0)
        self.assertEqual(self.s.add_boost("C1", "999.9", "ann"), 0)  # not a share thread

    def test_baseline_is_first_poll(self):
        self.assertEqual(self.s.tweets_to_poll(0, only_unseen=True), ["111"])
        self.s.record_tweet("111", {"like": 10, "repost": 2}, "author", "111")
        self.assertEqual(self.s.tweets_to_poll(0, only_unseen=True), [])
        self.assertEqual(self.s.tweets_to_poll(0), ["111"])
        self.assertEqual(self.s.tweets_to_poll(time.time() + 60), [])

    def test_leaderboard_scoring(self):
        for u in ("ann", "ben"):
            self.s.add_boost("C1", "100.1", u)
        self.s.add_share("C1", "200.1", "ann", ["222"])
        self.s.add_boost("C1", "200.1", "ben")
        self.s.link_x_account("ann", "x_ann", "ann")
        self.s.record_tweet("111", {"like": 10, "repost": 2}, "x_author", "111")
        self.s.record_tweet("111", {"like": 14, "repost": 4}, "x_author", "111")
        self.assertEqual(self.s.reply_targets(0), [("111", "111", "x_author", ["x_ann"])])
        self.s.add_replies("111", [
            ("r1", "x_ann", "x_author"),     # ann replied on X: +5
            ("r2", "x_author", "x_ann"),     # author replied to ann: +49
            ("r3", "x_author", "x_other"),   # author replied to someone else: nothing
            ("r2", "x_author", "x_ann"),     # duplicate from a later poll: ignored
        ])
        board = {r["user"]: r for r in self.s.leaderboard()}
        # tweet 111 pool: 4 likes * 0.5 + 2 reposts * 1.0 = 4, split two ways = 2 each
        self.assertEqual(board["ben"], {"user": "ben", "boosts": 2, "links": 0, "points": 20 + 2 + 20})
        self.assertEqual(board["ann"], {"user": "ann", "boosts": 1, "links": 1, "points": 20 + 2 + 5 + 49})
        self.assertEqual(board["sharer"]["links"], 1)
        self.assertEqual([r["user"] for r in self.s.leaderboard()], ["ben", "ann", "sharer"])


try:
    from slack_bolt import BoltRequest
    import bot
except ImportError:
    bot = None


@unittest.skipUnless(bot, "slack_bolt not installed")
class SlackWiringTest(unittest.TestCase):
    def setUp(self):
        import threading
        self.s = Store(":memory:")
        self.wake = threading.Event()
        from slack_bolt.authorization import AuthorizeResult
        self.app = bot.build_app(
            self.s, None, self.wake, channel="C1", signing_secret="test",
            authorize=lambda **_: AuthorizeResult(enterprise_id=None, team_id="T1", bot_user_id="UBOT", bot_token="xoxb-test"),
        )

    def send(self, **event):
        body = {"type": "event_callback", "event": {"type": "message", "channel": "C1", **event}}
        self.assertEqual(self.app.dispatch(BoltRequest(body=body, mode="socket_mode")).status, 200)

    def wait_for(self, cond):
        deadline = time.time() + 3
        while not cond() and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(cond())

    def test_share_then_rocket_reply(self):
        self.send(user="U1", ts="100.1", text="<https://x.com/a/status/111>")
        self.wait_for(lambda: self.s.tweets_to_poll(0) == ["111"])
        self.assertTrue(self.wake.is_set())
        self.send(user="U2", ts="100.2", thread_ts="100.1", text="nice")
        self.send(user="U2", ts="100.3", thread_ts="100.1", text=":rocket:")
        self.send(user="U3", ts="300.1", channel="C2", text="<https://x.com/a/status/333>")
        self.wait_for(lambda: any(r["boosts"] for r in self.s.leaderboard()))
        time.sleep(0.2)
        board = self.s.leaderboard()
        self.assertEqual([(r["user"], r["boosts"], r["links"]) for r in board], [("U2", 1, 0), ("U1", 0, 1)])
        text = bot.format_leaderboard(board, lambda u: u)
        self.assertIn("*U2* — 1 boost · 0 links shared · 20 pts", text)


if __name__ == "__main__":
    unittest.main()

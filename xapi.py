"""Minimal X API v2 client (app-only bearer token)."""
import json
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.x.com/2"


class XError(Exception):
    pass


class XClient:
    def __init__(self, bearer_token):
        self.bearer = bearer_token

    def _get(self, path, params=None):
        url = f"{BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.bearer}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            raise XError(f"X API {e.code} on {path}: {e.read()[:300]!r}") from e
        except urllib.error.URLError as e:
            raise XError(f"X API unreachable: {e.reason}") from e

    def fetch_tweets(self, tweet_ids):
        """Up to 100 ids -> {id: {"metrics": {...}, "author_id", "conversation_id"}}.

        Deleted or protected tweets are simply absent from the result.
        """
        body = self._get(
            "/tweets",
            {
                "ids": ",".join(tweet_ids),
                "tweet.fields": "public_metrics,author_id,conversation_id",
            },
        )
        out = {}
        for t in body.get("data", []):
            m = t.get("public_metrics", {})
            out[t["id"]] = {
                "metrics": {
                    "like": m.get("like_count", 0),
                    "repost": m.get("retweet_count", 0) + m.get("quote_count", 0),
                },
                "author_id": t.get("author_id"),
                "conversation_id": t.get("conversation_id"),
            }
        return out

    def fetch_replies(self, conversation_id, from_user_ids, max_pages=5):
        """Replies in a conversation written by the given X user ids (last 7 days).

        Returns [(reply_id, author_id, in_reply_to_user_id)].
        """
        who = " OR ".join(f"from:{u}" for u in from_user_ids)
        params = {
            "query": f"conversation_id:{conversation_id} ({who})",
            "tweet.fields": "author_id,in_reply_to_user_id",
            "max_results": 100,
        }
        out = []
        for _ in range(max_pages):
            body = self._get("/tweets/search/recent", params)
            out += [
                (t["id"], t["author_id"], t.get("in_reply_to_user_id"))
                for t in body.get("data", [])
            ]
            token = body.get("meta", {}).get("next_token")
            if not token:
                break
            params["next_token"] = token
        return out

    def lookup_user(self, handle):
        """@handle -> (x_user_id, canonical handle), or None if no such account."""
        body = self._get(f"/users/by/username/{urllib.parse.quote(handle)}")
        data = body.get("data")
        return (data["id"], data["username"]) if data else None

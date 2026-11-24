# Boost Bot

Slack bot that keeps score for the X boost crew.

- Someone posts an X link in the channel: that's a **shared link**.
- Someone replies in the thread with :rocket: that's a **boost**.
- `/stats` posts the leaderboard, ranked by who boosts the most.
- `/stats link @handle` ties your Slack user to your X account so replies on X can be credited to you.

## Scoring

| Engagement | Points | How it is credited |
| --- | --- | --- |
| Copy-link share | 20 | Assumed for every boost |
| Author replies to your reply | 49 | Each reply from the tweet's author to you on X (needs a linked handle) |
| Reply | 5 | Each reply you post in the tweet's conversation (needs a linked handle) |
| Repost | 1 | Reposts + quotes gained since the link was shared, split evenly between its boosters |
| Like | 0.5 | Likes gained since the link was shared, split evenly between its boosters |
| Follow | 4 | Not scored: X has no per-tweet follow data |

One boost per person per tweet; rocket-replying to your own link doesn't count.
Weights live in `WEIGHTS` in [core.py](core.py).

## How it connects

There are no webhooks and no inbound ports. The bot opens an outbound WebSocket to Slack
(Socket Mode) for messages and `/stats`, and polls the X API on a timer. Any always-on
machine with outbound HTTPS works; it needs no public URL, TLS certificate or tunnel.

Run exactly one instance. Slack spreads events across open connections, so a second copy
would silently take half the messages into its own database.

## Install

Three secrets are needed. A person with the right access has to create them; they cannot
be generated from the command line.

| Variable | Where it comes from |
| --- | --- |
| `SLACK_BOT_TOKEN` | https://api.slack.com/apps → **Create New App → From a manifest**, paste [manifest.yaml](manifest.yaml), **Install to Workspace**, then **OAuth & Permissions → Bot User OAuth Token** (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Same app → **Basic Information → App-Level Tokens** → generate one with the `connections:write` scope (`xapp-...`) |
| `X_BEARER_TOKEN` | X developer console → your app's bearer token. The plan must allow tweet lookup, recent search and user lookup |

Then in Slack, invite the bot to the boost channel with `/invite @Boost Bot`. To limit
tracking to that channel, copy its Channel ID (channel details → About) into
`BOOST_CHANNEL_ID`; left blank, every channel the bot is invited to is tracked.

### Try it in the foreground

Needs Python 3.10+ and the venv module (`apt install python3-venv` on Debian/Ubuntu).

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in the tokens
set -a; source .env; set +a
.venv/bin/python bot.py
```

A healthy start logs `Bolt app is running!`. Post an X link in the channel, reply to it
with :rocket:, and run `/stats`.

### Run it as a service (Linux, systemd)

```bash
sudo useradd --system --home /opt/boost-bot --shell /usr/sbin/nologin boostbot
sudo git clone git@github.com:RobSpectre/boost-bot.git /opt/boost-bot
cd /opt/boost-bot
sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env          # then fill in the tokens
sudo chmod 600 .env
sudo chown -R boostbot: /opt/boost-bot
sudo cp deploy/boost-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now boost-bot
```

If you install somewhere other than `/opt/boost-bot` or under a different user, edit the
paths and `User=` in [deploy/boost-bot.service](deploy/boost-bot.service) first. The
service restarts on crash and starts at boot.

### Operating it

- Status: `systemctl status boost-bot`
- Logs: `journalctl -u boost-bot -f`. `poll failed` lines mean an X API problem (bad token,
  rate limit, plan without search access); the bot keeps running and retries next cycle.
- Update: `sudo -u boostbot git -C /opt/boost-bot pull && sudo systemctl restart boost-bot`
- Data: everything is in `/opt/boost-bot/boost.db` (SQLite). Back that file up; deleting it
  resets the leaderboard.

## Polling

Every `POLL_MINUTES` the bot refreshes counts for links shared in the last `TRACK_DAYS`
(one request per 100 tweets) and runs one reply search per tweet that has a booster with a
linked handle. X's recent search only reaches back 7 days, so keep `TRACK_DAYS` at 7 or less.

## Tests

```bash
.venv/bin/python -m unittest -v
```

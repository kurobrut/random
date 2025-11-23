# bot_notifier.py
import os
import json
import time
import random
import requests
import urllib3
from datetime import datetime, timezone
import asyncio
from typing import Tuple, Dict, Any
import traceback

import discord
from discord.ext import commands, tasks

# disable insecure warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ===== LOAD CONFIG =====
with open("config.json", "r") as f:
    config = json.load(f)

# config fields (expected)
webhook_urls = config.get("webhook_urls") or [config.get("webhook_url")]
ratelimit_webhook_url = config.get("ratelimit_webhook_url")
target_users: Dict[str, int] = config.get("target_users", {})
cookie = config.get("cookie")

# ===== ENV =====
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable!")

# ===== HEADERS / GLOBALS =====
HEADERS = {
    "Cookie": f".ROBLOSECURITY={cookie}",
    "Content-Type": "application/json"
} if cookie else {}

COLORS = {
    "playing": 0x77dd77,
    "online": 0x89CFF0,
    "offline": 0xFF6961,
    "same_server": 0x9b59b6,
    "ratelimit": 0xFFA500,
    "renewal": 0xF1C40F
}

CACHE_FILE = "place_cache.json"
if os.path.exists(CACHE_FILE):
    try:
        with open(CACHE_FILE, "r") as f:
            place_cache = json.load(f)
        if not isinstance(place_cache, dict):
            place_cache = {}
    except Exception:
        place_cache = {}
else:
    place_cache = {}

def _save_cache_sync():
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(place_cache, f, indent=2)
    except Exception as e:
        print("[Error] saving cache:", e)

async def save_cache():
    await asyncio.to_thread(_save_cache_sync)

previous_data: Dict[str, Any] = {}
notified_pair = False

# ===== Rate limit cooldown trackers (sync-state) =====
last_rate_limit_sent = {"roblox": 0.0, "discord": 0.0}

# ---------- Blocking helpers (run in thread) ----------
def _send_rate_limit_alert_sync(api_name: str, description: str):
    """Send a rate-limit embed via ratelimit_webhook_url (blocking)."""
    try:
        now = time.time()
        if now - last_rate_limit_sent.get(api_name, 0) < 60:
            # cooldown (do not spam alerts)
            print(f"[Rate Limit] {api_name} hit, but cooldown active. Skipping Discord alert.")
            return

        alert_payload = {
            "embeds": [{
                "title": f"⚠️ {api_name.capitalize()} API Rate Limit",
                "description": description,
                "color": COLORS["ratelimit"],
                "timestamp": datetime.now(timezone.utc).isoformat()
            }]
        }
        if ratelimit_webhook_url:
            r = requests.post(ratelimit_webhook_url, json=alert_payload, timeout=10)
            print(f"[Rate Limit] Alert sent for {api_name} (status {r.status_code})")
        else:
            print("[Rate Limit] No ratelimit_webhook_url configured; skipping send.")
        last_rate_limit_sent[api_name] = now
    except Exception as e:
        print(f"[Error] Failed to send rate limit alert: {e}")

def _safe_discord_request_sync(payload: dict, webhook: str) -> requests.Response:
    """Send to a webhook with builtin discord-ratelimit retry (blocking)."""
    while True:
        try:
            r = requests.post(webhook, json=payload, timeout=10)
        except Exception as e:
            print(f"[Error] Discord webhook post failed: {e}. Retrying shortly...")
            time.sleep(1 + random.random())
            continue

        if r.status_code == 429:
            try:
                data = r.json()
                retry_after = data.get("retry_after", 1)
            except Exception:
                retry_after = 1
            print(f"[Discord Rate Limit] Waiting {retry_after}s before retrying...")
            _send_rate_limit_alert_sync("discord", f"Retrying after **{retry_after}** seconds")
            time.sleep(retry_after)
            continue
        return r

def _safe_request_sync(method: str, url: str, **kwargs) -> requests.Response:
    """Blocking safe request with retry/backoff and roblox rate-limit handling."""
    retries = 0
    base_delay = 1
    max_delay = 60
    while True:
        try:
            r = requests.request(method, url, verify=False, timeout=10, **kwargs)

            # roblox rate limit
            if r.status_code == 429 or r.headers.get("x-ratelimit-remaining") == "0":
                try:
                    reset_time = int(r.headers.get("x-ratelimit-reset", 2))
                except Exception:
                    reset_time = 2
                wait = reset_time + random.uniform(0.5, 1.5)
                print(f"[Roblox Rate Limit] Waiting {wait:.1f}s... ({url})")
                _send_rate_limit_alert_sync("roblox", f"Endpoint: `{url}`\nRetrying after **{wait:.1f}s**")
                time.sleep(wait)
                continue

            # server errors
            if 500 <= r.status_code < 600:
                wait = min(base_delay * (2 ** retries), max_delay) + random.uniform(0, 1)
                print(f"[Server Error {r.status_code}] Retrying in {wait:.1f}s... ({url})")
                time.sleep(wait)
                retries += 1
                continue

            return r
        except requests.ConnectionError as e:
            wait = min(base_delay * (2 ** retries), max_delay) + random.uniform(0, 1)
            print(f"[Connection Error] {e} - Retrying in {wait:.1f}s... ({url})")
            time.sleep(wait)
            retries += 1
        except requests.RequestException as e:
            wait = min(base_delay * (2 ** retries), max_delay) + random.uniform(0, 1)
            print(f"[Request Error] {e} - Retrying in {wait:.1f}s... ({url})")
            time.sleep(wait)
            retries += 1

# ---------- Async wrappers that call blocking helpers in thread ----------
async def send_rate_limit_alert(api_name: str, description: str):
    await asyncio.to_thread(_send_rate_limit_alert_sync, api_name, description)

async def safe_discord_request(payload: dict, webhook: str):
    return await asyncio.to_thread(_safe_discord_request_sync, payload, webhook)

async def safe_request(method: str, url: str, **kwargs) -> requests.Response:
    return await asyncio.to_thread(_safe_request_sync, method, url, **kwargs)

# ---------- Username & game helpers ----------
async def get_username(user_id: int) -> str:
    url = f"https://users.roblox.com/v1/users/{user_id}"
    try:
        res = await safe_request("GET", url, headers=HEADERS)
        if res and res.status_code == 200:
            return res.json().get("name", f"User_{user_id}")
    except Exception as e:
        print("[Error] get_username:", e)
    return f"User_{user_id}"

async def get_game_name_from_place(place_id) -> Tuple[str, str]:
    place_id = str(place_id)
    if place_id in place_cache and place_cache[place_id].get("placeName") != "Unknown Game":
        return place_cache[place_id]["placeName"], place_cache[place_id]["url"]

    url = f"https://games.roblox.com/v1/games/multiget-place-details?placeIds={place_id}"
    try:
        res = await safe_request("GET", url, headers=HEADERS)
        if res and res.status_code == 200:
            data = res.json()
            if data:
                info = data[0]
                game_name = info.get("name", "Unknown Game")
                safe_name = game_name.replace(" ", "-").replace("/", "-")
                game_url = f"https://www.roblox.com/games/{place_id}/{safe_name}"
                place_cache[place_id] = {
                    "placeId": place_id,
                    "placeName": game_name,
                    "universeId": info.get("universeId"),
                    "creatorName": info.get("creatorName"),
                    "creatorType": info.get("creatorType"),
                    "url": game_url,
                    "lastSeen": datetime.now(timezone.utc).isoformat()
                }
                await save_cache()
                return game_name, game_url
    except Exception as e:
        print("[Error] get_game_name_from_place:", e)

    place_cache[place_id] = {
        "placeId": place_id,
        "placeName": "Unknown Game",
        "universeId": None,
        "creatorName": None,
        "creatorType": None,
        "url": f"https://www.roblox.com/games/{place_id}",
        "lastSeen": datetime.now(timezone.utc).isoformat()
    }
    await save_cache()
    return "Unknown Game", f"https://www.roblox.com/games/{place_id}"

# ---------- Webhook sender (async) ----------
MY_ID_MENTION = config.get("my_id_mention") or ""  # if you want a mention, set this id in config

async def send_embed(title: str, description: str, color: int, mention_everyone: bool = False):
    content = f"<@{MY_ID_MENTION}>" if mention_everyone and MY_ID_MENTION else ""
    payload = {
        "content": content,
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }]
    }

    if not webhook_urls:
        print("[Warning] No webhook URLs configured.")
        return

    # send to all webhooks (sequentially). They themselves handle discord rate limits.
    for webhook in webhook_urls:
        try:
            r = await safe_discord_request(payload, webhook)
            if r is not None:
                # debug
                print(f"[Webhook] Sent '{title}' to webhook (status {r.status_code})")
        except Exception as e:
            print(f"[Error] sending embed to webhook {webhook}: {e}")

# ---------- Core check_players (async) ----------
async def check_players():
    global notified_pair, previous_data

    try:
        id_list = list(target_users.values())
        if not id_list:
            print("[Warning] No valid user IDs to check")
            return

        res = await safe_request("POST", "https://presence.roblox.com/v1/presence/users",
                                 json={"userIds": id_list}, headers=HEADERS)
        if not res or res.status_code != 200:
            print("[Error] Presence API failed", getattr(res, "status_code", None))
            return

        response = res.json()
        users_in_games: Dict[str, int] = {}
        presences: Dict[str, tuple] = {}
        place_ids: Dict[str, Any] = {}

        # Prepare statuses
        for presence in response.get("userPresences", []):
            uid = presence["userId"]
            friendly_name = next((name for name, id_ in target_users.items() if id_ == uid), f"User_{uid}")
            username = await get_username(uid)
            presence_type = presence.get("userPresenceType")
            game_id = presence.get("gameId")
            place_id = presence.get("placeId")

            status = ""
            color = COLORS["offline"]

            if presence_type in [2, 3]:
                game_name, game_url = await get_game_name_from_place(place_id) if place_id else ("Unknown Game", "")
                status = f"🎮 {username} is playing: {game_name}\n🔗 {game_url}"
                color = COLORS["playing"]
                if game_id:
                    users_in_games[friendly_name] = game_id
                    place_ids[friendly_name] = place_id
            elif presence_type == 1:
                status = f"🟢 {username} is online (not in game)"
                color = COLORS["online"]
            else:
                status = f"🔴 {username} is offline"
                color = COLORS["offline"]

            presences[friendly_name] = (status, presence_type, color)

        # === Same server check for particular target (kei_lanii44) ===
        a = "kei_lanii44"
        if a in users_in_games:
            same_server_players = {
                other for other, gid in users_in_games.items()
                if other != a and gid == users_in_games[a]
            }

            if same_server_players:
                game_name, game_url = await get_game_name_from_place(place_ids[a]) if a in place_ids else ("Unknown Game", "")
                players_signature = tuple(sorted(same_server_players))
                if previous_data.get("same_server") != players_signature:
                    others_list = ", ".join(same_server_players)
                    description = (
                        f"{a} is in the same server with:\n"
                        f"👥 {others_list}\n\n"
                        f"🎮 Game: {game_name}\n🔗 {game_url}"
                    )
                    # mention flag: your config's "my_id_mention" will be used if set
                    await send_embed("🎯 Target Match", description, COLORS["same_server"], mention_everyone=True)
                    previous_data["same_server"] = players_signature

                # don't send individual updates for the matched user
                presences.pop(a, None)
            else:
                previous_data["same_server"] = None

        # --- Send normal presence updates ---
        for friendly_name, (status, presence_type, color) in presences.items():
            if previous_data.get(friendly_name) != status:
                mention = (friendly_name == "kei_lanii44" and presence_type in [2, 3])
                await send_embed("Presence Update", status, color, mention_everyone=mention)
                previous_data[friendly_name] = status

    except Exception as e:
        print("[Error] check_players failed:", e)
        traceback.print_exc()

# ---------- Bot setup & loop ----------
intents = discord.Intents.default()
intents.messages = True  # not strictly needed, but safe to include

bot = commands.Bot(command_prefix="!", intents=intents)

@tasks.loop(seconds=20)
async def monitor_loop():
    # monitor_loop runs inside the event loop and calls async check
    await check_players()

@bot.event
async def on_ready():
    print(f"[Bot] Logged in as {bot.user} (id {bot.user.id})")
    if not monitor_loop.is_running():
        monitor_loop.start()

# optional command to force-run check
@bot.command(name="checknow")
@commands.is_owner()
async def cmd_checknow(ctx):
    await ctx.send("Running check now...")
    await check_players()
    await ctx.send("Done.")

# run bot
if __name__ == "__main__":
    try:
        bot.run(BOT_TOKEN)
    except KeyboardInterrupt:
        print("Shutting down...")
    except Exception as e:
        print("Failed to start bot:", e)

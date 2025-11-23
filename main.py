# bot_notifier_botmode.py
import os
import json
import asyncio
import traceback
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
import requests
import urllib3
import random
import time

# disable insecure warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ===== LOAD CONFIG =====
with open("config.json", "r") as f:
    config = json.load(f)

target_users = config.get("target_users", {})
cookie = config.get("cookie")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", 0))  # must set in GitHub secrets
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN or not CHANNEL_ID:
    raise RuntimeError("BOT_TOKEN or CHANNEL_ID not set in env!")

# ===== HEADERS / GLOBALS =====
HEADERS = {
    "Cookie": f".ROBLOSECURITY={cookie}",
    "Content-Type": "application/json"
} if cookie else {}

COLORS = {
    "playing": 0x77dd77,
    "online": 0x89CFF0,
    "offline": 0xFF6961,
    "same_server": 0x9b59b6
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

def save_cache_sync():
    with open(CACHE_FILE, "w") as f:
        json.dump(place_cache, f, indent=2)

async def save_cache():
    await asyncio.to_thread(save_cache_sync)

previous_data = {}

# ===== SAFE REQUEST =====
def safe_request_sync(method: str, url: str, **kwargs):
    retries = 0
    base_delay = 1
    max_delay = 60
    while True:
        try:
            r = requests.request(method, url, verify=False, timeout=10, **kwargs)
            if r.status_code == 429 or r.headers.get("x-ratelimit-remaining") == "0":
                reset_time = int(r.headers.get("x-ratelimit-reset", 2))
                wait = reset_time + random.uniform(0.5, 1.5)
                print(f"[Roblox Rate Limit] Waiting {wait:.1f}s for {url}")
                time.sleep(wait)
                continue
            if 500 <= r.status_code < 600:
                wait = min(base_delay * (2 ** retries), max_delay) + random.uniform(0, 1)
                print(f"[Server Error {r.status_code}] Retrying in {wait:.1f}s")
                time.sleep(wait)
                retries += 1
                continue
            return r
        except requests.RequestException as e:
            wait = min(base_delay * (2 ** retries), max_delay) + random.uniform(0, 1)
            print(f"[Request Error] {e} - retrying in {wait:.1f}s")
            time.sleep(wait)
            retries += 1

async def safe_request(method: str, url: str, **kwargs):
    return await asyncio.to_thread(safe_request_sync, method, url, **kwargs)

# ===== USERNAME & GAME HELPERS =====
async def get_username(user_id: int) -> str:
    url = f"https://users.roblox.com/v1/users/{user_id}"
    try:
        res = await safe_request("GET", url, headers=HEADERS)
        if res and res.status_code == 200:
            return res.json().get("name", f"User_{user_id}")
    except Exception:
        pass
    return f"User_{user_id}"

async def get_game_name_from_place(place_id):
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
    except Exception:
        pass
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

# ===== BOT SETUP =====
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

async def send_embed(title: str, description: str, color: int, channel_id: int = CHANNEL_ID):
    channel = bot.get_channel(channel_id)
    if not channel:
        print("[Error] Channel not found")
        return
    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
    await channel.send(embed=embed)

# ===== CORE CHECK PLAYERS =====
async def check_players():
    global previous_data
    try:
        id_list = list(target_users.values())
        if not id_list:
            print("[Warning] No valid user IDs")
            return
        res = await safe_request("POST", "https://presence.roblox.com/v1/presence/users", json={"userIds": id_list}, headers=HEADERS)
        if not res or res.status_code != 200:
            print("[Error] Presence API failed")
            return
        response = res.json()
        users_in_games = {}
        presences = {}
        place_ids = {}
        for presence in response.get("userPresences", []):
            uid = presence["userId"]
            friendly_name = next((name for name, id_ in target_users.items() if id_ == uid), f"User_{uid}")
            username = await get_username(uid)
            presence_type = presence.get("userPresenceType")
            game_id = presence.get("gameId")
            place_id = presence.get("placeId")
            status = ""
            color = COLORS["offline"]
            if presence_type in [2,3]:
                game_name, game_url = await get_game_name_from_place(place_id) if place_id else ("Unknown Game", "")
                status = f"🎮 {username} is playing: {game_name}\n🔗 {game_url}"
                color = COLORS["playing"]
                if game_id:
                    users_in_games[friendly_name] = game_id
                    place_ids[friendly_name] = place_id
            elif presence_type == 1:
                status = f"🟢 {username} is online (not in game)"
                color = COLORS["online"]
            presences[friendly_name] = (status, presence_type, color)

        a = "kei_lanii44"
        if a in users_in_games:
            same_server_players = {other for other,gid in users_in_games.items() if other!=a and gid==users_in_games[a]}
            if same_server_players:
                game_name, game_url = await get_game_name_from_place(place_ids[a]) if a in place_ids else ("Unknown Game","")
                players_signature = tuple(sorted(same_server_players))
                if previous_data.get("same_server") != players_signature:
                    others_list = ", ".join(same_server_players)
                    description = f"{a} is in the same server with:\n👥 {others_list}\n\n🎮 Game: {game_name}\n🔗 {game_url}"
                    await send_embed("🎯 Target Match", description, COLORS["same_server"])
                    previous_data["same_server"] = players_signature
                presences.pop(a, None)
            else:
                previous_data["same_server"] = None

        for friendly_name, (status, presence_type, color) in presences.items():
            if previous_data.get(friendly_name) != status:
                mention = (friendly_name == "kei_lanii44" and presence_type in [2,3])
                await send_embed("Presence Update", status, color)
                previous_data[friendly_name] = status

    except Exception:
        traceback.print_exc()

# ===== LOOP =====
@tasks.loop(seconds=20)
async def monitor_loop():
    await check_players()

@bot.event
async def on_ready():
    print(f"[Bot] Logged in as {bot.user}")
    if not monitor_loop.is_running():
        monitor_loop.start()

# optional command
@bot.command(name="checknow")
@commands.is_owner()
async def cmd_checknow(ctx):
    await ctx.send("Running check now...")
    await check_players()
    await ctx.send("Done.")

# run bot
if __name__ == "__main__":
    bot.run(BOT_TOKEN)

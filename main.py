import os
import json
import time
import random
import requests
import urllib3
from datetime import datetime, timezone
import asyncio
import discord
from discord.ext import tasks, commands

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ===== LOAD CONFIG =====
with open("config.json") as f:
    config = json.load(f)

target_users = config.get("target_users", {})
cookie = os.environ.get("COOKIE")  # GitHub secret

if not cookie:
    raise RuntimeError("No cookie found! Please set COOKIE env secret.")

HEADERS = {
    "Cookie": f".ROBLOSECURITY={cookie}",
    "Content-Type": "application/json"
}

COLORS = {
    "playing": 0x77dd77,
    "online": 0x89CFF0,
    "offline": 0xFF6961
}

CACHE_FILE = "place_cache.json"
if os.path.exists(CACHE_FILE):
    try:
        with open(CACHE_FILE, "r") as f:
            place_cache = json.load(f)
    except Exception:
        place_cache = {}
else:
    place_cache = {}

def save_cache():
    with open(CACHE_FILE, "w") as f:
        json.dump(place_cache, f, indent=2)

previous_data = {}

# ===== SAFE REQUEST =====
def safe_request(method, url, **kwargs):
    retries = 0
    while True:
        try:
            r = requests.request(method, url, verify=False, timeout=10, **kwargs)
            if r.status_code == 429:
                retry = int(r.headers.get("retry-after", 1))
                time.sleep(retry)
                continue
            return r
        except Exception:
            wait = min(2 ** retries, 60) + random.random()
            time.sleep(wait)
            retries += 1

# ===== GET USERNAME =====
def get_username(user_id):
    url = f"https://users.roblox.com/v1/users/{user_id}"
    res = safe_request("GET", url, headers=HEADERS)
    if res and res.status_code == 200:
        return res.json().get("name", f"User_{user_id}")
    return f"User_{user_id}"

# ===== GET GAME NAME =====
def get_game_name_from_place(place_id):
    place_id = str(place_id)
    if place_id in place_cache and place_cache[place_id]["placeName"] != "Unknown Game":
        return place_cache[place_id]["placeName"], place_cache[place_id]["url"]

    url = f"https://games.roblox.com/v1/games/multiget-place-details?placeIds={place_id}"
    res = safe_request("GET", url, headers=HEADERS)
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
                "url": game_url,
                "lastSeen": datetime.now(timezone.utc).isoformat()
            }
            save_cache()
            return game_name, game_url

    place_cache[place_id] = {
        "placeId": place_id,
        "placeName": "Unknown Game",
        "url": f"https://www.roblox.com/games/{place_id}",
        "lastSeen": datetime.now(timezone.utc).isoformat()
    }
    save_cache()
    return "Unknown Game", f"https://www.roblox.com/games/{place_id}"

# ===== CHECK PLAYERS =====
async def check_players(bot, channel_id):
    global previous_data
    id_list = list(target_users.values())
    if not id_list:
        return

    res = safe_request("POST", "https://presence.roblox.com/v1/presence/users", json={"userIds": id_list}, headers=HEADERS)
    if not res or res.status_code != 200:
        return

    response = res.json()
    channel = bot.get_channel(channel_id)
    if not channel:
        print(f"Channel {channel_id} not found!")
        return

    for presence in response.get("userPresences", []):
        uid = presence["userId"]
        friendly_name = next((n for n, i in target_users.items() if i == uid), f"User_{uid}")
        username = get_username(uid)
        presence_type = presence.get("userPresenceType")
        place_id = presence.get("placeId")

        status = ""
        color = COLORS["offline"]
        if presence_type in [2, 3]:
            game_name, game_url = get_game_name_from_place(place_id) if place_id else ("Unknown Game", "")
            status = f"🎮 {username} is playing: {game_name}\n🔗 {game_url}"
            color = COLORS["playing"]
        elif presence_type == 1:
            status = f"🟢 {username} is online (not in game)"
            color = COLORS["online"]
        else:
            status = f"🔴 {username} is offline"
            color = COLORS["offline"]

        if previous_data.get(friendly_name) != status:
            embed = discord.Embed(title="Presence Update", description=status, color=color, timestamp=datetime.now(timezone.utc))
            await channel.send(embed=embed)
            previous_data[friendly_name] = status

# ===== BOT SETUP =====
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

CHANNEL_ID = int(os.environ.get("CHANNEL_ID"))  # GitHub secret
BOT_TOKEN = os.environ.get("BOT_TOKEN")        # GitHub secret

@bot.event
async def on_ready():
    print(f"[Bot] Logged in as {bot.user}")
    monitor_loop.start()

@tasks.loop(seconds=20)
async def monitor_loop():
    await check_players(bot, CHANNEL_ID)

bot.run(BOT_TOKEN)

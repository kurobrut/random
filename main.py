import discord
from discord.ext import commands
import requests
import urllib3
import os
import random
import json
import time
from datetime import datetime, timezone
import asyncio

# Disable SSL warnings for Roblox API requests
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ===== LOAD SECRETS FROM ENV =====
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
COOKIE = os.environ.get("COOKIE")

if not BOT_TOKEN or not CHANNEL_ID:
    raise RuntimeError("Missing BOT_TOKEN or CHANNEL_ID environment variable!")

CHANNEL_ID = int(CHANNEL_ID)
HEADERS = {"Cookie": f".ROBLOSECURITY={COOKIE}", "Content-Type": "application/json"} if COOKIE else {}

# ===== TARGET USERS =====
target_users = {
    "kei_lanii44": 2030589429,
    "karofr2": 4383142978,
    "jared_0834": 1914569614,
    "lowbattery222": 594387194,
    "Kyro_soon": 3194557030,
    "Jack_TheKiller2344": 3129462659,
    "jeydiiii02": 9559990534
}

# ===== COLORS =====
COLORS = {
    "playing": 0x77dd77,
    "online": 0x89CFF0,
    "offline": 0xFF6961,
    "same_server": 0x9b59b6
}

# ===== PLACE CACHE =====
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

def save_cache():
    with open(CACHE_FILE, "w") as f:
        json.dump(place_cache, f, indent=2)

# ===== SAFE REQUEST WITH RETRIES =====
def safe_request(method, url, **kwargs):
    retries = 0
    base_delay = 1
    max_delay = 60
    while True:
        try:
            r = requests.request(method, url, verify=False, timeout=10, **kwargs)
            if r.status_code == 429 or r.headers.get("x-ratelimit-remaining") == "0":
                reset_time = int(r.headers.get("x-ratelimit-reset", 2))
                wait = reset_time + random.uniform(0.5, 1.5)
                print(f"[Rate Limit] Waiting {wait:.1f}s... ({url})")
                time.sleep(wait)
                continue
            if 500 <= r.status_code < 600:
                wait = min(base_delay * (2 ** retries), max_delay) + random.uniform(0, 1)
                print(f"[Server Error {r.status_code}] Retrying in {wait:.1f}s... ({url})")
                retries += 1
                time.sleep(wait)
                continue
            return r
        except requests.RequestException as e:
            wait = min(base_delay * (2 ** retries), max_delay) + random.uniform(0, 1)
            print(f"[Request Error] {e} - Retrying in {wait:.1f}s... ({url})")
            retries += 1
            time.sleep(wait)

# ===== USERNAME LOOKUP =====
def get_username(user_id):
    url = f"https://users.roblox.com/v1/users/{user_id}"
    res = safe_request("GET", url, headers=HEADERS)
    if res and res.status_code == 200:
        return res.json().get("name", f"User_{user_id}")
    return f"User_{user_id}"

# ===== GAME INFO =====
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
                "universeId": info.get("universeId"),
                "creatorName": info.get("creatorName"),
                "creatorType": info.get("creatorType"),
                "url": game_url,
                "lastSeen": datetime.now(timezone.utc).isoformat()
            }
            save_cache()
            return game_name, game_url

    place_cache[place_id] = {
        "placeId": place_id,
        "placeName": "Unknown Game",
        "universeId": None,
        "creatorName": None,
        "creatorType": None,
        "url": f"https://www.roblox.com/games/{place_id}",
        "lastSeen": datetime.now(timezone.utc).isoformat()
    }
    save_cache()
    return "Unknown Game", f"https://www.roblox.com/games/{place_id}"

# ===== DISCORD BOT =====
intents = discord.Intents.default()
intents.messages = True
bot = commands.Bot(command_prefix="!", intents=intents)

previous_data = {}

async def send_embed(channel, title, description, color, mention_everyone=False):
    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
    if mention_everyone:
        await channel.send(content="@everyone", embed=embed)
    else:
        await channel.send(embed=embed)

async def check_players():
    global previous_data

    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)

    while not bot.is_closed():
        id_list = list(target_users.values())
        if not id_list:
            await asyncio.sleep(20)
            continue

        res = safe_request(
            "POST",
            "https://presence.roblox.com/v1/presence/users",
            json={"userIds": id_list},
            headers=HEADERS
        )
        if not res or res.status_code != 200:
            await asyncio.sleep(20)
            continue

        response = res.json()
        users_in_games = {}
        presences = {}
        place_ids = {}

        for presence in response.get("userPresences", []):
            uid = presence["userId"]
            friendly_name = next((name for name, id_ in target_users.items() if id_ == uid), f"User_{uid}")
            username = get_username(uid)
            presence_type = presence.get("userPresenceType")
            game_id = presence.get("gameId")
            place_id = presence.get("placeId")

            status = ""
            color = COLORS["offline"]

            if presence_type in [2, 3]:
                game_name, game_url = get_game_name_from_place(place_id) if place_id else ("Unknown Game", "")
                status = f"🎮 {username} is playing: {game_name}\n🔗 {game_url}"
                color = COLORS["playing"]
                if game_id:
                    users_in_games[friendly_name] = game_id
                    place_ids[friendly_name] = place_id
            elif presence_type == 1:
                status = f"🟢 {username} is online (not in game)"
                color = COLORS["online"]
            elif presence_type == 0:
                status = f"🔴 {username} is offline"
                color = COLORS["offline"]

            presences[friendly_name] = (status, presence_type, color)

        # === Same server check for kei_lanii44 ===
        main_user = "kei_lanii44"
        if main_user in users_in_games:
            same_server_players = {
                other for other, gid in users_in_games.items()
                if other != main_user and gid == users_in_games[main_user]
            }

            if same_server_players:
                game_name, game_url = get_game_name_from_place(place_ids[main_user]) if main_user in place_ids else ("Unknown Game", "")
                players_signature = tuple(sorted(same_server_players))
                if previous_data.get("same_server") != players_signature:
                    others_list = ", ".join(same_server_players)
                    description = f"{main_user} is in the same server with:\n👥 {others_list}\n\n🎮 Game: {game_name}\n🔗 {game_url}"
                    await send_embed(channel, "🎯 Target Match", description, COLORS["same_server"], mention_everyone=True)
                    previous_data["same_server"] = players_signature

                presences.pop(main_user, None)
            else:
                previous_data["same_server"] = None

        # --- Send normal presence updates ---
        for friendly_name, (status, presence_type, color) in presences.items():
            if previous_data.get(friendly_name) != status:
                mention = (friendly_name == "kei_lanii44" and presence_type in [2, 3])
                await send_embed(channel, "Presence Update", status, color, mention_everyone=mention)
                previous_data[friendly_name] = status

        # Wait once per loop
        await asyncio.sleep(20)

@bot.event
async def on_ready():
    print(f"[Bot] Logged in as {bot.user}")
    bot.loop.create_task(check_players())

bot.run(BOT_TOKEN)

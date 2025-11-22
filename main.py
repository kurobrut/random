import discord
from discord.ext import tasks
import aiohttp
import asyncio
import json
import os
import random
from datetime import datetime, timezone, timedelta

# ===== CONFIG =====
with open("config.json") as f:
    config = json.load(f)

target_users = config["target_users"]
cookie = config.get("cookie")
renew_url = config.get("renew_url")

HEADERS = {
    "Cookie": f".ROBLOSECURITY={cookie}",
    "Content-Type": "application/json"
} if cookie else {}

COLORS = {
    "playing": 0x77dd77,
    "online": 0x89CFF0,
    "offline": 0xFF6961,
    "same_server": 0x9b59b6,
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

def save_cache():
    with open(CACHE_FILE, "w") as f:
        json.dump(place_cache, f, indent=2)

previous_data = {}

USERNAME_CACHE_FILE = "username_cache.json"
if os.path.exists(USERNAME_CACHE_FILE):
    try:
        with open(USERNAME_CACHE_FILE, "r") as f:
            username_cache = json.load(f)
        if not isinstance(username_cache, dict):
            username_cache = {}
    except Exception:
        username_cache = {}
else:
    username_cache = {}

def save_username_cache():
    with open(USERNAME_CACHE_FILE, "w") as f:
        json.dump(username_cache, f, indent=2)

# ===== DISCORD BOT =====
bot_token = "YOUR_BOT_TOKEN"
channel_id = YOUR_CHANNEL_ID  # numeric ID

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Bot(intents=intents)

async def send_embed(title, description, color, mention_everyone=False):
    channel = bot.get_channel(channel_id)
    if not channel:
        print("Channel not found!")
        return
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    content = "@everyone" if mention_everyone else ""
    await channel.send(content=content, embed=embed)

# ===== ASYNC HTTP REQUESTS =====
async def safe_request(session, method, url, **kwargs):
    retries = 0
    base_delay = 1
    max_delay = 60
    while True:
        try:
            async with session.request(method, url, **kwargs) as r:
                if r.status == 429:
                    retry_after = float((await r.json()).get("retry_after", 1))
                    print(f"[Rate Limit] Waiting {retry_after}s for {url}")
                    await asyncio.sleep(retry_after)
                    continue
                if 500 <= r.status < 600:
                    wait = min(base_delay * (2 ** retries), max_delay) + random.uniform(0, 1)
                    print(f"[Server Error {r.status}] Retrying in {wait:.1f}s ({url})")
                    await asyncio.sleep(wait)
                    retries += 1
                    continue
                return await r.json()
        except Exception as e:
            wait = min(base_delay * (2 ** retries), max_delay) + random.uniform(0, 1)
            print(f"[Request Error] {e} - Retrying in {wait:.1f}s ({url})")
            await asyncio.sleep(wait)
            retries += 1

async def get_username(session, user_id):
    user_id = str(user_id)
    if user_id in username_cache:
        return username_cache[user_id]

    url = f"https://users.roblox.com/v1/users/{user_id}"
    data = await safe_request(session, "GET", url, headers=HEADERS)
    username = data.get("name", f"User_{user_id}") if data else f"User_{user_id}"
    username_cache[user_id] = username
    save_username_cache()
    return username

async def get_game_name_from_place(session, place_id):
    place_id = str(place_id)
    if place_id in place_cache and place_cache[place_id]["placeName"] != "Unknown Game":
        return place_cache[place_id]["placeName"], place_cache[place_id]["url"]

    url = f"https://games.roblox.com/v1/games/multiget-place-details?placeIds={place_id}"
    data = await safe_request(session, "GET", url, headers=HEADERS)
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

# ===== PRESENCE CHECK LOOP =====
async def check_players_loop():
    await bot.wait_until_ready()
    global previous_data
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                id_list = list(target_users.values())
                if not id_list:
                    await asyncio.sleep(20)
                    continue

                data = await safe_request(session, "POST", "https://presence.roblox.com/v1/presence/users",
                                          headers=HEADERS, json={"userIds": id_list})
                if not data:
                    await asyncio.sleep(20)
                    continue

                users_in_games = {}
                presences = {}
                place_ids = {}

                for presence in data.get("userPresences", []):
                    uid = presence["userId"]
                    friendly_name = next((name for name, id_ in target_users.items() if id_ == uid), f"User_{uid}")
                    username = await get_username(session, uid)
                    presence_type = presence.get("userPresenceType")
                    game_id = presence.get("gameId")
                    place_id = presence.get("placeId")

                    status = ""
                    color = COLORS["offline"]

                    if presence_type in [2, 3]:
                        game_name, game_url = await get_game_name_from_place(session, place_id) if place_id else ("Unknown Game", "")
                        status = f"🎮 {username} is playing: {game_name}\n🔗 {game_url}\n"
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

                a = "kei_lanii44"
                if a in users_in_games:
                    same_server_players = {other for other, gid in users_in_games.items() if other != a and gid == users_in_games[a]}
                    if same_server_players:
                        game_name, game_url = await get_game_name_from_place(session, place_ids[a]) if a in place_ids else ("Unknown Game", "")
                        players_signature = tuple(sorted(same_server_players))
                        if previous_data.get("same_server") != players_signature:
                            others_list = ", ".join(same_server_players)
                            description = f"{a} is in the same server with:\n👥 {others_list}\n\n🎮 Game: {game_name}\n🔗 {game_url}"
                            await send_embed("🎯 Target Match", description, COLORS["same_server"], mention_everyone=True)
                            previous_data["same_server"] = players_signature
                        presences.pop(a, None)
                    else:
                        previous_data["same_server"] = None

                for friendly_name, (status, presence_type, color) in presences.items():
                    if previous_data.get(friendly_name) != status:
                        mention = (friendly_name == "kei_lanii44" and presence_type in [2, 3])
                        await send_embed("Presence Update", status, color, mention_everyone=mention)
                        previous_data[friendly_name] = status

            except Exception as e:
                print(f"[Error] {e}")

            await asyncio.sleep(20)

# ===== RENEWAL LOOP =====
async def renewal_notifier_loop():
    await bot.wait_until_ready()
    STATE_FILE = "renewal_state.json"
    RENEWAL_INTERVAL_DAYS = 4
    NOTIFY_BEFORE_DAYS = 1
    CHECK_INTERVAL_SECONDS = 3600

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                last_renewal = datetime.fromisoformat(data["last_renewal"])
        except Exception:
            last_renewal = datetime.now()
    else:
        last_renewal = datetime.now()

    next_renewal = last_renewal + timedelta(days=RENEWAL_INTERVAL_DAYS)
    notified = False
    async with aiohttp.ClientSession() as session:
        while True:
            now = datetime.now()
            time_until_renewal = next_renewal - now

            if not notified and time_until_renewal <= timedelta(days=NOTIFY_BEFORE_DAYS):
                description = f"Your renewal will occur in **{time_until_renewal.days + 1} day(s)**!\nNext renewal date: **{next_renewal.strftime('%Y-%m-%d %H:%M:%S')}**"
                await send_embed("⚠️ Renewal Reminder", description, COLORS["renewal"])
                notified = True

            if now >= next_renewal:
                try:
                    async with session.get(renew_url) as r:
                        print(f"[Renewal] Renew request sent ({r.status})")
                except Exception as e:
                    print(f"[Renewal Error] {e}")

                last_renewal = now
                next_renewal = last_renewal + timedelta(days=RENEWAL_INTERVAL_DAYS)
                notified = False
                with open(STATE_FILE, "w") as f:
                    json.dump({"last_renewal": last_renewal.isoformat()}, f, indent=2)
                description = f"Renewal completed successfully.\nNext renewal: **{next_renewal.strftime('%Y-%m-%d %H:%M:%S')}**"
                await send_embed("✅ Renewal Completed", description, COLORS["renewal"])

            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

# ===== START BOT =====
bot.loop.create_task(check_players_loop())
bot.loop.create_task(renewal_notifier_loop())
bot.run(bot_token)

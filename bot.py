import asyncio
import hashlib
import hmac
import json
import os
import random
import time
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "rodasfriendzone-demo-secret")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_PATH = "/telegram/webhook"
WEBAPP_URL = f"{RENDER_EXTERNAL_URL}/"
SELECTION_SECONDS = 30
DRAW_INTERVAL_SECONDS = 5

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing.")
if not RENDER_EXTERNAL_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL is missing on Render.")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


def generate_bingo_card():
    ranges = [range(1, 16), range(16, 31), range(31, 46), range(46, 61), range(61, 76)]
    card = []
    for r in ranges:
        card.append(random.sample(list(r), 5))
    card[2][2] = 0  # FREE
    return card


CARTELA_CARDS = {i: generate_bingo_card() for i in range(1, 101)}


def has_bingo(player):
    m = player["marked"]
    for r in range(5):
        if all(m[r][c] for c in range(5)):
            return True
    for c in range(5):
        if all(m[r][c] for r in range(5)):
            return True
    if all(m[i][i] for i in range(5)):
        return True
    if all(m[i][4 - i] for i in range(5)):
        return True
    return False


class Room:
    def __init__(self):
        self.game_number = 1
        self.state = "selection"
        self.selection_started = time.time()
        self.selection_end = self.selection_started + SELECTION_SECONDS
        self.taken = {}       # cartela -> user id
        self.players = {}     # user id -> player object
        self.drawn = []
        self.remaining = list(range(1, 76))
        self.current_number = None
        self.winner = None
        self.lock = asyncio.Lock()

    def new_selection(self):
        self.state = "selection"
        self.selection_started = time.time()
        self.selection_end = self.selection_started + SELECTION_SECONDS
        self.taken = {}
        self.players = {}
        self.drawn = []
        self.remaining = list(range(1, 76))
        self.current_number = None
        self.winner = None


room = Room()


# Demo balances only. Nothing here represents real money.
balances = {}


def demo_balance(user_id):
    return balances.setdefault(user_id, 1000)


def user_id_from_init_data(init_data):
    if not init_data:
        return None
    try:
        parts = dict(item.split("=", 1) for item in init_data.split("&") if "=" in item)
        user_json = parts.get("user")
        if not user_json:
            return None
        return json.loads(user_json).get("id")
    except Exception:
        return None


def validate_init_data(init_data):
    # This project is currently a virtual-balance DEMO. If the page is opened
    # directly in a browser, Telegram initData does not exist, so use demo user 1.
    # Inside Telegram, valid initData identifies the real Telegram user.
    if not init_data:
        return 1
    try:
        from urllib.parse import parse_qsl
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop("hash", None)
        if not received:
            return 1
        data_check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received):
            # Demo mode: keep the Mini App usable if opened outside Telegram.
            return 1
        user_json = pairs.get("user")
        if not user_json:
            return 1
        user_id = json.loads(user_json).get("id")
        return int(user_id) if user_id else 1
    except Exception:
        return 1


def response_payload(user_id):
    now = time.time()
    seconds = max(0, int(room.selection_end - now)) if room.state == "selection" else 0
    my_cartela = room.players.get(user_id, {}).get("cartela")
    cards = []
    if room.state == "game" and user_id in room.players:
        p = room.players[user_id]
        cards = p["card"]
    return {
        "state": room.state,
        "gameNumber": room.game_number,
        "selectionSeconds": seconds,
        "taken": list(room.taken.keys()),
        "myCartela": my_cartela,
        "players": len(room.players),
        "drawn": room.drawn,
        "current": room.current_number,
        "card": cards,
        "marked": room.players.get(user_id, {}).get("marked") if user_id in room.players else None,
        "winner": room.winner,
        "balance": demo_balance(user_id),
    }


async def api(request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_id = validate_init_data(init_data)
    if user_id is None:
        return web.json_response({"error": "Invalid Telegram initData"}, status=401)

    action = request.match_info.get("action")
    async with room.lock:
        if action == "state":
            return web.json_response(response_payload(user_id))

        if action == "select":
            if room.state != "selection":
                return web.json_response({"error": "Game already started."}, status=409)
            data = await request.json()
            cartela = int(data.get("cartela", 0))
            if not 1 <= cartela <= 100:
                return web.json_response({"error": "Choose a Cartela from 1 to 100."}, status=400)
            if user_id in room.players:
                return web.json_response({"error": "You already selected a Cartela."}, status=409)
            if cartela in room.taken:
                return web.json_response({"error": f"Cartela {cartela} is already taken. Choose another one."}, status=409)
            room.taken[cartela] = user_id
            card = [row[:] for row in CARTELA_CARDS[cartela]]
            marked = [[False] * 5 for _ in range(5)]
            marked[2][2] = True
            room.players[user_id] = {"cartela": cartela, "card": card, "marked": marked}
            return web.json_response(response_payload(user_id))

        if action == "leave":
            if room.state == "selection" and user_id in room.players:
                cartela = room.players[user_id]["cartela"]
                room.taken.pop(cartela, None)
                room.players.pop(user_id, None)
            return web.json_response(response_payload(user_id))

        return web.json_response({"error": "Unknown action"}, status=404)


async def finish_selection():
    async with room.lock:
        if room.state != "selection":
            return
        if not room.players:
            room.selection_started = time.time()
            room.selection_end = room.selection_started + SELECTION_SECONDS
            return
        room.state = "game"
        room.drawn = []
        room.remaining = list(range(1, 76))
        room.current_number = None
        room.winner = None


async def draw_loop():
    while True:
        await asyncio.sleep(DRAW_INTERVAL_SECONDS)
        async with room.lock:
            if room.state != "game":
                continue
            if room.winner:
                continue
            if not room.remaining:
                room.winner = {"type": "none", "cartela": None, "userId": None}
                room.state = "ended"
                continue
            number = random.choice(room.remaining)
            room.remaining.remove(number)
            room.drawn.append(number)
            room.current_number = number
            for p in room.players.values():
                for r in range(5):
                    for c in range(5):
                        if p["card"][r][c] == number:
                            p["marked"][r][c] = True
            for uid, p in room.players.items():
                if has_bingo(p):
                    room.winner = {"type": "winner", "cartela": p["cartela"], "userId": uid}
                    room.state = "ended"
                    break


async def game_loop():
    while True:
        await asyncio.sleep(1)
        async with room.lock:
            if room.state == "selection" and time.time() >= room.selection_end:
                pass
            elif room.state == "ended":
                room.game_number += 1
                room.new_selection()
                continue
            else:
                continue
        await finish_selection()


@dp.message(CommandStart())
async def start(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎱 Open Bingo", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await message.answer(
        "🎱 RodasFriendZone Bingo\n\nOpen the Mini App to choose your Cartela and play.\n\nDemo balance: 1,000 credits.",
        reply_markup=keyboard,
    )


async def health(request):
    return web.json_response({"ok": True, "game": room.game_number, "state": room.state})


async def on_startup(app):
    await bot.set_webhook(f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}", secret_token=WEBHOOK_SECRET)
    app["draw_task"] = asyncio.create_task(draw_loop())
    app["game_task"] = asyncio.create_task(game_loop())


async def on_cleanup(app):
    for key in ("draw_task", "game_task"):
        task = app.get(key)
        if task:
            task.cancel()
    await bot.delete_webhook()
    await bot.session.close()


async def telegram_webhook(request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return web.Response(status=403, text="Forbidden")
    update = types.Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return web.Response(text="OK")


app = web.Application()
app.router.add_get("/health", health)
app.router.add_post(WEBHOOK_PATH, telegram_webhook)
app.router.add_get("/api/{action}", api)
app.router.add_post("/api/{action}", api)
app.router.add_static("/", os.path.join(os.path.dirname(__file__), "web"), show_index=True)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)


if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)

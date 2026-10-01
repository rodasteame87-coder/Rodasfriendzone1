import asyncio
import hashlib
import hmac
import json
import os
import random
import time
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from dotenv import load_dotenv

load_dotenv()

# =========================
# CONFIG
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    "rodasfriendzone-demo-secret"
)
RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    ""
).rstrip("/")

PORT = int(os.getenv("PORT", "10000"))

WEBHOOK_PATH = "/telegram/webhook"
WEBAPP_URL = f"{RENDER_EXTERNAL_URL}/"

SELECTION_SECONDS = 30
DRAW_INTERVAL_SECONDS = 5

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing.")

if not RENDER_EXTERNAL_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL is missing on Render.")

# =========================
# TELEGRAM
# =========================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# =========================
# BINGO CARD GENERATION
# =========================

def generate_bingo_card():
    ranges = [
        range(1, 16),    # B
        range(16, 31),   # I
        range(31, 46),   # N
        range(46, 61),   # G
        range(61, 76),   # O
    ]

    card = []

    for number_range in ranges:
        card.append(
            random.sample(list(number_range), 5)
        )

    # FREE center
    card[2][2] = 0

    return card


# Cartelas 1-100
CARTELA_CARDS = {
    i: generate_bingo_card()
    for i in range(1, 101)
}

# =========================
# BINGO CHECK
# =========================

def has_bingo(player):
    marked = player["marked"]

    # Rows
    for row in range(5):
        if all(marked[row][col] for col in range(5)):
            return True

    # Columns
    for col in range(5):
        if all(marked[row][col] for row in range(5)):
            return True

    # Main diagonal
    if all(marked[i][i] for i in range(5)):
        return True

    # Other diagonal
    if all(marked[i][4 - i] for i in range(5)):
        return True

    return False


# =========================
# GAME ROOM
# =========================

class Room:

    def __init__(self):
        self.game_number = 1

        self.state = "selection"

        self.selection_started = time.time()
        self.selection_end = (
            self.selection_started + SELECTION_SECONDS
        )

        # Cartela -> user ID
        self.taken = {}

        # User ID -> player
        self.players = {}

        # Called Bingo numbers
        self.drawn = []

        # Numbers still available
        self.remaining = list(range(1, 76))

        # Latest called number
        self.current_number = None

        # Winner
        self.winner = None

        self.lock = asyncio.Lock()

    def new_selection(self):

        self.state = "selection"

        self.selection_started = time.time()

        self.selection_end = (
            self.selection_started + SELECTION_SECONDS
        )

        self.taken = {}
        self.players = {}

        self.drawn = []

        self.remaining = list(range(1, 76))

        self.current_number = None

        self.winner = None


room = Room()

# =========================
# DEMO BALANCE
# =========================

# Virtual credits only.
# No real-money payment system.

balances = {}


def demo_balance(user_id):
    return balances.setdefault(user_id, 1000)


# =========================
# TELEGRAM USER
# =========================

def validate_init_data(init_data):

    # Direct browser/demo mode
    if not init_data:
        return 1

    try:

        pairs = dict(
            parse_qsl(
                init_data,
                keep_blank_values=True
            )
        )

        received_hash = pairs.pop("hash", None)

        if not received_hash:
            return 1

        data_check_string = "\n".join(
            f"{key}={pairs[key]}"
            for key in sorted(pairs)
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(
            calculated_hash,
            received_hash
        ):
            # Demo mode
            return 1

        user_json = pairs.get("user")

        if not user_json:
            return 1

        user_id = json.loads(user_json).get("id")

        return int(user_id) if user_id else 1

    except Exception:

        # Keep demo Mini App usable
        return 1


# =========================
# API RESPONSE
# =========================

def response_payload(user_id):

    now = time.time()

    if room.state == "selection":
        seconds = max(
            0,
            int(room.selection_end - now)
        )
    else:
        seconds = 0

    my_player = room.players.get(user_id)

    my_cartela = (
        my_player.get("cartela")
        if my_player
        else None
    )

    card = (
        my_player["card"]
        if my_player and room.state in ("game", "ended")
        else None
    )

    marked = (
        my_player["marked"]
        if my_player and room.state in ("game", "ended")
        else None
    )

    return {
        "state": room.state,
        "gameNumber": room.game_number,
        "selectionSeconds": seconds,
        "taken": list(room.taken.keys()),
        "myCartela": my_cartela,
        "players": len(room.players),
        "drawn": room.drawn,
        "current": room.current_number,
        "card": card,
        "marked": marked,
        "winner": room.winner,
        "balance": demo_balance(user_id),
    }


# =========================
# API
# =========================

async def api(request):

    init_data = request.headers.get(
        "X-Telegram-Init-Data",
        ""
    )

    user_id = validate_init_data(init_data)

    action = request.match_info.get("action")

    async with room.lock:

        # ---------------------
        # STATE
        # ---------------------

        if action == "state":

            return web.json_response(
                response_payload(user_id)
            )

        # ---------------------
        # SELECT CARTELA
        # ---------------------

        if action == "select":

            if room.state != "selection":

                return web.json_response(
                    {
                        "error": "Game already started."
                    },
                    status=409
                )

            try:
                data = await request.json()
            except Exception:

                return web.json_response(
                    {
                        "error": "Invalid request."
                    },
                    status=400
                )

            try:
                cartela = int(
                    data.get("cartela", 0)
                )
            except Exception:
                cartela = 0

            if not 1 <= cartela <= 100:

                return web.json_response(
                    {
                        "error":
                        "Choose a Cartela from 1 to 100."
                    },
                    status=400
                )

            # One Cartela per player
            if user_id in room.players:

                return web.json_response(
                    {
                        "error":
                        "You already selected a Cartela."
                    },
                    status=409
                )

            # Already taken
            if cartela in room.taken:

                return web.json_response(
                    {
                        "error":
                        f"Cartela {cartela} is already taken. "
                        "Choose another one."
                    },
                    status=409
                )

            room.taken[cartela] = user_id

            card = [
                row[:]
                for row in CARTELA_CARDS[cartela]
            ]

            marked = [
                [False] * 5
                for _ in range(5)
            ]

            # FREE space
            marked[2][2] = True

            room.players[user_id] = {
                "cartela": cartela,
                "card": card,
                "marked": marked,
            }

            return web.json_response(
                response_payload(user_id)
            )

        # ---------------------
        # LEAVE CARTELA
        # ---------------------

        if action == "leave":

            if (
                room.state == "selection"
                and user_id in room.players
            ):

                cartela = room.players[user_id]["cartela"]

                room.taken.pop(
                    cartela,
                    None
                )

                room.players.pop(
                    user_id,
                    None
                )

            return web.json_response(
                response_payload(user_id)
            )

        return web.json_response(
            {
                "error": "Unknown action."
            },
            status=404
        )


# =========================
# FINISH CARTELA SELECTION
# =========================

async def finish_selection():

    async with room.lock:

        if room.state != "selection":
            return

        # Nobody joined:
        # start another 30-second selection.
        if not room.players:

            room.selection_started = time.time()

            room.selection_end = (
                room.selection_started
                + SELECTION_SECONDS
            )

            return

        # Start Bingo game
        room.state = "game"

        room.drawn = []

        room.remaining = list(
            range(1, 76)
        )

        room.current_number = None

        room.winner = None


# =========================
# BINGO DRAW LOOP
# =========================

async def draw_loop():

    while True:

        await asyncio.sleep(
            DRAW_INTERVAL_SECONDS
        )

        async with room.lock:

            if room.state != "game":
                continue

            if room.winner:
                continue

            if not room.remaining:

                room.winner = {
                    "type": "none",
                    "cartela": None,
                    "userId": None,
                }

                room.state = "ended"

                continue

            # Draw random number
            number = random.choice(
                room.remaining
            )

            room.remaining.remove(number)

            room.drawn.append(number)

            room.current_number = number

            # Mark every player's card
            for player in room.players.values():

                for row in range(5):

                    for col in range(5):

                        if (
                            player["card"][row][col]
                            == number
                        ):

                            player["marked"][row][col] = True

            # Check winners
            for user_id, player in room.players.items():

                if has_bingo(player):

                    room.winner = {
                        "type": "winner",
                        "cartela": player["cartela"],
                        "userId": user_id,
                    }

                    room.state = "ended"

                    break


# =========================
# GAME LOOP
# =========================

async def game_loop():

    while True:

        await asyncio.sleep(1)

        should_finish_selection = False

        async with room.lock:

            # Selection timer finished
            if (
                room.state == "selection"
                and time.time()
                >= room.selection_end
            ):

                should_finish_selection = True

            # Game ended
            elif room.state == "ended":

                room.game_number += 1

                room.new_selection()

                continue

            else:
                continue

        if should_finish_selection:

            await finish_selection()


# =========================
# TELEGRAM /START
# =========================

@dp.message(CommandStart())
async def start(message: types.Message):

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎱 Open Bingo",
                    web_app=WebAppInfo(
                        url=WEBAPP_URL
                    )
                )
            ]
        ]
    )

    await message.answer(
        "🎱 RodasFriendZone Bingo\n\n"
        "Open the Mini App to choose your Cartela "
        "and play.\n\n"
        "Demo balance: 1,000 credits.",
        reply_markup=keyboard,
    )


# =========================
# HEALTH
# =========================

async def health(request):

    return web.json_response(
        {
            "ok": True,
            "game": room.game_number,
            "state": room.state,
        }
    )


# =========================
# IMPORTANT:
# SERVE THE MINI APP
# =========================

async def index(request):

    index_path = os.path.join(
        os.path.dirname(__file__),
        "web",
        "index.html"
    )

    if not os.path.isfile(index_path):

        return web.Response(
            status=500,
            text=(
                "Mini App error: "
                "web/index.html was not found."
            )
        )

    return web.FileResponse(
        index_path
    )


# =========================
# STARTUP
# =========================

async def on_startup(app):

    webhook_url = (
        f"{RENDER_EXTERNAL_URL}"
        f"{WEBHOOK_PATH}"
    )

    await bot.set_webhook(
        webhook_url,
        secret_token=WEBHOOK_SECRET
    )

    app["draw_task"] = asyncio.create_task(
        draw_loop()
    )

    app["game_task"] = asyncio.create_task(
        game_loop()
    )

    print(
        "RodasFriendZone started."
    )

    print(
        f"Mini App: {WEBAPP_URL}"
    )

    print(
        f"Webhook: {webhook_url}"
    )


# =========================
# CLEANUP
# =========================

async def on_cleanup(app):

    for key in (
        "draw_task",
        "game_task"
    ):

        task = app.get(key)

        if task:

            task.cancel()

            try:
                await task
            except asyncio.CancelledError:
                pass

    await bot.delete_webhook()

    await bot.session.close()


# =========================
# TELEGRAM WEBHOOK
# =========================

async def telegram_webhook(request):

    secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    )

    if secret != WEBHOOK_SECRET:

        return web.Response(
            status=403,
            text="Forbidden"
        )

    try:

        update_data = await request.json()

        update = types.Update.model_validate(
            update_data,
            context={"bot": bot}
        )

        await dp.feed_update(
            bot,
            update
        )

        return web.Response(
            text="OK"
        )

    except Exception as error:

        print(
            f"Webhook error: {error}"
        )

        return web.Response(
            status=500,
            text="Webhook error"
        )


# =========================
# AIOHTTP APPLICATION
# =========================

app = web.Application()

# Health
app.router.add_get(
    "/health",
    health
)

# Telegram webhook
app.router.add_post(
    WEBHOOK_PATH,
    telegram_webhook
)

# Mini App API
app.router.add_get(
    "/api/{action}",
    api
)

app.router.add_post(
    "/api/{action}",
    api
)

# IMPORTANT:
# Explicitly serve web/index.html
app.router.add_get(
    "/",
    index
)

# Serve other files inside /web if needed
app.router.add_static(
    "/static/",
    os.path.join(
        os.path.dirname(__file__),
        "web"
    )
)

app.on_startup.append(
    on_startup
)

app.on_cleanup.append(
    on_cleanup
)


# =========================
# RUN
# =========================

if __name__ == "__main__":

    web.run_app(
        app,
        host="0.0.0.0",
        port=PORT
    )

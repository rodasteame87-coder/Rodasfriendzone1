import os
import asyncio
import logging
import random
import time
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://rodasfriendzone.onrender.com"
).rstrip("/")

WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    "rodasfriendzone-demo-secret"
)

WEBHOOK_PATH = "/telegram/webhook"

SELECTION_SECONDS = 30
DRAW_INTERVAL_SECONDS = 5

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing.")

# =========================================================
# PATHS
# =========================================================

BASE_DIR = Path(__file__).resolve().parent

# Supports both "web" and "Web"
WEB_DIR = None

for folder_name in ["web", "Web"]:
    possible = BASE_DIR / folder_name
    if possible.is_dir():
        WEB_DIR = possible
        break

if WEB_DIR is None:
    WEB_DIR = BASE_DIR / "web"

INDEX_FILE = WEB_DIR / "index.html"

logger.info("BASE_DIR: %s", BASE_DIR)
logger.info("WEB_DIR: %s", WEB_DIR)
logger.info("INDEX_FILE: %s", INDEX_FILE)
logger.info("INDEX EXISTS: %s", INDEX_FILE.exists())

# =========================================================
# BOT
# =========================================================

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# =========================================================
# BINGO CARD
# =========================================================

def generate_bingo_card():
    columns = [
        random.sample(range(1, 16), 5),
        random.sample(range(16, 31), 5),
        random.sample(range(31, 46), 5),
        random.sample(range(46, 61), 5),
        random.sample(range(61, 76), 5),
    ]

    card = []

    for row in range(5):
        current_row = []

        for col in range(5):
            if col == 2 and row == 2:
                current_row.append("FREE")
            else:
                current_row.append(columns[col][row])

        card.append(current_row)

    return card


CARTELA_CARDS = {
    number: generate_bingo_card()
    for number in range(1, 101)
}

# =========================================================
# BINGO CHECK
# =========================================================

def has_bingo(card, marked):
    # Rows
    for row in range(5):
        if all(card[row][col] in marked for col in range(5)):
            return True

    # Columns
    for col in range(5):
        if all(card[row][col] in marked for row in range(5)):
            return True

    # Diagonal
    if all(card[i][i] in marked for i in range(5)):
        return True

    # Other diagonal
    if all(card[i][4 - i] in marked for i in range(5)):
        return True

    return False


# =========================================================
# GAME
# =========================================================

class Game:
    def __init__(self):
        self.game_number = 0

        # selection / playing
        self.phase = "selection"

        self.selection_started = time.time()
        self.selection_ends = time.time() + SELECTION_SECONDS

        self.players = {}
        self.taken_cartelas = {}

        self.drawn_numbers = []
        self.current_number = None

        self.winner = None
        self.finished_at = None

        self.lock = asyncio.Lock()

    def remaining_selection(self):
        if self.phase != "selection":
            return 0

        return max(
            0,
            int(self.selection_ends - time.time())
        )


game = Game()

# =========================================================
# PLAYERS
# =========================================================

# Virtual demo balance only
balances = {}

def get_balance(user_id):
    if user_id not in balances:
        balances[user_id] = 1000

    return balances[user_id]


def player_id_from_request(request):
    # Mini App sends Telegram user information.
    # For demo purposes we also allow a simple demo ID.
    user_id = request.headers.get("X-User-ID")

    if user_id:
        return str(user_id)

    return "demo-user"


# =========================================================
# API STATE
# =========================================================

def card_for_player(player):
    cartela = player.get("cartela")

    if not cartela:
        return None

    return CARTELA_CARDS.get(cartela)


def build_state(user_id):
    remaining = game.remaining_selection()

    my_cartela = None

    if user_id in game.players:
        my_cartela = game.players[user_id].get("cartela")

    taken = list(game.taken_cartelas.keys())

    # Convert keys to integers
    taken = [int(x) for x in taken]

    card = card_for_player(
        game.players.get(user_id, {})
    )

    marked = []

    if card:
        for row in card:
            for value in row:
                if value == "FREE":
                    marked.append("FREE")
                elif value in game.drawn_numbers:
                    marked.append(value)

    return {
        "ok": True,

        "phase": game.phase,

        "game": game.game_number,

        "players": len(game.players),

        "wallet": get_balance(user_id),

        "selection_remaining": remaining,

        "taken_cartelas": taken,

        "my_cartela": my_cartela,

        "drawn_numbers": game.drawn_numbers,

        "current_number": game.current_number,

        "winner": game.winner,

        "card": card,

        "marked": marked,
    }


# =========================================================
# START NEW CARTELA SELECTION
# =========================================================

async def start_selection():
    async with game.lock:

        game.phase = "selection"

        game.selection_started = time.time()
        game.selection_ends = (
            time.time() + SELECTION_SECONDS
        )

        game.players = {}
        game.taken_cartelas = {}

        game.drawn_numbers = []
        game.current_number = None

        game.winner = None
        game.finished_at = None

        game.game_number += 1

        logger.info(
            "30-second Cartela selection started. Game %s",
            game.game_number
        )


# =========================================================
# FINISH SELECTION
# =========================================================

async def finish_selection():

    async with game.lock:

        if game.phase != "selection":
            return

        game.phase = "playing"

        logger.info(
            "Cartela selection finished. %s players.",
            len(game.players)
        )

    # If nobody selected a Cartela, start another selection
    if len(game.players) == 0:

        logger.info(
            "No players selected Cartelas."
        )

        await asyncio.sleep(1)
        await start_selection()

# =========================================================
# DRAW LOOP
# =========================================================

async def draw_numbers():

    while True:

        await asyncio.sleep(DRAW_INTERVAL_SECONDS)

        async with game.lock:

            if game.phase != "playing":
                continue

            remaining = [
                n
                for n in range(1, 76)
                if n not in game.drawn_numbers
            ]

            if not remaining:
                logger.info("All Bingo numbers drawn.")
                game.phase = "selection"
                game.selection_started = time.time()
                game.selection_ends = (
                    time.time() + SELECTION_SECONDS
                )
                continue

            number = random.choice(remaining)

            game.drawn_numbers.append(number)
            game.current_number = number

            logger.info(
                "Game %s called number %s",
                game.game_number,
                number
            )

            # Check winners
            for user_id, player in game.players.items():

                cartela = player.get("cartela")

                if not cartela:
                    continue

                card = CARTELA_CARDS[cartela]

                marked = set(game.drawn_numbers)
                marked.add("FREE")

                if has_bingo(card, marked):

                    game.winner = {
                        "user_id": user_id,
                        "cartela": cartela,
                    }

                    game.phase = "finished"
                    game.finished_at = time.time()

                    logger.info(
                        "WINNER! User %s Cartela %s",
                        user_id,
                        cartela
                    )

                    break

        # Wait briefly before changing finished state
        if game.phase == "finished":

            await asyncio.sleep(4)

            await start_selection()


# =========================================================
# GAME LOOP
# =========================================================

async def game_loop():

    await start_selection()

    while True:

        await asyncio.sleep(1)

        if game.phase == "selection":

            if game.remaining_selection() <= 0:
                await finish_selection()

        elif game.phase == "finished":

            await asyncio.sleep(2)

            await start_selection()


# =========================================================
# API: STATE
# =========================================================

async def api_state(request):

    user_id = player_id_from_request(request)

    return web.json_response(
        build_state(user_id)
    )


# =========================================================
# API: SELECT CARTELA
# =========================================================

async def api_select(request):

    user_id = player_id_from_request(request)

    try:
        data = await request.json()
    except Exception:
        data = {}

    cartela = data.get("cartela")

    try:
        cartela = int(cartela)
    except Exception:
        return web.json_response(
            {
                "ok": False,
                "error": "Invalid Cartela."
            },
            status=400
        )

    if cartela < 1 or cartela > 100:

        return web.json_response(
            {
                "ok": False,
                "error": "Cartela must be between 1 and 100."
            },
            status=400
        )

    async with game.lock:

        if game.phase != "selection":

            return web.json_response(
                {
                    "ok": False,
                    "error": "Cartela selection is closed."
                },
                status=400
            )

        if game.remaining_selection() <= 0:

            return web.json_response(
                {
                    "ok": False,
                    "error": "Selection time has ended."
                },
                status=400
            )

        # One Cartela per player
        if user_id in game.players:

            return web.json_response(
                {
                    "ok": False,
                    "error": "You already selected a Cartela."
                },
                status=400
            )

        # Cartela already taken
        if cartela in game.taken_cartelas:

            return web.json_response(
                {
                    "ok": False,
                    "error": (
                        f"Cartela {cartela} is already taken. "
                        "Choose another one."
                    )
                },
                status=400
            )

        game.players[user_id] = {
            "cartela": cartela,
            "joined_at": time.time()
        }

        game.taken_cartelas[cartela] = user_id

        logger.info(
            "User %s selected Cartela %s",
            user_id,
            cartela
        )

        # If all 100 are taken, immediately start game
        if len(game.taken_cartelas) >= 100:

            game.phase = "playing"

            logger.info(
                "All 100 Cartelas taken. Game starts immediately."
            )

    return web.json_response(
        build_state(user_id)
    )


# =========================================================
# API: LEAVE
# =========================================================

async def api_leave(request):

    user_id = player_id_from_request(request)

    async with game.lock:

        if game.phase != "selection":

            return web.json_response(
                {
                    "ok": False,
                    "error": "You cannot leave Cartela selection now."
                },
                status=400
            )

        player = game.players.pop(
            user_id,
            None
        )

        if player:

            cartela = player.get("cartela")

            if cartela in game.taken_cartelas:

                del game.taken_cartelas[cartela]

            logger.info(
                "User %s left. Cartela %s is available again.",
                user_id,
                cartela
            )

    return web.json_response(
        build_state(user_id)
    )


# =========================================================
# MINI APP INDEX
# =========================================================

async def index(request):

    logger.info(
        "GET / received. Looking for: %s",
        INDEX_FILE
    )

    if not INDEX_FILE.exists():

        # Extra diagnostic information
        folders = []

        try:
            folders = [
                item.name
                for item in BASE_DIR.iterdir()
            ]
        except Exception:
            pass

        return web.Response(
            status=500,
            content_type="text/plain",
            text=(
                "RodasFriendZone Mini App ERROR\n\n"
                "index.html was not found.\n\n"
                f"Expected: {INDEX_FILE}\n\n"
                f"Files/folders found: {folders}\n"
            )
        )

    return web.FileResponse(
        path=INDEX_FILE
    )


# =========================================================
# HEALTH
# =========================================================

async def health(request):

    return web.json_response(
        {
            "status": "ok",
            "service": "RodasFriendZone",
            "mini_app": INDEX_FILE.exists(),
            "index_file": str(INDEX_FILE),
            "game_phase": game.phase,
            "game": game.game_number,
        }
    )


# =========================================================
# TELEGRAM /START
# =========================================================

@dp.message(CommandStart())
async def start_command(message: types.Message):

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎯 OPEN RODAS BINGO",
                    web_app=WebAppInfo(
                        url=RENDER_EXTERNAL_URL + "/"
                    )
                )
            ]
        ]
    )

    await message.answer(
        "🎯 Welcome to RodasFriendZone Bingo!\n\n"
        "Choose your Cartela and play Bingo.",
        reply_markup=keyboard
    )


# =========================================================
# TELEGRAM WEBHOOK
# =========================================================

async def telegram_webhook(request):

    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:

        return web.Response(
            status=403,
            text="Forbidden"
        )

    try:
        data = await request.json()

        update = types.Update.model_validate(data)

        await dp.feed_update(
            bot,
            update
        )

        return web.Response(
            text="OK"
        )

    except Exception as e:

        logger.exception(
            "Webhook error: %s",
            e
        )

        return web.Response(
            status=500,
            text="Webhook error"
        )


# =========================================================
# STARTUP
# =========================================================

async def on_startup(app):

    webhook_url = (
        RENDER_EXTERNAL_URL
        + WEBHOOK_PATH
    )

    logger.info(
        "Setting Telegram webhook: %s",
        webhook_url
    )

    try:

        await bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True
        )

        logger.info(
            "Telegram webhook successfully configured."
        )

    except Exception as e:

        logger.exception(
            "Could not set Telegram webhook: %s",
            e
        )

    app["game_task"] = asyncio.create_task(
        game_loop()
    )

    app["draw_task"] = asyncio.create_task(
        draw_numbers()
    )


# =========================================================
# SHUTDOWN
# =========================================================

async def on_shutdown(app):

    logger.info("Shutting down...")

    for key in ["game_task", "draw_task"]:

        task = app.get(key)

        if task:
            task.cancel()

    try:
        await bot.delete_webhook()
    except Exception:
        pass

    await bot.session.close()


# =========================================================
# APP
# =========================================================

app = web.Application()

# Mini App
app.router.add_get("/", index)

# API
app.router.add_get("/api/state", api_state)
app.router.add_post("/api/select", api_select)
app.router.add_post("/api/leave", api_leave)

# Health check
app.router.add_get("/health", health)

# Telegram webhook
app.router.add_post(
    WEBHOOK_PATH,
    telegram_webhook
)

app.on_startup.append(on_startup)
app.on_cleanup.append(on_shutdown)


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    logger.info(
        "RodasFriendZone starting on port %s",
        PORT
    )

    logger.info(
        "Mini App URL: %s/",
        RENDER_EXTERNAL_URL
    )

    logger.info(
        "Index file: %s",
        INDEX_FILE
    )

    web.run_app(
        app,
        host="0.0.0.0",
        port=PORT
    )

import os
import asyncio
import random
import time
import logging
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo


# ============================================================
# SETTINGS
# ============================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("RodasFriendZone")

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

RENDER_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://rodasfriendzone.onrender.com"
).rstrip("/")

WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    "RodasFriendZone_927461_secret"
)

WEBHOOK_PATH = "/telegram/webhook"

SELECTION_SECONDS = 30
DRAW_INTERVAL = 5


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing.")


# ============================================================
# FILE LOCATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

# Works with either web/ or Web/
if (BASE_DIR / "web").is_dir():
    WEB_DIR = BASE_DIR / "web"
elif (BASE_DIR / "Web").is_dir():
    WEB_DIR = BASE_DIR / "Web"
else:
    WEB_DIR = BASE_DIR / "web"

INDEX_FILE = WEB_DIR / "index.html"


# ============================================================
# BOT
# ============================================================

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# ============================================================
# BINGO CARDS
# ============================================================

def make_card():

    b = random.sample(range(1, 16), 5)
    i = random.sample(range(16, 31), 5)
    n = random.sample(range(31, 46), 5)
    g = random.sample(range(46, 61), 5)
    o = random.sample(range(61, 76), 5)

    columns = [b, i, n, g, o]

    card = []

    for row in range(5):

        line = []

        for col in range(5):

            if row == 2 and col == 2:
                line.append("FREE")
            else:
                line.append(columns[col][row])

        card.append(line)

    return card


CARTELA_CARDS = {
    number: make_card()
    for number in range(1, 101)
}


# ============================================================
# BINGO CHECK
# ============================================================

def check_bingo(card, drawn):

    marked = set(drawn)
    marked.add("FREE")

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


# ============================================================
# GAME STATE
# ============================================================

class BingoGame:

    def __init__(self):

        self.game_number = 0

        self.phase = "selection"

        self.selection_started = time.time()

        self.selection_ends = (
            time.time() + SELECTION_SECONDS
        )

        self.players = {}

        self.taken_cartelas = {}

        self.drawn = []

        self.current_number = None

        self.winner = None

        self.lock = asyncio.Lock()


game = BingoGame()


# ============================================================
# DEMO WALLET
# ============================================================

balances = {}


def get_balance(user_id):

    if user_id not in balances:
        balances[user_id] = 1000

    return balances[user_id]


# ============================================================
# USER ID
# ============================================================

def get_user_id(request):

    user_id = request.headers.get("X-User-ID")

    if user_id:
        return str(user_id)

    return "demo-user"


# ============================================================
# TIMER
# ============================================================

def selection_time_left():

    if game.phase != "selection":
        return 0

    return max(
        0,
        int(game.selection_ends - time.time())
    )


# ============================================================
# GAME STATE RESPONSE
# ============================================================

def get_state(user_id):

    player = game.players.get(user_id)

    cartela = None
    card = None
    marked = []

    if player:

        cartela = player["cartela"]

        card = CARTELA_CARDS.get(cartela)

        if card:

            marked = list(game.drawn)

            marked.append("FREE")

    return {

        "ok": True,

        "phase": game.phase,

        "game": game.game_number,

        "players": len(game.players),

        "wallet": get_balance(user_id),

        "selection_remaining": selection_time_left(),

        "taken_cartelas": list(
            game.taken_cartelas.keys()
        ),

        "my_cartela": cartela,

        "card": card,

        "marked": marked,

        "drawn_numbers": game.drawn,

        "current_number": game.current_number,

        "winner": game.winner

    }


# ============================================================
# START SELECTION
# ============================================================

async def start_selection():

    async with game.lock:

        game.phase = "selection"

        game.selection_started = time.time()

        game.selection_ends = (
            time.time() + SELECTION_SECONDS
        )

        game.players.clear()

        game.taken_cartelas.clear()

        game.drawn.clear()

        game.current_number = None

        game.winner = None

        game.game_number += 1

        logger.info(
            "NEW CARTELA SELECTION - GAME %s",
            game.game_number
        )


# ============================================================
# FINISH SELECTION
# ============================================================

async def finish_selection():

    async with game.lock:

        if game.phase != "selection":
            return

        if not game.players:

            logger.info(
                "No players selected Cartelas."
            )

            game.selection_ends = (
                time.time() + SELECTION_SECONDS
            )

            return

        game.phase = "playing"

        logger.info(
            "BINGO GAME STARTED - %s PLAYERS",
            len(game.players)
        )


# ============================================================
# NUMBER DRAWING
# ============================================================

async def draw_loop():

    while True:

        await asyncio.sleep(DRAW_INTERVAL)

        async with game.lock:

            if game.phase != "playing":
                continue

            available = [
                number
                for number in range(1, 76)
                if number not in game.drawn
            ]

            if not available:

                game.phase = "finished"

                continue

            number = random.choice(available)

            game.drawn.append(number)

            game.current_number = number

            logger.info(
                "CALLED NUMBER: %s",
                number
            )

            # Check every player
            for user_id, player in game.players.items():

                cartela = player["cartela"]

                card = CARTELA_CARDS[cartela]

                if check_bingo(card, game.drawn):

                    game.winner = {

                        "user_id": user_id,

                        "cartela": cartela

                    }

                    game.phase = "finished"

                    logger.info(
                        "WINNER: %s / CARTELA %s",
                        user_id,
                        cartela
                    )

                    break


# ============================================================
# GAME LOOP
# ============================================================

async def game_loop():

    await start_selection()

    while True:

        await asyncio.sleep(1)

        if game.phase == "selection":

            if selection_time_left() <= 0:

                await finish_selection()

        elif game.phase == "finished":

            await asyncio.sleep(5)

            await start_selection()


# ============================================================
# API STATE
# ============================================================

async def api_state(request):

    user_id = get_user_id(request)

    return web.json_response(
        get_state(user_id)
    )


# ============================================================
# API SELECT CARTELA
# ============================================================

async def api_select(request):

    user_id = get_user_id(request)

    try:

        data = await request.json()

    except Exception:

        return web.json_response(
            {
                "ok": False,
                "error": "Invalid request."
            },
            status=400
        )

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

        if selection_time_left() <= 0:

            return web.json_response(
                {
                    "ok": False,
                    "error": "Selection time has ended."
                },
                status=400
            )

        if user_id in game.players:

            return web.json_response(
                {
                    "ok": False,
                    "error": "You already selected a Cartela."
                },
                status=400
            )

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

            "joined": time.time()

        }

        game.taken_cartelas[cartela] = user_id

        logger.info(
            "USER %s SELECTED CARTELA %s",
            user_id,
            cartela
        )

        # All 100 selected
        if len(game.taken_cartelas) >= 100:

            game.phase = "playing"

    return web.json_response(
        get_state(user_id)
    )


# ============================================================
# API LEAVE
# ============================================================

async def api_leave(request):

    user_id = get_user_id(request)

    async with game.lock:

        if game.phase != "selection":

            return web.json_response(
                {
                    "ok": False,
                    "error": "You cannot leave now."
                },
                status=400
            )

        player = game.players.pop(
            user_id,
            None
        )

        if player:

            cartela = player["cartela"]

            game.taken_cartelas.pop(
                cartela,
                None
            )

            logger.info(
                "USER %s LEFT CARTELA %s",
                user_id,
                cartela
            )

    return web.json_response(
        get_state(user_id)
    )


# ============================================================
# MINI APP HOME
# ============================================================

async def index(request):

    logger.info(
        "MINI APP REQUEST: %s",
        request.path
    )

    logger.info(
        "INDEX FILE: %s",
        INDEX_FILE
    )

    if not INDEX_FILE.exists():

        return web.Response(

            status=500,

            content_type="text/plain",

            text=(
                "RODASFRIENDZONE MINI APP ERROR\n\n"
                "index.html was not found.\n\n"
                f"Expected location:\n{INDEX_FILE}\n\n"
                f"Web folder:\n{WEB_DIR}\n\n"
                f"Base folder:\n{BASE_DIR}"
            )

        )

    return web.FileResponse(
        INDEX_FILE
    )


# ============================================================
# HEALTH CHECK
# ============================================================

async def health(request):

    return web.json_response({

        "status": "ok",

        "application": "RodasFriendZone",

        "mini_app": INDEX_FILE.exists(),

        "index": str(INDEX_FILE),

        "phase": game.phase,

        "game": game.game_number

    })


# ============================================================
# TELEGRAM START
# ============================================================

@dp.message(CommandStart())
async def start_command(message: types.Message):

    keyboard = InlineKeyboardMarkup(

        inline_keyboard=[

            [

                InlineKeyboardButton(

                    text="🎯 OPEN RODAS BINGO",

                    web_app=WebAppInfo(

                        url=RENDER_URL + "/"

                    )

                )

            ]

        ]

    )

    await message.answer(

        "🎯 Welcome to RodasFriendZone Bingo!\n\n"
        "Select your Cartela and play Bingo.",

        reply_markup=keyboard

    )


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

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

        data = await request.json()

        update = types.Update.model_validate(data)

        await dp.feed_update(
            bot,
            update
        )

        return web.Response(
            text="OK"
        )

    except Exception as error:

        logger.exception(
            "Webhook error: %s",
            error
        )

        return web.Response(
            status=500,
            text="Webhook error"
        )


# ============================================================
# STARTUP
# ============================================================

async def startup(app):

    logger.info(
        "======================================"
    )

    logger.info(
        "RODASFRIENDZONE MINI APP STARTING"
    )

    logger.info(
        "PORT: %s",
        PORT
    )

    logger.info(
        "WEB URL: %s",
        RENDER_URL
    )

    logger.info(
        "INDEX: %s",
        INDEX_FILE
    )

    logger.info(
        "INDEX EXISTS: %s",
        INDEX_FILE.exists()
    )

    logger.info(
        "======================================"
    )

    webhook_url = (
        RENDER_URL +
        WEBHOOK_PATH
    )

    try:

        await bot.set_webhook(

            url=webhook_url,

            secret_token=WEBHOOK_SECRET,

            drop_pending_updates=True

        )

        logger.info(
            "Telegram webhook configured."
        )

    except Exception as error:

        logger.exception(
            "Webhook setup failed: %s",
            error
        )

    app["game_task"] = asyncio.create_task(
        game_loop()
    )

    app["draw_task"] = asyncio.create_task(
        draw_loop()
    )


# ============================================================
# SHUTDOWN
# ============================================================

async def shutdown(app):

    logger.info(
        "RODASFRIENDZONE SHUTTING DOWN"
    )

    for task_name in [
        "game_task",
        "draw_task"
    ]:

        task = app.get(task_name)

        if task:

            task.cancel()

    try:

        await bot.delete_webhook()

    except Exception:

        pass

    await bot.session.close()


# ============================================================
# WEB APP
# ============================================================

app = web.Application()

app.router.add_get(
    "/",
    index
)

app.router.add_get(
    "/api/state",
    api_state
)

app.router.add_post(
    "/api/select",
    api_select
)

app.router.add_post(
    "/api/leave",
    api_leave
)

app.router.add_get(
    "/health",
    health
)

app.router.add_post(
    WEBHOOK_PATH,
    telegram_webhook
)

app.on_startup.append(
    startup
)

app.on_cleanup.append(
    shutdown
)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    web.run_app(

        app,

        host="0.0.0.0",

        port=PORT

    )

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import requests
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from sqlalchemy import select

from app.config import settings
from app.database import (
    async_session,
    get_favorite_ids,
    get_favorites,
    get_next_listing,
    get_or_create_user,
    get_user_filter,
    set_min_price,
    toggle_favorite,
    upsert_listings,
    wipe_session_data,
)
from app.final_parser import fetch_rental_listings
from app.models import Listing

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

router = Router()

_bot: Bot | None = None

SESSION_MINUTES = 30   # session dies after this many minutes of inactivity
PARSE_PAGES = 1        # 1 page per type = ~28-30 ads per session

WELCOME = (
    "👋 Привет! Я ищу жильё на Cian.\n\n"
    "Как это работает:\n"
    "• «🔍 Искать» — иду на Cian за свежими объявлениями\n"
    "• Смотришь их по одной карточке: ⬅️ ❤️ ➡️\n"
    "• Сессия живёт 30 минут; после её конца все объявления стираются,\n"
    "  остаются только ❤️ избранное.\n\n"
    "Кнопки внизу экрана всегда под рукой:\n"
    "🔍 Искать | ❤️ Избранное | 💰 Мин. цена"
)

MINPRICE_HELP = (
    "Отправь минимальную цену числом, например: 20000\n"
    "Или 0 — показывать всё."
)

TYPE_LABEL = {"flat": "Квартира", "room": "Комната"}

BTN_SEARCH = "🔍 Искать"
BTN_FAVORITES = "❤️ Избранное"
BTN_MINPRICE = "💰 Мин. цена"

# simple FIFO image cache: url -> bytes (RAM only, dies with the process)
_image_cache: dict[str, bytes] = {}
_IMAGE_CACHE_MAX = 150


class MinPriceForm(StatesGroup):
    waiting = State()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_listing(l: Listing) -> str:
    kind = TYPE_LABEL.get(l.offer_type, "Объявление")
    if l.offer_type == "flat" and l.rooms == 0:
        kind = "Студия"
    price = f"{l.price:,}".replace(",", " ")
    return f"{kind} — {price} ₽/мес\n📍 {l.address or 'адрес не указан'}\n{l.url}"


def main_menu_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SEARCH)],
            [KeyboardButton(text=BTN_FAVORITES), KeyboardButton(text=BTN_MINPRICE)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие кнопкой ниже",
    )
    return kb


# ------------------------------------------------------- card rendering

def card_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️", callback_data="browse:prev"),
        InlineKeyboardButton(text="❤️", callback_data="browse:like"),
        InlineKeyboardButton(text="➡️", callback_data="browse:next"),
    ]])


def _caption(l: Listing, liked: bool) -> str:
    text = format_listing(l)
    if liked:
        text += "\n\n❤️ в избранном"
    return text


def _download_image(url: str) -> bytes | None:
    """Download on OUR side (with cache) — Cian's CDN blocks Telegram's servers."""
    if url in _image_cache:
        return _image_cache[url]
    try:
        resp = requests.get(
            url,
            timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
                "Referer": "https://www.cian.ru/",
            },
        )
        if resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith("image"):
            data = resp.content
            if len(_image_cache) >= _IMAGE_CACHE_MAX:
                _image_cache.pop(next(iter(_image_cache)))
            _image_cache[url] = data
            return data
    except Exception:
        pass
    return None


async def _fetch_image(listing: Listing) -> bytes | None:
    if not listing.photo_url:
        return None
    return await asyncio.to_thread(_download_image, listing.photo_url)


async def _send_card_new(chat_id: int, l: Listing, liked: bool) -> Message | None:
    text = _caption(l, liked)
    data = await _fetch_image(l)
    if data:
        try:
            return await _bot.send_photo(
                chat_id,
                BufferedInputFile(data, filename=f"{l.cian_id}.jpg"),
                caption=text, reply_markup=card_kb(),
            )
        except TelegramBadRequest:
            pass
    return await _bot.send_message(chat_id, text, reply_markup=card_kb())


async def _replace_card(msg: Message, l: Listing, liked: bool) -> Message:
    """Swap the card inside the existing message when possible."""
    text = _caption(l, liked)
    data = await _fetch_image(l)

    if data and msg.photo:
        try:
            await msg.edit_media(
                InputMediaPhoto(
                    media=BufferedInputFile(data, filename=f"{l.cian_id}.jpg"),
                    caption=text,
                ),
                reply_markup=card_kb(),
            )
            return msg
        except TelegramBadRequest:
            pass

    if not data and not msg.photo:
        try:
            await msg.edit_text(text, reply_markup=card_kb())
            return msg
        except TelegramBadRequest:
            pass

    try:
        await msg.delete()
    except Exception:
        pass
    if data:
        try:
            return await _bot.send_photo(
                msg.chat.id,
                BufferedInputFile(data, filename=f"{l.cian_id}.jpg"),
                caption=text, reply_markup=card_kb(),
            )
        except TelegramBadRequest:
            pass
    return await _bot.send_message(msg.chat.id, text, reply_markup=card_kb())


async def _load_listing(listing_id: int) -> Listing | None:
    async with async_session() as session:
        res = await session.execute(select(Listing).where(Listing.id == listing_id))
        return res.scalars().first()


# ------------------------------------------------------- session / wipe

async def _wipe_except_favorites() -> None:
    async with async_session() as session:
        n = await wipe_session_data(session)
        if n:
            logger.info("Session wipe: removed %d non-favorited listings", n)


async def _session_expired(state: FSMContext) -> bool:
    data = await state.get_data()
    raw = data.get("last_active")
    if raw is None:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return True
    if (_utcnow() - last) > timedelta(minutes=SESSION_MINUTES):
        await state.clear()
        await _wipe_except_favorites()
        return True
    return False


# ------------------------------------------------------- search logic

async def run_search_and_show_first(message: Message, state: FSMContext) -> None:
    """Full search cycle: parse Cian, wipe, upsert, show first card."""
    # close the previous session's card, start clean
    data = await state.get_data()
    old_card = data.get("card_msg_id")
    if old_card:
        try:
            await message.bot.delete_message(message.chat.id, old_card)
        except Exception:
            pass
    await state.clear()

    wait_msg = await message.answer(
        "🔄 Ищу объявления на Cian...\n(10–20 секунд, потерпи)"
    )

    try:
        offers = await asyncio.to_thread(
            fetch_rental_listings, ("flat", "room"), settings.region, PARSE_PAGES
        )
    except Exception:
        logger.exception("Parse failed on search")
        await wait_msg.edit_text(
            "Не удалось получить объявления с Cian 😔 Попробуй ещё раз через пару минут."
        )
        return

    # new session: erase everything except favorited listings
    await _wipe_except_favorites()

    async with async_session() as session:
        await upsert_listings(session, offers)
        user = await get_or_create_user(
            session, message.from_user.id, message.from_user.username
        )
        listing = await get_next_listing(session, user.id)
        if listing is None:
            await wait_msg.edit_text(
                "Ничего не нашлось. Попробуй снизить минимальную цену: 💰 Мин. цена"
            )
            return
        fav_ids = await get_favorite_ids(session, user.id, [listing.id])

    await wait_msg.delete()

    await state.update_data(
        history=[listing.id],
        history_pos=0,
        fav_ids=sorted(fav_ids),
        browse_user_id=user.id,
        last_active=_utcnow().isoformat(),
    )
    msg = await _send_card_new(message.chat.id, listing, listing.id in fav_ids)
    await state.update_data(card_msg_id=msg.message_id if msg else None)


# ------------------------------------------------------- /start + menu buttons

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await message.answer(
        WELCOME,
        reply_markup=main_menu_kb(),
    )


@router.message(F.text == BTN_SEARCH)
async def btn_search(message: Message, state: FSMContext) -> None:
    if await _session_expired(state):
        pass  # just proceed: search starts a fresh session anyway
    await run_search_and_show_first(message, state)


@router.message(F.text == BTN_FAVORITES)
async def btn_favorites(message: Message, state: FSMContext) -> None:
    if await _session_expired(state):
        pass
    # don't swallow input while user is typing min price
    if await state.get_state() == MinPriceForm.waiting:
        await message.answer("Сначала введи цену числом (или 0).")
        return
    async with async_session() as session:
        user = await get_or_create_user(
            session, message.from_user.id, message.from_user.username
        )
        listings = await get_favorites(session, user.id, limit=20)

    if not listings:
        await message.answer("Пока пусто. Жми 🔍 Искать и ❤️ под карточками.")
        return

    lines = [f"{i}. {format_listing(l)}" for i, l in enumerate(listings, 1)]
    await message.answer("❤️ Твоё избранное:\n\n" + "\n\n".join(lines))


@router.message(F.text == BTN_MINPRICE)
async def btn_minprice(message: Message, state: FSMContext) -> None:
    async with async_session() as session:
        user = await get_or_create_user(
            session, message.from_user.id, message.from_user.username
        )
        f = await get_user_filter(session, user.id)
    current = f.min_price if (f and f.min_price is not None) else 0
    await state.set_state(MinPriceForm.waiting)
    await message.answer(
        f"Сейчас минимальная цена: {current} ₽\n\n" + MINPRICE_HELP
    )


# ------------------------------------------------------- browsing

@router.callback_query(F.data.startswith("browse:"))
async def browse_callback(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if await _session_expired(state):
        await callback.answer("Сессия завершена — жми 🔍 Искать", show_alert=True)
        return

    data = await state.get_data()
    history: list[int] = list(data.get("history") or [])
    pos: int = data.get("history_pos", 0)
    fav_ids: set[int] = set(data.get("fav_ids") or [])
    user_id: int | None = data.get("browse_user_id")

    if not history or user_id is None:
        await callback.answer("Сессия устарела — жми 🔍 Искать", show_alert=True)
        return

    await state.update_data(last_active=_utcnow().isoformat())
    toast = ""
    new_pos = pos

    if action == "info":
        await callback.answer(f"Карточка #{pos + 1} сессии")
        return

    if action == "prev":
        if pos == 0:
            await callback.answer("Это первое объявление сессии")
            return
        new_pos = pos - 1

    elif action == "like":
        current_id = history[pos]
        async with async_session() as session:
            now_liked = await toggle_favorite(session, user_id, current_id)
        if now_liked:
            fav_ids.add(current_id)
            toast = "Сохранено ❤️"
        else:
            fav_ids.discard(current_id)
            toast = "Убрано из избранного"
        await state.update_data(fav_ids=sorted(fav_ids))
        listing = await _load_listing(current_id)
        if listing:
            new_msg = await _replace_card(callback.message, listing,
                                          current_id in fav_ids)
            await state.update_data(card_msg_id=new_msg.message_id)
        await callback.answer(toast)
        return

    elif action == "next":
        if pos < len(history) - 1:
            # replay forward through already-seen cards of this session
            new_pos = pos + 1
        else:
            async with async_session() as session:
                listing = await get_next_listing(session, user_id,
                                                 exclude_ids=history)
            if listing is None:
                await callback.answer(
                    "Объявления этой сессии закончились 🎉 Жми 🔍 Искать"
                )
                return
            history.append(listing.id)
            new_pos = len(history) - 1
            await state.update_data(history=history)
    else:
        await callback.answer("Кнопка устарела — жми 🔍 Искать")
        return

    await state.update_data(history_pos=new_pos)
    listing = await _load_listing(history[new_pos])
    if listing is None:
        await callback.answer("Объявление больше не доступно")
        return

    new_msg = await _replace_card(callback.message, listing,
                                  history[new_pos] in fav_ids)
    await state.update_data(card_msg_id=new_msg.message_id)
    await callback.answer(toast or None)


# ------------------------------------------------------- /favorites command

@router.message(Command("favorites"))
async def cmd_favorites(message: Message, state: FSMContext) -> None:
    await btn_favorites(message, state)


# ------------------------------------------------------- /minprice command

@router.message(Command("minprice"))
async def cmd_minprice(message: Message, state: FSMContext) -> None:
    await btn_minprice(message, state)


# ------------------------------------------------------- min price input

@router.message(MinPriceForm.waiting, F.text)
async def minprice_input(message: Message, state: FSMContext) -> None:
    # menu buttons still work while waiting for the price
    if message.text in (BTN_SEARCH, BTN_FAVORITES, BTN_MINPRICE):
        if message.text == BTN_SEARCH:
            await state.clear()
            await run_search_and_show_first(message, state)
        elif message.text == BTN_FAVORITES:
            await state.clear()
            await btn_favorites(message, state)
        else:
            await btn_minprice(message, state)
        return

    try:
        value = int(message.text.strip().replace(" ", "").replace("₽", ""))
    except ValueError:
        await message.answer("Нужно число, например: 20000\n\n" + MINPRICE_HELP)
        return

    async with async_session() as session:
        user = await get_or_create_user(
            session, message.from_user.id, message.from_user.username
        )
        await set_min_price(session, user.id, value if value > 0 else None)
    await state.clear()
    await message.answer(
        f"✅ Минимальная цена: {value} ₽. Жми 🔍 Искать — покажу от дешёвых."
    )


async def main() -> None:
    global _bot
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    dp.include_router(router)

    _bot = bot
    logger.info("Bot started (session mode, menu keyboard)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
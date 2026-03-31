"""
Telegram-бот ранкової перевірки складу (inline-кнопки, editMessageReplyMarkup).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiohttp import web

_ENV_DIR = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv

    load_dotenv(_ENV_DIR / ".env")
except ImportError:
    pass

DATA_PATH = _ENV_DIR / "ingredients.json"
with open(DATA_PATH, encoding="utf-8") as f:
    GROUPS: list[dict] = json.load(f)

GROUP_TITLES = [g["title"] for g in GROUPS]
GROUP_ITEMS = [g["items"] for g in GROUPS]


def new_status_matrix() -> list[list[bool]]:
    return [[False] * len(items) for items in GROUP_ITEMS]


@dataclass
class UserSession:
    """Стан перевірки для одного користувача (один процес — одна пам'ять)."""

    status: list[list[bool]] = field(default_factory=new_status_matrix)
    screen: str = "welcome"  # welcome | group | summary | report_missing | report_have
    group_idx: int = 0


sessions: dict[int, UserSession] = {}


def get_session(user_id: int) -> UserSession:
    if user_id not in sessions:
        sessions[user_id] = UserSession()
    return sessions[user_id]


def start_new_check(user_id: int) -> None:
    """Кожна команда /st — нова перевірка з нульовим станом."""
    sessions[user_id] = UserSession()


def item_label(name: str, has: bool) -> str:
    return f"{name} {'✅' if has else '❌'}"


def build_group_keyboard(group_idx: int, status: list[list[bool]]) -> InlineKeyboardMarkup:
    """Дві кнопки в ряд — менше рядків у JSON, швидше editMessageReplyMarkup."""
    rows: list[list[InlineKeyboardButton]] = []
    items = GROUP_ITEMS[group_idx]
    st = status[group_idx]
    row: list[InlineKeyboardButton] = []
    for i, name in enumerate(items):
        row.append(
            InlineKeyboardButton(
                text=item_label(name, st[i]),
                callback_data=f"t:{group_idx}:{i}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:prev"),
            InlineKeyboardButton(text="➡️ Далі", callback_data="nav:next"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_caption(idx: int) -> str:
    return f"📦 <b>{GROUP_TITLES[idx]}</b>\n\nОберіть інгредієнти (натискання перемикає ❌/✅):"


def summary_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Показати відсутні ❌",
                    callback_data="rep:missing",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Показати наявні ✅",
                    callback_data="rep:have",
                )
            ],
        ]
    )


def summary_text() -> str:
    return "✅ <b>Перевірку завершено.</b>\n\nОберіть звіт:"


def format_report(kind: str, status: list[list[bool]]) -> str:
    lines: list[str] = []
    want_has = kind == "have"
    title = "На складі (✅)" if want_has else "Відсутні (❌)"
    lines.append(f"📋 <b>{title}</b>\n")
    empty = True
    for g, title_g in enumerate(GROUP_TITLES):
        names = [
            GROUP_ITEMS[g][i]
            for i, has in enumerate(status[g])
            if has == want_has
        ]
        if not names:
            continue
        empty = False
        lines.append(f"\n<b>{title_g}</b>")
        for n in names:
            lines.append(f"• {n}")
    if empty:
        lines.append("\n<i>Нічого в цій категорії.</i>")
    return "\n".join(lines)


def back_from_report_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="rep:back")],
        ]
    )


router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Привіт!\n\n"
        "Це бот для <b>ранкової перевірки продуктів</b> на складі.\n\n"
        "Щоб почати роботу, надішли команду <b>/st</b>.",
    )


@router.message(Command("st"))
async def cmd_st(message: Message) -> None:
    start_new_check(message.from_user.id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Почати перевірку", callback_data="flow:start")],
        ]
    )
    await message.answer(
        "🌅 <b>Ранкова перевірка продуктів</b>\n\nНатисніть кнопку нижче, щоб почати.",
        reply_markup=kb,
    )


@router.callback_query(F.data == "flow:start")
async def cb_start(query: CallbackQuery) -> None:
    if not query.message:
        await query.answer()
        return
    uid = query.from_user.id
    sess = get_session(uid)
    sess.screen = "group"
    sess.group_idx = 0
    await query.answer()
    await query.message.edit_text(
        group_caption(0),
        reply_markup=build_group_keyboard(0, sess.status),
    )


@router.callback_query(F.data.startswith("t:"))
async def cb_toggle(query: CallbackQuery) -> None:
    if not query.message:
        await query.answer()
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "t":
        await query.answer()
        return
    try:
        g, i = int(parts[1]), int(parts[2])
    except ValueError:
        await query.answer()
        return
    sess = get_session(query.from_user.id)
    if sess.screen != "group" or g != sess.group_idx or g < 0 or g >= len(GROUP_ITEMS):
        await query.answer()
        return
    if i < 0 or i >= len(sess.status[g]):
        await query.answer()
        return
    sess.status[g][i] = not sess.status[g][i]
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=build_group_keyboard(g, sess.status))


@router.callback_query(F.data.in_(("nav:prev", "nav:next")))
async def cb_nav(query: CallbackQuery) -> None:
    if not query.message:
        await query.answer()
        return
    sess = get_session(query.from_user.id)
    if sess.screen != "group":
        await query.answer()
        return

    idx = sess.group_idx

    if query.data == "nav:prev":
        if idx <= 0:
            sess.screen = "welcome"
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Почати перевірку", callback_data="flow:start")],
                ]
            )
            await query.answer()
            await query.message.edit_text(
                "🌅 <b>Ранкова перевірка продуктів</b>\n\nНатисніть кнопку нижче, щоб почати.",
                reply_markup=kb,
            )
            return
        idx -= 1
    else:
        if idx >= len(GROUP_TITLES) - 1:
            sess.screen = "summary"
            await query.answer()
            await query.message.edit_text(
                summary_text(),
                reply_markup=summary_keyboard(),
            )
            return
        idx += 1

    sess.group_idx = idx
    await query.answer()
    await query.message.edit_text(
        group_caption(idx),
        reply_markup=build_group_keyboard(idx, sess.status),
    )


@router.callback_query(F.data.in_(("rep:missing", "rep:have", "rep:back")))
async def cb_report(query: CallbackQuery) -> None:
    if not query.message:
        await query.answer()
        return
    sess = get_session(query.from_user.id)

    if query.data == "rep:back":
        if sess.screen not in ("report_missing", "report_have"):
            await query.answer()
            return
        sess.screen = "summary"
        await query.answer()
        await query.message.edit_text(
            summary_text(),
            reply_markup=summary_keyboard(),
        )
        return

    if sess.screen != "summary":
        await query.answer()
        return

    kind = "missing" if query.data == "rep:missing" else "have"
    sess.screen = "report_missing" if kind == "missing" else "report_have"
    text = format_report(kind, sess.status)
    await query.answer()
    await query.message.edit_text(
        text,
        reply_markup=back_from_report_keyboard(),
    )


async def _health(_request: web.Request) -> web.StreamResponse:
    return web.Response(text="ok")


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Задайте BOT_TOKEN у змінних оточення або у файлі .env")
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    me = await bot.get_me()
    print(f"Бот @{me.username} запущено. Очікування повідомлень… (Ctrl+C — зупинити)")

    runner: web.AppRunner | None = None
    port_str = os.getenv("PORT")
    if port_str:
        health_app = web.Application()
        health_app.router.add_get("/", _health)
        health_app.router.add_get("/health", _health)
        runner = web.AppRunner(health_app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", int(port_str)).start()
        print(f"HTTP для Railway: 0.0.0.0:{port_str} → GET / або /health")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        if runner is not None:
            await runner.cleanup()


if __name__ == "__main__":
    import asyncio

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Нормальна зупинка (Ctrl+C); без повного traceback
        print("\nБота зупинено.")

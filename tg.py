"""tg.py - thin helpers around python-telegram-bot (keyboards, safe sending, access checks)."""
import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from utils import RateLimiter

log = logging.getLogger(__name__)
limiter = RateLimiter()


def conn_of(context):
    return context.application.bot_data["conn"]


def cfg_of(context):
    return context.application.bot_data["cfg"]


def is_admin(context, user_id) -> bool:
    """Server-side admin check. Admin IDs only come from the ADMIN_IDS environment variable."""
    return user_id in cfg_of(context).admin_ids


def btn(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def kb(rows):
    """rows = [[(text, callback_data), ...], ...]"""
    return InlineKeyboardMarkup([[btn(t, d) for t, d in row] for row in rows if row])


async def safe_answer(query, text=None, show_alert=False):
    """
    Acknowledge a button press. Telegram refuses answers to buttons pressed long ago ("Query is too old"), which happens whenever the
    free Render service was asleep. That refusal must NOT stop the action the person asked for, so it is ignored.
    """
    try:
        await query.answer(text=text, show_alert=show_alert)
    except BadRequest as e:
        log.info("Button acknowledgement skipped (%s)", str(e)[:80])
    except TelegramError as e:
        log.warning("Button acknowledgement failed: %s", e)


async def safe_edit(query, text, reply_markup=None):
    """Edit a bot message; ignore 'message is not modified' and vanished messages."""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML",
                                      disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        log.warning("edit failed: %s", e)
        try:
            await query.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")
        except TelegramError:
            pass


async def reply(update, text, reply_markup=None):
    return await update.effective_message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML",
                                                     disable_web_page_preview=True)


async def safe_send(bot, chat_id, text, reply_markup=None) -> str:
    """Send a private message. Returns 'sent', 'blocked' (user never started / blocked the bot) or 'error'."""
    for attempt in range(2):
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup,
                                   disable_web_page_preview=True)
            return "sent"
        except Forbidden:
            return "blocked"
        except RetryAfter as e:
            await asyncio.sleep(min(float(e.retry_after) + 1, 30))
        except BadRequest as e:
            log.warning("send failed (%s): %s", chat_id, e)
            return "blocked" if "chat not found" in str(e).lower() else "error"
        except TelegramError as e:
            log.warning("send failed (%s): %s", chat_id, e)
            return "error"
    return "error"

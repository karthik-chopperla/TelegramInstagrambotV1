"""Telegram bot for sharing Instagram post links and authorized media."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from insto.config import BACKEND_AIOGRAPI, BACKEND_HIKERAPI, Config, load_config
from insto.exceptions import BackendError

_POST_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/[A-Za-z0-9_-]+/?(?:\?[^\s]*)?",
    re.IGNORECASE,
)
_POST_PATH_RE = re.compile(r"/(?:p|reel|reels|tv)/[A-Za-z0-9_-]+/?", re.IGNORECASE)
_PENDING_TTL_SECONDS = 10 * 60
_MAX_PENDING = 1000
_MAX_MEDIA_ITEMS = 10
_MAX_TELEGRAM_FILE_BYTES = 49 * 1024 * 1024
_MAX_TELEGRAM_DOWNLOAD_BYTES = 100 * 1024 * 1024
_BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")


@dataclass(slots=True)
class _PendingDownload:
    url: str
    user_id: int
    chat_id: int
    created_at: float


def extract_instagram_post_url(text: str) -> str | None:
    """Return a canonical Instagram post/Reel URL found in a message."""
    for match in _POST_URL_RE.finditer(text):
        candidate = match.group(0).rstrip(".,);]")
        parsed = urlsplit(candidate)
        if (
            parsed.scheme.lower() != "https"
            or parsed.hostname not in {"instagram.com", "www.instagram.com"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or not _POST_PATH_RE.fullmatch(parsed.path)
        ):
            continue
        return f"https://www.instagram.com{parsed.path.rstrip('/')}/"
    return None


def _read_dotenv_token() -> str | None:
    """Read TELEGRAM_BOT_TOKEN from a .env file in the current directory."""
    dotenv_path = Path.cwd() / ".env"
    if not dotenv_path.is_file():
        return None

    token: str | None = None
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if separator and key.strip() == "TELEGRAM_BOT_TOKEN":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            token = value
    return token


def _validate_backend_config(config: Config) -> None:
    if config.backend == BACKEND_HIKERAPI and not config.hiker_token:
        raise RuntimeError("No HikerAPI token configured. Run `insto setup` first.")
    if config.backend == BACKEND_AIOGRAPI and not (
        config.aiograpi_username and config.aiograpi_password
    ):
        raise RuntimeError("No aiograpi credentials configured. Run `insto setup` first.")


def _ensure_supported_platform() -> None:
    if os.name == "nt":
        raise RuntimeError(
            "The insto backend currently requires Linux or macOS. On Windows, run the bot from WSL."
        )


def _read_windows_clipboard() -> str:
    """Read the Windows clipboard from WSL without echoing its contents."""
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-Clipboard -Raw",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "Could not read the Windows clipboard. Copy the token and try again, "
            "or set TELEGRAM_BOT_TOKEN in the environment."
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            "Could not read the Windows clipboard. Copy the token and try again, "
            "or set TELEGRAM_BOT_TOKEN in the environment."
        )
    token = result.stdout.strip()
    if not _BOT_TOKEN_RE.fullmatch(token):
        raise RuntimeError(
            "The Windows clipboard does not appear to contain a Telegram bot token. "
            "Copy the replacement token from BotFather and try again."
        )
    return token


async def run_bot(token: str) -> None:
    """Start long-polling and serve link-sharing and permission-gated downloads."""
    try:
        from telegram import (
            InlineKeyboardButton,
            InlineKeyboardMarkup,
            Message,
            Update,
        )
        from telegram.error import TelegramError
        from telegram.ext import (
            Application,
            CallbackQueryHandler,
            CommandHandler,
            ContextTypes,
            MessageHandler,
            filters,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Telegram support is not installed. Install with `uv tool install 'insto[telegram]'`."
        ) from exc

    config = load_config()
    _validate_backend_config(config)
    _ensure_supported_platform()

    from insto.cli import _build_backend, _format_error
    from insto.service.runtime import open_runtime

    pending: dict[str, _PendingDownload] = {}
    download_lock = asyncio.Lock()

    async with open_runtime(
        config,
        role="oneshot",
        backend_factory=_build_backend,
    ) as runtime:

        async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            del context
            message = update.effective_message
            if message is not None:
                await message.reply_text(
                    "Send an Instagram post or Reel link. I will share the link first; "
                    "media download is available only after you confirm you own it "
                    "or have permission to download it."
                )

        async def share_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            del context
            message = update.effective_message
            user = update.effective_user
            chat = update.effective_chat
            if message is None or user is None or chat is None:
                return
            url = extract_instagram_post_url(message.text or "")
            if url is None:
                await message.reply_text(
                    "I could not find a supported Instagram post/Reel link. "
                    "Use an https://www.instagram.com/p/… or /reel/… link."
                )
                return

            now = time.monotonic()
            expired = [
                key for key, item in pending.items() if now - item.created_at > _PENDING_TTL_SECONDS
            ]
            for key in expired:
                pending.pop(key, None)
            while len(pending) >= _MAX_PENDING:
                oldest = min(pending, key=lambda key: pending[key].created_at)
                pending.pop(oldest)

            pending_id = uuid4().hex
            pending[pending_id] = _PendingDownload(
                url=url,
                user_id=user.id,
                chat_id=chat.id,
                created_at=now,
            )
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "I own it / have permission",
                            callback_data=f"download:{pending_id}",
                        ),
                        InlineKeyboardButton("Cancel", callback_data=f"cancel:{pending_id}"),
                    ]
                ]
            )
            await message.reply_text(
                f"Instagram link:\n{url}\n\n"
                "Download only if you own this media or have permission to use it.",
                reply_markup=keyboard,
                disable_web_page_preview=False,
            )

        async def cancel_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            del context
            query = update.callback_query
            if query is None:
                return
            pending_id = (query.data or "").partition(":")[2]
            item = pending.get(pending_id)
            message = query.message
            if (
                item is not None
                and isinstance(message, Message)
                and item.user_id == query.from_user.id
                and item.chat_id == message.chat_id
            ):
                pending.pop(pending_id, None)
                await query.answer()
                await query.edit_message_reply_markup(reply_markup=None)
                await message.reply_text("Download cancelled.")
            else:
                await query.answer("This download request has expired.", show_alert=True)

        async def download_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            del context
            query = update.callback_query
            if query is None:
                return
            pending_id = (query.data or "").partition(":")[2]
            item = pending.get(pending_id)
            message = query.message
            if item is None or not isinstance(message, Message):
                await query.answer("This download request has expired.", show_alert=True)
                return
            if item.user_id != query.from_user.id or item.chat_id != message.chat_id:
                await query.answer(
                    "Only the user who shared this link can confirm it.",
                    show_alert=True,
                )
                return
            if time.monotonic() - item.created_at > _PENDING_TTL_SECONDS:
                pending.pop(pending_id, None)
                await query.answer("This download request has expired.", show_alert=True)
                return

            pending.pop(pending_id, None)
            await query.answer()
            await query.edit_message_reply_markup(reply_markup=None)
            status = await message.reply_text("Checking the post and preparing authorized media…")

            try:
                post = await runtime.facade.post_info(item.url)
                if not post.media_urls:
                    raise BackendError("the post has no downloadable media")
                if len(post.media_urls) > _MAX_MEDIA_ITEMS:
                    raise BackendError(
                        f"post contains more than {_MAX_MEDIA_ITEMS} media items; "
                        "download limit reached"
                    )

                with tempfile.TemporaryDirectory(prefix="insto-telegram-") as temp_dir:
                    async with download_lock:
                        runtime.facade.reset_command_budget(_MAX_TELEGRAM_DOWNLOAD_BYTES)
                        paths = await runtime.facade.download_post_media(
                            post,
                            dest_dir=Path(temp_dir),
                        )

                    delivered = 0
                    too_large = 0
                    for path in paths:
                        if path.stat().st_size > _MAX_TELEGRAM_FILE_BYTES:
                            too_large += 1
                            continue
                        with path.open("rb") as media:
                            await message.reply_document(
                                document=media,
                                filename=path.name,
                                caption=item.url if delivered == 0 else None,
                            )
                        delivered += 1

                if delivered == 0:
                    await status.edit_text(
                        "The media exceeds Telegram's upload size limit; no file was sent."
                    )
                else:
                    result = f"Sent {delivered} media file(s)."
                    if too_large:
                        result += f" Skipped {too_large} file(s) over Telegram's upload limit."
                    await status.edit_text(result)
            except BackendError as exc:
                await status.edit_text(f"Could not retrieve this post: {_format_error(exc)}")
            except OSError as exc:
                await status.edit_text(f"Could not read or send the downloaded media: {exc}")
            except Exception as exc:
                await status.edit_text(
                    "The download or Telegram upload failed unexpectedly "
                    f"({type(exc).__name__}). Please try again later."
                )

        async def telegram_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
            del update
            error = context.error
            if error is not None:
                print(
                    f"Telegram update failed ({type(error).__name__}).",
                    file=sys.stderr,
                    flush=True,
                )

        def polling_error(error: TelegramError) -> None:
            print(
                f"Telegram polling failed ({type(error).__name__}).",
                file=sys.stderr,
                flush=True,
            )

        application = Application.builder().token(token).build()
        application.add_handler(CommandHandler("start", start))
        application.add_handler(
            CallbackQueryHandler(download_confirmed, pattern=r"^download:[0-9a-f]{32}$")
        )
        application.add_handler(
            CallbackQueryHandler(cancel_download, pattern=r"^cancel:[0-9a-f]{32}$")
        )
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, share_link))
        application.add_error_handler(telegram_error)

        initialized = False
        started = False
        try:
            await application.initialize()
            initialized = True
            await application.start()
            started = True
            if application.updater is None:
                raise RuntimeError("Telegram polling is unavailable in this application.")
            bot_user = await application.bot.get_me()
            await application.updater.start_polling(error_callback=polling_error)
            print(
                f"Telegram bot @{bot_user.username} is running; waiting for messages.",
                flush=True,
            )
            await asyncio.Event().wait()
        finally:
            try:
                if application.updater is not None and application.updater.running:
                    await application.updater.stop()
            finally:
                try:
                    if started:
                        await application.stop()
                finally:
                    if initialized:
                        await application.shutdown()


def main() -> int:
    """Console-script entry point."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        try:
            token = _read_dotenv_token() or ""
        except OSError as exc:
            print(f"Could not read the .env file ({type(exc).__name__}).", file=sys.stderr)
            return 2
    if token and not _BOT_TOKEN_RE.fullmatch(token):
        print("TELEGRAM_BOT_TOKEN in the environment or .env file is not valid.", file=sys.stderr)
        return 2
    if not _BOT_TOKEN_RE.fullmatch(token):
        try:
            input(
                "Copy your new Telegram token to the Windows clipboard, "
                "then press Enter here (token will not be shown): "
            )
            token = _read_windows_clipboard()
        except (EOFError, KeyboardInterrupt):
            print("Token input cancelled.", file=sys.stderr)
            return 2
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    try:
        asyncio.run(run_bot(token))
    except KeyboardInterrupt:
        return 0
    except RuntimeError as exc:
        print(f"Telegram bot could not start: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Telegram bot failed ({type(exc).__name__}).", file=sys.stderr)
        return 1
    return 0


__all__ = ["extract_instagram_post_url", "main", "run_bot"]

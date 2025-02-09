import logging
import traceback
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import aiohttp as aiohttp
from mongopersistence import MongoPersistence
from telegram import InlineQueryResult, InlineQueryResultVideo, Update
from telegram import Video as TelegramVideo
from telegram.constants import ChatType, MessageEntityType, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ChosenInlineResultHandler,
    ContextTypes,
    Defaults,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from app import commands, constants, settings
from app.context import CallbackContext
from app.database import Reporter
from app.database.connector import MongoDatabase
from app.models.medias import Media, MediaGroup, Video
from app.models.report import Report, ReportPlace, ReportType
from app.parsers import Parser
from app.utils import a, notify, patch
from app.utils.app_patchers.json_logger import env_wrapper

# noinspection PyProtectedMember
from app.utils.i18n import _, _n

logger = logging.getLogger(__name__)


async def _process_video(update: Update, ctx: CallbackContext, media: Video):
    extra_caption = ""
    if media.max_quality_url and media.max_quality_url != media.url:
        extra_caption = _(
            "\n\n\n<i>Original video is larger than <b>20 MB</b>,"
            " and bot can't send it.</i> "
            '<a href="{url}">'
            "This is original link</a>"
        ).format(url=media.max_quality_url)

    media_caption = (media.real_caption(ctx, "") + extra_caption).strip()

    try:
        tg_video = ctx.tg_video_cache.get(media.original_url)
        if tg_video:
            tg_video = TelegramVideo.de_json(tg_video, ctx.bot)

        res = await update.effective_message.reply_video(
            video=(tg_video or media.file_input or media.url),
            caption=media_caption,
            supports_streaming=True,
            width=media.video_width,
            height=media.video_height,
            duration=media.video_duration,
        )
        ctx.tg_video_cache[media.original_url] = res.video.to_dict()
    except BadRequest as e:
        logger.error(
            "Error sending video: %s",
            media.url,
            exc_info=e,
            stack_info=True,
        )
        if update.effective_chat.type == ChatType.PRIVATE:
            logger.info("Sending video as link: %s", media)
            await update.effective_message.reply_text(
                _(
                    "Error sending video: {title}\n"
                    "\n\n"
                    '<a href="{url}">Direct link to video</a>'
                ).format(
                    title=a(media_caption, media.original_url),
                    url=media.url,
                ),
            )
            logger.info("Send video as link: %s", media.url)
        raise e


async def _process_media_group(
    update: Update, _: CallbackContext, media: MediaGroup
):
    i_medias = media.input_medias

    for m in i_medias:
        m.caption = media.caption

    await update.message.reply_media_group(
        media=i_medias,
    )


async def link_parser(update: Update, ctx: CallbackContext):
    """Parse link from the user message."""
    m = update.effective_message
    text = getattr(m, "text", "")
    if m is not None:
        message_links = [
            text[entity.offset : entity.offset + entity.length]
            for entity in m.entities
            if entity.type
            in (MessageEntityType.URL, MessageEntityType.TEXT_LINK)
        ]
    else:
        message_links = []

    async with aiohttp.ClientSession() as session:
        medias: list[Media] = await Parser.parse(session, *message_links)

    for media in medias:
        if isinstance(media, Video):
            _from_location = (
                f" from {media.language_emoji}" if media.language_emoji else ""
            )
            logger.info("Sending video%s: %s", _from_location, media)
            return await _process_video(update, ctx, media)

        if isinstance(media, MediaGroup):
            logger.info("Sending medias from %s", media.original_url)
            return await _process_media_group(update, ctx, media)


def inline_query_description(video: Video) -> str:
    resp = ""
    if video.author:
        if video.extra_description:
            resp += video.extra_description
        else:
            resp += _("by @{author} ").format(author=video.author)
    resp += _("from {m_type}").format(m_type=video.type)
    if video.language:
        resp += f" {video.language_emoji}"
    return resp


async def inline_query_video_from_media(
    medias: list[Media],
    ctx: CallbackContext,
) -> list[InlineQueryResultVideo]:
    def content(media: Video) -> InlineQueryResultVideo:
        c = media.caption

        if not c:
            c = _("{m_type} video").format(m_type=media.type.value)

        return InlineQueryResultVideo(
            id=str(uuid.uuid4()),
            video_url=media.url,
            mime_type=media.mime_type,
            thumbnail_url=media.thumbnail_url or media.url,
            title=c,
            caption=media.real_caption(ctx),
            description=inline_query_description(media),
            video_width=media.video_width,
            video_height=media.video_height,
            video_duration=media.video_duration,
        )

    return [content(media) for media in medias if isinstance(media, Video)]


async def chosen_inline_query(update: Update, ctx: CallbackContext):
    video = ctx.temp_history.pop(update.chosen_inline_result.result_id, None)
    logger.info("Chosen video: %s", video)
    ctx.temp_history.clear()

    if not video or not ctx.settings.is_history_enabled(update):
        return

    if video not in ctx.history:
        logger.info("Add %s video to history", video)
        ctx.history.append(video)


async def inline_query(update: Update, ctx: CallbackContext):
    """Handle the inline query."""
    logger.info("Checking inline query...")
    query = (update.inline_query.query or "").strip()

    async def send_history():
        return await update.inline_query.answer(
            await inline_query_video_from_media(ctx.history[::-1], ctx),
            is_personal=True,
            switch_pm_text=_("Recently added"),
            switch_pm_parameter="help",
            cache_time=1,
        )

    if not query:
        answer = await send_history()
        logger.info("Send history from inline query: %s", answer)
        return answer

    logger.info("Inline query: %s", query)

    not_found_text = _(
        "No videos found. You don't think it's correct? Press here!"
    )

    async with aiohttp.ClientSession() as session:
        medias: list[Media] = await Parser.parse(session, query)

    logger.info("Medias: %s", medias)
    if not medias:
        r = Report(
            report_type=ReportType.MEDIA_NOT_FOUND,
            message=query,
            report_place=ReportPlace.INLINE,
            extra_data=None,
        )
        report_uid = await Reporter.save_report(r)
        logger.info("No medias found. Report: %s", r)
        return await update.inline_query.answer(
            [],
            is_personal=True,
            switch_pm_text=not_found_text,
            switch_pm_parameter=f"report_{report_uid}",
            cache_time=1,
        )

    results: list[InlineQueryResult] = await inline_query_video_from_media(
        medias, ctx
    )

    if ctx.settings.is_history_enabled(update):
        for video, iq_video in zip(
            filter(lambda x: isinstance(x, Video), medias), results
        ):
            ctx.temp_history[iq_video.id] = video.to_dict()
    r = Report(
        report_type=ReportType.WRONG_MEDIA,
        message=query,
        report_place=ReportPlace.INLINE,
        extra_data=None,
    )
    report_uid = await Reporter.save_report(r)
    logger.info("Report for wrong media: %r", r)
    return await update.inline_query.answer(
        results,
        is_personal=True,
        switch_pm_text=(
            _n("Found %d video", "Found %d videos", len(results)) % len(results)
            + _(". Is it correct media? Press here if not!")
            if results
            else not_found_text
        ),
        switch_pm_parameter=f"report_{report_uid}",
        cache_time=1,
    )


@env_wrapper
async def error(update: Update, context: CallbackContext):
    """Log Errors caused by Updates."""
    exc = context.error
    logger.warning('%s: %s. Update: "%s"', type(exc).__name__, exc, update)
    traceback.print_tb(context.error.__traceback__)
    await notify.send_message(
        message_type=notify.MessageType.EXCEPTION,
        text=f'{type(exc).__name__}: {exc}. Update: "{update}"',
        update=update,
        ctx=context,
        extras={"exception": exc},
    )


def post_something(
    message_type: notify.MessageType,
) -> Callable[[Application], Coroutine[Any, Any, None]]:
    async def wrap(app: Application):
        await notify.send_message(
            message_type=message_type,
            text=f"Application is {message_type.value}",
            extras={"app": app},
        )

    return wrap


async def post_init(app: Application):
    MongoDatabase.init()
    await post_something(notify.MessageType.START)(app)


async def post_shutdown(app: Application):
    MongoDatabase.close()
    await post_something(notify.MessageType.SHUTDOWN)(app)


def main() -> None:
    """Start the bot."""
    logger.debug("Token: %r", constants.TOKEN)

    persistence: MongoPersistence[dict, dict, dict] = MongoPersistence(
        mongo_url=constants.MONGO_URL,
        db_name=constants.MONGO_DB,
        name_col_user_data="user-data",
        name_col_chat_data="chat-data",
        name_col_bot_data="bot-data",
        name_col_conversations_data="conversations-data",
        create_col_if_not_exist=True,
        load_on_flush=False,
        update_interval=30,
    )
    defaults = Defaults(
        parse_mode=ParseMode.HTML,
        tzinfo=constants.TIME_ZONE,
    )
    application = (
        Application.builder()
        .persistence(persistence)
        .defaults(defaults=defaults)
        .token(constants.TOKEN)
        .context_types(ContextTypes(context=CallbackContext))
        .post_init(post_init)
        .post_stop(post_something(notify.MessageType.STOP))
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handlers(
        [
            ChosenInlineResultHandler(chosen_inline_query),
            settings.callback_handler(),
            InlineQueryHandler(inline_query),
            MessageHandler(filters.TEXT & ~filters.COMMAND, link_parser),
        ]
    )
    commands.connect_commands(application)
    patch(application)

    # Run the bot until the user presses Ctrl-C
    # log all errors
    application.add_error_handler(error)
    application.run_polling()


if __name__ == "__main__":
    main()

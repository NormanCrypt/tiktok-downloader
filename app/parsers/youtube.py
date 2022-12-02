import logging
import re
from typing import Match

import aiohttp
import pytube as pytube
from pytube.exceptions import PytubeError

from app import constants
from app.parsers.base import Parser as BaseParser, ParserType, Video, Media

logger = logging.getLogger(__name__)


class Parser(BaseParser):
    TYPE = ParserType.YOUTUBE
    REG_EXPS = [
        # https://www.youtube.com/watch?v=TCrP1SE2DkY
        # https://youtu.be/TCrP1SE2DkY
        re.compile(
            r"(?:https?://)?"
            r"(?:"
            r"(?:www\.)?youtube\.com/watch\?v="
            r"|youtu.be/"
            r")(?P<id>[\w-]+)"
        ),
        # https://youtube.com/shorts/hBOLCcvbGHM
        # https://youtube.com/watch?v=hBOLCcvbGHM
        re.compile(
            r"(?:https?://)?(?:www\.)?youtube\.com/shorts/(?P<id>[\w-]+)"
        )
    ]
    CUSTOM_EMOJI_ID = 5463206079913533096  # 📹

    @classmethod
    def _is_supported(cls) -> bool:
        return True

    @classmethod
    async def _parse(
        cls,
        session: aiohttp.ClientSession,
        match: Match
    ) -> list[Media]:
        try:
            yt_id = match.group('id')
        except IndexError:
            return []

        original_url = f"https://youtube.com/watch?v={yt_id}"

        logger.info("Getting video link from: %s", original_url)
        yt = pytube.YouTube(original_url)
        streams = (
            yt.streams
            .filter(type="video", progressive=True, file_extension="mp4")
            .order_by("resolution")
        )
        logger.info('Found %s streams', len(streams))
        stream = yt.streams.get_highest_resolution()
        max_quality_url = stream.url
        max_fs = 0

        for st in streams:
            logger.info("Stream: %s", st)
            file_size = st.filesize
            logger.info("Stream file size: %s", file_size)
            if constants.TG_FILE_LIMIT >= file_size > max_fs:
                logger.info("Found suitable stream with filesize %s", file_size)
                max_fs = file_size
                stream = st

        try:
            video = Video(
                author=yt.author,
                caption=yt.title,
                thumbnail_url=yt.thumbnail_url,
                type=ParserType.YOUTUBE,
                url=stream.url,
                original_url=original_url,
                max_quality_url=max_quality_url,
                mime_type=stream.mime_type,
            )
        except PytubeError as err:
            logger.error(
                "Failed to get video %r with error: %s",
                original_url,
                err
            )
            return []
        return [video]

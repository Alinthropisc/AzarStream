"""Media upload endpoint - upload file to Telegram cache channel and get file_id"""
import tempfile
import os
from contextlib import suppress
from dataclasses import dataclass

from litestar import post
from litestar.datastructures import UploadFile
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.exceptions import ClientException

from services.official_bot import OfficialBotService
from app.logging import get_logger

log = get_logger("controller.media_upload")


@dataclass
class MediaUploadForm:
    media_file: UploadFile
    media_type: str = "photo"


@post("/admin/ads/upload-media", name="ads:upload_media")
async def upload_media_to_telegram(
    data: MediaUploadForm = Body(media_type=RequestEncodingType.MULTI_PART),
) -> dict:
    """Upload media file to Telegram cache channel and return file_id"""
    media_file = data.media_file
    media_type = data.media_type or "photo"

    try:
        # Validate media type
        if media_type not in ("photo", "video", "animation"):
            raise ClientException(f"Unsupported media type: {media_type}. Use photo, video, or animation.")

        # Read file
        file_content = await media_file.read()
        filename = media_file.filename or "upload.tmp"

        if not file_content:
            raise ClientException("Empty file uploaded")

        log.info("Media upload started", filename=filename, size=len(file_content), media_type=media_type)

        # Save to temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{filename}") as tmp:
            tmp.write(file_content)
            tmp_path = tmp.name

        try:
            # Upload via official bot to cache channel
            upload_result = await OfficialBotService.upload_media(tmp_path, media_type)

            if not upload_result:
                log.error("Upload returned None", media_type=media_type)
                raise ClientException(
                    "Failed to upload. Check that MEDIA_FLOW_BOT_TOKEN and MEDIA_FLOW_CACHE_CHANNEL_ID are configured and bot has access to the channel."
                )

            log.info("Upload success", file_type=media_type, file_id_len=len(upload_result.file_id), message_id=upload_result.message_id)

            return {
                "file_id": upload_result.file_id,
                "message_id": upload_result.message_id,
                "file_type": media_type,
                "filename": filename,
            }

        finally:
            with suppress(BaseException):
                os.unlink(tmp_path)

    except ClientException:
        raise
    except Exception as e:
        log.error("Upload failed", error=str(e), error_type=type(e).__name__)
        raise ClientException(f"Upload failed: {e!r}") from e

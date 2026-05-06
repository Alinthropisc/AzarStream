"""Web controller for cookie management."""

from litestar import Controller, get, post
from litestar.response import Template, Redirect, File
from litestar.connection import Request
from litestar.enums import RequestEncodingType
from litestar.params import Body

from services.downloaders.cookie_manager import SUPPORTED_PLATFORMS, cookie_manager
from app.logging import get_logger

log = get_logger("controller.cookies")


class CookieController(Controller):
    """Controller for managing cookies via web UI."""

    path = "/admin/cookies"

    @get()
    async def list_cookies(
        self,
        request: Request,
    ) -> Template:
        """List all cookie files."""
        all_cookies = cookie_manager.list_all_cookies()

        # Группируем по платформам
        platforms = {}
        for info in all_cookies:
            if info.platform not in platforms:
                platforms[info.platform] = []
            platforms[info.platform].append(info)

        # Статистика
        total = len(all_cookies)
        expired = sum(1 for c in all_cookies if c.is_expired)
        expiring_soon = sum(1 for c in all_cookies if c.is_expiring_soon and not c.is_expired)
        valid = total - expired

        return Template(
            template_name="admin/cookies/list.html",
            context={
                "platforms": platforms,
                "all_cookies": all_cookies,
                "stats": {
                    "total": total,
                    "valid": valid,
                    "expired": expired,
                    "expiring_soon": expiring_soon,
                },
                "supported_platforms": SUPPORTED_PLATFORMS,
                "now": lambda: __import__("datetime").datetime.now(),
                "request": request,
            },
        )

    @get("/upload/{platform:str}")
    async def upload_form(
        self,
        request: Request,
        platform: str,
    ) -> Template:
        """Show upload form for a specific platform."""
        return Template(
            template_name="admin/cookies/upload.html",
            context={
                "platform": platform,
                "supported_platforms": SUPPORTED_PLATFORMS,
                "now": lambda: __import__("datetime").datetime.now(),
                "request": request,
            },
        )

    @post("/upload/{platform:str}")
    async def upload_cookies(
        self,
        request: Request,
        platform: str,
        data: dict = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        """Upload cookies file."""
        try:
            cookie_file = await request.form()
            file = cookie_file.get("cookie_file")

            if not file or not hasattr(file, "filename") or not file.filename:
                return Redirect(
                    path=f"/admin/cookies/upload/{platform}?error=No+file+selected"
                )

            # Читаем содержимое файла
            content = file.file.read()
            if isinstance(content, bytes):
                content = content.decode("utf-8")

            # Сохраняем
            bot_id = data.get("bot_id")
            account_name = data.get("account_name")
            ttl_days = int(data.get("ttl_days", 60))

            info = cookie_manager.save_cookies(
                platform=platform,
                cookies_content=content,
                bot_id=int(bot_id) if bot_id else None,
                account_name=account_name or None,
                ttl_days=ttl_days,
            )

            return Redirect(
                path=f"/admin/cookies?message=Cookies+uploaded+successfully+({info.cookie_count}+cookies)"
            )

        except Exception as e:
            log.exception("Failed to upload cookies", error=str(e))
            return Redirect(
                path=f"/admin/cookies/upload/{platform}?error={__import__('urllib.parse').quote(str(e))}"
            )

    @post("/delete/{platform:str}")
    async def delete_cookies(
        self,
        request: Request,
        platform: str,
        data: dict = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        """Delete cookies."""
        try:
            bot_id = data.get("bot_id")
            account_name = data.get("account_name")

            cookie_manager.delete_cookies(
                platform=platform,
                bot_id=int(bot_id) if bot_id else None,
                account_name=account_name or None,
            )

            return Redirect(
                path="/admin/cookies?message=Cookies+deleted"
            )

        except Exception as e:
            log.exception("Failed to delete cookies", error=str(e))
            return Redirect(
                path=f"/admin/cookies?error={__import__('urllib.parse').quote(str(e))}"
            )

    @get("/download/{platform:str}")
    async def download_cookies(
        self,
        request: Request,
        platform: str,
    ) -> File:
        """Download cookies file."""
        bot_id = request.query_params.get("bot_id")
        account_name = request.query_params.get("account_name")

        file_path = cookie_manager.get_cookie_file_path(
            platform,
            int(bot_id) if bot_id else None,
            account_name,
        )

        if not file_path.exists():
            return Redirect(path="/admin/cookies?error=File+not+found")

        return File(
            path=file_path,
            filename=file_path.name,
        )

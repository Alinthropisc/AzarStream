import asyncio
import time
from typing import Any

from litestar import Controller, Request, get, post
from litestar.response import Response
from litestar.status_codes import HTTP_200_OK, HTTP_503_SERVICE_UNAVAILABLE
from litestar.exceptions import HTTPException

from app.logging import get_logger
from bot.processor import update_processor
from services import bot_manager, cache

log = get_logger("webhook")


class WebhookController(Controller):
    path = "/webhook"

    @get("/health", name="webhook:health")
    async def health_check(self) -> Response:
        try:
            instances_count = (
                len(bot_manager._instances)
                if hasattr(bot_manager, "_instances")
                else 0
            )
            return Response(
                content={"status": "ok", "bots_loaded": instances_count},
                status_code=HTTP_200_OK,
            )
        except Exception as exc:
            log.error("Health check failed", error=str(exc))
            return Response(
                content={"status": "error", "detail": str(exc)},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
            )

    @post("/{bot_token:str}", name="webhook:handle")
    async def handle_webhook(
        self,
        bot_token: str,
        data: dict[str, Any],
        request: Request,
    ) -> Response:
        """
        Принимает webhook от Telegram.
        Возвращает 200 OK мгновенно.
        Обработка идёт в фоне с защитой от дублей.

        Изоляция между ботами (вариант #8/#9):
        Telegram присылает заголовок X-Telegram-Bot-Api-Secret-Token,
        совпадение с bot.webhook_secret гарантирует, что update именно от
        этого бота — даже если URL случайно совпали или кто-то стучится.
        """
        update_id = data.get("update_id")

        # ВАЖНО: всегда отвечаем 200, иначе Telegram будет ретраить webhook бесконечно.
        # Невалидные/неизвестные апдейты просто молча отбрасываем.
        instance = await bot_manager.get_bot_instance(bot_token)
        if not instance:
            log.warning("Webhook for unknown bot — dropping", token_suffix=bot_token[-8:])
            return Response(content={"ok": True}, status_code=HTTP_200_OK)

        # Сначала спрашиваем Redis: при multi-worker setup каждый Granian-процесс
        # имеет свой кэш BotInstance, и при удалении/пересоздании бота на одном
        # worker'е остальные могут хранить устаревший webhook_secret.
        # Redis — общий источник истины, заполняется в setup_webhook.
        shared_secret = await cache.get(f"bot:webhook_secret:{bot_token}")
        expected_secret = shared_secret or instance.model.webhook_secret
        if expected_secret:
            received = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if received != expected_secret:
                log.warning(
                    "Webhook secret mismatch — dropping (cross-bot isolation)",
                    bot=instance.model.username,
                    has_header=received is not None,
                    received_prefix=(received[:6] + "…") if received else None,
                    received_len=len(received) if received else 0,
                    expected_prefix=(expected_secret[:6] + "…") if expected_secret else None,
                    expected_len=len(expected_secret) if expected_secret else 0,
                )
                return Response(content={"ok": True}, status_code=HTTP_200_OK)

        log.info(
            "📨 Webhook received",
            bot=instance.model.username,
            update_id=update_id,
        )

        # Запускаем фоновую обработку с дедупликацией (по bot_id, не по суффиксу токена)
        asyncio.ensure_future(
            _process_with_dedup(bot_token, instance.model.bot_id, data)
        )

        return Response(content={"ok": True}, status_code=HTTP_200_OK)


async def _process_with_dedup(bot_token: str, bot_id: int, data: dict[str, Any]) -> None:
    """
    Обёртка с защитой от двойной обработки.

    Проблема: при 2+ workers Telegram может доставить один update
    на несколько воркеров одновременно. Redis-lock гарантирует
    что только один воркер обработает каждый update_id.
    """
    update_id = data.get("update_id")
    if not update_id:
        await _process_in_background(bot_token, data)
        return

    # Уникальный ключ для этого update — bot_id даёт детерминированную изоляцию
    dedup_key = f"webhook:dedup:{bot_id}:{update_id}"
    # TTL = 60 сек (Telegram повторяет undelivered updates до 24h,
    # но между повторами минимум 1 мин)
    dedup_ttl = 60

    try:
        # Атомарный SET NX — только один воркер успеет
        acquired = await cache.set_nx(dedup_key, "1", ttl=dedup_ttl)

        if not acquired:
            log.debug(
                "Duplicate update skipped",
                update_id=update_id,
                token=bot_token[:10],
            )
            return

        # Мы первые — обрабатываем
        await _process_in_background(bot_token, data)

    except Exception as exc:
        # Если Redis недоступен — обрабатываем без дедупликации
        # лучше дубль чем потеря сообщения
        log.warning(
            "Dedup check failed, processing anyway",
            update_id=update_id,
            error=str(exc),
        )
        await _process_in_background(bot_token, data)


async def _process_in_background(bot_token: str, data: dict[str, Any]) -> None:
    """Основная обработка update."""
    update_id = data.get("update_id")
    update_type = _detect_update_type(data)
    start_time = time.monotonic()

    log.info(
        "⚙️ Processing update",
        token=bot_token[:10],
        update_id=update_id,
        update_type=update_type,
    )

    try:
        await update_processor.process(bot_token, data)

        elapsed_ms = round((time.monotonic() - start_time) * 1000, 2)
        log.info(
            "Update processed",
            update_id=update_id,
            update_type=update_type,
            elapsed_ms=elapsed_ms,
        )

    except asyncio.CancelledError:
        log.warning("Update processing cancelled", update_id=update_id)
        raise

    except Exception as exc:
        elapsed_ms = round((time.monotonic() - start_time) * 1000, 2)
        log.exception(
            "Background processing error",
            update_id=update_id,
            update_type=update_type,
            elapsed_ms=elapsed_ms,
            error=str(exc),
            error_type=type(exc).__name__,
        )


def _detect_update_type(data: dict[str, Any]) -> str:
    update_types = (
        "message", "edited_message", "channel_post",
        "callback_query", "inline_query", "chosen_inline_result",
        "shipping_query", "pre_checkout_query",
        "poll", "poll_answer", "my_chat_member", "chat_member",
    )
    for update_type in update_types:
        if update_type in data:
            return update_type
    return "unknown"
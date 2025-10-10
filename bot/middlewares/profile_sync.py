import logging
from typing import Callable, Dict, Any, Awaitable, Optional

from aiogram import BaseMiddleware
from aiogram.types import Update, User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession

# предпочтительно: from db.dal.user_dal import user_dal
from db.dal import user_dal
from bot.utils.text_sanitizer import sanitize_username, sanitize_display_name, username_for_display


class ProfileSyncMiddleware(BaseMiddleware):

    async def __call__(
        self,
        handler: Callable[[Update, Dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: Dict[str, Any],
    ) -> Any:
        session: Optional[AsyncSession] = data.get("session")
        tg_user: Optional[TgUser] = data.get("event_from_user")

        if session and tg_user:
            try:
                db_user = await user_dal.get_user_by_id(session, tg_user.id)
                if db_user:
                    # 1) санитайзим ВСЕГДА, до сравнений
                    sanitized_username = sanitize_username(getattr(tg_user, "username", None))
                    sanitized_first_name = sanitize_display_name(getattr(tg_user, "first_name", None))
                    sanitized_last_name = sanitize_display_name(getattr(tg_user, "last_name", None))

                    # 2) сравниваем БД с санитайз-версиями
                    update_payload: Dict[str, Any] = {}
                    if db_user.username != sanitized_username:
                        update_payload["username"] = sanitized_username
                    if db_user.first_name != sanitized_first_name:
                        update_payload["first_name"] = sanitized_first_name
                    if db_user.last_name != sanitized_last_name:
                        update_payload["last_name"] = sanitized_last_name

                    if update_payload:
                        # 3) **kwargs, не словарь одним аргументом
                        await user_dal.update_user(session, tg_user.id, **update_payload)
                        logging.info(
                            "ProfileSyncMiddleware: Updated user %s profile fields: %s",
                            tg_user.id,
                            list(update_payload.keys()),
                        )

                        # 4) обновление описания на панели (если привязано)
                        try:
                            panel_service = data.get("panel_service")
                            if panel_service and db_user.panel_user_uuid:
                                parts = [
                                    username_for_display(sanitized_username, with_at=False) if sanitized_username else None,
                                    sanitized_first_name or None,
                                    sanitized_last_name or None,
                                ]
                                description_text = "\n".join(p for p in parts if p).strip()
                                if description_text:
                                    await panel_service.update_user_details_on_panel(
                                        db_user.panel_user_uuid,
                                        {"description": description_text},
                                    )
                        except Exception as e_upd_desc:
                            logging.warning(
                                "ProfileSyncMiddleware: Failed to update panel description for user %s: %s",
                                tg_user.id,
                                e_upd_desc,
                            )
            except Exception as e:
                logging.error(
                    "ProfileSyncMiddleware: Failed to sync profile for user %s: %s",
                    getattr(tg_user, "id", "N/A"),
                    e,
                    exc_info=True,
                )

        return await handler(event, data)

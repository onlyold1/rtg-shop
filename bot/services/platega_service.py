# bot/services/platega_service.py

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, Optional

import aiohttp
from aiohttp import web
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from config.settings import Settings
from bot.middlewares.i18n import JsonI18n
from bot.services.subscription_service import SubscriptionService
from bot.services.referral_service import ReferralService
from bot.services.notification_service import NotificationService
from db.dal import payment_dal, user_dal

from bot.keyboards.inline.user_keyboards import (
    get_payment_url_keyboard,
    get_connect_and_main_keyboard,
)

# --- Константы Platega API ---
_PLATEGA_BASE_URL = "https://app.platega.io"
_API_CREATE = "/transaction/process"
_API_STATUS = "/transaction/{transaction_id}"
_API_RATE = "/rates/payment_method_rate"

# Возможные статусы от Platega:
STATUS_PENDING = "PENDING"
STATUS_CONFIRMED = "CONFIRMED"
STATUS_EXPIRED = "EXPIRED"
STATUS_CANCELED = "CANCELED"
STATUS_FAILED = "FAILED"

PLATEGA_STATUS_CODE_MAP = {
    1: STATUS_PENDING,     # пример: "создан/в ожидании"
    7: STATUS_CONFIRMED,   # пример: "успешно оплачено"
    8: STATUS_EXPIRED,     # пример: "истёк"
    9: STATUS_CANCELED,    # пример: "отменён"
    10: STATUS_FAILED,     # пример: "ошибка/отклонён"
}

def _normalize_platega_status(raw) -> str:
    if isinstance(raw, int):
        return PLATEGA_STATUS_CODE_MAP.get(raw, str(raw).upper())
    if isinstance(raw, str):
        s = raw.strip().upper()
        if s.isdigit():
            return PLATEGA_STATUS_CODE_MAP.get(int(s), s)
        return s
    return str(raw).upper()

# Блокировка последовательной обработки входящих вебхуков
payment_processing_lock = asyncio.Lock()


class PlategaService:
    """
    Сервис-интеграция с Platega.

    Основные методы:
      • create_invoice(...) -> создаёт транзакцию и возвращает redirect URL
      • get_status(...) -> проверяет статус транзакции
      • get_rate(...) -> получает курс
      • platega_webhook_route(...) -> aiohttp-роут для приёма колбэков
    """

    def __init__(
        self,
        bot: Bot,
        settings: Settings,
        i18n: JsonI18n,
        async_session_factory: sessionmaker,
        subscription_service: SubscriptionService,
        referral_service: ReferralService,
    ):
        self.bot = bot
        self.settings = settings
        self.i18n = i18n
        self.async_session_factory = async_session_factory
        self.subscription_service = subscription_service
        self.referral_service = referral_service

        self._session: Optional[aiohttp.ClientSession] = None

        # Конфигурация
        self.enabled: bool = getattr(settings, "PLATEGA_ENABLED", False)
        self.merchant_id: Optional[str] = getattr(settings, "PLATEGA_MERCHANT_ID", None)
        self.secret: Optional[str] = getattr(settings, "PLATEGA_API_SECRET", None)
        self.return_url: Optional[str] = getattr(settings, "PLATEGA_RETURN_URL", None)
        self.failed_url: Optional[str] = getattr(settings, "PLATEGA_FAILED_URL", None)
        self.default_payment_method: int = int(
            getattr(settings, "PLATEGA_DEFAULT_METHOD", 2)
        )

    # --- Жизненный цикл HTTP-сессии ---

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.merchant_id and self.secret)

    async def _ensure_http(self):
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def close(self):
        if self._session and not self._session.closed:
            try:
                await self._session.close()
            except Exception as e:
                logging.warning(f"PlategaService: error closing session: {e}")

    # --- Вспомогательное ---

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-MerchantId": str(self.merchant_id),
            "X-Secret": str(self.secret),
        }

    async def _safe_username(self) -> str:
        """Попытка получить username бота (fallback — 'bot')."""
        try:
            me = await self.bot.get_me()
            return me.username or "bot"
        except Exception:
            return "bot"

    # --- Публичные методы сервиса ---

    async def create_invoice(
        self,
        session: AsyncSession,
        user_id: int,
        months: int,
        amount_rub: float,
        description: str,
        payment_method: Optional[int] = None,
    ) -> Optional[str]:
        """
        Создаёт платёж в нашей БД и транзакцию в Platega.
        Возвращает redirect URL для оплаты.
        """
        if not self.configured:
            logging.error("PlategaService: not configured or disabled.")
            return None

        pm = payment_method or self.default_payment_method
        currency = "RUB"

        # 1) создаём payment запись в БД
        payment_record_data = {
            "user_id": user_id,
            "amount": float(amount_rub),
            "currency": currency,
            "status": "pending",
            "description": description,
            "subscription_duration_months": months,
            "provider": "platega",
        }
        try:
            db_payment = await payment_dal.create_payment_record(
                session, payment_record_data
            )
            await session.commit()
        except Exception as e:
            await session.rollback()
            logging.error(
                f"PlategaService: failed to create DB payment for user {user_id}: {e}",
                exc_info=True,
            )
            return None

        # 2) создаём транзакцию в Platega
        await self._ensure_http()
        tx_uuid = str(uuid.uuid4())
        payload_str = json.dumps(
            {
                "user_id": user_id,
                "payment_db_id": db_payment.payment_id,
                "months": months,
            },
            ensure_ascii=False,
        )

        body = {
            "paymentMethod": pm,
            "id": tx_uuid,
            "paymentDetails": {
                "amount": int(amount_rub)
                if amount_rub == int(amount_rub)
                else amount_rub,
                "currency": currency,
            },
            "description": description,
            "return": self.return_url or f"https://t.me/{(await self._safe_username())}",
            "failedUrl": self.failed_url
            or f"https://t.me/{(await self._safe_username())}",
            "payload": payload_str,
        }

        try:
            async with self._session.post(
                _PLATEGA_BASE_URL + _API_CREATE,
                headers=self._auth_headers(),
                json=body,
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logging.error(f"Platega create_invoice HTTP {resp.status}: {text}")
                    try:
                        await payment_dal.update_payment_status_by_db_id(
                            session, db_payment.payment_id, "failed_creation"
                        )
                        await session.commit()
                    except Exception:
                        await session.rollback()
                    return None
                data = json.loads(text)
        except Exception as e:
            logging.error(f"Platega create_invoice request failed: {e}", exc_info=True)
            try:
                await payment_dal.update_payment_status_by_db_id(
                    session, db_payment.payment_id, "failed_creation"
                )
                await session.commit()
            except Exception:
                await session.rollback()
            return None

        transaction_id = data.get("transactionId")
        redirect_url = data.get("redirect")
        status_from_api = data.get("status", STATUS_PENDING)

        if not transaction_id or not redirect_url:
            logging.error(f"Platega create_invoice: invalid response: {data}")
            try:
                await payment_dal.update_payment_status_by_db_id(
                    session, db_payment.payment_id, "failed_creation"
                )
                await session.commit()
            except Exception:
                await session.rollback()
            return None

        try:
            await payment_dal.update_provider_payment_and_status(
                session=session,
                payment_db_id=db_payment.payment_id,
                provider_payment_id=str(transaction_id),
                new_status="pending"
                if status_from_api == STATUS_PENDING
                else str(status_from_api).lower(),
            )
            await session.commit()
        except Exception as e:
            await session.rollback()
            logging.error(
                f"Platega create_invoice: failed to update provider id for DB payment {db_payment.payment_id}: {e}",
                exc_info=True,
            )
        return redirect_url

    async def get_status(self, transaction_id: str) -> Optional[Dict[str, Any]]:
        if not self.configured:
            return None
        await self._ensure_http()
        try:
            async with self._session.get(
                _PLATEGA_BASE_URL + _API_STATUS.format(transaction_id=transaction_id),
                headers=self._auth_headers(),
            ) as resp:
                if resp.status != 200:
                    logging.warning(f"Platega get_status HTTP {resp.status}")
                    return None
                return await resp.json()
        except Exception as e:
            logging.error(f"Platega get_status error: {e}")
            return None

    async def get_rate(
        self,
        payment_method: Optional[int] = None,
        currency_from: str = "RUB",
        currency_to: str = "USDT",
    ) -> Optional[Dict[str, Any]]:
        if not self.configured:
            return None
        await self._ensure_http()
        pm = payment_method or self.default_payment_method
        params = {
            "merchantId": self.merchant_id,
            "paymentMethod": pm,
            "currencyFrom": currency_from,
            "currencyTo": currency_to,
        }
        try:
            async with self._session.get(
                _PLATEGA_BASE_URL + _API_RATE,
                headers=self._auth_headers() | {"accept": "application/json"},
                params=params,
            ) as resp:
                if resp.status != 200:
                    logging.warning(f"Platega get_rate HTTP {resp.status}")
                    return None
                return await resp.json()
        except Exception as e:
            logging.error(f"Platega get_rate error: {e}")
            return None


# ------------------------ Webhook / Callback ------------------------

async def _process_platega_confirmed(
    session: AsyncSession,
    bot: Bot,
    i18n: JsonI18n,
    settings: Settings,
    subscription_service: SubscriptionService,
    referral_service: ReferralService,
    event: Dict[str, Any],
) -> None:
    transaction_id = str(event.get("id") or "")
    status = str(event.get("status") or "").upper()
    amount_val = float(event.get("amount") or 0.0)
    currency = str(event.get("currency") or "RUB")

    if not transaction_id:
        logging.error("Platega callback: missing 'id' in event")
        return

    payment_model = await payment_dal.get_payment_by_provider_payment_id(
        session, transaction_id
    )
    if not payment_model:
        logging.error(
            f"Platega callback: payment not found for transaction_id={transaction_id}"
        )
        return

    user_id = payment_model.user_id
    months = payment_model.subscription_duration_months or 1
    payment_db_id = payment_model.payment_id

    try:
        await payment_dal.update_provider_payment_and_status(
            session=session,
            payment_db_id=payment_db_id,
            provider_payment_id=transaction_id,
            new_status="succeeded" if status == STATUS_CONFIRMED else "failed",
        )
        await session.flush()
    except Exception as e:
        logging.error(
            f"Platega callback: failed to update payment status for DB id {payment_db_id}: {e}",
            exc_info=True,
        )

    try:
        activation = await subscription_service.activate_subscription(
            session=session,
            user_id=user_id,
            months=months,
            payment_amount=amount_val,
            payment_db_id=payment_db_id,
            promo_code_id_from_payment=None,
            provider="platega",
        )
        if not activation or not activation.get("end_date"):
            raise RuntimeError("activation returned no end_date")

        referral_info = await referral_service.apply_referral_bonuses_for_payment(
            session=session,
            user_id=user_id,
            months=months,
            current_payment_db_id=payment_db_id,
            skip_if_active_before_payment=False,
        )

        final_end_date = activation["end_date"]
        applied_promo_bonus_days = activation.get("applied_promo_bonus_days", 0)
        applied_referee_bonus_days = (
            referral_info.get("referee_bonus_applied_days") if referral_info else None
        )
        if referral_info and referral_info.get("referee_new_end_date"):
            final_end_date = referral_info["referee_new_end_date"]

        db_user = await user_dal.get_user_by_id(session, user_id)
        user_lang = (
            db_user.language_code
            if db_user and db_user.language_code
            else settings.DEFAULT_LANGUAGE
        )
        _ = lambda key, **kwargs: i18n.gettext(user_lang, key, **kwargs)

        config_link = activation.get("subscription_url") or _(
            "config_link_not_available"
        )
        details_markup = get_connect_and_main_keyboard(
            user_lang, i18n, settings, config_link
        )

        if applied_referee_bonus_days and final_end_date:
            inviter_name_display = _("friend_placeholder")
            if db_user and db_user.referred_by_id:
                inviter = await user_dal.get_user_by_id(session, db_user.referred_by_id)
                if inviter and inviter.first_name:
                    inviter_name_display = inviter.first_name
                elif inviter and inviter.username:
                    inviter_name_display = f"@{inviter.username}"

            message_text = _(
                "payment_successful_with_referral_bonus_full",
                months=months,
                base_end_date=activation["end_date"].strftime("%Y-%m-%d"),
                bonus_days=applied_referee_bonus_days,
                final_end_date=final_end_date.strftime("%Y-%m-%d"),
                inviter_name=inviter_name_display,
                config_link=config_link,
            )
        elif applied_promo_bonus_days > 0 and final_end_date:
            message_text = _(
                "payment_successful_with_promo_full",
                months=months,
                bonus_days=applied_promo_bonus_days,
                end_date=final_end_date.strftime("%Y-%m-%d"),
                config_link=config_link,
            )
        else:
            message_text = _(
                "payment_successful_full",
                months=months,
                end_date=final_end_date.strftime("%Y-%m-%d"),
                config_link=config_link,
            )

        try:
            await bot.send_message(
                user_id,
                message_text,
                reply_markup=details_markup,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logging.error(f"Platega callback: failed to notify user {user_id}: {e}")

        try:
            note = NotificationService(bot, settings, i18n)
            await note.notify_payment_received(
                user_id=user_id,
                amount=amount_val,
                currency=settings.DEFAULT_CURRENCY_SYMBOL or currency,
                months=months,
                payment_provider="platega",
                username=(db_user.username if db_user else None),
            )
        except Exception as e:
            logging.error(f"Platega callback: notify_payment_received failed: {e}")

    except Exception as e:
        logging.error(
            f"Platega callback: activation/referral failed for user {user_id}: {e}",
            exc_info=True,
        )
        raise


async def _process_platega_cancelled(
    session: AsyncSession,
    bot: Bot,
    i18n: JsonI18n,
    settings: Settings,
    event: Dict[str, Any],
) -> None:
    transaction_id = str(event.get("id") or "")
    if not transaction_id:
        logging.error("Platega callback (cancelled): missing id")
        return

    payment_model = await payment_dal.get_payment_by_provider_payment_id(
        session, transaction_id
    )
    if not payment_model:
        logging.warning(
            f"Platega callback (cancelled): payment not found for transaction {transaction_id}"
        )
        return

    try:
        await payment_dal.update_provider_payment_and_status(
            session=session,
            payment_db_id=payment_model.payment_id,
            provider_payment_id=transaction_id,
            new_status="canceled",
        )
        await session.flush()
    except Exception as e:
        logging.error(f"Platega callback: failed to set canceled: {e}")

    db_user = await user_dal.get_user_by_id(session, payment_model.user_id)
    user_lang = (
        db_user.language_code if db_user and db_user.language_code else settings.DEFAULT_LANGUAGE
    )
    _ = lambda key, **kwargs: i18n.gettext(user_lang, key, **kwargs)
    try:
        await bot.send_message(payment_model.user_id, _("payment_failed"))
    except Exception as e:
        logging.error(f"Platega callback: failed to notify user about cancel: {e}")


# --- основной HTTP-роут колбэка ---

async def platega_webhook_route(request: web.Request):
    try:
        bot: Bot = request.app["bot"]
        i18n: JsonI18n = request.app["i18n"]
        settings: Settings = request.app["settings"]
        subscription_service: SubscriptionService = request.app["subscription_service"]
        referral_service: ReferralService = request.app["referral_service"]
        async_session_factory: sessionmaker = request.app["async_session_factory"]
    except KeyError as e:
        logging.error(f"Platega webhook: app context missing key: {e}")
        return web.Response(status=500, text="internal_error_missing_context")

    import logging
    logging.info("Platega webhook HIT")
    
    recv_merchant = request.headers.get("X-MerchantId")
    recv_secret = request.headers.get("X-Secret")
    expected_merchant = getattr(settings, "PLATEGA_MERCHANT_ID", None)
    expected_secret = getattr(settings, "PLATEGA_API_SECRET", None)

    if not expected_merchant or not expected_secret:
        logging.error("Platega webhook: credentials not configured in settings.")
        return web.Response(status=401, text="unauthorized_not_configured")

    if recv_merchant != expected_merchant or recv_secret != expected_secret:
        logging.warning(
            f"Platega webhook: invalid headers. got X-MerchantId={recv_merchant}, X-Secret={bool(recv_secret)}"
        )
        return web.Response(status=401, text="unauthorized")

    try:
        event = await request.json()
    except Exception:
        logging.error("Platega webhook: invalid JSON")
        return web.Response(status=400, text="bad_json")

    status = _normalize_platega_status(event.get("status"))
    tx_id = str(event.get("id") or "")

    if not tx_id:
        return web.Response(status=400, text="missing_id")

    async with payment_processing_lock:
        async with async_session_factory() as session:
            try:
                if status == STATUS_CONFIRMED:
                    await _process_platega_confirmed(
                        session=session,
                        bot=bot,
                        i18n=i18n,
                        settings=settings,
                        subscription_service=subscription_service,
                        referral_service=referral_service,
                        event=event,
                    )
                    await session.commit()
                elif status in (STATUS_CANCELED, STATUS_FAILED, STATUS_EXPIRED):
                    await _process_platega_cancelled(
                        session=session, bot=bot, i18n=i18n, settings=settings, event=event
                    )
                    await session.commit()
                else:
                    logging.info(
                        f"Platega webhook: transaction {tx_id} status={status}"
                    )
            except Exception as e:
                await session.rollback()
                logging.error(
                    f"Platega webhook: processing error for {tx_id}: {e}",
                    exc_info=True,
                )
                return web.Response(status=200, text="ok_internal_error_logged")

    return web.Response(status=200, text="ok")


# ------------------------ Хэндлер для UI (кнопка оплатить) ------------------------

async def pay_platega_flow(
    callback_event,
    i18n_data: Dict[str, Any],
    settings: Settings,
    platega_service: "PlategaService",
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    if not i18n or not getattr(callback_event, "message", None):
        try:
            await callback_event.answer(_("error_occurred_try_again"), show_alert=True)
        except Exception:
            pass
        return

    if not platega_service or not platega_service.configured:
        await callback_event.message.edit_text(_("payment_service_unavailable"))
        try:
            await callback_event.answer(_("payment_service_unavailable_alert"), show_alert=True)
        except Exception:
            pass
        return

    try:
        payload = callback_event.data.split(":", 1)[1]
        months_str, price_str = payload.split(":")
        months = int(months_str)
        amount_rub = float(price_str)
    except Exception:
        logging.error(f"pay_platega: bad callback data -> {callback_event.data}")
        try:
            await callback_event.answer(_("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    user_id = callback_event.from_user.id
    description = _("payment_description_subscription", months=months)

    payment_url = await platega_service.create_invoice(
        session=session,
        user_id=user_id,
        months=months,
        amount_rub=amount_rub,
        description=description,
        payment_method=None,
    )

    if payment_url:
        try:
            await callback_event.message.edit_text(
                _("payment_link_message", months=months),
                reply_markup=get_payment_url_keyboard(payment_url, current_lang, i18n),
                disable_web_page_preview=False,
            )
        except Exception as e:
            logging.warning(f"pay_platega: edit_text failed: {e}; sending new message")
            await callback_event.message.answer(
                _("payment_link_message", months=months),
                reply_markup=get_payment_url_keyboard(payment_url, current_lang, i18n),
                disable_web_page_preview=False,
            )
    else:
        await callback_event.message.edit_text(_("error_payment_gateway"))

    try:
        await callback_event.answer()
    except Exception:
        pass

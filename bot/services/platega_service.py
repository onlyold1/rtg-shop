# platega_service.py
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

import aiohttp
from aiohttp import web, ClientTimeout
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

# === Project-specific imports (adjust paths to your project structure) ===
# These are referenced in your existing yookassa_service.py/tribute_service.py.
# If paths differ, update the imports accordingly.
try:
    from db.dal import payment_dal  # expected to expose update_provider_payment_and_status, get_payment_by_provider_payment_id, link_provider_payment_id
except Exception:  # pragma: no cover - allow file to import standalone for review
    payment_dal = None  # type: ignore

try:
    from config.settings import Settings  # expected to contain PLATEGA_*
except Exception:  # pragma: no cover
    from dataclasses import dataclass
    @dataclass
    class Settings:  # fallback struct for local tests; replace with your real Settings
        PLATEGA_BASE_URL: str = "https://app.platega.io"
        PLATEGA_MERCHANT_ID: str = "00000000-0000-0000-0000-000000000000"
        PLATEGA_API_SECRET: str = "changeme"
        PLATEGA_CALLBACK_AUTH_CHECK: bool = True
        HTTP_CLIENT_TIMEOUT_SEC: int = 15


logger = logging.getLogger("platega_service")


# === Payment method constants from docs ===
class PlategaMethod:
    SBP_P2P_RF_ONLY = 1          # поиск только реквизитов банков РФ
    SBP_QR = 2                   # НСПК / QR
    ALL_RU = 9                   # реквизиты СБП / карты РФ
    CARD_RU_P2P = 10             # реквизиты только карт РФ
    CARD_2DS_MIR = 11            # карточный 2дс, карты МИР
    INTERNATIONAL = 12           # международный эквайринг


@dataclass
class CreatePaymentResult:
    transaction_id: str
    redirect_url: Optional[str]
    status: str
    expires_in: Optional[str]
    raw: Dict[str, Any]


class PlategaService:
    """
    Полноценная интеграция с Platega API.
    Реализует:
      - создание платежа (transaction/process)
      - проверку статуса (transaction/{id})
      - получение курсов (rates/payment_method_rate)
      - приём callback’ов
    """

    def __init__(
        self,
        settings: Settings,
        bot: Optional[Bot] = None,
        http_timeout_sec: Optional[int] = None,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self.settings = settings
        self.bot = bot

        self.base_url = getattr(settings, "PLATEGA_BASE_URL", "https://app.platega.io").rstrip("/")
        self.merchant_id = getattr(settings, "PLATEGA_MERCHANT_ID", None)
        self.api_secret = getattr(settings, "PLATEGA_API_SECRET", None)
        self.check_callback_auth = bool(getattr(settings, "PLATEGA_CALLBACK_AUTH_CHECK", True))

        timeout = http_timeout_sec or int(getattr(settings, "HTTP_CLIENT_TIMEOUT_SEC", 15))
        self._own_client = False
        if session is None:
            self._own_client = True
            self.http = aiohttp.ClientSession(timeout=ClientTimeout(total=timeout))
        else:
            self.http = session

        if not self.merchant_id or not self.api_secret:
            logger.warning("PlategaService: merchant credentials are not configured (PLATEGA_MERCHANT_ID / PLATEGA_API_SECRET)")

    # --- lifecycle ---
    async def aclose(self) -> None:
        if self._own_client and not self.http.closed:
            await self.http.close()

    # --- helpers ---
    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-MerchantId": str(self.merchant_id),
            "X-Secret": str(self.api_secret),
        }

    @staticmethod
    def map_status(provider_status: str) -> str:
        """Map Platega status to internal status used in your system."""
        s = (provider_status or "").upper()
        if s in {"PENDING"}:
            return "requires_action"
        if s in {"CONFIRMED"}:
            return "succeeded"
        if s in {"CANCELED", "FAILED", "EXPIRED"}:
            return "canceled"
        # fallback for unknown
        return "failed"

    # === API calls ===
    async def create_payment(
        self,
        *,
        db_session_factory: Optional[sessionmaker] = None,
        payment_db_id: Optional[int] = None,
        amount: int = 0,
        currency: str = "RUB",
        payment_method: int = PlategaMethod.SBP_QR,
        description: Optional[str] = None,
        return_url: Optional[str] = None,
        failed_url: Optional[str] = None,
        payload: Optional[str] = None,
        explicit_transaction_id: Optional[str] = None,
    ) -> CreatePaymentResult:
        """
        Создаёт транзакцию и возвращает redirect URL (если есть).
        Вы можете передать свой UUID (explicit_transaction_id), либо он будет сгенерирован.
        Рекомендуется записывать связывание provider_id <-> payment_db_id.
        """
        txn_id = explicit_transaction_id or str(uuid.uuid4())

        body = {
            "paymentMethod": int(payment_method),
            "id": txn_id,
            "paymentDetails": {
                "amount": amount,
                "currency": currency,
            },
        }
        if description:
            body["description"] = description
        if return_url:
            body["return"] = return_url
        if failed_url:
            body["failedUrl"] = failed_url
        if payload is not None:
            # Рекомендуем положить сюда ваш internal payment_db_id для последующего маппинга в callback.
            body["payload"] = payload

        url = f"{self.base_url}/transaction/process"
        logger.info("Platega create_payment: POST %s body=%s", url, body)

        async with self.http.post(url, headers=self._auth_headers(), json=body) as resp:
            text = await resp.text()
            if resp.status >= 400:
                logger.error("Platega create_payment error %s: %s", resp.status, text)
                raise web.HTTPBadRequest(text=text)

            # У Platega разные ответы для QR/P2P; приводим к общему виду
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                logger.error("Platega create_payment: invalid JSON response: %s", text)
                raise web.HTTPInternalServerError(text="invalid provider response")

        # Из ответа стараемся достать redirect/transactionId/status
        transaction_id = data.get("transactionId") or data.get("id") or txn_id
        redirect_url = data.get("redirect")
        status = data.get("status") or "PENDING"
        expires_in = data.get("expiresIn")

        # Линкуем в БД provider_payment_id → наш payment_db_id
        if db_session_factory and payment_db_id is not None and payment_dal and hasattr(payment_dal, "link_provider_payment_id"):
            try:
                async with db_session_factory() as session:  # type: AsyncSession
                    await payment_dal.link_provider_payment_id(
                        session,
                        payment_db_id=payment_db_id,
                        provider="platega",
                        provider_payment_id=transaction_id,
                    )
            except Exception:
                logger.exception("Platega create_payment: failed to link provider_payment_id")

        return CreatePaymentResult(
            transaction_id=transaction_id,
            redirect_url=redirect_url,
            status=status,
            expires_in=expires_in,
            raw=data,
        )

    async def get_status(self, transaction_id: str) -> Dict[str, Any]:
        url = f"{self.base_url}/transaction/{transaction_id}"
        logger.debug("Platega get_status: GET %s", url)

        async with self.http.get(url, headers=self._auth_headers()) as resp:
            text = await resp.text()
            if resp.status >= 400:
                logger.error("Platega get_status error %s: %s", resp.status, text)
                raise web.HTTPBadRequest(text=text)
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                logger.error("Platega get_status: invalid JSON: %s", text)
                raise web.HTTPInternalServerError(text="invalid provider response")

        data["internalStatus"] = self.map_status(data.get("status", ""))
        return data

    async def get_rate(
        self,
        *,
        payment_method: int,
        currency_from: str,
        currency_to: str,
        accept: str = "application/json",
    ) -> Dict[str, Any]:
        """
        Возвращает текущий курс обмена для указанного метода и валюты.
        Документация: GET /rates/payment_method_rate
        """
        url = (
            f"{self.base_url}/rates/payment_method_rate"
            f"?merchantId={self.merchant_id}"
            f"&paymentMethod={int(payment_method)}"
            f"&currencyFrom={currency_from}"
            f"&currencyTo={currency_to}"
        )
        headers = {
            "accept": accept,
            "X-MerchantId": str(self.merchant_id),
            "X-Secret": str(self.api_secret),
        }
        logger.debug("Platega get_rate: GET %s", url)

        async with self.http.get(url, headers=headers) as resp:
            text = await resp.text()
            if resp.status >= 400:
                logger.error("Platega get_rate error %s: %s", resp.status, text)
                raise web.HTTPBadRequest(text=text)
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                # Если запрошен text/plain — вернём как есть
                if accept == "text/plain":
                    return {"raw": text}
                logger.error("Platega get_rate: invalid JSON: %s", text)
                raise web.HTTPInternalServerError(text="invalid provider response")

        return data

    # === Callback handling ===
    async def verify_callback_headers(self, request: web.Request) -> None:
        """
        Минимальная аутентификация callback-а на основании заголовков X-MerchantId / X-Secret.
        Если провайдер поддержит подпись/HMAC — здесь это место для проверки.
        """
        if not self.check_callback_auth:
            return

        received_merchant = request.headers.get("X-MerchantId")
        received_secret = request.headers.get("X-Secret")
        if not received_merchant or not received_secret:
            logger.warning("Platega callback: missing auth headers")
            raise web.HTTPUnauthorized(reason="missing headers")

        # Простая проверка равенства. Если провайдер добавит подпись, здесь заменить логику.
        if str(received_merchant) != str(self.merchant_id) or str(received_secret) != str(self.api_secret):
            logger.warning("Platega callback: invalid auth headers")
            raise web.HTTPUnauthorized(reason="bad credentials")

    async def handle_callback(
        self,
        raw_body: bytes,
        db_session_factory: sessionmaker,
    ) -> web.Response:
        """
        Обработка callback POST:
        Headers: X-MerchantId, X-Secret
        Body JSON: { id, amount, currency, status, paymentMethod }
        """
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            logger.error("Platega callback: invalid JSON")
            return web.Response(status=400, text="bad_request_invalid_json")

        provider_payment_id = payload.get("id")
        provider_status = (payload.get("status") or "").upper()

        # Маппим в ваш внутренний статус
        internal_status = self.map_status(provider_status)

        # Определяем наш payment_db_id.
        # Вариант 1: у вас есть DAL-функция поиска по provider_payment_id.
        payment_db_id = None
        if payment_dal and hasattr(payment_dal, "get_payment_by_provider_payment_id"):
            try:
                async with db_session_factory() as session:  # type: AsyncSession
                    rec = await payment_dal.get_payment_by_provider_payment_id(session, provider_payment_id)
                    if rec:
                        payment_db_id = getattr(rec, "id", None)
            except Exception:
                logger.exception("Platega callback: lookup by provider_payment_id failed")

        if payment_db_id is None:
            logger.warning("Platega callback: payment_db_id not found, provider_id=%s", provider_payment_id)
            # Принимаем callback, чтобы не было повторов; событие залогировано для ручной обработки.
            return web.Response(status=200, text="ok_missing_payment_db_id_logged")

        # Обновляем статус в БД
        try:
            async with db_session_factory() as session:  # type: AsyncSession
                updated = await payment_dal.update_provider_payment_and_status(
                    session,
                    payment_db_id=payment_db_id,
                    new_status=internal_status,
                    provider_payment_id=provider_payment_id,
                )
                if not updated:
                    logger.error("Platega callback: DB update returned falsy value (id=%s)", payment_db_id)
                    return web.Response(status=200, text="ok_internal_processing_error_logged")
        except Exception:
            logger.exception("Platega callback: DB update error")
            return web.Response(status=200, text="ok_internal_processing_error_logged")

        # Доп. бизнес-логика: активация подписки/уведомления и т.п. при succeeded.
        # if internal_status == "succeeded":
        #     try:
        #         ...
        #     except Exception:
        #         logger.exception("Platega callback: business logic error")

        return web.Response(status=200, text="ok")


# === AIOHTTP route handlers to plug into your app ===
async def platega_create_handler(request: web.Request) -> web.Response:
    """
    Пример endpoint'а для создания платежа.
    POST /payments/platega/create
    Body JSON:
    {
      "payment_db_id": 123,
      "amount": 970,
      "currency": "RUB",
      "paymentMethod": 2,
      "description": "test",
      "returnUrl": "https://example.com/success",
      "failedUrl": "https://example.com/fail"
    }
    """
    settings: Settings = request.app["settings"]
    db_session_factory: sessionmaker = request.app["db_session_factory"]
    service = PlategaService(settings)
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="invalid json")

    result = await service.create_payment(
        db_session_factory=db_session_factory,
        payment_db_id=data.get("payment_db_id"),
        amount=int(data.get("amount", 0)),
        currency=data.get("currency", "RUB"),
        payment_method=int(data.get("paymentMethod", PlategaMethod.SBP_QR)),
        description=data.get("description"),
        return_url=data.get("returnUrl"),
        failed_url=data.get("failedUrl"),
        payload=str(data.get("payment_db_id")) if data.get("payment_db_id") is not None else None,
    )
    await service.aclose()
    return web.json_response(
        {
            "transactionId": result.transaction_id,
            "redirect": result.redirect_url,
            "status": result.status,
            "expiresIn": result.expires_in,
            "raw": result.raw,
        }
    )


async def platega_status_handler(request: web.Request) -> web.Response:
    """
    GET /payments/platega/status/{transaction_id}
    """
    settings: Settings = request.app["settings"]
    service = PlategaService(settings)
    transaction_id = request.match_info.get("transaction_id")
    if not transaction_id:
        return web.Response(status=400, text="missing transaction_id")

    data = await service.get_status(transaction_id)
    await service.aclose()
    return web.json_response(data)


async def platega_rates_handler(request: web.Request) -> web.Response:
    """
    GET /payments/platega/rates?paymentMethod=2&from=RUB&to=USDT
    """
    settings: Settings = request.app["settings"]
    service = PlategaService(settings)
    pm = int(request.query.get("paymentMethod", PlategaMethod.SBP_QR))
    frm = request.query.get("from", "RUB")
    to = request.query.get("to", "USDT")
    data = await service.get_rate(payment_method=pm, currency_from=frm, currency_to=to)
    await service.aclose()
    return web.json_response(data)


async def platega_callback_handler(request: web.Request) -> web.Response:
    """
    POST /webhooks/platega
    Headers must include X-MerchantId and X-Secret (unless PLATEGA_CALLBACK_AUTH_CHECK=False).
    Body:
    {
        "id": "uuid",
        "amount": 100,
        "currency": "RUB",
        "status": "CONFIRMED|CANCELED|PENDING|FAILED|EXPIRED",
        "paymentMethod": 2
    }
    """
    settings: Settings = request.app["settings"]
    db_session_factory: sessionmaker = request.app["db_session_factory"]
    service = PlategaService(settings)

    # Verify callback headers (simple equality check per docs)
    try:
        await service.verify_callback_headers(request)
    except web.HTTPException as e:
        return web.Response(status=e.status, text=e.reason)

    raw = await request.read()
    resp = await service.handle_callback(raw, db_session_factory)
    await service.aclose()
    return resp


# === App wiring helper ===
def setup_platega_routes(app: web.Application) -> None:
    """
    Вспомогательный метод для регистрации роутов в вашем aiohttp-приложении.
    """
    app.router.add_post("/payments/platega/create", platega_create_handler)
    app.router.add_get("/payments/platega/status/{transaction_id}", platega_status_handler)
    app.router.add_get("/payments/platega/rates", platega_rates_handler)
    app.router.add_post("/webhooks/platega", platega_callback_handler)


# === Example of usage in an aiohttp app (pseudo) ===
# from aiohttp import web
# app = web.Application()
# app["settings"] = Settings(...)
# app["db_session_factory"] = sessionmaker(..., class_=AsyncSession)
# setup_platega_routes(app)
# web.run_app(app, port=8080)

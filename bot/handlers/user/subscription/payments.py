import logging
from datetime import datetime
from aiogram import Router, F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from typing import Optional, List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from bot.keyboards.inline.user_keyboards import (
    get_payment_method_keyboard,
    get_payment_url_keyboard,
    get_yk_autopay_choice_keyboard,
    get_yk_saved_cards_keyboard,
    get_back_to_main_menu_markup,
)
from bot.services.yookassa_service import YooKassaService
from bot.services.freekassa_service import FreeKassaService
from bot.services.platega_service import PlategaService
from bot.services.crypto_pay_service import CryptoPayService
from bot.services.stars_service import StarsService
from bot.middlewares.i18n import JsonI18n
from db.dal import payment_dal, user_billing_dal

router = Router(name="user_subscription_payments_router")


def _parse_months_and_price(payload: str) -> Optional[Tuple[int, float]]:
    try:
        months_str, price_str = payload.split(":")
        return int(months_str), float(price_str)
    except (ValueError, IndexError):
        return None


def _format_saved_payment_method_title(get_text, network: Optional[str], last4: Optional[str], is_default: bool) -> str:
    def _is_yoomoney_network(name: Optional[str]) -> bool:
        s = (name or "").lower()
        return "yoomoney" in s or "yoo money" in s or "yoo-money" in s

    def _extract_last4(text: str) -> Optional[str]:
        digits = "".join(ch for ch in text if ch.isdigit())
        return digits[-4:] if len(digits) >= 4 else None

    if _is_yoomoney_network(network):
        inferred_last4 = last4 or (_extract_last4(network or "") or "****")
        title = get_text("payment_method_wallet_title", last4=inferred_last4)
    elif last4:
        network_name = network or get_text("payment_network_card")
        title = get_text("payment_method_card_title", network=network_name, last4=last4)
    else:
        network_name = network or get_text("payment_network_generic")
        title = get_text("payment_method_generic_title", network=network_name)
    return f"⭐ {title}" if is_default else title

@router.callback_query(F.data.startswith("pay_platega:"))
async def pay_platega_callback_handler(cb: types.CallbackQuery, settings: Settings, i18n_data: dict, session: AsyncSession, platega_service: PlategaService):
    get_text = ...  # как у тебя
    try:
        await cb.answer()
    except Exception:
        pass

    raw = cb.data or ""
    logging.warning("Platega click: raw_cb_data=%r bytes=%s", raw, len(raw.encode("utf-8")))

    # Снимаем префикс и парсим
    try:
        payload = raw.split(":", 1)[1]
    except Exception:
        await _platega_reply_parse_error(cb, get_text)
        return

    months, price, key = parse_platega_payload(f"pay_platega:{payload}")
    if months is None and key is None:
        # ни один формат не подошёл
        await _platega_reply_parse_error(cb, get_text)
        return

    # если формат с ключом — достанем контекст из стораджа (если ты так делаешь)
    if key is not None:
        try:
            # пример: ctx = await load_payment_context(key)  # реализуй по своему
            ctx = None
            if not ctx:
                raise RuntimeError("missing ctx")
            months = int(ctx["months"])
            price = float(ctx["price"])
        except Exception:
            logging.error("Platega: no context for key=%r (cb=%r)", key, raw)
            await _platega_reply_parse_error(cb, get_text)
            return

    # к этому моменту months и price гарантированно заданы
    user_id = cb.from_user.id
    description = get_text("payment_description_subscription", months=months)

    payment_url: Optional[str] = None
    try:
        payment_url = await platega_service.create_invoice(
            session=session,
            user_id=user_id,
            months=months,
            amount_rub=price,
            description=description,
        )
    except Exception as e:
        logging.exception("Platega: create_invoice failed: %s", e)

    if not payment_url:
        try:
            await cb.message.edit_text(
                get_text("error_payment_gateway"),
                reply_markup=get_back_to_main_menu_markup(),
            )
        except Exception:
            pass
        return

    try:
        await cb.message.edit_text(
            get_text("payment_link_message", months=months),
            reply_markup=get_payment_url_keyboard(
                payment_url,
                i18n_data.get("current_language", settings.DEFAULT_LANGUAGE),
                i18n_data.get("i18n_instance"),
                back_callback=f"subscribe_period:{months}",
                back_text_key="back_to_payment_methods_button",
            ),
            disable_web_page_preview=False,
        )
    except Exception:
        try:
            await cb.message.answer(
                get_text("payment_link_message", months=months),
                reply_markup=get_payment_url_keyboard(
                    payment_url,
                    i18n_data.get("current_language", settings.DEFAULT_LANGUAGE),
                    i18n_data.get("i18n_instance"),
                    back_callback=f"subscribe_period:{months}",
                    back_text_key="back_to_payment_methods_button",
                ),
                disable_web_page_preview=False,
            )
        except Exception:
            pass

async def _platega_reply_parse_error(cb: types.CallbackQuery, get_text):
    logging.error("Platega: cannot parse callback_data: %r", cb.data)
    # Можно просто alert, либо перевести на выбор способов оплаты
    try:
        await cb.answer(get_text("error_occurred_try_again"), show_alert=True)
    except Exception:
        pass
    try:
        await cb.message.edit_text(
            get_text("error_occurred_try_again"),
            reply_markup=get_back_to_main_menu_markup(),
        )
    except Exception:
        pass

async def _initiate_yk_payment(
    callback: types.CallbackQuery,
    *,
    settings: Settings,
    session: AsyncSession,
    yookassa_service: YooKassaService,
    i18n: Optional[JsonI18n],
    current_lang: str,
    get_text,
    user_id: int,
    months: int,
    price_rub: float,
    currency_code_for_yk: str,
    save_payment_method: bool,
    back_callback: str,
    payment_method_id: Optional[str] = None,
    selected_method_internal_id: Optional[int] = None,
) -> bool:
    """Create payment record and initiate YooKassa payment (new card or saved card)."""
    if not callback.message:
        return False

    payment_description = get_text("payment_description_subscription", months=months)
    payment_record_data = {
        "user_id": user_id,
        "amount": price_rub,
        "currency": currency_code_for_yk,
        "status": "pending_yookassa",
        "description": payment_description,
        "subscription_duration_months": months,
    }

    db_payment_record = None
    try:
        db_payment_record = await payment_dal.create_payment_record(session, payment_record_data)
        await session.commit()
        logging.info(
            f"Payment record {db_payment_record.payment_id} created for user {user_id} with status 'pending_yookassa'."
        )
    except Exception as e_db_payment:
        await session.rollback()
        logging.error(
            f"Failed to create payment record in DB for user {user_id}: {e_db_payment}",
            exc_info=True,
        )
        try:
            await callback.message.edit_text(get_text("error_creating_payment_record"))
        except Exception:
            pass
        return False

    if not db_payment_record:
        try:
            await callback.message.edit_text(get_text("error_creating_payment_record"))
        except Exception:
            pass
        return False

    yookassa_metadata = {
        "user_id": str(user_id),
        "subscription_months": str(months),
        "payment_db_id": str(db_payment_record.payment_id),
    }
    if payment_method_id:
        yookassa_metadata["used_saved_payment_method_id"] = payment_method_id

    receipt_email_for_yk = settings.YOOKASSA_DEFAULT_RECEIPT_EMAIL

    payment_response_yk = await yookassa_service.create_payment(
        amount=price_rub,
        currency=currency_code_for_yk,
        description=payment_description,
        metadata=yookassa_metadata,
        receipt_email=receipt_email_for_yk,
        save_payment_method=save_payment_method,
        payment_method_id=payment_method_id,
    )

    if payment_response_yk and payment_response_yk.get("confirmation_url"):
        pm = payment_response_yk.get("payment_method")
        try:
            if pm and pm.get("id"):
                pm_type = pm.get("type")
                title = pm.get("title")
                card = pm.get("card") or {}
                account_number = pm.get("account_number") or pm.get("account")
                if isinstance(card, dict) and (pm_type or "").lower() in {"bank_card", "bank-card", "card"}:
                    display_network = card.get("card_type") or title or "Card"
                    display_last4 = card.get("last4")
                elif (pm_type or "").lower() in {"yoo_money", "yoomoney", "yoo-money", "wallet"}:
                    display_network = "YooMoney"
                    display_last4 = (
                        account_number[-4:]
                        if isinstance(account_number, str) and len(account_number) >= 4
                        else None
                    )
                else:
                    display_network = title or (pm_type.upper() if pm_type else "Payment method")
                    display_last4 = None
                await user_billing_dal.upsert_yk_payment_method(
                    session,
                    user_id=user_id,
                    payment_method_id=pm["id"],
                    card_last4=display_last4,
                    card_network=display_network,
                )
                try:
                    await user_billing_dal.upsert_user_payment_method(
                        session,
                        user_id=user_id,
                        provider_payment_method_id=pm["id"],
                        provider="yookassa",
                        card_last4=display_last4,
                        card_network=display_network,
                        set_default=save_payment_method,
                    )
                except Exception:
                    pass
                await session.commit()
        except Exception:
            await session.rollback()
            logging.exception("Failed to save YooKassa payment method preliminarily")
        try:
            await payment_dal.update_payment_status_by_db_id(
                session,
                payment_db_id=db_payment_record.payment_id,
                new_status=payment_response_yk.get("status", "pending"),
                yk_payment_id=payment_response_yk.get("id"),
            )
            if selected_method_internal_id is not None:
                try:
                    await user_billing_dal.set_user_default_payment_method(
                        session, user_id, selected_method_internal_id
                    )
                except Exception:
                    logging.exception("Failed to set default payment method after initiating payment")
            await session.commit()
        except Exception as e_db_update_ykid:
            await session.rollback()
            logging.error(
                f"Failed to update payment record {db_payment_record.payment_id} with YK ID: {e_db_update_ykid}",
                exc_info=True,
            )
            try:
                await callback.message.edit_text(get_text("error_payment_gateway_link_failed"))
            except Exception:
                pass
            return False

        try:
            await callback.message.edit_text(
                get_text(key="payment_link_message", months=months),
                reply_markup=get_payment_url_keyboard(
                    payment_response_yk["confirmation_url"],
                    current_lang,
                    i18n,
                    back_callback=back_callback,
                    back_text_key="back_to_payment_methods_button",
                ),
                disable_web_page_preview=False,
            )
        except Exception as e_edit:
            logging.warning(
                f"Edit message for payment link failed: {e_edit}. Sending new one."
            )
            try:
                await callback.message.answer(
                    get_text(key="payment_link_message", months=months),
                    reply_markup=get_payment_url_keyboard(
                        payment_response_yk["confirmation_url"],
                        current_lang,
                        i18n,
                        back_callback=back_callback,
                        back_text_key="back_to_payment_methods_button",
                    ),
                    disable_web_page_preview=False,
                )
            except Exception:
                pass
        return True

    if payment_response_yk and payment_method_id:
        status_to_store = payment_response_yk.get("status", "pending")
        try:
            await payment_dal.update_payment_status_by_db_id(
                session,
                payment_db_id=db_payment_record.payment_id,
                new_status=status_to_store,
                yk_payment_id=payment_response_yk.get("id"),
            )
            if selected_method_internal_id is not None:
                try:
                    await user_billing_dal.set_user_default_payment_method(
                        session, user_id, selected_method_internal_id
                    )
                except Exception:
                    logging.exception("Failed to set default payment method after saved-card payment start")
            await session.commit()
        except Exception as e_db_update_saved:
            await session.rollback()
            logging.error(
                f"Failed to update saved-card payment record {db_payment_record.payment_id}: {e_db_update_saved}",
                exc_info=True,
            )
            try:
                await callback.message.edit_text(get_text("error_payment_gateway"))
            except Exception:
                pass
            return False

        message_text = get_text("yookassa_autopay_charge_initiated")
        try:
            await callback.message.edit_text(
                message_text,
                reply_markup=get_back_to_main_menu_markup(current_lang, i18n),
            )
        except Exception as e_edit:
            logging.warning(f"Failed to notify about saved-card charge start: {e_edit}")
            try:
                await callback.message.answer(
                    message_text,
                    reply_markup=get_back_to_main_menu_markup(current_lang, i18n),
                )
            except Exception:
                pass
        return True

    try:
        await payment_dal.update_payment_status_by_db_id(
            session, db_payment_record.payment_id, "failed_creation"
        )
        await session.commit()
    except Exception as e_db_fail_create:
        await session.rollback()
        logging.error(
            f"Additionally failed to update payment record to 'failed_creation': {e_db_fail_create}",
            exc_info=True,
        )
    logging.error(
        f"Failed to create payment in YooKassa for user {user_id}, payment_db_id {db_payment_record.payment_id}. Response: {payment_response_yk}"
    )
    try:
        await callback.message.edit_text(get_text("error_payment_gateway"))
    except Exception:
        pass
    return False


@router.callback_query(F.data.startswith("subscribe_period:"))
async def select_subscription_period_callback_handler(callback: types.CallbackQuery, settings: Settings, i18n_data: dict, session: AsyncSession):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    if not i18n or not callback.message:
        try:
            await callback.answer(get_text("error_occurred_try_again"), show_alert=True)
        except Exception:
            pass
        return

    try:
        months = int(callback.data.split(":")[-1])
    except (ValueError, IndexError):
        logging.error(f"Invalid subscription period in callback_data: {callback.data}")
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    price_rub = settings.subscription_options.get(months)
    if price_rub is None:
        logging.error(
            f"Price not found for {months} months subscription period in settings.subscription_options."
        )
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    currency_symbol_val = settings.DEFAULT_CURRENCY_SYMBOL
    text_content = get_text("choose_payment_method")
    tribute_url = settings.tribute_payment_links.get(months)
    stars_price = settings.stars_subscription_options.get(months)
    reply_markup = get_payment_method_keyboard(
        months,
        price_rub,
        tribute_url,
        stars_price,
        currency_symbol_val,
        current_lang,
        i18n,
        settings,
    )

    try:
        await callback.message.edit_text(text_content, reply_markup=reply_markup)
    except Exception as e_edit:
        logging.warning(
            f"Edit message for payment method selection failed: {e_edit}. Sending new one."
        )
        await callback.message.answer(text_content, reply_markup=reply_markup)
    try:
        await callback.answer()
    except Exception:
        pass


@router.callback_query(F.data.startswith("pay_yk:"))
async def pay_yk_callback_handler(callback: types.CallbackQuery, settings: Settings, i18n_data: dict, yookassa_service: YooKassaService, session: AsyncSession):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    if not i18n or not callback.message:
        try:
            await callback.answer(get_text("error_occurred_try_again"), show_alert=True)
        except Exception:
            pass
        return

    if not yookassa_service or not yookassa_service.configured:
        logging.error("YooKassa service is not configured or unavailable.")
        target_msg_edit = callback.message
        await target_msg_edit.edit_text(get_text("payment_service_unavailable"))
        try:
            await callback.answer(get_text("payment_service_unavailable_alert"), show_alert=True)
        except Exception:
            pass
        return

    try:
        _, data_payload = callback.data.split(":", 1)
    except ValueError:
        logging.error(f"Invalid pay_yk data in callback: {callback.data}")
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    parsed = _parse_months_and_price(data_payload)
    if not parsed:
        logging.error(f"Invalid pay_yk payload structure: {callback.data}")
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    months, price_rub = parsed
    user_id = callback.from_user.id
    currency_code_for_yk = "RUB"
    autopay_enabled = bool(getattr(settings, 'YOOKASSA_AUTOPAYMENTS_ENABLED', False))
    saved_methods: List = []
    if autopay_enabled:
        try:
            saved_methods = await user_billing_dal.list_user_payment_methods(
                session, user_id, provider="yookassa"
            )
        except Exception as e_list:
            logging.exception(f"Failed to load saved payment methods for user {user_id}: {e_list}")
            saved_methods = []

    if autopay_enabled and saved_methods:
        try:
            await callback.message.edit_text(
                get_text("yookassa_autopay_flow_prompt"),
                reply_markup=get_yk_autopay_choice_keyboard(
                    months,
                    price_rub,
                    current_lang,
                    i18n,
                    has_saved_cards=True,
                ),
            )
        except Exception as e_edit:
            logging.warning(f"Failed to show autopay choice: {e_edit}. Sending new message.")
            try:
                await callback.message.answer(
                    get_text("yookassa_autopay_flow_prompt"),
                    reply_markup=get_yk_autopay_choice_keyboard(
                        months,
                        price_rub,
                        current_lang,
                        i18n,
                        has_saved_cards=True,
                    ),
                )
            except Exception:
                pass
        try:
            await callback.answer()
        except Exception:
            pass
        return

    await _initiate_yk_payment(
        callback,
        settings=settings,
        session=session,
        yookassa_service=yookassa_service,
        i18n=i18n,
        current_lang=current_lang,
        get_text=get_text,
        user_id=user_id,
        months=months,
        price_rub=price_rub,
        currency_code_for_yk=currency_code_for_yk,
        save_payment_method=autopay_enabled,
        back_callback=f"subscribe_period:{months}",
    )
    try:
        await callback.answer()
    except Exception:
        pass


@router.callback_query(F.data.startswith("pay_yk_new:"))
async def pay_yk_new_card_handler(callback: types.CallbackQuery, settings: Settings, i18n_data: dict, yookassa_service: YooKassaService, session: AsyncSession):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    if not i18n or not callback.message:
        try:
            await callback.answer(get_text("error_occurred_try_again"), show_alert=True)
        except Exception:
            pass
        return

    if not yookassa_service or not yookassa_service.configured:
        logging.error("YooKassa service unavailable for pay_yk_new.")
        try:
            await callback.answer(get_text("payment_service_unavailable_alert"), show_alert=True)
        except Exception:
            pass
        try:
            await callback.message.edit_text(get_text("payment_service_unavailable"))
        except Exception:
            pass
        return

    try:
        _, data_payload = callback.data.split(":", 1)
    except ValueError:
        logging.error(f"Invalid pay_yk_new data in callback: {callback.data}")
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    parsed = _parse_months_and_price(data_payload)
    if not parsed:
        logging.error(f"Invalid pay_yk_new payload structure: {callback.data}")
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    months, price_rub = parsed
    user_id = callback.from_user.id
    currency_code_for_yk = "RUB"
    autopay_enabled = bool(getattr(settings, 'YOOKASSA_AUTOPAYMENTS_ENABLED', False))

    await _initiate_yk_payment(
        callback,
        settings=settings,
        session=session,
        yookassa_service=yookassa_service,
        i18n=i18n,
        current_lang=current_lang,
        get_text=get_text,
        user_id=user_id,
        months=months,
        price_rub=price_rub,
        currency_code_for_yk=currency_code_for_yk,
        save_payment_method=autopay_enabled,
        back_callback=f"subscribe_period:{months}",
    )
    try:
        await callback.answer()
    except Exception:
        pass


@router.callback_query(F.data.startswith("pay_yk_saved_list:"))
async def pay_yk_saved_list_handler(callback: types.CallbackQuery, settings: Settings, i18n_data: dict, yookassa_service: YooKassaService, session: AsyncSession):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    if not i18n or not callback.message:
        try:
            await callback.answer(get_text("error_occurred_try_again"), show_alert=True)
        except Exception:
            pass
        return

    autopay_enabled = bool(getattr(settings, 'YOOKASSA_AUTOPAYMENTS_ENABLED', False))
    if not autopay_enabled:
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    try:
        _, data_payload = callback.data.split(":", 1)
    except ValueError:
        logging.error(f"Invalid pay_yk_saved_list data: {callback.data}")
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    parts = data_payload.split(":")
    if len(parts) < 2:
        logging.error(f"pay_yk_saved_list payload missing components: {callback.data}")
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    try:
        months = int(parts[0])
        price_rub = float(parts[1])
        page = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        logging.error(f"pay_yk_saved_list payload parsing error: {callback.data}")
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    user_id = callback.from_user.id
    try:
        saved_methods = await user_billing_dal.list_user_payment_methods(
            session, user_id, provider="yookassa"
        )
    except Exception as e_list:
        logging.exception(f"Failed to list saved payment methods for user {user_id}: {e_list}")
        saved_methods = []

    if not saved_methods:
        try:
            await callback.message.edit_text(
                get_text("yookassa_autopay_no_saved_cards"),
                reply_markup=get_yk_autopay_choice_keyboard(
                    months,
                    price_rub,
                    current_lang,
                    i18n,
                    has_saved_cards=False,
                ),
            )
        except Exception as e_edit:
            logging.warning(f"Failed to display no-saved-card notice: {e_edit}")
            try:
                await callback.message.answer(
                    get_text("yookassa_autopay_no_saved_cards"),
                    reply_markup=get_yk_autopay_choice_keyboard(
                        months,
                        price_rub,
                        current_lang,
                        i18n,
                        has_saved_cards=False,
                    ),
                )
            except Exception:
                pass
        try:
            await callback.answer()
        except Exception:
            pass
        return

    cards: List[Tuple[str, str]] = []
    for method in saved_methods:
        title = _format_saved_payment_method_title(
            get_text, method.card_network, method.card_last4, method.is_default
        )
        cards.append((str(method.method_id), title))

    per_page = 5
    max_page = max(0, (len(cards) - 1) // per_page)
    page = max(0, min(page, max_page))

    try:
        await callback.message.edit_text(
            get_text("yookassa_autopay_choose_saved_card"),
            reply_markup=get_yk_saved_cards_keyboard(
                cards,
                months,
                price_rub,
                current_lang,
                i18n,
                page=page,
            ),
        )
    except Exception as e_edit:
        logging.warning(f"Failed to display saved card list: {e_edit}")
        try:
            await callback.message.answer(
                get_text("yookassa_autopay_choose_saved_card"),
                reply_markup=get_yk_saved_cards_keyboard(
                    cards,
                    months,
                    price_rub,
                    current_lang,
                    i18n,
                    page=page,
                ),
            )
        except Exception:
            pass
    try:
        await callback.answer()
    except Exception:
        pass


@router.callback_query(F.data.startswith("pay_yk_use_saved:"))
async def pay_yk_use_saved_handler(callback: types.CallbackQuery, settings: Settings, i18n_data: dict, yookassa_service: YooKassaService, session: AsyncSession):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    if not i18n or not callback.message:
        try:
            await callback.answer(get_text("error_occurred_try_again"), show_alert=True)
        except Exception:
            pass
        return

    autopay_enabled = bool(getattr(settings, 'YOOKASSA_AUTOPAYMENTS_ENABLED', False))
    if not autopay_enabled:
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    if not yookassa_service or not yookassa_service.configured:
        logging.error("YooKassa service unavailable for pay_yk_use_saved.")
        try:
            await callback.answer(get_text("payment_service_unavailable_alert"), show_alert=True)
        except Exception:
            pass
        try:
            await callback.message.edit_text(get_text("payment_service_unavailable"))
        except Exception:
            pass
        return

    try:
        _, data_payload = callback.data.split(":", 1)
    except ValueError:
        logging.error(f"Invalid pay_yk_use_saved data: {callback.data}")
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    parts = data_payload.split(":")
    if len(parts) < 3:
        logging.error(f"pay_yk_use_saved payload missing components: {callback.data}")
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    try:
        months = int(parts[0])
        price_rub = float(parts[1])
    except (ValueError, IndexError):
        logging.error(f"pay_yk_use_saved months/price parsing error: {callback.data}")
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    method_identifier = parts[2]
    user_id = callback.from_user.id

    try:
        saved_methods = await user_billing_dal.list_user_payment_methods(
            session, user_id, provider="yookassa"
        )
    except Exception as e_list:
        logging.exception(f"Failed to list saved payment methods for user {user_id}: {e_list}")
        saved_methods = []

    selected_method = None
    for method in saved_methods:
        if method_identifier.isdigit():
            if method.method_id == int(method_identifier):
                selected_method = method
                break
        if method.provider_payment_method_id == method_identifier:
            selected_method = method
            break

    if not selected_method:
        logging.warning(f"Selected payment method not found for user {user_id}: {method_identifier}")
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    currency_code_for_yk = "RUB"

    await _initiate_yk_payment(
        callback,
        settings=settings,
        session=session,
        yookassa_service=yookassa_service,
        i18n=i18n,
        current_lang=current_lang,
        get_text=get_text,
        user_id=user_id,
        months=months,
        price_rub=price_rub,
        currency_code_for_yk=currency_code_for_yk,
        save_payment_method=False,
        back_callback=f"pay_yk_saved_list:{months}:{price_rub}",
        payment_method_id=selected_method.provider_payment_method_id,
        selected_method_internal_id=selected_method.method_id,
    )
    try:
        await callback.answer()
    except Exception:
        pass


@router.callback_query(F.data.startswith("pay_fk:"))
async def pay_fk_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    freekassa_service: FreeKassaService,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    if not i18n or not callback.message:
        try:
            await callback.answer(get_text("error_occurred_try_again"), show_alert=True)
        except Exception:
            pass
        return

    if not freekassa_service or not freekassa_service.configured:
        logging.error("FreeKassa service is not configured or unavailable.")
        try:
            await callback.answer(get_text("payment_service_unavailable_alert"), show_alert=True)
        except Exception:
            pass
        try:
            await callback.message.edit_text(get_text("payment_service_unavailable"))
        except Exception:
            pass
        return

    try:
        _, data_payload = callback.data.split(":", 1)
        months_str, price_str = data_payload.split(":")
        months = int(months_str)
        price_rub = float(price_str)
    except (ValueError, IndexError):
        logging.error(f"Invalid pay_fk data in callback: {callback.data}")
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    user_id = callback.from_user.id
    payment_description = get_text("payment_description_subscription", months=months)
    currency_code = getattr(freekassa_service, "default_currency", None) or settings.DEFAULT_CURRENCY_SYMBOL or "RUB"

    payment_record_payload = {
        "user_id": user_id,
        "amount": price_rub,
        "currency": currency_code,
        "status": "pending_freekassa",
        "description": payment_description,
        "subscription_duration_months": months,
        "provider": "freekassa",
    }

    try:
        payment_record = await payment_dal.create_payment_record(session, payment_record_payload)
        await session.commit()
    except Exception as e_db_create:
        await session.rollback()
        logging.error(
            f"FreeKassa: failed to create payment record for user {user_id}: {e_db_create}",
            exc_info=True,
        )
        try:
            await callback.message.edit_text(get_text("error_creating_payment_record"))
        except Exception:
            pass
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    success, response_data = await freekassa_service.create_order(
        payment_db_id=payment_record.payment_id,
        user_id=payment_record.user_id,
        months=months,
        amount=price_rub,
        currency=freekassa_service.default_currency,
        method_code=44,
        ip_address=freekassa_service.server_ip,
        extra_params={
            "us_method": 44,
        },
    )

    if success:
        location = response_data.get("location")
        order_hash = response_data.get("orderHash")
        order_id_api = response_data.get("orderId")
        provider_identifier = order_hash or order_id_api

        if provider_identifier:
            try:
                await payment_dal.update_provider_payment_and_status(
                    session,
                    payment_record.payment_id,
                    str(provider_identifier),
                    payment_record.status,
                )
                await session.commit()
            except Exception as e_status:
                await session.rollback()
                logging.error(
                    f"FreeKassa: failed to store provider order id for payment {payment_record.payment_id}: {e_status}",
                    exc_info=True,
                )

        if location:
            order_identifier_display = str(order_id_api or provider_identifier or payment_record.payment_id)
            order_info_text = get_text(
                "free_kassa_order_info",
                order_id=order_identifier_display,
                date=datetime.now().strftime("%Y-%m-%d"),
            )
            try:
                await callback.message.edit_text(
                    f"{order_info_text}\n\n" + get_text(key="payment_link_message", months=months),
                    reply_markup=get_payment_url_keyboard(
                        location,
                        current_lang,
                        i18n,
                        back_callback=f"subscribe_period:{months}",
                        back_text_key="back_to_payment_methods_button",
                    ),
                    disable_web_page_preview=False,
                )
            except Exception as e_edit:
                logging.warning(f"FreeKassa: failed to display payment link ({e_edit}), sending new message.")
                try:
                    await callback.message.answer(
                        f"{order_info_text}\n\n" + get_text(key="payment_link_message", months=months),
                        reply_markup=get_payment_url_keyboard(
                            location,
                            current_lang,
                            i18n,
                            back_callback=f"subscribe_period:{months}",
                            back_text_key="back_to_payment_methods_button",
                        ),
                        disable_web_page_preview=False,
                    )
                except Exception:
                    pass
            try:
                await callback.answer()
            except Exception:
                pass
            return

        logging.error(
            "FreeKassa: create_order succeeded but no payment link returned for payment %s. Response: %s",
            payment_record.payment_id,
            response_data,
        )
    else:
        logging.error(
            "FreeKassa: create_order failed for payment %s with response %s",
            payment_record.payment_id,
            response_data,
        )

    try:
        await payment_dal.update_payment_status_by_db_id(
            session,
            payment_record.payment_id,
            "failed_creation",
        )
        await session.commit()
    except Exception as e_status:
        await session.rollback()
        logging.error(f"FreeKassa: failed to mark payment {payment_record.payment_id} as failed_creation: {e_status}", exc_info=True)

    try:
        await callback.message.edit_text(get_text("error_payment_gateway"))
    except Exception:
        pass
    try:
        await callback.answer(get_text("error_payment_gateway"), show_alert=True)
    except Exception:
        pass


@router.callback_query(F.data.startswith("pay_crypto:"))
async def pay_crypto_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    cryptopay_service: CryptoPayService,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = (lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key)

    if not i18n or not callback.message:
        try:
            await callback.answer(get_text("error_occurred_try_again"), show_alert=True)
        except Exception:
            pass
        return

    if not cryptopay_service or not getattr(cryptopay_service, "configured", False):
        try:
            await callback.answer(get_text("payment_service_unavailable_alert"), show_alert=True)
        except Exception:
            pass
        return

    try:
        _, data_payload = callback.data.split(":", 1)
        months_str, price_str = data_payload.split(":")
        months = int(months_str)
        price_amount = float(price_str)
    except (ValueError, IndexError):
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    user_id = callback.from_user.id
    payment_description = get_text("payment_description_subscription", months=months)

    invoice_url = await cryptopay_service.create_invoice(
        session=session,
        user_id=user_id,
        months=months,
        amount=price_amount,
        description=payment_description,
    )

    if invoice_url:
        try:
            await callback.message.edit_text(
                get_text(key="payment_link_message", months=months),
                reply_markup=get_payment_url_keyboard(
                    invoice_url,
                    current_lang,
                    i18n,
                    back_callback=f"subscribe_period:{months}",
                    back_text_key="back_to_payment_methods_button",
                ),
                disable_web_page_preview=False,
            )
        except Exception:
            try:
                await callback.message.answer(
                    get_text(key="payment_link_message", months=months),
                    reply_markup=get_payment_url_keyboard(
                        invoice_url,
                        current_lang,
                        i18n,
                        back_callback=f"subscribe_period:{months}",
                        back_text_key="back_to_payment_methods_button",
                    ),
                    disable_web_page_preview=False,
                )
            except Exception:
                pass
        try:
            await callback.answer()
        except Exception:
            pass
        return

    try:
        await callback.answer(get_text("error_payment_gateway"), show_alert=True)
    except Exception:
        pass


@router.callback_query(F.data.startswith("pay_stars:"))
async def pay_stars_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    stars_service: StarsService,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = (lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key)

    if not i18n or not callback.message:
        try:
            await callback.answer(get_text("error_occurred_try_again"), show_alert=True)
        except Exception:
            pass
        return

    if not settings.STARS_ENABLED:
        try:
            await callback.answer(get_text("payment_service_unavailable_alert"), show_alert=True)
        except Exception:
            pass
        return

    try:
        _, data_payload = callback.data.split(":", 1)
        months_str, stars_price_str = data_payload.split(":")
        months = int(months_str)
        stars_price = int(stars_price_str)
    except (ValueError, IndexError):
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    user_id = callback.from_user.id
    payment_description = get_text("payment_description_subscription", months=months)

    payment_db_id = await stars_service.create_invoice(
        session=session,
        user_id=user_id,
        months=months,
        stars_price=stars_price,
        description=payment_description,
    )

    if payment_db_id:
        try:
            await callback.message.edit_text(
                get_text("payment_invoice_sent_message", months=months),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text=get_text("back_to_payment_methods_button"),
                        callback_data=f"subscribe_period:{months}",
                    )]
                ]),
            )
        except Exception as e_edit:
            logging.warning(f"Stars payment: failed to show invoice info message ({e_edit})")
        try:
            await callback.answer()
        except Exception:
            pass
        return

    try:
        await callback.answer(get_text("error_payment_gateway"), show_alert=True)
    except Exception:
        pass


@router.pre_checkout_query()
async def handle_pre_checkout_query(query: types.PreCheckoutQuery):
    try:
        await query.answer(ok=True)
    except Exception:
        # Nothing else to do here; Telegram will show an error if not answered
        pass


@router.message(F.successful_payment)
async def handle_successful_stars_payment(
    message: types.Message,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    stars_service: StarsService,
):
    payload = (message.successful_payment.invoice_payload
               if message and message.successful_payment else "")
    try:
        payment_db_id_str, months_str = (payload or "").split(":", 1)
        payment_db_id = int(payment_db_id_str)
        months = int(months_str)
    except Exception:
        return

    stars_amount = int(message.successful_payment.total_amount) if message.successful_payment else 0
    await stars_service.process_successful_payment(
        session=session,
        message=message,
        payment_db_id=payment_db_id,
        months=months,
        stars_amount=stars_amount,
        i18n_data=i18n_data,
    )

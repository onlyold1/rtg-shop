from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardButton
from aiogram.types import InlineKeyboardMarkup, WebAppInfo
from typing import Dict, Optional, List, Tuple

from config.settings import Settings


def get_main_menu_inline_keyboard(
        lang: str,
        i18n_instance,
        settings: Settings,
        show_trial_button: bool = False) -> InlineKeyboardMarkup:
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()

    if show_trial_button and settings.TRIAL_ENABLED:
        builder.row(
            InlineKeyboardButton(text=_(key="menu_activate_trial_button"),
                                 callback_data="main_action:request_trial"))

    builder.row(
        InlineKeyboardButton(text=_(key="menu_subscribe_inline"),
                             callback_data="main_action:subscribe"))
    if settings.SUBSCRIPTION_MINI_APP_URL:
        builder.row(
            InlineKeyboardButton(
                text=_(key="menu_my_subscription_inline"),
                web_app=WebAppInfo(url=settings.SUBSCRIPTION_MINI_APP_URL),
            )
        )
    else:
        builder.row(
            InlineKeyboardButton(
                text=_(key="menu_my_subscription_inline"),
                callback_data="main_action:my_subscription",
            )
        )

    referral_button = InlineKeyboardButton(
        text=_(key="menu_referral_inline"),
        callback_data="main_action:referral")
    promo_button = InlineKeyboardButton(
        text=_(key="menu_apply_promo_button"),
        callback_data="main_action:apply_promo")
    builder.row(referral_button, promo_button)

    language_button = InlineKeyboardButton(
        text=_(key="menu_language_settings_inline"),
        callback_data="main_action:language")
    status_button_list = []
    if settings.SERVER_STATUS_URL:
        status_button_list.append(
            InlineKeyboardButton(text=_(key="menu_server_status_button"),
                                 url=settings.SERVER_STATUS_URL))

    if status_button_list:
        builder.row(language_button, *status_button_list)
    else:
        builder.row(language_button)

    if settings.SUPPORT_LINK:
        builder.row(
            InlineKeyboardButton(text=_(key="menu_support_button"),
                                 url=settings.SUPPORT_LINK))

    terms_urls_configured = [
        url for url in [
            settings.TERMS_OF_SERVICE_URL_PRIMARY,
            settings.TERMS_OF_SERVICE_URL_SECONDARY,
        ] if url
    ]

    if terms_urls_configured:
        builder.row(
            InlineKeyboardButton(
                text=_(key="menu_terms_button"),
                callback_data="main_action:terms",
            ))
    elif settings.TERMS_OF_SERVICE_URL:
        builder.row(
            InlineKeyboardButton(text=_(key="menu_terms_button"),
                                 url=settings.TERMS_OF_SERVICE_URL))

    return builder.as_markup()


def get_language_selection_keyboard(i18n_instance,
                                    current_lang: str) -> InlineKeyboardMarkup:
    _ = lambda key, **kwargs: i18n_instance.gettext(current_lang, key, **kwargs
                                                    )
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🇬🇧 English {'✅' if current_lang == 'en' else ''}",
                   callback_data="set_lang_en")
    builder.button(text=f"🇷🇺 Русский {'✅' if current_lang == 'ru' else ''}",
                   callback_data="set_lang_ru")
    builder.button(text=_(key="back_to_main_menu_button"),
                   callback_data="main_action:back_to_main")
    builder.adjust(1)
    return builder.as_markup()


def get_terms_selection_keyboard(
        lang: str,
        i18n_instance,
        settings: Settings) -> Optional[InlineKeyboardMarkup]:
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    has_terms_links = False
   
    if settings.TERMS_OF_SERVICE_URL_PRIMARY:
        builder.button(text=_(key="terms_primary_button"),
                       url=settings.TERMS_OF_SERVICE_URL_PRIMARY)
        has_terms_links = True

    if settings.TERMS_OF_SERVICE_URL_SECONDARY:
        builder.button(text=_(key="terms_secondary_button"),
                       url=settings.TERMS_OF_SERVICE_URL_SECONDARY)
        has_terms_links = True

    if has_terms_links:
        builder.button(text=_(key="back_to_main_menu_button"),
                       callback_data="main_action:back_to_main")
        builder.adjust(1)
        return builder.as_markup()

    return None


def get_trial_confirmation_keyboard(lang: str,
                                    i18n_instance) -> InlineKeyboardMarkup:
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    builder.button(text=_(key="trial_confirm_activate_button"),
                   callback_data="trial_action:confirm_activate")
    builder.button(text=_(key="cancel_button"),
                   callback_data="main_action:back_to_main")
    builder.adjust(1)
    return builder.as_markup()


def get_subscription_options_keyboard(subscription_options: Dict[
    int, Optional[int]], currency_symbol_val: str, lang: str,
                                      i18n_instance) -> InlineKeyboardMarkup:
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    if subscription_options:
        for months, price in subscription_options.items():
            if price is not None:
                button_text = _("subscribe_for_months_button",
                                months=months,
                                price=price,
                                currency_symbol=currency_symbol_val)
                builder.button(text=button_text,
                               callback_data=f"subscribe_period:{months}")
        builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text=_(key="back_to_main_menu_button"),
                             callback_data="main_action:back_to_main"))
    return builder.as_markup()


def get_payment_method_keyboard(months: int, price: float,
                                tribute_url: Optional[str],
                                stars_price: Optional[int],
                                currency_symbol_val: str, lang: str,
                                i18n_instance, settings: Settings) -> InlineKeyboardMarkup:
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    if settings.STARS_ENABLED and stars_price is not None:
        builder.button(text=_("pay_with_stars_button"),
                       callback_data=f"pay_stars:{months}:{stars_price}")
    if settings.FREEKASSA_ENABLED:
        builder.button(text=_("pay_with_sbp_button"),
                       callback_data=f"pay_fk:{months}:{price}")
    if settings.TRIBUTE_ENABLED and tribute_url:
        builder.button(text=_("pay_with_tribute_button"), url=tribute_url)
    if settings.PLATEGA_ENABLED:
        builder.button(text=_("pay_with_platega_button"),
                       callback_data=f"pay_platega:{months}:{price}")
    if settings.YOOKASSA_ENABLED:
        builder.button(text=_("pay_with_yookassa_button"),
                       callback_data=f"pay_yk:{months}:{price}")
    if settings.CRYPTOPAY_ENABLED:
        builder.button(text=_("pay_with_cryptopay_button"),
                       callback_data=f"pay_crypto:{months}:{price}")
    builder.button(text=_(key="cancel_button"),
                   callback_data="main_action:subscribe")
    builder.adjust(1)
    return builder.as_markup()


def get_payment_url_keyboard(payment_url: str,
                             lang: str,
                             i18n_instance,
                             back_callback: Optional[str] = None,
                             back_text_key: str = "back_to_main_menu_button"
                             ) -> InlineKeyboardMarkup:
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    builder.button(text=_(key="pay_button"), url=payment_url)
    if back_callback:
        builder.button(text=_(key=back_text_key), callback_data=back_callback)
    else:
        builder.button(text=_(key="back_to_main_menu_button"),
                       callback_data="main_action:back_to_main")
    builder.adjust(1)
    return builder.as_markup()

def get_yk_autopay_choice_keyboard(
    months: int,
    price: float,
    lang: str,
    i18n_instance,
    has_saved_cards: bool = True,
) -> InlineKeyboardMarkup:
    """Keyboard for choosing between saved card charge or new card payment when auto-renew is enabled."""
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    price_str = str(price)
    if has_saved_cards:
        builder.row(
            InlineKeyboardButton(
                text=_(key="yookassa_autopay_pay_saved_card_button"),
                callback_data=f"pay_yk_saved_list:{months}:{price_str}",
            )
        )
    builder.row(
        InlineKeyboardButton(
            text=_(key="yookassa_autopay_pay_new_card_button"),
            callback_data=f"pay_yk_new:{months}:{price_str}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=_(key="back_to_payment_methods_button"),
            callback_data=f"subscribe_period:{months}",
        )
    )
    return builder.as_markup()


def get_yk_saved_cards_keyboard(
    cards: List[Tuple[str, str]],
    months: int,
    price: float,
    lang: str,
    i18n_instance,
    page: int = 0,
) -> InlineKeyboardMarkup:
    """Paginated keyboard for selecting a saved YooKassa card."""
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    per_page = 5
    total = len(cards)
    start = page * per_page
    end = min(total, start + per_page)
    price_str = str(price)

    for method_id, title in cards[start:end]:
        builder.row(
            InlineKeyboardButton(
                text=title,
                callback_data=f"pay_yk_use_saved:{months}:{price_str}:{method_id}",
            )
        )

    nav_buttons: List[InlineKeyboardButton] = []
    if start > 0:
        nav_buttons.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"pay_yk_saved_list:{months}:{price_str}:{page-1}",
            )
        )
    if end < total:
        nav_buttons.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"pay_yk_saved_list:{months}:{price_str}:{page+1}",
            )
        )
    if nav_buttons:
        builder.row(*nav_buttons)

    builder.row(
        InlineKeyboardButton(
            text=_(key="yookassa_autopay_pay_new_card_button"),
            callback_data=f"pay_yk_new:{months}:{price_str}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=_(key="back_to_autopay_method_choice_button"),
            callback_data=f"pay_yk:{months}:{price_str}",
        )
    )
    return builder.as_markup()


def get_referral_link_keyboard(lang: str,
                               i18n_instance) -> InlineKeyboardMarkup:
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    builder.button(text=_(key="referral_share_message_button"),
                   callback_data="referral_action:share_message")
    builder.button(text=_(key="back_to_main_menu_button"),
                   callback_data="main_action:back_to_main")
    builder.adjust(1)
    return builder.as_markup()


def get_back_to_main_menu_markup(lang: str,
                                 i18n_instance,
                                 callback_data: Optional[str] = None) -> InlineKeyboardMarkup:
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    if callback_data:
        builder.button(text=_(key="back_to_main_menu_button"),
                       callback_data=callback_data)
    else:
        builder.button(text=_(key="back_to_main_menu_button"),
                       callback_data="main_action:back_to_main")
    return builder.as_markup()


def get_subscribe_only_markup(lang: str, i18n_instance) -> InlineKeyboardMarkup:
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    builder.button(text=_(key="menu_subscribe_inline"),
                   callback_data="main_action:subscribe")
    return builder.as_markup()


def get_user_banned_keyboard(support_link: Optional[str], lang: str,
                             i18n_instance) -> Optional[InlineKeyboardMarkup]:
    if not support_link:
        return None
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    builder.button(text=_(key="menu_support_button"), url=support_link)
    return builder.as_markup()

def get_channel_subscription_keyboard(
        lang: str,
        i18n_instance,
        channel_link: Optional[str],
        include_check_button: bool = True) -> Optional[InlineKeyboardMarkup]:
    """
    Return keyboard with buttons to open the required channel and trigger a subscription re-check.
    """
    if i18n_instance is None:
        return None

    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()

    has_buttons = False

    if channel_link:
        builder.button(
            text=_(key="channel_subscription_join_button"),
            url=channel_link,
        )
        has_buttons = True

    if include_check_button:
        builder.button(
            text=_(key="channel_subscription_verify_button"),
            callback_data="channel_subscription:verify",
        )
        has_buttons = True

    if not has_buttons:
        return None

    builder.adjust(1)
    return builder.as_markup()

def get_connect_and_main_keyboard(
        lang: str,
        i18n_instance,
        settings: Settings,
        config_link: Optional[str],
        preserve_message: bool = False) -> InlineKeyboardMarkup:
    """Keyboard with a connect button and a back to main menu button."""
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()

    if settings.SUBSCRIPTION_MINI_APP_URL:
        builder.row(
            InlineKeyboardButton(
                text=_("connect_button"),
                web_app=WebAppInfo(url=settings.SUBSCRIPTION_MINI_APP_URL),
            )
        )
    elif config_link:
        builder.row(
            InlineKeyboardButton(text=_("connect_button"), url=config_link)
        )
    else:
        builder.row(
            InlineKeyboardButton(
                text=_("connect_button"),
                callback_data="main_action:my_subscription",
            )
        )

    back_callback = "main_action:back_to_main_keep" if preserve_message else "main_action:back_to_main"
    builder.row(
        InlineKeyboardButton(
            text=_("back_to_main_menu_button"),
            callback_data=back_callback,
        )
    )

                
    return builder.as_markup()

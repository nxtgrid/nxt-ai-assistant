from nicegui_app import branding


def test_public_brand_contract_is_exact():
    assert branding.PUBLIC_PRODUCT_NAME == "Mini-Grids Assistant"
    assert branding.LOGIN_PROMPT == (
        "Please sign in with your Google account to access Mini-Grids Assistant."
    )
    assert branding.LOGO_FILENAME == "mini_grids_assistant_logo.png"
    assert branding.LOGO_URL == "/assets/mini_grids_assistant_logo.png"
    assert branding.FAVICON_16_FILENAME == "favicon-16.png"
    assert branding.FAVICON_32_FILENAME == "favicon-32.png"
    assert branding.FAVICON_ICO_FILENAME == "favicon.ico"
    assert {
        branding.BRAND_NIGHT,
        branding.BRAND_BLUE,
        branding.BRAND_CANVAS,
        branding.BRAND_WHITE,
        branding.BRAND_MIST,
    } == {"#141824", "#4DA6FF", "#F0F2F6", "#FFFFFF", "#CBD5E1"}

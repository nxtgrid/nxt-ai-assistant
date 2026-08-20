import struct
from pathlib import Path

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


ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"


def _png_ihdr(path: Path) -> tuple[int, int, int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">IIBB", data[16:26])


def test_active_brand_assets_have_expected_binary_contract():
    assert _png_ihdr(ASSETS_DIR / branding.LOGO_FILENAME) == (880, 890, 8, 6)
    assert _png_ihdr(ASSETS_DIR / branding.FAVICON_16_FILENAME) == (16, 16, 8, 6)
    assert _png_ihdr(ASSETS_DIR / branding.FAVICON_32_FILENAME) == (32, 32, 8, 6)
    assert (ASSETS_DIR / branding.FAVICON_ICO_FILENAME).read_bytes()[:4] == b"\x00\x00\x01\x00"

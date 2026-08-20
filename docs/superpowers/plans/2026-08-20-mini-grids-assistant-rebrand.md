# Mini-Grids Assistant Rebrand Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace user-facing Anansi branding with the approved Mini-Grids Assistant name and lightning-tie mark without renaming internal packages, services, routes, or operational identifiers.

**Architecture:** Add one dependency-free Python brand contract for the public name, asset URLs, copy, and existing color tokens. NiceGUI surfaces consume that contract, the standalone Telegram Mini App keeps one explicit HTML title, and checked-in raster derivatives provide deterministic browser assets. Static and binary-contract tests keep public branding separate from internal Anansi identifiers.

**Tech Stack:** Python 3.11, NiceGUI/Quasar, pytest, HTML, PNG/ICO assets, macOS `sips` for deterministic local derivatives

**Spec:** `docs/superpowers/specs/2026-08-20-mini-grids-assistant-rebrand-design.md`

## Global Constraints

- The public product name is exactly `Mini-Grids Assistant`, including capitalization and hyphenation.
- The approved master asset is `/Users/vaibha/Downloads/MiniGridsAssistant.png` (880×890, RGBA, transparent).
- Keep `anansi`, `anansi_app`, deployment/component names, environment variables, routes, storage keys, bot/command names, code comments, and internal engineering terminology unchanged.
- Do not add runtime or build dependencies for the rebrand.
- Preserve the existing 240px sidebar width, bot-status polling, OAuth behavior, RBAC behavior, and navigation order.
- Use `#141824`, `#4DA6FF`, `#F0F2F6`, `#FFFFFF`, and `#CBD5E1` as the complete UI brand palette for changed surfaces.
- Outside the approved reflective logo asset, add no animation, gradient, glow, decorative illustration, or replacement typography.

---

## File Structure

- Create `anansi_app/nicegui_app/branding.py` as the single Python source of truth for public brand strings, asset names/URLs, and shared color tokens.
- Create `anansi_app/tests/test_branding.py` for brand-contract, binary-asset, runtime-reference, and public-copy regression tests.
- Add `anansi_app/assets/mini_grids_assistant_logo.png` as the approved master asset.
- Replace `anansi_app/assets/favicon-16.png`, `anansi_app/assets/favicon-32.png`, and `anansi_app/assets/favicon.ico` with derivatives of the approved mark.
- Delete `anansi_app/assets/anansi_logo.png`, `anansi_app/assets/anansi_logo_nobg.png`, and `anansi_app/assets/anansi_spider.png` after all runtime references move to the new asset.
- Modify `anansi_app/nicegui_app/main.py` for login presentation, public browser title, and favicon selection.
- Modify `anansi_app/nicegui_app/layout.py` for the authenticated sidebar lockup and shared palette consumption.
- Modify `anansi_app/nicegui_app/pages/settings.py` and `anansi_app/nicegui_app/pages/prompts.py` for public product copy.
- Modify `mini_app/index.html` for the Telegram Mini App document title.
- Modify `anansi_app/assets/README.md` and `anansi_app/README.md` to document the internal/public naming boundary and active assets.

---

### Task 1: Establish the Public Brand Contract

**Files:**
- Create: `anansi_app/nicegui_app/branding.py`
- Create: `anansi_app/tests/test_branding.py`

**Interfaces:**
- Consumes: no application modules or runtime dependencies
- Produces: `PUBLIC_PRODUCT_NAME: str`, `LOGIN_PROMPT: str`, `LOGO_FILENAME: str`, `LOGO_URL: str`, `FAVICON_16_FILENAME: str`, `FAVICON_32_FILENAME: str`, `FAVICON_ICO_FILENAME: str`, `BRAND_NIGHT: str`, `BRAND_BLUE: str`, `BRAND_CANVAS: str`, `BRAND_WHITE: str`, and `BRAND_MIST: str`

- [ ] **Step 1: Write the failing public-brand contract test**

Add the following initial content to `anansi_app/tests/test_branding.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" python -m pytest anansi_app/tests/test_branding.py -v
```

Expected: collection fails because `nicegui_app.branding` does not exist.

- [ ] **Step 3: Implement the dependency-free brand contract**

Create `anansi_app/nicegui_app/branding.py` with:

```python
"""Public Mini-Grids Assistant identity for the internally named Anansi app."""

PUBLIC_PRODUCT_NAME = "Mini-Grids Assistant"
LOGIN_PROMPT = f"Please sign in with your Google account to access {PUBLIC_PRODUCT_NAME}."

LOGO_FILENAME = "mini_grids_assistant_logo.png"
LOGO_URL = f"/assets/{LOGO_FILENAME}"
FAVICON_16_FILENAME = "favicon-16.png"
FAVICON_32_FILENAME = "favicon-32.png"
FAVICON_ICO_FILENAME = "favicon.ico"

BRAND_NIGHT = "#141824"
BRAND_BLUE = "#4DA6FF"
BRAND_CANVAS = "#F0F2F6"
BRAND_WHITE = "#FFFFFF"
BRAND_MIST = "#CBD5E1"
```

- [ ] **Step 4: Run the contract test to verify it passes**

Run:

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" python -m pytest anansi_app/tests/test_branding.py -v
```

Expected: `1 passed`.

- [ ] **Step 5: Commit the brand contract**

```bash
git add anansi_app/nicegui_app/branding.py anansi_app/tests/test_branding.py
git commit -m "feat(anansi_app): define Mini-Grids Assistant public brand"
```

---

### Task 2: Install the Approved Logo and Favicon Derivatives

**Files:**
- Create: `anansi_app/assets/mini_grids_assistant_logo.png`
- Modify: `anansi_app/assets/favicon-16.png`
- Modify: `anansi_app/assets/favicon-32.png`
- Modify: `anansi_app/assets/favicon.ico`
- Modify: `anansi_app/tests/test_branding.py`

**Interfaces:**
- Consumes: asset filenames from `nicegui_app.branding`; approved file `/Users/vaibha/Downloads/MiniGridsAssistant.png`
- Produces: one RGBA master at 880×890, RGBA favicons at 16×16 and 32×32, and a valid ICO resource at the canonical static path

- [ ] **Step 1: Add failing binary asset contract tests**

Add `import struct` and `from pathlib import Path` at the top of
`anansi_app/tests/test_branding.py`, then append:

```python
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
```

- [ ] **Step 2: Run the asset tests to verify they fail**

Run:

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" python -m pytest anansi_app/tests/test_branding.py -v
```

Expected: failure reports the missing `mini_grids_assistant_logo.png`.

- [ ] **Step 3: Copy the master and generate deterministic derivatives**

Run from the repository root:

```bash
cp /Users/vaibha/Downloads/MiniGridsAssistant.png anansi_app/assets/mini_grids_assistant_logo.png
sips -z 16 16 anansi_app/assets/mini_grids_assistant_logo.png --out anansi_app/assets/favicon-16.png
sips -z 32 32 anansi_app/assets/mini_grids_assistant_logo.png --out anansi_app/assets/favicon-32.png
sips -s format ico anansi_app/assets/favicon-32.png --out anansi_app/assets/favicon.ico
```

Expected: `file` reports RGBA PNGs at 880×890, 16×16, and 32×32, plus one 32×32 Windows icon resource.

- [ ] **Step 4: Run the asset contract tests to verify they pass**

Run:

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" python -m pytest anansi_app/tests/test_branding.py -v
```

Expected: `2 passed`.

- [ ] **Step 5: Commit the asset replacement**

```bash
git add anansi_app/assets anansi_app/tests/test_branding.py
git commit -m "feat(anansi_app): install Mini-Grids Assistant logo assets"
```

---

### Task 3: Rebrand the Login Page, Sidebar, Browser Title, and Favicon

**Files:**
- Modify: `anansi_app/nicegui_app/main.py:23-60,242-252`
- Modify: `anansi_app/nicegui_app/layout.py:15-20,146-169,175-176`
- Delete: `anansi_app/assets/anansi_logo.png`
- Delete: `anansi_app/assets/anansi_logo_nobg.png`
- Delete: `anansi_app/assets/anansi_spider.png`
- Modify: `anansi_app/tests/test_branding.py`

**Interfaces:**
- Consumes: all public identity, asset URL, and palette constants from `nicegui_app.branding`
- Produces: a dark responsive login canvas, compact authenticated sidebar lockup, `Mini-Grids Assistant` browser title, and new favicon binding; preserves all route/auth/status behavior

- [ ] **Step 1: Add failing runtime wiring tests**

Append to `anansi_app/tests/test_branding.py`:

```python
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_nicegui_shell_consumes_public_brand_contract():
    main_source = (REPO_ROOT / "anansi_app/nicegui_app/main.py").read_text()
    layout_source = (REPO_ROOT / "anansi_app/nicegui_app/layout.py").read_text()

    assert "ui.label(branding.PUBLIC_PRODUCT_NAME)" in main_source
    assert "ui.label(branding.LOGIN_PROMPT)" in main_source
    assert "title=branding.PUBLIC_PRODUCT_NAME" in main_source
    assert "ASSETS_DIR / branding.FAVICON_32_FILENAME" in main_source
    assert "ui.image(branding.LOGO_URL)" in main_source
    assert "ui.image(branding.LOGO_URL)" in layout_source
    assert "ui.label(branding.PUBLIC_PRODUCT_NAME)" in layout_source
    assert "anansi_logo" not in main_source
    assert "anansi_logo" not in layout_source


def test_legacy_product_marks_are_removed():
    for filename in ("anansi_logo.png", "anansi_logo_nobg.png", "anansi_spider.png"):
        assert not (ASSETS_DIR / filename).exists()
```

- [ ] **Step 2: Run the runtime wiring test to verify it fails**

Run:

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" python -m pytest anansi_app/tests/test_branding.py::test_nicegui_shell_consumes_public_brand_contract -v
```

Expected: failure because `main.py` and `layout.py` still contain legacy literals and asset paths.

- [ ] **Step 3: Apply the public brand to `main.py`**

Import `branding` beside `auth` and `layout`. In `login_page`, set the body to
`branding.BRAND_NIGHT` and replace the existing `absolute-center` column with this structure:

```python
ui.query("body").style(f"background-color: {branding.BRAND_NIGHT}")
with (
    ui.column()
    .classes("absolute-center items-center gap-3 text-center")
    .style("width: min(92vw, 28rem);")
):
    logo_path = ASSETS_DIR / branding.LOGO_FILENAME
    if logo_path.exists():
        ui.image(branding.LOGO_URL).classes("w-40 h-40").props(
            'fit=contain alt="Mini-Grids Assistant logo"'
        )
    ui.label(branding.PUBLIC_PRODUCT_NAME).classes("text-h4 text-weight-bold").style(
        f"color: {branding.BRAND_WHITE}"
    )
    ui.label(branding.LOGIN_PROMPT).style(f"color: {branding.BRAND_MIST}")
```

Keep the current denied/OAuth error branches inside the column. Replace the current sign-in
button expression with:

```python
ui.button("Sign in with Google", on_click=lambda: ui.navigate.to("/auth/login")).props(
    "unelevated no-caps"
).style(f"background-color: {branding.BRAND_BLUE}; color: {branding.BRAND_WHITE}")
```

In `create_app`, pass
`title=branding.PUBLIC_PRODUCT_NAME` and
`favicon=str(ASSETS_DIR / branding.FAVICON_32_FILENAME)`.

- [ ] **Step 4: Apply the public brand to `layout.py`**

Import `branding`, replace the three local palette literals with aliases to
`branding.BRAND_NIGHT`, `branding.BRAND_BLUE`, and `branding.BRAND_CANVAS`, and replace
`_render_status_logo`'s current row contents with:

```python
with (
    ui.row()
    .classes("items-center gap-2 w-full no-wrap")
    .style("padding: 0.75rem 0.75rem 0;")
):
    ui.image(branding.LOGO_URL).classes("w-10 h-10 shrink-0").props(
        'fit=contain alt="Mini-Grids Assistant logo"'
    )
    ui.label(branding.PUBLIC_PRODUCT_NAME).classes("text-bold").style(
        "color: #ffffff; font-size: 0.95rem; line-height: 1.1; "
        "max-width: 8.25rem; white-space: normal;"
    )
    dot = ui.element("div").style(
        "width: 10px; height: 10px; min-width: 10px; border-radius: 9999px;"
        f" background-color: {_STATUS_COLORS['down']};"
    )
```

Leave `_refresh`, its two timers, status colors, status tooltips, drawer width, user row, and
navigation code unchanged.

Delete the three legacy assets only after both runtime modules have switched to
`branding.LOGO_URL`:

```bash
git rm anansi_app/assets/anansi_logo.png \
  anansi_app/assets/anansi_logo_nobg.png \
  anansi_app/assets/anansi_spider.png
```

- [ ] **Step 5: Run the focused brand and layout tests**

Run:

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" python -m pytest \
  anansi_app/tests/test_branding.py \
  anansi_app/tests/test_layout.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit the primary UI surfaces**

```bash
git add anansi_app/nicegui_app/main.py anansi_app/nicegui_app/layout.py \
  anansi_app/assets anansi_app/tests/test_branding.py
git commit -m "feat(anansi_app): rebrand login and shell as Mini-Grids Assistant"
```

---

### Task 4: Replace Remaining User-Facing Product Copy

**Files:**
- Modify: `anansi_app/nicegui_app/pages/settings.py:287-290`
- Modify: `anansi_app/nicegui_app/pages/prompts.py:215-220`
- Modify: `mini_app/index.html:6`
- Modify: `anansi_app/tests/test_branding.py`

**Interfaces:**
- Consumes: `branding.PUBLIC_PRODUCT_NAME` in Python; exact public-name literal in standalone HTML
- Produces: consistent user-visible product naming without changing internal Anansi terminology

- [ ] **Step 1: Add failing public-copy tests**

Append to `anansi_app/tests/test_branding.py`:

```python
def test_remaining_user_facing_copy_uses_public_name():
    settings_source = (REPO_ROOT / "anansi_app/nicegui_app/pages/settings.py").read_text()
    prompts_source = (REPO_ROOT / "anansi_app/nicegui_app/pages/prompts.py").read_text()
    mini_app_html = (REPO_ROOT / "mini_app/index.html").read_text()

    assert 'f"Configure {branding.PUBLIC_PRODUCT_NAME} bot behavior and features."' in settings_source
    assert 'f"Every prompt {branding.PUBLIC_PRODUCT_NAME} sends to a model, in one place. "' in prompts_source
    assert "<title>Mini-Grids Assistant</title>" in mini_app_html
    assert "<title>Anansi Mini App</title>" not in mini_app_html
```

- [ ] **Step 2: Run the public-copy test to verify it fails**

Run:

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" python -m pytest anansi_app/tests/test_branding.py::test_remaining_user_facing_copy_uses_public_name -v
```

Expected: the assertions fail against the current public copy.

- [ ] **Step 3: Update the settings and prompts captions**

Import `branding` in both page modules. Render these exact strings:

```python
ui.label(f"Configure {branding.PUBLIC_PRODUCT_NAME} bot behavior and features.")
```

and:

```python
ui.label(
    f"Every prompt {branding.PUBLIC_PRODUCT_NAME} sends to a model, in one place. "
    "Overridable prompts can be edited here without a redeploy; locked prompts are reviewed "
    "and shipped with the app."
).classes("text-caption")
```

Keep the existing headings (`Bot Settings`, `Prompts`) because they describe functions rather
than the product's internal name.

- [ ] **Step 4: Update the standalone Telegram Mini App title**

Change only the `<title>` element in `mini_app/index.html`:

```html
<title>Mini-Grids Assistant</title>
```

Leave JavaScript comments, CSS class names, API paths, and runtime behavior unchanged.

- [ ] **Step 5: Run the public-copy and affected page tests**

Run:

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" python -m pytest \
  anansi_app/tests/test_branding.py \
  anansi_app/tests/test_settings_page.py \
  anansi_app/tests/test_prompts_page.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit the remaining public copy**

```bash
git add mini_app/index.html anansi_app/nicegui_app/pages/settings.py \
  anansi_app/nicegui_app/pages/prompts.py anansi_app/tests/test_branding.py
git commit -m "feat: use Mini-Grids Assistant across public UI copy"
```

---

### Task 5: Document the Naming Boundary and Verify the Finished Rebrand

**Files:**
- Modify: `anansi_app/assets/README.md`
- Modify: `anansi_app/README.md:1-5`

**Interfaces:**
- Consumes: the completed brand module, asset set, and public UI changes
- Produces: maintainable documentation and complete automated/visual verification evidence

- [ ] **Step 1: Document the active asset set**

Replace `anansi_app/assets/README.md` with a concise inventory that states:

```markdown
# Assets Folder

This folder contains static assets for the internally named Anansi app, presented to users as
**Mini-Grids Assistant**.

## Product identity

- `mini_grids_assistant_logo.png` — approved transparent 880×890 master mark
- `favicon-16.png` — 16×16 browser derivative
- `favicon-32.png` — 32×32 browser derivative used by NiceGUI
- `favicon.ico` — canonical ICO derivative

Do not reintroduce the retired spider/robot marks. Public identity constants and asset URLs
live in `nicegui_app/branding.py`.

## Organization logo

`org_logo_white.svg` is an independent operator-provided organization-logo slot.
```

- [ ] **Step 2: Clarify the internal/public naming boundary in the app README**

Keep the heading `# Anansi Admin App`. Immediately below it, add:

```markdown
> **Naming:** Anansi is the internal project and deployment name. The user-facing product is
> **Mini-Grids Assistant**; its public identity is defined in `nicegui_app/branding.py`.
```

Do not mass-replace later internal Anansi references in the README.

- [ ] **Step 3: Run the focused and full automated verification**

Run:

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" python -m pytest anansi_app/tests/test_branding.py -q
PYTHONPATH="$PWD:$PWD/anansi_app" python -m pytest anansi_app/tests -q
python .github/scripts/check_test_wiring.py
pre-commit run --all-files
```

Expected: the brand tests pass, the complete `anansi_app` suite passes with zero failures,
test wiring reports no unpublished new tests, and every pre-commit hook passes.

- [ ] **Step 4: Perform login-page visual QA**

Start the app without the development auth bypass:

```bash
cd anansi_app
PORT=8501 NICEGUI_RELOAD=false python -m nicegui_app.main
```

Open `http://127.0.0.1:8501/login` at 1440×900 and 390×844. Capture screenshots and verify:

- the silver lightning-tie mark is centered and fully visible at 160×160
- `Mini-Grids Assistant` and the sign-in copy are exact and legible
- the sign-in button is electric blue and keyboard focus remains visible
- no horizontal scroll, clipping, legacy logo, or visible `Anansi` product label appears

Stop the server after both viewport checks.

- [ ] **Step 5: Perform authenticated-shell and browser-chrome visual QA**

Restart with local auth bypass:

```bash
cd anansi_app
GRID_DESIGN_DEV_NO_AUTH=true PORT=8501 NICEGUI_RELOAD=false python -m nicegui_app.main
```

Open `http://127.0.0.1:8501/` at 1440×900 and verify:

- the 40×40 logo, two-line product name, and status dot fit inside the 240px drawer
- the user/logout row and navigation remain in their existing positions
- the tab title is `Mini-Grids Assistant`
- the new mark remains recognizable in the 16px and 32px favicon presentations

In a second terminal, start the Telegram Mini App with:

```bash
npm --prefix mini_app ci
npm --prefix mini_app run dev -- --host 127.0.0.1 --port 5173
```

Open `http://127.0.0.1:5173/` and verify its tab title is `Mini-Grids Assistant`. Stop both
development servers after inspection.

- [ ] **Step 6: Commit the naming documentation**

```bash
git add anansi_app/assets/README.md anansi_app/README.md
git commit -m "docs(anansi_app): document public Mini-Grids Assistant identity"
```

- [ ] **Step 7: Confirm final branch state**

Run:

```bash
git status --short --branch
git log --oneline --decorate -5
```

Expected: branch `codex/mini-grids-assistant-rebrand`, clean worktree, and five focused rebrand
commits after the planning commit.

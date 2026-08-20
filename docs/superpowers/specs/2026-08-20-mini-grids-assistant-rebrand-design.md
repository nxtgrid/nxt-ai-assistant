# Mini-Grids Assistant Rebrand Design

## Goal

Present the existing Anansi application to users as **Mini-Grids Assistant** while retaining
**Anansi** as the internal project, package, deployment, and operational name.

The user-approved master mark is `/Users/vaibha/Downloads/MiniGridsAssistant.png`, an
880×890 RGBA PNG with a transparent background. It depicts a reflective silver necktie whose
blade forms a lightning bolt.

## Naming Boundary

Use **Mini-Grids Assistant** anywhere the product identifies itself to a person:

- NiceGUI login page heading and sign-in copy
- authenticated sidebar brand lockup
- browser window/tab title
- browser favicons
- Telegram Mini App document title
- settings and prompts explanatory copy that currently names Anansi

Retain **Anansi** in technical and operational contexts:

- repository and Python package names (`anansi_app`, `anansi`)
- DigitalOcean app/component names and deployment commands
- environment variables, OAuth callback paths, routes, storage keys, and CSS class names
- bot usernames, command names, code comments, module docstrings, tests describing internals,
  and internal engineering documentation
- the internal README title, with a short note explaining the public product name

This is a presentation-layer rebrand, not a package, service, API, or deployment rename.

## Visual Direction

The reflective silver mark is the signature element. Its dimensional rendering is an
intentional contrast against the otherwise flat, utilitarian admin interface; no additional
glows, gradients, illustrations, or decorative effects should compete with it.

Brand tokens:

- **Night:** `#141824` — login canvas and sidebar
- **Electric blue:** `#4DA6FF` — primary actions and selected navigation
- **Canvas:** `#F0F2F6` — authenticated content background
- **White:** `#FFFFFF` — product name on dark surfaces
- **Mist:** `#CBD5E1` — supporting copy on dark surfaces

Typography stays within the existing NiceGUI/Quasar sans-serif stack to avoid a new font
dependency. The product name uses a restrained bold weight; body and utility labels keep the
existing regular and compact treatments.

## Surface Design

### Login

The login page uses a full `#141824` canvas. The logo is centered at 160×160 CSS pixels, with
the product name immediately below in white and the Google sign-in explanation in `#CBD5E1`.
The primary button uses `#4DA6FF`, sentence-case copy (`Sign in with Google`), and the existing
error states. The content column is capped at 28rem and remains centered on narrow screens.

### Sidebar

The 240px sidebar remains dark. The logo renders at 40×40 CSS pixels. The full product name
wraps naturally to two compact lines beside it; the live status dot remains visible and keeps
its current polling and tooltip behavior. The longer public name must not increase the drawer
width or displace the user/logout row.

### Browser and Telegram Chrome

The NiceGUI title and Telegram Mini App `<title>` become `Mini-Grids Assistant`. Favicons are
derived from the approved master mark at 16×16 and 32×32 with alpha preserved; `favicon.ico`
contains the 32×32 derivative.

## Asset Contract

The repository contains these active product assets:

- `anansi_app/assets/mini_grids_assistant_logo.png` — unchanged 880×890 master copy
- `anansi_app/assets/favicon-16.png` — 16×16 RGBA derivative
- `anansi_app/assets/favicon-32.png` — 32×32 RGBA derivative
- `anansi_app/assets/favicon.ico` — 32×32 ICO derivative

After every runtime reference uses the new mark, remove the unused legacy visual-brand files
`anansi_logo.png`, `anansi_logo_nobg.png`, and `anansi_spider.png` so there is no stale
user-facing mark to reuse accidentally. Keep
`org_logo_white.svg`; it is an independent operator-provided organization logo slot.

## Accessibility and Responsive Requirements

- Logo images expose `Mini-Grids Assistant logo` as alt text.
- Login text and controls maintain readable contrast against `#141824`.
- The login column fits a 390×844 viewport without horizontal scrolling.
- The sidebar lockup fits the existing 240px drawer without clipping or overlapping the
  status dot.
- Keyboard focus behavior and reduced-motion behavior remain unchanged; the rebrand adds no
  animation.

## Verification

Automated tests verify the brand constants, asset presence and binary dimensions, alpha
channels, ICO header, removal of runtime references to legacy logo names, public copy, and both
document titles. Visual QA covers the login page and authenticated sidebar at desktop and
mobile widths, plus the browser favicon at 16px and 32px.

# mini_app

Lightweight customer-facing chat widget built with Vite and Vanilla JS. Embeds in operator portals via iframe or script tag.

## Stack

- **Vite** — build tooling
- **Vanilla JS** — no framework
- **anansi-styles.css** — pre-compiled CSS bundle (see note below)

## Development

```bash
cd mini_app
npm install
npm run dev       # dev server at http://localhost:5173
npm run build     # production build → dist/
npm run preview   # preview production build
```

## CSS

`src/assets/anansi-styles.css` is a pre-compiled CSS bundle. To use your own styles, replace this file with any CSS file and update the import in `src/main.js`.

## Deployment

The built `dist/` is served by `chat_orchestrator` via the `/mini-app/*` FastAPI route. The Docker build in `chat_orchestrator/Dockerfile` copies the built assets automatically.

## Environment

The widget reads its API endpoint from `window.__ANANSI_API_URL__` injected by the server, or falls back to the same origin.

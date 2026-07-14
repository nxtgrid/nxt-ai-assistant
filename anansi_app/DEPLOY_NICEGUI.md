# Deploying the NiceGUI admin app (`anansi-app`)

The admin UI was migrated from Streamlit to NiceGUI; the migration is complete
and the Streamlit app (`app.py`, `components/`, `.streamlit/`, `streamlit[auth]`)
has been removed from the tree. Service name, port (`8501`), Dockerfile path,
and instance size are unchanged.

## Architecture

- Entry point: `python -m nicegui_app.main` (via `start.sh`), which also
  co-launches the broadcast-scheduler daemon.
- Auth: Authlib in `nicegui_app/auth.py`, in-process (no `secrets.toml`).
  Session store is `app.storage.user`, signed with `AUTH_COOKIE_SECRET`.
- Health check: `/healthz` (this is what the live DO spec's `health_check.http_path`
  must point at — already updated).
- Env vars: the same `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` (or
  `AUTH_CLIENT_ID`/`AUTH_CLIENT_SECRET`), `AUTH_REDIRECT_URI`,
  `AUTH_COOKIE_SECRET`, and the `ALLOWED_VIEWER_EMAILS` / `GRID_DESIGN_*`
  whitelists. The OAuth callback path is still `/oauth2callback`.
- Optional knobs (all defaulted in `start.sh`): `PORT` (8501), `HOST` (0.0.0.0),
  `NICEGUI_STORAGE_PATH` (`/tmp/nicegui`), `NICEGUI_RELOAD` (false).

## Deploy (DigitalOcean App Platform)

Deploys automatically on push to `main` (`deploy_on_push: true`). No spec
changes are needed for ordinary code changes — the health-check path is
already `/healthz` on the live spec.

> **CRITICAL — never update a live app directly from `.do/app.example.yaml`.** The example spec has
> `${PLACEHOLDER}` / plaintext values; pushing it destroys the live encrypted
> secrets. Always fetch → edit → push the *live* spec (see
> gitignored `spec-backup-*.yaml` files for the
> fetch → edit → push pattern if a spec change is ever needed again).

## Verify after deploy

```bash
APP_ID=<your-digitalocean-app-id>
doctl apps logs "$APP_ID" --component anansi-app --type run | \
  grep -iE "NiceGUI|scheduler|Uvicorn|error"
# Then hit the app URL: /healthz should return {"status":"ok"} and / should
# redirect to /login (or render for a whitelisted, signed-in user).
```

## Rollback

There is no parallel Streamlit UI to fall back to anymore — roll back via git:

```bash
git revert <bad-commit>   # or: git checkout <last-good-sha> -- anansi_app/
git push origin main
```

The DO health check stays at `/healthz` for any NiceGUI-era commit, so no spec
change is needed for a same-app rollback. Only touch the live spec if rolling
back to a pre-migration commit that still expects `/_stcore/health` (unlikely —
that history predates this doc).

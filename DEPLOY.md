# Deploying the Value Terminal

One Render web service runs everything: the Flask API, the built React
terminal (served same-origin — no CORS, no second deploy), and the feed
scheduler in-process. [render.yaml](render.yaml) describes it; Render reads
that file automatically.

> **Why not Vercel + a separate backend?** Two deploys, CORS config, and an
> exposed API origin buy nothing here — the Flask app already serves the
> built frontend. One service, one URL, and the access gate covers app and
> API together. (If you ever do want the split: build `frontend/` with
> `npm run build`, host `dist/` anywhere static, and set `API_BASE` at the
> top of `ValueTerminal.jsx` to the backend URL.)

## 1. Push to GitHub

The repo is already committed locally. Create an empty repo on GitHub
(private is fine — Render connects via the GitHub app), then:

```bash
cd "/Users/michael/Documents/WC2026 project"
git remote add origin https://github.com/<you>/wc26-value-terminal.git
git push -u origin main
```

(Or with the GitHub CLI: `brew install gh && gh auth login &&
gh repo create wc26-value-terminal --private --source . --push`.)

`.env` is gitignored — your API keys never leave this machine via git.

## 2. Create the Render Blueprint

1. <https://dashboard.render.com> → **New +** → **Blueprint**.
2. Connect GitHub and pick the repo. Render finds `render.yaml`.
3. It asks for the three un-synced env vars:
   - `API_FOOTBALL_KEY` — your API-Football key
   - `ODDS_API_KEY` — your The Odds API key
   - `ACCESS_CODE` — optional. Set it and the whole app asks for a
     password (friends enter any username + the code). Leave it empty
     for a public app.
4. **Apply**. First build takes a few minutes (node builds the frontend,
   then the python image). The app comes up at
   `https://wc26-value-terminal.onrender.com` (rename the service for a
   different subdomain).

Every `git push` to `main` redeploys automatically.

## 3. What the free tier means (read this)

- **Sleeps when idle.** After ~15 min with no visitors the instance spins
  down; the next visit takes ~50 s to wake. Fine for a few friends. This
  also pauses the feed scheduler — the feed refreshes while the app is
  awake, which is exactly when anyone is looking at it.
- **The ledger resets on deploys/restarts.** Free instances have no
  persistent disk, so `bets.db`, the feed, the API cache, and
  `history.jsonl` live in the container. Logged bets survive normal
  sleeping (the filesystem persists across spin-down) but are lost on a
  redeploy or platform restart. When the bet history starts mattering:
  upgrade the instance, uncomment the `disk:` block in `render.yaml`,
  and `/data` survives everything.
- **API budget is shared.** The deployed scheduler spends the same free
  API-Football quota (~100 calls/day) as your local builds. `MAX_MATCHES=8`
  and the on-volume cache keep it in bounds; don't run aggressive local
  builds on the same day the cloud cache is cold.
- **Custom domain** (your ".app"): Render → service → Settings → Custom
  Domains. Buy the domain anywhere, add the CNAME Render shows you; TLS is
  automatic. Optional — the `.onrender.com` URL works as-is.

## Updating the props CSV

Player-prop prices come from `backend/props/bet365_worldcup_master_props.csv`
(hand-collected). To refresh for a new match: replace the file, commit, push —
the redeploy picks it up. Locally the same file is used via
`--props-csv backend/props/bet365_worldcup_master_props.csv`.

## Railway instead of Render

Also works (`railway up` with the same Dockerfile; set the env vars from
`render.yaml`). Railway's volumes are available on the hobby plan, which
solves ledger persistence sooner, but there's no free always-on tier anymore —
the $5 trial credit runs out. Render's free tier is the better fit for
"friends occasionally check it".

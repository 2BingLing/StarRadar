<p align="center">
  <a href="README.md">中文</a> · <b>English</b>
</p>

<p align="center">
  <img src="static/readme/hero-en.svg" width="100%" alt="StarRadar · See the next rising star · A radar for discovering rising GitHub projects">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Live-GitHub%20Pages-1677ff" alt="Live demo">
  <img src="https://img.shields.io/badge/Python-3.10%2B-1677ff" alt="Python">
  <img src="https://img.shields.io/badge/Frontend-Vanilla%20JS-1677ff" alt="Frontend">
  <img src="https://img.shields.io/badge/Automation-Daily%2006_00-28a86b" alt="Automation">
  <img src="https://img.shields.io/badge/AI-Chinese%20explanations-f5a623" alt="AI">
  <img src="https://img.shields.io/badge/Tests-143%20passed-28a86b" alt="Tests">
</p>

---

## Positioning

By the time a project hits GitHub Trending, the hype has already been captured. StarRadar works the other way around — it targets **50–5000 star "mid-tier candidates"** and measures *who is taking off* by growth momentum, not *who is already the biggest*.

| Traditional trending | StarRadar |
| --- | --- |
| Sorted by total stars, giants dominate | Sorted by **growth momentum**, newcomers first |
| Only tells you "it got popular" | Tells you **why it deserves attention** (AI explanations) |
| One size fits all | Questionnaire + behavior memory + personalized picks |

> **Core idea**: stars alone mean nothing — growth momentum does. Giants that fail to outpace their own size class never make the StarRadar board.

---

## Screenshots

<p align="center">
  <img src="static/screenshots/shot-hero.png" alt="StarRadar homepage" width="780">
  <br>
  <sub>Homepage · Today's star map + potential board</sub>
</p>

---

## Core capabilities

| Capability | Description |
| --- | --- |
| **Daily potential radar** | Multi-bucket sampling across 3 star ranges (50-200 / 200-1000 / 1000-5000), noise filtering (courses / lecture notes / mirrors); 5D scoring: velocity · acceleration · community health · freshness · signal, benchmarked against peers of similar size |
| **Weekly trend report** | TrendScore v2 momentum ranking: growth 40% + novelty 20% + accel 15% + excess 10% + topic 10% + health 5%; momentum ticket gate keeps giants off the board; four sections: new stars / hot TOP / domain trends / my follows, with cross-week tracking and status badges |
| **Personalized picks** | 40-tag questionnaire → cold-start profile → topic match + MMR diversity reranking → recommendation reasons; behavior EMA (α=0.3) incremental updates + forgetting curve, improving the more you use it |
| **AI Chinese explanations** | Each listed project gets a "why it deserves attention" note, incrementally cached (regenerated only when stars change >20%), gracefully falling back to rule text |
| **GitHub native integration** | One-click login (PAT / OAuth device flow / redirect login) → star, fork, copy clone command and take notes right on the page |
| **Fully automated pipeline** | Daily 06:00 UTC: collect → score → explain → snapshot → deploy Pages; weekly report every Monday 08:00 |

---

## 5D potential scoring

<p align="center">
  <img src="static/readme/radar-5d.en.svg" width="100%" alt="StarRadar 5D potential scoring: velocity, acceleration, health, freshness, signal (sample data from tsingyuai/growth-lab)">
</p>

All five dimensions are measured against a **dynamic baseline of same-size projects**, not absolute values — a rising mid-tier project can outscore a giant a hundred times its size.

---

## Personalization loop

Data flow: browser (localStorage) → local `--serve` persistence (SQLite) → daily pipeline rebuilds your profile → generates picks.

| Layer | Scope | Depends on backend data |
| --- | --- | --- |
| Browser real-time layer | Potential board ordering, "relevant to you" badges, category tags | No (questionnaire + behavior computed instantly in your browser) |
| Backend long-term layer | Personalized picks, cross-device memory, daily rebuild | Yes (questionnaire / behavior must reach memory.db) |

**How the long-term layer becomes active**: run `python src/main.py --serve` locally and open the page — it auto-syncs everything after ~8 seconds (the footer "Sync data" button also works manually). CI then rebuilds your picks daily with the freshest profile. If you only browse GitHub Pages and never sync, the long-term layer stays at the last synced profile (the browser real-time layer is unaffected).

---

## Sign-in mechanism & permissions

"Sign in with GitHub" uses the **OAuth Device Flow** (the Client ID is bundled as a public value — zero configuration for anyone who clones):

1. Click sign in → GitHub asks for an 8-digit code → authorize once
2. The GitHub-issued token returns directly to **your browser and stays local** (localStorage; the personal pipeline also keeps a copy in a local file to fetch "my stars / my repos")
3. The scopes shown on the authorization page are `public_repo` (star / fork public repos) and `read:user` (read your username and avatar)

**Safety boundaries**:

- **The token never passes through any third party** — not the author's server, not the hosting platform, not a public proxy; on GitHub Pages (no backend) the token stays in your browser only
- **The bundled Client ID is a public value** (not a secret, no risk); the Client Secret is never bundled or committed — GitHub auto-revokes any publicly leaked secret, which is exactly why sign-in uses device flow instead of an embedded-secret redirect
- **The app only calls**: star/unstar, fork, and reading your starred/repo info. It never uses your token for anything else (no code changes, no private repos, no other apps)
- **Sign out anytime**: the page's "Sign out" clears the local token instantly; you can also revoke at GitHub → Settings → Applications → StarRadar and the token dies immediately
- **No data uploads**: questionnaire / behavior data is only persisted locally via `--serve` (memory.db); the GitHub Pages deployment contains no personal data

> Want the one-click "authorize & bounce back" website experience? Register your own OAuth App and set `GH_OAUTH_CLIENT_ID / GH_OAUTH_CLIENT_SECRET` in your local `.env` — sign-in auto-upgrades to the redirect flow (callback URL `http://127.0.0.1/api/oauth/callback`).

---

## Quick start

```bash
git clone https://github.com/2BingLing/StarRadar.git
cd StarRadar
pip install -r requirements.txt
cp .env.example .env        # GITHUB_TOKEN required; LLM_* optional (any OpenAI-compatible endpoint)

python src/main.py --serve  # local observatory → http://127.0.0.1:8970/
python src/main.py          # daily pipeline: collect → score → profile → explain → JSON
python src/main.py --weekly # build / refresh the weekly trend report
pytest                      # 143 passed
```

---

## Structure / tech stack

| Layer | Contents |
| --- | --- |
| src/ | collector · analyzer · profile (profile / recommendations) · reporter (reports / LLM explanations) · search (semantic search) · web (local server + OAuth) |
| static/ | GitHub Pages frontend (vanilla JS, zero frameworks): index.html · js/ · css/ · data/ (CI-generated JSON) |
| data/profile/ | memory.db (questionnaire + behavior + snapshots) + interests.json (interest profile) |
| .github/ | daily.yml daily refresh · weekly.yml Monday report · Pages deployment |
| Backend | Python 3.10+ · SQLite · numpy · scikit-learn · BGE embeddings · HNSW · BM25 |
| Automation | GitHub Actions (free unlimited minutes on public repos) |

---

<p align="center">
  <img src="static/screenshots/shot-potential.png" alt="Potential radar full page" width="780">
  <br>
  <sub>Potential radar · full page</sub>
</p>

---

## License

[GNU Affero General Public License v3.0](LICENSE) — free to use and modify, but **derivative works and network services based on this project must be open-sourced under AGPL as well**.

---

<p align="center">
  <img src="static/starlogo.png" alt="StarRadar" width="40">
  <br>
  <sub>Your private star map, refreshed weekly · StarRadar</sub>
</p>

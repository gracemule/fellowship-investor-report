# Deployment

Two free Render web services, each in its own workspace, plus Neon Postgres. Deployed by Claude through the Render tools on
6 Oct 2026; secret values were pasted by the owner, never by Claude.

| Service | Workspace | Address | What it is |
| --- | --- | --- | --- |
| `chui-reporter` | My Workspace | https://chui-reporter.onrender.com | The interface and the agent (Docker, `Dockerfile`, no LibreOffice). |
| `chui-converter` | x | https://chui-converter.onrender.com | LibreOffice behind an authenticated API (Docker, `Dockerfile.converter`). |

Both: free plan, region Frankfurt (next to the Neon database in London), one instance, auto-deploy from `main`. The other
workspaces (`y`, `m`) are unused and free for a later service.

## Why this shape

* **LibreOffice on its own service.** It needs ~210 MB while converting a small file (measured on the live service: 9.9 s,
  peak 210 MB of 512 MB), so it must not share a 512 MB host with the agent. The app sends it the document plus the three
  Larken font files (licensed, so never in an image); the service installs them once and LibreOffice lays the report out
  with the real font. iLoveAPI is not needed (it converts but drops Larken; see the README).
* **One service per workspace.** Each free workspace has 750 instance hours a month; one always-on service uses 744.
* **The agent stays inside the app service.** It is a worker thread that shares the app's database connections and run state;
  splitting it off would add a network hop for no benefit.

## Keeping the services awake (UptimeRobot)

A free service sleeps after ~15 minutes without traffic. Add two monitors, type HTTP(s), interval 5 minutes, expected status
200 (the free plan sends HEAD requests, which both services accept). Both the bare address and `/healthz` answer HEAD and GET
with 200, so a monitor pointed at either is up; `/healthz` is preferred because it does no work:

* https://chui-reporter.onrender.com/healthz
* https://chui-converter.onrender.com/healthz

`/healthz` does no work and never touches the database, so the pings do not keep Neon awake. `/readyz` (app only) does check the
database; use it by hand, not in a monitor. If a service has been asleep, the app waits for the converter to wake (it retries
for about two minutes).

## Environment

Non-secret settings are already on the services. Secrets live only in the Render dashboard (Environment, "Add from .env"):

| Service | Secret | Notes |
| --- | --- | --- |
| both | `CHUI_CONVERTER_TOKEN` | Must be the same value on both. Rotate by changing both. |
| `chui-reporter` | `DATABASE_URL` | Neon pooled connection string. |
| `chui-reporter` | `CHUI_ACCESS_PASSWORD` | The shared sign-in password. The app refuses to start without it. |
| `chui-reporter` | `DEEPSEEK_API_KEY`, `TAVILY_API_KEY` | `BRAVE_API_KEY` (search fallback) and `ANTHROPIC_API_KEY` (model fallback) are optional. |

`CHUI_PDF_CONVERTER=remote`, `CHUI_CONVERTER_URL`, `WITH_LIBREOFFICE=0`, `CHUI_PROVIDER`, `CHUI_SEARCH_ORDER` and
`CHUI_ENV=production` are set on the app. In production the idle server checks the database for orphaned runs hourly
(`CHUI_JANITOR_SECONDS`), so Neon can scale to zero between uses.

## Reading the logs (no secrets in them)

* The app logs one line at start-up, `settings seen by this server: ...`, naming each setting and whether it is set or empty (and
  its length), never its value. A mistyped name or an empty box shows up there at once.
* Each service logs the converter token as a length and an 8-character fingerprint. The two fingerprints must be equal; if they
  differ, the converter answers 401 and its log says what it was shown (length and fingerprint only).
* A token pasted with quotes, spaces or the whole `NAME=value` line is accepted.

## Checks after a change

1. `curl -I https://chui-converter.onrender.com/healthz` and the same for the app: 200 (done 6 Oct 2026: both 200; `/readyz`
   reports the database reachable from Frankfurt; `/api/state` without signing in is 401).
2. `curl https://chui-converter.onrender.com/selftest`: `ok: true`, with the time and LibreOffice's peak memory (rate limited).
3. Sign in, start a run, and confirm the Sources tab shows "LibreOffice service". Then
   `python -m chui_reporter.render.check_converter` (with `CHUI_PDF_CONVERTER=remote` and the two converter settings) must
   report that Larken is in the PDF.

## Returning a quarter to blank (for a demo, or after a wrong start)

    python -m chui_reporter.admin reset-quarter 2026Q2            # shows what it would delete
    python -m chui_reporter.admin reset-quarter 2026Q2 --apply    # does it (add --brand to remove the shared brand kit too)

It removes that quarter's synced files, report, figures, review notes, runs, conversations (and the agent's saved memory) and feed,
and the cache of web pages the researchers read. It refuses while a run is active, runs as one transaction, and never touches
another quarter.

The brand kit (logos and fonts) is installed once for every quarter and is not removed by this unless asked. A database that
has never held it (or lost it with `--brand`) gets it back, without opening the app, with:

    python -m chui_reporter.admin install-brand "/path/to/folder"   # the folder that contains Branding; nothing else is read

## Database size (Neon free tier)

The app's own data is small (tens of MB). The agent's saved checkpoints were the only thing that grew; they are pruned as the agent
works (see the README) and a one-time `python -m chui_reporter.runtime.retention --apply` shrank them from 327 MB to 2 MB on
6 Oct 2026. If the database ever looks large again, run that command without `--apply` first: it reports what it would keep.

## Limits to know about

* Free services have a fraction of a CPU: conversions and page images are slower than on a laptop.
* `/selftest` is public by design (it takes no input and is rate limited to one run per 30 s) so the service can be checked
  before the token is set. Remove it if that is not wanted.
* The Larken licence flag (fsType 4) is why a hosted converter cannot be used; the files are only ever sent to the converter
  service the owner controls.

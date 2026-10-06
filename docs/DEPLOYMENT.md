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
200 (the free plan sends HEAD requests, which both services accept):

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

## Checks after a change

1. `curl -I https://chui-converter.onrender.com/healthz` and the same for the app: 200.
2. `curl https://chui-converter.onrender.com/selftest`: `ok: true`, with the time and LibreOffice's peak memory (rate limited).
3. Sign in, start a run, and confirm the Sources tab shows "LibreOffice service". Then
   `python -m chui_reporter.render.check_converter` (with `CHUI_PDF_CONVERTER=remote` and the two converter settings) must
   report that Larken is in the PDF.

## Limits to know about

* Free services have a fraction of a CPU: conversions and page images are slower than on a laptop.
* `/selftest` is public by design (it takes no input and is rate limited to one run per 30 s) so the service can be checked
  before the token is set. Remove it if that is not wanted.
* The Larken licence flag (fsType 4) is why a hosted converter cannot be used; the files are only ever sent to the converter
  service the owner controls.

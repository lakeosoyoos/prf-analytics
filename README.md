# PRF Analytics (Mac app)

The **PRF tab** of the aip_products web app, split out as its own Mac app that works offline:

| Tab | What it is |
|---|---|
| **County map** | One county map with five views: County Base Value, best **win rate**, best **return per $1**, best **return per acre**, and **commission per acre**. It includes the buyer and seller lenses, the productivity input, the AIP picker and the range sliders. |
| **Interval analyzer** | For any state, county and grid, it shows the per-interval **heat map**, the year-by-year matrix and the **recommended allocation**. Two dropdowns set the window: **Window ends** (the last year, 2004–2025) and **Lookback** (5, 10, 15 or 20 years back from it). **Save client deck…** (PowerPoint or PDF) turns the grid, county and use you picked, with the coverage, productivity and window set in the analyzer, into the agency's three-slide deck: a title slide ("PRF Analysis" and the run date on one line, the county below it), the five most consistent and five best-return allocations, and a closing slide. The look is your own deck (`engine/data/assets/prf_deck_template.pptx`, made from the Ventura deck); only the text changes. |
| **All allocations** | Every interval allocation for a chosen state, county, grid and use (about 50,000 under the 2027 rules), with average net return per acre, win rate and return per $1 over the analyzer's window, coverage and productivity controls. **Sort by** any metric; filter by number of intervals or an interval that must be included; **Save as CSV…** writes the rows in the optimizer workbook's columns. Rank #1 is the analyzer's recommendation. |
| **Old vs new index** | Compares the CPC-based and NCEI-based recommendations for every grid: five summary numbers, a grid map, and a **list of the recommendations** for every grid in every county. You can sort, filter, search and save it as CSV. |
| **Herd size** | The FSA acres-per-animal-unit map. |

Installed at **`~/Applications/PRF Analytics.app`**. Open it from Spotlight, or drag it into the Dock.

## How it works

It computes nothing on its own. `engine/src/` holds **byte-for-byte copies** of the web app's
PRF modules (`prfpage`, `prfintervalpage`, `prfvintagepage`, `prfherdpage` and what they
import). They run against `engine/data/prf_analytics.db`, a PRF-only copy of the web app's
`catalog_app.db` (36 MB instead of 89 MB). A small local server (`prf_app/server.py`, which
listens only on this computer) passes their pages to a native WebKit window
(`prf_app/desktop.py`, pywebview).

The one file that is not a verbatim copy is `engine/src/prfopt.py`. The pages only need its six
interval constants, and the real module loads pandas. `sync_engine.py` lifts the constants out of
the source automatically, and a test compares them with the original.

## Updating after the web app changes

When a new sweep, a new ADM year or a PRF code change lands in `~/aip_products`:

```bash
./build_app.command --sync
```

This copies the code and data again, runs the tests, builds, signs, runs the self-tests on the
built app, and installs it. **Nothing is installed unless every gate passes:**

1. `pytest`: the parity test renders every view from the PRF-only DB and from the full DB and
   requires identical results. That catches a table the cut left out, which would otherwise
   show up only as a silently empty map.
2. The bundle signature must pass `codesign --verify --strict`.
3. `--self-test`: the **built** app serves every view from inside the bundle.
4. `--check-window`: the built app opens its real window, clicks through all four tabs,
   confirms the Save and commission buttons are connected, then closes. You'll see a window
   for a few seconds.

The header's data stamp shows when the data was built and which aip_products commit it came
from. A `+` means it was synced from uncommitted changes.

## Commission rates

**Commission rates…** (on the County map tab) copies the two rate files out of the app into
`~/Library/Application Support/PRF Analytics/` and shows them in Finder. Edit them, then press
**Reload map**. From then on the app uses your copies. Delete them to go back to the rates
that came with the app.

## Notes

- **Build location.** `~/Desktop` is synced by iCloud Drive, and iCloud keeps adding file
  metadata that `codesign` rejects. The build therefore runs in
  `~/Library/Caches/PRF Analytics build/`, and the build environment lives in
  `~/.prf_analytics_venv`. To rebuild the environment:
  `python3 -m venv ~/.prf_analytics_venv && ~/.prf_analytics_venv/bin/pip install -r requirements.txt`.
- **Signing.** The app is *ad-hoc signed*, which is enough for this Mac. On another Mac, the
  first launch needs right-click → **Open** (it isn't notarized). The build is arm64 (Apple Silicon).
- **Log.** `~/Library/Logs/PRF Analytics.log`.
- **Dev run:** `~/.prf_analytics_venv/bin/python run_prf_analytics.py` opens the window, and
  `--browser` opens the same app in a browser tab instead.
- The web app is unchanged. Its PRF tab is still there.

## Web version (PRF Analytics on Streamlit)

`web/streamlit_app.py` is the same app in a browser, behind a passcode: County map, Interval
analyzer (with the client-deck builder), All allocations, Old vs new index. It calls the same
`prf_app/views.py` over the same `engine/data/prf_analytics.db`, so the numbers match the Mac app.

- **Run locally:** `cd web && ~/.prf_web_venv/bin/streamlit run streamlit_app.py`
  (the local passcode is in `web/.streamlit/secrets.toml`, which git ignores).
- **Deploy (Streamlit Community Cloud):** main file `web/streamlit_app.py`; the host installs
  `web/requirements.txt`; put `app_passcode = "…"` in the app's **Secrets**. Keep the GitHub
  repo **private**: it carries the commission-rate seeds.
- **Update after new data:** `./build_app.command --sync` (refreshes `engine/`), then commit and
  push — the hosted app redeploys itself.

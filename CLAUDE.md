# CLAUDE.md — China Leadership Tracker (public site repo)

Public, daily-updating database of Chinese leaders' activities built from CCTV
*Xinwen Lianbo* transcripts and People's Daily front pages. Live site: https://nelsonwang222.github.io/china-leadership-tracker/
(GitHub Pages from `docs/` on `main`). Owner: nelsonwang222.

**Single-repo setup (since 2026-10-20):** this repo holds the static site
(`docs/`), the update workflows, and BOTH data pipelines. The XWLB pipeline
used to live in the private repo `nelsonwang222/china-leadership-tracker-
pipeline`; that repo became unreachable (every checkout fails with 404), the
daily job failed every night, and the dataset froze at 2026-08-02. The
scanner was rebuilt here as `scripts/xwlb_build.py` (stdlib only, same shape
as `scripts/rmrb_build.py`) and `update.yml` now runs it in-repo.

## Architecture

```
docs/                 vanilla HTML/CSS/JS site — no build step, no framework
docs/data/            built JSON published by the workflow (index, leaders,
                      per-year event shards, meta) + docs/data/rmrb/* (People's
                      Daily feed, owned by rmrb.yml)
scripts/xwlb_build.py CCTV Xinwen Lianbo scanner (XWLB feed)
scripts/rmrb_build.py People's Daily front-page scanner (RMRB feed)
xwlb/                 XWLB raw records + translation cache (append-only)
rmrb/                 RMRB raw records + translation cache (append-only)
.github/workflows/update.yml  runs scripts/xwlb_build.py, publishes docs/data/
.github/workflows/rmrb.yml    runs scripts/rmrb_build.py, publishes docs/data/rmrb/
.github/workflows/pages.yml   deploys docs/ to Pages (push to docs/** or
                              called from update.yml / rmrb.yml)
```

Both scanners run HERE (public repos get free unlimited Actions minutes) and
share the translation secrets (`GEMINI_API_KEY` / `ANTHROPIC_API_KEY` /
`DEEPSEEK_API_KEY`, plus the `TRANSLATE_MODEL` / `DEEPSEEK_MODEL` vars) in
THIS repo's settings. Because the workflows are public, so are their run logs
(event titles, counts, cadence) — keep pipeline stdout modest.

The 2016–present archive is fully translated as of 2026-07-05 (19,760/19,760);
routine runs only fetch/translate new broadcasts and take minutes, not hours.

Local dev: `python -m http.server -d docs`. Frontend data contract:
`index.json` holds compact array-of-arrays rows (see its `columns` field);
full Chinese text lives only in per-year `events-YYYY.json` shards,
lazy-loaded/prefetched client-side.

Key frontend facts (state as of 2026-07-08):

- **Match modes**: the filter bar has three mutually exclusive switches
  (`wireRadio`, exactly one always on): AS PRIMARY ACTOR (default; leader
  filter matches `e.leaders`) / AS MENTIONED (`e.mentions`) / FULL TEXT 原文
  (search loads transcript shards; leader matches either list). Per-leader
  actor/mention counts are read off the chart's COUNT line by switching
  modes — leader cards deliberately show no counts (dropped as confusing).
- **Roster page** shows all 58 in official ranking — `leaders.json` arrives
  pre-sorted (tier, then roster order); "LEADERS TRACKED" is roster size,
  NOT leaders-with-events (title-based primaries leave ~14 at zero).
- **XLSX export** under the chart: password-gated (SHA-256 hash constant in
  app.js — a deterrent only, the JSON is public; plaintext password is
  recorded only in the workspace-root PROJECT_OVERVIEW.md, outside git),
  session-cached via sessionStorage, generates a real .xlsx from a
  dependency-free stored-entry zip writer + inline-string sheet XML. Columns
  include Mode (how the selected leader relates to each row).
- **Type labels** flow from the pipeline via `index.json`'s `types` map; a
  new event type needs only a cosmetic `TYPE_CODES` entry here.
- **Deliberately noindexed** (2026-07-08, user request): index.html has
  `<meta name="robots" content="noindex, nofollow">` — don't remove it
  without asking. robots.txt is NOT an option for a project Pages site:
  crawlers read it only at the origin root (nelsonwang222.github.io/),
  which this repo doesn't control; that would need a user-site repo and
  would affect every project under the domain. The meta tag can't cover
  the raw JSON under docs/data/ (Pages sets no custom headers), and none
  of this is access control — the site stays publicly reachable.

- **Design**: implemented from Claude Design project
  992c228a-c88e-4e22-8073-51d1654f2795 ("Modernizing China leadership
  tracker"). Tokens live in `docs/style.css` (`--bg/--panel/--line/--ink/
  --muted/--faint/--accent`, dark default + light via `html[data-theme]`);
  fonts Newsreader + IBM Plex Mono from Google Fonts.

## XWLB source (since 2026-10-20, in-repo)

`scripts/xwlb_build.py` rebuilds the CCTV feed without the private pipeline
repo. Facts worth preserving:

- Day pages are `tv.cctv.com/lm/xwlb/day/YYYYMMDD.shtml`; each anchors to
  its segments at `tv.cctv.com/YYYY/MM/DD/VIDE*.shtml`. The first entry of a
  day page is the full-episode player (《新闻联播》 YYYYMMDD 19:00) - it has
  NO transcript, so it is excluded from the segment count.
- A segment page carries the title in `<title>` (prefixed `[视频]`) and the
  full transcript in `<div class="content_area" id="content_area"><p>…</p>`;
  the text ends at the `京ICP备` footer. ~11-13 segments per day.
- New days are MERGED into the published `docs/data/{index.json,meta.json,
  events-YYYY.json}` - existing rows are never rewritten or dropped (the
  builder loads the published index, adds only ids it doesn't have, and
  recomputes meta). Never write `docs/data/rmrb/**` from here.
- Event-type classification is keyword-based and was tuned against the
  2016-2026 archive's own type distribution: 出席+会议/年会/论坛 -> ceremony,
  主持+会议/学习 -> chaired_meeting, 会晤 -> meeting, incoming visits
  (访华/对中国) -> visit_to_china, outgoing (将对/将赴/将访问) -> foreign_trip.
  Re-check the archive with a `Counter` over `index.json` before changing a rule.
- New ids are `YYYYMMDD-NN` per day, so they never collide with the archive's
  ids. One row per segment (the old pipeline merged some segments), so daily
  event counts from 2026-10-20 onward are ~11-13, not ~4.
- State: `xwlb/records.jsonl` (raw), `xwlb/translations.jsonl` (cache),
  `xwlb/checked.json` (empty-date memory, TTL 7 days, today never tombstoned).
- Translation reuses the rmrb providers; precedence Claude > Gemini > DeepSeek.

## People's Daily front-page source (since 2026-08)

`scripts/rmrb_build.py` is a self-contained, stdlib-only scanner for the
People's Daily e-paper front page (paper.people.com.cn/rmrb/pc). Facts worth
preserving:

- The free e-paper PC layout archive starts 2024-12-01; older interfaces
  return 403 to automated clients. The scanner backfills from 2026-07-01
  (chosen for v1) and resumes from `rmrb/records.jsonl` after any interruption.
- The paper site anti-bots after a burst of requests. Keep `--sleep` at
  >= 0.5s, cap runs with `--max-days`, stop after consecutive misses, and let
  the next scheduled run self-heal.
- Data files: `rmrb/records.jsonl` (raw, append-only), `rmrb/translations.jsonl`
  (translation cache, append-only), `rmrb/checked.json` (empty-date memory,
  TTL 7 days), and built `docs/data/rmrb/{index.json, events-YYYY.json,
  meta.json}`. The rmrb workflow only writes these paths, so it never
  conflicts with update.yml's `docs/data/index.json` +
  `docs/data/events-YYYY.json`.
- The rmrb workflow runs at 05:30 and 23:30 Beijing time. Today is never
  tombstoned as empty (the 05:30 run can legitimately run before the paper is
  posted), and dates whose articles partially failed stay retryable instead of
  being marked complete.
- Translation supports Claude, Gemini, or DeepSeek (`DEEPSEEK_API_KEY`,
  default model `deepseek-chat`); precedence is Claude > Gemini > DeepSeek.
- Frontend contract: app.js loads `data/rmrb/index.json` + `data/rmrb/meta.json`,
  tags every row `source` ("xwlb"/"rmrb"), and merges rmrb shards into
  `loadYearDetail()`. Any UI change that talks about counts or sources must
  handle both feeds.
- RMRB rows are typed `article`; actor/mention tagging is exact-name with a
  small boilerplate exclusion (e.g.
  习近平新时代中国特色社会主义思想
  is not a mention). This is deliberately simpler than the XWLB pipeline's
  classifier and can be refined later.

## Lessons learned (change future behavior)

1. **Never push to main (either repo) while an Actions run is in flight**
   unless you must — a run commits to BOTH repos at the end. The commit steps
   do `git pull --rebase -X theirs` with retries, so a collision no longer
   loses a run's work, but it still wastes a rebase. Routine runs take
   minutes now (they took ~2h during the translation backfill). Check
   `gh run list --workflow "Daily update" --limit 1` first.
2. **Pages deploys via Actions (`pages.yml`), not the legacy branch builder**
   — the legacy builder repeatedly wedged silently in `status: building`
   (fix was requeueing via `gh api -X POST repos/.../pages/builds`), so
   Pages source was switched to "GitHub Actions" on 2026-07-05. pages.yml
   runs on pushes touching `docs/**` AND as a job called from update.yml —
   both are needed, because GITHUB_TOKEN pushes don't trigger push
   workflows. Verify deploys by `curl`ing a changed file's content; served
   files are CDN-cached ~10 min — always hard-refresh when eyeballing.
   deploy-pages can fail transiently with "Deployment failed, try again
   later" (seen lasting ~1h on 2026-07-06, GitHub status page green the
   whole time); pages.yml retries once, and a red deploy job self-heals at
   the next scheduled run — or `gh workflow run "Deploy Pages"` manually.
3. **Workflow-file pushes need the `workflow` OAuth scope.** The gh token
   didn't have it; fix is `gh auth refresh -h github.com -s workflow`
   (device-code flow the user must complete in a browser).
4. **SVG drag: don't rely on `setPointerCapture`** — synthetic/CDP pointer
   events lose capture. Attach `pointermove`/`pointerup` to `window`, convert
   coords via `svg.getScreenCTM().inverse()`, and suppress the click handler
   when movement exceeded ~3px.
5. **git author email must be a GitHub-linked address** (e.g.
   `76817107+nelsonwang222@users.noreply.github.com`, set in repo git config)
   or the owner never appears in the Contributors graph — the `.local`
   hostname email doesn't count.
6. **Design handoff:** treat `.dc.html` mockups as visual spec only — their
   data/logic is placeholder. Keep real features the mockup omits (date
   pickers, chart, full-text toggle) and restyle them into the new language.
7. **History was rewritten on 2026-07-05** (git filter-repo, force push) to
   purge pre-split `pipeline/`, `data/`, and `requirements.txt` from all
   commits. Any stale clone must `git fetch && git reset --hard origin/main`,
   never merge/rebase old history back in. Old commits may stay reachable on
   GitHub by direct SHA until GitHub garbage-collects them (GitHub Support
   can purge sooner on request). Before force-pushing, verify the rewrite two
   ways: no stripped path anywhere in `git log --all --name-only`, and
   `git ls-tree` hashes of the kept paths identical to the original HEAD.
8. **The whole cross-repo wiring is scriptable with gh** — no web UI needed:
   `gh repo deploy-key add key.pub --allow-write` (private repo),
   `gh secret set PIPELINE_DEPLOY_KEY < key` (this repo), and
   `gh api -X PUT repos/.../pages -F build_type=workflow` to flip the Pages
   source to Actions. Deploy keys beat PATs here: repo-scoped, no expiry
   surprises, and creatable headlessly.
9. **Why this shape:** on a Free plan, making a repo private kills its Pages
   site and meters its Actions minutes — and even paid private repos serve
   Pages publicly. Hence: public site repo + private pipeline repo, workflow
   in the public one. Going private on the single repo was never an option.
10. **Keep every stat consistent with what clicking shows.** A leader card
    count that differs from the filter's result count reads as a bug; when
    the leader filter became primary-actor-only, card counts had to go and
    "LEADERS TRACKED" had to become roster size (it silently fell 58→44 by
    still counting leaders-with-primary-events).
11. **A pipeline semantics change is a two-repo change.** Reclassifications
    ship as pipeline commits, but anything the UI states about the data
    (toggle labels, stats, export columns) usually needs a matching docs/
    commit — grep app.js for the affected field before pushing the pipeline
    side. Data goes live only at the next Daily update run (trigger one
    manually with `gh workflow run "Daily update"`); docs/ changes deploy on
    push via pages.yml.
12. **In-browser .xlsx needs no library**: an xlsx is a zip of XML; stored
    (uncompressed) zip entries + a CRC32 table + inline-string sheet XML is
    ~130 lines and opens fine in Excel with CJK intact. Validate with
    Python zipfile + ElementTree, not by eyeballing.

- Two write workflows on one repo: keep them on disjoint paths (XWLB: docs/data/index.json + events-YYYY.json; RMRB: rmrb/ + docs/data/rmrb/). Same `git pull --rebase -X theirs` retry pattern; conflicts should never arise.
13. **A failing scheduled job hides behind "it still updates"**: update.yml
    failed nightly for ~11 weeks (private pipeline repo 404) while rmrb.yml
    kept publishing, so the site looked alive but the XWLB feed was frozen.
    Check `gh api repos/.../actions/workflows/<file>/runs` for `conclusion`
    before assuming a feed is current - and prefer a pipeline that lives in
    the same repo as its data so one dead dependency cannot freeze a feed.
14. **Never hardcode one data source's meta in the frontend**: app.js pinned
    the date filter to `meta.last_date` of a single feed, so the "To" date
    stopped at 2026-08-02 even though rmrb data ran to the present. Compute
    filter bounds as the union of every feed's meta AND the loaded events.

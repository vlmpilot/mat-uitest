---
name: mobileapp-testing
description: Runs AI-assisted mobile UI tests against a connected Android device via the MAT CLI — generates test cases from natural language, executes them with screenshot-backed evidence, heals broken locators after an app update, explores an app into a page graph, and attributes failures with an LLM. Use when testing an Android app on a real device or emulator; when the user mentions UI tests, regression, smoke tests, 真机测试, 冒烟, or 移动端自动化; or when an existing mobile test case broke after a UI change.
license: Complete terms in LICENSE.txt
---

# Mobile App UI Testing with MAT

To test Android apps on a connected device, use the `mat` CLI. Every command is self-documenting: run `mat --help` first, and `<command> --help` before inventing flags. Do not read the engine source — the CLI is the interface.

## Setup (agent-executable — run this first; covers not-installed and already-installed)

Version floor: **mat-uitest ≥ 0.7.0**; the `mat summary` digest command used
throughout this skill needs **≥ 0.7.1** — always install the latest available.
Human-only prerequisite: an Android device
with USB debugging enabled (or an emulator) plugged in — everything else below the
agent can do itself.

```bash
# 1) Already installed? → verify and move on
command -v mat && mat doctor

# 2) Not installed → uv route (recommended: ships its own Python ≥3.11,
#    so an old system Python doesn't matter)
curl -LsSf https://astral.sh/uv/install.sh | sh    # skip if `command -v uv` succeeds
export PATH="$HOME/.local/bin:$PATH"   # CRITICAL, same-session PATH —
                                       # `uv tool update-shell` only affects FUTURE shells,
                                       # so without this line the just-installed `mat`
                                       # is invisible in the current session
uv tool install mat-uitest
mat doctor

# 3) `mat` still not found (unusual shell setups) → run without installing:
uvx --from mat-uitest mat <args...>

# 4) Alternatives, only if you prefer them AND system Python ≥ 3.11:
pipx install mat-uitest && pipx ensurepath
py -m pip install --user mat-uitest                   # Windows
```

**adb missing** (`mat doctor` reports it) — self-installable:

```bash
# macOS (Linux: same URL with `linux` instead of `darwin`)
curl -sL -o /tmp/pt.zip https://dl.google.com/android/repository/platform-tools-latest-darwin.zip \
  && unzip -q /tmp/pt.zip -d "$HOME/.local" && export PATH="$HOME/.local/platform-tools:$PATH"
adb version
```

(Windows: Android Studio's SDK Manager, or `choco install adb`.)

**Workspace bootstrap**: run `mat init` once in the project where tests will live —
it creates `cases/` (with an example case) and `artifacts/`. Every `mat run
cases/<file>.json` path below assumes this layout.

**iOS (beta)**: simulator (`mat run --platform ios`; needs the `[ios]` extra and WDA on `localhost:8100`) **and USB real device** — run `mat ios-setup` first (Xcode signed-in account, device trusted, one ⌘U build; `--start` also builds and launches WDA). Real-device limits: **USB only**, device must stay **unlocked** with auto-lock set to **Never**, free-provisioning cert expires in **7 days**, no `--record`. **Blocked on iOS** (do not attempt): `index` / `explore` / `arrival` — Android is the full-capability platform.
**Not supported** (do not attempt): cloud device
farms, web apps (for web testing see Anthropic's webapp-testing skill).

**Helper commands** (run `mat --help` to see all):
- `mat doctor` - Environment self-check (Python / adb / device / driver / AI config) with actionable fixes
- `mat devices` - List connected devices

## Decision Tree: Choosing Your Approach

```
Task → Is there an existing test case (cases/*.json)?
    ├─ Yes → mat run cases/<file>.json
    │        → mat summary <run_dir>   (≤40-line digest; see below)
    │
    ├─ No, but you can describe the flow in one sentence
    │        → mat gen "<description>" --package <pkg> --calibrate
    │
    └─ No idea what to test → mat explore <pkg> --minutes 2
```

**AI capability tiers** (what a fresh install can do): with **no API key** configured,
`run` / `explore` / `index` / `heal` / `devices` / `summary` all work — execution,
evidence, and locator self-healing are local. `gen` and `diagnose` need a key: see
the "AI 接入" item in `mat doctor` (your own OpenAI-compatible key in the workspace
`.env`, or the official proxy token).

**Before generating cases or relying on them long-term**: run `mat index <pkg>`
once to crawl the app into a page graph. With an index, locator renames — the
most common app-update breakage — are healed for free; without one, every
rename breaks your cases.

## Device Readiness (check before anything else)

- **0 devices** in `mat devices` → run `mat doctor`; check USB debugging.
- **Multiple devices** → always pass `--serial`; otherwise behavior is undefined.

## Inspect the UI Tree, Then Act

1. **Check the device is really ready**:
   ```bash
   mat devices        # exactly one "device" state line, or pass --serial
   ```
2. **Run, then read `run.json` — never the HTML report.** The report is a
   self-contained HTML with inline base64 screenshots (measured **up to 15 MB**);
   reading it will blow up your context window. Read `<run_dir>/run.json`
   (≈6 KB) instead — or better, get a ≤40-line digest:
   ```bash
   mat summary <run_dir>          # use this CLI form; `python -m insta_engine.run_summary`
                                   # only works when insta_engine is importable by the
                                   # current Python (NOT the case under uv tool / pipx)
   ```
   `mat summary` ships from mat-uitest **0.7.1**; if `mat --version` is older, it
   does not exist yet — read `run.json` directly in that case.
   `run.json` carries `run.status`, `run.step_success_rate`,
   `run.failed_categories`, and per-step `status / category / error / warning /
   screenshot / logcat_file`. Every step carries a **rule-based five-way
   classification**: `app-crash` (real product bug) / `assert-fail` /
   `locate-fail` / `env-failure` / `unknown`. Open one screenshot file only
   when you need to verify a specific failure visually.
3. **For failures**: `mat diagnose <run_dir>` adds per-step **LLM attribution**
   — a different, **four-way** taxonomy: `app-bug` / `ui-change` / `env-flake` /
   `precondition`, plus evidence + suggestion. The input is a **directory
   containing `run.json`** (e.g. `artifacts/<name>`), not the report file.
   Confidence < 0.6 is flagged `need_human` — treat low-confidence results as
   hypotheses, not conclusions.
4. **Self-healing produces proposals, not rewrites**: `mat heal list <run_dir>`
   shows them; `mat heal apply` writes `*-healed.json` and **never overwrites
   the original case**. Review before applying.
5. **Give the human a live view (not for you)**: while a long job runs, the
   user can watch it — if a local console session exists (`mat console`),
   the run/explore API responses carry a `live` field (`/live/<job_id>`).
   Surface it as a clickable link — same base URL you called the API on
   (`mat console` defaults to `http://127.0.0.1:7420`), e.g.
   `http://127.0.0.1:7420/live/<job_id>` — so the user sees the device
   screen + step events in real time. You do not
   need this page — your ground truth stays `run.json` / tool outputs.

## Common Pitfalls (real-device lessons)

- **Vendor keyboards can block text injection** (Huawei/Honor secure keyboard):
  the input step "passes" but the field stays empty and login then fails with
  "账号或密码错误". MAT ≥0.5.0 detects this automatically (read-back after
  set_text) and falls back to `adb shell input text` — reliable for ASCII;
  CJK fields may still fail, but the failure surfaces as a real step failure
  instead of a silently empty field.
- **First-launch onboarding screens** (账号引导/隐私弹窗) sit in front of the real
  UI: walk through them once manually, or add a tap step for "继续但不同步".
- **Screen must be on and unlocked**: `mat run` wakes the device, but a locked
  device mid-run still stalls — check `mat doctor` if steps hang.
- **Login flows need credentials stored via `mat cred`** (the only hidden
  prerequisite): save once with `mat cred set` (e.g. name `测试号`; input is
  hidden), reference it in case params as `${cred.测试号}` — the engine injects
  the real value at run time and it never appears in reports/logs. If a login
  case fails with wrong-password errors, check the credential exists
  (`mat cred list`) before blaming the case. See `mat cred --help`.
- **Exploration is not deterministic**: page-graph walks depend on device state
  (toggles, logged-in state). Run twice and compare distributions before
  concluding anything; a "perfect 100%" on 4 steps deserves suspicion, not celebration.

## Best Practices

- **Locator choice**: `res-id` renames are the most common app-update breakage,
  and the page graph from `mat index` heals them for free. Without an index,
  prefer `text` locators (their exact→description→contains chain survives copy
  tweaks) and write a `fallback` locator.
- Keep test cases **self-contained**: never depend on state left by a previous
  case; declare needed preconditions in the case itself.
- One behavior per case — a failing case should pinpoint one regression.
- Reports are single-file HTML with inline screenshots: attach them to bug
  reports as-is. (Humans read the report; agents read `run.json`.)
- After `explore`: `mat promote <pkg>` turns walked paths into replayable
  cases; `mat coverage <pkg>` lists graph controls no case touches. Both are
  one-shot follow-ups — not part of the main loop.

## Prior Art

The shape of this skill — a decision tree up front, reconnaissance before action,
real-device pitfalls, and scripts treated as black boxes — follows the convention
established by Anthropic's `webapp-testing` skill (Apache-2.0). The structure is
borrowed; every command, flag, threshold, label, and pitfall above is MAT's own and
was verified against this CLI. Devices reached over `adb` are the part that skill
does not cover.

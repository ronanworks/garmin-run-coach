# garmin-run-coach

English | [简体中文](README.zh-CN.md)

A running-coach skill for AI agents (an Agent Skill in `SKILL.md` format). It reads your own Garmin watch data, analyses your training against the research literature, sets race goals, builds a periodized training plan, and writes the workouts to your Garmin Connect calendar so they sync to your watch.

Design principle: **scripts compute the numbers, rules make the judgements, and the literature backs the rules.** Every conclusion is labelled as one of:
- what the data shows
- what research shows (with an evidence ID)
- coaching practice (a heuristic, not a research finding)

When the evidence is not strong enough, the answer is "unclear".

> **Language:** the skill's instructions, rule files and generated reports are written in Chinese. Agents can still talk to you in English, but the reports and plans the scripts produce are in Chinese. The detailed install guide ([安装说明.md](安装说明.md)) is also in Chinese.

## What it does

| Feature | Details |
|---|---|
| Data access | Guides you through creating a Garmin Personal API key (global and China regions), then pulls activities, per-second FIT data, HRV, sleep and resting HR to your own computer |
| Training analysis | Weekly volume and long runs, three-zone intensity distribution, single-run spikes, load and monotony, recovery markers, daily routine, best efforts, critical speed, multi-model race prediction |
| Heart rate vs. pace | Tracks "HR at a given pace" and "pace at a given HR" from steady segments. It estimates your personal noise first, then gives a three-way verdict (likely better / unclear / likely worse). Comparisons stay within the same apparent-temperature band, and cadence-locked HR is excluded |
| Session and race review | Compares each workout step on your watch with its pace and HR targets, measures recovery between reps, and flags suspicious HR data. Races add splits, grade-adjusted pace, wall detection, HR/speed decoupling and weather |
| Goals and weather | Three goal tiers with checkpoints, and how much to slow your target for race-day WBGT |
| Training plan | Periodized plan generator with 14 rule checks, including a strength module and a standard aerobic check run |
| Daily decision | "Change today only": combines HRV, resting HR, sleep and how you feel to decide whether today stays as planned or turns easy. The rest of the plan stays fixed |
| Return to training | After illness, injury or a break, tiers by days off. Illness follows the IOC consensus symptom checklist and self-test session. A check run measures what you lost. No single run more than 110% of your longest run in the past 30 days |
| Weekly review | Plan vs. actual, a weekly pain question, next week's single-run cap, and an "if…then…" plan. It can run on a schedule |
| Race week | Caffeine dose by body weight, a warm-up workout by race distance, pre-race sleep, painkiller and cardiac reminders |
| Fueling | Sweat rate by weighing, a race fueling plan, and a gut-training progression |
| Citation self-check | Checks evidence IDs before a report goes out, blocks fabricated references, and stamps the skill version and model |
| Write to your watch | Creates Garmin workouts and schedules them (browser automation, garmin_mcp, or a manual checklist). Always previews first and waits for your OK |

The scripts use only the Python 3 standard library; nothing to install.

## Install

1. Download this repository: click the green **Code → Download ZIP**, unzip it, and rename the folder to `garmin-run-coach`.
2. Put the `garmin-run-coach` folder in your agent's skills directory. For Claude Code, that is `~/.claude/skills/garmin-run-coach/`.
3. Restart your agent and say something like: "Analyse my Garmin data — I want to run a marathon next spring."

## Privacy

- Your heart rate, sleep and GPS data stay on your computer in `~/garmin-coach/`. Nothing is uploaded, and this repository contains no personal data.
- A Garmin Personal API key may only be used for your own data. Don't share it. Type it into your own terminal, never into a chat.
- Weather lookups use city-level coordinates only.

## Disclaimer

- This is a training-advice tool, not a medical device. If you have chest pain, fainting, palpitations, fever, or pain that changes how you run, stop and see a doctor.
- Writing to Garmin Connect uses the unofficial web API or the third-party open-source tool [garmin_mcp](https://github.com/Taxuspt/garmin_mcp). It may break when Garmin changes things; use at your own risk.
- Research findings describe groups, not individuals. Treat them as guidance, not promises.

## Evidence and acknowledgements

- Evidence library: `references/evidence.md`, 138 entries, each with an evidence grade and verification status. `scripts/check_refs.py library --online` re-checks every PMID against PubMed.
- Data: Garmin Connect Personal API. Historical weather: [Open-Meteo](https://open-meteo.com/).
- The second workout-writing route is based on the open-source project [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) (MIT licence).
- This project is not affiliated with Garmin.

## License

[MIT](LICENSE): free to use, modify and share (including commercially), as long as you keep the copyright and licence notice. No warranty.

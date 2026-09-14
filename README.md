# ReShuffle

Discord bot that shuffles members in a voice channel, keeps a live order as people join/leave, and can auto-run via scheduled events.

## Features

- Optional book club extension: books, participants, native Discord meetings, host rotation, private preparation plans, essays, and durable reminders. Book and meeting cards let each reader create an essay forum post, open their existing work with **Открыть моё эссе**, and browse published essays. The create button reuses imported and manually registered work before an empty draft. New posts use a shared webhook with the reader's server nickname and avatar; readers write and edit their own messages inside. An empty header does not count as an essay. The essay forum needs Manage Webhooks and Manage Threads; `essay_webhooks: false` keeps the original bot-header mode. See [Russian setup and command guide](docs/BOOK_CLUB.md).
- Optional temporary essay archive import through a separately installed Codex CLI: allowlisted administrators preview a bounded archive sample, request a scan, review the proposed book/author associations, then explicitly copy selected essays into the club forum. With webhooks enabled, headers, essay text and files use the author's nickname and avatar. Generated source links and permanent import markers are omitted from posts; originals and internal source bindings remain. A temporary recovery marker is removed after the Discord message ID is saved; existing webhook copies can be cleaned in place with `restyle`. The importer is disabled by default and has a persistent launch quota; ordinary chat messages never invoke Codex. Failed scans can accept an audited human plan, and completed imports can be restyled in their existing topics, without another model call.
- Shuffle members from your current voice channel with a hybrid command
- Timed SDG breakout shuffle that physically moves members between rooms every 5 minutes
- Live-updating list that reacts to joins/leaves
- Schedule a voice event that auto-starts a shuffled list and auto-completes
- Attach shuffled-list automation to existing scheduled events
- List scheduled events and IDs
- Persist voice-channel activity per user in SQLite
- Report recent voice sessions, daily totals, and weekly totals

## Requirements
- Python 3.10+
- Discord bot with Message Content and Server Members intents enabled
- Permissions: View Channels, Send Messages, Read Message History
- For scheduling: Manage Events permission

## Setup
1. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`. Optionally set `RELIABLE_ROLE_ID` and `TRUSTED_ROLE_ID`.
2. Install dependencies:

```bash
python -m venv .venv
. .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
```

3. Run the bot:

```bash
python Shuffle.py
```

## Commands
- `/shuffle [exclude]` (or `!shuffle [exclude]`) - shuffle members in your current voice channel; users with `Товарищ` or `надежный` can exclude members by mention, ID, or exact name
- `/shuffle_priority [exclude]` - one-off shuffle with `Надежный`, then `Товарищ`, first when available
- `/shuffle_priority_channel_add <voice_channel>` - make `/shuffle` use priority-first order in a voice channel; `MasterMind` does this by default
- `/shuffle_priority_channel_remove <voice_channel>` - remove a manually configured priority-first shuffle channel
- `/shuffle_priority_channel_list` - list priority-first shuffle channels
- `/sdg_shuffle` - start timed breakout-room shuffling from your current voice channel; role `нашедшийся` stays together for 10 minutes, role `core` works like the old `**`, everyone else reshuffles every 5 minutes
- `/sdg_shuffle_stop` - stop the active timed SDG shuffle in the server
- `/sdg_next_round` - force the active SDG shuffle to switch to the next round now; only `Товарищ`, `надежный`, or `Move Members`
- `/sdg_graph` - show the active SDG weighted partner graph; only `Товарищ`, `надежный`, or `Move Members`
- `/sdg_days [voice_channel]` - list members in a voice channel with how many full days they have been on the server
- `/shuffle_exclude_add <users>` - add users to a persistent exclusion list for all future shuffles; only `Товарищ` or `надежный`
- `/shuffle_exclude_remove <users>` - remove users from the persistent exclusion list; only `Товарищ` or `надежный`
- `/shuffle_exclude_list` - show the current persistent exclusion list; only `Товарищ` or `надежный`
- `/shuffle_hot_joiners_on` - allow brand-new late joiners to be added to active shuffles; only `Товарищ` or `надежный`
- `/shuffle_hot_joiners_off` - prevent brand-new late joiners from being added; reconnects still return; only `Товарищ` or `надежный`
- `/shuffle_hot_joiners_status` - show the current hot-joiner setting; only `Товарищ` or `надежный`
- `/shuffle_count_month [YYYY-MM]` - show saved MasterMind shuffle member counts for a month; defaults to the current month
- `/schedule_event` - schedule a voice event that auto-runs a shuffled list in the command channel
- `/schedule_event_menu` - open a modal to schedule an event
- `/attach_event <event_id>` - attach shuffled-list automation to an existing scheduled event; the bot will not move members between voice rooms
- `/detach_event <event_id>` - detach/disable shuffled-list automation for one scheduled event occurrence
- `/event_shuffle_target_add <voice_channel> [target_channel]` - auto-post shuffled lists for scheduled events in a voice channel; target defaults to that voice-channel chat; only `Товарищ`
- `/event_shuffle_target_remove <voice_channel>` - remove an auto-post target for a voice channel; only `Товарищ`
- `/event_shuffle_target_list` - list configured scheduled-event auto-post targets; only `Товарищ`
- `/event_shuffle_list` - list scheduled events with shuffle attachment/auto-target status; only `Товарищ`
- `/list_events` - list scheduled server events and their IDs
- `/voice_stats [member]` - show today, this week, and all-time voice totals for a member
- `/voice_sessions [member] [limit]` - show recent tracked voice sessions for a member
- `/voice_daily [member] [days]` - show per-day totals for the last N days
- `/question` (or `!question`) - pick a random human from your voice channel and give them a non-repeating question
- `/ping` - test if the bot is responsive
- `!sync` - sync application commands to the current guild

## Configuration

- `BOOKCLUB_ENABLED` (optional, default `false`) — load the book club extension. Run `/club setup` as the server owner or a member with Manage Server to create its category, two text channels, two forums, voice channel, essay webhook, and initial catalog. Repeating the command checks and repairs missing resources; `check_only:true` checks without changes. Enable Community manually first; the bot needs Manage Channels and its normal club permissions. Existing human access rules and channel names are preserved.
- `BOOKCLUB_CONFIG_FILE` (optional) — leave empty for `/club setup`; channel IDs and settings persist in the existing SQLite database. Alternatively use `bookclub.example.json` to bind existing channels and restrict enabled servers. Unchanged file values do not undo channel repairs; explicit edits take effect on restart. `/club publish` remains available for manual configuration. See [setup and recovery](docs/BOOK_CLUB.md).
- The catalog and `/club books` include **Добавить книгу** and **Загрузить список** buttons. Organizers can attach UTF-8 TXT/CSV/TSV/JSON to `/club library import` (64 KiB, 100 books) and confirm a preview; no model is called. `/club setup` creates forum tags while preserving renamed channels, topics and tags. Set granular role permissions directly in Discord: ordinary setup and restart preserve them; only explicit `repair_permissions:true` repairs the bot's own permissions. Вестник is for manual organizational announcements; Площадь is for casual chat. The bot posts a static description in each and keeps schedules in book cards and server events.
- `BOOKCLUB_IMPORT_CONFIG_FILE` (optional) — path to a separate, startup-only importer configuration. Copy `bookclub.import.example.json` to `bookclub.import.json`, replace all user/server/source-channel IDs, and enable it for the migration window. Default `max_runs: 1` is shared across allowed servers and survives restarts. `/club import login` returns the official device-login URL and code; `preview` checks the selected range without login or quota use, while `scan`, `review`, and `apply confirm:true` separate analysis from copying. `restore run:ID file:plan.json confirm:true` recovers a failed/unknown scan from an approved plan; `restyle run:ID` previews corrections to an already completed import, followed by `confirm:true`. Neither command invokes Codex or resets its budget. Restyling keeps topics and participant replies but replaces verified bot copies with new webhook messages at the end of each topic. Afterward set `enabled: false` and restart. Codex CLI 0.152.1 or newer must be installed on the bot host (inside the container for Docker). See [temporary import and limits](docs/BOOK_CLUB.md#временный-импорт-старых-эссе-через-codex).
- `DISCORD_TOKEN` (required)
- `RELIABLE_ROLE_ID` (optional) - numeric role ID; if unset, edit `RELIABLE_ROLE_NAME` in `Shuffle.py`
- `TRUSTED_ROLE_ID` (optional) - numeric role ID; if unset, edit `TRUSTED_ROLE_NAME` in `Shuffle.py`
- `SDG_NEWCOMER_ROLE_ID` (optional) - numeric role ID for `нашедшийся`; if unset, the bot matches by role name
- `SDG_CORE_ROLE_ID` (optional) - numeric role ID for `core`; if unset, the bot matches by role name
- `RESHUFFLE_DATA_DIR` (optional) - writable directory for runtime state files; useful in Docker
- `QUESTION_BANK_FILE` (optional) - path to editable JSON question list; defaults to `questions.json` in the runtime data directory
- `MASTERMIND_SHUFFLE_CHANNEL_ID` (optional) - voice channel ID for saved MasterMind shuffle counts; defaults to `1434301778605899808`
- `MASTERMIND_SHUFFLE_GOAL` (optional) - green-goal member count for MasterMind reports; defaults to `15`

## Voice Tracking Storage
- Voice activity is stored in `voice_activity.sqlite3` inside the runtime data directory
- Active sessions survive bot restarts and are reconciled on reconnect/startup
- Persistent shuffle exclusions are stored in `persistent_shuffle_exclusions.json` inside the runtime data directory
- Guild shuffle settings, including hot-joiners and priority shuffle channels, are stored in `shuffle_settings.json` inside the runtime data directory
- Scheduled-event auto-post targets are stored in `event_auto_shuffle_targets.json` inside the runtime data directory
- Exclusion and hot-joiner setting operations are audited to `shuffle_admin_audit.jsonl` inside the runtime data directory
- MasterMind shuffle member-count changes are stored in `shuffle_counts.jsonl` inside the runtime data directory
- Voice questions are read from `questions.json`; used-question cycle state is stored in `question_state.json`

## Docker
```bash
docker build -t reshuffle .
docker run --env-file .env reshuffle
```

For persistent runtime state with Docker Compose:

```bash
docker compose up --build -d
```

The included `docker-compose.yml` mounts a named volume at `/data` and sets `RESHUFFLE_DATA_DIR=/data`, so `persistent_shuffle_exclusions.json`, `shuffle_settings.json`, scheduled-event auto-post targets, audit logs, `shuffle_counts.jsonl`, and the SQLite voice activity database survive container recreation.

The default image does not install Codex. To use the temporary importer, build the optional CLI variant:

```bash
docker build --build-arg INSTALL_CODEX=true --build-arg CODEX_VERSION=0.152.1 -t reshuffle-codex .
```

Run that image with the same persistent `/data` volume and set `BOOKCLUB_IMPORT_CONFIG_FILE=/data/bookclub.import.json`. A Codex installation or login on the Docker host is not automatically available inside the container. The importer keeps its separate login under `/data/bookclub-codex-profile`; keep this volume private and persistent. Do not include authentication files in the image or repository. CLI timeouts and launch quotas limit importer activity; `max_accounted_tokens` is accounting protection, not a hard token ceiling for one response.

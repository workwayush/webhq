# webhq

Automated sender for the Website HQ cold-email outreach system. Runs on a
GitHub Actions schedule, reads "Ready" leads from a Notion database, and
sends each one's pre-written email through Gmail.

**This repo never touches the "Raw Data" table.** It only reads/writes
"Main" and reads "Settings", through a Notion integration that is only
ever shared with those two - never with Raw Data. "Sync Leads" stays a
manual, Claude-run step. That separation is intentional: nothing in this
repo can see the raw lead list or run the sync.

## How the schedule works

`.github/workflows/send-emails.yml` fires twice a day in UTC (to cover
both US daylight-saving offsets), Monday-Saturday. The script checks the
real US-Eastern clock and only does real work on the firing that lands
near 9:30 AM ET; the other exits in a couple of seconds with no API calls.

## Required GitHub secrets

Add these under **Settings -> Secrets and variables -> Actions -> New
repository secret**:

| Secret | Value |
|---|---|
| `NOTION_TOKEN` | Secret of the internal Notion integration shared with Main + Settings (not Raw Data) |
| `MAIN_DATA_SOURCE_ID` | `70b4eefa-f029-469d-929f-2313c2367439` |
| `SETTINGS_DATA_SOURCE_ID` | `5c5779a0-b4e1-4158-babb-eba4926263ab` |
| `GMAIL_ADDRESS` | The sending Gmail address |
| `GMAIL_APP_PASSWORD` | The 16-character Gmail app password (not your normal Gmail password) |

No other setup is needed - the script only uses Python's standard
library, so there's nothing to `pip install`.

## Notion Settings table

The live controls live in the "Settings" table in Notion (not in this
repo), in a single row called "Current":

- **Pause Sending** - master off switch. While ON, scheduled runs do
  nothing, and manual runs refuse to send unless you tick "ignore_pause".
- **Dry Run** - while ON, the script logs what it *would* send but sends
  no email and makes no Notion changes.
- **Send Days** - which weekdays the scheduled run is allowed to act on.
- **Daily Limit** - max leads processed per run.

Change these in Notion any time; nothing needs to be redeployed here.

## Running a manual test

Go to **Actions -> Send Daily Outreach Emails -> Run workflow** and fill
in the inputs. This matches the test sequence in the Setup Guide:

1. **Dry run** - `dry_run_override: force_true`, leave everything else
   blank. Nothing is sent or changed; check the run log for the leads it
   would have processed.
2. **5 test emails to yourself** - `dry_run_override: force_false`,
   `limit: 5`, `test_email: <your own address>`. Real lead content is
   sent to you instead of the lead, and lead statuses are left
   untouched (so no real leads get consumed by the test).
3. **First real batch** - `dry_run_override: force_false`, `limit: 10`,
   leave `test_email` blank. This sends to real leads and updates their
   Notion status. Check for bounces the next day.
4. **Ramp up** - once step 3 looks clean, just let the daily schedule run
   (it uses the Daily Limit from Notion Settings, currently 75), or raise
   the limit gradually with manual runs.

If `Pause Sending` is ON in Notion (it is, by default) you'll also need
to tick `ignore_pause` for manual test runs, or turn Pause Sending off in
Notion before step 4.

## Turning on failure notifications

In GitHub: profile picture -> **Settings -> Notifications -> Actions**,
make sure failed-workflow emails are enabled for this repo. The script
exits with a failure status if any lead's email fails to send, so you'll
get notified even though the rest of that day's batch still goes out.

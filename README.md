# JobTracker

Pulls the community VFX/animation job sheet into the Notion Job Tracker.
Stateless: no local database. Each run reads existing Posting URL + Role
pairs back from Notion and skips them, including Not Interested rows.

## Setup

1. Create an internal integration at notion.so/profile/integrations and copy
   the token.
2. In Notion, open the Job Tracker database, then `...` -> Connections ->
   add the integration. Without this the API returns nothing.
3. `cp .env.example .env` and fill in the token.

## Run

    docker compose build
    docker compose run --rm jobtracker

Dry run, no Notion writes and no token needed:

    docker compose run --rm jobtracker --dry-run

## Daily schedule (Pi crontab)

    0 7 * * * cd /home/<user>/jobtracker && /usr/bin/docker compose run --rm jobtracker >> /var/log/jobtracker.log 2>&1

## Filtering

All filtering lives in `config.toml`: title keywords, software keywords and
the allowed country list. No code change needed to adjust them.

## Failure modes

- The script refuses to run if the sheet's header row no longer contains the
  expected column names, rather than mapping the wrong fields.
- If the CSV export stops being public, it exits with an error. The fallback
  is the Sheets API with an API key.

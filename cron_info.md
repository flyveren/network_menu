# Cron Job Overview

This project relies on a handful of Python scripts that are scheduled inside the
`navigation` container. Cron is started from `start.sh`, writes into
`/var/log/cron.log`, and uses `/etc/crontabs/root` (symlinked from the repo’s
`cron/root`) as its job list.

All commands run with `cd /usr/src/app`, so paths are relative to the `app/`
directory in the repository.

| Schedule (UTC) | Command | Purpose |
| --- | --- | --- |
| `*/15 * * * *` | `python3 scripts/fetch_news.py --category all` | Refresh the combined news feed. |
| `*/15 * * * *` | `python3 scripts/fetch_news.py --category indland` | Refresh domestic news. |
| `*/15 * * * *` | `python3 scripts/fetch_news.py --category udland` | Refresh international news. |
| `*/15 * * * *` | `python3 scripts/fetch_news.py --category kultur` | Refresh culture news. |
| `*/15 * * * *` | `python3 scripts/fetch_news.py --category debat` | Refresh debate/opinion news. |
| `*/15 * * * *` | `python3 scripts/fetch_weather.py` | Pull the latest weather snapshot. |
| `0 */4 * * *` | `python3 scripts/fetch_voxmeter.py` | Update block-level Voxmeter polling (Rød/Blå). |
| `0 */4 * * *` | `python3 scripts/fetch_voxmeter_parties.py` | Update party-level Voxmeter numbers and history. |
| `*/10 7-17 * * *` | `python3 scripts/scrape_facebook_parties.py` | Scrape each party’s latest Facebook post during the day (07:00–17:59). |
| `0 18-23 * * *` | `python3 scripts/scrape_facebook_parties.py` | Evening sweep at the top of each hour (18:00–23:00). |
| `0 0-6 * * *` | `python3 scripts/scrape_facebook_parties.py` | Overnight sweep hourly (00:00–06:00). |

> All times are in UTC, matching the container’s clock. Adjust the cron fields
> if you need local-time execution.

## Inspecting and Editing Jobs

1. **Host file**: update `cron/root` in the repo.
2. **Container file**: copy the updated content into `/etc/crontabs/root`:
   ```sh
   docker compose exec navigation sh -c 'cat /usr/src/app/cron/root > /etc/crontabs/root'
   docker compose exec navigation sh -c 'chown root:root /etc/crontabs/root && chmod 600 /etc/crontabs/root'
   ```
3. **Restart cron** so it reloads the file:
   ```sh
   docker compose exec navigation pkill crond
   docker compose exec navigation crond -b -l 2 -L /var/log/cron.log
   ```

## Monitoring

* **Ensure the daemon is running**:
  ```sh
  docker compose exec navigation ps aux | grep crond
  ```
* **Tail the log**:
  ```sh
  docker compose exec navigation tail -n 200 /var/log/cron.log
  ```
  Every job logs its `[CRON] … Running:` line (via the Facebook wrapper) or a
  script-specific `[fetch_*]` entry.

## Manual Script Execution

If you need to run a job immediately (outside its schedule), execute it inside
the container from `/usr/src/app`, e.g.:
```sh
docker compose exec navigation bash -lc \
  "cd /usr/src/app && python3 scripts/scrape_facebook_parties.py"
```
Manual runs still log to `/var/log/cron.log` and write to `app/data/`.

## Troubleshooting Checklist

1. **Crontab permissions** – BusyBox cron ignores files that are not owned by
   root or that are writable by others. Keep `/etc/crontabs/root` at `600`.
2. **Driver dependencies** – The Facebook scraper requires Firefox/Geckodriver,
   already installed in the Docker image, but check for updates if scraping
   fails.
3. **Environment variables** – Facebook login depends on
   `FACEBOOK_EMAIL`/`FACEBOOK_PASSWORD` defined in `.env` (mounted into the
   container).
4. **Disk/log growth** – Rotate or truncate `/var/log/cron.log` if it becomes
   too large (`: > /var/log/cron.log`), then restart cron.


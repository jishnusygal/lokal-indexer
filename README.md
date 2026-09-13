# Lokal Indexer

A single-container FastAPI Torznab indexer with SQLite persistence, a Chromium scraper, cached torrent files, and Telegram failure alerts. Runtime configuration lives in SQLite; the initial admin password is supplied through a private environment variable.

## Run

```sh
mkdir -p config/lokal-indexer/data
docker compose up -d --build
```

Open **http://localhost:8000/setup** on the first run and create the admin password. The setup page is available only until the first account is created. For unattended deployments, copy `config/lokal-indexer/.env.example` to `config/lokal-indexer/.env`, set `LOKAL_ADMIN_PASSWORD`, and start the container. Like `iptv-gtw`, the **Indexer URL** and **Indexer base URL** resolve automatically from the incoming request and reverse-proxy headers. When Prowlarr calls the service by its Docker service name, generated URLs use that internal hostname; when a browser reaches it through Traefik, URLs use the forwarded public hostname. Set `LOKAL_PUBLIC_URL` only when a fixed URL is required. Only a salted `scrypt` hash is stored in `config/lokal-indexer/data`. This password is separate from the Torznab API key and persists in the data volume. Copy the generated API key from the settings page, configure the target URL and CSS selectors, and click **Save settings**, then **Sync now**. Reload to view results. Sync intervals are in minutes; saving reschedules the next run without restarting. A configured installation also syncs on startup. Empty target URL pauses scraping; an in-flight sync finishes with its original settings.

To rotate the admin password, use **Settings → Admin Password**. Existing plaintext password files are upgraded to salted `scrypt` hashes on startup. Treat the data and torrent directories as private: SQLite contains API and Telegram credentials. Use TLS at a reverse proxy and restrict network access to trusted clients before exposing the service beyond your machine. Access logging is disabled to avoid logging API keys in query strings. Do not enable query-string logging at your proxy.

## Sonarr / Radarr

1. In Settings → Indexers, add **Torznab → Custom**.
2. Set the URL to `http://YOUR_HOST:8000`, API path to `/api`, and enter the API key shown in `/settings`.
3. Select TV category **5000** in Sonarr or Movies **2000** in Radarr. Test and save.
4. Set **Public service URL** in Lokal to the address your applications **and download client** can reach, for example `http://your-indexer-host:8000`. Inside a shared Docker network, use the Docker service name and port. `localhost` inside another container points to that container.

Authenticated endpoints:

- `/api?t=caps&apikey=…`
- `/api?t=search&q=example&apikey=…`
- `/api?t=movie&imdbid=1234567&apikey=…`
- `/api?t=tvsearch&q=example&season=1&ep=2&apikey=…`
- `/download/{info_hash}?apikey=…`

Search supports `cat`, `offset`, and `limit` (1–100), literal case-insensitive title terms, numeric IMDb IDs with optional `tt`, season and episode. Empty searches return recent releases. Capabilities advertise only supported ID searches; TVDB/TMDB resolution is not implemented. Magnet-only releases expose magnet enclosures directly, while torrent releases use the authenticated local download endpoint. Rotating the API key invalidates previously generated download links.

## Configure the source adapter

There is no universal forum HTML layout. This implementation is a complete **configurable CSS row adapter**, not a site-specific scraper. It supports releases directly linked from listing rows, optional same-host pagination, and JavaScript-rendered content. A real target must be checked against this contract before deployment. It does not implement interactive login, topic-detail traversal, CAPTCHA solving, or Cloudflare bypass.

Default selectors match this example:

```html
<article class="release" data-category="tv" data-imdb="tt1234567" data-size="1073741824">
  <span class="title">Example.Show.S01E02.1080p</span>
  <span class="size">1 GiB</span>
  <time datetime="2026-09-12T12:00:00Z"></time>
  <a href="/files/example.torrent">Torrent</a>
  <!-- Or a magnet link with a valid urn:btih info hash -->
</article>
```

- Configure **Release row**, **Title**, **Torrent link**, **Magnet link**, **Size**, and **Publication date** in the UI. Selectors except the row and next-page selectors are relative to a row. Links use `href`; publication dates use ISO 8601 `datetime` attributes.
- Torrent sizes come from bencoded metadata. Magnet-only rows require `data-size` or a size element containing bytes or units (`MB`, `GiB`, etc.).
- `data-category` is `movie` or `tv`. Without it, `S01`/`S01E02` title tokens identify TV; other titles become movies. Season and episode are inferred from these tokens. `data-imdb` is optional.
- Release titles are normalized before storage and Torznab output: leading site branding and video extensions are removed, while language, source, codec, audio, and advertised-size tags are retained for Radarr/Sonarr Custom Formats. The actual torrent payload size remains authoritative for the Torznab `size` field.
- Missing dates use ingestion time. Existing hashes keep their original metadata and publication date. Both hexadecimal and Base32 v1 magnet hashes are supported. Torrent and magnet hashes must agree when both are present.
- **Next page link** is optional. Configure it for pagination; maximum pages bounds each sync. Index pages must stay on the configured host. Torrent links can point to a separate HTTP(S) host. Browser cookies are shared with torrent requests during the sync.
- A sync is limited to 15 minutes and 2000 rows per page. Torrent metadata is limited to 10 MiB after download (and rejected early when Content-Length is provided). HTTP responses without Content-Length are buffered by Playwright, so this is not a streaming memory bound. Configure trusted sources only.

Zero rows, malformed records, download failures, and browser errors mark the sync as failed. Release inserts are transactional: failed syncs do not publish partial results. Validated torrent files are written atomically; a failed transaction may leave harmless orphan files. Duplicate info hashes add no new records. The last 1000 sync logs are retained and the latest 20 appear in the UI. The release/torrent cache has no automatic eviction; monitor disk space.

For Telegram, create a bot with BotFather, start a conversation with it (or add it to your group), then set the bot token and chat ID in the UI. Failure alerts use MarkdownV2. Telegram outages do not hide the original failure. Notification delivery is best effort, without retry queues.

## Storage and operations

The default Compose deployment stores SQLite and the hashed admin password in `config/lokal-indexer/data`, and torrent files in `./torrents`. Set `LOKAL_DATA_DIR` and `LOKAL_TORRENT_DIR` in the shell or `.env` to use different host directories. Back up both directories with the container stopped so SQLite WAL and torrent files form a consistent snapshot. Restore them together, preserving UID 10001 ownership. Schema tables are created on startup; future schema changes require migrations rather than merely restarting with changed models.

The container runs as UID 10001 with one Uvicorn worker. **Do not scale replicas or worker count**: the APScheduler and scrape lock are process-local. `/health` checks process/database availability; sync failures are reported separately in the UI. Chromium is installed with native Debian dependencies during the multi-stage build. The generous shutdown grace period permits an active bounded scrape to finish.

## Local development and tests

Python 3.11+:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m playwright install chromium
.venv/bin/uvicorn main:app --no-access-log
# In another terminal:
.venv/bin/pytest -q
```

Run from the repository root. Local data uses `config/lokal-indexer/data/indexer.db` and `./torrents`. Tests use isolated temporary storage and local HTML/HTTP fixtures. They exercise authentication, CSRF, settings persistence, rescheduling, XML/search semantics, torrent handling, scrape failure notifications, and a real headless-browser scrape. No live forum or Telegram credentials are needed.

Protocol reference: [Torznab specification](https://torznab.github.io/spec-1.3-draft/torznab/Specification-v1.3.html).

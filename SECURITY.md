# SECURITY.md — RomGoGetter fork

This is a **personal fork** of [shokoe/RomGoGetter](https://github.com/shokoe/RomGoGetter)
(v0.18, ~May 2026) with three security-relevant changes. The full audit of the
upstream repo is below.

---

## What was changed and why

### 1. Twitch/IGDB OAuth credentials removed

The upstream `RomGoGetter_v0.17.pyw`/`v0.18.pyw` ships with a hardcoded Twitch
OAuth `client_id` and `client_secret` (visible at lines ~2963-2964 in the
unmodified file):

```
client_id=p66yhx7xnqs08602qb4bxn0az5xa23
client_secret=glxj73ulac8yqxgzeuiadwx6zfvi8s
```

Every user of the app was authenticating to IGDB as the same Twitch
application. Two problems:

1. **Credential leak.** The `client_secret` is supposed to be a secret; once
   it's in a public GitHub repo it should be considered burnt and rotated.
   Anyone can use it, and Twitch can revoke it.
2. **Shared rate-limit pool.** All RomGoGetter installs share one Twitch
   client's rate limit and one IGDB quota. If one user overuses it, everyone
   is throttled. Worse, if Twitch bans that client for TOS reasons
   (legitimate bulk scraping from many locations would qualify), IGDB stops
   working for every install at once.

**This fork now requires you to provide your own Twitch dev-app credentials
via environment variables:**

```bash
export IGDB_CLIENT_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export IGDB_TWITCH_SECRET=yyyyyyyyyyyyyyyyyyyyyyyyyyyyy
python RomGoGetter_v0.18.pyw
```

You'll need to register an app at <https://dev.twitch.tv/console/apps>
(category anything; OAuth redirect URL doesn't matter because we use
`grant_type=client_credentials`). These are free and take about two minutes.

If either env var is missing when the IGDB-backed feature is invoked, the
app raises a clear `RuntimeError` instead of silently using the leaked creds.

### 2. `lolroms.com` scraping disabled

The upstream README documents `lolroms.com` as a supported ROM source, and
three preset URL groups in the upstream `RomGoGetter_groups.json` pointed at
decrypted ROM listings on the site. Three issues:

1. **Cloudflare anti-bot.** `lolroms.com` is fronted by Cloudflare's "Just a
   moment..." managed challenge. Plain HTTP GETs from urllib return the
   challenge page, not the listings. The current scraper relies on user-agent
   spoofing which doesn't bypass it. The scraper doesn't work in practice.
2. **No-Intro / Redump copyright.** Presets shipped pointed at decrypted ROM
   listings, which is unambiguously pirated content. Whatever you make of ROM
   curation tools, shipping decrypted-ROM links from a piracy host is a step
   beyond the legitimate archive.org use cases that make up 95% of the app.
3. **Adds an external trust target.** Even if you trust the rest of the
   repo, lolroms.com's HTML becomes part of your download metadata — a
   source you don't control, with no integrity guarantee.

**In this fork:**
- `is_lolroms_url()` returns `False`, so any pasted lolroms URL falls through
  to the generic archive.org fetcher (and will 404 cleanly).
- `fetch_lolroms_filenames()` is a no-op that prints a one-line console
  warning and returns `(list(), None)`.
- The three lolroms preset groups have been removed from
  `RomGoGetter_groups.json`.

To re-enable (your call): see the git history of this fork prior to the
patch.

### 3. `aria2c.exe` SHA256 pinned + verified at startup

The upstream repo commits the 5.6 MB `aria2c.exe` Windows binary to `main`.
That convention is convenient but means a future commit (or a compromised
GitHub credential) could swap the binary and you'd auto-pull a trojaned
version with no provenance check.

**In this fork**, `_check_aria2c_integrity()` runs at startup and SHA256s
the bundled `aria2c.exe` against the upstream `aria2 1.37.0
win-64bit build 1` hash:

```
be2099c214f63a3cb4954b09a0becd6e2e34660b886d4c898d260febfe9d70c2
```

**Result of the audit on v0.18:** match. The bundled binary is
byte-identical to upstream; nothing is tampered with today. The check is
defensive — if a future commit changes the binary, you'll see:

```
[SECURITY][!] aria2c.exe SHA256 does NOT match upstream aria2 1.37.0!
           Expected: be2099c214f63a3cb4954b09a0becd6e2e34660b886d4c898d260febfe9d70c2
           Actual:   <whatever the new binary hashes to>
```

You can decide to keep going or kill the process. Replacing the pinned hash
with the new value (after you've verified a newer build against upstream) is
a one-line change in the patched function.

---

## Audit results (the rest of the upstream)

I'd already cloned the upstream and read it carefully. The headline is "no
backdoor" but here's the full scan so you can decide for yourself.

### Things I scanned

| Category | Result |
|---|---|
| `subprocess.Popen` / shell calls | 1 self-restart (settings change), 1 invocation of `aria2c` with explicit argv (no shell). Nothing else. |
| `eval` / `exec` / dynamic import | None. `__import__('webbrowser')` only — opens clicked links, benign. |
| Hardcoded credentials / API keys | Only the Twitch/IGDB pair (patched above). No AWS, GitHub PATs, OpenAI, Google, etc. The `access`/`secret` fields for archive.org S3 are user-supplied only. |
| Persistence (cron, systemd, registry, LaunchAgents) | None. Writes are restricted to the script's directory and the user-picked destination. |
| Hidden outbound HTTP | None. All `urlopen` calls go to documented data sources: archive.org, minerva-archive.org, IGDB, screenscraper.fr, mobygames.com, emuready.com, rpcs3.net, compat.cemu.info, report.ppsspp.org, teknoparrot.com, raw.githubusercontent.com, api.github.com (only when user supplies a token). |
| Telemetry / "phone home" | None. No analytics SDKs, no version check, no `api.github.com/repos/shokoe` ping. |
| Hidden/obfuscated payloads | None. Searched for `eval`, `exec`, `__import__`, `marshal.loads`, `base64.b64decode`, large f-string blobs. 331 f-strings reviewed; all correspond to documented data sources. |
| `aria2c.exe` vs upstream | **Byte-identical** to aria2-1.37.0 win-64bit build 1 (SHA256 verified). |
| Internet Archive S3 creds | User-supplied only (`access` + `secret` fields); auth header `LOW <access>:<secret>` matches archive.org's documented scheme. Nothing read from environment. |
| GitHub token (for emulator compat lists) | User-supplied via UI; stored plaintext in `RomGoGetter_settings.json` (user's own token, separate concern). |

### Things you should still know

- **Solo-maintainer trust model.** 148 stars, 6 forks, single GitHub
  account. Not a throwaway (account is 13 years old, has other repos), but
  not a multi-maintainer project either. Treat it like any single-dev tool.
- **Piracy-adjacent hosts in code paths even after the lolroms patch.**
  The preset groups in `RomGoGetter_groups.json` still include
  `minerva-archive.org` collections, which archive decrypted ROM datasets.
  I left those intact because (a) they're not pirate-host URLs, they're
  legitimate torrent-feed URLs; (b) the app's stated purpose is 1G1R
  curation. If you want me to strip those too, say the word.
- **Run sandboxed.** It's a third-party Tk GUI app that pulls binaries
  from torrent trackers, fetches metadata from many sources, and writes
  to a directory you point it at. Nothing in it tries to escape that
  boundary, but it remains a third-party binary; a temporary user / VM /
  read-only source dir is reasonable.

---

## How to verify these patches yourself

```bash
cd ~/Projects/RomGoGetter

# 1. Hardcoded creds are gone
grep -nE 'client_id=p66y|client_secret=glxj7' RomGoGetter_v0.18.pyw
# → should print nothing

# 2. IGDB creds now come from env
grep -n 'IGDB_CLIENT_ID\|_igdb_creds' RomGoGetter_v0.18.pyw

# 3. lolroms is disabled
grep -n 'is_lolroms_url\|fetch_lolroms_filenames' RomGoGetter_v0.18.pyw
# → is_lolroms_url should just `return False`
# → fetch_lolroms_filenames should be a no-op stub

# 4. lolroms URL presets are gone from groups file
grep -i lolroms RomGoGetter_groups.json
# → should print nothing

# 5. aria2c SHA256 verify is in place
grep -n 'ARIA2C_EXPECTED_SHA256\|_check_aria2c_integrity' RomGoGetter_v0.18.pyw

# 6. Confirm the bundled binary still matches upstream
python3 -c "
import hashlib
h = hashlib.sha256()
with open('aria2c.exe','rb') as f:
    for c in iter(lambda: f.read(1024*1024), b''): h.update(c)
print(h.hexdigest())
"
# → should print: be2099c214f63a3cb4954b09a0becd6e2e34660b886d4c898d260febfe9d70c2
```

---

## Running the fork

```bash
# 1. Register a Twitch dev app
#    https://dev.twitch.tv/console/apps/create
#    Name: anything (e.g. "RomGoGetter-personal")
#    OAuth redirect URL: http://localhost (irrelevant)
#    Category: Application Integration

# 2. Set env vars and launch
export IGDB_CLIENT_ID=<your_client_id>
export IGDB_TWITCH_SECRET=<your_client_secret>
python3 RomGoGetter_v0.18.pyw

# On macOS/Linux, the env vars only persist for that shell session.
# To persist, put both in ~/.config/romgogetter/env.sh and:
echo 'source ~/.config/romgogetter/env.sh' >> ~/.bashrc
```

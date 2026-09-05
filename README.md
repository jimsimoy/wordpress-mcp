# wordpress-mcp

A minimal [Model Context Protocol](https://modelcontextprotocol.io) server that exposes basic
WordPress site management (users, posts/pages, ACF fields) over the WP REST API.

One server instance manages one WordPress site — pick which site by pointing `--config` at that
site's config file.

## Tools

- `wp_site_info` — basic site info (name, URL, WP version, description)
- `wp_list_users` / `wp_get_user` / `wp_create_user` / `wp_update_user` / `wp_delete_user`
- `wp_list_posts` / `wp_get_post` / `wp_create_post` / `wp_update_post` / `wp_delete_post`
  — works with any post type by REST base slug (`pages`, `posts`, or a custom post type's
  `rest_base`)
- `wp_get_acf_fields` / `wp_update_acf_fields` — read/write [ACF](https://www.advancedcustomfields.com/)
  field data, when a site has ACF fields exposed via REST (`show_in_rest`)
- `wp_list_media` / `wp_get_media` / `wp_find_media_by_filename` / `wp_upload_media` / `wp_delete_media`
  — media library. Uploads send the file as a raw binary body with `Content-Disposition`
  (WordPress accepts this; no multipart encoder needed) and then set title/alt/caption in a
  follow-up call. `wp_upload_media` reuses an existing attachment with the same filename by
  default, because WordPress does not dedupe — it appends `-1`, `-2` and creates a new
  attachment every time, which quietly breaks repeat deploys.

## Elementor pages

`wp_create_post` / `wp_update_post` accept a `meta` object, which is what makes Elementor
layouts writable. Elementor registers these keys for REST itself
(`elementor/modules/wp-rest/classes/elementor-post-meta.php`, hooked unconditionally on
`rest_api_init`):

| Key | Type | Notes |
|---|---|---|
| `_elementor_data` | string | the whole layout, as a **JSON string** |
| `_elementor_edit_mode` | string | set to `builder` |
| `_elementor_template_type` | string | e.g. `wp-page` |
| `_elementor_page_settings` | object | accepts `custom_css`, `hide_title` |
| `_elementor_conditions` | array | Pro only |

Pair with `template: "elementor_canvas"` for a page with no theme header/footer.
Elementor regenerates its per-post CSS file automatically after the write.

Two things to know before pointing this at anything that matters:

- **There is no undo.** Read and keep the existing `_elementor_data` before overwriting it.
- **`sanitize_elementor_data()` runs the payload through `kses_post_deep` for any user without
  the `unfiltered_html` capability**, silently stripping attributes. Confirm the target user has
  it (single-site administrators do; some multisite and hardened setups do not).

Media IDs and URLs are embedded in the layout JSON, so a layout moved between environments needs
its attachment IDs remapped — upload first, then rewrite the IDs before writing the page.

## Auth modes

Two, and which you need depends on how the site is protected.

| Mode | Use when | Config keys |
|---|---|---|
| `app_password` | Normal site, no HTTP Basic auth in front of it | `wp_user`, `wp_app_password` |
| `cookie` | Site sits behind htpasswd, or you only have a normal login | `wp_user`, `wp_password`, `http_basic_auth`, optional `wp_login_url` |

**Application passwords cannot be used behind HTTP Basic auth.** Both rely on the
`Authorization: Basic` header and the basic-auth layer consumes it first. That is what `cookie`
mode exists for — it logs in via the form and authenticates with a REST nonce, leaving
`Authorization` free for htpasswd.

`wp_login_url` covers sites that move or hide `wp-login.php` (WPS Hide Login and similar); give it
the slug or a full URL. Cookie logins also append a cache-buster automatically, because managed
hosts will serve a cached login page with `Set-Cookie` stripped — see AGENTS.md for why that
surfaces as a misleading "Incorrect username or password".

Site configs live in `configs/*.json` and are gitignored. Keep it that way.

## Git helpers

```
./git-pull-current.sh              pull the current branch, then show status
./git-push-current.sh              scan, then push the current branch
./git-commit.sh "message"          scan the message, stage all, commit, push
```

**This repo is public, so the push and commit scripts scan before they act.** They check the
outgoing diff *and* the commit messages for credentials, private keys, `user:pass` pairs, `.local`
hostnames, IPs and email addresses, and refuse to push if anything matches. A force-push cannot
unpublish a mistake — old objects stay fetchable by SHA — so the only reliable moment to catch one
is before it leaves.

For names that must never appear here but that would themselves be a leak if hardcoded in a public
script, create `.git-deny-patterns` — one regex per line. It is gitignored and never ships.

```
# .git-deny-patterns
some-client-name
internal-project-code
```

False positives get a narrow exception in `GUARD_ALLOW` in `git-guard.sh`. Don't disable the scan.

## Setup

```bash
pip install -r requirements.txt
cp config.example.json configs/my-site.json
# edit configs/my-site.json with your site's URL and credentials
```

### Auth modes

- `app_password` (default) — HTTP Basic auth using a WordPress
  [application password](https://make.wordpress.org/core/2020/11/05/application-passwords-integration-guide/).
  Simplest option; use this unless you have a specific reason not to.
- `cookie` — logs in with a WordPress username/password and uses the resulting session + REST
  nonce. Useful when the site sits behind HTTP basic auth at the web-server level (e.g. a staging
  environment), since that consumes the `Authorization` header before it reaches WordPress. Set
  `http_basic_auth` (`"user:pass"`) to have the client also satisfy that outer layer.

See `config.example.json` for the full config schema.

## Running

```bash
python3 server.py --config configs/my-site.json
```

This starts an MCP server over stdio. Point an MCP client (Claude Code, Claude Desktop, etc.) at
this command to connect.

Example Claude Code / Claude Desktop MCP config entry:

```json
{
  "mcpServers": {
    "wordpress-my-site": {
      "command": "python3",
      "args": ["/path/to/wordpress-mcp/server.py", "--config", "/path/to/configs/my-site.json"]
    }
  }
}
```

## Notes

- `configs/*.json` is gitignored — those files hold live credentials and should never be
  committed. Keep one config file per site.
- ACF tools only work if the target field group(s) have `show_in_rest` enabled; otherwise the
  tools report that no ACF data is visible rather than guessing.

## Using this against live or staging sites

If an AI agent is driving this tool, see [AGENTS.md](AGENTS.md) first — it sets the rate-limiting
policy for any config pointed at a live or staging site (space out requests, no retry loops, no
bulk calls) and requires reporting a hit-count summary at the end of a task.

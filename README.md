# WordPress MCP — Site & Elementor Automation for AI Clients

<div align="center">

<img src="https://img.shields.io/badge/python-3.9%2B-blue.svg?style=flat-square" alt="Python 3.9+">
<a href="https://github.com/jimsimoy/wordpress-mcp/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg?style=flat-square" alt="License: MIT"></a>
<a href="https://modelcontextprotocol.io"><img src="https://img.shields.io/badge/MCP-compatible-green.svg?style=flat-square" alt="MCP Compatible"></a>
<img src="https://img.shields.io/badge/tools-22-brightgreen.svg?style=flat-square" alt="22 Tools">

**22 tools for managing WordPress sites — users, posts/pages, media, ACF, and writable Elementor layouts — over the WP REST API, for any MCP client.**

by [Jan Ivan Simoy](https://github.com/jimsimoy)

</div>

---

## What is this?

WordPress MCP is a [Model Context Protocol](https://modelcontextprotocol.io) server that exposes WordPress site management over the WP REST API. One server instance manages one WordPress site — pick which site by pointing `--config` at that site's config file, so a single MCP client can run several instances for several sites.

Beyond the standard content-management surface, it can read, back up, and write **Elementor** page layouts directly — something most WordPress tooling doesn't expose, because Elementor registers those fields for REST itself rather than through a public API.

---

## Tools

| Category | Tools | What you can do |
|---|---|---|
| **Site** | 1 | Basic site info (name, URL, WP version) |
| **Users** | 5 | List, get, create, update, delete |
| **Posts & Pages** | 5 | List, get, create, update, delete — any post type by REST base slug |
| **ACF Fields** | 2 | Read/write [ACF](https://www.advancedcustomfields.com/) field data where REST-exposed |
| **Media** | 5 | List, get, find by filename, upload, delete |
| **Elementor** | 4 | Read a layout, back it up, clear the CSS cache, and deploy a layout between environments with attachment IDs remapped |

<details>
<summary>Full tool reference</summary>

| Tool | Description |
|---|---|
| `wp_site_info` | Basic site info (name, URL, WP version, description) |
| `wp_list_users` / `wp_get_user` / `wp_create_user` / `wp_update_user` / `wp_delete_user` | User management |
| `wp_list_posts` / `wp_get_post` / `wp_create_post` / `wp_update_post` / `wp_delete_post` | Works with any post type by REST base slug (`pages`, `posts`, or a custom post type) |
| `wp_get_acf_fields` / `wp_update_acf_fields` | Read/write ACF field data, when a site has ACF fields exposed via REST |
| `wp_list_media` / `wp_get_media` / `wp_find_media_by_filename` / `wp_upload_media` / `wp_delete_media` | Media library management |
| `wp_get_elementor_page` | Read a page's parsed layout, page settings (incl. `custom_css`), template, and referenced attachment IDs |
| `wp_backup_elementor_page` | Save a page's current Elementor layout and settings to a timestamped local JSON file before any overwrite |
| `wp_clear_elementor_cache` | Clear Elementor's generated CSS cache site-wide — required after writing `_elementor_page_settings` |
| `wp_deploy_elementor_page` | Deploy a layout to a page: back up what's there, upload media, remap attachment IDs/URLs for the target site, validate, then write |

</details>

---

## Elementor pages

`wp_create_post` / `wp_update_post` accept a `meta` object, which is what makes Elementor layouts writable — Elementor registers these keys for REST itself (`elementor/modules/wp-rest/classes/elementor-post-meta.php`, hooked unconditionally on `rest_api_init`):

| Key | Type | Notes |
|---|---|---|
| `_elementor_data` | string | the whole layout, as a **JSON string** |
| `_elementor_edit_mode` | string | set to `builder` |
| `_elementor_template_type` | string | e.g. `wp-page` |
| `_elementor_page_settings` | object | accepts `custom_css`, `hide_title` |
| `_elementor_conditions` | array | Pro only |

Pair with `template: "elementor_canvas"` for a page with no theme header/footer. Elementor regenerates its per-post CSS file automatically after the write.

Two things worth knowing before pointing this at anything that matters:

- **There is no undo.** `wp_backup_elementor_page` saves the existing layout before you overwrite it — REST writes replace it wholesale and WordPress keeps no revision of post meta.
- **`sanitize_elementor_data()` runs the payload through `kses_post_deep` for any user without the `unfiltered_html` capability**, silently stripping attributes. Confirm the target user has it before trusting a write.

Media IDs and URLs are embedded in the layout JSON, so a layout moved between environments needs its attachment IDs remapped — `wp_deploy_elementor_page` handles this by uploading media first (idempotent by filename) and rewriting the IDs before writing the page.

See [ELEMENTOR_FINDINGS.md](ELEMENTOR_FINDINGS.md) for the underlying research this was built from.

---

## Auth modes

Two, and which you need depends on how the site is protected.

| Mode | Use when | Config keys |
|---|---|---|
| `app_password` | Normal site, no HTTP Basic auth in front of it | `wp_user`, `wp_app_password` |
| `cookie` | Site sits behind htpasswd, or you only have a normal login | `wp_user`, `wp_password`, `http_basic_auth`, optional `wp_login_url` |

**Application passwords cannot be used behind HTTP Basic auth.** Both rely on the `Authorization: Basic` header and the basic-auth layer consumes it first. That is what `cookie` mode exists for — it logs in via the form and authenticates with a REST nonce, leaving `Authorization` free for htpasswd.

`wp_login_url` covers sites that move or hide `wp-login.php` (WPS Hide Login and similar); give it the slug or a full URL. Cookie logins also append a cache-buster automatically, because managed hosts will serve a cached login page with `Set-Cookie` stripped — see [AGENTS.md](AGENTS.md) for why that surfaces as a misleading "Incorrect username or password".

---

## Requirements

| Requirement | Version |
|---|---|
| Python | 3.9 or later |
| WordPress | Any version with the REST API enabled (default since WP 4.7) |

---

## Installation

```bash
git clone https://github.com/jimsimoy/wordpress-mcp.git
cd wordpress-mcp
pip install -r requirements.txt
cp config.example.json configs/my-site.json
# edit configs/my-site.json with your site's URL and credentials
```

Site configs live in `configs/*.json` and are gitignored — they hold live credentials and should never be committed. See [config.example.json](config.example.json) for the full config schema.

---

## Client Setup

```bash
python3 server.py --config configs/my-site.json
```

This starts an MCP server over stdio. Point an MCP client at it, e.g.:

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

---

## Security

- `configs/*.json` is gitignored — those files hold live credentials and should never be committed. Keep one config file per site.
- **This repo is public, so the included git helpers scan before they act:**

  ```
  ./git-pull-current.sh              pull the current branch, then show status
  ./git-push-current.sh              scan, then push the current branch
  ./git-commit.sh "message"          scan the message, stage all, commit, push
  ```

  They check the outgoing diff *and* the commit message for credentials, private keys, `user:pass` pairs, `.local` hostnames, IPs, and email addresses, and refuse to push if anything matches — a force-push cannot unpublish a mistake, since old objects stay fetchable by SHA, so the only reliable moment to catch one is before it leaves. For names that must never appear here, add a pattern to `.git-deny-patterns` (gitignored, never ships).

- **Using this against live or staging sites:** if an AI agent is driving this tool, see [AGENTS.md](AGENTS.md) first — it sets the rate-limiting policy for any config pointed at a live or staging site (space out requests, no retry loops, no bulk calls) and requires reporting a hit-count summary at the end of a task.

---

## Notes

- ACF tools only work if the target field group(s) have `show_in_rest` enabled; otherwise the tools report that no ACF data is visible rather than guessing.

---

## Project Structure

```
server.py       # MCP server entry point and tool definitions
wp_client.py    # WordPress REST API client
configs/        # Per-site config files (gitignored)
```

The server communicates over stdio using JSON-RPC 2.0, the standard MCP transport.

---

## License

[MIT](./LICENSE) — free to use, modify, and distribute.

---

<div align="center">

[Report a Bug](https://github.com/jimsimoy/wordpress-mcp/issues) · [Request a Feature](https://github.com/jimsimoy/wordpress-mcp/issues)

</div>

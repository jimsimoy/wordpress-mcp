# Agent Instructions — wordpress-mcp

This server talks directly to real WordPress sites over their REST API. If you are an AI agent
(or any automated script) driving this tool, read this before calling any tool against a site
whose config points at a **live** or **staging** environment (as opposed to local dev).

## Live / staging request policy

- **Never send bulk, rapid, or looped requests to a live or staging site.** Going through this
  MCP server instead of `curl`/a browser doesn't exempt you — same budget, same pacing as if a
  person were doing it by hand.
- **Prove the exact sequence against local dev first.** Only run a call chain against live/staging
  once you already know it works.
- **State the request count before you run anything remote.** Say up front how many calls you're
  about to make to that site.
- **Space every call with a real multi-second gap.** No rapid-fire loops, no parallel/concurrent
  calls to the same site.
- **Never retry-loop on failure.** Stop and report the failure instead of hammering the endpoint.
- **Use a realistic User-Agent.** `wp_client.py`'s default already does this — don't override it
  with something that fingerprints as a script.
- **Report a summary at the end of the task.** When you finish a series of tool calls, state the
  exact number of requests actually sent to each live/staging site touched (broken out per site if
  more than one), so the user always knows the real hit count — not just what was planned.

Prefer the local dev environment for testing and reproduction. Only fall back to live/staging when
the thing being verified genuinely can't be reproduced locally.

---

## What this tool has actually been used for

Not aspirational — each of these has been done end to end against real WordPress installs.

- **Building a complete Elementor page from generated JSON.** A ~10-section landing page —
  containers, headings, icon boxes, image widgets, a multi-step Pro form, inline SVG and inline
  `<script>` — authored as code, written over REST, rendered correctly with no editor clicks.
- **Moving that page between environments.** Local dev → staging, with media uploaded and every
  attachment ID and URL rewritten for the target site.
- **Driving a site behind HTTP Basic auth** (managed hosts often put staging behind htpasswd).
- **Logging into a site that hides `wp-login.php`** (WPS Hide Login and similar).
- **Reading a user's capabilities before writing**, to predict whether markup will be sanitised.

## Things worth knowing before you start

**Elementor layouts are writable over plain REST — no companion plugin.** Elementor registers
`_elementor_data` and friends for REST itself, unconditionally. The layout is a **JSON string**
inside `meta`. Pair with `template: "elementor_canvas"` for a page with no theme header/footer.

**Check `unfiltered_html` before you write anything containing markup.**
`sanitize_elementor_data()` runs the payload through `kses_post_deep` for any user lacking that
capability, which silently strips `<script>` and `<svg>` from HTML widgets. The page still saves
and still renders — just without those elements, and with no error anywhere. Read
`/wp/v2/users/me?context=edit` and check `capabilities.unfiltered_html` first.

**Attachment IDs are per-site.** A layout carries `{"id": 123, "url": "https://source/…"}` for
every image. Move it without remapping and each image points at whatever ID 123 happens to be on
the target. `wp_deploy_elementor_page` handles this; if you write the meta yourself, you own it.

**WordPress does not dedupe uploads.** Same filename twice gives you `image`, `image-1`, `image-2`
and three attachments. Look up by filename and reuse before uploading, or repeat deploys quietly
litter the media library.

**Elementor caches its generated CSS per post and never re-checks it.**
`Post_CSS::is_update_required()` never returns true, so custom CSS written over REST is stored but
never reaches the page until the cache is cleared. `_elementor_css` is not REST-registered, so
`wp/v2` cannot clear it — use Elementor's own `DELETE /elementor/v1/cache`.

**Take a backup before overwriting a layout.** There is no undo. Read the existing
`_elementor_data` and keep it before you write.

## Auth on managed hosts — the two traps

**App passwords do not work behind HTTP Basic auth.** Both use the `Authorization: Basic` header,
and the basic-auth layer consumes it before WordPress sees it. Use `cookie` mode, which logs in via
the form and authenticates with a REST nonce, leaving `Authorization` free for htpasswd.

**A cached login page breaks cookie auth, and lies about why.** Managed hosts happily serve
`wp-login.php` from an edge cache with `Set-Cookie` stripped. You then never receive
`wordpress_test_cookie`, nor any cookie a login-security plugin sets on render — Limit Login
Attempts Reloaded issues `llar_login_flow` and rejects a POST without it. **It reports that
rejection as "Incorrect username or password."** Credentials that work in a browser will fail here,
and the error points you at the wrong thing. The client now appends a unique query string to the
login URL to force a cache miss.

*Diagnostic, if you hit this: GET the login page twice — once plain, once with `?cb=<random>` — and
compare `Set-Cookie`. Cookies on the second and not the first means a cached login page. Two GETs,
no login attempts, no lockout risk.*

## Advice for an agent driving this

1. **Prove the call chain locally first.** Every capability above was built and exercised against a
   local install before it was ever pointed at a remote one.
2. **Do not retry a failed login.** Login-attempt limiters are common; a retry loop locks the
   account out. Read the actual response body instead — WordPress usually says what is wrong, and
   when it doesn't (see above), diagnose with GETs rather than more POSTs.
3. **Use `dry_run` on the deploy tool.** It uploads media and reports the remapping without writing
   the page, so you see exactly what would change.
4. **Verify by fetching the rendered page, not by trusting the write response.** A 200 means the
   meta was stored. It does not mean the markup survived sanitising, the CSS cache was cleared, or
   the images resolved. Fetch the page with a cache-buster and assert on what is actually in the
   HTML.
5. **Say how many requests you are about to make**, per the policy above, and space them.

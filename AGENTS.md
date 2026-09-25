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

## Start here: writing an Elementor page

**Use `wp_deploy_elementor_page`.** It is the only path that does the whole job: backs up the
current layout, uploads media idempotently, remaps every attachment id and URL for the target
site, writes, and clears the cache. Hand-rolling those steps is how pages get broken.

**Writing `_elementor_data` yourself is guarded.** `wp_update_post` with
`meta['_elementor_data']` — and the `set_elementor()` client method behind it — now **refuse**
when the target already has a layout:

```
refused_unbacked_overwrite
```

Post meta has no revision history, so that write would be unrecoverable. To proceed either pass
`backup_dir` (the current layout is saved there first and the path comes back as `backup_file`),
or pass `allow_unbacked` if you have already taken a backup yourself. A page with no existing
layout is not guarded — there is nothing to lose.

**Never blanket-overwrite a page you did not generate in full.** This is the failure that
motivated the guard. Pages drift: a person edits them in the Elementor UI, and that edit is never
back-ported to whatever build script or payload file you are deploying from. Regenerating and
writing the whole page silently destroys their work, and the diff is invisible unless you look for
it. A real case: a page had accumulated a mobile CSS pass, responsive settings on eight widgets,
and an extra widget inside each of four cards — none of it in the source that a full deploy would
have pushed.

So, for a change to an existing page:

1. `wp_get_elementor_page` — read the live tree. **That is your source of truth, not your repo.**
2. Mutate only the nodes you actually mean to change.
3. Write it back (`backup_dir` set, or via the deploy tool).
4. **Diff the before/after tree and assert the change count.** "142 elements before, 142 after,
   exactly 2 changes" is the standard to hold yourself to. If the count surprises you, stop.

## The sequence that works, local → staging

1. **Build and verify on local dev.** Never author straight onto a remote site.
2. Verify on the **rendered page in a real browser**, not the write response and not the database
   value. A correct row is not a correct page.
3. Read the target's current state and keep the backup.
4. Deploy with `source_host` set so media ids and URLs are rewritten for the target.
5. `wp_clear_elementor_cache`, then purge any host-level cache (Varnish, caching plugins).
6. Re-fetch the rendered remote page with a cache-buster and assert on the HTML.
7. Report the actual request count for the remote site.

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
- **Changing a plugin's settings over its own REST routes**, with the read-modify-write, counter and
  line-break handling in an adapter (`wp_plugin_settings_update`), proven on local dev over the real MCP
  protocol before it was pointed at anything remote.

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

**Elementor caches in two places, and both will serve you stale output.**
It caches generated CSS per post — `Post_CSS::is_update_required()` never returns true, so custom
CSS written over REST is stored but never reaches the page — *and* it caches rendered element HTML
in post meta (`_elementor_element_cache`). The second one is the nastier: after a completely
correct write to `_elementor_data`, the page keeps serving the OLD markup, with the database
showing the new value and no error anywhere.

Neither is REST-registered, so `wp/v2` cannot clear them; Elementor's own
`DELETE /elementor/v1/cache` clears both, which is what `wp_clear_elementor_cache` calls. Note
that PHP-side `files_manager->clear_cache()` does **not** clear the element cache — if you are
scripting outside this tool, delete `_elementor_element_cache` explicitly.

**Take a backup before overwriting a layout.** There is no undo. This is now enforced rather
than advised — see "Start here" above.

**Plugin settings routes usually REPLACE, they do not merge.** Many rebuild the stored object from the
data you POST plus the plugin's *defaults*, so posting only the keys you changed resets everything else
to defaults with a 200 and no error. Read the whole object, change your keys, send the whole object
back. Use `wp_plugin_settings_update` where an adapter exists; with `wp_rest_request`, do it yourself.

**A "read" can have a side effect.** Some plugins rebuild an admin preview on GET and, for example,
assign the next invoice number to a random order. Check the adapter's notes, and read once, not in a loop.

**Text fields get sanitised in ways that change line breaks.** One plugin turns a stored `\r\n` into
`\r<br />`; the template then runs `nl2br()` and every line break prints twice, an address with blank
lines between its lines. A round-trip through the plugin's own settings route can therefore *introduce* a
visual bug. Verify the rendered output, not just the save response.

**Never send counters or other computed fields back.** GET responses often include read-only values
(next invoice number, totals). Posting them back can move a counter.

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
5. **Say how many requests you are about to make**, per the policy above, and space them. Login
   is three requests in cookie mode. `wp_request_count` returns the exact total this server has sent,
   and `request_delay` in the site config enforces the spacing for every tool. Put `read_only: true` on
   any live site you only need to inspect.
6. **`dry_run` first on `wp_plugin_settings_update`.** It is the default, and it costs only the reads.

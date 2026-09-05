# Can this MCP server drive Elementor pages? — findings, 4 Sep 2026

**Short answer: yes, and it needs less work than expected. WordPress already allows it;
this server just doesn't expose the field.** The real bottleneck for a local→staging
pipeline is media, not Elementor data.

---

## 1. Elementor already exposes its meta over REST

`elementor/modules/wp-rest/classes/elementor-post-meta.php` calls `register_meta()` for every
post type with `elementor` support, hooked unconditionally on `rest_api_init`
(`modules/wp-rest/module.php:28`) — **not** behind a feature experiment.

| Meta key | Type | Notes |
|---|---|---|
| `_elementor_edit_mode` | string | enum `''` \| `builder` |
| `_elementor_template_type` | string | enum of registered document types (`wp-page`, …) |
| `_elementor_data` | string | **the whole layout, as a JSON string** |
| `_elementor_page_settings` | object | `additionalProperties: true` → `custom_css` goes here |
| `_elementor_conditions` | array | Pro only |

All are `context: edit`, authorised by `check_edit_permission()` →
`$document->is_editable_by_current_user()`.

## 2. The client already supports it — only the tool schema doesn't

`wp_client.py` `update_post()` / `create_post()` whitelist includes **`meta`** and **`template`**
(lines ~204, ~213). But `server.py`'s `inputSchema` for `wp_update_post` / `wp_create_post`
exposes `template` and omits `meta`, so an MCP client can never pass it.

**Minimum viable change is adding one property to two schemas.**

## 3. Proved end to end on local (not assumed)

Against a local dev site, on a throwaway page (never a real build):

1. `GET /wp-json/wp/v2/pages/<id>?context=edit` → all five elementor meta keys returned.
2. `POST` with `template: elementor_canvas` and a `meta` object carrying a real container tree,
   `_elementor_edit_mode: builder`, `_elementor_template_type: wp-page`, and
   `_elementor_page_settings: {hide_title, custom_css}` → **all accepted**.
3. Front end rendered correctly: styled container, boxed width, canvas template (no theme
   header/footer), and **Elementor regenerated `post-<id>.css` (2782 bytes) on its own** —
   no manual cache step was needed for the page CSS.
4. Test page deleted and the test application password revoked afterwards.

## 4. Staging is compatible

- Elementor **4.2.3** / Pro **4.2.2** — byte-identical versions to local, so the same meta module is present.
- `/wp-json/` reachable through the htpasswd layer; `elementor/v1` + `elementor-pro/v1` namespaces registered.
- **htpasswd consumes the `Authorization` header**, so app-password auth cannot reach WP.
  Use this repo's `wp_auth_mode: "cookie"` and put the basic-auth pair in the site config's
  `http_basic_auth` field — that mode exists for exactly this case. Never inline credentials
  in documentation; `configs/*.json` is gitignored precisely so they stay out of the repo.

*(Requests sent to staging during this review: **2**, both GETs, no writes.)*

---

## What to build

### A. Minimal — unlock what already works
Add to `wp_update_post` and `wp_create_post` inputSchema:

```python
"meta": {
    "type": "object",
    "description": "Registered meta keys. Elementor: _elementor_data (JSON string), "
                   "_elementor_edit_mode ('builder'), _elementor_template_type ('wp-page'), "
                   "_elementor_page_settings (object incl. custom_css).",
    "additionalProperties": True,
},
```

### B. Better — purpose-built tools with guardrails
- `wp_get_elementor_page(post_type, post_id)` → parsed data + page settings + template
- `wp_update_elementor_page(post_id, elementor_data, custom_css=None, template='elementor_canvas')`

Guardrails worth having, each for a real failure mode:
1. **Validate the JSON parses, and that the top level is a list of elements, before sending.**
   Malformed `_elementor_data` gives a blank page and an editor that won't open.
2. **Return the previous `_elementor_data` (or write it to a backup file) on every overwrite.**
   There is no undo through REST.
3. **`unfiltered_html` check.** `sanitize_elementor_data()` runs the payload through
   `Utils::kses_post_deep()` for any user lacking `unfiltered_html` — silently stripping
   attributes. The local admin account has it; **confirm the staging user does too** before
   trusting a write, and fail loudly rather than writing sanitised data.
4. **Cache purge.** Elementor's own post CSS regenerates automatically, but staging also has
   **Breeze + Object Cache Pro** in front. Neither exposes a REST purge. Either add a tiny
   mu-plugin endpoint or purge from the Cloudways/WP UI after deploying.

### C. The actual bottleneck — media
The generated `_elementor_data` embeds **attachment IDs and absolute URLs**
(a hero image plus a row of logos, all on the source site's uploads URL).
Those IDs mean nothing on staging.

This server has **no media tools at all**. To make local→staging a one-command deploy you need:
- `wp_upload_media(file_path, title, alt)` → `POST /wp-json/wp/v2/media` (multipart, not JSON —
  a genuinely new code path in `wp_client.request()`, which currently only sends JSON bodies)
- `wp_find_media_by_slug(slug)` so uploads are idempotent instead of duplicating on every deploy
- an ID/URL remapping pass in the generator: reference media by **filename**, resolve to
  per-environment IDs at deploy time

Without that, the Elementor JSON transfers but every image breaks.

---

## Recommended shape for a generated-page workflow

If the layout is generated from code rather than clicked together in the editor, the natural end
state is one generator with two targets:

```
sections.py ──► page_payload.json ──┬─► create_page.php   (local, direct DB/PHP)
                                    └─► MCP wp_deploy_elementor_page (staging, then live)
```

That makes the environment port a single command and — importantly — makes the **same** command
replayable against production, so the deployment is a repeatable procedure rather than a
remembered sequence of manual steps.

Build order: do the small unblocking change first (a few lines, unlocks testing), then media
handling (the real blocker), then the guardrails — before anything is pointed at a live site.

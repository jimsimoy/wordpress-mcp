#!/usr/bin/env python3
"""
WordPress REST API client.

Supports two auth modes:
  app_password  — Basic auth with WP application password (Authorization header)
  cookie        — WP login + REST nonce, for sites behind HTTP basic auth where
                  the Authorization header is consumed by nginx before reaching WP
"""

import base64
import http.cookiejar
import json
import mimetypes
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings

warnings.filterwarnings("ignore")


# Python's default urllib User-Agent ("Python-urllib/3.x") is a well-known
# automated-client signature that WAFs/firewalls specifically fingerprint and
# flag. A normal browser UA is what any legitimate authenticated admin session
# would present — this isn't disguising anything about *who* is making the
# request (still fully authenticated as a real WP user), just not needlessly
# broadcasting "this is a script" on every single header.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class WPClient:
    def __init__(self, config: dict):
        self.base  = config["wp_url"].rstrip("/")
        self._ssl  = ssl._create_unverified_context()
        self._opener = None
        self._user_agent = config.get("user_agent", DEFAULT_USER_AGENT)

        auth_mode = config.get("wp_auth_mode", "app_password")

        if auth_mode == "cookie":
            self._http_basic = config.get("http_basic_auth", "")
            self._cookie_login(config)
        else:
            user  = config["wp_user"]
            token = config.get("wp_app_password") or config.get("wp_password", "")
            encoded = base64.b64encode(f"{user}:{token}".encode()).decode()
            self.headers = {
                "Authorization": f"Basic {encoded}",
                "Content-Type":  "application/json",
                "User-Agent":    self._user_agent,
            }

    def _cookie_login(self, config: dict):
        admin_url = config.get("wp_admin_url", self.base + "/wp-admin").rstrip("/")
        wp_root   = admin_url.rsplit("/wp-admin", 1)[0]
        # WPS Hide Login (and similar) move wp-login.php to a custom slug and 404 the
        # original, so allow the login endpoint to be overridden. `wp_login_url` may be
        # absolute or just the slug.
        login_url = config.get("wp_login_url") or (wp_root + "/wp-login.php")
        if not login_url.startswith("http"):
            login_url = wp_root + "/" + login_url.lstrip("/")

        # Managed hosts (Cloudways/Varnish, and friends) will happily serve the login
        # page from an edge cache with Set-Cookie stripped. That costs us two cookies
        # the POST depends on: WordPress's own `wordpress_test_cookie`, and any set by
        # a login-security plugin — Limit Login Attempts Reloaded issues
        # `llar_login_flow` on render and rejects the POST without it, reporting the
        # failure as "Incorrect username or password". That misleading error is worth
        # remembering: it is indistinguishable from genuinely wrong credentials.
        # A unique query string per login forces a cache miss.
        cache_buster = f"_wpmcp={int(time.time() * 1000)}"
        login_url += ("&" if "?" in login_url else "?") + cache_buster

        nonce_url = admin_url + "/admin-ajax.php?action=rest-nonce"

        nginx_b64 = ""
        if self._http_basic:
            nginx_b64 = "Basic " + base64.b64encode(self._http_basic.encode()).decode()

        cj = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ssl),
            urllib.request.HTTPCookieProcessor(cj),
        )

        base_hdrs = {"User-Agent": self._user_agent}
        if nginx_b64:
            base_hdrs["Authorization"] = nginx_b64

        # GET the login page first so wordpress_test_cookie is set in the jar
        self._opener.open(urllib.request.Request(login_url, headers=base_hdrs))

        login_data = urllib.parse.urlencode({
            "log":         config["wp_user"],
            "pwd":         config["wp_password"],
            "wp-submit":   "Log In",
            "redirect_to": "/wp-admin/",
            "testcookie":  "1",
        }).encode()

        hdrs = {**base_hdrs, "Content-Type": "application/x-www-form-urlencoded"}
        req  = urllib.request.Request(login_url, data=login_data, headers=hdrs, method="POST")
        try:
            with self._opener.open(req) as r:
                final_url = r.geturl()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"WP login failed (HTTP {e.code}). Check wp_user / wp_password.")
        if "wp-login" in final_url or final_url.rstrip("/") == login_url.rstrip("/"):
            raise RuntimeError(
                "WP login failed — ended up back at the login page. Check wp_user / "
                "wp_password, and wp_login_url if the site hides wp-login.php."
            )

        with self._opener.open(urllib.request.Request(nonce_url, headers=base_hdrs)) as r:
            nonce = r.read().decode().strip()

        self.headers = {"Content-Type": "application/json", "X-WP-Nonce": nonce, "User-Agent": self._user_agent}
        if nginx_b64:
            self.headers["Authorization"] = nginx_b64

    # ── Low-level request ──────────────────────────────────────────────────────

    def request(self, method: str, path: str, payload=None, params: dict = None,
                raw_body: bytes = None, extra_headers: dict = None):
        """JSON request by default. Pass `raw_body` to send bytes as-is (media upload),
        in which case `extra_headers` must carry the right Content-Type/Disposition."""
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        hdrs = dict(self.headers)
        if raw_body is not None:
            hdrs.pop("Content-Type", None)
            data = raw_body
        else:
            data = json.dumps(payload).encode() if payload is not None else None
        if extra_headers:
            hdrs.update(extra_headers)

        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)

        def _decode(body: bytes):
            # Some endpoints (e.g. DELETE elementor/v1/cache) answer 200 with an
            # empty or non-JSON body — that is success, not a parse failure.
            if not body or not body.strip():
                return {"ok": True}
            try:
                return json.loads(body)
            except ValueError:
                return {"ok": True, "raw": body.decode(errors="replace")[:300]}

        try:
            if self._opener:
                with self._opener.open(req) as r:
                    return _decode(r.read())
            else:
                with urllib.request.urlopen(req, context=self._ssl) as r:
                    return _decode(r.read())
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                return json.loads(body)
            except Exception:
                raise RuntimeError(f"HTTP {e.code} {path}: {body[:300]}")

    # ── Users ──────────────────────────────────────────────────────────────────

    def list_users(self, per_page: int = 20, page: int = 1, search: str = None,
                   roles: list = None, order_by: str = "id") -> list:
        params = {"per_page": per_page, "page": page, "orderby": order_by, "context": "edit"}
        if search:
            params["search"] = search
        if roles:
            params["roles"] = ",".join(roles)
        return self.request("GET", "/wp-json/wp/v2/users", params=params)

    def get_user(self, user_id: int) -> dict:
        return self.request("GET", f"/wp-json/wp/v2/users/{user_id}", params={"context": "edit"})

    def get_user_by_slug(self, slug: str) -> dict | None:
        results = self.request("GET", "/wp-json/wp/v2/users", params={"slug": slug, "per_page": 1})
        return results[0] if results else None

    def create_user(self, username: str, email: str, password: str,
                    first_name: str = "", last_name: str = "",
                    roles: list = None, description: str = "") -> dict:
        payload = {
            "username":    username,
            "email":       email,
            "password":    password,
            "first_name":  first_name,
            "last_name":   last_name,
            "description": description,
        }
        if roles:
            payload["roles"] = roles
        return self.request("POST", "/wp-json/wp/v2/users", payload=payload)

    def update_user(self, user_id: int, **fields) -> dict:
        allowed = {"email", "first_name", "last_name", "password", "roles",
                   "description", "name", "nickname", "url"}
        payload = {k: v for k, v in fields.items() if k in allowed}
        return self.request("POST", f"/wp-json/wp/v2/users/{user_id}", payload=payload)

    def delete_user(self, user_id: int, reassign_to: int) -> dict:
        return self.request(
            "DELETE",
            f"/wp-json/wp/v2/users/{user_id}",
            params={"force": "true", "reassign": reassign_to},
        )

    # ── Site info ──────────────────────────────────────────────────────────────

    def site_info(self) -> dict:
        return self.request("GET", "/wp-json/")

    # ── Posts / Pages / custom post types ─────────────────────────────────────
    # `post_type` is the REST base slug (e.g. "pages", "posts", or a custom post
    # type's registered rest_base) — kept generic across sites rather than
    # hardcoding "pages".

    def list_posts(self, post_type: str = "pages", per_page: int = 20, page: int = 1,
                   search: str = None, slug: str = None, status: str = None) -> list:
        params = {"per_page": per_page, "page": page, "context": "edit"}
        if search:
            params["search"] = search
        if slug:
            params["slug"] = slug
        if status:
            params["status"] = status
        return self.request("GET", f"/wp-json/wp/v2/{post_type}", params=params)

    def get_post(self, post_type: str, post_id: int) -> dict:
        return self.request("GET", f"/wp-json/wp/v2/{post_type}/{post_id}", params={"context": "edit"})

    def get_post_by_slug(self, post_type: str, slug: str) -> dict | None:
        results = self.list_posts(post_type=post_type, slug=slug, per_page=1,
                                  status="any" if post_type != "posts" else None)
        return results[0] if results else None

    def _guard_elementor_overwrite(self, post_type, post_id, meta, backup_dir, allow_unbacked):
        """Refuse to replace an existing Elementor layout with no way back.

        A REST write replaces `_elementor_data` wholesale, and WordPress keeps no
        revision history for post meta — so an overwrite is unrecoverable. Both
        `update_post(meta=...)` and `set_elementor()` funnel through `update_post`,
        so the check lives here rather than on each caller; guarding only the
        Elementor helper would leave the raw-meta route wide open.

        Returns `(backup_file, refusal)`. When `refusal` is set the caller must
        return it untouched instead of writing.
        """
        if not isinstance(meta, dict) or not meta.get("_elementor_data"):
            return None, None                    # not an Elementor write, nothing to guard
        if allow_unbacked and not backup_dir:
            return None, None                    # caller asserts it has already backed up

        current = self.get_elementor(post_type, post_id)
        if isinstance(current, dict) and current.get("code"):
            return None, current                 # could not read — surface that, do not write
        existing = current.get("data")
        if existing in (None, "", "[]", []):
            return None, None                    # blank page, nothing to lose

        if backup_dir:
            from elementor_deploy import backup_layout
            return backup_layout(post_id, existing, current.get("page_settings"),
                                 current.get("template"), backup_dir), None

        return None, {
            "code": "refused_unbacked_overwrite",
            "message": (
                f"Refusing to overwrite: {post_type}/{post_id} already has an Elementor layout "
                f"({len(existing) if isinstance(existing, str) else 'non-empty'} bytes) and this "
                "write would replace it permanently — WordPress keeps no revisions of post meta. "
                "Use wp_deploy_elementor_page (it backs up automatically), or pass backup_dir to "
                "save the current layout first. allow_unbacked=True skips this check and should "
                "only be used when you have already taken a backup yourself."
            ),
        }

    def update_post(self, post_type: str, post_id: int, backup_dir: str = None,
                    allow_unbacked: bool = False, **fields) -> dict:
        allowed = {"title", "content", "excerpt", "status", "slug", "date",
                   "featured_media", "meta", "parent", "menu_order", "template"}
        payload = {k: v for k, v in fields.items() if k in allowed and v is not None}

        backup_file, refusal = self._guard_elementor_overwrite(
            post_type, post_id, payload.get("meta"), backup_dir, allow_unbacked)
        if refusal:
            return refusal

        result = self.request("POST", f"/wp-json/wp/v2/{post_type}/{post_id}", payload=payload)
        if backup_file and isinstance(result, dict) and not result.get("code"):
            result = {**result, "_backup_file": backup_file}
        return result

    def create_post(self, post_type: str, **fields) -> dict:
        allowed = {"title", "content", "excerpt", "status", "slug", "date",
                   "featured_media", "meta", "parent", "menu_order", "template"}
        payload = {k: v for k, v in fields.items() if k in allowed and v is not None}
        return self.request("POST", f"/wp-json/wp/v2/{post_type}", payload=payload)

    def delete_post(self, post_type: str, post_id: int, force: bool = False) -> dict:
        return self.request("DELETE", f"/wp-json/wp/v2/{post_type}/{post_id}",
                            params={"force": "true" if force else "false"})

    # ── Elementor ─────────────────────────────────────────────────────────────

    def get_elementor(self, post_type: str, post_id: int) -> dict:
        post = self.get_post(post_type, post_id)
        if isinstance(post, dict) and post.get("code"):
            return post
        meta = post.get("meta", {}) or {}
        return {
            "post_id":       post.get("id"),
            "slug":          post.get("slug"),
            "status":        post.get("status"),
            "template":      post.get("template"),
            "link":          post.get("link"),
            "data":          meta.get("_elementor_data"),
            "page_settings": meta.get("_elementor_page_settings"),
            "edit_mode":     meta.get("_elementor_edit_mode"),
            "template_type": meta.get("_elementor_template_type"),
        }

    def set_elementor(self, post_type: str, post_id: int, layout, page_settings: dict = None,
                      template: str = None, status: str = None,
                      backup_dir: str = None, allow_unbacked: bool = False) -> dict:
        """Write a layout to a page.

        Overwriting an existing layout is refused unless `backup_dir` is given (the
        current layout is saved there first) or `allow_unbacked=True`. Prefer
        `wp_deploy_elementor_page`, which backs up and remaps media for you.
        """
        meta = {
            "_elementor_data": layout if isinstance(layout, str) else json.dumps(layout),
            "_elementor_edit_mode": "builder",
            "_elementor_template_type": "wp-page",
        }
        if page_settings is not None:
            meta["_elementor_page_settings"] = page_settings
        fields = {"meta": meta}
        if template is not None:
            fields["template"] = template
        if status is not None:
            fields["status"] = status
        return self.update_post(post_type, post_id, backup_dir=backup_dir,
                                allow_unbacked=allow_unbacked, **fields)

    def clear_elementor_cache(self) -> dict:
        """Elementor caches generated CSS per post and never re-checks it
        (`Post_CSS::is_update_required()` is always false), so a REST write to
        `_elementor_page_settings` — custom_css included — is stored but never
        reaches the rendered page until this is called. `_elementor_css` is not a
        REST-registered meta key, so it cannot be cleared through wp/v2.
        Elementor's own endpoint (requires manage_options) is the way in."""
        return self.request("DELETE", "/wp-json/elementor/v1/cache")

    # ── Media ─────────────────────────────────────────────────────────────────
    # WP accepts a raw binary body with Content-Disposition rather than requiring
    # multipart/form-data, which keeps this on urllib without a multipart encoder.

    def list_media(self, per_page: int = 20, page: int = 1, search: str = None,
                   mime_type: str = None) -> list:
        params = {"per_page": per_page, "page": page, "context": "edit"}
        if search:
            params["search"] = search
        if mime_type:
            params["mime_type"] = mime_type
        return self.request("GET", "/wp-json/wp/v2/media", params=params)

    def get_media(self, media_id: int) -> dict:
        return self.request("GET", f"/wp-json/wp/v2/media/{media_id}", params={"context": "edit"})

    def find_media_by_filename(self, filename: str) -> dict | None:
        """Look up an existing attachment by its uploaded filename, so repeated
        deploys reuse the attachment instead of creating -1, -2, -3 duplicates.
        Matches on source_url basename, since WP mangles slugs on collision."""
        stem = os.path.splitext(os.path.basename(filename))[0]
        results = self.list_media(search=stem, per_page=100)
        if not isinstance(results, list):
            return None
        target = os.path.basename(filename)
        for m in results:
            if os.path.basename(m.get("source_url", "")) == target:
                return m
        return None

    def upload_media(self, file_path: str, title: str = None, alt_text: str = None,
                     caption: str = None, filename: str = None) -> dict:
        if not os.path.isfile(file_path):
            raise RuntimeError(f"File not found: {file_path}")

        name = filename or os.path.basename(file_path)
        mime = mimetypes.guess_type(name)[0]
        if not mime:
            raise RuntimeError(f"Could not determine MIME type for {name} — pass a known extension.")

        with open(file_path, "rb") as f:
            blob = f.read()

        created = self.request(
            "POST", "/wp-json/wp/v2/media",
            raw_body=blob,
            extra_headers={
                "Content-Type": mime,
                "Content-Disposition": f'attachment; filename="{name}"',
            },
        )
        if not isinstance(created, dict) or created.get("code"):
            return created

        # title/alt/caption are metadata, set in a follow-up call
        fields = {}
        if title is not None:
            fields["title"] = title
        if alt_text is not None:
            fields["alt_text"] = alt_text
        if caption is not None:
            fields["caption"] = caption
        if fields:
            updated = self.request("POST", f"/wp-json/wp/v2/media/{created['id']}", payload=fields)
            if isinstance(updated, dict) and not updated.get("code"):
                return updated
        return created

    def delete_media(self, media_id: int, force: bool = True) -> dict:
        return self.request("DELETE", f"/wp-json/wp/v2/media/{media_id}",
                            params={"force": "true" if force else "false"})

    # ── ACF fields ─────────────────────────────────────────────────────────────
    # Relies on the site exposing ACF data via REST (ACF PRO's "Show in REST API"
    # per field group, or an equivalent custom rest_field registration). If a post
    # has no "acf" key in its REST response, that's not enabled for it — this
    # client doesn't assume it and reports what it actually finds rather than
    # guessing.

    def get_acf_fields(self, post_type: str, post_id: int) -> dict | None:
        post = self.get_post(post_type, post_id)
        if isinstance(post, dict) and post.get("code"):
            return post
        return post.get("acf") if isinstance(post, dict) else None

    def update_acf_fields(self, post_type: str, post_id: int, acf_fields: dict) -> dict:
        """Merges the given keys into the post's acf object (POST /wp/v2/{type}/{id} with {"acf": {...}}).
        Only the keys present in acf_fields are sent — REST-exposed ACF field groups
        merge rather than replace the whole acf object on partial payloads."""
        return self.request("POST", f"/wp-json/wp/v2/{post_type}/{post_id}",
                            payload={"acf": acf_fields})

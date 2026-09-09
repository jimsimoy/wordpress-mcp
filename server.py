#!/usr/bin/env python3
"""
WordPress MCP Server

Exposes WordPress management tools via the Model Context Protocol (stdio transport).
One server instance = one WordPress site. Start with --config pointing to the site's
config file.

Usage:
  python3 server.py --config configs/your-site.json
  python3 server.py --config /absolute/path/to/config.json

Config files: see config.example.json for the full schema.
"""

import argparse
import json
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
    CallToolResult,
)

from wp_client import WPClient
import elementor_deploy as ed

# ── Bootstrap ──────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="WordPress MCP Server")
    parser.add_argument(
        "--config", required=True,
        help="Path to the site config JSON file (absolute or relative to this script)",
    )
    return parser.parse_args()


def load_config(path: str) -> dict:
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    with open(path) as f:
        return json.load(f)


# ── Helpers ────────────────────────────────────────────────────────────────────

def ok(data) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, indent=2))])


def err(message: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=f"Error: {message}")],
        isError=True,
    )


# ── Server setup ───────────────────────────────────────────────────────────────

SERVER_INSTRUCTIONS = """\
This server writes to real WordPress sites. One server instance = one site; the config
decides whether that is local dev, staging, or live. Read this before your first call.

## Pacing — applies to staging and live, not local dev
- Prove a call chain on local dev BEFORE running it against staging or live.
- State the request count up front, before you run anything remote.
- Space calls several seconds apart. No parallel calls to the same site, no rapid loops.
- Never retry-loop on failure. Stop and report it.
- Report the actual number of requests sent to each remote site when you finish.

## Elementor: use wp_deploy_elementor_page
`_elementor_data` is post meta. A REST write REPLACES it wholesale, and WordPress keeps
NO revision history for post meta, so an overwrite cannot be undone.

- **Use `wp_deploy_elementor_page`.** It backs up first, uploads and de-duplicates media,
  remaps every attachment id and URL for the target site, writes, then clears the cache.
- Writing `_elementor_data` yourself through `wp_update_post`'s `meta` is guarded: it is
  REFUSED when the page already has a layout. Pass `backup_dir` to save the current
  layout first. Only set `allow_unbacked` when you have already taken a backup.
- **Never blanket-overwrite a page you did not generate in full.** Pages drift: someone
  edits them in the Elementor UI and the change is never back-ported to whatever source
  file you are deploying from. A full-page write silently destroys that work. Read the
  live layout, change only the nodes you mean to change, write it back, then diff the
  before/after tree and confirm the change count is exactly what you intended.

## The build/deploy sequence that works
1. Build and verify on local dev first — never author straight onto a remote site.
2. `wp_get_elementor_page` to read current state; keep it as your baseline.
3. `wp_deploy_elementor_page` with a payload file, `backup_dir`, and `source_host` so
   media ids and URLs are rewritten for the target.
4. `wp_clear_elementor_cache` — required, see below.
5. Re-fetch the RENDERED page and verify the change is actually visible. A correct
   database value is not evidence the page changed.

## Traps that cost real time
- **Caching.** Elementor caches generated CSS per post AND caches rendered element HTML
  in post meta. After a correct write the page can keep serving the OLD markup with no
  error anywhere. `wp_clear_elementor_cache` clears both — always call it after a write.
  Host-level caches (Varnish, and any caching plugin) sit in front of that and need
  purging separately.
- **`unfiltered_html`.** Without that capability WordPress runs your payload through
  `kses`, silently stripping `<script>` and `<svg>`. The page still saves and still
  renders — just missing those elements. Check the user's capabilities before writing
  markup.
- **Attachment ids are per-site.** A layout carries `{"id": N, "url": "..."}` for every
  image. Move it without remapping and each image points at whatever id N happens to be
  on the target. `wp_deploy_elementor_page` handles this; a raw meta write does not.
- **WordPress does not de-duplicate uploads.** The same filename twice gives you two
  attachments. Use `wp_find_media_by_filename` and reuse before uploading.
- **Deletes take `force`.** `force=true` skips the trash and is permanent.
"""

server = Server("wordpress-mcp", instructions=SERVER_INSTRUCTIONS)
_client: WPClient | None = None


def get_client() -> WPClient:
    if _client is None:
        raise RuntimeError("WP client not initialised")
    return _client


# ── Tool definitions ───────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="wp_site_info",
            description="Return basic info about the connected WordPress site (name, URL, WP version, description).",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="wp_list_users",
            description=(
                "List WordPress users. Optionally filter by search term or roles. "
                "Returns id, username, name, email, roles for each user."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "search":   {"type": "string",  "description": "Search by username, name, or email"},
                    "roles":    {"type": "array", "items": {"type": "string"},
                                 "description": "Filter by role(s) e.g. ['administrator','editor']"},
                    "per_page": {"type": "integer", "default": 20, "description": "Users per page (max 100)"},
                    "page":     {"type": "integer", "default": 1,  "description": "Page number"},
                },
            },
        ),
        Tool(
            name="wp_get_user",
            description="Get a single WordPress user by ID.",
            inputSchema={
                "type": "object",
                "required": ["user_id"],
                "properties": {
                    "user_id": {"type": "integer", "description": "WordPress user ID"},
                },
            },
        ),
        Tool(
            name="wp_create_user",
            description=(
                "Create a new WordPress user. "
                "username, email, and password are required. "
                "role defaults to 'subscriber' if omitted."
            ),
            inputSchema={
                "type": "object",
                "required": ["username", "email", "password"],
                "properties": {
                    "username":    {"type": "string"},
                    "email":       {"type": "string"},
                    "password":    {"type": "string"},
                    "first_name":  {"type": "string"},
                    "last_name":   {"type": "string"},
                    "roles":       {"type": "array", "items": {"type": "string"},
                                    "description": "e.g. ['editor'] — defaults to ['subscriber']"},
                    "description": {"type": "string"},
                },
            },
        ),
        Tool(
            name="wp_update_user",
            description="Update fields on an existing WordPress user. Only the fields you supply are changed.",
            inputSchema={
                "type": "object",
                "required": ["user_id"],
                "properties": {
                    "user_id":     {"type": "integer"},
                    "email":       {"type": "string"},
                    "first_name":  {"type": "string"},
                    "last_name":   {"type": "string"},
                    "password":    {"type": "string", "description": "Set a new password"},
                    "roles":       {"type": "array", "items": {"type": "string"}},
                    "description": {"type": "string"},
                    "name":        {"type": "string", "description": "Display name"},
                    "nickname":    {"type": "string"},
                    "url":         {"type": "string"},
                },
            },
        ),
        Tool(
            name="wp_delete_user",
            description=(
                "Delete a WordPress user. "
                "All their content is reassigned to reassign_to_id. "
                "This is irreversible — confirm before calling."
            ),
            inputSchema={
                "type": "object",
                "required": ["user_id", "reassign_to_id"],
                "properties": {
                    "user_id":        {"type": "integer", "description": "ID of the user to delete"},
                    "reassign_to_id": {"type": "integer", "description": "ID of user to receive their content"},
                },
            },
        ),
        Tool(
            name="wp_list_posts",
            description=(
                "List posts/pages/custom post types. post_type is the REST base slug "
                "(e.g. 'pages', 'posts', or a custom post type's rest_base) — defaults to 'pages'. "
                "Optionally filter by search term, exact slug, or status."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "post_type": {"type": "string", "default": "pages", "description": "REST base slug, e.g. 'pages' or 'posts'"},
                    "search":    {"type": "string", "description": "Search by title/content"},
                    "slug":      {"type": "string", "description": "Exact slug match"},
                    "status":    {"type": "string", "description": "e.g. 'publish', 'draft', 'any'"},
                    "per_page":  {"type": "integer", "default": 20},
                    "page":      {"type": "integer", "default": 1},
                },
            },
        ),
        Tool(
            name="wp_get_post",
            description="Get a single post/page by ID, including its core fields and 'acf' data if that post's ACF field groups are REST-exposed.",
            inputSchema={
                "type": "object",
                "required": ["post_id"],
                "properties": {
                    "post_type": {"type": "string", "default": "pages"},
                    "post_id":   {"type": "integer"},
                },
            },
        ),
        Tool(
            name="wp_update_post",
            description=(
                "Update core fields on an existing post/page — title, content, excerpt, status, slug, etc. "
                "Only the fields you supply are changed. Does NOT touch ACF data — use wp_update_acf_fields for that."
            ),
            inputSchema={
                "type": "object",
                "required": ["post_id"],
                "properties": {
                    "post_type":       {"type": "string", "default": "pages"},
                    "post_id":         {"type": "integer"},
                    "title":           {"type": "string"},
                    "content":         {"type": "string"},
                    "excerpt":         {"type": "string"},
                    "status":          {"type": "string", "description": "e.g. 'publish', 'draft', 'private'"},
                    "slug":            {"type": "string"},
                    "featured_media":  {"type": "integer"},
                    "parent":          {"type": "integer"},
                    "menu_order":      {"type": "integer"},
                    "template":        {"type": "string", "description": "Page template file, e.g. 'template-generic.php'"},
                    "meta":            {
                        "type": "object",
                        "additionalProperties": True,
                        "description": (
                            "Registered post meta. Only keys registered with show_in_rest are accepted; "
                            "anything else is silently ignored by WordPress. "
                            "Elementor pages (Elementor 3.x/4.x registers these): "
                            "'_elementor_data' (the layout, as a JSON *string*), "
                            "'_elementor_edit_mode' ('builder'), "
                            "'_elementor_template_type' ('wp-page'), "
                            "'_elementor_page_settings' (object; accepts 'custom_css', 'hide_title'). "
                            "Pair with template='elementor_canvas' for a page with no theme header/footer. "
                            "NOTE: writing '_elementor_data' to a page that already has a layout is "
                            "REFUSED unless you pass backup_dir (or allow_unbacked). Prefer "
                            "wp_deploy_elementor_page, which backs up and remaps media for you."
                        ),
                    },
                    "backup_dir": {
                        "type": "string",
                        "description": (
                            "Directory to save the page's current Elementor layout to before "
                            "overwriting it. Required when meta['_elementor_data'] would replace an "
                            "existing layout — post meta has no revision history, so the overwrite "
                            "is otherwise unrecoverable. The saved path comes back as '_backup_file'."
                        ),
                    },
                    "allow_unbacked": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Skip the backup requirement. Only set this when you have already taken "
                            "a backup of the current layout yourself."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="wp_create_post",
            description="Create a new post/page. title is typically required; status defaults to 'draft' if omitted.",
            inputSchema={
                "type": "object",
                "properties": {
                    "post_type": {"type": "string", "default": "pages"},
                    "title":     {"type": "string"},
                    "content":   {"type": "string"},
                    "excerpt":   {"type": "string"},
                    "status":    {"type": "string", "default": "draft"},
                    "slug":      {"type": "string"},
                    "parent":    {"type": "integer"},
                    "template":  {"type": "string", "description": "Page template file, e.g. 'template-generic.php'"},
                    "meta":            {
                        "type": "object",
                        "additionalProperties": True,
                        "description": (
                            "Registered post meta. Only keys registered with show_in_rest are accepted; "
                            "anything else is silently ignored by WordPress. "
                            "Elementor pages (Elementor 3.x/4.x registers these): "
                            "'_elementor_data' (the layout, as a JSON *string*), "
                            "'_elementor_edit_mode' ('builder'), "
                            "'_elementor_template_type' ('wp-page'), "
                            "'_elementor_page_settings' (object; accepts 'custom_css', 'hide_title'). "
                            "Pair with template='elementor_canvas' for a page with no theme header/footer."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="wp_delete_post",
            description="Delete a post/page. By default moves it to trash; force=true permanently deletes. Irreversible when forced — confirm before calling.",
            inputSchema={
                "type": "object",
                "required": ["post_id"],
                "properties": {
                    "post_type": {"type": "string", "default": "pages"},
                    "post_id":   {"type": "integer"},
                    "force":     {"type": "boolean", "default": False},
                },
            },
        ),
        Tool(
            name="wp_get_acf_fields",
            description=(
                "Get the ACF field data for a post/page. Returns an empty result if that post's field "
                "groups aren't REST-exposed (ACF's 'Show in REST API' setting, or an equivalent "
                "show_in_rest registration) — this tool reports what it actually finds, it doesn't assume."
            ),
            inputSchema={
                "type": "object",
                "required": ["post_id"],
                "properties": {
                    "post_type": {"type": "string", "default": "pages"},
                    "post_id":   {"type": "integer"},
                },
            },
        ),
        Tool(
            name="wp_update_acf_fields",
            description=(
                "Update ACF field(s) on a post/page. Pass only the field names you want to change — "
                "REST-exposed ACF field groups merge partial 'acf' payloads rather than replacing the "
                "whole object. Requires the target field group(s) to have show_in_rest enabled; if not, "
                "the site will silently ignore the acf payload — check wp_get_acf_fields first to confirm "
                "the fields you want are actually visible before writing."
            ),
            inputSchema={
                "type": "object",
                "required": ["post_id", "acf_fields"],
                "properties": {
                    "post_type":  {"type": "string", "default": "pages"},
                    "post_id":    {"type": "integer"},
                    "acf_fields": {"type": "object", "description": "Map of ACF field name -> new value"},
                },
            },
        ),
        Tool(
            name="wp_list_media",
            description="List media library items. Use search to narrow by filename or title.",
            inputSchema={
                "type": "object",
                "properties": {
                    "search":    {"type": "string"},
                    "mime_type": {"type": "string", "description": "e.g. 'image/webp', 'image/png'"},
                    "per_page":  {"type": "integer", "default": 20},
                    "page":      {"type": "integer", "default": 1},
                },
            },
        ),
        Tool(
            name="wp_get_media",
            description="Get one media item by ID, including source_url and generated sizes.",
            inputSchema={
                "type": "object",
                "required": ["media_id"],
                "properties": {"media_id": {"type": "integer"}},
            },
        ),
        Tool(
            name="wp_find_media_by_filename",
            description=(
                "Find an existing attachment by uploaded filename (e.g. 'hero.webp'). "
                "Use before uploading to keep deploys idempotent — WordPress does not dedupe, "
                "it appends -1/-2 and creates a new attachment each time."
            ),
            inputSchema={
                "type": "object",
                "required": ["filename"],
                "properties": {"filename": {"type": "string"}},
            },
        ),
        Tool(
            name="wp_upload_media",
            description=(
                "Upload a local file to the media library and return its attachment ID and URL. "
                "By default reuses an existing attachment with the same filename rather than "
                "creating a duplicate; pass allow_duplicate=true to force a new upload."
            ),
            inputSchema={
                "type": "object",
                "required": ["file_path"],
                "properties": {
                    "file_path":       {"type": "string", "description": "Absolute path to the local file"},
                    "title":           {"type": "string"},
                    "alt_text":        {"type": "string", "description": "Set this — it is the image's accessible name"},
                    "caption":         {"type": "string"},
                    "filename":        {"type": "string", "description": "Override the uploaded filename"},
                    "allow_duplicate": {"type": "boolean", "default": False},
                },
            },
        ),
        Tool(
            name="wp_delete_media",
            description="Delete a media item. force=true (default) deletes permanently — irreversible, confirm first.",
            inputSchema={
                "type": "object",
                "required": ["media_id"],
                "properties": {
                    "media_id": {"type": "integer"},
                    "force":    {"type": "boolean", "default": True},
                },
            },
        ),
        Tool(
            name="wp_clear_elementor_cache",
            description=(
                "Clear Elementor's generated CSS cache site-wide (DELETE elementor/v1/cache). "
                "REQUIRED after writing _elementor_page_settings: Elementor caches per-post CSS and "
                "never re-checks it, so custom_css changes are stored but do not render until this runs. "
                "Needs manage_options."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="wp_get_elementor_page",
            description=(
                "Read an Elementor page: parsed layout, page settings (incl. custom_css), template, "
                "and the media attachment IDs the layout references."
            ),
            inputSchema={
                "type": "object",
                "required": ["post_id"],
                "properties": {
                    "post_type":    {"type": "string", "default": "pages"},
                    "post_id":      {"type": "integer"},
                    "include_data": {"type": "boolean", "default": False,
                                     "description": "Include the full layout JSON. Off by default — it is large."},
                },
            },
        ),
        Tool(
            name="wp_backup_elementor_page",
            description=(
                "Save a page's current Elementor layout and settings to a timestamped local JSON file. "
                "REST writes replace the layout wholesale and WordPress keeps no revision of post meta, "
                "so an overwrite is otherwise unrecoverable."
            ),
            inputSchema={
                "type": "object",
                "required": ["post_id", "backup_dir"],
                "properties": {
                    "post_type":  {"type": "string", "default": "pages"},
                    "post_id":    {"type": "integer"},
                    "backup_dir": {"type": "string", "description": "Local directory to write the backup into"},
                },
            },
        ),
        Tool(
            name="wp_deploy_elementor_page",
            description=(
                "Deploy an Elementor layout to a page: back up what is there, upload any media, rewrite "
                "the layout's attachment IDs and URLs for THIS site, validate, then write. "
                "This is the safe way to move a layout between environments — attachment IDs are "
                "per-site, so a layout written without remapping points at the wrong images. "
                "Pass media as a manifest of local files; uploads are idempotent by filename."
            ),
            inputSchema={
                "type": "object",
                "required": ["post_id", "payload_file", "backup_dir"],
                "properties": {
                    "post_type":    {"type": "string", "default": "pages"},
                    "post_id":      {"type": "integer"},
                    "payload_file": {
                        "type": "string",
                        "description": ("Local JSON file with keys: elementor_data (list), custom_css (string, "
                                        "optional), media (optional list of {source_id, file_path, filename, "
                                        "title, alt}). source_id is the attachment ID used in elementor_data "
                                        "on the machine it was built on."),
                    },
                    "backup_dir":   {"type": "string"},
                    "source_host":  {"type": "string", "description": "Host in the incoming layout, e.g. 'mysite.local'"},
                    "template":     {"type": "string", "default": "elementor_canvas"},
                    "status":       {"type": "string"},
                    "dry_run":      {"type": "boolean", "default": False,
                                     "description": "Upload and remap, report what would change, but do not write the page."},
                },
            },
        ),
    ]


# ── Tool dispatch ──────────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> CallToolResult:
    client = get_client()

    try:
        if name == "wp_site_info":
            data = client.site_info()
            summary = {
                "name":        data.get("name"),
                "description": data.get("description"),
                "url":         data.get("url"),
                "wp_version":  data.get("generator", ""),
                "timezone":    data.get("timezone_string"),
                "namespaces":  data.get("namespaces", []),
            }
            return ok(summary)

        elif name == "wp_list_users":
            users = client.list_users(
                per_page=arguments.get("per_page", 20),
                page=arguments.get("page", 1),
                search=arguments.get("search"),
                roles=arguments.get("roles"),
            )
            # Slim down the response
            slim = [
                {
                    "id":       u.get("id"),
                    "username": u.get("username") or u.get("slug"),
                    "name":     u.get("name"),
                    "email":    u.get("email", ""),
                    "roles":    u.get("roles", []),
                    "link":     u.get("link"),
                }
                for u in (users if isinstance(users, list) else [])
            ]
            return ok({"total": len(slim), "users": slim})

        elif name == "wp_get_user":
            user_id = int(arguments["user_id"])
            data    = client.get_user(user_id)
            if data.get("code"):
                return err(data.get("message", str(data)))
            return ok(data)

        elif name == "wp_create_user":
            roles = arguments.get("roles") or ["subscriber"]
            data  = client.create_user(
                username=arguments["username"],
                email=arguments["email"],
                password=arguments["password"],
                first_name=arguments.get("first_name", ""),
                last_name=arguments.get("last_name", ""),
                roles=roles,
                description=arguments.get("description", ""),
            )
            if data.get("code"):
                return err(data.get("message", str(data)))
            return ok({
                "id":       data.get("id"),
                "username": data.get("slug"),
                "name":     data.get("name"),
                "email":    data.get("email"),
                "roles":    data.get("roles", []),
            })

        elif name == "wp_update_user":
            user_id = int(arguments.pop("user_id"))
            data    = client.update_user(user_id, **arguments)
            if data.get("code"):
                return err(data.get("message", str(data)))
            return ok({
                "id":       data.get("id"),
                "username": data.get("slug"),
                "name":     data.get("name"),
                "email":    data.get("email"),
                "roles":    data.get("roles", []),
            })

        elif name == "wp_delete_user":
            user_id        = int(arguments["user_id"])
            reassign_to_id = int(arguments["reassign_to_id"])
            data           = client.delete_user(user_id, reassign_to_id)
            if isinstance(data, dict) and data.get("code"):
                return err(data.get("message", str(data)))
            return ok({"deleted": True, "previous": data.get("previous", {})})

        elif name == "wp_list_posts":
            post_type = arguments.get("post_type", "pages")
            results = client.list_posts(
                post_type=post_type,
                per_page=arguments.get("per_page", 20),
                page=arguments.get("page", 1),
                search=arguments.get("search"),
                slug=arguments.get("slug"),
                status=arguments.get("status"),
            )
            if isinstance(results, dict) and results.get("code"):
                return err(results.get("message", str(results)))
            slim = [
                {
                    "id":     p.get("id"),
                    "slug":   p.get("slug"),
                    "status": p.get("status"),
                    "title":  (p.get("title") or {}).get("rendered", ""),
                    "link":   p.get("link"),
                    "parent": p.get("parent"),
                }
                for p in (results if isinstance(results, list) else [])
            ]
            return ok({"total": len(slim), "posts": slim})

        elif name == "wp_get_post":
            post_type = arguments.get("post_type", "pages")
            post_id   = int(arguments["post_id"])
            data      = client.get_post(post_type, post_id)
            if isinstance(data, dict) and data.get("code"):
                return err(data.get("message", str(data)))
            return ok(data)

        elif name == "wp_update_post":
            post_type = arguments.pop("post_type", "pages")
            post_id   = int(arguments.pop("post_id"))
            data      = client.update_post(post_type, post_id, **arguments)
            if isinstance(data, dict) and data.get("code"):
                return err(data.get("message", str(data)))
            out = {
                "id":      data.get("id"),
                "slug":    data.get("slug"),
                "status":  data.get("status"),
                "title":   (data.get("title") or {}).get("rendered", ""),
                "link":    data.get("link"),
                "modified": data.get("modified"),
            }
            # set when this write replaced an existing Elementor layout — tell the caller
            # where the previous one was saved, so the write is actually reversible.
            if data.get("_backup_file"):
                out["backup_file"] = data["_backup_file"]
            return ok(out)

        elif name == "wp_create_post":
            post_type = arguments.pop("post_type", "pages")
            data      = client.create_post(post_type, **arguments)
            if isinstance(data, dict) and data.get("code"):
                return err(data.get("message", str(data)))
            return ok({
                "id":     data.get("id"),
                "slug":   data.get("slug"),
                "status": data.get("status"),
                "link":   data.get("link"),
            })

        elif name == "wp_delete_post":
            post_type = arguments.get("post_type", "pages")
            post_id   = int(arguments["post_id"])
            force     = bool(arguments.get("force", False))
            data      = client.delete_post(post_type, post_id, force=force)
            if isinstance(data, dict) and data.get("code"):
                return err(data.get("message", str(data)))
            return ok({"deleted": True, "forced": force})

        elif name == "wp_get_acf_fields":
            post_type = arguments.get("post_type", "pages")
            post_id   = int(arguments["post_id"])
            acf       = client.get_acf_fields(post_type, post_id)
            if isinstance(acf, dict) and acf.get("code"):
                return err(acf.get("message", str(acf)))
            is_empty = acf is None or acf == [] or acf == {}
            return ok({
                "acf": acf,
                "note": (
                    "No ACF data found — this post's field groups likely aren't "
                    "REST-exposed (show_in_rest). Nothing to read or write via REST here."
                    if is_empty else None
                ),
            })

        elif name == "wp_update_acf_fields":
            post_type  = arguments.get("post_type", "pages")
            post_id    = int(arguments["post_id"])
            acf_fields = arguments["acf_fields"]
            data       = client.update_acf_fields(post_type, post_id, acf_fields)
            if isinstance(data, dict) and data.get("code"):
                return err(data.get("message", str(data)))
            returned_acf = data.get("acf")
            applied = {
                k: (returned_acf.get(k) == v if isinstance(returned_acf, dict) else False)
                for k, v in acf_fields.items()
            }
            return ok({
                "id": data.get("id"),
                "acf_after_update": returned_acf,
                "fields_confirmed_applied": applied,
                "note": (
                    None if isinstance(returned_acf, dict) and all(applied.values())
                    else "One or more fields did not come back matching what was sent — the "
                         "target field group(s) may not have show_in_rest enabled for writes."
                ),
            })

        elif name == "wp_list_media":
            results = client.list_media(
                per_page=arguments.get("per_page", 20),
                page=arguments.get("page", 1),
                search=arguments.get("search"),
                mime_type=arguments.get("mime_type"),
            )
            if isinstance(results, dict) and results.get("code"):
                return err(results.get("message", str(results)))
            slim = [
                {
                    "id":         m.get("id"),
                    "title":      (m.get("title") or {}).get("rendered", ""),
                    "filename":   os.path.basename(m.get("source_url", "")),
                    "source_url": m.get("source_url"),
                    "mime_type":  m.get("mime_type"),
                    "alt_text":   m.get("alt_text", ""),
                }
                for m in (results if isinstance(results, list) else [])
            ]
            return ok({"total": len(slim), "media": slim})

        elif name == "wp_get_media":
            data = client.get_media(int(arguments["media_id"]))
            if isinstance(data, dict) and data.get("code"):
                return err(data.get("message", str(data)))
            return ok(data)

        elif name == "wp_find_media_by_filename":
            found = client.find_media_by_filename(arguments["filename"])
            if found is None:
                return ok({"found": False, "note": "No attachment with that filename — safe to upload."})
            return ok({
                "found":      True,
                "id":         found.get("id"),
                "source_url": found.get("source_url"),
                "mime_type":  found.get("mime_type"),
                "alt_text":   found.get("alt_text", ""),
            })

        elif name == "wp_upload_media":
            file_path = arguments["file_path"]
            filename  = arguments.get("filename") or os.path.basename(file_path)

            if not arguments.get("allow_duplicate", False):
                existing = client.find_media_by_filename(filename)
                if existing:
                    return ok({
                        "id":         existing.get("id"),
                        "source_url": existing.get("source_url"),
                        "mime_type":  existing.get("mime_type"),
                        "reused":     True,
                        "note": (
                            "An attachment with this filename already exists, so it was reused "
                            "rather than uploaded again. Pass allow_duplicate=true to force a new upload."
                        ),
                    })

            data = client.upload_media(
                file_path=file_path,
                title=arguments.get("title"),
                alt_text=arguments.get("alt_text"),
                caption=arguments.get("caption"),
                filename=arguments.get("filename"),
            )
            if isinstance(data, dict) and data.get("code"):
                return err(data.get("message", str(data)))
            return ok({
                "id":         data.get("id"),
                "source_url": data.get("source_url"),
                "mime_type":  data.get("mime_type"),
                "alt_text":   data.get("alt_text", ""),
                "reused":     False,
            })

        elif name == "wp_delete_media":
            media_id = int(arguments["media_id"])
            force    = bool(arguments.get("force", True))
            data     = client.delete_media(media_id, force=force)
            if isinstance(data, dict) and data.get("code"):
                return err(data.get("message", str(data)))
            return ok({"deleted": True, "forced": force, "id": media_id})

        elif name == "wp_clear_elementor_cache":
            data = client.clear_elementor_cache()
            if isinstance(data, dict) and data.get("code"):
                return err(data.get("message", str(data)))
            return ok({"cleared": True, "response": data})

        elif name == "wp_get_elementor_page":
            post_type = arguments.get("post_type", "pages")
            info = client.get_elementor(post_type, int(arguments["post_id"]))
            if isinstance(info, dict) and info.get("code"):
                return err(info.get("message", str(info)))
            try:
                layout = ed.parse_layout(info.get("data"))
            except Exception as e:
                return err(f"_elementor_data is not valid JSON: {e}")
            out = {
                "post_id":       info["post_id"],
                "slug":          info["slug"],
                "status":        info["status"],
                "template":      info["template"],
                "link":          info["link"],
                "top_level_elements": len(layout),
                "media_ids":     ed.unique_media_ids(layout),
                "page_settings": info.get("page_settings"),
                "is_elementor":  info.get("edit_mode") == "builder",
            }
            if arguments.get("include_data"):
                out["elementor_data"] = layout
            return ok(out)

        elif name == "wp_backup_elementor_page":
            post_type = arguments.get("post_type", "pages")
            post_id   = int(arguments["post_id"])
            info = client.get_elementor(post_type, post_id)
            if isinstance(info, dict) and info.get("code"):
                return err(info.get("message", str(info)))
            path = ed.backup_layout(post_id, info.get("data"), info.get("page_settings"),
                                    info.get("template"), arguments["backup_dir"])
            return ok({"backup_file": path, "bytes": os.path.getsize(path),
                       "had_existing_layout": bool(info.get("data"))})

        elif name == "wp_deploy_elementor_page":
            post_type = arguments.get("post_type", "pages")
            post_id   = int(arguments["post_id"])
            dry_run   = bool(arguments.get("dry_run", False))

            with open(arguments["payload_file"]) as f:
                payload = json.load(f)
            layout = payload.get("elementor_data")
            if layout is None:
                return err("payload_file has no 'elementor_data' key")

            problem = ed.validate_layout(layout)
            if problem:
                return err(f"Refusing to deploy — incoming layout invalid: {problem}")

            # 1. back up whatever is currently on the page
            current = client.get_elementor(post_type, post_id)
            if isinstance(current, dict) and current.get("code"):
                return err(current.get("message", str(current)))
            backup = ed.backup_layout(post_id, current.get("data"), current.get("page_settings"),
                                      current.get("template"), arguments["backup_dir"])

            # 2. upload media (idempotent) and build the id/url mapping
            mapping, uploads = {}, []
            for m in payload.get("media", []) or []:
                filename = m.get("filename") or os.path.basename(m["file_path"])
                found = client.find_media_by_filename(filename)
                if found:
                    att, reused = found, True
                else:
                    att = client.upload_media(m["file_path"], title=m.get("title"),
                                              alt_text=m.get("alt"), filename=filename)
                    if isinstance(att, dict) and att.get("code"):
                        return err(f"Media upload failed for {filename}: {att.get('message')}")
                    reused = False
                mapping[int(m["source_id"])] = {"id": att["id"], "url": att["source_url"]}
                uploads.append({"filename": filename, "id": att["id"], "reused": reused})

            # 3. remap ids + urls for this site
            site_host = client.base.split("://", 1)[-1].split("/")[0]
            remapped, report = ed.remap_media(layout, mapping,
                                              source_host=arguments.get("source_host"),
                                              target_host=site_host)

            result = {
                "backup_file":   backup,
                "media":         uploads,
                "remapped":      report["remapped"],
                "host_rewrites": report["host_rewrites"],
                "unmapped_media": report["unmapped"],
            }
            if report["unmapped"]:
                result["warning"] = (
                    f"{len(report['unmapped'])} media reference(s) had no entry in the manifest and "
                    "still point at the source site's attachment IDs. Those images will break. "
                    "Add them to payload.media and re-run."
                )
            if dry_run:
                result["dry_run"] = True
                result["note"] = "Nothing was written to the page."
                return ok(result)

            # 4. write
            settings = dict(current.get("page_settings") or {})
            if payload.get("custom_css") is not None:
                settings["custom_css"] = payload["custom_css"]
            settings.setdefault("hide_title", "yes")

            # allow_unbacked: step 1 above already wrote `backup` for this page, so the
            # client-side guard would only re-read the layout to prove what we just saved.
            written = client.set_elementor(post_type, post_id, remapped, page_settings=settings,
                                           template=arguments.get("template", "elementor_canvas"),
                                           status=arguments.get("status"),
                                           allow_unbacked=True)
            if isinstance(written, dict) and written.get("code"):
                return err(f"Write failed (backup kept at {backup}): {written.get('message')}")

            # Without this the page keeps serving stale CSS — custom_css in particular
            # is stored but never rendered, which looks like the write silently failed.
            cache = client.clear_elementor_cache()
            result["elementor_cache_cleared"] = not (isinstance(cache, dict) and cache.get("code"))
            if not result["elementor_cache_cleared"]:
                result["cache_warning"] = (
                    "Could not clear Elementor's CSS cache (needs manage_options). The layout is "
                    "written but the page may render with stale CSS until the cache is cleared."
                )
            result["written"] = True
            result["link"] = written.get("link")
            result["modified"] = written.get("modified")
            return ok(result)

        else:
            return err(f"Unknown tool: {name}")

    except Exception as e:
        return err(str(e))


# ── Entry point ────────────────────────────────────────────────────────────────

async def main():
    global _client
    args   = parse_args()
    config = load_config(args.config)

    sys.stderr.write(f"[wordpress-mcp] Connecting to {config['wp_url']} …\n")
    _client = WPClient(config)
    sys.stderr.write(f"[wordpress-mcp] Ready.\n")

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

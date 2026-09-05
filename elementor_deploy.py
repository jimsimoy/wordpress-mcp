#!/usr/bin/env python3
"""
Elementor deploy helpers: media remapping and layout backups.

Elementor stores media as {"id": <attachment id>, "url": "<absolute url>"} inside the
layout JSON. Both are environment-specific: attachment ids are assigned per-site, and the
url carries the source host. Moving a layout between environments without rewriting them
leaves the page pointing at attachment ids that mean something else (or nothing) on the
target, so every image silently breaks.
"""

import copy
import datetime
import json
import os
import re


MEDIA_KEYS = ("image", "background_image", "background_overlay_image",
              "background_slideshow_gallery", "gallery", "logo")


def find_media_refs(data):
    """Every {"id": int, "url": str} media reference in a layout, with its path.
    Matches on shape rather than a key whitelist, so widget types we haven't
    seen still get picked up."""
    refs = []

    def walk(node, path=""):
        if isinstance(node, dict):
            if isinstance(node.get("id"), int) and isinstance(node.get("url"), str):
                refs.append({"path": path, "id": node["id"], "url": node["url"]})
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(data)
    return refs


def unique_media_ids(data):
    return sorted({r["id"] for r in find_media_refs(data)})


def remap_media(data, mapping, source_host=None, target_host=None):
    """Return a copy of `data` with media ids/urls rewritten.

    mapping: {old_id: {"id": new_id, "url": new_url}}
    Any id not in the mapping is left alone and reported in `unmapped`, so a
    partial mapping fails loudly rather than shipping a half-broken page.
    """
    out = copy.deepcopy(data)
    remapped, unmapped = [], []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("id"), int) and isinstance(node.get("url"), str):
                old = node["id"]
                if old in mapping:
                    node["id"] = mapping[old]["id"]
                    node["url"] = mapping[old]["url"]
                    remapped.append({"from": old, "to": mapping[old]["id"]})
                else:
                    unmapped.append({"id": old, "url": node["url"]})
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(out)

    # Catch any host string that isn't part of a {id,url} pair (inline HTML, custom CSS…)
    host_rewrites = 0
    if source_host and target_host and source_host != target_host:
        blob = json.dumps(out)
        host_rewrites = blob.count(source_host)
        if host_rewrites:
            out = json.loads(blob.replace(source_host, target_host))

    return out, {
        "remapped": len(remapped),
        "unmapped": unmapped,
        "host_rewrites": host_rewrites,
    }


def backup_layout(post_id, existing_data, page_settings, template, backup_dir):
    """Write the current layout to a timestamped file before it is overwritten.
    REST writes replace `_elementor_data` wholesale and WordPress keeps no
    revision of post meta, so without this an overwrite is unrecoverable."""
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(backup_dir, f"elementor-{post_id}-{stamp}.json")
    with open(path, "w") as f:
        json.dump({
            "post_id": post_id,
            "backed_up_at": stamp,
            "template": template,
            "_elementor_data": existing_data,
            "_elementor_page_settings": page_settings,
        }, f, indent=2)
    return path


def parse_layout(raw):
    """_elementor_data comes back as a JSON string (or already-parsed list)."""
    if raw in (None, "", []):
        return []
    if isinstance(raw, list):
        return raw
    return json.loads(raw)


def validate_layout(data):
    """Cheap structural check. A malformed layout produces a blank page and an
    editor that will not open, so refuse to write one."""
    if not isinstance(data, list):
        return "top level must be a list of elements"
    if not data:
        return "layout is empty — refusing to write (this would blank the page)"
    for i, el in enumerate(data):
        if not isinstance(el, dict):
            return f"element[{i}] is not an object"
        if "elType" not in el:
            return f"element[{i}] has no elType"
        if "id" not in el:
            return f"element[{i}] has no id"
    return None

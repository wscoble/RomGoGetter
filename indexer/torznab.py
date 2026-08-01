"""
torznab.py — Torznab XML endpoint implementation.

This is what Prowlarr / Questarr hit to discover ROMs. The XML response follows
the Torznab spec (https://torznab.github.io/spec-1.3-draft/torznab/Specification-v1.3.html)
which is the same protocol Sonarr/Radarr/Prowlarr/Lidarr/Readarr use.
"""
from __future__ import annotations

import os
import time
import urllib.parse
import xml.etree.ElementTree as ET
from xml.dom import minidom

INDEXER_NAME = "RomGoGetter"
INDEXER_URL  = os.environ.get("RGG_PUBLIC_URL", "http://localhost:9696")


# Category list advertised in /api?t=caps.
# Torznab spec categories for games aren't first-class, but newznab defines:
#   1000 = Console
#     1010 = Console/PS1, 1020 = PS2, 1030 = Wii, etc.
#   4000 = PC
#     4050 = PC/Mac
# We advertise the broad categories Questarr defaults to (4000,1000).
CATEGORIES = [
    {"id": "1000", "name": "Console", "description": "Console games"},
    {"id": "4000", "name": "PC",      "description": "PC games"},
    {"id": "5000", "name": "Other",   "description": "Misc"},
]


def caps_xml() -> str:
    """Build Torznab caps XML."""
    root = ET.Element("caps")
    server = ET.SubElement(root, "server")
    server.set("title", INDEXER_NAME)
    server.set("version", "1.0")
    server.set("url", INDEXER_URL)

    limits = ET.SubElement(root, "limits")
    limits.set("max", "100")
    limits.set("default", "50")

    searching = ET.SubElement(root, "searching")
    search_el = ET.SubElement(searching, "search")
    search_el.set("available", "yes")
    search_el.set("supportedParams", "q,cat,limit,offset")

    cats = ET.SubElement(root, "categories")
    for cat in CATEGORIES:
        c = ET.SubElement(cats, "category")
        c.set("id", cat["id"])
        c.set("name", cat["name"])
        c.set("description", cat["description"])

    return _prettify(root)


def items_xml(items: list[dict], total: int | None = None) -> str:
    """Build RSS XML with Torznab <item> entries."""
    rss = ET.Element("rss", {
        "version": "2.0",
        "xmlns:atom": "http://www.w3.org/2005/Atom",
        "xmlns:torznab": "http://torznab.com/spec/API-1.3-draft",
    })
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = INDEXER_NAME
    ET.SubElement(channel, "description").text = "Security-hardened ROM indexer for Questarr"
    ET.SubElement(channel, "link").text = INDEXER_URL
    ET.SubElement(channel, "language").text = "en-US"
    ET.SubElement(channel, "category").text = "5000"

    for item in items:
        i = ET.SubElement(channel, "item")
        ET.SubElement(i, "title").text = item["title"]
        ET.SubElement(i, "guid").text = item["guid"]
        ET.SubElement(i, "link").text = item["link"]
        ET.SubElement(i, "pubDate").text = item.get("pubDate") or _rfc822_now()
        ET.SubElement(i, "category").text = item.get("category", "5000")
        ET.SubElement(i, "description").text = (
            f"From {item.get('indexer', 'archive.org')} — match score {item.get('score', 0):.2f}"
        )
        size = int(item.get("size", 0) or 0)
        if size:
            enclosure = ET.SubElement(i, "enclosure")
            enclosure.set("url", item["link"])
            enclosure.set("length", str(size))
            enclosure.set("type", "application/zip")
        # Torznab attrs (mirrors Prowlarr's output)
        for name, val in [
            ("size", str(size)),
            ("category", item.get("category", "5000")),
        ]:
            a = ET.SubElement(i, "torznab:attr")
            a.set("name", name)
            a.set("value", str(val))
    return _prettify(rss)


def _rfc822_now() -> str:
    return time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())


def _prettify(elem: ET.Element) -> str:
    rough = ET.tostring(elem, encoding="utf-8")
    parsed = minidom.parseString(rough)
    return parsed.toprettyxml(indent="  ", encoding="UTF-8").decode("utf-8")
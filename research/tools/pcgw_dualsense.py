"""
Collect DualSense feature support for Steam games from PCGamingWiki page wikitext
(the Cargo query/export endpoints are closed to anonymous clients).

Usage: python -I pcgw_dualsense.py games.txt > out.jsonl
games.txt: one "appid|name" per line.
For each game the page is found by name (falling back to a search), the Steam AppID
in its infobox is checked, and the {{Input ...}} template parameters about
PlayStation/DualSense controllers are printed as JSON.
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request

API = "https://www.pcgamingwiki.com/w/api.php"
HEADERS = {"User-Agent": "Mozilla/5.0 (decky-dualsense-wired research; compatibility check)"}
KEYS = re.compile(r"playstation|dualsense|haptic|light bar", re.I)


def api(**params):
    params["format"] = "json"
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def wikitext(title):
    data = api(action="parse", page=title, prop="wikitext", redirects=1)
    if "error" in data:
        return None, None
    return data["parse"]["title"], data["parse"]["wikitext"]["*"]


def search(name):
    data = api(action="query", list="search", srsearch=name, srlimit=5)
    return [hit["title"] for hit in data.get("query", {}).get("search", [])]


def steam_ids(text):
    m = re.search(r"\|\s*steam appid\s*=\s*([^\n|]*)", text, re.I)
    ids = set(re.findall(r"\d+", m.group(1))) if m else set()
    m2 = re.search(r"\|\s*steam appid side\s*=\s*([^\n|]*)", text, re.I)
    if m2:
        ids |= set(re.findall(r"\d+", m2.group(1)))
    return ids


def input_params(text):
    start = text.find("{{Input")
    if start < 0:
        return {}
    end = text.find("\n}}", start)
    block = text[start:end]
    params = {}
    for line in block.splitlines():
        m = re.match(r"\|\s*([^=]+?)\s*=\s*(.*)", line)
        if m and KEYS.search(m.group(1)):
            params[m.group(1).strip()] = m.group(2).strip()
    return params


def lookup(appid, name):
    candidates = [name.replace("’", "'")]
    for title in candidates + search(candidates[0]):
        real, text = wikitext(title)
        if text and appid in steam_ids(text):
            return {"appid": appid, "name": name, "page": real, "input": input_params(text)}
        time.sleep(0.3)
    return {"appid": appid, "name": name, "page": None, "input": {}}


def main():
    with open(sys.argv[1], encoding="utf-8") as f:
        games = [line.strip().split("|", 1) for line in f if "|" in line]
    for appid, name in games:
        print(json.dumps(lookup(appid, name), ensure_ascii=False), flush=True)
        time.sleep(0.5)


if __name__ == "__main__":
    main()

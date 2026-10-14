"""inaturalist: what people have found near a place lately on iNaturalist, or
one person's own observations.

Runs on the Tesserae side, never the client. Returns a JSON-serialisable dict,
or {"error": "friendly message"}; never raises.

Two calls to iNaturalist's public API (no key; it asks for no more than one
request a second and 10,000 a day, and the answer is kept for refresh_min):

  /v1/observations/species_counts   the species found in the circle (or by the
                                    user) in the period, most observed first,
                                    each with its taxon and the taxon's photo
  /v1/observations                  the newest observations there, with the
                                    observer, the place and the photo

Pictures, in the order the "pictures" option asks for them:

  photo      the newest observation's own photo, else the species' photo on
             iNaturalist, resized for the layout and credited to its observer
  gen        an AI painting in a chosen style, made once per species through
             fal.ai from the observation's own openly licensed photo and kept
             (experimental, needs a fal key in the widget settings). Painting
             takes longer than Tesserae gives a fetch, so it runs in a
             background thread (paint.py) and the species shows its photo
             until the painting is ready. The admin page (blueprint() below)
             lists the paintings, keeps earlier versions, repaints and deletes.

The cluster page (sheet.py) cuts each painting out of its paper and packs the
species onto one page; until a painting exists the cluster is a montage of
photos. Every picture is resized and inlined as a data URI, as are the caption
fonts, because the render sandbox has no network.

Files in ctx["data_dir"]:
  result_<slug>.json    iNaturalist's answers (TTL = refresh_min)
  img/                  fetched originals and resized copies
  gen/                  AI paintings, their credits, cut-outs, the daily budget,
                        the last failure per species, and the sheets
  gen/archive/          earlier paintings kept when a species is repainted
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import json
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent


def _sibling(name: str) -> Any:
    """A helper module beside this file, loaded by path once under a name of
    its own (the plugin folder is neither a package nor on sys.path)."""
    full = f"_tesserae_plugins.inaturalist.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, HERE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


P = _sibling("paint")
S = _sibling("sheet")

INAT_SPECIES = "https://api.inaturalist.org/v1/observations/species_counts"
INAT_OBS = "https://api.inaturalist.org/v1/observations"
# Photos under any Creative Commons licence; an all-rights-reserved photo is
# never fetched, so what the panel shows may be shown.
LICENSED = "cc0,cc-by,cc-by-nc,cc-by-sa,cc-by-nc-sa,cc-by-nd,cc-by-nc-nd"
ERR_UNREACHABLE = "Couldn't reach iNaturalist."
ERR_BUSY = "iNaturalist is busy (it answered {err}); back on the next refresh."
ERR_NOWHERE = (
    "Set a place in this cell (or the app's location), or an iNaturalist username."
)

LAYOUTS = ("cluster", "gallery", "latest", "list", "bleed", "tally")
PICTURES = ("photo", "gen", "none")
LEVELS = ("colour", "gray16", "gray4", "mono")
CLUSTER_STYLES = ("sheet", "numbered", "wreath", "notebook", "drawer")
CLUSTER_TEXTS = ("key", "under", "none")
FOOTERS = ("show", "credits", "hide")
TEXT_SCALES = ("100", "60", "80", "125", "150")
ARRANGEMENTS = ("auto", "oval", "circle", "ring", "rows", "scatter", "heap")
SIZINGS = ("kind", "busy", "rare", "same")
LABEL_LINES = ("name", "sci", "kind", "count", "when", "who")
# The longest text each label line can take, for reserving room when packing.
_LABEL_ROOM = {
    "sci": None,
    "kind": "Grasshopper",
    "count": "999 observations",
    "when": "seen 30 Sep",
    "who": "by somebody-long",
}
DAYS = {"7d": 7, "14d": 14, "30d": 30, "90d": 90, "365d": 365}
PERIODS = {
    "7d": "this week",
    "14d": "this fortnight",
    "30d": "this month",
    "90d": "these three months",
    "365d": "this year",
}
QUALITIES = ("research", "")
RADII = ("2", "5", "10", "25", "50")
FONTS = {
    "classic": (),
    "playfair": ("playfair",),
    "typewriter": ("special_elite",),
    "hand": ("caveat",),
    "modern": ("archivo_black", "archivo_narrow"),
    "theme": (),
}
# The engraved faces every page is set in.
FONT_BASE: tuple[str, ...] = ("im_fell", "im_fell_sc")
# The longest edge of a picture for each layout, in pixels.
SIZE = {
    "cluster": 1400,
    "gallery": 480,
    "latest": 900,
    "list": 900,
    "bleed": 1200,
    "tally": 0,
}
# Most species the lists take, and the sheet.
LIST_MAX = 12
SHEET_MAX = S.SHEET_MAX
SPECIES_PER_PAGE = 50
OBS_PER_PAGE = 30

_slug = P._slug


def _tz(options: dict[str, Any]) -> tzinfo | None:
    """IANA zone carried on the location option, or None for the server's."""
    for value in (options.get("timezone"), options.get("location")):
        if isinstance(value, dict):
            value = value.get("timezone")
        if isinstance(value, str) and value.strip():
            with contextlib.suppress(ValueError, KeyError, OSError):
                return ZoneInfo(value.strip())
    return None


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _choice(value: Any, allowed: tuple[str, ...]) -> str:
    return value if isinstance(value, str) and value in allowed else allowed[0]


def _picks(value: Any, allowed: tuple[str, ...] | None = None) -> list[str]:
    """A multiselect's picks as a list of strings (a saved cell may carry a
    comma string or a single value)."""
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]
    if not isinstance(value, list):
        return []
    out = [str(v) for v in value if str(v)]
    return [v for v in out if v in allowed] if allowed else out


def _place(options: dict[str, Any]) -> tuple[float, float, str] | None:
    """(lat, lon, name) from the cell's location (or the app's, promoted)."""
    try:
        lat, lon = float(options.get("latitude")), float(options.get("longitude"))
    except (TypeError, ValueError):
        return None
    name = ""
    loc = options.get("location")
    if isinstance(loc, dict):
        name = str(loc.get("label") or loc.get("name") or "")
    elif isinstance(loc, str):
        name = loc
    name = name or str(options.get("label") or "")
    return lat, lon, name.split(",")[0].strip()


# ----------------------------------------------------------------------
# iNaturalist
# ----------------------------------------------------------------------


def _query(
    place: tuple[float, float, str] | None,
    radius_km: str,
    login: str,
    taxa: list[str],
    days: int,
    quality: str,
    today: date,
) -> dict[str, str]:
    q: dict[str, str] = {
        "d1": (today - timedelta(days=days)).isoformat(),
        "photo_license": LICENSED,
        "locale": "en",
    }
    if place:
        q["lat"], q["lng"], q["radius"] = (
            f"{place[0]:.4f}",
            f"{place[1]:.4f}",
            radius_km,
        )
    if login:
        q["user_login"] = login
    if taxa:
        q["taxon_id"] = ",".join(taxa)
    if quality:
        q["quality_grade"] = quality
    return q


def _from_inat(q: dict[str, str]) -> tuple[dict[str, Any] | None, str | None]:
    """iNaturalist's species counts and newest observations for a query."""
    sq = urllib.parse.urlencode({**q, "per_page": str(SPECIES_PER_PAGE)})
    species, err = P._json(f"{INAT_SPECIES}?{sq}")
    if err == "unreachable":
        return None, ERR_UNREACHABLE
    if err:
        return None, ERR_BUSY.format(err=err)
    if not isinstance(species, dict) or not isinstance(species.get("results"), list):
        return None, "iNaturalist's answer wasn't in the expected shape."
    oq = urllib.parse.urlencode(
        {
            **q,
            "photos": "true",
            "order_by": "observed_on",
            "order": "desc",
            "per_page": str(OBS_PER_PAGE),
        }
    )
    obs, err = P._json(f"{INAT_OBS}?{oq}")
    rows = obs.get("results") if isinstance(obs, dict) and not err else None
    return {
        "species": species["results"],
        "species_total": int(species.get("total_results") or 0),
        "obs": rows if isinstance(rows, list) else [],
        "obs_total": int(obs.get("total_results") or 0)
        if isinstance(obs, dict) and not err
        else 0,
        "obs_error": bool(err),
    }, None


def _credit(photo: dict[str, Any] | None) -> str:
    """'© Kevin Yonando (CC BY-NC) · iNaturalist' from a photo record."""
    if not photo:
        return "iNaturalist"
    who = str(photo.get("attribution_name") or "").strip() or (
        str(photo.get("attribution") or "").removeprefix("(c) ").split(",")[0].strip()
    )
    lic = str(photo.get("license_code") or "").upper().replace("-", " ")
    return (
        f"© {who}{f' ({lic})' if lic else ''} · iNaturalist" if who else "iNaturalist"
    )


def _medium(url: str) -> str:
    return re.sub(r"/(square|small|thumb)\.", "/medium.", url)


def _day_words(iso: str) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso or "")
    if not m:
        return ""
    return f"{int(m.group(3))} {date(1, int(m.group(2)), 1).strftime('%b')}"


def _days_ago(iso: str, today: date) -> int | None:
    with contextlib.suppress(ValueError, TypeError):
        return max(0, (today - date.fromisoformat(str(iso)[:10])).days)
    return None


def _rows(
    raw: dict[str, Any], today: date
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(species rows, newest finds) from iNaturalist's two answers."""
    newest: dict[int, dict[str, Any]] = {}
    for r in raw.get("obs", []):
        t = r.get("taxon") or {}
        if t.get("id") and t["id"] not in newest:
            newest[t["id"]] = r

    def photo_of(r: dict[str, Any] | None) -> tuple[str, str, bool]:
        """(medium url, credit, licensed) of an observation's first photo."""
        p = (r or {}).get("photos") or []
        p0 = p[0] if p and isinstance(p[0], dict) else None
        if not p0 or not p0.get("url"):
            return "", "", False
        return _medium(str(p0["url"])), _credit(p0), bool(p0.get("license_code"))

    species = []
    for s in raw.get("species", []):
        t = s.get("taxon") or {}
        name = str(t.get("preferred_common_name") or t.get("name") or "").strip()
        if not name:
            continue
        group, kind, factor = P.kind_of(t)
        n = newest.get(t.get("id"))
        own, own_credit, own_lic = photo_of(n)
        dp = t.get("default_photo") or {}
        dp_url = str(dp.get("medium_url") or _medium(str(dp.get("url") or "")))
        seen = str((n or {}).get("observed_on") or "")
        row = {
            "id": t.get("id"),
            "name": name,
            "sci": str(t.get("name") or ""),
            "iconic": str(t.get("iconic_taxon_name") or ""),
            "group": group,
            "kind": kind,
            "factor": factor,
            "count": int(s.get("count") or 0),
            "last": _day_words(seen),
            "ago_days": _days_ago(seen, today) if seen else None,
            "who": str(((n or {}).get("user") or {}).get("login") or ""),
            "place": str((n or {}).get("place_guess") or "").split(",")[0],
            # the photo shown: the observation's own, else the species'
            "photo": own or dp_url,
            "photo_credit": own_credit if own else _credit(dp) if dp_url else "",
        }
        # the reference a painting is made from: an openly licensed photo only
        if own and own_lic:
            row["ref"], row["ref_credit"] = own, own_credit
        elif dp_url and dp.get("license_code"):
            row["ref"], row["ref_credit"] = dp_url, _credit(dp)
        species.append(row)
    by_sci = {s["sci"]: s for s in species}
    finds = []
    for r in raw.get("obs", []):
        t = r.get("taxon") or {}
        name = str(t.get("preferred_common_name") or t.get("name") or "").strip()
        if not name:
            continue
        group, kind, _f = P.kind_of(t)
        sp = by_sci.get(str(t.get("name") or ""))
        own, own_credit, _lic = photo_of(r)
        seen = str(r.get("observed_on") or "")
        finds.append(
            {
                "name": name,
                "sci": str(t.get("name") or ""),
                "group": group,
                "kind": kind,
                "count": sp["count"] if sp else 1,
                "last": _day_words(seen),
                "ago_days": _days_ago(seen, today) if seen else None,
                "who": str((r.get("user") or {}).get("login") or ""),
                "place": str(r.get("place_guess") or "").split(",")[0],
                "photo": own or (sp or {}).get("photo", ""),
                "photo_credit": own_credit
                if own
                else (sp or {}).get("photo_credit", ""),
                "species": sp,
            }
        )
    return species, finds


# ----------------------------------------------------------------------
# Pictures
# ----------------------------------------------------------------------


def _photo(
    row: dict[str, Any], size: int, data_dir: Path, levels: str
) -> dict[str, Any] | None:
    url = str(row.get("photo") or "")
    if not url:
        return None
    raw = P._cached_get(data_dir, url)
    if not raw:
        return None
    key = hashlib.sha1(f"{url}|{size}".encode()).hexdigest()[:20]
    src = P._render(raw, size, None, data_dir / "img" / f"f_{key}.jpg", levels)
    if not src:
        return None
    return {
        "src": src,
        "kind": "photo",
        "credit": row.get("photo_credit") or "iNaturalist",
        "focus": [0.5, 0.42],
    }


def _attach_pictures(
    rows: list[dict[str, Any]],
    mode: str,
    size: int,
    fal_key: str,
    data_dir: Path,
    levels: str = "colour",
    style: str = "gould",
    allow_new: bool = True,
) -> list[str]:
    """Give each row a ``pic`` per the pictures option. Returns notices. A
    painting is at most one new one per fetch, in the background; the
    photos are fetched side by side."""
    notices: list[str] = []
    if mode == "none" or not rows:
        return notices
    if mode == "gen" and not fal_key:
        notices.append(P.NOTE_NO_KEY)
    started = not allow_new
    for row in rows:
        row["pic"] = None
        if mode != "gen":
            continue
        pic, did, note = _generated_for(
            row, size, data_dir, fal_key, not started, levels, style
        )
        started = started or did
        if note and note not in notices:
            notices.append(note)
        row["pic"] = pic
    missing = [r for r in rows if not r.get("pic")]
    with ThreadPoolExecutor(max_workers=6) as pool:
        photos = list(pool.map(lambda r: _photo(r, size, data_dir, levels), missing))
    for row, pic in zip(missing, photos, strict=True):
        row["pic"] = pic
    return notices


def _generated_for(
    row: dict[str, Any],
    size: int,
    data_dir: Path,
    fal_key: str,
    allow_new: bool,
    levels: str,
    style: str,
) -> tuple[dict[str, Any] | None, bool, str | None]:
    """A find row paints its species; a species row paints itself."""
    sp = row.get("species") if isinstance(row.get("species"), dict) else row
    return P._generated(
        sp, size, data_dir, fal_key, allow_new=allow_new, levels=levels, style=style
    )


def _specimen_sheet(
    species: list[dict[str, Any]],
    n: int,
    style: str,
    ratio: float,
    fal_key: str,
    data_dir: Path,
    levels: str,
    label: float = S.SHEET_LABEL,
    mode: str = "sheet",
    lines: tuple[str, ...] = ("name",),
    arrange: str = "auto",
    sizing: str = "kind",
) -> tuple[dict[str, Any] | None, list[str]]:
    """The cluster as a printed page: the most observed species cut out of
    their AI paintings and laid out together (one new painting per
    refresh). None until a species has a painting."""
    items = [dict(s) for s in sorted(species, key=lambda s: -s["count"])[:n]]
    notices: list[str] = []
    if not fal_key:
        notices.append(P.NOTE_NO_KEY)
    started = False
    for b in items:
        b["key"] = None
        pic, did, note = P._generated(
            b,
            SIZE["gallery"],
            data_dir,
            fal_key,
            allow_new=not started,
            levels=levels,
            style=style,
        )
        started = started or did
        if note and note not in notices:
            notices.append(note)
        if pic is not None:
            b["key"] = P._pkey(_slug(b["sci"] or b["name"]), style)
    ready = [b for b in items if b["key"]]
    if not ready:
        return None, notices
    for b in ready:
        room = []
        for line in lines:
            if line == "name":
                room.append(b["name"])
            elif line == "sci":
                room.append(b["sci"] or "")
            elif line in _LABEL_ROOM:
                room.append(_LABEL_ROOM[line])
        b["label_room"] = [t for t in room if t]
        if sizing == "same":
            b["factor"] = 1.0
        elif sizing == "busy":
            top = max(x["count"] for x in ready) or 1
            b["factor"] = round(0.65 + (b["count"] / top) ** 0.325, 2)
        elif sizing == "rare":
            top = max(x["count"] for x in ready) or 1
            b["factor"] = round(1.75 - b["count"] / top, 2)
    made = S._sheet(
        ready,
        [b["key"] for b in ready],
        ratio,
        SIZE["cluster"],
        data_dir,
        levels,
        label,
        mode,
        arrange,
    )
    if not made:
        return None, notices
    by_name = {b["name"]: b for b in items}
    shown = [
        {
            **m,
            "count": by_name.get(m["name"], {}).get("count", 0),
            "last": by_name.get(m["name"], {}).get("last", ""),
            "kind": by_name.get(m["name"], {}).get("kind", ""),
            "who": by_name.get(m["name"], {}).get("who", ""),
        }
        for m in made["birds"]
    ]
    credit = (
        "AI paintings, "
        + P._style(style)[0].split(" (")[0].lower()
        + " (fal.ai nano-banana), after the observers' iNaturalist photos"
    )
    return {
        "sheet": True,
        "pic": {
            "src": made["src"],
            "kind": "sheet",
            "paper": made["paper"],
            "credit": credit,
        },
        "birds": shown,
        "lines": list(lines),
        "ratio": made["ratio"],
        "label": made["label"],
        "mode": made["mode"],
        "waiting": [b["name"] for b in items if not b["key"]],
        "waiting_verb": "paint",
        "montage": False,
    }, notices


def _montage(
    species: list[dict[str, Any]], n: int, size: int, data_dir: Path, levels: str
) -> tuple[dict[str, Any], list[str]]:
    rows = [dict(s) for s in sorted(species, key=lambda s: -s["count"])[:n]]
    notices = _attach_pictures(rows, "photo", size, "", data_dir, levels)
    shown = [
        {"name": r["name"], "sci": r["sci"], "count": r["count"], "pic": r.get("pic")}
        for r in rows
    ]
    return {"pic": None, "birds": shown, "montage": True, "waiting": []}, notices


def _font_css(groups: tuple[str, ...]) -> str:
    """@font-face rules for the bundled faces in ``groups``, as data URIs."""
    if not groups:
        return ""
    try:
        manifest = json.loads(
            (HERE / "fonts" / "fonts.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return ""
    rules = []
    for file, face in manifest.items():
        if face.get("group") not in groups:
            continue
        try:
            data = base64.b64encode((HERE / "fonts" / file).read_bytes()).decode()
        except OSError:
            continue
        rules.append(
            f"@font-face{{font-family:'{face['family']}';font-style:{face['style']};"
            f"font-weight:{face['weight']};font-display:block;"
            f"src:url(data:font/woff2;base64,{data}) format('woff2');unicode-range:{face['range']};}}"
        )
    return "".join(rules)


# ----------------------------------------------------------------------
# fetch
# ----------------------------------------------------------------------


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    """The widget's data. The daily limit and a custom prompt ride on context
    variables for the call (and the painting threads it starts)."""
    cap_token = P._CAP.set(P._daily_cap(settings))
    custom_token = P._CUSTOM.set(None)
    try:
        return _fetch(options, settings, ctx=ctx)
    finally:
        P._CUSTOM.reset(custom_token)
        P._CAP.reset(cap_token)


def _fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    layout = _choice(options.get("layout"), LAYOUTS)
    pictures = _choice(options.get("pictures"), PICTURES)
    if layout == "tally":
        pictures = "none"
    levels = _choice(options.get("panel"), LEVELS)
    art_style = _choice(options.get("art_style"), P.GEN_STYLE_IDS)
    if art_style == "custom":
        how = (
            " ".join(str(options.get("art_prompt") or "").split())[:800]
            or P.CUSTOM_EXAMPLE
        )
        art_style = P._custom_tag(how)
        P._CUSTOM.set({art_style: ("Custom style", how)})
    cluster_style = _choice(options.get("cluster_style"), CLUSTER_STYLES)
    cluster_lines = tuple(
        v for v in LABEL_LINES if v in _picks(options.get("cluster_labels"))
    ) or ("name",)
    cluster_arrange = _choice(options.get("cluster_arrange"), ARRANGEMENTS)
    cluster_text = _choice(options.get("cluster_text"), CLUSTER_TEXTS)
    cluster_sizing = _choice(options.get("cluster_sizing"), SIZINGS)
    text_scale = int(_choice(options.get("cluster_text_scale"), TEXT_SCALES)) / 100
    footer = _choice(options.get("footer"), FOOTERS)
    font = _choice(options.get("font"), tuple(FONTS))
    # one number for the cluster, the gallery and the paintings (a cell saved
    # before 0.1.3 carries it as cluster_size)
    want = _int(options.get("count") or options.get("cluster_size"), 8, 1, SHEET_MAX)
    cluster_n = max(2, want)
    days_key = options.get("days") if options.get("days") in DAYS else "30d"
    quality = _choice(options.get("quality"), QUALITIES)
    radius_km = _choice(options.get("radius_km"), ("10", *RADII))
    refresh_min = _int(options.get("refresh_min"), 30, 5, 240)
    login = re.sub(
        r"[^A-Za-z0-9_.-]", "", str(options.get("user_login") or "").strip()
    )[:60]
    taxa = [t for t in _picks(options.get("groups")) if t.isdigit()]
    label = str(options.get("label") or "").strip()
    fal_key = str((settings or {}).get("fal_api_key") or "").strip()
    now = datetime.now(_tz(options))
    today = now.date()
    cw, ch = int(ctx.get("cell_w") or 0), int(ctx.get("cell_h") or 0)
    data_dir = Path(ctx.get("data_dir") or ".")
    with contextlib.suppress(OSError):
        data_dir.mkdir(parents=True, exist_ok=True)

    place = _place(options)
    if not place and not login:
        return {"error": ERR_NOWHERE, "label": label or "Nature nearby"}
    q = _query(place, radius_km, login, taxa, DAYS[days_key], quality, today)
    slug = hashlib.sha1(json.dumps(q, sort_keys=True).encode()).hexdigest()[:12]
    cache = data_dir / f"result_{slug}.json"
    raw = None
    if cache.exists() and time.time() - cache.stat().st_mtime < refresh_min * 60:
        with contextlib.suppress(OSError, json.JSONDecodeError):
            raw = json.loads(cache.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raw, err = _from_inat(q)
        if err or raw is None:
            # a stale answer beats an empty page while iNaturalist is busy
            with contextlib.suppress(OSError, json.JSONDecodeError):
                raw = (
                    json.loads(cache.read_text(encoding="utf-8"))
                    if cache.exists()
                    else None
                )
            if not isinstance(raw, dict):
                return {
                    "error": err or ERR_UNREACHABLE,
                    "label": label or "Nature nearby",
                }
        else:
            with contextlib.suppress(OSError):
                cache.write_text(json.dumps(raw), encoding="utf-8")

    species, finds = _rows(raw, today)
    place_name = place[2] if place else ""
    if not label:
        label = f"{login}'s finds" if login else (place_name or "Nature nearby")
    near = ""
    if place:
        near = f"within {radius_km} km" + (
            ""
            if place_name.lower() == label.lower() or not place_name
            else f" of {place_name}"
        )
    area = " · ".join(x for x in (near, f"by {login}" if login else "") if x)
    period = PERIODS[days_key]

    groups: dict[str, dict[str, Any]] = {}
    for s in species:
        g = groups.setdefault(s["group"], {"label": s["group"], "species": 0, "obs": 0})
        g["species"] += 1
        g["obs"] += s["count"]
    notices: list[str] = []
    cluster = None
    shown = species[: cluster_n if layout == "cluster" else min(want, LIST_MAX)]
    latest = finds[0] if finds else (species[0] if species else None)
    if layout == "cluster" and species:
        label_size = max(S.SHEET_LABEL, 13 / ch) if ch else S.SHEET_LABEL
        if pictures == "gen":
            ratio = cw / ch if cw and ch else 4 / 3
            if cluster_style == "numbered" and cluster_text == "key":
                # the numbered plate shares the cell with its key
                ratio = ratio * 0.64 / 0.92 if ratio >= 1 else ratio * 0.92 / 0.62
            cluster, notices = _specimen_sheet(
                species,
                cluster_n,
                art_style,
                ratio,
                fal_key,
                data_dir,
                levels,
                label_size,
                cluster_style,
                cluster_lines,
                cluster_arrange,
                cluster_sizing,
            )
        if cluster is None and pictures != "none":
            cluster, more = _montage(
                species, cluster_n, SIZE["gallery"], data_dir, levels
            )
            notices += [m for m in more if m not in notices]
        elif cluster is None:
            cluster, _m = _montage(
                species, cluster_n, SIZE["gallery"], data_dir, "colour"
            )
            for b in cluster["birds"]:
                b["pic"] = None
        cluster.setdefault("label", label_size)
        cluster.setdefault("lines", list(cluster_lines))
    elif layout == "gallery":
        notices = _attach_pictures(
            shown, pictures, SIZE["gallery"], fal_key, data_dir, levels, art_style
        )
    elif layout in ("latest", "list", "bleed") and latest:
        notices = _attach_pictures(
            [latest], pictures, SIZE[layout], fal_key, data_dir, levels, art_style
        )

    for f in finds:
        f.pop("species", None)
    for s in [*species, *finds]:
        s.pop("photo", None)
        s.pop("photo_credit", None)
        s.pop("ref", None)
        s.pop("ref_credit", None)
    credits: list[str] = []
    pictured: list[dict[str, Any]] = [*shown, *([latest] if latest else [])]
    if cluster:
        pictured = [{"pic": cluster["pic"]}] if cluster.get("pic") else cluster["birds"]
    for s in pictured:
        c = (s.get("pic") or {}).get("credit")
        if c and c not in credits:
            credits.append(c)
    if len(credits) > 3:
        credits = [
            "Photos "
            + ", ".join(c.removesuffix(" · iNaturalist") for c in credits[:2])
            + f" and {len(credits) - 2} more · iNaturalist"
        ]
    if raw.get("obs_error"):
        notices.append("newest observations did not load")
    groups_fonts: tuple[str, ...] = FONT_BASE
    if layout == "cluster" and cluster_style == "notebook":
        groups_fonts = (*groups_fonts, "caveat")
    groups_fonts = tuple(dict.fromkeys((*groups_fonts, *FONTS[font])))

    return {
        "label": label,
        "area": area,
        "period": period,
        "layout": layout,
        "pictures": pictures,
        "art_style": art_style,
        "font": font,
        "panel": levels,
        "show_sci": options.get("show_sci", True) is not False,
        "footer": footer,
        "count": want,
        "cluster": cluster,
        "cluster_text": cluster_text,
        "text_scale": text_scale,
        "cluster_lines": list(cluster_lines),
        "species": shown,
        "finds": finds[: min(want, LIST_MAX)],
        "latest": latest,
        "species_total": int(raw.get("species_total") or len(species)),
        "obs_total": int(raw.get("obs_total") or 0) or sum(s["count"] for s in species),
        "groups": sorted(groups.values(), key=lambda g: -g["obs"]),
        "date": now.strftime("%a %d %b"),
        "date_iso": now.strftime("%Y-%m-%d"),
        "credits": credits,
        # what needs the owner (no key, a failure) is printed on the panel;
        # progress (a painting under way, the day's limit) shows on the paintings page
        "notices": [n for n in notices if not P._is_progress(n)],
        "status": [n for n in notices if P._is_progress(n)],
        "font_css": _font_css(groups_fonts),
        "empty": not species,
    }


# ----------------------------------------------------------------------
# Admin page: the AI paintings
# ----------------------------------------------------------------------

_SLUG_RE = re.compile(r"[a-z0-9_]{1,60}(--[a-z]{1,20}){0,2}")
_ARCHIVED_RE = re.compile(r"([a-z0-9_]{1,60}(?:--[a-z]{1,20}){0,2})__(\d{8}-\d{6})")


def _known_species(data_dir: Path) -> list[tuple[str, str, str]]:
    """(common name, scientific name, iconic taxon) for the admin page's
    picker and the batch painter: every species this widget has shown."""
    seen: dict[str, tuple[str, str]] = {}
    with contextlib.suppress(OSError):
        for f in sorted(data_dir.glob("result_*.json")):
            raw = P._read_json(f, {})
            for s in raw.get("species", []) if isinstance(raw, dict) else []:
                t = (s.get("taxon") or {}) if isinstance(s, dict) else {}
                if t.get("name"):
                    seen.setdefault(
                        str(t["name"]),
                        (
                            str(t.get("preferred_common_name") or t["name"]),
                            str(t.get("iconic_taxon_name") or ""),
                        ),
                    )
    return sorted(
        ((n, sci, k) for sci, (n, k) in seen.items()), key=lambda b: b[0].lower()
    )


def _gallery(data_dir: Path) -> dict[str, Any]:
    """Everything the admin page shows, read from gen/: one entry per
    painting key (a species in a style)."""
    folder = P._gen_dir(data_dir)
    entries: dict[str, dict[str, Any]] = {}

    def entry(key: str) -> dict[str, Any]:
        base, _, style = key.partition("--")
        style = (
            style if style in P.GEN_STYLES or P._CUSTOM_RE.fullmatch(style) else "gould"
        )
        return entries.setdefault(
            key,
            {
                "slug": key,
                "name": base.replace("_", " ").capitalize(),
                "sci": "",
                "kind": "",
                "style": style,
                "style_label": P._style(style)[0],
                "active": None,
                "archive": [],
                "error": None,
            },
        )

    def take(e: dict[str, Any], meta: Any) -> None:
        if not isinstance(meta, dict):
            return
        if meta.get("name"):
            e["name"] = str(meta["name"])
        e["sci"] = e["sci"] or str(meta.get("sci") or "")
        e["kind"] = e["kind"] or str(meta.get("kind") or "")
        if meta.get("how") and not e.get("how"):
            e["how"] = str(meta["how"])
            words = e["how"]
            e["style_label"] = "Custom: " + (
                words[:44].rstrip() + "…" if len(words) > 45 else words
            )

    with contextlib.suppress(OSError):
        for f in folder.glob("*.jpg"):
            if (
                f.stem.startswith("sheet_")
                or not _SLUG_RE.fullmatch(f.stem)
                or not (folder / f"{f.stem}.json").exists()
            ):
                continue  # a resized copy or a built page, not a painting
            meta = P._read_json(folder / f"{f.stem}.json", {})
            e = entry(f.stem)
            take(e, meta)
            e["active"] = {
                "file": f.name,
                "credit": str(meta.get("credit") or ""),
                "made": time.strftime(
                    "%d %b %Y",
                    time.localtime(int(meta.get("made") or f.stat().st_mtime)),
                ),
            }
        for f in sorted((folder / "archive").glob("*.jpg"), reverse=True):
            m = _ARCHIVED_RE.fullmatch(f.stem)
            if not m:
                continue
            e = entry(m.group(1))
            take(e, P._read_json(f.with_suffix(".json"), {}))
            e["archive"].append(
                {
                    "file": f.name,
                    "made": datetime.strptime(m.group(2), "%Y%m%d-%H%M%S").strftime(
                        "%d %b"
                    ),
                }
            )
        for f in folder.glob("*.err"):
            if _SLUG_RE.fullmatch(f.stem) and not f.stem.startswith("sheet_"):
                err = P._read_json(f, {})
                e = entry(f.stem)
                take(e, err)
                e["error"] = str(err.get("why") or "failed")
    with P._PAINTING_LOCK:
        running = set(P._PAINTING)
    known = {_slug(sci): (n, sci, k) for n, sci, k in _known_species(data_dir)}
    for key, e in entries.items():
        e["painting"] = key in running
        base = key.partition("--")[0]
        if base in known:
            name, sci, kind = known[base]
            e["sci"] = e["sci"] or sci
            e["kind"] = e["kind"] or kind
            if e["name"].lower() == base.replace("_", " "):
                e["name"] = name
    ordered = sorted(
        entries.values(),
        key=lambda e: (e["active"] is None, e["name"].lower(), e["style"]),
    )
    return {
        "paintings": ordered,
        "used": P._budget_used(folder),
        "cap": P._CAP.get(),
        "running": sorted(running),
        "styles": [(k, v[0]) for k, v in P.GEN_STYLES.items()],
        "custom_example": P.CUSTOM_EXAMPLE,
        "style_groups": [
            (g, [(k, P.GEN_STYLES[k][0]) for k in ids]) for g, ids in P.GEN_STYLE_GROUPS
        ],
        "kinds": [(k, v[0]) for k, v in P.KINDS.items()],
    }


def _job_for(e: dict[str, Any]) -> dict[str, Any] | None:
    if not e["sci"]:
        return None
    job = {
        "key": e["slug"],
        "style": e["style"],
        "name": e["name"],
        "sci": e["sci"],
        "kind": e.get("kind") or "",
    }
    if e.get("how"):
        job["how"] = e["how"]
    return job


def blueprint() -> Any:
    """The admin page at /plugins/<id>/: every AI painting this widget has
    made, with earlier versions kept, and buttons to paint, repaint, delete
    or bring back an earlier version."""
    from flask import (
        Blueprint,
        abort,
        current_app,
        flash,
        redirect,
        render_template,
        request,
        send_from_directory,
        url_for,
    )

    bp = Blueprint("inaturalist_admin", __name__, template_folder="templates")

    def plugin() -> Any:
        registry = current_app.config["PLUGIN_REGISTRY"]
        for p in registry.plugins.values():
            with contextlib.suppress(OSError, AttributeError):
                if Path(p.path).resolve() == HERE:
                    return p
        abort(404)

    def data_dir() -> Path:
        path = Path(plugin().data_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def settings() -> dict[str, Any]:
        store = current_app.config.get("SETTINGS_STORE")
        if store is None:
            return {}
        p = plugin()
        got = store.get_for_runtime("plugins", p.id, p.manifest.get("settings", []))
        return got if isinstance(got, dict) else {}

    def fal_key() -> str:
        return str(settings().get("fal_api_key") or "").strip()

    @bp.before_request
    def daily_cap() -> None:
        P._CAP.set(P._daily_cap(settings()))

    def slug_or_404(slug: str) -> str:
        if not _SLUG_RE.fullmatch(slug):
            abort(404)
        return slug

    def back() -> Any:
        return redirect(url_for("inaturalist_admin.index"))

    def start(job: dict[str, Any], label: str) -> None:
        key = fal_key()
        folder = P._gen_dir(data_dir())
        if not key:
            flash("Add a fal.ai key in this widget's settings first.", "warn")
        elif P._budget_used(folder) >= P._CAP.get():
            flash(
                f"Today's limit of {P._CAP.get()} paintings is used up. Raise it in this widget's settings.",
                "warn",
            )
        elif not P._start_painting(job, data_dir(), key):
            flash(f"{label} is already being painted.", "info")
        else:
            with contextlib.suppress(OSError):
                (folder / f"{job['key']}.err").unlink()
            flash(
                f"Painting {label}. It takes up to a minute; this page redraws when it lands.",
                "success",
            )

    @bp.get("/")
    def index() -> str:
        dd = data_dir()
        return render_template(
            "inaturalist/admin.html",
            g=_gallery(dd),
            known=_known_species(dd),
            has_key=bool(fal_key()),
        )

    @bp.get("/progress")
    def progress() -> Any:
        """What is painting right now, for the page's live ticker."""
        from flask import jsonify

        folder = P._gen_dir(data_dir())
        now = time.time()
        with P._PAINTING_LOCK:
            running = [
                {
                    "key": k,
                    "name": name,
                    "style": P._style(style)[0],
                    "seconds": int(now - since),
                }
                for k, (since, name, style) in P._PAINTING_SINCE.items()
            ]
            batches = [P._style(t)[0] for t in sorted(P._BATCH)]
            finished = [{"key": k, "at": int(at)} for k, at in P._FINISHED]
        return jsonify(
            {
                "running": running,
                "batches": batches,
                "finished": finished,
                "used": P._budget_used(folder),
                "cap": P._CAP.get(),
                "has_key": bool(fal_key()),
            }
        )

    @bp.get("/painting/<path:name>")
    def image(name: str) -> Any:
        folder = P._gen_dir(data_dir())
        stem, _, ext = name.rpartition(".")
        if ext != "jpg":
            abort(404)
        if _SLUG_RE.fullmatch(stem):
            return send_from_directory(folder, name)
        if stem.startswith("archive/") and _ARCHIVED_RE.fullmatch(stem[8:]):
            return send_from_directory(folder / "archive", name[8:])
        abort(404)

    @bp.post("/paint")
    def paint() -> Any:
        sci = request.form.get("sci", "").strip()
        name = request.form.get("name", "").strip()
        known = {s: (n, k) for n, s, k in _known_species(data_dir())}
        kind = request.form.get("kind", "").strip()
        if not name:
            name = known.get(sci, (sci, ""))[0]
        if not kind:
            kind = known.get(sci, ("", ""))[1]
        if not sci or not re.fullmatch(r"[A-Z][a-z-]+ [a-z-]+( [a-z-]+)?", sci):
            flash("Give the scientific name, for example Amanita muscaria.", "warn")
            return back()
        if kind not in P.KINDS:
            kind = ""
        style = _choice(request.form.get("style"), P.GEN_STYLE_IDS)
        job = {
            "key": P._pkey(_slug(sci), style),
            "style": style,
            "name": name[:80],
            "sci": sci,
            "kind": kind,
        }
        if style == "custom":
            how = (
                " ".join(request.form.get("how", "").split())[:800] or P.CUSTOM_EXAMPLE
            )
            job["style"] = P._custom_tag(how)
            job["key"] = P._pkey(_slug(sci), job["style"])
            job["how"] = how
        start(job, f"the {name[:80]}")
        return back()

    @bp.post("/<slug>/repaint")
    def repaint(slug: str) -> Any:
        slug = slug_or_404(slug)
        e = next(
            (e for e in _gallery(data_dir())["paintings"] if e["slug"] == slug), None
        )
        job = _job_for(e) if e else None
        if job is None:
            abort(404)
        start(job, f"the {e['name']}")
        return back()

    @bp.post("/<slug>/delete")
    def delete(slug: str) -> Any:
        slug = slug_or_404(slug)
        folder = P._gen_dir(data_dir())
        for f in (
            folder / f"{slug}.jpg",
            folder / f"{slug}.json",
            folder / f"{slug}.err",
            folder / f"{slug}.cut.png",
        ):
            with contextlib.suppress(OSError):
                f.unlink()
        P._drop_derived(folder, slug)
        flash(
            "Painting deleted. With AI paintings on, the species is painted again the next time it is shown.",
            "success",
        )
        return back()

    def retire(keys: list[str]) -> int:
        folder = P._gen_dir(data_dir())
        done = 0
        for key in keys:
            if not _SLUG_RE.fullmatch(key) or not (folder / f"{key}.jpg").exists():
                continue
            P._archive(folder, key)
            with contextlib.suppress(OSError):
                (folder / f"{key}.err").unlink()
            done += 1
        return done

    def retired(n: int) -> None:
        if n:
            flash(
                f"{n} painting{'' if n == 1 else 's'} moved to earlier versions. The widget paints them again as the species are shown, within the daily limit.",
                "success",
            )
        else:
            flash("No paintings were selected.", "info")

    @bp.post("/budget-reset")
    def budget_reset() -> Any:
        P._write(
            P._gen_dir(data_dir()) / "budget.json",
            json.dumps({time.strftime("%Y-%m-%d"): 0}).encode(),
        )
        flash("Today's count is back to 0.", "success")
        return back()

    @bp.post("/bulk-delete")
    def bulk_delete() -> Any:
        retired(retire(request.form.getlist("keys")))
        return back()

    @bp.post("/style-paint")
    def style_paint() -> Any:
        """Paint every species this widget has shown in one style, skipping
        the ones already painted, one after another within the daily limit."""
        style = request.form.get("art", "")
        if style not in P.GEN_STYLES:
            abort(400)
        key = fal_key()
        if not key:
            flash("Add a fal.ai key in this widget's settings first.", "warn")
            return back()
        folder = P._gen_dir(data_dir())
        jobs = [
            {
                "key": P._pkey(_slug(sci), style),
                "style": style,
                "name": name,
                "sci": sci,
                "kind": kind,
            }
            for name, sci, kind in _known_species(data_dir())
            if not (folder / f"{P._pkey(_slug(sci), style)}.jpg").exists()
        ]
        label = P.GEN_STYLES[style][0]
        room = max(0, P._CAP.get() - P._budget_used(folder))
        if not jobs:
            flash(
                f"Every species shown so far already has a painting in {label}.", "info"
            )
        elif not room:
            flash(
                f"Today's limit of {P._CAP.get()} paintings is used up. Raise it in this widget's settings.",
                "warn",
            )
        elif not P._start_batch(jobs, data_dir(), key):
            flash(f"{label} is already being painted for every species.", "info")
        else:
            n = min(len(jobs), room)
            flash(
                f"Painting {n} species in {label}, one after another"
                + (
                    f"; {len(jobs) - n} more wait for tomorrow's limit"
                    if len(jobs) > n
                    else ""
                )
                + ". This page redraws as they arrive.",
                "success",
            )
        return back()

    @bp.post("/style-delete")
    def style_delete() -> Any:
        style = request.form.get("art", "")
        if style not in P.GEN_STYLES and not P._CUSTOM_RE.fullmatch(style):
            abort(400)
        keys = [
            e["slug"]
            for e in _gallery(data_dir())["paintings"]
            if e["style"] == style and e["active"]
        ]
        retired(retire(keys))
        return back()

    @bp.post("/<slug>/clear-error")
    def clear_error(slug: str) -> Any:
        slug = slug_or_404(slug)
        with contextlib.suppress(OSError):
            (P._gen_dir(data_dir()) / f"{slug}.err").unlink()
        return back()

    @bp.post("/archive/<stem>/use")
    def use_archived(stem: str) -> Any:
        import os

        m = _ARCHIVED_RE.fullmatch(stem)
        if not m:
            abort(404)
        folder = P._gen_dir(data_dir())
        slug, src = m.group(1), folder / "archive" / f"{stem}.jpg"
        if not src.exists():
            abort(404)
        meta = P._read_json(src.with_suffix(".json"), {})
        if (folder / f"{slug}.jpg").exists():
            P._archive(folder, slug)
        os.replace(src, folder / f"{slug}.jpg")
        if not isinstance(meta, dict) or not meta:
            meta = {"credit": "AI painting", "made": int(time.time())}
        P._write(folder / f"{slug}.json", json.dumps(meta).encode())
        with contextlib.suppress(OSError):
            src.with_suffix(".json").unlink()
        P._drop_derived(folder, slug)
        flash("That version is the species' painting again.", "success")
        return back()

    @bp.post("/archive/<stem>/delete")
    def delete_archived(stem: str) -> Any:
        if not _ARCHIVED_RE.fullmatch(stem):
            abort(404)
        arch = P._gen_dir(data_dir()) / "archive"
        for f in (arch / f"{stem}.jpg", arch / f"{stem}.json"):
            with contextlib.suppress(OSError):
                f.unlink()
        return back()

    return bp

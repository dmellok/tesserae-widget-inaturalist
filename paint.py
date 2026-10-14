"""The painting pipeline for the iNaturalist widget: fal.ai paintings of a
species from its own observation photo, kept under data/gen/, with the
daily budget, the live ticker's state and the art styles. Loaded by
server.py (and sheet.py) by path, once, under a module name of its own.

Shared with the Backyard Birds widget function for function: the styles
are the same words with the bird taken out ({kind} stands for what the
subject is), so a painting made for one looks like one made for the other."""

from __future__ import annotations

import base64
import contextlib
import contextvars
import hashlib
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

USER_AGENT = "tesserae-inaturalist/0.1 (+https://github.com/dmellok/tesserae)"
HTTP_TIMEOUT_S = 12
IMG_MAX_BYTES = 12_000_000
INAT_TAXA = "https://api.inaturalist.org/v1/taxa"
FAL_EDIT = "https://fal.run/fal-ai/nano-banana/edit"
# The background-removal model that cuts the subject from its paper once a painting is made.
FAL_CUTOUT = "https://fal.run/fal-ai/birefnet/v2"
GEN_DAILY_CAP = 12
GEN_CAPS = (6, 12, 24, 48, 96)
GEN_TIMEOUT_S = 120
# A species whose painting failed is left alone this long before another try.
GEN_RETRY_S = 6 * 3600
NOTE_NO_KEY = "AI paintings need a fal.ai key in this widget's settings."
NOTE_PAINTING = "Painting {name} with fal.ai; it appears on a later refresh."
NOTE_GEN_FAILED = "AI painting of {name} failed: {why}"
NOTE_GEN_CAP = "AI paintings: today's limit of {cap} is used up."

# The daily limit for the call (and the painting threads it starts, which copy the context).
_CAP: contextvars.ContextVar[int] = contextvars.ContextVar(
    "inat_cap", default=GEN_DAILY_CAP
)


def _daily_cap(settings: dict[str, Any] | None) -> int:
    with contextlib.suppress(TypeError, ValueError):
        cap = int((settings or {}).get("gen_daily_cap") or GEN_DAILY_CAP)
        if cap in GEN_CAPS:
            return cap
    return GEN_DAILY_CAP


def _is_progress(note: str) -> bool:
    """A notice about progress (a painting under way, the day's limit) rather
    than something the owner must fix; printed on the paintings page, not the panel."""
    return note.startswith(("Painting ", "AI paintings: today"))


# What a species is, by iNaturalist's iconic taxon: the group it is filed
# under, the noun the prompt calls it, how it is posed on the page in the
# manner of the plate a naturalist would draw, and how big it is drawn beside
# the others on a specimen sheet (a mammal large, an insect small).
KINDS: dict[str, tuple[str, str, str, float]] = {
    "Aves": (
        "Birds",
        "bird",
        "perched on a simple branch, the whole bird and its perch",
        1.0,
    ),
    "Mammalia": (
        "Mammals",
        "mammal",
        "standing in profile, the whole animal from nose to tail, nothing under its feet",
        1.5,
    ),
    "Reptilia": (
        "Reptiles",
        "reptile",
        "the whole animal seen from the side, nothing under it",
        1.0,
    ),
    "Amphibia": (
        "Amphibians",
        "amphibian",
        "the whole animal seen from the side a little above, nothing under it",
        0.75,
    ),
    "Actinopterygii": (
        "Fishes",
        "fish",
        "seen from the side as in a fish plate, the whole fish, no water",
        1.0,
    ),
    "Insecta": (
        "Insects",
        "insect",
        "the whole insect alone, seen from above with its wings open and every leg shown, as a pinned specimen in an entomological plate, on nothing",
        0.6,
    ),
    "Arachnida": (
        "Spiders",
        "spider",
        "the whole animal alone, seen from above with every leg shown, on nothing",
        0.55,
    ),
    "Mollusca": (
        "Molluscs",
        "mollusc",
        "the whole animal with its shell, seen from the side, on nothing",
        0.6,
    ),
    "Plantae": (
        "Plants",
        "plant",
        "a single cut stem with its leaves and flower or fruit, as a pressed specimen in a botanical plate, no soil, no pot, no ground",
        1.25,
    ),
    "Fungi": (
        "Fungi",
        "fungus",
        "the whole fruiting body alone in exactly the form the photo shows, whether a cap on a stem, a stemless bracket or shelf, a cup, a club, a coral, a puffball or a crust, cut cleanly from whatever it grew on, as a specimen in a mycological plate, with no ground, soil, leaf litter, wood, bark or log around it",
        0.85,
    ),
    "Chromista": (
        "Seaweeds",
        "seaweed",
        "a single frond laid flat as a pressed specimen in a seaweed plate, no rock, no sand, no water",
        0.9,
    ),
    "Protozoa": (
        "Slime moulds",
        "slime mould",
        "the organism alone, as in a naturalist's plate, with no log, bark or leaf under it",
        0.55,
    ),
    "Animalia": (
        "Other animals",
        "animal",
        "the whole animal alone, seen from the side as in a naturalist's plate, on nothing",
        0.8,
    ),
}
# Finer than the iconic taxon where a plate would say so: (ancestor taxon id, label, size factor).
FINER: tuple[tuple[int, str, float], ...] = (
    (47224, "Butterfly", 0.7),
    (47157, "Moth", 0.65),
    (47792, "Dragonfly", 0.7),
    (47208, "Beetle", 0.55),
    (47336, "Ant", 0.45),
    (47221, "Bee", 0.5),
    (47201, "Wasp", 0.5),
    (47822, "Fly", 0.5),
    (47744, "Bug", 0.5),
    (47651, "Grasshopper", 0.6),
    (48112, "Mantis", 0.65),
    (47118, "Spider", 0.55),
    (848319, "Marsupial", 1.5),
    (40268, "Bat", 0.7),
    (43698, "Rodent", 0.7),
    (41573, "Carnivore", 1.4),
    (152871, "Whale", 2.0),
    (26172, "Lizard", 1.0),
    (39532, "Turtle", 1.0),
    (20979, "Frog", 0.7),
    (47273, "Shark", 1.6),
    (47114, "Snail", 0.55),
    (47186, "Crab", 0.7),
    (47534, "Jellyfish", 0.9),
    (47549, "Sea star", 0.8),
    (47217, "Orchid", 1.0),
    (47604, "Daisy", 1.0),
    (47148, "Rose family", 1.1),
    (136329, "Conifer", 1.3),
    (121943, "Fern", 1.1),
    (311249, "Moss", 0.55),
    (50814, "Mushroom", 0.85),
    (54743, "Lichen", 0.6),
)


def kind_of(taxon: dict[str, Any] | None) -> tuple[str, str, float]:
    """(group label, kind label, size factor) for an iNaturalist taxon record."""
    t = taxon or {}
    group, noun, _pose, factor = KINDS.get(
        str(t.get("iconic_taxon_name") or ""), KINDS["Animalia"]
    )
    anc = t.get("ancestor_ids") or []
    for tid, label, f in FINER:
        if tid in anc:
            return group, label, f
    return group, noun.capitalize(), factor


def noun_of(iconic: str | None) -> tuple[str, str]:
    """(noun, pose) the prompt uses for an iconic taxon."""
    _g, noun, pose, _f = KINDS.get(str(iconic or ""), KINDS["Animalia"])
    return noun, pose


def _write(path: Path, data: bytes) -> None:
    """Write via a temp file and rename, so a concurrent render never reads a
    half-written image."""
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{id(data)}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60] or "x"


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------


def _http(
    url: str,
    token: str = "",
    accept: str = "application/json",
    *,
    auth_scheme: str = "Bearer",
    body: bytes | None = None,
    timeout: float = HTTP_TIMEOUT_S,
    detail: bool = False,
) -> tuple[bytes | None, str | None]:
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if token:
        headers["Authorization"] = f"{auth_scheme} {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=headers, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(IMG_MAX_BYTES + 1)
            return (None, "too large") if len(data) > IMG_MAX_BYTES else (data, None)
    except urllib.error.HTTPError as err:
        if err.code in (401, 403) and not detail:
            return None, "auth"
        why = f"HTTP {err.code}"
        if detail:
            with contextlib.suppress(Exception):
                body = json.loads(err.read(20_000).decode("utf-8", "replace"))
                msg = body.get("detail") if isinstance(body, dict) else None
                if isinstance(msg, list) and msg and isinstance(msg[0], dict):
                    msg = msg[0].get("msg")
                if isinstance(msg, str) and msg.strip():
                    why += f": {msg.strip()[:160]}"
        return None, why
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None, "unreachable"


def _json(url: str, token: str = "", **kw: Any) -> tuple[Any | None, str | None]:
    body, err = _http(url, token, **kw)
    if err or body is None:
        return None, err
    try:
        return json.loads(body.decode("utf-8")), None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "not json"


# ----------------------------------------------------------------------
# BirdNET-Go
# ----------------------------------------------------------------------


def _cached_get(data_dir: Path, url: str, token: str = "") -> bytes | None:
    """Fetch an image once and keep it; a failure is remembered for a day so a
    dead link isn't retried on every refresh."""
    folder = data_dir / "img"
    folder.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(url.encode()).hexdigest()[:20]
    path, miss = folder / f"{key}.bin", folder / f"{key}.miss"
    if path.exists():
        with contextlib.suppress(OSError):
            return path.read_bytes()
    if miss.exists() and time.time() - miss.stat().st_mtime < 86400:
        return None
    body, err = _http(url, token, accept="image/*", timeout=40)
    if err or not body:
        with contextlib.suppress(OSError):
            miss.touch()
        return None
    _write(path, body)
    return body


def _render(
    raw: bytes,
    size: int,
    box: list[float] | None = None,
    cache: Path | None = None,
    levels: str = "colour",
) -> str:
    """Crop to ``box`` (fractions), fit the longest edge to ``size`` and return a
    JPEG data URI. Cached per (source, box, size, levels) when ``cache`` is
    given. For a greyscale or black-and-white panel the picture is converted
    here, with the contrast stretched so a watercolour plate still reads once
    the panel dithers it; the harder the panel, the harder the stretch."""
    if cache is not None and levels != "colour":
        cache = cache.with_name(f"{cache.stem}_{levels}{cache.suffix}")
    if cache is not None and cache.exists():
        with contextlib.suppress(OSError):
            return (
                "data:image/jpeg;base64,"
                + base64.b64encode(cache.read_bytes()).decode()
            )
    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover - Pillow ships with Tesserae
        return "data:image/jpeg;base64," + base64.b64encode(raw).decode()
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    except Exception:  # noqa: BLE001 - any undecodable image is just skipped
        return ""
    if box and len(box) == 4:
        w, h = im.size
        x0, y0, x1, y1 = (max(0.0, min(1.0, float(v))) for v in box)
        if x1 - x0 > 0.05 and y1 - y0 > 0.05:
            im = im.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
    im.thumbnail((size, size), Image.Resampling.LANCZOS)
    if levels != "colour":
        cutoff = {"gray16": 0.5, "gray4": 1.0, "mono": 1.0}.get(levels, 1.0)
        im = ImageOps.autocontrast(im.convert("L"), cutoff=cutoff)
        if levels == "mono":
            # The panel dithers every mid-tone to a texture, and a plate's
            # reds and greens all sit in the mid-tones, so lift them a little
            # rather than pushing contrast, which turned the bird into a blot.
            im = im.point([round(255 * (i / 255) ** 0.8) for i in range(256)])
    out = io.BytesIO()
    im.save(out, "JPEG", quality=82, optimize=True, progressive=True)
    data = out.getvalue()
    if cache is not None:
        _write(cache, data)
    return "data:image/jpeg;base64," + base64.b64encode(data).decode()


def _paper(src: str, cache: Path, levels: str = "colour") -> str:
    """The page colour around a plate (or a painting in the plate style), as
    #rrggbb: the median of a thin band along the picture's edges. Full bleed
    fills the panel with it so the whole plate fits without a visible edge."""
    if levels == "mono":
        return "#ffffff"
    if levels != "colour":
        cache = cache.with_name(f"{cache.stem}_{levels}{cache.suffix}")
    with contextlib.suppress(OSError):
        hit = cache.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"#[0-9a-f]{6}", hit):
            return hit
    colour = "#f3ead6"
    try:
        from PIL import Image, ImageStat

        im = Image.open(io.BytesIO(base64.b64decode(src.split(",", 1)[1]))).convert(
            "RGB"
        )
        w, h = im.size
        b = max(2, min(w, h) // 40)
        bands = [
            im.crop((0, 0, w, b)),
            im.crop((0, h - b, w, h)),
            im.crop((0, 0, b, h)),
            im.crop((w - b, 0, w, h)),
        ]
        strip = Image.new("RGB", (sum(x.width * x.height for x in bands), 1))
        strip.putdata([px for band in bands for px in band.getdata()])
        r, g, bl = (int(v) for v in ImageStat.Stat(strip).median)
        colour = f"#{r:02x}{g:02x}{bl:02x}"
    except Exception:  # noqa: BLE001 - fall back to the default cream
        pass
    _write(cache, colour.encode())
    return colour


def _inat_ref(sci: str, data_dir: Path) -> tuple[str, str]:
    """(image URL, credit) of the first openly licensed iNaturalist photo of a
    species, cached for a month. The default photo is often all rights
    reserved, so the taxon's whole photo list is walked."""
    folder = data_dir / "img"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"inat_{_slug(sci)}.json"
    with contextlib.suppress(OSError, json.JSONDecodeError):
        if time.time() - path.stat().st_mtime < 30 * 86400:
            hit = json.loads(path.read_text(encoding="utf-8"))
            return str(hit.get("url", "")), str(hit.get("credit", ""))
    url = credit = ""
    q = urllib.parse.urlencode({"q": sci, "rank": "species", "per_page": 1})
    found, err = _json(f"{INAT_TAXA}?{q}")
    results = found.get("results") if isinstance(found, dict) and not err else None
    if results:
        taxon, err = _json(f"{INAT_TAXA}/{int(results[0].get('id') or 0)}")
        rows = taxon.get("results") if isinstance(taxon, dict) and not err else None
        photos = (
            [tp.get("photo") or {} for tp in (rows[0].get("taxon_photos") or [])]
            if rows
            else []
        )
        photo = next((p for p in photos if p.get("license_code")), None)
        if photo:
            url = str(photo.get("url") or "").replace("/square.", "/large.")
            who = (
                str(photo.get("attribution_name") or "").strip()
                or str(photo.get("attribution") or "")
                .removeprefix("(c) ")
                .split(",")[0]
                .strip()
            )
            credit = (
                f"© {who} ({str(photo['license_code']).upper()}) · iNaturalist"
                if who
                else "iNaturalist"
            )
    if results is not None:
        _write(path, json.dumps({"url": url, "credit": credit}).encode())
    return url, credit


_BIRD_STYLES: dict[str, tuple[str, str]] = {
    "gould": (
        "Hand-coloured lithograph (Gould)",
        "a plate from a nineteenth-century natural history book: a hand-coloured "
        "lithograph in the manner of John Gould. Soft watercolour washes over fine "
        "engraved linework, plain cream paper background, generous margins",
    ),
    "audubon": (
        "Watercolour after Audubon",
        "a life-size watercolour in the manner of John James Audubon's Birds of America: "
        "a lively natural pose among a few leaves, watercolour and pastel with fine "
        "detail in the feathers, plain cream paper background",
    ),
    "woodblock": (
        "Japanese woodblock print",
        "a Japanese kacho-e woodblock print in the manner of Ohara Koson: flat areas of "
        "colour with a fine dark keyline, a soft graded background, a single flowering "
        "branch, the texture of washi paper",
    ),
    "engraving": (
        "Black-ink engraving",
        "a black-ink copperplate engraving from a Victorian encyclopaedia of natural "
        "history: fine cross-hatching and stippling, no colour at all, plain white paper",
    ),
    "linocut": (
        "Linocut",
        "a bold linocut print in black ink with one muted colour: strong carved lines, "
        "simple shapes, a little texture from the block, plain off-white paper",
    ),
    "sketch": (
        "Field sketch",
        "a naturalist's field sketch in graphite with a light watercolour wash: loose "
        "construction lines, a few annotations of shape only, on cream sketchbook paper",
    ),
    "anime": (
        "Anime",
        "a hand-drawn anime film illustration: clean confident line art, soft cel shading, large expressive eyes and gentle pastel colours, still true to the species, on plain pale paper",
    ),
    "origami": (
        "Origami",
        "a flat, colourful origami illustration of the bird: folded from bright paper in bold flat planes of the species' own colours, every facet a clean flat shape with crisp straight creases drawn as fine lines, no shading, no gradients and no studio light, graphic and poster-like, on a plain pale paper background",
    ),
    "felt": (
        "Needle felt",
        "a needle-felted wool figure of the bird, soft and fuzzy, in the species' true colours and markings, photographed on a plain pale background",
    ),
    "clay": (
        "Clay figure",
        "a hand-sculpted plasticine figure of the bird for a stop-motion film, chunky with a slight sheen and visible thumbprints, in the species' true colours, on a plain pale background",
    ),
    "kawaii": (
        "Kawaii sticker",
        "a cute kawaii sticker of the bird: round and chibi with a happy face, flat bright colours, a thick white die-cut border, on plain white paper",
    ),
    "pixel": (
        "Pixel art",
        "16-bit pixel art sprite of the bird with a limited palette, crisp square pixels and no anti-aliasing, true to the species' colours, on a plain pale background",
    ),
    "bricks": (
        "Brick-built",
        "a model of the bird built from small plastic toy bricks, studs showing, in the species' colours, on a plain pale background",
    ),
    "risograph": (
        "Risograph print",
        "a risograph print in two or three overprinted spot colours, grainy texture and slight misregistration, on plain off-white paper",
    ),
    "screenprint": (
        "Screen print poster",
        "a mid-century screen-printed nature poster: flat shapes of colour, simple bold forms and a slightly textured ink, on plain cream paper",
    ),
    "embroidery": (
        "Embroidery",
        "satin-stitch embroidery in silk thread on plain natural linen, every feather in stitches of the species' true colours",
    ),
    "stainedglass": (
        "Stained glass",
        "a stained-glass panel of the bird: dark lead lines around pieces of jewel-coloured glass in the species' colours, on a plain pale background",
    ),
    "mosaic": (
        "Mosaic",
        "a mosaic of small square tesserae of stone and glass in the species' colours, set in pale grout, on a plain pale background",
    ),
    "papercut": (
        "Paper cut",
        "a layered cut-paper collage of the bird, with sheets of coloured paper in the species' colours and soft shadows between the layers, on plain white paper",
    ),
    "sumie": (
        "Ink and wash",
        "a Japanese sumi-e ink and wash painting: a few confident brushstrokes of black ink with soft grey washes and a touch of colour, on plain rice paper",
    ),
    "pointillism": (
        "Pointillism",
        "a pointillist painting made entirely of small dots of pure colour that blend at a distance, on plain white paper",
    ),
    "artnouveau": (
        "Art Nouveau illustration",
        "an Art Nouveau illustration with sinuous flowing lines, a muted palette and decorative leaves curling round the branch, on plain cream paper",
    ),
    "watercolour": (
        "Modern watercolour",
        "a loose, airy modern watercolour with soft wet edges, a few precise details and plenty of white space, on plain white paper",
    ),
    "charcoal": (
        "Charcoal",
        "a dramatic charcoal drawing with rich blacks, smudged soft tones and a few white chalk highlights, on plain toned paper",
    ),
    "hats": (
        "Tiny hats",
        "a realistic hand-coloured lithograph in the manner of John Gould on warm cream paper, except that the bird wears a small hat that suits its personality, painted with the same care",
    ),
    "superhero": (
        "Superhero",
        "a realistic natural history painting on plain cream paper of the bird striking a heroic pose with a small flowing cape, dramatic light, plumage still true to life",
    ),
    "portrait": (
        "Victorian portrait",
        "a stiff, dignified Victorian oil portrait of the bird as a family ancestor, in a small gilt oval frame, plumage painted true to life, on plain cream paper",
    ),
    "pirate": (
        "Pirate",
        "a realistic hand-coloured lithograph in the manner of John Gould on warm cream paper, except that the bird is a pirate with an eye patch, a small tricorn hat and a cross scowl",
    ),
    "artsandcrafts": (
        "Arts and Crafts (Morris)",
        "an Arts and Crafts illustration in the manner of William Morris: the bird set among dense, repeating stylised foliage and flowers like a wallpaper or tapestry panel, flat muted earth, olive and indigo colours with a firm dark outline, on plain unbleached paper",
    ),
    "jugendstil": (
        "Jugendstil",
        "a Jugendstil illustration, the German Art Nouveau: squarer, more geometric ornament, a bold even outline, flat areas of muted colour, the bird framed in a decorative cartouche of stylised leaves, on plain cream paper",
    ),
    "glasgow": (
        "Glasgow style",
        "a Glasgow style illustration in the manner of Charles Rennie Mackintosh and the Macdonald sisters: a tall elongated composition, stylised roses and fine grids, pale washed colours with a few strong dark lines and a great deal of empty space, on plain pale paper",
    ),
    "secession": (
        "Vienna Secession",
        "a Vienna Secession illustration in the manner of Klimt and Moser: the bird's plumage true to life but its surroundings made of flat decorative shapes, gold, black and jewel colours, mosaic-like patterned areas, spirals and squares, on plain cream paper",
    ),
    "deco": (
        "Art Deco",
        "an Art Deco illustration: stepped geometry, sunbursts and fans, the bird simplified into clean curves and chevrons, a palette of gold, black, teal and cream with flat colour and crisp edges, on plain cream paper",
    ),
    "fairytale": (
        "Golden Age fairy-tale plate",
        "a Golden Age illustration in the manner of Arthur Rackham and Edmund Dulac: fine ink line with soft muted watercolour wash, gnarled branches and twisted roots as a frame, a faintly magical atmosphere, on plain toned paper",
    ),
    "mucha": (
        "Mucha poster",
        "an Art Nouveau poster in the manner of Alphonse Mucha: the bird in a halo roundel, a flowing ornamental border, pastel flat colour with a crisp dark outline, and empty panels where lettering would go, on plain cream paper",
    ),
    "beardsley": (
        "Beardsley ink",
        "a black-and-white ink illustration in the manner of Aubrey Beardsley: bold sweeping lines, large flat black masses against white, ornamental dots and fine hatching, no colour at all, on plain white paper",
    ),
    "nouveauglass": (
        "Nouveau stained window",
        "a Tiffany-style Art Nouveau stained-glass window of the bird: leaded curving lines around pieces of opalescent coloured glass, wisteria and dragonfly ornament, soft light through the glass, on a plain pale background",
    ),
    "japonisme": (
        "Japonisme",
        "a Japonisme illustration, Western Art Nouveau borrowing from ukiyo-e: a flat bird on a branch, an asymmetric composition with an ornamented border and a small seal-like roundel, soft flat colours and a fine keyline, on plain cream paper",
    ),
    "chinoiserie": (
        "Chinoiserie",
        "a hand-painted chinoiserie wallpaper panel: the bird on a flowering branch with delicate blossom and butterflies, soft flat colours on a pale ground, fine brushwork, on plain pale paper",
    ),
    "persian": (
        "Persian miniature",
        "a Persian miniature painting: flat jewel colours, gold leaf, flattened perspective, the bird among tulips and stylised flowers under a patterned sky, with a gold and blue illuminated border, on plain cream paper",
    ),
    "bestiary": (
        "Medieval bestiary",
        "a page from a medieval bestiary: the bird painted in red, blue and green tempera with gold leaf on a vellum ground, drawn as a manuscript illuminator would, with a vine and acanthus border, no text",
    ),
    "byzantine": (
        "Byzantine icon",
        "a Byzantine icon panel of the bird: a burnished gold ground, a formal frontal pose with a halo-like circle behind the head, deep reds and blues in egg tempera, fine gold lines in the feathers",
    ),
    "folkart": (
        "Folk art",
        "a folk-art painting of the bird in the manner of Pennsylvania Dutch fraktur and Scandinavian rosemaling: symmetric scrolls, hearts and tulips, flat bright colours with a dark outline, on plain cream paper",
    ),
    "harper": (
        "Mid-century modern (Harper)",
        "a mid-century modern illustration in the manner of Charley Harper: the bird reduced to circles, triangles and clean geometric shapes, flat colour in a handful of tones, no outlines, true to the species' markings, on plain cream paper",
    ),
    "cigarette": (
        "Edwardian cigarette card",
        "an Edwardian cigarette card chromolithograph of the bird: a small framed picture with a decorative border, slightly faded printed inks, fine detail, and a blank panel below for the name, on plain cream card",
    ),
    "delft": (
        "Delft tile",
        "a Dutch Delft tile painting of the bird: cobalt blue on white tin-glazed earthenware, the bird in a tiled square with corner ornaments, brushy blue washes and a fine crackle in the glaze",
    ),
}


def _dekind(how: str) -> str:
    """A bird style's words with the bird taken out: {kind} stands for the subject."""
    for pat, rep in (
        (r"\bthe bird's\b", "the {kind}'s"),
        (r"\bthe bird\b", "the {kind}"),
        (r"\ba flat bird\b", "a flat {kind}"),
        (r"\bbird\b", "{kind}"),
        (r"\bplumage\b", "colouring"),
        (r"\bevery feather\b", "every mark"),
        (r"\bin the feathers\b", "in the markings"),
        (r"\bbirds\b", "subjects"),
    ):
        how = re.sub(pat, rep, how)
    return how


# Art styles for AI paintings: (label, how to paint it), with {kind} filled in
# per species. Each style keeps its own painting per species.
GEN_STYLES: dict[str, tuple[str, str]] = {
    k: (label, _dekind(how)) for k, (label, how) in _BIRD_STYLES.items()
}
# "custom" is the owner's own prompt (the art_prompt option), painted under a
# tag made from the prompt's words so each prompt keeps its own paintings.
GEN_STYLE_IDS = (*GEN_STYLES, "custom")
CUSTOM_EXAMPLE = (
    "an Art Nouveau illustration with sinuous flowing lines, a muted palette of "
    "sage, ochre and dusty rose, and a border of curling leaves and lilies, on "
    "plain cream paper"
)
_CUSTOM_RE = re.compile(r"custom[a-z]{8}")
_CUSTOM: contextvars.ContextVar[dict[str, tuple[str, str]] | None] = (
    contextvars.ContextVar("bb_custom", default=None)
)


def _custom_tag(how: str) -> str:
    """The style tag for a custom prompt: 'custom' and eight letters from its
    words, so the same prompt always finds its paintings."""
    n = int(hashlib.sha1(" ".join(how.lower().split()).encode()).hexdigest()[:16], 16)
    letters = ""
    for _ in range(8):
        n, r = divmod(n, 26)
        letters += chr(97 + r)
    return "custom" + letters


def _style(style: str | None) -> tuple[str, str]:
    """(label, prompt words) for a style id or custom tag. A custom tag is
    known through the request that named it; outside one (the admin page)
    the label alone, and the painting's own meta carries its words."""
    if style in GEN_STYLES:
        return GEN_STYLES[style]
    known = _CUSTOM.get() or {}
    if style in known:
        return known[style]
    if style and _CUSTOM_RE.fullmatch(style):
        return ("Custom style", "")
    return GEN_STYLES["gould"]


# The styles in the groups the pickers show them in.
GEN_STYLE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Plates and prints",
        (
            "gould",
            "audubon",
            "woodblock",
            "engraving",
            "linocut",
            "sketch",
            "risograph",
            "screenprint",
            "harper",
        ),
    ),
    (
        "Decorative",
        (
            "artnouveau",
            "mucha",
            "jugendstil",
            "glasgow",
            "secession",
            "deco",
            "artsandcrafts",
            "japonisme",
            "fairytale",
            "beardsley",
            "nouveauglass",
        ),
    ),
    (
        "Period and ornament",
        (
            "chinoiserie",
            "persian",
            "bestiary",
            "byzantine",
            "folkart",
            "delft",
            "cigarette",
        ),
    ),
    ("Fine art", ("watercolour", "sumie", "pointillism", "charcoal")),
    (
        "Crafts",
        (
            "origami",
            "papercut",
            "embroidery",
            "felt",
            "clay",
            "bricks",
            "stainedglass",
            "mosaic",
        ),
    ),
    ("Playful", ("anime", "kawaii", "pixel")),
    ("Jokes", ("hats", "superhero", "portrait", "pirate")),
)
GEN_NATURE = (
    "Paint only the {kind} in the reference photo, the {name} ({sci}), as {style}. Keep its "
    "colours, markings, shape and proportions exactly as in the photo: {pose}. The subject "
    "alone on plain, empty paper with a clear margin all round, as a specimen cut from its "
    "surroundings. Leave out everything around it in the photo: no ground, soil, leaf "
    "litter, grass, rocks, wood, bark, logs, water or sky, no other creatures or plants, "
    "no scenery, no text, no lettering, no border, no signature.{about}"
)
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"
ABOUT_MAX = 400


def _about(sci: str, data_dir: Path) -> str:
    """A sentence or two on what the species looks like, from the English
    Wikipedia summary of it, kept a month; '' when there is none. Fed to the
    model so a bracket fungus is not given a stem it never had."""
    if not sci:
        return ""
    folder = data_dir / "img"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"about_{_slug(sci)}.json"
    with contextlib.suppress(OSError, json.JSONDecodeError):
        if time.time() - path.stat().st_mtime < 30 * 86400:
            return str(json.loads(path.read_text(encoding="utf-8")).get("about", ""))
    answer, err = _json(
        WIKI_SUMMARY
        + urllib.parse.quote(sci.strip().replace(" ", "_"))
        + "?redirect=true"
    )
    about = ""
    if not err and isinstance(answer, dict):
        text = " ".join(str(answer.get("extract") or "").split())
        # the first sentences, to the cap, ending on a sentence
        if len(text) > ABOUT_MAX:
            cut = text[:ABOUT_MAX]
            text = cut[: cut.rfind(". ") + 1] if ". " in cut else cut
        about = text
    if not err:
        _write(path, json.dumps({"about": about}).encode())
    return about


_PAINTING: set[str] = set()
# For the paintings page's live ticker: when each painting started, its bird
# and style, and the last few to finish.
_PAINTING_SINCE: dict[str, tuple[float, str, str]] = {}  # key -> (since, name, style)
_FINISHED: list[tuple[str, float]] = []
_PAINTING_LOCK = threading.Lock()


def _pkey(slug: str, style: str) -> str:
    """File key for a species' painting in a style."""
    return f"{slug}--{style}"


def _gen_dir(data_dir: Path) -> Path:
    folder = data_dir / "gen"
    with contextlib.suppress(OSError):
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def _read_json(path: Path, default: Any) -> Any:
    with contextlib.suppress(OSError, json.JSONDecodeError):
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def _budget_used(folder: Path) -> int:
    used = _read_json(folder / "budget.json", {})
    return int(used.get(time.strftime("%Y-%m-%d"), 0)) if isinstance(used, dict) else 0


def _spend_budget(folder: Path) -> None:
    today = time.strftime("%Y-%m-%d")
    _write(
        folder / "budget.json",
        json.dumps({today: _budget_used(folder) + 1}).encode(),
    )


def _drop_derived(folder: Path, key: str) -> None:
    """Remove the resized copies and paper colours made from a painting,
    after it is replaced or deleted."""
    pattern = re.compile(rf"{re.escape(key)}(_\d+(_\w+)?\.jpg|(_\w+)?\.paper)")
    with contextlib.suppress(OSError):
        for f in folder.iterdir():
            if pattern.fullmatch(f.name):
                with contextlib.suppress(OSError):
                    f.unlink()


def _paint(job: dict[str, Any], data_dir: Path, fal_key: str) -> str | None:
    """Make one species' painting and keep it under job["key"]. Blocking: run
    it through _start_painting. ``job`` is {key, style, name, sci, kind} and
    may carry ``ref`` (an openly licensed photo URL of the observation
    itself) with ``ref_credit``; without one the species' first openly
    licensed iNaturalist photo stands in. Returns None, or why it failed
    (also kept in <key>.err for renders and the admin page)."""
    folder = _gen_dir(data_dir)
    key = job["key"]
    err_path = folder / f"{key}.err"
    label, how = _style(job.get("style") or "gould")
    how = str(job.get("how") or how)

    def fail(why: str) -> str:
        info = {k: job.get(k) for k in ("name", "sci", "style", "kind")}
        _write(
            err_path, json.dumps({"why": why, "at": int(time.time()), **info}).encode()
        )
        return why

    if not how:
        return fail(
            "the custom prompt behind this painting is not known; paint it again from the widget"
        )
    image_url, ref_credit = str(job.get("ref") or ""), str(job.get("ref_credit") or "")
    if not image_url and job.get("sci"):
        image_url, ref_credit = _inat_ref(job["sci"], data_dir)
    if not image_url:
        return fail("no openly licensed reference photo on iNaturalist")
    noun, pose = noun_of(job.get("kind"))
    about = str(job.get("about") or _about(job.get("sci") or "", data_dir))
    prompt = GEN_NATURE.format(
        kind=noun,
        name=job["name"],
        sci=job["sci"],
        style=how.replace("{kind}", noun),
        pose=pose,
        about=f" About the subject: {about}" if about else "",
    )
    _spend_budget(folder)
    body = json.dumps(
        {
            "prompt": prompt,
            "image_urls": [image_url],
            "num_images": 1,
            "output_format": "jpeg",
            "aspect_ratio": "3:4",
        }
    ).encode()
    answer, err = _json(
        FAL_EDIT,
        fal_key,
        auth_scheme="Key",
        body=body,
        timeout=GEN_TIMEOUT_S,
        detail=True,
    )
    if err:
        if err.startswith(("HTTP 401", "HTTP 403")):
            err = "fal.ai refused the key or the account has no balance (" + err + ")"
        return fail(err if err.startswith("fal") else f"fal.ai answered {err}")
    images = answer.get("images") if isinstance(answer, dict) else None
    out_url = str(images[0].get("url") or "") if images else ""
    raw, err = (
        _http(out_url, accept="image/*", timeout=60) if out_url else (None, "no image")
    )
    if not raw:
        return fail(f"fal.ai sent no picture ({err})")
    credit = f"AI painting, {label.split(' (')[0].lower()} (fal.ai nano-banana)"
    if ref_credit:
        credit += f" after {ref_credit}"
    img = folder / f"{key}.jpg"
    if img.exists():  # a repaint: the old one is kept, not lost
        _archive(folder, key)
    meta = {
        "credit": credit,
        "made": int(time.time()),
        "style": job.get("style") or "gould",
        "name": job.get("name", ""),
        "sci": job.get("sci", ""),
        "kind": job.get("kind", ""),
    }
    if _CUSTOM_RE.fullmatch(str(job.get("style") or "")):
        meta["how"] = how  # a custom prompt's words travel with the painting
    _write(folder / f"{key}.json", json.dumps(meta).encode())
    _write(img, raw)
    _drop_derived(folder, key)
    with contextlib.suppress(OSError):
        err_path.unlink()
    # the cut-out: the painting with its paper removed, from the model's own
    # copy of the picture; best effort, the sheet cuts it out itself without
    cut = _cut_out(out_url, fal_key)
    if cut:
        _write(folder / f"{key}.cut.png", cut)
    return None


def _cut_out(image_url: str, fal_key: str) -> bytes | None:
    """The painting at ``image_url`` with its paper removed, as PNG bytes with
    alpha, from the BiRefNet model on fal.ai; None when it declined."""
    body = json.dumps(
        {
            "image_url": image_url,
            "model": "General Use (Light)",
            "operating_resolution": "1024x1024",
            "output_format": "png",
            "refine_foreground": True,
        }
    ).encode()
    answer, err = _json(
        FAL_CUTOUT, fal_key, auth_scheme="Key", body=body, timeout=GEN_TIMEOUT_S
    )
    if err or not isinstance(answer, dict):
        return None
    url = str((answer.get("image") or {}).get("url") or "")
    if not url:
        return None
    raw, _err = _http(url, accept="image/*", timeout=60)
    return raw if raw and raw[:2] == b"\x89P" else None


def _archive(folder: Path, key: str) -> None:
    """Move a painting (and its credit) into gen/archive/."""
    arch = folder / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    with contextlib.suppress(OSError):
        os.replace(folder / f"{key}.jpg", arch / f"{key}__{stamp}.jpg")
    with contextlib.suppress(OSError):
        os.replace(folder / f"{key}.cut.png", arch / f"{key}__{stamp}.cut.png")
    with contextlib.suppress(OSError):
        os.replace(folder / f"{key}.json", arch / f"{key}__{stamp}.json")
    _drop_derived(folder, key)


def _start_painting(job: dict[str, Any], data_dir: Path, fal_key: str) -> bool:
    """Start a painting in the background. False if one is already running
    under this key. The thread carries the caller's context, so the widget's
    network capability scope still applies to it."""
    key = job["key"]
    if not job.get("how"):
        job["how"] = _style(job.get("style"))[1]
    with _PAINTING_LOCK:
        if key in _PAINTING:
            return False
        _PAINTING.add(key)
        _PAINTING_SINCE[key] = (
            time.time(),
            str(job.get("name") or key),
            str(job.get("style") or "gould"),
        )

    def run() -> None:
        try:
            _paint(job, data_dir, fal_key)
        except Exception as err:  # noqa: BLE001 - a thread must not die loudly
            _write(
                _gen_dir(data_dir) / f"{key}.err",
                json.dumps({"why": str(err)[:200], "at": int(time.time())}).encode(),
            )
        finally:
            with _PAINTING_LOCK:
                _PAINTING.discard(key)
                _PAINTING_SINCE.pop(key, None)
                _FINISHED.append((key, time.time()))
                del _FINISHED[:-20]

    ctx = contextvars.copy_context()
    threading.Thread(
        target=ctx.run, args=(run,), name=f"inat-paint-{key}", daemon=True
    ).start()
    return True


_BATCH: set[str] = set()


def _start_batch(jobs: list[dict[str, Any]], data_dir: Path, fal_key: str) -> bool:
    """Paint ``jobs`` one after another in one background thread, stopping at
    the daily cap. False when a batch in this style is already running. Each
    painting goes through _start_painting, so the admin page and the widget
    see it as a painting under way like any other."""
    if not jobs:
        return False
    tag = jobs[0].get("style") or "gould"
    with _PAINTING_LOCK:
        if tag in _BATCH:
            return False
        _BATCH.add(tag)
    folder = _gen_dir(data_dir)

    def run() -> None:
        try:
            for job in jobs:
                if _budget_used(folder) >= _CAP.get():
                    break
                if not _start_painting(job, data_dir, fal_key):
                    continue
                while True:
                    time.sleep(0.5)
                    with _PAINTING_LOCK:
                        if job["key"] not in _PAINTING:
                            break
        finally:
            with _PAINTING_LOCK:
                _BATCH.discard(tag)

    ctx = contextvars.copy_context()
    threading.Thread(
        target=ctx.run, args=(run,), name=f"inat-batch-{tag}", daemon=True
    ).start()
    return True


def _painting(
    key: str, size: int, data_dir: Path, levels: str = "colour"
) -> dict[str, Any] | None:
    """A kept painting, resized for the layout, or None."""
    folder = _gen_dir(data_dir)
    img = folder / f"{key}.jpg"
    if not img.exists():
        return None
    meta = _read_json(folder / f"{key}.json", {})
    meta = meta if isinstance(meta, dict) else {}
    try:
        raw = img.read_bytes()
    except OSError:
        return None
    src = _render(raw, size, None, folder / f"{key}_{size}.jpg", levels)
    if not src:
        return None
    return {
        "src": src,
        "kind": "generated",
        "credit": str(meta.get("credit") or "AI painting"),
        "focus": [0.5, 0.45],
        "paper": _paper(src, folder / f"{key}.paper", levels),
    }


def _may_paint(
    data_dir: Path, key: str, label: str, fal_key: str, allow_new: bool
) -> tuple[bool, str | None]:
    """(go ahead, notice) for starting a painting under ``key``: not while one
    is running, not within GEN_RETRY_S of a failure, not past the daily cap."""
    folder = _gen_dir(data_dir)
    with _PAINTING_LOCK:
        busy = key in _PAINTING
    if busy:
        return False, NOTE_PAINTING.format(name=label)
    failed = _read_json(folder / f"{key}.err", None)
    if (
        isinstance(failed, dict)
        and time.time() - float(failed.get("at") or 0) < GEN_RETRY_S
    ):
        return False, NOTE_GEN_FAILED.format(name=label, why=failed.get("why"))
    if not (allow_new and fal_key):
        return False, None
    if _budget_used(folder) >= _CAP.get():
        return False, NOTE_GEN_CAP.format(cap=_CAP.get())
    return True, None


def _generated(
    row: dict[str, Any],
    size: int,
    data_dir: Path,
    fal_key: str,
    allow_new: bool,
    levels: str = "colour",
    style: str = "gould",
) -> tuple[dict[str, Any] | None, bool, str | None]:
    """(picture, started_one, notice). Reads a kept painting, or starts one in
    the background when allowed: one new painting per fetch at most, and
    the daily limit, so a page fills in over a few refreshes. A species
    whose painting failed is left alone for GEN_RETRY_S and the failure is
    shown as a notice. The row's ``ref`` (an openly licensed photo of the
    observation itself) is the reference the species is painted from."""
    key = _pkey(_slug(row["sci"] or row["name"]), style)
    pic = _painting(key, size, data_dir, levels)
    if pic is not None:
        return pic, False, None
    go, note = _may_paint(data_dir, key, f"the {row['name']}", fal_key, allow_new)
    if not go:
        return None, False, note
    job = {
        "key": key,
        "style": style,
        "name": row["name"],
        "sci": row["sci"],
        "kind": row.get("iconic") or "",
    }
    if row.get("ref"):
        job["ref"], job["ref_credit"] = row["ref"], row.get("ref_credit") or ""
    started = _start_painting(job, data_dir, fal_key)
    return None, started, NOTE_PAINTING.format(name=f"the {row['name']}")

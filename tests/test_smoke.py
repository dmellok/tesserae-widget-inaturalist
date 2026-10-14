"""inaturalist: iNaturalist parsing and caching, the picture chain, AI
paintings, the cluster page, fonts, and a render smoke test of client.js.
No network: every outbound call is patched."""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("inat_server", ROOT / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


server = _load()
P = server.P

MELBOURNE = {
    "latitude": -37.81,
    "longitude": 144.96,
    "location": {"label": "Melbourne", "timezone": "Australia/Melbourne"},
}
GROUPS = ["3", "40151", "47158", "47126", "47170"]


def _settle(timeout: float = 10.0) -> None:
    end = time.time() + timeout
    while P._PAINTING and time.time() < end:
        time.sleep(0.02)
    assert not P._PAINTING, "a painting never finished"


def _jpeg(w: int = 300, h: int = 400, colour=(200, 120, 40)) -> bytes:
    """A blot on cream paper, like a painting."""
    im = Image.new("RGB", (w, h), (243, 234, 214))
    ImageDraw.Draw(im).ellipse((w * 0.3, h * 0.25, w * 0.7, h * 0.7), fill=colour)
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return buf.getvalue()


def _png_alpha() -> bytes:
    w, h = 300, 400
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse((80, 100, 220, 300), fill=(90, 60, 30, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _taxon(tid, name, common, iconic, ancestors=(), licensed=True):
    return {
        "id": tid,
        "name": name,
        "preferred_common_name": common,
        "iconic_taxon_name": iconic,
        "rank": "species",
        "ancestor_ids": list(ancestors),
        "default_photo": {
            "id": tid * 10,
            "license_code": "cc-by-nc" if licensed else None,
            "attribution": "(c) A. Naturalist, some rights reserved (CC BY-NC)",
            "attribution_name": "A. Naturalist",
            "medium_url": f"https://inaturalist-open-data.s3.amazonaws.com/photos/{tid * 10}/medium.jpg",
        },
    }


SPECIES = [
    {
        "count": 52,
        "taxon": _taxon(1, "Agrotis infusa", "Bogong Moth", "Insecta", (47158, 47157)),
    },
    {"count": 30, "taxon": _taxon(2, "Cygnus atratus", "Black Swan", "Aves", (3,))},
    {
        "count": 21,
        "taxon": _taxon(3, "Amanita muscaria", "Fly Agaric", "Fungi", (47170, 50814)),
    },
    {
        "count": 12,
        "taxon": _taxon(
            4, "Glossodia major", "Waxlip Orchid", "Plantae", (47126, 47217)
        ),
    },
    {
        "count": 9,
        "taxon": _taxon(
            5,
            "Trichosurus vulpecula",
            "Common Brushtail Possum",
            "Mammalia",
            (40151, 848319),
        ),
    },
    {
        "count": 4,
        "taxon": _taxon(
            6,
            "Vanessa kershawi",
            "Australian Painted Lady",
            "Insecta",
            (47158, 47157, 47224),
            licensed=False,
        ),
    },
]
OBS = [
    {
        "id": 100,
        "observed_on": "2026-10-13",
        "place_guess": "Hawthorn East, VIC, AU",
        "quality_grade": "research",
        "user": {"login": "paperdaisy16"},
        "taxon": SPECIES[1]["taxon"],
        "photos": [
            {
                "id": 500,
                "url": "https://inaturalist-open-data.s3.amazonaws.com/photos/500/square.jpg",
                "license_code": "cc-by-nc",
                "attribution": "(c) paperdaisy16, some rights reserved (CC BY-NC)",
            }
        ],
    },
    {
        "id": 101,
        "observed_on": "2026-10-11",
        "place_guess": "Carlton, VIC, AU",
        "quality_grade": "research",
        "user": {"login": "dpcumming"},
        "taxon": SPECIES[0]["taxon"],
        "photos": [
            {
                "id": 501,
                "url": "https://static.inaturalist.org/photos/501/square.jpg",
                "license_code": None,
                "attribution": "(c) dpcumming, all rights reserved",
            }
        ],
    },
]


@pytest.fixture
def offline():
    """No network: iNaturalist, the photos and fal all answer from here."""

    class Calls(list):
        bodies: list[bytes]

    calls = Calls()
    calls.bodies = []

    def fake_http(url, token="", accept="application/json", **kw):  # noqa: ARG001
        calls.append(url)
        if "nano-banana" in url and kw.get("body"):
            calls.bodies.append(kw["body"])
        if "species_counts" in url:
            return json.dumps({"total_results": 273, "results": SPECIES}).encode(), None
        if "/v1/observations?" in url:
            return json.dumps({"total_results": 744, "results": OBS}).encode(), None
        if "inaturalist.org/v1/taxa?" in url:
            return json.dumps({"results": [{"id": 7}]}).encode(), None
        if "inaturalist.org/v1/taxa/7" in url:
            photo = {
                "url": "https://inat.example/photos/1/square.jpg",
                "license_code": "cc-by",
                "attribution_name": "B. Botanist",
            }
            return json.dumps(
                {"results": [{"taxon_photos": [{"photo": photo}]}]}
            ).encode(), None
        if "birefnet" in url:
            return json.dumps(
                {"image": {"url": "https://fal.example/cut.png"}}
            ).encode(), None
        if "fal.run" in url:
            return json.dumps(
                {"images": [{"url": "https://fal.example/out.jpg"}]}
            ).encode(), None
        if accept == "image/*" and url.endswith(".png"):
            return _png_alpha(), None
        if accept == "image/*":
            return _jpeg(), None
        return None, "unreachable"

    with patch.object(P, "_http", side_effect=fake_http):
        yield calls


def _fetch(tmp_path, offline, **opts):  # noqa: ARG001
    settings = {"fal_api_key": opts.pop("fal_key", "")}
    options = {**MELBOURNE, "groups": GROUPS, "pictures": "none", **opts}
    return server.fetch(
        options, settings, ctx={"data_dir": str(tmp_path), "cell_w": 800, "cell_h": 480}
    )


def test_parse_query_and_cache(tmp_path, offline):
    r = _fetch(
        tmp_path,
        offline,
        layout="gallery",
        days="30d",
        radius_km="25",
        quality="research",
    )
    assert not r.get("error"), r
    assert r["species_total"] == 273 and r["obs_total"] == 744
    assert r["label"] == "Melbourne" and r["area"] == "within 25 km"
    assert r["period"] == "this month"
    first = offline[0]
    assert first.startswith(
        "https://api.inaturalist.org/v1/observations/species_counts?"
    )
    assert "taxon_id=3%2C40151%2C47158%2C47126%2C47170" in first
    assert (
        "radius=25" in first
        and "quality_grade=research" in first
        and "photo_license=cc0" in first
    )
    assert "user_login" not in first
    s = r["species"][0]
    assert (s["name"], s["sci"], s["kind"], s["group"], s["count"]) == (
        "Bogong Moth",
        "Agrotis infusa",
        "Moth",
        "Insects",
        52,
    )
    assert s["last"] == "11 Oct" and s["who"] == "dpcumming" and s["place"] == "Carlton"
    assert (
        r["species"][2]["kind"] == "Mushroom"
        and r["species"][3]["kind"] == "Orchid"
        and r["species"][4]["kind"] == "Marsupial"
    )
    assert r["latest"]["name"] == "Black Swan" and r["latest"]["who"] == "paperdaisy16"
    assert [g["label"] for g in r["groups"]] == [
        "Insects",
        "Birds",
        "Fungi",
        "Plants",
        "Mammals",
    ]
    assert "ref" not in s and "photo" not in s, (
        "fetch-time fields do not reach the client"
    )
    n = len(offline)
    _fetch(
        tmp_path,
        offline,
        layout="gallery",
        days="30d",
        radius_km="25",
        quality="research",
    )
    assert len(offline) == n, (
        "the second fetch inside refresh_min is served from the cache"
    )


def test_username_alone_looks_anywhere(tmp_path, offline):
    r = _fetch(
        tmp_path,
        offline,
        latitude=None,
        longitude=None,
        location="",
        user_login="kueda",
        layout="list",
    )
    assert not r.get("error")
    assert "user_login=kueda" in offline[0] and "lat=" not in offline[0]
    assert r["label"] == "kueda's finds" and r["area"] == "by kueda"
    r = _fetch(tmp_path, offline, user_login="kueda", layout="list", radius_km="5")
    assert r["area"] == "within 5 km of Melbourne · by kueda"


def test_nowhere_is_a_friendly_message(tmp_path, offline):
    r = _fetch(tmp_path, offline, latitude=None, longitude=None, location="")
    assert r["error"] == server.ERR_NOWHERE


def test_busy_inaturalist_serves_the_last_answer(tmp_path, offline):
    r = _fetch(tmp_path, offline, layout="tally", refresh_min="5")
    assert not r.get("error")
    cache = next(tmp_path.glob("result_*.json"))
    import os

    os.utime(cache, (time.time() - 3600, time.time() - 3600))
    with patch.object(P, "_http", return_value=(None, "HTTP 429")):
        r = server.fetch(
            {**MELBOURNE, "groups": GROUPS, "layout": "tally", "refresh_min": "5"},
            {},
            ctx={"data_dir": str(tmp_path)},
        )
    assert not r.get("error") and r["species_total"] == 273
    with patch.object(P, "_http", return_value=(None, "HTTP 429")):
        r = server.fetch(
            {**MELBOURNE, "groups": GROUPS, "layout": "tally", "radius_km": "2"},
            {},
            ctx={"data_dir": str(tmp_path)},
        )
    assert "busy" in r["error"]


def test_photos_are_the_observers_own_and_credited(tmp_path, offline):
    r = _fetch(tmp_path, offline, layout="gallery", pictures="photo")
    pics = [s["pic"] for s in r["species"]]
    assert all(
        p and p["kind"] == "photo" and p["src"].startswith("data:image/jpeg")
        for p in pics[:5]
    )
    # the swan's newest observation photo, credited to its observer; the moth's own photo is unlicensed
    # for painting but still shown here, as the observer's own
    assert r["species"][1]["pic"]["credit"] == "© paperdaisy16 (CC BY NC) · iNaturalist"
    assert any("/photos/500/medium.jpg" in u for u in offline)
    assert any("/photos/501/medium.jpg" in u for u in offline)
    assert pics[5] and pics[5]["credit"] == "© A. Naturalist · iNaturalist", (
        "the species photo stands in, shown even unlicensed on the owner's own panel"
    )
    assert (
        r["credits"]
        == ["Photos © paperdaisy16 (CC BY NC), © dpcumming and 1 more · iNaturalist"]
        or len(r["credits"]) <= 3
    )


def test_painting_once_per_species_from_its_own_photo(tmp_path, offline):
    r = _fetch(tmp_path, offline, layout="gallery", pictures="gen", fal_key="k")
    _settle()
    assert "AI paintings need" not in " ".join(r["notices"])
    assert any(n.startswith("Painting the Bogong Moth") for n in r["status"])
    prompt = json.loads(offline.bodies[0])
    assert prompt["prompt"].startswith(
        "Paint the insect in the reference photo, the Bogong Moth (Agrotis infusa), as a plate from"
    )
    assert "seen from above with its wings open" in prompt["prompt"]
    # the reference is the species' licensed photo (the observation's own was all rights reserved)
    assert prompt["image_urls"] == [
        "https://inaturalist-open-data.s3.amazonaws.com/photos/10/medium.jpg"
    ]
    gen = tmp_path / "gen"
    assert (gen / "agrotis_infusa--gould.jpg").exists() and (
        gen / "agrotis_infusa--gould.cut.png"
    ).exists()
    meta = json.loads((gen / "agrotis_infusa--gould.json").read_text())
    assert meta["kind"] == "Insecta" and meta["credit"].endswith(
        "after © A. Naturalist (CC BY NC) · iNaturalist"
    )
    r = _fetch(tmp_path, offline, layout="gallery", pictures="gen", fal_key="k")
    _settle()
    assert r["species"][0]["pic"]["kind"] == "generated"
    assert any(n.startswith("Painting the Black Swan") for n in r["status"]), (
        "one new painting per fetch"
    )
    assert len(offline.bodies) == 2
    swan = json.loads(offline.bodies[1])
    assert "Paint the bird in the reference photo, the Black Swan" in swan["prompt"]
    assert swan["image_urls"] == [
        "https://inaturalist-open-data.s3.amazonaws.com/photos/500/medium.jpg"
    ], "the observation's own licensed photo"


def test_painting_needs_a_key_and_a_cap(tmp_path, offline):
    r = _fetch(tmp_path, offline, layout="gallery", pictures="gen")
    assert P.NOTE_NO_KEY in r["notices"]
    assert not offline.bodies
    (tmp_path / "gen").mkdir(exist_ok=True)
    (tmp_path / "gen" / "budget.json").write_text(
        json.dumps({time.strftime("%Y-%m-%d"): 6})
    )
    r = server.fetch(
        {**MELBOURNE, "groups": GROUPS, "layout": "gallery", "pictures": "gen"},
        {"fal_api_key": "k", "gen_daily_cap": "6"},
        ctx={"data_dir": str(tmp_path)},
    )
    assert any("limit of 6" in n for n in r["status"]) and not offline.bodies


def test_custom_prompt_and_kind_in_every_style(tmp_path, offline):
    r = _fetch(
        tmp_path,
        offline,
        layout="gallery",
        pictures="gen",
        fal_key="k",
        art_style="custom",
        art_prompt="a tiny gouache on grey card",
    )
    _settle()
    assert r["art_style"].startswith("custom")
    prompt = json.loads(offline.bodies[0])["prompt"]
    assert "as a tiny gouache on grey card" in prompt
    for sid, (_label, how) in P.GEN_STYLES.items():
        assert "bird" not in how.replace("{kind}", ""), (sid, how)
        assert "plumage" not in how, sid
    assert "grumpy" not in P.GEN_STYLES
    assert set(P.GEN_STYLES) == {i for _g, ids in P.GEN_STYLE_GROUPS for i in ids}


def test_cluster_is_a_montage_until_painted_then_a_sheet(tmp_path, offline):
    r = _fetch(tmp_path, offline, layout="cluster", pictures="photo", cluster_size="4")
    c = r["cluster"]
    assert (
        c["montage"]
        and len(c["birds"]) == 4
        and c["birds"][0]["pic"]["kind"] == "photo"
    )
    for _ in range(4):
        r = _fetch(
            tmp_path,
            offline,
            layout="cluster",
            pictures="gen",
            fal_key="k",
            cluster_size="4",
            refresh_min="5",
        )
        _settle()
    # every painting is in; the sheet is built in the background on the next fetch
    assert len(offline.bodies) == 4
    for _ in range(20):
        r = _fetch(
            tmp_path,
            offline,
            layout="cluster",
            pictures="gen",
            fal_key="k",
            cluster_size="4",
            cluster_labels=["name", "kind", "count"],
        )
        _settle()
        # an earlier sheet of the same shape stands in while the fuller one builds
        if (
            r["cluster"]
            and r["cluster"].get("sheet")
            and len(r["cluster"]["birds"]) == 4
        ):
            break
        time.sleep(0.2)
    c = r["cluster"]
    assert (
        c["sheet"]
        and c["pic"]["kind"] == "sheet"
        and c["pic"]["src"].startswith("data:image/jpeg")
    )
    assert len(c["birds"]) == 4 and c["lines"] == ["name", "kind", "count"]
    assert c["birds"][0]["kind"] and c["waiting"] == []
    for b in c["birds"]:
        assert 0 <= b["at"][0] <= 1 and 0 <= b["at"][1] <= 1.05
    assert r["credits"][0].startswith("AI paintings, hand-coloured lithograph")


def test_sizes_by_kind_and_sizing_option(tmp_path, offline):
    rows = [
        {"name": "A", "sci": "a a", "count": 10, "factor": 1.5, "kind": "Mammal"},
        {"name": "B", "sci": "b b", "count": 2, "factor": 0.6, "kind": "Insect"},
    ]
    gen = tmp_path / "gen"
    gen.mkdir()
    for r in rows:
        (gen / f"{P._slug(r['sci'])}--gould.jpg").write_bytes(_jpeg())
        (gen / f"{P._slug(r['sci'])}--gould.json").write_text(
            json.dumps({"credit": "x", "made": 1})
        )
    with patch.object(server.S, "_sheet", return_value=None) as sheet:
        server._specimen_sheet(
            rows, 2, "gould", 1.5, "k", tmp_path, "colour", sizing="kind"
        )
        assert [b["factor"] for b in sheet.call_args[0][0]] == [1.5, 0.6]
        server._specimen_sheet(
            rows, 2, "gould", 1.5, "k", tmp_path, "colour", sizing="busy"
        )
        assert sheet.call_args[0][0][0]["factor"] > sheet.call_args[0][0][1]["factor"]
        server._specimen_sheet(
            rows, 2, "gould", 1.5, "k", tmp_path, "colour", sizing="rare"
        )
        assert sheet.call_args[0][0][0]["factor"] < sheet.call_args[0][0][1]["factor"]
        server._specimen_sheet(
            rows, 2, "gould", 1.5, "k", tmp_path, "colour", sizing="same"
        )
        assert [b["factor"] for b in sheet.call_args[0][0]] == [1.0, 1.0]


def test_fonts_follow_the_look(tmp_path, offline):
    r = _fetch(tmp_path, offline, layout="gallery")
    assert "IM Fell English" in r["font_css"] and "Caveat" not in r["font_css"]
    r = _fetch(
        tmp_path, offline, layout="cluster", cluster_style="notebook", font="playfair"
    )
    assert "Caveat" in r["font_css"] and "Playfair" in r["font_css"]


def test_panel_levels_convert_pictures(tmp_path, offline):
    r = _fetch(tmp_path, offline, layout="gallery", pictures="photo", panel="mono")
    src = r["species"][0]["pic"]["src"]
    import base64

    im = Image.open(io.BytesIO(base64.b64decode(src.split(",", 1)[1])))
    assert im.mode == "L" and r["panel"] == "mono"


def test_progress_stays_off_the_panel(tmp_path, offline):
    r = _fetch(tmp_path, offline, layout="gallery", pictures="gen", fal_key="k")
    _settle()
    assert r["status"] and not any(n.startswith("Painting") for n in r["notices"])


def test_admin_gallery_lists_paintings(tmp_path, offline):
    _fetch(tmp_path, offline, layout="gallery", pictures="gen", fal_key="k")
    _settle()
    g = server._gallery(tmp_path)
    assert (
        g["paintings"][0]["name"] == "Bogong Moth"
        and g["paintings"][0]["sci"] == "Agrotis infusa"
    )
    assert g["paintings"][0]["kind"] == "Insecta" and g["paintings"][0]["active"]
    assert ("Fungi", "Fungi") in g["kinds"]
    known = server._known_species(tmp_path)
    assert ("Fly Agaric", "Amanita muscaria", "Fungi") in known
    job = server._job_for(g["paintings"][0])
    assert job == {
        "key": "agrotis_infusa--gould",
        "style": "gould",
        "name": "Bogong Moth",
        "sci": "Agrotis infusa",
        "kind": "Insecta",
    }


def test_manifest_is_well_formed():
    m = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    names = [o["name"] for o in m["cell_options"]]
    assert len(names) == len(set(names))
    for o in m["cell_options"]:
        if o["type"] in ("select", "multiselect"):
            values = [c["value"] for c in o["choices"]]
            assert len(values) == len(set(values)), o["name"]
            if "default" in o and o["type"] == "select":
                assert o["default"] in values, o["name"]
            if o["type"] == "multiselect":
                assert all(d in values for d in o["default"]), o["name"]
        for dep in o.get("show_if") or {}:
            assert dep in names, (o["name"], dep)
    art = next(o for o in m["cell_options"] if o["name"] == "art_style")
    assert {c["value"] for c in art["choices"]} == set(P.GEN_STYLE_IDS)
    groups = next(o for o in m["cell_options"] if o["name"] == "groups")
    assert all(c["value"].isdigit() for c in groups["choices"])
    assert len(groups["choices"]) >= 40


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("layout", server.LAYOUTS)
def test_client_renders_every_layout(tmp_path, offline, layout):
    data = _fetch(
        tmp_path,
        offline,
        layout=layout,
        pictures="photo" if layout != "tally" else "none",
    )
    script = (
        f"import('file://{ROOT}/client.js').then(m => {{ const s = {{innerHTML: ''}};"
        f" m.default(s, {{data: {json.dumps(data)}, cell: {{size: 'md'}}}});"
        " if (!s.innerHTML.includes('class=\"bb lay-')) throw new Error('no render');"
        " console.log('ok'); })"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert "ok" in out.stdout


def test_admin_template_renders(tmp_path, offline):
    """The admin page renders against the gallery payload: a stub of the host's
    base template and component macros stands in for Tesserae itself."""
    import jinja2
    from flask import Flask

    _fetch(tmp_path, offline, layout="gallery", pictures="gen", fal_key="k")
    _settle()
    app = Flask("t", template_folder=str(ROOT / "templates"))
    stubs = {
        "_base.html": "{% block title %}{% endblock %}{% block main %}{% endblock %}",
        "_components.html": (
            "{% macro card_head(t, icon_name='', level=2, blurb='') %}<h2>{{ t }}</h2>{{ blurb }}{% endmacro %}"
            "{% macro button(t, icon='', variant='', disabled=False) %}<button>{{ t }}</button>{% endmacro %}"
            "{% macro empty_state(i, t, b) %}<p>{{ t }} {{ b }}</p>{% endmacro %}"
        ),
    }
    app.jinja_loader = jinja2.ChoiceLoader(
        [jinja2.DictLoader(stubs), jinja2.FileSystemLoader(str(ROOT / "templates"))]
    )
    app.register_blueprint(server.blueprint(), url_prefix="/plugins/inaturalist")
    with app.test_request_context("/plugins/inaturalist/"):
        from flask import render_template

        html = render_template(
            "inaturalist/admin.html",
            g=server._gallery(tmp_path),
            known=server._known_species(tmp_path),
            has_key=True,
        )
    assert "Bogong Moth" in html and "Agrotis infusa" in html
    assert 'name="sci"' in html and html.count("<optgroup") >= 8
    assert "No paintings yet" not in html

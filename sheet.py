"""The specimen sheet for the iNaturalist widget: each species' painting cut
out of its plain paper (the cut-out model's alpha when the painting came
with one, else the paper found from the picture's edges), the cut-outs
packed onto one page in one of five looks and printed on it. The Backyard
Birds widget's code with the bird length table replaced by the size factor
each row carries. Loaded by server.py by path; it takes the painting
module's state from the same loaded instance."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent


def _sibling(name: str) -> Any:
    full = f"_tesserae_plugins.inaturalist.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, _HERE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


P = _sibling("paint")
_PAINTING, _PAINTING_LOCK = P._PAINTING, P._PAINTING_LOCK
_read_json, _write, _render, _paper, _gen_dir = (
    P._read_json,
    P._write,
    P._render,
    P._paper,
    P._gen_dir,
)

# Layout version of the specimen sheet, part of its cache tag.
SHEET_LAYOUT = "nature1"
# Most species a specimen sheet shows.
SHEET_MAX = 24
# Label type size as a fraction of the sheet's height.
SHEET_LABEL = 0.017


def _subject_mask(im: Any, paper: tuple[int, int, int]) -> Any:
    """The subject (and its perch or ground) in a painting on plain paper, as an L mask.

    The paper is found by spreading in from the picture's edges over
    anything that looks like paper: close to the paper colour, or a smooth,
    grey, light wash such as a painted ground or shadow. What the spread
    cannot reach is the bird, so a white bird enclosed by its own outline
    stays whole even though its feathers are the paper's colour. Worked on a
    small copy for speed, then scaled back up."""
    import numpy as np
    from PIL import Image, ImageFilter

    w, h = im.size
    k = min(1.0, 320 / max(w, h))
    small = im.resize(
        (max(1, int(w * k)), max(1, int(h * k))), Image.Resampling.BILINEAR
    )
    a = np.asarray(small).astype(np.int16)
    diff = np.abs(a - np.array(paper, dtype=np.int16)).max(axis=2)
    lum = small.convert("L")
    hp = np.abs(
        np.asarray(lum, dtype=np.int16)
        - np.asarray(lum.filter(ImageFilter.GaussianBlur(2)), dtype=np.int16)
    )
    texture = np.asarray(
        Image.fromarray(np.clip(hp * 8, 0, 255).astype(np.uint8)).filter(
            ImageFilter.BoxBlur(2)
        )
    )
    sat = a.max(axis=2) - a.min(axis=2)
    L = np.asarray(lum, dtype=np.int16)
    paper_l = int(0.299 * paper[0] + 0.587 * paper[1] + 0.114 * paper[2])
    paperlike = diff < 24
    washlike = (texture < 24) & (sat < 26) & (L > paper_l - 95)
    cand = paperlike | washlike
    bg = np.zeros_like(cand)
    bg[0, :], bg[-1, :], bg[:, 0], bg[:, -1] = (
        cand[0, :],
        cand[-1, :],
        cand[:, 0],
        cand[:, -1],
    )
    for _ in range(4 * max(bg.shape)):
        grown = bg.copy()
        grown[1:, :] |= bg[:-1, :]
        grown[:-1, :] |= bg[1:, :]
        grown[:, 1:] |= bg[:, :-1]
        grown[:, :-1] |= bg[:, 1:]
        grown &= cand
        if (grown == bg).all():
            break
        bg = grown
    # a wash's own hard edge is texture, not paper: let the paper spread a
    # few more steps from the wash (never from plain paper, so a bird's pale
    # edge against plain paper is left alone) into pale grey pixels
    edge = (sat < 26) & (L > paper_l - 95)
    reach = bg & washlike & ~paperlike
    for _ in range(8):
        grown = reach.copy()
        grown[1:, :] |= reach[:-1, :]
        grown[:-1, :] |= reach[1:, :]
        grown[:, 1:] |= reach[:, :-1]
        grown[:, :-1] |= reach[:, 1:]
        reach = grown & (edge | bg)
    bg = bg | reach
    obj = Image.fromarray(((~bg) * 255).astype(np.uint8))
    # drop specks, close pinholes, back to full size
    obj = obj.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(5))
    return obj.resize((w, h), Image.Resampling.BILINEAR).point(
        lambda v: 255 if v > 127 else 0
    )


def _cutout_masked(
    raw: bytes, cut_png: bytes | None = None
) -> tuple[Any, Any, tuple[int, int, int]] | None:
    """(the bird cropped from its painting with the paper turned white and
    everything outside its outline whitened too, its mask, the paper
    colour). Whitening outside a soft mask drops any wash or vignette the
    painter left round the bird, so only the bird and its perch are printed
    on the sheet. With ``cut_png``, the painting as the cut-out model
    returned it, its alpha is the mask and nothing is guessed."""
    try:
        from PIL import Image, ImageChops, ImageFilter, ImageStat
    except ImportError:  # pragma: no cover
        return None
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    im.thumbnail((1000, 1000))
    w, h = im.size
    b = max(2, min(w, h) // 40)
    edge = Image.new("RGB", (w, b * 2))
    edge.paste(im.crop((0, 0, w, b)), (0, 0))
    edge.paste(im.crop((0, h - b, w, h)), (0, b))
    paper = tuple(int(v) for v in ImageStat.Stat(edge).median)
    hard = None
    if cut_png:
        with contextlib.suppress(Exception):
            alpha = Image.open(io.BytesIO(cut_png)).convert("RGBA").getchannel("A")
            hard = alpha.resize((w, h), Image.Resampling.BILINEAR).point(
                lambda v: 255 if v > 127 else 0
            )
    if hard is None:
        try:
            hard = _subject_mask(im, paper)
        except ImportError:  # pragma: no cover - numpy ships with Tesserae
            diff = ImageChops.difference(im, Image.new("RGB", im.size, paper)).convert(
                "L"
            )
            hard = diff.point(lambda v: 255 if v > 30 else 0)
            hard = hard.filter(ImageFilter.MaxFilter(7)).filter(
                ImageFilter.MinFilter(5)
            )
    # the crop box ignores specks: measure it on a slightly eroded mask
    box = hard.filter(ImageFilter.MinFilter(7)).getbbox() or hard.getbbox()
    if not box:
        return None
    pad = max(w, h) // 60
    box = (
        max(0, box[0] - pad),
        max(0, box[1] - pad),
        min(w, box[2] + pad),
        min(h, box[3] + pad),
    )
    lut = []
    for c in paper:
        lut += [min(255, round(i * 255 / max(1, c))) for i in range(256)]
    crop = im.crop(box).point(lut)
    mask = hard.crop(box).filter(ImageFilter.GaussianBlur(2))
    white = Image.new("RGB", crop.size, (255, 255, 255))
    return Image.composite(crop, white, mask), hard.crop(box), paper  # type: ignore[return-value]


def _rot_of(name: str, mode: str) -> float:
    """A small, steady tilt for the field notebook's taped-in birds."""
    if mode != "notebook":
        return 0.0
    return (int(hashlib.sha1(name.encode()).hexdigest()[:4], 16) % 9) - 4.0


def _pack_sheet(
    birds: list[dict[str, Any]],
    W: int,
    H: int,
    label: float = SHEET_LABEL,
    mode: str = "sheet",
    arrange: str = "auto",
) -> list[tuple[int, int, float]] | None:
    """Place cut-outs on a W x H page: biggest first, a little apart, each with
    room under it for its label. Where they go depends on ``mode``:

      sheet, numbered  the free spot nearest the middle of an oval
      wreath           a ring round an empty middle, nearest the ring's line
      notebook         rows across a ruled page, top left first

    Returns (x, y, scale) per bird, or None if numpy is missing; birds that
    do not fit at all get scale 0."""
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - numpy ships with Tesserae
        return None
    from PIL import Image, ImageFilter

    cell = 6
    gw, gh = W // cell, H // cell
    ys, xs = np.mgrid[0:gh, 0:gw]
    if arrange == "auto":
        arrange = {"wreath": "ring", "notebook": "rows"}.get(mode, "oval")
    cx, cy, rx, ry = gw / 2, gh * 0.47, gw * 0.48, gh * 0.45
    if arrange in ("circle", "ring"):
        cy = gh / 2
        rx = ry = min(gw * 0.48, gh * 0.47)
        if arrange == "ring" and mode == "wreath":
            rx, ry = gw * 0.48, gh * 0.45
    oval = ((xs + 0.5 - cx) / rx) ** 2 + ((ys + 0.5 - cy) / ry) ** 2
    top = 0.1 if mode == "notebook" else 0.05
    left = 0.12 if mode == "notebook" else 0.04
    page = (xs >= gw * left) & (xs < gw * 0.96) & (ys >= gh * top) & (ys < gh * 0.93)
    if arrange == "ring":
        inside = (oval <= 1) & (oval >= 0.3**2)
    else:
        # the oval and circle are soft targets: birds are placed from the
        # middle out and pulled inward, but only the page margin is a wall,
        # so a tall plate does not cap how big everything can be drawn
        inside = page
    label_rows = max(2, int(label * 1.5 * H / cell))
    gap = 1

    def footprint(b: dict[str, Any], scale: float) -> Any:
        m = b["mask"]
        if b.get("rot"):
            m = m.rotate(b["rot"], expand=True, resample=Image.Resampling.BILINEAR)
        tw, th = (
            max(1, int(m.width * scale / cell)),
            max(1, int(m.height * scale / cell)),
        )
        small = (
            m.resize((tw, th)).filter(ImageFilter.MaxFilter(2 * gap + 1))
            if min(tw, th) > 2
            else m.resize((tw, th))
        )
        bird = np.asarray(small) > 40
        # the label's strip under the bird, as wide as the bird or its
        # longest line, as tall as its lines
        lines = ["00"] if mode == "numbered" else (b.get("lines") or [b["name"]])
        rows = label_rows * max(1, len(lines)) if lines else 0
        # the first line is the name in small caps; the rest are smaller italic
        name_w = (
            int(
                max(len(t) * (0.74 if i == 0 else 0.5) for i, t in enumerate(lines))
                * label
                * H
                / cell
            )
            + 3
            if lines
            else 0
        )
        fw = max(tw, name_w)
        fp = np.zeros((th + rows + gap, fw), dtype=bool)
        off = (fw - tw) // 2
        fp[:th, off : off + tw] = bird
        if lines:
            fp[th + 1 : th + rows, (fw - name_w) // 2 : (fw - name_w) // 2 + name_w] = (
                True
            )
        return fp, off

    seed = int(
        hashlib.sha1("|".join(b["name"] for b in birds).encode()).hexdigest()[:8], 16
    )

    def order(ty: Any, tx: Any, fh: int, fw: int) -> tuple[Any, Any]:
        """(candidate order, the distance used to stop looking)."""
        flat = np.zeros(ty.shape, dtype=float)
        if arrange == "rows":
            return np.argsort(ty * gw + tx, axis=None, kind="stable"), flat
        if arrange == "scatter":
            # a steady shuffle, so the page does not jump between refreshes
            return np.random.default_rng(seed).permutation(ty.size), flat
        if arrange == "heap":
            # the biggest settle at the bottom middle, the rest pile on top
            d = ((tx + fw / 2 - cx) / (gw * 0.6)) ** 2 + (
                (gh * 0.93 - (ty + fh)) / gh
            ) ** 2
            return np.argsort(d, axis=None), flat
        d = ((tx + fw / 2 - cx) / rx) ** 2 + ((ty + fh / 2 - cy) / ry) ** 2
        if arrange == "ring":
            # nearest the ring's line (65% of the way out), not the middle
            return np.argsort(np.abs(np.sqrt(d) - 0.65), axis=None), d
        return np.argsort(d, axis=None), d

    def pull(x: int, y: int, fh: int, fw: int) -> tuple[int, int]:
        """Which way this bird wants to move, as a unit step, or (0, 0)."""
        bx, by = x + fw / 2, y + fh / 2
        if arrange == "rows":
            return (-1 if bx > gw * 0.14 else 0, -1 if by > gh * (top + 0.05) else 0)
        if arrange == "heap":
            return (int(np.sign(cx - bx)) if abs(cx - bx) > 2 else 0, 1)
        if arrange == "ring":
            # toward the ring's line, not the middle
            r = (((bx - cx) / rx) ** 2 + ((by - cy) / ry) ** 2) ** 0.5
            if abs(r - 0.65) < 0.03:
                return (0, 0)
            d = -1 if r > 0.65 else 1
            return (int(np.sign(bx - cx)) * d, int(np.sign(by - cy)) * d)
        return (
            int(np.sign(cx - bx)) if abs(cx - bx) > 2 else 0,
            int(np.sign(cy - by)) if abs(cy - by) > 2 else 0,
        )

    def compact(occ: Any, placed: list[Any]) -> None:
        """Nudge every bird one cell at a time toward where it is pulled,
        while the move keeps it clear of the others and inside the page,
        until nothing moves: the loose first packing becomes a tight group,
        like birds cut from plates and pasted close together."""
        for _ in range(600):
            moved = False
            for i in range(len(placed) - 1, -1, -1):
                y, x, fp, off, scale = placed[i]
                fh, fw = fp.shape
                dx, dy = pull(x, y, fh, fw)
                if not dx and not dy:
                    continue
                occ[y : y + fh, x : x + fw] &= ~fp
                for sx, sy in ((dx, dy), (dx, 0), (0, dy)):
                    if not sx and not sy:
                        continue
                    nx, ny = x + sx, y + sy
                    if nx < 0 or ny < 0 or nx + fw > gw or ny + fh > gh:
                        continue
                    if (occ[ny : ny + fh, nx : nx + fw] & fp).any():
                        continue
                    if not inside[ny : ny + fh, nx : nx + fw][fp].all():
                        continue
                    x, y, moved = nx, ny, True
                    break
                occ[y : y + fh, x : x + fw] |= fp
                placed[i] = (y, x, fp, off, scale)
            if not moved:
                break

    def attempt(unit: float) -> list[tuple[int, int, float]] | None:
        occ = np.zeros((gh, gw), dtype=bool)
        placed: list[Any] = []
        for b in birds:
            scale = unit * b["factor"]
            fp, off = footprint(b, scale)
            fh, fw = fp.shape
            if fh >= gh or fw >= gw:
                return None
            ty, tx = np.mgrid[0 : gh - fh : 2, 0 : gw - fw : 2]
            ranked, d = order(ty, tx, fh, fw)
            spot = None
            for k in ranked:
                if arrange in ("oval", "circle") and d.flat[k] > 2.2:
                    break
                y, x = int(ty.flat[k]), int(tx.flat[k])
                if (occ[y : y + fh, x : x + fw] & fp).any():
                    continue
                if not inside[y : y + fh, x : x + fw][fp].all():
                    continue
                spot = (y, x)
                break
            if spot is None:
                return None
            y, x = spot
            occ[y : y + fh, x : x + fw] |= fp
            placed.append((y, x, fp, off, scale))
            # close up after each bird, so the next finds room nearer the middle
            if arrange != "scatter":
                compact(occ, placed)
        return [((x + off) * cell, y * cell, scale) for y, x, _fp, off, scale in placed]

    area = sum((b["mask"].width * b["mask"].height) * b["factor"] ** 2 for b in birds)
    room = float(inside.sum()) * cell * cell
    unit = ((0.5 * room) / max(1, area)) ** 0.5
    best = None
    for _ in range(12):  # shrink until everything fits
        best = attempt(unit)
        if best:
            break
        unit *= 0.88
    if not best:
        return [(0, 0, 0.0)] * len(birds)
    for _ in range(16):  # then grow while it still fits, to fill the page
        bigger = attempt(unit * 1.05)
        if not bigger:
            break
        unit, best = unit * 1.05, bigger
    return best


def _drawer_grid(n: int, W: int, H: int) -> list[tuple[int, int, int, int]]:
    """Compartments of the specimen drawer, as (x, y, w, h) inside its frame."""
    import math

    frame = int(min(W, H) * 0.05)
    iw, ih = W - 2 * frame, H - 2 * frame
    cols = max(1, round(math.sqrt(n * iw / ih)))
    rows = math.ceil(n / cols)
    cols = math.ceil(n / rows)
    cw, ch = iw / cols, ih / rows
    return [
        (int(frame + c * cw), int(frame + r * ch), int(cw), int(ch))
        for r in range(rows)
        for c in range(cols)
    ][:n]


def _build_sheet(
    rows: list[dict[str, Any]],
    keys: list[str],
    ratio: float,
    size: int,
    label: float,
    folder: Path,
    tag: str,
    mode: str = "sheet",
    arrange: str = "auto",
) -> None:
    """Make the cluster page under ``tag`` in the look ``mode`` (slow: run it
    in a thread). Paper looks (sheet, numbered, wreath) multiply the cut-outs
    onto one sheet so they read as printed on it; the notebook tapes them
    onto ruled paper at a slight tilt; the drawer sets each in a compartment
    on felt."""
    sheet_path, info_path = folder / f"sheet_{tag}.jpg", folder / f"sheet_{tag}.json"
    try:
        from PIL import Image, ImageChops, ImageDraw, ImageFilter
    except ImportError:  # pragma: no cover
        return
    birds = []
    for r, k in zip(rows, keys, strict=True):
        with contextlib.suppress(OSError):
            cut_png = None
            with contextlib.suppress(OSError):
                cut_png = (folder / f"{k}.cut.png").read_bytes()
            got = _cutout_masked((folder / f"{k}.jpg").read_bytes(), cut_png)
            if got:
                cut, mask, paper = got
                birds.append(
                    {
                        "name": r["name"],
                        "sci": r["sci"],
                        "cut": cut,
                        "mask": mask,
                        "paper": paper,
                        "factor": float(r.get("factor") or 1.0),
                        "rot": _rot_of(r["name"], mode),
                        "lines": r.get("label_room"),
                    }
                )
    if not birds:
        return
    W, H = (size, int(size / ratio)) if ratio >= 1 else (int(size * ratio), size)
    # biggest first: in the middle of a plate, on the ring of a wreath
    birds.sort(key=lambda b: -(b["factor"] ** 2) * b["mask"].width * b["mask"].height)
    avg = tuple(sum(b["paper"][i] for b in birds) // len(birds) for i in range(3))

    if mode == "drawer":
        sheet = Image.new("RGB", (W, H), (58, 44, 32))  # the drawer's wood
        draw = ImageDraw.Draw(sheet)
        cells = _drawer_grid(len(birds), W, H)
        line = max(2, int(min(W, H) * 0.006))
        for x, y, w, h in cells:
            draw.rectangle(
                (x, y, x + w, y + h),
                fill=(232, 224, 205),
                outline=(58, 44, 32),
                width=line,
            )
        # one scale for all, so they stay true to size against each other
        n_lines = max((len(b.get("lines") or [1]) for b in birds), default=1)
        tag_h = label * (1.4 * n_lines + 1.2) * H
        unit = min(
            min((w * 0.84) / b["cut"].width, (h - tag_h) * 0.84 / b["cut"].height)
            / b["factor"]
            for b, (_x, _y, w, h) in zip(birds, cells, strict=False)
        )
        spots = []
        for b, (x, y, w, h) in zip(birds, cells, strict=False):
            s = unit * b["factor"]
            bw, bh = b["cut"].width * s, b["cut"].height * s
            spots.append(
                (int(x + (w - bw) / 2), int(y + (h - tag_h - bh) / 2 + line), s)
            )
        anchors = [
            ((x + w / 2) / W, (y + h - tag_h * 0.95) / H) for x, y, w, h in cells
        ]
    else:
        spots = _pack_sheet(birds, W, H, label, mode, arrange)
        if spots is None:
            return
        sheet = Image.new("RGB", (W, H), avg if mode != "notebook" else (246, 239, 219))
        if mode == "notebook":
            draw = ImageDraw.Draw(sheet)
            step = max(8, int(H * 0.042))
            for y in range(int(H * 0.09), H, step):
                draw.line((0, y, W, y), fill=(201, 214, 227), width=max(1, H // 900))
            draw.line(
                (int(W * 0.1), 0, int(W * 0.1), H),
                fill=(214, 120, 110),
                width=max(1, H // 700),
            )
        anchors = []

    shown = []
    for i, (b, (x, y, scale)) in enumerate(zip(birds, spots, strict=True)):
        if scale <= 0:
            continue
        cut, mask = b["cut"], b["mask"]
        size_ = (max(1, int(cut.width * scale)), max(1, int(cut.height * scale)))
        fit = cut.resize(size_, Image.Resampling.LANCZOS)
        if mode in ("notebook", "drawer"):
            soft = mask.resize(size_).filter(ImageFilter.GaussianBlur(1.2))
            if b["rot"]:
                fit = fit.rotate(
                    b["rot"],
                    expand=True,
                    fillcolor=(255, 255, 255),
                    resample=Image.Resampling.BICUBIC,
                )
                soft = soft.rotate(
                    b["rot"], expand=True, resample=Image.Resampling.BILINEAR
                )
            sheet.paste(fit, (x, y), soft)
            if mode == "notebook":
                # two strips of tape over the top corners
                tape = Image.new(
                    "RGBA",
                    (max(8, fit.width // 4), max(6, H // 45)),
                    (226, 214, 170, 150),
                )
                for tx in (x - tape.width // 3, x + fit.width - tape.width * 2 // 3):
                    t = tape.rotate(-25 if tx < x else 25, expand=True)
                    sheet.paste(t, (tx, y - t.height // 3), t)
        else:
            region = sheet.crop((x, y, x + fit.width, y + fit.height))
            sheet.paste(ImageChops.multiply(region, fit), (x, y))
        fw, fh = fit.size
        ax, ay = anchors[i] if anchors else ((x + fw / 2) / W, (y + fh) / H)
        shown.append(
            {
                "name": b["name"],
                "sci": b["sci"],
                "box": [
                    round(x / W, 4),
                    round(y / H, 4),
                    round(fw / W, 4),
                    round(fh / H, 4),
                ],
                "at": [round(ax, 4), round(ay, 4)],
                "rot": b["rot"],
            }
        )
    out = io.BytesIO()
    sheet.save(out, "JPEG", quality=88)
    _write(sheet_path, out.getvalue())
    info = {
        "birds": shown,
        "w": W,
        "h": H,
        "ratio": round(ratio, 3),
        "label": label,
        "mode": mode,
        "arrange": arrange,
    }
    _write(info_path, json.dumps(info).encode())
    with contextlib.suppress(OSError):
        for old in folder.glob("sheet_*"):
            if (
                not old.name.startswith(f"sheet_{tag}")
                and time.time() - old.stat().st_mtime > 86400
            ):
                old.unlink()


def _sheet(
    rows: list[dict[str, Any]],
    keys: list[str],
    ratio: float,
    size: int,
    data_dir: Path,
    levels: str,
    label: float = SHEET_LABEL,
    mode: str = "sheet",
    arrange: str = "auto",
) -> dict[str, Any] | None:
    """Today's painted birds as a specimen sheet: each painting cut out and
    printed on one page in an oval cluster, true to size against each
    other, named underneath. {src, birds: [{name, box, label}], paper}.
    Cached on the paintings it is made of."""
    folder = _gen_dir(data_dir)
    stamps = []
    for k in keys:
        with contextlib.suppress(OSError):
            stamps.append(f"{k}:{int((folder / f'{k}.jpg').stat().st_mtime)}")
    if len(stamps) != len(keys) or not keys:
        return None
    names = "|".join(
        r["name"]
        + "/"
        + "/".join(r.get("label_room") or [])
        + f"/{r.get('factor') or 0:.2f}"
        for r in rows
    )
    tag = hashlib.sha1(
        f"{'|'.join(stamps)}|{ratio:.3f}|{size}|{names}|{label:.4f}|{mode}|{arrange}|{SHEET_LAYOUT}".encode()
    ).hexdigest()[:16]
    sheet_path, info_path = folder / f"sheet_{tag}.jpg", folder / f"sheet_{tag}.json"
    info = _read_json(info_path, None)
    if not (sheet_path.exists() and isinstance(info, dict)):
        # cutting out and packing takes a few seconds, longer than Tesserae
        # gives a fetch on a small computer, so it is done in the background
        # and the newest earlier sheet of the same shape shows meanwhile
        job = f"sheet_{tag}"
        with _PAINTING_LOCK:
            fresh = job not in _PAINTING
            _PAINTING.add(job)
        if fresh:

            def run() -> None:
                try:
                    _build_sheet(
                        rows, keys, ratio, size, label, folder, tag, mode, arrange
                    )
                finally:
                    with _PAINTING_LOCK:
                        _PAINTING.discard(job)

            threading.Thread(target=run, name=f"inat-{job}", daemon=True).start()
        older = []
        with contextlib.suppress(OSError):
            for f in folder.glob("sheet_*.json"):
                old = _read_json(f, None)
                if (
                    isinstance(old, dict)
                    and abs(float(old.get("ratio") or 0) - round(ratio, 3)) < 0.01
                    and abs(float(old.get("label") or 0) - label) < 1e-4
                    and old.get("mode", "sheet") == mode
                    and old.get("arrange", "auto") == arrange
                    and f.with_suffix(".jpg").exists()
                ):
                    older.append((f.stat().st_mtime, f, old))
        if not older:
            return None
        _mt, f, info = max(older, key=lambda o: o[0])
        sheet_path, tag = f.with_suffix(".jpg"), f.stem.removeprefix("sheet_")
    try:
        raw = sheet_path.read_bytes()
    except OSError:
        return None
    src = _render(raw, size, None, folder / f"sheet_{tag}_{size}.jpg", levels)
    if not src:
        return None
    return {
        "src": src,
        "birds": info["birds"],
        "ratio": info["w"] / info["h"],
        "paper": _paper(src, folder / f"sheet_{tag}.paper", levels),
        "label": label,
        "mode": info.get("mode", "sheet"),
    }


# ----------------------------------------------------------------------
# The sky: sunrise, sunset and the moon, for night mode
# ----------------------------------------------------------------------

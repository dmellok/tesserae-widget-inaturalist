/* inaturalist: what people have found near a place lately on iNaturalist, or
   one person's own observations, as printed pages.

   Six layouts off one payload, all printed matter: cream paper, engraved
   type, hairline rules. The server (server.py) fetches iNaturalist, resizes
   the pictures, paints with fal.ai when asked and builds the cluster page;
   everything arrives inlined, because the render sandbox has no network. */
/* ---------------- shared: helpers ---------------- */

const esc = (s) =>
  String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

/* a leaf, for a species with no picture */
const LEAF = '<svg class="ico" viewBox="0 0 256 256" aria-hidden="true"><path d="M223.45,40.07a8,8,0,0,0-7.52-7.52C139.8,28.08,78.82,51,52.82,94c-18.73,31-18.77,66.93-.13,101.3l-29,29a12,12,0,0,0,17,17l29-29C87,230.8,104.4,236,121.5,236c20,0,39.33-7.1,56.5-21.5C221,188.4,228,122.9,223.45,40.07ZM163.72,197.5c-27.73,23.26-62.88,21-92.2.21L163.53,105.7a12,12,0,0,0-17-17L54.5,180.73c-20.77-29.3-23-64.4.28-92.2C76.8,61.9,122.1,51.9,195.7,54.6,198.4,128.2,188.4,173.5,163.72,197.5Z"/></svg>';

function ago(days) {
  if (days == null) return '';
  if (days < 1) return 'to-day';
  if (days === 1) return 'yesterday';
  if (days < 14) return `${days} days ago`;
  if (days < 60) return `${Math.round(days / 7)} weeks ago`;
  return `${Math.round(days / 30)} months ago`;
}

function pic(p, cls) {
  if (!p || !p.src) return `<div class="${cls} nopic">${LEAF}</div>`;
  /* a painting (on plain paper) is never cropped: it is fitted whole and the
     frame filled with its paper; a photo has no paper to extend and fills the frame */
  if (p.paper) return `<div class="${cls} fit" style="background:${esc(p.paper)}"><img src="${p.src}" alt=""></div>`;
  const pos = p.focus ? `object-position:${(p.focus[0] * 100).toFixed(1)}% ${(p.focus[1] * 100).toFixed(1)}%` : '';
  return `<div class="${cls}"><img src="${p.src}" alt="" style="${pos}"></div>`;
}

/* A display size that never breaks a word: the smaller of the style's own
   size and what fits the longest word (or the whole name) in the width. */
function fit(name, maxCqmin, widthCqw, em, oneLine) {
  const words = String(name || '').split(/\s+/);
  const chars = oneLine ? String(name || '').length : Math.max(...words.map((w) => w.length), 1);
  return `font-size:min(${maxCqmin}cqmin, ${(widthCqw / (chars * em)).toFixed(2)}cqw)`;
}

/* the printed header every everyday page shares: the place and the period in small caps over a double rule */
function head(d) {
  return `<div class="ph"><span class="ph-k">${esc(d.label)} · ${esc(d.area)}</span><b class="ph-d">${esc(d.date)}</b></div><div class="ph-rule"></div>`;
}

function foot(d) {
  /* show: status and credits; credits: the attributions alone; hide: nothing */
  const mode = d.footer === false ? 'hide' : d.footer || 'show';
  if (mode === 'hide') return '';
  const bits = [...(mode === 'credits' ? [] : d.notices || []), ...(d.credits || [])];
  return bits.length ? `<div class="credit">${bits.map(esc).join(' · ')}</div>` : '';
}

const whoLine = (s) => [s.who ? `by ${s.who}` : '', s.place || ''].filter(Boolean).join(' · ');

/* ---------------- layouts ---------------- */

/* The newest find's picture to the edge of the page, its name set over the paper. */
function hero(d, L, big) {
  const p = L.pic;
  const kick = `${esc(L.kind || 'Found')} · ${esc(L.last ? `seen ${L.last}` : 'seen lately')}`;
  const sub = [d.show_sci !== false && L.sci ? esc(L.sci) : '', esc(whoLine(L)), L.count > 1 ? `${L.count} observations ${esc(d.period)}` : ''].filter(Boolean).join(' · ');
  let picture;
  if (!p || !p.src) picture = `<div class="ev-nopic">${LEAF}</div>`;
  else {
    const plate = !!p.paper;
    const pos = p.focus ? `object-position:${(p.focus[0] * 100).toFixed(1)}% ${(p.focus[1] * 100).toFixed(1)}%` : '';
    picture = `<div class="${plate ? 'ev-plate' : 'ev-photo'}"><img src="${p.src}" alt="" style="${pos}"></div>`;
  }
  return `<div class="ev-hero${big ? ' big' : ''}">${picture}<div class="ev-fade"></div>
      <div class="ev-cap"><div class="ev-kick">${kick}</div><div class="ev-name" style="${fit(L.name, big ? 8.4 : 6.6, big ? 46 : 32, 0.56)}">${esc(L.name)}.</div><div class="ev-sub">${sub}</div></div>
    </div>`;
}

function list(d, showSci) {
  const L = d.latest;
  const rows = d.finds.filter((o) => o !== L).map((o) => `
    <div class="ev-row">
      <div class="ev-rn"><div class="ev-rname">${esc(o.name)}</div><div class="ev-rsub">${esc(o.kind)}${showSci && o.sci ? ` · ${esc(o.sci)}` : ''}${o.who ? ` · ${esc(o.who)}` : ''}</div></div>
      <div class="ev-rwhen">${esc(ago(o.ago_days))}</div>
    </div>`).join('');
  return `
    <div class="ev">
      ${hero(d, L, false)}
      <div class="ev-side">${head(d)}<div class="ev-rows fitrows">${rows}</div>
        <div class="ev-foot"><span class="ev-tot">${d.species_total} species · ${d.obs_total} observations ${esc(d.period)}</span></div>
      </div>
    </div>${foot(d)}`;
}

function latest(d, showSci) {
  const L = d.latest;
  const others = d.species.filter((s) => s.sci !== L.sci).sort((a, b) => b.count - a.count);
  const rows = others.map((s) => `<div class="ev-row small"><div class="ev-rn"><div class="ev-rname">${esc(s.name)}</div>${showSci && s.sci ? `<div class="ev-rsub">${esc(s.sci)}</div>` : ''}</div><div class="ev-rct">${s.count}</div></div>`).join('');
  return `
    <div class="ev ev-latest">
      ${hero(d, L, true)}
      <div class="ev-side">${head(d)}
        <div class="ev-stats"><div><b>${d.species_total}</b><span>species</span></div><div><b>${d.obs_total}</b><span>observations</span></div><div><b>${d.groups.length}</b><span>kinds</span></div></div>
        <div class="ev-also">Also found ${esc(d.period)}</div>
        <div class="ev-rows fitrows">${rows}</div>
      </div>
    </div>${foot(d)}`;
}

function gallery(d, showSci) {
  const tiles = d.species.slice(0, 12).map((s) => `
    <div class="gc">
      ${pic(s.pic, 'gc-p')}
      <div class="gc-n">${esc(s.name)}</div>
      ${showSci && s.sci ? `<div class="gc-s">${esc(s.sci)}</div>` : `<div class="gc-s">${esc(s.kind)}</div>`}
      <div class="gc-c">${s.count}<span> observation${s.count === 1 ? '' : 's'}${s.last ? ` · ${esc(s.last)}` : ''}</span></div>
    </div>`).join('');
  return `${head(d)}<div class="grid n${Math.min(d.species.length, 12)}">${tiles}</div>
    <div class="ev-foot bare"><span class="ev-tot">${d.species_total} species · ${d.obs_total} observations ${esc(d.period)}</span></div>${foot(d)}`;
}

/* The tally: a ledger of the kinds found, with how many species and observations in each. */
function tally(d, showSci) {
  const mx = Math.max(1, ...d.groups.map((g) => g.obs));
  const rows = d.groups.map((g) => `
    <div class="ty-row">
      <div class="ty-k">${esc(g.label)}</div>
      <div class="ty-bar"><i style="width:${Math.max(2, (g.obs / mx) * 100).toFixed(1)}%"></i></div>
      <div class="ty-n"><b>${g.species}</b> sp.</div>
      <div class="ty-n ty-obs"><b>${g.obs}</b> obs.</div>
    </div>`).join('');
  const top = d.species.slice(0, Number(d.count) || 8).map((s, i) => `<div class="ev-row small"><em>${i + 1}.</em><div class="ev-rn"><div class="ev-rname">${esc(s.name)}</div><div class="ev-rsub">${esc(s.kind)}${showSci && s.sci ? ` · ${esc(s.sci)}` : ''}</div></div><div class="ev-rct">${s.count}</div></div>`).join('');
  return `${head(d)}
    <div class="ty">
      <div class="ty-groups"><div class="ev-also">By kind</div><div class="ty-rows">${rows}</div></div>
      <div class="ty-top"><div class="ev-also">Most observed</div><div class="ev-rows fitrows">${top}</div></div>
    </div>
    <div class="ev-foot bare"><span class="ev-tot">${d.species_total} species · ${d.obs_total} observations ${esc(d.period)}</span></div>${foot(d)}`;
}

function bleed(d, showSci) {
  const L = d.latest;
  const sci = showSci && L.sci ? esc(L.sci) : '';
  const cap = `<div class="bd">
      <div class="bd-t"><div class="kick">${esc(L.kind || 'Found')} · ${esc(L.last ? `seen ${L.last}` : 'seen lately')}</div><div class="bd-n" style="${fit(L.name, 7.4, 62, 0.56, true)}">${esc(L.name)}</div>${sci ? `<div class="bd-s">${sci}</div>` : ''}</div>
      <div class="bd-c a"><b>${L.count}</b><span>observation${L.count === 1 ? '' : 's'}</span></div>
      <div class="bd-c k"><b>${d.species_total}</b><span>species</span></div></div>`;
  const credit = L.pic && L.pic.credit ? `<div class="bcredit">${esc(L.pic.credit)}${L.who ? ` · by ${esc(L.who)}` : ''}</div>` : '';
  const paper = L.pic && L.pic.paper ? ` style="background:${esc(L.pic.paper)}"` : '';
  return `<div class="bl${L.pic && L.pic.paper ? ' on-paper' : ''}"${paper}>${pic(L.pic, 'bp')}${cap}${credit}</div>`;
}

/* The cluster as one printed page, in one of five looks (C.mode); the
   builder says where each label goes (b.at, as fractions of the page). */
function sheet(d, C) {
  const mode = C.mode || 'sheet';
  const items = C.birds || [];
  const lines = C.lines || ['name'];
  const text = d.cluster_text || 'key';
  const keyed = mode === 'numbered' && text === 'key';
  const labelOf = (b) => lines.map((l) => {
    if (l === 'name') return `<span class="sh-ln">${esc(b.name)}</span>`;
    if (l === 'sci') return b.sci ? `<span class="sh-ln sh-sci">${esc(b.sci)}</span>` : '';
    if (l === 'kind') return b.kind ? `<span class="sh-ln sh-sm">${esc(b.kind)}</span>` : '';
    if (l === 'count') return `<span class="sh-ln sh-sm">${b.count} observation${b.count === 1 ? '' : 's'}</span>`;
    if (l === 'when') return b.last ? `<span class="sh-ln sh-sm">seen ${esc(b.last)}</span>` : '';
    if (l === 'who') return b.who ? `<span class="sh-ln sh-sm">by ${esc(b.who)}</span>` : '';
    return '';
  }).join('');
  const labels = text === 'none' ? '' : items.map((b, i) => {
    const [x, y] = b.at || [b.box[0] + b.box[2] / 2, b.box[1] + b.box[3]];
    const t = keyed ? String(i + 1) : labelOf(b);
    if (!t) return '';
    const rot = mode === 'notebook' && b.rot ? `;transform:translate(-50%, 15%) rotate(${-b.rot}deg)` : '';
    return `<span class="sh-l" style="left:${(x * 100).toFixed(2)}%;top:${(y * 100).toFixed(2)}%${rot}">${t}</span>`;
  }).join('');
  const waiting = (C.waiting || []).length;
  const verb = C.waiting_verb || 'paint';
  const line = `${esc(d.label)} · ${esc(d.area)} · ${esc(d.period)} · ${d.species_total} species${waiting ? ` · ${waiting} still to ${verb}` : ''}`;
  let extra = `<div class="sh-c">${line}</div>`;
  if (mode === 'wreath') {
    extra = `<div class="sh-w"><div class="sh-wk">${esc(d.label)}</div><div class="sh-wt">Found ${esc(d.period)}</div>
      <div class="sh-wd">${esc(d.area)} · ${d.species_total} species${waiting ? ` · ${waiting} waiting` : ''}</div></div>`;
  } else if (mode === 'notebook') {
    extra = `<div class="sh-nb">${esc(d.label)}, ${esc(d.date)} &mdash; ${d.species_total} species ${esc(d.period)}${waiting ? `, ${waiting} waiting` : ''}</div>`;
  } else if (mode === 'drawer') {
    extra = `<div class="sh-plaque">${esc(d.label)} · ${esc(d.area)}</div>`;
  }
  const page = `<div class="cl-sheet sh-s sh-${keyed ? 'numbered' : mode}" style="--r:${C.ratio};--lf:${(C.label || 0.017) * (d.text_scale || 1)}"><img src="${C.pic.src}" alt="">${labels}${text === 'key' && mode !== 'numbered' ? extra : ''}</div>`;
  if (keyed) {
    const rows = items.map((b, i) => `<li><em>${i + 1}.</em><span>${esc(b.name)}</span><b>${b.count}</b></li>`).join('');
    return `
    <div class="cl cl-numbered" style="background:${esc(C.pic.paper)}">
      <div class="cl-pic">${page}</div>
      <div class="cl-key">
        <div class="cl-k">${esc(d.label)} · ${esc(d.date)}</div>
        <div class="cl-t">Found ${esc(d.period)}</div>
        <ol class="fitrows">${rows}</ol>
        <div class="cl-f">${d.species_total} species · ${d.obs_total} observations${waiting ? ` · ${waiting} waiting` : ''}</div>
      </div>
    </div>${foot(d)}`;
  }
  return `<div class="sh" style="background:${mode === 'drawer' ? '#3a2c20' : esc(C.pic.paper)}">${page}</div>${foot(d)}`;
}

function cluster(d, showSci) {
  const C = d.cluster || { birds: [] };
  if (C.sheet && C.pic) return sheet(d, C);
  const items = C.birds || [];
  const text = d.cluster_text || 'key';
  const numbered = text === 'key';
  const caption = (b, i) => (text === 'none' ? '' : `<figcaption>${numbered ? i + 1 : esc(b.name)}</figcaption>`);
  const art = `<div class="cl-mont n${Math.min(items.length, 6)}">${items.map((b, i) => `
        <figure>${pic(b.pic, 'cl-tile')}${caption(b, i)}</figure>`).join('')}</div>`;
  const rows = items.map((b, i) => `
      <li><em>${i + 1}.</em><span>${esc(b.name)}${showSci && b.sci ? `<i>${esc(b.sci)}</i>` : ''}</span><b>${b.count}</b></li>`).join('');
  const waiting = (C.waiting || []).length ? `<div class="cl-f">Still to paint: ${C.waiting.map(esc).join(', ')}</div>` : '';
  const key = text === 'key' ? `
      <div class="cl-key">
        <div class="cl-k">${esc(d.label)} · ${esc(d.date)}</div>
        <div class="cl-t">Found ${esc(d.period)}</div>
        <ol class="fitrows">${rows}</ol>
        <div class="cl-f">${d.species_total} species · ${d.obs_total} observations</div>${waiting}
      </div>` : '';
  return `
    <div class="cl montage${key ? '' : ' nokey'}">
      <div class="cl-pic">${art}</div>${key}
    </div>${foot(d)}`;
}

function body(d, shadow) {
  const layout = d.layout || 'cluster';
  const showSci = d.show_sci !== false;
  if (d.error) return `<div class="msg"><div class="msg-t">Nature nearby</div><div class="msg-b">${esc(d.error)}</div></div>`;
  if (d.empty || !d.latest) {
    return `${head(d)}<div class="msg"><div class="msg-b">Nothing found ${esc(d.period)}.</div>
            <div class="msg-h">${d.area ? 'Try a wider circle, a longer period or more kinds.' : 'Set a place, or an iNaturalist username, in this cell.'}</div></div>`;
  }
  if (layout === 'latest') return latest(d, showSci);
  if (layout === 'gallery') return gallery(d, showSci);
  if (layout === 'bleed') return bleed(d, showSci);
  if (layout === 'tally') return tally(d, showSci);
  if (layout === 'cluster') return cluster(d, showSci);
  return list(d, showSci);
}

function page(d, size, inner) {
  const layout = d.layout || 'cluster';
  return `
    <div class="bb lay-${esc(layout)} size-${esc(size)} lv-${esc(d.panel || 'colour')} font-${esc(d.font || 'classic')}" style="--ts:${Number(d.text_scale) > 0 ? Number(d.text_scale) : 1}">${inner}</div>
    <style>
      :host { display: block; height: 100%; }
      .bb {
        container-type: size; height: 100%; width: 100%; overflow: hidden; position: relative;
        display: flex; flex-direction: column;
        background: #f3ead6; color: #2a2116;
        font-family: var(--bb-serif); font-variant-numeric: tabular-nums;
        --ink: #2a2116; --paper: #f3ead6; --alert: #b4351f; --muted: #d9cba5;
        --rule: clamp(2px, 0.5cqmin, 5px);
        /* the type on the printed looks: a roman and a small-caps face */
        --bb-serif: 'IM Fell English', Georgia, serif; --bb-sc: 'IM Fell English SC', Georgia, serif;
      }
      .font-playfair { --bb-serif: 'Playfair Display', Georgia, serif; --bb-sc: 'Playfair Display', Georgia, serif; }
      .font-typewriter { --bb-serif: 'Special Elite', 'Courier New', monospace; --bb-sc: 'Special Elite', 'Courier New', monospace; }
      .font-hand { --bb-serif: Caveat, cursive; --bb-sc: Caveat, cursive; }
      .font-modern { --bb-serif: 'Archivo Narrow', Arial, sans-serif; --bb-sc: 'Archivo Black', Arial, sans-serif; }
      .font-theme { --bb-serif: var(--font-family, inherit); --bb-sc: var(--font-family, inherit); }
      
      .ico { width: 100%; height: 100%; fill: currentColor; display: block; }
      img { display: block; width: 100%; height: 100%; object-fit: cover; }
      .nopic { background: var(--muted); color: #8a7a55; padding: 18%; }
      .bb .fit img { object-fit: contain; mix-blend-mode: multiply; }
      .kick { font-family: var(--bb-sc); font-size: 2.6cqmin; letter-spacing: .14em; color: #6b5a3a; }
      .credit, .bcredit { font-size: 1.9cqmin; opacity: .6; padding: 0.8cqmin 2.4cqmin; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-style: italic; }

      /* the printed header every everyday page shares */
      .ph { display: flex; justify-content: space-between; align-items: baseline; gap: 2cqmin; padding: 0 0 .6cqmin; }
      .ph-k { font-family: var(--bb-sc); font-size: 2.5cqmin; letter-spacing: .13em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .ph-d { font-weight: 400; font-size: 4.2cqmin; letter-spacing: .04em; white-space: nowrap; }
      .ph-rule { border-top: 1px solid var(--ink); position: relative; margin-bottom: .8cqmin; flex: 0 0 auto; }
      .ph-rule::after { content: ""; position: absolute; left: 0; right: 0; top: 2px; border-top: 1px solid var(--ink); }
      .lay-gallery .ph, .lay-tally .ph { padding: 3cqmin 3.4cqmin .6cqmin; }
      .lay-gallery .ph-rule, .lay-tally .ph-rule { margin: 0 3.4cqmin .8cqmin; }

      /* list and latest: the newest find to the edge, the rest beside it */
      .ev { flex: 1; min-height: 0; display: flex; }
      .ev-hero { flex: 0 0 40%; min-width: 0; position: relative; }
      .ev-hero.big { flex-basis: 54%; }
      .ev-plate, .ev-photo, .ev-nopic { position: absolute; inset: 0; overflow: hidden; }
      .ev-plate img, .ev-photo img { width: 100%; height: 100%; object-fit: cover; }
      .ev-plate img { mix-blend-mode: multiply; object-fit: contain; }
      .ev-photo { mask-image: linear-gradient(to right, #000 78%, transparent 100%); -webkit-mask-image: linear-gradient(to right, #000 78%, transparent 100%); }
      .ev-nopic { padding: 26% 26% 40%; color: #cdbf9d; }
      .ev-cap { position: absolute; left: 3.4cqmin; right: 2cqmin; bottom: 3cqmin; z-index: 2; }
      .ev-fade { position: absolute; left: 0; right: 0; bottom: 0; height: 46%; background: linear-gradient(to top, var(--paper) 42%, rgba(243, 234, 214, 0)); z-index: 1; }
      .ev-hero.big .ev-fade { height: 46%; background: linear-gradient(to top, var(--paper) 50%, rgba(243, 234, 214, 0)); }
      .ev-kick { font-family: var(--bb-sc); font-size: 2.3cqmin; letter-spacing: .14em; color: #6b5a3a; }
      .ev-name { font-family: var(--bb-sc); font-size: 6.6cqmin; line-height: 1.02; letter-spacing: .02em; margin-top: .4cqmin; }
      .ev-hero.big .ev-name { font-size: 8.4cqmin; }
      .ev-sub { font-style: italic; font-size: 2.9cqmin; margin-top: .6cqmin; }
      .ev-side { flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; padding: 3cqmin 3.4cqmin 2.2cqmin 3cqmin; }
      .ev-rows { flex: 1 1 0; min-height: 0; }
      .ev-row { display: flex; align-items: baseline; gap: 2cqmin; padding: 1.3cqmin 0; border-bottom: 1px solid #c9b98f; }
      .ev-row.small { padding: .9cqmin 0; }
      .ev-row em { font-style: normal; min-width: 3.2cqmin; font-size: 3.2cqmin; }
      .ev-rn { flex: 1; min-width: 0; }
      .ev-rname { font-family: var(--bb-sc); font-size: 3.7cqmin; letter-spacing: .03em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .ev-row.small .ev-rname { font-size: 3.3cqmin; }
      .ev-rsub { font-style: italic; font-size: 2.4cqmin; opacity: .75; margin-top: -.3cqmin; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .ev-rct { font-size: 4.8cqmin; font-variant-numeric: tabular-nums; min-width: 7cqmin; text-align: right; }
      .ev-row.small .ev-rct { font-size: 3.8cqmin; }
      .ev-rwhen { font-style: italic; font-size: 2.6cqmin; white-space: nowrap; opacity: .85; }
      .ev-stats { display: flex; gap: 3cqmin; padding: 1cqmin 0 1.4cqmin; border-bottom: 1px solid #c9b98f; }
      .ev-stats > div { flex: 0 0 auto; }
      .ev-stats b { display: block; font-weight: 400; font-size: 6cqmin; line-height: 1; font-variant-numeric: tabular-nums; }
      .ev-stats span { font-family: var(--bb-sc); font-size: 2cqmin; letter-spacing: .12em; }
      .ev-also { font-family: var(--bb-sc); font-size: 2.3cqmin; letter-spacing: .14em; padding: 1.4cqmin 0 .4cqmin; }
      .ev-foot { flex: 0 0 auto; display: flex; align-items: flex-end; gap: 2.4cqmin; margin-top: 1.4cqmin; }
      .ev-foot.bare { padding: 0 3.4cqmin 1.6cqmin; justify-content: space-between; }
      .ev-tot { font-family: var(--bb-sc); font-size: 2.3cqmin; letter-spacing: .12em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .fitrows { flex: 1 1 0; min-height: 0; display: flex; flex-direction: column; flex-wrap: wrap; overflow: hidden; align-content: flex-start; }
      .fitrows > * { width: 100%; flex: 0 0 auto; box-sizing: border-box; }
      @container (max-aspect-ratio: 1) {
        .ev { flex-direction: column; }
        .ev-hero { flex: 0 0 44%; }
        .ev-hero.big { flex-basis: 52%; }
        .ev-photo { mask-image: linear-gradient(to bottom, #000 76%, transparent 100%); -webkit-mask-image: linear-gradient(to bottom, #000 76%, transparent 100%); }
        .ev-fade { height: 40%; }
        .ev-side { padding-top: 2cqmin; }
        .ev-name { font-size: 7.2cqmin; }
      }
      @container (max-height: 260px) { .ev-rsub { display: none; } }

      /* gallery: specimen cards on mounts */
      .grid { flex: 1; min-height: 0; display: grid; gap: 2cqmin; padding: .6cqmin 3.4cqmin 1cqmin; grid-template-columns: repeat(4, 1fr); grid-auto-rows: 1fr; }
      .grid.n1 { grid-template-columns: 1fr; } .grid.n2 { grid-template-columns: repeat(2, 1fr); }
      .grid.n3 { grid-template-columns: repeat(3, 1fr); } .grid.n4 { grid-template-columns: repeat(4, 1fr); }
      .grid.n5, .grid.n6 { grid-template-columns: repeat(3, 1fr); }
      .grid.n7, .grid.n8 { grid-template-columns: repeat(4, 1fr); }
      .grid.n9, .grid.n10, .grid.n11, .grid.n12 { grid-template-columns: repeat(4, 1fr); }
      @container (max-aspect-ratio: 1) {
        .grid { grid-template-columns: repeat(2, 1fr) !important; }
        .grid.n7, .grid.n8, .grid.n9, .grid.n10, .grid.n11, .grid.n12 { grid-template-columns: repeat(3, 1fr) !important; }
      }
      .gc { background: #fffdf6; box-shadow: 0 0 0 1px #c9b98f; padding: 1.2cqmin 1.2cqmin 1cqmin; display: flex; flex-direction: column; min-height: 0; text-align: center; }
      .gc-p { flex: 1; min-height: 0; overflow: hidden; }
      .gc-p img { object-fit: cover; }
      .gc-p.fit { padding: .6cqmin; }
      .gc-n { font-family: var(--bb-sc); font-size: 2.7cqmin; letter-spacing: .03em; margin-top: .8cqmin; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .gc-s { font-style: italic; font-size: 1.9cqmin; opacity: .75; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .gc-c { font-size: 3.2cqmin; margin-top: .2cqmin; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .gc-c span { font-size: 1.9cqmin; font-style: italic; opacity: .75; }
      @container (max-height: 320px) { .gc-s, .gc-c span { display: none; } }

      /* tally: a ledger of kinds */
      .ty { flex: 1; min-height: 0; display: flex; gap: 4cqmin; padding: .4cqmin 3.4cqmin 0; }
      .ty-groups { flex: 1 1 56%; min-width: 0; display: flex; flex-direction: column; }
      .ty-top { flex: 1 1 44%; min-width: 0; display: flex; flex-direction: column; }
      .ty-rows { flex: 1 1 0; min-height: 0; display: flex; flex-direction: column; justify-content: flex-start; overflow: hidden; }
      .ty-row { display: grid; grid-template-columns: minmax(0, 11em) minmax(0, 1fr) auto auto; align-items: center; gap: 1.6cqmin; padding: .9cqmin 0; border-bottom: 1px solid #c9b98f; font-size: 3cqmin; }
      .ty-k { font-family: var(--bb-sc); letter-spacing: .03em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .ty-bar { height: 2.2cqmin; background: #e9dfc4; }
      .ty-bar i { display: block; height: 100%; background: var(--ink); opacity: .78; }
      .ty-n { font-size: 2.6cqmin; font-style: italic; white-space: nowrap; min-width: 6cqmin; text-align: right; }
      .ty-n b { font-weight: 400; font-style: normal; font-size: 3.2cqmin; }
      @container (max-aspect-ratio: 1) { .ty { flex-direction: column; gap: 1cqmin; } .ty-groups { flex: 0 0 auto; } .ty-rows { flex: 0 0 auto; } .ty-top { flex: 1 1 0; min-height: 0; } }
      @container (max-width: 420px) { .ty-obs { display: none; } }

      /* bleed: the picture is the page, a name band along the foot */
      .bl { flex: 1; min-height: 0; position: relative; overflow: hidden; background: #fff; }
      .bp { position: absolute; inset: 0; }
      .bp.fit { padding: 2cqmin 2cqmin 18cqmin; box-sizing: border-box; }
      .bp img { width: 100%; height: 100%; object-fit: cover; }
      .bp.fit img { object-fit: contain; }
      .bd { position: absolute; left: 0; right: 0; bottom: 0; display: flex; align-items: stretch; background: var(--paper); border-top: var(--rule) solid var(--ink); }
      .bd-t { flex: 1; min-width: 0; padding: 1.6cqmin 2.6cqmin 1.8cqmin; }
      .bd-n { font-family: var(--bb-sc); line-height: 1.02; letter-spacing: .02em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: .3cqmin; }
      .bd-s { font-style: italic; font-size: 2.8cqmin; margin-top: .4cqmin; }
      .bd-c { flex: 0 0 auto; padding: 1.4cqmin 2.6cqmin; border-left: 1px solid var(--ink); display: flex; flex-direction: column; justify-content: center; min-width: 12cqmin; }
      .bd-c b { font-weight: 400; font-size: 6.2cqmin; line-height: 1; }
      .bd-c span { font-family: var(--bb-sc); font-size: 2cqmin; letter-spacing: .12em; }
      .bl .bcredit { position: absolute; left: 0; right: 0; top: 0; text-align: right; color: #fff; text-shadow: 0 0 .6cqmin rgba(0,0,0,.6); opacity: .85; }
      .bl.on-paper .bcredit { color: var(--ink); text-shadow: none; opacity: .6; }

      /* cluster: one picture of the species together with a key beside or below it */
      .cl { flex: 1; min-height: 0; display: flex; gap: 3.4cqmin; padding: 3.4cqmin; background: #f3ead6; }
      .cl-pic { flex: 1 1 0; min-width: 0; min-height: 0; display: flex; container-type: size; }
      .cl-sheet { position: relative; margin: auto; aspect-ratio: var(--r); width: min(100cqw, calc(100cqh * var(--r))); }
      .cl-sheet img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: fill; }
      .cl-mont { flex: 1; min-height: 0; display: grid; gap: 1.6cqmin; grid-template-columns: repeat(2, 1fr); grid-auto-rows: 1fr; }
      .cl-mont.n3, .cl-mont.n5, .cl-mont.n6 { grid-template-columns: repeat(3, 1fr); }
      .cl-mont figure { margin: 0; position: relative; min-height: 0; display: flex; background: #fffdf6; box-shadow: 0 0 0 1px #c9b98f; }
      .cl-tile { flex: 1; min-width: 0; min-height: 0; overflow: hidden; }
      .cl-tile.fit { padding: 1cqmin; }
      .cl-mont figcaption { position: absolute; left: 1cqmin; top: .6cqmin; font-family: var(--bb-serif); font-size: calc(3.4cqmin * var(--ts, 1)); line-height: 1; background: #fffdf6; padding: .2cqmin .8cqmin; }
      .cl-key { flex: 0 0 34%; min-width: 0; min-height: 0; display: flex; flex-direction: column; justify-content: center; font-family: var(--bb-serif); }
      .cl.nokey { padding: 2cqmin; }
      .cl.nokey .cl-mont figcaption { left: 50%; top: auto; bottom: .6cqmin; transform: translateX(-50%); max-width: 92%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .cl-key ol.fitrows { flex: 0 1 auto; max-height: 100%; }
      .cl-k, .cl-t, .cl-f { flex: 0 0 auto; }
      .cl-k { font-family: var(--bb-sc); font-size: calc(2.6cqmin * var(--ts, 1)); letter-spacing: .14em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .cl-t { font-family: var(--bb-sc); font-size: calc(6cqmin * var(--ts, 1)); letter-spacing: .04em; line-height: 1.05; margin-top: 1cqmin; padding-bottom: 1.6cqmin; border-bottom: 1px solid currentColor; box-shadow: 0 2px 0 #f3ead6, 0 3px 0 currentColor; }
      .cl ol { list-style: none; margin: 2cqmin 0 0; padding: 0; }
      .cl li { display: flex; align-items: baseline; gap: 1.4cqmin; padding: .7cqmin 0; border-bottom: 1px solid #b7a67c; font-size: calc(3.4cqmin * var(--ts, 1)); }
      .cl li em { font-style: normal; min-width: 3.4cqmin; }
      .cl li span { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .cl li span i { margin-left: 1.2cqmin; font-size: calc(2.6cqmin * var(--ts, 1)); opacity: .75; }
      .cl li b { font-weight: 400; }
      .cl-f { margin-top: 1.6cqmin; font-style: italic; font-size: calc(2.6cqmin * var(--ts, 1)); opacity: .8; }
      @container (max-aspect-ratio: 1) {
        .cl { flex-direction: column; gap: 2.6cqmin; }
        .cl-key { flex: 0 0 auto; justify-content: flex-start; }
        .cl-t { font-size: calc(7cqmin * var(--ts, 1)); }
        .cl li { font-size: calc(3.8cqmin * var(--ts, 1)); padding: .9cqmin 0; }
        .cl-mont.n3 { grid-template-columns: repeat(2, 1fr); }
        .cl-mont.n3 figure:last-child { grid-column: 1 / -1; }
        .cl-numbered .cl-key { max-height: 38%; flex: 0 0 auto; justify-content: flex-start; }
        .cl-numbered li { font-size: calc(2.8cqmin * var(--ts, 1)); padding: .4cqmin 0; }
        .cl-numbered .cl-pic { overflow: hidden; }
        .cl-numbered .cl-t { padding-bottom: 1cqmin; font-size: calc(5cqmin * var(--ts, 1)); }
        .cl-numbered ol.fitrows { margin-top: 1.2cqmin; }
        .cl-numbered .cl-key ol.fitrows { flex: 1 1 auto; min-height: 0; column-gap: 4cqmin; }
        .cl-numbered .cl-key ol.fitrows > li { width: calc(50% - 2cqmin); margin-right: 4cqmin; }
      }

      /* specimen sheet: the whole cell is the page */
      .sh { flex: 1; min-height: 0; display: flex; container-type: size; }
      .sh-s { container-type: size; color: #2a2116; }
      .sh-ln { display: block; }
      .sh-sci { font-family: var(--bb-serif); font-style: italic; letter-spacing: 0; }
      .sh-sm { font-family: var(--bb-serif); font-style: italic; letter-spacing: 0; font-size: .85em; opacity: .85; }
      .sh-l { position: absolute; transform: translate(-50%, 15%); white-space: nowrap; text-align: center; line-height: 1.15; font-family: var(--bb-sc); font-size: calc(var(--lf) * 100cqh); letter-spacing: .04em; }
      .sh-c { position: absolute; left: 0; right: 0; bottom: 1.6%; text-align: center; font-family: var(--bb-serif); font-style: italic; font-size: calc(var(--lf) * 100cqh); opacity: .8; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .sh-numbered .sh-l { font-family: var(--bb-serif); font-size: calc(var(--lf) * 170cqh); }
      .sh-w { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); width: 34%; text-align: center; font-family: var(--bb-serif); }
      .sh-wk { font-family: var(--bb-sc); font-size: calc(var(--lf) * 110cqh); letter-spacing: .16em; }
      .sh-wt { font-family: var(--bb-sc); font-size: calc(var(--lf) * 300cqh); line-height: 1.05; margin: .4em 0 .2em; }
      .sh-wd { font-style: italic; font-size: calc(var(--lf) * 110cqh); }
      .sh-notebook { color: #23324a; }
      .sh-notebook .sh-l { font-family: Caveat, cursive; font-size: calc(var(--lf) * 150cqh); letter-spacing: 0; }
      .sh-nb { position: absolute; left: 12%; right: 3%; top: 2.2%; font-family: Caveat, cursive; font-size: calc(var(--lf) * 190cqh); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .sh-drawer .sh-l { background: #fbf8ef; color: #1d1a16; padding: .25em .6em .2em; box-shadow: 0 0 0 1px #6d6457; transform: translate(-50%, 0); max-width: 30%; overflow: hidden; text-overflow: ellipsis; }
      .sh-plaque { position: absolute; left: 50%; top: 0; height: 5%; transform: translateX(-50%); display: flex; align-items: center; padding: 0 1.2em; max-width: 70%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        background: #c9a95f; color: #2a2116; box-shadow: inset 0 0 0 1px #8a6f33; font-family: var(--bb-sc); font-size: calc(var(--lf) * 110cqh); letter-spacing: .12em; }

      /* the empty and error pages */
      .msg { flex: 1; display: flex; flex-direction: column; justify-content: center; padding: 4cqmin; text-align: center; }
      .msg-t { font-family: var(--bb-sc); font-size: 5cqmin; letter-spacing: .08em; }
      .msg-b { font-style: italic; font-size: 3.6cqmin; }
      .msg-h { font-size: 2.6cqmin; opacity: .7; margin-top: 1cqmin; }

      /* greyscale and mono panels: white paper, black ink */
      .lv-gray16 .bb, .lv-gray4 .bb, .lv-mono .bb,
      .lv-gray16, .lv-gray4, .lv-mono { background: #fff; color: #000; --paper: #fff; --ink: #000; --muted: #ddd; }
      .lv-gray16 .ev-fade, .lv-gray4 .ev-fade, .lv-mono .ev-fade { background: linear-gradient(to top, #fff 42%, rgba(255, 255, 255, 0)); }
      .lv-gray16 .ev-kick, .lv-gray4 .ev-kick, .lv-mono .ev-kick, .lv-gray16 .kick, .lv-gray4 .kick, .lv-mono .kick { color: #000; }
      .lv-gray16 .gc, .lv-gray4 .gc, .lv-mono .gc, .lv-gray16 .cl-mont figure, .lv-gray4 .cl-mont figure, .lv-mono .cl-mont figure { background: #fff; box-shadow: 0 0 0 1px #000; }
      .lv-gray16 .ev-row, .lv-gray4 .ev-row, .lv-mono .ev-row, .lv-gray16 .ev-stats, .lv-gray4 .ev-stats, .lv-mono .ev-stats, .lv-gray16 .ty-row, .lv-gray4 .ty-row, .lv-mono .ty-row, .lv-gray16 .cl li, .lv-gray4 .cl li, .lv-mono .cl li { border-bottom-color: #000; }
      .lv-gray16 .ty-bar, .lv-gray4 .ty-bar, .lv-mono .ty-bar { background: #eee; }
      .lv-gray16 .cl, .lv-gray4 .cl, .lv-mono .cl, .lv-gray16 .sh, .lv-gray4 .sh, .lv-mono .sh { background: #fff !important; color: #000; }
      .lv-gray16 .cl-t, .lv-gray4 .cl-t, .lv-mono .cl-t { box-shadow: 0 2px 0 #fff, 0 3px 0 #000; }
      .lv-gray16 .sh-plaque, .lv-gray4 .sh-plaque, .lv-mono .sh-plaque { background: #fff; color: #000; box-shadow: inset 0 0 0 1px #000; }
      .lv-gray16 .sh-notebook, .lv-gray4 .sh-notebook, .lv-mono .sh-notebook, .lv-gray16 .sh-s, .lv-gray4 .sh-s, .lv-mono .sh-s { color: #000; }
      .lv-mono .ev-name, .lv-mono .ev-rname, .lv-mono .gc-n, .lv-mono .sh-l, .lv-mono .sh-c, .lv-mono .cl-key, .lv-mono .ty-k { -webkit-text-stroke: .35px currentColor; }
    </style>`;
}

/* Chromium ignores @font-face inside a shadow root, so the faces are
   registered on the host document, once per set. */
function installFonts(shadow, css) {
  if (!css) return;
  const doc = (shadow && shadow.host && shadow.host.ownerDocument) || (typeof document !== 'undefined' ? document : null);
  if (!doc || !doc.head) return;
  let h = 0;
  for (let i = 0; i < css.length; i += 97) h = (h * 31 + css.charCodeAt(i)) >>> 0;
  const id = `inaturalist-fonts-${h.toString(36)}-${css.length}`;
  if (doc.getElementById(id)) return;
  const el = doc.createElement('style');
  el.id = id;
  el.textContent = css;
  doc.head.appendChild(el);
}

export default function render(shadow, ctx) {
  const d = (ctx && ctx.data) || {};
  const size = (ctx && ctx.cell && ctx.cell.size) || 'md';
  installFonts(shadow, d.font_css);
  shadow.innerHTML = `
    <link rel="stylesheet" href="/static/style/spectra-widgets.css">${page(d, size, body(d, shadow))}`;
}

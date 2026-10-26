"""PRF section: herd size requirements — FSA Acres Per Animal Unit, by county.

Built the same way as src/prfpage.py and src/drppage.py — d3 v7, topojson-client and the
us-atlas topology embedded inline, zero network requests — but reading a different source
(FSA's National Crop Table, src/connectors/fsa_nct.py) and a different grain (county x
crop/type, non-irrigated only, no grid level: FSA publishes nothing finer than county).

NON-IRRIGATED ONLY, ON PURPOSE: PRF's own "Grazing" intended use has no irrigated/
non-irrigated split (src/connectors/prf_adm.py maps it straight to Non-Irrigated — only
Haying carries the distinction), so an Irrigated grazing figure was never a PRF-relevant
number and src/connectors/fsa_nct.rollup_grazing_rows() drops it before it ever reaches
fsa_grazing_county. There is accordingly no irrigation control on this page.

WHAT THE NUMBER IS AND ISN'T
-----------------------------
"Acres Per Animal Unit" is FSA's NAP/ARC-PLC stocking-rate figure: the acres of a given
crop/type needed to carry ONE animal unit for the grazing period. It answers "how much of
this insured ground does a given herd size need" (or, inverted, "how big a herd can this
acreage carry") — which is why the section is titled herd size requirements rather than
carrying capacity or stocking rate, even though it is the same number.

It is NOT a generic "PRF ground" figure. Neither RMA nor FSA publishes a carrying capacity
for PRF-insured acreage as such — FSA's table is scoped to the specific crop/type it prices
for grazing (annual forage like wheat or oat pasture, and perennial GRASS types including
NATIVE). GRASS / NATIVE is offered as the default because it is the closest available
analog to native rangeland and the widest-covered combination in the table (2,662 of 2,924
counties with any grazing row, non-irrigated, FY2026) — not because it is officially "the"
PRF figure.

PLANTING PERIOD IS AGGREGATED, NOT THROWN AWAY, BUT NOT HERE EITHER
----------------------------------------------------------------------
FSA's raw table (fsa_grazing_capacity) keeps every "Planting Period" row. In dry-country
counties (verified on Arizona native range) the SAME county/crop/type/irrigation carries
multiple planting-period rows that disagree by two orders of magnitude (1 to 160 acres/AU),
because the figure depends on the state of the forage when grazing begins; humid counties
mostly carry a single period. Collapsing that blindly would silently pick an arbitrary
period, so src/connectors/fsa_nct.py's rollup_grazing_rows() does it explicitly at ingest
time: fsa_grazing_county stores the MAXIMUM across a cell's planting periods (the most
acres required = the conservative figure for a herd grazed there continuously through its
leanest period) plus the true minimum and a period count, so the tooltip can still show the
spread without this module — or the app's shipped database — needing the ~125k-row raw
table (see src/db.py for why that one never ships).
"""
from __future__ import annotations

import json
import sqlite3

from .prfpage import _fips5

DEFAULT_CROP_KEY = "GRASS|NATIVE"


def _crop_key(crop_name: str, crop_type_name: str) -> str:
    crop_type_name = (crop_type_name or "").strip()
    return f"{crop_name}|{crop_type_name}" if crop_type_name else crop_name


def _crop_label(crop_key: str) -> str:
    if "|" in crop_key:
        name, typ = crop_key.split("|", 1)
        return f"{name.title()} — {typ.title()}"
    return crop_key.title()


def build_herd_size_payload(conn: sqlite3.Connection) -> dict:
    """FSA Acres Per Animal Unit, one figure per county x crop/type (non-irrigated grazing
    only — see module docstring). Pure read; testable.

    Reads the pre-aggregated fsa_grazing_county + fsa_crop_type tables (the ones that ship
    — see src/db.py and src/connectors/fsa_nct.rollup_grazing_rows for how the aggregation
    over FSA planting periods was done, at ingest time, not here).

    Shape:
        counties[fips][crop_key] = {
            "aupa": float,   # max across the county's planting periods
            "days": int|None,
            "n": int,        # number of planting periods on file
            "min": float,
        }

    Plus the axes the in-map controls are built from (crops, crop_labels), county display
    names (borrowed from prf_county's ADM-cased names where a county is in both tables), the
    year the table covers, and min/max over every stored value (the color scale's domain).
    When fsa_grazing_county is empty (or the table is missing) every collection comes back
    empty and min/max are None — the section still renders, all neutral, with an honest note.
    """
    try:
        rows = conn.execute(
            "SELECT g.year, g.county_fips, c.crop_key, "
            "g.acres_per_animal_unit, g.min_acres_per_animal_unit, g.n_periods, "
            "g.grazing_days, c.crop_name, c.crop_type_name "
            "FROM fsa_grazing_county g "
            "JOIN fsa_crop_type c ON c.crop_id = g.crop_id"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []  # tables not created yet — degrade to empty

    prf_names: dict[str, str] = {}
    try:
        for r in conn.execute(
                "SELECT DISTINCT county_fips, county_name FROM prf_county "
                "WHERE county_name IS NOT NULL"):
            fips = _fips5(r["county_fips"])
            if fips and fips not in prf_names:
                prf_names[fips] = str(r["county_name"]).strip()
    except sqlite3.OperationalError:
        pass

    year: int | None = None
    county_names: dict[str, str] = {}
    crops: set[str] = set()
    counties: dict[str, dict] = {}
    values: list[float] = []

    for r in rows:
        fips = _fips5(r["county_fips"])
        if not fips:
            continue
        aupa = r["acres_per_animal_unit"]
        try:
            aupa = float(aupa)
        except (TypeError, ValueError):
            continue
        if aupa <= 0:
            continue
        try:
            row_year = int(r["year"])
        except (TypeError, ValueError):
            row_year = None
        year = row_year if year is None else year

        crop_key = r["crop_key"]
        crops.add(crop_key)
        county_names.setdefault(fips, prf_names.get(fips) or fips)
        min_aupa = r["min_acres_per_animal_unit"]
        try:
            min_aupa = float(min_aupa)
        except (TypeError, ValueError):
            min_aupa = aupa
        days = r["grazing_days"]
        try:
            days = int(days) if days is not None else None
        except (TypeError, ValueError):
            days = None
        counties.setdefault(fips, {})[crop_key] = {
            "aupa": round(aupa, 2),
            "min": round(min_aupa, 2),
            "n": int(r["n_periods"] or 1),
            "days": days,
        }
        values.append(aupa)

    crop_labels = {r["crop_key"]: _crop_label(_crop_key(r["crop_name"], r["crop_type_name"]))
                  for r in rows}
    default_crop = DEFAULT_CROP_KEY if DEFAULT_CROP_KEY in crops else (
        sorted(crops, key=lambda k: crop_labels[k])[0] if crops else None)

    return {
        "counties": counties,
        "county_names": county_names,
        "crops": sorted(crops, key=lambda k: crop_labels[k]),
        "crop_labels": crop_labels,
        "default_crop": default_crop,
        "year": year,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "row_count": len(rows),
        "county_count": len(counties),
    }


def _js_embed_json(obj) -> str:
    return json.dumps(obj, separators=(",", ":")).replace("</", "<\\/")


def render_herd_size_page_html(payload: dict, d3_js: str, topojson_js: str,
                               atlas: dict) -> str:
    for blob in (d3_js, topojson_js):
        if "</script" in blob.lower():
            raise ValueError("asset JS contains '</script'; cannot inline safely")
    html = _TEMPLATE
    html = html.replace("__D3__", d3_js)
    html = html.replace("__TOPOJSON__", topojson_js)
    html = html.replace("__ATLAS__", _js_embed_json(atlas))
    html = html.replace("__PAYLOAD__", _js_embed_json(payload))
    return html


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PRF — herd size requirements</title>
<style>
  :root {
    color-scheme: light;
    --surface: #fcfcfb; --page: #f9f9f7;
    --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
    --grid: #e1e0d9; --baseline: #c3c2b7; --ring: rgba(11,11,11,0.10);
    --none: #ececea; --accent: #2a78d6;
  }
  * { box-sizing: border-box; margin: 0; }
  html, body { height: 100%; }
  body {
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    background: var(--page); color: var(--ink); font-size: 14px;
    display: flex; flex-direction: column;
  }
  header { padding: 12px 18px 8px; }
  header h1 { font-size: 16px; font-weight: 650; }
  header .sub { color: var(--ink-2); font-size: 12.5px; margin-top: 2px; }
  .filters {
    display: flex; gap: 12px; align-items: center; flex-wrap: wrap;
    padding: 8px 18px 10px; border-bottom: 1px solid var(--grid);
    background: var(--surface);
  }
  .filters label { color: var(--ink-2); font-size: 12px; }
  .filters select {
    font: inherit; font-size: 13px; padding: 4px 6px; max-width: 320px;
    border: 1px solid var(--baseline); border-radius: 6px; background: var(--surface);
    color: var(--ink);
  }
  #cropSel { font-weight: 600; max-width: 340px; }
  #main { flex: 1; display: flex; min-height: 0; }
  #mapWrap { flex: 1; position: relative; min-width: 0; }
  #map { width: 100%; height: 100%; display: block; }
  .county { stroke: var(--surface); stroke-width: 0.4; vector-effect: non-scaling-stroke; }
  .county.hovered { stroke: var(--ink); stroke-width: 1.2; }
  .county.dimmed { opacity: 0.30; }
  .statelines { fill: none; stroke: var(--baseline); stroke-width: 0.7;
                pointer-events: none; vector-effect: non-scaling-stroke; }
  .focusline { fill: none; stroke: var(--ink); stroke-width: 2.2; stroke-linejoin: round;
               pointer-events: none; vector-effect: non-scaling-stroke; }
  .hovercase { fill: none; stroke: var(--surface); stroke-width: 4; stroke-linejoin: round;
               pointer-events: none; vector-effect: non-scaling-stroke; }
  .hoverline { fill: none; stroke: var(--ink); stroke-width: 2; stroke-linejoin: round;
               pointer-events: none; vector-effect: non-scaling-stroke; }
  #tooltip {
    position: absolute; pointer-events: none; display: none; z-index: 5;
    background: var(--surface); border: 1px solid var(--ring); border-radius: 8px;
    box-shadow: 0 2px 10px rgba(11,11,11,0.12); padding: 7px 10px; font-size: 12.5px;
    max-width: 350px;
  }
  #tooltip .t-name { font-weight: 650; }
  #tooltip .t-val { color: var(--ink-2); margin-top: 1px; }
  #tooltip .t-line { color: var(--ink-2); font-size: 11.5px; margin-top: 1px; }
  #tooltip .t-math { color: var(--ink-2); font-size: 11.5px; margin-top: 3px;
    font-variant-numeric: tabular-nums; }
  #tooltip .t-more { color: var(--muted); font-size: 11px; margin-top: 4px; font-style: italic; }
  #legend {
    position: absolute; left: 16px; bottom: 14px; background: var(--surface);
    border: 1px solid var(--ring); border-radius: 8px; padding: 8px 10px; font-size: 11.5px;
  }
  #legend .l-title { color: var(--ink-2); margin-bottom: 5px; }
  #legend .l-row { display: flex; align-items: center; gap: 0; }
  #legend .l-cell { width: 34px; height: 10px; }
  #legend .l-labels { display: flex; font-size: 10px; color: var(--muted); margin-top: 1px; }
  #legend .l-labels span { width: 34px; text-align: left; }
  #legend .l-none { display: flex; align-items: center; gap: 6px; margin-top: 7px; color: var(--muted); }
  #legend .l-none .sw { width: 14px; height: 10px; background: var(--none); border: 1px solid var(--grid); }
  #note {
    position: absolute; top: 12px; left: 16px; right: 16px; z-index: 4; display: none;
    background: #fdf6e7; border: 1px solid #eda100; border-radius: 8px;
    color: #6b4b00; padding: 8px 12px; font-size: 12.5px;
  }
  footer { padding: 6px 18px; color: var(--muted); font-size: 11px; border-top: 1px solid var(--grid); }

  #crumb {
    position: absolute; top: 12px; left: 16px; z-index: 4; display: flex; align-items: center;
    gap: 6px; background: var(--surface); border: 1px solid var(--ring); border-radius: 8px;
    padding: 5px 9px; font-size: 12px; box-shadow: 0 1px 4px rgba(11,11,11,0.06);
  }
  #crumb .c-step { color: var(--accent); cursor: pointer; }
  #crumb .c-step:hover { text-decoration: underline; }
  #crumb .c-here { color: var(--ink); font-weight: 600; }
  #crumb .c-sep { color: var(--muted); }
  #crumb .c-hint { color: var(--muted); border-left: 1px solid var(--grid); padding-left: 8px; }
  #zoomBox {
    position: absolute; right: 16px; bottom: 14px; z-index: 4; display: flex; flex-direction: column;
    align-items: center; gap: 6px; background: var(--surface); border: 1px solid var(--ring);
    border-radius: 8px; padding: 9px 7px; box-shadow: 0 1px 4px rgba(11,11,11,0.06);
  }
  #zoomBox button {
    font: inherit; font-size: 15px; line-height: 1; width: 24px; height: 22px; cursor: pointer;
    border: 1px solid var(--baseline); border-radius: 5px; background: var(--surface); color: var(--ink);
  }
  #zoomBox button:hover { border-color: var(--accent); color: var(--accent); }
  #zSlider { writing-mode: vertical-lr; direction: rtl; width: 22px; height: 104px; accent-color: var(--accent); }
  #zLabel { font-size: 10.5px; color: var(--muted); font-variant-numeric: tabular-nums; }
</style>
</head>
<body>
<header>
  <h1 id="pageTitle">PRF — herd size requirements (FSA acres per animal unit)</h1>
  <div class="sub" id="pageSub"></div>
</header>
<div class="filters">
  <label>Crop / type <select id="cropSel"></select></label>
  <span id="countLine" style="color:var(--muted);font-size:12px;margin-left:auto"></span>
</div>
<div id="main">
  <div id="mapWrap">
    <div id="note"></div>
    <div id="crumb"></div>
    <svg id="map" viewBox="0 0 975 610" preserveAspectRatio="xMidYMid meet"></svg>
    <div id="zoomBox">
      <button id="zIn" type="button" title="Zoom in">+</button>
      <input type="range" id="zSlider" min="0" max="100" value="0">
      <button id="zOut" type="button" title="Zoom out">&minus;</button>
      <span id="zLabel">1&times;</span>
    </div>
    <div id="legend"></div>
    <div id="tooltip"></div>
  </div>
</div>
<footer id="pageFoot">
  Source: FSA National Crop Table FY __YEAR__ — Acres Per Animal Unit for GRAZING crop/types,
  by county. Not an RMA table and not scoped to PRF ground specifically: it is FSA's
  NAP/ARC-PLC stocking-rate figure for the crop/type selected above. Where a county carries
  more than one FSA "planting period" for the same crop/type, this map shows the largest
  (most acres required) &mdash; the conservative figure for a herd grazed there continuously
  through its leanest period &mdash; and the tooltip gives the full spread. Counties with no
  row for the current selection are shown neutral.
</footer>

<script>__D3__</script>
<script>__TOPOJSON__</script>
<script>
var US_ATLAS = __ATLAS__;
var DATA = __PAYLOAD__;

(function () {
  "use strict";

  var NONE = getComputedStyle(document.documentElement).getPropertyValue("--none").trim() || "#ececea";
  // Warm ramp, matching the app-wide convention that a warm ramp means "amount required /
  // cost", not "return" (see src/drppage.py's COST_RAMP) — darker here means MORE acres are
  // required per animal unit, i.e. thinner grazing, not a better or worse dollar outcome.
  var RAMP = ["#fee8c8", "#fdd49e", "#fdbb84", "#fc8d59", "#ef6548", "#d7301f", "#990000"];

  var COUNTIES = DATA.counties || {};
  document.getElementById("pageFoot").innerHTML =
    document.getElementById("pageFoot").innerHTML.replace("__YEAR__", DATA.year || "—");

  function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // ---------------- controls
  var cropSel = document.getElementById("cropSel");
  (DATA.crops || []).forEach(function (k) {
    var o = document.createElement("option");
    o.value = k; o.textContent = (DATA.crop_labels && DATA.crop_labels[k]) || k;
    cropSel.appendChild(o);
  });
  var crop = (DATA.crops || []).indexOf(DATA.default_crop) >= 0
    ? DATA.default_crop : ((DATA.crops || [])[0] || null);
  if (crop) cropSel.value = crop;

  // ---------------- value lookup
  // Non-irrigated grazing only — see module docstring for why there is no irrigation
  // control here.
  function cellFor(fips) {
    var c = COUNTIES[fips]; if (!c) return null;
    return c[crop] || null;
  }
  function valFor(fips) {
    var c = cellFor(fips);
    return c ? c.aupa : null;
  }

  // ---------------- formatting
  function fmt(v) { return v === null || v === undefined ? "&mdash;" : v.toFixed(1); }

  // ---------------- geometry (same AlbersUSA params us-atlas pre-projects to)
  var path = d3.geoPath(d3.geoAlbersUsa().scale(1300).translate([487.5, 305]));
  var countiesFC = topojson.feature(US_ATLAS, US_ATLAS.objects.counties).features;
  var stateMesh = topojson.mesh(US_ATLAS, US_ATLAS.objects.states, function () { return true; });
  var statesFC = topojson.feature(US_ATLAS, US_ATLAS.objects.states).features;
  var stateById = {}; statesFC.forEach(function (s) { stateById[String(s.id)] = s; });
  var countyById = {}; countiesFC.forEach(function (c) { countyById[String(c.id)] = c; });

  function countyName(fips) {
    return (DATA.county_names && DATA.county_names[fips]) ||
      ((countyById[fips] && countyById[fips].properties && countyById[fips].properties.name) || "County");
  }
  function stateName(fips2) {
    var s = stateById[fips2];
    return (s && s.properties && s.properties.name) || "State";
  }

  var svg = d3.select("#map");
  var g = svg.append("g");
  var gCounties = g.append("g");
  var countySel = gCounties.selectAll("path").data(countiesFC).join("path")
      .attr("class", "county").attr("d", path)
      .on("mousemove", function (ev, d) { hover.call(this, ev, d); })
      .on("mouseout", unhover)
      .on("click", function (ev, d) { ev.stopPropagation(); countyClicked(d); });
  g.append("path").attr("class", "statelines").attr("d", path(stateMesh));
  var gFocus = g.append("path").attr("class", "focusline");
  var gHoverCase = g.append("path").attr("class", "hovercase");
  var gHoverLine = g.append("path").attr("class", "hoverline");
  function showHover(feat) {
    var d = feat ? path(feat) : null;
    gHoverCase.attr("d", d);
    gHoverLine.attr("d", d);
  }

  // ---------------- tooltip
  function tipHtml(fips, heading) {
    var c = cellFor(fips), v = valFor(fips);
    var h = '<div class="t-name">' + esc(heading || countyName(fips)) + '</div>' +
            '<div class="t-val">Acres per animal unit: ' + fmt(v) + '</div>';
    if (!c) {
      h += '<div class="t-line">No ' + esc((DATA.crop_labels && DATA.crop_labels[crop]) || crop) +
           ' grazing row for this county.</div>';
      return h;
    }
    if (c.n > 1) {
      h += '<div class="t-math">' + c.n + ' FSA planting periods on file: ' +
           fmt(c.min) + '–' + fmt(c.aupa) + ' acres/AU</div>';
      h += '<div class="t-line">Shown value is the largest (most acres required) of the ' +
           c.n + ' periods &mdash; the conservative figure for continuous grazing through ' +
           'the leanest period.</div>';
    }
    if (c.days !== null && c.days !== undefined) {
      h += '<div class="t-math">Grazing period assumed: ' + c.days + ' days</div>';
    }
    return h;
  }
  function place(ev, html) {
    tip.style.display = "block";
    tip.innerHTML = html;
    var wrap = document.getElementById("mapWrap").getBoundingClientRect();
    var x = ev.clientX - wrap.left + 14, y = ev.clientY - wrap.top + 10;
    if (x > wrap.width - 360) x -= 380;
    tip.style.left = x + "px"; tip.style.top = y + "px";
  }
  var tip = document.getElementById("tooltip");

  // ONE HIGHLIGHT AT A TIME, matching src/prfpage.py: at the nation level a click zooms to
  // the county's STATE, so the outline traces the state (not the county under the cursor)
  // until you have actually drilled in. See that module for the full rationale.
  function hover(ev, d) {
    d3.select(this).classed("hovered", level !== 0);
    showHover(level === 0 ? (stateById[String(d.id).slice(0, 2)] || d) : d);
    place(ev, level === 0 ? stateTip(String(d.id).slice(0, 2)) : tipHtml(String(d.id)));
  }
  function unhover() {
    d3.select(this).classed("hovered", false);
    showHover(null);
    tip.style.display = "none";
  }

  // Nation-level rollup: the highlight traces the STATE, so the box must describe the state,
  // not the county the cursor happens to be over — the mismatch src/prfpage.py's stateTip
  // was added to fix. A range, not an average: acres/AU is a per-crop-per-county figure and
  // summing or averaging it across counties has no meaning.
  function stateTip(fips2) {
    var vals = [], best = null, bestFips = null;
    for (var fips in COUNTIES) {
      if (fips.slice(0, 2) !== fips2) continue;
      var v = valFor(fips);
      if (v === null || v === undefined) continue;
      vals.push(v);
      if (best === null || v > best) { best = v; bestFips = fips; }
    }
    var h = '<div class="t-name">' + esc(stateName(fips2)) + '</div>';
    if (!vals.length) {
      h += '<div class="t-val">No county in this state has ' +
           esc((DATA.crop_labels && DATA.crop_labels[crop]) || crop) + ' data.</div>' +
           '<div class="t-more">Click to zoom in and read counties individually.</div>';
      return h;
    }
    h += '<div class="t-val">Acres per animal unit: <b>' +
         fmt(Math.min.apply(null, vals)) + ' – ' + fmt(Math.max.apply(null, vals)) +
         '</b> across counties</div>';
    h += '<div class="t-line">' + vals.length + (vals.length === 1 ? ' county' : ' counties') +
         ' with a value &mdash; a range, not an average.</div>';
    if (bestFips) {
      h += '<div class="t-line">most acres required: ' + esc(countyName(bestFips)) +
           ' at ' + fmt(best) + '</div>';
    }
    h += '<div class="t-more">Click to zoom in and read counties individually.</div>';
    return h;
  }

  // ---------------- drill-down: nation -> state, and NO FURTHER
  // FSA's table has no grain finer than county, so there is nothing below state zoom to
  // drill into — a click on a county at state level does nothing (see countyClicked).
  var K_MIN = 1, K_MAX = 96;
  var level = 0;
  var focusState = null;
  var curK = 1;

  var zoom = d3.zoom().scaleExtent([K_MIN, K_MAX]).on("zoom", function (ev) {
    g.attr("transform", ev.transform);
    curK = ev.transform.k;
    syncZoomUI();
  });
  svg.call(zoom).on("dblclick.zoom", null);
  svg.on("click", function () { drillOut(); });

  function applyTransform(t, dur) {
    if (dur === 0 || document.hidden) svg.call(zoom.transform, t);
    else svg.transition().duration(dur).call(zoom.transform, t);
  }
  function zoomToFeature(f, dur) {
    var b = path.bounds(f);
    var dx = b[1][0] - b[0][0], dy = b[1][1] - b[0][1];
    var cx = (b[0][0] + b[1][0]) / 2, cy = (b[0][1] + b[1][1]) / 2;
    var k = Math.max(K_MIN, Math.min(K_MAX, 0.85 / Math.max(dx / 975, dy / 610)));
    applyTransform(d3.zoomIdentity.translate(975 / 2 - k * cx, 610 / 2 - k * cy).scale(k),
                   dur === undefined ? 650 : dur);
  }
  function resetZoom(dur) {
    applyTransform(d3.zoomIdentity, dur === undefined ? 650 : dur);
  }

  function countyClicked(d) {
    var st = String(d.id).slice(0, 2);
    if (level === 0) {
      focusState = st; level = 1;
      if (stateById[st]) zoomToFeature(stateById[st]);
      applyLevel();
    }
    // TERMINAL: already at state level, and FSA has no county-detail grain to drill into
    // further — the click is absorbed here rather than reaching the background handler,
    // which zooms OUT. "No deeper" and "go back" are different intentions.
  }
  function drillOut() {
    if (level !== 1) return;
    focusState = null; level = 0; resetZoom();
    applyLevel();
  }
  function drillTo(lv) {
    if (lv === 0) { level = 0; focusState = null; resetZoom(); }
    applyLevel();
  }

  function applyLevel() {
    showHover(null);
    countySel.classed("dimmed", function (d) {
      if (level === 0) return false;
      return String(d.id).slice(0, 2) !== focusState;
    });
    gFocus.attr("d", (level === 1 && focusState && stateById[focusState])
      ? path(stateById[focusState]) : null);
    updateCrumb();
  }

  var crumb = document.getElementById("crumb");
  function updateCrumb() {
    var h = '<span class="' + (level === 0 ? "c-here" : "c-step") + '" data-lv="0">United States</span>';
    if (focusState) {
      h += '<span class="c-sep">&rsaquo;</span><span class="c-here">' +
           esc(stateName(focusState)) + '</span>';
    }
    var hint = level === 0
      ? "click a county for its state"
      : "county is the finest grain FSA publishes &middot; click away to go back";
    h += '<span class="c-hint">' + hint + '</span>';
    crumb.innerHTML = h;
    crumb.querySelectorAll(".c-step").forEach(function (el) {
      el.addEventListener("click", function (ev) {
        ev.stopPropagation(); drillTo(parseInt(el.getAttribute("data-lv"), 10));
      });
    });
  }

  // ---------------- zoom slider
  var zSlider = document.getElementById("zSlider"), zLabel = document.getElementById("zLabel");
  function kToSlider(k) { return 100 * Math.log(k / K_MIN) / Math.log(K_MAX / K_MIN); }
  function sliderToK(v) { return K_MIN * Math.pow(K_MAX / K_MIN, v / 100); }
  function syncZoomUI() {
    zSlider.value = String(kToSlider(curK));
    zLabel.innerHTML = (curK < 10 ? curK.toFixed(1) : Math.round(curK)) + "&times;";
  }
  function zoomToK(k) {
    var kk = Math.max(K_MIN, Math.min(K_MAX, k));
    if (document.hidden) svg.call(zoom.scaleTo, kk);
    else svg.transition().duration(120).call(zoom.scaleTo, kk);
  }
  zSlider.addEventListener("input", function () { zoomToK(sliderToK(+zSlider.value)); });
  document.getElementById("zIn").addEventListener("click", function (ev) {
    ev.stopPropagation(); zoomToK(curK * 1.6);
  });
  document.getElementById("zOut").addEventListener("click", function (ev) {
    ev.stopPropagation(); zoomToK(curK / 1.6);
  });

  // ---------------- colour scale + render
  var lo = 0, hi = 1, hasData = false, scale = null;

  function domainFor() {
    var min = null, max = null;
    for (var fips in COUNTIES) {
      var v = valFor(fips);
      if (v === null || v === undefined) continue;
      if (min === null || v < min) min = v;
      if (max === null || v > max) max = v;
    }
    return [min, max];
  }

  function drawLegend() {
    var el = document.getElementById("legend");
    if (!hasData) { el.style.display = "none"; return; }
    el.style.display = "";
    var cells = RAMP.map(function (c) {
      return '<div class="l-cell" style="background:' + c + '"></div>';
    }).join("");
    var thr = scale.thresholds();
    var labels = '<span>' + fmt(lo) + '</span>' +
      thr.map(function (t) { return '<span>' + fmt(t) + '</span>'; }).join("");
    el.innerHTML =
      '<div class="l-title">Acres per animal unit &mdash; darker means MORE acres required ' +
      '(thinner grazing)</div>' +
      '<div class="l-row">' + cells + '</div>' +
      '<div class="l-labels">' + labels + '</div>' +
      '<div class="l-none"><span class="sw"></span>no data for this selection</div>';
  }

  function refresh() {
    var shaded = 0;
    countySel.attr("fill", function (d) {
      var v = valFor(String(d.id));
      if (v === null || v === undefined) return NONE;
      shaded++;
      return scale(v);
    });
    document.getElementById("countLine").innerHTML = !hasData ? "" :
      shaded + " counties with " + esc((DATA.crop_labels && DATA.crop_labels[crop]) || crop) +
      " data";

    var note = document.getElementById("note"), msg = "";
    if (DATA.row_count === 0) {
      msg = "No FSA grazing data loaded yet — run `refresh --source fsa_nct --force` to " +
            "populate fsa_grazing_county.";
    } else if (shaded === 0) {
      msg = "No county has a value for this crop/type.";
    }
    note.style.display = msg ? "block" : "none";
    note.textContent = msg;
    drawLegend();
  }

  function applyMetric() {
    var mm = domainFor();
    hasData = mm[0] !== null && mm[0] !== undefined && mm[1] !== null && mm[1] !== undefined;
    lo = hasData ? mm[0] : 0;
    hi = hasData ? mm[1] : 1;
    if (hi <= lo) hi = lo + 1;
    scale = d3.scaleQuantize().domain([lo, hi]).range(RAMP);
    refresh();
  }

  cropSel.addEventListener("change", function () {
    if (crop === cropSel.value) return;
    crop = cropSel.value;
    applyMetric();
  });

  document.getElementById("pageSub").textContent =
    (DATA.county_count || 0) + " counties with non-irrigated grazing data · FY " +
    (DATA.year || "—") + " · pick a crop/type above.";

  applyMetric();
  applyLevel();
  syncZoomUI();
})();
</script>
</body>
</html>
"""

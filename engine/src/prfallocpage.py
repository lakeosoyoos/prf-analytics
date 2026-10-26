"""PRF section: every interval allocation for one grid, scored and sorted.

The interval analyzer (src/prfintervalpage.py) searches every admissible allocation for a grid
and shows the ONE that wins. This page shows the whole search: every allocation (2-5
non-adjacent intervals, 5% steps, 10%..cap each, JAN-FEB no more than its own cap from crop
year 2027), each with its success rate, return per $1 and return per acre over the chosen
window, sorted by whichever metric is picked -- tens of thousands of rows, paged.

NOT A SECOND IMPLEMENTATION. The header, the controls (metric, coverage, window, productivity),
the per-interval arithmetic and the policy-space rules are lifted out of the analyzer's own page
template at import time (see _part below), so the two pages cannot disagree about what an
allocation is or what it earned. Only the table is this module's. Sorting reproduces the
analyzer's choice of winner: by the chosen metric, ties broken by the other one, remaining ties
in enumeration order -- so row 1 here is the analyzer's "Recommended allocation".

Return per acre sorts identically to return per $1 (it is the same number times the protection
per acre), so picking it changes the units shown, not the order.
"""
from __future__ import annotations

from .prfintervalpage import _TEMPLATE as _ANALYZER, _js_embed_json, build_interval_payload

__all__ = ["build_alloc_payload", "render_alloc_page_html"]


def _part(start: str, end: str) -> str:
    """The analyzer template from `start` (included) up to `end` (excluded). Fails loudly if a
    marker has moved, rather than shipping a page missing the code it relies on."""
    i = _ANALYZER.find(start)
    j = _ANALYZER.find(end, i + 1)
    if i < 0 or j < 0:
        raise RuntimeError(f"prfintervalpage template changed: cannot find {start!r} .. {end!r}")
    return _ANALYZER[i:j]


_STYLE = _part("<style>", "</style>") + "</style>"
# state, controls, window geometry, per-interval nets (needs a draw() to call back)
_CORE = _part("  var IV = DATA.intervals", "  // ---------------- colour")
# combos / compositions / the JAN-FEB limit
_POLICY = _part("  // ---------------- policy space", "  function recommend(series) {")
# page title + subtitle, and PRF_STATE for the host window
_HEADER = _part("  // ---------------- header", "  draw();\n})();")


def build_alloc_payload(conn, grid_id: int, *, county_fips: str | None = None,
                        use: str = "Grazing") -> dict:
    """The analyzer's own payload: the same history, rates, cap and base value."""
    return build_interval_payload(conn, grid_id, county_fips=county_fips, use=use)


def render_alloc_page_html(payload: dict) -> str:
    return (_TEMPLATE.replace("__STYLE__", _STYLE).replace("__CORE__", _CORE)
            .replace("__POLICY__", _POLICY).replace("__HEADER__", _HEADER)
            .replace("__PAYLOAD__", _js_embed_json(payload)))


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PRF — all allocations</title>
__STYLE__
<style>
  .tools { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; padding: 9px 16px 0; }
  .tools label { color: var(--ink-2); font-size: 12px; display: flex; align-items: center; gap: 6px; }
  .count { color: var(--ink-2); font-size: 12px; font-variant-numeric: tabular-nums; }
  .pager { display: flex; gap: 8px; align-items: center; justify-content: flex-end; padding: 8px 16px 0; }
  .pager button {
    font: inherit; font-size: 12.5px; padding: 3px 10px; border: 1px solid var(--baseline);
    border-radius: 6px; background: var(--surface); cursor: pointer;
  }
  .pager button:disabled { color: var(--muted); cursor: default; }
  table.all { margin-top: 6px; }
  table.all th, table.all td { padding: 5px 8px; font-size: 12px; }
  table.all td.l, table.all th.l { text-align: left; }
  table.all tbody tr:nth-child(even) td { background: #f2f2ef; }
  table.all th.sorted, table.all td.sorted { font-weight: 700; color: var(--ink); }
  table.all th.sorted::after { content: " \25BE"; }
  table.all td.neg { color: #9a3b1b; }
  .top1 td { background: #e5f5e0 !important; }
  .check { margin: 8px 16px 0; font-size: 11.5px; color: var(--ink-2); }
  .save {
    font: inherit; font-size: 12.5px; padding: 4px 12px; border-radius: 6px; cursor: pointer;
    border: 1px solid #1f5fb4; background: #2a78d6; color: #fff; font-weight: 600;
  }
  .check.agree { color: #1c6b38; }
</style>
</head>
<body>
<header>
  <h1 id="title">PRF — all allocations</h1>
  <div class="sub" id="sub"></div>
</header>

<div class="bar">
  <label>Sort by <span class="seg" id="metricSeg"></span></label>
  <label>Coverage <span class="seg" id="covSeg"></span></label>
  <label>Window ends <select id="endYear"></select></label>
  <label>Lookback <select id="lookLen"></select>
    <span class="lookout" id="lookOut"></span></label>
  <label id="pfWrap">Productivity <input type="number" id="pf" min="0.6" max="1.5" step="0.05" value="1.00"></label>
</div>

<div class="tools">
  <label>Intervals used <select id="kSel">
    <option value="any">Any</option><option value="2">2</option><option value="3">3</option>
    <option value="4">4</option><option value="5">5</option></select></label>
  <label>Must include <select id="ivSel"><option value="any">Any interval</option></select></label>
  <span class="count" id="count"></span>
  <span style="flex:1"></span>
  <button type="button" class="save" id="saveCsv">Save as CSV…</button>
</div>
<div class="check" id="check"></div>

<section>
  <div class="scroller"><table class="all" id="all"></table></div>
  <div class="note" id="note"></div>
</section>
<div class="pager">
  <button type="button" id="prev">‹ Prev</button>
  <span class="count" id="pageOut"></span>
  <button type="button" id="next">Next ›</button>
</div>

<script>
var DATA = __PAYLOAD__;

(function () {
  "use strict";

__CORE__
__POLICY__
  // ---------------- the whole search, kept so a re-sort or a page turn costs nothing
  var PER = 100, page = 0;
  var SCORED = {key: null, rows: []};

  function scoreAll() {
    var key = coverage + "|" + startIdx() + "|" + endIdx() + "|" + DATA.use;
    if (SCORED.key === key) return SCORED.rows;
    var offered = offeredSet(), series = {};
    IV.forEach(function (iv) {
      if (!offered[iv]) return;
      var s = summarise(iv);
      if (s) series[iv] = s.series;
    });
    var usable = {};
    IV.forEach(function (iv) { if (series[iv]) usable[iv] = 1; });
    var cap = DATA.cap || 60, rows = [];
    // The same loop as the analyzer's recommend(), keeping every allocation instead of the best.
    combos(usable).forEach(function (idxs) {
      var ws = idxs.map(function (i) { return series[IV[i]]; });
      compositionsForCombo(idxs, cap).forEach(function (props) {
        var total = 0, wins = 0, yrs = ws[0].length, counted = 0;
        for (var y = 0; y < yrs; y++) {
          var net = 0, ok = true;
          for (var j = 0; j < ws.length; j++) {
            var v = ws[j][y];
            if (v === null) { ok = false; break; }
            net += (props[j] / 100) * v;
          }
          if (!ok) continue;
          counted++; total += net;
          if (net > 0) wins++;
        }
        if (!counted) return;
        rows.push({idxs: idxs, props: props, win: wins / counted, avg: total / counted,
                   wins: wins, n: counted, seq: rows.length});
      });
    });
    SCORED = {key: key, rows: rows};
    return rows;
  }

  // By the chosen metric, ties broken by the other one, then enumeration order: exactly the
  // analyzer's pick for row 1 (and src/prfopt.rank_by_win_rate / rank_by_avg_net_return).
  function sorted(rows) {
    var byWin = metric === "win";
    var out = rows.slice().sort(function (a, b) {
      var sa = byWin ? a.win : a.avg, sb = byWin ? b.win : b.avg;
      if (Math.abs(sa - sb) > 1e-12) return sb - sa;
      var ta = byWin ? a.avg : a.win, tb = byWin ? b.avg : b.win;
      if (Math.abs(ta - tb) > 1e-12) return tb - ta;
      return a.seq - b.seq;
    });
    out.forEach(function (r, i) { r.rank = i + 1; });
    return out;
  }

  var ivSel = el("ivSel"), kSel = el("kSel");
  ivSel.innerHTML += IV.map(function (iv) {
    return "<option value='" + iv + "'>" + esc(LBL[iv] || iv) + "</option>";
  }).join("");
  kSel.addEventListener("change", function () { page = 0; draw(); });
  ivSel.addEventListener("change", function () { page = 0; draw(); });
  el("prev").addEventListener("click", function () { page--; draw(true); });
  el("next").addEventListener("click", function () { page++; draw(true); });

  // ---------------- Save as CSV: the optimizer workbook's own columns, in the order on screen
  // (sorted, and filtered if a filter is set). average_net_return is per acre, as there.
  function pyList(items, quote) {
    return "[" + items.map(function (x) { return quote ? "'" + x + "'" : String(x); }).join(", ") + "]";
  }
  function csvCell(v) {
    var t = String(v);
    return /[",\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
  }
  function csvName() {
    var yrs = windowYears();
    return ["PRF_allocations", DATA.grid_id, (DATA.county_name || "").replace(/[^A-Za-z0-9]+/g, "_"),
            (DATA.use || "").replace(/[^A-Za-z0-9]+/g, "_"), Math.round(parseFloat(coverage) * 100) + "pct",
            yrs[0] + "-" + yrs[yrs.length - 1]].filter(Boolean).join("_") + ".csv";
  }
  function buildCsv() {
    var prot = protection();
    var key = [DATA.grid_id, DATA.county_name || "", (DATA.use || "").toLowerCase()].join("_");
    var lines = ["grid_county_use,combinations,proportions,average_net_return,win rate,state," +
                 "rank,net_return_per_dollar,coverage,years"];
    var yrs = windowYears(), span = yrs[0] + "-" + yrs[yrs.length - 1];
    (LAST.shown || []).forEach(function (r) {
      lines.push([key, pyList(r.idxs.map(function (i) { return IV[i]; }), true), pyList(r.props),
                  prot === null ? "" : r.avg * prot, r.win, DATA.state || "", r.rank, r.avg,
                  coverage, span].map(csvCell).join(","));
    });
    return lines.join("\n") + "\n";
  }
  el("saveCsv").addEventListener("click", function () {
    var name = csvName(), text = buildCsv(), host = window.parent && window.parent.pywebview;
    if (host && host.api && host.api.save_text_file) {
      host.api.save_text_file(name, text).then(function (p) {
        if (p && window.parent.PRF_TOAST) window.parent.PRF_TOAST("Saved " + p.split("/").pop());
      });
      return;
    }
    var a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([text], {type: "text/csv"}));
    a.download = name; document.body.appendChild(a); a.click(); a.remove();
  });

  function money(v, dp) { return (v >= 0 ? "" : "−") + "$" + Math.abs(v).toFixed(dp); }

  var LAST = {sig: null, list: null, shown: null};
  function draw(keepPage) {
    var yrs = windowYears();
    el("lookOut").textContent = lookback + " yr (" + yrs[0] + "–" + yrs[yrs.length - 1] + ")";
    if (!keepPage) page = Math.max(0, page);

    var rows = scoreAll();
    var sig = SCORED.key + "|" + metric;
    if (LAST.sig !== sig) LAST = {sig: sig, list: sorted(rows), shown: null};
    var all = LAST.list;
    var k = kSel.value, must = ivSel.value, mustIdx = IV.indexOf(must);
    var list = all.filter(function (r) {
      return (k === "any" || r.idxs.length === +k) && (must === "any" || r.idxs.indexOf(mustIdx) >= 0);
    });
    var pages = Math.max(1, Math.ceil(list.length / PER));
    page = Math.min(Math.max(0, page), pages - 1);
    var prot = protection();

    var cols = [["rank", "Rank"], ["iv", "Combinations"], ["al", "Proportions"],
                ["acre", "Average net return / acre"], ["win", "Win rate"], ["net", "Return per $1"]];
    var head = "<thead><tr>" + cols.map(function (c) {
      var cls = (c[0] === metric ? "sorted" : "") + (c[0] === "iv" || c[0] === "al" ? " l" : "");
      return "<th class='" + cls + "'>" + c[1] + "</th>";
    }).join("") + "</tr></thead>";
    var body = list.slice(page * PER, (page + 1) * PER).map(function (r) {
      var ivs = r.idxs.map(function (i) { return esc(LBL[IV[i]] || IV[i]); }).join(", ");
      var acre = prot === null ? "&mdash;" : money(r.avg * prot, 2);
      function td(key, text, extra) {
        return "<td class='" + (key === metric ? "sorted " : "") + (extra || "") + "'>" + text + "</td>";
      }
      return "<tr class='" + (r.rank === 1 ? "top1" : "") + "'>" +
        td("rank", "#" + r.rank.toLocaleString()) +
        td("iv", ivs, "l") + td("al", r.props.join(", "), "l") +
        td("acre", acre, r.avg < 0 ? "neg" : "") +
        td("win", (r.win * 100).toFixed(1) + "%" + " <span style='color:var(--muted)'>(" + r.wins + "/" + r.n + ")</span>") +
        td("net", money(r.avg, 4), r.avg < 0 ? "neg" : "") + "</tr>";
    }).join("");
    el("all").innerHTML = head + "<tbody>" + (body ||
      "<tr><td colspan='6'>No allocation matches these filters.</td></tr>") + "</tbody>";
    LAST.shown = list;

    var filtered = list.length !== all.length;
    el("count").innerHTML = "<b>" + list.length.toLocaleString() + "</b> allocations" +
      (filtered ? " of " + all.length.toLocaleString() : "") + " over " + yrs.length +
      " years at " + Math.round(parseFloat(coverage) * 100) + "% coverage";
    el("pageOut").textContent = list.length
      ? "Rows " + (page * PER + 1).toLocaleString() + "–" +
        Math.min(list.length, (page + 1) * PER).toLocaleString() + " · page " + (page + 1) + " of " + pages
      : "";
    el("prev").disabled = page <= 0;
    el("next").disabled = page >= pages - 1;

    // Row 1 against what the sweep stored -- only when this is the sweep's own window.
    var sw = (DATA.sweep || {})[coverage], top = all[0], chk = el("check");
    chk.className = "check"; chk.innerHTML = "";
    if (sw && top && +sw.y0 === yrs[0] && +sw.y1 === yrs[yrs.length - 1]) {
      var combo = metric === "win" ? sw.win_combo : sw.net_combo;
      var props = metric === "win" ? sw.win_props : sw.net_props;
      var mine = top.idxs.map(function (ix, j) { return IV[ix] + ":" + top.props[j]; }).join(" ");
      var theirs = (combo || []).map(function (iv, j) { return iv + ":" + (props || [])[j]; }).join(" ");
      if (mine === theirs) {
        chk.className = "check agree";
        chk.innerHTML = "✓ Rank #1 matches the stored sweep (prf_opt_best) for " + yrs[0] + "–" + yrs[yrs.length - 1] + ".";
      } else {
        chk.innerHTML = "The stored sweep's pick is <b>" + esc(theirs || "—") + "</b>; see the analyzer's note on near-ties.";
      }
    }

    el("note").innerHTML =
      "Every admissible allocation: 2–5 non-adjacent intervals, " + RULES.step_pct + "% steps, " +
      RULES.min_pct + "%–" + (DATA.cap || 60) + "% each" +
      (DATA.janfeb_cap ? ", Jan–Feb at most " + DATA.janfeb_cap + "%" : "") +
      ". Success rate is the share of years the allocation's net return was above zero; returns " +
      "are averages per year. Sorted by <b>" + esc(METRICS[metric].label) + "</b>, ties broken by " +
      (metric === "win" ? "return" : "success rate") + ". Return per acre sorts the same as " +
      "return per $1 — it is that number times $" + (prot === null ? "?" : prot.toFixed(2)) +
      " of protection per acre (CBV × coverage × productivity " + pf().toFixed(2) + ").";
  }

__HEADER__
  draw();
})();
</script>
</body>
</html>
"""

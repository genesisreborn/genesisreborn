/* Drawing tools for the StockPulse chart (trend line, ray, extended line, horizontal / vertical line,
 * rectangle, Fibonacci retracement, text, measure, long / short position with risk sizing). Drawings are stored as {time, price} points so they
 * survive interval changes, and are saved per symbol on the server.
 *
 * Uses globals from chart.html: api, css, esc, fmt, state.
 */
(function () {
  const TOOLS = [
    ["cursor", "↖", "光标 / 选择（Esc）"], ["trend", "╱", "趋势线（Alt+T）"], ["ray", "➚", "射线"],
    ["extended", "⟷", "延长线"], ["hline", "─", "水平线（Alt+H）"], ["vline", "│", "垂直线（Alt+V）"],
    ["rect", "▭", "矩形"], ["fib", "≡", "斐波那契回撤（Alt+F）"], ["text", "T", "文字"], ["measure", "⇕", "测量（价格/涨跌/K 线数）"],
    ["long", "⬆", "做多仓位：开仓 / 止损 / 目标、盈亏比和股数（Alt+L）"], ["short", "⬇", "做空仓位（Alt+S）"],
  ];
  const POSITION = new Set(["long", "short"]);
  // account size and risk per trade for the position tool's share count (per browser)
  const risk = (() => { try { return { account: 100000, pct: 1, ...JSON.parse(localStorage.getItem("sp.risk") || "{}") }; } catch (e) { return { account: 100000, pct: 1 }; } })();
  const saveRisk = () => { try { localStorage.setItem("sp.risk", JSON.stringify(risk)); } catch (e) {} };
  const TWO_POINT = new Set(["trend", "ray", "extended", "rect", "fib", "measure"]);
  const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1, 1.618];
  const NAMES = Object.fromEntries(TOOLS.map(t => [t[0], t[2].split("（")[0]]));

  let chart = null, series = null;
  let bars = [], times = [], step = 86400, symbol = null;
  let drawings = [], tool = "cursor", magnet = false, hidden = false;
  let draft = null, selected = null, drag = null, dirty = true, loaded = false;
  const saveTimers = new Map();   // per symbol, so switching symbols never cancels a pending save
  let capturing = false;  // we own the current gesture: keep it away from the chart (no panning)

  const canvas = document.getElementById("drawCanvas");
  const area = document.getElementById("chartArea");
  const ctx = canvas.getContext("2d");
  const uid = () => Math.random().toString(36).slice(2, 10);

  // ------------------------------------------------------------ coordinates
  function timeToLogical(t) {
    const n = times.length;
    if (!n) return null;
    if (t <= times[0]) return (t - times[0]) / step;
    if (t >= times[n - 1]) return n - 1 + (t - times[n - 1]) / step;
    let lo = 0, hi = n - 1;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (times[mid] <= t) lo = mid; else hi = mid; }
    return lo + (t - times[lo]) / (times[hi] - times[lo]);
  }
  function logicalToTime(l) {
    const n = times.length;
    if (!n) return null;
    if (l <= 0) return Math.round(times[0] + l * step);
    if (l >= n - 1) return Math.round(times[n - 1] + (l - (n - 1)) * step);
    const i = Math.floor(l);
    return Math.round(times[i] + (l - i) * (times[i + 1] - times[i]));
  }
  function toXY(p) {
    const l = timeToLogical(p.time);
    const x = l == null ? null : chart.timeScale().logicalToCoordinate(l);
    const y = series.priceToCoordinate(p.price);
    return x == null || y == null ? null : { x, y };
  }
  function fromXY(x, y) {
    const l = chart.timeScale().coordinateToLogical(x);
    let price = series.coordinateToPrice(y);
    if (l == null || price == null) return null;
    let time = logicalToTime(Math.round(l * 1000) / 1000);
    if (magnet) {
      const i = Math.round(l);
      if (i >= 0 && i < bars.length) {
        const b = bars[i];
        time = b.time;
        price = [b.open, b.high, b.low, b.close].reduce((a, v) => Math.abs(v - price) < Math.abs(a - price) ? v : a);
      }
    }
    return { time, price };
  }
  function plotSize() {
    const w = chart.timeScale().width();
    const panes = chart.panes();
    const h = panes.length ? panes[0].getHeight() : canvas.height;
    return { w, h };
  }

  // ------------------------------------------------------------ rendering
  function resize() {
    const r = area.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(r.width * dpr); canvas.height = Math.round(r.height * dpr);
    canvas.style.width = r.width + "px"; canvas.style.height = r.height + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    dirty = true;
  }

  function line(a, b, color, width, dash) {
    ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash ? [6, 4] : []);
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
  }
  function extend(a, b, w, both) {
    const dx = b.x - a.x, dy = b.y - a.y;
    if (Math.abs(dx) < 1e-9) return [{ x: a.x, y: both ? -1e4 : a.y }, { x: b.x, y: dy > 0 ? 1e4 : -1e4 }];
    const k = dy / dx;
    const far = dx > 0 ? w + 50 : -50;
    const end = { x: far, y: a.y + k * (far - a.x) };
    if (!both) return [a, end];
    const near = dx > 0 ? -50 : w + 50;
    return [{ x: near, y: a.y + k * (near - a.x) }, end];
  }
  function label(text, x, y, color, align = "left") {
    ctx.font = "11px -apple-system, 'PingFang SC', sans-serif";
    const tw = ctx.measureText(text).width;
    const bx = align === "right" ? x - tw - 8 : x;
    ctx.fillStyle = css("--panel"); ctx.globalAlpha = 0.85; ctx.fillRect(bx, y - 9, tw + 8, 16); ctx.globalAlpha = 1;
    ctx.fillStyle = color; ctx.fillText(text, bx + 4, y + 3);
  }
  const priceText = (p) => fmt(p, p < 10 ? 4 : 2);

  function drawOne(d, isSel) {
    const pts = d.points.map(toXY);
    if (pts.some(p => !p)) return;
    const { w, h } = plotSize();
    const color = d.color || css("--s1");
    const width = d.width || 2;
    const [a, b] = pts;
    switch (d.type) {
      case "trend": line(a, b, color, width, d.dash); break;
      case "ray": { const [p, q] = extend(a, b, w, false); line(p, q, color, width, d.dash); break; }
      case "extended": { const [p, q] = extend(a, b, w, true); line(p, q, color, width, d.dash); break; }
      case "hline": line({ x: 0, y: a.y }, { x: w, y: a.y }, color, width, d.dash); label(priceText(d.points[0].price) + (d.alert ? " 🔔" : ""), w - 2, a.y, color, "right"); break;
      case "vline": line({ x: a.x, y: 0 }, { x: a.x, y: h }, color, width, d.dash); break;
      case "rect": {
        ctx.fillStyle = color; ctx.globalAlpha = 0.12; ctx.fillRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y));
        ctx.globalAlpha = 1; ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(d.dash ? [6, 4] : []);
        ctx.strokeRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y)); break;
      }
      case "fib": {
        const p0 = d.points[0].price, p1 = d.points[1].price;
        const x0 = Math.min(a.x, b.x), x1 = w;
        FIB_LEVELS.forEach((lv, i) => {
          const price = p1 + (p0 - p1) * lv;
          const y = series.priceToCoordinate(price);
          if (y == null) return;
          const c = i % 2 ? color : css("--muted");
          line({ x: x0, y }, { x: x1, y }, c, 1, lv !== 0 && lv !== 1);
          label(`${lv} (${priceText(price)})`, x0 + 2, y - 8, c);
        });
        line(a, b, css("--muted"), 1, true); break;
      }
      case "text": {
        ctx.font = "13px -apple-system, 'PingFang SC', sans-serif";
        const tw = ctx.measureText(d.text || "").width;
        ctx.fillStyle = css("--panel"); ctx.globalAlpha = 0.85; ctx.fillRect(a.x - 4, a.y - 14, tw + 8, 20); ctx.globalAlpha = 1;
        ctx.fillStyle = color; ctx.fillText(d.text || "", a.x, a.y); break;
      }
      case "measure": {
        const up = d.points[1].price >= d.points[0].price;
        const c = up ? css("--up") : css("--down");
        ctx.fillStyle = c; ctx.globalAlpha = 0.12; ctx.fillRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y)); ctx.globalAlpha = 1;
        line(a, b, c, 1, true);
        const dp = d.points[1].price - d.points[0].price, pct = dp / d.points[0].price * 100;
        const nb = Math.round(timeToLogical(d.points[1].time) - timeToLogical(d.points[0].time));
        label(`${dp >= 0 ? "+" : ""}${priceText(dp)} (${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%) · ${nb} 根K线`, Math.min(a.x, b.x), Math.min(a.y, b.y) - 12, c); break;
      }
    }
    if (POSITION.has(d.type)) drawPosition(d, pts, w);
    if (isSel) {
      ctx.setLineDash([]);
      pts.forEach(p => { ctx.fillStyle = css("--panel"); ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(p.x, p.y, 5, 0, Math.PI * 2); ctx.fill(); ctx.stroke(); });
    }
  }

  function positionStats(d) {
    const [entry, stop, target] = d.points.map(p => p.price);
    const riskPer = Math.abs(entry - stop), reward = Math.abs(target - entry);
    const shares = riskPer > 0 ? Math.floor(risk.account * risk.pct / 100 / riskPer) : 0;
    return { entry, stop, target, rr: riskPer > 0 ? reward / riskPer : null, shares, riskAmt: shares * riskPer,
             rewardAmt: shares * reward, notional: shares * entry,
             stopPct: (stop / entry - 1) * 100, targetPct: (target / entry - 1) * 100 };
  }
  function drawPosition(d, pts, w) {
    const [e, s, t] = pts;
    const x0 = e.x, x1 = Math.max(s.x, e.x + 20);
    const st = positionStats(d);
    ctx.globalAlpha = 0.18;
    ctx.fillStyle = css("--up"); ctx.fillRect(x0, Math.min(e.y, t.y), x1 - x0, Math.abs(t.y - e.y));
    ctx.fillStyle = css("--down"); ctx.fillRect(x0, Math.min(e.y, s.y), x1 - x0, Math.abs(s.y - e.y));
    ctx.globalAlpha = 1;
    line({ x: x0, y: e.y }, { x: x1, y: e.y }, css("--text"), 1, false);
    line({ x: x0, y: t.y }, { x: x1, y: t.y }, css("--up"), 1, false);
    line({ x: x0, y: s.y }, { x: x1, y: s.y }, css("--down"), 1, false);
    const sign = (v) => (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
    const money = (v) => Math.round(v).toLocaleString();
    label(`目标 ${priceText(st.target)} (${sign(st.targetPct)}) · 盈 ${money(st.rewardAmt)}`, x0 + 2, t.y + (t.y < e.y ? -10 : 12), css("--up"));
    label(`止损 ${priceText(st.stop)} (${sign(st.stopPct)}) · 亏 ${money(st.riskAmt)}`, x0 + 2, s.y + (s.y > e.y ? 12 : -10), css("--down"));
    label(`${d.type === "long" ? "多" : "空"} ${priceText(st.entry)} · 盈亏比 ${st.rr == null ? "—" : st.rr.toFixed(2)} · ${st.shares} 股 (${money(st.notional)})`,
          x1 + 4, e.y, css("--text"));   // right of the box, so it never collides with the stop / target labels
  }

  function redraw() {
    if (!dirty || !chart || !series) return;
    dirty = false;
    const r = area.getBoundingClientRect();
    ctx.clearRect(0, 0, r.width, r.height);
    if (hidden) return;
    const { w, h } = plotSize();
    ctx.save(); ctx.beginPath(); ctx.rect(0, 0, w, h); ctx.clip();
    drawings.forEach(d => { if (!d.hidden) drawOne(d, d === selected); });
    if (draft) drawOne(draft, true);
    ctx.restore();
  }
  // Panes and price scales re-layout after container resizes or autoscale; watch a cheap signature each frame.
  let layoutSig = "";
  function checkLayout() {
    if (!chart || !series || !bars.length) return;
    const { w, h } = plotSize();
    const p = bars[bars.length - 1].close;
    const sig = [w, h, series.priceToCoordinate(p), series.priceToCoordinate(p * 1.1), chart.timeScale().logicalToCoordinate(0)].join(",");
    if (sig !== layoutSig) { layoutSig = sig; dirty = true; }
  }
  (function loop() { checkLayout(); redraw(); requestAnimationFrame(loop); })();

  // ------------------------------------------------------------ hit testing
  function distToSeg(p, a, b) {
    const dx = b.x - a.x, dy = b.y - a.y;
    const L = dx * dx + dy * dy;
    const t = L ? Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / L)) : 0;
    return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
  }
  function hit(p) {
    const { w, h } = plotSize();
    for (let i = drawings.length - 1; i >= 0; i--) {
      const d = drawings[i];
      if (d.hidden) continue;
      const pts = d.points.map(toXY);
      if (pts.some(q => !q)) continue;
      const hIdx = pts.findIndex(q => Math.hypot(q.x - p.x, q.y - p.y) < 8);
      if (hIdx >= 0 && !d.locked) return { d, handle: hIdx };
      const [a, b] = pts;
      let dist = Infinity;
      if (d.type === "trend" || d.type === "fib") dist = distToSeg(p, a, b);
      if (d.type === "ray") { const [x, y] = extend(a, b, w, false); dist = distToSeg(p, x, y); }
      if (d.type === "extended") { const [x, y] = extend(a, b, w, true); dist = distToSeg(p, x, y); }
      if (d.type === "hline") dist = Math.abs(p.y - a.y);
      if (d.type === "vline") dist = Math.abs(p.x - a.x);
      if (d.type === "rect" || d.type === "measure" || d.type === "fib") {
        const inside = p.x >= Math.min(a.x, b.x) - 4 && p.x <= Math.max(a.x, b.x) + 4 && p.y >= Math.min(a.y, b.y) - 4 && p.y <= Math.max(a.y, b.y) + 4;
        if (inside) dist = 0;
      }
      if (POSITION.has(d.type)) {
        const ys = pts.map(q => q.y), x1 = Math.max(pts[1].x, pts[0].x + 20);
        if (p.x >= pts[0].x - 4 && p.x <= x1 + 4 && p.y >= Math.min(...ys) - 4 && p.y <= Math.max(...ys) + 4) dist = 0;
      }
      if (d.type === "text") dist = (p.x >= a.x - 4 && p.x <= a.x + 200 && p.y >= a.y - 16 && p.y <= a.y + 6) ? 0 : Infinity;
      if (dist < 6 && p.y <= h) return { d, handle: null };
    }
    return null;
  }

  // ------------------------------------------------------------ interaction
  const local = (e) => { const r = area.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
  const inPlot = (p) => { const { w, h } = plotSize(); return p.x >= 0 && p.x <= w && p.y >= 0 && p.y <= h; };

  function finishDraft() {
    if (!draft) return;
    drawings.push(draft); selected = draft; draft = null;
    setTool("cursor"); changed();
  }

  area.addEventListener("pointerdown", (e) => {
    if (!chart || e.button !== 0) return;
    const p = local(e);
    if (!inPlot(p)) return;
    if (tool !== "cursor") {
      e.stopPropagation(); e.preventDefault(); capturing = true;
      const pt = fromXY(p.x, p.y);
      if (!pt) return;
      if (draft) { draft.points[1] = pt; finishDraft(); return; }
      if (TWO_POINT.has(tool)) {
        draft = { id: uid(), type: tool, points: [pt, { ...pt }], color: defaultColor(tool), width: 2 };
        drag = { mode: "draft", start: p };
      } else if (POSITION.has(tool)) {
        // stop 2% away, target at 2R, box 25 bars wide; all three handles can be dragged afterwards
        const dir = tool === "long" ? 1 : -1, end = logicalToTime(timeToLogical(pt.time) + 25);
        const stopPx = pt.price * (1 - dir * 0.02), targetPx = pt.price + dir * 2 * Math.abs(pt.price - stopPx);
        draft = { id: uid(), type: tool, points: [pt, { time: end, price: stopPx }, { time: end, price: targetPx }], color: css("--s1"), width: 1 };
        finishDraft();
      } else {
        const d = { id: uid(), type: tool, points: [pt], color: defaultColor(tool), width: tool === "text" ? 1 : 2 };
        if (tool === "text") { d.text = prompt("文字内容：", "") || ""; if (!d.text) return setTool("cursor"); }
        draft = d; finishDraft();
      }
      dirty = true; return;
    }
    const h = hit(p);
    if (h) {
      e.stopPropagation(); e.preventDefault(); capturing = true;
      selected = h.d; renderProps();
      if (!h.d.locked) drag = { mode: h.handle == null ? "move" : "handle", handle: h.handle, start: fromXY(p.x, p.y), orig: h.d.points.map(q => ({ ...q })), startXY: p };
      dirty = true;
    } else if (selected) { selected = null; renderProps(); dirty = true; }
  }, true);

  area.addEventListener("pointermove", (e) => {
    if (!chart) return;
    const p = local(e);
    dirty = true;
    if (draft && TWO_POINT.has(draft.type)) {
      const pt = fromXY(p.x, p.y); if (pt) draft.points[1] = pt;
      e.stopPropagation(); return;
    }
    if (drag && selected && (drag.mode === "move" || drag.mode === "handle")) {
      e.stopPropagation(); e.preventDefault();
      const pt = fromXY(p.x, p.y);
      if (!pt || !drag.start) return;
      if (drag.mode === "handle" && POSITION.has(selected.type)) {
        const pts = selected.points;
        if (drag.handle === 0) pts[0] = pt;
        else { pts[drag.handle].price = pt.price; pts[1].time = pts[2].time = pt.time; }   // stop & target share the box's right edge
      } else if (drag.mode === "handle") selected.points[drag.handle] = pt;
      else {
        const dl = timeToLogical(pt.time) - timeToLogical(drag.start.time), dp = pt.price - drag.start.price;
        selected.points = drag.orig.map(q => ({ time: logicalToTime(timeToLogical(q.time) + dl), price: q.price + dp }));
      }
      return;
    }
    if (tool === "cursor") area.style.cursor = inPlot(p) && hit(p) ? "pointer" : "";
    else area.style.cursor = "crosshair";
  }, true);

  area.addEventListener("pointerup", (e) => {
    const p = local(e);
    if (drag && drag.mode === "draft" && draft && Math.hypot(p.x - drag.start.x, p.y - drag.start.y) > 6) {
      const pt = fromXY(p.x, p.y); if (pt) draft.points[1] = pt;
      finishDraft();
    } else if (drag && (drag.mode === "move" || drag.mode === "handle")) {
      e.stopPropagation(); changed();
    }
    drag = null;
    setTimeout(() => { capturing = !!draft; }, 0);
  }, true);
  // The chart library also listens to mouse/touch events; swallow them while we own the gesture.
  ["mousedown", "mousemove", "mouseup", "touchstart", "touchmove", "touchend"].forEach(t =>
    area.addEventListener(t, (e) => { if (capturing || draft) e.stopPropagation(); }, { capture: true, passive: true }));

  area.addEventListener("dblclick", (e) => {
    const h = hit(local(e));
    if (h && h.d.type === "text") { const t = prompt("修改文字：", h.d.text); if (t != null) { h.d.text = t; changed(); } e.stopPropagation(); }
  }, true);
  area.addEventListener("wheel", () => { dirty = true; }, { passive: true });

  document.addEventListener("keydown", (e) => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
    if (e.key === "Escape") { draft = null; setTool("cursor"); selected = null; renderProps(); dirty = true; }
    if ((e.key === "Delete" || e.key === "Backspace") && selected) { removeDrawing(selected); e.preventDefault(); }
    if (e.altKey) {
      const map = { t: "trend", h: "hline", v: "vline", f: "fib", l: "long", s: "short" };
      const k = map[e.key.toLowerCase()];
      if (k) { setTool(k); e.preventDefault(); }
    }
  });

  function defaultColor(t) {
    return { long: css("--s1"), short: css("--s1"), hline: css("--s4"), vline: css("--muted"), rect: css("--s7"), fib: css("--s2"), text: css("--text"), measure: css("--s1") }[t] || css("--s1");
  }

  // ------------------------------------------------------------ toolbar & properties
  const bar = document.getElementById("drawTools");
  function renderToolbar() {
    bar.innerHTML = TOOLS.map(([k, icon, title]) => `<button data-tool="${k}" class="${tool === k ? "on" : ""}" title="${esc(title)}" aria-label="${esc(title)}">${icon}</button>`).join("")
      + `<div class="sep"></div>
         <button data-act="magnet" class="${magnet ? "on" : ""}" title="磁吸：吸附到开高低收" aria-label="磁吸">🧲</button>
         <button data-act="hide" class="${hidden ? "on" : ""}" title="隐藏/显示所有图形" aria-label="隐藏图形">👁</button>
         <button data-act="list" title="图形列表" aria-label="图形列表">☰</button>
         <button data-act="clear" title="删除本代码的所有图形" aria-label="删除所有图形">🗑</button>`;
    bar.querySelectorAll("[data-tool]").forEach(b => b.onclick = () => setTool(b.dataset.tool));
    bar.querySelectorAll("[data-act]").forEach(b => b.onclick = () => {
      const a = b.dataset.act;
      if (a === "magnet") magnet = !magnet;
      if (a === "hide") { hidden = !hidden; dirty = true; }
      if (a === "clear" && drawings.length && confirm(`删除 ${symbol} 的全部 ${drawings.length} 个图形？`)) { drawings = []; selected = null; changed(); renderProps(); }
      if (a === "list") toggleList();
      renderToolbar();
    });
  }
  function setTool(t) { tool = t; draft = null; renderToolbar(); area.style.cursor = t === "cursor" ? "" : "crosshair"; }

  const props = document.getElementById("drawProps");
  function renderProps() {
    if (!selected) { props.hidden = true; return; }
    const d = selected;
    props.hidden = false;
    props.innerHTML = `<b style="font-size:12px">${esc(NAMES[d.type] || d.type)}</b>
      <input type="color" id="dpColor" value="${esc(toHex(d.color))}" title="颜色">
      <select id="dpWidth" title="线宽">${[1, 2, 3, 4].map(n => `<option ${n === (d.width || 2) ? "selected" : ""}>${n}</option>`).join("")}</select>
      <button id="dpDash" class="${d.dash ? "on" : ""}" title="虚线">┄</button>
      <button id="dpLock" title="锁定">${d.locked ? "🔒" : "🔓"}</button>
      ${d.type === "text" ? '<button id="dpText">改文字</button>' : ""}
      ${POSITION.has(d.type) ? `<label title="账户资金（用于算股数）">资金 <input id="dpAcct" type="number" min="0" step="1000" value="${risk.account}" style="width:90px"></label>
        <label title="每笔愿意亏损的资金比例">风险% <input id="dpRisk" type="number" min="0.05" max="10" step="0.05" value="${risk.pct}" style="width:60px"></label>` : ""}
      ${d.type === "hline" ? `<button id="dpAlert" title="在这个价位设置提醒">🔔 ${d.alert ? "已设提醒" : "设提醒"}</button>` : ""}
      <button class="danger" id="dpDel" title="删除（Delete）">删除</button>`;
    const on = (id, fn) => { const el = document.getElementById(id); if (el) el.onclick = fn; };
    document.getElementById("dpColor").oninput = (e) => { d.color = e.target.value; dirty = true; };
    document.getElementById("dpColor").onchange = changed;
    document.getElementById("dpWidth").onchange = (e) => { d.width = +e.target.value; changed(); };
    on("dpDash", () => { d.dash = !d.dash; changed(); renderProps(); });
    on("dpLock", () => { d.locked = !d.locked; changed(); renderProps(); });
    on("dpText", () => { const t = prompt("修改文字：", d.text); if (t != null) { d.text = t; changed(); } });
    on("dpAlert", () => createAlert(d));
    on("dpDel", () => removeDrawing(d));
    const acct = document.getElementById("dpAcct"), rp = document.getElementById("dpRisk");
    if (acct) acct.onchange = () => { const v = +acct.value; if (v > 0) { risk.account = v; saveRisk(); dirty = true; } };
    if (rp) rp.onchange = () => { const v = +rp.value; if (v > 0 && v <= 10) { risk.pct = v; saveRisk(); dirty = true; } };
  }
  function toHex(c) {
    if (!c) return "#3987e5";
    if (/^#[0-9a-f]{6}$/i.test(c)) return c;
    const el = document.createElement("canvas").getContext("2d"); el.fillStyle = c; return el.fillStyle.length === 7 ? el.fillStyle : "#3987e5";
  }

  async function createAlert(d) {
    const level = +d.points[0].price.toFixed(4);
    const last = bars.length ? bars[bars.length - 1].close : level;
    const kind = level >= last ? "cross_up" : "cross_down";
    const realtime = confirm(`在 ${priceText(level)} 设置${kind === "cross_up" ? "上穿" : "下穿"}提醒。\n\n要盘中实时提醒吗？（确定 = 实时，取消 = K 线收盘时检查）`);
    try {
      await api("/api/alerts", { method: "POST", body: JSON.stringify({
        symbol, interval: state.interval, kind, level, realtime, mode: "once", note: "图表水平线",
        condition: `${kind === "cross_up" ? "crossover" : "crossunder"}(close, ${level})` }) });
      d.alert = true; changed(); renderProps();
      if (typeof drawAlertLines === "function") drawAlertLines();
    } catch (e) { alert(e.message); }
  }

  const list = document.getElementById("drawList");
  function toggleList() {
    list.hidden = !list.hidden;
    if (!list.hidden) renderList();
  }
  function renderList() {
    if (list.hidden) return;
    list.innerHTML = `<div class="row" style="margin:0 0 6px"><b>${esc(symbol || "")} 的图形（${drawings.length}）</b><button style="flex:0 0 auto" id="dlClose">×</button></div>` +
      (drawings.map((d, i) => `<div class="row" style="margin:2px 0">
        <span style="flex:2"><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${esc(d.color)}"></span> ${esc(NAMES[d.type])}${d.text ? "：" + esc(d.text.slice(0, 12)) : ""}</span>
        <button style="flex:0 0 auto" data-i="${i}" data-a="sel">选中</button>
        <button style="flex:0 0 auto" data-i="${i}" data-a="vis">${d.hidden ? "显示" : "隐藏"}</button>
        <button class="danger" style="flex:0 0 auto" data-i="${i}" data-a="del">×</button></div>`).join("") || '<div class="muted">还没有图形。左侧工具栏选择工具后在图上点击。</div>');
    document.getElementById("dlClose").onclick = toggleList;
    list.querySelectorAll("[data-a]").forEach(b => b.onclick = () => {
      const d = drawings[+b.dataset.i];
      if (b.dataset.a === "sel") { selected = d; renderProps(); }
      if (b.dataset.a === "vis") { d.hidden = !d.hidden; changed(); }
      if (b.dataset.a === "del") removeDrawing(d);
      dirty = true; renderList();
    });
  }

  function removeDrawing(d) {
    drawings = drawings.filter(x => x !== d);
    if (selected === d) { selected = null; renderProps(); }
    changed();
  }

  // ------------------------------------------------------------ persistence
  function changed() {
    dirty = true; renderList();
    // never overwrite the server copy with a list that failed to load
    if (!loaded) { console.warn("图形列表未加载成功，本次修改不会保存"); return; }
    const sym = symbol, snapshot = JSON.parse(JSON.stringify(drawings));
    clearTimeout(saveTimers.get(sym));
    saveTimers.set(sym, setTimeout(async () => {
      saveTimers.delete(sym);
      try { await api("/api/drawings?symbol=" + encodeURIComponent(sym), { method: "PUT", body: JSON.stringify(snapshot) }); }
      catch (e) { console.warn("保存图形失败", e); }
    }, 600));
  }
  async function load(sym) {
    loaded = false; drawings = []; selected = null; renderProps(); renderList(); dirty = true;
    let list;
    try { list = await api("/api/drawings?symbol=" + encodeURIComponent(sym)); } catch (e) { list = null; }
    if (sym !== symbol) return;            // a newer symbol was opened meanwhile
    drawings = Array.isArray(list) ? list.filter(d => d && typeof d === "object" && Array.isArray(d.points)) : [];
    loaded = Array.isArray(list);
    selected = null; renderProps(); renderList(); dirty = true;
  }

  // ------------------------------------------------------------ public API
  window.Drawings = {
    attach(c, s) {
      chart = c; series = s;
      chart.timeScale().subscribeVisibleLogicalRangeChange(() => { dirty = true; });
      chart.subscribeCrosshairMove(() => { dirty = true; });
      resize();
    },
    setBars(b, interval, sym) {
      bars = b; times = b.map(x => x.time);
      const n = times.length;
      const diffs = [];
      for (let i = Math.max(1, n - 60); i < n; i++) diffs.push(times[i] - times[i - 1]);
      diffs.sort((a, c) => a - c);
      step = diffs.length ? diffs[Math.floor(diffs.length / 2)] : 86400;
      if (sym !== symbol) { symbol = sym; load(sym); }
      dirty = true;
    },
    get all() { return drawings; },
  };
  new ResizeObserver(resize).observe(area);
  renderToolbar();
})();

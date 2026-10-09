// Face reset audit -- single page, no build step. See ../serve.py for the API.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const pct = (x) => `${Math.round((x || 0) * 100)}%`;

const S = {
  view: "people", items: [], cur: 0, status: null, people: [],
  detail: null, selected: new Set(), anchor: null, hover: null,
  faceIndex: new Map(),   // every face rendered, for the whole-photo view
};

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

function toast(msg, ms = 2500) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => t.classList.remove("show"), ms);
}

// ---------------------------------------------------------------- faces --
// A face crop is the asset preview as a CSS background, scaled and shifted so
// a square around the bounding box fills the element. Percentages make it
// independent of the preview's actual pixel size.
const lazy = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    e.target.style.backgroundImage = `url(/img/${e.target.dataset.asset})`;
    lazy.unobserve(e.target);
  }
}, { rootMargin: "400px" });

function faceHTML(f) {
  S.faceIndex.set(f.id, f);
  const [x1, y1, x2, y2] = f.box, [W, H] = f.size;
  let side = Math.min(Math.max(x2 - x1, y2 - y1) * 1.5, W, H);
  let x = Math.min(Math.max((x1 + x2) / 2 - side / 2, 0), W - side);
  let y = Math.min(Math.max((y1 + y2) / 2 - side / 2, 0), H - side);
  const px = W > side ? (x / (W - side)) * 100 : 0;
  const py = H > side ? (y / (H - side)) * 100 : 0;
  const style = `background-size:${(W / side) * 100}% ${(H / side) * 100}%;` +
                `background-position:${px}% ${py}%`;
  return `<div class="face" data-asset="${f.asset}" data-face="${f.id}" style="${style}"
               title="${esc(f.file)}"></div>`;
}

function hydrate(root = document) {
  $$(".face[data-asset]", root).forEach((el) => lazy.observe(el));
}

function showPhoto(f) {
  const d = $("#photo"), img = $("img", d), box = $(".box", d);
  img.onload = () => {
    const sx = img.clientWidth / f.size[0], sy = img.clientHeight / f.size[1];
    Object.assign(box.style, {
      left: `${8 + f.box[0] * sx}px`, top: `${8 + f.box[1] * sy}px`,
      width: `${(f.box[2] - f.box[0]) * sx}px`, height: `${(f.box[3] - f.box[1]) * sy}px`,
    });
  };
  img.src = `/img/${f.asset}`;
  $("p", d).textContent = `${f.file} — was ${f.oldName ?? "?"}, now ${f.newName ?? "?"}`;
  d.showModal();
}

// --------------------------------------------------------------- shell --
async function loadStatus() {
  S.status = await api("/api/status");
  const m = $("#mode"), s = S.status;
  m.className = "badge " + (s.dryRun ? "dry" : "live");
  m.textContent = s.simulated ? "SIMULATED · dry run" : s.dryRun ? "DRY RUN" : `LIVE · ${s.server}`;
  m.title = `journal: ${s.journal.entries} entries, ${s.journal.failed} failed`;
}

async function show(view) {
  S.view = view;
  S.cur = 0;
  $$("nav button").forEach((b) => b.setAttribute("aria-current", b.dataset.view === view));
  await reload();
}

async function reload(keepCur = false) {
  const cur = keepCur ? S.cur : 0;
  await VIEWS[S.view].load();
  S.cur = Math.min(cur, Math.max(0, S.items.length - 1));
  renderList();
  await select(S.cur);
}

function renderList() {
  const v = VIEWS[S.view];
  let html = "", lastGroup = null;
  S.items.forEach((it, i) => {
    const g = v.group?.(it);
    if (g && g !== lastGroup) { html += `<div class="group">${esc(g)}</div>`; lastGroup = g; }
    html += `<div class="item${i === S.cur ? " cur" : ""}" data-i="${i}">${v.row(it)}</div>`;
  });
  $("#list").innerHTML = html || `<p class="muted" style="padding:12px">${v.empty}</p>`;
}

async function select(i) {
  if (!S.items.length) { $("#detail").innerHTML = ""; return; }
  S.cur = i;
  $$("#list .item").forEach((el) => el.classList.toggle("cur", +el.dataset.i === i));
  $(`#list .item[data-i="${i}"]`)?.scrollIntoView({ block: "nearest" });
  S.selected.clear();
  S.anchor = null;
  await VIEWS[S.view].detail(S.items[i]);
  hydrate($("#detail"));
}

function move(d) {
  if (S.items.length) select(Math.min(Math.max(S.cur + d, 0), S.items.length - 1));
}

// -------------------------------------------------------------- people --
const KIND_LABEL = { confident: "Confident", survivor: "Survivors (kept their name)",
  split: "Split over clusters", blend: "Blended with others", weak: "Too little to judge",
  lost: "No faces assigned" };

const people = {
  empty: "No after-reset data yet. Import it with 01_import.py --label after.",
  async load() {
    const r = await api("/api/pairings");
    S.items = r.pairings;
  },
  group: (p) => KIND_LABEL[p.kind],
  // "auto" = confident enough for Apply all; survivors can be too.
  row(p) {
    const st = p.decision?.status;
    return `<span>${esc(p.old.name || "(unnamed)")}</span>
            <span class="${st ? "st-" + st : "muted"}">${st || ""}</span>
            <span class="sub">${p.total} faces · <span class="kind ${p.kind}">${p.kind}</span>${p.confident ? ' · <span class="st-applied">auto</span>' : ""}</span>`;
  },
  async detail(p) {
    const dec = p.decision, applied = dec?.status === "applied";
    const ticked = new Set(dec?.targets?.length ? dec.targets : p.targets);
    const f = dec?.fields && Object.keys(dec.fields).length ? dec.fields : {
      name: p.old.name, birthDate: p.old.birthDate, isHidden: p.old.isHidden,
      isFavorite: p.old.isFavorite,
    };
    S.detail = { p, ticked };
    const rows = p.clusters.slice(0, 6).map((c, i) => `
      <tr class="${ticked.has(c.id) ? "on" : ""}" data-c="${c.id}">
        <td><label><input type="checkbox" ${ticked.has(c.id) ? "checked" : ""} ${applied ? "disabled" : ""}>
            <kbd>${i + 1}</kbd></label></td>
        <td class="num">${c.faces}</td><td class="num">${pct(c.coverage)}</td>
        <td class="num">${pct(c.purity)}</td><td class="num">${c.size}</td>
        <td>${c.current?.name ? `named <b>${esc(c.current.name)}</b><br>` : ""}
            ${c.id === p.old.id ? "<b>the surviving person</b><br>" : ""}
            ${c.others.map((o) => `${esc(o.name || "(unnamed)")} ×${o.faces}`).join(", ") || '<span class="muted">—</span>'}</td>
        <td><div class="strip">${(p.samples[c.id] || []).map(faceHTML).join("")}</div></td>
      </tr>`).join("");
    const more = p.clusters.length > 6 ?
      `<p class="muted">${p.clusters.length - 6} more clusters with a face or two each — strays, handled in Face review.</p>` : "";
    $("#detail").innerHTML = `
      <div class="person-head">
        <img src="/thumb/${p.old.id}" alt="">
        <div>
          <h2>${esc(p.old.name || "(unnamed)")}${p.old.nameFromTag ? ' <span class="kind">name from Google tag</span>' : ""}</h2>
          <div><span class="kind ${p.kind}">${p.kind}</span> ${esc(p.why)}</div>
          <div class="muted">${p.total} recognised faces before · ${p.unassigned} now unassigned</div>
          ${dec ? `<div class="st-${dec.status}">${dec.status}${dec.error ? `: <span class="error">${esc(dec.error)}</span>` : ""}</div>` : ""}
        </div>
      </div>
      <h3>Where the faces went — tick the clusters that are this person</h3>
      <table class="clusters">
        <tr><th></th><th class="num">faces</th><th class="num">coverage</th><th class="num">purity</th>
            <th class="num">size</th><th>also contains</th><th>sample of this person's faces there</th></tr>
        ${rows}
      </table>${more}
      <p class="muted">Coverage: share of this person's faces in the cluster. Purity: share of the cluster's
        faces from named people that were this person. The first ticked cluster is kept; the others merge into it.</p>
      <h3>Restore</h3>
      <div class="fields">
        <label for="f-name">Name</label><input id="f-name" type="text" value="${esc(f.name)}" ${applied ? "disabled" : ""}>
        <label for="f-birth">Birthday</label><input id="f-birth" type="date" value="${esc(f.birthDate || "")}" ${applied ? "disabled" : ""}>
        <label for="f-hidden">Hidden</label><input id="f-hidden" type="checkbox" ${f.isHidden ? "checked" : ""} ${applied ? "disabled" : ""}>
        <label for="f-fav">Favourite</label><input id="f-fav" type="checkbox" ${f.isFavorite ? "checked" : ""} ${applied ? "disabled" : ""}>
      </div>
      <div class="toolbar">
        ${applied ? '<span class="st-applied">Applied. Reverse with undo.py.</span>' : `
          <button class="primary" id="apply"><kbd>Enter</kbd> Apply</button>
          <button id="skip"><kbd>s</kbd> Skip</button>
          ${dec ? '<button id="undecide"><kbd>u</kbd> Un-decide</button>' : ""}`}
        <span style="flex:1"></span>
        <button id="apply-all"><kbd>Shift</kbd>+<kbd>A</kbd> Apply all confident</button>
      </div>`;
    $$("table.clusters tr[data-c] input").forEach((cb) =>
      cb.addEventListener("change", () => people.toggle(cb.closest("tr").dataset.c)));
    $("#apply")?.addEventListener("click", people.apply);
    $("#skip")?.addEventListener("click", people.skip);
    $("#undecide")?.addEventListener("click", people.undecide);
    $("#apply-all").addEventListener("click", people.applyAll);
  },
  toggle(cid) {
    const { ticked, p } = S.detail;
    if (p.decision?.status === "applied") return;
    ticked.has(cid) ? ticked.delete(cid) : ticked.add(cid);
    const tr = $(`tr[data-c="${cid}"]`);
    tr.classList.toggle("on", ticked.has(cid));
    $("input", tr).checked = ticked.has(cid);
  },
  targets() {
    // Order matters: the first is the cluster that survives the merge. Keep
    // the proposal's order (survivor / largest first) for the ticked ones.
    const { ticked, p } = S.detail;
    const order = [p.old.id, ...p.clusters.map((c) => c.id)];
    return order.filter((c, i) => ticked.has(c) && order.indexOf(c) === i);
  },
  fields() {
    return {
      name: $("#f-name").value.trim(), birthDate: $("#f-birth").value || null,
      isHidden: $("#f-hidden").checked, isFavorite: $("#f-fav").checked,
    };
  },
  async apply() {
    if (S.busy) return toast("Still working on the previous request…");
    const targets = people.targets();
    if (!targets.length) return toast("Tick at least one cluster");
    S.busy = true;
    $("#apply").disabled = true;
    // A merge first records every face it moves (for undo): for a person with
    // thousands of photos that takes a while, so say so instead of looking stuck.
    toast(targets.length > 1 ? `Merging ${targets.length - 1} cluster(s) — recording faces for undo…`
                             : "Applying…", 600000);
    try {
      await api(`/api/pairings/${S.detail.p.old.id}`, { action: "apply", targets, fields: people.fields() });
      toast(`Applied ${S.detail.p.old.name || "person"}`);
      await reload(true);
      move(1);
    } catch (e) { toast(e.message, 6000); await reload(true); }
    finally { S.busy = false; }
  },
  async skip() {
    await api(`/api/pairings/${S.detail.p.old.id}`, { action: "skip" });
    await reload(true);
    move(1);
  },
  async undecide() {
    try { await api(`/api/pairings/${S.detail.p.old.id}`, { action: "undecide" }); }
    catch (e) { return toast(e.message, 5000); }
    await reload(true);
  },
  async applyAll() {
    if (S.busy) return toast("Still working on the previous request…");
    const todo = S.items.filter((p) => p.confident && !p.decision);
    if (!todo.length) return toast("No undecided confident pairings");
    if (!confirm(`Apply ${todo.length} confident pairings?\n\n` +
                 todo.map((p) => `${p.old.name || "(unnamed)"} — ${p.why}`).join("\n"))) return;
    let ok = 0, failed = 0;
    S.busy = true;
    for (const p of todo) {
      try {
        await api(`/api/pairings/${p.old.id}`, { action: "apply", targets: p.targets, fields: {
          name: p.old.name, birthDate: p.old.birthDate, isHidden: p.old.isHidden, isFavorite: p.old.isFavorite } });
        ok++;
      } catch (e) { failed++; }
      toast(`Applying… ${ok + failed}/${todo.length}`);
    }
    S.busy = false;
    toast(`Applied ${ok}${failed ? `, ${failed} failed — see the list` : ""}`, 5000);
    await reload(true);
  },
  key(e) {
    if (/^[1-6]$/.test(e.key)) {
      const c = S.detail.p.clusters[+e.key - 1];
      if (c) people.toggle(c.id);
    } else if (e.key === "Enter") people.apply();
    else if (e.key === "s") people.skip();
    else if (e.key === "u") people.undecide();
    else if (e.key === "A") people.applyAll();
    else return false;
    return true;
  },
};

// ---------------------------------------------------------------- tags --
const tags = {
  empty: "No People/* tags found.",
  async load() {
    S.items = (await api("/api/tags")).tags;
    S.people = (await api("/api/people")).people;
  },
  group: (t) => (t.verdict ? "Decided" : "To check"),
  row(t) {
    const v = t.verdict, a = t.stats.agreement;
    const st = !v ? "" : v.trusted === 1 ? "trusted" : v.trusted === 0 ? "untrusted" : "";
    return `<span>${esc(t.name)}</span><span class="st-${st}">${st}</span>
            <span class="sub">${t.stats.tagged} photos · ${t.personName ? `→ ${esc(t.personName)} · ${a === undefined ? "" : pct(a) + " agree"}` : "no person matched"}</span>`;
  },
  async detail(t) {
    const person = S.detail?.tag === t.id && S.detail.person !== undefined ? S.detail.person : t.person;
    S.detail = { tag: t.id, person, t };
    const d = await api(`/api/tags/${t.id}${person ? `?person=${person}` : ""}`);
    const st = d.stats;
    const cands = t.candidates.map((c, i) => `
      <button data-p="${c.id}" class="${c.id === person ? "primary" : ""}"><kbd>${i + 1}</kbd>
        ${esc(c.name || "(unnamed cluster)")} <span class="muted">on ${pct(c.share)} of photos${c.match ? ` · ${c.match} matches` : ""}</span></button>`).join("");
    const opts = S.people.map((p) => `<option value="${p.id}" ${p.id === person ? "selected" : ""}>${esc(p.name)}</option>`).join("");
    const grid = (faces, label) => faces?.length ? `<div class="grid">${faces.map((f) => `
      <div class="card" data-face="${f.id}">${faceHTML(f)}<div class="lbl">${label(f)}</div></div>`).join("")}</div>`
      : '<p class="muted">None.</p>';
    $("#detail").innerHTML = `
      <h2>${esc(t.value)}</h2>
      <h3>Which person is this tag?</h3>
      <div class="row">${cands} <select id="pick"><option value="">other person…</option>${opts}</select></div>
      ${person ? `
      <div class="stats">
        <div><b>${st.tagged}</b>photos tagged</div>
        <div><b>${st.both}</b>tag and face agree</div>
        <div><b>${st.tagOnly}</b>tagged, face is someone else</div>
        <div><b>${st.tagNoFaces}</b>tagged, no face found</div>
        <div><b>${st.faceOnly}</b>face, no tag (Takeout photos)</div>
        <div><b>${pct(st.agreement)}</b>agreement</div>
      </div>
      <div class="toolbar">
        <button class="primary" id="yes"><kbd>y</kbd> Trust this tag</button>
        <button id="no"><kbd>n</kbd> Don't trust it</button>
        ${t.verdict ? '<button id="undo"><kbd>u</kbd> Un-decide</button>' : ""}
      </div>
      <h3>Tagged, but the face found is someone else (${st.tagOnly})</h3>
      ${grid(d.examples.tagOnly, (f) => `face: ${esc(f.oldName)}`)}
      <h3>Face, but no tag, on Takeout photos (${st.faceOnly})</h3>
      ${grid(d.examples.faceOnly, (f) => esc(f.file))}
      <h3>Agreeing, for reference</h3>
      ${grid(d.examples.both, (f) => esc(f.file))}` : '<p class="muted">Pick the person this tag means.</p>'}`;
    $$("#detail [data-p]").forEach((b) => b.addEventListener("click", () => tags.pick(b.dataset.p)));
    $("#pick").addEventListener("change", (e) => e.target.value && tags.pick(e.target.value));
    $("#yes")?.addEventListener("click", () => tags.verdict(1));
    $("#no")?.addEventListener("click", () => tags.verdict(0));
    $("#undo")?.addEventListener("click", () => tags.verdict(null));
  },
  async pick(pid) {
    S.detail.person = pid;
    await tags.detail(S.detail.t);
    hydrate($("#detail"));
  },
  async verdict(trusted) {
    await api(`/api/tags/${S.detail.tag}`, { person: S.detail.person, trusted });
    S.detail = null;
    await reload(true);
    if (trusted !== null) move(1);
  },
  key(e) {
    if (/^[1-4]$/.test(e.key)) {
      const c = S.detail.t.candidates[+e.key - 1];
      if (c) tags.pick(c.id);
    } else if (e.key === "y" && S.detail.person) tags.verdict(1);
    else if (e.key === "n" && S.detail.person) tags.verdict(0);
    else if (e.key === "u") tags.verdict(null);
    else return false;
    return true;
  },
};

// -------------------------------------------------------------- review --
const review = {
  empty: "Nothing to review — apply some pairings first.",
  async load() {
    const r = await api("/api/review");
    S.items = r.people;
    S.people = (await api("/api/people")).people;
  },
  group: (p) => (p.todo ? "To review" : "Done"),
  row: (p) => `<span>${esc(p.name)}</span><span class="muted">${p.todo || "✓"}</span>
               <span class="sub">${p.left} left · ${p.joined} joined</span>`,
  async detail(p) {
    const d = await api(`/api/review/${p.id}`);
    S.detail = d;
    S.faces = new Map([...d.left, ...d.joined].map((f) => [f.id, f]));
    const card = (f, side) => {
      // Green when a trusted tag on the photo names this person.
      const tagged = f.tags.length ? `<div class="tagchip ${f.tags.includes(d.name) ? "" : "disagree"}">tag: ${esc(f.tags.join(", "))}</div>` : "";
      const lbl = side === "left" ? `→ ${esc(f.newName)}` : `← ${esc(f.oldName)}`;
      const done = f.done ? `<div class="lbl muted">${f.done.action}${f.done.error ? " (failed)" : ""}</div>` : "";
      return `<div class="card${f.done && !f.done.error ? " done" : ""}" data-face="${f.id}">
        ${faceHTML(f)}<div class="lbl" title="${esc(f.file)}">${lbl}</div>${tagged}${done}</div>`;
    };
    $("#detail").innerHTML = `
      <h2>${esc(d.name)}</h2>
      <p class="muted">Select faces, then <kbd>o</kbd> put them back on their old person, or <kbd>n</kbd> keep the new assignment.
        Immich cannot un-assign a face, so a wrong face can only be moved to someone else.</p>
      <div class="toolbar">
        <button id="restore"><kbd>o</kbd> Restore old person</button>
        <button id="keep"><kbd>n</kbd> Keep new</button>
        <select id="moveto"><option value="">Move to…</option>${S.people.filter((x) => x.paired).map((x) =>
          `<option value="${x.id}">${esc(x.name)}</option>`).join("")}</select>
        <span class="muted" id="selcount"></span>
      </div>
      <h3>Left ${esc(d.name)} — was this person, now elsewhere (${d.left.length})</h3>
      <div class="grid" data-side="left">${d.left.map((f) => card(f, "left")).join("") || '<p class="muted">None.</p>'}</div>
      <h3>Joined ${esc(d.name)} — now this person, was not before (${d.joined.length})</h3>
      <div class="grid" data-side="joined">${d.joined.map((f) => card(f, "joined")).join("") || '<p class="muted">None.</p>'}</div>`;
    $("#restore").addEventListener("click", () => review.decide("restore"));
    $("#keep").addEventListener("click", () => review.decide("keep"));
    $("#moveto").addEventListener("change", (e) => e.target.value && review.decide("move", e.target.value));
    $$("#detail .card").forEach((c) => c.addEventListener("click", (e) => review.click(c, e)));
    review.paintSel();
  },
  click(card, e) {
    const cards = $$("#detail .card"), i = cards.indexOf(card), id = card.dataset.face;
    if (e.shiftKey && S.anchor !== null) {
      const [a, b] = [Math.min(S.anchor, i), Math.max(S.anchor, i)];
      cards.slice(a, b + 1).forEach((c) => S.selected.add(c.dataset.face));
    } else {
      S.selected.has(id) ? S.selected.delete(id) : S.selected.add(id);
      S.anchor = i;
    }
    review.paintSel();
  },
  paintSel() {
    $$("#detail .card").forEach((c) => c.classList.toggle("sel", S.selected.has(c.dataset.face)));
    const n = S.selected.size;
    $("#selcount").textContent = n ? `${n} selected` : "";
  },
  async decide(how, to) {
    if (S.busy) return toast("Still working on the previous request…");
    const ids = [...S.selected];
    if (!ids.length) return toast("Select faces first (a = all pending)");
    const paired = new Set(S.people.filter((x) => x.paired).map((x) => x.id));
    const decisions = [], skipped = [];
    for (const id of ids) {
      const f = S.faces.get(id);
      if (how === "keep") decisions.push({ face: id, action: "keep" });
      else if (how === "move") decisions.push({ face: id, action: "move", person: to });
      else if (paired.has(f.old)) decisions.push({ face: id, action: "move", person: f.old });
      else skipped.push(id);
    }
    if (skipped.length) toast(`${skipped.length} face(s) had no paired old person — left selected`, 4000);
    if (!decisions.length) return;
    S.busy = true;
    toast(`Moving ${decisions.length} face(s)…`, 600000);
    let r;
    try { r = await api("/api/faces", { decisions }); }
    finally { S.busy = false; }
    toast(`${r.ok} done${r.failed.length ? `, ${r.failed.length} failed: ${r.failed[0].error}` : ""}`, 4000);
    await reload(true);
    skipped.forEach((id) => S.selected.add(id));
    review.paintSel();
  },
  key(e) {
    if (e.key === "a") {
      $$("#detail .card:not(.done)").forEach((c) => S.selected.add(c.dataset.face));
      review.paintSel();
    } else if (e.key === "Escape") { S.selected.clear(); review.paintSel(); }
    else if (e.key === "o") review.decide("restore");
    else if (e.key === "n") review.decide("keep");
    else return false;
    return true;
  },
};

const VIEWS = { people, tags, review };

// ------------------------------------------------------------- wiring --
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input[type=text], input[type=date], select") && e.key !== "Escape") return;
  if (e.ctrlKey || e.metaKey || e.altKey || $("dialog[open]")) return;
  if (VIEWS[S.view].key?.(e)) return e.preventDefault();
  const k = e.key;
  if (k === "j" || k === "ArrowDown") move(1);
  else if (k === "k" || k === "ArrowUp") move(-1);
  else if (k === "p") show("people");
  else if (k === "t") show("tags");
  else if (k === "r") show("review");
  else if (k === "?") $("#help").showModal();
  else if (k === "v" && S.hover) showHover();
  else return;
  e.preventDefault();
});

function showHover() {
  const f = S.faceIndex.get(S.hover);
  if (f) showPhoto(f);
}
document.addEventListener("mouseover", (e) => {
  S.hover = e.target.closest("[data-face]")?.dataset.face ?? null;
});
document.addEventListener("dblclick", (e) => {
  const id = e.target.closest("[data-face]")?.dataset.face;
  if (id) { S.hover = id; showHover(); }
});
$("#list").addEventListener("click", (e) => {
  const it = e.target.closest(".item");
  if (it) select(+it.dataset.i);
});
$$("nav button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));
$("#help-btn").addEventListener("click", () => $("#help").showModal());

(async () => {
  await loadStatus();
  await show(S.status.hasAfter ? "people" : "tags");
})();

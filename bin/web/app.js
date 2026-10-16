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
  const low = s.minScore != null && s.minScore < s.normalScore;
  const b = $("#banner");
  b.hidden = !low;
  if (low) b.textContent = `Min detection score is ${s.minScore} — new uploads pick up junk faces while it is lowered. ` +
                           `The usual is ${s.normalScore}.`;
}
setInterval(() => loadStatus().catch(() => {}), 15000);

async function show(view) {
  S.view = view;
  history.replaceState(null, "", `#${view}`);
  S.cur = 0;
  $$("nav button").forEach((b) => b.setAttribute("aria-current", b.dataset.view === view));
  await reload();
}

async function reload(keepCur = false) {
  // Lists re-sort as work gets done, so keep the same item, not the same
  // position; fall back to the position when the item has left the list.
  const cur = keepCur ? S.cur : 0, curId = keepCur ? S.items[S.cur]?.id : undefined;
  await VIEWS[S.view].load();
  const same = curId === undefined ? -1 : S.items.findIndex((it) => it.id === curId);
  S.cur = same >= 0 ? same : Math.min(cur, Math.max(0, S.items.length - 1));
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
  $("#list").innerHTML = (v.head?.() ?? "") + (html || `<p class="muted" style="padding:12px">${v.empty}</p>`);
  v.wire?.();
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
            <span class="sub">${t.stats.tagged} photos · ${t.personName ? `→ ${esc(t.personName)} · ${a === undefined ? "" : pct(a) + " face found"}` : "no person matched"}</span>`;
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
    const photos = (list) => list?.length ? `<div class="grid">${list.map((a) => `
      <a class="card" href="${esc(S.status.server)}/photos/${a.asset}" target="_blank" rel="noopener">
        <div class="face photo" data-asset="${a.asset}"></div>
        <div class="lbl" title="${esc(a.file)}">${a.faces.length ? `also: ${esc(a.faces.join(", "))}` : esc(a.file)}</div></a>`).join("")}</div>`
      : '<p class="muted">None.</p>';
    $("#detail").innerHTML = `
      <h2>${esc(t.value)}</h2>
      <h3>Which person is this tag?</h3>
      <div class="row">${cands} <select id="pick"><option value="">other person…</option>${opts}</select></div>
      ${person ? `
      <div class="stats">
        <div><b>${st.tagged}</b>photos tagged</div>
        <div><b>${st.both}</b>tag and face agree</div>
        <div><b>${st.tagOnly + st.tagNoFaces}</b>tagged, face not detected</div>
        <div><b>${st.faceOnly}</b>face, no tag (Takeout photos)</div>
        <div><b>${pct(st.agreement)}</b>of tagged photos have the face</div>
      </div>
      <div class="toolbar">
        <button class="primary" id="yes"><kbd>y</kbd> Trust this tag</button>
        <button id="no"><kbd>n</kbd> Don't trust it</button>
        ${t.verdict ? '<button id="undo"><kbd>u</kbd> Un-decide</button>' : ""}
      </div>
      <h3>Tagged, but no face of this person detected (${st.tagOnly} with other faces, ${st.tagNoFaces} with none)</h3>
      <p class="muted">Usually the person is there — in the background, small or turned away — and Immich
        never detected the face. These do not count against the tag. Only distrust it if the person is
        really not in the photo. Click a photo to open it in Immich.</p>
      ${photos(d.examples.tagOnly)}
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
    S.side = new Map([...d.left.map((f) => [f.id, "left"]), ...d.joined.map((f) => [f.id, "joined"])]);
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
      <p class="muted">Select faces, then answer for all of them: <kbd>y</kbd> they are ${esc(d.name)}, or
        <kbd>x</kbd> they are not. Immich cannot un-assign a face, so a face that is nobody you know
        can only be moved to someone else (or left where it is).</p>
      <div class="toolbar">
        <button id="yes" class="primary"><kbd>y</kbd> This is ${esc(d.name)}</button>
        <button id="no"><kbd>x</kbd> Not ${esc(d.name)}</button>
        <button id="notface" title="Delete the face detection: food, a hand, a pattern. Not undoable."><kbd>d</kbd> Not a face</button>
        <select id="moveto"><option value="">Move to…</option>${S.people.filter((x) => x.paired).map((x) =>
          `<option value="${x.id}">${esc(x.name)}</option>`).join("")}</select>
        <span class="muted" id="selcount"></span>
      </div>
      <h3>Left ${esc(d.name)} — was this person, now elsewhere (${d.left.length})</h3>
      <div class="grid" data-side="left">${d.left.map((f) => card(f, "left")).join("") || '<p class="muted">None.</p>'}</div>
      <h3>Joined ${esc(d.name)} — now this person, was not before (${d.joined.length})</h3>
      <div class="grid" data-side="joined">${d.joined.map((f) => card(f, "joined")).join("") || '<p class="muted">None.</p>'}</div>`;
    $("#yes").addEventListener("click", () => review.decide("is"));
    $("#no").addEventListener("click", () => review.decide("isnt"));
    $("#moveto").addEventListener("change", (e) => e.target.value && review.decide("move", e.target.value));
    $("#notface").addEventListener("click", notFace);
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
    // "is"/"isnt" answer the question "is this face the person on screen?";
    // what that means in Immich depends on the section the face is in:
    //   left   (was this person, now elsewhere): is -> move back, isnt -> leave
    //   joined (now this person, was not):       is -> leave,     isnt -> back to old person
    const paired = new Set(S.people.filter((x) => x.paired).map((x) => x.id));
    const me = S.detail.id, decisions = [], skipped = [];
    for (const id of ids) {
      const f = S.faces.get(id), side = S.side.get(id);
      if (how === "move") decisions.push({ face: id, action: "move", person: to });
      else if ((how === "is") === (side === "joined")) decisions.push({ face: id, action: "keep" });
      else if (how === "is") decisions.push({ face: id, action: "move", person: me });
      else if (paired.has(f.old)) decisions.push({ face: id, action: "move", person: f.old });
      else skipped.push(id);
    }
    if (skipped.length) toast(`${skipped.length} face(s) were nobody before — pick someone with "Move to…", ` +
                              `or leave them; they stay selected`, 6000);
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
    else if (e.key === "y") review.decide("is");
    else if (e.key === "x") review.decide("isnt");
    else if (e.key === "d") notFace();
    else return false;
    return true;
  },
};

// ------------------------------------------------------------ featured --
// Score = size^w × recency^(1-w): a weighted geometric mean, so a photo has to
// be decent on both counts -- a huge face from 2009 does not beat everything.
const SIZE_FULL = 0.4;        // face 40% of the photo's height scores 1
const HALF_LIFE_YEARS = 3;    // recency halves every 3 years before the latest photo
const MIN_AGE_YEARS = 5;      // with a birthday known, younger photos are left out

const FEATURED_PAGE = 60;     // candidates shown at first, and added by "Show more"

const featured = {
  empty: "No applied pairings yet.",
  weight: 0.5,
  limit: FEATURED_PAGE,
  async load() { S.items = (await api("/api/featured")).people; },
  group: (p) => (p.set ? "Featured photo set" : "To choose"),
  row: (p) => `<span>${esc(p.name)}</span><span class="st-applied">${p.set ? "set" : ""}</span>`,
  async detail(p) {
    $("#detail").innerHTML = `<h2>${esc(p.name)}</h2><p class="muted">Loading this person's photos from Immich…</p>`;
    const d = await api(`/api/featured/${p.id}`);
    S.detail = d;
    featured.limit = FEATURED_PAGE;
    featured.render();
  },
  score(d) {
    const years = (a, b) => (Date.parse(a) - Date.parse(b)) / 3.15576e10;
    const latest = d.faces.reduce((m, f) => (f.date > m ? f.date : m), "");
    const w = featured.weight;
    return d.faces
      .filter((f) => !d.birthDate || years(f.date, d.birthDate) >= MIN_AGE_YEARS)
      .map((f) => {
        const size = Math.min(f.height / SIZE_FULL, 1);
        const recency = 0.5 ** (Math.max(years(latest, f.date), 0) / HALF_LIFE_YEARS);
        return { ...f, sizeScore: size, recency, score: size ** w * recency ** (1 - w) };
      })
      .sort((a, b) => b.score - a.score);
  },
  render() {
    const d = S.detail, all = featured.score(d), ranked = all.slice(0, featured.limit);
    const excluded = d.birthDate ? d.faces.length - all.length : 0;
    $("#detail").innerHTML = `
      <div class="person-head">
        <img src="/live-thumb/${d.person}?t=${Date.now()}" alt="current featured photo">
        <div>
          <h2>${esc(d.name)}</h2>
          <div class="muted">Current featured photo on the left · ${d.faces.length} faces on timeline photos
            ${excluded ? ` · ${excluded} taken before age ${MIN_AGE_YEARS} left out` : ""}</div>
        </div>
      </div>
      <div class="toolbar">
        <label for="weight">Recency</label>
        <input id="weight" type="range" min="0" max="1" step="0.05" value="${featured.weight}">
        <label for="weight">Face size</label>
        <span class="muted">— click a photo, or press <kbd>1</kbd>–<kbd>9</kbd>, to make it the featured one</span>
        <span style="flex:1"></span>
        <button id="keep-current"><kbd>Enter</kbd> Keep current</button>
      </div>
      <div class="grid">${ranked.map((f, i) => `
        <div class="card" data-face="${f.id}" data-asset="${f.asset}">${faceHTML(f)}
          <div class="lbl">${i < 9 ? `<kbd>${i + 1}</kbd> ` : ""}${f.date.slice(0, 4)} · face ${Math.round(f.height * 100)}%</div>
          <div class="lbl muted">score ${f.score.toFixed(2)}</div></div>`).join("")}</div>
      ${all.length > ranked.length ? `<div class="toolbar"><button id="more">Show more
        <span class="muted">(${ranked.length} of ${all.length})</span></button></div>` : ""}`;
    hydrate($("#detail"));
    $("#weight").addEventListener("input", (e) => {
      featured.weight = +e.target.value;
      featured.render();
      $("#weight").focus();
    });
    $$("#detail .card").forEach((c) => c.addEventListener("click", () => featured.choose(c.dataset.asset)));
    $("#keep-current").addEventListener("click", featured.keep);
    $("#more")?.addEventListener("click", () => {
      const y = $("#detail").scrollTop;
      featured.limit += FEATURED_PAGE;
      featured.render();
      $("#detail").scrollTop = y;
    });
  },
  async choose(asset) {
    if (S.busy) return;
    S.busy = true;
    try {
      await api(`/api/featured/${S.detail.id}`, { asset });
      toast(`Featured photo set for ${S.detail.name}`, 3000);
      await featured.next();
    } catch (e) { toast(e.message, 6000); }
    finally { S.busy = false; }
  },
  async keep() {
    if (S.busy) return;
    S.busy = true;
    try {
      await api(`/api/featured/${S.detail.id}`, { keep: true });
      toast(`Kept the current photo for ${S.detail.name}`, 3000);
      await featured.next();
    } catch (e) { toast(e.message, 6000); }
    finally { S.busy = false; }
  },
  // Mark this person done and go to the next one still to do: after this
  // one, else from the top.
  async next() {
    S.items[S.cur].set = true;
    renderList();
    const unset = (i) => !S.items[i].set;
    const idx = [...S.items.keys()];
    const next = idx.find((i) => i > S.cur && unset(i)) ?? idx.find(unset);
    if (next === undefined) toast("Every person has a featured photo now", 4000);
    else await select(next);
  },
  key(e) {
    if (/^[1-9]$/.test(e.key)) {
      const card = $$("#detail .card")[+e.key - 1];
      if (card) featured.choose(card.dataset.asset);
      return true;
    }
    if (e.key === "Enter") { featured.keep(); return true; }
    return false;
  },
};

// ---------------------------------------------------- not a face / clusters --
// Deleting a face detection cannot be undone through Immich (no restore
// endpoint), so it is confirmed every time, and kept for false detections.
async function notFace() {
  if (S.busy) return toast("Still working on the previous request…");
  const ids = [...S.selected];
  if (!ids.length) return toast("Select faces first");
  if (!confirm(`Delete ${ids.length} face detection(s)?\n\nThe photos stay; only the face boxes go. ` +
               `Immich cannot restore a deleted face, so only do this for things that are not faces.`)) return;
  S.busy = true;
  toast(`Deleting ${ids.length} face(s)…`, 600000);
  let r;
  try { r = await api("/api/faces", { decisions: ids.map((face) => ({ face, action: "notface" })) }); }
  catch (e) { toast(e.message, 6000); return; }
  finally { S.busy = false; }
  toast(`${r.ok} deleted${r.failed.length ? `, ${r.failed.length} failed: ${r.failed[0].error}` : ""}`, 6000);
  await reload(true);
}

const clusters = {
  empty: "No unclaimed clusters.",
  async load() {
    const r = await api("/api/clusters");
    S.items = r.clusters;
    S.server = r.server;
    S.people = (await api("/api/people")).people;
  },
  row: (c) => `<span class="mono">${c.id.slice(0, 8)}</span><span class="muted">${c.faces}</span>
               <span class="sub">unnamed${c.others ? ` · +${c.others} on other users' photos` : ""}</span>`,
  async detail(c) {
    const d = await api(`/api/clusters/${c.id}`);
    S.detail = d;
    S.faces = new Map(d.faces.map((f) => [f.id, f]));
    const card = (f) => {
      const gone = f.done?.action === "notface" && !f.done.error;
      const lbl = gone ? "deleted" : f.done?.error ? "failed" : f.done ? `→ ${esc(S.people.find((x) => x.id === f.done.target)?.name ?? "moved")}` : "";
      return `<div class="card${f.done && !f.done.error ? " done" : ""}" data-face="${f.id}">
        ${faceHTML(f)}<div class="lbl" title="${esc(f.file)}">${lbl || esc(f.file)}</div></div>`;
    };
    $("#detail").innerHTML = `
      <h2>Unnamed cluster <span class="mono">${d.id.slice(0, 8)}</span></h2>
      <p class="muted">Clusters nobody was paired with, largest first: false detections and unrecognisable
        faces collect here. Select faces, then <kbd>d</kbd> deletes the false detections (not undoable), or
        "Move to…" puts a real face on someone you know.
        <a href="${esc(S.server)}/people/${d.id}" target="_blank" rel="noopener">Open in Immich</a> to hide the cluster.
        ${d.others ? `${d.others} face(s) on other users' photos are not shown — they need that user's key.` : ""}</p>
      <div class="toolbar">
        <button id="notface"><kbd>d</kbd> Not a face</button>
        <select id="moveto"><option value="">Move to…</option>${S.people.filter((x) => x.paired).map((x) =>
          `<option value="${x.id}">${esc(x.name)}</option>`).join("")}</select>
        <span class="muted" id="selcount"></span>
      </div>
      <div class="grid">${d.faces.map(card).join("") || '<p class="muted">None.</p>'}</div>`;
    $("#notface").addEventListener("click", notFace);
    $("#moveto").addEventListener("change", (e) => e.target.value && clusters.move(e.target.value));
    $$("#detail .card").forEach((c) => c.addEventListener("click", (e) => review.click(c, e)));
    review.paintSel();
  },
  async move(to) {
    if (S.busy) return toast("Still working on the previous request…");
    const ids = [...S.selected];
    if (!ids.length) return toast("Select faces first");
    S.busy = true;
    let r;
    try { r = await api("/api/faces", { decisions: ids.map((face) => ({ face, action: "move", person: to })) }); }
    catch (e) { toast(e.message, 6000); return; }
    finally { S.busy = false; }
    toast(`${r.ok} moved${r.failed.length ? `, ${r.failed.length} failed: ${r.failed[0].error}` : ""}`, 4000);
    await reload(true);
  },
  key(e) {
    if (e.key === "a") {
      $$("#detail .card:not(.done)").forEach((c) => S.selected.add(c.dataset.face));
      review.paintSel();
    } else if (e.key === "Escape") { S.selected.clear(); review.paintSel(); }
    else if (e.key === "d") notFace();
    else return false;
    return true;
  },
};

// ------------------------------------------------------- missing faces --
// PLAN.md stage 4: per tag, small rounds of tagged photos without the tagged
// person's face. One verdict per photo; detection rounds pool every tag's
// "not detected" photos and run while the threshold is lowered.
const VERDICTS = { null: "choose…", face: "this face", wrong: "not in photo", notdetected: "not detected", cant: "can't tell" };

const missing = {
  empty: "No tag has a paired person yet.",
  async load() {
    const r = await api("/api/missing");
    const order = ["Detection results to review", "In progress", "Not started", "Finished"];
    S.items = r.tags.sort((a, b) => order.indexOf(missing.group(a)) - order.indexOf(missing.group(b)));
    S.detect = r.detect;
  },
  group(t) {
    const y = t.tally;
    if (y.toReview) return "Detection results to review";
    if (!t.untested) return "Finished";
    return y.checked ? "In progress" : "Not started";
  },
  row(t) {
    const y = t.tally;
    return `<span>${esc(t.name)}</span><span class="muted">${t.untested}</span>
      <span class="sub">→ ${esc(t.personName)} · ✓${y.confirmed} ✗${y.wrong}` +
      `${y.queued ? ` · ${y.queued} queued` : ""}${y.toReview ? ` · ${y.toReview} to review` : ""}</span>`;
  },
  detectPanel() {
    const d = S.detect;
    if (!d) return "";
    const score = d.minScore ?? "?";
    let body;
    if (d.state === "running") {
      body = `Detecting… ${d.done} of ${d.total} photos, ${d.added} face(s) added so far.`;
    } else {
      const last = d.state === "done" ? `Last round: ${d.total} photos, ${d.added} face(s) added. ` :
                   d.state === "error" ? `<span class="error">Last round stopped: ${esc(d.error)}</span> ` : "";
      const how = d.canRun ? `<button id="detect-run" class="primary">Run detection on ${d.queued} photo(s)</button>` :
        `To run: set <i>Administration → Machine Learning → Facial Recognition → Min detection score</i>
         to ${d.maxScore} or less (e.g. 0.4); this updates by itself.`;
      body = `${last}${d.queued} photo(s) to detect — queued and not yet reviewed, from all tags.
        Min detection score: <b>${score}</b>.
        ${d.queued ? how : ""}`;
    }
    return `<div class="panel">${body}
      <button id="m-sync" title="Look up every open photo in Immich again: faces added outside this app count">Re-check against Immich</button></div>`;
  },
  async detail(t) {
    const d = await api(`/api/missing/${t.id}`);
    S.detail = d;
    S.mcur = 0;
    S.verdicts = new Map(d.photos.map((p) => [p.asset, { ...p.default }]));
    const y = d.tally;
    $("#detail").innerHTML = `
      <h2>${esc(d.name)} <span class="muted">→ ${esc(d.personName)}</span></h2>
      <div class="stats">
        <div><b>${y.confirmed}</b>confirmed</div><div><b>${y.wrong}</b>not in photo</div>
        <div><b>${y.queued}</b>queued for detection</div><div><b>${y.undetectable}</b>undetectable</div>
        <div><b>${y.cant}</b>can't tell</div><div><b>${y.present}</b>already had the face</div>
      </div>
      ${missing.detectPanel()}
      ${d.photos.length ? `
      <p class="muted">For each photo: <kbd>1</kbd>–<kbd>9</kbd> or click the face that is ${esc(d.personName)},
        <kbd>w</kbd> not in the photo, <kbd>d</kbd> not detected (queue for detection), <kbd>x</kbd> can't tell.
        <kbd>←</kbd> <kbd>→</kbd> move between photos, <kbd>Enter</kbd> submits the whole round.
        Green = suggested; <i>new</i> = added by detection; faces on named people show their name.
        Photos left on <i>choose…</i> are not submitted and come back later.
        ${d.hasCentroids ? "" : "No fingerprints for this person, so faces are ranked by size only."}</p>
      <div class="toolbar"><button id="m-submit" class="primary"><kbd>Enter</kbd> Submit verdicts</button>
        <span class="muted">${d.photos.filter((p) => p.refreshed).length} from detection, ${d.photos.filter((p) => !p.refreshed).length} new</span></div>
      <div class="mgrid">${d.photos.map((p, i) => missing.card(p, i)).join("")}</div>` :
      `<p class="muted">Nothing to review for this tag${t.untested ? "" : " — every tagged photo is done"}.</p>`}`;
    $("#detect-run")?.addEventListener("click", missing.runDetect);
    $("#m-sync")?.addEventListener("click", missing.sync);
    $("#m-submit")?.addEventListener("click", missing.submit);
    $$("#detail .mcard").forEach((c) => c.addEventListener("dblclick", () =>
      missing.zoom(S.detail.photos[+c.dataset.i])));
    // v / z open the photo under the mouse, if any, rather than the current one.
    S.mhover = null;
    $$("#detail .mcard").forEach((c) => {
      c.addEventListener("mouseenter", () => { S.mhover = +c.dataset.i; });
      c.addEventListener("mouseleave", () => { if (S.mhover === +c.dataset.i) S.mhover = null; });
    });
    $$("#detail .mcard").forEach((c) => c.addEventListener("click", (e) => {
      S.mcur = +c.dataset.i;
      const box = e.target.closest("[data-fid]"), btn = e.target.closest("[data-v]");
      if (box) missing.set(c.dataset.asset, "face", box.dataset.fid);
      else if (btn) missing.set(c.dataset.asset, btn.dataset.v);
      missing.paint();
    }));
    missing.paint();
    if (S.detect?.state === "running") missing.poll();
  },
  card(p, i) {
    const [W, H] = p.faces[0]?.size ?? [1, 1];
    const boxes = p.faces.map((f, n) => {
      const [x1, y1, x2, y2] = f.box, [w, h] = f.size;
      const tag = f.isTarget ? "" : f.named ? esc(f.personName) : f.added ? "new" : "";
      return `<div class="fbox${f.added ? " added" : ""}${f.named && !f.isTarget ? " named" : ""}" data-fid="${f.id}"
        style="left:${x1 / w * 100}%;top:${y1 / h * 100}%;width:${(x2 - x1) / w * 100}%;height:${(y2 - y1) / h * 100}%"
        title="${f.score != null ? `similarity ${f.score}` : "no fingerprint"}"><span>${n + 1}${tag ? ` ${tag}` : ""}</span></div>`;
    }).join("");
    return `<div class="mcard" data-i="${i}" data-asset="${p.asset}">
      <div class="mphoto face" data-asset="${p.asset}" style="aspect-ratio:${W}/${H}">${boxes}</div>
      <div class="mfoot"><span class="lbl" title="${esc(p.file)}">${p.refreshed ? "<b>detection</b> · " : ""}${esc(p.file)}</span>
        <span class="mverdict"></span></div>
      <div class="row">${["wrong", "notdetected", "cant"].map((v) =>
        `<button data-v="${v}"><kbd>${{ wrong: "w", notdetected: "d", cant: "x" }[v]}</kbd> ${VERDICTS[v]}</button>`).join("")}</div>
    </div>`;
  },
  // The photo large, boxes numbered as on the card. Click a box to pick it,
  // anywhere else to toggle 2x; keys keep working (1-9, w/d/x, Esc closes).
  zoom(ph) {
    const d = $("#zoom"), inner = $(".zinner", d);
    const [W, H] = ph.faces[0]?.size ?? [1, 1];
    // Fit the photo to the window, keeping its proportions so the boxes
    // (in percent) stay on the faces; "big" doubles it inside a scroller.
    const fit = Math.min(innerWidth * 0.92 / W, innerHeight * 0.84 / H);
    inner.className = "zinner";
    inner.style.setProperty("--w", `${W * fit}px`);
    inner.style.setProperty("--h", `${H * fit}px`);
    inner.style.backgroundImage = `url(/img/${ph.asset})`;
    inner.innerHTML = $(`#detail .mcard[data-asset="${ph.asset}"] .mphoto`).innerHTML;
    $("p", d).textContent = `${ph.file} — 1–9 or click a face · w / d / x · click the photo to zoom · Esc closes`;
    inner.onclick = (e) => {
      const box = e.target.closest("[data-fid]");
      if (!box) return inner.classList.toggle("big");
      missing.set(ph.asset, "face", box.dataset.fid);
      d.close(); missing.paint(); missing.go(1);
    };
    d.onkeydown = (e) => {
      const f = /^[1-9]$/.test(e.key) && ph.faces[+e.key - 1];
      const v = { w: "wrong", d: "notdetected", x: "cant" }[e.key];
      if (f) missing.set(ph.asset, "face", f.id);
      else if (v) missing.set(ph.asset, v);
      else if (e.key === "v" || e.key === "z") { d.close(); return; }
      else return;
      e.preventDefault(); d.close(); missing.paint(); missing.go(1);
    };
    d.showModal();
  },
  set(asset, verdict, face = null) {
    S.verdicts.set(asset, { verdict, face });
  },
  paint() {
    $$("#detail .mcard").forEach((c) => {
      const v = S.verdicts.get(c.dataset.asset);
      c.classList.toggle("cur", +c.dataset.i === S.mcur);
      $$(".fbox", c).forEach((b) => b.classList.toggle("pick", v.verdict === "face" && b.dataset.fid === v.face));
      $$("[data-v]", c).forEach((b) => b.classList.toggle("primary", b.dataset.v === v.verdict));
      const el = $(".mverdict", c);
      el.textContent = VERDICTS[v.verdict];
      el.className = `mverdict v-${v.verdict}`;
    });
  },
  go(d) {
    const n = S.detail?.photos.length;
    if (!n) return;
    S.mcur = Math.min(Math.max(S.mcur + d, 0), n - 1);
    missing.paint();
    $(`#detail .mcard[data-i="${S.mcur}"]`)?.scrollIntoView({ block: "nearest" });
  },
  async submit() {
    if (S.busy) return toast("Still working on the previous request…");
    // Unanswered photos are not sent; they come back in a later round.
    const verdicts = [...S.verdicts].filter(([, v]) => v.verdict).map(([asset, v]) => ({ asset, ...v }));
    if (!verdicts.length) return toast("No verdicts yet");
    S.busy = true;
    toast(`Submitting ${verdicts.length} verdicts…`, 600000);
    let r;
    try { r = await api(`/api/missing/${S.detail.id}`, { verdicts }); }
    catch (e) { toast(e.message, 6000); return; }
    finally { S.busy = false; }
    toast(`${r.ok} saved${r.failed.length ? `, ${r.failed.length} failed: ${r.failed[0].error}` : ""}`, 4000);
    await reload(true);
  },
  async sync() {
    if (S.busy) return toast("Still working on the previous request…");
    S.busy = true;
    toast("Checking every open photo in Immich…", 600000);
    let r;
    try { r = await api("/api/missing-sync", {}); }
    catch (e) { toast(e.message, 6000); return; }
    finally { S.busy = false; }
    toast(`${r.checked} photos checked: ${r.present || 0} now have the face, ` +
          `${r.requeued || 0} back in the rounds, ${r.open || 0} still open`, 8000);
    await reload(true);
  },
  async runDetect() {
    try { S.detect = await api("/api/detect", {}); }
    catch (e) { return toast(e.message, 6000); }
    missing.poll();
  },
  poll() {
    clearTimeout(missing.timer);
    missing.timer = setTimeout(async () => {
      if (S.view !== "missing") return;
      S.detect = await api("/api/detect");
      const panel = $("#detail .panel");
      if (panel) panel.outerHTML = missing.detectPanel();
      $("#detect-run")?.addEventListener("click", missing.runDetect);
      $("#m-sync")?.addEventListener("click", missing.sync);
      if (S.detect.state === "running") missing.poll();
      else { toast("Detection round finished", 4000); await reload(true); }
    }, 3000);
  },
  key(e) {
    const ph = S.detail?.photos?.[S.mcur];
    if (e.key === "ArrowRight" || e.key === "l") missing.go(1);
    else if (e.key === "ArrowLeft" || e.key === "h") missing.go(-1);
    else if (e.key === "Enter") missing.submit();
    else if (!ph) return false;
    else if (e.key === "v" || e.key === "z") {
      if (S.mhover != null) { S.mcur = S.mhover; missing.paint(); }
      missing.zoom(S.detail.photos[S.mcur]);
    }
    else if (/^[1-9]$/.test(e.key)) {
      const f = ph.faces[+e.key - 1];
      if (!f) return true;
      missing.set(ph.asset, "face", f.id); missing.paint(); missing.go(1);
    } else if (e.key === "w" || e.key === "d" || e.key === "x") {
      missing.set(ph.asset, { w: "wrong", d: "notdetected", x: "cant" }[e.key]); missing.paint(); missing.go(1);
    } else return false;
    return true;
  },
};

// ------------------------------------------------------------- split --
// A person named "SPLIT…" in Immich, or any person added here by its id or
// Immich URL, holds several people. Its live faces,
// grouped by look when a grouping was computed, go out in bulk: to someone
// existing, to a new person, or deleted as not a face.
const split = {
  empty: 'Nothing to split. Add a cluster above, or rename a mixed person to SPLIT in Immich.',
  head: () => `<form id="splitadd" class="splitadd"><input type="text" placeholder="Cluster id or Immich URL"
                 aria-label="Cluster id or Immich URL"><button><kbd>n</kbd> Add</button></form>`,
  wire() {
    $("#splitadd").addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = e.target.querySelector("input");
      let r;
      try { r = await api("/api/split-add", { id: input.value }); }
      catch (err) { toast(err.message, 6000); return; }
      await reload();
      const i = S.items.findIndex((x) => x.id === r.id);
      if (i >= 0) await select(i);
    });
  },
  async remove(id) {
    await api(`/api/split-remove/${id}`, {});
    toast("Removed from the list; nothing changed in Immich");
    await reload(true);
  },
  async load() {
    const r = await api("/api/split");
    S.items = r.items;
    S.server = r.server;
  },
  row: (p) => `<span>${p.name ? esc(p.name) : '<i class="muted">unnamed</i>'}</span><span class="muted">${p.photos}</span>
               <span class="sub mono">${p.id.slice(0, 8)}</span>`,
  async detail(p) {
    const [d, ppl] = await Promise.all([api(`/api/split/${p.id}`), api("/api/split-people")]);
    S.detail = d;
    S.faces = new Map(d.faces.map((f) => [f.id, f]));
    let html = "", last = null;
    for (const f of d.faces) {
      if (d.grouped && f.group !== last) {
        const n = d.faces.filter((x) => x.group === f.group).length;
        html += `<div class="splitgroup" data-group="${esc(f.group)}">${f.group ? `Group ${esc(f.group)}` : "Not grouped"}
          <span class="muted">${n} face(s)</span></div>`;
        last = f.group;
      }
      html += `<div class="card" data-face="${f.id}" title="${esc(f.file)}${f.tags.length ? `\nTags: ${esc(f.tags.join(", "))}` : ""}">
        ${faceHTML(f)}<div class="lbl">${esc(f.file)}</div></div>`;
    }
    $("#detail").innerHTML = `
      <h2>${d.name ? esc(d.name) : '<i class="muted">Unnamed</i>'} <span class="muted">${d.faces.length} faces</span></h2>
      <p class="muted">Select faces (click, shift-click a range, click a group heading for the whole group),
        then move them to a person or delete those that are not faces. Hover a face for its photo's Google tags;
        <kbd>v</kbd> shows the whole photo. ${d.grouped ? "Groups are by look, computed with Immich's recognition model — " +
        "faces behind goggles group by gear more than by person, so check each group." : ""}
        <a href="${esc(d.server)}/people/${d.id}" target="_blank" rel="noopener">Open in Immich</a>
        ${p.added ? '· <button id="splitremove">Remove from this list</button>' : ""}</p>
      <div class="toolbar">
        <button id="notface"><kbd>d</kbd> Not a face</button>
        <select id="moveto"><option value="">Move to…</option><option value="__new">New person…</option>${ppl.people.filter((x) => x.id !== d.id).map((x) =>
          `<option value="${x.id}">${esc(x.name)}</option>`).join("")}</select>
        <span class="muted" id="selcount"></span>
      </div>
      <div class="grid">${html || '<p class="muted">No faces left.</p>'}</div>`;
    $("#notface").addEventListener("click", () => split.decide("notface"));
    $("#splitremove")?.addEventListener("click", () => split.remove(d.id));
    $("#moveto").addEventListener("change", (e) => {
      const v = e.target.value;
      e.target.value = "";
      if (v === "__new") {
        const name = prompt("Name of the new person:");
        if (name?.trim()) split.decide("move", null, name.trim());
      } else if (v) split.decide("move", v);
    });
    $$("#detail .card").forEach((c) => c.addEventListener("click", (e) => review.click(c, e)));
    $$("#detail .splitgroup").forEach((g) => g.addEventListener("click", () => {
      const ids = d.faces.filter((f) => f.group === g.dataset.group).map((f) => f.id);
      const all = ids.every((id) => S.selected.has(id));
      ids.forEach((id) => all ? S.selected.delete(id) : S.selected.add(id));
      review.paintSel();
    }));
    review.paintSel();
  },
  async decide(action, to = null, newName = null) {
    if (S.busy) return toast("Still working on the previous request…");
    const faces = [...S.selected].map((id) => S.faces.get(id)).filter(Boolean).map((f) => ({ id: f.id, asset: f.asset }));
    if (!faces.length) return toast("Select faces first");
    if (action === "notface" && !confirm(`Delete ${faces.length} face detection(s)?\n\nThe photos stay; only the ` +
        `face boxes go. Immich cannot restore a deleted face, so only do this for things that are not faces.`)) return;
    S.busy = true;
    toast(`${action === "notface" ? "Deleting" : "Moving"} ${faces.length} face(s)…`, 600000);
    let r;
    try { r = await api(`/api/split/${S.detail.id}`, { action, faces, to, newName }); }
    catch (e) { toast(e.message, 6000); return; }
    finally { S.busy = false; }
    toast(`${r.ok} ${action === "notface" ? "deleted" : "moved"}` +
          `${r.failed.length ? `, ${r.failed.length} failed: ${r.failed[0].error}` : ""}`, 5000);
    S.selected.clear();
    await reload(true);
  },
  key(e) {
    if (e.key === "a") { $$("#detail .card").forEach((c) => S.selected.add(c.dataset.face)); review.paintSel(); }
    else if (e.key === "Escape") { S.selected.clear(); review.paintSel(); }
    else if (e.key === "d") split.decide("notface");
    else if (e.key === "n") $("#splitadd input").focus();
    else return false;
    return true;
  },
};

const VIEWS = { people, tags, review, featured, clusters, missing, split };

// ------------------------------------------------------------- wiring --
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input[type=text], input[type=date], select") && e.key !== "Escape") return;
  if (e.target.matches("input[type=range]") && e.key.startsWith("Arrow")) return;
  if (e.ctrlKey || e.metaKey || e.altKey || $("dialog[open]")) return;
  if (VIEWS[S.view].key?.(e)) return e.preventDefault();
  const k = e.key;
  if (k === "j" || k === "ArrowDown") move(1);
  else if (k === "k" || k === "ArrowUp") move(-1);
  else if (k === "p") show("people");
  else if (k === "t") show("tags");
  else if (k === "r") show("review");
  else if (k === "f") show("featured");
  else if (k === "c") show("clusters");
  else if (k === "m") show("missing");
  else if (k === "s") show("split");
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
  const fromHash = location.hash.slice(1);
  await show(VIEWS[fromHash] ? fromHash : S.status.hasAfter ? "people" : "tags");
})();

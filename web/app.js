// StackSense UI. Builds the form from /api/schema, sends JSON to /api/recommend and renders the explained result.
(function () {
  const $ = (s) => document.querySelector(s);
  const esc = (t) => String(t == null ? "" : t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  let FIELDS = [];
  let lastOutput = null;
  let scenarioId = null;

  const SECTION_TITLES = { project: "Project", scale: "Scale", team: "Team", budget: "Budget", timeline: "Timeline",
    infrastructure: "Infrastructure", requirements: "Requirements", compliance: "Compliance", priorities: "Priorities (1-5)", extras: "Extras" };

  function fieldId(path) { return "f_" + path.replace(/\./g, "__"); }

  function buildForm(fields) {
    const form = $("#form");
    form.innerHTML = "";
    const groups = {};
    fields.forEach((f) => { const sec = f.path.split(".")[0]; (groups[sec] = groups[sec] || []).push(f); });
    Object.keys(SECTION_TITLES).forEach((sec) => {
      if (!groups[sec]) return;
      const fs = document.createElement("fieldset");
      if (sec === "extras") fs.className = "extra";
      fs.innerHTML = `<legend>${SECTION_TITLES[sec]}</legend>`;
      groups[sec].forEach((f) => fs.appendChild(fieldRow(f)));
      form.appendChild(fs);
    });
  }

  function fieldRow(f) {
    const row = document.createElement("div");
    row.className = "field";
    row.dataset.path = f.path;
    const name = f.path.includes(".") ? f.path.split(".").slice(1).join(".") : f.path;
    const id = fieldId(f.path);
    let ctl = "";
    if (f.type === "enum" || f.type === "avail") {
      const opts = (f.required ? [] : [""]).concat(f.allowed).map((v) =>
        `<option value="${esc(v)}">${v === "" ? "(default" + (f.default != null ? ": " + esc(f.default) : "") + ")" : esc(v)}</option>`).join("");
      ctl = `<select id="${id}">${opts}</select>`;
    } else if (f.type === "enum_list") {
      ctl = `<div class="checks" id="${id}">` + f.allowed.map((v) => `<label><input type="checkbox" value="${esc(v)}">${esc(v)}</label>`).join("") + "</div>";
    } else if (f.type === "bool") {
      ctl = `<select id="${id}"><option value="">(default${f.default != null ? ": " + f.default : ""})</option><option value="true">true</option><option value="false">false</option></select>`;
    } else if (f.type === "priority") {
      ctl = `<select id="${id}"><option value="">(default 3)</option>${[1, 2, 3, 4, 5].map((n) => `<option>${n}</option>`).join("")}</select>`;
    } else if (f.type === "str_list") {
      ctl = `<input type="text" id="${id}" placeholder="comma-separated">`;
    } else if (f.path === "project.description") {
      ctl = `<textarea id="${id}" placeholder="Free text: StackSense extracts keyword signals (no LLM)"></textarea>`;
    } else if (f.type === "str") {
      ctl = `<input type="text" id="${id}">`;
    } else {
      ctl = `<input type="number" id="${id}" min="${f.min != null ? f.min : ""}" step="any" placeholder="${f.type === "int_or_null" ? "null" : ""}">`;
    }
    row.innerHTML = `<label for="${id}" title="${esc(f.help)}">${esc(name)}${f.required ? " <b>*</b>" : ""}</label>${ctl}`;
    return row;
  }

  function setPath(obj, path, val) {
    const parts = path.split(".");
    let cur = obj;
    for (let i = 0; i < parts.length - 1; i++) { cur[parts[i]] = cur[parts[i]] || {}; cur = cur[parts[i]]; }
    cur[parts[parts.length - 1]] = val;
  }
  function getPath(obj, path) {
    return path.split(".").reduce((o, k) => (o && typeof o === "object" ? o[k] : undefined), obj);
  }

  function readForm() {
    const out = {};
    if (scenarioId) out.scenario_id = scenarioId;
    FIELDS.forEach((f) => {
      const el = document.getElementById(fieldId(f.path));
      if (!el) return;
      let v;
      if (f.type === "enum_list") {
        v = [...el.querySelectorAll("input:checked")].map((x) => x.value);
        if (!v.length && f.path === "compliance") return;
        if (!v.length && !f.required) return;
      } else if (f.type === "bool") {
        if (el.value === "") return; v = el.value === "true";
      } else if (f.type === "str_list") {
        v = el.value.split(",").map((s) => s.trim()).filter(Boolean);
        if (!v.length && f.path !== "team.languages") return;
      } else if (f.type === "priority") {
        if (el.value === "") return; v = Number(el.value);
      } else if (["int", "num", "int_or_null"].includes(f.type)) {
        if (el.value === "") { if (f.type === "int_or_null") return; if (!f.required) return; return; }
        v = Number(el.value);
      } else {
        if (el.value === "") return; v = el.value;
      }
      setPath(out, f.path, v);
    });
    return out;
  }

  function fillForm(sc) {
    scenarioId = sc.scenario_id || null;
    FIELDS.forEach((f) => {
      const el = document.getElementById(fieldId(f.path));
      if (!el) return;
      const v = getPath(sc, f.path);
      if (f.type === "enum_list") {
        el.querySelectorAll("input").forEach((x) => { x.checked = Array.isArray(v) && v.includes(x.value); });
      } else if (f.type === "str_list") {
        el.value = Array.isArray(v) ? v.join(", ") : "";
      } else if (f.type === "bool") {
        el.value = v === true ? "true" : v === false ? "false" : "";
      } else {
        el.value = v == null ? "" : String(v);
      }
    });
  }

  async function run(payload) {
    $("#run").disabled = true;
    try {
      const res = await fetch("/api/recommend", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      lastOutput = await res.json();
      render(lastOutput);
    } finally { $("#run").disabled = false; }
  }

  function chips(list) { return `<div class="chips">${(list || []).map((x) => `<span class="chip">${esc(x)}</span>`).join("")}</div>`; }

  function card(layer, c) {
    return `<div class="card"><div class="layer">${esc(layer)}</div><div class="choice">${esc(c.choice)}</div>
      <p>${esc(c.justification)}</p>
      <div class="sub">Driven by</div>${chips(c.driven_by)}
      <div class="sub">Alternatives considered</div><ul class="alts">${(c.alternatives || []).map((a) => `<li><b>${esc(a.choice)}</b>: ${esc(a.why_not)}</li>`).join("")}</ul>
      ${c.tradeoffs && c.tradeoffs.length ? `<div class="sub">Trade-offs</div><ul class="trade">${c.tradeoffs.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : ""}
    </div>`;
  }

  function render(o) {
    document.querySelectorAll(".field.err").forEach((x) => x.classList.remove("err"));
    (o.validation_errors || []).forEach((e) => { const row = document.querySelector(`.field[data-path="${e.field}"]`); if (row) row.classList.add("err"); });
    const st = o.stack;
    const cost = o.estimated_monthly_cost_usd;
    let h = `<div class="head"><span class="status ${esc(o.status)}">${esc(o.status)}</span>${o.architecture_style ? `<span class="style">${esc(o.architecture_style.replace(/_/g, " "))}</span>` : ""}
      ${o.scenario_id ? `<span class="chip">${esc(o.scenario_id)}</span>` : ""}
      <span style="flex:1"></span><button class="btn ghost" id="exportOut" type="button">Export output JSON</button></div>
      <div class="summary">${esc(o.summary)}</div>`;
    if (o.validation_errors && o.validation_errors.length) {
      h += `<h3>Validation errors</h3><table><tr><th>Field</th><th>Problem</th></tr>${o.validation_errors.map((e) => `<tr><td><span class="chip">${esc(e.field)}</span></td><td>${esc(e.message)}</td></tr>`).join("")}</table>`;
    }
    if (cost) {
      const d = o.derived || {};
      h += `<div class="kpis"><div class="kpi"><div class="v">$${cost.min.toLocaleString()} – $${cost.max.toLocaleString()}</div><div class="l">estimated monthly cost (USD)</div></div>
        <div class="kpi"><div class="v">L${d.scale_level}</div><div class="l">scale level (${(d.peak_concurrent_users || 0).toLocaleString()} peak users, ~${d.estimated_peak_rps} req/s)</div></div>
        <div class="kpi"><div class="v">${(o.warnings || []).filter((w) => w.severity !== "info").length}</div><div class="l">warnings &amp; critical issues (+ ${(o.warnings || []).filter((w) => w.severity === "info").length} info notes)</div></div>
        <div class="kpi"><div class="v">${d.combinations_evaluated || 0}</div><div class="l">valid stack combinations scored</div></div></div>`;
    }
    if (o.warnings && o.warnings.length) {
      h += `<h3>Warnings and conflicts</h3>` + o.warnings.map((w) => `<div class="warn ${esc(w.severity)}"><code>${esc(w.severity.toUpperCase())} · ${esc(w.code)}</code>${esc(w.message)} ${chips(w.fields)}</div>`).join("");
    }
    if (st) {
      h += `<h3>Core stack</h3><div class="cards">${["frontend", "backend", "database", "hosting"].map((k) => card(k, st[k])).join("")}</div>`;
      if (st.supporting && st.supporting.length) {
        h += `<h3>Supporting services</h3><table><tr><th>Layer</th><th>Choice</th><th>Why</th><th>Driven by</th><th>$/mo</th></tr>` +
          st.supporting.map((s) => `<tr><td><b>${esc(s.layer)}</b></td><td>${esc(s.choice)}</td><td>${esc(s.justification)}${s.alternatives && s.alternatives.length ? `<br><small><i>Not ${esc(s.alternatives[0].choice)}: ${esc(s.alternatives[0].why_not)}</i></small>` : ""}</td><td>${chips(s.driven_by)}</td><td>${s.estimated_monthly_usd ? esc(s.estimated_monthly_usd.join("–")) : ""}</td></tr>`).join("") + `</table>`;
      }
    }
    if (cost) {
      h += `<h3>Cost assumptions</h3><ul class="trade">${cost.assumptions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>`;
      if (cost.breakdown) h += `<div class="chips">${Object.entries(cost.breakdown).map(([k, v]) => `<span class="chip">${esc(k)}: $${v[0]}–${v[1]}</span>`).join("")}</div>`;
    }
    if (o.assumptions && o.assumptions.length) {
      h += `<h3>Assumptions and defaults applied</h3><ul class="trade">${o.assumptions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>`;
    }
    if (o.derived && o.derived.description_signals && Object.keys(o.derived.description_signals).length) {
      h += `<h3>Signals read from the description</h3>${chips(Object.entries(o.derived.description_signals).map(([k, v]) => k + ': "' + v + '"'))}`;
    }
    h += `<details><summary>Raw output JSON</summary><div class="raw">${esc(JSON.stringify(o, null, 2))}</div></details>`;
    $("#results").innerHTML = h;
    $("#exportOut").onclick = () => download((o.scenario_id || "stacksense") + "_output.json", o);
    $("#results").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function download(name, obj) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" }));
    a.download = name; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  async function init() {
    const sch = await (await fetch("/api/schema")).json();
    FIELDS = sch.fields;
    $("#ver").textContent = "engine v" + sch.engine_version + " · " + Object.values(sch.kb).reduce((a, b) => a + b, 0) + " core technologies";
    buildForm(FIELDS);
    const list = await (await fetch("/api/samples")).json();
    const sel = $("#sample");
    ["public", "edge"].forEach((g) => {
      const og = document.createElement("optgroup");
      og.label = g === "public" ? "Public scenarios" : "Edge cases";
      list.filter((s) => s.group === g).forEach((s) => { const o = document.createElement("option"); o.value = s.id; o.textContent = s.id + " · " + s.title; og.appendChild(o); });
      sel.appendChild(og);
    });
    sel.onchange = async () => {
      if (!sel.value) return;
      const sc = await (await fetch("/api/sample?id=" + encodeURIComponent(sel.value))).json();
      fillForm(sc);
      run(sc);  // run the file exactly as stored (judges' path)
    };
    $("#file").onchange = async (e) => {
      const f = e.target.files[0]; if (!f) return;
      let data;
      try { data = JSON.parse(await f.text()); } catch (err) { render({ status: "invalid_input", summary: "File is not valid JSON: " + err.message, validation_errors: [{ field: "$", message: err.message }] }); return; }
      if (Array.isArray(data)) data = data[0];
      if (data && typeof data === "object") fillForm(data);
      run(data);
      e.target.value = "";
    };
    $("#run").onclick = () => run(readForm());
    $("#exportIn").onclick = () => download((scenarioId || "scenario") + ".json", readForm());
    $("#reset").onclick = () => { scenarioId = null; buildForm(FIELDS); sel.value = ""; };
    const q = new URLSearchParams(location.search).get("sample");
    if (q) { sel.value = q; sel.onchange(); }
  }
  init();
})();

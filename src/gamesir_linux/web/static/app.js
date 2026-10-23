"use strict";

// ---------------------------------------------------------------------------
// tiny helpers

const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "style") {
      // CSSOM, not the style attribute: the page's CSP forbids inline style attributes.
      for (const decl of String(v).split(";")) {
        const i = decl.indexOf(":");
        if (i > 0) el.style.setProperty(decl.slice(0, i).trim(), decl.slice(i + 1).trim());
      }
    }
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const SVG = "http://www.w3.org/2000/svg";
function s(tag, attrs = {}) {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

async function api(path, body) {
  const opt = { method: body === undefined ? "GET" : "POST", headers: { "X-GameSir": "1" } };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opt);
  } catch {
    throw new Error("Lost contact with gamesir-linux. Is it still running?");
  }
  let json;
  try { json = await res.json(); } catch { throw new Error(`Unexpected reply from gamesir-linux (HTTP ${res.status}).`); }
  if (!json.ok) throw new Error(json.error || "The request failed.");
  return json.data;
}

function toast(message, kind = "info") {
  const el = h("div", { class: `toast ${kind}`, text: message });
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "error" ? 8000 : 3000);
}

function confirmDialog(title, text, okLabel) {
  const dlg = $("#confirm");
  $("#confirm-title").textContent = title;
  $("#confirm-text").textContent = text;
  $("#confirm-ok").textContent = okLabel;
  dlg.returnValue = "";
  dlg.showModal();
  return new Promise((resolve) => dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true }));
}

// ---------------------------------------------------------------------------
// state

const SECTIONS = [
  { id: "controller", label: "Controller" },
  { id: "buttons", label: "Buttons" },
  { id: "sticks", label: "Sticks" },
  { id: "triggers", label: "Triggers" },
  { id: "vibration", label: "Vibration" },
  { id: "lighting", label: "Home light" },
  { id: "profiles", label: "Profiles" },
  { id: "power", label: "Power" },
  { id: "firmware", label: "Firmware", offline: true },
];

const app = {
  meta: null,
  status: { connected: false },
  dev: null,          // the controller's settings, as last read back
  section: "controller",
  scanning: false,
  found: null,
};

const val = (entry) => (entry && "value" in entry ? entry.value : undefined);
const errOf = (entry) => (entry && entry.error) || null;

function sectionFromHash() {
  const id = location.hash.replace("#", "");
  return SECTIONS.some((x) => x.id === id) ? id : "controller";
}

function go(id) {
  if (location.hash !== `#${id}`) location.hash = id;
  else render();
}

// ---------------------------------------------------------------------------
// saving: every change is written, then read back by the server

async function save(path, body, apply, statusEl) {
  try {
    const data = await api(path, body);
    apply(data);
    if (statusEl) {
      statusEl.textContent = "Saved";
      statusEl.classList.add("show");
      setTimeout(() => statusEl.classList.remove("show"), 1600);
    }
    renderChrome();
  } catch (e) {
    toast(e.message, "error");
    render();
  }
}

const savedMark = () => h("span", { class: "saved", "aria-live": "polite" });

// ---------------------------------------------------------------------------
// building blocks

function row(label, help, control, extra = {}) {
  return h("div", { class: `row${extra.stack ? " stack" : ""}` },
    h("div", {}, h("span", { class: "row-label", id: extra.id }, label), help ? h("span", { class: "row-help" }, help) : null),
    h("div", { class: "row-control" }, control));
}

function unavailable(entry) {
  return h("span", { class: "unavailable" }, `Not available: ${errOf(entry)}`);
}

function toggle(label, help, entry, onChange) {
  if (errOf(entry)) return row(label, help, unavailable(entry));
  const mark = savedMark();
  const id = `t-${Math.random().toString(36).slice(2)}`;
  const input = h("input", { type: "checkbox", role: "switch", "aria-labelledby": id, checked: !!val(entry) });
  input.addEventListener("change", () => onChange(input.checked, mark));
  return row(label, help, [mark, h("label", { class: "switch" }, input, h("span"))], { id });
}

function featureToggle(feature, label, help) {
  const entry = app.dev.features[feature];
  return toggle(label, help, entry, (on, mark) =>
    save("/api/feature", { feature, enabled: on }, (v) => { app.dev.features[feature] = { value: v }; }, mark));
}

function slider({ label, help, value, min = 0, max = 100, unit = "", onInput, onCommit }) {
  const out = h("output", {}, `${value}${unit}`);
  const mark = savedMark();
  const input = h("input", { type: "range", min, max: Math.max(max, value), step: 1, value, "aria-label": label });
  input.addEventListener("input", () => { out.textContent = `${input.value}${unit}`; onInput && onInput(+input.value); });
  input.addEventListener("change", () => onCommit(+input.value, mark));
  return row(label, help, h("div", { class: "slider" }, input, out, mark), { stack: true });
}

function segmented(name, options, current, onChange) {
  const group = h("div", { class: "segmented", role: "radiogroup" });
  for (const [value, text] of options) {
    const input = h("input", { type: "radio", name, value, checked: value === current });
    input.addEventListener("change", () => onChange(value));
    group.append(h("label", {}, input, h("span", {}, text)));
  }
  return group;
}

// ---------------------------------------------------------------------------
// chrome: status bar, nav, controller map

function renderChrome() {
  const st = $("#status");
  st.replaceChildren();
  if (app.status.connected) {
    const via = { simulated: "Simulated controller", usb: "Connected by cable" }[app.status.mode] || "Connected over Bluetooth";
    st.append(h("span", { class: "conn on" }, via));
    const bat = app.dev && val(app.dev.battery);
    if (bat) {
      st.append(h("span", { class: "battery", title: bat.charging ? "Charging" : "Battery" },
        h("span", { class: "battery-cell", "aria-hidden": "true" }, h("span", { style: `width:${bat.level}%` })),
        `${bat.level}%${bat.charging ? ", charging" : ""}`));
    }
    st.append(h("button", { class: "btn small", onclick: disconnect }, "Disconnect"));
  } else {
    st.append(h("span", { class: "conn" }, "No controller connected"));
  }

  const nav = $("#nav");
  nav.replaceChildren();
  for (const sec of SECTIONS) {
    const usable = app.status.connected || sec.offline;
    nav.append(h("li", {}, h("button", {
      "aria-current": app.section === sec.id ? "page" : null,
      disabled: !usable && sec.id !== "controller",
      onclick: () => go(sec.id),
    }, sec.label, sectionHint(sec.id))));
  }

  const pad = $("#pad");
  pad.classList.toggle("offline", !app.status.connected);
  for (const part of pad.querySelectorAll(".part")) {
    part.classList.toggle("active", app.status.connected && part.dataset.section === app.section);
    part.setAttribute("aria-disabled", app.status.connected ? "false" : "true");
  }
  const light = app.dev && val(app.dev.light);
  document.documentElement.style.setProperty("--led", light ? `rgb(${light.rgb.join(",")})` : "#ffffff");
}

function sectionHint(id) {
  if (!app.dev) return null;
  const d = app.dev;
  let text = null;
  if (id === "controller" && val(d.info)) text = `firmware ${val(d.info).firmware_version}`;
  if (id === "profiles" && val(d.slots)) text = `${val(d.slots)[1] + 1} of ${val(d.slots)[0]}`;
  if (id === "power" && val(d.auto_sleep) !== undefined) text = `sleep ${val(d.auto_sleep)} min`;
  return text ? h("span", { class: "hint" }, text) : null;
}

function wireMap() {
  for (const part of $("#pad").querySelectorAll(".part")) {
    const open = () => { if (app.status.connected) go(part.dataset.section); };
    part.addEventListener("click", open);
    part.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });
  }
}

// ---------------------------------------------------------------------------
// panels

function panelConnect() {
  const wrap = h("div", {},
    h("h1", {}, "Connect your controller"),
    h("p", { class: "lede" }, "Settings are read from the controller and written back to it, by cable or over Bluetooth. Nothing is stored on this computer."),
    h("ol", { class: "steps" },
      h("li", {}, "By cable: plug the controller in and hold Menu and View for 2 seconds until the light turns green (PC mode)."),
      h("li", {}, "Or over Bluetooth: turn the controller on in Android mode by holding Home and A for 3 seconds until the light turns yellow."),
      h("li", {}, "Find it below and connect.")),
  );
  const findBtn = h("button", { class: "btn primary", onclick: scan, disabled: app.scanning },
    app.scanning ? "Searching…" : "Find controllers");
  wrap.append(h("div", { class: "btn-row" }, findBtn,
    h("button", { class: "btn", onclick: () => connect({ simulate: true }) }, "Use the simulator")));

  if (app.found) {
    if (app.found.length === 0) {
      wrap.append(h("p", { class: "note" },
        "No Nova 2 Lite found. By cable, check the light is green. Over Bluetooth, check it is in Android mode and close to this computer. If it still doesn't appear, pair it once in your system's Bluetooth settings."));
    } else {
      wrap.append(h("ul", { class: "found" }, app.found.map((d) =>
        h("li", {}, h("span", {}, d.name, h("span", { class: "addr" }, d.link === "usb" ? `USB cable, ${d.address}` : d.address)),
          h("button", { class: "btn primary small", onclick: () => connect({ address: d.address, name: d.name, link: d.link }) }, "Connect")))));
    }
  }
  wrap.append(h("p", { class: "callout" },
    "The simulator behaves like a controller, so you can try every setting without one."));
  return wrap;
}

function panelController() {
  const d = app.dev;
  const info = val(d.info);
  const bat = val(d.battery);
  const fact = (term, value, small) => h("div", { class: "fact" }, h("dt", {}, term), h("dd", {}, value ?? "Unknown", small ? h("small", {}, small) : null));
  return h("div", {},
    h("h1", {}, app.status.name || "Nova 2 Lite"),
    h("p", { class: "lede" }, app.status.mode === "simulated"
      ? "This is the simulator: changes behave like the real controller but go nowhere."
      : `${app.status.mode === "usb" ? `Connected by cable (${app.status.address})` : `Connected to ${app.status.address}`}. Every value shown here was read back from the controller.`),
    h("dl", { class: "facts" },
      fact("Battery", bat ? `${bat.level}%` : null, bat && bat.charging ? "charging" : null),
      fact("Firmware", info && info.firmware_version, info ? regionName(info.firmware_version) : null),
      fact("Hardware", info && info.hardware_version),
      fact("Made", info && info.production_date),
      fact("Bluetooth address", info && info.mac),
      fact("Protocol", info && `GCM ${info.gcm_version}`)),
    h("div", { class: "btn-row" },
      h("button", { class: "btn", onclick: () => reload(true) }, "Read settings again")),
  );
}

function regionName(fw) {
  if (!fw || !/^\d+$/.test(fw)) return null;
  return { "0": "China build", "1": "international build" }[fw.slice(-1)] || null;
}

function panelButtons() {
  const d = app.dev;
  const labels = app.meta.keys;
  const wrap = h("div", {},
    h("h1", {}, "Buttons"),
    h("p", { class: "lede" }, "Choose what the two back buttons do."));
  const rows = h("div", { class: "rows" });
  for (const source of app.meta.remappable) {
    const entry = d.mappings[source];
    if (errOf(entry)) { rows.append(row(labels[source], null, unavailable(entry))); continue; }
    const m = val(entry);
    const current = m.kind === "disabled" ? "" : (m.kind === "key" ? (m.targets[0] ?? "") : `__${m.kind}`);
    const select = h("select", { "aria-label": labels[source] },
      h("option", { value: "" }, "Do nothing"),
      h("optgroup", { label: "Buttons" }, app.meta.targets.map((k) => h("option", { value: k }, labels[k]))),
      d.capabilities.stick_targets
        ? h("optgroup", { label: "Stick directions" }, app.meta.stick_targets.map((k) => h("option", { value: k }, labels[k])))
        : null,
      m.kind === "macro" ? h("option", { value: "__macro", disabled: true }, "A macro (set in the GameSir app)") : null,
      m.kind === "keyboard" ? h("option", { value: "__keyboard", disabled: true }, "A keyboard key (set in the GameSir app)") : null,
    );
    select.value = current;
    const mark = savedMark();
    select.addEventListener("change", () =>
      save("/api/mapping", { source, target: select.value || null }, (v) => { d.mappings[source] = { value: v }; }, mark));
    const reset = h("button", { class: "btn small", onclick: () =>
      save("/api/mapping", { source, reset: true }, (v) => { d.mappings[source] = { value: v }; render(); }, mark) }, "Reset");
    rows.append(row(labels[source], source === "L4" ? "Under the left grip" : "Under the right grip", [mark, select, reset]));
  }
  wrap.append(rows);
  if (!d.capabilities.stick_targets) {
    wrap.append(h("p", { class: "note" }, "Mapping a back button to a stick direction needs firmware 6900 or newer."));
  }

  wrap.append(h("h2", {}, "Layout"),
    h("div", { class: "rows" },
      featureToggle("DPAD_DIAGONAL_LOCK", "Lock D-pad diagonals", "Only up, down, left and right register"),
      featureToggle("SWAP_LT_LB", "Swap LB and LT"),
      featureToggle("SWAP_RT_RB", "Swap RB and RT")));
  wrap.append(h("p", { class: "callout" },
    "The other buttons can only be remapped through the USB cable or the 2.4 GHz dongle. That connection isn't supported on Linux yet."));
  return wrap;
}

// stick dead-zone picture: outer rim = full travel
function deadZonePicture() {
  const svg = s("svg", { class: "dz", viewBox: "0 0 160 160", role: "img" });
  const outer = s("circle", { class: "outer", cx: 80, cy: 80, r: 70 });
  const live = s("circle", { class: "live", cx: 80, cy: 80, r: 70 });
  const inner = s("circle", { class: "inner", cx: 80, cy: 80, r: 0 });
  const anti = s("circle", { class: "anti", cx: 80, cy: 80, r: 0 });
  const rim = s("circle", { class: "rim", cx: 80, cy: 80, r: 70 });
  const dot = s("circle", { class: "dot", cx: 80, cy: 80, r: 3 });
  svg.append(outer, live, inner, anti, rim, dot);
  const update = ({ inner: i, outer: o, anti: a }) => {
    live.setAttribute("r", Math.max(0, 70 * (1 - o / 100)));
    inner.setAttribute("r", 70 * Math.min(i, 100) / 100);
    anti.setAttribute("r", 70 * Math.min(a, 100) / 100);
    svg.setAttribute("aria-label", `Inner dead zone ${i}%, outer dead zone ${o}%, anti-dead zone ${a}%`);
  };
  return { svg, update };
}

function panelSticks() {
  const d = app.dev;
  const wrap = h("div", {},
    h("h1", {}, "Sticks"),
    h("p", { class: "lede" }, "Dead zones decide how far a stick must move before it counts, and where it reaches full tilt. Shaded areas are ignored."));
  const pair = h("div", { class: "pair" });
  for (const [side, prefix] of [["Left stick", "LEFT"], ["Right stick", "RIGHT"]]) {
    const zones = { inner: `${prefix}_STICK_INNER`, outer: `${prefix}_STICK_OUTER`, anti: `${prefix}_STICK_ANTI` };
    const entries = Object.fromEntries(Object.entries(zones).map(([k, z]) => [k, d.dead_zones[z]]));
    const values = Object.fromEntries(Object.entries(entries).map(([k, e]) => [k, val(e) ?? 0]));
    const pic = deadZonePicture();
    pic.update(values);
    const col = h("div", {}, h("h2", {}, side), pic.svg,
      h("div", { class: "legend" }, h("span", {}, h("i"), "Ignored"), h("span", {}, h("i", { class: "a" }), "Output starts here")));
    const rows = h("div", { class: "rows" });
    const spec = [
      ["inner", "Inner dead zone", "Movement near the centre is ignored"],
      ["outer", "Outer dead zone", "Full tilt is reached this far before the edge"],
      ["anti", "Anti-dead zone", "Output jumps to this value as soon as the stick moves"],
    ];
    for (const [key, label, help] of spec) {
      const zone = zones[key];
      if (errOf(entries[key])) { rows.append(row(label, help, unavailable(entries[key]))); continue; }
      rows.append(slider({
        label, help, value: values[key], unit: "%", max: 50,
        onInput: (v) => { values[key] = v; pic.update(values); },
        onCommit: (v, mark) => save("/api/dead-zone", { zone, value: v }, (got) => { d.dead_zones[zone] = { value: got }; }, mark),
      }));
    }
    rows.append(featureToggle(`INVERT_${prefix}_X`, "Invert horizontal"), featureToggle(`INVERT_${prefix}_Y`, "Invert vertical"));
    col.append(rows);
    pair.append(col);
  }
  wrap.append(pair, h("h2", {}, "Both sticks"), h("div", { class: "rows" },
    featureToggle("SWAP_STICKS", "Swap left and right stick"),
    featureToggle("SWAP_LEFT_STICK_DPAD", "Swap left stick and D-pad")));
  wrap.append(h("p", { class: "note" }, "Values are shown as percentages of full travel. That's an assumption until it has been checked on a real controller."));
  return wrap;
}

function panelTriggers() {
  const d = app.dev;
  const wrap = h("div", {},
    h("h1", {}, "Triggers"),
    h("p", { class: "lede" }, "Set how much of each trigger's travel is ignored at the start and the end of the pull."));
  const quick = val(d.trigger_quick);
  const pair = h("div", { class: "pair" });
  for (const [side, prefix, qi] of [["Left trigger", "LEFT", 0], ["Right trigger", "RIGHT", 1]]) {
    const zones = { start: `${prefix}_TRIGGER_FRONT`, end: `${prefix}_TRIGGER_BACK` };
    const values = { start: val(d.dead_zones[zones.start]) ?? 0, end: val(d.dead_zones[zones.end]) ?? 0 };
    const startEl = h("span", { class: "dead start" });
    const endEl = h("span", { class: "dead end" });
    const paint = () => { startEl.style.width = `${values.start}%`; endEl.style.width = `${values.end}%`; };
    paint();
    const col = h("div", {}, h("h2", {}, side),
      h("div", { class: "travel", role: "img", "aria-label": `${side}: first ${values.start}% and last ${values.end}% of travel ignored` }, startEl, endEl),
      h("div", { class: "travel-scale" }, h("span", {}, "Released"), h("span", {}, "Fully pressed")));
    const rows = h("div", { class: "rows" });
    for (const [key, label, help] of [["start", "Start dead zone", "Ignored at the beginning of the pull"], ["end", "End dead zone", "Full press is reached this early"]]) {
      const zone = zones[key];
      if (errOf(d.dead_zones[zone])) { rows.append(row(label, help, unavailable(d.dead_zones[zone]))); continue; }
      rows.append(slider({
        label, help, value: values[key], unit: "%", max: 50,
        onInput: (v) => { values[key] = v; paint(); },
        onCommit: (v, mark) => save("/api/dead-zone", { zone, value: v }, (got) => { d.dead_zones[zone] = { value: got }; }, mark),
      }));
    }
    if (errOf(d.trigger_quick)) rows.append(row("Quick trigger", null, unavailable(d.trigger_quick)));
    else rows.append(toggle("Quick trigger", "Registers as fully pressed as soon as you pull", { value: quick[qi] }, (on, mark) => {
      const next = [...quick]; next[qi] = on;
      save("/api/trigger-quick", { left: next[0], right: next[1] }, (v) => { d.trigger_quick = { value: v }; render(); }, mark);
    }));
    col.append(rows);
    pair.append(col);
  }
  wrap.append(pair);
  return wrap;
}

function panelVibration() {
  const d = app.dev;
  const wrap = h("div", {}, h("h1", {}, "Vibration"), h("p", { class: "lede" }, "Rumble strength for the motor in each grip."));
  if (errOf(d.vibration)) { wrap.append(unavailable(d.vibration)); return wrap; }
  const v = val(d.vibration);
  const rows = h("div", { class: "rows" });
  for (const [field, label] of [["left_grip", "Left grip"], ["right_grip", "Right grip"]]) {
    rows.append(slider({
      label, value: v[field], unit: "%",
      onCommit: (n, mark) => save("/api/vibration", { [field]: n }, (got) => { d.vibration = { value: got }; }, mark),
    }));
  }
  wrap.append(rows, h("p", { class: "note" }, "A real controller reports 75% out of the box. 100% as the maximum hasn't been confirmed yet."));
  return wrap;
}

const COLOURS = [
  ["White", [255, 255, 255]], ["Red", [255, 40, 40]], ["Orange", [255, 140, 0]], ["Yellow", [255, 220, 0]],
  ["Green", [40, 220, 80]], ["Cyan", [0, 210, 230]], ["Blue", [40, 90, 255]], ["Purple", [170, 60, 255]],
  ["Pink", [255, 80, 180]], ["Off", [0, 0, 0]],
];
const hex = (rgb) => "#" + rgb.map((c) => c.toString(16).padStart(2, "0")).join("");
const rgbOf = (hx) => [1, 3, 5].map((i) => parseInt(hx.slice(i, i + 2), 16));

function panelLighting() {
  const d = app.dev;
  const wrap = h("div", {}, h("h1", {}, "Home light"), h("p", { class: "lede" }, "The colour of the light in the Home button."));
  if (errOf(d.light)) { wrap.append(unavailable(d.light)); return wrap; }
  const light = val(d.light);
  const set = (rgb) => save("/api/light", { rgb }, (got) => { d.light = { value: got }; render(); }, null);
  const cur = hex(light.rgb);
  wrap.append(h("div", { class: "swatches", role: "group", "aria-label": "Colours" }, COLOURS.map(([name, rgb]) =>
    h("button", { class: "swatch", style: `background:${hex(rgb)}`, title: name, "aria-label": name,
      "aria-pressed": hex(rgb) === cur ? "true" : "false", onclick: () => set(rgb) }))));
  const picker = h("input", { type: "color", value: cur, "aria-label": "Custom colour" });
  picker.addEventListener("change", () => set(rgbOf(picker.value)));
  wrap.append(h("div", { class: "colour-now" },
    h("span", { class: "chip", style: `background:${cur}` }),
    h("div", {}, h("div", { class: "row-label" }, (COLOURS.find(([, rgb]) => hex(rgb) === cur) || ["Custom colour"])[0]),
      h("span", { class: "row-help" }, cur.toUpperCase()))));
  wrap.append(h("div", { class: "rows custom-colour" }, row("Custom colour", "Any colour the light can show", picker)));
  return wrap;
}

function panelProfiles() {
  const d = app.dev;
  const wrap = h("div", {}, h("h1", {}, "Profiles"));
  if (errOf(d.slots)) {
    wrap.append(h("p", { class: "lede" }, "This controller didn't report any profiles over Bluetooth, so switching profiles isn't available."),
      h("p", { class: "note" }, `Controller reply: ${errOf(d.slots)}`));
    return wrap;
  }
  const [total, current] = val(d.slots);
  wrap.append(h("p", { class: "lede" }, "Each profile keeps its own set of settings. Switching loads that profile's settings from the controller."));
  const mark = savedMark();
  const options = Array.from({ length: total }, (_, i) => [String(i), `Profile ${i + 1}`]);
  wrap.append(h("div", { class: "rows" }, row("Active profile", null, [mark, segmented("slot", options, String(current), (n) =>
    save("/api/slot", { slot: +n }, (state) => { app.dev = state; render(); }, mark))])));
  return wrap;
}

const SLEEP_MINUTES = [5, 10, 15, 20, 30, 60];

function panelPower() {
  const d = app.dev;
  const wrap = h("div", {}, h("h1", {}, "Power"));
  const rows = h("div", { class: "rows" });
  if (errOf(d.auto_sleep)) rows.append(row("Sleep after", null, unavailable(d.auto_sleep)));
  else {
    const cur = val(d.auto_sleep);
    const opts = SLEEP_MINUTES.includes(cur) ? SLEEP_MINUTES : [...SLEEP_MINUTES, cur].sort((a, b) => a - b);
    const select = h("select", { "aria-label": "Sleep after" }, opts.map((m) => h("option", { value: m }, `${m} minutes`)));
    select.value = String(cur);
    const mark = savedMark();
    select.addEventListener("change", () => save("/api/auto-sleep", { value: +select.value }, (v) => { d.auto_sleep = { value: v }; }, mark));
    rows.append(row("Sleep after", "Turns off when idle for this long", [mark, select]));
  }
  rows.append(
    featureToggle("LOW_BATTERY_WARNING", "Low battery warning", "Vibrates when the battery runs low"),
    featureToggle("PLAY_WHILE_CHARGING", "Play while charging"),
    featureToggle("AUTO_POWER_ON_DOCK", "Turn on when lifted from the dock", "For the charging dock sold separately"));
  wrap.append(rows);
  wrap.append(h("div", { class: "danger-zone" },
    h("h2", {}, "Factory settings"),
    h("p", { class: "note" }, "Resets every setting on the controller, including button mapping and the Home light."),
    h("button", { class: "btn danger", onclick: restore }, "Restore factory settings")));
  return wrap;
}

async function panelFirmware() {
  const wrap = h("div", {}, h("h1", {}, "Firmware"));
  let fw;
  try { fw = await api("/api/firmware"); } catch (e) { wrap.append(h("p", { class: "note" }, e.message)); return wrap; }
  const device = fw.device_version;
  wrap.append(h("dl", { class: "facts" },
    h("div", { class: "fact" }, h("dt", {}, "On the controller"), h("dd", {}, device || "Not connected", device ? h("small", {}, regionName(device)) : null)),
    h("div", { class: "fact" }, h("dt", {}, "Downloaded release"), h("dd", {}, fw.extracted ? `GameSir Connect ${fw.extracted}` : "None yet"))));

  if (fw.available.length) {
    const rows = h("div", { class: "rows" });
    for (const f of fw.available) {
      let help = f.target === "dongle" ? "For the 2.4 GHz dongle" : `${f.region === "china" ? "China" : "International"} build`;
      if (f.matches_device_region === false) help += ", not for your controller";
      if (f.newer_than_device) help += ", newer than yours";
      if (f.newer_than_device === false) help += ", not newer than yours";
      rows.append(row(`${f.target === "dongle" ? "Dongle" : "Controller"} firmware ${f.version}`, help, h("span", { class: "row-help" }, f.file)));
    }
    wrap.append(h("h2", {}, "Firmware in the downloaded release"), rows);
  }

  const status = h("p", { class: "note", "aria-live": "polite" });
  const check = h("button", { class: "btn", onclick: async () => {
    check.disabled = true; status.textContent = "Checking GameSir's update feed…";
    try {
      const r = await api("/api/firmware/check", {});
      status.textContent = r.update_available
        ? `GameSir Connect ${r.latest} is available.` : `You have the latest release (GameSir Connect ${r.latest}).`;
    } catch (e) { status.textContent = e.message; }
    check.disabled = false;
  } }, "Check for updates");
  const dl = h("button", { class: "btn primary", onclick: async () => {
    dl.disabled = true; status.textContent = "Downloading GameSir Connect and extracting its firmware. This takes a minute…";
    try {
      const r = await api("/api/firmware/download", {});
      toast(r.updated ? `Downloaded firmware from GameSir Connect ${r.latest}` : "Already up to date");
      render();
    } catch (e) { status.textContent = e.message; dl.disabled = false; }
  } }, fw.extracted ? "Download latest firmware" : "Download firmware");
  wrap.append(h("div", { class: "btn-row" }, check, dl), status);
  wrap.append(h("p", { class: "callout" },
    "Installing firmware happens over the USB cable in update mode, which this tool can't do yet. Downloaded files are checked against GameSir's checksums and kept until then."));
  return wrap;
}

const PANELS = {
  controller: panelController, buttons: panelButtons, sticks: panelSticks, triggers: panelTriggers,
  vibration: panelVibration, lighting: panelLighting, profiles: panelProfiles, power: panelPower, firmware: panelFirmware,
};

let renderSeq = 0;
async function render() {
  app.section = sectionFromHash();
  renderChrome();
  const panel = $("#panel");
  const seq = ++renderSeq;
  let content;
  const def = SECTIONS.find((x) => x.id === app.section);
  if (!app.status.connected && !def.offline) content = panelConnect();
  else if (app.status.connected && !app.dev) content = h("p", { class: "busy" }, "Reading settings from the controller…");
  else content = await PANELS[app.section]();
  if (seq !== renderSeq) return;
  panel.replaceChildren(content);
}

// ---------------------------------------------------------------------------
// actions

async function reload(announce) {
  try {
    app.dev = await api("/api/state");
    if (announce) toast("Settings read from the controller");
  } catch (e) {
    toast(e.message, "error");
  }
  render();
}

async function scan() {
  app.scanning = true; render();
  try { app.found = await api("/api/scan", {}); } catch (e) { toast(e.message, "error"); app.found = null; }
  app.scanning = false; render();
}

async function connect(target) {
  try {
    app.status = await api("/api/connect", target);
  } catch (e) { toast(e.message, "error"); return; }
  app.dev = null; render();
  await reload(false);
}

async function disconnect() {
  try { app.status = await api("/api/disconnect", {}); } catch (e) { toast(e.message, "error"); }
  app.dev = null; app.found = null;
  go("controller");
}

async function restore() {
  const yes = await confirmDialog("Restore factory settings?",
    "Every setting on the controller goes back to how it shipped. This can't be undone.", "Restore");
  if (!yes) return;
  await save("/api/restore", { confirm: true }, (state) => { app.dev = state; toast("Factory settings restored"); render(); }, null);
}

// ---------------------------------------------------------------------------

async function start() {
  wireMap();
  window.addEventListener("hashchange", render);
  try {
    [app.meta, app.status] = await Promise.all([api("/api/meta"), api("/api/status")]);
  } catch (e) {
    $("#panel").replaceChildren(h("p", {}, e.message));
    return;
  }
  render();
  if (app.status.connected) await reload(false);
}

start();

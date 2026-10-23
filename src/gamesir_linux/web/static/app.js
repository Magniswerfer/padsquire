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
  for (const c of children.flat(Infinity)) {
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
  { id: "guide", label: "Button guide", offline: true },
  { id: "setup", label: "Setup and help", offline: true },
];

const app = {
  meta: null,
  status: { connected: false },
  dev: null,          // the controller's settings, as last read back
  section: "controller",
  scanning: false,
  found: null,
  devices: null,          // what is plugged in, polled (USB only)
  setup: null,            // this computer's checks
  connecting: false,
  userDisconnected: false,
  polls: 0,
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

function toggle(label, help, entry, onChange, readonly = false) {
  if (errOf(entry)) return row(label, help, unavailable(entry));
  const mark = savedMark();
  const id = `t-${Math.random().toString(36).slice(2)}`;
  const input = h("input", { type: "checkbox", role: "switch", "aria-labelledby": id, checked: !!val(entry), disabled: readonly });
  input.addEventListener("change", () => onChange(input.checked, mark));
  return row(label, help, [mark, h("label", { class: "switch" }, input, h("span"))], { id });
}

function featureToggle(feature, label, help) {
  const entry = app.dev.features[feature];
  // The controller ignores changes to some toggles by cable or receiver: show them, but say so.
  const readonly = app.status.mode === "usb" && app.meta.usb_readonly_features.includes(feature);
  const note = readonly ? "Can't be changed by cable or the receiver. Bluetooth isn't tested yet." : null;
  return toggle(label, [help, note].filter(Boolean).join(". "), entry, (on, mark) =>
    save("/api/feature", { feature, enabled: on }, (v) => { app.dev.features[feature] = { value: v }; }, mark), readonly);
}

function slider({ label, help, value, min = 0, max = 100, step = 1, unit = "", onInput, onCommit }) {
  const out = h("output", {}, `${value}${unit}`);
  const mark = savedMark();
  const input = h("input", { type: "range", min, max: Math.max(max, value), step, value, "aria-label": label });
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
    const via = app.status.mode === "simulated" ? "Simulated controller"
      : app.status.mode === "usb" ? (app.status.via === "dongle" ? "Connected through the 2.4 GHz receiver" : "Connected by cable")
      : "Connected over Bluetooth";
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
  if (id === "controller" && val(d.info)) {
    const own = app.status.via === "dongle" ? val(d.controller_firmware) : val(d.info).firmware_version;
    text = own ? `firmware ${own}` : "through the receiver";
  }
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
    setupNotice(),
    h("p", { class: "note" }, "Plug it in by cable or through the 2.4 GHz receiver and it's found and connected automatically."),
    h("p", { class: "lede" }, "Settings are read from the controller and written back to it, by cable, through the 2.4 GHz receiver or over Bluetooth. Nothing is stored on this computer."),
    h("ol", { class: "steps" },
      h("li", {}, "By cable: plug the controller in and hold Menu and View for 2 seconds until the light turns green (PC mode)."),
      h("li", {}, "Or through the 2.4 GHz receiver: plug the receiver in, turn the controller on, and hold Menu and View for 2 seconds until the light turns green."),
      h("li", {}, "Or over Bluetooth: turn the controller on in Android mode by holding Home and A for 2 seconds until the light turns yellow."),
      h("li", {}, "Find it below and connect.")),
  );
  const findBtn = h("button", { class: "btn primary", onclick: scan, disabled: app.scanning },
    app.scanning ? "Searching…" : "Find controllers");
  wrap.append(h("div", { class: "btn-row" }, findBtn,
    h("button", { class: "btn", onclick: () => connect({ simulate: true }) }, "Use the simulator")));

  if (app.found) {
    if (app.found.length === 0) {
      wrap.append(h("p", { class: "note" },
        "No Nova 2 Lite found. By cable or receiver, check the light is green. Over Bluetooth, check it is in Android mode and close to this computer. If it still doesn't appear, pair it once in your system's Bluetooth settings."));
    } else {
      wrap.append(h("ul", { class: "found" }, app.found.map((d) =>
        h("li", {}, h("span", {}, d.name, h("span", { class: "addr" }, d.link === "usb" ? `${d.via === "dongle" ? "2.4 GHz receiver" : "USB cable"}, ${d.address}` : d.address)),
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
  const dongle = app.status.mode === "usb" && app.status.via === "dongle";
  const where = app.status.mode !== "usb" ? `Connected to ${app.status.address}`
    : dongle ? `Connected through the 2.4 GHz receiver (${app.status.address})` : `Connected by cable (${app.status.address})`;
  // Through the receiver the firmware version in the basic info is the receiver's own.
  const own = val(d.controller_firmware);
  const firmware = dongle
    ? [fact("Firmware", own, own ? regionName(own) : "not reported"), fact("Receiver firmware", info && info.firmware_version)]
    : [fact("Firmware", info && info.firmware_version, info ? regionName(info.firmware_version) : null)];
  return h("div", {},
    h("h1", {}, app.status.name || "Nova 2 Lite"),
    h("p", { class: "lede" }, app.status.mode === "simulated"
      ? "This is the simulator: changes behave like the real controller but go nowhere."
      : `${where}. Every value shown here was read back from the controller.`),
    h("dl", { class: "facts" },
      fact("Battery", bat ? `${bat.level}%` : null, bat && bat.charging ? "charging" : null),
      ...firmware,
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
  if (!d.remaps) return panelBackButtons();
  const labels = app.meta.keys;
  const wrap = h("div", {},
    h("h1", {}, "Buttons"),
    h("p", { class: "lede" }, "Choose what each button does. A remapped button sends the button you pick instead of its own; the change is stored on the controller."));
  const rows = h("div", { class: "rows two" });
  for (const key of app.meta.remap_sources) {
    const entry = d.remaps[key];
    if (errOf(entry)) { rows.append(row(labels[key], null, unavailable(entry))); continue; }
    const targets = val(entry);
    const combo = targets.length > 1 ? targets.join("+") : null;
    const select = h("select", { "aria-label": labels[key] },
      h("option", { value: "" }, ["L4", "R4"].includes(key) ? "Nothing (default)" : "Its own function"),
      h("optgroup", { label: "Act as" }, app.meta.remap_targets.filter((t) => t !== key).map((t) => h("option", { value: t }, labels[t]))),
      combo ? h("option", { value: combo, disabled: true }, `${targets.map((t) => labels[t]).join(" + ")} together`) : null);
    select.value = combo || targets[0] || "";
    const mark = savedMark();
    select.addEventListener("change", () =>
      save("/api/remap", { key, targets: select.value ? [select.value] : [] }, (v) => { d.remaps[key] = { value: v }; }, mark));
    const help = targets.length ? `Now acts as ${targets.map((t) => labels[t]).join(" + ")}` : null;
    rows.append(row(labels[key], help, [mark, select]));
  }
  wrap.append(rows,
    h("div", { class: "btn-row" }, h("button", { class: "btn", onclick: resetRemaps }, "Reset all buttons")));
  if (app.status.mode === "usb") {
    // What the computer receives right now: a remapped button lights up as its new button.
    const shown = ["A", "B", "X", "Y", "LB", "RB", "LT", "RT", "UP", "DOWN", "LEFT", "RIGHT", "SELECT", "START", "L3", "R3", "HOME"];
    const chips = Object.fromEntries(shown.map((k) => [k, h("span", { class: "chip" }, labels[k] || k)]));
    const signal = h("span", { class: "signal" }, "Connecting…");
    let last = 0;
    wrap.append(h("h2", {}, "Pressed now ", signal),
      h("p", { class: "note" }, "Press a button to see what the computer receives. L4, R4 and Capture aren't reported in this mode, so they only show up through what they're mapped to."),
      h("div", { class: "chips", "aria-live": "off" }, Object.values(chips)));
    liveWatch(() => {
      const ok = Date.now() - last < 2500;
      signal.textContent = ok ? "receiving" : "no live signal";
      signal.classList.toggle("off", !ok);
    });
    app.live = (st) => {
      last = Date.now();
      const down = new Set(st.pressed || []);
      for (const [k, el] of Object.entries(chips)) el.classList.toggle("on", down.has(k));
    };
  }
  wrap.append(h("h2", {}, "Layout"),
    h("div", { class: "rows" },
      featureToggle("DPAD_DIAGONAL_LOCK", "Lock D-pad diagonals", "Only up, down, left and right register"),
      featureToggle("SWAP_LT_LB", "Swap LB and LT"),
      featureToggle("SWAP_RT_RB", "Swap RB and RT")));
  wrap.append(h("p", { class: "note" }, "On this firmware a button can act as another button, but not as a stick direction, and it can't be switched off."),
    h("p", { class: "note" }, "If the controller stops responding after a change, turn it off and on again (hold Home and M for 10 seconds). The change is kept."));
  return wrap;
}

async function resetRemaps() {
  const changed = app.meta.remap_sources.filter((k) => (val(app.dev.remaps[k]) || []).length);
  if (!changed.length) { toast("Every button already does its own function"); return; }
  const yes = await confirmDialog("Reset all buttons?", `${changed.length} remapped button${changed.length > 1 ? "s go" : " goes"} back to its own function.`, "Reset");
  if (!yes) return;
  for (const key of changed) {
    try { app.dev.remaps[key] = { value: await api("/api/remap", { key, targets: [] }) }; }
    catch (e) { toast(e.message, "error"); break; }
  }
  render();
}

// Over Bluetooth only the back buttons can be remapped, with the GameSir app's own command.
function panelBackButtons() {
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
    "Every other button can be remapped by cable or through the 2.4 GHz receiver."));
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
  // live stick position, -1..1 per axis (y: up is negative), clamped to the rim
  const move = (x, y) => {
    const m = Math.hypot(x, y);
    const k = m > 1 ? 1 / m : 1;
    dot.setAttribute("cx", 80 + 70 * x * k);
    dot.setAttribute("cy", 80 + 70 * y * k);
    return Math.min(m, 1);
  };
  return { svg, update, move };
}

function panelSticks() {
  const d = app.dev;
  const wrap = h("div", {},
    h("h1", {}, "Sticks"),
    h("p", { class: "lede" }, "Dead zones decide how far a stick must move before it counts, and where it reaches full tilt. Shaded areas are ignored."));
  const pair = h("div", { class: "pair" });
  const live = [];
  for (const [side, prefix, axes] of [["Left stick", "LEFT", ["lx", "ly"]], ["Right stick", "RIGHT", ["rx", "ry"]]]) {
    const zones = { inner: `${prefix}_STICK_INNER`, outer: `${prefix}_STICK_OUTER`, anti: `${prefix}_STICK_ANTI` };
    const entries = Object.fromEntries(Object.entries(zones).map(([k, z]) => [k, d.dead_zones[z]]));
    const values = Object.fromEntries(Object.entries(entries).map(([k, e]) => [k, val(e) ?? 0]));
    const pic = deadZonePicture();
    pic.update(values);
    const readout = h("p", { class: "live-readout", "aria-live": "off" });
    live.push((st) => { readout.textContent = `Tilt ${Math.round(100 * pic.move(st[axes[0]], st[axes[1]]))}%`; });
    const col = h("div", {}, h("h2", {}, side), pic.svg, readout,
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
  wrap.append(h("p", { class: "note" }, liveNote("The dot shows where each stick is now, as the computer receives it. ") +
    "Values are shown as percentages of full travel. That's an assumption until it has been checked on a real controller."));
  app.live = (st) => live.forEach((f) => f(st));
  return wrap;
}

function panelTriggers() {
  const d = app.dev;
  const wrap = h("div", {},
    h("h1", {}, "Triggers"),
    h("p", { class: "lede" }, "Set how much of each trigger's travel is ignored at the start and the end of the pull."));
  const quick = val(d.trigger_quick);
  const pair = h("div", { class: "pair" });
  const live = [];
  for (const [side, prefix, qi, axis] of [["Left trigger", "LEFT", 0, "lt"], ["Right trigger", "RIGHT", 1, "rt"]]) {
    const zones = { start: `${prefix}_TRIGGER_FRONT`, end: `${prefix}_TRIGGER_BACK` };
    const values = { start: val(d.dead_zones[zones.start]) ?? 0, end: val(d.dead_zones[zones.end]) ?? 0 };
    const startEl = h("span", { class: "dead start" });
    const endEl = h("span", { class: "dead end" });
    const paint = () => { startEl.style.width = `${values.start}%`; endEl.style.width = `${values.end}%`; };
    paint();
    const levelEl = h("span", { class: "level" });
    const readout = h("p", { class: "live-readout", "aria-live": "off" });
    live.push((st) => {
      levelEl.style.width = `${100 * st[axis]}%`;
      readout.textContent = `Pressed ${Math.round(100 * st[axis])}%`;
    });
    const col = h("div", {}, h("h2", {}, side),
      h("div", { class: "travel", role: "img", "aria-label": `${side}: first ${values.start}% and last ${values.end}% of travel ignored` }, startEl, endEl, levelEl),
      h("div", { class: "travel-scale" }, h("span", {}, "Released"), h("span", {}, "Fully pressed")), readout);
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
  const note = liveNote("The bar shows how far each trigger is pulled now, as the computer receives it.");
  if (note) wrap.append(h("p", { class: "note" }, note));
  app.live = (st) => live.forEach((f) => f(st));
  return wrap;
}

// ---------------------------------------------------------------------------
// live input: stick and trigger positions streamed while Sticks or Triggers is open

function liveNote(text) {
  return app.status.mode === "usb" ? text : "";
}

let liveAbort = null;
let liveTimer = null;

function stopLive() {
  if (liveAbort) { liveAbort.abort(); liveAbort = null; }
  clearInterval(liveTimer);
  liveTimer = null;
}

// Runs `check` every second while the live view is open (stopped by stopLive).
function liveWatch(check) {
  clearInterval(liveTimer);
  liveTimer = setInterval(check, 1000);
}

async function startLive(onState) {
  if (liveAbort) { liveAbort.abort(); liveAbort = null; }   // replace the stream, keep the watch
  if (app.status.mode !== "usb") return;
  const ctl = new AbortController();
  liveAbort = ctl;
  try {
    // fetch, not EventSource: the server wants the X-GameSir header on every API call
    const res = await fetch("/api/input", { headers: { "X-GameSir": "1" }, signal: ctl.signal });
    if (!res.ok || !res.body) return;
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (line) onState(JSON.parse(line));
      }
    }
  } catch {
    // aborted on navigation, or the controller went away
  }
}

function panelVibration() {
  const d = app.dev;
  const wrap = h("div", {}, h("h1", {}, "Vibration"), h("p", { class: "lede" }, "Rumble strength for the motor in each grip."));
  if (errOf(d.vibration)) { wrap.append(unavailable(d.vibration)); return wrap; }
  const v = val(d.vibration);
  const rows = h("div", { class: "rows" });
  for (const [field, label, motor] of [["left_grip", "Left grip", "left"], ["right_grip", "Right grip", "right"]]) {
    const item = slider({
      label, value: v[field], unit: "%", step: 25,         // the controller stores multiples of 25
      onCommit: (n, mark) => save("/api/vibration", { [field]: n }, (got) => { d.vibration = { value: got }; }, mark),
    });
    const test = h("button", { class: "btn small", onclick: async () => {
      test.disabled = true;
      try { await api("/api/vibration/test", { motor, strength: val(d.vibration)[field] || 75 }); }
      catch (e) { toast(e.message, "error"); }
      test.disabled = false;
    } }, "Test");
    item.querySelector(".row-control").append(test);
    rows.append(item);
  }
  wrap.append(rows,
    h("p", { class: "note" }, "Test runs that motor for a moment at the strength set above."),
    h("p", { class: "note" }, "The Nova 2 Lite has a motor in each grip and none in the triggers. Strength goes in steps of 25%, the only values the controller keeps; out of the box it's 75%."));
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
  const receiver = fw.dongle_version;
  const shipped = fw.available.find((f) => f.target === "dongle");
  const receiverNote = receiver && shipped ? (Number(receiver) >= shipped.version ? "up to date" : `${shipped.version} is available`) : null;
  wrap.append(h("dl", { class: "facts" },
    h("div", { class: "fact" }, h("dt", {}, "On the controller"), h("dd", {},
      device || (receiver ? "Unknown" : "Not connected"),
      device ? h("small", {}, regionName(device)) : null)),
    receiver ? h("div", { class: "fact" }, h("dt", {}, "On the 2.4 GHz receiver"), h("dd", {}, receiver, receiverNote ? h("small", {}, receiverNote) : null)) : null,
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

  upd.fw = fw;
  upd.box = h("div", { class: "update", "aria-live": "polite" }, h("p", { class: "busy" }, "Looking for the controller…"));
  upd.shown = "";
  wrap.append(h("h2", {}, "Install firmware"), upd.box);
  pollUpdate();
  return wrap;
}

// ---------------------------------------------------------------------------
// installing firmware: the update-mode steps run on the server as jobs; this polls them

const upd = { timer: null, box: null, fw: null, data: null, shown: "", backup: null, file: null,
              checks: {}, seenJob: null };

function stopUpdatePoll() {
  clearTimeout(upd.timer);
  upd.timer = null;
}

async function pollUpdate() {
  stopUpdatePoll();
  if (!upd.box) return;
  if (!upd.box.isConnected) {
    // The panel is built before render() puts it on the page: wait for it, unless we left.
    if (app.section === "firmware") upd.timer = setTimeout(pollUpdate, 100);
    return;
  }
  try { upd.data = await api("/api/update"); } catch (e) { upd.data = { error: e.message }; }
  if (!upd.box.isConnected) return;
  await afterJob(upd.data.job);
  await ensureCheck();
  const key = JSON.stringify([upd.data, upd.backup, upd.file, upd.checks[checkKey()]]);
  if (key !== upd.shown) {
    upd.shown = key;
    upd.box.replaceChildren(...updateView());
  }
  const running = upd.data.job && upd.data.job.state === "running";
  upd.timer = setTimeout(pollUpdate, running ? 500 : 2000);
}

// When a job has just finished: keep the rest of the app in step with the controller.
async function afterJob(job) {
  if (!job || job.state === "running" || upd.seenJob === `${job.id}${job.state}`) return;
  const first = upd.seenJob === null;
  upd.seenJob = `${job.id}${job.state}`;
  if (first) return;                 // finished before this page was opened
  if (job.state === "failed") { toast(job.error, "error"); return; }
  if (job.kind === "backup") { upd.backup = job.result.backup; toast("Backup saved"); }
  if (job.kind === "restore") toast("Backup written back. Restart it to use it.");
  if (job.kind === "install") toast(`Firmware ${job.result.version} installed. Restart it to use it.`);
  if (job.kind === "enter") { app.status = await api("/api/status"); app.dev = null; renderChrome(); }
  if (job.kind === "restart") {
    try {
      const usb = (await api("/api/scan", {})).filter((d) => d.link === "usb");
      if (usb.length === 1) await connect(usb[0]);
      toast(job.result && job.result.device === "receiver" ? "The receiver is back in normal mode" : "The controller is back in normal mode");
    } catch (e) { toast(e.message, "error"); }
    upd.fw = await api("/api/firmware").catch(() => upd.fw);
  }
}

const checkKey = () => `${upd.file}|${upd.backup}|${upd.data && upd.data.installed_version}`;

async function ensureCheck() {
  if (!upd.file || !upd.backup || upd.checks[checkKey()]) return;
  const key = checkKey();
  try { upd.checks[key] = await api("/api/update/check", { file: upd.file, backup: upd.backup }); }
  catch (e) { upd.checks[key] = { allowed: false, errors: [e.message], warnings: [] }; }
}

async function runStep(path, body, title, text, okLabel) {
  if (title && !(await confirmDialog(title, text, okLabel))) return;
  try {
    upd.data.job = await api(path, body || {});
    upd.shown = "";
  } catch (e) { toast(e.message, "error"); }
  pollUpdate();
}

const addr = (n) => `0x${n.toString(16)}`;

function updateView() {
  const d = upd.data;
  if (d.error) return [h("p", { class: "note" }, d.error)];
  const job = d.job;
  const running = job && job.state === "running";
  const inUpdate = d.mode === "download";
  const out = [];

  const notice = setupNotice(true);
  if (notice) out.push(notice);
  const device = d.device || "controller";
  const thing = device === "receiver" ? "receiver" : "controller";
  const where = inUpdate ? `${thing === "receiver" ? "Receiver" : "Controller"} in update mode`
    : d.mode === "normal" ? "Normal mode" : "Not found";
  out.push(h("dl", { class: "facts" },
    h("div", { class: "fact" }, h("dt", {}, "Device"), h("dd", {}, where,
      inUpdate && d.identity ? h("small", {}, `${d.identity.chip}, ${d.identity.flash_size / 1024} KB flash`) : null)),
    h("div", { class: "fact" }, h("dt", {}, "Firmware on it"), h("dd", {}, (inUpdate && d.installed_version) || "Unknown"))));

  // 1. update mode
  const steps = h("ol", { class: "steps" });
  let first;
  if (inUpdate) {
    first = h("p", { class: "note" }, thing === "receiver"
      ? "The receiver is in update mode. The controller can't connect through it until you restart it."
      : "The controller is in update mode. Its normal functions are off until you restart it.");
  } else if (d.mode === "normal") {
    const enter = (target, title, text) => runStep("/api/update/enter", { confirm: true, target }, title, text, "Switch");
    first = h("div", {}, h("p", { class: "note" }, "Nothing is written in this step."),
      h("div", { class: "btn-row" },
        d.cable ? h("button", { class: "btn primary", disabled: running, onclick: () => enter("controller", "Switch the controller to update mode?",
          "The controller stops working as a gamepad until you restart it from this page. Nothing is written yet.") }, "Switch the controller to update mode") : null,
        d.receiver ? h("button", { class: `btn${d.cable ? "" : " primary"}`, disabled: running, onclick: () => enter("receiver", "Switch the receiver to update mode?",
          "The controller can't connect through the receiver until you restart the receiver from this page. Nothing is written yet.") }, "Switch the receiver to update mode") : null),
      h("p", { class: "note" }, "The controller's firmware is installed by cable; the receiver's through the receiver itself."));
  } else first = h("p", { class: "note" }, "Connect the controller by cable, or plug in the 2.4 GHz receiver with the controller on, and switch it to PC/Xbox mode: hold Menu + View until the Home light is green.");
  steps.append(h("li", { class: inUpdate ? "done" : "" }, h("h3", {}, "Switch to update mode"), first));

  // 2. backup
  const mine = d.identity ? d.backups.filter((b) => b.chip_key === d.identity.chip_key && b.jedec_id === d.identity.jedec_id) : d.backups;
  if (upd.backup && !mine.some((b) => b.name === upd.backup)) upd.backup = null;
  if (!upd.backup && mine.length) upd.backup = mine[0].name;
  const backupStep = h("li", { class: mine.length ? "done" : "" }, h("h3", {}, `Back up the ${thing}`));
  if (mine.length) {
    const sel = h("select", { "aria-label": "Backup", disabled: running, onchange: (e) => { upd.backup = e.target.value; pollUpdate(); } },
      mine.map((b) => h("option", { value: b.name, selected: b.name === upd.backup }, `${b.created.replace("T", " ").slice(0, 16)}  (${b.name})`)));
    backupStep.append(h("p", { class: "note" }, d.identity ? `Backups of this ${thing}:` : "Backups on this computer:"), sel);
  } else {
    backupStep.append(h("p", { class: "note" }, thing === "receiver"
      ? "There is no backup of this receiver yet. Every write needs one: it holds the firmware and the pairing with your controller."
      : "There is no backup of this controller yet. Every write needs one: it holds the firmware, settings, Bluetooth address and calibration."));
  }
  backupStep.append(h("div", { class: "btn-row" }, h("button", { class: `btn${mine.length ? "" : " primary"}`, disabled: running || !inUpdate,
    onclick: () => runStep("/api/update/backup") }, mine.length ? "Make another backup" : "Make a backup")),
    h("p", { class: "note" }, "Backups are saved in ~/.local/share/gamesir-linux/backups. Copy them somewhere safe."));
  steps.append(backupStep);

  // 3. install or restore
  const files = (upd.fw && upd.fw.available || []).filter((f) => f.target === (thing === "receiver" ? "dongle" : "controller"));
  if (upd.file && !files.some((f) => f.file === upd.file)) upd.file = null;
  if (!upd.file && files.length) upd.file = (files.find((f) => f.matches_device_region !== false && f.newer_than_device !== false) || files[0]).file;
  const install = h("li", {}, h("h3", {}, "Install firmware"));
  if (!files.length) install.append(h("p", { class: "note" }, "Download firmware above first."));
  else {
    install.append(h("select", { "aria-label": "Firmware", disabled: running, onchange: (e) => { upd.file = e.target.value; pollUpdate(); } },
      files.map((f) => h("option", { value: f.file, selected: f.file === upd.file },
        f.target === "dongle" ? `Receiver firmware ${f.version}` : `Firmware ${f.version}, ${f.region === "china" ? "China" : "International"} build`))));
    const check = upd.checks[checkKey()];
    if (!upd.backup) install.append(h("p", { class: "note" }, "Needs a backup of this controller."));
    else if (!check) install.append(h("p", { class: "note" }, "Checking…"));
    else if (check.allowed) {
      install.append(h("p", { class: "note ok" }, `Fits this ${thing}: ${check.partition} goes to ${addr(check.start)}–${addr(check.end - 1)}. ${thing === "receiver" ? "The pairing with your controller is kept." : "Settings, Bluetooth address and calibration are kept."}`));
    } else install.append(h("ul", { class: "problems" }, check.errors.map((e) => h("li", {}, e))));
    if (check && check.warnings.length) install.append(h("ul", { class: "warnings" }, check.warnings.map((w) => h("li", {}, w))));
    const f = files.find((x) => x.file === upd.file);
    install.append(h("div", { class: "btn-row" },
      h("button", { class: "btn primary", disabled: running || !inUpdate || !(check && check.allowed), onclick: () => runStep("/api/update/install",
        { file: upd.file, backup: upd.backup, confirm: true }, `Install firmware ${f && f.version}?`,
        `The firmware area of the ${thing} is erased and rewritten. Keep it plugged in until it finishes. If anything fails, it stays in update mode and you can run it again or write the backup back.`,
        "Install") }, `Install firmware ${f ? f.version : ""}`)));
  }
  install.append(h("p", { class: "note" }, thing === "receiver"
      ? "To go back to the firmware in a backup, write the backup back. The pairing stays as it is."
      : "To go back to the firmware in a backup, write the backup back. Settings, Bluetooth address and calibration stay as they are."),
    h("div", { class: "btn-row" }, h("button", { class: "btn", disabled: running || !inUpdate || !upd.backup, onclick: () => runStep("/api/update/restore",
      { backup: upd.backup, confirm: true }, "Write the backup back?",
      `The firmware area of the ${thing} is erased and rewritten with the firmware in the backup. Keep it plugged in until it finishes.`, "Write it back") },
      "Write the backup back")));
  steps.append(install);

  // 4. restart
  steps.append(h("li", {}, h("h3", {}, `Restart the ${thing}`),
    d.stay ? h("p", { class: "callout danger" }, "The last write did not verify. Don't restart or unplug the controller: run the same step again.") : null,
    h("div", { class: "btn-row" }, h("button", { class: "btn", disabled: running || !inUpdate || d.stay,
      onclick: () => runStep("/api/update/restart") }, `Restart ${thing}`))));
  out.push(steps);
  out.push(h("p", { class: "note" }, h("strong", {}, "Not known yet: "),
    "how to get the controller into update mode if its firmware no longer starts. The M + X + B shortcut goes through the firmware, so it wouldn't help then. That's why a write that fails keeps the controller in update mode until it has been done again: don't unplug or restart it in between."));

  // the job in progress, or the last one
  if (job) {
    const names = { enter: "Switching to update mode", identify: "Identifying the chip", backup: "Backing up",
                    restore: "Writing the backup back", install: "Installing firmware", restart: "Restarting" };
    const box = h("div", { class: `job ${job.state}` }, h("h3", {}, names[job.kind] || job.kind,
      job.state === "done" ? ": done" : job.state === "failed" ? ": failed" : "…"));
    if (running) {
      box.append(h("p", { class: "note" }, job.label || "Starting…"),
        h("progress", { max: job.total || 1, value: job.done || 0 }),
        ["restore", "install"].includes(job.kind) ? h("p", { class: "note" }, h("strong", {}, "Keep it plugged in until this finishes.")) : null);
    }
    if (job.error) box.append(h("p", { class: "callout danger" }, job.error));
    if (job.log.length) box.append(h("ol", { class: "log" }, job.log.map((l) => h("li", {}, l))));
    out.push(box);
  }
  return out;
}

// ---------------------------------------------------------------------------
// button guide: the controller map labelled like the manual, and how-tos in our own words

// [label, side, label y, anchor x, anchor y], in the map's viewBox coordinates
const GUIDE_LABELS = [
  ["LT left trigger", "l", -4, 100, 4],
  ["LB left bumper", "l", 18, 86, 22],
  ["Home button and light", "l", 36, 191, 50],
  ["View button", "l", 56, 158, 62],
  ["Left stick (press for L3)", "l", 95, 99, 95],
  ["L4 back button", "l", 133, 95, 141],
  ["D-pad", "l", 186, 151, 146],
  ["RT right trigger", "r", -4, 300, 4],
  ["RB right bumper", "r", 18, 314, 22],
  ["Menu button", "r", 38, 242, 62],
  ["Screenshot button", "r", 58, 209, 80],
  ["A, B, X and Y buttons", "r", 96, 297, 96],
  ["R4 back button", "r", 133, 305, 141],
  ["Right stick (press for R3)", "r", 162, 248, 146],
  ["M button", "r", 192, 210, 171],
];

function guideDrawing() {
  const svg = $("#pad").cloneNode(true);
  svg.removeAttribute("id");
  for (const el of svg.querySelectorAll("title, .part-label")) el.remove();
  for (const el of [svg, ...svg.querySelectorAll("*")]) {
    for (const a of ["tabindex", "role", "aria-label", "aria-disabled", "data-section"]) el.removeAttribute(a);
    el.classList.remove("part", "active", "offline");
  }
  svg.setAttribute("class", "pad guide-pad");
  svg.setAttribute("viewBox", "-205 -16 810 300");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "The Nova 2 Lite from the front, with every button labelled");
  for (const [label, side, ly, ax, ay] of GUIDE_LABELS) {
    const left = side === "l";
    const edge = left ? -10 : 410;
    // horizontal from the label, then a 45° run into the part, like the manual's leader lines
    const bend = left ? ax - Math.abs(ay - ly) : ax + Math.abs(ay - ly);
    svg.append(
      s("path", { class: "leader", d: `M${edge} ${ly} H${bend} L${ax} ${ay}` }),
      s("circle", { class: "anchor", cx: ax, cy: ay, r: 1.8 }));
    const text = s("text", { class: "callout-label", x: left ? edge - 6 : edge + 6, y: ly, "text-anchor": left ? "end" : "start" });
    text.textContent = label;
    svg.append(text);
  }
  return svg;
}

function table(head, rows) {
  return h("div", { class: "table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, head.map((c) => h("th", { scope: "col" }, c)))),
    h("tbody", {}, rows.map((r) => h("tr", {}, r.map((c, i) => i === 0 ? h("th", { scope: "row" }, c) : h("td", {}, c)))))));
}

function panelGuide() {
  return h("div", {},
    h("h1", {}, "Button guide"),
    h("p", { class: "lede" }, "Every button on the Nova 2 Lite, the modes and what the Home light means, and the shortcuts built into the controller."),
    guideDrawing(),
    h("h2", {}, "What the buttons do"),
    table(["Button", "What it does"], [
      ["Home", "Turns the controller on. Hold it for 5 seconds to turn it off. Its light shows the mode and the connection."],
      ["View and Menu", "The usual View and Menu buttons. Held together for 2 seconds, they switch mode on the cable or the receiver."],
      ["Screenshot", "Held with Home for 3 seconds, it puts the controller back into pairing."],
      ["M", "Shortcuts for turbo, back buttons, hair triggers and vibration (see below). Hold it on its own to show the current mode."],
      ["L4 and R4", "Two extra buttons on the back. Each can copy any other button."],
      ["Sticks", "Press a stick down for L3 or R3."],
      ["Trigger stop switches", "Two switches on the back that shorten the triggers' travel for quicker presses."],
      ["Reset hole", "A small hole on the back. Pressing the button inside with a paperclip forces the controller off."],
    ]),
    h("h2", {}, "Modes and lights"),
    h("p", { class: "note" }, "On the cable or the 2.4 GHz receiver the controller picks a mode by itself. To change it, hold View and Menu for 2 seconds; it cycles through the three."),
    table(["Mode", "Light", "Use it for"], [
      ["Xbox", "Green", "PC. This is the mode gamesir-linux configures, by cable or through the receiver."],
      ["Switch", "Red", "Nintendo Switch."],
      ["DS4", "Blue", "Android, and anything that expects a PlayStation controller."],
    ]),
    h("p", { class: "note" }, "Over Bluetooth, hold Home and one other button for 2 seconds. The light flashes quickly while it waits to pair."),
    table(["Bluetooth mode", "Hold", "Light", "Appears as"], [
      ["DS4 (PC, Android, iOS)", "Home\u00a0+\u00a0B", "Blue", "Wireless Controller"],
      ["Switch", "Home\u00a0+\u00a0Y", "Red", "Pair from Controllers, Change Grip/Order on the Switch"],
      ["Android", "Home\u00a0+\u00a0A", "Yellow", "GameSir-Nova 2 Lite"],
      ["G-Touch (Android)", "Home\u00a0+\u00a0RB", "Cyan", "GameSir-Nova 2 Lite_G"],
    ]),
    h("p", { class: "note" }, "To pair the 2.4 GHz receiver, hold Home and X for 2 seconds until the light flashes green, then press the pairing button on the receiver."),
    h("h2", {}, "Everyday use"),
    table(["", ""], [
      ["Turn on or off", "Press Home to turn on. Hold Home for 5 seconds to turn off."],
      ["Sleep", "Turns itself off after 10 minutes without use while connected."],
      ["Battery", "Below 15%, the Home light flashes red. Charging while off, the light slowly pulses."],
      ["Home light", "Solid: connected. Flashing quickly: pairing. Flashing slowly: reconnecting to the last device."],
      ["Pair again", "Hold Screenshot and Home for 3 seconds."],
      ["Check the mode", "Hold M."],
    ]),
    h("h2", {}, "M button shortcuts"),
    table(["Shortcut", "How"], [
      ["Program a back button", "Hold M and L4 (or R4) until the light flashes slowly in white. Press the button it should copy, then L4 (or R4) again to save. Leaving without choosing a button clears it."],
      ["Turbo", "Hold M and press A, B, X, Y, LB, RB, LT or RT. Repeat to turn it off. Double-press M to clear every turbo button."],
      ["Stick dead zone", "Hold M and press a stick down for 2 seconds to turn that stick's dead zone on or off."],
      ["Hair trigger", "Hold M and LT (or RT) for 2 seconds. The trigger stop switch must be on long travel."],
      ["Vibration", "Hold M and press View for weaker, Menu for stronger."],
      ["Swap layout", "Hold M and A for 2 seconds to swap A with B and X with Y."],
    ]),
    h("h2", {}, "Calibration"),
    h("p", { class: "note" }, "Set both trigger stop switches to long travel. Hold View, Menu and Home together until the light flashes slowly in white. Turn both sticks around their full range 2 or 3 times, press both triggers all the way down 3 times, then press A to finish. After flipping a trigger stop switch, press that trigger fully 3 times so it learns the new travel."),
    h("h2", {}, "The full manual"),
    h("p", { class: "note" }, "This page is our own summary. GameSir's manual, with the safety and regulatory information, comes in the box and is on ",
      h("a", { href: "https://gamesir.com/support/manuals", target: "_blank", rel: "noopener noreferrer" }, "GameSir's manuals page"), "."),
  );
}

const PANELS = {
  controller: panelController, buttons: panelButtons, sticks: panelSticks, triggers: panelTriggers,
  vibration: panelVibration, lighting: panelLighting, profiles: panelProfiles, power: panelPower, firmware: panelFirmware,
  guide: panelGuide, setup: panelSetup,
};

let renderSeq = 0;
async function render() {
  app.section = sectionFromHash();
  stopLive();
  stopUpdatePoll();
  app.live = null;
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
  if (app.live) startLive(app.live);
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
  if (app.connecting) return;
  app.connecting = true;
  try {
    app.status = await api("/api/connect", target);
    app.userDisconnected = false;
  } catch (e) { toast(e.message, "error"); return; }
  finally { app.connecting = false; }
  app.dev = null; render();
  await reload(false);
}

async function disconnect() {
  try { app.status = await api("/api/disconnect", {}); } catch (e) { toast(e.message, "error"); }
  app.dev = null; app.found = null;
  app.userDisconnected = true;     // no automatic reconnect until it is unplugged
  go("controller");
}

// Plugging in and out: poll what is on USB (sysfs, cheap) and act on changes. Bluetooth is not
// polled: a scan takes seconds, so it stays behind the Find button.
async function watchDevices() {
  try {
    const d = await api("/api/devices");
    const before = JSON.stringify(app.devices && app.devices.usb);
    app.devices = d;
    const updating = (upd.data && upd.data.job && upd.data.job.state === "running") || d.download.length > 0;
    if (!d.usb.length) app.userDisconnected = false;
    if (d.lost) {
      app.status = d.status; app.dev = null;
      toast("The controller was disconnected", "error");
      render();
    } else if (!app.status.connected && !updating) {
      if (d.usb.length === 1 && !app.userDisconnected && !app.connecting) {
        toast(d.usb[0].via === "dongle" ? "Found the controller through the 2.4 GHz receiver" : "Found the controller on the cable");
        await connect(d.usb[0]);
      } else if (JSON.stringify(d.usb) !== before) {
        app.found = d.usb.concat((app.found || []).filter((x) => x.link === "ble"));
        if (!SECTIONS.find((x) => x.id === app.section).offline) render();
      }
    }
    // Re-check this computer now and then, so the notices go away once a command has been run.
    if (++app.polls % 10 === 0) {
      const setup = await api("/api/setup");
      if (JSON.stringify(setup) !== JSON.stringify(app.setup)) {
        app.setup = setup;
        if (["setup", "firmware"].includes(app.section) || !app.status.connected) render();
      }
    }
  } catch { /* the server is gone; api() said so elsewhere */ }
  setTimeout(watchDevices, 2000);
}

// ---------------------------------------------------------------------------
// setup and help: what this computer still needs, and what to do when something goes wrong

function copyButton(text) {
  const btn = h("button", { class: "btn small", onclick: async () => {
    try { await navigator.clipboard.writeText(text); btn.textContent = "Copied"; }
    catch { btn.textContent = "Select and copy it"; }
    setTimeout(() => { btn.textContent = "Copy"; }, 1600);
  } }, "Copy");
  return btn;
}

function command(text) {
  return h("div", { class: "command" }, h("code", {}, text), copyButton(text));
}

// What this computer still lacks, said on the front page and before firmware steps.
function missing(need) {
  return (app.setup || []).filter((c) => !c.ok && c.need === need && c.id !== "device");
}

function setupNotice(only) {
  const now = only ? [] : missing("settings");
  const later = missing("updates");
  if (!now.length && !later.length) return null;
  const names = (list) => list.map((c) => c.title).join(", ");
  return h("div", {},
    now.length ? h("p", { class: "callout danger" },
      `Before the controller can be used, this computer needs a one-time setup: ${names(now)}. It takes one command; `,
      h("a", { href: "#setup" }, "see Setup and help"), " for it and what it does.") : null,
    later.length ? h("p", { class: "callout" },
      `Before firmware updates and backups work: ${names(later)}. `,
      h("a", { href: "#setup" }, "See Setup and help"), " for what to run and why.") : null);
}

const NEEDS = { settings: "Needed for everything", updates: "Needed for firmware updates and backups", optional: "Optional" };

async function panelSetup() {
  const wrap = h("div", {}, h("h1", {}, "Setup and help"),
    h("p", { class: "lede" }, "What this computer needs to work with the controller, checked just now. Each command comes with what it does and links to its documentation: read that first, run it in a terminal, then check again."));
  try { app.setup = await api("/api/setup"); }
  catch (e) { wrap.append(h("p", { class: "note" }, e.message)); return wrap; }
  const list = h("ol", { class: "steps" });
  for (const c of app.setup) {
    list.append(h("li", { class: c.ok ? "done" : "" },
      h("h3", {}, c.title, h("span", { class: "need" }, NEEDS[c.need])),
      h("p", { class: "note" }, c.detail),
      c.command ? command(c.command) : null,
      c.command && c.explain.length ? h("details", { class: "explain" }, h("summary", {}, "What this does"),
        h("ul", {}, c.explain.map((x) => h("li", {}, x))),
        c.file ? [h("p", {}, `The file, ${c.file.path}:`), h("pre", { class: "file" }, c.file.content)] : null,
        c.docs.length ? h("p", {}, "Documentation: ", c.docs.map((d, i) => [i ? ", " : "",
          h("a", { href: d.url, target: "_blank", rel: "noopener noreferrer" }, d.title)])) : null) : null,
      c.id === "firmware" && !c.ok ? h("div", { class: "btn-row" }, h("button", { class: "btn", onclick: () => go("firmware") }, "Go to Firmware")) : null));
  }
  wrap.append(list, h("div", { class: "btn-row" }, h("button", { class: "btn", onclick: render }, "Check again")));

  const qa = (q, ...a) => h("details", { class: "faq" }, h("summary", {}, q), ...a.map((x) => typeof x === "string" ? h("p", {}, x) : x));
  wrap.append(h("h2", {}, "If something doesn't work"),
    qa("The controller isn't found",
      "Check the Home light is green: hold Menu and View for 2 seconds; each press moves to the next mode (green, red, blue). In blue mode it pretends to be a PlayStation controller and can't be configured.",
      "By cable, try another cable or port: some cables only charge. Through the receiver, turn the controller on and wait for it to link before switching to green.",
      "If it shows up but can't be opened, the device permissions above aren't in place yet, or the controller was plugged in before they were: unplug it and plug it in again."),
    qa("The live view or the controller stops responding",
      "Turn the controller off and on again: hold Home and M for about 10 seconds, then turn it on and switch to green. Settings you changed are kept. This has been seen through the receiver right after a change, and is being looked into."),
    qa("Bluetooth doesn't find it",
      "Put the controller in Android mode: hold Home and A for 2 seconds until the light flashes yellow. Pair it once in your system's Bluetooth settings, then use Find controllers here."),
    qa("Switching to update mode doesn't work",
      "The sg module must be loaded (see above). Use a USB port on the computer itself or a powered USB 2 hub; some hubs disturb the controller's update mode.",
      "Nothing has been written at that point. To get the controller back: unplug it, hold Home and M for about 10 seconds, and turn it on again. The receiver: unplug it and plug it back in."),
    qa("A firmware write fails",
      "The controller or receiver stays in update mode on purpose. Don't unplug it and don't restart it: run the same step again. If it keeps failing, write your backup back the same way. Every write is checked block by block, and nothing outside the firmware area is ever touched."),
    qa("Where are my backups?",
      "In ~/.local/share/gamesir-linux/backups, one file per backup with a .json next to it. They hold the firmware and your controller's Bluetooth pairing and calibration: copy them somewhere safe, and don't share them."),
    qa("Starting this app",
      command("gamesir-linux gui"),
      "It runs on this computer only (127.0.0.1) and opens your browser with a one-time link. Stop it with Ctrl+C in its terminal."));
  return wrap;
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
  try { app.setup = await api("/api/setup"); } catch { /* shown on the Setup page */ }
  render();
  if (app.status.connected) await reload(false);
  watchDevices();
}

start();

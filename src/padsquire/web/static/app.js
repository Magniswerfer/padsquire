// SPDX-License-Identifier: AGPL-3.0-or-later
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
  const opt = { method: body === undefined ? "GET" : "POST", headers: { "X-PadSquire": "1" } };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opt);
  } catch {
    throw new Error("Lost contact with PadSquire. Is it still running?");
  }
  let json;
  try { json = await res.json(); } catch { throw new Error(`Unexpected reply from PadSquire (HTTP ${res.status}).`); }
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

const uid = (prefix) => `${prefix}-${Math.random().toString(36).slice(2)}`;

// an icon from ds/icons.svg
function icon(name) {
  const svg = s("svg", { class: "icon", "aria-hidden": "true" });
  svg.append(s("use", { href: `/static/ds/icons.svg#${name}` }));
  return svg;
}

// a notice: kind is "", "ok", "warn" or "danger"; children are its text, paragraphs or buttons
// the mark on a step that's done, in place of its number
const stepDone = () => h("span", { class: "step-done" }, icon("check"));

// A button that shows it's working while fn runs: disabled, a spinner, and a label saying what
// happens ("Backing up…"); back to itself afterwards, if it is still on the page.
async function working(button, label, fn) {
  const idle = button.textContent;
  button.disabled = true;
  button.classList.add("working");
  button.setAttribute("aria-busy", "true");
  button.textContent = label;
  try { return await fn(); }
  finally {
    button.disabled = false;
    button.classList.remove("working");
    button.removeAttribute("aria-busy");
    button.textContent = idle;
  }
}

// The same for a firmware step, which runs on the server as a job and outlives the click: the
// button that started it keeps saying so for as long as the job runs.
function jobButton(job, kinds, label, button) {
  if (job && job.state === "running" && kinds.includes(job.kind)) {
    button.disabled = true;
    button.classList.add("working");
    button.setAttribute("aria-busy", "true");
    button.textContent = label;
  }
  return button;
}

function notice(kind, ...children) {
  const name = { ok: "check", warn: "alert", danger: "alert" }[kind] || "info";
  return h("div", { class: `notice${kind ? ` ${kind}` : ""}` }, icon(name), h("div", { class: "notice-body" }, ...children));
}

function row(label, help, control, extra = {}) {
  return h("div", { class: `setting${extra.stack ? " stack" : ""}` },
    h("div", {}, h("span", { class: "label", id: extra.id }, label), help ? h("span", { class: "hint" }, help) : null),
    h("div", { class: "setting-control" }, control));
}

function unavailable(entry) {
  return h("span", { class: "chip warn unavailable" }, `Not available: ${errOf(entry)}`);
}

function toggle(label, help, entry, onChange, readonly = false) {
  if (errOf(entry)) return row(label, help, unavailable(entry));
  const mark = savedMark();
  const id = uid("t");
  const input = h("input", { class: "switch", type: "checkbox", role: "switch", "aria-labelledby": id, checked: !!val(entry), disabled: readonly });
  input.addEventListener("change", () => onChange(input.checked, mark));
  return row(label, help, [mark, input], { id });
}

function featureToggle(feature, label, help) {
  const entry = app.dev.features[feature];
  // The controller ignores changes to some toggles by cable or receiver: show them, but say so.
  const readonly = app.status.mode === "usb" && app.meta.usb_readonly_features.includes(feature);
  const note = readonly ? "Can't be changed by cable or the receiver. Bluetooth isn't tested yet." : null;
  return toggle(label, [help, note].filter(Boolean).join(". "), entry, (on, mark) =>
    save("/api/feature", { feature, enabled: on }, (v) => { app.dev.features[feature] = { value: v }; }, mark), readonly);
}

// --fill paints the range's track up to the thumb (WebKit and Blink need it; Firefox has its own)
function fill(input) {
  const span = (+input.max - +input.min) || 1;
  input.style.setProperty("--fill", `${(input.value - input.min) / span * 100}%`);
}

function slider({ label, help, value, min = 0, max = 100, step = 1, unit = "", onInput, onCommit }) {
  const id = uid("r");
  const out = h("output", { class: "value", for: id }, `${value}${unit}`);
  const mark = savedMark();
  const input = h("input", { class: "range", type: "range", id, min, max: Math.max(max, value), step, value, "aria-label": label });
  fill(input);
  input.addEventListener("input", () => { fill(input); out.textContent = `${input.value}${unit}`; onInput && onInput(+input.value); });
  input.addEventListener("change", () => onCommit(+input.value, mark));
  return row(label, help, h("div", { class: "range-row" }, input, out, mark), { stack: true });
}

function segmented(name, options, current, onChange) {
  const group = h("div", { class: "segmented", role: "radiogroup" });
  for (const [value, text] of options) {
    const id = uid(name);
    const input = h("input", { type: "radio", name, id, value, checked: value === current });
    input.addEventListener("change", () => onChange(value));
    group.append(input, h("label", { for: id }, text));
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
    // phones get the short label (app.css); the full one stays for screen readers and as a tooltip
    const short = app.status.mode === "simulated" ? "Simulated"
      : app.status.mode === "usb" ? (app.status.via === "dongle" ? "Receiver" : "Cable") : "Bluetooth";
    st.append(h("span", { class: "chip ok", title: via }, icon("check"),
      h("span", { class: "long" }, via), h("span", { class: "short", "aria-hidden": "true" }, short)));
    const bat = app.dev && val(app.dev.battery);
    if (bat) {
      st.append(h("span", { class: "battery", title: bat.charging ? "Charging" : "Battery" },
        batteryIcon(bat.level), `${bat.level}%${bat.charging ? ", charging" : ""}`));
    }
    st.append(h("button", { class: "button small", onclick: disconnect }, "Disconnect"));
  } else {
    st.append(h("span", { class: "chip" }, icon("plug"), h("span", { class: "long" }, "No controller connected"),
      h("span", { class: "short", "aria-hidden": "true" }, "Not connected")));
  }

  const nav = $("#nav");
  nav.replaceChildren();
  for (const sec of SECTIONS) {
    const usable = app.status.connected || sec.offline || sec.id === "controller";
    nav.append(h("li", {}, h("a", {
      href: usable ? `#${sec.id}` : null,
      "aria-current": app.section === sec.id ? "page" : null,
      "aria-disabled": usable ? null : "true",
    }, sec.label, sectionHint(sec.id))));
  }

  navToCurrent();

  $("#pad").classList.toggle("offline", !app.status.connected);
  // The Home light colour is the user's own: it colours the preview and the model's Home light.
  const light = app.dev && val(app.dev.light);
  const colour = light ? hex(light.rgb) : null;
  if (colour) document.documentElement.style.setProperty("--home-light", colour);
  else document.documentElement.style.removeProperty("--home-light");
  if (model.el && model.light !== colour) {
    model.light = colour;
    model.el.look = { light: { color: colour || "accent" } };
  }
}

// On narrow screens the nav is one row that scrolls sideways: keep the current page in view,
// scrolling only the row, never the page. Run again when the width or the fonts change.
function navToCurrent() {
  const nav = $("#nav");
  const li = nav.querySelector('[aria-current="page"]')?.parentElement;
  if (!li || nav.scrollWidth <= nav.clientWidth) return;
  const left = li.getBoundingClientRect().left - nav.getBoundingClientRect().left;
  if (left < 0 || left + li.offsetWidth > nav.clientWidth) nav.scrollLeft += left - (nav.clientWidth - li.offsetWidth) / 2;
}
window.addEventListener("resize", navToCurrent);
document.fonts?.ready.then(navToCurrent);

// A battery drawn to scale: the fill is the charge, inside the outline with an even gap.
// Low (15% and under, when the Home light flashes red) is shown in the warning colour.
function batteryIcon(level) {
  const svg = s("svg", { class: `battery-icon${level <= 15 ? " low" : ""}`, viewBox: "0 0 26 14", "aria-hidden": "true" });
  const inner = 17.5 * Math.max(0, Math.min(100, level)) / 100;
  svg.append(
    s("rect", { class: "case", x: 0.75, y: 0.75, width: 21.5, height: 12.5, rx: 3 }),
    s("rect", { class: "tip", x: 23, y: 4.5, width: 2.25, height: 5, rx: 1 }),
    s("rect", { class: "charge", x: 2.75, y: 2.75, width: inner, height: 8.5, rx: 1.5 }));
  return svg;
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
  return text ? h("small", {}, text) : null;
}

// ---------------------------------------------------------------------------
// the 3D controller (pad-model.js), moved by the live input the server reads

const model = { el: null, light: undefined, held: new Set() };

// live input names (as gcm.Key) to the model's parts
const MODEL_BUTTONS = {
  A: "button_a", B: "button_b", X: "button_x", Y: "button_y", LB: "bumper_left", RB: "bumper_right",
  SELECT: "button_view", START: "button_menu", HOME: "button_home", L3: "stick_left", R3: "stick_right",
};

function moveModel(st) {
  const el = model.el;
  if (!el) return;
  const down = new Set((st.pressed || []).filter((k) => k in MODEL_BUTTONS));
  for (const k of down) if (!model.held.has(k)) el.press(MODEL_BUTTONS[k]);
  for (const k of model.held) if (!down.has(k)) el.release(MODEL_BUTTONS[k]);
  model.held = down;
  // sticks: x right, y towards the player, as the kernel reports them
  el.tilt("stick_left", st.lx || 0, st.ly || 0);
  el.tilt("stick_right", st.rx || 0, st.ry || 0);
  const p = new Set(st.pressed || []);
  el.tilt("dpad", p.has("RIGHT") - p.has("LEFT"), p.has("DOWN") - p.has("UP"));
  el.pull("trigger_left", st.lt || 0);
  el.pull("trigger_right", st.rt || 0);
}

function releaseModel() {
  moveModel({ pressed: [] });
}

async function startModel() {
  await customElements.whenDefined("pad-model");
  const el = await $("#pad").ready;
  model.el = el;
  renderChrome();          // gives it the Home light colour
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
  const findBtn = h("button", { class: `button primary${app.scanning ? " working" : ""}`, onclick: scan, disabled: app.scanning },
    app.scanning ? "Searching…" : "Find controllers");
  wrap.append(h("div", { class: "button-row" }, findBtn,
    h("button", { class: "button", onclick: () => connect({ simulate: true }) }, "Use the simulator")));

  if (app.found) {
    if (app.found.length === 0) {
      wrap.append(h("p", { class: "note" },
        "No Nova 2 Lite found. By cable or receiver, check the light is green. Over Bluetooth, check it is in Android mode and close to this computer. If it still doesn't appear, pair it once in your system's Bluetooth settings."));
    } else {
      wrap.append(h("ul", { class: "found" }, app.found.map((d) =>
        h("li", {}, h("span", {}, d.name, h("span", { class: "addr" }, d.link === "usb" ? `${d.via === "dongle" ? "2.4 GHz receiver" : "USB cable"}, ${d.address}` : d.address)),
          h("button", { class: "button primary small", onclick: (e) => working(e.currentTarget, "Connecting…",
            () => connect({ address: d.address, name: d.name, link: d.link })) }, "Connect")))));
    }
  }
  wrap.append(notice("",
    "The simulator behaves like a controller, so you can try every setting without one."));
  return wrap;
}

function panelController() {
  const d = app.dev;
  const info = val(d.info);
  const bat = val(d.battery);
  const fact = (term, value, small) => h("div", { class: "fact well" }, h("dt", {}, term), h("dd", {}, value ?? "Unknown", small ? h("small", {}, small) : null));
  const dongle = app.status.mode === "usb" && app.status.via === "dongle";
  const where = app.status.mode !== "usb" ? `Connected to ${app.status.address}`
    : (dongle ? "Connected through the 2.4 GHz receiver" : "Connected by cable")
      // the hidraw node on Linux; macOS's own device IDs mean nothing to anyone
      + (app.status.address.startsWith("/dev/") ? ` (${app.status.address})` : "");
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
    h("div", { class: "button-row" },
      h("button", { class: "button", onclick: (e) => working(e.currentTarget, "Reading…", () => reload(true)) }, "Read settings again")),
  );
}

function regionName(fw) {
  if (!fw || !/^\d+$/.test(fw)) return null;
  return { "0": "China build", "1": "international build" }[fw.slice(-1)] || null;
}

// gcm.Key names to the face figure's parts
const KEY_PARTS = {
  A: "a", B: "b", X: "x", Y: "y", LB: "lb", RB: "rb", LT: "lt", RT: "rt", UP: "up", DOWN: "down",
  LEFT: "left", RIGHT: "right", L4: "l4", R4: "r4", L3: "ls", R3: "rs", SELECT: "view", START: "menu",
  CAPTURE: "share", HOME: "home", M: "m",   // M only from the macOS live view (state reports)
};

// The Buttons page: pick a button on the controller, change what it does beside it. The same
// drawing shows what the computer receives while you press buttons (by cable or receiver).
async function panelButtons() {
  const d = app.dev;
  if (!d.remaps) return panelBackButtons();
  const labels = app.meta.keys, max = app.meta.remap_max_targets, sources = app.meta.remap_sources;
  const targetsOf = (k) => val(d.remaps[k]) || [];
  const acts = (k) => targetsOf(k).map((t) => labels[t]).join(" + ");
  const wrap = h("div", {},
    h("h1", {}, "Buttons"),
    h("p", { class: "lede" }, "Choose a button on the controller to change what it does. A remapped button sends the buttons you pick instead of its own; the change is stored on the controller."));

  let figure;
  try { figure = await faceFigure(); } catch (e) { wrap.append(h("p", { class: "note" }, e.message)); return wrap; }
  figure.setAttribute("role", "group");
  figure.setAttribute("aria-label", "The controller's buttons");
  const part = (k) => figure.querySelector(`[data-part="${KEY_PARTS[k]}"]`);
  let selected = sources.find((k) => targetsOf(k).length) || "A";

  const editor = h("div", { class: "remap-editor" });
  const changed = h("div", { class: "remap-changed" });

  const paint = () => {
    for (const k of sources) {
      const p = part(k);
      if (!p) continue;
      const err = errOf(d.remaps[k]);
      p.toggleAttribute("data-remapped", targetsOf(k).length > 0);
      p.toggleAttribute("data-selected", k === selected);
      p.setAttribute("aria-pressed", k === selected ? "true" : "false");
      p.setAttribute("aria-label", `${labels[k]}: ${err ? "not available" : targetsOf(k).length ? `acts as ${acts(k)}` : "its own function"}`);
    }
    const list = sources.filter((k) => targetsOf(k).length);
    changed.replaceChildren(...[
      h("h2", {}, "Changed"),
      list.length
        ? h("ul", { class: "changed-list" }, list.map((k) => h("li", {},
            h("button", { class: "button link", type: "button", onclick: () => select(k) }, labels[k]),
            h("span", {}, `acts as ${acts(k)}`))))
        : h("p", { class: "note" }, "Every button does its own job."),
      list.length ? h("div", { class: "button-row" }, h("button", { class: "button", onclick: resetRemaps }, "Reset all buttons")) : null,
    ].filter(Boolean));
  };

  // One button's remap: up to three targets, pressed together. The first menu also offers the
  // button's own function; each further menu can be removed again.
  const drawEditor = () => {
    const key = selected, entry = d.remaps[key];
    // the button being changed: chosen here or on the drawing (easier with a keyboard, or on a phone)
    const picker = h("select", { class: "input", id: "remap-button", onchange: (e) => select(e.target.value) },
      sources.map((k) => h("option", { value: k, selected: k === key }, targetsOf(k).length ? `${labels[k]} (changed)` : labels[k])));
    const head = h("div", { class: "field" }, h("label", { class: "label", for: "remap-button" }, "Button"), picker);
    if (errOf(entry)) { editor.replaceChildren(head, unavailable(entry)); return; }
    const status = h("p", { class: "hint" });
    const picks = h("div", { class: "remap-pick" });
    const actions = h("div", { class: "button-row" });
    const mark = savedMark();
    let chosen = [...targetsOf(key)];
    const commit = (next) => {
      next = next.filter((t, i) => t && next.indexOf(t) === i);
      chosen = next;
      draw();
      save("/api/remap", { key, targets: next }, (v) => {
        d.remaps[key] = { value: v }; chosen = [...v]; draw(); paint();
        picker.selectedOptions[0].textContent = v.length ? `${labels[key]} (changed)` : labels[key];
      }, mark);
    };
    const menu = (i) => {
      const others = app.meta.remap_targets.filter((t) => t !== key && (t === chosen[i] || !chosen.includes(t)));
      const first = i === 0
        ? h("option", { value: "" }, ["L4", "R4"].includes(key) ? "Nothing (default)" : "Its own function")
        : h("option", { value: "" }, "Remove this button");
      const select = h("select", { class: "input", "aria-label": i === 0 ? `${labels[key]} acts as` : `${labels[key]}, button ${i + 1}` },
        first, h("optgroup", { label: i === 0 ? "Act as" : "Together with" }, others.map((t) => h("option", { value: t }, labels[t]))));
      select.value = chosen[i] || "";
      select.addEventListener("change", () => {
        if (i === 0 && !select.value) return commit([]);       // own function again: drop the rest too
        const next = [...chosen];
        next[i] = select.value;
        commit(next);
      });
      return select;
    };
    const draw = () => {
      status.textContent = chosen.length ? `Acts as ${chosen.map((t) => labels[t]).join(" + ")}` : "Does its own job";
      picks.replaceChildren(...(chosen.length ? chosen.map((_, i) => menu(i)) : [menu(0)]));
      actions.replaceChildren(...[
        chosen.length && chosen.length < max
          ? h("button", { class: "button small", type: "button", onclick: () => { picks.append(menu(chosen.length)); } }, "Add a button pressed together")
          : null,
        chosen.length ? h("button", { class: "button small quiet", type: "button", onclick: () => commit([]) }, "Back to its own function") : null,
      ].filter(Boolean));
    };
    draw();
    editor.replaceChildren(
      head,
      h("div", { class: "remap-head" }, status, mark),
      h("div", { class: "field" }, h("span", { class: "label" }, "Acts as"), picks),
      actions);
  };

  const select = (k) => { selected = k; paint(); drawEditor(); };
  for (const k of sources) {
    const p = part(k);
    if (!p) continue;
    p.setAttribute("tabindex", "0");
    p.setAttribute("role", "button");
    p.classList.add("pickable");
    p.addEventListener("click", () => select(k));
    p.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(k); }
    });
  }
  paint();
  drawEditor();

  const map = h("figure", { class: "button-map well" }, figure);
  if (app.status.mode === "usb") {
    // What the computer receives right now: a remapped button lights up as its new button.
    const signal = h("span", { class: "chip" }, "Connecting…");
    let last = 0;
    map.append(h("figcaption", {}, signal,
      h("span", {}, "Press a button: what the computer receives lights up. L4, R4 and Capture aren't reported in this mode.")));
    liveWatch(() => {
      const ok = Date.now() - last < 2500;
      signal.textContent = ok ? "Receiving" : "No live signal";
      signal.classList.toggle("ok", ok);
      signal.classList.toggle("danger", !ok);
    });
    app.live = (st) => {
      last = Date.now();
      const down = new Set((st.pressed || []).map((k) => KEY_PARTS[k]));
      for (const p of figure.querySelectorAll("[data-part]")) p.toggleAttribute("data-pressed", down.has(p.dataset.part));
    };
  }
  wrap.append(h("div", { class: "remap" }, map, editor), changed);
  wrap.append(h("h2", {}, "Layout"),
    h("div", { class: "rows" },
      featureToggle("DPAD_DIAGONAL_LOCK", "Lock D-pad diagonals", "Only up, down, left and right register"),
      featureToggle("SWAP_LT_LB", "Swap LB and LT"),
      featureToggle("SWAP_RT_RB", "Swap RB and RT")));
  wrap.append(h("p", { class: "note" }, "On this firmware a button can act as another button, but not as a stick direction, and it can't be switched off."),
    restartBox());
  return wrap;
}

// Occasionally, right after a change, the controller stops sending input until it restarts.
function restartBox() {
  const receiver = app.status.mode === "usb" && app.status.via === "dongle";
  const sg = !(app.setup || []).some((c) => c.id === "sg" && !c.ok);
  const box = notice("",
    h("p", {}, h("strong", {}, "If the controller stops responding after a change. "),
      "This happens now and then, mostly through the receiver; the change itself is kept. A restart fixes it."),
    app.status.mode === "usb" && sg
      ? h("div", { class: "button-row" }, h("button", { class: `button${app.restarting ? " working" : ""}`, disabled: !!app.restarting,
          onclick: restartDevice }, app.restarting ? "Restarting…" : receiver ? "Restart the receiver" : "Restart the controller"))
      : app.status.mode === "usb" ? h("p", {}, "The restart button needs a one-time setup: ", h("a", { href: "#setup" }, "see Setup and help"), ".") : null,
    h("p", { class: "note" }, receiver
      ? "If restarting the receiver doesn't help, turn the controller off and on: hold Home for about 5 seconds, turn it on, and hold Menu and View until the light is green."
      : "Or by hand: hold Home for about 5 seconds, turn it on, and hold Menu and View until the light is green."));
  return box;
}

async function restartDevice() {
  if (app.restarting) return;
  const receiver = app.status.via === "dongle";
  const yes = await confirmDialog(receiver ? "Restart the receiver?" : "Restart the controller?",
    (receiver ? "The receiver restarts and the controller links to it again, in a few seconds. Nothing is changed or erased."
              : "The controller restarts in a few seconds. Your settings are kept; nothing is changed or erased.")
    + (onMac() ? ` ${MAC_PASSWORD_SHORT}` : ""), "Restart");
  if (!yes) return;
  try { await api("/api/update/reboot", { confirm: true }); }
  catch (e) { toast(e.message, "error"); return; }
  app.restarting = true;               // its button (rebuilt by render) says so until it's back
  app.status = await api("/api/status"); app.dev = null;
  render();
  try {
    for (let i = 0; i < 80; i++) {
      await new Promise((r) => setTimeout(r, 500));
      const job = ((await api("/api/update").catch(() => null)) || {}).job;
      if (job && job.kind === "reboot" && job.state !== "running") {
        toast(job.state === "done" ? (receiver ? "The receiver restarted" : "The controller restarted") : job.error,
          job.state === "done" ? "info" : "error");
        return;        // watchDevices connects again once it is back
      }
    }
  } finally {
    app.restarting = false;
    render();
  }
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
    const select = h("select", { class: "input", "aria-label": labels[source] },
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
    const reset = h("button", { class: "button small", onclick: () =>
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
  wrap.append(notice("",
    "Every other button can be remapped by cable or through the 2.4 GHz receiver."));
  return wrap;
}

// The dead-zone circle (.stick-view in ds/components.css): the shaded inner disc and outer ring
// are ignored. The dot is what the computer gets now; the ring is where the stick must be for
// it, worked out backwards from that output (the live values come after the dead zones). That
// assumes the usual scaling: output rises evenly from the inner dead zone to full at the outer
// one. `anti` counts only where the controller applies it (not by cable or receiver, firmware 8201).
function stickView(antiApplied) {
  const svg = s("svg", { class: "stick-view", viewBox: "-110 -110 220 220", role: "img" });
  const outer = s("circle", { class: "sv-outer", r: 100 });
  const reach = s("circle", { class: "sv-reach", r: 100 });
  const inner = s("circle", { class: "sv-inner", r: 0 });
  const raw = s("circle", { class: "sv-raw", r: 9 });
  const out = s("circle", { class: "sv-out", r: 6 });
  svg.append(outer, reach, inner, s("circle", { class: "sv-rim", r: 100 }),
    s("line", { class: "sv-axis", x1: -100, x2: 100, y1: 0, y2: 0 }),
    s("line", { class: "sv-axis", x1: 0, x2: 0, y1: -100, y2: 100 }), raw, out);
  let z = { inner: 0, outer: 0, anti: 0 };
  const update = (values) => {
    z = { ...values, anti: antiApplied ? values.anti : 0 };
    reach.setAttribute("r", 100 - z.outer);
    inner.setAttribute("r", z.inner);
    svg.setAttribute("aria-label", `Inner dead zone ${values.inner}%, outer dead zone ${values.outer}%, anti-dead zone ${values.anti}%`);
  };
  // live output, -1..1 on each axis
  const show = (x, y) => {
    const m = Math.min(1, Math.hypot(x, y));
    const o = 100 * m, full = 100 - z.outer;
    const t = o <= 0 ? 0 : o >= 100 ? full : o <= z.anti ? z.inner
      : z.inner + (o - z.anti) / (100 - z.anti) * (full - z.inner);
    const [ux, uy] = m > 0 ? [x / Math.hypot(x, y), y / Math.hypot(x, y)] : [0, 0];
    out.setAttribute("cx", ux * o); out.setAttribute("cy", uy * o);
    raw.setAttribute("cx", ux * t); raw.setAttribute("cy", uy * t);
    return Math.round(o);
  };
  return { svg, update, show };
}

function panelSticks() {
  const d = app.dev;
  const wrap = h("div", {},
    h("h1", {}, "Sticks"),
    h("p", { class: "lede" }, "Dead zones decide how far a stick must move before it counts, and where it reaches full tilt."));
  const pair = h("div", { class: "pair" });
  const live = [];
  // Firmware 8201 stores the anti-dead zone but doesn't apply it by cable or receiver (2026-10-23).
  const antiApplied = app.status.mode !== "usb";
  for (const [side, prefix, axes] of [["Left stick", "LEFT", ["lx", "ly"]], ["Right stick", "RIGHT", ["rx", "ry"]]]) {
    const zones = { inner: `${prefix}_STICK_INNER`, outer: `${prefix}_STICK_OUTER`, anti: `${prefix}_STICK_ANTI` };
    const entries = Object.fromEntries(Object.entries(zones).map(([k, z]) => [k, d.dead_zones[z]]));
    const values = Object.fromEntries(Object.entries(entries).map(([k, e]) => [k, val(e) ?? 0]));
    const pic = stickView(antiApplied);
    pic.update(values);
    const readout = h("p", { class: "live-readout", "aria-live": "off" });
    live.push((st) => { readout.textContent = `Output ${pic.show(st[axes[0]], st[axes[1]])}%`; });
    const col = h("div", {}, h("h2", {}, side), pic.svg, readout,
      h("div", { class: "legend" }, h("span", {}, h("i"), "Ignored"), h("span", {}, h("i", { class: "raw" }), "The stick"),
        h("span", {}, h("i", { class: "out" }), "What the computer gets")));
    const rows = h("div", { class: "rows" });
    const spec = [
      ["inner", "Inner dead zone", "Movement near the centre is ignored"],
      ["outer", "Outer dead zone", "Full tilt is reached this far before the edge"],
      ["anti", "Anti-dead zone", antiApplied ? "Output jumps to this value as soon as the stick moves"
        : "Stored, but the controller ignores it by cable and through the receiver"],
    ];
    for (const [key, label, help] of spec) {
      const zone = zones[key];
      if (errOf(entries[key])) { rows.append(row(label, help, unavailable(entries[key]))); continue; }
      rows.append(slider({
        label, help, value: values[key], unit: "%", max: key === "outer" ? 25 : 50,
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
  wrap.append(h("p", { class: "note" }, liveNote("The shaded parts of each circle are ignored. The dot shows what the computer gets from the stick now, and the ring where the stick is for that. ") +
    "The outer dead zone stops at 25%: above that the controller's firmware gets full tilt wrong, and the stick jumps to the opposite side."));
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
    // "back" is where full press is reached (95 out of the box, confirmed with the live view), so the
    // end dead zone is 100 minus it.
    const back = val(d.dead_zones[zones.end]);
    const values = { start: val(d.dead_zones[zones.start]) ?? 0, end: back == null ? 0 : Math.max(0, 100 - back) };
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
      h("div", { class: "travel well", role: "img", "aria-label": `${side}: first ${values.start}% and last ${values.end}% of travel ignored` }, startEl, endEl, levelEl),
      h("div", { class: "travel-scale" }, h("span", {}, "Released"), h("span", {}, "Fully pressed")), readout);
    const rows = h("div", { class: "rows" });
    for (const [key, label, help] of [["start", "Start dead zone", "Ignored at the beginning of the pull"], ["end", "End dead zone", "Full press is reached this much before the end of the pull"]]) {
      const zone = zones[key];
      if (errOf(d.dead_zones[zone])) { rows.append(row(label, help, unavailable(d.dead_zones[zone]))); continue; }
      rows.append(slider({
        label, help, value: values[key], unit: "%", max: 50,
        onInput: (v) => { values[key] = v; paint(); },
        onCommit: (v, mark) => save("/api/dead-zone", { zone, value: key === "end" ? 100 - v : v },
          (got) => { d.dead_zones[zone] = { value: got }; }, mark),
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

function stopWatch() {
  clearInterval(liveTimer);
  liveTimer = null;
}

// Runs `check` every second while the page is open (stopped by stopWatch on the next render).
function liveWatch(check) {
  clearInterval(liveTimer);
  liveTimer = setInterval(check, 1000);
}

// One stream while connected by cable or receiver, for the model on every page and for the
// page's own readouts (app.live) where it has them. It stops when the connection goes.
function ensureLive() {
  const wanted = app.status.connected && app.status.mode === "usb";
  if (!wanted && liveAbort) { liveAbort.abort(); liveAbort = null; releaseModel(); }
  if (wanted && !liveAbort) startLive();
}

async function startLive() {
  const ctl = new AbortController();
  liveAbort = ctl;
  try {
    // fetch, not EventSource: the server wants the X-PadSquire header on every API call
    const res = await fetch("/api/input", { headers: { "X-PadSquire": "1" }, signal: ctl.signal });
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
        if (!line) continue;
        const st = JSON.parse(line);
        moveModel(st);
        if (app.live) app.live(st);
      }
    }
  } catch {
    // aborted, or the controller went away
  } finally {
    // a stream that ended by itself is started again on the next render
    if (liveAbort === ctl) { liveAbort = null; releaseModel(); }
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
    const test = h("button", { class: "button small", onclick: () => working(test, "Testing…", async () => {
      try { await api("/api/vibration/test", { motor, strength: val(d.vibration)[field] || 75 }); }
      catch (e) { toast(e.message, "error"); }
    }) }, "Test");
    item.querySelector(".setting-control").append(test);
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
  wrap.append(h("div", { class: "colour-now well" },
    h("span", { class: "colour-dot" }),
    h("div", {}, h("div", { class: "label" }, (COLOURS.find(([, rgb]) => hex(rgb) === cur) || ["Custom colour"])[0]),
      h("span", { class: "hint" }, cur.toUpperCase()))));
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
    const select = h("select", { class: "input", "aria-label": "Sleep after" }, opts.map((m) => h("option", { value: m }, `${m} minutes`)));
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
  wrap.append(notice("warn",
    h("h3", {}, "Factory settings"),
    h("p", {}, "Resets every setting on the controller, including button mapping and the Home light."),
    h("div", { class: "button-row" }, h("button", { class: "button", onclick: (e) => restore(e.currentTarget) }, "Restore factory settings"))));
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
    h("div", { class: "fact well" }, h("dt", {}, "On the controller"), h("dd", {},
      device || (receiver ? "Unknown" : "Not connected"),
      device ? h("small", {}, regionName(device)) : null)),
    receiver ? h("div", { class: "fact well" }, h("dt", {}, "On the 2.4 GHz receiver"), h("dd", {}, receiver, receiverNote ? h("small", {}, receiverNote) : null)) : null,
    h("div", { class: "fact well" }, h("dt", {}, "Downloaded release"), h("dd", {}, fw.extracted ? `GameSir Connect ${fw.extracted}` : "None yet"))));

  if (fw.available.length) {
    const rows = h("div", { class: "rows" });
    for (const f of fw.available) {
      let help = f.target === "dongle" ? "For the 2.4 GHz dongle" : `${f.region === "china" ? "China" : "International"} build`;
      if (f.matches_device_region === false) help += ", not for your controller";
      if (f.newer_than_device) help += ", newer than yours";
      if (f.newer_than_device === false) help += ", not newer than yours";
      rows.append(row(`${f.target === "dongle" ? "Dongle" : "Controller"} firmware ${f.version}`, help, h("span", { class: "hint" }, f.file)));
    }
    wrap.append(h("h2", {}, "Firmware in the downloaded release"), rows);
  }

  const status = h("p", { class: "note", "aria-live": "polite" });
  const check = h("button", { class: "button", onclick: () => working(check, "Checking…", async () => {
    status.textContent = "Checking for new official firmware…";
    try {
      const r = await api("/api/firmware/check", {});
      status.textContent = r.update_available
        ? `GameSir Connect ${r.latest} is available.` : `You have the latest release (GameSir Connect ${r.latest}).`;
    } catch (e) { status.textContent = e.message; }
  }) }, "Check for updates");
  const dl = h("button", { class: "button primary", onclick: () => working(dl, "Downloading…", async () => {
    status.textContent = "Downloading GameSir Connect and extracting its firmware. This takes a minute…";
    try {
      const r = await api("/api/firmware/download", {});
      toast(r.updated ? `Downloaded firmware from GameSir Connect ${r.latest}` : "Already up to date");
      render();
    } catch (e) { status.textContent = e.message; }
  }) }, fw.extracted ? "Download latest firmware" : "Download firmware");
  wrap.append(h("div", { class: "button-row" }, check, dl), status);

  upd.fw = fw;
  // Rebuilt (a reconnect re-renders the page): start from what was shown, not a placeholder, so
  // the page doesn't jump while a step runs.
  upd.box = h("div", { class: "update", "aria-live": "polite" },
    ...(upd.data ? updateView() : [h("p", { class: "busy" }, "Looking for the controller…")]));
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
  if (!job) return;
  if (job.state === "running") {                // seen running here: its result is news to this page
    if (upd.seenJob === null) upd.seenJob = `${job.id}running`;
    return;
  }
  if (upd.seenJob === `${job.id}${job.state}`) return;
  const first = upd.seenJob === null;           // finished before this page was opened
  upd.seenJob = `${job.id}${job.state}`;
  if (first) return;
  if (job.state === "failed") { toast(job.error, "error"); return; }
  if (job.result && job.result.restarted) {        // macOS: the whole round in one go, restart included
    if (job.result.backup) upd.backup = job.result.backup;
    toast(job.kind === "backup" ? "Backup saved; the controller is back in normal mode"
      : job.kind === "install" ? `Firmware ${job.result.version} installed; the controller is back in normal mode`
      : "Backup written back; the controller is back in normal mode");
    try {
      const usb = (await api("/api/devices")).usb;      // no Bluetooth scan for this
      if (usb.length === 1) await connect(usb[0]);
    } catch (e) { toast(e.message, "error"); }
    upd.fw = await api("/api/firmware").catch(() => upd.fw);
    return;
  }
  if (job.kind === "backup") { upd.backup = job.result.backup; toast("Backup saved"); }
  if (job.kind === "restore") toast("Backup written back. Restart it to use it.");
  if (job.kind === "install") toast(`Firmware ${job.result.version} installed. Restart it to use it.`);
  if (job.kind === "enter") { app.status = await api("/api/status"); app.dev = null; renderChrome(); }
  if (job.kind === "restart") {
    try {
      const usb = (await api("/api/devices")).usb;      // no Bluetooth scan for this
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

// A backup's time (stored in UTC, as in its file name) in this computer's time zone.
function localStamp(iso) {
  const t = new Date(iso);
  if (isNaN(t)) return iso.replace("T", " ").slice(0, 16);
  const p = (n) => String(n).padStart(2, "0");
  return `${t.getFullYear()}-${p(t.getMonth() + 1)}-${p(t.getDate())} ${p(t.getHours())}:${p(t.getMinutes())}`;
}

// macOS: one button does the whole round (see /api/update/run); elsewhere the steps go one by one.
const oneGo = (d) => onMac() && d.mode === "normal" && d.cable;
const runAll = (action, extra, title, text, okLabel) =>
  runStep("/api/update/run", { action, ...extra }, title, `${text} ${MAC_PASSWORD_SHORT}`, okLabel);

// macOS: update mode needs administrator rights there, so a password dialog comes up. Say why
// before it does, in the page and in the confirm dialog right before it.
const onMac = () => app.meta && app.meta.platform === "macos";
const MAC_PASSWORD_SHORT = "Next, macOS asks for your password: only an administrator may take the controller's update-mode disk away from macOS. It asks once for this session, and PadSquire never sees the password.";
function macPasswordNote() {
  return notice("",
    h("p", {}, h("strong", {}, "Why your Mac asks for your password. "),
      "In update mode the controller shows up as a USB disk, and macOS lets only an administrator take a disk away from its own disk driver. So when you switch to update mode (or restart the controller from this app), macOS asks for your password in its own dialog."),
    h("p", {}, "It asks once per session. With it, a small PadSquire helper runs as administrator and holds the controller's update-mode disk until the controller restarts, then quits. It only passes on PadSquire's update commands, the same ones the Linux version sends; the checks on what may be written stay in the app. PadSquire never sees your password: macOS handles it."),
    h("p", {}, "macOS may then also ask whether \u201cPython\u201d may access a disk. That is PadSquire's helper (PadSquire runs on Python), reaching the controller's update-mode disk; allow it. Each button below does its whole job in one go: it switches the controller to update mode, does the step and restarts it, because on a Mac the controller leaves update mode by itself soon after."));
}

function updateView() {
  const d = upd.data;
  if (d.error) return [h("p", { class: "note" }, d.error)];
  const job = d.job;
  const running = job && job.state === "running";
  const inUpdate = d.mode === "download";
  // A one-go action (macOS) passes through update mode and back: keep the page's shape still
  // meanwhile, so only its progress box changes.
  const shownInUpdate = inUpdate && !(running && job.one_go);
  const out = [];

  const notice = setupNotice(true);
  if (notice) out.push(notice);
  if (onMac()) out.push(macPasswordNote());
  const device = d.device || "controller";
  const thing = device === "receiver" ? "receiver" : "controller";
  const where = inUpdate ? `${thing === "receiver" ? "Receiver" : "Controller"} in update mode`
    : d.mode === "normal" ? "Normal mode" : "Not found";
  out.push(h("dl", { class: "facts" },
    h("div", { class: "fact well" }, h("dt", {}, "Device"), h("dd", {}, where,
      inUpdate && d.identity ? h("small", {}, `${d.identity.chip}, ${d.identity.flash_size / 1024} KB flash`) : null)),
    h("div", { class: "fact well" }, h("dt", {}, "Firmware on it"), h("dd", {}, (inUpdate && d.installed_version) || "Unknown"))));

  // 1. update mode
  const steps = h("ol", { class: "steps" });
  let first;
  if (shownInUpdate) {
    first = h("p", { class: "note" }, thing === "receiver"
      ? "The receiver is in update mode. The controller can't connect through it until you restart it."
      : "The controller is in update mode. Its normal functions are off until you restart it.");
  } else if (d.mode === "normal" && onMac()) {
    first = h("p", { class: "note" }, "On a Mac there is no separate switch: each button below switches the controller to update mode, does its step and restarts it, in one go.");
  } else if (d.mode === "normal") {
    const enter = (target, title, text) => runStep("/api/update/enter", { confirm: true, target }, title, text, "Switch");
    first = h("div", {}, h("p", { class: "note" }, "Nothing is written in this step."),
      h("div", { class: "button-row" },
        d.cable ? jobButton(job, ["enter"], "Switching…", h("button", { class: "button primary", disabled: running, onclick: () => enter("controller", "Switch the controller to update mode?",
          "The controller stops working as a gamepad until you restart it from this page. Nothing is written yet."
          + (onMac() ? ` ${MAC_PASSWORD_SHORT}` : "")) }, "Switch the controller to update mode")) : null,
        d.receiver ? jobButton(job, ["enter"], "Switching…", h("button", { class: `button${d.cable ? "" : " primary"}`, disabled: running, onclick: () => enter("receiver", "Switch the receiver to update mode?",
          "The controller can't connect through the receiver until you restart the receiver from this page. Nothing is written yet."
          + (onMac() ? ` ${MAC_PASSWORD_SHORT}` : "")) }, "Switch the receiver to update mode")) : null),
      h("p", { class: "note" }, "The controller's firmware is installed by cable; the receiver's through the receiver itself."));
  } else first = h("p", { class: "note" }, "Connect the controller by cable, or plug in the 2.4 GHz receiver with the controller on, and switch it to PC/Xbox mode: hold Menu + View until the Home light is green.");
  steps.append(h("li", { class: inUpdate ? "done" : "" }, inUpdate ? stepDone() : null, h("h3", {}, "Switch to update mode"), first));

  // 2. backup
  const mine = d.identity ? d.backups.filter((b) => b.chip_key === d.identity.chip_key && b.jedec_id === d.identity.jedec_id) : d.backups;
  if (upd.backup && !mine.some((b) => b.name === upd.backup)) upd.backup = null;
  if (!upd.backup && mine.length) upd.backup = mine[0].name;
  const backupStep = h("li", { class: mine.length ? "done" : "" }, mine.length ? stepDone() : null, h("h3", {}, `Back up the ${thing}`));
  if (mine.length) {
    const sel = h("select", { class: "input", "aria-label": "Backup", disabled: running, onchange: (e) => { upd.backup = e.target.value; pollUpdate(); } },
      mine.map((b) => h("option", { value: b.name, selected: b.name === upd.backup }, `${localStamp(b.created)}  (${b.name})`)));
    backupStep.append(h("p", { class: "note" }, d.identity ? `Backups of this ${thing}:` : "Backups on this computer:"), sel);
  } else {
    backupStep.append(h("p", { class: "note" }, thing === "receiver"
      ? "There is no backup of this receiver yet. Every write needs one: it holds the firmware and the pairing with your controller."
      : "There is no backup of this controller yet. Every write needs one: it holds the firmware, settings, Bluetooth address and calibration."));
  }
  backupStep.append(h("div", { class: "button-row" }, jobButton(job, ["backup"], "Backing up…", h("button", { class: `button${mine.length ? "" : " primary"}`, disabled: running || !(inUpdate || oneGo(d)),
    onclick: () => inUpdate ? runStep("/api/update/backup")
      : runAll("backup", {}, "Make a backup?", "The controller switches to update mode, its whole flash is read twice and compared, and it restarts. Nothing is written to it.", "Back up") },
    mine.length ? "Make another backup" : "Make a backup"))),
    h("p", { class: "note" }, "Backups are saved in ~/.local/share/padsquire/backups. Copy them somewhere safe."));
  steps.append(backupStep);

  // 3. install or restore
  const files = (upd.fw && upd.fw.available || []).filter((f) => f.target === (thing === "receiver" ? "dongle" : "controller"));
  if (upd.file && !files.some((f) => f.file === upd.file)) upd.file = null;
  if (!upd.file && files.length) upd.file = (files.find((f) => f.matches_device_region !== false && f.newer_than_device !== false) || files[0]).file;
  const install = h("li", {}, h("h3", {}, "Install firmware"));
  if (!files.length) install.append(h("p", { class: "note" }, "Download firmware above first."));
  else {
    install.append(h("select", { class: "input", "aria-label": "Firmware", disabled: running, onchange: (e) => { upd.file = e.target.value; pollUpdate(); } },
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
    install.append(h("div", { class: "button-row" },
      jobButton(job, ["install"], "Installing…", h("button", { class: "button primary", disabled: running || !(inUpdate || oneGo(d)) || !(check && check.allowed), onclick: () => inUpdate
        ? runStep("/api/update/install",
          { file: upd.file, backup: upd.backup, confirm: true }, `Install firmware ${f && f.version}?`,
          `The firmware area of the ${thing} is erased and rewritten. Keep it plugged in until it finishes. If anything fails, it stays in update mode and you can run it again or write the backup back.`,
          "Install")
        : runAll("install", { file: upd.file, backup: upd.backup, confirm: true }, `Install firmware ${f && f.version}?`,
          "The controller switches to update mode, its firmware area is erased and rewritten and checked, and it restarts. Keep it plugged in until it finishes. If the write fails it doesn't restart: run it again straight away, or write the backup back.",
          "Install") }, `Install firmware ${f ? f.version : ""}`))));
  }
  install.append(h("p", { class: "note" }, thing === "receiver"
      ? "To go back to the firmware in a backup, write the backup back. The pairing stays as it is."
      : "To go back to the firmware in a backup, write the backup back. Settings, Bluetooth address and calibration stay as they are."),
    h("div", { class: "button-row" }, jobButton(job, ["restore"], "Writing it back…", h("button", { class: "button", disabled: running || !(inUpdate || oneGo(d)) || !upd.backup, onclick: () => inUpdate
      ? runStep("/api/update/restore",
        { backup: upd.backup, confirm: true }, "Write the backup back?",
        `The firmware area of the ${thing} is erased and rewritten with the firmware in the backup. Keep it plugged in until it finishes.`, "Write it back")
      : runAll("restore", { backup: upd.backup, confirm: true }, "Write the backup back?",
        "The controller switches to update mode, its firmware area is erased and rewritten with the firmware in the backup and checked, and it restarts. Keep it plugged in until it finishes.", "Write it back") },
      "Write the backup back"))));
  steps.append(install);

  // 4. restart (on a Mac only needed when it is stuck in update mode; the buttons above restart it)
  if (!onMac() || shownInUpdate) steps.append(h("li", {}, h("h3", {}, `Restart the ${thing}`),
    d.stay ? notice("danger", "The last write did not verify. Don't restart or unplug the controller: run the same step again.") : null,
    h("div", { class: "button-row" }, jobButton(job, ["restart"], "Restarting…", h("button", { class: "button", disabled: running || !inUpdate || d.stay,
      onclick: () => runStep("/api/update/restart") }, `Restart ${thing}`)))));
  out.push(steps);
  out.push(h("p", { class: "note" }, h("strong", {}, "Not known yet: "),
    "how to get the controller into update mode if its firmware no longer starts. The M + X + B shortcut goes through the firmware, so it wouldn't help then. That's why a write that fails keeps the controller in update mode until it has been done again: don't unplug or restart it in between."));

  // the job in progress, or the last one
  if (job) {
    const names = { enter: "Switching to update mode", identify: "Identifying the chip", backup: "Backing up",
                    restore: "Writing the backup back", install: "Installing firmware", restart: "Restarting" };
    const box = h("div", { class: `job well ${job.state}` }, h("h3", {}, names[job.kind] || job.kind,
      job.state === "done" ? ": done" : job.state === "failed" ? ": failed" : "…"));
    if (running) {
      box.append(h("p", { class: "note" }, job.label || "Starting…"),
        h("progress", { max: job.total || 1, value: job.done || 0 }),
        ["restore", "install"].includes(job.kind) ? h("p", { class: "note" }, h("strong", {}, "Keep it plugged in until this finishes.")) : null);
    }
    if (job.error) box.append(notice("danger", job.error));
    if (job.log.length) box.append(h("ol", { class: "log" }, job.log.map((l) => h("li", {}, l))));
    out.push(box);
  }
  return out;
}

// ---------------------------------------------------------------------------
// button guide: the controller map labelled like the manual, and how-tos in our own words

// ---------------------------------------------------------------------------
// controller figures: the website's outline drawings (img/nova-2-lite-front.svg and -back.svg,
// traced from the 3D model) with the controls laid over them where they are. Positions are the
// website's measurements (tools/nova-2-lite/shape.py), in the drawings' own millimetres: x to
// the right and y down from the middle of the top edge, as you look at the face.

const outlines = {};
async function outline(view) {
  if (!outlines[view]) {
    outlines[view] = (async () => {
      const res = await fetch(`/static/img/nova-2-lite-${view}.svg`);
      if (!res.ok) throw new Error(`Couldn't load the controller drawing (HTTP ${res.status}).`);
      // dropped before parsing: even a parsed-only <style> counts against the page's CSP
      const text = (await res.text()).replace(/<style[\s\S]*?<\/style>/, "");
      return new DOMParser().parseFromString(text, "image/svg+xml").querySelector("g.line");
    })().catch((e) => { delete outlines[view]; throw e; });
  }
  return document.importNode(await outlines[view], true);
}

// The face's controls: a circle [x, y, r] or a rounded rect [x, y, width, height, angle].
// The shoulders are drawn just above the top edge, where the front view shows only a sliver of
// them: the bumpers in a solid line, the triggers behind them dashed. L4 and R4 (on the back)
// are dashed too.
const FACE = {
  lt: { rect: [-42.5, -15.5, 19, 5.6, -7], glyph: "LT", size: 3.4, ghost: true },
  rt: { rect: [42.5, -15.5, 19, 5.6, 7], glyph: "RT", size: 3.4, ghost: true },
  lb: { rect: [-41, -6.8, 21, 5.6, -7], glyph: "LB", size: 3.4, outlined: true },
  rb: { rect: [41, -6.8, 21, 5.6, 7], glyph: "RB", size: 3.4, outlined: true },
  ls: { circle: [-40.3, 21, 8.4], glyph: "L3", size: 4 },
  rs: { circle: [19.6, 39.8, 8.4], glyph: "R3", size: 4 },
  y: { circle: [39.8, 12.9, 4.8], glyph: "Y", size: 4.2 },
  x: { circle: [30, 22.7, 4.8], glyph: "X", size: 4.2 },
  b: { circle: [49.6, 22.7, 4.8], glyph: "B", size: 4.2 },
  a: { circle: [39.8, 32.5, 4.8], glyph: "A", size: 4.2 },
  home: { circle: [0, 7.4, 4.3] },
  view: { rect: [-14.9, 10.7, 3.9, 7.6, -20], name: [-14.9, 18.8, "View"] },
  menu: { rect: [14.9, 10.7, 3.9, 7.6, 20], name: [14.9, 18.8, "Menu"] },
  share: { rect: [0, 17, 8.6, 3.8, 0], name: [0, 23, "Capture"] },
  m: { rect: [0, 49, 8.6, 3.6, 0], glyph: "M", size: 2.8 },
  up: { rect: [-19.8, 33.2, 7.5, 7.1, 0], rx: 1.2, arrow: 0 },
  down: { rect: [-19.8, 47.8, 7.5, 7.1, 0], rx: 1.2, arrow: 180 },
  left: { rect: [-27.1, 40.5, 7.1, 7.5, 0], rx: 1.2, arrow: 270 },
  right: { rect: [-12.5, 40.5, 7.1, 7.5, 0], rx: 1.2, arrow: 90 },
  l4: { rect: [-32, 57.5, 27, 9, -17], glyph: "L4", size: 3.6, ghost: true },
  r4: { rect: [32, 57.5, 27, 9, 17], glyph: "R4", size: 3.6, ghost: true },
};

function svgText(x, y, text, cls, size) {
  const t = s("text", { class: cls, x, y });
  if (size) t.setAttribute("font-size", size);
  t.textContent = text;
  return t;
}

function facePart(id, spec) {
  const g = s("g", { class: `part${spec.ghost ? " ghost" : ""}${spec.outlined ? " outlined" : ""}`, "data-part": id });
  let shape;
  if (spec.circle) {
    const [cx, cy, r] = spec.circle;
    shape = s("circle", { class: "shape", cx, cy, r });
  } else {
    const [cx, cy, w, hgt, angle] = spec.rect;
    shape = s("rect", { class: "shape", x: cx - w / 2, y: cy - hgt / 2, width: w, height: hgt,
      rx: spec.rx ?? Math.min(w, hgt) / 2, transform: `rotate(${angle} ${cx} ${cy})` });
  }
  g.append(shape);
  const [cx, cy] = spec.circle || spec.rect;
  if (spec.glyph) g.append(svgText(cx, cy, spec.glyph, "glyph", spec.size));
  if (spec.arrow !== undefined) {
    g.append(s("path", { class: "arrow", d: "M0 -1.7 L1.6 1.1 L-1.6 1.1 Z", transform: `translate(${cx} ${cy}) rotate(${spec.arrow})` }));
  }
  if (spec.name) g.append(svgText(spec.name[0], spec.name[1], spec.name[2], "part-name"));
  return g;
}

// The face with every control drawn and labelled on it.
async function faceFigure() {
  const svg = s("svg", { class: "figure face", viewBox: "-79 -22 158 126" });
  svg.append(await outline("front"));
  const parts = s("g", { class: "parts" });
  for (const [id, spec] of Object.entries(FACE)) parts.append(facePart(id, spec));
  svg.append(parts);
  return svg;
}

// Labels with leader lines, like the manual's. Each label: its lines, where it sits, which way
// it reads, and the leader's points from the label to the part.
const GUIDE_FRONT = [
  { lines: ["Left bumper", "LB"], at: [-88, -6], anchor: "end", path: [[-86, -6], [-53, -6], [-47, 0]] },
  { lines: ["View button"], at: [-88, 6], anchor: "end", path: [[-86, 6], [-19, 6], [-16, 8.5]] },
  { lines: ["Left stick", "press for L3"], at: [-88, 21], anchor: "end", path: [[-86, 21], [-51.7, 21]] },
  { lines: ["D-pad"], at: [-88, 40.5], anchor: "end", path: [[-86, 40.5], [-32.2, 40.5]] },
  { lines: ["Right bumper", "RB"], at: [88, -6], anchor: "start", path: [[86, -6], [53, -6], [47, 0]] },
  { lines: ["Menu button"], at: [88, 6], anchor: "start", path: [[86, 6], [19, 6], [16, 8.5]] },
  { lines: ["Face buttons", "A, B, X and Y"], at: [88, 22.7], anchor: "start", path: [[86, 22.7], [54.4, 22.7]] },
  { lines: ["Right stick", "press for R3"], at: [88, 45], anchor: "start", path: [[86, 45], [33, 45], [30.6, 43]] },
  { lines: ["Home button", "and its light"], at: [0, -19], anchor: "middle", path: [[0, -10], [0, 3.1]] },
  { lines: ["Screenshot button"], at: [6, 110], anchor: "start", path: [[4.3, 17.5], [7, 20], [7, 106]] },
  { lines: ["M button"], at: [-4, 110], anchor: "end", path: [[-2, 50.8], [-2, 106]] },
];
// Seen from behind, so the controller's right is on the left.
const GUIDE_BACK = [
  { lines: ["Right trigger", "RT"], at: [-88, 4], anchor: "end", path: [[-86, 4], [-51.7, 4]] },
  { lines: ["Trigger stop", "shortens the pull"], at: [-88, 22], anchor: "end", path: [[-86, 22], [-24.5, 22]] },
  { lines: ["Back button R4"], at: [-88, 61], anchor: "end", path: [[-86, 61], [-45.8, 61]] },
  { lines: ["Left trigger", "LT"], at: [88, 4], anchor: "start", path: [[86, 4], [51.7, 4]] },
  { lines: ["Trigger stop", "shortens the pull"], at: [88, 22], anchor: "start", path: [[86, 22], [24.5, 22]] },
  { lines: ["Back button L4"], at: [88, 61], anchor: "start", path: [[86, 61], [45.8, 61]] },
  { lines: ["USB-C port"], at: [0, -16], anchor: "middle", path: [[0, -11], [0, 2.7]] },
  { lines: ["Charging contacts"], at: [0, 110], anchor: "middle", path: [[0, 62.5], [0, 106]] },
];

async function guideFigure(view, labels, description) {
  const svg = s("svg", { class: "figure guide", viewBox: "-150 -24 300 140", role: "img", "aria-label": description });
  svg.append(await outline(view));
  for (const { lines, at: [x, y], anchor, path } of labels) {
    svg.append(s("polyline", { class: "leader", points: path.map((p) => p.join(",")).join(" ") }));
    const [ax, ay] = path[path.length - 1];
    svg.append(s("circle", { class: "anchor", cx: ax, cy: ay, r: 1.1 }));
    // a label above or below its part stacks its lines away from the leader
    const up = anchor === "middle" && y < 0;
    lines.forEach((line, i) => {
      const t = svgText(x, up ? y + i * 6 : y + i * 6, line, i ? "callout-hint" : "callout-label");
      t.setAttribute("text-anchor", anchor);
      svg.append(t);
    });
  }
  return svg;
}

function table(head, rows) {
  return h("div", { class: "table-scroll" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, head.map((c) => h("th", { scope: "col" }, c)))),
    h("tbody", {}, rows.map((r) => h("tr", {}, r.map((c, i) => i === 0 ? h("th", { scope: "row" }, c) : h("td", {}, c)))))));
}

async function panelGuide() {
  let pic;
  try {
    pic = h("div", { class: "guide-figures" },
      h("figure", { class: "well figure-scroll" }, await guideFigure("front", GUIDE_FRONT, "The Nova 2 Lite from the front, with every button labelled"),
        h("figcaption", {}, "From the front")),
      h("figure", { class: "well figure-scroll" }, await guideFigure("back", GUIDE_BACK, "The Nova 2 Lite from the back: triggers, trigger stops, back buttons, the USB-C port and the charging contacts"),
        h("figcaption", {}, "From the back")));
  } catch (e) { pic = h("p", { class: "note" }, e.message); }
  return h("div", {},
    h("h1", {}, "Button guide"),
    h("p", { class: "lede" }, "Every button on the Nova 2 Lite, the modes and what the Home light means, and the shortcuts built into the controller."),
    pic,
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
      ["Xbox", "Green", "PC. This is the mode PadSquire configures, by cable or through the receiver."],
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
  stopWatch();
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
  ensureLive();
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
  const btn = h("button", { class: "button small quiet", onclick: async () => {
    try { await navigator.clipboard.writeText(text); btn.textContent = "Copied"; }
    catch { btn.textContent = "Select and copy it"; }
    setTimeout(() => { btn.textContent = "Copy"; }, 1600);
  } }, "Copy");
  return btn;
}

function command(text) {
  return h("div", { class: "codeblock command" },
    h("div", { class: "codeblock-bar" }, h("span", {}, "Terminal"), copyButton(text)),
    h("pre", {}, h("code", {}, text)));
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
    now.length ? notice("danger",
      `Before the controller can be used, this computer needs a one-time setup: ${names(now)}. It takes one command; `,
      h("a", { href: "#setup" }, "see Setup and help"), " for it and what it does.") : null,
    later.length ? notice("",
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
    list.append(h("li", { class: c.ok ? "done" : "" }, c.ok ? stepDone() : null,
      h("h3", {}, c.title, h("span", { class: "chip" }, NEEDS[c.need])),
      h("p", { class: "note" }, c.detail),
      c.command ? command(c.command) : null,
      c.command && c.explain.length ? h("details", { class: "explain" }, h("summary", {}, "What this does"),
        h("ul", {}, c.explain.map((x) => h("li", {}, x))),
        c.file ? [h("p", {}, `The file, ${c.file.path}:`), h("pre", { class: "file" }, c.file.content)] : null,
        c.docs.length ? h("p", {}, "Documentation: ", c.docs.map((d, i) => [i ? ", " : "",
          h("a", { href: d.url, target: "_blank", rel: "noopener noreferrer" }, d.title)])) : null) : null,
      c.id === "firmware" && !c.ok ? h("div", { class: "button-row" }, h("button", { class: "button", onclick: () => go("firmware") }, "Go to Firmware")) : null));
  }
  wrap.append(list, h("div", { class: "button-row" }, h("button", { class: "button", onclick: render }, "Check again")));

  const qa = (q, ...a) => h("details", { class: "faq" }, h("summary", {}, q), ...a.map((x) => typeof x === "string" ? h("p", {}, x) : x));
  wrap.append(h("h2", {}, "If something doesn't work"), h("div", { class: "faqs" },
    qa("The controller isn't found",
      "Check the Home light is green: hold Menu and View for 2 seconds; each press moves to the next mode (green, red, blue). In blue mode it pretends to be a PlayStation controller and can't be configured.",
      "By cable, try another cable or port: some cables only charge. Through the receiver, turn the controller on and wait for it to link before switching to green.",
      !onMac()
        ? "If it shows up but can't be opened, the device permissions above aren't in place yet, or the controller was plugged in before they were: unplug it and plug it in again."
        : "If it shows up but can't be opened, another program has it: macOS lets only one program use it at a time. Close other PadSquire windows and GameSir software, or unplug it and plug it in again."),
    qa("The live view or the controller stops responding",
      "This has been seen right after a change, mostly through the receiver. Use Restart on the Buttons page: by cable it restarts the controller, through the receiver it restarts the receiver. Nothing is changed or erased, and settings are kept.",
      "If that doesn't help, turn the controller off and on: hold Home for about 5 seconds, turn it on, and switch to green."),
    qa("Bluetooth doesn't find it",
      "Put the controller in Android mode: hold Home and A for 2 seconds until the light flashes yellow. Pair it once in your system's Bluetooth settings, then use Find controllers here."),
    onMac() ? qa("Why does my Mac ask for my password?",
      "In update mode the controller shows up as a USB disk, and macOS lets only an administrator take a disk away from its own disk driver. Firmware backups, updates and the restart button all need update mode, so they ask for your password in macOS's own dialog, once per session; PadSquire never sees it.",
      "With it, a small PadSquire helper runs as administrator and holds the controller's update-mode disk until the controller restarts, then quits. It only passes on PadSquire's update commands, the same ones the Linux version sends, and can't send the one command that would change the chip for good. If you cancel the dialog, nothing happens and the controller stays as it was.") : null,
    qa("Switching to update mode doesn't work",
      onMac()
        ? "Use a USB port on the Mac itself or a powered USB 2 hub; some hubs disturb the controller's update mode. If the password dialog didn't come up, look behind other windows."
        : "The sg module must be loaded (see above). Use a USB port on the computer itself or a powered USB 2 hub; some hubs disturb the controller's update mode.",
      "Nothing has been written at that point. To get the controller back: unplug it, hold Home for about 5 seconds, and turn it on again. The receiver: unplug it and plug it back in."),
    qa("The controller stays on and the buttons do nothing",
      "It's in update mode. That mode is built into the chip and doesn't run the firmware, so Home can't turn it off, and on battery it stays on when unplugged. Your firmware and settings are still there.",
      "Plug it in by cable. " + (onMac()
        ? "On a Mac it restarts by itself within about a minute; or use Restart on the Firmware page."
        : "Then use Restart on the Firmware page, or run: padsquire uboot restart")),
    qa("A firmware write fails",
      "The controller or receiver stays in update mode on purpose. Don't unplug it and don't restart it: run the same step again. If it keeps failing, write your backup back the same way. Every write is checked block by block, and nothing outside the firmware area is ever touched."),
    qa("Where are my backups?",
      "In ~/.local/share/padsquire/backups, one file per backup with a .json next to it. They hold the firmware and your controller's Bluetooth pairing and calibration: copy them somewhere safe, and don't share them."),
    qa("Starting this app",
      command("padsquire gui"),
      "It runs on this computer only (127.0.0.1) and opens your browser with a one-time link. Stop it with Ctrl+C in its terminal.")));
  return wrap;
}

async function restore(button) {
  const yes = await confirmDialog("Restore factory settings?",
    "Every setting on the controller goes back to how it shipped. This can't be undone.", "Restore");
  if (!yes) return;
  await working(button, "Restoring…", () =>
    save("/api/restore", { confirm: true }, (state) => { app.dev = state; toast("Factory settings restored"); render(); }, null));
}

// ---------------------------------------------------------------------------

// Light and dark follow the system; choosing one pins it with data-theme on <html>.
function wireTheme() {
  for (const input of document.querySelectorAll("#theme input")) {
    input.addEventListener("change", () => {
      if (input.value === "auto") delete document.documentElement.dataset.theme;
      else document.documentElement.dataset.theme = input.value;
    });
  }
}

// The logo is padsquire.com's icon, inlined so its colours follow the theme tokens (as an
// <img> it would only follow the system's light or dark).
async function showLogo() {
  try {
    const res = await fetch("/static/img/icon.svg");
    const text = (await res.text()).replace(/<style[\s\S]*?<\/style>/, "");
    $("#logo").append(document.importNode(new DOMParser().parseFromString(text, "image/svg+xml").documentElement, true));
  } catch { /* the name alone is enough */ }
}

async function start() {
  wireTheme();
  showLogo();
  startModel();
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

// Tell the server this page is closing: it stops shortly after the last page is gone (a reload
// comes back in time). A beacon carries the cookie but no custom header; /api/bye needs neither.
window.addEventListener("pagehide", () => navigator.sendBeacon("/api/bye"));

start();

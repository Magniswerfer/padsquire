// SPDX-License-Identifier: AGPL-3.0-or-later
// The Cyclone 2's pages. It keeps its settings in profiles (1-4, and the Shift layer), so every
// page shows one profile, the one it is running unless another is picked. Uses app.js's helpers.
"use strict";

const C2_PROFILES = [["1", "Profile 1"], ["2", "Profile 2"], ["3", "Profile 3"], ["4", "Profile 4"], ["5", "Shift"]];

function c2Shown() {
  const d = app.dev;
  const current = String(val(d.current_profile) || 1);
  const pick = app.c2Profile && d.profiles[app.c2Profile] ? app.c2Profile : current;
  return { number: pick, current, profile: val(d.profiles[pick]), entry: d.profiles[pick] };
}

// The page's heading, a picker for the profile shown, and the note that changes come later.
function c2Head(title, lede) {
  const { number, current } = c2Shown();
  const names = Object.fromEntries(C2_PROFILES.map(([n, label]) => {
    const p = val(app.dev.profiles[n]);
    return [n, p && p.name ? p.name : label];
  }));
  return h("div", {},
    h("h1", {}, title),
    lede ? h("p", { class: "lede" }, lede) : null,
    row("Profile", number === current ? "The one the controller is using now." : "Not the one in use; the controller is on " + names[current] + ".",
      segmented("c2-profile", C2_PROFILES.map(([n]) => [n, names[n]]), number, (n) => { app.c2Profile = n; render(); })),
    c2ReadOnly());
}

function c2ReadOnly() {
  return notice("", h("p", {}, "PadSquire reads the Cyclone 2's settings but can't change them yet. To change them today, use ",
    h("a", { href: "https://github.com/broroeror/gamesir-linux-tools", target: "_blank", rel: "noopener noreferrer" }, "Deadband"), "."));
}

const c2Value = (text) => h("span", { class: "value" }, text);
const pct = (n) => `${n}%`;

function c2DeadZone(dz) {
  const anti = dz.anti_start > 0 || (dz.anti_end > 0 && dz.anti_end < 100);
  return `${pct(dz.start)} to ${pct(dz.end)}` + (anti ? `, anti dead zone ${pct(dz.anti_start)} to ${pct(dz.anti_end)}` : "");
}

function c2Controller() {
  const d = app.dev;
  const fw = val(d.firmware);
  const bat = val(d.battery);
  const fact = (term, value, small) => h("div", { class: "fact well" }, h("dt", {}, term), h("dd", {}, value ?? "Unknown", small ? h("small", {}, small) : null));
  const dongle = app.status.via === "dongle";
  const current = val(d.current_profile);
  const profile = current && val(d.profiles[String(current)]);
  return h("div", {},
    h("h1", {}, "Controller"),
    h("p", { class: "lede" }, app.status.simulated
      ? "Cyclone 2. This is the simulator: it answers the way the Cyclone 2's protocol notes say the real one does."
      : `Cyclone 2, connected ${dongle ? "through its 2.4 GHz receiver" : "by cable"}. Every value shown here was read from the controller.`),
    h("dl", { class: "facts" },
      fact("Battery", bat ? `${bat.level}%` : null, bat && bat.charging ? "on the cable" : null),
      fact("Firmware", fw && fw.controller),
      dongle ? fact("Receiver firmware", (fw && fw.receiver) || null) : null,
      fact("Profile in use", current ? (profile && profile.name) || `Profile ${current}` : null)),
    c2ReadOnly(),
    h("p", { class: "note" }, "PadSquire's support for the Cyclone 2 is new and not yet tested on a real one. ",
      "If something looks wrong, a report helps: see ", h("a", { href: "#setup" }, "Help"), "."),
    h("div", { class: "button-row" },
      h("button", { class: "button", onclick: (e) => working(e.currentTarget, "Reading…", () => reload(true)) }, "Read settings again")));
}

function c2Sticks() {
  const { profile, entry } = c2Shown();
  const wrap = c2Head("Sticks", "The dead zones and range of each stick.");
  if (!profile) { wrap.append(unavailable(entry)); return wrap; }
  const rows = h("div", { class: "rows" });
  for (const [side, label] of [["left", "Left stick"], ["right", "Right stick"]]) {
    const st = profile.sticks[side];
    rows.append(
      row(`${label}: dead zone`, "Where the stick starts to count, and where it reaches full.", c2Value(c2DeadZone(st.dead_zone))),
      row(`${label}: range`, "Round, like most controllers, or the stick's own raw range.", c2Value(st.square ? "Raw" : "Round")));
  }
  wrap.append(rows);
  return wrap;
}

function c2Triggers() {
  const { profile, entry } = c2Shown();
  const wrap = c2Head("Triggers", "How far each trigger travels before it counts, and the quick trigger.");
  if (!profile) { wrap.append(unavailable(entry)); return wrap; }
  const rows = h("div", { class: "rows" });
  for (const key of ["LT", "RT"]) {
    const t = profile.triggers[key];
    const q = t.quick_trigger;
    rows.append(
      row(`${key}: dead zone`, null, c2Value(c2DeadZone(t.dead_zone))),
      row(`${key}: quick trigger`, "Counts as fully pressed from a light pull.",
        c2Value(q.enabled ? `On, ${pct(q.start)} to ${pct(q.end)}` : "Off")));
  }
  wrap.append(rows);
  return wrap;
}

function c2Vibration() {
  const { profile, entry } = c2Shown();
  const wrap = c2Head("Vibration", "Rumble strength for the grips and the triggers.");
  if (!profile) { wrap.append(unavailable(entry)); return wrap; }
  const v = profile.vibration;
  wrap.append(h("div", { class: "rows" },
    row("Left grip", null, c2Value(pct(v.left))), row("Right grip", null, c2Value(pct(v.right))),
    row("Left trigger", null, c2Value(pct(v.LT))), row("Right trigger", null, c2Value(pct(v.RT)))));
  return wrap;
}

function c2Lighting() {
  const light = val(app.dev.light);
  const wrap = h("div", {}, h("h1", {}, "Lighting"),
    h("p", { class: "lede" }, "The Cyclone 2 keeps five lighting effects; one of them is in use."), c2ReadOnly());
  if (!light) { wrap.append(unavailable(app.dev.light)); return wrap; }
  const swatch = (rgb) => h("span", { class: "c2-colour", title: hex(rgb), style: `background: ${hex(rgb)}` });
  const rows = h("div", { class: "rows" });
  light.effects.forEach((e, i) => {
    rows.append(row(`Effect ${i + 1}${i === light.active_effect ? " (in use)" : ""}`,
      `Brightness ${pct(e.brightness)}, speed ${e.speed} of 20.`,
      h("div", { class: "swatches" }, e.frames[0].map(swatch))));
  });
  wrap.append(rows);
  return wrap;
}

window.C2_PANELS = {
  controller: c2Controller, sticks: c2Sticks, triggers: c2Triggers, vibration: c2Vibration, lighting: c2Lighting,
};

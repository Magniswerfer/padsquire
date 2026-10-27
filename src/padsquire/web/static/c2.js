// SPDX-License-Identifier: AGPL-3.0-or-later
// The Cyclone 2's pages. It keeps its settings in four profiles and answers only for the one it
// is using, so every page shows that one, and picking another switches the controller to it.
// Changes are written to the controller and read back (c2usb.py). Uses app.js's helpers.
"use strict";

const C2_PROFILES = [1, 2, 3, 4];
const C2_CURVES = [["linear", "Linear"], ["concave", "Concave"], ["s-curve", "S-curve"]];

function c2Profile() { return val(app.dev.profile); }

function c2Set(name, value, mark) {
  const profile = val(app.dev.current_profile);     // the one shown; refused if the controller moved on
  return save("/api/c2/setting", { name, value, profile }, (p) => { app.dev.profile = { value: p }; }, mark);
}

async function c2Switch(number) {
  try {
    app.dev = await api("/api/c2/profile", { number });
    toast(`Switched to profile ${number}`);
  } catch (e) { toast(e.message, "error"); }
  render();
}

function c2ProfileRow() {
  const current = val(app.dev.current_profile);
  const p = c2Profile();
  return row("Profile", "Picking one switches the controller to it, like its own profile button. Changes below go to this profile.",
    segmented("c2-profile", C2_PROFILES.map((n) => [String(n), n === current && p && p.name ? p.name : `Profile ${n}`]),
      String(current), (n) => c2Switch(+n)));
}

function c2Page(title, lede, build) {
  const wrap = h("div", {}, h("h1", {}, title), lede ? h("p", { class: "lede" }, lede) : null, c2ProfileRow());
  const p = c2Profile();
  if (!p) { wrap.append(unavailable(app.dev.profile)); return wrap; }
  wrap.append(build(p), c2NewNote());
  return wrap;
}

function c2NewNote() {
  return h("p", { class: "note" }, "Every change is written to the controller and read back. ",
    "PadSquire's Cyclone 2 support follows ",
    h("a", { href: "https://github.com/broroeror/gamesir-linux-tools", target: "_blank", rel: "noopener noreferrer" }, "Deadband"),
    ", which works out these settings on real controllers; PadSquire itself hasn't been tested on one yet.");
}

// Dead zone: where the input starts to count and where it reaches full, plus the anti dead zone.
function c2DeadZone(name, label, dz) {
  const send = (key) => (n, mark) => c2Set(name, { ...dz, [key]: n }, mark);
  return [
    slider({ label: "Starts at", help: "Movement before this point is ignored.", value: dz.start, max: 50, unit: "%", onCommit: send("start") }),
    slider({ label: "Full at", help: "From this point on it counts as fully pushed.", value: dz.end, min: 50, unit: "%", onCommit: send("end") }),
    slider({ label: "Anti dead zone", help: "Output begins here, to make up for a dead zone a game adds.", value: dz.anti_start, max: 50, unit: "%", onCommit: send("anti_start") }),
  ];
}

function c2Curve(name, curve) {
  const current = { 0: "linear", 1: "concave", 2: "s-curve" }[curve.status] || "custom";
  const options = current === "custom" ? [...C2_CURVES, ["custom", "Custom"]] : C2_CURVES;
  return row("Response", current === "custom" ? "A curve drawn in another app; picking a preset replaces it." : "How the output follows the movement.",
    segmented(`${name}-curve`, options, current, (v) => { if (v !== "custom") c2Set(name, v); }));
}

function c2Sticks() {
  return c2Page("Sticks", "The dead zones, range and response of each stick.", (p) => {
    const rows = h("div", { class: "rows" });
    for (const [side, label] of [["left", "Left stick"], ["right", "Right stick"]]) {
      const st = p.sticks[side];
      rows.append(h("h2", {}, label),
        ...c2DeadZone(`sticks.${side}.dead_zone`, label, st.dead_zone),
        row("Range", "Round, like most controllers, or the stick's own raw range.",
          segmented(`${side}-range`, [["round", "Round"], ["raw", "Raw"]], st.square ? "raw" : "round",
            (v) => c2Set(`sticks.${side}.range`, v))),
        c2Curve(`sticks.${side}.curve`, st.curve));
    }
    return rows;
  });
}

function c2Triggers() {
  return c2Page("Triggers", "How far each trigger travels before it counts, and the quick trigger.", (p) => {
    const rows = h("div", { class: "rows" });
    for (const [key, label] of [["LT", "Left trigger"], ["RT", "Right trigger"]]) {
      const t = p.triggers[key];
      const q = t.quick_trigger;
      const mode = !q.enabled ? "off" : q.mode === 2 ? "fixed" : "adaptive";
      rows.append(h("h2", {}, label),
        ...c2DeadZone(`triggers.${key}.dead_zone`, label, t.dead_zone),
        row("Quick trigger", "Counts as pressed from a light pull, and as let go as soon as it moves back.",
          segmented(`${key}-quick`, [["off", "Off"], ["adaptive", "Adaptive"], ["fixed", "Fixed"]], mode,
            (v) => c2Set(`triggers.${key}.quick_trigger`, v))),
        c2Curve(`triggers.${key}.curve`, t.curve));
    }
    return rows;
  });
}

function c2Vibration() {
  return c2Page("Vibration", "Rumble strength for the motor in each grip.", (p) => {
    const rows = h("div", { class: "rows" });
    for (const [side, label] of [["left", "Left grip"], ["right", "Right grip"]]) {
      const item = slider({ label, value: p.vibration[side], unit: "%", step: 25,
        onCommit: (n, mark) => c2Set(`vibration.${side}`, n, mark) });
      const test = h("button", { class: "button small", onclick: () => working(test, "Testing…", async () => {
        try { await api("/api/c2/vibration-test", { motor: side, strength: c2Profile().vibration[side] || 75 }); }
        catch (e) { toast(e.message, "error"); }
      }) }, "Test");
      item.querySelector(".setting-control").append(test);
      rows.append(item);
    }
    rows.append(h("p", { class: "note" }, "Test runs that motor for a moment at the strength set above. Out of the box it's 75%."));
    return rows;
  });
}

function c2Controller() {
  const d = app.dev;
  const fw = val(d.firmware);
  const bat = val(d.battery);
  const p = c2Profile();
  const fact = (term, value, small) => h("div", { class: "fact well" }, h("dt", {}, term), h("dd", {}, value ?? "Unknown", small ? h("small", {}, small) : null));
  const dongle = app.status.via === "dongle";
  return h("div", {},
    h("h1", {}, "Controller"),
    h("p", { class: "lede" }, app.status.simulated
      ? "Cyclone 2. This is the simulator: it answers the way the Cyclone 2's protocol notes say the real one does."
      : `Cyclone 2, connected ${dongle ? "through its 2.4 GHz receiver" : "by cable"}. Every value shown here was read from the controller.`),
    h("dl", { class: "facts" },
      fact("Battery", bat ? `${bat.level}%` : null, bat && bat.charging ? "on the cable" : null),
      fact("Firmware", fw && fw.controller),
      dongle ? fact("Receiver firmware", (fw && fw.receiver) || null) : null,
      fact("Profile in use", val(d.current_profile) ? (p && p.name) || `Profile ${val(d.current_profile)}` : null)),
    p ? h("div", { class: "rows" },
      c2ProfileRow(),
      row("Report rate", "How often it tells the computer where everything is. Higher reacts sooner and uses a little more battery.",
        segmented("report-rate", [["250", "250 Hz"], ["500", "500 Hz"], ["1000", "1000 Hz"]], String(p.report_rate),
          (v) => c2Set("report_rate", +v)))) : null,
    window.addedPanel ? addedPanel() : null,
    c2NewNote(),
    h("div", { class: "button-row" },
      h("button", { class: "button", onclick: (e) => working(e.currentTarget, "Reading…", () => reload(true)) }, "Read settings again")));
}

function c2Lighting() {
  const light = val(app.dev.light);
  const wrap = h("div", {}, h("h1", {}, "Lighting"),
    h("p", { class: "lede" }, "The Cyclone 2 keeps five lighting effects; one of them is in use."),
    notice("", h("p", {}, "PadSquire shows the lighting but can't change it yet. To change it today, use ",
      h("a", { href: "https://github.com/broroeror/gamesir-linux-tools", target: "_blank", rel: "noopener noreferrer" }, "Deadband"), ".")));
  if (!light) { wrap.append(unavailable(app.dev.light)); return wrap; }
  // a frame's colours by light (Deadband): left grip, right grip, (none), profile light, Home
  const lights = [[0, "Left grip"], [1, "Right grip"], [3, "Profile light"], [4, "Home"]];
  const swatch = ([i, name], frame) => h("span", { class: "c2-colour", title: `${name}: ${hex(frame[i])}`, style: `background: ${hex(frame[i])}` });
  const rows = h("div", { class: "rows" });
  light.effects.forEach((e, i) => {
    rows.append(row(`Effect ${i + 1}${i === light.active_effect ? " (in use)" : ""}`,
      `Brightness ${e.brightness}%, speed ${e.speed} of 20.`,
      h("div", { class: "swatches" }, lights.map((l) => swatch(l, e.frames[0])))));
  });
  wrap.append(rows);
  return wrap;
}

window.C2_PANELS = {
  controller: c2Controller, sticks: c2Sticks, triggers: c2Triggers, vibration: c2Vibration, lighting: c2Lighting,
};

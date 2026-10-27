// SPDX-License-Identifier: AGPL-3.0-or-later
// Adding a GameSir controller PadSquire doesn't know yet (addcontroller.py): identify it without
// changing anything, keep it under a name, then unlock its settings a group at a time with a
// test that changes one value, reads it back and puts it back. Uses app.js's helpers.
"use strict";

app.adding = {};     // usb_id -> { found, error, busy }

const ISSUE_URL = "https://github.com/Magniswerfer/padsquire/issues/new?template=hardware-test.yml";

function addFlow(d) {
  const st = app.adding[d.usb_id] || {};
  const iface = d.interfaces.find((i) => [0xFF7A, 0xFFF0].includes(i.usage_page));
  if (!iface) return null;
  const set = (patch) => { app.adding[d.usb_id] = { ...st, ...patch }; render(); };
  if (!st.found) {
    return h("div", { class: "add-flow" },
      st.error ? h("p", { class: "note" }, `It didn't answer: ${st.error}.`) : null,
      h("button", { class: "button small", onclick: (e) => working(e.currentTarget, "Asking…", async () => {
        try { set({ found: await api("/api/add/identify", { address: iface.path }), error: null }); }
        catch (err) { set({ error: err.message }); }
      }) }, st.error ? "Try again" : "Add this controller"),
      h("p", { class: "note" }, "First PadSquire asks who it is, in the language of each controller it knows. That only reads; nothing on it is changed."));
  }
  const f = st.found;
  const kind = f.family === "c2" ? "a Cyclone 2" : "the Nova 2 Lite";
  const name = h("input", { class: "input", type: "text", maxlength: 60, value: d.name || "", "aria-label": "Name for this controller", placeholder: "Name, like Nova Vision" });
  const link = h("select", { class: "input", "aria-label": "How it is connected" },
    h("option", { value: "cable" }, "by cable"), h("option", { value: "receiver" }, "through a receiver"));
  return h("div", { class: "add-flow" },
    notice("ok", h("p", {}, `It answered like ${kind}: ${f.protocol}${f.firmware ? `, firmware ${f.firmware}` : ""}.`),
      h("p", {}, "Give it a name and add it. It then connects like ", kind, ", with every setting locked until a test shows it works on this controller.")),
    h("div", { class: "button-row" }, name, link,
      h("button", { class: "button primary small", onclick: (e) => working(e.currentTarget, "Adding…", async () => {
        try {
          await api("/api/add/save", { address: iface.path, name: name.value, link: link.value });
          delete app.adding[d.usb_id];
          toast(`Added ${name.value.trim()}`);
          app.devices = await api("/api/devices");
          render();
        } catch (err) { toast(err.message, "error"); }
      }) }, "Add")));
}

// On the Controller page of an added controller: its tests, a report, and removing it.
function addedPanel() {
  if (!app.status.added || !app.status.tests) return null;
  const tests = Object.entries(app.status.tests);
  const state = (r) => r === "works" ? h("span", { class: "chip ok" }, icon("check"), "Works")
    : r ? h("span", { class: "chip warn" }, "Didn't work") : h("span", { class: "chip" }, "Not tested");
  const rows = h("div", { class: "rows" }, tests.map(([group, t]) => row(t.label,
    t.result && t.result !== "works" ? t.result : null,
    h("div", { class: "button-row" }, state(t.result),
      h("button", { class: "button small", onclick: (e) => working(e.currentTarget, "Testing…", async () => {
        try {
          const r = await api("/api/add/test", { group });
          app.status = r.status;
          toast(r.result === "works" ? `${t.label} works; you can change it now` : `${t.label} didn't work: ${r.result}`, r.result === "works" ? "info" : "error");
        } catch (err) { toast(err.message, "error"); }
        render();
      }) }, t.result ? "Test again" : "Test")))));
  return h("div", {},
    h("h2", {}, "Added by you"),
    h("p", { class: "note" }, "PadSquire doesn't know this controller, so each group of settings stays locked until its test passes. ",
      "A test changes one setting, reads it back, and puts the original back. Settings without a test, and firmware updates, stay off for it."),
    rows,
    h("p", { class: "note" }, "Help the next person with this controller: save a report and attach it to a hardware test report. ",
      "It holds the USB IDs, what answered and the test results, no serial numbers or Bluetooth address."),
    h("div", { class: "button-row" },
      h("button", { class: "button", onclick: async () => {
        try { await writeJson(await api("/api/add/report"), "padsquire-controller-report.json", "The report"); }
        catch (err) { toast(err.message, "error"); }
      } }, "Save a report"),
      h("a", { class: "button", href: ISSUE_URL, target: "_blank", rel: "noopener noreferrer" }, "Open a hardware test report"),
      h("button", { class: "button", onclick: async () => {
        if (!await confirmDialog("Remove this controller?", "PadSquire forgets it and its test results. Nothing on the controller changes.", "Remove")) return;
        try {
          const usbId = (await api("/api/add/report")).controller.usb_id;
          app.status = await api("/api/add/remove", { usb_id: usbId });
          app.dev = null; app.userDisconnected = true;
          toast("Removed");
        } catch (err) { toast(err.message, "error"); }
        render();
      } }, "Remove")));
}

window.addFlow = addFlow;
window.addedPanel = addedPanel;

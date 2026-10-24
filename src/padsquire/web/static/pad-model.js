// <pad-model src="/models/nova-2-lite.glb"> shows a controller you can turn with the
// mouse or a finger, and click to press its buttons. Whatever is inside the element
// stays as the fallback until the model has loaded, and for good without WebGL; the
// model then fades in over it. /img/nova-2-lite-angle.svg is drawn from the camera
// the model starts at, so the one turns into the other in place. three.js (~150 kB gzipped) is only fetched once the
// element is near the screen.
//
// Attributes:
//   look     JSON merged over LOOK below, e.g. look='{"shell":{"grain":0.02}}'
//   gamepad  mirror a connected controller (after the visitor presses a button on it)
//   demo     now and then press a button, flick a stick or pull a trigger
//
// It wears the dark (Midnight Gray) controller's colours in dark mode and the light
// (Luminous White) one's in light mode, following the page's color-scheme. Pressing
// a part turns it into view: the face for the face, the back for the shoulders.
//
// From script, once `el.ready` resolves: el.press(name, amount), el.release(name),
// el.tap(name), el.tilt(stick, x, y), el.pull(trigger, amount), el.look = {...},
// el.parts (the names). Parts: stick_left/right, dpad, button_a/b/x/y,
// button_view/menu/share/mode/home, bumper_left/right, trigger_left/right,
// paddle_left/right, trigger_stop_left/right.

// How the materials look, for each colour scheme. "accent" means the page's --accent
// colour. Heights and sizes of the surface patterns are in millimetres: "grain" is
// the fine texture, "peel" a broader, softer one that still shows from a distance,
// and "mottle" a faint unevenness in the colour.
export const LOOKS = {
  dark: {
    shell: { color: "#36393a", roughness: 0.72, grain: 0.014, grainSize: 0.5, peel: 0.02, peelSize: 1.6, mottle: 0.05 },
    underside: { color: "#19191a", roughness: 0.62, grain: 0.01, grainSize: 0.5, peel: 0.015, peelSize: 1.6, mottle: 0.04, stipple: 0.08, stippleSize: 0.9, gripLift: 0.35, gripRoughness: 0.92 },
    control: { color: "#161719", roughness: 0.42 },
    button: { color: "#161719", roughness: 0.34, letter: "#3f61da" },
    rubber: { color: "#1c1d1f", roughness: 0.88, knurl: 0.11, knurlSize: 0.75 },
    accent: { color: "#3f61da", roughness: 0.5 },
    light: { color: "accent", glow: 1.4 },
    metal: { color: "#d4aa5c", roughness: 0.3 },
    scene: { occlusion: 1, environment: 0.85, key: 1.3, exposure: 1 },
  },
  light: {
    shell: { color: "#dcdee2", roughness: 0.66, grain: 0.012, grainSize: 0.5, peel: 0.024, peelSize: 1.6, mottle: 0.025 },
    underside: { color: "#6f7277", roughness: 0.62, grain: 0.01, grainSize: 0.5, peel: 0.015, peelSize: 1.6, mottle: 0.03, stipple: 0.08, stippleSize: 0.9, gripLift: 0.08, gripRoughness: 0.9 },
    control: { color: "#7f8287", roughness: 0.5 },
    button: { color: "#e6e8eb", roughness: 0.45, letter: "accent" },
    rubber: { color: "#82858a", roughness: 0.85, knurl: 0.11, knurlSize: 0.75 },
    accent: { color: "accent", roughness: 0.5 },
    light: { color: "accent", glow: 1.2 },
    metal: { color: "#d4aa5c", roughness: 0.3 },
    scene: { occlusion: 1, environment: 0.8, key: 1.0, exposure: 0.82 },
  },
};

const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");

// Camera directions for view(); the face is up (+y), the grips towards +z.
const VIEWS = {
  home: [-0.095, 0.215, 0.23], front: [0, 1, 0.12], top: [0, 0.35, -1], back: [0, -1, -0.12],
  under: [0, -0.4, 1], left: [-1, 0.25, 0.05], right: [1, 0.25, 0.05],
};

// Where pressing a part turns the camera: which parts, from which direction, how
// close, and how long after the last press it turns back.
const FOCUS = {
  face: { dir: [-0.05, 1, 0.42], zoom: 1.45 },
  // (from above rather than behind, so left and right stay where the player has them)
  shoulders: { dir: [0, 1, 0.1], zoom: 1.15 },
  back: { dir: [0, -1, 0.3], zoom: 1.15 },
};
const FOCUS_HOLD = 2600; // ms
function region(name) {
  if (name.startsWith("bumper") || name.startsWith("trigger_left") || name.startsWith("trigger_right")) return "shoulders";
  if (name.startsWith("paddle") || name.startsWith("trigger_stop")) return "back";
  return "face";
}

class PadModel extends HTMLElement {
  constructor() {
    super();
    this.ready = new Promise((resolve) => { this.resolveReady = resolve; });
    this.extraLook = {};
  }

  connectedCallback() {
    if (this.started) return;
    this.started = true;
    const seen = new IntersectionObserver((entries) => {
      if (!entries.some((e) => e.isIntersecting)) return;
      seen.disconnect();
      this.start().catch((e) => console.warn("pad-model:", e));
    }, { rootMargin: "200px" });
    seen.observe(this);
  }

  disconnectedCallback() {
    this.stop?.();
  }

  // "light" or "dark", from the page's color-scheme where the element sits
  get scheme() {
    return /dark/.test(getComputedStyle(this).colorScheme) ? "dark" : "light";
  }

  get look() {
    let fromAttribute = {};
    try {
      fromAttribute = JSON.parse(this.getAttribute("look") || "{}");
    } catch {
      console.warn("pad-model: the look attribute isn't valid JSON");
    }
    return merge(merge(LOOKS[this.scheme], fromAttribute), this.extraLook);
  }

  set look(value) {
    this.extraLook = merge(this.extraLook, value);
    this.applyLook?.();
  }

  get parts() { return this.rig ? [...this.rig.parts.keys()] : []; }
  press(name, amount = 1) {
    const was = this.rig?.parts.get(name)?.target.a || 0;
    this.rig?.set(name, { a: amount });
    if (amount >= 0.5 && was < 0.5) this.used(name);
  }
  release(name) { this.rig?.set(name, { a: 0 }); }
  pull(name, amount = 1) { this.press(name, amount); }
  tilt(name, x, y) {
    const was = this.rig?.parts.get(name)?.target;
    this.rig?.set(name, { x, y });
    if ((x || y) && was && !was.x && !was.y) this.used(name);
  }
  tap(name, x = 0, y = 0) { this.rig?.tap(name, x, y); this.used(name); }

  // a part has just started moving: show it, and pop
  used(name) {
    this.follow(name);
    this.pops?.add(name);
  }

  // Turn so the part being used is in view, and back again a little after the last
  // press. A view() chosen from script, or a drag, wins until it is let go.
  follow(name) {
    if (!this.rig?.parts.has(name) || (this.goal && !this.following)) return;
    const where = region(name);
    const { dir, zoom } = FOCUS[where];
    this.goal = dir;
    this.zoom = zoom;
    this.focus = this.centres?.[where] || null;
    this.following = performance.now() + FOCUS_HOLD;
  }

  // Turn to a preset: "home", "front", "back", "left", "right", "top" or "under".
  // It stays there until someone drags it.
  // `name` can also be a direction [x, y, z]; zoom > 1 moves in closer, towards
  // `at` (a point on the model in metres, from its centre) if given.
  view(name, zoom = 1, at = null) {
    this.goal = Array.isArray(name) ? name : VIEWS[name] || VIEWS.home;
    this.zoom = zoom;
    this.focus = at;
    this.following = 0;
  }

  async start() {
    const T = await import("./vendor/three.js");
    const canvas = document.createElement("canvas");
    let renderer;
    try {
      renderer = new T.WebGLRenderer({ canvas, antialias: true, alpha: true });
    } catch {
      return; // no WebGL: keep the fallback
    }
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    renderer.outputColorSpace = T.SRGBColorSpace;
    renderer.toneMapping = T.ACESFilmicToneMapping;

    const scene = new T.Scene();
    const pmrem = new T.PMREMGenerator(renderer);
    scene.environment = pmrem.fromScene(new T.RoomEnvironment(), 0.04).texture;
    pmrem.dispose();
    const key = new T.DirectionalLight(0xffffff, 1);
    key.position.set(-0.25, 0.5, 0.3);
    scene.add(key);

    const loader = new T.GLTFLoader().setMeshoptDecoder(T.MeshoptDecoder);
    const pad = (await loader.loadAsync(this.getAttribute("src"))).scene;
    await document.fonts?.ready;
    const looks = surfaces(T, pad, getComputedStyle(this).fontFamily);
    const floor = new T.Box3().setFromObject(pad).min.y;
    scene.add(pad, shadow(T, floor));
    this.rig = new Rig(T, pad);

    // the middle of each group of parts, for follow() to look at
    pad.updateMatrixWorld(true);
    this.centres = {};
    for (const where of Object.keys(FOCUS)) {
      const box = new T.Box3();
      for (const [name, { node }] of this.rig.parts) if (region(name) === where) box.expandByObject(node);
      this.centres[where] = box.isEmpty() ? null : box.getCenter(new T.Vector3()).toArray();
    }
    // the shoulders sit at the top of the frame, with the face below them
    const { face, shoulders } = this.centres;
    if (face && shoulders) this.centres.shoulders = shoulders.map((v, i) => v + (face[i] - v) * 0.85);

    this.applyLook = () => {
      const look = this.look;
      const accent = getComputedStyle(this).getPropertyValue("--accent").trim() || "#1d5fc4";
      looks(look, accent);
      scene.environmentIntensity = look.scene.environment;
      key.intensity = look.scene.key;
      renderer.toneMappingExposure = look.scene.exposure ?? 1;
    };
    this.applyLook();

    const camera = new T.PerspectiveCamera(28, 1, 0.01, 2);
    camera.position.set(-0.095, 0.215, 0.23);
    const controls = new T.OrbitControls(camera, canvas);
    controls.target.set(0, -0.006, 0.004);
    controls.enableDamping = true;
    controls.enablePan = false;
    controls.enableZoom = false;
    controls.rotateSpeed = 0.7;
    controls.update();
    canvas.style.touchAction = "pan-y"; // a vertical swipe still scrolls the page
    const home = camera.position.clone();
    const centre = controls.target.clone();

    // the little bursts of characters when a part is used, drawn over the model
    const overlay = document.createElement("canvas");
    overlay.setAttribute("aria-hidden", "true");
    overlay.style.pointerEvents = "none";
    this.pops = new Pops(T, overlay, this.rig, camera, this);

    // fade in over the fallback once the first frame is drawn, then drop it
    const fallback = [...this.children];
    canvas.style.opacity = "0";
    this.append(canvas, overlay);
    this.classList.add("ready");
    let shown = false;
    const reveal = () => {
      const ms = reducedMotion.matches ? 0 : 500;
      for (const [el, to] of [[canvas, "1"], ...fallback.map((el) => [el, "0"])]) {
        el.style.transition = `opacity ${ms}ms ease-out`;
        el.style.opacity = to;
      }
      setTimeout(() => fallback.forEach((el) => el.remove()), ms + 50);
    };

    const size = () => {
      const { width, height } = this.getBoundingClientRect();
      if (!width || !height) return;
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      // keep the whole controller in view on narrow screens
      camera.zoom = Math.min(1, camera.aspect / 1.35);
      camera.updateProjectionMatrix();
      this.pops.size(width, height);
    };
    const resized = new ResizeObserver(size);
    resized.observe(this);
    size();

    // A click (not a drag) presses whatever is under the pointer.
    const ray = new T.Raycaster();
    let down = null;
    const onDown = (e) => { down = [e.clientX, e.clientY]; };
    const onUp = (e) => {
      if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 5) return;
      const r = canvas.getBoundingClientRect();
      ray.setFromCamera(new T.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1), camera);
      const hit = ray.intersectObject(pad, true)[0];
      let node = hit?.object;
      while (node && !node.userData.kind) node = node.parent;
      if (!node) return;
      // a stick or the d-pad goes the way you clicked it
      const p = node.worldToLocal(hit.point.clone());
      const len = Math.hypot(p.x, p.z) || 1;
      this.tap(node.name, p.x / len, p.z / len);
    };
    canvas.addEventListener("pointerdown", onDown);
    canvas.addEventListener("pointerup", onUp);

    // A slow sway while nobody is holding it; it eases back after you let go.
    let held = false;
    let idleSince = performance.now();
    controls.addEventListener("start", () => { held = true; this.goal = null; this.following = 0; });
    // (dragging keeps whatever was zoomed into in the middle)
    controls.addEventListener("end", () => { held = false; idleSince = performance.now(); });
    // repaint when the colour scheme changes, from the system or the page's own switch
    const scheme = matchMedia("(prefers-color-scheme: dark)");
    const repaint = () => this.applyLook();
    scheme.addEventListener("change", repaint);
    const themed = new MutationObserver(repaint);
    themed.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "class", "style"] });

    let visible = true;
    const onScreen = new IntersectionObserver(([e]) => { visible = e.isIntersecting; });
    onScreen.observe(this);

    const t0 = performance.now();
    let last = t0;
    let nextDemo = t0 + 2500;
    const mirror = this.hasAttribute("gamepad") ? new Mirror(this.rig) : null;
    renderer.setAnimationLoop((now) => {
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      if (!visible) return;
      const playing = mirror?.update();
      for (const name of mirror?.started || []) this.used(name);
      if (this.following && now > this.following && !playing) {
        // back to where it started, then let it sway again
        this.goal = VIEWS.home;
        this.zoom = 1;
        this.focus = null;
        this.following = -1;
      }
      if (this.goal) {
        // swing round at the same distance rather than cutting through the model
        const k = reducedMotion.matches ? 1 : 1 - Math.exp(-dt * 5);
        const goal = new T.Vector3(...this.goal).normalize();
        controls.target.lerp(this.focus ? new T.Vector3(...this.focus) : centre, k);
        const offset = camera.position.clone().sub(controls.target);
        const dir = offset.clone().normalize();
        const turn = new T.Quaternion().setFromUnitVectors(dir, goal);
        dir.applyQuaternion(new T.Quaternion().slerp(turn, k));
        const distance = offset.length() + (home.distanceTo(centre) / (this.zoom || 1) - offset.length()) * k;
        camera.position.copy(controls.target).addScaledVector(dir, distance);
        if (this.following === -1 && dir.angleTo(goal) < 0.01 && controls.target.distanceTo(centre) < 1e-4) {
          this.goal = null;
          this.following = 0;
          idleSince = now - 2500; // sway straight away; it eases in
        }
      } else if (!held && !reducedMotion.matches && now - idleSince > 2500) {
        const ease = Math.min(1, (now - idleSince - 2500) / 4000);
        const t = (now - t0) / 1000;
        const target = home.clone().applyAxisAngle(new T.Vector3(0, 1, 0), Math.sin(t * 0.35) * 0.32);
        camera.position.lerp(target, 0.02 * ease);
      }
      if (this.hasAttribute("demo") && !playing && !reducedMotion.matches && now > nextDemo) {
        this.rig.demo();
        nextDemo = now + 1800 + Math.random() * 2200;
      }
      this.rig.update(dt);
      controls.update();
      renderer.render(scene, camera);
      if (!shown) {
        shown = true;
        requestAnimationFrame(reveal);
      }
      this.pops.draw(now);
    });

    this.stop = () => {
      renderer.setAnimationLoop(null);
      resized.disconnect();
      onScreen.disconnect();
      scheme.removeEventListener("change", repaint);
      themed.disconnect();
      canvas.removeEventListener("pointerdown", onDown);
      canvas.removeEventListener("pointerup", onUp);
      controls.dispose();
      renderer.dispose();
    };
    this.resolveReady(this);
  }
}

function merge(base, over) {
  const out = { ...base };
  for (const [k, v] of Object.entries(over || {})) {
    out[k] = v && typeof v === "object" && !Array.isArray(v) ? merge(base[k] || {}, v) : v;
  }
  return out;
}

// -- moving parts ----------------------------------------------------------------
// Every part that moves is a node sitting on its pivot, with how it moves in its
// extras (from tools/nova-2-lite/build_model.py): buttons press along their axis,
// sticks and the d-pad tilt, triggers and bumpers swing on a hinge, the trigger
// stops slide.

class Rig {
  constructor(T, pad) {
    this.T = T;
    this.parts = new Map();
    pad.updateMatrixWorld(true);
    pad.traverse((node) => {
      if (!node.userData.kind) return;
      // the middle of the part, in its own space (its pivot can be well inside the body)
      const middle = node.worldToLocal(new T.Box3().setFromObject(node).getCenter(new T.Vector3()));
      this.parts.set(node.name, {
        node, move: node.userData, middle,
        position: node.position.clone(), quaternion: node.quaternion.clone(),
        target: { a: 0, x: 0, y: 0 }, now: { a: 0, x: 0, y: 0 },
      });
    });
    this.timers = new Map();
  }

  set(name, values) {
    const part = this.parts.get(name);
    if (part) Object.assign(part.target, values);
  }

  // press and let go; a stick or the d-pad leans towards (x, y) as it does
  tap(name, x = 0, y = 0) {
    const part = this.parts.get(name);
    if (!part) return;
    const { kind } = part.move;
    if (kind === "slide") return this.set(name, { a: part.target.a > 0.5 ? 0 : 1 }); // a switch stays put
    if (kind === "stick") this.set(name, { x: x * 0.8, y: y * 0.8 });
    else if (kind === "dpad") this.set(name, { x: Math.round(x), y: Math.abs(x) > Math.abs(y) ? 0 : Math.round(y) });
    else this.set(name, { a: 1 });
    clearTimeout(this.timers.get(name));
    this.timers.set(name, setTimeout(() => this.set(name, { a: 0, x: 0, y: 0 }), kind === "hinge" ? 380 : 170));
  }

  demo() {
    const names = [...this.parts.keys()].filter((n) => !n.startsWith("trigger_stop"));
    const name = names[Math.floor(Math.random() * names.length)];
    const angle = Math.random() * Math.PI * 2;
    this.tap(name, Math.cos(angle), Math.sin(angle));
  }

  update(dt) {
    const { T } = this;
    const k = 1 - Math.exp(-dt * 28);
    const rad = Math.PI / 180;
    for (const part of this.parts.values()) {
      const { now, target, node, move } = part;
      for (const key of ["a", "x", "y"]) now[key] += (target[key] - now[key]) * k;
      node.position.copy(part.position);
      node.quaternion.copy(part.quaternion);
      if (move.kind === "button") {
        node.position.y -= move.travel * now.a;
      } else if (move.kind === "stick") {
        // x right, y towards the player (as on a gamepad); stays inside the circle
        const amount = Math.min(1, Math.hypot(now.x, now.y));
        if (amount > 1e-4) {
          const axis = new T.Vector3(now.y, 0, -now.x).normalize();
          node.quaternion.multiply(new T.Quaternion().setFromAxisAngle(axis, amount * move.tilt * rad));
        }
        node.position.y -= move.travel * now.a;
      } else if (move.kind === "dpad") {
        const amount = Math.min(1, Math.hypot(now.x, now.y));
        if (amount > 1e-4) {
          const axis = new T.Vector3(now.y, 0, -now.x).normalize();
          node.quaternion.multiply(new T.Quaternion().setFromAxisAngle(axis, amount * move.tilt * rad));
        }
      } else if (move.kind === "hinge") {
        node.quaternion.multiply(new T.Quaternion().setFromAxisAngle(new T.Vector3(...move.axis), move.swing * now.a * rad));
      } else if (move.kind === "slide") {
        node.position.addScaledVector(new T.Vector3(...move.axis), move.travel * now.a);
      }
    }
  }
}

// Mirrors a controller in the standard gamepad layout. Browsers only reveal one
// after a button on it has been pressed while the page is open.
// `started` lists the parts that began moving this frame.
class Mirror {
  constructor(rig) {
    this.rig = rig;
    this.active = new Set();
    this.started = [];
  }

  update() {
    this.started = [];
    const pad = [...(navigator.getGamepads?.() || [])].find((p) => p && p.mapping === "standard");
    if (!pad) return false;
    const b = (i) => pad.buttons[i]?.value || 0;
    const dead = (v) => (Math.abs(v) < 0.08 ? 0 : v);
    const now = {};
    [["button_a", 0], ["button_b", 1], ["button_x", 2], ["button_y", 3], ["bumper_left", 4], ["bumper_right", 5],
      ["trigger_left", 6], ["trigger_right", 7], ["button_view", 8], ["button_menu", 9], ["button_home", 16]]
      .forEach(([name, i]) => { now[name] = { a: b(i) }; });
    now.stick_left = { x: dead(pad.axes[0]), y: dead(pad.axes[1]), a: b(10) };
    now.stick_right = { x: dead(pad.axes[2]), y: dead(pad.axes[3]), a: b(11) };
    now.dpad = { x: b(15) - b(14), y: b(13) - b(12) };
    const active = new Set();
    for (const [name, values] of Object.entries(now)) {
      this.rig.set(name, values);
      if (Object.values(values).some((v) => Math.abs(v) > 0.3)) active.add(name);
    }
    this.started = [...active].filter((name) => !this.active.has(name));
    this.active = active;
    return active.size > 0;
  }
}

// -- pops ------------------------------------------------------------------------
// When a part is used, a ring of characters bursts out from it, in the same type,
// ramp and ink as the swirling field behind the hero. Each pop stays on its part
// while the camera moves, and fades in under half a second.

const RAMP = " .·:-=+*#%";
const POP_TIME = 460;   // ms
const POP_REACH = 44;   // px

class Pops {
  constructor(T, canvas, rig, camera, host) {
    Object.assign(this, { T, canvas, rig, camera, host });
    this.ctx = canvas.getContext("2d");
    this.live = [];
    this.last = new Map();
    this.width = this.height = 0;
    this.dirty = false;
  }

  size(width, height) {
    const dpr = Math.min(devicePixelRatio || 1, 2);
    this.width = width;
    this.height = height;
    this.canvas.width = Math.round(width * dpr);
    this.canvas.height = Math.round(height * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const css = getComputedStyle(this.host);
    const mono = css.getPropertyValue("--font-mono").trim() || "ui-monospace, monospace";
    const px = width < 420 ? 11 : 13;
    this.font = `600 ${px}px ${mono}`;
    this.ctx.font = this.font;
    this.cellW = this.ctx.measureText("M").width;
    this.cellH = px * 1.25;
  }

  add(name) {
    const part = this.rig.parts.get(name);
    const now = performance.now();
    if (!part || reducedMotion.matches || now - (this.last.get(name) || 0) < 150) return;
    this.last.set(name, now);
    this.live.push({ part, start: now, seed: Math.random() * 1000 });
  }

  draw(now) {
    const { ctx, cellW, cellH } = this;
    if (!this.live.length) {
      if (this.dirty) ctx.clearRect(0, 0, this.width, this.height);
      this.dirty = false;
      return;
    }
    this.live = this.live.filter((p) => now - p.start < POP_TIME);
    ctx.clearRect(0, 0, this.width, this.height);
    ctx.font = this.font;
    ctx.textBaseline = "middle";
    ctx.textAlign = "center";
    ctx.fillStyle = getComputedStyle(this.host).getPropertyValue("--accent").trim() || "#2c48a8";
    for (const pop of this.live) {
      const t = (now - pop.start) / POP_TIME;
      const at = pop.part.middle.clone().applyMatrix4(pop.part.node.matrixWorld).project(this.camera);
      const cx = (at.x * 0.5 + 0.5) * this.width;
      const cy = (-at.y * 0.5 + 0.5) * this.height;
      const ease = 1 - Math.pow(1 - t, 3);
      const radius = 8 + ease * POP_REACH;
      const band = 7 + 7 * t;
      const fade = 1 - t * t;
      const reach = radius + band * 2;
      // on the same kind of grid as the field, centred on the part
      for (let r = -Math.ceil(reach / cellH); r <= Math.ceil(reach / cellH); r++) {
        for (let c = -Math.ceil(reach / cellW); c <= Math.ceil(reach / cellW); c++) {
          const x = c * cellW, y = r * cellH;
          const d = Math.hypot(x, y);
          const ring = Math.exp(-(((d - radius) / band) ** 2));
          // a little flicker, so it reads as type rather than a smooth ring
          const flicker = 0.75 + 0.25 * Math.sin(pop.seed + c * 12.9898 + r * 78.233 + t * 40);
          const v = 0.8 * ring * fade * flicker;
          const i = Math.floor(v * RAMP.length);
          if (i <= 0) continue;
          ctx.globalAlpha = 0.15 + v * 0.45;
          ctx.fillText(RAMP[Math.min(i, RAMP.length - 1)], cx + x, cy + y);
        }
      }
    }
    ctx.globalAlpha = 1;
    this.dirty = true;
  }
}

// -- surfaces --------------------------------------------------------------------
// The model carries baked ambient occlusion (vertex colours); the shader uses it to
// darken indirect light in creases, gaps and wells. It also adds the surface: a fine
// grain and a broader orange-peel texture on the shell, stippled grips underneath and
// knurled stick caps. Each is a height pattern in millimetres that tilts the normal
// (bump mapping) and fades out before it gets finer than a pixel. The face buttons
// get their letters from a small canvas texture.

const PATTERNS = /* glsl */ `
  varying vec3 vPadP;
  uniform int padKind;
  uniform float padOcclusion, padGrain, padGrainSize, padStipple, padStippleSize, padKnurl, padKnurlSize;
  uniform float padGripLift, padGripRoughness, padPeel, padPeelSize, padMottle;
  uniform vec3 padKnurlBand;

  float padHash(vec3 p) {
    p = fract(p * 0.3183099 + 0.1);
    p *= 17.0;
    return fract(p.x * p.y * p.z * (p.x + p.y + p.z));
  }
  float padNoise(vec3 p) {
    vec3 i = floor(p), f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    return mix(mix(mix(padHash(i), padHash(i + vec3(1, 0, 0)), f.x),
                   mix(padHash(i + vec3(0, 1, 0)), padHash(i + vec3(1, 1, 0)), f.x), f.y),
               mix(mix(padHash(i + vec3(0, 0, 1)), padHash(i + vec3(1, 0, 1)), f.x),
                   mix(padHash(i + vec3(0, 1, 1)), padHash(i + vec3(1, 1, 1)), f.x), f.y), f.z);
  }
  // 1 while a pattern of this period is comfortably larger than a pixel, then 0
  float padDetail(float period) {
    float px = length(fwidth(vPadP));
    return 1.0 - smoothstep(0.25 * period, 0.6 * period, px);
  }
  float padGrainAt(vec3 p) {
    float s = padGrainSize, q = padPeelSize;
    return padGrain * (padNoise(p / s) * padDetail(s) + 0.6 * padNoise(p / (0.45 * s)) * padDetail(0.45 * s))
         + padPeel * (padNoise(p / q + 7.0) * padDetail(q) + 0.5 * padNoise(p / (0.5 * q) + 3.0) * padDetail(0.5 * q));
  }
  // a faint, soft unevenness in the colour, about centimetre-sized
  float padMottleAt(vec3 p) {
    return padMottle * (padNoise(p / 6.0) + 0.5 * padNoise(p / 2.5 + 11.0) - 0.75);
  }
  // where the grips are, in model space (x across, z towards the player)
  float padGrips(vec3 p) {
    return smoothstep(24.0, 34.0, abs(p.x)) * smoothstep(24.0, 44.0, p.z);
  }
  float padHeight(vec3 p) {
    if (padKind == 1) return padGrainAt(p);
    if (padKind == 2) {
      // dots on a turned 3D lattice: wherever the surface cuts through, a pebble
      vec3 c = fract(mat3(0.80, 0.36, -0.48, -0.48, 0.86, -0.16, 0.36, 0.36, 0.86) * p / padStippleSize) - 0.5;
      float dots = padStipple * smoothstep(0.36, 0.2, length(c)) * padDetail(padStippleSize);
      return padGrips(p) * dots + padGrainAt(p);
    }
    if (padKind == 3) {
      // diamond knurling round the rim of a stick cap (p in the stick's own space)
      float r = length(p.xz);
      float u = atan(p.z, p.x) * r / padKnurlSize, v = p.y / padKnurlSize;
      float a = 1.0 - abs(fract(u + v) - 0.5) * 2.0, b = 1.0 - abs(fract(u - v) - 0.5) * 2.0;
      float rim = smoothstep(padKnurlBand.z, padKnurlBand.z + 0.3, r)
                * smoothstep(padKnurlBand.x, padKnurlBand.x + 0.3, p.y)
                * smoothstep(padKnurlBand.y, padKnurlBand.y - 0.3, p.y);
      return padKnurl * rim * a * b * padDetail(padKnurlSize);
    }
    return 0.0;
  }
  vec3 padBump(vec3 pos, vec3 n, float h, float face) {
    vec3 sx = dFdx(pos), sy = dFdy(pos);
    vec3 r1 = cross(sy, n), r2 = cross(n, sx);
    float det = dot(sx, r1) * face;
    vec3 grad = sign(det) * (dFdx(h) * r1 + dFdy(h) * r2);
    return normalize(abs(det) * n - grad);
  }
`;

const KINDS = { shell: 1, underside: 2, rubber: 3 };

// A row of letters, white on clear, for the face buttons to show in their own colour.
const LETTERS = ["A", "B", "X", "Y"];
function letterTexture(T, font) {
  const cell = 128;
  const c = document.createElement("canvas");
  c.width = cell * LETTERS.length;
  c.height = cell;
  const g = c.getContext("2d");
  g.fillStyle = "#fff";
  g.textAlign = "center";
  g.textBaseline = "middle";
  g.font = `700 ${cell * 0.8}px ${font || "sans-serif"}`;
  LETTERS.forEach((l, i) => g.fillText(l, (i + 0.5) * cell, cell * 0.54));
  const texture = new T.CanvasTexture(c);
  texture.anisotropy = 4;
  return texture;
}

// Gives every material its uniforms and returns a function that applies a look.
function surfaces(T, pad, font) {
  const letters = letterTexture(T, font);
  const byName = new Map();
  const meshes = [];
  pad.traverse((o) => o.isMesh && meshes.push(o));
  pad.updateMatrixWorld(true);
  for (const mesh of meshes) {
    const name = mesh.material.name;
    const kind = KINDS[name] || 0;
    // the pattern lives in model space, or for a stick in the stick's own space
    let ref = mesh;
    while (ref.parent && !ref.userData.kind && ref.name !== "nova-2-lite") ref = ref.parent;
    const local = new T.Matrix4().copy(ref.matrixWorld).invert().multiply(mesh.matrixWorld);
    const knurl = ref.userData.knurl || [0, 0, 0];
    const letter = LETTERS.indexOf(ref.userData.letter);
    const material = mesh.material.clone();
    mesh.material = material;
    const uniforms = {
      padKind: { value: kind }, padLocal: { value: local }, padKnurlBand: { value: new T.Vector3(...knurl) },
      padOcclusion: { value: 1 }, padGrain: { value: 0 }, padGrainSize: { value: 1 },
      padStipple: { value: 0 }, padStippleSize: { value: 1 }, padKnurl: { value: 0 }, padKnurlSize: { value: 1 },
      padGripLift: { value: 0 }, padGripRoughness: { value: 0.9 },
      padPeel: { value: 0 }, padPeelSize: { value: 1 }, padMottle: { value: 0 },
      padLetters: { value: letters }, padLetter: { value: letter }, padLetterColor: { value: new T.Color() },
    };
    material.onBeforeCompile = (shader) => {
      Object.assign(shader.uniforms, uniforms);
      shader.vertexShader = shader.vertexShader
        .replace("#include <common>", "#include <common>\nvarying vec3 vPadP;\nuniform mat4 padLocal;")
        .replace("#include <project_vertex>", "#include <project_vertex>\nvPadP = (padLocal * vec4(transformed, 1.0)).xyz;");
      shader.fragmentShader = shader.fragmentShader
        .replace("#include <common>", "#include <common>\n" + PATTERNS +
          "uniform sampler2D padLetters;\nuniform float padLetter;\nuniform vec3 padLetterColor;\n")
        // the occlusion goes into the lighting below, not the colour
        .replace("#include <color_fragment>", "")
        .replace("#include <roughnessmap_fragment>", `#include <roughnessmap_fragment>
          float padH = padHeight(vPadP);
          diffuseColor.rgb *= 1.0 + padMottleAt(vPadP);
          // a face button's letter, printed on its top (p in the button's own space;
          // the letter texture spans 9 mm, so the letters stand about 5 mm tall)
          if (padLetter >= 0.0 && vPadP.y > 3.0) {
            vec2 uv = vec2(0.5 + vPadP.x / 9.0, 0.5 - vPadP.z / 9.0);
            float ink = all(greaterThan(uv, vec2(0.0))) && all(lessThan(uv, vec2(1.0)))
              ? texture2D(padLetters, vec2((uv.x + padLetter) / ${LETTERS.length}.0, uv.y)).a : 0.0;
            diffuseColor.rgb = mix(diffuseColor.rgb, padLetterColor, ink);
          }
          // the textured grips scatter more light: matter and a touch lighter
          if (padKind == 2) {
            float grips = padGrips(vPadP);
            roughnessFactor = mix(roughnessFactor, padGripRoughness, grips);
            diffuseColor.rgb *= 1.0 + padGripLift * grips;
          }`)
        .replace("#include <normal_fragment_maps>", `#include <normal_fragment_maps>
          normal = padBump(-vViewPosition, normal, padH * 0.001, faceDirection);`)
        .replace("#include <aomap_fragment>", `#include <aomap_fragment>
          #ifdef USE_COLOR
            float padOcc = mix(1.0, vColor.r, padOcclusion);
            reflectedLight.indirectDiffuse *= padOcc;
            reflectedLight.indirectSpecular *= padOcc;
            reflectedLight.directDiffuse *= mix(1.0, padOcc, 0.6);
            reflectedLight.directSpecular *= mix(1.0, padOcc, 0.6);
          #endif`);
    };
    material.customProgramCacheKey = () => "pad";
    if (!byName.has(name)) byName.set(name, []);
    byName.get(name).push({ material, uniforms });
  }

  return (look, accent) => {
    for (const [name, list] of byName) {
      const l = look[name] || {};
      for (const { material, uniforms } of list) {
        if (l.color) material.color.setStyle(l.color === "accent" ? accent : l.color);
        if (l.roughness !== undefined) material.roughness = l.roughness;
        if (name === "light") {
          material.emissive.setStyle(l.color === "accent" ? accent : l.color || "#78aaff");
          material.emissiveIntensity = l.glow ?? 1;
        }
        uniforms.padOcclusion.value = look.scene.occlusion;
        uniforms.padGrain.value = l.grain || 0;
        uniforms.padGrainSize.value = l.grainSize || 1;
        uniforms.padStipple.value = l.stipple || 0;
        uniforms.padStippleSize.value = l.stippleSize || 1;
        uniforms.padKnurl.value = l.knurl || 0;
        uniforms.padKnurlSize.value = l.knurlSize || 1;
        uniforms.padGripLift.value = l.gripLift || 0;
        uniforms.padGripRoughness.value = l.gripRoughness ?? 0.9;
        uniforms.padPeel.value = l.peel || 0;
        uniforms.padPeelSize.value = l.peelSize || 1;
        uniforms.padMottle.value = l.mottle || 0;
        if (l.letter) uniforms.padLetterColor.value.setStyle(l.letter === "accent" ? accent : l.letter);
      }
    }
  };
}

// A soft contact shadow under the controller.
function shadow(T, floor) {
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const g = c.getContext("2d");
  const grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  grad.addColorStop(0, "rgba(0,0,0,.38)");
  grad.addColorStop(0.55, "rgba(0,0,0,.14)");
  grad.addColorStop(1, "rgba(0,0,0,0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, 128, 128);
  const plane = new T.Mesh(
    new T.PlaneGeometry(0.2, 0.13),
    new T.MeshBasicMaterial({ map: new T.CanvasTexture(c), transparent: true, depthWrite: false }),
  );
  plane.rotation.x = -Math.PI / 2;
  plane.position.y = floor + 0.0003;
  return plane;
}

customElements.define("pad-model", PadModel);

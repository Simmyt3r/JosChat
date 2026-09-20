/**
 * static/js/background.js
 * -------------------------
 * Ambient background for the auth screen: a handful of short chains of
 * hexagonal "blocks" drifting slowly, linked node-to-node like the
 * append-only hash chain in supabase/schema.sql (add_block/validate_chain).
 * Every few seconds a bright pulse travels along one link, echoing a new
 * block being hashed and chained on send. Deliberately not a generic
 * proximity particle network — links only ever follow chain order, never
 * nearest-neighbour, so the shape stays legible as "a chain".
 *
 * No dependencies, ~2D canvas only. Pauses when the tab is hidden and
 * renders a single static frame under prefers-reduced-motion.
 */

(function () {
  const canvas = document.getElementById("bg-canvas");
  if (!canvas || !canvas.getContext) return;
  const ctx = canvas.getContext("2d");

  // Read from CSS (--bg-chain / --bg-glow) so both themes look right.
  let ACCENT = "47, 98, 232";
  let ACCENT_LIGHT = "130, 168, 255";
  function readColors() {
    const cs = getComputedStyle(document.documentElement);
    ACCENT = cs.getPropertyValue("--bg-chain").trim() || ACCENT;
    ACCENT_LIGHT = cs.getPropertyValue("--bg-glow").trim() || ACCENT_LIGHT;
  }
  readColors();

  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  let dpr = Math.min(window.devicePixelRatio || 1, 2);
  let width = 0, height = 0;
  let chains = [];
  let pulses = [];
  let running = true;
  let paused = false;      // set by JoschatBackground.setActive(false) while chatting
  let looping = false;
  let lastTime = performance.now();
  let pulseTimer = 0;

  function resize() {
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function rand(min, max) {
    return min + Math.random() * (max - min);
  }

  // Coordinates are stored as fractions of the viewport (0..1) so resizing
  // never needs to rescale anything.
  function buildChains() {
    chains = [];
    const chainCount = width < 640 ? 3 : width < 1100 ? 5 : 7;
    for (let c = 0; c < chainCount; c++) {
      const nodeCount = Math.round(rand(4, 8));
      const nodes = [];
      let x = rand(0.05, 0.95);
      let y = rand(0.05, 0.95);
      for (let i = 0; i < nodeCount; i++) {
        nodes.push({
          x, y,
          vx: rand(-0.0035, 0.0035),
          vy: rand(-0.0035, 0.0035),
          r: rand(3.5, 7),
          flash: 0,
        });
        // next node seeded near the previous one, chain-style
        x = Math.min(0.98, Math.max(0.02, x + rand(-0.16, 0.16)));
        y = Math.min(0.98, Math.max(0.02, y + rand(-0.16, 0.16)));
      }
      chains.push({ nodes, depth: rand(0.45, 1) });
    }
  }

  function hexPath(cx, cy, r) {
    ctx.beginPath();
    for (let i = 0; i < 6; i++) {
      const angle = (Math.PI / 3) * i - Math.PI / 2;
      const px = cx + r * Math.cos(angle);
      const py = cy + r * Math.sin(angle);
      if (i === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    }
    ctx.closePath();
  }

  function spawnPulse() {
    const eligible = chains.filter((c) => c.nodes.length > 1);
    if (eligible.length === 0) return;
    const chain = eligible[Math.floor(Math.random() * eligible.length)];
    const linkIndex = Math.floor(Math.random() * (chain.nodes.length - 1));
    pulses.push({ chain, linkIndex, t: 0, speed: rand(0.5, 0.8) });
  }

  function step(dt) {
    for (const chain of chains) {
      for (const node of chain.nodes) {
        node.x += node.vx * dt * 0.06;
        node.y += node.vy * dt * 0.06;
        if (node.x < 0.02 || node.x > 0.98) node.vx *= -1;
        if (node.y < 0.02 || node.y > 0.98) node.vy *= -1;
        node.x = Math.min(0.98, Math.max(0.02, node.x));
        node.y = Math.min(0.98, Math.max(0.02, node.y));
        if (node.flash > 0) node.flash = Math.max(0, node.flash - dt * 0.0022);
      }
    }

    for (let i = pulses.length - 1; i >= 0; i--) {
      const p = pulses[i];
      p.t += p.speed * (dt / 1000);
      if (p.t >= 1) {
        const target = p.chain.nodes[p.linkIndex + 1];
        if (target) target.flash = 1;
        pulses.splice(i, 1);
      }
    }

    pulseTimer -= dt;
    if (pulseTimer <= 0) {
      spawnPulse();
      pulseTimer = rand(1800, 3400);
    }
  }

  function draw() {
    ctx.clearRect(0, 0, width, height);

    for (const chain of chains) {
      const depth = chain.depth;
      // links
      ctx.lineWidth = 1;
      ctx.strokeStyle = `rgba(${ACCENT}, ${0.16 * depth})`;
      for (let i = 0; i < chain.nodes.length - 1; i++) {
        const a = chain.nodes[i], b = chain.nodes[i + 1];
        ctx.beginPath();
        ctx.moveTo(a.x * width, a.y * height);
        ctx.lineTo(b.x * width, b.y * height);
        ctx.stroke();
      }
      // nodes
      for (const node of chain.nodes) {
        const alpha = (0.3 + node.flash * 0.6) * depth;
        ctx.fillStyle = `rgba(${ACCENT}, ${alpha})`;
        hexPath(node.x * width, node.y * height, node.r + node.flash * 3);
        ctx.fill();
        if (node.flash > 0.05) {
          ctx.strokeStyle = `rgba(${ACCENT_LIGHT}, ${node.flash * 0.8})`;
          ctx.lineWidth = 1.2;
          hexPath(node.x * width, node.y * height, node.r + 4 + node.flash * 4);
          ctx.stroke();
        }
      }
    }

    for (const p of pulses) {
      const a = p.chain.nodes[p.linkIndex];
      const b = p.chain.nodes[p.linkIndex + 1];
      if (!a || !b) continue;
      const px = (a.x + (b.x - a.x) * p.t) * width;
      const py = (a.y + (b.y - a.y) * p.t) * height;
      const fade = 1 - Math.abs(p.t - 0.5) * 1.2;
      ctx.beginPath();
      ctx.arc(px, py, 2.4, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${ACCENT_LIGHT}, ${Math.max(0.2, fade)})`;
      ctx.fill();
    }
  }

  function loop(now) {
    if (!running || paused) { looping = false; return; }
    const dt = Math.min(64, now - lastTime);
    lastTime = now;
    step(dt);
    draw();
    requestAnimationFrame(loop);
  }

  function kick() {
    if (looping || reduceMotion || !running || paused) return;
    looping = true;
    lastTime = performance.now();
    requestAnimationFrame(loop);
  }

  function start() {
    resize();
    buildChains();
    if (reduceMotion) {
      draw(); // one static frame, no motion, no pulses
      return;
    }
    pulseTimer = rand(600, 1600);
    kick();
  }

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      resize();
      if (reduceMotion) draw();
    }, 150);
  });

  document.addEventListener("visibilitychange", () => {
    running = !document.hidden;
    kick();
  });

  window.addEventListener("joschat:theme", () => {
    readColors();
    if (reduceMotion) draw();
  });

  // The app calls this: the chain animation only plays on the log-in screens.
  window.JoschatBackground = {
    setActive(active) {
      paused = !active;
      if (active) {
        resize();
        if (reduceMotion) draw(); else kick();
      }
    },
  };

  start();
})();

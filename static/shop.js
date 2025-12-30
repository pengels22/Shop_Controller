
(() => {
  // -----------------------------
  // Config
  // -----------------------------
  const POLL_MS = 1000;
  // If you run behind a proxy or different host, set BASE_URL like: "http://192.168.1.50:8080"
  const BASE_URL = "";

  // Pressure gauge settings (UI only)
  const PSI_MAX = 120;

  // Service detection: bench is "in service" when its VBUS enable is ON
  const SERVICE_VBUS_BY_BENCH = {
    bench1: "port1_vcc_en",
    bench2: "port2_vcc_en",
    bench3: "port3_vcc_en",
    bench4: "port4_vcc_en"
  };

  // Map bench -> rails master checkbox id
  const MASTER_TOGGLE_BY_BENCH = {
    bench1: "bench1Master",
    bench2: "bench2Master",
    bench3: "bench3Master",
    bench4: "bench4Master"
  };

  const SERVICE_HOLD_MS = 1000;

  // Keep last-known backend state snapshot for toggle decisions
  let lastState = null;

  // Store rails snapshot (per bench) for restore when service is turned OFF
  const railSnapshot = { bench1: null, bench2: null, bench3: null, bench4: null };

  function benchNum(bench) {
    return Number(String(bench).replace("bench", ""));
  }
  function benchRailsChannels(bench) {
    const n = benchNum(bench);
    return { v5: `bench${n}_5v`, v12: `bench${n}_12v`, hv: `bench${n}_hv` };
  }
  function snapshotRails(bench, st) {
    if (!st) return;
    if (railSnapshot[bench]) return; // already captured
    const r = benchRailsChannels(bench);
    railSnapshot[bench] = { v5: !!st[r.v5], v12: !!st[r.v12], hv: !!st[r.hv] };
  }
  async function restoreRails(bench) {
    const snap = railSnapshot[bench];
    if (!snap) return;
    const r = benchRailsChannels(bench);

    // Restore in safe ON order: 5V -> 12V -> HV
    await setChannel(r.v5,  snap.v5);
    await setChannel(r.v12, snap.v12);
    await setChannel(r.hv,  snap.hv);

    railSnapshot[bench] = null;
  }

  // -----------------------------
  // DOM
  // -----------------------------
  const dot = document.getElementById("dot");
  const statusText = document.getElementById("statusText");
  const lastUpdate = document.getElementById("lastUpdate");
  const lastError = document.getElementById("lastError");
  const baseUrlLabel = document.getElementById("baseUrlLabel");
  const pollLabel = document.getElementById("pollLabel");

  const bench1Master = document.getElementById("bench1Master");
  const bench2Master = document.getElementById("bench2Master");
  const bench3Master = document.getElementById("bench3Master");
  const bench4Master = document.getElementById("bench4Master");

  const lightsToggle = document.getElementById("lightsToggle");
  const airToggle = document.getElementById("airToggle");

  const allOffBtn = document.getElementById("allOffBtn");
  const refreshBtn = document.getElementById("refreshBtn");

  const psiValue = document.getElementById("psiValue");
  const psiBar = document.getElementById("psiBar");

  baseUrlLabel.textContent = BASE_URL ? BASE_URL : "same-origin";
  pollLabel.textContent = `${POLL_MS}ms`;

  // -----------------------------
  // Helpers
  // -----------------------------
  const api = (path) => `${BASE_URL}${path}`;

  async function fetchJson(path, opts) {
    const res = await fetch(api(path), {
      headers: { "Content-Type": "application/json" },
      ...opts
    });
    if (!res.ok) {
      const t = await res.text().catch(() => "");
      throw new Error(`${res.status} ${res.statusText}${t ? " - " + t : ""}`);
    }
    return res.json();
  }

  function setStatus(ok, msg) {
    statusText.textContent = msg;
    dot.classList.remove("ok", "bad");
    if (ok === true) dot.classList.add("ok");
    else if (ok === false) dot.classList.add("bad");
  }

  function stampUpdate() {
    const d = new Date();
    lastUpdate.textContent = d.toLocaleTimeString();
  }

  function setErr(msg) {
    lastError.textContent = msg || "—";
    lastError.style.color = msg ? "var(--warn)" : "var(--muted)";
  }

  function clamp(n, a, b) {
    return Math.max(a, Math.min(b, n));
  }

  function isBenchInService(st, bench) {
    const vbusCh = SERVICE_VBUS_BY_BENCH[bench];
    if (!vbusCh) return false;
    return !!st[vbusCh];
  }

  function applyServiceUi(st) {
    document.querySelectorAll(".svc-btn[data-bench]").forEach(btn => {
      const bench = btn.getAttribute("data-bench");
      if (!bench) return;

      const active = isBenchInService(st, bench);
      btn.classList.toggle("svc-active", active);

      // Force rails master OFF + disabled while in service
      const masterId = MASTER_TOGGLE_BY_BENCH[bench];
      const masterEl = masterId ? document.getElementById(masterId) : null;
      if (masterEl) {
        if (active) {
          masterEl.checked = false;
          masterEl.disabled = true;
        } else {
          masterEl.disabled = false;
        }
      }
    });
  }

  // -----------------------------
  // Backend actions
  // -----------------------------
  async function setChannel(channel, state) {
    return fetchJson("/api/set", {
      method: "POST",
      body: JSON.stringify({ channel, state })
    });
  }

  async function benchMasterSet(benchN, on) {
    const hv = `bench${benchN}_hv`;
    const v12 = `bench${benchN}_12v`;
    const v5 = `bench${benchN}_5v`;

    if (on) {
      await setChannel(v5, true);
      await setChannel(v12, true);
      await setChannel(hv, true);
    } else {
      await setChannel(hv, false);
      await setChannel(v12, false);
      await setChannel(v5, false);
    }
  }

  async function setService(bench, enable) {
    return fetchJson("/api/bench_service", {
      method: "POST",
      body: JSON.stringify({ bench, enable })
    });
  }

  async function allOff() {
    return fetchJson("/api/all_off", { method: "POST", body: JSON.stringify({}) });
  }

  // -----------------------------
  // State sync
  // -----------------------------
  function masterFromState(st, n) {
    const hv = !!st[`bench${n}_hv`];
    const v12 = !!st[`bench${n}_12v`];
    const v5 = !!st[`bench${n}_5v`];
    return hv && v12 && v5;
  }

  async function refreshState() {
    const data = await fetchJson("/api/state");
    const st = data.state || {};
    lastState = st;

    if (!bench1Master.disabled) bench1Master.checked = masterFromState(st, 1);
    if (!bench2Master.disabled) bench2Master.checked = masterFromState(st, 2);
    if (!bench3Master.disabled) bench3Master.checked = masterFromState(st, 3);
    if (!bench4Master.disabled) bench4Master.checked = masterFromState(st, 4);

    lightsToggle.checked = !!st["lights"];
    airToggle.checked = !!st["air_compressor"];

    applyServiceUi(st);

    stampUpdate();
    setStatus(true, "Online");
    setErr("");
  }

  async function refreshPressure() {
    try {
      const data = await fetchJson("/api/air_pressure");
      const psi = Number(data.psi);
      if (!Number.isFinite(psi)) throw new Error("bad psi");
      psiValue.textContent = psi.toFixed(1);
      const pct = clamp((psi / PSI_MAX) * 100, 0, 100);
      psiBar.style.width = `${pct}%`;
    } catch (e) {
      psiValue.textContent = "—";
      psiBar.style.width = "0%";
    }
  }

  // -----------------------------
  // Wire events
  // -----------------------------
  bench1Master.addEventListener("change", async () => {
    if (bench1Master.disabled) return;
    try { await benchMasterSet(1, bench1Master.checked); await refreshState(); }
    catch (e) { setErr(String(e)); await refreshState().catch(()=>{}); }
  });
  bench2Master.addEventListener("change", async () => {
    if (bench2Master.disabled) return;
    try { await benchMasterSet(2, bench2Master.checked); await refreshState(); }
    catch (e) { setErr(String(e)); await refreshState().catch(()=>{}); }
  });
  bench3Master.addEventListener("change", async () => {
    if (bench3Master.disabled) return;
    try { await benchMasterSet(3, bench3Master.checked); await refreshState(); }
    catch (e) { setErr(String(e)); await refreshState().catch(()=>{}); }
  });
  bench4Master.addEventListener("change", async () => {
    if (bench4Master.disabled) return;
    try { await benchMasterSet(4, bench4Master.checked); await refreshState(); }
    catch (e) { setErr(String(e)); await refreshState().catch(()=>{}); }
  });

  lightsToggle.addEventListener("change", async () => {
    try { await setChannel("lights", lightsToggle.checked); await refreshState(); }
    catch (e) { setErr(String(e)); await refreshState().catch(()=>{}); }
  });

  airToggle.addEventListener("change", async () => {
    try { await setChannel("air_compressor", airToggle.checked); await refreshState(); }
    catch (e) { setErr(String(e)); await refreshState().catch(()=>{}); }
  });

  allOffBtn.addEventListener("click", async () => {
    if (!confirm("ALL OFF will kill all rails and disable USB ports. Continue?")) return;
    try { await allOff(); await refreshState(); }
    catch (e) { setErr(String(e)); }
  });

  refreshBtn.addEventListener("click", async () => {
    try { await refreshState(); await refreshPressure(); }
    catch (e) { setErr(String(e)); }
  });

  // -----------------------------
  // Service buttons: hold 1s toggles ON/OFF (no warning prompt)
  // -----------------------------
  function wireHoldToggle(button) {
    let holdTimer = null;

    const start = (e) => {
      button.classList.add("svc-hold");
      holdTimer = setTimeout(async () => {
        try {
          button.disabled = true;
          setStatus(null, "Service…");

          const bench = button.getAttribute("data-bench");
          if (!bench) return;

          const currentlyOn = !!(lastState && isBenchInService(lastState, bench));

          if (!currentlyOn) {
            snapshotRails(bench, lastState);
            await setService(bench, true);
            await refreshState();
          } else {
            await setService(bench, false);
            await restoreRails(bench);
            await refreshState();
          }
        } catch (err) {
          setErr(String(err));
        } finally {
          button.disabled = false;
          button.classList.remove("svc-hold");
          setStatus(null, "Online");
        }
      }, SERVICE_HOLD_MS);
    };

    const cancel = () => {
      if (holdTimer) clearTimeout(holdTimer);
      holdTimer = null;
      button.classList.remove("svc-hold");
    };

    // Mouse
    button.addEventListener("mousedown", (e) => { e.preventDefault(); start(e); });
    button.addEventListener("mouseup", cancel);
    button.addEventListener("mouseleave", cancel);

    // Touch (avoid blocking global focus more than needed)
    button.addEventListener("touchstart", (e) => { start(e); }, { passive: true });
    button.addEventListener("touchend", cancel);
    button.addEventListener("touchcancel", cancel);

    // Tap does nothing
    button.addEventListener("click", (e) => { e.preventDefault(); });
  }

  document.querySelectorAll(".svc-btn[data-bench]").forEach(btn => wireHoldToggle(btn));

  // -----------------------------
  // Poll loop
  // -----------------------------
  async function poll() {
    try {
      await refreshState();
      await refreshPressure();
    } catch (e) {
      setStatus(false, "Offline");
      setErr(String(e));
      dot.classList.add("bad");
    }
  }

  // -----------------------------
  // Terminal (xterm.js + Socket.IO)
  // -----------------------------
  const termHost = document.getElementById("terminalHost");
  const termStatus = document.getElementById("termStatus");
  const termClearBtn = document.getElementById("termClearBtn");
  const termReconnectBtn = document.getElementById("termReconnectBtn");

  let term = null;
  let fitAddon = null;
  let sock = null;

  function setTermStatus(msg, ok = true) {
    if (!termStatus) return;
    termStatus.textContent = msg;
    termStatus.style.color = ok ? "var(--muted)" : "var(--warn)";
  }

  function initTerm() {
    if (!termHost) return;

    term = new Terminal({
      cursorBlink: true,
      fontSize: 13,
      fontFamily:
        'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
      theme: { background: "rgba(0,0,0,0)" }
    });

    fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(termHost);
    fitAddon.fit();

    // ✅ CRITICAL: focus terminal on click/tap
    termHost.addEventListener("mousedown", () => term.focus());
    termHost.addEventListener("touchstart", () => term.focus(), { passive: true });

    term.onData((d) => {
      if (sock && sock.connected) sock.emit("term_in", d);
    });

    termClearBtn?.addEventListener("click", () => { term.clear(); term.focus(); });
    termReconnectBtn?.addEventListener("click", () => connectTerm());

    window.addEventListener("resize", () => {
      try {
        fitAddon.fit();
        sendResize();
      } catch (e) {}
    });
  }

  function sendResize() {
    if (!term || !sock || !sock.connected) return;
    sock.emit("term_resize", { cols: term.cols || 80, rows: term.rows || 24 });
  }

  function connectTerm() {
    if (!term) initTerm();
    if (!term) return;

    if (sock) {
      try { sock.disconnect(); } catch (e) {}
      sock = null;
    }

    setTermStatus("connecting…");

    const socketBase = BASE_URL ? BASE_URL : window.location.origin;
    sock = io(socketBase, { transports: ["websocket"] });

    sock.on("connect", () => {
      setTermStatus("connected");
      try { fitAddon.fit(); } catch (e) {}
      sock.emit("term_open", { cols: term.cols || 80, rows: term.rows || 24 });
      sendResize();
      setTimeout(() => term.focus(), 50);
    });

    sock.on("disconnect", () => setTermStatus("disconnected", false));
    sock.on("connect_error", () => setTermStatus("connect error", false));

    sock.on("term_out", (txt) => {
      term.write(txt);
    });
  }

  connectTerm();

  // Kickoff
  setStatus(null, "Connecting…");
  poll();
  setInterval(poll, POLL_MS);
})();


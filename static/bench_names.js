// ============================================================
// bench_names.js
// - Server-persisted Bench Project Names
// - Uses /api/bench_names (GET/POST)
// - Applies names to Bench 1..4 description lines
// - Modal opens ONLY on button click, closes on Save/Cancel/Esc
// ============================================================
(() => {
  const API_GET = "/api/bench_names";
  const API_SET = "/api/bench_names";

  // Optional fallback (only used if server fetch fails)
  const LS_KEY = "bench_project_names_v2_fallback";

  // --- DOM refs ---
  const btnOpen = document.getElementById("benchNamesBtn");
  const modal = document.getElementById("benchNamesModal");
  const backdrop = document.getElementById("benchNamesBackdrop");
  const btnClose = document.getElementById("benchNamesClose");
  const btnCancel = document.getElementById("benchNamesCancel");
  const btnSave = document.getElementById("benchNamesSave");
  const btnClear = document.getElementById("benchNamesClear");

  const inputs = [
    document.getElementById("benchName1"),
    document.getElementById("benchName2"),
    document.getElementById("benchName3"),
    document.getElementById("benchName4"),
  ];

  // If the page doesn't have the modal markup/button for some reason, bail safely.
  if (!modal || !backdrop || !inputs[0] || !btnOpen) {
    return;
  }

  // Defaults shown when no project name is set
  const DEFAULT_DESC = [
    "Power 5V, 12V, and HV together",
    "HV + 12V + 5V together",
    "HV + 12V + 5V together",
    "HV + 12V + 5V together",
  ];

  function getBenchTiles() {
    // Each bench tile contains input#benchXMaster
    return [1, 2, 3, 4]
      .map((n) => {
        const master = document.getElementById(`bench${n}Master`);
        if (!master) return null;
        const tile = master.closest(".tile");
        if (!tile) return null;
        const desc = tile.querySelector(".desc");
        return { n, tile, desc };
      })
      .filter(Boolean);
  }

  function normalizeFromServer(obj) {
    return [
      String(obj?.b1 || ""),
      String(obj?.b2 || ""),
      String(obj?.b3 || ""),
      String(obj?.b4 || ""),
    ];
  }

  function normalizeToServer(namesArr) {
    return {
      b1: String(namesArr[0] || ""),
      b2: String(namesArr[1] || ""),
      b3: String(namesArr[2] || ""),
      b4: String(namesArr[3] || ""),
    };
  }

  function applyNamesToUI(names) {
    const tiles = getBenchTiles();
    for (const t of tiles) {
      if (!t.desc) continue;
      const v = (names[t.n - 1] || "").trim();
      t.desc.textContent = v ? v : DEFAULT_DESC[t.n - 1];
    }
  }

  function openModal(names) {
    inputs.forEach((inp, i) => (inp.value = (names[i] || "").trim()));
    backdrop.hidden = false;
    modal.hidden = false;
    setTimeout(() => inputs[0].focus(), 0);
  }

  function closeModal() {
    modal.hidden = true;
    backdrop.hidden = true;
  }

  // --- Server I/O ---
  async function fetchServerNames() {
    const res = await fetch(API_GET, { cache: "no-store" });
    if (!res.ok) throw new Error(`GET ${API_GET} -> ${res.status}`);
    const json = await res.json();
    return normalizeFromServer(json);
  }

  async function saveServerNames(names) {
    const payload = normalizeToServer(names);
    const res = await fetch(API_SET, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(`POST ${API_SET} -> ${res.status}`);
    const json = await res.json();
    return normalizeFromServer(json);
  }

  // --- Fallback localStorage (optional) ---
  function loadFallbackNames() {
    try {
      const raw = localStorage.getItem(LS_KEY);
      if (!raw) return ["", "", "", ""];
      return normalizeFromServer(JSON.parse(raw));
    } catch {
      return ["", "", "", ""];
    }
  }

  function saveFallbackNames(names) {
    try {
      localStorage.setItem(LS_KEY, JSON.stringify(normalizeToServer(names)));
    } catch {}
  }

  // --- Init: load from server and apply ---
  (async () => {
    try {
      const names = await fetchServerNames();
      applyNamesToUI(names);
      saveFallbackNames(names);
    } catch (e) {
      // server unreachable? use fallback
      const fallback = loadFallbackNames();
      applyNamesToUI(fallback);
    }
  })();

  // --- Wire buttons ---
  btnOpen.addEventListener("click", async () => {
    try {
      const names = await fetchServerNames(); // always load latest from server
      openModal(names);
    } catch {
      openModal(loadFallbackNames());
    }
  });

  btnClose.addEventListener("click", closeModal);
  btnCancel.addEventListener("click", closeModal);
  backdrop.addEventListener("click", closeModal);

  btnSave.addEventListener("click", async () => {
    const names = inputs.map((inp) => (inp.value || "").trim());
    try {
      const saved = await saveServerNames(names);
      applyNamesToUI(saved);
      saveFallbackNames(saved);
      closeModal(); // <-- disappears on save
    } catch (e) {
      // If save fails, still apply locally and keep fallback
      applyNamesToUI(names);
      saveFallbackNames(names);
      closeModal();
      console.warn("bench_names: save failed, using fallback", e);
    }
  });

  btnClear.addEventListener("click", async () => {
    const blank = ["", "", "", ""];
    inputs.forEach((inp) => (inp.value = ""));
    try {
      const saved = await saveServerNames(blank);
      applyNamesToUI(saved);
      saveFallbackNames(saved);
      closeModal();
    } catch (e) {
      applyNamesToUI(blank);
      saveFallbackNames(blank);
      closeModal();
      console.warn("bench_names: clear failed, using fallback", e);
    }
  });

  window.addEventListener("keydown", (e) => {
    if (modal.hidden) return;
    if (e.key === "Escape") closeModal();
  });
})();

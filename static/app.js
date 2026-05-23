(() => {
  const STORAGE_KEY = "meeting-point-finder:last-config";
  const statusEl = document.getElementById("status");
  const minutesInput = document.getElementById("max-minutes");
  const minutesValue = document.getElementById("minutes-value");
  const objectiveSelect = document.getElementById("objective");
  const cityHintInput = document.getElementById("city-hint");
  const locationStatus = document.getElementById("location-status");
  const useLocationBtn = document.getElementById("use-location");
  const importSetupBtn = document.getElementById("import-setup");
  const exportSetupBtn = document.getElementById("export-setup");
  const setupFileInput = document.getElementById("setup-file");
  const computeBtn = document.getElementById("compute-btn");
  const computeLabel = document.getElementById("compute-label");
  const exampleSelect = document.getElementById("example-select");
  const loadExampleBtn = document.getElementById("load-example");
  const scoreHeatmapToggle = document.getElementById("score-heatmap");
  const scoreLegend = document.getElementById("score-legend");
  const mapLoading = document.getElementById("map-loading");
  const mapLoadingText = document.getElementById("map-loading-text");
  const addressList = document.getElementById("address-list");
  const results = document.getElementById("results");
  const participantsEl = document.getElementById("participants");
  const mpCoordsEl = document.getElementById("mp-coords");
  const mpReachEl = document.getElementById("mp-reachability");
  const objBadge = document.getElementById("objective-badge");

  const PROFILE_LABELS = {
    walking: "Walking",
    cycling: "Cycling",
    driving: "Driving",
    "driving-traffic": "Driving + traffic",
  };
  const MARKER_COLORS = {
    user: "#0f766e",
    friend: "#0ea5e9",
    meeting: "#fbbf24",
  };
  const SCORE_SOURCE_ID = "score-heatmap-source";
  const SCORE_LAYER_ID = "score-heatmap-layer";
  const SCORE_SURFACE_MAX_CELLS = 3600;
  const SCORE_INTERPOLATION_POWER = 2;
  const OBJECTIVE_LABELS = {
    min_sum: "Lowest total",
    min_max: "Fairest split",
  };
  const DEFAULT_CENTER = [-84.39, 33.77];
  const DEFAULT_ZOOM = 13;
  const BUSY_PRIORITY = ["compute", "routes", "example", "location", "examples", "map", "import"];
  const activeBusy = new Map();
  let currentUserLocation = null;
  let examples = [];
  let canCompute = false;
  let loadingControls = false;
  let latestMeetingData = null;
  let latestPayloadKey = null;
  let selectedExampleId = "";
  let map = null;
  let mapReady = Promise.resolve();
  const restoredSetup = readStoredSetup();

  function syncBusyUi() {
    const busyKey = BUSY_PRIORITY.find((key) => activeBusy.has(key));
    const busyMessage = busyKey ? activeBusy.get(busyKey) : "";
    const computeLoading = activeBusy.has("compute") || activeBusy.has("routes") || activeBusy.has("example");
    const blockControls = loadingControls || computeLoading;

    computeBtn.disabled = blockControls || !canCompute;
    loadExampleBtn.disabled = blockControls || !canCompute || !examples.length;
    exampleSelect.disabled = blockControls || !examples.length;
    scoreHeatmapToggle.disabled = blockControls || !canCompute;
    computeBtn.classList.toggle("is-loading", computeLoading);
    computeBtn.setAttribute("aria-busy", computeLoading ? "true" : "false");
    statusEl.classList.toggle("status-busy", Boolean(busyMessage));
    mapLoading.classList.toggle("hidden", !busyMessage);
    if (busyMessage) mapLoadingText.textContent = busyMessage;
  }

  function startBusy(key, message) {
    activeBusy.set(key, message);
    syncBusyUi();
  }

  function finishBusy(key) {
    activeBusy.delete(key);
    syncBusyUi();
  }

  function setLoading(loading, label = "Finding best spot") {
    loadingControls = loading;
    computeLabel.textContent = loading ? label : "Find best spot";
    syncBusyUi();
  }

  function apiErrorMessage(response, data) {
    if (typeof data?.detail === "string") return data.detail;
    if (Array.isArray(data?.detail) && data.detail.length) {
      return data.detail.map((item) => item.msg || item.type || "Invalid input").join("; ");
    }
    return `API error: ${response.status}`;
  }

  function readStoredSetup() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY));
    } catch (err) {
      console.warn("Could not restore meeting setup", err);
      return null;
    }
  }

  function normalizeLocation(value) {
    const lat = Number(value?.lat);
    const lng = Number(value?.lng);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
    if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return null;
    const accuracy = Number(value?.accuracy_m);
    return {
      lat,
      lng,
      accuracy_m: Number.isFinite(accuracy) ? accuracy : null,
      area: typeof value?.area === "string" ? value.area : "",
      updated_at: Number(value?.updated_at) || Date.now(),
    };
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function normalizeMapView(value) {
    const center = value?.center;
    const lng = Number(Array.isArray(center) ? center[0] : center?.lng);
    const lat = Number(Array.isArray(center) ? center[1] : center?.lat);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
    if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return null;

    const zoom = Number(value?.zoom);
    const bearing = Number(value?.bearing);
    const pitch = Number(value?.pitch);
    return {
      center: { lng, lat },
      zoom: Number.isFinite(zoom) ? clamp(zoom, 1, 18) : DEFAULT_ZOOM,
      bearing: Number.isFinite(bearing) ? clamp(bearing, -180, 180) : 0,
      pitch: Number.isFinite(pitch) ? clamp(pitch, 0, 60) : 0,
    };
  }

  function currentMapView() {
    if (!map) return normalizeMapView(restoredSetup?.map_view);
    const center = map.getCenter();
    return {
      center: { lng: center.lng, lat: center.lat },
      zoom: map.getZoom(),
      bearing: map.getBearing(),
      pitch: map.getPitch(),
    };
  }

  function applyMapView(view, animate = false) {
    const normalized = normalizeMapView(view);
    if (!map || !normalized) return;
    const camera = {
      center: [normalized.center.lng, normalized.center.lat],
      zoom: normalized.zoom,
      bearing: normalized.bearing,
      pitch: normalized.pitch,
      duration: animate ? 700 : 0,
    };
    if (animate) {
      map.flyTo(camera);
    } else {
      map.jumpTo(camera);
    }
  }

  function formatDistance(meters) {
    if (meters >= 1000) return `${(meters / 1000).toFixed(1)} km`;
    return `${Math.round(meters)} m`;
  }

  function accuracyText(location) {
    if (!Number.isFinite(location?.accuracy_m)) return "";
    return `, ±${formatDistance(location.accuracy_m)}`;
  }

  function locationLabel(location) {
    if (!location) return "Location not set";
    return `Using ${location.area || "your current area"}${accuracyText(location)}`;
  }

  function locationPopupLines(location) {
    const lines = [];
    if (location.area) lines.push(location.area);
    if (Number.isFinite(location.accuracy_m)) lines.push(`Accuracy ${formatDistance(location.accuracy_m)}`);
    return lines;
  }

  function normalizeObjective(value) {
    return value === "min_max" ? "min_max" : "min_sum";
  }

  function totalEtaMinutes(participants) {
    return participants.reduce((total, participant) => total + participant.eta_minutes, 0);
  }

  function meetingPayload(participants, includeDebug) {
    return {
      addresses: participants.map((p) => p.address),
      profiles: participants.map((p) => p.profile),
      city_hint: cityHintInput.value.trim() || undefined,
      max_minutes: Number(minutesInput.value),
      objective: normalizeObjective(objectiveSelect.value),
      use_grid_search: true,
      include_debug: includeDebug,
    };
  }

  function meetingPayloadKey(participants) {
    const payload = meetingPayload(participants, false);
    delete payload.include_debug;
    return JSON.stringify(payload);
  }

  function removeTrackedMapItem(ids, id, remove) {
    if (!map) return;
    if (map.getLayer(id) || map.getSource(id)) remove(id);
    const idx = ids.indexOf(id);
    if (idx >= 0) ids.splice(idx, 1);
  }

  minutesInput.addEventListener("input", () => {
    minutesValue.textContent = minutesInput.value;
  });

  function profileSelect(selected = "walking") {
    const select = document.createElement("select");
    select.className = "row-profile";
    Object.entries(PROFILE_LABELS).forEach(([value, label]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      option.selected = value === selected;
      select.appendChild(option);
    });
    return select;
  }

  function currentParticipants() {
    return Array.from(addressList.querySelectorAll(".address-row"))
      .map((row) => ({
        address: row.querySelector("input").value.trim(),
        profile: row.querySelector("select").value,
      }))
      .filter((p) => p.address);
  }

  function matchingLatestResult(participants) {
    if (!latestMeetingData || !latestPayloadKey) return null;
    if (latestPayloadKey !== meetingPayloadKey(participants)) return null;
    return {
      payload_key: latestPayloadKey,
      data: latestMeetingData,
      saved_at: Date.now(),
    };
  }

  function setupState() {
    const participants = currentParticipants();
    return {
      version: 1,
      city_hint: cityHintInput.value.trim(),
      objective: normalizeObjective(objectiveSelect.value),
      max_minutes: Number(minutesInput.value),
      participants,
      user_location: currentUserLocation,
      selected_example_id: selectedExampleId,
      map_view: currentMapView(),
      latest_result: matchingLatestResult(participants),
    };
  }

  function saveFormState() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(setupState()));
    } catch (err) {
      console.warn("Could not save meeting setup", err);
    }
  }

  function forgetResult() {
    latestMeetingData = null;
    latestPayloadKey = null;
  }

  function markManualEdit() {
    selectedExampleId = "";
    exampleSelect.value = "";
    forgetResult();
    saveFormState();
  }

  function clearRows() {
    addressList.replaceChildren();
  }

  function addRow(val = "", profile = "walking") {
    const row = document.createElement("div");
    row.className = "address-row";
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = "123 Peachtree St NE";
    input.value = val;
    const mode = profileSelect(profile);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "remove";
    btn.textContent = "×";
    btn.addEventListener("click", () => {
      row.remove();
      markManualEdit();
    });
    row.appendChild(input);
    row.appendChild(mode);
    row.appendChild(btn);
    addressList.appendChild(row);
  }

  function loadSetup(state, persist = true, options = {}) {
    const participants = Array.isArray(state?.participants) ? state.participants.slice(0, 10) : [];
    const nextExampleId =
      typeof options.selectedExampleId === "string"
        ? options.selectedExampleId
        : typeof state?.selected_example_id === "string"
          ? state.selected_example_id
          : "";
    const nextMapView = normalizeMapView(state?.map_view);
    if (!options.keepResult) forgetResult();
    clearRows();
    selectedExampleId = nextExampleId;
    if (exampleSelect.options.length) exampleSelect.value = selectedExampleId;
    cityHintInput.value = typeof state?.city_hint === "string" ? state.city_hint : "";
    objectiveSelect.value = normalizeObjective(state?.objective);
    currentUserLocation = normalizeLocation(state?.user_location);
    locationStatus.textContent = currentUserLocation
      ? locationLabel(currentUserLocation)
      : cityHintInput.value
        ? `Using ${cityHintInput.value}`
        : "Location not set";
    if (Number.isFinite(state?.max_minutes)) {
      minutesInput.value = state.max_minutes;
      minutesValue.textContent = state.max_minutes;
    }

    if (participants.length) {
      participants.forEach((p) => addRow(p.address || "", normalizeImportedProfile(p.profile)));
    } else {
      addRow();
      addRow();
    }

    if (nextMapView) applyMapView(nextMapView, Boolean(options.animateMap));
    if (persist) saveFormState();
  }

  function restoreFormState() {
    loadSetup(restoredSetup, false);
  }

  function normalizeImportedProfile(value) {
    const normalized = String(value || "").trim().toLowerCase();
    if (PROFILE_LABELS[normalized]) return normalized;
    if (["car", "drive", "driving"].includes(normalized)) return "driving";
    if (["bike", "bicycle", "cycle", "cycling"].includes(normalized)) return "cycling";
    if (["walk", "walking"].includes(normalized)) return "walking";
    if (["traffic", "driving traffic", "driving-traffic"].includes(normalized)) return "driving-traffic";
    return "walking";
  }

  function parseCsv(text) {
    const rows = [];
    let row = [];
    let value = "";
    let quoted = false;
    for (let i = 0; i < text.length; i += 1) {
      const char = text[i];
      const next = text[i + 1];
      if (char === '"' && quoted && next === '"') {
        value += '"';
        i += 1;
      } else if (char === '"') {
        quoted = !quoted;
      } else if (char === "," && !quoted) {
        row.push(value.trim());
        value = "";
      } else if ((char === "\n" || char === "\r") && !quoted) {
        if (char === "\r" && next === "\n") i += 1;
        row.push(value.trim());
        if (row.some(Boolean)) rows.push(row);
        row = [];
        value = "";
      } else {
        value += char;
      }
    }
    row.push(value.trim());
    if (row.some(Boolean)) rows.push(row);
    return rows;
  }

  function participantsFromCsv(text) {
    const rows = parseCsv(text);
    if (!rows.length) return [];
    const normalizedHeaders = rows[0].map((cell) => cell.trim().toLowerCase());
    const addressHeaders = ["address", "location", "place", "name", "title"];
    const profileHeaders = ["profile", "mode", "transport", "transportation"];
    const addressIndex = normalizedHeaders.findIndex((header) => addressHeaders.includes(header));
    const profileIndex = normalizedHeaders.findIndex((header) => profileHeaders.includes(header));
    const hasHeaders = addressIndex >= 0 || profileIndex >= 0;
    const dataRows = hasHeaders ? rows.slice(1) : rows;
    const fallbackAddressIndex = addressIndex >= 0 ? addressIndex : 0;

    return dataRows
      .map((row) => ({
        address: row[fallbackAddressIndex] || "",
        profile: normalizeImportedProfile(profileIndex >= 0 ? row[profileIndex] : ""),
      }))
      .filter((p) => p.address)
      .slice(0, 10);
  }

  function setupFromFileText(text) {
    const trimmed = text.trim();
    if (trimmed.startsWith("{")) {
      const parsed = JSON.parse(trimmed);
      return {
        city_hint: parsed.city_hint || "",
        objective: normalizeObjective(parsed.objective),
        max_minutes: Number(parsed.max_minutes) || Number(minutesInput.value),
        participants: Array.isArray(parsed.participants) ? parsed.participants : [],
        user_location: normalizeLocation(parsed.user_location),
        selected_example_id: typeof parsed.selected_example_id === "string" ? parsed.selected_example_id : "",
        map_view: normalizeMapView(parsed.map_view),
        latest_result: parsed.latest_result || null,
      };
    }
    return {
      city_hint: cityHintInput.value.trim(),
      objective: normalizeObjective(objectiveSelect.value),
      max_minutes: Number(minutesInput.value),
      participants: participantsFromCsv(text),
    };
  }

  function exportSetup() {
    const blob = new Blob([JSON.stringify(setupState(), null, 2)], { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "meeting-point-setup.json";
    link.click();
    URL.revokeObjectURL(link.href);
  }

  async function importSetup(file) {
    startBusy("import", "Importing setup");
    try {
      const text = await file.text();
      const setup = setupFromFileText(text);
      if (!setup.participants.length) {
        statusEl.textContent = "No places found in import.";
        return;
      }
      loadSetup(setup);
      statusEl.textContent = `Imported ${setup.participants.length} places.`;
    } finally {
      finishBusy("import");
    }
  }

  function normalizeExample(example) {
    const participants = Array.isArray(example?.participants)
      ? example.participants
          .map((participant) => ({
            address: String(participant?.address || "").trim(),
            profile: normalizeImportedProfile(participant?.profile),
          }))
          .filter((participant) => participant.address)
          .slice(0, 10)
      : [];
    if (!participants.length) return null;
    return {
      id: String(example?.id || example?.label || ""),
      label: String(example?.label || "Example"),
      city_hint: String(example?.city_hint || ""),
      objective: normalizeObjective(example?.objective),
      max_minutes: Number(example?.max_minutes) || Number(minutesInput.value),
      map_view: normalizeMapView(example?.map_view),
      participants,
    };
  }

  async function loadExamples() {
    startBusy("examples", "Loading examples");
    try {
      const response = await fetch("/examples.json");
      if (!response.ok) throw new Error("Examples request failed");
      const data = await response.json();
      examples = Array.isArray(data) ? data.map(normalizeExample).filter(Boolean) : [];
    } catch (err) {
      console.warn("Could not load examples", err);
      examples = [];
    } finally {
      finishBusy("examples");
    }

    if (!examples.length) {
      setLoading(false);
      return;
    }

    examples.forEach((example) => {
      const option = document.createElement("option");
      option.value = example.id;
      option.textContent = example.label;
      exampleSelect.appendChild(option);
    });
    if (selectedExampleId && examples.some((example) => example.id === selectedExampleId)) {
      exampleSelect.value = selectedExampleId;
    }
    setLoading(false);
  }

  async function loadSelectedExample() {
    const example = examples.find((item) => item.id === exampleSelect.value);
    if (!example) return;
    if (!canCompute) {
      statusEl.textContent = "Mapbox public token missing. Examples need the map to compute routes.";
      return;
    }
    selectedExampleId = example.id;
    startBusy("example", `Loading ${example.label}`);
    try {
      loadSetup({ ...example, selected_example_id: example.id }, true, { animateMap: true, selectedExampleId: example.id });
      statusEl.textContent = `Loaded ${example.label}. Finding best spot...`;
      await computeMeetingPoint();
    } finally {
      finishBusy("example");
    }
  }

  restoreFormState();
  const examplesReady = loadExamples();

  const meetingForm = document.getElementById("meeting-form");
  document.getElementById("add-row").addEventListener("click", () => {
    addRow();
    markManualEdit();
  });
  meetingForm.addEventListener("input", markManualEdit);
  meetingForm.addEventListener("change", (event) => {
    if (event.target === exampleSelect) return;
    markManualEdit();
  });
  useLocationBtn.addEventListener("click", () => {
    selectedExampleId = "";
    exampleSelect.value = "";
    forgetResult();
    useBrowserLocation({ moveToLocation: true, updateAreaHint: true });
  });
  scoreHeatmapToggle.addEventListener("change", () => {
    if (!scoreHeatmapToggle.checked) {
      removeScoreHeatmap();
      return;
    }
    const participants = currentParticipants();
    if (latestMeetingData?.debug?.candidate_points_geojson && latestPayloadKey === meetingPayloadKey(participants)) {
      drawScoreHeatmap(latestMeetingData);
      return;
    }
    if (participants.length) {
      computeMeetingPoint();
    }
  });
  loadExampleBtn.addEventListener("click", loadSelectedExample);
  exampleSelect.addEventListener("change", () => {
    if (exampleSelect.value) {
      loadSelectedExample();
    } else {
      selectedExampleId = "";
      forgetResult();
      saveFormState();
    }
  });
  importSetupBtn.addEventListener("click", () => setupFileInput.click());
  exportSetupBtn.addEventListener("click", exportSetup);
  setupFileInput.addEventListener("change", async () => {
    const file = setupFileInput.files?.[0];
    if (!file) return;
    try {
      await importSetup(file);
    } catch (err) {
      console.warn("Could not import setup", err);
      statusEl.textContent = "Could not import that file.";
    } finally {
      setupFileInput.value = "";
    }
  });

  const mapToken = window.MAPBOX_TOKEN;
  if (!mapToken) {
    statusEl.textContent = "Mapbox public token missing. Set MAPBOX_PUBLIC_TOKEN or use a public pk token.";
    setLoading(false);
    return;
  }
  canCompute = true;
  setLoading(false);

  mapboxgl.accessToken = mapToken;
  const restoredMapView = normalizeMapView(restoredSetup?.map_view);
  const initialMapView =
    restoredMapView ||
    normalizeMapView({
      center: currentUserLocation ? [currentUserLocation.lng, currentUserLocation.lat] : DEFAULT_CENTER,
      zoom: currentUserLocation ? 13.5 : DEFAULT_ZOOM,
    });
  startBusy("map", "Loading map");
  map = new mapboxgl.Map({
    container: "map",
    style: "mapbox://styles/mapbox/streets-v12",
    center: [initialMapView.center.lng, initialMapView.center.lat],
    zoom: initialMapView.zoom,
    bearing: initialMapView.bearing,
    pitch: initialMapView.pitch,
  });
  mapReady = new Promise((resolve) => {
    if (map.loaded()) {
      finishBusy("map");
      resolve();
      return;
    }
    map.on("load", () => {
      finishBusy("map");
      resolve();
    });
  });
  map.on("moveend", saveFormState);

  const layerIds = [];
  const sources = [];
  let markers = [];
  let userMarker = null;

  renderUserLocation(currentUserLocation, { updateAreaHint: false });
  initializeMapState();

  function storedLatestResult() {
    const result = restoredSetup?.latest_result;
    if (!result?.data || typeof result.payload_key !== "string") return null;
    return {
      payloadKey: result.payload_key,
      data: result.data,
    };
  }

  async function restoreSavedResult() {
    const participants = currentParticipants();
    if (!participants.length) return false;

    const currentPayloadKey = meetingPayloadKey(participants);
    const stored = storedLatestResult();
    if (stored?.payloadKey === currentPayloadKey) {
      startBusy("routes", "Restoring saved result");
      try {
        await renderMeetingData(stored.data, stored.payloadKey, { fitMap: !restoredMapView, drawRoutes: true });
        statusEl.textContent = "";
      } catch (err) {
        console.warn("Could not restore saved result", err);
        forgetResult();
        saveFormState();
      } finally {
        finishBusy("routes");
      }
      if (latestMeetingData) return true;
    }

    if (selectedExampleId && canCompute) {
      statusEl.textContent = "Restoring example...";
      await computeMeetingPoint({ fitMap: !restoredMapView });
      return true;
    }
    return false;
  }

  async function initializeMapState() {
    await mapReady;
    await examplesReady;
    const restored = await restoreSavedResult();
    useBrowserLocation({
      moveToLocation: !restoredMapView && !restored && !selectedExampleId,
      updateAreaHint: !cityHintInput.value && !selectedExampleId,
    });
  }

  function clearMap() {
    markers.forEach((m) => m.remove());
    markers = [];
    layerIds.forEach((id) => {
      if (map.getLayer(id)) map.removeLayer(id);
    });
    sources.forEach((id) => {
      if (map.getSource(id)) map.removeSource(id);
    });
    layerIds.length = 0;
    sources.length = 0;
    scoreLegend.classList.add("hidden");
  }

  function removeScoreHeatmap() {
    removeTrackedMapItem(layerIds, SCORE_LAYER_ID, (id) => map.removeLayer(id));
    removeTrackedMapItem(sources, SCORE_SOURCE_ID, (id) => map.removeSource(id));
    scoreLegend.classList.add("hidden");
  }

  function candidateScoreFeatures(data) {
    return (data?.debug?.candidate_points_geojson?.features || []).filter(
      (feature) => feature?.properties?.reachable && Number.isFinite(Number(feature.properties.score))
    );
  }

  function visitCoordinates(coords, visitor) {
    if (!Array.isArray(coords)) return;
    if (typeof coords[0] === "number" && typeof coords[1] === "number") {
      visitor(coords);
      return;
    }
    coords.forEach((item) => visitCoordinates(item, visitor));
  }

  function geometryBounds(geometry) {
    const bounds = [Infinity, Infinity, -Infinity, -Infinity];
    visitCoordinates(geometry?.coordinates, ([lng, lat]) => {
      bounds[0] = Math.min(bounds[0], lng);
      bounds[1] = Math.min(bounds[1], lat);
      bounds[2] = Math.max(bounds[2], lng);
      bounds[3] = Math.max(bounds[3], lat);
    });
    return Number.isFinite(bounds[0]) ? bounds : null;
  }

  function pointInRing(point, ring) {
    let inside = false;
    const [lng, lat] = point;
    for (let idx = 0, prev = ring.length - 1; idx < ring.length; prev = idx++) {
      const [lngA, latA] = ring[idx];
      const [lngB, latB] = ring[prev];
      if ((latA > lat) !== (latB > lat) && lng < ((lngB - lngA) * (lat - latA)) / (latB - latA) + lngA) {
        inside = !inside;
      }
    }
    return inside;
  }

  function pointInPolygon(point, polygon) {
    if (!polygon?.length || !pointInRing(point, polygon[0])) return false;
    return !polygon.slice(1).some((hole) => pointInRing(point, hole));
  }

  function pointInGeometry(point, geometry) {
    if (geometry?.type === "Polygon") return pointInPolygon(point, geometry.coordinates);
    if (geometry?.type === "MultiPolygon") return geometry.coordinates.some((polygon) => pointInPolygon(point, polygon));
    return false;
  }

  function scoreSurfaceDimensions(bounds) {
    const width = Math.max(bounds[2] - bounds[0], 1e-9);
    const height = Math.max(bounds[3] - bounds[1], 1e-9);
    const ratio = Math.min(5, Math.max(0.2, width / height));
    const cols = Math.max(16, Math.round(Math.sqrt(SCORE_SURFACE_MAX_CELLS * ratio)));
    const rows = Math.max(16, Math.round(SCORE_SURFACE_MAX_CELLS / cols));
    return { cols, rows };
  }

  function interpolatedScore(lng, lat, features) {
    const lonScale = Math.max(0.1, Math.cos((lat * Math.PI) / 180));
    let weightedScore = 0;
    let totalWeight = 0;
    for (const feature of features) {
      const [pointLng, pointLat] = feature.geometry.coordinates;
      const score = Number(feature.properties.score);
      const dx = (lng - pointLng) * lonScale;
      const dy = lat - pointLat;
      const distanceSquared = dx * dx + dy * dy;
      if (distanceSquared < 1e-12) return score;
      const weight = 1 / Math.pow(distanceSquared, SCORE_INTERPOLATION_POWER / 2);
      weightedScore += score * weight;
      totalWeight += weight;
    }
    return totalWeight ? weightedScore / totalWeight : null;
  }

  function scoreSurfaceFeatures(data, scoreFeatures) {
    const geometry = data?.debug?.intersection_polygons_geojson;
    const bounds = geometryBounds(geometry);
    if (!bounds) return [];

    const { cols, rows } = scoreSurfaceDimensions(bounds);
    const cellWidth = (bounds[2] - bounds[0]) / cols;
    const cellHeight = (bounds[3] - bounds[1]) / rows;
    const features = [];

    for (let row = 0; row < rows; row += 1) {
      const y0 = bounds[1] + row * cellHeight;
      const y1 = row === rows - 1 ? bounds[3] : y0 + cellHeight;
      const centerLat = (y0 + y1) / 2;
      for (let col = 0; col < cols; col += 1) {
        const x0 = bounds[0] + col * cellWidth;
        const x1 = col === cols - 1 ? bounds[2] : x0 + cellWidth;
        const centerLng = (x0 + x1) / 2;
        if (!pointInGeometry([centerLng, centerLat], geometry)) continue;
        const score = interpolatedScore(centerLng, centerLat, scoreFeatures);
        if (!Number.isFinite(score)) continue;
        features.push({
          type: "Feature",
          properties: { score },
          geometry: {
            type: "Polygon",
            coordinates: [
              [
                [x0, y0],
                [x1, y0],
                [x1, y1],
                [x0, y1],
                [x0, y0],
              ],
            ],
          },
        });
      }
    }
    return features;
  }

  function drawScoreHeatmap(data) {
    removeScoreHeatmap();
    if (!scoreHeatmapToggle.checked) return;

    const scoreFeatures = candidateScoreFeatures(data);
    const surfaceFeatures = scoreSurfaceFeatures(data, scoreFeatures);
    if (!surfaceFeatures.length) {
      scoreLegend.classList.add("hidden");
      return;
    }

    const scores = scoreFeatures.map((feature) => Number(feature.properties.score));
    const minScore = Math.min(...scores);
    const maxScore = Math.max(...scores);
    const midScore = minScore === maxScore ? minScore + 1 : (minScore + maxScore) / 2;
    const highScore = minScore === maxScore ? minScore + 2 : maxScore;

    sources.push(SCORE_SOURCE_ID);
    map.addSource(SCORE_SOURCE_ID, {
      type: "geojson",
      data: {
        type: "FeatureCollection",
        features: surfaceFeatures,
      },
    });
    layerIds.push(SCORE_LAYER_ID);
    map.addLayer({
      id: SCORE_LAYER_ID,
      type: "fill",
      source: SCORE_SOURCE_ID,
      paint: {
        "fill-color": [
          "interpolate",
          ["linear"],
          ["to-number", ["get", "score"]],
          minScore,
          "#16a34a",
          midScore,
          "#facc15",
          highScore,
          "#dc2626",
        ],
        "fill-opacity": 0.42,
        "fill-outline-color": "rgba(255, 255, 255, 0)",
      },
    }, map.getLayer("routes-layer") ? "routes-layer" : undefined);
    scoreLegend.classList.remove("hidden");
  }

  function markerPopupContent(title, lines = []) {
    const root = document.createElement("div");
    root.className = "map-popup";
    const heading = document.createElement("strong");
    heading.textContent = title;
    root.appendChild(heading);

    lines.filter(Boolean).forEach((line) => {
      const item = document.createElement("p");
      item.textContent = line;
      root.appendChild(item);
    });
    return root;
  }

  function addMarker(lng, lat, options = {}) {
    const marker = new mapboxgl.Marker({ color: options.color || MARKER_COLORS.friend })
      .setLngLat([lng, lat])
      .addTo(map);
    const popup = new mapboxgl.Popup({
      closeButton: false,
      closeOnClick: true,
      className: "marker-popup",
      offset: 28,
    }).setDOMContent(markerPopupContent(options.title || "Location", options.lines || []));
    const element = marker.getElement();
    const openPopup = () => popup.setLngLat([lng, lat]).addTo(map);
    const closePopup = () => popup.remove();

    element.tabIndex = 0;
    element.setAttribute("role", "button");
    element.setAttribute("aria-label", options.ariaLabel || options.title || "Map marker");
    element.addEventListener("mouseenter", openPopup);
    element.addEventListener("mouseleave", closePopup);
    element.addEventListener("focus", openPopup);
    element.addEventListener("blur", closePopup);
    element.addEventListener("click", (event) => {
      event.stopPropagation();
      openPopup();
    });

    const trackedMarker = {
      remove() {
        closePopup();
        marker.remove();
      },
    };
    if (options.track !== false) markers.push(trackedMarker);
    return trackedMarker;
  }

  function renderUserLocation(location, options = {}) {
    if (!location) return;
    if (options.moveToLocation) {
      map.flyTo({ center: [location.lng, location.lat], zoom: 13.5, duration: 700 });
    }
    if (userMarker) userMarker.remove();
    userMarker = addMarker(location.lng, location.lat, {
      color: MARKER_COLORS.user,
      title: "Your location",
      lines: locationPopupLines(location),
      ariaLabel: `Your location. ${locationLabel(location)}`,
      track: false,
    });
    locationStatus.textContent = locationLabel(location);
    if (options.updateAreaHint && location.area) cityHintInput.value = location.area;
  }

  async function reverseGeocode(lng, lat) {
    const url = new URL(
      `https://api.mapbox.com/geocoding/v5/mapbox.places/${lng},${lat}.json`
    );
    url.searchParams.set("types", "place,locality,region");
    url.searchParams.set("limit", "1");
    url.searchParams.set("access_token", mapToken);
    const res = await fetch(url);
    if (!res.ok) throw new Error("Reverse geocoding failed");
    const data = await res.json();
    return data.features?.[0]?.place_name || "";
  }

  function useBrowserLocation(options = {}) {
    if (!navigator.geolocation) {
      locationStatus.textContent = "Location unavailable";
      return;
    }

    startBusy("location", currentUserLocation ? "Updating your area" : "Finding your area");
    locationStatus.textContent = currentUserLocation ? "Updating your area..." : "Waiting for permission...";
    navigator.geolocation.getCurrentPosition(
      async (position) => {
        const { latitude, longitude } = position.coords;
        let area = currentUserLocation?.area || "";
        try {
          area = await reverseGeocode(longitude, latitude);
        } catch (err) {
          console.warn("Could not reverse geocode current location", err);
        }
        currentUserLocation = {
          lat: latitude,
          lng: longitude,
          accuracy_m: Number.isFinite(position.coords.accuracy) ? position.coords.accuracy : null,
          area,
          updated_at: Date.now(),
        };
        if (options.updateAreaHint && !area) cityHintInput.value = `${latitude.toFixed(4)}, ${longitude.toFixed(4)}`;
        renderUserLocation(currentUserLocation, {
          moveToLocation: Boolean(options.moveToLocation),
          updateAreaHint: Boolean(options.updateAreaHint),
        });
        saveFormState();
        finishBusy("location");
      },
      () => {
        locationStatus.textContent = currentUserLocation ? locationLabel(currentUserLocation) : "Location off";
        finishBusy("location");
      },
      { enableHighAccuracy: true, maximumAge: 60000, timeout: 12000 }
    );
  }

  async function fetchDirections(origin, destination, profile) {
    const coords = `${origin[0]},${origin[1]};${destination[0]},${destination[1]}`;
    const url = new URL(
      `https://api.mapbox.com/directions/v5/mapbox/${profile}/${coords}`
    );
    url.searchParams.set("geometries", "geojson");
    url.searchParams.set("overview", "full");
    url.searchParams.set("access_token", mapToken);
    const res = await fetch(url);
    if (!res.ok) throw new Error("Directions request failed");
    const data = await res.json();
    const route = data.routes?.[0];
    if (!route || !route.geometry) throw new Error("No route");
    return route.geometry;
  }

  async function drawRoutes(participants, meetingPoint) {
    await mapReady;
    const lines = [];
    for (const p of participants) {
      try {
        const geom = await fetchDirections(
          [p.lng, p.lat],
          [meetingPoint.lng, meetingPoint.lat],
          p.profile || "walking"
        );
        lines.push({
          type: "Feature",
          geometry: geom,
          properties: { address: p.address, profile: p.profile || "walking" },
        });
      } catch (err) {
        console.warn("Route failed for", p.address, err);
      }
    }
    if (!lines.length) return;
    const sourceId = "routes";
    sources.push(sourceId);
    map.addSource(sourceId, {
      type: "geojson",
      data: { type: "FeatureCollection", features: lines },
    });
    const layerId = "routes-layer";
    layerIds.push(layerId);
    map.addLayer({
      id: layerId,
      type: "line",
      source: sourceId,
      paint: {
        "line-color": [
          "match",
          ["get", "profile"],
          "walking",
          "#6366f1",
          "cycling",
          "#10b981",
          "driving",
          "#f59e0b",
          "driving-traffic",
          "#ef4444",
          "#6366f1",
        ],
        "line-width": 4,
        "line-opacity": 0.7,
      },
    });
  }

  function fitBounds(features) {
    if (!features.length) return;
    const bounds = new mapboxgl.LngLatBounds();
    features.forEach((f) => bounds.extend(f));
    map.fitBounds(bounds, { padding: 60, maxZoom: 16, duration: 600 });
  }

  async function renderMeetingData(data, payloadKey, options = {}) {
    await mapReady;
    clearMap();
    latestMeetingData = data;
    latestPayloadKey = payloadKey;

    const objective = normalizeObjective(data.objective);
    objBadge.textContent = OBJECTIVE_LABELS[objective];
    if (!data.reachable) {
      mpCoordsEl.textContent = "No common reachable region";
      mpReachEl.textContent = `No spot where every friend is within ${data.max_minutes} minutes.`;
      participantsEl.innerHTML = "";
      drawScoreHeatmap(data);
      results.classList.remove("hidden");
      saveFormState();
      return;
    }

    mpCoordsEl.textContent = `${data.meeting_point.lat.toFixed(6)}, ${data.meeting_point.lng.toFixed(6)}`;
    const totalEta = totalEtaMinutes(data.participants);
    const maxEta = Math.max(...data.participants.map((p) => p.eta_minutes));
    mpReachEl.textContent =
      objective === "min_sum"
        ? `Total ETA ${totalEta.toFixed(1)} min; every friend is within ${data.max_minutes} minutes.`
        : `Worst ETA ${maxEta.toFixed(1)} min; every friend is within ${data.max_minutes} minutes.`;

    participantsEl.innerHTML = "";
    data.participants.forEach((p) => {
      const tr = document.createElement("tr");
      const tdA = document.createElement("td");
      tdA.textContent = p.address;
      const tdM = document.createElement("td");
      tdM.textContent = PROFILE_LABELS[p.profile] || p.profile;
      const tdE = document.createElement("td");
      tdE.textContent = p.eta_minutes.toFixed(1);
      tr.appendChild(tdA);
      tr.appendChild(tdM);
      tr.appendChild(tdE);
      participantsEl.appendChild(tr);
      addMarker(p.lng, p.lat, {
        color: MARKER_COLORS.friend,
        title: p.address,
        lines: [
          PROFILE_LABELS[p.profile] || p.profile,
          `${p.eta_minutes.toFixed(1)} min to meeting point`,
        ],
        ariaLabel: `${p.address}. ${PROFILE_LABELS[p.profile] || p.profile}. ${p.eta_minutes.toFixed(1)} minutes to meeting point.`,
      });
    });

    addMarker(data.meeting_point.lng, data.meeting_point.lat, {
      color: MARKER_COLORS.meeting,
      title: "Best meeting point",
      lines: [
        `Worst ETA ${maxEta.toFixed(1)} min`,
        `Total ETA ${totalEta.toFixed(1)} min`,
        `${data.meeting_point.lat.toFixed(5)}, ${data.meeting_point.lng.toFixed(5)}`,
      ],
      ariaLabel: `Best meeting point. Worst ETA ${maxEta.toFixed(1)} minutes.`,
    });
    const pts = data.participants.map((p) => [p.lng, p.lat]);
    pts.push([data.meeting_point.lng, data.meeting_point.lat]);
    if (options.fitMap !== false) fitBounds(pts);

    drawScoreHeatmap(data);
    results.classList.remove("hidden");

    if (options.drawRoutes !== false) {
      startBusy("routes", "Drawing routes");
      try {
        await drawRoutes(data.participants, data.meeting_point);
      } finally {
        finishBusy("routes");
      }
    }
    saveFormState();
  }

  async function computeMeetingPoint(options = {}) {
    const participants = currentParticipants();
    if (!participants.length) {
      statusEl.textContent = "Add at least one friend.";
      return;
    }
    statusEl.textContent = "Finding best spot...";
    startBusy("compute", "Finding best spot");
    setLoading(true, "Finding...");
    results.classList.add("hidden");
    forgetResult();
    await mapReady;
    clearMap();

    const payloadKey = meetingPayloadKey(participants);
    const payload = meetingPayload(participants, scoreHeatmapToggle.checked);

    let response;
    let data;
    try {
      response = await fetch("/api/meeting-point", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      data = await response.json();
    } catch (err) {
      statusEl.textContent = "Network error";
      finishBusy("compute");
      setLoading(false);
      return;
    }

    if (!response.ok) {
      statusEl.textContent = apiErrorMessage(response, data);
      finishBusy("compute");
      setLoading(false);
      return;
    }
    finishBusy("compute");
    try {
      await renderMeetingData(data, payloadKey, { fitMap: options.fitMap !== false, drawRoutes: true });
      statusEl.textContent = "";
    } catch (err) {
      console.warn("Could not draw meeting result", err);
      statusEl.textContent = "Found a result, but could not draw it on the map.";
    } finally {
      setLoading(false);
    }
  }

  function onSubmit(ev) {
    ev.preventDefault();
    computeMeetingPoint();
  }

  meetingForm.addEventListener("submit", onSubmit);
})();

(() => {
  const statusEl = document.getElementById("status");
  const minutesInput = document.getElementById("max-minutes");
  const minutesValue = document.getElementById("minutes-value");
  const objectiveSel = document.getElementById("objective");
  const profileSel = document.getElementById("profile");
  const cityHintInput = document.getElementById("city-hint");
  const addressList = document.getElementById("address-list");
  const results = document.getElementById("results");
  const participantsEl = document.getElementById("participants");
  const mpCoordsEl = document.getElementById("mp-coords");
  const mpReachEl = document.getElementById("mp-reachability");
  const objBadge = document.getElementById("objective-badge");

  minutesInput.addEventListener("input", () => {
    minutesValue.textContent = minutesInput.value;
  });

  function addRow(val = "") {
    const row = document.createElement("div");
    row.className = "address-row";
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = "123 Peachtree St NE";
    input.value = val;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "remove";
    btn.textContent = "×";
    btn.addEventListener("click", () => row.remove());
    row.appendChild(input);
    row.appendChild(btn);
    addressList.appendChild(row);
  }

  // Seed with two rows
  addRow();
  addRow();

  document.getElementById("add-row").addEventListener("click", () => addRow());

  const mapToken = window.MAPBOX_TOKEN;
  if (!mapToken) {
    statusEl.textContent = "MAPBOX_TOKEN not set on server; set MAPBOX_PUBLIC_TOKEN or MAPBOX_TOKEN env.";
    return;
  }

  mapboxgl.accessToken = mapToken;
  const map = new mapboxgl.Map({
    container: "map",
    style: "mapbox://styles/mapbox/streets-v12",
    center: [-84.39, 33.77],
    zoom: 13,
  });

  const layerIds = [];
  const sources = [];
  let markers = [];

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
  }

  function addMarker(lng, lat, color = "#22d3ee") {
    const marker = new mapboxgl.Marker({ color })
      .setLngLat([lng, lat])
      .addTo(map);
    markers.push(marker);
  }

  async function fetchDirections(origin, destination) {
    const url = new URL(
      `https://api.mapbox.com/directions/v5/mapbox/${profileSel.value}/${origin[0]},${origin[1]};${destination[0]},${destination[1]}`
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
    const lines = [];
    for (const p of participants) {
      try {
        const geom = await fetchDirections([p.lng, p.lat], [meetingPoint.lng, meetingPoint.lat]);
        lines.push({ type: "Feature", geometry: geom, properties: { address: p.address } });
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
        "line-color": "#6366f1",
        "line-width": 4,
        "line-opacity": 0.7,
      },
    });
  }

  function drawIntersection(geojson) {
    if (!geojson) return;
    const sourceId = "intersection";
    sources.push(sourceId);
    map.addSource(sourceId, {
      type: "geojson",
      data: geojson,
    });
    const layerId = "intersection-fill";
    const outlineId = "intersection-outline";
    layerIds.push(layerId, outlineId);
    map.addLayer({
      id: layerId,
      type: "fill",
      source: sourceId,
      paint: {
        "fill-color": "#22d3ee",
        "fill-opacity": 0.25,
      },
    });
    map.addLayer({
      id: outlineId,
      type: "line",
      source: sourceId,
      paint: {
        "line-color": "#22d3ee",
        "line-width": 2,
      },
    });
  }

  function fitBounds(features) {
    if (!features.length) return;
    const bounds = new mapboxgl.LngLatBounds();
    features.forEach((f) => bounds.extend(f));
    map.fitBounds(bounds, { padding: 60, maxZoom: 16, duration: 600 });
  }

  async function onSubmit(ev) {
    ev.preventDefault();
    const addresses = Array.from(addressList.querySelectorAll("input"))
      .map((i) => i.value.trim())
      .filter(Boolean);
    if (!addresses.length) {
      statusEl.textContent = "Add at least one address.";
      return;
    }
    statusEl.textContent = "Computing...";
    results.classList.add("hidden");
    clearMap();

    const payload = {
      addresses,
      city_hint: cityHintInput.value.trim() || undefined,
      max_minutes: Number(minutesInput.value),
      objective: objectiveSel.value,
      profile: profileSel.value,
    };

    let response;
    try {
      response = await fetch("/api/meeting-point", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch (err) {
      statusEl.textContent = "Network error";
      return;
    }

    if (!response.ok) {
      statusEl.textContent = "API error: " + response.status;
      return;
    }
    const data = await response.json();
    statusEl.textContent = "";

    objBadge.textContent = data.objective;
    if (!data.reachable) {
      mpCoordsEl.textContent = "No common reachable region";
      mpReachEl.textContent = `max_minutes: ${data.max_minutes}`;
      participantsEl.innerHTML = "";
      results.classList.remove("hidden");
      return;
    }

    mpCoordsEl.textContent = `${data.meeting_point.lat.toFixed(6)}, ${data.meeting_point.lng.toFixed(6)}`;
    mpReachEl.textContent = `Within ${data.max_minutes} minutes walking`;

    participantsEl.innerHTML = "";
    data.participants.forEach((p) => {
      const tr = document.createElement("tr");
      const tdA = document.createElement("td");
      tdA.textContent = p.address;
      const tdE = document.createElement("td");
      tdE.textContent = p.eta_minutes.toFixed(1);
      tr.appendChild(tdA);
      tr.appendChild(tdE);
      participantsEl.appendChild(tr);
      addMarker(p.lng, p.lat, "#0ea5e9");
    });

    addMarker(data.meeting_point.lng, data.meeting_point.lat, "#fbbf24");
    drawIntersection(data.debug?.intersection_polygons_geojson);

    // Fit view
    const pts = data.participants.map((p) => [p.lng, p.lat]);
    pts.push([data.meeting_point.lng, data.meeting_point.lat]);
    fitBounds(pts);

    // Draw routes after fit; fire and forget
    drawRoutes(data.participants, data.meeting_point);

    results.classList.remove("hidden");
  }

  document.getElementById("meeting-form").addEventListener("submit", onSubmit);
})();

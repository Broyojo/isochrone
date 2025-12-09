## 1. Goal

Given N addresses, compute a meeting point that is “fair” based on walking time:

* Prefer a point everyone can reach within a given max walking time `T` (e.g. 15 minutes).
* If multiple points qualify, choose one minimizing an objective (e.g. sum or max of travel times).
* If no such region exists at `T`, relax `T` or fall back to global optimization.

---

## 2. Stack

You can swap parts, but here’s a sane default:

* **Backend**: Python + FastAPI (or Flask)
* **Geospatial**: `shapely`, `pyproj`
* **HTTP**: `requests` or `httpx`
* **Frontend**: Mapbox GL JS, talking to backend JSON API

---

## 3. API Surface

Single core endpoint:

### `POST /api/meeting-point`

**Request body:**

```json
{
  "addresses": ["addr1", "addr2", "addr3"],
  "profile": "walking",
  "max_minutes": 15,
  "objective": "min_sum",      // "min_sum" | "min_max"
  "grid_resolution_m": 150,    // optional, for candidate search
  "city_hint": "Atlanta, GA"   // optional, concatenated to addresses
}
```

**Response (success):**

```json
{
  "meeting_point": {
    "lat": 33.7765,
    "lng": -84.3898
  },
  "participants": [
    {
      "address": "addr1",
      "lat": 33.77,
      "lng": -84.39,
      "eta_minutes": 11.2
    },
    {
      "address": "addr2",
      "lat": 33.78,
      "lng": -84.38,
      "eta_minutes": 9.6
    }
  ],
  "objective": "min_sum",
  "objective_value": 20.8,
  "max_minutes": 15,
  "reachable": true,
  "debug": {
    "intersection_polygons_geojson": {/* optional, see below */},
    "candidate_points_geojson": {/* optional */}
  }
}
```

**Response (no feasible region under constraints):**

```json
{
  "reachable": false,
  "reason": "no_common_reachable_region",
  "max_minutes": 15
}
```

HTTP status is still 200 unless it’s an actual server error.

---

## 4. Backend Logic

### 4.1 Input handling

1. Validate:

   * `addresses` nonempty, max N (e.g. 10).
   * `max_minutes` in some sane range (e.g. 5–60).
2. Normalize addresses:

   * Optionally append a `city_hint` if provided.
   * Deduplicate addresses.

### 4.2 Geocoding (Mapbox Geocoding API)

For each address:

* Call geocoding with `limit=1`.
* Extract `[lng, lat]`.
* If geocoding fails or returns no results ⇒ return 400 with which address failed.

Cache by address string to avoid repeat calls.

### 4.3 Isochrone fetch (Mapbox Isochrone API)

For each coordinate `(lng, lat)`:

* Call Isochrone API with:

  * profile: `"mapbox/walking"`
  * `contours_minutes=max_minutes`
  * `polygons=true`
* Extract the polygon geometry for that contour as `shapely.geometry.shape`.

Result: `P_i` polygon per participant.

Handle:

* API errors or 4xx/5xx ⇒ 502 to caller with message.
* Rate limiting ⇒ either simple backoff or return 429-ish error and tell user to try again.

### 4.4 Intersection region

* Compute intersection of all `P_i`:

```python
from functools import reduce
from shapely.ops import unary_union

# Either:
from shapely.ops import unary_intersection
intersection = unary_intersection(P_i_list)
# or manual reduce with intersection()
```

* If `intersection.is_empty`:

  * Set `reachable=false`.
  * Optionally: try a second pass with a larger `max_minutes` (e.g. *internal* bump 15 ⇒ 20). If you do that, expose the final value in the response.
  * If still empty ⇒ return “no_common_reachable_region”.

At this point you have a (multi)polygon region where everyone can get in `≤ max_minutes`.

### 4.5 Candidate point selection

Two layers: simple centroid fallback and more exact grid search.

#### 4.5.1 Cheap option: centroid

* Compute `intersection.centroid` ⇒ `(x, y)` in lon/lat.
* This is a valid meeting point (guaranteed reachable by all under `max_minutes` if geometry came from isochrones).
* Optionally, you can stop here and just return this.

#### 4.5.2 Better option: grid search inside intersection

1. **Generate grid of candidate points** within `intersection`:

   * Project geometry to a metric CRS (e.g. Web Mercator 3857 or a local UTM) for spacing in meters.
   * Build a regular grid at `grid_resolution_m` spacing covering the bounding box.
   * Keep only points that fall inside `intersection`.

2. **Compute travel times for each candidate**:

   * Use Mapbox **Matrix API** or **Directions API**:

     * Sources: all participants.
     * Destinations: candidate points (you may need to chunk if over API limits).
   * For each candidate, you get per-participant travel times.

3. **Objective computation**:

For candidate `c` with times `t_1, …, t_n`:

* `min_sum`: `obj(c) = sum(t_i)`
* `min_max`: `obj(c) = max(t_i)`

Reject candidates where any `t_i > max_minutes * 60` to be safe.

4. **Select best candidate**:

   * Argmin over all candidates.
   * If no candidate passes constraints, fall back to centroid.

5. **Result**:

   * Best candidate `c*`, with times `t_i*`.
   * Convert back to lon/lat.
   * Package per-participant times in response.

---

## 5. Frontend behavior

Single page with:

* Mapbox GL JS map.
* Input fields for addresses, add/remove rows.
* Controls:

  * `max_minutes` slider.
  * profile selection (for now, just “walking”, but keep UI generic).

Flow:

1. User enters addresses and clicks “Compute”.
2. Frontend sends POST to `/api/meeting-point`.
3. On success:

   * Place markers for user locations.
   * Place marker for `meeting_point`.
   * Optionally draw debug intersection polygon and candidate points from `debug` if returned.
   * Render a small table showing per-user walking time.
4. On failure:

   * Show clear message:

     * “There is no place all of you can reach within 15 minutes walking. Try a larger time limit.”

---

## 6. Config and constants

* `MAX_PARTICIPANTS` e.g. 10
* `DEFAULT_MAX_MINUTES` e.g. 15
* `MAX_MAX_MINUTES` e.g. 60
* `DEFAULT_GRID_RESOLUTION_M` e.g. 150–250 m
* Mapbox keys loaded from env

---

## 7. Edge cases to handle

* Geocoding failure or low confidence.
* Addresses that are extremely far apart ⇒ intersection empty.
* Mapbox rate limit (429) or network errors.
* MultiPolygon intersection (multiple disjoint reachable regions):

  * Either take union bounding box centroid, or
  * Choose the component with the largest area and work inside that.
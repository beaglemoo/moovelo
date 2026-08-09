# Rider's guide

Everything you can do in Moovelo, in roughly the order you'd do it: plan a
route, read what the app tells you about it, save it, and get it onto a
head unit. For what's running underneath, see
[docs/architecture.md](architecture.md).

## Planning a route

Open the app and click anywhere on the map to drop your first waypoint;
click again to extend the route. Moovelo routes between waypoints using
Valhalla, so every click reroutes along real roads and paths rather than
drawing a straight line.

- **Drag a waypoint marker** to move it - the route reroutes as you drag.
- **Drag the route line itself** to insert a new waypoint exactly where you
  drop it. Grab anywhere on the line (not a marker) and let go; the new
  point is inserted into the correct leg of the route, wherever along the
  route you grabbed it.
- **Right-click the map** (or long-press on a touch device) for a context
  menu: *Route from here* (moves the start), *Add waypoint* (appends to
  the end), *Route to here* (moves the end), and *Remove waypoint* /
  *Clear route* depending on what you clicked. If the place index is built
  (see [Place search and POIs](#place-search-and-pois) below), the menu
  also shows the name of the place you right-clicked.
- **Undo** and **Redo** step through your whole editing history -
  waypoint changes, preset switches, reordering, avoids, even Clear
  (Cmd/Ctrl+Z and Cmd/Ctrl+Shift+Z work too). **Clear** empties the
  route; if that was an accident, Undo brings it back.

### Presets

Three bicycle presets sit in the toolbar - **road**, **gravel**, and
**quiet** - each a different bundle of Valhalla bicycle costing options
rather than just a label:

| Preset | Behaviour |
|--------|-----------|
| road | Fast tarmac riding, comfortable on carriageways, hard-avoids unpaved surfaces |
| gravel | Seeks out unpaved tracks, towpaths and bridleways, biased away from tarmac |
| quiet | Strongly prefers cycleways and calm streets, softens climbs |

Switching preset re-routes your current waypoints immediately, so it's a
good way to see how different a road-bike line and a gravel line really
are between the same two points.

## Reading the panel

Once a route exists, a panel appears below the map.

**Stats.** Distance, ride time, and ascent/descent. The ride time shown
here is your *personal* estimate, not Valhalla's flat routing duration -
see [Ride time](#ride-time) below.

**Surface bar.** A stacked bar showing what the road surface is actually
made of along the route - Paved, Gravel, Path, and an Other bucket for
anything Valhalla tags but doesn't fit those three (impassable edges,
unrecognised surface strings). It also reports the percentage of the
route on marked cycling infrastructure (a cycleway or a lane), which is
tracked separately from surface type, since a paved cycleway and a paved
carriageway are both "paved" but not the same ride. This is decorative,
not authoritative: it never blocks saving, exporting or pushing a route,
and it quietly disappears rather than erroring when your route can't be
matched back onto the road network exactly (see
[Import](#import) below for when that happens).

**Gradient-coloured profile and line.** The elevation chart under the map
and the route line on the map itself are both coloured by gradient band -
blue for descent, then green, yellow, orange, red and dark red as the
climb gets steeper (0-3%, 3-6%, 6-9%, 9-12%, 12%+). Both use the same
maths, so a red stretch on the chart is the same red stretch on the map. A
route with no elevation data (see
[Troubleshooting](troubleshooting.md)) keeps the plain, unbanded line.

**Climbs.** Below the chart, a list of the climbs on this route, each
categorised HC down to 4 in the informal road-cycling sense (harder
climbs first). Categorisation is by a length/gradient score, not raw
gradient alone, so a long, steady drag can outrank a short, sharp ramp.
Hover a climb in the list to highlight it on both the chart and the map.

### Ride time

The time shown in the stats bar and throughout the library is computed
per-rider from the route's gradient and surface, not Valhalla's routing
duration - a route that's mostly gravel with a long climb shows a
noticeably longer time than the same distance on a flat, paved road,
because it should. It's recalculated on every read from your current
settings, so changing your rider profile updates the displayed time on
every route you've already saved without needing to re-save anything.

This estimate never changes what gets exported or pushed to Wahoo - the
FIT file and the Wahoo course always carry Valhalla's own duration, which
is what a head unit needs for sensible cue timing. See the FAQ entry
["Why does the app's ride time differ from Valhalla's duration?"](faq.md)
for the reasoning.

## Rider settings

At `/settings`, three fields feed the ride-time estimate:

- **Weight (kg)** - used together with FTP to work out your power-to-weight
  ratio.
- **Flat-road speed (km/h)** - your baseline pace on flat, paved ground.
  This is the number everything else scales from.
- **FTP (W, optional)** - if set, nudges your flat-road speed up or down
  from a 2.8 W/kg baseline, using a cube-root scaling (aero power roughly
  scales with speed cubed) rather than a full physics model. Riding at
  double the baseline watts/kg is modelled as riding about 26% faster on
  the flat, not twice as fast. Leave it blank and only your flat-road
  speed is used.

None of this changes routing itself - only the displayed time.

## Place search and POIs

Search, points of interest along a route, reverse-geocoded place names,
and the cycle-network overlay all depend on an **optional, opt-in index**
built from the same OpenStreetMap extract Valhalla already downloaded.
Nothing is fetched from an external geocoder - it's entirely local. Until
you build it, the search box, the POI panel, and the network overlay are
all hidden, and the app behaves exactly like a default install. Building
it is one command:

```sh
docker compose --profile index run --rm indexer
```

Full details - what it indexes, how long it takes, disk cost, and how to
keep it current - are in [docs/data.md](data.md).

Once it's built:

- A **search box** appears over the map. Type a place name (typos are
  tolerated - "birmingam" still finds Birmingham) and results are
  weighted toward wherever you're currently looking at on the map, since
  many English place names are shared by several towns or villages. Arrow
  through results and press Enter to add a waypoint there, or use the
  **From** / **To** buttons on a result to set it as the start or end of
  your route.
- **Right-clicking the map** shows the name of the place under your
  cursor at the top of the context menu.
- The **save dialog** suggests a name like "Tring to Ivinghoe Beacon"
  instead of today's date, based on reverse-geocoding your start and end
  points.
- A **POI panel** below the elevation chart lists water, coffee, toilets,
  bike shops/repair stands, food, pubs, rest stops and accommodation along
  your route, in the order you'll pass them, with distance off the route
  and opening hours where OpenStreetMap has them. Toggle categories with
  the chips at the top; water, coffee, toilets and bike are on by default,
  the rest are a click away. Hover an entry to highlight it on the map,
  and vice versa.
- A **cycle-network overlay** toggle appears on the map, drawing the
  signed National/Regional/Local Cycle Network as coloured lines. It's
  most useful on the OSM standard basemap, which draws no cycle routes at
  all - the default CyclOSM basemap already shows the national network
  itself, so the overlay mostly just recolours what's already there.

## Weather and wind

Off unless `WEATHER_API_URL` is set to an Open-Meteo-compatible forecast
URL (see [.env.example](../.env.example)). When configured, a "Show wind"
panel appears under the elevation chart. Pick a start time and press
**Show wind** - it never fetches on its own, only on that click, so an
instance with weather configured still makes no outbound request until
you ask for one.

It samples wind roughly every 10 km along the route (more often on a
short route, up to 20 samples on a long one), timing each sample to when
you'd actually reach it based on your ride-time estimate. For each point
you get:

- An **arrow**, rotated to point in the direction the wind is blowing
  *toward* (so it reads as "the wind is pushing this way").
- A **speed** in km/h.
- A **head/tailwind reading** relative to your direction of travel at
  that point - "18 km/h headwind", "12 km/h tailwind", or "crosswind"
  when the along-route component is small enough (under roughly 3 km/h)
  that calling it a headwind or tailwind would overstate it.

Forecast providers only look ahead so far - for Open-Meteo, about 16
days. Pick a start time beyond that and the panel says so ("Start time is
beyond the forecast window") rather than showing wrong numbers.

## Saving and the library

Click **Save** to name and store a route; **Save changes** appears once
you've edited a saved route. The library at `/library` lists everything
you've saved, with:

- **Tags** (free text, comma-separated) and **notes** - a route can carry
  several tags ("gravel", "with the kids") rather than living in one
  folder.
- **Favourites** - star a route to pin it to the top when filtering.
- **Search, filter and sort** - search covers both names and notes (so
  "cafe at 12km" written in a note is findable later), filter by tag,
  favourite, or planned/imported, and sort by date, name, distance or
  climbing.
- **Duplicate** - copies the route exactly as saved.
- **Reverse** - creates a *new* route ridden the other way. This
  deliberately re-routes between the reversed waypoints rather than just
  flipping the stored line: one-way streets and turn instructions are
  direction-dependent, so a flipped line would hand you turn cues for a
  ride you're not making. An imported route (see below) has no waypoints
  of its own to reverse, so its track is reversed and map-matched again
  instead.

## Import

Drop a GPX, TCX or FIT file anywhere in the app, or use the **Import**
button in the library. Multiple files can be dropped at once - each is
uploaded and processed one at a time, with its own row and status in the
import results.

What happens to an imported file:

- Its track is **map-matched** back onto the road network. This is what
  recovers turn-by-turn cues for a file that had none to begin with - a
  GPX export with just a line of points comes back with real maneuvers,
  the same as a route you'd planned yourself, and can be pushed to a head
  unit with cues.
- If the track **can't be matched** - it runs off the edge of your loaded
  map extract, or follows paths the routing graph doesn't have - it's
  kept as an unmatched line rather than rejected outright. An unmatched
  route has no turn cues and no surface breakdown (surface relies on the
  same exact-match requirement as matching), but everything else -
  distance, elevation, saving, exporting as GPX - still works. The
  library marks it "imported" either way; only the maneuver count tells
  you whether matching succeeded.
- **Elevation** comes from Valhalla the same way it does for a planned
  route, so ascent figures stay comparable across your whole library. The
  file's own recorded elevation is only used as a fallback, if your
  routing tiles were built without elevation data.
- File limits: 20 MB and 100,000 track points per file, GPX/TCX/FIT only.

An imported route's waypoints are just its start and end - editing it in
the planner re-routes between them, which throws away the imported track
and its cues. Moovelo asks before letting that happen.

## Export

Every saved route (and the planner toolbar, once a route is saved) offers
two downloads:

- **GPX** - a plain track with elevation. No timing, no turn instructions.
  Opens in pretty much anything.
- **FIT** - a course file carrying Valhalla's turn-by-turn maneuvers as
  course points, timed against the route's routing-engine duration. This
  is what gives a Wahoo (or other FIT-reading head unit) turn-by-turn cues
  as you ride, which a GPX simply cannot carry.

Export is disabled while a route has unsaved changes, since it downloads
from what's actually stored, not what's currently on screen.

## Wahoo sync

Push a saved route straight to your Wahoo account so it appears on your
ELEMNT after its next WiFi sync, as a FIT course with cues. This needs a
one-time setup (a free Wahoo developer app, HTTPS on your instance) -
the full walkthrough is [docs/wahoo-sync.md](wahoo-sync.md).

Once connected, every saved route gets a **Send to Wahoo** button. Pushes
are queued in the background rather than blocking the UI, and show a
status badge: queued, pushing, synced, or error (hover for the reason,
click again to retry). Re-pushing an edited or renamed route **updates
the same course** on Wahoo's side instead of creating a duplicate.

## Share links

Click **Share** on a saved route in the library to generate a public,
read-only link and copy it to your clipboard. Anyone with the link can
view the route on a map with its elevation profile and download it as
GPX - no account needed, and nothing about who owns the route or its
internal ID is exposed. **Unshare** revokes the link immediately;
sharing again issues a new one, so an old link stops working.

## The admin page

The first account you register becomes an admin automatically, and gets
a `/admin` page listing:

- User and route counts.
- The current auth configuration at a glance - whether signups and
  password login are enabled, whether SSO is configured (and which
  provider), and whether Wahoo is configured.
- Every user, with their route count, whether they've connected Wahoo,
  and a **Delete** button (removes the user and all their routes; admins
  can't be deleted from here).

It's read-only otherwise - configuration changes are still made through
environment variables and a restart, not from the page.

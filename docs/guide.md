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
  the end), *Route to here* (moves the end), *Remove waypoint* /
  *Clear route* depending on what you clicked, and *Loop from here* /
  *Isochrone from here* (see [Loops from a point](#loops-from-a-point) and
  [Isochrones](#isochrones) below). Right-click *on the route line itself*
  instead adds *Avoid this road* (see
  [Avoiding a road](#avoiding-a-road) below). If the place index is built
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

### Custom costing

The three presets are starting points, not the whole story. A fourth
**Custom…** option next to them opens the same Valhalla costing options
the presets are built from, as sliders:

| Control | What it changes |
|---------|-----------------|
| Bike type | Road, Hybrid, Cross or Mountain - changes which surfaces the router considers normal for you |
| Cycling speed | 10-35 km/h, the speed the router assumes on flat, good surface (this feeds routing decisions, not the displayed ride time) |
| Prefer roads | 0-100%, willingness to ride on busier carriageways |
| Hills OK | 0-100%, willingness to climb rather than route around |
| Avoid rough surfaces | 0-100%, how hard to dodge unpaved ground |

Move any slider and the route re-plans, with the preset row switching to
**Custom…**. Name a setup and press **Save as preset** to keep it - your
saved presets are listed in the same popover, one click to re-apply, and
you can hold up to 20 of them.

Saved presets are a convenience library for the sliders, nothing more. A
saved route stores the actual costing numbers it was planned with, so
deleting a preset never changes, breaks or re-routes a route you built
with it.

### Loops from a point

Right-click anywhere and choose **Loop from here** to ask for a round
trip of a given length - anything from 5 to 200 km. Moovelo tries eight
directions out of that point, and for each one hunts for the distance
out it needs to go to bring the whole ride back near your target, then
shows you up to three genuinely different candidates as coloured ghost
lines with distance, climbing and surface for each.

Two things worth knowing. The distance is approximate: these are real
roads, so a 60 km ask typically lands within a couple of percent rather
than exactly. And picking one is not a commitment - **Use this loop**
drops it in as ordinary waypoints you can drag, extend and re-plan like
anything else. It's a starting point, not a black box.

### Alternatives

For a straight A-to-B route, **Alternatives** asks the router for other
sensible ways between the same two points, drawn as ghost lines with how
much longer or hillier each is than what you have. Click one to adopt
it - and Undo puts back exactly the route you had, not a re-derived one.

The button is only available with exactly two waypoints. Alternatives
have no meaning once a route is pinned through via points, so it
disables itself (and says why) as soon as you add a third.

### Avoiding a road

Right-click **on the route line** for **Avoid this road**: the route
re-plans excluding that spot, and the avoided point stays on the map as
a marker with a chip you can remove to put it back. Up to 10 at a time.

Avoids are a planning tool, not part of the route: they're not saved
with it. The saved geometry already goes the way you shaped it, so
reloading a route gives you the line you kept - just without the list of
places you told it to skip. Editing that reloaded route re-plans without
those avoids, so re-add any that still matter.

## Reading the panel

Once a route exists, a panel appears below the map.

**Resizing and collapsing.** Drag the handle at the top edge of the panel
up or down to give it more or less room - useful when you want more map to
place waypoints on. Click the chevron on the handle (or press Enter with
the handle focused) to collapse the panel down to just its stats line, and
click again to bring the details back. Your chosen height is remembered in
your browser.

**Units.** The **km / mi** button in the top bar switches every distance,
elevation and speed the app displays between metric and imperial - the
stats bar, the elevation profile axes, place and POI distances, climbs,
alternates, and share pages all follow it. The choice is remembered in
your browser (it is a display setting, not part of your account, so it is
not shared with anyone you send a route to). Route files you export and
push to your Wahoo are unaffected - those carry their own data. One known
gap: the route assistant still talks in kilometres in its chat and
proposals, because it reasons over metric figures; the toggle only changes
what the app itself renders.

**Appearance.** The **Auto / Light / Dark** button in the top bar cycles
the app's theme. Auto is the default and follows your operating system's
light/dark setting; Light and Dark pin the app to one regardless of the
OS. Like units, this is remembered per-browser, not per-account, and it
never changes anything that gets exported or pushed to Wahoo.

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

**Waypoint list.** Every waypoint in route order, named by
reverse-geocoding when the place index is built and by position
("Start", "Via 2", "Finish") when it's not. Reorder rows by dragging
them or with the up/down buttons - the buttons are the touch and
keyboard path, since drag-and-drop fires nothing on a touchscreen - and
remove any waypoint from its row. Hovering a row highlights that marker
on the map.

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

### Calibrating flat-road speed from your rides

Once at least 5 of your rides have been [matched to a route you
planned](#planned-vs-actual), `/settings` shows a card: "Your rides
suggest 24.1 km/h (from 17 rides). Apply?" It is a suggestion, not a
change - nothing updates until you press **Apply**, and pressing it does
exactly what typing a number into the flat-road speed field and saving
would. The card is not shown at all below the 5-ride floor; a fit from one
or two rides is closer to noise than to a calibration.

The fit works per ride: for every matched ride, it asks "what flat-road
speed would have made the model predict this ride's actual moving time,
over the route it followed?" and combines the answers with the **median**
rather than the average, so a single odd ride - a long unplanned stop
folded into your moving time, a GPS dropout - cannot pull the number by
itself the way an average could.

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

## Isochrones

Right-click any point and choose **Isochrone from here** to see how far
you could get from it in a given time - 5 to 120 minutes - drawn as a
shaded area over the map. A marker stays at the origin, because the
overlay belongs to that point and not to whatever route you're
planning: it survives edits to the route on purpose, and goes away when
you press **Hide isochrone** or clear the route.

Read it as a shape, not a promise. The reach is computed by the routing
engine's own flat speed model, not your personal ride-time settings, so
it doesn't know about your FTP or the gradient penalty the stats bar
uses. The 120-minute ceiling is the routing engine's, not ours.

## The route assistant

Off unless someone configures it. An admin sets an endpoint and a model
on the **/admin** page (or via the `LLM_*` variables); until then there
is no chat panel and nothing calls out anywhere.

Once it is on, a small **Ask for a route** pill sits over the bottom-right
of the map. Click it to open the chat card; click the **×** to collapse it
back to a pill without losing the conversation - it stays open in the
background, not reset. The card floats over the map rather than sitting in
a panel below it, so you can drag it out of the way by its header to keep
working the map - panning, zooming, checking a proposal against what's
actually on the ground - while a turn is still running. It remembers where
you left it and whether it was open, and re-appears there next time.

Ask for what you want in plain words - "a 40 km gravel loop from here,
with water on it", "take me to Ivinghoe Beacon avoiding the A41" - and the
reply streams back as it works, with a line telling you which step it is
on. Long questions take a while, because each round trip to the model is
followed by real routing and searching; **Stop** ends a turn you have lost
patience with.

What comes back is an offer, not a change. A route it builds is drawn as
a dashed green line with a card showing its distance and climbing;
**Use this route** turns it into ordinary waypoints you can drag, and
**Discard** throws it away. Accepting is an ordinary edit, so **Undo**
takes you back. Edit your route while an offer is on screen and the
offer goes away, because it no longer describes what you are looking at.

Three things worth knowing:

- **It cannot make up a place or a distance.** It has no way to state a
  coordinate: it looks places up and then hands the result to the same
  routing engine the rest of the app uses. The figures on the card are
  read off the route that came back, not off anything the assistant
  wrote.
- **It only does bike routes.** Ask it for a poem, a recipe or help with
  your homework and it will decline and offer to plan a route instead.
  There is also a cap on how many requests one account can make in an
  hour, so it cannot quietly run up a bill at whatever endpoint the
  operator configured.
- **It only sees the planner.** Your saved library - names, tags, notes -
  is not available to it.
- **It needs the place index for anything by name.** Without the
  optional indexer (see [Place search and POIs](#place-search-and-pois))
  it will say so and offer to work from your current waypoints or the
  map centre instead.

Whatever you type goes to the endpoint that was configured, along with
place names and route figures it looks up on your behalf. Point it at
Ollama or LM Studio on your own hardware and nothing leaves your
network - see the [FAQ](faq.md#does-any-of-my-data-leave-my-network).

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
- **Details** - opens the route's own page (`/library/{id}`), showing its
  map, elevation profile and, once you have imported a ride that followed
  it, every one of those rides with actual moving time, distance and
  ascent set against what was planned. See
  [Planned vs actual](#planned-vs-actual) below.
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

## Activities

Rides you actually did live under **Activities**, separately from the
routes you planned. Import a GPX, TCX or FIT file straight off a head
unit and it lands there, with its date, distance, moving time and ascent.
Dropping files anywhere on this page - including a Strava export .zip -
imports them here rather than treating them as a route to plan, which is
what dropping a file elsewhere in the app does.

The distinction is deliberate. A route is a plan: it has a preset, it can
be re-routed, it can be pushed to your head unit. An activity is a
record. It is stored exactly as it was recorded - no map matching, no
elevation backfill, no re-routing - because a picture of where you rode
should show where you rode, wandering GPS and all. There is no edit
button for the same reason.

A few details worth knowing:

- **Moving time** excludes standing still and excludes gaps left by a
  paused recording, so a long cafe stop does not inflate it.
- **A file with no timestamps** still imports. It is dated by when you
  imported it and labelled as such, rather than showing a blank date.
- **Elevation** comes from the file itself. Your own barometer is a
  better record of the ride than a lookup would be.
- The same 20 MB and 100,000-point limits apply as to route import.

### Planned vs actual

Click a ride's name to open its own page (`/activities/{id}`): the
recorded trace on the map, its elevation profile, and - when it has been
matched to a saved route - that route's name (linking to its own page),
your actual moving time, distance and ascent set against what the route
predicted or planned. The predicted time is the same rider-settings-aware
model shown everywhere else in the app, not a separate estimate for this
page.

Matching happens automatically on import (see below), but you know your
own ride better than any geometry ever will. A dropdown on the ride page
lets you pick a different route, or clear the match entirely - either
choice is remembered, and Moovelo will not try to auto-match that ride
again unless you clear it back.

The same comparison appears the other way round, on the route's own page
(`/library/{id}` - see [Saving and the library](#saving-and-the-library)):
every ride that has been matched to it, each linking back to that ride's
own page.

Enough matched rides also feed a flat-road speed suggestion at
`/settings` - see
[Calibrating flat-road speed from your rides](#calibrating-flat-road-speed-from-your-rides).

### Importing a Strava export

Upload the zip Strava emails you when you request your archive, and every
ride in it lands in one go - the whole thing is read on a worker, with a
progress line rather than a spinner, because hundreds of files take
minutes.

Runs, swims and walks are skipped and counted. Re-uploading a later
archive adds only the rides you did since, so keeping the history current
costs nothing. A file that will not parse costs that one ride and says
which, rather than failing the import.

The archive itself is capped at 500 MB, which comfortably covers a full
Strava export. Only a handful of archives can be queued at once - if
you've just uploaded several and the import worker hasn't caught up yet,
a further upload is turned away with a message asking you to try again in
a few minutes, rather than piling up unprocessed in memory.

There is no Strava *sync*, and that is deliberate - see
[the FAQ](faq.md#why-is-there-no-strava-sync) for the clause numbers.

Moovelo can tell a *course* file from an *activity* file, because both
formats say which they are - so a course exported from Moovelo and
re-imported comes back as a route, not as a ride you never did.

### Your personal heatmap

Once you have imported at least one ride, a **Heatmap** button appears
next to the map's basemap and cycle-routes controls. It draws every ride
you have imported as a faint line, so roads you have ridden more than
once show up darker where the lines overlap - the same idea as the
heatmaps in other cycling apps, but built entirely from your own imports
and never leaving your own instance.

It is your data only. Nobody else's rides ever appear on it, and it is
off until you import at least one ride - there is nothing to draw before
then, so the button stays hidden rather than sitting there doing nothing.

### Cycle-network coverage

The Activities page reports how much of the signed cycle network you have
actually ridden, split by tier: national (NCN), regional, local and
international routes each get their own percentage. The heatmap above
shows you *where* you have been; this puts a number on it.

**What the percentage means.** The area is a box around your own rides,
not the whole country. The denominator is the length of every signed way
that actually falls *inside* that box, not a way's whole length the
moment it merely comes near - a way is clipped to the box before it's
measured, so a route that only brushes the edge of your box with a long
way elsewhere isn't counted as if all of it were there (one cross-Channel
ferry link was measured inflating a nearby box's figure almost 4x before
this was fixed). The numerator is the ones your rides have been matched
onto. Rides are matched to real OSM ways, not measured by proximity, so
riding the carriageway does not credit you for the cycleway running
alongside it.

A way carrying two routes of the same tier counts once. A way carrying an
NCN route *and* a local one counts towards both, because it genuinely is
part of both networks - the tiers are reported separately and never added
together, so nothing is double counted.

**A way counts once you have ridden any of it.** Coverage is measured in
ways touched, not metres pedalled: ride 10 m of a 500 m lane and the whole
lane counts as ridden. That is deliberate - it answers "have I been down
this road", which is the question a coverage map is for - but it does mean
the ridden figure is not your distance cycled and will usually exceed it.
A 2.7 km test ride reported 3.9 km ridden, which is correct for what is
being measured and surprising if you expect a total.

Coverage needs two things that ordinary route planning does not:

- **A place index that includes route members.** If you built your index
  before this feature existed, coverage says so rather than reporting 0%,
  which would read as "you have ridden nothing". Re-run the indexer - see
  [docs/data.md](data.md).
- **Your rides matched to ways.** New imports are matched as they arrive.
  Rides imported earlier are not, so the card offers a **Match older rides**
  button that works through them in the background. It is a button rather
  than something that happens on its own, because matching hundreds of
  rides is minutes of routing and should not start because you opened a
  page.

A ride that cannot be matched - it left your map extract, or followed
paths the routing graph does not have - is kept exactly as it was. It just
does not contribute to coverage.

### All-roads coverage

Alongside the signed-network percentage, the same card reports how much of
*every* bikeable road near you have ridden - not just the National
Cycle Network and its regional and local relatives, but ordinary streets,
tracks, paths and bridleways too. Grouped by road type ("residential
streets", "footways", "tracks", ...) rather than network tier, since roads
have no equivalent of NCN/RCN/LCN to group by, and sorted with whichever
you have ridden most of first.

The two percentages answer different questions. The signed-network one
tells you how much of a curated, named network you have covered. This one
is blunter and wider: it counts a quiet residential street the same as a
gravel bridleway, and it is the honest answer to "how much of the roads
around here have I actually ridden". Footways, paths, bridleways and
tracks are all counted - people ride them - but motorways and a handful of
other classes a bicycle cannot legally or physically use are not. Steps are
excluded on the same test: you carry a bike up them rather than ride it, so
counting them would make your coverage read lower than it really is.

The ways-touched rule above applies here too, and bites harder, because
ordinary streets are longer than signed route segments.

It needs the same two things the signed-network percentage does: a place
index that includes every bikeable way (an index built before this
shipped says so rather than reporting 0% - see
[docs/data.md](data.md#the-place-index-optional)), and your rides matched
to OSM ways, which the same **Match older rides** button backfills for
both percentages in one pass.

### Your climb log

A **Climbs** card on the Activities page lists every hill you have
recognisably climbed, deduplicated: ride the same hill on five different
occasions and it is one entry with a count of 5, not five separate lines.
Each entry shows the climb's category (HC down to 4, the same
categorisation as [Climb detection](#reading-the-panel) on an individual
route), its length and gain, and how many times you have ridden it.

Deduplication is geometric - it recognises the same hill by where it
starts and roughly how long it is, not by name (OpenStreetMap does not
name most hills) - so the tolerances are deliberately generous rather than
exact. They are provisional and expected to be tuned as this sees more
real riding history; if two rides up what is obviously the same hill ever
show as separate entries, or two different hills merge into one, that is
exactly the kind of thing worth reporting.

The per-ascent time shown for each ride up a climb is an estimate, not a
recording: Moovelo does not keep a timestamp for every point along a ride,
only the ride's total moving time, so a climb's time is that total spread
over the climb's share of the ride's distance. It assumes your average
speed on the climb matched your average speed for the whole ride, which is
never quite true - climbs are slower than the flat and the descents - so
treat it as a reasonable guess rather than a stopwatch split.

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

## Installing as an app

Moovelo is a progressive web app: you can install it to your home screen
or desktop and launch it in its own window, without a browser address
bar. It still needs a network connection to plan and save - installing
does not make routing work offline - but it launches faster and feels
like a native app. Installing requires HTTPS, which staging and prod
already have; a plain `http://` dev instance will not offer it.

- **Android (Chrome)**: open the site, then the browser menu -> **Install
  app** (or **Add to Home screen**).
- **iOS/iPadOS (Safari)**: the Share button -> **Add to Home Screen**.
  iOS has no automatic install prompt, so this manual step is the only
  way; the app name and icon come from the same manifest.
- **Desktop (Chrome/Edge)**: an install icon appears at the right of the
  address bar, or use the browser menu -> **Install Moovelo**.

The app updates itself: each deploy ships under a new version, and the
next time you open it online the new version replaces the old one - a
cached copy is only ever used as an offline fallback, never in place of a
newer one that is reachable.

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

Everything above is read-only - that configuration is still set through
environment variables and a restart. The one exception is the route
assistant, below.

### Configuring the route assistant

The assistant is optional and off until you point it at a model. You can
set it entirely from the admin page, or with `LLM_BASE_URL`, `LLM_MODEL`
and friends if you'd rather keep your install declarative. The page wins
where both are set, field by field, so you can override just the model
and leave the endpoint to the environment.

What's on the page:

- **Endpoint** - OpenRouter, or any OpenAI-compatible URL. Point it at
  Ollama or LM Studio on your own machine and nothing leaves your
  network.
- **API key** - optional; a local endpoint usually needs none. Once
  saved it's never shown again, only reported as stored, and it's kept
  in the database in plain text.
- **Model** - **must support tool calling.** A model that can't call
  tools will chat happily and never plan a route, so **Browse** lists
  only capable models, with prices.
- **Routing** (OpenRouter only) - which provider serves your request.
  This is worth setting: the same model can be served by providers an
  order of magnitude apart in price, and letting the gateway pick
  measured over twice the cost of asking for the cheapest.
- **Maximum input price** - a hard ceiling in dollars per million
  tokens. The only setting that actually bounds what a request can cost.
- **Preferred providers** - a preference, not a restriction. The gateway
  still falls back elsewhere rather than failing a reply, which is
  deliberate: some providers reject tool calls partway through a
  conversation, and a strict pin turns that into a failed answer instead
  of a slower one.

**Test** runs one real completion and tells you three things: that the
endpoint answers, how long it took, and whether the model actually
called a tool. That last one is the useful part - a model that answers
in prose looks perfectly healthy and is useless for planning, so the
test reports it as a failure.

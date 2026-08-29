/**
 * The trips pages.
 *
 * Both the list and a single trip load this. The list only needs the shared
 * formatting; everything below the first section runs on a trip page and
 * exits quietly on the list, which is why `window.TRIP` is checked once and
 * not defended against thereafter.
 *
 * The one real behaviour is the review queue. Assigning a transaction changes
 * every total on the page, so the endpoint returns the recomputed summary and
 * the page rewrites its figures in place. A reload per decision was the
 * alternative, and a queue of forty candidates is exactly where that becomes
 * unusable.
 */
(function () {
  "use strict";

  const { categoryColor, formatCurrency, formatDay } = window.Theme;

  // --- Shared rendering ------------------------------------------------------

  /**
   * Render every server-set amount in the page's number format.
   *
   * Separate from theme.js's `data-money` because most figures here are costs
   * rather than movements: a trip that cost 900 € is not "+900 €" in green.
   * Only an element that asks for it with `data-tone="sign"` is coloured.
   */
  function renderAmounts(root) {
    root.querySelectorAll("[data-amount]").forEach((node) => {
      const value = Number(node.dataset.amount);
      if (Number.isNaN(value)) return;
      node.textContent = formatCurrency(value);
      if (node.dataset.tone === "sign") {
        node.classList.toggle("is-positive", value > 0);
        node.classList.toggle("is-negative", value < 0);
      }
    });
  }

  function paint(root) {
    root.querySelectorAll("[data-category]").forEach((node) => {
      node.style.background = categoryColor(node.dataset.category);
    });
    // The tag carries its category on its edge rather than as a fill, so a row
    // stays readable while still being tied to a band of the mix.
    root.querySelectorAll("[data-edge]").forEach((node) => {
      node.style.borderColor = categoryColor(node.dataset.edge);
    });
    root.querySelectorAll("[data-day]").forEach((node) => {
      node.textContent = formatDay(node.dataset.day);
    });
  }

  renderAmounts(document);
  paint(document);

  // --- Detected trips awaiting confirmation ---------------------------------
  //
  // Lives above the single-trip code because it belongs to the list page,
  // which has no `window.TRIP` at all.

  /** Refuse or reinstate one detected stretch. */
  async function decideProposal(card, action) {
    const anchor = card.dataset.proposal;
    card.classList.add("is-saving");
    const response = await fetch(`/api/trips/proposals/${anchor}/${action}`, {
      method: "POST",
    });
    card.classList.remove("is-saving");
    if (!response.ok) {
      card.classList.add("is-error");
      return;
    }
    // A refusal moves the card into the refused list and a reinstatement
    // moves it back, and both change the counts in the panel head. Rather
    // than rebuild two lists in place, the page reloads — this is a decision
    // made a handful of times, not a queue.
    window.location.reload();
  }

  document.addEventListener("click", (event) => {
    const dismiss = event.target.closest(".found-dismiss");
    if (dismiss) {
      decideProposal(dismiss.closest("[data-proposal]"), "dismiss");
      return;
    }
    const restore = event.target.closest(".found-restore");
    if (restore) decideProposal(restore.closest("[data-proposal]"), "restore");
  });

  // --- The map ---------------------------------------------------------------
  //
  // Everywhere the trips went, drawn from coastlines kept in the repository
  // rather than fetched from a tile service. A map service would be told, one
  // square at a time, which parts of the world this reader's bank statements
  // are about, which is a lot to give away for a coordinate that has not
  // moved in three hundred years. `app/places.py` resolves the destinations;
  // `scripts/build_world_data.py` makes the outlines.
  //
  // The projection is Equal Earth (Šavrič, Patterson & Jenny, 2018): equal
  // area, so a fortnight in Kenya is not dwarfed by a weekend in Norway the
  // way Mercator would have it, and unlike the other equal-area projections
  // it does not stretch the tropics into ribbons. Coastlines and dots are
  // projected by the same function here, in the browser, because a map whose
  // land and whose points disagreed by a degree would be worse than no map.

  const EQUAL_EARTH = [1.340264, -0.081106, 0.000893, 0.003796];
  const SIN_60 = Math.sqrt(3) / 2;

  /** Equal Earth, in radians of the map's own units, north upwards. */
  function project(longitude, latitude) {
    const [a1, a2, a3, a4] = EQUAL_EARTH;
    const lambda = (longitude * Math.PI) / 180;
    const theta = Math.asin(SIN_60 * Math.sin((latitude * Math.PI) / 180));
    const t2 = theta * theta;
    const t6 = t2 * t2 * t2;
    return [
      (lambda * Math.cos(theta)) /
        (SIN_60 * (a1 + 3 * a2 * t2 + t6 * (7 * a3 + 9 * a4 * t2))),
      -theta * (a1 + a2 * t2 + t6 * (a3 + a4 * t2)),
    ];
  }

  // The drawing is done in a fixed thousand-unit square rather than in the
  // projection's own units, so that a radius, a hairline and a font size can
  // be written as the numbers they will look like. The SVG scales to fit.
  const MAP_WIDTH = 1000;
  const MAP_RATIO = 2.5;
  const MAP_HEIGHT = MAP_WIDTH / MAP_RATIO;

  // How far north and south the map goes. Below the southern edge is
  // Antarctica, which the outline data leaves out; above the northern one is
  // the part of the Arctic that only ever holds ice.
  const MAP_NORTH = 83;
  const MAP_SOUTH = -57;

  // The frame closes in on where the trips actually were — a reader whose
  // holidays are all in Europe should not be given an ocean — but never past
  // this many degrees of longitude, because a map of four countries stops
  // being a map of the world and the coastlines are not drawn finely enough
  // to survive it.
  const MIN_SPAN_DEGREES = 150;

  /** Turn a ring of longitude/latitude pairs into an SVG path. */
  function ringPath(flat, place) {
    let path = "";
    for (let index = 0; index < flat.length; index += 2) {
      const [x, y] = place(flat[index], flat[index + 1]);
      path += `${index ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`;
    }
    return `${path}Z`;
  }

  /**
   * The window on the world, in projected units, at the page's aspect ratio.
   *
   * Grown from the points outwards rather than shrunk from the world inwards:
   * the interesting thing about the map is the dots, and the amount of ocean
   * around them is only a consequence of where they are.
   */
  function frameFor(points) {
    const world = {
      left: project(-180, 0)[0],
      right: project(180, 0)[0],
      top: project(0, MAP_NORTH)[1],
      bottom: project(0, MAP_SOUTH)[1],
    };
    const floor = project(MIN_SPAN_DEGREES / 2, 0)[0] * 2;

    let left = world.right;
    let right = world.left;
    let top = world.bottom;
    let bottom = world.top;
    points.forEach((point) => {
      const [x, y] = project(point.lon, point.lat);
      left = Math.min(left, x);
      right = Math.max(right, x);
      top = Math.min(top, y);
      bottom = Math.max(bottom, y);
    });

    // A tenth of the span again on every side, so no dot sits on the frame.
    const margin = Math.max((right - left) * 0.1, (bottom - top) * 0.1, 0.05);
    left -= margin;
    right += margin;
    top -= margin;
    bottom += margin;

    let width = Math.max(right - left, (bottom - top) * MAP_RATIO, floor);
    width = Math.min(width, world.right - world.left);
    let height = width / MAP_RATIO;
    if (height > world.bottom - world.top) {
      height = world.bottom - world.top;
      width = height * MAP_RATIO;
    }

    // Centred on the points, then pushed back inside the world's edges, so
    // that closing in on Iceland does not leave half the frame off the map.
    const clamp = (value, low, high) => Math.min(Math.max(value, low), high);
    const x = clamp(
      (left + right) / 2 - width / 2,
      world.left,
      world.right - width
    );
    const y = clamp(
      (top + bottom) / 2 - height / 2,
      world.top,
      world.bottom - height
    );
    return { x, y, width, height };
  }

  /**
   * Where the labels go: beside the biggest dots, and nowhere else.
   *
   * Greedy, largest spend first, and a label that would land on one already
   * placed is simply not drawn. Every name is in the popup and on a card
   * below, so a label is a convenience rather than the only way to read a
   * dot, and half a dozen legible ones beat forty overlapping.
   */
  function placeLabels(marks) {
    // Seeded with the dots themselves, so a label never lands on one — the
    // nearest dot to a label is usually not the dot it belongs to.
    const taken = marks.map((mark) => ({
      x: mark.x - mark.radius,
      y: mark.y - mark.radius,
      width: mark.radius * 2,
      height: mark.radius * 2,
    }));
    marks.forEach((mark) => {
      const width = mark.point.place.length * 5.4 + 8;
      const boxes = [
        { x: mark.x + mark.radius + 5, y: mark.y - 6, side: 1 },
        { x: mark.x - mark.radius - 5 - width, y: mark.y - 6, side: -1 },
      ];
      for (const box of boxes) {
        if (box.x < 2 || box.x + width > MAP_WIDTH - 2) continue;
        const clash = taken.some(
          (other) =>
            box.x < other.x + other.width &&
            box.x + width > other.x &&
            box.y < other.y + other.height &&
            box.y + 12 > other.y
        );
        if (clash) continue;
        taken.push({ x: box.x, y: box.y, width, height: 12 });
        mark.label = box.side;
        return;
      }
    });
  }

  /** Draw the map, once the outlines have arrived. */
  function drawAtlas(root, atlas, world) {
    const svg = root.querySelector(".atlas-map");
    const frame = frameFor(atlas.points);
    const scale = MAP_WIDTH / frame.width;
    const place = (longitude, latitude) => {
      const [x, y] = project(longitude, latitude);
      return [(x - frame.x) * scale, (y - frame.y) * scale];
    };

    const visited = new Set(atlas.countries);
    const land = world.countries
      .map((country) => {
        const paths = country.rings
          .map((ring) => ringPath(ring, place))
          .join("");
        const been = visited.has(country.id) ? " is-visited" : "";
        return `<path class="atlas-land${been}" d="${paths}" />`;
      })
      .join("");

    // Area with the amount, not radius: a trip that cost four times another
    // should look four times the trip, and a reader reads a circle by how
    // much of it there is.
    const biggest = Math.max(...atlas.points.map((point) => point.net), 1);
    const marks = atlas.points.map((point) => {
      const [x, y] = place(point.lon, point.lat);
      const share = Math.sqrt(Math.max(point.net, 0) / biggest);
      return { point, x, y, radius: 3.2 + share * 6.5, label: 0 };
    });
    placeLabels(marks);

    const dots = marks
      .map((mark, index) => {
        const label = mark.label
          ? `<text class="atlas-label" x="${(
              mark.x +
              mark.label * (mark.radius + 5)
            ).toFixed(1)}" y="${(mark.y + 3.5).toFixed(1)}" text-anchor="${
              mark.label > 0 ? "start" : "end"
            }">${escapeHtml(mark.point.place)}</text>`
          : "";
        const trips = mark.point.trips.length;
        const title = `${mark.point.place}, ${trips} ${
          trips === 1 ? "trip" : "trips"
        }`;
        return `<g class="atlas-mark" data-mark="${index}" tabindex="0"
                   role="button" aria-label="${escapeHtml(title)}">
                  <circle class="atlas-halo" cx="${mark.x.toFixed(
                    1
                  )}" cy="${mark.y.toFixed(1)}" r="${(
          mark.radius + 6
        ).toFixed(1)}" />
                  <circle class="atlas-dot" cx="${mark.x.toFixed(
                    1
                  )}" cy="${mark.y.toFixed(1)}" r="${mark.radius.toFixed(
          1
        )}" />
                 </g>${label}`;
      })
      .join("");

    svg.setAttribute("viewBox", `0 0 ${MAP_WIDTH} ${MAP_HEIGHT}`);
    svg.innerHTML = `<g class="atlas-lands">${land}</g><g class="atlas-marks">${dots}</g>`;
    return marks;
  }

  function escapeHtml(text) {
    const box = document.createElement("span");
    box.textContent = text;
    return box.innerHTML;
  }

  /**
   * The card that opens off a dot.
   *
   * A dot can carry several trips — three weekends in the same city are one
   * place — so the card is a list of links rather than the dot itself being
   * one. It follows the pointer off the dot and onto itself, because the
   * links inside it have to be reachable.
   */
  function wireAtlas(root, marks) {
    const popup = root.querySelector(".atlas-pop");
    let pinned = null;
    let closing = null;

    function show(index) {
      const mark = marks[index];
      if (!mark) return;
      const rows = mark.point.trips
        .map(
          (trip) =>
            `<a class="atlas-trip" href="/trips/${trip.id}">
               <span class="atlas-trip-name">${escapeHtml(trip.name)}</span>
               <span class="atlas-trip-when">${formatDay(
                 trip.start_date
               )} – ${formatDay(trip.end_date)} ${trip.end_date.slice(
              0,
              4
            )}</span>
               <span class="atlas-trip-sum num">${formatCurrency(
                 trip.net
               )}</span>
             </a>`
        )
        .join("");
      const where = mark.point.country && mark.point.country !== mark.point.place
          ? `<span class="atlas-pop-where">${escapeHtml(
              mark.point.country
            )}</span>`
          : "";
      popup.innerHTML = `<div class="atlas-pop-head">
          <span class="atlas-pop-place">${escapeHtml(mark.point.place)}</span>
          ${where}
        </div>${rows}`;
      // Positioned as a fraction of the map rather than in pixels, so it
      // stays on its dot when the map is resized under it.
      popup.style.left = `${(mark.x / MAP_WIDTH) * 100}%`;
      popup.style.top = `${(mark.y / MAP_HEIGHT) * 100}%`;
      popup.classList.toggle("is-left", mark.x > MAP_WIDTH * 0.6);
      popup.classList.toggle("is-below", mark.y < MAP_HEIGHT * 0.35);
      popup.hidden = false;
    }

    function hide() {
      if (pinned !== null) return;
      popup.hidden = true;
    }

    root.addEventListener("pointerover", (event) => {
      const mark = event.target.closest("[data-mark]");
      window.clearTimeout(closing);
      if (mark) {
        show(Number(mark.dataset.mark));
      } else if (!event.target.closest(".atlas-pop")) {
        closing = window.setTimeout(hide, 120);
      }
    });
    root.addEventListener("pointerleave", () => {
      window.clearTimeout(closing);
      closing = window.setTimeout(hide, 120);
    });
    root.addEventListener("focusin", (event) => {
      const mark = event.target.closest("[data-mark]");
      if (mark) show(Number(mark.dataset.mark));
    });

    // A dot with one trip behind it goes straight there; a dot with several
    // has nowhere single to go, so the click pins the card open instead.
    root.addEventListener("click", (event) => {
      const node = event.target.closest("[data-mark]");
      if (!node) return;
      const mark = marks[Number(node.dataset.mark)];
      if (mark.point.trips.length === 1) {
        window.location.href = `/trips/${mark.point.trips[0].id}`;
        return;
      }
      pinned = pinned === node ? null : node;
      show(Number(node.dataset.mark));
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      pinned = null;
      hide();
    });
  }

  const atlasRoot = document.getElementById("atlas");
  if (atlasRoot) {
    const atlas = JSON.parse(
      document.getElementById("atlas-data").textContent
    );
    fetch(atlasRoot.dataset.world)
      .then((response) => response.json())
      .then((world) => {
        wireAtlas(atlasRoot, drawAtlas(atlasRoot, atlas, world));
        atlasRoot.classList.add("is-drawn");
      })
      .catch(() => {
        // The trips are all listed as cards below, so a map that cannot be
        // drawn is a missing decoration rather than missing information.
        atlasRoot.remove();
      });
  }

  // --- When the trips were ---------------------------------------------------
  //
  // One row per year, twelve months across, rather than one long axis running
  // from the first trip to the last. A single axis answers only which year a
  // trip was in; rows of the same twelve months line up underneath one another
  // and answer the more interesting question as well — that the holidays are
  // all in July, or that one year had none until December.
  //
  // A trip that crosses New Year is drawn as a piece in each year's row. It is
  // one trip and two segments, which is what it was.

  const MONTHS = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
  ];

  /** A YYYY-MM-DD key as a local date, avoiding the UTC drift of Date.parse. */
  function dateOf(iso) {
    const [year, month, dayOfMonth] = String(iso).split("-").map(Number);
    return new Date(year, month - 1, dayOfMonth);
  }

  /**
   * How far through a given year a date falls, from 0 to 1.
   *
   * Measured against that year's real length rather than a flat 365, so that
   * a leap year's row still lines up month for month with the others.
   *
   * The year is passed in rather than read off the date, because the end of a
   * row is the first instant of the next year — midnight on the 1st of
   * January — and asking that date which year it is in gives the wrong one
   * and a piece of negative width.
   */
  function throughYear(date, year) {
    const opens = new Date(year, 0, 1);
    const closes = new Date(year + 1, 0, 1);
    return (date - opens) / (closes - opens);
  }

  /**
   * One trip cut into a piece per year it touches.
   *
   * Returns:
   *   Objects of `{year, from, to, trip}` where from and to are fractions of
   *   that year. The end date is inclusive — a trip that ends on the 9th was
   *   away on the 9th — so the piece runs to the start of the following day.
   */
  function yearPieces(trip) {
    const starts = dateOf(trip.start_date);
    const ends = dateOf(trip.end_date);
    const pieces = [];
    for (let year = starts.getFullYear(); year <= ends.getFullYear(); year++) {
      const opens = new Date(year, 0, 1);
      const closes = new Date(year, 11, 31);
      const from = starts > opens ? starts : opens;
      const to = ends < closes ? ends : closes;
      pieces.push({
        year,
        from: throughYear(from, year),
        to: throughYear(
          new Date(to.getFullYear(), to.getMonth(), to.getDate() + 1),
          year
        ),
        trip,
      });
    }
    return pieces;
  }

  /** The twelve month divisions, drawn once per row and once as the axis. */
  function monthTicks(labelled) {
    const bar = document.createElement("div");
    bar.className = labelled ? "seasons-axis" : "seasons-grid";
    MONTHS.forEach((month, index) => {
      const tick = document.createElement("span");
      tick.className = "seasons-tick";
      tick.style.left = `${(index / 12) * 100}%`;
      if (labelled) tick.textContent = month;
      bar.appendChild(tick);
    });
    return bar;
  }

  /** Draw one year's row: its label, its months, and the trips inside it. */
  function seasonRow(year, pieces) {
    const row = document.createElement("div");
    row.className = "seasons-year";

    const label = document.createElement("span");
    label.className = "seasons-label";
    label.textContent = year;

    const track = document.createElement("div");
    track.className = "seasons-track";
    track.appendChild(monthTicks(false));

    pieces.sort((first, second) => first.from - second.from);
    pieces.forEach((piece, index) => {
      const block = document.createElement("a");
      block.className = "seasons-block";
      block.href = `/trips/${piece.trip.id}`;
      block.style.left = `${piece.from * 100}%`;
      block.style.width = `${(piece.to - piece.from) * 100}%`;
      // Coloured by what the trip mostly was, the same reading the mix bar on
      // its card gives. A trip with nothing assigned to it yet has no mix to
      // take a colour from and stays neutral.
      block.style.background = piece.trip.top_category
        ? categoryColor(piece.trip.top_category)
        : "var(--rule-strong)";
      // Both years when the trip crosses one, for the same reason the cards
      // print both: "28 Dec to 03 Jan 2024" names the wrong year for the 28th.
      const began = piece.trip.start_date.slice(0, 4);
      const ended = piece.trip.end_date.slice(0, 4);
      block.setAttribute(
        "aria-label",
        `${piece.trip.name}, ${formatDay(piece.trip.start_date)} ` +
          `${began === ended ? "" : `${began} `}to ` +
          `${formatDay(piece.trip.end_date)} ${ended}`
      );

      // The name sits after the block and runs until the next trip starts,
      // clipped rather than wrapped: the row is a year and must stay one line.
      // Hovering lifts it clear, so a clipped name is still readable.
      //
      // A link for the pointer, which needs the larger target, and nothing at
      // all for the keyboard: the block beside it already goes to the same
      // trip and already carries the whole name, so a second stop here would
      // read every trip out twice.
      const name = document.createElement("a");
      name.className = "seasons-name";
      name.href = block.href;
      name.tabIndex = -1;
      name.setAttribute("aria-hidden", "true");
      name.textContent = piece.trip.name;
      name.style.left = `${piece.to * 100}%`;
      const next = pieces[index + 1];
      name.style.maxWidth = `${((next ? next.from : 1) - piece.to) * 100}%`;

      track.append(block, name);
    });

    row.append(label, track);
    return row;
  }

  /**
   * The figure, newest year at the top.
   *
   * Newest first because the trip cards below are in that order, and a reader
   * who finds a trip in one and looks for it in the other should not have to
   * reverse the list in their head.
   */
  function drawSeasons(root, trips) {
    const byYear = new Map();
    trips.forEach((trip) => {
      yearPieces(trip).forEach((piece) => {
        if (!byYear.has(piece.year)) byYear.set(piece.year, []);
        byYear.get(piece.year).push(piece);
      });
    });

    const axis = document.createElement("div");
    axis.className = "seasons-head";
    axis.append(document.createElement("span"), monthTicks(true));
    root.appendChild(axis);

    // Every year between the first and the last, including the ones with no
    // trips in them: a year nobody went anywhere is a fact about the years,
    // and a list that skipped it would read as if it had never happened.
    const years = [...byYear.keys()];
    for (let year = Math.max(...years); year >= Math.min(...years); year--) {
      root.appendChild(seasonRow(year, byYear.get(year) || []));
    }
  }

  const seasonsRoot = document.getElementById("seasons");
  if (seasonsRoot) {
    drawSeasons(
      seasonsRoot,
      JSON.parse(document.getElementById("trips-data").textContent)
    );
  }

  const trip = window.TRIP;
  if (!trip) return;

  // Unfurl the mix once, from here rather than from the template. A
  // server-rendered `animate` class leaves the bands parked at their opening
  // keyframe — drawn at full width but scaled to nothing, so every band is
  // invisible to a click.
  const bar = document.querySelector(".spectrum-bar");
  if (bar && !window.Theme.prefersReducedMotion()) {
    requestAnimationFrame(() => bar.classList.add("animate"));
  }

  // --- Totals ----------------------------------------------------------------

  /** Rewrite the page's figures from a summary the server just recomputed. */
  function applySummary(summary) {
    const figures = {
      "trip-net": summary.net,
      "trip-spend": summary.spend,
      "trip-refunds": summary.refunds,
      "trip-per-day": summary.per_day,
    };
    Object.entries(figures).forEach(([id, value]) => {
      const node = document.getElementById(id);
      if (node) node.textContent = formatCurrency(value);
    });
    // A green zero reads as money received, so the colour follows the figure.
    const refunds = document.getElementById("trip-refunds");
    if (refunds) refunds.classList.toggle("is-positive", summary.refunds > 0);
    // The assigned count lives on the list it counts, and goes stale the
    // moment a decision is made if it is not written here too.
    const count = document.getElementById("trip-count");
    if (count) count.textContent = `${summary.count} assigned`;
  }

  // --- Deciding --------------------------------------------------------------

  /**
   * Say something about one transaction, and show what it did to the totals.
   *
   * The row is left in place rather than removed. A decision made in a queue
   * is often made too fast, and a row that vanishes takes the undo with it.
   */
  async function decide(row, decision, share) {
    const body = new FormData();
    body.append("transaction_id", row.dataset.transaction);
    if (decision) body.append("decision", decision);
    if (share) body.append("share", String(share));

    row.classList.add("is-saving");
    const response = await fetch(`/api/trips/${trip.id}/assign`, {
      method: "POST",
      body,
    });
    row.classList.remove("is-saving");

    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      note(row, detail.detail || "That could not be saved.");
      return;
    }

    const result = await response.json();
    applySummary(result.summary);
    mark(row, decision, result.moved_from, share);
  }

  /** Say what happened to one row, in the row. */
  function mark(row, decision, movedFrom, share) {
    row.classList.toggle("is-included", decision === "included");
    row.classList.toggle("is-dismissed", decision === "excluded");
    // A part share is the one case where "counted" alone would be a half
    // truth, so the row says how much of the payment it is counting.
    const part = share && share < 1 ? Math.round(share * 100) : null;
    if (movedFrom) {
      note(row, `Moved here from ${movedFrom}, which no longer counts it.`);
    } else if (decision === "included" && part) {
      note(row, `Counting ${part}% of this payment. Reload to see it in the mix.`);
    } else if (decision === "included") {
      note(row, "Counted in full. Reload to see it in the mix.");
    } else if (decision === "excluded") {
      note(row, "Not the trip. It will not be proposed again.");
    } else {
      note(row, "Back in the queue.");
    }
  }

  function note(row, text) {
    let node = row.querySelector(".trip-note");
    if (!node) {
      node = document.createElement("span");
      node.className = "trip-note";
      row.appendChild(node);
    }
    node.textContent = text;
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-decision]");
    if (!button) return;
    const row = button.closest("[data-transaction]");
    if (!row) return;
    const share = row.querySelector(".trip-share");
    decide(
      row,
      button.dataset.decision,
      share ? Number(share.value) / 100 : undefined
    );
  });

  // The share box stays out of the way until somebody asks for it: almost
  // every payment belongs to its trip whole.
  document.addEventListener("click", (event) => {
    const opener = event.target.closest(".trip-split-open");
    if (!opener) return;
    const field = opener.parentElement.querySelector(".trip-split");
    if (!field) return;
    opener.hidden = true;
    field.hidden = false;
    const input = field.querySelector(".trip-share");
    input.focus();
    input.select();
  });

  // A share is a correction to an existing assignment, so it re-sends the
  // inclusion rather than being its own endpoint.
  document.addEventListener("change", (event) => {
    const field = event.target.closest(".trip-share");
    if (!field) return;
    const percent = Math.min(100, Math.max(1, Number(field.value) || 100));
    field.value = percent;
    decide(field.closest("[data-transaction]"), "included", percent / 100);
  });

  // --- Splitting the whole trip ---------------------------------------------

  /**
   * Share the whole journey between a number of people.
   *
   * A reload rather than a patch: the split scales the mix, the phase
   * figures, the budget bar and every headline at once, and this is a
   * decision made once per trip rather than an entry in a queue.
   */
  async function setSplit(ways) {
    const body = new FormData();
    body.append("ways", String(ways));
    const response = await fetch(`/api/trips/${trip.id}/split`, {
      method: "POST",
      body,
    });
    if (response.ok) window.location.reload();
  }

  document.querySelectorAll(".split-toggle button").forEach((button) => {
    button.addEventListener("click", () => setSplit(Number(button.dataset.ways)));
  });

  const splitOther = document.querySelector(".split-other");
  if (splitOther) {
    splitOther.addEventListener("change", () => {
      const ways = Math.min(50, Math.max(1, Number(splitOther.value) || 1));
      setSplit(ways);
    });
  }

  // --- Adding what is selected ----------------------------------------------

  const addButton = document.getElementById("trip-add-selected");
  const selectedCount = document.getElementById("trip-selected-count");

  function picked() {
    // :checked matches a disabled box too, and a disabled one belongs to
    // another trip — collecting it would move it out from under that trip.
    return [
      ...document.querySelectorAll(".trip-pick input:checked:not(:disabled)"),
    ].map((box) => box.closest("[data-transaction]").dataset.transaction);
  }

  /** Keep the button honest about how many rows it is about to add. */
  function refreshCount() {
    if (!addButton) return;
    const count = picked().length;
    selectedCount.textContent = count;
    addButton.disabled = count === 0;
  }

  document.addEventListener("change", (event) => {
    if (event.target.closest(".trip-pick")) refreshCount();
  });
  refreshCount();

  if (addButton) {
    addButton.addEventListener("click", async () => {
      const ids = picked();
      if (!ids.length) return;
      addButton.disabled = true;
      const body = new FormData();
      ids.forEach((id) => body.append("transaction_id", id));
      const response = await fetch(`/api/trips/${trip.id}/assign-selected`, {
        method: "POST",
        body,
      });
      // Everything moves at once here — the mix, the timeline, the phase
      // figures, the assigned list — so this reloads rather than patching.
      if (response.ok) window.location.reload();
      else {
        addButton.disabled = false;
        addButton.textContent = "That did not work";
      }
    });
  }

  // Deleting a trip throws away every decision made about it, and the button
  // sits under a disclosure somebody opened to change a date.
  const deleteForm = document.getElementById("trip-delete");
  if (deleteForm) {
    const button = deleteForm.querySelector("button");
    let armed = false;
    deleteForm.addEventListener("submit", (event) => {
      if (armed) return;
      event.preventDefault();
      armed = true;
      button.textContent = "Really delete — every decision goes with it";
      button.classList.add("is-armed");
    });
  }

  // --- Searching for what the windows missed ---------------------------------

  const search = document.getElementById("trip-search");
  const results = document.getElementById("trip-search-results");

  /** One search hit, as a row that can be assigned like any candidate. */
  function resultRow(item) {
    const row = document.createElement("div");
    row.className = "trip-queue-row";
    row.dataset.transaction = item.id;
    if (item.assigned) row.classList.add("is-included");

    const date = document.createElement("span");
    date.className = "detail-date";
    date.textContent = item.date;

    const name = document.createElement("span");
    name.className = "detail-name";
    name.textContent = item.merchant;
    name.title = item.booking_text;

    const tag = document.createElement("span");
    tag.className = "detail-tag";
    tag.textContent = item.minor;
    tag.style.borderColor = categoryColor(item.minor);
    name.appendChild(tag);

    if (item.claimed_by) {
      const claim = document.createElement("span");
      claim.className = "trip-claim";
      claim.textContent = `already counted in ${item.claimed_by}`;
      name.appendChild(claim);
    }

    const money = document.createElement("span");
    money.className = "detail-amount num";
    money.textContent = formatCurrency(item.amount);
    money.classList.add(item.amount < 0 ? "is-negative" : "is-positive");

    const actions = document.createElement("span");
    actions.className = "trip-queue-actions";
    const add = document.createElement("button");
    add.type = "button";
    add.className = "ghost-button is-small";
    add.dataset.decision = item.assigned ? "" : "included";
    add.textContent = item.assigned ? "Remove" : "Add to this trip";
    actions.appendChild(add);

    row.append(date, name, money, actions);
    return row;
  }

  let searchTimer = null;
  if (search) {
    search.addEventListener("input", () => {
      window.clearTimeout(searchTimer);
      const query = search.value.trim();
      if (query.length < 2) {
        results.replaceChildren();
        return;
      }
      // Every search reads the whole history server-side, so it waits for a
      // pause in the typing rather than firing per keystroke.
      searchTimer = window.setTimeout(async () => {
        const response = await fetch(
          `/api/trips/${trip.id}/search?q=${encodeURIComponent(query)}`
        );
        if (!response.ok) return;
        const payload = await response.json();
        results.replaceChildren();
        if (!payload.results.length) {
          const empty = document.createElement("p");
          empty.className = "panel-text";
          empty.textContent = "Nothing matches that.";
          results.appendChild(empty);
          return;
        }
        payload.results.forEach((item) => results.appendChild(resultRow(item)));
      }, 250);
    });
  }

  // --- When it was paid ------------------------------------------------------
  //
  // The page's one drawn figure, and the only argument for the whole feature
  // that cannot be made with a number: a holiday is a few days, and the money
  // for it is spread across months either side. Every payment sits at its real
  // date, so the picture is the evidence.

  /** Parse a YYYY-MM-DD key as a local date, avoiding UTC drift. */
  function day(iso) {
    const [y, m, d] = String(iso).split("-").map(Number);
    return new Date(y, m - 1, d);
  }

  const DAY_MS = 86400000;

  function drawTimeline() {
    const plot = document.getElementById("timeline-plot");
    if (!plot || !trip.assigned.length) return;

    const rows = trip.assigned.map((item) => ({
      ...item,
      at: day(item.date),
      value: item.amount * item.share,
    }));

    const start = day(trip.start);
    // The window is inclusive of its last day, so it closes at the following
    // midnight — otherwise a one-day trip would have no width at all.
    const end = new Date(day(trip.end).getTime() + DAY_MS);

    const first = Math.min(start.getTime(), ...rows.map((r) => r.at.getTime()));
    const last = Math.max(end.getTime(), ...rows.map((r) => r.at.getTime()));
    // A little air either side so a mark on the first or last day is not
    // clipped in half by the edge of the plot.
    const pad = Math.max((last - first) * 0.04, DAY_MS);
    const from = first - pad;
    const span = last - first + pad * 2;
    const at = (time) => ((time - from) / span) * 100;

    // Month marks. Without them the two months between a booking and a
    // holiday are an empty box; with them the gap reads as the time it
    // actually was, which is the whole point of drawing this.
    const grid = document.getElementById("timeline-grid");
    grid.replaceChildren();
    const months = (last - first) / DAY_MS / 30.44;
    const step = months > 30 ? 12 : months > 14 ? 3 : 1;
    const cursor = new Date(from);
    cursor.setDate(1);
    cursor.setHours(0, 0, 0, 0);
    // Step to the first boundary that falls inside the plot.
    while (cursor.getMonth() % step !== 0) cursor.setMonth(cursor.getMonth() + 1);
    while (cursor.getTime() < from + span) {
      if (cursor.getTime() > from) {
        const tick = document.createElement("span");
        tick.className = "timeline-tick";
        tick.style.left = `${at(cursor.getTime())}%`;
        tick.textContent = cursor.toLocaleDateString("en-GB", {
          month: "short",
          ...(step === 12 ? { year: "numeric" } : {}),
        });
        grid.appendChild(tick);
      }
      cursor.setMonth(cursor.getMonth() + step);
    }

    const window_ = document.getElementById("timeline-window");
    window_.style.left = `${at(start.getTime())}%`;
    window_.style.width = `${at(end.getTime()) - at(start.getTime())}%`;

    // The zero line goes where this trip's own mix of spending and money back
    // needs it; see Theme.baselineFor.
    const spent = rows.filter((r) => r.value < 0).map((r) => -r.value);
    const back = rows.filter((r) => r.value > 0).map((r) => r.value);
    const biggest = Math.max(...spent, ...back, 0);
    const { baseline, scale } = window.Theme.baselineFor(
      Math.max(...spent, 0),
      Math.max(...back, 0)
    );
    plot.style.setProperty("--baseline", `${baseline}%`);

    const height = (value) =>
      biggest ? Math.max(3, Math.sqrt(Math.abs(value) / biggest) * scale) : 3;

    const marks = document.getElementById("timeline-marks");
    marks.replaceChildren();
    rows
      .slice()
      .sort((a, b) => Math.abs(b.value) - Math.abs(a.value))
      .forEach((row) => {
        const mark = document.createElement("span");
        // Money back hangs below the line rather than rising from it: the
        // direction of the payment is the direction of the mark.
        mark.className = `timeline-mark${row.value > 0 ? " is-back" : ""}`;
        mark.style.left = `${at(row.at.getTime())}%`;
        mark.style.height = `${height(row.value)}%`;
        mark.style.background = categoryColor(row.minor);
        mark.title = `${row.date} · ${row.merchant} · ${formatCurrency(row.value)}`;
        marks.appendChild(mark);
      });

    // The three totals survive, but bound to where they happened instead of
    // sitting in three identical boxes.
    const regions = [
      ["booked", "before you left", from, start.getTime()],
      ["during", "away", start.getTime(), end.getTime()],
      ["settled", "after you got back", end.getTime(), from + span],
    ];
    const legend = document.getElementById("timeline-legend");
    legend.replaceChildren();
    regions.forEach(([key, label, a, b]) => {
      const total = trip.byPhase[key];
      if (!total) return;
      const item = document.createElement("span");
      item.className = "timeline-region";
      item.style.left = `${(at(a) + at(b)) / 2}%`;
      item.innerHTML =
        '<span class="timeline-region-value"></span>' +
        '<span class="timeline-region-label"></span>';
      item.querySelector(".timeline-region-value").textContent =
        formatCurrency(total);
      item.querySelector(".timeline-region-label").textContent = label;
      legend.appendChild(item);
    });

    const caption = document.getElementById("timeline-span");
    if (caption) {
      const months = Math.round((last - first) / DAY_MS / 30.44);
      const days = Math.round((end - start) / DAY_MS);
      caption.textContent =
        months >= 2
          ? `${days} days away, paid for across ${months} months`
          : `${days} days away`;
    }
  }

  // A figure that cannot draw must not take the page with it. drawTimeline
  // runs before the popup and queue handlers are bound, so an exception here
  // used to leave every button on the page dead — which is a far worse
  // failure than a missing chart.
  try {
    drawTimeline();
  } catch (error) {
    console.error("[trips] the timeline could not be drawn", error);
  }

  // --- The mix popup ---------------------------------------------------------

  const modal = document.getElementById("slice-modal");
  let returnFocus = null;

  function closeModal() {
    modal.classList.remove("show");
    modal.hidden = true;
    document.body.classList.remove("modal-open");
    if (returnFocus && returnFocus.focus) returnFocus.focus();
    returnFocus = null;
  }

  /** The payments behind one band of the mix. */
  function openSlice(minor) {
    const rows = trip.assigned.filter((item) => item.minor === minor);
    if (!rows.length) return;

    document.getElementById("slice-swatch").style.background =
      categoryColor(minor);
    document.getElementById("slice-heading").textContent = minor;

    // Stated as a cost, the way the band's own key states it. The rows below
    // keep their real signs, because a refund inside a category is a fact
    // about that category and hiding it would make the rows disagree with the
    // total they add up to.
    const total = rows.reduce((sum, item) => sum + item.amount * item.share, 0);
    document.getElementById("slice-summary").textContent = `${
      rows.length
    } ${rows.length === 1 ? "payment" : "payments"} · ${formatCurrency(
      Math.abs(total)
    )}`;

    const list = document.getElementById("slice-rows");
    list.replaceChildren();
    rows.forEach((item) => {
      const row = document.createElement("div");
      row.className = "detail-row";

      const date = document.createElement("span");
      date.className = "detail-date";
      date.textContent = item.date;

      const name = document.createElement("span");
      name.className = "detail-name";
      name.textContent = item.merchant;
      // The bank's own line is what the merchant was derived from, so it stays
      // within reach without taking a second row of space.
      name.title = item.booking_text;

      const money = document.createElement("span");
      money.className = "detail-amount num";
      money.textContent = formatCurrency(item.amount * item.share);
      money.classList.add(item.amount < 0 ? "is-negative" : "is-positive");

      row.append(date, name, money);
      list.appendChild(row);
    });

    returnFocus = document.activeElement;
    modal.hidden = false;
    document.body.classList.add("modal-open");
    requestAnimationFrame(() => modal.classList.add("show"));
    document.getElementById("slice-close").focus();
  }

  document.addEventListener("click", (event) => {
    const band = event.target.closest("[data-slice]");
    if (band) openSlice(band.dataset.slice);
  });

  document.getElementById("slice-close").addEventListener("click", closeModal);
  modal.addEventListener("click", (event) => {
    if (event.target === modal) closeModal();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !modal.hidden) closeModal();
  });
})();

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

  const acceptAll = document.getElementById("trip-accept-all");
  if (acceptAll) {
    acceptAll.addEventListener("click", async () => {
      acceptAll.disabled = true;
      acceptAll.textContent = "Adding…";
      const response = await fetch(`/api/trips/${trip.id}/accept-suggested`, {
        method: "POST",
      });
      // Everything moves at once here — the mix, the phases, the assigned
      // list — so this is the one action that reloads rather than patching.
      if (response.ok) window.location.reload();
      else {
        acceptAll.disabled = false;
        acceptAll.textContent = "That did not work";
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

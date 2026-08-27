/**
 * The recurring page.
 *
 * Everything the page draws is already in the server's payload, so nothing
 * here fetches: the schedule popup renders from `window.RECURRING.items`
 * rather than asking for the charges a second time. The page's only real
 * behaviour is the per-month/per-year toggle and that popup.
 */
(function () {
  "use strict";

  const { categoryColor, formatCurrency, formatDay, formatMonthLong } =
    window.Theme;

  const items = new Map(
    (window.RECURRING?.items || []).map((item) => [item.id, item])
  );

  // The drift the detector allows inside one schedule. A charge beyond it is
  // marked in the popup, because it is the reason a median is shown here and
  // not a sum.
  const AMOUNT_TOLERANCE = 0.15;

  /**
   * Day, month and year.
   *
   * The shared `formatDay` leaves the year off, which is right for a list
   * already scoped to one month — but a schedule's charges run for years, and
   * "19 Dec" against "19 Dec" a year earlier is the one thing this popup must
   * not blur.
   */
  function formatFullDate(iso) {
    const [year, month, day] = String(iso).split("-");
    return new Date(Number(year), Number(month) - 1, Number(day))
      .toLocaleDateString("en-GB", {
        day: "2-digit",
        month: "short",
        year: "numeric",
      });
  }

  /** What detection concluded, and what the user can say instead. */
  const VERDICT = {
    active: "this is still running",
    due: "the next charge is overdue",
    ended: "this has stopped",
  };
  const DECISION = {
    active: "still running",
    ended: "ended",
    dismissed: "not a recurring payment",
  };

  /** How a period is said in prose, where the bare adjective reads wrong. */
  const CADENCE = {
    monthly: "every month",
    quarterly: "every three months",
    "half-yearly": "every six months",
    yearly: "every year",
  };

  // --- Static rendering ------------------------------------------------------

  // Colour every swatch and proportional bar from the shared palette, so the
  // page reads with the same key as every chart.
  document.querySelectorAll("[data-category]").forEach((node) => {
    node.style.background = categoryColor(node.dataset.category);
  });

  // Dates come from the server as ISO so they can be compared; they are shown
  // the way the rest of the app shows a day.
  document.querySelectorAll("[data-day]").forEach((node) => {
    node.textContent = formatDay(node.dataset.day);
  });

  // --- Per month / per year --------------------------------------------------

  const unitNodes = document.querySelectorAll("[data-monthly][data-annual]");

  /**
   * Show every normalised amount in one unit.
   *
   * The annual figure is the persuasive one — 14,99 € a month reads as
   * nothing and 179,88 € a year does not — but the monthly one is what the
   * schedules can be compared in, so the page can be read either way.
   */
  function applyUnit(unit) {
    unitNodes.forEach((node) => {
      const amount = Number(node.dataset[unit === "annual" ? "annual" : "monthly"]);
      if (Number.isNaN(amount)) return;
      node.textContent = formatCurrency(amount);
      node.classList.toggle("is-positive", amount > 0);
      node.classList.toggle("is-negative", amount < 0);
    });
    document.querySelectorAll(".unit-toggle button").forEach((button) => {
      const active = button.dataset.unit === unit;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
  }

  document.querySelectorAll(".unit-toggle button").forEach((button) => {
    button.addEventListener("click", () => applyUnit(button.dataset.unit));
  });
  applyUnit("monthly");

  // --- The schedule popup ----------------------------------------------------

  const modal = document.getElementById("schedule-modal");
  let returnFocus = null;
  // Which schedule the popup is currently showing, so the override buttons
  // know what they are deciding about.
  let openId = null;

  function openModal() {
    if (!modal.hidden) return;
    returnFocus = document.activeElement;
    modal.hidden = false;
    document.body.classList.add("modal-open");
    requestAnimationFrame(() => modal.classList.add("show"));
    document.getElementById("schedule-close").focus();
  }

  function closeModal() {
    openId = null;
    modal.classList.remove("show");
    modal.hidden = true;
    document.body.classList.remove("modal-open");
    if (returnFocus && returnFocus.focus) returnFocus.focus();
    returnFocus = null;
  }

  /** The one-line summary under the heading: what this schedule is. */
  function summarize(item) {
    const parts = [
      `${formatCurrency(item.amount)} ${CADENCE[item.period] || item.period}`,
      `${item.occurrences} charges since ${formatFullDate(item.first_seen)}`,
    ];
    if (item.status === "ended") {
      parts.push(`stopped after ${formatFullDate(item.last_seen)}`);
    } else if (item.days_away < 0) {
      parts.push(`expected ${-item.days_away} days ago`);
    } else {
      parts.push(`next ${formatFullDate(item.next_expected)}`);
    }
    return parts.join(" · ");
  }

  /**
   * The notes that are worth a line of their own.
   *
   * A price change is the one thing on this page the median deliberately
   * hides — detection tolerates fifteen percent of drift so a price rise stays
   * one schedule rather than splitting into two — so it is stated outright.
   */
  function flagsFor(item) {
    const flags = [];
    if (item.price_change) {
      const from = formatCurrency(item.price_change.from);
      const to = formatCurrency(item.price_change.to);
      const when = formatMonthLong(item.price_change.changed_on);
      flags.push(`Was ${from} until ${when}, now ${to}.`);
    }
    if (item.status === "due") {
      flags.push(
        "The next charge has not arrived. Either it is simply late, or the " +
          "schedule has ended."
      );
    }
    if (item.status === "ended") {
      flags.push(
        `Nothing since ${formatFullDate(item.last_seen)}, ` +
          "which is more than one interval ago."
      );
    }
    return flags;
  }

  function renderCharges(item) {
    const list = document.getElementById("schedule-charges");
    list.replaceChildren();

    // Newest first, and each row against the schedule's own median, so a
    // month that broke the pattern is visible without reading the numbers.
    item.charges.forEach((charge) => {
      const row = document.createElement("div");
      row.className = "detail-row";
      row.innerHTML =
        '<span class="detail-date"></span>' +
        '<span class="detail-name"></span>' +
        '<span class="detail-amount num"></span>';

      row.querySelector(".detail-date").textContent = formatFullDate(charge.date);

      const name = row.querySelector(".detail-name");
      name.textContent = charge.merchant;
      // The bank's own line is what the merchant was derived from, so it is
      // kept within reach without taking a second row of space.
      name.title = charge.booking_text;

      const amount = row.querySelector(".detail-amount");
      amount.textContent = formatCurrency(charge.amount);
      amount.classList.add(charge.amount < 0 ? "is-negative" : "is-positive");
      // Off every price this schedule has charged by more than the detector's
      // own tolerance: worth marking, because it is why the card shows a
      // price and not a sum. Measured against the old price as well as the
      // current one, so a schedule that was raised does not report every
      // charge from before the raise as an anomaly.
      const levels = item.price_change
        ? [item.price_change.from, item.price_change.to]
        : [item.amount];
      const drift = Math.min(
        ...levels.map((level) =>
          level ? Math.abs(charge.amount - level) / Math.abs(level) : 0
        )
      );
      if (drift > AMOUNT_TOLERANCE) amount.classList.add("is-outlier");

      list.appendChild(row);
    });
  }

  /**
   * The strip that shows what detection decided and lets it be overruled.
   *
   * Detection reads a pattern out of dates and amounts, so it can only guess
   * at two things: whether the pattern is a schedule at all, and whether it
   * is still running. Both answers are shown, because a decision is only
   * worth reviewing next to the one it overruled.
   */
  function renderOverride(item) {
    const verdict = document.getElementById("schedule-verdict");
    verdict.textContent = VERDICT[item.detected_status] || item.detected_status;
    verdict.classList.toggle("is-overruled", Boolean(item.override));

    document.querySelectorAll(".override-actions button").forEach((button) => {
      const decision = button.dataset.decision;
      if (!decision) {
        // Nothing to undo until a decision has been made.
        button.hidden = !item.override;
        return;
      }
      const chosen = decision === item.override;
      button.classList.toggle("is-active", chosen);
      button.setAttribute("aria-pressed", String(chosen));
      button.textContent = chosen ? `✓ ${DECISION[decision]}` : BUTTON_TEXT[decision];
    });
  }

  /** The buttons' resting labels, captured before any of them is rewritten. */
  const BUTTON_TEXT = {};
  document.querySelectorAll(".override-actions button").forEach((button) => {
    if (button.dataset.decision) {
      BUTTON_TEXT[button.dataset.decision] = button.textContent.trim();
    }
  });

  /**
   * Record a decision, or hand the schedule back to the detector.
   *
   * The page is server-rendered and a decision moves a schedule between
   * sections, changes the totals and can remove it altogether, so it reloads
   * rather than trying to patch all of that in place.
   */
  async function decide(id, decision) {
    const body = new FormData();
    body.append("schedule_id", id);
    if (decision) body.append("decision", decision);

    const response = await fetch(
      decision ? "/api/recurring/override" : "/api/recurring/override/clear",
      { method: "POST", body }
    );
    if (!response.ok) {
      // Said in the popup rather than in a browser dialog: a modal alert
      // blocks the page and there is a perfectly good place to put a
      // sentence right here.
      const detail = await response.json().catch(() => ({}));
      const note = document.createElement("p");
      note.className = "schedule-modal-flag is-error";
      note.textContent = detail.detail || "That decision could not be saved.";
      const flags = document.getElementById("schedule-flags");
      flags.hidden = false;
      flags.appendChild(note);
      return;
    }
    window.location.reload();
  }

  document.querySelectorAll(".override-actions button").forEach((button) => {
    button.addEventListener("click", () => {
      if (openId) decide(openId, button.dataset.decision);
    });
  });

  function openSchedule(id) {
    const item = items.get(id);
    if (!item) return;

    const swatch = document.getElementById("schedule-swatch");
    swatch.style.background = categoryColor(item.minor);

    document.getElementById("schedule-heading").textContent = item.merchant;
    document.getElementById("schedule-summary").textContent = summarize(item);

    const flags = document.getElementById("schedule-flags");
    flags.replaceChildren();
    flagsFor(item).forEach((text) => {
      const note = document.createElement("p");
      note.className = "schedule-modal-flag";
      note.textContent = text;
      flags.appendChild(note);
    });
    flags.hidden = flags.childElementCount === 0;

    renderOverride(item);
    renderCharges(item);
    openId = id;
    openModal();
  }

  // One delegated listener: cards, strip stops and ended rows all carry the
  // same data-schedule, so they all open the same popup.
  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-schedule]");
    if (trigger) openSchedule(trigger.dataset.schedule);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !modal.hidden) {
      closeModal();
      return;
    }
    if (event.key !== "Enter" && event.key !== " ") return;
    const trigger = event.target.closest?.("[data-schedule]");
    if (!trigger) return;
    // Space scrolls the page otherwise, which is exactly the wrong answer to
    // somebody activating a card.
    event.preventDefault();
    openSchedule(trigger.dataset.schedule);
  });

  document.getElementById("schedule-close").addEventListener("click", closeModal);
  modal.addEventListener("click", (event) => {
    if (event.target === modal) closeModal();
  });
})();

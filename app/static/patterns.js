/**
 * The patterns page.
 *
 * Everything is already in the server's payload — the findings, the series
 * behind each one, and the transactions that prove them — so nothing here
 * fetches except the two writes that put a finding away and bring it back.
 * That is deliberate: a card makes a claim about the money, and evidence that
 * arrives in a second request is evidence that can be read after the claim it
 * was supposed to support.
 *
 * The script owns three things the template cannot: number formatting, which
 * has to match every other figure in the app; the sparklines and the two
 * figures, which are drawn rather than marked up; and the captions, which are
 * fragments joined with a separator rather than sentences.
 */
(function () {
  "use strict";

  const {
    categoryColor,
    formatCurrency,
    formatDay,
    formatMonth,
    formatMonthLong,
    formatPercent,
    token,
  } = window.Theme;

  const data = window.PATTERNS || {};
  const findings = new Map(
    [...(data.findings || []), ...(data.dismissed || [])].map((item) => [
      item.id,
      item,
    ])
  );
  const putAway = new Set((data.dismissed || []).map((item) => item.id));

  // How many months of the series a card's sparkline draws, and how many the
  // figure above shows. The figure is the whole recent history; a sparkline is
  // a shape, and past a couple of years it is a smear.
  const SPARK_POINTS = 24;
  const FIGURE_MONTHS = 36;

  const MONTH_NAMES = "JFMAMJJASOND".split("");

  /** What each kind of finding is measured in. */
  const PER_MONTH = new Set([
    "drift",
    "price",
    "lapsed",
    "commitment",
    "gone",
    "arrived",
    "cash",
    "mix",
    "savings",
  ]);

  /**
   * Which kinds get "last N months" in front of their caption.
   *
   * These are the findings measured over a window rather than dated to an
   * event, and without the prefix their card quietly implies its numbers are
   * about one month. The rest already carry their own dates — a schedule says
   * when it last collected, a merchant when it was last seen — and a month
   * name in front of those is a second date competing with the real one.
   */
  const OVER_A_WINDOW = new Set(["drift", "mix", "savings", "cash"]);

  function calendarMonth(number) {
    return new Date(2000, Number(number) - 1, 1).toLocaleDateString("en-GB", {
      month: "long",
    });
  }

  // --- Captions ---------------------------------------------------------------
  //
  // One line per card, in the app's voice: fragments joined with a separator,
  // never a sentence. Each kind knows the two or three numbers that make its
  // headline checkable — what it was, what it is, and what the page would
  // otherwise leave the reader guessing at.

  const CAPTIONS = {
    drift(facts) {
      const parts = [
        `${formatCurrency(facts.after)} vs ${formatCurrency(facts.before)}`,
      ];
      if (facts.count_before || facts.count_after) {
        parts.push(`${facts.count_after}× vs ${facts.count_before}× a month`);
      }
      // Which half of the move it was. The whole reason the split is computed
      // is that "more often" and "more each time" are different problems.
      const frequency = Math.abs(facts.from_frequency || 0);
      const ticket = Math.abs(facts.from_ticket || 0);
      if (frequency || ticket) {
        parts.push(
          frequency >= ticket * 2
            ? "mostly going more often"
            : ticket >= frequency * 2
              ? "mostly paying more each time"
              : "both, about evenly"
        );
      }
      return parts;
    },

    outlier(facts) {
      const parts = [
        `${formatCurrency(facts.spend)} vs ${formatCurrency(facts.typical)} typical`,
      ];
      // Said in words rather than in sigmas: the caption is uppercased by the
      // stylesheet, and an uppercased σ is a Σ, which means something else.
      if (facts.sigmas) {
        parts.push(`${Math.abs(facts.sigmas)}× the usual swing`);
      }
      const cause = facts.attribution;
      if (cause) {
        const label =
          cause.reason === "schedules" ? "yearly charges" : cause.label;
        parts.push(`${label} · ${formatCurrency(cause.amount)}`);
      } else {
        parts.push("no single cause");
      }
      return parts;
    },

    price(facts) {
      return [
        `${formatCurrency(Math.abs(facts.from))} → ${formatCurrency(
          Math.abs(facts.to)
        )} ${facts.period}`,
        `${formatCurrency(facts.annual)} a year`,
      ];
    },

    lapsed(facts) {
      return [
        `${formatCurrency(facts.amount)} ${facts.period}`,
        `last ${formatDay(facts.last_seen)}`,
        `due ${formatDay(facts.expected)}`,
        `${facts.occurrences}× before that`,
      ];
    },

    commitment(facts) {
      return [
        `${formatCurrency(facts.now)} now`,
        `${formatCurrency(facts.then)} in ${formatMonth(facts.since)}`,
        `${facts.count} schedules`,
      ];
    },

    seasonal(facts) {
      const parts = [calendarMonth(facts.calendar_month)];
      parts.push(
        `${formatCurrency(facts.typical)} vs ${formatCurrency(facts.usual)}`
      );
      if (facts.index) parts.push(`${facts.index}× a normal month`);
      parts.push(
        facts.months_away === 1
          ? "next month"
          : `in ${facts.months_away} months`
      );
      parts.push(`${facts.years} years seen`);
      return parts;
    },

    gone(facts) {
      return [
        `${facts.visits} visits`,
        `last ${formatDay(facts.last_seen)}`,
        `quiet ${facts.silent_days} days`,
        `usually every ${facts.typical_gap} days`,
      ];
    },

    arrived(facts) {
      return [
        `${facts.visits} visits since ${formatDay(facts.first_seen)}`,
        `${formatCurrency(facts.total)} so far`,
      ];
    },

    mix(facts) {
      return [
        `${facts.up} ${facts.up_points > 0 ? "+" : ""}${facts.up_points} pts`,
        `${facts.down} ${facts.down_points} pts`,
        `total held at ${formatCurrency(facts.total)}`,
      ];
    },

    savings(facts) {
      return [
        `${formatPercent(facts.after * 100)} vs ${formatPercent(
          facts.before * 100
        )} of income`,
        `${formatCurrency(facts.income)} in`,
        `${formatCurrency(facts.spend)} out`,
      ];
    },

    cash(facts) {
      return [
        `${formatCurrency(facts.after)} vs ${formatCurrency(facts.before)} a month`,
        `${facts.count_after}× vs ${facts.count_before}× a month`,
      ];
    },
  };

  function captionFor(item) {
    const build = CAPTIONS[item.kind];
    const parts = build ? build(item.facts || {}) : [];
    if (OVER_A_WINDOW.has(item.kind)) {
      parts.unshift(`last ${data.recent_months} months`);
    }
    return parts.filter(Boolean).join(" · ");
  }

  /**
   * What a card is called.
   *
   * Everything names itself except an unusual month, whose subject *is* a
   * month — and the server sends that as a sortable key rather than as
   * something to read.
   */
  function nameOf(item) {
    return item.kind === "outlier" ? formatMonthLong(item.month) : item.label;
  }

  // --- Static rendering -------------------------------------------------------

  document.querySelectorAll("[data-category]").forEach((node) => {
    node.style.background = categoryColor(node.dataset.category);
  });

  document.querySelectorAll("[data-headline]").forEach((node) => {
    const amount = Number(node.dataset.headline);
    if (Number.isNaN(amount)) return;
    const card = node.closest("[data-finding]");
    const item = card && findings.get(card.dataset.finding);
    const sign = amount > 0 ? "+" : "";
    const unit = item && PER_MONTH.has(item.kind) ? " a month" : "";
    node.textContent = `${sign}${formatCurrency(amount)}${unit}`;
    // More money leaving is the direction that costs something — except on
    // the one card that measures what was kept rather than what was spent,
    // where the same arithmetic sign means the opposite thing and colouring
    // it by the rule above painted a collapsing savings rate green.
    const costly = item && item.kind === "savings" ? amount < 0 : amount > 0;
    node.classList.toggle("is-negative", costly);
    node.classList.toggle("is-positive", !costly);
  });

  document.querySelectorAll("[data-finding] .finding-name").forEach((node) => {
    const card = node.closest("[data-finding]");
    const item = findings.get(card.dataset.finding);
    if (item) node.textContent = nameOf(item);
  });

  document.querySelectorAll("[data-caption]").forEach((node) => {
    const item = findings.get(node.dataset.caption);
    if (item) node.textContent = captionFor(item);
  });

  /**
   * A finding's series as a sparkline.
   *
   * Drawn against its own extent rather than a shared one: a card is about
   * the shape of one thing over time, and a scale borrowed from the largest
   * category on the page would flatten every other card to a straight line.
   * The zero line is drawn only when the series crosses it, so a series that
   * never goes negative is not squashed into the top half of the box.
   */
  function sparkline(node, item) {
    const points = (item.series || []).slice(-SPARK_POINTS);
    if (points.length < 2) return;

    const values = points.map((point) => point.value);
    const top = Math.max(...values, 0);
    const bottom = Math.min(...values, 0);
    const height = top - bottom || 1;
    const stroke = item.category ? categoryColor(item.category) : token("--paper", "#e8ecf1");

    const x = (index) => (index / (points.length - 1)) * 100;
    const y = (value) => 26 - ((value - bottom) / height) * 24;

    const line = values.map((value, index) => `${x(index)},${y(value)}`).join(" ");
    const zero = bottom < 0 && top > 0 ? y(0) : null;

    node.innerHTML = [
      '<svg viewBox="0 0 100 28" preserveAspectRatio="none" role="presentation">',
      zero === null
        ? ""
        : `<line x1="0" y1="${zero}" x2="100" y2="${zero}" class="spark-zero" />`,
      `<polyline points="${line}" fill="none" stroke="${stroke}" stroke-width="1.5"`,
      ' vector-effect="non-scaling-stroke" stroke-linejoin="round" />',
      `<circle cx="${x(points.length - 1)}" cy="${y(values[values.length - 1])}"`,
      ` r="2" fill="${stroke}" vector-effect="non-scaling-stroke" />`,
      "</svg>",
    ].join("");
  }

  document.querySelectorAll("[data-spark]").forEach((node) => {
    const item = findings.get(node.dataset.spark);
    if (item) sparkline(node, item);
  });

  // --- The month figure --------------------------------------------------------

  /**
   * What a month cost, and how much of it was decided before it started.
   *
   * Three series over one axis, because the gap between them is the point.
   * The bars are what left the account; the filled band under them is the
   * standing commitments carried at their monthly equivalent, so a yearly
   * premium sits in every month rather than in the one it happens to be
   * collected in; the line is what came in. Everything above the band and
   * below the line was a decision somebody could still have made differently.
   */
  function drawMonths() {
    const canvas = document.getElementById("months-chart");
    const series = (data.series || []).slice(-FIGURE_MONTHS);
    if (!canvas || series.length < 2) return;

    const rule = token("--rule", "#262e39");
    const paper = token("--paper", "#e8ecf1");
    const dim = token("--paper-dim", "#8d99a8");
    const faint = token("--paper-faint", "#616d7c");
    const positive = token("--pos", "#5bc98f");

    Chart.defaults.color = dim;
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.borderColor = rule;

    new Chart(canvas, {
      data: {
        labels: series.map((row) => row.month),
        datasets: [
          {
            type: "bar",
            label: "Spent",
            data: series.map((row) => row.spend),
            backgroundColor: "rgba(232, 236, 241, 0.16)",
            hoverBackgroundColor: "rgba(232, 236, 241, 0.3)",
            borderWidth: 0,
            order: 3,
          },
          {
            type: "line",
            label: "Committed",
            data: series.map((row) => row.committed),
            borderColor: faint,
            backgroundColor: "rgba(97, 109, 124, 0.35)",
            borderWidth: 1,
            borderDash: [4, 3],
            pointRadius: 0,
            pointHoverRadius: 3,
            fill: "origin",
            stepped: true,
            order: 2,
          },
          {
            type: "line",
            label: "Came in",
            data: series.map((row) => row.income),
            borderColor: positive,
            borderWidth: 1.5,
            pointRadius: 0,
            pointHoverRadius: 4,
            tension: 0.25,
            fill: false,
            order: 1,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: token("--ink-2", "#161b23"),
            borderColor: rule,
            borderWidth: 1,
            titleColor: paper,
            bodyColor: dim,
            padding: 10,
            displayColors: true,
            callbacks: {
              title: (items) => formatMonthLong(items[0].label),
              label: (item) =>
                `${item.dataset.label}: ${formatCurrency(item.parsed.y)}`,
            },
          },
        },
        scales: {
          x: {
            grid: { display: false },
            border: { color: rule },
            ticks: {
              maxRotation: 0,
              autoSkip: true,
              autoSkipPadding: 12,
              padding: 6,
              callback(value) {
                return formatMonth(this.getLabelForValue(value));
              },
            },
          },
          y: {
            beginAtZero: true,
            grid: { drawTicks: false, color: rule },
            border: { display: false },
            ticks: {
              padding: 8,
              maxTicksLimit: 6,
              callback: (value) => window.Theme.formatCurrencyCompact(value),
            },
          },
        },
      },
    });

    const caption = document.getElementById("months-caption");
    if (caption) {
      caption.textContent = `${series.length} complete months · to ${formatMonth(
        data.reference
      )}`;
    }

    const key = document.getElementById("months-key");
    if (key) {
      key.innerHTML = [
        ["Spent", "rgba(232, 236, 241, 0.5)"],
        ["Already committed", faint],
        ["Came in", positive],
      ]
        .map(
          ([label, colour]) =>
            `<span class="pattern-key-item"><i style="background:${colour}"></i>${label}</span>`
        )
        .join("");
    }
  }

  // --- The year profile --------------------------------------------------------

  /**
   * Each calendar month against a normal one.
   *
   * Drawn from a line at 1.0 rather than from the floor, because what the
   * profile says is "this month runs above or below normal" and a bar chart
   * rising from zero says only "December exists". Months with too few years
   * behind them are drawn faintly instead of being left out: an empty slot
   * would read as a month that costs nothing.
   */
  function drawYear() {
    const plot = document.getElementById("year-bars");
    const axis = document.getElementById("year-axis");
    const profile = data.seasonal || [];
    if (!plot || !profile.length) return;

    const widest = Math.max(
      ...profile.map((row) => Math.abs((row.index || 1) - 1)),
      0.25
    );

    plot.innerHTML = "";
    axis.innerHTML = "";
    profile.forEach((row) => {
      const index = row.index === null ? 1 : row.index;
      const offset = index - 1;
      const height = (Math.abs(offset) / widest) * 46;

      const slot = document.createElement("div");
      slot.className = "year-slot";
      const bar = document.createElement("span");
      bar.className = `year-bar${offset >= 0 ? " is-over" : " is-under"}${
        row.known ? "" : " is-thin"
      }`;
      bar.style.height = `${Math.max(height, 1)}%`;
      bar.title = `${calendarMonth(row.month)}: ${formatCurrency(row.typical)}`;
      slot.appendChild(bar);
      plot.appendChild(slot);

      const label = document.createElement("span");
      label.className = "year-tick";
      label.textContent = MONTH_NAMES[row.month - 1];
      axis.appendChild(label);
    });

    const caption = document.getElementById("year-caption");
    if (caption) {
      const known = profile.filter((row) => row.known).length;
      caption.textContent = `median month vs the year · ${known}/12 with ${
        data.min_season_years
      }+ years behind them`;
    }
  }

  // --- The popup ---------------------------------------------------------------

  const modal = document.getElementById("finding-modal");
  let open = null;

  function openModal(item) {
    open = item;
    document.getElementById("finding-heading").textContent = nameOf(item);
    const swatch = document.getElementById("finding-swatch");
    swatch.style.background = item.category
      ? categoryColor(item.category)
      : token("--paper-faint", "#616d7c");
    document.getElementById("finding-summary").textContent = captionFor(item);
    renderState();
    renderRows(item);

    modal.hidden = false;
    requestAnimationFrame(() => modal.classList.add("show"));
    document.body.classList.add("modal-open");
    document.getElementById("finding-close").focus();
  }

  function closeModal() {
    open = null;
    modal.classList.remove("show");
    document.body.classList.remove("modal-open");
    setTimeout(() => {
      modal.hidden = true;
    }, 150);
  }

  function renderState() {
    const state = document.getElementById("finding-state");
    const hidden = Boolean(open && putAway.has(open.id));
    state.textContent = hidden ? "put away" : "showing";
    state.classList.toggle("is-overruled", hidden);

    // Only one of the two is ever a thing to do, and marking the other as
    // chosen said the opposite of the state above it: a finding that is
    // showing is not a finding somebody brought back.
    modal.querySelectorAll(".override-actions button").forEach((button) => {
      const away = button.dataset.decision === "dismiss";
      button.classList.toggle("is-active", away && hidden);
      button.disabled = away === hidden;
    });
  }

  /**
   * The transactions that produced the claim, largest first.
   *
   * Built the same way the dashboard's drill-down builds its rows, down to
   * the category sitting under the merchant rather than beside it: three
   * columns and a fourth thing to say is what pushed the amount onto a line
   * of its own, and a list whose figures do not line up cannot be read as a
   * list of figures.
   */
  function renderRows(item) {
    const list = document.getElementById("finding-rows");
    list.innerHTML = "";

    (item.rows || []).forEach((row) => {
      const line = document.createElement("div");
      line.className = "detail-row";
      line.innerHTML =
        '<span class="detail-date"></span>' +
        '<span class="detail-name"></span>' +
        '<span class="detail-amount num"></span>';

      line.querySelector(".detail-date").textContent = formatDay(row.date);

      const name = line.querySelector(".detail-name");
      name.textContent = row.merchant;
      // The bank's own line is what the merchant was derived from, so it
      // stays within reach without taking a second row of space.
      name.title = row.booking_text;

      // Only worth saying when the card does not already say it: a card about
      // a category is a list of that category.
      if (row.minor && row.minor !== item.category) {
        const tag = document.createElement("span");
        tag.className = "detail-tag";
        tag.textContent = row.minor;
        tag.style.borderLeftColor = categoryColor(row.minor);
        name.appendChild(document.createElement("br"));
        name.appendChild(tag);
      }

      const amount = line.querySelector(".detail-amount");
      amount.textContent = formatCurrency(row.amount);
      amount.classList.add(row.amount < 0 ? "is-negative" : "is-positive");

      list.appendChild(line);
    });

    if (!list.children.length) {
      const empty = document.createElement("p");
      empty.className = "field-note";
      empty.textContent = "Nothing to list — this finding is about an absence.";
      list.appendChild(empty);
    }
  }

  // --- Putting a finding away --------------------------------------------------

  /**
   * Store what the user said about a finding.
   *
   * Posted as the finding's own three facts rather than as an id, because
   * that triple is what a dismissal is keyed on: findings are recomputed from
   * scratch on every load, and anything keyed on a position in a ranked list
   * would stop matching the moment another finding outranked it.
   */
  async function decide(item, decision) {
    const body = new FormData();
    body.append("kind", item.kind);
    body.append("subject", item.subject || "");
    body.append("month", item.month || "");

    const response = await fetch(`/api/patterns/${decision}`, {
      method: "POST",
      body,
    });
    if (!response.ok) return false;

    if (decision === "dismiss") putAway.add(item.id);
    else putAway.delete(item.id);
    return true;
  }

  function cardOf(item) {
    return document.querySelector(`[data-finding="${CSS.escape(item.id)}"]`);
  }

  async function putFindingAway(item) {
    if (!(await decide(item, "dismiss"))) return;
    const card = cardOf(item);
    if (card) card.classList.add("is-put-away");
    if (open && open.id === item.id) renderState();
  }

  async function bringFindingBack(item) {
    if (!(await decide(item, "restore"))) return;
    const card = cardOf(item);
    if (card) card.classList.remove("is-put-away");
    if (open && open.id === item.id) renderState();
  }

  // --- Wiring ------------------------------------------------------------------

  document.querySelectorAll("[data-put-away]").forEach((button) => {
    button.addEventListener("click", (event) => {
      // The button sits inside the card, and the card opens the popup.
      event.stopPropagation();
      const item = findings.get(button.dataset.putAway);
      if (!item) return;
      if (putAway.has(item.id)) bringFindingBack(item);
      else putFindingAway(item);
    });
  });

  document.querySelectorAll("[data-finding]").forEach((card) => {
    const item = findings.get(card.dataset.finding);
    if (!item) return;
    if (putAway.has(item.id)) card.classList.add("is-put-away");
    card.addEventListener("click", () => openModal(item));
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openModal(item);
      }
    });
  });

  modal.querySelectorAll(".override-actions button").forEach((button) => {
    button.addEventListener("click", () => {
      if (!open) return;
      if (button.dataset.decision === "dismiss") putFindingAway(open);
      else bringFindingBack(open);
    });
  });

  document.getElementById("finding-close").addEventListener("click", closeModal);
  modal.addEventListener("click", (event) => {
    if (event.target === modal) closeModal();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !modal.hidden) closeModal();
  });

  drawMonths();
  drawYear();
})();

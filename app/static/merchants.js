/**
 * The merchants page.
 *
 * Everything is in the server's payload — the ranking, each merchant's whole
 * monthly history and the payments behind it — so nothing here fetches. The
 * page stores no decision, which is the difference between this and every
 * other detected view in the app: concentration is a property of the history
 * rather than a claim about it.
 *
 * The script owns what the template cannot: number formatting, which has to
 * match every other figure in the app; the bars, whose widths are shares
 * rather than markup; the curve; and the captions, which are fragments joined
 * with a separator rather than sentences.
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

  const data = window.MERCHANTS || {};
  const merchants = new Map((data.merchants || []).map((item) => [item.key, item]));

  // A ticket has to move by this much before the caption mentions it. Below
  // that the figure is the basket changing rather than the price.
  const TICKET_SHIFT = 0.05;

  // How few visits make a median ticket worth comparing at all.
  const MIN_COMPARABLE_VISITS = 3;

  function percent(share) {
    return formatPercent((Number(share) || 0) * 100);
  }

  /**
   * Whether a merchant's ticket moved enough to be worth a fragment.
   *
   * Both windows need enough visits behind them: a median over two payments
   * is not a price, and reporting one as a change invents a trend out of
   * which two things happened to be bought.
   */
  function ticketShift(item) {
    if (!data.comparable) return null;
    if (!item.previous_ticket || !item.ticket) return null;
    if ((item.previous_visits || 0) < MIN_COMPARABLE_VISITS) return null;
    if (item.visits < MIN_COMPARABLE_VISITS) return null;
    const delta = item.ticket - item.previous_ticket;
    if (Math.abs(delta) < item.previous_ticket * TICKET_SHIFT) return null;
    return delta;
  }

  /** One row's line: what a visit costs, how often, and whether that moved. */
  function captionFor(item) {
    const parts = [item.category, `${formatCurrency(item.ticket)} a visit`];
    parts.push(`${item.visits}× in the year`);
    const delta = ticketShift(item);
    if (delta !== null) {
      const sign = delta > 0 ? "+" : "−";
      parts.push(`${sign}${formatCurrency(Math.abs(delta))} a visit year on year`);
    }
    return parts.join(" · ");
  }

  // --- Static rendering -------------------------------------------------------

  document.querySelectorAll("[data-category]").forEach((node) => {
    node.style.background = categoryColor(node.dataset.category);
  });

  document.querySelectorAll("[data-share]").forEach((node) => {
    node.textContent = percent(node.dataset.share);
  });

  document.querySelectorAll("[data-percent]").forEach((node) => {
    node.textContent = percent(node.dataset.percent);
  });

  // A merchant's bar is drawn against the largest share on the page rather
  // than against the whole ledger: at 7% of everything, the leader's bar would
  // otherwise be a sliver and the ranking invisible. The category bars below
  // are shares of their own category and keep their absolute scale.
  const widest = Math.max(...(data.merchants || []).map((item) => item.share), 0.0001);
  document.querySelectorAll(".merchant-bar span").forEach((node) => {
    const share = Number(node.dataset.width) || 0;
    node.style.width = `${Math.max((share / widest) * 100, 1.5)}%`;
  });

  document.querySelectorAll(".owned-bar span, .owned-bar i").forEach((node) => {
    node.style.width = `${(Number(node.dataset.width) || 0) * 100}%`;
  });

  document.querySelectorAll("[data-caption]").forEach((node) => {
    const item = merchants.get(node.dataset.caption);
    if (item) node.textContent = captionFor(item);
  });

  const rankedCaption = document.getElementById("ranked-caption");
  if (rankedCaption && data.months && data.months.length) {
    rankedCaption.textContent = [
      `top ${(data.merchants || []).length} of ${data.count}`,
      `${formatMonth(data.first)} – ${formatMonth(data.last)}`,
    ].join(" · ");
  }

  // --- The concentration curve -------------------------------------------------

  /**
   * Cumulative share of the spending against merchant rank.
   *
   * Drawn with the line a ledger paying every merchant equally would follow,
   * because the curve on its own has no scale — a steep one and a flat one
   * are the same picture until there is something to be steep against. How
   * far the curve bulges above the straight line is the concentration.
   *
   * The x axis is a rank rather than a name: at three hundred merchants the
   * names are unreadable, and the question the figure answers is how many, not
   * which.
   */
  function drawCurve() {
    const canvas = document.getElementById("curve-chart");
    const curve = data.curve || [];
    if (!canvas || curve.length < 2) return;

    const rule = token("--rule", "#262e39");
    const paper = token("--paper", "#e8ecf1");
    const dim = token("--paper-dim", "#8d99a8");
    const faint = token("--paper-faint", "#616d7c");
    const reach = curve[curve.length - 1];

    new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels: curve.map((_, index) => index + 1),
        datasets: [
          {
            label: "Cumulative share",
            data: curve.map((value) => value * 100),
            borderColor: paper,
            borderWidth: 2,
            fill: false,
            pointRadius: 0,
            tension: 0.1,
          },
          {
            label: "If every merchant were paid the same",
            data: curve.map((_, index) => ((index + 1) / curve.length) * reach * 100),
            borderColor: faint,
            borderWidth: 1,
            borderDash: [4, 4],
            fill: false,
            pointRadius: 0,
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
            callbacks: {
              title: (items) => `${items[0].label} merchants`,
              label: (item) =>
                `${item.dataset.label}: ${formatPercent(item.parsed.y)}`,
            },
          },
        },
        scales: {
          x: {
            grid: { display: false },
            border: { color: rule },
            ticks: { maxRotation: 0, autoSkip: true, autoSkipPadding: 16, padding: 6 },
          },
          y: {
            beginAtZero: true,
            grid: { drawTicks: false, color: rule },
            border: { display: false },
            ticks: {
              padding: 8,
              maxTicksLimit: 6,
              callback: (value) => `${value} %`,
            },
          },
        },
      },
    });

    const caption = document.getElementById("curve-caption");
    if (caption) {
      const parts = [`${data.count} merchants`];
      if (data.half) parts.push(`half at ${data.half}`);
      if (data.four_fifths) parts.push(`four fifths at ${data.four_fifths}`);
      // Said only when it is true, and said plainly: a ranking that quietly
      // divided unnamed spending among the merchants it could name would
      // report a ledger more concentrated than the one in the file.
      if (data.unnamed_share > 0.005) {
        parts.push(`${percent(data.unnamed_share)} with no merchant name`);
      }
      caption.textContent = parts.join(" · ");
    }
  }

  // --- The popup ---------------------------------------------------------------

  const modal = document.getElementById("merchant-modal");

  /**
   * A merchant's whole history as a sparkline.
   *
   * Every complete month rather than the ranking window, because the row
   * above it already says what the year cost: what the popup adds is whether
   * that year is the shape of the whole relationship or the end of one.
   */
  function sparkline(node, item) {
    const points = item.series || [];
    if (points.length < 2) {
      node.innerHTML = "";
      return;
    }

    const values = points.map((point) => point.value);
    const top = Math.max(...values, 0);
    const bottom = Math.min(...values, 0);
    const height = top - bottom || 1;
    const stroke = categoryColor(item.category);

    const x = (index) => (index / (points.length - 1)) * 100;
    const y = (value) => 30 - ((value - bottom) / height) * 28;

    const line = values.map((value, index) => `${x(index)},${y(value)}`).join(" ");
    node.innerHTML = [
      '<svg viewBox="0 0 100 32" preserveAspectRatio="none" role="presentation">',
      bottom < 0 && top > 0
        ? `<line x1="0" y1="${y(0)}" x2="100" y2="${y(0)}" class="spark-zero" />`
        : "",
      `<polyline points="${line}" fill="none" stroke="${stroke}" stroke-width="1.5"`,
      ' vector-effect="non-scaling-stroke" stroke-linejoin="round" />',
      "</svg>",
    ].join("");
  }

  function openModal(item) {
    document.getElementById("merchant-heading").textContent = item.name;
    document.getElementById("merchant-swatch").style.background = categoryColor(
      item.category
    );
    document.getElementById("merchant-summary").textContent = [
      item.category,
      `${percent(item.share)} of everything spent`,
      `${formatCurrency(item.ticket)} a visit`,
      `${item.visits}× in the year`,
    ].join(" · ");

    sparkline(document.getElementById("merchant-spark"), item);
    document.getElementById("merchant-history").textContent = [
      `since ${formatMonthLong(item.first_seen.slice(0, 7))}`,
      `last seen ${formatDay(item.last_seen)}`,
      `${(item.series || []).length} months drawn`,
    ].join(" · ");

    renderRows(item);

    modal.hidden = false;
    requestAnimationFrame(() => modal.classList.add("show"));
    document.body.classList.add("modal-open");
    document.getElementById("merchant-close").focus();
  }

  function closeModal() {
    modal.classList.remove("show");
    document.body.classList.remove("modal-open");
    setTimeout(() => {
      modal.hidden = true;
    }, 150);
  }

  /**
   * The largest payments to this merchant, largest first.
   *
   * Built the way the dashboard's drill-down builds its rows, down to the
   * category sitting under the merchant rather than beside it, so the figures
   * line up as a column of figures. The category is only worth repeating
   * where it is not the one the row is filed under.
   */
  function renderRows(item) {
    const list = document.getElementById("merchant-rows");
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
      name.title = row.booking_text;

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
  }

  // --- Wiring ------------------------------------------------------------------

  document.querySelectorAll("[data-merchant]").forEach((row) => {
    const item = merchants.get(row.dataset.merchant);
    if (!item) return;
    row.addEventListener("click", () => openModal(item));
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openModal(item);
      }
    });
  });

  document.getElementById("merchant-close").addEventListener("click", closeModal);
  modal.addEventListener("click", (event) => {
    if (event.target === modal) closeModal();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !modal.hidden) closeModal();
  });

  drawCurve();
})();

/**
 * Dashboard behaviour: data loading, chart rendering and the filter controls.
 *
 * Data flow: one fetch per filter change gives every panel what it needs.
 * Changing only the category selection re-renders from the cached response
 * instead of hitting the API again.
 */
(function () {
  "use strict";

  const {
    categoryColor,
    categoryRank,
    formatCurrency,
    formatCurrencyCompact,
    formatPercent,
    formatMonth,
    formatMonthLong,
    token,
    prefersReducedMotion,
  } = window.Theme;

  const shouldLoadBalances = window.RENDER_BALANCES === true;

  const charts = {
    monthly: null,
    cashflow: null,
    revenue: null,
    expense: null,
  };

  // Latest API responses, kept so category toggles re-render without a fetch.
  let categoryData = null; // chart-data at the selected granularity
  let majorData = null; // chart-data at major granularity, for the spectrum

  const el = (id) => document.getElementById(id);

  // --- Chart.js theming -----------------------------------------------------

  function themeCharts() {
    const paperDim = token("--paper-dim", "#8d99a8");
    const paperFaint = token("--paper-faint", "#616d7c");
    const rule = token("--rule", "#262e39");

    Chart.defaults.font.family =
      '"IBM Plex Mono", ui-monospace, SFMono-Regular, monospace';
    Chart.defaults.font.size = 11;
    Chart.defaults.color = paperFaint;
    Chart.defaults.borderColor = rule;
    Chart.defaults.animation = prefersReducedMotion() ? false : { duration: 400 };

    Chart.defaults.plugins.legend.labels.color = paperDim;
    Chart.defaults.plugins.legend.labels.boxWidth = 10;
    Chart.defaults.plugins.legend.labels.boxHeight = 10;
    Chart.defaults.plugins.legend.labels.usePointStyle = true;

    // Tooltips are drawn as HTML rather than on the canvas, so the charts and
    // the spectrum bar share one surface, one type scale and one alignment.
    Chart.defaults.plugins.tooltip.enabled = false;
    Chart.defaults.plugins.tooltip.position = "nearest";
    Chart.defaults.plugins.tooltip.external = externalTooltip;
    // Nothing of the tooltip is painted on the canvas any more, so its
    // canvas-side animation is dead weight — and animating it while charts are
    // being rebuilt is what makes Chart.js throw from its animator.
    Chart.defaults.plugins.tooltip.animation = false;
    Chart.defaults.plugins.tooltip.animations = false;
  }

  // --- Tooltips -------------------------------------------------------------

  // Callbacks return "name<SEP>value" so the renderer can lay both halves out
  // in their own columns instead of relying on a colon inside one string.
  const SEP = "\u001f";

  let tipNode = null;

  function tipElement() {
    if (!tipNode) {
      tipNode = document.createElement("div");
      tipNode.className = "chart-tip";
      tipNode.setAttribute("role", "tooltip");
      document.body.appendChild(tipNode);
    }
    return tipNode;
  }

  function hideTip() {
    if (tipNode) tipNode.classList.remove("is-visible");
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"]/g, (char) => {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[char];
    });
  }

  /** Anchor the tip above (x, y) in viewport coordinates, clamped on screen. */
  function placeTip(tip, x, y) {
    tip.classList.add("is-visible");
    const rect = tip.getBoundingClientRect();
    const margin = 8;
    const left = Math.max(
      margin,
      Math.min(x - rect.width / 2, window.innerWidth - rect.width - margin)
    );
    let top = y - rect.height - 12;
    if (top < margin) top = y + 18; // flip below when there is no room above
    tip.style.left = `${Math.round(left)}px`;
    tip.style.top = `${Math.round(top)}px`;
  }

  function tipRow(line, swatch) {
    const [name, value] = String(line).split(SEP);
    const dot = swatch
      ? `<span class="chart-tip-swatch" style="background:${escapeHtml(swatch)}"></span>`
      : '<span class="chart-tip-swatch is-empty"></span>';
    return (
      "<li>" +
      dot +
      `<span class="chart-tip-name">${escapeHtml(name)}</span>` +
      `<span class="chart-tip-value">${escapeHtml(value === undefined ? "" : value)}</span>` +
      "</li>"
    );
  }

  function tipMarkup(title, rows, footer) {
    const parts = [];
    if (title) parts.push(`<p class="chart-tip-title">${escapeHtml(title)}</p>`);
    if (rows.length) parts.push(`<ul class="chart-tip-rows">${rows.join("")}</ul>`);
    (footer || []).forEach((line) => {
      const [name, value] = String(line).split(SEP);
      parts.push(
        '<p class="chart-tip-foot">' +
          `<span>${escapeHtml(name)}</span>` +
          `<span>${escapeHtml(value === undefined ? "" : value)}</span>` +
          "</p>"
      );
    });
    return parts.join("");
  }

  /** Chart.js external tooltip handler shared by every chart on the page. */
  function externalTooltip(context) {
    const { chart, tooltip } = context;
    if (!tooltip.opacity) {
      hideTip();
      return;
    }

    const rows = [];
    (tooltip.body || []).forEach((item, index) => {
      const colors = tooltip.labelColors[index] || {};
      const swatch =
        typeof colors.backgroundColor === "string"
          ? colors.backgroundColor
          : typeof colors.borderColor === "string"
            ? colors.borderColor
            : "";
      // Only the first line of an entry carries the series colour; further
      // lines are detail about the same series.
      item.lines.forEach((line, lineIndex) =>
        rows.push(tipRow(line, lineIndex === 0 ? swatch : ""))
      );
    });

    const tip = tipElement();
    tip.innerHTML = tipMarkup(
      (tooltip.title || [])[0] || "",
      rows,
      tooltip.footer || []
    );

    const canvas = chart.canvas.getBoundingClientRect();
    placeTip(tip, canvas.left + tooltip.caretX, canvas.top + tooltip.caretY);
  }

  /** Give a plain DOM element the same tooltip the charts use. */
  function bindTip(node, markup) {
    const show = (event) => {
      const tip = tipElement();
      tip.innerHTML = markup;
      const rect = node.getBoundingClientRect();
      const x = event && event.clientX ? event.clientX : rect.left + rect.width / 2;
      placeTip(tip, x, rect.top);
    };
    node.addEventListener("mouseenter", show);
    node.addEventListener("mousemove", show);
    node.addEventListener("mouseleave", hideTip);
    node.addEventListener("focus", show);
    node.addEventListener("blur", hideTip);
  }

  window.addEventListener("scroll", hideTip, true);
  window.addEventListener("resize", hideTip);

  /** Round up to the next 1/2/5 x 10^n so axis ticks land on round numbers. */
  function niceCeil(value) {
    if (!value || !Number.isFinite(value)) return 1;
    const magnitude = Math.pow(10, Math.floor(Math.log10(value)));
    const step =
      [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10].find(
        (s) => value <= s * magnitude
      ) || 10;
    return step * magnitude;
  }

  function moneyAxis() {
    const rule = token("--rule", "#262e39");
    const ruleStrong = token("--rule-strong", "#35404e");
    return {
      // The zero line is the reference for every signed chart here, so it gets
      // the stronger hairline.
      grid: {
        drawTicks: false,
        color: (ctx) => (ctx.tick.value === 0 ? ruleStrong : rule),
      },
      border: { display: false },
      ticks: {
        padding: 8,
        maxTicksLimit: 7,
        callback: (value) => formatCurrencyCompact(value),
      },
    };
  }

  function monthAxis() {
    return {
      grid: { display: false },
      border: { color: token("--rule", "#262e39") },
      ticks: {
        maxRotation: 0,
        autoSkip: true,
        autoSkipPadding: 12,
        padding: 6,
      },
    };
  }

  // --- Balances -------------------------------------------------------------

  const BALANCE_ACCOUNTS = [
    { label: "Girokonto", aliases: ["girokonto", "giro"] },
    { label: "Tagesgeld PLUS", aliases: ["tagesgeld"] },
    { label: "Visa-Karte", aliases: ["visa-kredit", "visa kredit", "visa"] },
    { label: "Depot", aliases: ["depot"] },
  ];

  function orderBalances(balances) {
    const entries = Object.entries(balances || {});
    const rows = BALANCE_ACCOUNTS.map(({ label, aliases }) => {
      const match = entries.find(([key]) =>
        aliases.some((alias) => String(key).toLowerCase().includes(alias))
      );
      return [label, match ? Number(match[1]) || 0 : 0];
    });
    const total = rows.reduce((sum, [, amount]) => sum + amount, 0);
    rows.push(["Total", total]);
    return rows;
  }

  function renderBalances(entries, container) {
    container.innerHTML = "";
    entries.forEach(([label, amount], index) => {
      const card = document.createElement("div");
      card.className =
        "balance-card" + (index === entries.length - 1 ? " is-total" : "");

      const heading = document.createElement("h4");
      heading.textContent = label;

      const value = document.createElement("p");
      value.className =
        "balance-amount" + (amount < 0 ? " is-negative" : "");
      value.textContent = formatCurrency(amount);

      card.append(heading, value);
      container.appendChild(card);
    });
  }

  function showBalanceNotice(container, message) {
    container.innerHTML = "";
    container.classList.remove("balance-strip");
    const notice = document.createElement("div");
    notice.className = "panel-outline";
    notice.innerHTML =
      '<p class="panel-title">Balances unavailable</p>' +
      '<p class="panel-text"></p>';
    notice.querySelector(".panel-text").textContent = message;
    container.appendChild(notice);
  }

  function formatLoadedAt(value) {
    if (!value) return "Not loaded";
    const stamp = String(value).endsWith("Z") ? value : `${value}Z`;
    const parsed = new Date(stamp);
    if (Number.isNaN(parsed.getTime())) return "Loaded";
    return `Loaded ${parsed.toLocaleString("de-DE", {
      timeZone: "Europe/Berlin",
      dateStyle: "medium",
      timeStyle: "short",
    })}`;
  }

  async function loadBalances() {
    const grid = el("balances-grid");
    const meta = el("balances-loaded-at");
    if (!grid) return;

    try {
      const response = await fetch("/api/account-balances");
      const snapshot = await response.json();

      if (!["ready", "stale"].includes(snapshot.status)) {
        meta.textContent = "";
        showBalanceNotice(
          grid,
          snapshot.error || "Enable startup auth to load live balances."
        );
        return;
      }

      meta.textContent =
        formatLoadedAt(snapshot.loaded_at) +
        (snapshot.status === "stale" ? " · stored fallback" : "");

      const balances = snapshot.balances || {};
      if (!Object.keys(balances).length) {
        showBalanceNotice(
          grid,
          "The Comdirect session is ready but returned no balances."
        );
        return;
      }
      renderBalances(orderBalances(balances), grid);
    } catch (error) {
      meta.textContent = "";
      showBalanceNotice(grid, "Could not reach the backend for balances.");
    }
  }

  // --- Category spectrum ----------------------------------------------------

  /** Total expense per category, largest first. */
  function expenseTotals(data) {
    return (data.categories || [])
      .map((category) => {
        const values = data.data[category] || [];
        const spend = values.reduce(
          (sum, value) => (value < 0 ? sum + Math.abs(value) : sum),
          0
        );
        return { category, spend };
      })
      .filter((row) => row.spend > 0)
      .sort((a, b) => b.spend - a.spend);
  }

  function revenueTotals(data) {
    return (data.categories || [])
      .map((category) => {
        const values = data.data[category] || [];
        const income = values.reduce(
          (sum, value) => (value > 0 ? sum + value : sum),
          0
        );
        return { category, income };
      })
      .filter((row) => row.income > 0)
      .sort((a, b) => b.income - a.income);
  }

  function renderPeriodSummary(months) {
    const summary = el("period-summary");
    if (!summary) return;
    if (!months || !months.length) {
      summary.textContent = "No transactions imported yet.";
      return;
    }
    const first = formatMonthLong(months[0]);
    const last = formatMonthLong(months[months.length - 1]);
    summary.textContent =
      `${first} to ${last} · ${months.length} month${months.length === 1 ? "" : "s"}`;
  }

  function renderSpectrum(data) {
    const bar = el("spectrum-bar");
    const keys = el("spectrum-keys");
    const caption = el("spectrum-caption");
    if (!bar) return;

    const rows = expenseTotals(data);
    const total = rows.reduce((sum, row) => sum + row.spend, 0);

    const year = el("year-select").value;
    caption.textContent = `Expenses · ${year || "all years"}`;

    bar.innerHTML = "";
    keys.innerHTML = "";

    if (!total) {
      keys.innerHTML =
        '<p class="spectrum-empty">No expenses in this period yet.</p>';
      return;
    }

    rows.forEach((row, index) => {
      const share = (row.spend / total) * 100;
      const color = categoryColor(row.category);

      const seg = document.createElement("div");
      seg.className = "spectrum-seg";
      seg.style.flex = `${row.spend} 0 0`;
      seg.style.background = color;
      seg.style.animationDelay = `${index * 0.03}s`;
      seg.tabIndex = 0; // the segment is the only handle on its share
      seg.setAttribute("aria-label", `${row.category}: ${formatCurrency(row.spend)}`);
      bindTip(
        seg,
        tipMarkup(
          row.category,
          [
            tipRow(`Spent${SEP}${formatCurrency(row.spend)}`, color),
            tipRow(`Share${SEP}${formatPercent(share)}`, ""),
          ],
          [`Period total${SEP}${formatCurrency(total)}`]
        )
      );
      bar.appendChild(seg);

      const key = document.createElement("div");
      key.className = "spectrum-key";
      key.innerHTML =
        '<span class="option-swatch"></span>' +
        "<span></span>" +
        '<span class="spectrum-value"></span>' +
        '<span class="spectrum-share"></span>';
      key.querySelector(".option-swatch").style.background = color;
      key.children[1].textContent = row.category;
      key.querySelector(".spectrum-value").textContent = formatCurrency(row.spend);
      key.querySelector(".spectrum-share").textContent = formatPercent(share);
      keys.appendChild(key);
    });

    if (!prefersReducedMotion()) {
      bar.classList.remove("animate");
      void bar.offsetWidth; // restart the unfurl on every filter change
      bar.classList.add("animate");
    }
  }

  // --- Monthly stacked chart ------------------------------------------------

  function renderMonthlyChart(data, selected) {
    // Stack in hierarchy order rather than alphabetically: minors of the same
    // major then sit together, and the stack reads as major-category bands.
    const shown = (data.categories || [])
      .filter((category) => selected.includes(category))
      .sort((a, b) => categoryRank(a) - categoryRank(b) || a.localeCompare(b));

    const datasets = shown.map((category) => ({
      label: category,
      data: data.data[category],
      backgroundColor: categoryColor(category),
      borderWidth: 0,
      maxBarThickness: 42,
    }));

    // A symmetric axis keeps the zero line in the same place across filters,
    // so months can be compared by eye between views.
    let extent = 0;
    (data.months || []).forEach((_, index) => {
      let positive = 0;
      let negative = 0;
      shown.forEach((category) => {
        const value = data.data[category][index] || 0;
        if (value > 0) positive += value;
        else negative += value;
      });
      extent = Math.max(extent, positive, Math.abs(negative));
    });
    extent = niceCeil(extent * 1.05);

    const config = {
      type: "bar",
      data: {
        labels: (data.months || []).map(formatMonth),
        datasets,
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "nearest", intersect: true },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) =>
                items.length
                  ? formatMonthLong((data.months || [])[items[0].dataIndex])
                  : "",
              label: (ctx) =>
                `${ctx.dataset.label}${SEP}${formatCurrency(ctx.parsed.y)}`,
              // The stack only shows the hovered band, so the month's totals
              // go in the footer to keep that band in context.
              footer: (items) => {
                if (!items.length) return [];
                const index = items[0].dataIndex;
                let incoming = 0;
                let outgoing = 0;
                shown.forEach((category) => {
                  const value = data.data[category][index] || 0;
                  if (value > 0) incoming += value;
                  else outgoing += value;
                });
                const share = items[0].parsed.y >= 0 ? incoming : outgoing;
                return [
                  `Month in${SEP}${formatCurrency(incoming)}`,
                  `Month out${SEP}${formatCurrency(outgoing)}`,
                  `Net${SEP}${formatCurrency(incoming + outgoing)}`,
                  `Share of ${items[0].parsed.y >= 0 ? "in" : "out"}${SEP}${formatPercent(
                    share ? (items[0].parsed.y / share) * 100 : 0
                  )}`,
                ];
              },
            },
          },
        },
        scales: {
          x: { ...monthAxis(), stacked: true },
          y: { ...moneyAxis(), stacked: true, min: -extent, max: extent },
        },
      },
    };

    if (charts.monthly) charts.monthly.destroy();
    charts.monthly = new Chart(el("monthlyChart"), config);
  }

  // --- Cashflow chart -------------------------------------------------------

  /**
   * The running total lives on its own scale, so the shared zero gridline says
   * nothing about it. Draw its zero explicitly, dashed and in the line's
   * colour, so the line can be read against the right-hand ticks.
   */
  function runningZeroLine(color) {
    return {
      id: "runningZeroLine",
      afterDatasetsDraw(chart) {
        const scale = chart.scales.y1;
        if (!scale || !chart.chartArea) return;
        const y = scale.getPixelForValue(0);
        const { left, right } = chart.chartArea;
        const ctx = chart.ctx;
        ctx.save();
        ctx.setLineDash([3, 3]);
        ctx.lineWidth = 1;
        ctx.strokeStyle = color;
        ctx.globalAlpha = 0.4;
        ctx.beginPath();
        ctx.moveTo(left, y);
        ctx.lineTo(right, y);
        ctx.stroke();
        ctx.restore();
      },
    };
  }

  function renderCashflowChart(data) {
    const positive = token("--pos", "#5bc98f");
    const negative = token("--neg", "#e0655c");
    const paper = token("--paper", "#e8ecf1");

    const totalEl = el("total-cashflow");
    totalEl.textContent = formatCurrency(data.total);
    totalEl.classList.toggle("is-positive", data.total >= 0);
    totalEl.classList.toggle("is-negative", data.total < 0);

    let running = 0;
    const cumulative = data.data.map((value) => (running += value));

    const extent = niceCeil((Math.max(...data.data.map(Math.abs)) || 1) * 1.05);

    // Round bounds for the running total, with a little room on the far side
    // of zero so its zero line never sits flush against the frame — that is
    // the reference readers need to tell a rising total from a falling one.
    const high = Math.max(...cumulative, 0);
    const low = Math.min(...cumulative, 0);
    const pad = niceCeil((high - low || 1) * 0.08);
    const cumTop = high > 0 ? niceCeil(high) : pad;
    const cumBottom = low < 0 ? -niceCeil(-low) : -pad;

    const config = {
      type: "bar",
      data: {
        labels: data.months.map(formatMonth),
        datasets: [
          {
            type: "bar",
            label: "Net per month",
            data: data.data,
            backgroundColor: data.data.map((v) => (v >= 0 ? positive : negative)),
            borderWidth: 0,
            maxBarThickness: 42,
            order: 1,
          },
          {
            type: "line",
            label: "Running total",
            data: cumulative,
            borderColor: paper,
            backgroundColor: paper,
            borderWidth: 1.5,
            pointRadius: 0,
            pointHoverRadius: 4,
            tension: 0.25,
            yAxisID: "y1",
            order: 2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: true, position: "bottom", align: "start" },
          tooltip: {
            callbacks: {
              title: (items) =>
                items.length ? formatMonthLong(data.months[items[0].dataIndex]) : "",
              label: (ctx) =>
                `${ctx.dataset.label}${SEP}${formatCurrency(ctx.parsed.y)}`,
              footer: (items) => {
                if (!items.length) return [];
                const index = items[0].dataIndex;
                const previous = index > 0 ? cumulative[index - 1] : 0;
                return [
                  `Change vs. previous${SEP}${formatCurrency(
                    cumulative[index] - previous
                  )}`,
                ];
              },
            },
          },
        },
        scales: {
          x: monthAxis(),
          y: { ...moneyAxis(), min: -extent, max: extent },
          // The running total is free to use its own range: forcing it
          // symmetric would flatten the trend it exists to show. That means
          // its zero is not the bar axis' zero, so its ticks carry the line's
          // colour and runningZeroLine draws its zero.
          y1: {
            ...moneyAxis(),
            position: "right",
            grid: { drawOnChartArea: false },
            min: cumBottom,
            max: cumTop,
            ticks: { ...moneyAxis().ticks, color: paper },
          },
        },
      },
    };

    if (charts.cashflow) charts.cashflow.destroy();
    charts.cashflow = new Chart(el("cashflowChart"), {
      ...config,
      plugins: [runningZeroLine(paper)],
    });
  }

  // --- Breakdowns -----------------------------------------------------------

  const BREAKDOWN_LIMIT = 8;

  /** Keep the top N and roll the tail into a single "Other categories" row. */
  function collapseTail(rows, valueKey) {
    if (rows.length <= BREAKDOWN_LIMIT + 1) return rows.slice();
    const head = rows.slice(0, BREAKDOWN_LIMIT);
    const tail = rows.slice(BREAKDOWN_LIMIT);
    const rest = tail.reduce((sum, row) => sum + row[valueKey], 0);
    head.push({
      category: `${tail.length} smaller categories`,
      [valueKey]: rest,
      muted: true,
    });
    return head;
  }

  function renderBreakdown(canvasId, listId, totalId, rows, valueKey, chartKey) {
    const collapsed = collapseTail(rows, valueKey);
    const total = rows.reduce((sum, row) => sum + row[valueKey], 0);
    const colors = collapsed.map((row) =>
      row.muted ? token("--rule-strong", "#35404e") : categoryColor(row.category)
    );

    el(totalId).textContent = formatCurrency(total);

    const list = el(listId);
    list.innerHTML = "";
    if (!collapsed.length) {
      list.innerHTML =
        '<li class="spectrum-empty">Nothing recorded in this period.</li>';
    }
    collapsed.forEach((row, index) => {
      const share = total ? (row[valueKey] / total) * 100 : 0;
      const item = document.createElement("li");
      item.className = "legend-row";
      item.innerHTML =
        '<span class="option-swatch"></span>' +
        '<span class="legend-name"></span>' +
        '<span class="legend-value"></span>' +
        '<span class="legend-share"></span>';
      item.querySelector(".option-swatch").style.background = colors[index];
      item.querySelector(".legend-name").textContent = row.category;
      item.querySelector(".legend-value").textContent = formatCurrency(
        row[valueKey]
      );
      item.querySelector(".legend-share").textContent = formatPercent(share);
      list.appendChild(item);
    });

    const config = {
      type: "doughnut",
      data: {
        labels: collapsed.map((row) => row.category),
        datasets: [
          {
            data: collapsed.map((row) => row[valueKey]),
            backgroundColor: colors,
            borderColor: token("--ink-1", "#11151b"),
            borderWidth: 2,
            hoverOffset: 6,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "62%",
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => (items.length ? items[0].label : ""),
              label: (ctx) => {
                const share = total ? (ctx.parsed / total) * 100 : 0;
                return [
                  `Amount${SEP}${formatCurrency(ctx.parsed)}`,
                  `Share${SEP}${formatPercent(share)}`,
                ];
              },
              footer: () => [`Period total${SEP}${formatCurrency(total)}`],
            },
          },
        },
      },
    };

    if (charts[chartKey]) charts[chartKey].destroy();
    charts[chartKey] = new Chart(el(canvasId), config);
  }

  // --- Category filter ------------------------------------------------------

  function selectedCategories() {
    return Array.from(
      el("category-dropdown").querySelectorAll(
        'input[type="checkbox"]:checked:not(#cat-all)'
      )
    ).map((input) => input.value);
  }

  function updateFilterSummary() {
    const dropdown = el("category-dropdown");
    const all = dropdown.querySelectorAll(
      'input[type="checkbox"]:not(#cat-all)'
    );
    const checked = selectedCategories();
    const display = el("category-display");

    if (checked.length === 0) display.textContent = "None selected";
    else if (checked.length === all.length) display.textContent = "All categories";
    else display.textContent = `${checked.length} of ${all.length}`;

    const allBox = el("cat-all");
    allBox.checked = checked.length === all.length;
    allBox.indeterminate = checked.length > 0 && checked.length < all.length;
  }

  // Only called when the category list itself changes (first load, or a
  // granularity switch), so every box starts ticked: the previous ticks named
  // categories from the other granularity and would carry no meaning here.
  function buildCategoryFilter(data) {
    const dropdown = el("category-dropdown");
    dropdown.innerHTML = "";

    const allOption = document.createElement("div");
    allOption.className = "multiselect-option";
    allOption.innerHTML =
      '<input type="checkbox" id="cat-all" checked />' +
      '<label for="cat-all">All categories</label>';
    dropdown.appendChild(allOption);

    const divider = document.createElement("div");
    divider.className = "multiselect-divider";
    dropdown.appendChild(divider);

    (data.categories || [])
      .filter((category) => category !== "Uncategorized")
      .forEach((category, index) => {
        const option = document.createElement("div");
        option.className = "multiselect-option";

        const box = document.createElement("input");
        box.type = "checkbox";
        box.id = `cat-${index}`;
        box.value = category;
        box.checked = true;
        box.addEventListener("change", () => {
          updateFilterSummary();
          renderMonthlyChart(categoryData, selectedCategories());
        });

        const swatch = document.createElement("span");
        swatch.className = "option-swatch";
        swatch.style.background = categoryColor(category);

        const label = document.createElement("label");
        label.htmlFor = box.id;
        label.textContent = category;

        option.append(box, swatch, label);
        dropdown.appendChild(option);
      });

    el("cat-all").addEventListener("change", (event) => {
      dropdown
        .querySelectorAll('input[type="checkbox"]:not(#cat-all)')
        .forEach((box) => {
          box.checked = event.target.checked;
        });
      updateFilterSummary();
      renderMonthlyChart(categoryData, selectedCategories());
    });

    updateFilterSummary();
  }

  function wireMultiselect() {
    const button = el("category-button");
    const dropdown = el("category-dropdown");

    const close = () => {
      dropdown.classList.remove("show");
      button.setAttribute("aria-expanded", "false");
    };

    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const open = dropdown.classList.toggle("show");
      button.setAttribute("aria-expanded", String(open));
    });

    dropdown.addEventListener("click", (event) => event.stopPropagation());
    document.addEventListener("click", close);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && dropdown.classList.contains("show")) {
        close();
        button.focus();
      }
    });
  }

  // --- Data loading ---------------------------------------------------------

  function currentYear() {
    const value = el("year-select").value;
    return value ? Number(value) : "";
  }

  function currentGranularity() {
    return el("category-type-select").value || "minor";
  }

  async function fetchChartData(year, categoryType) {
    const params = new URLSearchParams();
    if (year) params.append("year", year);
    if (categoryType) params.append("category_type", categoryType);
    const response = await fetch(`/api/chart-data?${params}`);
    return response.json();
  }

  async function fetchCashflow(year) {
    const params = new URLSearchParams();
    if (year) params.append("year", year);
    const response = await fetch(`/api/cashflow-data?${params}`);
    return response.json();
  }

  // Filter changes fire faster than the API answers. Every refresh takes a
  // ticket and drops out if a newer one has been issued while it waited:
  // without that, a slow earlier response renders last and tears down the
  // charts the newer one already built, leaving empty canvases behind.
  let refreshTicket = 0;

  // Set whenever the category list needs rebuilding. It is cleared only by the
  // refresh that actually rebuilds, so a refresh dropped as stale hands the job
  // to its successor instead of losing it.
  let filterNeedsRebuild = true;

  async function refresh() {
    hideTip(); // the elements it points at are about to be replaced
    const ticket = (refreshTicket += 1);
    const stale = () => ticket !== refreshTicket;

    const year = currentYear();
    const granularity = currentGranularity();

    const [data, cashflow] = await Promise.all([
      fetchChartData(year, granularity),
      fetchCashflow(year),
    ]);
    if (stale()) return null;

    const major =
      granularity === "major" ? data : await fetchChartData(year, "major");
    if (stale()) return null;

    categoryData = data;
    majorData = major;

    if (filterNeedsRebuild) {
      buildCategoryFilter(data);
      filterNeedsRebuild = false;
    }

    renderPeriodSummary(data.months);
    renderSpectrum(majorData);
    renderMonthlyChart(data, selectedCategories());
    renderCashflowChart(cashflow);
    renderBreakdown(
      "revenuePieChart",
      "revenue-legend",
      "revenue-total",
      revenueTotals(data),
      "income",
      "revenue"
    );
    renderBreakdown(
      "expensePieChart",
      "expense-legend",
      "expense-total",
      expenseTotals(data),
      "spend",
      "expense"
    );
    return data;
  }

  function populateYears(years) {
    const select = el("year-select");
    (years || []).forEach((year) => {
      const option = document.createElement("option");
      option.value = year;
      option.textContent = year;
      select.appendChild(option);
    });
  }

  function updateFilterLabel() {
    el("category-label").textContent =
      currentGranularity() === "major" ? "Major categories" : "Minor categories";
  }

  async function init() {
    themeCharts();
    wireMultiselect();
    if (shouldLoadBalances) loadBalances();

    updateFilterLabel();
    const data = await refresh();
    if (data) populateYears(data.available_years);

    el("year-select").addEventListener("change", refresh);
    el("category-type-select").addEventListener("change", () => {
      updateFilterLabel();
      filterNeedsRebuild = true; // the category names are about to change
      refresh();
    });
  }

  document.addEventListener("DOMContentLoaded", init);
})();

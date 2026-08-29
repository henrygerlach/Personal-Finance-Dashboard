/**
 * Shared theme utilities: the category colour system, number formatting and
 * small DOM helpers used by every page.
 *
 * The palette is derived from CATEGORY_HIERARCHY rather than hand-listed, so a
 * new category in constants.py gets a sensible colour with no frontend edit.
 * Each major category owns a hue; its minor categories are perceptual
 * lightness steps within that hue. Colours are computed in OKLCH and converted
 * to sRGB here so Chart.js and CSS receive plain hex either way.
 */
(function (global) {
  "use strict";

  // --- OKLCH -> hex ---------------------------------------------------------

  function gamma(x) {
    return x <= 0.0031308 ? 12.92 * x : 1.055 * Math.pow(x, 1 / 2.4) - 0.055;
  }

  function clamp255(x) {
    return Math.max(0, Math.min(255, Math.round(x * 255)));
  }

  function oklchToHex(L, C, H) {
    const h = (H * Math.PI) / 180;
    const a = C * Math.cos(h);
    const b = C * Math.sin(h);

    const l_ = L + 0.3963377774 * a + 0.2158037573 * b;
    const m_ = L - 0.1055613458 * a - 0.0638541728 * b;
    const s_ = L - 0.0894841775 * a - 1.291485548 * b;

    const l = l_ * l_ * l_;
    const m = m_ * m_ * m_;
    const s = s_ * s_ * s_;

    const r = gamma(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s);
    const g = gamma(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s);
    const bl = gamma(0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s);

    return (
      "#" +
      [r, g, bl]
        .map((v) => clamp255(v).toString(16).padStart(2, "0"))
        .join("")
    );
  }

  // --- Category palette -----------------------------------------------------

  // One hue per major category, spread around the wheel so no two majors
  // read as the same colour in a stacked bar. Lightness and chroma are held
  // close together so the stack reads as an even field rather than a set of
  // flares, and each major's minors are lightness steps within its hue.
  const MAJOR_TONES = {
    // Income is the only major that is money coming in, so it gets the one
    // green in the palette and nothing else comes near that hue.
    Income: { L: 0.74, C: 0.13, H: 148 },
    Housing: { L: 0.66, C: 0.12, H: 268 },
    "Food & Drink": { L: 0.77, C: 0.13, H: 72 },
    Transportation: { L: 0.71, C: 0.11, H: 222 },
    "Health & Personal Care": { L: 0.68, C: 0.13, H: 36 },
    Shopping: { L: 0.7, C: 0.12, H: 350 },
    "Leisure & Entertainment": { L: 0.72, C: 0.12, H: 322 },
    Travel: { L: 0.68, C: 0.115, H: 296 },
    "Family & Education": { L: 0.7, C: 0.11, H: 200 },
    Pets: { L: 0.73, C: 0.1, H: 96 },
    Financial: { L: 0.72, C: 0.115, H: 170 },
    "Gifts & Donations": { L: 0.75, C: 0.12, H: 6 },
    // Transfers move money without spending it, so they stay near-neutral —
    // and they are excluded from the expense charts entirely.
    "Transfers & Savings": { L: 0.62, C: 0.03, H: 250 },
    // Nothing has been decided about these yet, so they must not compete for
    // attention with a category that has.
    Uncategorized: { L: 0.5, C: 0.008, H: 250 },
  };

  // Buckets that mean "not classified" are deliberately colourless: they should
  // read as absence, not as another category competing for attention.
  const NEUTRAL_CATEGORIES = {
    "Needs Review": { L: 0.5, C: 0.008, H: 250 },
    Uncategorized: { L: 0.5, C: 0.008, H: 250 },
  };

  const MINOR_STEP = 0.058;

  function hashHue(name) {
    let hash = 0;
    for (let i = 0; i < name.length; i += 1) {
      hash = (hash * 31 + name.charCodeAt(i)) % 360;
    }
    return hash;
  }

  function buildPalette(hierarchy) {
    const palette = Object.create(null);
    const majorOf = Object.create(null);
    // Position of each category in the hierarchy, so charts can stack minors
    // in major-category blocks instead of alphabetically.
    const rank = Object.create(null);
    let position = 0;

    Object.keys(hierarchy || {}).forEach((major) => {
      const tone = MAJOR_TONES[major] || {
        L: 0.68,
        C: 0.1,
        H: hashHue(major),
      };
      palette[major] = oklchToHex(tone.L, tone.C, tone.H);
      rank[major] = position += 1;

      const minors = hierarchy[major] || [];
      minors.forEach((minor, index) => {
        // A minor name can appear under several majors ("Other" appears under
        // most of them) and the API aggregates by name alone, so the first
        // major that claims a name owns its colour.
        if (minor in palette) return;

        majorOf[minor] = major;
        rank[minor] = position += 1;

        if (NEUTRAL_CATEGORIES[minor]) {
          const n = NEUTRAL_CATEGORIES[minor];
          palette[minor] = oklchToHex(n.L, n.C, n.H);
          return;
        }

        const offset = index - (minors.length - 1) / 2;
        const L = Math.max(0.42, Math.min(0.87, tone.L + offset * MINOR_STEP));
        // Pull chroma in at the light and dark ends so the ramp stays even.
        const C = tone.C * (1 - Math.min(0.35, Math.abs(offset) * 0.12));
        palette[minor] = oklchToHex(L, C, tone.H);
      });
    });

    return { palette, majorOf, rank };
  }

  const hierarchy = global.CATEGORY_HIERARCHY || {};
  const { palette, majorOf, rank } = buildPalette(hierarchy);
  const unknownCache = Object.create(null);

  /** Stable colour for any category name, known or not. */
  function categoryColor(name) {
    const key = String(name || "Other");
    if (palette[key]) return palette[key];
    if (NEUTRAL_CATEGORIES[key]) {
      const n = NEUTRAL_CATEGORIES[key];
      return (palette[key] = oklchToHex(n.L, n.C, n.H));
    }
    // Categories left over from earlier rule sets still need a fixed colour —
    // deriving it from the name keeps it stable across reloads.
    if (!unknownCache[key]) {
      unknownCache[key] = oklchToHex(0.68, 0.09, hashHue(key));
    }
    return unknownCache[key];
  }

  /** Sort key placing a category where it sits in the hierarchy. */
  function categoryRank(name) {
    return name in rank ? rank[name] : Number.MAX_SAFE_INTEGER;
  }

  /** Major category a minor belongs to, or the name itself if it is a major. */
  function majorFor(name) {
    if (hierarchy[name]) return name;
    return majorOf[name] || "Other";
  }

  // --- Formatting -----------------------------------------------------------

  const euro = new Intl.NumberFormat("de-DE", {
    style: "currency",
    currency: "EUR",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

  const euroCompact = new Intl.NumberFormat("de-DE", {
    style: "currency",
    currency: "EUR",
    notation: "compact",
    maximumFractionDigits: 1,
  });

  function formatCurrency(value) {
    return euro.format(Number(value) || 0);
  }

  function formatCurrencyCompact(value) {
    return euroCompact.format(Number(value) || 0);
  }

  function formatPercent(value) {
    return `${(Number(value) || 0).toFixed(1)} %`;
  }

  function formatMonth(month) {
    const [year, monthNum] = String(month).split("-");
    const date = new Date(Number(year), Number(monthNum) - 1);
    // en-US keeps every month abbreviation to three letters, which keeps
    // the axis labels from jostling each other.
    return date.toLocaleDateString("en-US", { month: "short", year: "2-digit" });
  }

  /** Day and month of an ISO date, for lists that already state the month. */
  function formatDay(isoDate) {
    const [year, month, day] = String(isoDate).split("-");
    const date = new Date(Number(year), Number(month) - 1, Number(day));
    return date.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
  }

  /** Unambiguous month for prose, where "Jan 23" could read as a date. */
  function formatMonthLong(month) {
    const [year, monthNum] = String(month).split("-");
    const date = new Date(Number(year), Number(monthNum) - 1);
    return date.toLocaleDateString("en-GB", { month: "long", year: "numeric" });
  }

  /**
   * Where to put the zero line in a two-directional bar figure, and how tall
   * the bars may be.
   *
   * Both the trip timeline and the recurring month dial draw spending rising
   * from a line and money coming in hanging below it, to one shared scale so
   * the two directions stay comparable. Where that line belongs depends on
   * the data: a trip with no refunds should give the whole height to its
   * spending, while one that got half its money back needs the line near the
   * middle. Fixing it meant a single large refund was drawn straight through
   * the bottom of the plot, and pinning it to the middle meant one small
   * refund wasted half the figure.
   *
   * The line is placed so that the tallest bar on each side lands exactly on
   * its own margin, which is the only position where neither side is clipped
   * and no space is wasted.
   *
   * @param {number} up Largest magnitude drawn above the line.
   * @param {number} down Largest magnitude drawn below it.
   * @returns {{baseline: number, scale: number}} The line's distance from the
   *   bottom of the plot, and the height of a full-magnitude bar — both as
   *   percentages of the plot's height.
   */
  function baselineFor(up, down) {
    // Heights go by square root, so one large payment cannot flatten every
    // other bar to nothing. The shares are compared in the same terms.
    const biggest = Math.max(up, down, 0);
    if (!biggest) return { baseline: 4, scale: 0 };
    const upShare = Math.sqrt(Math.max(up, 0) / biggest);
    const downShare = Math.sqrt(Math.max(down, 0) / biggest);

    // Headroom at the top for axis labels, a hairline of margin at the foot.
    const TOP = 88;
    const FOOT = 4;
    const baseline = (TOP * downShare + FOOT * upShare) / (upShare + downShare);
    const scale = upShare ? (TOP - baseline) / upShare : (baseline - FOOT) / downShare;
    return { baseline, scale };
  }

  /** Read a design token off :root so JS-drawn surfaces match the stylesheet. */
  function token(name, fallback) {
    const value = getComputedStyle(document.documentElement)
      .getPropertyValue(name)
      .trim();
    return value || fallback;
  }

  const prefersReducedMotion = () =>
    global.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Any element carrying data-money renders its value in the page's number
  // format, so server-rendered figures match chart and dashboard figures.
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-money]").forEach((node) => {
      const amount = Number(node.dataset.money);
      if (Number.isNaN(amount)) return;
      node.textContent = formatCurrency(amount);
      node.classList.toggle("is-positive", amount > 0);
      node.classList.toggle("is-negative", amount < 0);
    });
  });

  global.Theme = {
    baselineFor,
    categoryColor,
    categoryRank,
    majorFor,
    hierarchy,
    formatCurrency,
    formatCurrencyCompact,
    formatPercent,
    formatMonth,
    formatMonthLong,
    formatDay,
    token,
    oklchToHex,
    prefersReducedMotion,
  };
})(window);

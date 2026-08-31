/**
 * The Insights menu in the top bar.
 *
 * Opened by click rather than by hover: a hover menu cannot be reached on a
 * touch screen at all, and on a pointer it opens itself whenever the cursor
 * crosses the bar on its way somewhere else.
 *
 * The menu is the only part of the app's chrome that hides a destination, so
 * it has to be reachable the way a menu is expected to be — arrow keys move
 * through it, Home and End jump to its ends, Escape closes it and hands focus
 * back to the button that opened it.
 */
(function () {
  "use strict";

  const button = document.getElementById("insights-button");
  const menu = document.getElementById("insights-menu");
  if (!button || !menu) return;

  const items = () => Array.from(menu.querySelectorAll(".nav-menu-item"));

  function open(focusIndex) {
    menu.hidden = false;
    button.setAttribute("aria-expanded", "true");
    requestAnimationFrame(() => menu.classList.add("show"));
    if (focusIndex !== undefined) {
      const list = items();
      const target = list[(focusIndex + list.length) % list.length];
      if (target) target.focus();
    }
  }

  function close(returnFocus) {
    menu.classList.remove("show");
    button.setAttribute("aria-expanded", "false");
    menu.hidden = true;
    if (returnFocus) button.focus();
  }

  const isOpen = () => !menu.hidden;

  function step(delta) {
    const list = items();
    const from = list.indexOf(document.activeElement);
    const next = list[(from + delta + list.length) % list.length];
    if (next) next.focus();
  }

  button.addEventListener("click", (event) => {
    event.stopPropagation();
    if (isOpen()) close(false);
    else open();
  });

  button.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      open(0);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      open(-1);
    }
  });

  menu.addEventListener("keydown", (event) => {
    const list = items();
    if (event.key === "ArrowDown") {
      event.preventDefault();
      step(1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      step(-1);
    } else if (event.key === "Home") {
      event.preventDefault();
      if (list[0]) list[0].focus();
    } else if (event.key === "End") {
      event.preventDefault();
      if (list.length) list[list.length - 1].focus();
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && isOpen()) close(true);
  });

  // Anywhere outside closes it, the menu itself included: every item in it is
  // a link, and leaving the menu open behind the page it just navigated to
  // would be the only thing on screen that outlived its own page.
  document.addEventListener("click", (event) => {
    if (isOpen() && !button.contains(event.target)) close(false);
  });

  // Focus leaving the bar closes it too, so tabbing past the menu does not
  // leave it hanging open over the page.
  document.addEventListener("focusin", (event) => {
    if (isOpen() && !menu.contains(event.target) && event.target !== button) {
      close(false);
    }
  });
})();

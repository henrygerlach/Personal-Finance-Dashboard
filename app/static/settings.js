document.querySelectorAll(".reveal-toggle").forEach((button) => {
  const input = document.getElementById(button.dataset.target);
  button.addEventListener("click", () => {
    const revealed = input.type === "text";
    input.type = revealed ? "password" : "text";
    button.classList.toggle("is-active", !revealed);
    button.setAttribute("aria-label", revealed ? "Show" : "Hide");
  });
});

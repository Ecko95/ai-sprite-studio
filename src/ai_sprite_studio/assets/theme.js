// Theme bootstrap — loaded synchronously in <head> so the right palette is set
// before first paint (no flash of the wrong theme). CSP forbids inline script.
(() => {
	const KEY = "studioTheme";
	const root = document.documentElement;

	const preferred = () => {
		const saved = localStorage.getItem(KEY);
		if (saved === "light" || saved === "dark") return saved;
		return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
	};

	const apply = (theme) => {
		root.setAttribute("data-theme", theme);
		document.querySelectorAll("[data-theme-toggle]").forEach((btn) => {
			btn.textContent = theme === "dark" ? "☀" : "☾";
			btn.setAttribute("aria-label", theme === "dark" ? "Switch to light theme" : "Switch to dark theme");
		});
	};

	root.setAttribute("data-theme", preferred());

	document.addEventListener("DOMContentLoaded", () => {
		apply(preferred());
		document.querySelectorAll("[data-theme-toggle]").forEach((btn) => {
			btn.addEventListener("click", () => {
				const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
				localStorage.setItem(KEY, next);
				apply(next);
			});
		});
	});

	matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
		if (!localStorage.getItem(KEY)) apply(preferred());
	});
})();

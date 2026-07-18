// SPDX-License-Identifier: Apache-2.0
// Editing-suite add-on injected next to the vendored curator UI (CSP-safe:
// external script, styles set via CSSOM). Talks only to the local API.
(() => {
	const CELL = 256;

	const style = (el, props) => Object.assign(el.style, props);

	const post = async (query) => {
		const res = await fetch("/curator/normalize?" + query, { method: "POST" });
		if (!res.ok) {
			let message = res.statusText;
			try { message = (await res.json()).error || message; } catch (_e) {}
			throw new Error(message);
		}
	};

	const panel = document.createElement("div");
	panel.id = "suite-panel";
	style(panel, {
		position: "fixed", right: "16px", bottom: "16px", zIndex: "50",
		background: "#fff", border: "1px solid #d5dbe3", borderRadius: "10px",
		boxShadow: "0 4px 18px rgba(20,30,50,.15)", padding: "10px",
		font: "12px system-ui, sans-serif", display: "flex",
		flexDirection: "column", gap: "6px", minWidth: "230px",
	});

	const title = document.createElement("strong");
	title.textContent = "editing suite";
	panel.appendChild(title);

	const statusLine = document.createElement("span");
	style(statusLine, { color: "#667", minHeight: "14px" });

	const button = (label) => {
		const b = document.createElement("button");
		b.type = "button";
		b.textContent = label;
		style(b, { cursor: "pointer", padding: "5px 8px", borderRadius: "6px", border: "1px solid #c8cfd9", background: "#f6f8fb" });
		return b;
	};

	const busy = async (label, work) => {
		statusLine.textContent = label + "…";
		try {
			await work();
			statusLine.textContent = "done — reloading";
			window.location.reload(); // ponytail: full reload after server-side frame rewrite; in-place refresh if it ever feels slow
		} catch (err) {
			statusLine.textContent = "failed: " + err.message;
		}
	};

	const normalizeBtn = button("⚖ auto scale + recenter (all frames)");
	normalizeBtn.addEventListener("click", () => busy("normalizing", () => post("op=auto")));
	panel.appendChild(normalizeBtn);

	// Nudge controls — horizontal only: the canonical contract locks the foot
	// baseline to the cell bottom, so vertical drift is fixed by normalize.
	const nudgeRow = document.createElement("div");
	style(nudgeRow, { display: "flex", gap: "4px", alignItems: "center" });
	const frameInput = document.createElement("input");
	frameInput.type = "number";
	frameInput.min = "0";
	frameInput.value = "0";
	frameInput.title = "frame #";
	style(frameInput, { width: "52px", padding: "4px", border: "1px solid #c8cfd9", borderRadius: "6px" });
	const left = button("◀");
	const right = button("▶");
	const nudge = (dx) => busy("nudging frame " + frameInput.value,
		() => post("op=nudge&index=" + encodeURIComponent(frameInput.value) + "&dx=" + dx));
	left.addEventListener("click", (e) => nudge(e.shiftKey ? -10 : -2));
	right.addEventListener("click", (e) => nudge(e.shiftKey ? 10 : 2));
	const nudgeLabel = document.createElement("span");
	nudgeLabel.textContent = "nudge frame";
	nudgeRow.append(nudgeLabel, frameInput, left, right);
	panel.appendChild(nudgeRow);

	// Onion skin — every frame ghosted in one canvas + baseline/centre guides.
	const onionBtn = button("👻 onion skin");
	panel.appendChild(onionBtn);
	panel.appendChild(statusLine);

	const onion = document.createElement("canvas");
	onion.width = CELL;
	onion.height = CELL;
	style(onion, {
		display: "none", width: CELL * 1.5 + "px", height: CELL * 1.5 + "px",
		imageRendering: "pixelated", background:
			"repeating-conic-gradient(#eceff3 0% 25%, #ffffff 0% 50%) 0 0 / 16px 16px",
		border: "1px solid #d5dbe3", borderRadius: "8px",
	});
	panel.insertBefore(onion, statusLine);

	const drawOnion = async () => {
		const run = await (await fetch("/api/run")).json();
		const frames = (((run.states || [])[0] || {}).frames || []).filter((f) => f.present);
		const ctx = onion.getContext("2d");
		ctx.imageSmoothingEnabled = false;
		ctx.clearRect(0, 0, CELL, CELL);
		ctx.globalAlpha = Math.max(0.12, 0.5 / Math.max(1, frames.length / 3));
		await Promise.all(frames.map((f) => new Promise((done) => {
			const img = new Image();
			img.onload = () => { ctx.drawImage(img, 0, 0); done(); };
			img.onerror = done;
			img.src = f.url;
		})));
		ctx.globalAlpha = 1;
		ctx.strokeStyle = "rgba(220,40,60,.85)"; // foot baseline
		ctx.beginPath(); ctx.moveTo(0, CELL - 0.5); ctx.lineTo(CELL, CELL - 0.5); ctx.stroke();
		ctx.strokeStyle = "rgba(40,110,220,.55)"; // horizontal centre
		ctx.beginPath(); ctx.moveTo(CELL / 2 + 0.5, 0); ctx.lineTo(CELL / 2 + 0.5, CELL); ctx.stroke();
	};

	onionBtn.addEventListener("click", () => {
		const show = onion.style.display === "none";
		onion.style.display = show ? "block" : "none";
		localStorage.setItem("suiteOnion", show ? "1" : "0");
		if (show) drawOnion().catch((err) => { statusLine.textContent = "failed: " + err.message; });
	});

	document.body.appendChild(panel);
	if (localStorage.getItem("suiteOnion") === "1") onionBtn.click();
})();

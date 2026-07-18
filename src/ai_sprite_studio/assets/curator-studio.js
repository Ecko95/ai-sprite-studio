// Curator Studio — full-window curation UI over the local /api/run contract.
// Vanilla JS, CSP `default-src 'self'`; talks to the same endpoints as the
// vendored curator but with a timeline / stage / inspector layout.
(() => {
	const $ = (id) => document.getElementById(id);

	// ---- state --------------------------------------------------------
	let run = null;
	let cell = { width: 256, height: 256 };
	let frames = new Map(); // index -> {index, url}
	let order = []; // full display order (all frame indices)
	let hidden = new Set(); // excluded from playback + from `selected` on save
	let deleted = new Set();
	let active = null; // active frame index
	let fps = 12;
	let loop = true;
	let playing = false;
	let play_timer = 0;
	const images = new Map(); // url -> HTMLImageElement

	// Onion layers keyed by neighbour offset (negative = previous, red tint).
	const ONION_OFFSETS = [-3, -2, -1, 1, 2, 3];
	const onion = new Map(ONION_OFFSETS.map((offset) => [offset, {
		on: Math.abs(offset) === 1,
		opacity: Math.abs(offset) === 1 ? 0.35 : 0.18,
	}]));

	// ---- toast + busy -------------------------------------------------
	const toast_el = $("toast");
	let toast_timer = 0;
	const toast = (message, kind) => {
		toast_el.textContent = message;
		toast_el.className = "show" + (kind ? " " + kind : "");
		clearTimeout(toast_timer);
		toast_timer = setTimeout(() => { toast_el.className = ""; }, 4200);
	};

	// Indeterminate overlay with honest elapsed seconds for blocking server work.
	const busy = async (label, work) => {
		const overlay = $("busy");
		const label_el = $("busy-label");
		overlay.hidden = false;
		const started = Date.now();
		label_el.textContent = label + "…";
		const tick = setInterval(() => {
			label_el.textContent = label + "… (" + Math.round((Date.now() - started) / 1000) + "s, still working)";
		}, 500);
		try {
			return await work();
		} finally {
			clearInterval(tick);
			overlay.hidden = true;
		}
	};

	const api_error = async (res) => {
		let message = res.statusText;
		try { message = (await res.json()).error || message; } catch (_e) {}
		return message;
	};

	// ---- image cache --------------------------------------------------
	const img = (url) => {
		if (!images.has(url)) {
			const image = new Image();
			image.src = url;
			image.addEventListener("load", () => draw_stage(), { once: true });
			images.set(url, image);
		}
		return images.get(url);
	};

	// ---- derived lists ------------------------------------------------
	const play_list = () => order.filter((i) => !deleted.has(i) && !hidden.has(i));
	const selected_list = play_list;

	// ---- load run -----------------------------------------------------
	const load_run = async () => {
		const data = await (await fetch("/api/run")).json();
		if (data.error || !data.states || !data.states.length) {
			$("empty-state").hidden = false;
			$("layout").hidden = true;
			return false;
		}
		run = data;
		cell = data.cell || cell;
		const state = data.states[0];
		frames = new Map(state.frames.filter((f) => f.present).map((f) => [f.index, f]));
		const indices = [...frames.keys()];
		const entry = ((data.curation || {}).states || {}).upload || {};
		const stored_order = Array.isArray(entry.order)
			? entry.order.filter((i) => frames.has(i))
			: [];
		order = [...stored_order, ...indices.filter((i) => !stored_order.includes(i))];
		deleted = new Set((entry.deleted || []).filter((i) => frames.has(i)));
		if (Array.isArray(entry.selected)) {
			const sel = new Set(entry.selected);
			hidden = new Set(order.filter((i) => !sel.has(i) && !deleted.has(i)));
		} else {
			hidden = new Set();
		}
		fps = Number(entry.fps) || (state.fps > 1 ? state.fps : 12);
		loop = typeof entry.loop === "boolean" ? entry.loop : !!state.loop;
		$("fps").value = fps;
		$("loop").checked = loop;
		if (active === null || !frames.has(active)) active = play_list()[0] ?? indices[0] ?? null;
		$("stage").width = cell.width;
		$("stage").height = cell.height;
		$("empty-state").hidden = true;
		$("layout").hidden = false;
		render_timeline();
		render_onion_controls();
		draw_stage();
		if (data.hasAtlas && data.atlas) show_atlas(false);
		return true;
	};

	// ---- timeline -----------------------------------------------------
	let drag_index = null;

	const render_timeline = () => {
		const strip = $("timeline-strip");
		strip.textContent = "";
		for (const index of order) {
			const frame = frames.get(index);
			if (!frame) continue;
			const thumb = document.createElement("div");
			thumb.className = "thumb";
			if (index === active) thumb.classList.add("active");
			if (hidden.has(index)) thumb.classList.add("hidden-frame");
			if (deleted.has(index)) thumb.classList.add("deleted");
			thumb.draggable = !deleted.has(index);
			thumb.dataset.index = index;

			const image = document.createElement("img");
			image.src = frame.url;
			image.alt = "frame " + index;
			const badge = document.createElement("span");
			badge.className = "thumb-index";
			badge.textContent = index;
			const buttons = document.createElement("span");
			buttons.className = "thumb-buttons";
			const eye = document.createElement("button");
			eye.type = "button";
			eye.className = "eye-btn";
			eye.textContent = hidden.has(index) ? "🚫" : "👁";
			eye.title = hidden.has(index) ? "show frame" : "hide frame (excluded from playback and save)";
			eye.addEventListener("click", (event) => {
				event.stopPropagation();
				if (hidden.has(index)) hidden.delete(index);
				else hidden.add(index);
				render_timeline();
				draw_stage();
				schedule_save();
			});
			const del = document.createElement("button");
			del.type = "button";
			del.className = "del-btn";
			del.textContent = deleted.has(index) ? "↩" : "🗑";
			del.title = deleted.has(index) ? "restore frame" : "delete frame";
			del.addEventListener("click", (event) => {
				event.stopPropagation();
				if (deleted.has(index)) deleted.delete(index);
				else { deleted.add(index); hidden.delete(index); }
				render_timeline();
				draw_stage();
				schedule_save();
			});
			buttons.append(eye, del);
			thumb.append(image, badge, buttons);

			thumb.addEventListener("click", () => { active = index; render_timeline(); draw_stage(); });
			thumb.addEventListener("dragstart", (event) => {
				drag_index = index;
				event.dataTransfer.effectAllowed = "move";
			});
			thumb.addEventListener("dragover", (event) => {
				event.preventDefault();
				thumb.classList.add("drag-over");
			});
			thumb.addEventListener("dragleave", () => thumb.classList.remove("drag-over"));
			thumb.addEventListener("drop", (event) => {
				event.preventDefault();
				thumb.classList.remove("drag-over");
				if (drag_index === null || drag_index === index) return;
				const from = order.indexOf(drag_index);
				let to = order.indexOf(index);
				order.splice(from, 1);
				if (from < to) to -= 1;
				const rect = thumb.getBoundingClientRect();
				const after = (rect.height >= rect.width)
					? event.clientY > rect.top + rect.height / 2
					: event.clientX > rect.left + rect.width / 2;
				order.splice(to + (after ? 1 : 0), 0, drag_index);
				drag_index = null;
				render_timeline();
				draw_stage();
				schedule_save();
			});
			strip.appendChild(thumb);
		}
		const visible = play_list().length;
		$("frame-count").textContent = visible + "/" + order.length;
	};

	// ---- stage + onion ------------------------------------------------
	const tint_canvas = document.createElement("canvas");

	const draw_tinted = (ctx, image, color, opacity) => {
		if (!image.complete || !image.naturalWidth) return;
		tint_canvas.width = cell.width;
		tint_canvas.height = cell.height;
		const tctx = tint_canvas.getContext("2d");
		tctx.clearRect(0, 0, cell.width, cell.height);
		tctx.drawImage(image, 0, 0, cell.width, cell.height);
		tctx.globalCompositeOperation = "source-in";
		tctx.fillStyle = color;
		tctx.fillRect(0, 0, cell.width, cell.height);
		tctx.globalCompositeOperation = "source-over";
		ctx.globalAlpha = opacity;
		ctx.drawImage(tint_canvas, 0, 0);
		ctx.globalAlpha = 1;
	};

	const draw_stage = () => {
		const canvas = $("stage");
		const ctx = canvas.getContext("2d");
		ctx.imageSmoothingEnabled = false;
		ctx.clearRect(0, 0, canvas.width, canvas.height);
		const list = play_list();
		const position = list.indexOf(active);
		if (position !== -1 && !playing) {
			for (const [offset, layer] of onion) {
				if (!layer.on) continue;
				const neighbour = list[position + offset];
				if (neighbour === undefined) continue;
				const frame = frames.get(neighbour);
				if (!frame) continue;
				draw_tinted(ctx, img(frame.url), offset < 0 ? "#e11d48" : "#16a34a", layer.opacity);
			}
		}
		const frame = active !== null ? frames.get(active) : null;
		if (frame) {
			const image = img(frame.url);
			if (image.complete && image.naturalWidth) ctx.drawImage(image, 0, 0, cell.width, cell.height);
		}
		$("frame-indicator").textContent = position === -1
			? (active === null ? "—" : "#" + active + " (not in playback)")
			: (position + 1) + "/" + list.length + " · #" + active;
	};

	const render_onion_controls = () => {
		const wrap = $("onion-layers");
		wrap.textContent = "";
		for (const offset of ONION_OFFSETS) {
			const layer = onion.get(offset);
			const row = document.createElement("div");
			row.className = "onion-layer " + (offset < 0 ? "prev" : "next");
			const check = document.createElement("input");
			check.type = "checkbox";
			check.checked = layer.on;
			check.addEventListener("change", () => { layer.on = check.checked; draw_stage(); });
			const tag = document.createElement("span");
			tag.className = "onion-tag";
			tag.textContent = offset < 0 ? String(offset) : "+" + offset;
			const range = document.createElement("input");
			range.type = "range";
			range.min = "0";
			range.max = "1";
			range.step = "0.05";
			range.value = layer.opacity;
			range.title = "opacity";
			range.addEventListener("input", () => { layer.opacity = Number(range.value); draw_stage(); });
			row.append(check, tag, range);
			wrap.appendChild(row);
		}
	};

	// ---- playback (real export speed) ---------------------------------
	const step = (direction) => {
		const list = play_list();
		if (!list.length) return;
		const position = Math.max(0, list.indexOf(active));
		active = list[(position + direction + list.length) % list.length];
		render_timeline();
		draw_stage();
	};

	const set_playing = (on) => {
		playing = on;
		$("play-btn").textContent = on ? "⏸" : "▶";
		clearInterval(play_timer);
		if (!on) { draw_stage(); return; }
		play_timer = setInterval(() => {
			const list = play_list();
			if (!list.length) { set_playing(false); return; }
			const position = list.indexOf(active);
			if (position === list.length - 1 && !loop) { set_playing(false); return; }
			active = list[(position + 1) % list.length];
			render_timeline();
			draw_stage();
		}, 1000 / fps);
	};

	$("play-btn").addEventListener("click", () => set_playing(!playing));
	$("step-prev").addEventListener("click", () => { set_playing(false); step(-1); });
	$("step-next").addEventListener("click", () => { set_playing(false); step(1); });

	$("fps").addEventListener("change", () => {
		fps = Math.min(60, Math.max(1, Number($("fps").value) || 12));
		$("fps").value = fps;
		if (playing) set_playing(true); // re-time the interval
		schedule_save();
	});
	$("loop").addEventListener("change", () => { loop = $("loop").checked; schedule_save(); });

	document.addEventListener("keydown", (event) => {
		if (/^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)) return;
		if (event.key === "ArrowLeft") { event.preventDefault(); set_playing(false); step(-1); }
		else if (event.key === "ArrowRight") { event.preventDefault(); set_playing(false); step(1); }
		else if (event.key === " ") { event.preventDefault(); set_playing(!playing); }
	});

	// ---- save curation ------------------------------------------------
	let save_timer = 0;
	const save_status = $("save-status");

	const build_payload = () => ({
		version: run.schemaVersion || 1,
		kind: "sprite-gen-curation",
		runRevision: run.runRevision,
		states: {
			upload: {
				selected: selected_list(),
				order: order.slice(),
				deleted: [...deleted],
				fps,
				loop,
			},
		},
	});

	const save = async () => {
		save_status.textContent = "saving…";
		save_status.className = "save-status";
		try {
			const res = await fetch("/api/curation", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify(build_payload()),
			});
			if (res.status === 409) {
				toast("Run changed elsewhere — reloaded the latest frames", "err");
				await load_run();
				save_status.textContent = "reloaded";
				return;
			}
			if (!res.ok) throw new Error(await api_error(res));
			save_status.textContent = "saved";
			save_status.className = "save-status ok";
		} catch (err) {
			save_status.textContent = "save failed: " + err.message;
			save_status.className = "save-status err";
		}
	};

	const schedule_save = () => {
		save_status.textContent = "editing…";
		clearTimeout(save_timer);
		save_timer = setTimeout(save, 400);
	};

	$("save-btn").addEventListener("click", save);

	// ---- normalize ----------------------------------------------------
	const normalize = (query, label) => busy(label, async () => {
		const res = await fetch("/curator/normalize?" + query, { method: "POST" });
		if (!res.ok) { toast(label + " failed: " + await api_error(res), "err"); return; }
		images.clear(); // frame artifacts were rewritten
		await load_run();
		toast(label + " done", "ok");
	});

	$("auto-normalize").addEventListener("click", () => normalize("op=auto", "Auto normalize"));
	const nudge = (dx) => {
		if (active === null) return;
		normalize("op=nudge&index=" + active + "&dx=" + dx, "Nudge frame " + active);
	};
	$("nudge-left").addEventListener("click", (event) => nudge(event.shiftKey ? -10 : -2));
	$("nudge-right").addEventListener("click", (event) => nudge(event.shiftKey ? 10 : 2));

	// ---- atlas --------------------------------------------------------
	const show_atlas = async (bust) => {
		$("atlas-view").hidden = false;
		$("atlas-img").src = "/run/sprite-sheet-alpha.png" + (bust ? "?t=" + Date.now() : "");
		try {
			const manifest = await (await fetch("/run/manifest.json")).json();
			const info = [];
			if (manifest.cell) info.push("cell " + manifest.cell.width + "×" + manifest.cell.height);
			const state = (manifest.states || [])[0];
			if (state && state.frames) info.push(state.frames.length + " frames");
			$("atlas-meta").textContent = info.join(" · ");
		} catch (_err) { /* manifest is optional context */ }
	};

	$("render-atlas").addEventListener("click", () => busy("Rendering atlas", async () => {
		const res = await fetch("/download/atlas");
		if (!res.ok) { toast("Atlas failed: " + await api_error(res), "err"); return; }
		await show_atlas(true);
		toast("Atlas rendered", "ok");
	}));

	// ---- exports ------------------------------------------------------
	const download = async (path, fallback_name) => {
		const res = await fetch(path);
		if (!res.ok) throw new Error(await api_error(res));
		const blob = await res.blob();
		const link = document.createElement("a");
		link.href = URL.createObjectURL(blob);
		link.download = res.headers.get("X-Filename") || fallback_name;
		link.click();
		URL.revokeObjectURL(link.href);
	};

	const wire_export = (id, label, path_fn, fallback) => {
		$(id).addEventListener("click", () => busy(label, async () => {
			try {
				await download(path_fn(), fallback);
				toast(label + " downloaded", "ok");
			} catch (err) {
				toast(label + " failed: " + err.message, "err");
			}
		}));
	};

	const clamp_scale = (id) => Math.min(8, Math.max(1, Number($(id).value) || 1));
	wire_export("export-gif", "GIF",
		() => "/download/gif?scale=" + clamp_scale("gif-scale") + "&fps=" + fps + "&loop=" + (loop ? 1 : 0),
		"animation.gif");
	wire_export("export-pngs", "PNGs",
		() => "/download/pngs?scale=" + clamp_scale("png-scale"), "frames.zip");
	wire_export("export-atlas", "Atlas", () => "/download/atlas", "sprite-sheet-alpha.png");

	// ---- video library ------------------------------------------------
	const load_videos = async () => {
		const wrap = $("studio-videos");
		try {
			const data = await (await fetch("/curator/videos")).json();
			wrap.textContent = "";
			$("studio-videos-empty").hidden = data.videos.length > 0;
			for (const video of data.videos) {
				const card = document.createElement("div");
				card.className = "video-card";
				const player = document.createElement("video");
				player.controls = true;
				player.preload = "metadata";
				player.src = video.url;
				const meta = document.createElement("div");
				meta.className = "video-meta";
				const name = document.createElement("span");
				name.className = "video-name";
				name.textContent = video.name;
				meta.appendChild(name);
				const actions = document.createElement("div");
				actions.className = "video-actions";
				const extract = document.createElement("button");
				extract.type = "button";
				extract.className = "ghost";
				extract.textContent = "Extract into session";
				extract.addEventListener("click", () => busy("Extracting " + video.name, async () => {
					const count = Math.min(128, Math.max(1, Number($("studio-frames").value) || 96));
					const body = new FormData();
					body.append("video_id", video.id);
					const res = await fetch("/curator/video-upload?frames=" + count, { method: "POST", body });
					if (!res.ok) { toast("Extract failed: " + await api_error(res), "err"); return; }
					active = null;
					images.clear();
					await load_run();
					toast("Extracted " + video.name + " into the session", "ok");
				}));
				actions.appendChild(extract);
				card.append(player, meta, actions);
				wrap.appendChild(card);
			}
		} catch (_err) {
			$("studio-videos-empty").hidden = false;
		}
	};

	// ---- init ---------------------------------------------------------
	load_run();
	load_videos();
})();

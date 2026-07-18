// Landing page: mode toggle, Higgsfield guided wizard, video library.
// CSP `default-src 'self'` — no inline handlers; all logic lives here.
(() => {
	const $ = (id) => document.getElementById(id);

	// ---- toast + progress helpers ------------------------------------
	const toast_el = $("toast");
	let toast_timer = 0;
	const toast = (message, kind) => {
		toast_el.textContent = message;
		toast_el.className = "show" + (kind ? " " + kind : "");
		clearTimeout(toast_timer);
		toast_timer = setTimeout(() => { toast_el.className = ""; }, 4200);
	};

	const copy_text = async (text, ok_message) => {
		try {
			await navigator.clipboard.writeText(text);
			toast(ok_message, "ok");
		} catch (_err) {
			toast("Copy failed — select the text and copy manually", "err");
		}
	};

	// ---- mode toggle --------------------------------------------------
	const guided_btn = $("mode-guided");
	const manual_btn = $("mode-manual");
	const set_mode = (mode) => {
		const guided = mode !== "manual";
		guided_btn.setAttribute("aria-pressed", String(guided));
		manual_btn.setAttribute("aria-pressed", String(!guided));
		$("panel-guided").hidden = !guided;
		$("panel-manual").hidden = guided;
		localStorage.setItem("studioMode", guided ? "guided" : "manual");
	};
	guided_btn.addEventListener("click", () => set_mode("guided"));
	manual_btn.addEventListener("click", () => set_mode("manual"));
	set_mode(localStorage.getItem("studioMode") || "guided");

	// ---- wizard rail --------------------------------------------------
	const rail_buttons = [...document.querySelectorAll(".wizard-rail button")];
	const panels = [...document.querySelectorAll("[data-step-panel]")];
	const done = new Set();
	const goto_step = (step) => {
		rail_buttons.forEach((btn) => {
			const n = btn.dataset.step;
			if (n === String(step)) btn.setAttribute("aria-current", "step");
			else btn.removeAttribute("aria-current");
			btn.classList.toggle("done", done.has(Number(n)));
		});
		panels.forEach((panel) => { panel.hidden = panel.dataset.stepPanel !== String(step); });
	};
	rail_buttons.forEach((btn) => btn.addEventListener("click", () => goto_step(btn.dataset.step)));
	document.querySelectorAll("[data-goto]").forEach((btn) =>
		btn.addEventListener("click", () => goto_step(btn.dataset.goto)));

	// ---- step 1: reference prompt ------------------------------------
	const ref_prompt = $("ref-prompt");
	let ref_prompt_dirty = false;
	const compose_reference = () => {
		const desc = $("ref-desc").value.trim();
		const parts = [
			desc || "a game character",
			$("ref-style").value.trim(),
			$("ref-bg").value.trim(),
		].filter(Boolean);
		return parts.join(", ") + ".";
	};
	const sync_reference = () => { if (!ref_prompt_dirty) ref_prompt.value = compose_reference(); };
	["ref-desc", "ref-style"].forEach((id) => $(id).addEventListener("input", sync_reference));
	ref_prompt.addEventListener("input", () => { ref_prompt_dirty = true; });
	sync_reference();

	$("ref-copy").addEventListener("click", () => copy_text(ref_prompt.value, "Reference prompt copied"));

	const show_reference = (blob) => {
		const url = URL.createObjectURL(blob);
		$("ref-image").src = url;
		$("ref-download").href = url;
		$("ref-preview").classList.add("show");
	};

	$("ref-manual-file").addEventListener("change", (event) => {
		const file = event.target.files[0];
		if (file) show_reference(file);
	});

	$("ref-generate").addEventListener("click", async () => {
		const button = $("ref-generate");
		button.disabled = true;
		const progress = $("ref-progress");
		progress.classList.add("show", "indeterminate");
		const started = Date.now();
		const timer = setInterval(() => {
			toast("Generating reference… (" + Math.round((Date.now() - started) / 1000) + "s, still working)");
		}, 2000);
		try {
			const res = await fetch("/curator/reference", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ prompt: ref_prompt.value, size: $("ref-size").value }),
			});
			if (res.ok) {
				show_reference(await res.blob());
				$("ref-fallback").classList.remove("show");
				done.add(1);
				goto_step(1);
				toast("Reference generated", "ok");
			} else {
				let message = res.statusText;
				try { message = (await res.json()).error || message; } catch (_e) {}
				if (message.includes("OPENAI_API_KEY")) {
					$("ref-fallback").classList.add("show");
					toast("No OpenAI key — copy the prompt and generate elsewhere", "err");
				} else {
					toast("Generation failed: " + message, "err");
				}
			}
		} catch (err) {
			toast("Generation failed: " + err, "err");
		} finally {
			clearInterval(timer);
			progress.classList.remove("show", "indeterminate");
			button.disabled = false;
		}
	});

	// ---- step 2: animation prompt ------------------------------------
	const ACTIONS = {
		walk: "a smooth walk cycle animation",
		run: "a fast run cycle animation",
		idle: "a subtle idle animation with gentle breathing motion",
		attack: "a single attack animation returning to rest pose",
	};
	// Locked constraint lines — Higgsfield CDance mini 2.0 needs these verbatim
	// so the extracted frames stay chroma-keyable and consistently framed.
	const CONSTRAINTS = [
		"Keep the exact character from the reference image, unchanged design.",
		"Static locked camera, no camera motion, no zoom, no pan.",
		"The character stays centered in the frame at all times.",
		"Plain solid green background, completely unchanged throughout.",
		"4 second duration, consistent character scale throughout.",
	];
	const anim_prompt = $("anim-prompt");
	let anim_prompt_dirty = false;
	const compose_animation = () => {
		const action = $("anim-action").value;
		const base = action === "custom"
			? ($("anim-custom").value.trim() || "a custom animation")
			: ACTIONS[action];
		const direction = $("anim-direction").value;
		let line = "The character performs " + base;
		if (direction) line += ", " + { left: "facing left", right: "facing right", away: "facing away from the camera" }[direction];
		if ($("anim-loop").checked) line += ", as a perfectly seamless loop (last frame flows into the first)";
		return line + ".\n" + CONSTRAINTS.join("\n");
	};
	const sync_animation = () => { if (!anim_prompt_dirty) anim_prompt.value = compose_animation(); };
	$("anim-action").addEventListener("change", () => {
		$("anim-custom-wrap").hidden = $("anim-action").value !== "custom";
		sync_animation();
	});
	["anim-direction", "anim-loop", "anim-custom"].forEach((id) => {
		$(id).addEventListener("input", sync_animation);
		$(id).addEventListener("change", sync_animation);
	});
	anim_prompt.addEventListener("input", () => { anim_prompt_dirty = true; });
	sync_animation();
	$("anim-copy").addEventListener("click", () => {
		copy_text(anim_prompt.value, "Animation prompt copied — run it in Higgsfield");
		done.add(2);
		goto_step(2);
	});

	// ---- step 3: fetch video ------------------------------------------
	const video_status = $("video-status");
	const video_progress = $("video-progress");
	const video_bar = video_progress.querySelector(".bar");

	// XHR gives real upload progress for file posts (fetch can't, without streams).
	const post_video = (query, on_done) => {
		const file = $("video-file").files[0];
		const url = $("video-url").value.trim();
		if (!file && !url) { toast("Paste a result URL or choose a video file first", "err"); return; }
		const buttons = [$("video-save"), $("video-extract")];
		buttons.forEach((b) => { b.disabled = true; });
		const started = Date.now();
		let uploading = !!file;
		video_progress.classList.add("show");
		video_progress.classList.toggle("indeterminate", !uploading);
		const tick = setInterval(() => {
			const seconds = Math.round((Date.now() - started) / 1000);
			video_status.textContent = (uploading ? "Uploading" : "Working on the server") +
				"… (" + seconds + "s, still working)";
		}, 500);
		const finish = () => {
			clearInterval(tick);
			video_progress.classList.remove("show", "indeterminate");
			video_bar.style.width = "0%";
			buttons.forEach((b) => { b.disabled = false; });
		};

		const xhr = new XMLHttpRequest();
		xhr.open("POST", "/curator/video-upload?" + query);
		xhr.responseType = "json";
		if (file) {
			xhr.upload.addEventListener("progress", (event) => {
				if (!event.lengthComputable) return;
				video_bar.style.width = Math.round((event.loaded / event.total) * 100) + "%";
				if (event.loaded >= event.total) {
					// Upload finished; the server-side decode/extract has no progress
					// signal — switch to an honest indeterminate bar.
					uploading = false;
					video_progress.classList.add("indeterminate");
				}
			});
		}
		xhr.addEventListener("load", () => {
			finish();
			if (xhr.status >= 200 && xhr.status < 300) on_done(xhr.response || {});
			else {
				const message = (xhr.response && xhr.response.error) || xhr.statusText;
				video_status.textContent = "";
				toast("Failed: " + message, "err");
			}
		});
		xhr.addEventListener("error", () => { finish(); toast("Network error", "err"); });

		const body = new FormData();
		if (file) body.append("file", file, file.name);
		else body.append("url", url);
		const name = $("video-name").value.trim();
		if (name) body.append("name", name);
		xhr.send(body);
	};

	const frames_query = () => {
		const frames = Math.min(128, Math.max(1, Number($("video-frames").value) || 96));
		return "frames=" + frames;
	};

	$("video-save").addEventListener("click", () => post_video("save_only=1", () => {
		video_status.textContent = "Saved to the library.";
		toast("Video saved to library", "ok");
		load_library();
	}));

	$("video-extract").addEventListener("click", () => post_video(frames_query(), () => {
		done.add(3);
		video_status.textContent = "Frames extracted — opening Curator Studio…";
		toast("Frames extracted", "ok");
		load_library();
		window.location = "/curator/studio";
	}));

	// ---- video library ------------------------------------------------
	const format_size = (bytes) => {
		if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + " MB";
		if (bytes >= 1024) return Math.round(bytes / 1024) + " KB";
		return bytes + " B";
	};

	const extract_from_library = (video, card) => {
		const buttons = [...card.querySelectorAll("button")];
		buttons.forEach((b) => { b.disabled = true; });
		const started = Date.now();
		const tick = setInterval(() => {
			toast("Extracting from " + video.name + "… (" + Math.round((Date.now() - started) / 1000) + "s)");
		}, 1500);
		const body = new FormData();
		body.append("video_id", video.id);
		fetch("/curator/video-upload?" + frames_query(), { method: "POST", body })
			.then(async (res) => {
				if (!res.ok) throw new Error((await res.json()).error || res.statusText);
				toast("Frames extracted — opening Curator Studio…", "ok");
				window.location = "/curator/studio";
			})
			.catch((err) => toast("Extract failed: " + err.message, "err"))
			.finally(() => { clearInterval(tick); buttons.forEach((b) => { b.disabled = false; }); });
	};

	const load_library = async () => {
		const grid = $("library-grid");
		try {
			const data = await (await fetch("/curator/videos")).json();
			grid.textContent = "";
			$("library-empty").hidden = data.videos.length > 0;
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
				name.title = video.name;
				const size = document.createElement("span");
				size.textContent = format_size(video.size);
				meta.append(name, size);
				const actions = document.createElement("div");
				actions.className = "video-actions";
				const extract = document.createElement("button");
				extract.type = "button";
				extract.className = "primary";
				extract.textContent = "Extract";
				extract.addEventListener("click", () => extract_from_library(video, card));
				const remove = document.createElement("button");
				remove.type = "button";
				remove.className = "ghost";
				remove.textContent = "Delete";
				remove.addEventListener("click", async () => {
					if (!confirm("Delete " + video.name + " from the library?")) return;
					const res = await fetch(video.url, { method: "DELETE" });
					if (res.ok) { toast("Deleted " + video.name, "ok"); load_library(); }
					else toast("Delete failed", "err");
				});
				actions.append(extract, remove);
				card.append(player, meta, actions);
				grid.appendChild(card);
			}
		} catch (_err) {
			grid.textContent = "";
			$("library-empty").hidden = false;
		}
	};
	load_library();
})();

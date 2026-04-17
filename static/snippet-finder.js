/**
 * Floating snippet finder. Self-contained — injects its own DOM on load,
 * binds Cmd/Ctrl+Shift+F + click to open, Esc to close. POSTs to /find.
 *
 * Clicking a result navigates to the investigate page with the matched
 * path + line range pre-filled.
 */
(function () {
  if (window.__sfMounted) return;
  window.__sfMounted = true;

  const tpl = `
    <button class="sf-fab" id="sf-fab" title="Investigate your codebase — paste any snippet (Enter)">
      <span class="sf-icon" aria-hidden="true">⌕</span>
      <span class="sf-label"><b>Investigate your codebase</b> — paste any snippet</span>
      <span class="sf-kbd" aria-label="Press Enter">↵</span>
    </button>
    <div class="sf-overlay" id="sf-overlay" role="dialog" aria-hidden="true">
      <div class="sf-panel" role="document">
        <div class="sf-head">
          <h2>Investigate your codebase</h2>
          <span class="sf-hint">git grep, no LLM</span>
          <button class="sf-close" id="sf-close" aria-label="Close">×</button>
        </div>
        <div class="sf-input">
          <textarea id="sf-text" placeholder="Paste any code from gobroker — even a single line works. Multi-line pastes get an exact line range." spellcheck="false" autocomplete="off"></textarea>
          <div class="sf-row">
            <span class="sf-meta" id="sf-meta">Hint: longer & more unique lines find better matches.</span>
            <button id="sf-go">Find in gobroker</button>
          </div>
        </div>
        <div class="sf-results" id="sf-results"></div>
      </div>
    </div>
  `;
  const wrap = document.createElement("div");
  wrap.innerHTML = tpl;
  document.body.appendChild(wrap);

  const $ = id => document.getElementById(id);
  const fab = $("sf-fab");
  const overlay = $("sf-overlay");
  const closeBtn = $("sf-close");
  const textarea = $("sf-text");
  const goBtn = $("sf-go");
  const results = $("sf-results");
  const meta = $("sf-meta");

  function open() {
    overlay.classList.add("on");
    overlay.setAttribute("aria-hidden", "false");
    setTimeout(() => textarea.focus(), 50);
  }
  function close() {
    overlay.classList.remove("on");
    overlay.setAttribute("aria-hidden", "true");
  }

  fab.addEventListener("click", open);
  closeBtn.addEventListener("click", close);
  overlay.addEventListener("click", e => {
    if (e.target === overlay) close();
  });

  document.addEventListener("keydown", e => {
    // ⇧⌘F (mac) or Shift+Ctrl+F (win/linux) — toggle.
    if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "f") {
      e.preventDefault();
      overlay.classList.contains("on") ? close() : open();
    }
    // Bare Enter opens the bar when nothing else is focused (idle page).
    if (e.key === "Enter" && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey
        && !overlay.classList.contains("on")) {
      const t = e.target;
      const tag = t && t.tagName;
      const editable = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT"
        || (t && t.isContentEditable);
      if (!editable) {
        e.preventDefault();
        open();
      }
    }
    if (e.key === "Escape" && overlay.classList.contains("on")) {
      close();
    }
    // Cmd/Ctrl+Enter inside textarea = submit.
    if (overlay.classList.contains("on") && (e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      runFind();
    }
  });

  textarea.addEventListener("input", () => {
    const lines = textarea.value.split("\n").filter(l => l.trim()).length;
    const chars = textarea.value.length;
    meta.textContent = chars
      ? `${lines} line${lines === 1 ? "" : "s"} · ${chars} chars`
      : "Hint: longer & more unique lines find better matches.";
  });

  goBtn.addEventListener("click", runFind);

  async function runFind() {
    const snippet = textarea.value;
    if (!snippet.trim()) {
      results.innerHTML = `<div class="sf-empty"><span class="sf-emoji">📋</span>Paste a snippet above and click Find.</div>`;
      return;
    }
    goBtn.disabled = true;
    results.innerHTML = `<div class="sf-status"><span class="sf-spin"></span>Searching gobroker for matching code…</div>`;
    try {
      const r = await fetch("/find", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ snippet }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      renderResults(data);
    } catch (e) {
      results.innerHTML = `<div class="sf-empty"><span class="sf-emoji">⚠️</span>Search failed: ${escapeHtml(e.message)}</div>`;
    } finally {
      goBtn.disabled = false;
    }
  }

  function renderResults(data) {
    const matches = data.matches || [];
    if (!matches.length) {
      const note = data.note || "No matches found.";
      results.innerHTML = `<div class="sf-empty"><span class="sf-emoji">🤷</span>${escapeHtml(note)}</div>`;
      return;
    }
    const queried = data.queried && data.queried !== "cached"
      ? `<div class="sf-queried">matched on: ${escapeHtml(truncate(data.queried.trim(), 80))}</div>`
      : "";
    const rows = matches.map(m => {
      const range = m.line_start === m.line_end
        ? `L${m.line_start}`
        : `L${m.line_start}-${m.line_end}`;
      const investigateUrl = `/?path=${encodeURIComponent(m.path)}&range=${encodeURIComponent(rangeFor(m))}`;
      return `
        <a class="sf-result" href="${investigateUrl}">
          <div class="sf-path" title="${escapeHtml(m.path)}">${escapeHtml(m.path)}</div>
          <div class="sf-line">${range}${m.score > 1 ? `  ·  ${m.score} lines confirmed` : ""}</div>
          ${m.preview ? `<div class="sf-preview">${escapeHtml(m.preview)}</div>` : ""}
        </a>`;
    }).join("");
    results.innerHTML = queried + rows;
  }

  function rangeFor(m) {
    if (m.line_start === m.line_end) return String(m.line_start);
    return `${m.line_start}-${m.line_end}`;
  }

  function truncate(s, n) {
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
})();

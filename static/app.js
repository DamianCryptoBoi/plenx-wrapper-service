"use strict";

const $ = (id) => document.getElementById(id);
const LEGACY_STORAGE_KEY = "plenx.accounts.v1";
const SELECTED_KEY = "plenx.selectedAccount.v1";
const SERVER_IDS = {video: "56947b53-029e-4751-bbca-b9de54f5d628", image: "dd597ae5-6ab3-4e76-9914-9da5589e21c1"};
const ACTIVE = new Set(["queued", "pending", "processing", "running", "submitting", "in_progress", "starting"]);
const COMPLETED = new Set(["completed", "succeeded", "success"]);
const jobsByAccount = new Map();
const profilesByAccount = new Map();
const accountIssues = new Map();
const cooldowns = new Map();
const cards = new Map();
let accounts = [];
let selected = "";
let mode = "video";
let statusFilter = "all";
let editingId = null;
let refreshing = false;
let creditsRefreshPending = false;
let submitting = false;
let wrapperPassword = "";
let sessionVersion = 0;
let setupRequired = false;
let authStatusReady = false;

function configureAuthMode(required) {
  setupRequired = required;
  $("login-title").textContent = required ? "Set your wrapper password" : "Unlock your workspace";
  $("login-submit").textContent = required ? "Set password" : "Sign in";
  $("confirm-password-field").hidden = !required;
  $("confirm-password").disabled = !required;
  $("confirm-password").required = required;
  $("wrapper-password").autocomplete = required ? "new-password" : "current-password";
  $("wrapper-password").placeholder = required ? "Choose a wrapper password" : "Your wrapper password";
  $("login-help").textContent = required
    ? "First visit: create a wrapper password to protect this service. Use printable ASCII without spaces. It is saved privately on the server; Plenx tokens stay on the server too."
    : "Use the password configured on your server. Plenx account tokens stay on the server. This password stays in page memory until you log out or close/reload the page.";
  if (!required) $("confirm-password").value = "";
}

async function loadAuthStatus() {
  const response = await fetch("/auth/status", {cache: "no-store"});
  if (!response.ok) throw new Error("Could not check password setup. Try again.");
  const data = await response.json();
  if (typeof data.setup_required !== "boolean") throw new Error("Service returned an invalid password setup status.");
  configureAuthMode(data.setup_required);
  authStatusReady = true;
}

function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
  $("notice").hidden = !message;
}

try {
  selected = localStorage.getItem(SELECTED_KEY) || "";
} catch {
  // Selection is optional; credentials never need browser storage.
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}

function selectAccount(id) {
  selected = id;
  try { localStorage.setItem(SELECTED_KEY, id); } catch { /* Only a nonsecret account ID is remembered. */ }
  renderAccounts();
}

async function loadAccounts() {
  const version = sessionVersion;
  const data = await api(null, "/api/accounts");
  if (version !== sessionVersion) throw new Error("Session changed; sign in again.");
  accounts = data.accounts;
  if (!accounts.some(a => a.id === selected)) selected = accounts[0]?.id || "";
  selectAccount(selected);
  renderRequests();
}

function lockDashboard() {
  sessionVersion += 1;
  wrapperPassword = "";
  accounts = [];
  jobsByAccount.clear();
  profilesByAccount.clear();
  accountIssues.clear();
  cooldowns.clear();
  $("account-dialog").close();
  $("wrapper-password").value = "";
  $("confirm-password").value = "";
  $("logout").hidden = true;
  $("add-account").disabled = true;
  renderAccounts();
  renderRequests();
  if (!$("login-dialog").open) $("login-dialog").showModal();
}

async function migrateLegacyAccounts() {
  const raw = localStorage.getItem(LEGACY_STORAGE_KEY);
  if (!raw) return;
  let remaining = JSON.parse(raw);
  if (!Array.isArray(remaining)) throw new Error("Old browser account data could not be read. Check browser storage before removing it.");
  for (const legacy of [...remaining]) {
    const saved = await api(null, "/api/accounts", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: legacy.name, token: legacy.token}),
    });
    if (selected === legacy.id) selected = saved.id;
    remaining = remaining.filter(item => item !== legacy);
    if (remaining.length) localStorage.setItem(LEGACY_STORAGE_KEY, JSON.stringify(remaining));
    else localStorage.removeItem(LEGACY_STORAGE_KEY);
  }
  // Also remove an old empty array, if there were no accounts to migrate.
  if (!remaining.length) localStorage.removeItem(LEGACY_STORAGE_KEY);
  notice("Existing browser accounts moved to the server; browser tokens removed.");
}

$("login-dialog").addEventListener("cancel", event => event.preventDefault());
$("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  $("login-submit").disabled = true;
  $("login-error").hidden = true;
  const password = $("wrapper-password").value;
  try {
    if (!authStatusReady) await loadAuthStatus();
    if (setupRequired) {
      const confirmation = $("confirm-password").value;
      if (password !== confirmation) throw new Error("Passwords do not match.");
      const response = await fetch("/auth/setup", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({password, confirm_password: confirmation}),
      });
      const data = await response.json();
      if (!response.ok) {
        if (response.status === 409) await loadAuthStatus();
        throw new Error(typeof data.detail === "string" ? data.detail : "Could not save the wrapper password.");
      }
      configureAuthMode(false);
    }
    wrapperPassword = password;
    sessionVersion += 1;
    // Verify the wrapper password before sending any old browser credentials.
    await loadAccounts();
    $("wrapper-password").value = "";
    $("login-dialog").close();
    $("logout").hidden = false;
    $("add-account").disabled = false;
    try { await migrateLegacyAccounts(); }
    catch (error) { notice(`Account migration stopped: ${error.message} Unsaved browser credentials were kept for retry.`, true); }
    await loadAccounts();
    await refresh({credits: true});
  } catch (error) {
    lockDashboard();
    $("login-error").textContent = error.message;
    $("login-error").hidden = false;
  } finally {
    $("login-submit").disabled = false;
  }
});
$("logout").addEventListener("click", () => { notice(""); lockDashboard(); });

function renderAccounts() {
  const generationSelect = $("generation-account");
  const filterSelect = $("account-filter");
  const previousFilter = filterSelect.value;
  $("accounts").replaceChildren();
  generationSelect.replaceChildren();
  filterSelect.replaceChildren(new Option("All accounts", ""));
  if (!accounts.length) generationSelect.add(new Option("Add an account first", ""));
  accounts.forEach((account, index) => {
    const item = node("div", "account-item");
    const choose = node("button", "account-choose");
    choose.type = "button";
    choose.setAttribute("aria-pressed", String(account.id === selected));
    choose.setAttribute("aria-label", `Select ${account.name}`);
    choose.append(node("span", "account-avatar", String(index + 1).padStart(2, "0")));
    const copy = node("span", "account-copy");
    copy.append(node("span", "account-name", account.name));
    const profile = profilesByAccount.get(account.id);
    const balance = profile?.data ? `${formatBalance(profile.data.credit_balance)} credits · ${formatBalance(profile.data.free_credits)} free` : "Checking balance…";
    copy.append(node("span", accountIssues.has(account.id) || profile?.error ? "account-detail account-problem" : "account-detail", profile?.error ? "Balance unavailable" : accountIssues.has(account.id) ? "Needs attention" : balance));
    choose.append(copy);
    choose.addEventListener("click", () => selectAccount(account.id));
    const edit = node("button", "account-edit", "⋯");
    edit.setAttribute("aria-label", `Edit ${account.name}`);
    edit.addEventListener("click", () => openAccount(account));
    item.append(choose, edit);
    $("accounts").append(item);
    generationSelect.add(new Option(account.name, account.id));
    filterSelect.add(new Option(account.name, account.id));
  });
  generationSelect.value = selected;
  if (accounts.some(a => a.id === previousFilter)) filterSelect.value = previousFilter;
  $("account-count").textContent = accounts.length;
  $("account-empty").hidden = accounts.length > 0;
  $("generate").disabled = !accounts.length || submitting;
  renderAccountSummary();
}

function formatBalance(value) {
  return new Intl.NumberFormat(undefined, {maximumFractionDigits: 6}).format(value);
}

function renderAccountSummary() {
  const summary = $("account-summary");
  const account = accounts.find(a => a.id === selected);
  summary.hidden = !account;
  if (!account) return;
  const entry = profilesByAccount.get(account.id);
  const signature = JSON.stringify({id: account.id, entry});
  if (summary.dataset.signature === signature) return;
  summary.dataset.signature = signature;
  summary.replaceChildren();
  if (!entry?.data) {
    summary.append(node("p", entry?.error ? "small error-text" : "small muted", entry?.error ? `Balances unavailable: ${entry.error}` : "Checking this account’s balances…"));
    return;
  }
  const profile = entry.data;
  const balances = node("div", "balance-grid");
  for (const [label, key] of [["Credits", "credit_balance"], ["Free credits", "free_credits"], ["Rubies", "ruby_balance"]]) {
    const item = node("div", "balance-item");
    item.append(node("span", "balance-label", label), node("strong", "balance-value", formatBalance(profile[key])));
    balances.append(item);
  }
  summary.append(balances);
  const details = [];
  if (typeof profile.plan_name === "string") details.push(profile.plan_name);
  if (Number.isInteger(profile.daily_image_used) && Number.isInteger(profile.daily_image_limit)) {
    details.push(`Daily images: ${formatBalance(profile.daily_image_used)} / ${formatBalance(profile.daily_image_limit)} used (${formatBalance(Math.max(0, profile.daily_image_limit - profile.daily_image_used))} left)`);
  }
  if (details.length) summary.append(node("p", "small muted balance-note", details.join(" · ")));
  summary.append(node("p", "small muted balance-note", `Checked ${new Date(entry.checkedAt).toLocaleTimeString()}`));
  if (entry.error) summary.append(node("p", "small error-text balance-note", `Refresh failed; showing previous balances. ${entry.error}`));
}

async function refreshProfile(account) {
  const previous = profilesByAccount.get(account.id);
  try {
    const data = await api(account, "/api/v1/users/me");
    if (!accounts.some(a => a.id === account.id && a.updated_at === account.updated_at)) return;
    profilesByAccount.set(account.id, {data, checkedAt: Date.now(), lastAttempt: Date.now()});
    if (accountIssues.get(account.id)?.auth) accountIssues.delete(account.id);
  } catch (error) {
    if (!accounts.some(a => a.id === account.id && a.updated_at === account.updated_at)) return;
    profilesByAccount.set(account.id, {...previous, lastAttempt: Date.now(), error: error.message});
    if (error.status === 401 || error.status === 403) accountIssues.set(account.id, {message: error.message, auth: true});
  }
}

function openAccount(account = null) {
  editingId = account?.id || null;
  $("account-dialog-title").textContent = account ? "Edit account" : "Add account";
  $("account-name").value = account?.name || "";
  $("account-token").value = "";
  $("account-token").required = !account;
  $("account-token").placeholder = account ? "Leave blank to keep current token" : "Paste token or Bearer …";
  $("token-label").textContent = account ? "New Plenx token / optional" : "Plenx auth token";
  $("remove-account").hidden = !account;
  $("account-error").hidden = true;
  $("account-dialog").showModal();
  $("account-name").focus();
}

$("add-account").addEventListener("click", () => openAccount());
$("close-account").addEventListener("click", () => $("account-dialog").close());
$("account-dialog").addEventListener("close", () => { $("account-token").value = ""; });
$("account-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const existing = accounts.find(a => a.id === editingId);
  const token = $("account-token").value.trim().replace(/^Bearer\s+/i, "");
  const name = $("account-name").value.trim();
  try {
    if (!name) throw new Error("Enter an account name.");
    if ((!existing && !token) || /\s/.test(token)) throw new Error("Paste a valid Plenx token without spaces or line breaks.");
    const body = {name};
    if (token) body.token = token;
    const account = await api(null, existing ? `/api/accounts/${editingId}` : "/api/accounts", {
      method: existing ? "PATCH" : "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    });
    jobsByAccount.delete(account.id);
    profilesByAccount.delete(account.id);
    accountIssues.delete(account.id);
    cooldowns.delete(account.id);
    selected = account.id;
    await loadAccounts();
    $("account-dialog").close();
    notice("Account saved on the server. Its Plenx token is never returned to this browser.");
    await refresh({credits: true});
  } catch (error) {
    $("account-error").textContent = error.message;
    $("account-error").hidden = false;
  }
});
$("remove-account").addEventListener("click", async () => {
  try {
    await api(null, `/api/accounts/${editingId}`, {method: "DELETE"});
    jobsByAccount.delete(editingId);
    profilesByAccount.delete(editingId);
    accountIssues.delete(editingId);
    cooldowns.delete(editingId);
    $("account-dialog").close();
    await loadAccounts();
    notice("Account removed from the server. Add the same token again to recover its request history.");
  } catch (error) {
    $("account-error").textContent = error.message;
    $("account-error").hidden = false;
  }
});
$("generation-account").addEventListener("change", event => selectAccount(event.target.value));

function setMode(next) {
  // Preserve each media type's server setting when switching the composer.
  SERVER_IDS[mode] = $("server-id").value || SERVER_IDS[mode];
  mode = next;
  for (const media of ["video", "image"]) {
    $("mode-" + media).setAttribute("aria-pressed", String(mode === media));
    const section = $(media + "-fields");
    section.hidden = mode !== media;
    section.querySelectorAll("input,select,textarea").forEach(input => { input.disabled = mode !== media; });
  }
  $("image-advanced").hidden = mode !== "image";
  $("image-advanced").querySelectorAll("input,select,textarea").forEach(input => { input.disabled = mode !== "image"; });
  $("server-id").value = SERVER_IDS[mode];
  $("generate").replaceChildren(document.createTextNode(`Generate ${mode} `), node("span", "", "↗"));
  $("generation-hint").textContent = `${mode === "video" ? "Video · omni-flash" : "Image · gem-pix-2"} · submitted with your selected account`;
}
$("mode-video").addEventListener("click", () => setMode("video"));
$("mode-image").addEventListener("click", () => setMode("image"));

async function api(account, path, options = {}) {
  const password = wrapperPassword;
  const version = sessionVersion;
  const headers = {...options.headers, Authorization: `Bearer ${password}`};
  if (account) headers["X-Account-ID"] = account.id;
  const response = await fetch(path, {...options, headers});
  if (version !== sessionVersion || password !== wrapperPassword) throw new Error("Session changed; sign in again.");
  if (response.status === 204) return null;
  let data;
  try { data = await response.json(); } catch { throw new Error(`Service returned an invalid response (${response.status}).`); }
  if (version !== sessionVersion || password !== wrapperPassword) throw new Error("Session changed; sign in again.");
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map(d => `${d.loc.slice(1).join(".")}: ${d.msg}`).join("; ") : data.detail;
    const error = new Error(typeof detail === "string" ? detail : `Request failed (${response.status}).`);
    error.status = response.status;
    if (response.status === 401 && !response.headers.get("x-plenx-status")) lockDashboard();
    if (response.status === 429 && account) {
      const retry = response.headers.get("retry-after");
      const delay = retry && /^\d+$/.test(retry) ? Number(retry) * 1000 : Date.parse(retry || "") - Date.now();
      cooldowns.set(account.id, Date.now() + (Number.isFinite(delay) && delay > 0 ? delay : 30000));
    }
    throw error;
  }
  return data;
}

$("generation-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (submitting) return;
  const account = accounts.find(a => a.id === selected);
  if (!account) return notice("Add an account first.", true);
  const prompt = $("prompt").value.trim();
  if (!prompt) return notice("Enter a prompt.", true);
  const submittedMode = mode;
  submitting = true;
  $("generate").disabled = true;
  $("generate").textContent = "Submitting…";
  notice("");
  try {
    let path, options;
    if (submittedMode === "video") {
      path = "/api/v1/veo/generate";
      const body = new FormData();
      Object.entries({prompt, model: $("video-model").value.trim(), aspect_ratio: $("video-aspect").value.trim(), duration: $("video-duration").value, resolution: $("video-resolution").value.trim(), server_id: $("server-id").value.trim()}).forEach(([key, value]) => body.append(key, value));
      if ($("workplace-id").value.trim()) body.append("workplace_id", $("workplace-id").value.trim());
      options = {method: "POST", body};
    } else {
      path = "/api/v1/queue/submit";
      const references = $("image-references").value.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
      if (references.length > 20) throw new Error("Use at most 20 reference URLs.");
      const body = {task_type: "image/gem-pix-2", payload: {
        prompt, aspect_ratio: $("image-aspect").value.trim(), num_outputs: Number($("image-count").value),
        guidance_scale: Number($("image-guidance").value), num_inference_steps: Number($("image-steps").value),
        output_format: $("image-format").value.trim(), go_fast: $("image-fast").checked,
        resolution: $("image-resolution").value.trim(), resolution_tier: $("image-tier").value.trim(),
        server_id: $("server-id").value.trim(), uses_credits: $("image-credits").checked, references_urls: references,
      }};
      if ($("workplace-id").value.trim()) body.workplace_id = $("workplace-id").value.trim();
      options = {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
    }
    const data = await api(account, path, options);
    notice(`${submittedMode === "video" ? "Video" : "Image"} ${data.status} with ${account.name}. ${data.message || ""}`);
  } catch (error) {
    notice(error.message, true);
  } finally {
    submitting = false;
    setMode(mode);
    $("generate").disabled = !accounts.length;
    await refresh({credits: true});
  }
});

function mediaUrls(job) {
  const response = job.response || {};
  const result = response.result || {};
  const candidates = job.kind === "video" ? [response.result_url] : [...(Array.isArray(result.image_urls) ? result.image_urls : []), result.result_url];
  return [...new Set(candidates.filter(url => {
    if (typeof url !== "string") return false;
    try { return new URL(url).protocol === "https:"; } catch { return false; }
  }))];
}

function renderJob(job) {
  const card = node("article", "request-card");
  card.append(node("span", "media-icon", job.kind === "video" ? "▷" : "▧"));
  const content = node("div", "job-content");
  content.append(node("p", "job-prompt", job.prompt));
  const parameters = job.kind === "video" ? job.parameters : job.parameters.payload;
  const meta = node("div", "job-meta");
  [job.accountName, job.kind === "video" ? parameters.model : "gem-pix-2", parameters.aspect_ratio, job.kind === "video" ? `${parameters.duration}s / ${parameters.resolution}p` : parameters.resolution, new Date(job.created_at).toLocaleString()].forEach(text => meta.append(node("span", "", text)));
  content.append(meta);
  if (job.task_id) content.append(node("p", "job-meta job-id", job.task_id));
  if (ACTIVE.has(job.status)) {
    const progress = job.response?.progress;
    const message = job.response?.current_step || job.response?.message;
    const position = job.response?.position;
    const pieces = [typeof progress === "number" ? `${progress}%` : null, position ? `Queue #${position}` : null, message];
    const text = pieces.filter(Boolean).join(" · ");
    if (text) content.append(node("p", "progress-text", text));
  }
  const error = job.error || job.poll_error || job.response?.error_message || job.response?.error;
  if (error) content.append(node("p", "job-error", typeof error === "string" ? error : JSON.stringify(error)));
  const side = node("div", "job-side");
  // Status text is untrusted; class names are restricted to known values.
  const statusClass = ACTIVE.has(job.status) ? "processing" : COMPLETED.has(job.status) ? "completed" : "failed";
  side.append(node("span", `badge ${statusClass}`, job.status.replaceAll("_", " ")));
  const urls = mediaUrls(job);
  if (urls.length) {
    const previews = node("div", "job-media");
    urls.forEach((url, index) => {
      const preview = node(job.kind === "video" ? "video" : "img");
      preview.src = url;
      if (job.kind === "video") { preview.controls = true; preview.preload = "none"; }
      else { preview.alt = job.prompt; preview.loading = "lazy"; preview.referrerPolicy = "no-referrer"; }
      previews.append(preview);
      const link = node("a", "result-link", `Open ${job.kind}${urls.length > 1 ? ` ${index + 1}` : ""} ↗`);
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      side.append(link);
    });
    content.append(previews);
  }
  const details = node("details", "job-details");
  details.append(node("summary", "", "Request & response"), node("pre", "", JSON.stringify({request: job.parameters, response: job.response}, null, 2)));
  content.append(details);
  card.append(content, side);
  return card;
}

function renderRequests() {
  const all = accounts.flatMap(account => (jobsByAccount.get(account.id) || []).map(job => ({...job, accountId: account.id, accountName: account.name}))).sort((a, b) => b.created_at.localeCompare(a.created_at));
  $("request-count").textContent = all.length;
  $("active-count").textContent = all.filter(job => ACTIVE.has(job.status)).length;
  const shown = all.filter(job => (!$("account-filter").value || job.accountId === $("account-filter").value) && (statusFilter === "all" || (statusFilter === "active" ? ACTIVE.has(job.status) : statusFilter === "completed" ? COMPLETED.has(job.status) : !ACTIVE.has(job.status) && !COMPLETED.has(job.status))));
  const visibleKeys = new Set();
  shown.forEach((job, index) => {
    const key = `${job.accountId}:${job.id}`;
    visibleKeys.add(key);
    const signature = JSON.stringify(job);
    let saved = cards.get(key);
    if (!saved || saved.signature !== signature) {
      const element = renderJob(job);
      if (saved) saved.element.replaceWith(element);
      saved = {element, signature};
      cards.set(key, saved);
    }
    const atIndex = $("request-list").children[index];
    if (atIndex !== saved.element) $("request-list").insertBefore(saved.element, atIndex || null);
  });
  for (const [key, saved] of cards) {
    if (!visibleKeys.has(key)) { saved.element.remove(); cards.delete(key); }
  }
  $("empty-requests").hidden = shown.length > 0;
  $("empty-requests").querySelector("h3").textContent = all.length ? "Nothing in this view." : "Make something.";
}

async function refresh({credits = false} = {}) {
  if (!wrapperPassword) return;
  if (refreshing) {
    if (credits) creditsRefreshPending = true;
    return;
  }
  refreshing = true;
  $("refresh").disabled = true;
  try {
    await Promise.all(accounts.map(async account => {
      try {
        const first = await api(account, "/api/requests");
        if (!accounts.some(a => a.id === account.id && a.updated_at === account.updated_at)) return;
        jobsByAccount.set(account.id, first.requests);
        const active = first.requests.filter(job => job.task_id && ACTIVE.has(job.status));
        if ((cooldowns.get(account.id) || 0) > Date.now()) return;
        const profile = profilesByAccount.get(account.id);
        if (credits || !profile || Date.now() - profile.lastAttempt >= 60000) await refreshProfile(account);
        if (accountIssues.get(account.id)?.auth || (cooldowns.get(account.id) || 0) > Date.now()) return;
        let finished = false;
        // Bound concurrency for accounts with many active requests.
        for (let offset = 0; offset < active.length; offset += 4) {
          const results = await Promise.all(active.slice(offset, offset + 4).map(async job => {
            try {
              const data = await api(account, `/api/v1/${job.kind === "video" ? "video" : "queue"}/status/${encodeURIComponent(job.task_id)}`);
              if (!ACTIVE.has(data.status.toLowerCase())) finished = true;
              return null;
            } catch (error) { return error; }
          }));
          const error = results.find(Boolean);
          if (error) {
            accountIssues.set(account.id, {message: error.message, auth: error.status === 401 || error.status === 403});
            if (error.status === 401 || error.status === 403 || error.status === 429) break;
          } else accountIssues.delete(account.id);
        }
        if (active.length) {
          const latest = await api(account, "/api/requests");
          if (accounts.some(a => a.id === account.id && a.updated_at === account.updated_at)) jobsByAccount.set(account.id, latest.requests);
        }
        if (finished && (cooldowns.get(account.id) || 0) <= Date.now()) await refreshProfile(account);
      } catch (error) {
        if (!wrapperPassword) return;
        accountIssues.set(account.id, {message: error.message});
        notice(`${account.name}: ${error.message}`, true);
      }
    }));
    renderAccounts();
    renderRequests();
  } finally {
    refreshing = false;
    $("refresh").disabled = false;
    if (creditsRefreshPending) {
      creditsRefreshPending = false;
      await refresh({credits: true});
    }
  }
}

$("refresh").addEventListener("click", () => refresh({credits: true}));
$("account-filter").addEventListener("change", renderRequests);
$("status-filters").addEventListener("click", event => {
  const button = event.target.closest("button[data-filter]");
  if (!button) return;
  statusFilter = button.dataset.filter;
  $("status-filters").querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b === button)));
  renderRequests();
});
async function tick() {
  try { if ($("auto-poll").checked && !document.hidden) await refresh(); }
  finally { setTimeout(tick, 5000); }
}

$("server-id").value = SERVER_IDS.video;
setMode("video");
renderAccounts();
renderRequests();
lockDashboard();
loadAuthStatus().catch(error => {
  $("login-error").textContent = error.message;
  $("login-error").hidden = false;
}).finally(() => { $("login-submit").disabled = false; });
setTimeout(tick, 5000);

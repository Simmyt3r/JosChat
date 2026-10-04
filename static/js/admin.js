/**
 * static/js/admin.js
 * -------------------
 * Client for templates/admin.html. Shares the same login session as the
 * main app (sessionStorage key "joschat_session"), so an admin who is
 * already signed in on this device doesn't have to log in twice — but this
 * page is only a convenience shell: every real check happens server-side,
 * because @require_admin on each /api/admin/* route is what actually gates
 * access (see routes/admin.py). An account that isn't an admin can load
 * this HTML fine; it just can't get any of the API calls below to succeed.
 */

const API_BASE = "/api";
const SESSION_KEY = "joschat_session";
const THEME_KEY = "joschat_theme";

let accessToken = null;
let refreshToken = null;
let refreshInFlight = null;
let allUsers = [];
let confirmingUserId = null;

const $ = (id) => document.getElementById(id);

// --- Theme (shared key with the main app) ----------------------------------

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem(THEME_KEY, theme); } catch { /* ignore */ }
  $("theme-color").content = theme === "light" ? "#f2f5fb" : "#0d1322";
  const btn = $("admin-theme-btn");
  const use = btn.querySelector("use");
  const label = theme === "light" ? "Switch to dark theme" : "Switch to light theme";
  use.setAttribute("href", theme === "light" ? "#i-moon" : "#i-sun");
  btn.setAttribute("aria-label", label);
  btn.title = label;
}

function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
}

// --- Toasts ------------------------------------------------------------------

function toast(message, kind = "info") {
  const box = $("admin-toasts");
  const el = document.createElement("div");
  el.className = `toast${kind === "error" ? " is-error" : kind === "success" ? " is-success" : ""}`;
  if (kind === "error") el.setAttribute("role", "alert");
  if (kind !== "info") {
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("class", "icon");
    icon.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", kind === "error" ? "#i-alert" : "#i-check");
    icon.appendChild(use);
    el.appendChild(icon);
  }
  const text = document.createElement("span");
  text.textContent = message;
  el.appendChild(text);
  const close = document.createElement("button");
  close.type = "button";
  close.className = "icon-btn";
  close.setAttribute("aria-label", "Dismiss");
  close.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-x" /></svg>';
  close.addEventListener("click", () => el.remove());
  el.appendChild(close);
  box.appendChild(el);
  while (box.children.length > 3) box.firstElementChild.remove();
  setTimeout(() => el.remove(), kind === "error" ? 6500 : 3800);
}

// --- Session / auth ------------------------------------------------------

function saveSession() {
  try { sessionStorage.setItem(SESSION_KEY, JSON.stringify({ accessToken, refreshToken })); } catch { /* ignore */ }
}

function loadSession() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(SESSION_KEY) || "null");
    if (saved && saved.accessToken) {
      accessToken = saved.accessToken;
      refreshToken = saved.refreshToken || null;
      return true;
    }
  } catch { /* ignore */ }
  return false;
}

function refreshSession() {
  if (!refreshToken) return Promise.resolve(false);
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = (async () => {
    try {
      const res = await fetch(`${API_BASE}/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
      if (!res.ok) return false;
      const data = await res.json();
      accessToken = data.access_token;
      refreshToken = data.refresh_token || refreshToken;
      saveSession();
      return true;
    } catch {
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}

/** fetch() against the API with the bearer token attached, retrying once on 401. */
async function apiFetch(path, options = {}) {
  const send = () => fetch(`${API_BASE}${path}`, {
    ...options,
    headers: { ...(options.headers || {}), Authorization: `Bearer ${accessToken}` },
  });
  let res;
  try {
    res = await send();
  } catch {
    toast("Can't reach Joschat. Check your connection.", "error");
    return null;
  }
  if (res.status === 401 && await refreshSession()) {
    try { res = await send(); } catch { return null; }
  }
  return res;
}

function showGuard(title, message) {
  $("admin-boot").hidden = true;
  $("admin-shell").hidden = true;
  $("admin-guard").hidden = false;
  $("admin-guard-title").textContent = title;
  $("admin-guard-message").textContent = message;
}

function showShell() {
  $("admin-boot").hidden = true;
  $("admin-guard").hidden = true;
  $("admin-shell").hidden = false;
}

async function boot() {
  applyTheme(document.documentElement.dataset.theme || "dark");

  if (!loadSession()) {
    showGuard("Sign in first", "Log in to Joschat, then come back to this page.");
    return;
  }

  const res = await apiFetch("/auth/me");
  if (!res) {
    showGuard("Can't check access", "Joschat couldn't be reached to confirm you're an admin.");
    return;
  }
  if (res.status === 401) {
    showGuard("Session expired", "Log in to Joschat again, then come back to this page.");
    return;
  }
  if (res.status === 404) {
    showGuard("Finish setting up your account", "You're signed in but haven't finished creating your Joschat profile yet.");
    return;
  }
  if (!res.ok) {
    showGuard("Can't check access", "Something went wrong confirming your account.");
    return;
  }

  const data = await res.json();
  const profile = data.profile;
  if (!profile || profile.role !== "admin") {
    showGuard("Admins only", "You're signed in, but this account doesn't have admin privileges.");
    return;
  }

  window.__adminProfileId = profile.id;
  $("admin-whoami").textContent = `Signed in as @${profile.username}`;
  showShell();
  loadStats();
  loadConversations();
  loadUsers();
}

async function signOut() {
  const token = accessToken;
  try { sessionStorage.removeItem(SESSION_KEY); } catch { /* ignore */ }
  if (token) {
    fetch(`${API_BASE}/auth/logout`, { method: "POST", headers: { Authorization: `Bearer ${token}` } }).catch(() => {});
  }
  window.location.href = "/";
}

// --- Stats -----------------------------------------------------------------

const STAT_FIELDS = [
  ["total_users", "Users"],
  ["total_conversations", "Conversations"],
  ["total_messages", "Messages"],
  ["total_blocks", "Blocks"],
  ["total_calls", "Calls"],
];

async function loadStats() {
  const res = await apiFetch("/admin/stats");
  if (!res || !res.ok) {
    toast("Couldn't load platform stats.", "error");
    return;
  }
  const stats = await res.json();
  const grid = $("stat-grid");
  grid.replaceChildren();
  for (const [key, label] of STAT_FIELDS) {
    const card = document.createElement("div");
    card.className = "stat-card";
    if (key === "total_users" && stats.suspended_users > 0) card.classList.add("is-warning");
    const value = document.createElement("span");
    value.className = "stat-value";
    value.textContent = stats[key] ?? 0;
    const lbl = document.createElement("span");
    lbl.className = "stat-label";
    lbl.textContent = key === "total_users" && stats.suspended_users > 0
      ? `${label} (${stats.suspended_users} suspended)`
      : label;
    card.append(value, lbl);
    grid.appendChild(card);
  }

  const byStatus = stats.calls_by_status || {};
  const parts = Object.entries(byStatus).map(([status, n]) => `${n} ${status}`);
  $("calls-by-status").textContent = parts.length
    ? `Calls by outcome: ${parts.join(", ")}`
    : "No calls yet.";
}

// --- Blockchain audits -------------------------------------------------------

function renderAuditResult(el, { good, lines }) {
  el.hidden = false;
  el.classList.toggle("is-good", good);
  el.classList.toggle("is-bad", !good);
  el.replaceChildren();
  for (const line of lines) {
    const p = document.createElement("p");
    p.style.margin = "0 0 0.3rem";
    p.textContent = line;
    el.appendChild(p);
  }
  if (el.lastElementChild) el.lastElementChild.style.margin = "0";
}

async function validateChain() {
  const btn = $("validate-chain-btn");
  btn.disabled = true;
  try {
    const res = await apiFetch("/admin/blockchain/validate");
    if (!res || !res.ok) {
      toast("Couldn't run the chain audit.", "error");
      return;
    }
    const report = await res.json();
    renderAuditResult($("chain-audit-result"), {
      good: report.is_valid,
      lines: report.is_valid
        ? [`All ${report.blocks_checked} blocks verified. The chain is intact.`]
        : [`Tampering detected at block ${report.invalid_index}.`, `${report.blocks_checked} blocks checked.`],
    });
  } finally {
    btn.disabled = false;
  }
}

async function validateConversation(idOverride) {
  const input = $("conversation-audit-input");
  const id = idOverride ?? input.value;
  if (!id) {
    toast("Enter a conversation ID first.", "error");
    return;
  }
  if (idOverride) {
    input.value = idOverride;
    input.scrollIntoView({ behavior: "smooth", block: "center" });
  }
  const btn = $("validate-conversation-btn");
  btn.disabled = true;
  try {
    const res = await apiFetch(`/admin/conversations/${encodeURIComponent(id)}/validate`);
    if (res && res.status === 404) {
      toast(`Conversation ${id} doesn't exist.`, "error");
      $("conversation-audit-result").hidden = true;
      return;
    }
    if (!res || !res.ok) {
      toast("Couldn't run that audit.", "error");
      return;
    }
    const report = await res.json();
    renderAuditResult($("conversation-audit-result"), {
      good: report.all_verified,
      lines: report.all_verified
        ? [`All ${report.messages_checked} messages in conversation ${report.conversation_id} verified.`]
        : [
            `${report.flagged_message_ids.length} of ${report.messages_checked} messages failed verification in conversation ${report.conversation_id}.`,
            `Flagged message IDs: ${report.flagged_message_ids.join(", ")}`,
          ],
    });
  } finally {
    btn.disabled = false;
  }
}

// --- Conversations ------------------------------------------------------

async function loadConversations() {
  const tbody = $("conversations-tbody");
  const res = await apiFetch("/admin/conversations");
  if (!res || !res.ok) {
    tbody.innerHTML = '<tr><td colspan="6" class="admin-table-empty">Couldn\'t load conversations.</td></tr>';
    return;
  }
  const { conversations } = await res.json();
  if (!conversations.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="admin-table-empty">No conversations yet.</td></tr>';
    return;
  }
  tbody.replaceChildren();
  for (const c of conversations) {
    const tr = document.createElement("tr");

    const idTd = document.createElement("td");
    idTd.textContent = c.id;

    const participantsTd = document.createElement("td");
    participantsTd.textContent = (c.participants || []).map((u) => `@${u}`).join(", ") || "—";

    const typeTd = document.createElement("td");
    typeTd.textContent = c.is_group ? "Group" : "Direct";

    const countTd = document.createElement("td");
    countTd.textContent = c.message_count;

    const startedTd = document.createElement("td");
    startedTd.textContent = c.created_at ? new Date(c.created_at).toLocaleString() : "—";

    const actionTd = document.createElement("td");
    const auditBtn = document.createElement("button");
    auditBtn.type = "button";
    auditBtn.className = "audit-link-btn";
    auditBtn.textContent = "Audit";
    auditBtn.addEventListener("click", () => validateConversation(c.id));
    actionTd.appendChild(auditBtn);

    tr.append(idTd, participantsTd, typeTd, countTd, startedTd, actionTd);
    tbody.appendChild(tr);
  }
}

// --- Users -----------------------------------------------------------------

async function loadUsers() {
  const res = await apiFetch("/admin/users");
  if (!res || !res.ok) {
    $("users-tbody").innerHTML = '<tr><td colspan="5" class="admin-table-empty">Couldn\'t load users.</td></tr>';
    return;
  }
  const data = await res.json();
  allUsers = data.users;
  renderUsers();
}

function renderUsers() {
  const tbody = $("users-tbody");
  const query = ($("user-search").value || "").trim().toLowerCase();
  const rows = query ? allUsers.filter((u) => u.username.toLowerCase().includes(query)) : allUsers;

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="admin-table-empty">${query ? "No users match that search." : "No users yet."}</td></tr>`;
    return;
  }

  const myId = (window.__adminProfileId) || null;

  tbody.replaceChildren();
  for (const u of rows) {
    const tr = document.createElement("tr");

    const nameTd = document.createElement("td");
    nameTd.textContent = `@${u.username}`;

    const roleTd = document.createElement("td");
    const roleBadge = document.createElement("span");
    roleBadge.className = `badge badge-role-${u.role}`;
    roleBadge.textContent = u.role;
    roleTd.appendChild(roleBadge);

    const statusTd = document.createElement("td");
    const statusBadge = document.createElement("span");
    statusBadge.className = `badge badge-status-${u.status}`;
    statusBadge.textContent = u.status;
    statusTd.appendChild(statusBadge);

    const joinedTd = document.createElement("td");
    joinedTd.className = "col-joined";
    joinedTd.textContent = u.created_at ? new Date(u.created_at).toLocaleDateString() : "—";

    const actionTd = document.createElement("td");
    if (u.id === myId) {
      const you = document.createElement("span");
      you.className = "row-you";
      you.textContent = "(you)";
      actionTd.appendChild(you);
    } else if (confirmingUserId === u.id) {
      const wrap = document.createElement("span");
      wrap.className = "row-confirm";
      const label = document.createElement("span");
      label.textContent = "Suspend this user?";
      const confirmBtn = document.createElement("button");
      confirmBtn.type = "button";
      confirmBtn.className = "btn btn-decline btn-compact";
      confirmBtn.textContent = "Confirm";
      confirmBtn.addEventListener("click", () => setUserStatus(u.id, "suspend"));
      const cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "btn btn-secondary btn-compact";
      cancelBtn.textContent = "Cancel";
      cancelBtn.addEventListener("click", () => { confirmingUserId = null; renderUsers(); });
      wrap.append(label, confirmBtn, cancelBtn);
      actionTd.appendChild(wrap);
    } else {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-secondary btn-compact row-action";
      if (u.status === "suspended") {
        btn.textContent = "Reinstate";
        btn.addEventListener("click", () => setUserStatus(u.id, "reinstate"));
      } else {
        btn.textContent = "Suspend";
        btn.addEventListener("click", () => { confirmingUserId = u.id; renderUsers(); });
      }
      actionTd.appendChild(btn);
    }

    tr.append(nameTd, roleTd, statusTd, joinedTd, actionTd);
    tbody.appendChild(tr);
  }
}

async function setUserStatus(userId, action) {
  confirmingUserId = null;
  const res = await apiFetch(`/admin/users/${encodeURIComponent(userId)}/${action}`, { method: "POST" });
  if (!res || !res.ok) {
    const body = res ? await res.json().catch(() => ({})) : {};
    toast(body.error || "That didn't work.", "error");
    renderUsers();
    return;
  }
  const { user } = await res.json();
  allUsers = allUsers.map((u) => (u.id === user.id ? user : u));
  toast(action === "suspend" ? `@${user.username} suspended.` : `@${user.username} reinstated.`, "success");
  renderUsers();
  loadStats();
}

// --- Wire up -----------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  $("admin-theme-btn").addEventListener("click", toggleTheme);
  $("admin-signout-btn").addEventListener("click", signOut);
  $("validate-chain-btn").addEventListener("click", validateChain);
  $("validate-conversation-btn").addEventListener("click", () => validateConversation());
  $("conversation-audit-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") validateConversation();
  });
  $("user-search").addEventListener("input", renderUsers);
  boot();
});

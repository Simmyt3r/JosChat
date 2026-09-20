/**
 * static/js/app.js
 * -----------------
 * Reference client for the full Joschat flow:
 *   1. Auth against the Flask API (which wraps Supabase Auth), with session
 *      restore + automatic token refresh.
 *   2. Starting chats by username and listing your conversations.
 *   3. Sending a message: encrypt client-side, POST to /api/messages/send.
 *   4. Receiving messages LIVE via a direct Supabase Realtime subscription
 *      (no Flask/Socket.IO server involved), with a polling fallback if
 *      Realtime is unavailable.
 *   5. Uploading media through /api/media/upload (Cloudinary).
 *   6. A sketch of WebRTC call signalling over a Supabase Realtime Broadcast
 *      channel (NOT wired into the UI — see the bottom of this file).
 *
 * ENCRYPTION NOTE: this is a demo scheme, NOT a full public-key E2EE
 * handshake. Each conversation is encrypted with an AES-GCM key derived
 * (PBKDF2) from a passphrase that the participants agree on out-of-band and
 * type in on their own devices. The server only ever sees ciphertext. A
 * production build should perform proper key exchange using each user's
 * `public_key` column in `profiles` (e.g. X25519 + HKDF, mirroring the
 * Signal Protocol's design) before this is trusted with anything sensitive.
 *
 * The passphrase (and the login session) live in sessionStorage: they survive
 * a page reload but are wiped when the tab is closed.
 *
 * UI NOTES
 *   - Every message shows a hexagonal "seal". Pressing "Verify chat" asks the
 *     server to recompute the hash chain and sweeps a result over each seal.
 *   - All user-supplied text is inserted with textContent, never innerHTML.
 */

const API_BASE = "/api";
const SESSION_KEY = "joschat_session";
const PASSPHRASE_KEY_PREFIX = "joschat_pass_";
const THEME_KEY = "joschat_theme";
const POLL_INTERVAL_MS = 4000;

// Mirrors the server: 25 MB ceiling and the same allowed extensions.
const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;
const ALLOWED_EXT = new Set(["png", "jpg", "jpeg", "gif", "webp", "mp4", "mov", "webm", "mp3", "wav", "ogg", "m4a"]);
const IMAGE_EXT = new Set(["png", "jpg", "jpeg", "gif", "webp"]);
const VIDEO_EXT = new Set(["mp4", "mov", "webm"]);
const AUDIO_EXT = new Set(["mp3", "wav", "ogg", "m4a"]);
const USERNAME_RE = /^[A-Za-z0-9_.-]{3,30}$/;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const GROUP_GAP_MS = 5 * 60 * 1000;   // messages this close together share a bubble group
const AVATAR_HUES = [215, 262, 322, 8, 32, 158, 190];

let accessToken = null;
let refreshToken = null;
let currentProfile = null;
let supabaseClient = null;
let supabaseConfig = null;
let currentConversationId = null;
let currentParticipants = {};     // user_id -> username
let realtimeChannel = null;
let realtimeConnected = false;
let pollTimer = null;
let authMode = "login";
let refreshInFlight = null;

let conversations = null;         // null = not loaded yet, [] = loaded but empty
let messageStore = [];            // messages of the open conversation, oldest first
const derivedKeys = new Map();    // conversationId -> CryptoKey
const plainCache = new Map();     // messageId -> decrypted text (or undefined if it failed)
const messageStatus = new Map();  // messageId -> "verified" | "tampered" (session only)
const expandedBlocks = new Set(); // messageIds whose block details are open
const drafts = new Map();         // conversationId -> unsent text

let unlockOpen = false;           // passphrase panel open while already unlocked (changing it)
let pendingFile = null;
let pendingThumbUrl = null;
let sending = false;
let verifying = false;
let historyPushed = false;
let lastRenderedCount = 0;
let lastNetToast = 0;

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

const $ = (id) => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const isNarrow = () => window.matchMedia("(max-width: 859px)").matches;
const canHover = () => window.matchMedia("(hover: hover)").matches;
const isTouchKeyboard = () => window.matchMedia("(pointer: coarse)").matches;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function iconSvg(name) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.appendChild(use);
  return svg;
}

function setIcon(button, name) {
  const use = button.querySelector("use");
  if (use) use.setAttribute("href", `#i-${name}`);
}

function setBusy(button, busy) {
  button.classList.toggle("is-busy", busy);
  button.disabled = busy;
}

async function loadPublicConfig() {
  try {
    const res = await fetch(`${API_BASE}/config`);
    supabaseConfig = await res.json();
  } catch (err) {
    console.warn("Could not load /api/config", err);
  }
}

function ensureSupabaseClient() {
  // supabase-js comes from a CDN <script>; if it is blocked/offline the app
  // still works — it just falls back to polling for new messages.
  if (supabaseClient || !supabaseConfig || typeof supabase === "undefined") return supabaseClient;
  if (!supabaseConfig.supabase_url || !supabaseConfig.supabase_anon_key) return null;
  supabaseClient = supabase.createClient(
    supabaseConfig.supabase_url,
    supabaseConfig.supabase_anon_key,
    { auth: { persistSession: false, autoRefreshToken: false, detectSessionInUrl: false } }
  );
  return supabaseClient;
}

async function readJson(res) {
  try {
    return await res.json();
  } catch {
    return { error: `Unexpected response from the server (HTTP ${res.status})` };
  }
}

// --- Toasts -----------------------------------------------------------------

function toast(message, kind = "info", ms) {
  const box = $("toasts");
  const el = document.createElement("div");
  el.className = `toast${kind === "error" ? " is-error" : kind === "success" ? " is-success" : ""}`;
  if (kind === "error") el.setAttribute("role", "alert");
  if (kind !== "info") el.appendChild(iconSvg(kind === "error" ? "alert" : "check"));
  const text = document.createElement("span");
  text.textContent = message;
  el.appendChild(text);
  const close = document.createElement("button");
  close.type = "button";
  close.className = "icon-btn";
  close.setAttribute("aria-label", "Dismiss");
  close.appendChild(iconSvg("x"));
  el.appendChild(close);

  const remove = () => el.remove();
  close.addEventListener("click", remove);
  box.appendChild(el);
  while (box.children.length > 3) box.firstElementChild.remove();
  setTimeout(remove, ms || (kind === "error" ? 6500 : 3800));
}

// --- Avatars ----------------------------------------------------------------

function avatarHue(name) {
  let h = 0;
  for (const ch of String(name)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return AVATAR_HUES[h % AVATAR_HUES.length];
}

function paintAvatar(el, name) {
  const clean = String(name || "?").replace(/^@/, "");
  el.style.setProperty("--h", avatarHue(clean));
  el.textContent = (clean[0] || "?").toUpperCase();
}

// --- Views ------------------------------------------------------------------

function showView(name) {   // "auth" | "setup" | "chat"
  $("auth-view").hidden = name !== "auth";
  $("setup-view").hidden = name !== "setup";
  $("chat-shell").hidden = name !== "chat";
  document.body.classList.toggle("in-app", name === "chat");
  if (window.JoschatBackground) window.JoschatBackground.setActive(name !== "chat");
  $("boot").hidden = true;
}

// --- Theme ------------------------------------------------------------------

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem(THEME_KEY, theme); } catch { /* ignore */ }
  $("theme-color").content = theme === "light" ? "#f2f5fb" : "#0d1322";
  const btn = $("theme-btn");
  // The button shows the theme you would switch TO.
  setIcon(btn, theme === "light" ? "moon" : "sun");
  const label = theme === "light" ? "Switch to dark theme" : "Switch to light theme";
  btn.setAttribute("aria-label", label);
  btn.title = label;
  window.dispatchEvent(new Event("joschat:theme"));
}

function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
}

// ---------------------------------------------------------------------------
// Log in / create account
// ---------------------------------------------------------------------------

const AUTH_FIELDS = {
  username: { input: "username", msg: "username-msg" },
  email: { input: "email", msg: "email-msg" },
  password: { input: "password", msg: "password-msg" },
};

function setAuthMessage(text, kind = "error") {
  const el = $("auth-message");
  el.textContent = text || "";
  el.hidden = !text;
  el.className = `banner ${kind === "success" ? "is-success" : "is-error"}`;
}

function setFieldError(key, text) {
  const { input, msg } = AUTH_FIELDS[key];
  const inputEl = $(input), msgEl = $(msg);
  inputEl.setAttribute("aria-invalid", text ? "true" : "false");
  msgEl.classList.toggle("is-error", Boolean(text));
  msgEl.textContent = text || msgEl.dataset.hint || "";
}

function clearAuthErrors() {
  for (const key of Object.keys(AUTH_FIELDS)) setFieldError(key, "");
  setAuthMessage("");
}

function setMode(mode, opts = {}) {
  authMode = mode;
  const register = mode === "register";
  $("tab-login").setAttribute("aria-selected", String(!register));
  $("tab-register").setAttribute("aria-selected", String(register));
  $("tab-login").tabIndex = register ? -1 : 0;
  $("tab-register").tabIndex = register ? 0 : -1;
  $("field-username").hidden = !register;
  $("password").autocomplete = register ? "new-password" : "current-password";
  $("password-msg").dataset.hint = register ? "At least 6 characters." : "";
  $("username-msg").dataset.hint = "3–30 characters: letters, numbers, _ . or -";
  $("submit-btn").querySelector(".btn-label").textContent = register ? "Create account" : "Log in";
  $("auth-form-wrap").hidden = false;
  $("auth-tagline").hidden = false;
  $("check-email").hidden = true;
  clearAuthErrors();
  if (opts.keepMessage) setAuthMessage(opts.keepMessage.text, opts.keepMessage.kind);
}

function validateAuthForm() {
  const errors = {};
  const email = $("email").value.trim();
  const password = $("password").value;
  if (authMode === "register") {
    const username = $("username").value.trim();
    if (!username) errors.username = "Choose a username.";
    else if (!USERNAME_RE.test(username)) errors.username = "Use 3–30 letters, numbers, _ . or - (no spaces).";
  }
  if (!email) errors.email = "Enter your email address.";
  else if (!EMAIL_RE.test(email)) errors.email = "That doesn't look like an email address.";
  if (!password) errors.password = authMode === "register" ? "Choose a password." : "Enter your password.";
  else if (authMode === "register" && password.length < 6) errors.password = "Use at least 6 characters.";
  return errors;
}

async function submitAuth(event) {
  event.preventDefault();
  clearAuthErrors();
  const errors = validateAuthForm();
  const keys = Object.keys(errors);
  if (keys.length) {
    for (const key of keys) setFieldError(key, errors[key]);
    $(AUTH_FIELDS[keys[0]].input).focus();
    return;
  }

  const btn = $("submit-btn");
  setBusy(btn, true);
  try {
    if (authMode === "login") await login();
    else await register();
  } catch (err) {
    console.error(err);
    setAuthMessage("Can't reach Joschat. Check your connection and try again.");
  } finally {
    setBusy(btn, false);
  }
}

async function register() {
  const username = $("username").value.trim();
  const email = $("email").value.trim();
  const password = $("password").value;

  const res = await fetch(`${API_BASE}/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, email, password }),
  });
  const data = await readJson(res);
  if (!res.ok) {
    const message = data.error || "Registration failed.";
    if (res.status === 409 && /username/i.test(message)) setFieldError("username", message);
    else if (res.status === 409 && /email/i.test(message)) setFieldError("email", message);
    else setAuthMessage(message);
    return;
  }

  if (data.confirmation_required) {
    $("check-email-address").textContent = email;
    $("auth-form-wrap").hidden = true;
    $("auth-tagline").hidden = true;
    $("check-email").hidden = false;
    return;
  }

  // No email confirmation needed: carry straight on into the app.
  await login({ fromRegister: true });
}

async function login(opts = {}) {
  const email = $("email").value.trim();
  const password = $("password").value;

  const res = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const data = await readJson(res);
  if (!res.ok) {
    if (opts.fromRegister) {
      setMode("login", { keepMessage: { text: "Your account was created. Log in to continue.", kind: "success" } });
    } else {
      setAuthMessage(data.error || "Login failed.");
    }
    return;
  }

  $("password").value = "";
  await startSession(data);
}

// ---------------------------------------------------------------------------
// Session handling: persist, refresh, restore, sign out
// ---------------------------------------------------------------------------

function saveSession() {
  try {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify({ accessToken, refreshToken }));
  } catch { /* storage unavailable (private mode etc.) — session lasts until reload */ }
}

function applyToken() {
  // Realtime enforces the Row Level Security policies in schema.sql, which
  // are written in terms of auth.uid(). Without handing the logged-in user's
  // JWT to the Realtime connection it runs as the anonymous role and is
  // silently sent NO events. Must be repeated whenever the token changes.
  const client = ensureSupabaseClient();
  if (client) client.realtime.setAuth(accessToken);
}

async function startSession(data) {
  accessToken = data.access_token;
  refreshToken = data.refresh_token || null;
  currentProfile = data.profile || null;
  saveSession();
  applyToken();

  if (!currentProfile) {
    // Authenticated, but no profiles row (e.g. an earlier sign-up that failed
    // half-way). Let the user finish instead of being locked out.
    $("setup-username").value = "";
    setSetupError("");
    showView("setup");
    $("setup-username").focus();
    return;
  }
  showChatHome();
}

function showChatHome() {
  paintAvatar($("me-avatar"), currentProfile.username);
  $("whoami").textContent = `@${currentProfile.username}`;
  showView("chat");
  showList();
  loadConversations();
}

function setSetupError(text) {
  const input = $("setup-username"), msg = $("setup-msg");
  input.setAttribute("aria-invalid", text ? "true" : "false");
  msg.classList.toggle("is-error", Boolean(text));
  msg.textContent = text || "3–30 characters: letters, numbers, _ . or -";
}

async function createProfile(event) {
  event.preventDefault();
  const username = $("setup-username").value.trim();
  if (!USERNAME_RE.test(username)) {
    setSetupError("Use 3–30 letters, numbers, _ . or - (no spaces).");
    return $("setup-username").focus();
  }
  setSetupError("");
  const btn = $("setup-btn");
  setBusy(btn, true);
  try {
    const res = await apiFetch("/auth/profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username }),
    });
    if (!res) return;
    const data = await readJson(res);
    if (!res.ok) return setSetupError(data.error || "Could not save your username.");
    currentProfile = data.profile;
    showChatHome();
  } finally {
    setBusy(btn, false);
  }
}

function refreshSession() {
  // Share one in-flight refresh between concurrent requests: refresh tokens
  // are single-use, so two parallel refreshes would invalidate each other.
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
      if (data.profile) currentProfile = data.profile;
      saveSession();
      applyToken();
      return true;
    } catch {
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}

/**
 * fetch() against the API with the bearer token attached. On a 401 it tries
 * one token refresh and retries; if that fails the user is signed out.
 * Returns the Response, or null if the session ended.
 */
async function apiFetch(path, options = {}) {
  const send = () => fetch(`${API_BASE}${path}`, {
    ...options,
    headers: { ...(options.headers || {}), Authorization: `Bearer ${accessToken}` },
  });
  let res;
  try {
    res = await send();
  } catch {
    // The offline banner already explains a dropped connection; otherwise
    // tell the user, but not on every poll.
    if (navigator.onLine !== false && Date.now() - lastNetToast > 8000) {
      lastNetToast = Date.now();
      toast("Can't reach Joschat. Check your connection.", "error");
    }
    return null;
  }
  if (res.status === 401 && await refreshSession()) {
    try { res = await send(); } catch { return null; }
  }
  if (res.status === 401) {
    signOut("Your session expired. Log in again to continue.");
    return null;
  }
  return res;
}

async function restoreSession() {
  let saved = null;
  try { saved = JSON.parse(sessionStorage.getItem(SESSION_KEY) || "null"); } catch { /* ignore */ }
  if (!saved || !saved.accessToken) return false;
  accessToken = saved.accessToken;
  refreshToken = saved.refreshToken || null;

  const res = await apiFetch("/auth/me");
  if (res && res.ok) {
    const data = await res.json();
    await startSession({ access_token: accessToken, refresh_token: refreshToken, profile: data.profile });
    return true;
  }
  if (res && res.status === 404) {
    const body = await readJson(res);
    if (body.code === "profile_missing") {
      await startSession({ access_token: accessToken, refresh_token: refreshToken, profile: null });
      return true;
    }
  }
  if (res) signOut();   // (a null res means apiFetch already signed us out)
  return false;
}

async function signOut(message) {
  const token = accessToken;
  closeConversation();
  accessToken = refreshToken = currentProfile = null;
  conversations = null;
  messageStore = [];
  derivedKeys.clear();
  plainCache.clear();
  messageStatus.clear();
  drafts.clear();
  try { sessionStorage.removeItem(SESSION_KEY); } catch { /* ignore */ }
  if (token) {
    // Best effort; the token may already be dead.
    fetch(`${API_BASE}/auth/logout`, { method: "POST", headers: { Authorization: `Bearer ${token}` } })
      .catch(() => {});
  }
  $("conversation-list").replaceChildren();
  $("chat-search").value = "";
  showView("auth");
  setMode("login", typeof message === "string" ? { keepMessage: { text: message, kind: "error" } } : {});
}

// ---------------------------------------------------------------------------
// Conversations: list, search, start a chat
// ---------------------------------------------------------------------------

function conversationLabel(conv) {
  if (conv.title) return conv.title;
  return (conv.other_usernames || []).map((n) => `@${n}`).join(", ") || `Conversation ${conv.id}`;
}

function conversationAvatarName(conv) {
  return conv.title || (conv.other_usernames || [])[0] || String(conv.id);
}

function hasPassphrase(conversationId) {
  if (derivedKeys.has(conversationId)) return true;
  try { return Boolean(sessionStorage.getItem(PASSPHRASE_KEY_PREFIX + conversationId)); } catch { return false; }
}

function searchQuery() {
  return $("chat-search").value.trim().replace(/^@/, "");
}

async function loadConversations() {
  if (conversations === null) renderConversationList();   // skeleton on first load
  const res = await apiFetch("/conversations");
  if (!res) {
    if (conversations === null) { conversations = []; renderConversationList(); }
    return;
  }
  const data = await readJson(res);
  if (!res.ok) {
    toast(data.error || "Couldn't load your chats.", "error");
    if (conversations === null) { conversations = []; renderConversationList(); }
    return;
  }
  conversations = data.conversations;
  renderConversationList();
}

function renderConversationList() {
  const list = $("conversation-list");
  const empty = $("conv-empty");
  const newRow = $("new-chat-row");
  list.replaceChildren();

  if (conversations === null) {
    for (let i = 0; i < 4; i++) {
      const row = document.createElement("li");
      row.className = "skeleton-row";
      row.setAttribute("aria-hidden", "true");
      row.innerHTML = '<span class="skeleton hex"></span><span style="flex:1"><span class="skeleton line" style="display:block;width:55%;margin-bottom:.45rem"></span><span class="skeleton line" style="display:block;width:35%"></span></span>';
      list.appendChild(row);
    }
    empty.hidden = true;
    newRow.hidden = true;
    return;
  }

  const raw = searchQuery();
  const q = raw.toLowerCase();
  const filtered = q ? conversations.filter((c) => conversationLabel(c).toLowerCase().includes(q)) : conversations;

  for (const conv of filtered) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "conv";
    if (conv.id === currentConversationId) btn.setAttribute("aria-current", "true");

    const av = document.createElement("span");
    av.className = "avatar";
    av.setAttribute("aria-hidden", "true");
    paintAvatar(av, conversationAvatarName(conv));

    const text = document.createElement("span");
    text.className = "conv-text";
    const name = document.createElement("span");
    name.className = "conv-name";
    name.textContent = conversationLabel(conv);
    const sub = document.createElement("span");
    const unlocked = hasPassphrase(conv.id);
    sub.className = `conv-sub${unlocked ? "" : " is-locked"}`;
    sub.appendChild(iconSvg(unlocked ? "unlock" : "lock"));
    sub.appendChild(document.createTextNode(unlocked ? "Unlocked" : "Enter passphrase to read"));
    text.append(name, sub);

    btn.append(av, text);
    btn.addEventListener("click", () => openConversation(conv.id, conversationLabel(conv), conversationAvatarName(conv)));
    li.appendChild(btn);
    list.appendChild(li);
  }

  // "Start a chat with @name" appears when what was typed is a valid username
  // that doesn't already have a chat.
  const hasExact = conversations.some((c) => (c.other_usernames || []).some((u) => u.toLowerCase() === q));
  const isMe = currentProfile && currentProfile.username.toLowerCase() === q;
  const canStart = Boolean(q) && USERNAME_RE.test(raw) && !hasExact && !isMe;
  newRow.hidden = !canStart;
  if (canStart) $("new-chat-label").textContent = `Start a chat with @${raw}`;

  // Empty states point at what to do next.
  empty.hidden = true;
  if (!conversations.length && !q) {
    empty.replaceChildren(...emptyBlock("No chats yet", "Type someone's username in the search box above to start your first chat."));
    empty.hidden = false;
  } else if (q && !filtered.length && !canStart) {
    const why = isMe ? "That's you. Enter someone else's username." : `No chats match “${raw}”. Usernames use letters, numbers, _ . or - only.`;
    empty.replaceChildren(...emptyBlock("Nothing found", why));
    empty.hidden = false;
  }
}

function emptyBlock(title, body) {
  const t = document.createElement("p");
  t.className = "side-empty-title";
  t.textContent = title;
  const b = document.createElement("p");
  b.textContent = body;
  return [t, b];
}

async function startChat(rawName) {
  const username = String(rawName || "").trim().replace(/^@/, "");
  if (!USERNAME_RE.test(username)) {
    return toast("Usernames are 3–30 characters: letters, numbers, _ . or -", "error");
  }
  if (currentProfile && username.toLowerCase() === currentProfile.username.toLowerCase()) {
    return toast("That's your own username. Enter someone else's.", "error");
  }

  const row = $("new-chat-row");
  row.disabled = true;
  try {
    const res = await apiFetch("/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ participant_usernames: [username] }),
    });
    if (!res) return;
    const data = await readJson(res);
    if (!res.ok) return toast(data.error || "Couldn't start that chat.", "error");

    $("chat-search").value = "";
    $("chat-search-clear").hidden = true;
    await loadConversations();
    openConversation(data.conversation.id, `@${username}`, username);
  } finally {
    row.disabled = false;
  }
}

function onSearchKeydown(event) {
  if (event.key === "Escape") {
    event.target.value = "";
    onSearchInput();
    return;
  }
  if (event.key !== "Enter") return;
  event.preventDefault();
  const raw = searchQuery();
  if (!raw) return;
  const exact = (conversations || []).find((c) => (c.other_usernames || []).some((u) => u.toLowerCase() === raw.toLowerCase()));
  if (exact) {
    openConversation(exact.id, conversationLabel(exact), conversationAvatarName(exact));
    $("chat-search").value = "";
    onSearchInput();
  } else {
    startChat(raw);
  }
}

function onSearchInput() {
  $("chat-search-clear").hidden = !$("chat-search").value;
  renderConversationList();
}

// ---------------------------------------------------------------------------
// Navigation between the list and a conversation (small screens)
// ---------------------------------------------------------------------------

function showList() {
  $("chat-shell").dataset.view = "list";
}

function showThread() {
  $("chat-shell").dataset.view = "thread";
  if (isNarrow() && !historyPushed) {
    history.pushState({ joschat: "thread" }, "");
    historyPushed = true;
  }
}

function leaveThread() {
  closeConversation();
  $("thread-view").hidden = true;
  $("thread-empty").hidden = false;
  showList();
  renderConversationList();
}

function onBackButton() {
  if (historyPushed) history.back();   // popstate handler does the rest
  else leaveThread();
}

// ---------------------------------------------------------------------------
// Encryption (demo scheme — see the module docstring)
// ---------------------------------------------------------------------------

function bytesToBase64(bytes) {
  let binary = "";
  const CHUNK = 0x8000;   // avoid "too many arguments" on large inputs
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

function base64ToBytes(b64) {
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

async function deriveKey(passphrase, conversationId) {
  const material = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(passphrase), "PBKDF2", false, ["deriveKey"]
  );
  return crypto.subtle.deriveKey(
    {
      name: "PBKDF2",
      // Per-conversation salt so the same passphrase never yields the same key twice.
      salt: new TextEncoder().encode(`joschat:conversation:${conversationId}`),
      iterations: 200000,
      hash: "SHA-256",
    },
    material,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"]
  );
}

function getConversationKey() {
  return derivedKeys.get(currentConversationId) || null;
}

async function encryptText(plaintext) {
  const key = getConversationKey();
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv }, key, new TextEncoder().encode(plaintext)
  );
  const combined = new Uint8Array(iv.length + ciphertext.byteLength);
  combined.set(iv, 0);
  combined.set(new Uint8Array(ciphertext), iv.length);
  return bytesToBase64(combined);
}

// Returns the text, or undefined when the key is wrong / the data is corrupt.
async function decryptWith(key, b64) {
  try {
    const combined = base64ToBytes(b64);
    const iv = combined.slice(0, 12);
    const ciphertext = combined.slice(12);
    const plaintext = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, key, ciphertext);
    return new TextDecoder().decode(plaintext);
  } catch {
    return undefined;
  }
}

async function ensurePlain(msg, key) {
  if (plainCache.has(msg.id)) return;
  plainCache.set(msg.id, await decryptWith(key, msg.encrypted_content));
}

// --- Unlock panel -----------------------------------------------------------

function setUnlockError(text) {
  $("unlock-error").textContent = text || "";
  $("conv-passphrase").setAttribute("aria-invalid", text ? "true" : "false");
}

function otherName() {
  return $("conv-title").textContent || "the other person";
}

function updateLockUI() {
  const unlocked = derivedKeys.has(currentConversationId);
  const showPanel = !unlocked || unlockOpen;
  $("unlock-panel").hidden = !showPanel;
  $("composer").hidden = showPanel;
  $("unlock-cancel").hidden = !(unlocked && unlockOpen);

  const changing = unlocked && unlockOpen;
  $("unlock-title").textContent = changing ? "Change the passphrase" : "This chat is locked";
  $("unlock-text").textContent = changing
    ? "Enter the new passphrase you and " + otherName() + " agreed on. Messages written with a different passphrase stay locked."
    : "Messages are encrypted on your device. Enter the passphrase you and " + otherName() + " agreed on. Share it outside Joschat.";
  $("unlock-btn").querySelector(".btn-label").textContent = changing ? "Save passphrase" : "Unlock chat";

  const lockBtn = $("lock-btn");
  lockBtn.dataset.locked = String(!unlocked);
  setIcon(lockBtn, unlocked ? "unlock" : "lock");
  const label = unlocked ? "Change passphrase" : "Enter passphrase";
  lockBtn.setAttribute("aria-label", label);
  lockBtn.title = label;
}

async function applyPassphrase(event) {
  event.preventDefault();
  const passphrase = $("conv-passphrase").value;
  if (!passphrase) return setUnlockError("Enter the passphrase for this chat.");
  const conversationId = currentConversationId;
  setUnlockError("");

  const btn = $("unlock-btn");
  setBusy(btn, true);
  try {
    const key = await deriveKey(passphrase, conversationId);

    // If the chat already has messages, make sure this passphrase opens at least
    // one of them. Otherwise it is almost certainly a typo, and accepting it
    // would make you send messages the other person can't read.
    const sample = messageStore.slice(-20);
    if (sample.length) {
      let opensSomething = false;
      for (const m of sample) {
        if ((await decryptWith(key, m.encrypted_content)) !== undefined) { opensSomething = true; break; }
      }
      if (!opensSomething) {
        return setUnlockError(`That passphrase doesn't open these messages. Check it with ${otherName()} and try again.`);
      }
    }
    if (currentConversationId !== conversationId) return;

    derivedKeys.set(conversationId, key);
    try { sessionStorage.setItem(PASSPHRASE_KEY_PREFIX + conversationId, passphrase); } catch { /* ignore */ }
    plainCache.clear();
    unlockOpen = false;
    $("conv-passphrase").value = "";
    updateLockUI();
    renderConversationList();
    await renderAllMessages({ forceStick: true });
    if (canHover()) $("message-text").focus();
  } finally {
    setBusy(btn, false);
  }
}

function onLockButton() {
  if (derivedKeys.has(currentConversationId)) {
    unlockOpen = true;
    updateLockUI();
  }
  $("conv-passphrase").focus();
}

function cancelUnlock() {
  unlockOpen = false;
  setUnlockError("");
  $("conv-passphrase").value = "";
  updateLockUI();
}

// ---------------------------------------------------------------------------
// Opening a conversation, live updates
// ---------------------------------------------------------------------------

function renderMessageSkeletons() {
  const list = $("messages");
  list.replaceChildren();
  ["theirs", "mine", "theirs", "mine"].forEach((side, i) => {
    const li = document.createElement("li");
    li.className = `skeleton skeleton-msg ${side}`;
    li.style.width = ["48%", "38%", "60%", "30%"][i];
    li.setAttribute("aria-hidden", "true");
    list.appendChild(li);
  });
}

async function openConversation(conversationId, label, avatarName) {
  if (currentConversationId === conversationId && !$("thread-view").hidden) {
    showThread();
    return;
  }
  closeConversation();
  currentConversationId = conversationId;
  messageStore = [];
  plainCache.clear();
  messageStatus.clear();
  expandedBlocks.clear();
  lastRenderedCount = 0;
  unlockOpen = false;
  currentParticipants = {};

  $("conv-title").textContent = label || `#${conversationId}`;
  paintAvatar($("thread-avatar"), avatarName || label);
  $("thread-empty").hidden = true;
  $("thread-view").hidden = false;
  $("verify-banner").hidden = true;
  $("jump-btn").hidden = true;
  $("messages-empty").hidden = true;
  $("conv-passphrase").value = "";
  setUnlockError("");
  $("message-text").value = drafts.get(conversationId) || "";
  autosizeComposer();
  updateSendState();
  setLiveStatus("connecting");
  renderMessageSkeletons();
  $("unlock-panel").hidden = true;
  $("composer").hidden = true;
  showThread();
  renderConversationList();   // moves the highlight

  // Restore this tab's passphrase for the conversation, if it was entered before.
  let saved = null;
  try { saved = sessionStorage.getItem(PASSPHRASE_KEY_PREFIX + conversationId); } catch { /* ignore */ }
  if (saved && !derivedKeys.has(conversationId)) {
    derivedKeys.set(conversationId, await deriveKey(saved, conversationId));
  }
  if (currentConversationId !== conversationId) return;
  updateLockUI();

  // Who is in this conversation (for sender names).
  const infoRes = await apiFetch(`/conversations/${conversationId}`);
  if (!infoRes || currentConversationId !== conversationId) return;
  if (infoRes.ok) {
    const info = await infoRes.json();
    for (const p of info.participants) currentParticipants[p.user_id] = p.username;
  }

  const res = await apiFetch(`/messages/${conversationId}`);
  if (!res || currentConversationId !== conversationId) return;
  const data = await readJson(res);
  if (!res.ok) {
    $("messages").replaceChildren();
    const empty = $("messages-empty");
    empty.textContent = data.error || "Couldn't load messages. Go back and try again.";
    empty.hidden = false;
    return;
  }
  await addMessages(data.messages, { forceStick: true, forceRender: true });

  subscribeToConversation(conversationId);
  startPolling(conversationId);

  if (canHover()) {
    if (derivedKeys.has(conversationId)) $("message-text").focus();
    else $("conv-passphrase").focus();
  }
}

function closeConversation() {
  if (currentConversationId !== null) drafts.set(currentConversationId, $("message-text").value);
  if (realtimeChannel && supabaseClient) supabaseClient.removeChannel(realtimeChannel);
  realtimeChannel = null;
  realtimeConnected = false;
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
  currentConversationId = null;
  clearAttachment();
  setLiveStatus("");
}

const LIVE_TEXT = { live: "Live", connecting: "Connecting…", polling: "Checking for updates" };

function setLiveStatus(state) {
  const box = $("thread-status");
  box.dataset.state = state || "connecting";
  $("live-status").textContent = LIVE_TEXT[state] || "";
}

function subscribeToConversation(conversationId) {
  const client = ensureSupabaseClient();
  if (!client) {
    setLiveStatus("polling");
    return;
  }
  applyToken();
  realtimeChannel = client
    .channel(`conversation-${conversationId}`)
    .on(
      "postgres_changes",
      {
        event: "INSERT",
        schema: "public",
        table: "messages",
        filter: `conversation_id=eq.${conversationId}`,
      },
      (payload) => { if (currentConversationId === conversationId) addMessages([payload.new]); }
    )
    .subscribe((status) => {
      if (currentConversationId !== conversationId) return;
      realtimeConnected = status === "SUBSCRIBED";
      setLiveStatus(realtimeConnected ? "live" : "polling");
    });
}

// Polling is the safety net, not the primary path: it only fetches while the
// Realtime channel is not connected, so a working setup makes no extra requests.
function startPolling(conversationId) {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if (realtimeConnected || currentConversationId !== conversationId || document.hidden) return;
    refreshMessages();
  }, POLL_INTERVAL_MS);
}

async function refreshMessages() {
  const conversationId = currentConversationId;
  if (conversationId === null) return;
  const res = await apiFetch(`/messages/${conversationId}`);
  if (!res || !res.ok || currentConversationId !== conversationId) return;
  const data = await res.json();
  addMessages(data.messages);
}

// ---------------------------------------------------------------------------
// Rendering messages
// ---------------------------------------------------------------------------

async function addMessages(messages, opts = {}) {
  const known = new Set(messageStore.map((m) => m.id));
  const fresh = messages.filter((m) => !known.has(m.id));
  if (!fresh.length && !opts.forceRender) return;
  if (fresh.length && !opts.forceRender && !verifying) $("verify-banner").hidden = true;
  messageStore.push(...fresh);
  messageStore.sort((a, b) => a.id - b.id);
  const sentByMe = fresh.some((m) => currentProfile && m.sender_id === currentProfile.id);
  await renderAllMessages({ forceStick: Boolean(opts.forceStick) || sentByMe });
}

function scrollToBottom() {
  const wrap = $("messages-wrap");
  wrap.scrollTop = wrap.scrollHeight;
}

function sameGroup(a, b) {
  if (!a || !b || a.sender_id !== b.sender_id) return false;
  const ta = new Date(a.created_at), tb = new Date(b.created_at);
  return ta.toDateString() === tb.toDateString() && Math.abs(tb - ta) < GROUP_GAP_MS;
}

function dayLabel(iso) {
  const d = new Date(iso);
  const today = new Date();
  const yesterday = new Date();
  yesterday.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === yesterday.toDateString()) return "Yesterday";
  const sameYear = d.getFullYear() === today.getFullYear();
  return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", ...(sameYear ? {} : { year: "numeric" }) });
}

function timeLabel(iso) {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

async function renderAllMessages({ forceStick = false } = {}) {
  const conversationId = currentConversationId;
  if (conversationId === null) return;

  const key = getConversationKey();
  if (key) await Promise.all(messageStore.map((m) => ensurePlain(m, key)));
  if (currentConversationId !== conversationId) return;

  const wrap = $("messages-wrap");
  const wasNearBottom = wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight < 96;
  const isFirst = lastRenderedCount === 0;
  const isGroup = Object.keys(currentParticipants).length > 2;

  const frag = document.createDocumentFragment();
  messageStore.forEach((msg, i) => {
    const prev = messageStore[i - 1];
    const next = messageStore[i + 1];
    if (!prev || new Date(prev.created_at).toDateString() !== new Date(msg.created_at).toDateString()) {
      const day = document.createElement("li");
      day.className = "day";
      const chip = document.createElement("span");
      chip.textContent = dayLabel(msg.created_at);
      day.appendChild(chip);
      frag.appendChild(day);
    }
    frag.appendChild(buildMessageItem(msg, sameGroup(prev, msg), sameGroup(msg, next), isGroup));
  });
  $("messages").replaceChildren(frag);

  const empty = $("messages-empty");
  empty.hidden = messageStore.length > 0;
  if (!messageStore.length) {
    empty.textContent = key
      ? "No messages yet. Your first message starts the chain."
      : "No messages yet. Enter the passphrase below, then say hello.";
  }

  const grew = messageStore.length > lastRenderedCount;
  lastRenderedCount = messageStore.length;
  if (forceStick || wasNearBottom || isFirst) {
    scrollToBottom();
    $("jump-btn").hidden = true;
  } else if (grew) {
    $("jump-btn").hidden = false;
  }
}

const SEAL_MARKUP =
  '<svg viewBox="0 0 24 24" aria-hidden="true"><polygon class="hex" points="12,2.5 20.5,7.25 20.5,16.75 12,21.5 3.5,16.75 3.5,7.25"/><polyline class="tick" points="8,12.3 11,15 16,9.3"/><path class="bang" d="M12 7.6v5M12 15.9v.01"/></svg>';
const SEAL_STATE_TEXT = {
  sealed: "sealed in the chain",
  verified: "verified",
  tampered: "failed verification and may have been changed",
};

function describeSeal(seal, state, blockIndex) {
  seal.dataset.state = state;
  seal.setAttribute("aria-label", `Block ${blockIndex}, ${SEAL_STATE_TEXT[state]}. Show details.`);
}

function buildSeal(msg, expanded) {
  const seal = document.createElement("button");
  seal.type = "button";
  seal.className = "seal";
  seal.dataset.msgId = msg.id;
  seal.setAttribute("aria-expanded", String(expanded));
  seal.innerHTML = SEAL_MARKUP;   // static markup, no user data
  describeSeal(seal, messageStatus.get(msg.id) || "sealed", msg.block_index);
  return seal;
}

function buildMessageItem(msg, contPrev, contNext, isGroup) {
  const mine = Boolean(currentProfile && msg.sender_id === currentProfile.id);
  const li = document.createElement("li");
  li.className = `msg ${mine ? "mine" : "theirs"}${contPrev ? " cont-prev" : ""}${contNext ? " cont-next" : ""}`;
  li.dataset.msgId = msg.id;

  if (!mine && isGroup && !contPrev) {
    const who = document.createElement("div");
    who.className = "sender";
    who.textContent = `@${currentParticipants[msg.sender_id] || "unknown"}`;
    li.appendChild(who);
  }

  const bubble = document.createElement("div");
  bubble.className = "bubble";

  const safeUrl = safeHttpsUrl(msg.media_url);
  if (safeUrl) bubble.appendChild(buildMedia(safeUrl));

  // textContent (never innerHTML): decrypted text comes from another user and
  // must not be able to inject markup/script into this page.
  const key = getConversationKey();
  const plaintext = key ? plainCache.get(msg.id) : null;
  if (plaintext === null) {
    const locked = document.createElement("div");
    locked.className = "msg-text is-locked";
    locked.append(iconSvg("lock"), document.createTextNode("Locked message"));
    bubble.appendChild(locked);
  } else if (plaintext === undefined) {
    const failed = document.createElement("div");
    failed.className = "msg-text is-failed";
    failed.append(iconSvg("alert"), document.createTextNode("Can't decrypt. The passphrase may not match."));
    bubble.appendChild(failed);
  } else if (!(plaintext === "(attachment)" && safeUrl)) {
    const body = document.createElement("div");
    body.className = "msg-text";
    body.textContent = plaintext;
    bubble.appendChild(body);
  }

  const foot = document.createElement("div");
  foot.className = "msg-foot";
  const time = document.createElement("time");
  time.className = "msg-time";
  time.dateTime = msg.created_at;
  time.textContent = timeLabel(msg.created_at);
  foot.append(time, buildSeal(msg, expandedBlocks.has(msg.id)));
  bubble.appendChild(foot);

  const info = document.createElement("div");
  info.className = "block-info";
  info.hidden = !expandedBlocks.has(msg.id);
  info.append(document.createTextNode(`Block #${msg.block_index}`));
  const code = document.createElement("code");
  code.textContent = String(msg.block_hash);
  info.appendChild(code);
  bubble.appendChild(info);

  li.appendChild(bubble);
  return li;
}

function extOf(pathname) {
  const m = /\.([A-Za-z0-9]+)$/.exec(pathname);
  return m ? m[1].toLowerCase() : "";
}

function buildMedia(href) {
  const url = new URL(href);
  const ext = extOf(url.pathname);
  const wrap = document.createElement("div");
  wrap.className = "media";

  if (IMAGE_EXT.has(ext) || (!ext && url.pathname.includes("/image/upload/"))) {
    const a = document.createElement("a");
    a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer";
    const img = document.createElement("img");
    img.src = href; img.alt = "Image attachment"; img.loading = "lazy";
    a.appendChild(img);
    wrap.appendChild(a);
  } else if (VIDEO_EXT.has(ext)) {
    const v = document.createElement("video");
    v.src = href; v.controls = true; v.preload = "metadata"; v.playsInline = true;
    wrap.appendChild(v);
  } else if (AUDIO_EXT.has(ext)) {
    const a = document.createElement("audio");
    a.src = href; a.controls = true; a.preload = "none";
    wrap.appendChild(a);
  } else {
    const a = document.createElement("a");
    a.className = "file-chip";
    a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer";
    a.append(iconSvg("file"), document.createTextNode("Open attachment"));
    wrap.appendChild(a);
  }
  return wrap;
}

function safeHttpsUrl(value) {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Composer: text, attachments, sending
// ---------------------------------------------------------------------------

function autosizeComposer() {
  const ta = $("message-text");
  ta.style.height = "auto";
  ta.style.height = `${Math.min(ta.scrollHeight + 2, 160)}px`;
}

function updateSendState() {
  const hasContent = $("message-text").value.trim().length > 0 || Boolean(pendingFile);
  $("send-btn").disabled = sending || !hasContent;
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function setPendingFile(file) {
  if (!file) return;
  const ext = (file.name.split(".").pop() || "").toLowerCase();
  if (!ALLOWED_EXT.has(ext)) {
    return toast("That file type isn't supported. Send an image, video or audio file.", "error");
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    return toast(`That file is ${formatBytes(file.size)}. The limit is 25 MB.`, "error");
  }
  clearAttachment();
  pendingFile = file;
  $("attachment-name").textContent = file.name;
  $("attachment-size").textContent = formatBytes(file.size);
  const thumb = $("attachment-thumb");
  if (file.type.startsWith("image/")) {
    pendingThumbUrl = URL.createObjectURL(file);
    thumb.src = pendingThumbUrl;
    thumb.hidden = false;
    $("attachment-icon").style.display = "none";
  } else {
    thumb.hidden = true;
    $("attachment-icon").style.display = "";
  }
  $("attachment-chip").hidden = false;
  updateSendState();
}

function clearAttachment() {
  pendingFile = null;
  if (pendingThumbUrl) URL.revokeObjectURL(pendingThumbUrl);
  pendingThumbUrl = null;
  $("attachment-chip").hidden = true;
  $("attachment-thumb").hidden = true;
  $("media-file").value = "";
  updateSendState();
}

async function sendMessage() {
  if (!currentConversationId || sending) return;
  if (!getConversationKey()) return toast("Unlock this chat with its passphrase first.", "error");

  const text = $("message-text").value.trim();
  if (!text && !pendingFile) return;

  sending = true;
  const sendBtn = $("send-btn");
  sendBtn.classList.add("is-busy");
  sendBtn.disabled = true;
  const conversationId = currentConversationId;

  try {
    let media_url = null, media_public_id = null;
    if (pendingFile) {
      const form = new FormData();
      form.append("file", pendingFile);
      form.append("conversation_id", conversationId);
      const uploadRes = await apiFetch("/media/upload", { method: "POST", body: form });
      if (!uploadRes) return;
      const uploadData = await readJson(uploadRes);
      if (!uploadRes.ok) return toast(`Upload failed: ${uploadData.error}`, "error");
      media_url = uploadData.secure_url;
      media_public_id = uploadData.public_id;
    }

    const encrypted_content = await encryptText(text || "(attachment)");

    const res = await apiFetch("/messages/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        conversation_id: conversationId,
        encrypted_content,
        media_url,
        media_public_id,
      }),
    });
    if (!res) return;
    const data = await readJson(res);
    if (!res.ok) return toast(`Message not sent: ${data.error}`, "error");

    $("message-text").value = "";
    drafts.delete(conversationId);
    autosizeComposer();
    clearAttachment();
    // Show it immediately; the Realtime/poll copy of the same row is
    // de-duplicated by id, so it never appears twice.
    if (currentConversationId === conversationId) await addMessages([data.message], { forceStick: true });
  } finally {
    sending = false;
    sendBtn.classList.remove("is-busy");
    updateSendState();
    if (canHover()) $("message-text").focus();
  }
}

// ---------------------------------------------------------------------------
// Verify: recompute the chain on the server, then sweep the result over the seals
// ---------------------------------------------------------------------------

function showVerifyBanner(text, bad) {
  const banner = $("verify-banner");
  banner.classList.toggle("is-bad", bad);
  setIcon(banner, bad ? "alert" : "check");
  $("verify-text").textContent = text;
  banner.hidden = false;
}

async function verifyChain() {
  if (!currentConversationId || verifying) return;
  const conversationId = currentConversationId;
  verifying = true;
  const btn = $("verify-btn");
  setBusy(btn, true);
  btn.querySelector(".btn-label").textContent = "Verifying…";
  $("verify-banner").hidden = true;

  try {
    const res = await apiFetch(`/messages/verify/${conversationId}`);
    if (!res) return;
    const data = await readJson(res);
    if (!res.ok) return toast(data.error || "Couldn't verify this chat.", "error");
    if (currentConversationId !== conversationId) return;

    const outcome = (data.results || []).map((r) => [Number(r.message_id), Boolean(r.verified)]);
    if (!outcome.length) return showVerifyBanner("Nothing to verify yet. Send a message first.", false);

    // One seal at a time, oldest first. The sweep IS the feedback: it shows
    // which messages were checked and what each one turned out to be.
    const step = reduceMotion ? 0 : Math.max(10, Math.min(70, Math.floor(1500 / outcome.length)));
    let bad = 0;
    for (const [id, ok] of outcome) {
      if (currentConversationId !== conversationId) return;
      messageStatus.set(id, ok ? "verified" : "tampered");
      if (!ok) bad += 1;
      const seal = document.querySelector(`#messages .seal[data-msg-id="${id}"]`);
      if (seal) {
        describeSeal(seal, ok ? "verified" : "tampered", (messageStore.find((m) => m.id === id) || {}).block_index);
        if (step) {
          seal.classList.remove("pop");
          void seal.offsetWidth;   // restart the animation
          seal.classList.add("pop");
        }
      }
      if (step) await sleep(step);
    }

    const total = outcome.length;
    if (bad === 0) {
      showVerifyBanner(
        total === 1 ? "The message is verified. Nothing has been changed." : `All ${total} messages are verified. Nothing has been changed.`,
        false
      );
    } else {
      showVerifyBanner(
        `${bad} of ${total} ${total === 1 ? "message" : "messages"} failed verification and may have been changed after sending.`,
        true
      );
    }
  } finally {
    verifying = false;
    setBusy(btn, false);
    btn.querySelector(".btn-label").textContent = "Verify chat";
  }
}

function onSealClick(event) {
  const seal = event.target.closest(".seal");
  if (!seal) return;
  const info = seal.closest(".bubble").querySelector(".block-info");
  const open = info.hidden;
  info.hidden = !open;
  seal.setAttribute("aria-expanded", String(open));
  const id = Number(seal.dataset.msgId);
  if (open) expandedBlocks.add(id); else expandedBlocks.delete(id);
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

function bindEvents() {
  // Auth
  $("auth-form").addEventListener("submit", submitAuth);
  $("tab-login").addEventListener("click", () => setMode("login"));
  $("tab-register").addEventListener("click", () => setMode("register"));
  for (const id of ["tab-login", "tab-register"]) {
    $(id).addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      const next = authMode === "login" ? "register" : "login";
      setMode(next);
      $(`tab-${next}`).focus();
    });
  }
  $("back-to-login").addEventListener("click", () => setMode("login"));
  $("toggle-password").addEventListener("click", () => {
    const pw = $("password");
    const show = pw.type === "password";
    pw.type = show ? "text" : "password";
    const btn = $("toggle-password");
    btn.setAttribute("aria-pressed", String(show));
    btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
    setIcon(btn, show ? "eye-off" : "eye");
  });
  for (const [key, { input }] of Object.entries(AUTH_FIELDS)) {
    $(input).addEventListener("input", () => {
      if ($(input).getAttribute("aria-invalid") === "true") setFieldError(key, "");
    });
  }
  $("setup-form").addEventListener("submit", createProfile);
  $("setup-username").addEventListener("input", () => {
    if ($("setup-username").getAttribute("aria-invalid") === "true") setSetupError("");
  });
  $("setup-signout").addEventListener("click", () => signOut());

  // Sidebar
  $("theme-btn").addEventListener("click", toggleTheme);
  $("signout-btn").addEventListener("click", () => signOut());
  $("chat-search").addEventListener("input", onSearchInput);
  $("chat-search").addEventListener("keydown", onSearchKeydown);
  $("chat-search-clear").addEventListener("click", () => {
    $("chat-search").value = "";
    onSearchInput();
    $("chat-search").focus();
  });
  $("new-chat-row").addEventListener("click", () => startChat(searchQuery()));

  // Thread
  $("back-btn").addEventListener("click", onBackButton);
  $("verify-btn").addEventListener("click", verifyChain);
  $("verify-dismiss").addEventListener("click", () => { $("verify-banner").hidden = true; });
  $("lock-btn").addEventListener("click", onLockButton);
  $("unlock-panel").addEventListener("submit", applyPassphrase);
  $("unlock-cancel").addEventListener("click", cancelUnlock);
  $("messages").addEventListener("click", onSealClick);
  $("jump-btn").addEventListener("click", () => { scrollToBottom(); $("jump-btn").hidden = true; });
  $("messages-wrap").addEventListener("scroll", () => {
    const w = $("messages-wrap");
    if (w.scrollHeight - w.scrollTop - w.clientHeight < 96) $("jump-btn").hidden = true;
  }, { passive: true });

  // Composer
  const ta = $("message-text");
  ta.addEventListener("input", () => { autosizeComposer(); updateSendState(); });
  ta.addEventListener("keydown", (e) => {
    // Desktop: Enter sends, Shift+Enter adds a line. Touch keyboards: Enter adds a line.
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && !isTouchKeyboard()) {
      e.preventDefault();
      sendMessage();
    }
  });
  ta.addEventListener("paste", (e) => {
    const file = [...(e.clipboardData ? e.clipboardData.files : [])][0];
    if (file) { e.preventDefault(); setPendingFile(file); }
  });
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); sendMessage(); });
  $("attach-btn").addEventListener("click", () => $("media-file").click());
  $("media-file").addEventListener("change", (e) => setPendingFile(e.target.files[0]));
  $("attachment-remove").addEventListener("click", clearAttachment);

  // Global
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && unlockOpen && derivedKeys.has(currentConversationId)) cancelUnlock();
  });
  window.addEventListener("popstate", () => {
    historyPushed = false;
    if ($("chat-shell").dataset.view === "thread" && isNarrow()) leaveThread();
  });
  window.addEventListener("offline", () => { $("net-banner").hidden = false; });
  window.addEventListener("online", () => {
    $("net-banner").hidden = true;
    if (accessToken) { loadConversations(); refreshMessages(); }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && accessToken && currentConversationId !== null && !realtimeConnected) refreshMessages();
  });
  if (navigator.onLine === false) $("net-banner").hidden = false;
}

// ---------------------------------------------------------------------------
// WebRTC calling — SKETCH ONLY, not wired into the UI.
//
// Signalling runs over a Supabase Realtime Broadcast channel (peer-to-peer
// media never touches the server once connected). Before this can be used for
// real it still needs: an `ontrack` handler that plays the remote stream, a
// "ringing" handshake so the offer isn't lost if the callee isn't listening
// yet, call UI (buttons/accept/hang-up), and a TURN server for restrictive
// networks. Left as-is deliberately so it isn't mistaken for a finished feature.
// ---------------------------------------------------------------------------

let peerConnection = null;

function callChannelName(conversationId) {
  return `call-${conversationId}`;
}

async function startCall(conversationId, calleeId, callType = "voice") {
  await apiFetch("/calls/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ conversation_id: conversationId, callee_id: calleeId, call_type: callType }),
  });

  const localStream = await navigator.mediaDevices.getUserMedia({
    audio: true, video: callType === "video",
  });

  peerConnection = new RTCPeerConnection({
    iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
  });
  localStream.getTracks().forEach((track) => peerConnection.addTrack(track, localStream));

  const channel = ensureSupabaseClient().channel(callChannelName(conversationId));

  peerConnection.onicecandidate = (event) => {
    if (event.candidate) {
      channel.send({ type: "broadcast", event: "ice-candidate", payload: event.candidate });
    }
  };

  channel
    .on("broadcast", { event: "answer" }, async ({ payload }) => {
      await peerConnection.setRemoteDescription(new RTCSessionDescription(payload));
    })
    .on("broadcast", { event: "ice-candidate" }, async ({ payload }) => {
      try { await peerConnection.addIceCandidate(payload); } catch (_) {}
    })
    .subscribe(async (status) => {
      if (status !== "SUBSCRIBED") return;
      const offer = await peerConnection.createOffer();
      await peerConnection.setLocalDescription(offer);
      channel.send({ type: "broadcast", event: "offer", payload: offer });
    });
}

async function listenForIncomingCalls(conversationId, callType = "voice") {
  const channel = ensureSupabaseClient().channel(callChannelName(conversationId));

  channel
    .on("broadcast", { event: "offer" }, async ({ payload }) => {
      const accept = confirm("Incoming call — accept?");
      if (!accept) return;

      const localStream = await navigator.mediaDevices.getUserMedia({
        audio: true, video: callType === "video",
      });

      peerConnection = new RTCPeerConnection({
        iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
      });
      localStream.getTracks().forEach((t) => peerConnection.addTrack(t, localStream));
      peerConnection.onicecandidate = (event) => {
        if (event.candidate) {
          channel.send({ type: "broadcast", event: "ice-candidate", payload: event.candidate });
        }
      };

      await peerConnection.setRemoteDescription(new RTCSessionDescription(payload));
      const answer = await peerConnection.createAnswer();
      await peerConnection.setLocalDescription(answer);
      channel.send({ type: "broadcast", event: "answer", payload: answer });
    })
    .on("broadcast", { event: "ice-candidate" }, async ({ payload }) => {
      if (peerConnection) {
        try { await peerConnection.addIceCandidate(payload); } catch (_) {}
      }
    })
    .subscribe();
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

(async function init() {
  applyTheme(document.documentElement.dataset.theme || "dark");
  bindEvents();
  setMode("login");
  try {
    await loadPublicConfig();
    await restoreSession();
  } catch (err) {
    console.error(err);
  } finally {
    // Nothing restored: show the login form.
    if ($("auth-view").hidden && $("setup-view").hidden && $("chat-shell").hidden) {
      showView("auth");
      if (canHover()) $("email").focus();
    }
    $("boot").hidden = true;
  }
})();

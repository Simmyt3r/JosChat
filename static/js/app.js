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
 */

const API_BASE = "/api";
const SESSION_KEY = "joschat_session";
const PASSPHRASE_KEY_PREFIX = "joschat_pass_";
const POLL_INTERVAL_MS = 4000;

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

// Messages of the open conversation, oldest first. Kept in memory so they can
// be re-rendered when the passphrase changes, and de-duplicated by id (a
// message can arrive via fetch, Realtime and polling).
let messageStore = [];
const derivedKeys = new Map();    // conversationId -> CryptoKey

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

const $ = (id) => document.getElementById(id);

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

function setStatus(message, kind = "") {
  const el = $("auth-status");
  el.textContent = message;
  el.className = "status-line" + (kind ? ` ${kind}` : "");
}

function setChatStatus(message, kind = "") {
  const el = $("chat-status");
  el.textContent = message;
  el.className = "status-line" + (kind ? ` ${kind}` : "");
}

// ---------------------------------------------------------------------------
// Auth panel — tab switching, status messages, submit routing
// ---------------------------------------------------------------------------

function setMode(mode, opts = {}) {
  authMode = mode;
  $("tab-login").classList.toggle("active", mode === "login");
  $("tab-register").classList.toggle("active", mode === "register");
  $("field-username").classList.toggle("hidden", mode !== "register");
  $("password").autocomplete = mode === "login" ? "current-password" : "new-password";
  $("submit-btn").textContent = mode === "login" ? "Log in" : "Create account";
  if (opts.clearStatus !== false) setStatus("");
}

async function submitAuth() {
  const btn = $("submit-btn");
  btn.disabled = true;
  try {
    if (authMode === "login") await login();
    else await register();
  } catch (err) {
    console.error(err);
    setStatus("Could not reach the server. Check your connection and try again.", "error");
  } finally {
    btn.disabled = false;
  }
}

async function readJson(res) {
  try {
    return await res.json();
  } catch {
    return { error: `Unexpected response from server (HTTP ${res.status})` };
  }
}

async function register() {
  const username = $("username").value.trim();
  const email = $("email").value.trim();
  const password = $("password").value;

  if (!username || !email || !password) {
    return setStatus("Fill in username, email, and password.", "error");
  }

  setStatus("Creating account…");
  const res = await fetch(`${API_BASE}/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, email, password }),
  });
  const data = await readJson(res);
  if (!res.ok) return setStatus(data.error || "Registration failed.", "error");

  setMode("login", { clearStatus: false });
  setStatus(data.message || "Account created — log in below.", "success");
}

async function login() {
  const email = $("email").value.trim();
  const password = $("password").value;

  if (!email || !password) {
    return setStatus("Enter your email and password.", "error");
  }

  setStatus("Signing in…");
  const res = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const data = await readJson(res);
  if (!res.ok) return setStatus(data.error || "Login failed.", "error");

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

  $("auth-panel").classList.add("hidden");
  $("chat-panel").classList.remove("hidden");
  setChatStatus("");

  if (!currentProfile) {
    // Authenticated, but no profiles row (e.g. an earlier sign-up that failed
    // half-way). Let the user finish instead of being locked out.
    $("whoami").textContent = "(username not set)";
    $("profile-setup").classList.remove("hidden");
    $("chat-main").classList.add("hidden");
    return;
  }
  showChatHome();
}

function showChatHome() {
  $("whoami").textContent = currentProfile.username;
  $("profile-setup").classList.add("hidden");
  $("chat-main").classList.remove("hidden");
  loadConversations();
}

async function createProfile() {
  const username = $("setup-username").value.trim();
  if (!username) return setChatStatus("Choose a username.", "error");
  const res = await apiFetch("/auth/profile", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username }),
  });
  if (!res) return;
  const data = await readJson(res);
  if (!res.ok) return setChatStatus(data.error || "Could not save username.", "error");
  currentProfile = data.profile;
  showChatHome();
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
    setChatStatus("Network error — check your connection.", "error");
    return null;
  }
  if (res.status === 401 && await refreshSession()) {
    try { res = await send(); } catch { return null; }
  }
  if (res.status === 401) {
    signOut("Your session expired — please log in again.");
    return null;
  }
  return res;
}

async function restoreSession() {
  let saved = null;
  try { saved = JSON.parse(sessionStorage.getItem(SESSION_KEY) || "null"); } catch { /* ignore */ }
  if (!saved || !saved.accessToken) return;
  accessToken = saved.accessToken;
  refreshToken = saved.refreshToken || null;

  const res = await apiFetch("/auth/me");
  if (res && res.ok) {
    const data = await res.json();
    return startSession({ access_token: accessToken, refresh_token: refreshToken, profile: data.profile });
  }
  if (res && res.status === 404) {
    const body = await readJson(res);
    if (body.code === "profile_missing") {
      return startSession({ access_token: accessToken, refresh_token: refreshToken, profile: null });
    }
  }
  if (res) signOut();   // (a null res means apiFetch already signed us out)
}

async function signOut(message) {
  const token = accessToken;
  closeConversation();
  accessToken = refreshToken = currentProfile = null;
  messageStore = [];
  derivedKeys.clear();
  try { sessionStorage.removeItem(SESSION_KEY); } catch { /* ignore */ }
  if (token) {
    // Best effort; the token may already be dead.
    fetch(`${API_BASE}/auth/logout`, { method: "POST", headers: { Authorization: `Bearer ${token}` } })
      .catch(() => {});
  }
  $("chat-panel").classList.add("hidden");
  $("auth-panel").classList.remove("hidden");
  $("conversation-list").innerHTML = "";
  $("conversation-view").classList.add("hidden");
  setMode("login", { clearStatus: false });
  setStatus(typeof message === "string" ? message : "", typeof message === "string" ? "error" : "");
}

// ---------------------------------------------------------------------------
// Conversations
// ---------------------------------------------------------------------------

function conversationLabel(conv) {
  if (conv.title) return conv.title;
  return (conv.other_usernames || []).map((n) => `@${n}`).join(", ") || `Conversation ${conv.id}`;
}

async function loadConversations() {
  const res = await apiFetch("/conversations");
  if (!res) return;
  const data = await readJson(res);
  if (!res.ok) return setChatStatus(data.error || "Could not load conversations.", "error");

  const list = $("conversation-list");
  list.innerHTML = "";
  if (!data.conversations.length) {
    const li = document.createElement("li");
    li.className = "conv-empty";
    li.textContent = "No chats yet — start one with a username above.";
    list.appendChild(li);
    return;
  }
  for (const conv of data.conversations) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn-secondary conv-item" + (conv.id === currentConversationId ? " active" : "");
    btn.textContent = conversationLabel(conv);
    btn.addEventListener("click", () => openConversation(conv.id, conversationLabel(conv)));
    li.appendChild(btn);
    list.appendChild(li);
  }
}

async function startChat() {
  const username = $("new-chat-username").value.trim().replace(/^@/, "");
  if (!username) return setChatStatus("Enter the username of the person you want to chat with.", "error");
  setChatStatus("");

  const res = await apiFetch("/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ participant_usernames: [username] }),
  });
  if (!res) return;
  const data = await readJson(res);
  if (!res.ok) return setChatStatus(data.error || "Could not start chat.", "error");

  $("new-chat-username").value = "";
  await loadConversations();
  openConversation(data.conversation.id, `@${username}`);
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

async function decryptText(b64) {
  const key = getConversationKey();
  if (!key) return null;                       // locked
  try {
    const combined = base64ToBytes(b64);
    const iv = combined.slice(0, 12);
    const ciphertext = combined.slice(12);
    const plaintext = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, key, ciphertext);
    return new TextDecoder().decode(plaintext);
  } catch {
    return undefined;                          // wrong key / corrupt data
  }
}

async function applyPassphrase() {
  const passphrase = $("conv-passphrase").value;
  if (!passphrase) return setChatStatus("Enter the shared passphrase for this chat.", "error");
  setChatStatus("");
  derivedKeys.set(currentConversationId, await deriveKey(passphrase, currentConversationId));
  try { sessionStorage.setItem(PASSPHRASE_KEY_PREFIX + currentConversationId, passphrase); } catch { /* ignore */ }
  $("conv-passphrase").value = "";
  $("conv-passphrase").placeholder = "Passphrase set — enter a new one to change it";
  await renderAllMessages();
}

// ---------------------------------------------------------------------------
// Messaging
// ---------------------------------------------------------------------------

async function openConversation(conversationId, label) {
  closeConversation();
  currentConversationId = conversationId;
  messageStore = [];
  $("messages").innerHTML = "";
  $("verify-result").textContent = "";
  $("conv-title").textContent = label || `#${conversationId}`;
  $("conversation-view").classList.remove("hidden");
  setChatStatus("");

  // Restore this tab's passphrase for the conversation, if it was entered before.
  $("conv-passphrase").value = "";
  $("conv-passphrase").placeholder = "Shared passphrase for this chat";
  let saved = null;
  try { saved = sessionStorage.getItem(PASSPHRASE_KEY_PREFIX + conversationId); } catch { /* ignore */ }
  if (saved && !derivedKeys.has(conversationId)) {
    derivedKeys.set(conversationId, await deriveKey(saved, conversationId));
  }
  if (derivedKeys.has(conversationId)) {
    $("conv-passphrase").placeholder = "Passphrase set — enter a new one to change it";
  }

  // Who is in this conversation (for sender names).
  currentParticipants = {};
  const infoRes = await apiFetch(`/conversations/${conversationId}`);
  if (!infoRes) return;
  if (infoRes.ok) {
    const info = await infoRes.json();
    for (const p of info.participants) currentParticipants[p.user_id] = p.username;
  }

  const res = await apiFetch(`/messages/${conversationId}`);
  if (!res) return;
  const data = await readJson(res);
  if (!res.ok) return setChatStatus(data.error || "Could not load messages.", "error");
  if (currentConversationId !== conversationId) return;   // user switched chats meanwhile

  await addMessages(data.messages);
  loadConversations();   // refresh highlight

  subscribeToConversation(conversationId);
  startPolling(conversationId);
}

function closeConversation() {
  if (realtimeChannel && supabaseClient) supabaseClient.removeChannel(realtimeChannel);
  realtimeChannel = null;
  realtimeConnected = false;
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
  currentConversationId = null;
  setLiveStatus("");
}

function setLiveStatus(text) {
  const el = $("live-status");
  if (el) el.textContent = text;
}

function subscribeToConversation(conversationId) {
  const client = ensureSupabaseClient();
  if (!client) {
    setLiveStatus("· checking for new messages every few seconds");
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
      setLiveStatus(realtimeConnected ? "· live" : "· reconnecting… (checking every few seconds)");
    });
}

// Polling is the safety net, not the primary path: it only fetches while the
// Realtime channel is not connected, so a working setup makes no extra requests.
function startPolling(conversationId) {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    if (realtimeConnected || currentConversationId !== conversationId || document.hidden) return;
    const res = await apiFetch(`/messages/${conversationId}`);
    if (!res || !res.ok || currentConversationId !== conversationId) return;
    const data = await res.json();
    addMessages(data.messages);
  }, POLL_INTERVAL_MS);
}

async function addMessages(messages) {
  const known = new Set(messageStore.map((m) => m.id));
  const fresh = messages.filter((m) => !known.has(m.id));
  if (!fresh.length) return;
  messageStore.push(...fresh);
  messageStore.sort((a, b) => a.id - b.id);
  await renderAllMessages();
}

async function renderAllMessages() {
  const list = $("messages");
  list.innerHTML = "";
  for (const msg of messageStore) list.appendChild(await buildMessageItem(msg));
  list.scrollTop = list.scrollHeight;
}

async function buildMessageItem(msg) {
  const plaintext = await decryptText(msg.encrypted_content);
  const mine = currentProfile && msg.sender_id === currentProfile.id;

  const li = document.createElement("li");
  li.className = mine ? "mine" : "theirs";

  const who = document.createElement("div");
  who.className = "sender";
  who.textContent = mine ? "You" : `@${currentParticipants[msg.sender_id] || "unknown"}`;
  li.appendChild(who);

  // textContent (never innerHTML): decrypted text comes from another user and
  // must not be able to inject markup/script into this page.
  const body = document.createElement("div");
  if (plaintext === null) {
    body.textContent = "🔒 Enter the chat's shared passphrase above to read this message.";
    body.className = "locked";
  } else if (plaintext === undefined) {
    body.textContent = "[unable to decrypt — the passphrase may not match]";
    body.className = "locked";
  } else {
    body.textContent = plaintext;
  }
  li.appendChild(body);

  const safeUrl = safeHttpsUrl(msg.media_url);
  if (safeUrl) {
    const wrap = document.createElement("div");
    const a = document.createElement("a");
    a.href = safeUrl;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = "📎 attachment";
    wrap.appendChild(a);
    li.appendChild(wrap);
  }

  const meta = document.createElement("div");
  meta.className = "verified";
  meta.textContent = `block #${msg.block_index} · ${String(msg.block_hash).slice(0, 12)}…`;
  li.appendChild(meta);
  return li;
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

async function sendMessage() {
  if (!currentConversationId) return setChatStatus("Open a conversation first.", "error");
  if (!getConversationKey()) return setChatStatus("Enter the shared passphrase for this chat first.", "error");

  const text = $("message-text").value;
  const fileInput = $("media-file");
  if (!text.trim() && fileInput.files.length === 0) return;

  const sendBtn = $("send-btn");
  sendBtn.disabled = true;
  setChatStatus("");
  const conversationId = currentConversationId;

  try {
    let media_url = null, media_public_id = null;
    if (fileInput.files.length > 0) {
      const form = new FormData();
      form.append("file", fileInput.files[0]);
      form.append("conversation_id", conversationId);
      const uploadRes = await apiFetch("/media/upload", { method: "POST", body: form });
      if (!uploadRes) return;
      const uploadData = await readJson(uploadRes);
      if (!uploadRes.ok) return setChatStatus(`Upload failed: ${uploadData.error}`, "error");
      media_url = uploadData.secure_url;
      media_public_id = uploadData.public_id;
    }

    const encrypted_content = await encryptText(text.trim() || "(attachment)");

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
    if (!res.ok) return setChatStatus(`Send failed: ${data.error}`, "error");

    $("message-text").value = "";
    fileInput.value = "";
    // Show it immediately; the Realtime/poll copy of the same row is
    // de-duplicated by id, so it never appears twice.
    if (currentConversationId === conversationId) await addMessages([data.message]);
  } finally {
    sendBtn.disabled = false;
  }
}

async function verifyChain() {
  if (!currentConversationId) return;
  const res = await apiFetch(`/messages/verify/${currentConversationId}`);
  if (!res) return;
  const data = await readJson(res);
  if (!res.ok) {
    $("verify-result").textContent = data.error || "Verification failed.";
    return;
  }
  $("verify-result").textContent = data.all_verified
    ? "✅ All messages verified — blockchain intact."
    : "🔴 Tampering detected in one or more messages!";
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
  await loadPublicConfig();
  await restoreSession();
})();

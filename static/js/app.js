/**
 * static/js/app.js
 * -----------------
 * Minimal reference client demonstrating the full Joschat flow:
 *   1. Auth against the Flask API (which wraps Supabase Auth).
 *   2. Sending a message: encrypt client-side, POST to /api/messages/send.
 *   3. Receiving messages LIVE via a direct Supabase Realtime subscription
 *      (no Flask/Socket.IO server involved at all).
 *   4. Uploading media through /api/media/upload (Cloudinary).
 *   5. A sketch of WebRTC call signalling over a Supabase Realtime
 *      Broadcast channel.
 *
 * ENCRYPTION NOTE: for demo purposes this uses a single symmetric AES-GCM
 * key shared between conversation participants (entered once and stored
 * in localStorage), NOT a full public-key E2EE handshake. A production
 * build should perform proper key exchange using each user's `public_key`
 * column in `profiles` (e.g. X25519 + HKDF, mirroring the Signal
 * Protocol's design) before this line of code is trusted with anything
 * sensitive.
 */

let accessToken = null;
let currentProfile = null;
let supabaseClient = null;
let currentConversationId = null;
let realtimeChannel = null;
let authMode = "login";

const API_BASE = "/api";

async function loadPublicConfig() {
  const res = await fetch(`${API_BASE}/config`);
  const config = await res.json();
  supabaseClient = supabase.createClient(config.supabase_url, config.supabase_anon_key);
}

// ---------------------------------------------------------------------------
// Auth panel — tab switching, status messages, submit routing
// ---------------------------------------------------------------------------

function setStatus(message, kind = "") {
  const el = document.getElementById("auth-status");
  el.textContent = message;
  el.className = "status-line" + (kind ? ` ${kind}` : "");
}

function setMode(mode, opts = {}) {
  authMode = mode;
  document.getElementById("tab-login").classList.toggle("active", mode === "login");
  document.getElementById("tab-register").classList.toggle("active", mode === "register");
  document.getElementById("field-username").classList.toggle("hidden", mode !== "register");
  document.getElementById("submit-btn").textContent = mode === "login" ? "Log in" : "Create account";
  if (opts.clearStatus !== false) setStatus("");
}

async function submitAuth() {
  const btn = document.getElementById("submit-btn");
  btn.disabled = true;
  try {
    if (authMode === "login") await login();
    else await register();
  } finally {
    btn.disabled = false;
  }
}

async function register() {
  const username = document.getElementById("username").value.trim();
  const email = document.getElementById("email").value.trim();
  const password = document.getElementById("password").value;

  if (!username || !email || !password) {
    return setStatus("Fill in username, email, and password.", "error");
  }

  setStatus("Creating account…");
  const res = await fetch(`${API_BASE}/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, email, password }),
  });
  const data = await res.json();
  if (!res.ok) return setStatus(data.error || "Registration failed.", "error");

  setStatus("Account created — log in below.", "success");
  setMode("login", { clearStatus: false });
}

async function login() {
  const email = document.getElementById("email").value.trim();
  const password = document.getElementById("password").value;

  if (!email || !password) {
    return setStatus("Enter your email and password.", "error");
  }

  setStatus("Signing in…");
  const res = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const data = await res.json();
  if (!res.ok) return setStatus(data.error || "Login failed.", "error");

  accessToken = data.access_token;
  currentProfile = data.profile;
  document.getElementById("whoami").textContent = currentProfile.username;
  document.getElementById("auth-panel").classList.add("hidden");
  document.getElementById("chat-panel").classList.remove("hidden");
}

function authHeaders(extra = {}) {
  return { Authorization: `Bearer ${accessToken}`, ...extra };
}

// ---------------------------------------------------------------------------
// Messaging
// ---------------------------------------------------------------------------

async function getDemoKey() {
  // Derives (or retrieves) a demo AES-GCM key from a passphrase stored in
  // localStorage. Replace with real per-conversation key exchange in
  // production — see the module docstring above.
  let raw = localStorage.getItem("joschat_demo_key");
  if (!raw) {
    raw = crypto.randomUUID();
    localStorage.setItem("joschat_demo_key", raw);
  }
  const keyMaterial = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(raw.padEnd(32, "0").slice(0, 32)),
    "AES-GCM", false, ["encrypt", "decrypt"]
  );
  return keyMaterial;
}

async function encryptText(plaintext) {
  const key = await getDemoKey();
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv }, key, new TextEncoder().encode(plaintext)
  );
  const combined = new Uint8Array(iv.length + ciphertext.byteLength);
  combined.set(iv, 0);
  combined.set(new Uint8Array(ciphertext), iv.length);
  return btoa(String.fromCharCode(...combined));
}

async function decryptText(b64) {
  try {
    const key = await getDemoKey();
    const combined = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const iv = combined.slice(0, 12);
    const ciphertext = combined.slice(12);
    const plaintext = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, key, ciphertext);
    return new TextDecoder().decode(plaintext);
  } catch {
    return "[unable to decrypt]";
  }
}

async function openConversation() {
  currentConversationId = parseInt(document.getElementById("conversation-id").value, 10);
  document.getElementById("messages").innerHTML = "";

  const res = await fetch(`${API_BASE}/messages/${currentConversationId}`, {
    headers: authHeaders(),
  });
  const data = await res.json();
  if (!res.ok) return alert(`Error: ${data.error}`);

  for (const msg of data.messages) await renderMessage(msg);

  // Live updates: subscribe directly to Supabase Realtime for INSERTs on
  // this conversation. No Flask/Socket.IO involved.
  if (realtimeChannel) supabaseClient.removeChannel(realtimeChannel);
  realtimeChannel = supabaseClient
    .channel(`conversation-${currentConversationId}`)
    .on(
      "postgres_changes",
      {
        event: "INSERT",
        schema: "public",
        table: "messages",
        filter: `conversation_id=eq.${currentConversationId}`,
      },
      (payload) => renderMessage(payload.new)
    )
    .subscribe();
}

async function renderMessage(msg) {
  const plaintext = await decryptText(msg.encrypted_content);
  const li = document.createElement("li");
  li.innerHTML = `
    <div>${plaintext}</div>
    ${msg.media_url ? `<div><a href="${msg.media_url}" target="_blank">📎 attachment</a></div>` : ""}
    <div class="verified">block #${msg.block_index} · ${msg.block_hash.slice(0, 12)}…</div>
  `;
  document.getElementById("messages").appendChild(li);
  document.getElementById("messages").scrollTop = 1e9;
}

async function sendMessage() {
  const text = document.getElementById("message-text").value;
  const fileInput = document.getElementById("media-file");
  if (!currentConversationId) return alert("Open a conversation first");

  let media_url = null, media_public_id = null;
  if (fileInput.files.length > 0) {
    const form = new FormData();
    form.append("file", fileInput.files[0]);
    form.append("conversation_id", currentConversationId);
    const uploadRes = await fetch(`${API_BASE}/media/upload`, {
      method: "POST",
      headers: authHeaders(),
      body: form,
    });
    const uploadData = await uploadRes.json();
    if (!uploadRes.ok) return alert(`Upload failed: ${uploadData.error}`);
    media_url = uploadData.secure_url;
    media_public_id = uploadData.public_id;
  }

  const encrypted_content = await encryptText(text || "(attachment)");

  const res = await fetch(`${API_BASE}/messages/send`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({
      conversation_id: currentConversationId,
      encrypted_content,
      media_url,
      media_public_id,
    }),
  });
  const data = await res.json();
  if (!res.ok) return alert(`Send failed: ${data.error}`);

  document.getElementById("message-text").value = "";
  fileInput.value = "";
  // No need to manually render — the Realtime subscription above will
  // deliver this exact row back to us (and to the other participant).
}

async function verifyChain() {
  if (!currentConversationId) return;
  const res = await fetch(`${API_BASE}/messages/verify/${currentConversationId}`, {
    headers: authHeaders(),
  });
  const data = await res.json();
  document.getElementById("verify-result").textContent = data.all_verified
    ? "✅ All messages verified — blockchain intact."
    : "🔴 Tampering detected in one or more messages!";
}

// ---------------------------------------------------------------------------
// WebRTC calling — signalling over a Supabase Realtime Broadcast channel
// (peer-to-peer media never touches the server once connected).
// ---------------------------------------------------------------------------

let peerConnection = null;

function callChannelName(conversationId) {
  return `call-${conversationId}`;
}

async function startCall(conversationId, calleeId, callType = "voice") {
  await fetch(`${API_BASE}/calls/start`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ conversation_id: conversationId, callee_id: calleeId, call_type: callType }),
  });

  const localStream = await navigator.mediaDevices.getUserMedia({
    audio: true, video: callType === "video",
  });

  peerConnection = new RTCPeerConnection({
    iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
  });
  localStream.getTracks().forEach((track) => peerConnection.addTrack(track, localStream));

  const channel = supabaseClient.channel(callChannelName(conversationId));

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
  const channel = supabaseClient.channel(callChannelName(conversationId));

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

// Entry point
loadPublicConfig();

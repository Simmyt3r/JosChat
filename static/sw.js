/* Only encrypted message bodies enter this durable outbox. No plaintext, keys,
 * attachments or refresh tokens are persisted here. Access tokens expire normally. */
const CACHE_NAME = "joschat-cache-v8";
const OUTBOX_DB = "joschat-outbox";
const SYNC_TAG = "joschat-send-messages";
const OFFLINE_URLS = ["/", "/static/js/app.js", "/static/js/background.js", "/static/js/crypto.js",
  "/static/css/style.css", "/static/manifest.json", "/static/favicon.ico",
  "/static/icons/icon-192.png", "/static/icons/icon-512.png"];
const clientOwners = new Map();
let flushInFlight = null;

function openOutbox() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(OUTBOX_DB, 1);
    req.onupgradeneeded = () => req.result.createObjectStore("messages", { keyPath: "client_message_id" });
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
async function outboxOperation(mode, operation) {
  const db = await openOutbox();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction("messages", mode);
      const req = operation(tx.objectStore("messages"));
      tx.oncomplete = () => resolve(req.result);
      tx.onerror = tx.onabort = () => reject(tx.error || new Error("Outbox write failed"));
    });
  } finally { db.close(); }
}
const saveQueued = (row) => outboxOperation("readwrite", (store) => store.put(row));
const deleteQueued = (id) => outboxOperation("readwrite", (store) => store.delete(id));
const allQueued = () => outboxOperation("readonly", (store) => store.getAll());
// Update only if the row still exists: logout/discard must not be undone by an
// in-flight send finishing later.
async function updateQueued(row) {
  const db = await openOutbox();
  try {
    await new Promise((resolve, reject) => {
      const tx = db.transaction("messages", "readwrite");
      const store = tx.objectStore("messages");
      const req = store.get(row.client_message_id);
      req.onsuccess = () => { if (req.result) store.put(row); };
      tx.oncomplete = resolve;
      tx.onerror = tx.onabort = () => reject(tx.error);
    });
  } finally { db.close(); }
}
async function notifyOwner(owner, type, detail = {}) {
  const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const client of clients) {
    if (clientOwners.get(client.id) === owner) client.postMessage({ type, owner_id: owner, ...detail });
  }
}
async function registerSync() {
  try { if (self.registration.sync) await self.registration.sync.register(SYNC_TAG); } catch { /* foreground retry remains available */ }
}
async function deliver(row) {
  let res;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    res = await fetch("/api/messages/send", { method: "POST", signal: controller.signal,
      headers: { "Content-Type": "application/json", Authorization: row.authorization,
        "X-Joschat-Owner": row.owner_id }, body: JSON.stringify(row.payload) });
  } catch {
    await updateQueued({ ...row, status: "queued" });
    return null;
  } finally { clearTimeout(timer); }
  const data = await res.clone().json().catch(() => ({}));
  if (res.ok && data.message) {
    await deleteQueued(row.client_message_id);
    await notifyOwner(row.owner_id, "OUTBOX_SENT", { message: data.message, client_message_id: row.client_message_id });
  } else if (res.status === 401) {
    await updateQueued({ ...row, status: "auth-required" });
    await notifyOwner(row.owner_id, "OUTBOX_AUTH_REQUIRED");
  } else if (res.status >= 500 || res.status === 429 || (res.ok && !data.message)) {
    await updateQueued({ ...row, status: "queued" });
    return null;
  } else {
    await updateQueued({ ...row, status: "failed", error: data.error || `HTTP ${res.status}` });
    await notifyOwner(row.owner_id, "OUTBOX_FAILED", { client_message_id: row.client_message_id, error: data.error });
  }
  return res;
}
function flushOutbox() {
  if (flushInFlight) return flushInFlight;
  flushInFlight = (async () => {
    const blocked = new Set();
    const responses = new Map();
    const rows = (await allQueued()).sort((a, b) => a.created_at.localeCompare(b.created_at));
    for (const row of rows) {
      if (blocked.has(row.owner_id)) continue;
      if (row.status !== "queued") { blocked.add(row.owner_id); continue; }
      const res = await deliver(row);
      responses.set(row.client_message_id, res);
      if (!res || !res.ok) blocked.add(row.owner_id); // preserve ordering across reconnects
    }
    // Throw so supported browsers retry transient network errors using their own
    // background-sync backoff; permanent/auth failures await user intervention.
    return { responses, retryNeeded: (await allQueued()).some((r) => r.status === "queued") };
  })().finally(() => { flushInFlight = null; });
  return flushInFlight;
}
async function handleSend(event) {
  const req = event.request;
  const payload = await req.clone().json().catch(() => null);
  const owner = req.headers.get("X-Joschat-Owner");
  const authorization = req.headers.get("Authorization");
  if (!owner || !authorization || !payload ||
      !/^[0-9a-f-]{36}$/i.test(payload.client_message_id || "") ||
      typeof payload.encrypted_content !== "string") return fetch(req);
  clientOwners.set(event.clientId, owner);
  const row = { client_message_id: payload.client_message_id, owner_id: owner,
    authorization, payload, status: "queued", created_at: new Date().toISOString() };
  try { await saveQueued(row); } catch {
    // Never claim to have queued a message when durable storage failed.
    return fetch(req);
  }
  // Flush oldest first, including this message. The server's stable client id
  // makes replay safe if a response is lost or another tab sends concurrently.
  const result = await flushOutbox();
  const res = result.responses.get(row.client_message_id);
  if (res) {
    if (!res.ok && res.status !== 401) await deleteQueued(row.client_message_id);
    return res;
  }
  await registerSync();
  return new Response(JSON.stringify({ queued: true, client_message_id: row.client_message_id,
    created_at: row.created_at }), { status: 202, headers: { "Content-Type": "application/json" } });
}

self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (!data.type || !data.type.startsWith("OUTBOX_") || !data.owner_id) return;
  const reply = (value) => { if (event.ports[0]) event.ports[0].postMessage(value); };
  event.waitUntil((async () => {
    if (event.source) clientOwners.set(event.source.id, data.owner_id);
    if (data.type === "OUTBOX_SESSION") {
      if (!data.access_token) return;
      for (const row of await allQueued()) {
        if (row.owner_id === data.owner_id) await updateQueued({ ...row,
          authorization: `Bearer ${data.access_token}`,
          status: row.status === "auth-required" || data.retry ? "queued" : row.status });
      }
      await flushOutbox().catch(() => {});
    } else if (data.type === "OUTBOX_CLEAR") {
      for (const row of await allQueued()) if (row.owner_id === data.owner_id) await deleteQueued(row.client_message_id);
      if (event.source) clientOwners.delete(event.source.id);
    } else if (data.type === "OUTBOX_DISCARD") {
      const row = (await allQueued()).find((r) => r.client_message_id === data.client_message_id && r.owner_id === data.owner_id);
      if (row) await deleteQueued(row.client_message_id);
    }
    const rows = (await allQueued()).filter((r) => r.owner_id === data.owner_id).map(({ authorization, ...row }) => row);
    reply({ rows });
  })().catch((err) => reply({ error: err.message })));
});
self.addEventListener("sync", (event) => {
  if (event.tag === SYNC_TAG) event.waitUntil(flushOutbox().then((result) => {
    if (result.retryNeeded) throw new Error("Messages still waiting for connectivity");
  }));
});
self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => Promise.all(OFFLINE_URLS.map((url) => cache.add(url).catch(() => {})))));
  self.skipWaiting();
});
self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((names) => Promise.all(names.filter((n) => n.startsWith("joschat-cache-") && n !== CACHE_NAME).map((n) => caches.delete(n))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (req.method === "POST" && url.pathname === "/api/messages/send") {
    event.respondWith(handleSend(event)); return;
  }
  if (req.method !== "GET" || url.pathname.startsWith("/api/")) return;
  // Cache the public shell and static assets only; never authenticated pages.
  if (url.pathname !== "/" && !url.pathname.startsWith("/static/")) return;
  event.respondWith(fetch(req).then((res) => {
    if (res.ok) event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.put(req, res.clone())));
    return res;
  }).catch(() => caches.match(req)));
});

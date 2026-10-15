/* Group and account controls. app.js owns the shared session and thread state. */
async function groupChange(data) {
  const id = currentConversationId;
  const res = await apiFetch(`/conversations/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
  if (!res) return;
  const result = await readJson(res);
  if (!res.ok) { $("manage-group-error").textContent = result.error || "Could not update the group."; return; }
  closeCallDialog("manage-group-dialog");
  await loadConversations();
  if (result.left) leaveThread();
  else await openConversation(id, result.conversation.title, result.conversation.title, true);
  if (result.passphrase_changed) toast("Membership updated. Agree on a fresh passphrase with the current members.");
}

function showGroupSettings() {
  $("manage-group-error").textContent = "";
  $("rename-group-name").value = $("conv-title").textContent;
  const owner = String(currentGroupOwner) === currentProfile.id;
  $("rename-group-form").hidden = $("add-member-form").hidden = !owner;
  $("manage-member-list").replaceChildren();
  for (const [id, name] of Object.entries(currentParticipants)) {
    const item = document.createElement("li"); item.textContent = "@" + name;
    if (owner && id !== currentProfile.id) {
      const button = document.createElement("button"); button.type = "button"; button.className = "btn btn-ghost btn-compact"; button.textContent = "Remove";
      button.addEventListener("click", () => groupChange({ action: "remove", user_id: id })); item.append(button);
    }
    $("manage-member-list").append(item);
  }
  openCallDialog("manage-group-dialog");
}

async function notificationState() {
  const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  $("enable-notifications-btn").disabled = !supported || !supabaseConfig?.vapid_public_key;
  $("notification-state").textContent = !supported ? "This browser does not support push notifications." :
    !supabaseConfig?.vapid_public_key ? "Notifications are not configured yet." : Notification.permission === "denied" ? "Notifications are blocked. Change this in your browser settings." : "Notifications show a generic alert; message contents stay private.";
}

async function enableNotifications() {
  try {
    if (await Notification.requestPermission() !== "granted") return notificationState();
    const registration = await navigator.serviceWorker.ready;
    let subscription = await registration.pushManager.getSubscription();
    if (!subscription) subscription = await registration.pushManager.subscribe({ userVisibleOnly: true,
      applicationServerKey: base64ToBytes(supabaseConfig.vapid_public_key.replace(/-/g, "+").replace(/_/g, "/")) });
    const res = await apiFetch("/notifications/subscriptions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(subscription.toJSON()) });
    if (!res?.ok) throw new Error("Could not enable notifications for this account.");
    await outboxRequest("NOTIFICATION_OWNER", { owner_id: currentProfile.id });
    $("notification-state").textContent = "Notifications enabled on this device.";
  } catch (e) { $("notification-state").textContent = e.message; }
}

async function disableNotifications(token = accessToken) {
  await outboxRequest("NOTIFICATION_OWNER", { owner_id: null }).catch(() => {});
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return;
  const registration = await navigator.serviceWorker.getRegistration();
  if (!registration) return;
  const sub = await registration.pushManager.getSubscription();
  if (sub) {
    try {
      await fetch("/api/notifications/subscriptions", { method: "DELETE", headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` }, body: JSON.stringify({ endpoint: sub.endpoint }) });
    } finally { await sub.unsubscribe(); }
  }
  $("notification-state").textContent = "Notifications disabled on this device.";
}

function bindCompletionEvents() {
  document.addEventListener("visibilitychange", () => { if (!document.hidden) checkIncomingCall(); });
  $("manage-group-btn").addEventListener("click", showGroupSettings);
  $("read-history-form").addEventListener("submit", async e => {
    e.preventDefault(); const pass=$("history-passphrase").value; if (!pass) return;
    rememberGroupPassphrase(currentConversationId,pass); $("history-passphrase").value="";
    plainCache.clear();
    for (const item of mediaCache.values()) if (item?.blobUrl) URL.revokeObjectURL(item.blobUrl);
    mediaCache.clear(); closeCallDialog("manage-group-dialog");
    await renderAllMessages({forceStick:false}); toast("Earlier messages checked with this passphrase.");
  });
  $("manage-group-close").addEventListener("click", () => closeCallDialog("manage-group-dialog"));
  $("rename-group-form").addEventListener("submit", (e) => { e.preventDefault(); groupChange({ action: "rename", title: $("rename-group-name").value }); });
  $("add-member-form").addEventListener("submit", (e) => { e.preventDefault(); groupChange({ action: "add", username: $("add-member-name").value }); });
  $("leave-group-btn").addEventListener("click", () => groupChange({ action: "leave" }));
  $("profile-btn").addEventListener("click", () => {
    $("edit-username").value = currentProfile.username; $("edit-phone").value = currentProfile.phone_number || "";
    $("edit-profile-error").textContent = ""; notificationState(); openCallDialog("profile-dialog");
  });
  $("profile-close").addEventListener("click", () => closeCallDialog("profile-dialog"));
  $("enable-notifications-btn").addEventListener("click", enableNotifications);
  $("disable-notifications-btn").addEventListener("click", () => disableNotifications().catch((e) => { $("notification-state").textContent = e.message; }));
  $("edit-profile-form").addEventListener("submit", async (e) => {
    e.preventDefault(); const res = await apiFetch("/auth/profile", { method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("edit-username").value, phone_number: $("edit-phone").value }) });
    if (!res) return;
    const data = await readJson(res);
    if (!res.ok) { $("edit-profile-error").textContent = data.error; return; }
    currentProfile = data.profile; $("whoami").textContent = "@" + currentProfile.username; paintAvatar($("me-avatar"), currentProfile.username);
    closeCallDialog("profile-dialog"); toast("Profile saved.", "success");
  });
  // Membership may change on another device. Reopening also enforces key epochs.
  setInterval(async () => {
    if (!currentIsGroup || !currentConversationId || document.hidden || navigator.onLine === false) return;
    const id = currentConversationId;
    const res = await apiFetch(`/conversations/${id}`); if (!res || id !== currentConversationId) return;
    if (res.status === 403) { leaveThread(); await loadConversations(); toast("You are no longer a member of this group."); return; }
    if (!res.ok) return;
    const data = await readJson(res);
    if ((data.conversation.key_epoch || 1) !== currentGroupEpoch || data.conversation.title !== $("conv-title").textContent)
      await openConversation(id, data.conversation.title, data.conversation.title, true);
  }, 15000);
}

function checkMessagePins(messages) {
  if (!currentProfile || !currentConversationId) return;
  const name = `joschat-pins:${currentProfile.id}:${currentConversationId}`;
  let pins; try { pins = JSON.parse(localStorage.getItem(name) || "{}"); } catch { pins = {}; }
  for (const msg of messages) {
    if (msg.queued || !Number.isSafeInteger(msg.block_index)) continue;
    const old = pins[msg.block_index];
    if (old && old !== msg.block_hash) msg.verified = false;
    else if (msg.verified === true) pins[msg.block_index] = msg.block_hash;
  }
  const latest = Object.keys(pins).map(Number).sort((a,b) => b-a).slice(0,200);
  try { localStorage.setItem(name, JSON.stringify(Object.fromEntries(latest.map(i=>[i,pins[i]])))); } catch {}
}

// Reading earlier epochs never changes the active sending key.
const groupReadingKeyCache = new Map();
function rememberGroupPassphrase(id, passphrase) {
  let old; try { old = JSON.parse(sessionStorage.getItem(PASSPHRASE_KEY_PREFIX+id+"_archive") || "[]"); } catch { old=[]; }
  if (!Array.isArray(old)) old=[];
  sessionStorage.setItem(PASSPHRASE_KEY_PREFIX+id+"_archive",JSON.stringify([...new Set([passphrase,...old])].slice(0,5)));
}
async function groupReadingKeys(id,epoch) {
  let phrases; try { phrases=JSON.parse(sessionStorage.getItem(PASSPHRASE_KEY_PREFIX+id+"_archive") || "[]"); } catch { phrases=[]; }
  if (!Array.isArray(phrases)) phrases=[];
  const current=sessionStorage.getItem(PASSPHRASE_KEY_PREFIX+id);
  const unique=[...new Set([current,...phrases].filter(p=>typeof p==="string" && p.length))].slice(0,5);
  return Promise.all(unique.map(pass=>{
    const name=JSON.stringify([id,epoch,pass]);
    if (!groupReadingKeyCache.has(name)) groupReadingKeyCache.set(name,deriveKey(pass,id,epoch));
    return groupReadingKeyCache.get(name);
  }));
}

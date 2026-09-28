// TEST-ONLY stand-in for supabase-js's Realtime client. Real supabase-js is blocked
// in this sandbox (external network), so this exists purely to exercise app.js's own
// channel/broadcast/presence calls end-to-end, over a simple HTTP-polling relay that
// is itself served by the test's mocked /api/** handler. It does not attempt to be a
// faithful Phoenix-protocol implementation.
(function () {
  const senderId = Math.random().toString(36).slice(2);
  function makeChannel(topic) {
    const handlers = [];
    const presence = {};
    let cursor = 0, joined = false, timer = null;
    const ch = {
      on(type, config, cb) { handlers.push({ type, config, cb }); return ch; },
      subscribe(statusCb) {
        fetch("/api/__relay/join", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ topic }) })
          .then(() => { joined = true; if (statusCb) statusCb("SUBSCRIBED"); poll(); });
        return ch;
      },
      send({ event, payload }) {
        return fetch("/api/__relay/publish", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ topic, kind: "bc", event, payload, sender: senderId }) }).then(() => {});
      },
      track(payload) {
        presence[senderId] = payload;
        return fetch("/api/__relay/publish", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ topic, kind: "presence", key: senderId, payload, sender: senderId }) }).then(() => {});
      },
      untrack() {
        return fetch("/api/__relay/publish", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ topic, kind: "presence-leave", key: senderId, sender: senderId }) }).then(() => {});
      },
      presenceState() { return presence; },
      unsubscribe() {
        joined = false;
        if (timer) clearInterval(timer);
        fetch("/api/__relay/leave", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ topic }) });
      },
    };
    function poll() {
      timer = setInterval(async () => {
        if (!joined) return;
        const res = await fetch(`/api/__relay/poll?topic=${encodeURIComponent(topic)}&since=${cursor}`);
        const data = await res.json();
        cursor = data.cursor;
        for (const msg of data.messages) {
          if (msg.sender === senderId) continue;   // mirrors real broadcast { self: false }
          if (msg.kind === "bc") {
            for (const h of handlers) if (h.type === "broadcast" && h.config.event === msg.event) h.cb({ payload: msg.payload });
          } else if (msg.kind === "presence") {
            presence[msg.key] = msg.payload;
            for (const h of handlers) if (h.type === "presence" && h.config.event === "sync") h.cb();
          } else if (msg.kind === "presence-leave") {
            delete presence[msg.key];
            for (const h of handlers) if (h.type === "presence" && h.config.event === "sync") h.cb();
          }
        }
      }, 100);
    }
    return ch;
  }
  window.supabase = {
    createClient() {
      return {
        realtime: { setAuth() {} },
        channel(topic) { return makeChannel(topic); },
        removeChannel(channel) { channel.unsubscribe(); },
      };
    },
  };
})();

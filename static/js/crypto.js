/**
 * static/js/crypto.js
 * --------------------
 * Public-key end-to-end encryption for Joschat direct chats. Loaded before
 * app.js; also loads under Node so it can be unit-tested (tests/js).
 *
 * HOW IT WORKS
 *   1. Every account has an ECDH P-256 key pair created IN THE BROWSER. The
 *      private key is generated non-extractable (JavaScript can use it but can
 *      never read its bytes) and is kept in this browser's IndexedDB. Only the
 *      PUBLIC key is ever sent to the server.
 *   2. For a direct chat, each person combines their own private key with the
 *      other person's public key (ECDH). Both arrive at the same secret without
 *      it ever crossing the network. HKDF-SHA-256 turns that secret into an
 *      AES-256-GCM key that is specific to the conversation and to the two keys.
 *   3. Messages are sealed with AES-GCM and marked "e2." so they can be told
 *      apart from older passphrase messages. The conversation id and sender id
 *      are authenticated as additional data: a ciphertext copied into another
 *      conversation, or re-attributed to another sender, fails to decrypt.
 *
 * WHAT THIS DOES NOT PROTECT AGAINST
 *   The server distributes public keys, so a malicious or compromised server
 *   could hand you the wrong key for a contact. Comparing the "safety number"
 *   with your contact over another channel is what closes that gap, and the app
 *   warns when a contact's key changes. There is no forward secrecy (the keys
 *   are long-lived), and keys are per browser: a new device needs new keys.
 */
(function (root) {
  "use strict";

  const subtle = root.crypto && root.crypto.subtle;
  const V2_PREFIX = "e2.";
  const CURVE = "P-256";
  const enc = new TextEncoder();
  const dec = new TextDecoder();

  // ---- encoding helpers ----------------------------------------------------

  function bytesToB64(bytes) {
    let binary = "";
    const CHUNK = 0x8000;   // avoid "too many arguments" on large inputs
    for (let i = 0; i < bytes.length; i += CHUNK) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    return btoa(binary);
  }

  function b64ToBytes(b64) {
    return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));   // atob throws on invalid input
  }

  function toHex(bytes) {
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  async function sha256Hex(bytes) {
    return toHex(new Uint8Array(await subtle.digest("SHA-256", bytes)));
  }

  // ---- keys ------------------------------------------------------------------

  /**
   * Creates a new key pair. `privateKey` is a non-extractable CryptoKey;
   * `publicKey` is the 65-byte uncompressed point, base64-encoded (this is what
   * the server stores); `fingerprint` is the SHA-256 of those bytes, in hex.
   */
  async function generateKeyPair() {
    // extractable=false applies to the private key. The public half of a pair is
    // always exportable, which is what lets us share it.
    const pair = await subtle.generateKey({ name: "ECDH", namedCurve: CURVE }, false, ["deriveBits"]);
    const raw = new Uint8Array(await subtle.exportKey("raw", pair.publicKey));
    return { privateKey: pair.privateKey, publicKey: bytesToB64(raw), fingerprint: await sha256Hex(raw) };
  }

  /** SHA-256 (hex) of a public key given as base64. Throws if it is not a P-256 point. */
  async function fingerprintOf(publicKeyB64) {
    const raw = b64ToBytes(publicKeyB64);
    if (raw.length !== 65 || raw[0] !== 4) throw new Error("Not a P-256 public key");
    return sha256Hex(raw);
  }

  /** Imports somebody's public key. Throws on anything that is not a valid point on the curve. */
  async function importPublicKey(publicKeyB64) {
    const raw = b64ToBytes(publicKeyB64);
    if (raw.length !== 65 || raw[0] !== 4) throw new Error("Not a P-256 public key");
    return subtle.importKey("raw", raw, { name: "ECDH", namedCurve: CURVE }, true, []);
  }

  /** 8 groups of 4 hex characters, for showing a person their own key fingerprint. */
  function formatFingerprint(hex) {
    return hex.slice(0, 32).match(/.{4}/g).join(" ");
  }

  /**
   * The number two people compare to be sure nobody swapped a key in between.
   * Both sides compute the same value regardless of who is "me": 8 groups of 5.
   */
  async function safetyNumber(fingerprintA, fingerprintB) {
    const [low, high] = [fingerprintA, fingerprintB].sort();
    const digest = await sha256Hex(enc.encode(`joschat/safety/v1|${low}|${high}`));
    return digest.slice(0, 40).match(/.{5}/g).join(" ");
  }

  // ---- per-conversation key and message envelope -----------------------------

  /**
   * AES-GCM key shared by exactly the two holders of these two key pairs, for
   * one conversation. Deterministic: both people derive the same key.
   */
  async function deriveConversationKey({ privateKey, peerPublicKey, ownFingerprint, peerFingerprint, conversationId }) {
    const peer = await importPublicKey(peerPublicKey);
    const secret = await subtle.deriveBits({ name: "ECDH", public: peer }, privateKey, 256);
    const hkdfKey = await subtle.importKey("raw", secret, "HKDF", false, ["deriveKey"]);
    const [low, high] = [ownFingerprint, peerFingerprint].sort();
    return subtle.deriveKey(
      {
        name: "HKDF",
        hash: "SHA-256",
        salt: new Uint8Array(32),
        info: enc.encode(`joschat/e2/v1|conversation:${conversationId}|keys:${low}:${high}`),
      },
      hkdfKey,
      { name: "AES-GCM", length: 256 },
      false,
      ["encrypt", "decrypt"]
    );
  }

  // What the ciphertext is bound to. Change either and decryption fails.
  function aad(conversationId, senderId) {
    return enc.encode(`joschat/e2/v1|conversation:${conversationId}|sender:${senderId}`);
  }

  function isV2(ciphertext) {
    return typeof ciphertext === "string" && ciphertext.startsWith(V2_PREFIX);
  }

  /** Returns "e2." + base64(iv || ciphertext). A fresh random IV per message. */
  async function encrypt(key, plaintext, conversationId, senderId) {
    const iv = root.crypto.getRandomValues(new Uint8Array(12));
    const sealed = new Uint8Array(await subtle.encrypt(
      { name: "AES-GCM", iv, additionalData: aad(conversationId, senderId) }, key, enc.encode(plaintext)
    ));
    const combined = new Uint8Array(iv.length + sealed.length);
    combined.set(iv, 0);
    combined.set(sealed, iv.length);
    return V2_PREFIX + bytesToB64(combined);
  }

  /** The text, or undefined when the key, conversation, sender or data is wrong. */
  async function decrypt(key, ciphertext, conversationId, senderId) {
    try {
      if (!isV2(ciphertext)) return undefined;
      const combined = b64ToBytes(ciphertext.slice(V2_PREFIX.length));
      const iv = combined.slice(0, 12);
      const plain = await subtle.decrypt(
        { name: "AES-GCM", iv, additionalData: aad(conversationId, senderId) }, key, combined.slice(12)
      );
      return dec.decode(plain);
    } catch {
      return undefined;
    }
  }

  // ---- key storage (browser only) ----------------------------------------------

  const DB_NAME = "joschat-keys";
  const STORE = "keypairs";

  function storeAvailable() {
    return typeof root.indexedDB !== "undefined" && root.indexedDB !== null;
  }

  function openDb() {
    return new Promise((resolve, reject) => {
      const req = root.indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = () => req.result.createObjectStore(STORE, { keyPath: "fingerprint" });
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function withStore(mode, action) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, mode);
      let request;
      try { request = action(tx.objectStore(STORE)); } catch (err) { db.close(); reject(err); return; }
      tx.oncomplete = () => { db.close(); resolve(request ? request.result : undefined); };
      tx.onerror = tx.onabort = () => { db.close(); reject(tx.error); };
    });
  }

  // Keyed by fingerprint, not by user id: the user id does not exist yet when the
  // key is created during registration, and the fingerprint is what the server holds.
  const KeyStore = {
    available: storeAvailable,
    save: ({ fingerprint, privateKey, publicKey }) =>
      withStore("readwrite", (s) => s.put({ fingerprint, privateKey, publicKey, createdAt: Date.now() })),
    load: (fingerprint) => withStore("readonly", (s) => s.get(fingerprint)),
    remove: (fingerprint) => withStore("readwrite", (s) => s.delete(fingerprint)),
  };

  const api = {
    supported: Boolean(subtle),
    V2_PREFIX,
    generateKeyPair,
    fingerprintOf,
    importPublicKey,
    formatFingerprint,
    safetyNumber,
    deriveConversationKey,
    isV2,
    encrypt,
    decrypt,
    KeyStore,
  };

  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.JoschatCrypto = api;
})(typeof globalThis !== "undefined" ? globalThis : this);

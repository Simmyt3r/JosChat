// Run with:  node --test tests/js/crypto.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const C = require("../../static/js/crypto.js");

async function pairOfPeople() {
  const alice = await C.generateKeyPair();
  const bob = await C.generateKeyPair();
  const keyFor = (me, them, conversationId = 7) => C.deriveConversationKey({
    privateKey: me.privateKey, peerPublicKey: them.publicKey,
    ownFingerprint: me.fingerprint, peerFingerprint: them.fingerprint, conversationId,
  });
  return { alice, bob, keyFor };
}

test("a key pair is a 65-byte P-256 public key with a non-extractable private key", async () => {
  const kp = await C.generateKeyPair();
  const raw = Buffer.from(kp.publicKey, "base64");
  assert.equal(raw.length, 65);
  assert.equal(raw[0], 4);                                  // uncompressed point
  assert.equal(kp.privateKey.extractable, false);
  await assert.rejects(globalThis.crypto.subtle.exportKey("pkcs8", kp.privateKey));   // JS cannot read it
  await assert.rejects(globalThis.crypto.subtle.exportKey("jwk", kp.privateKey));
  assert.equal(kp.fingerprint, await C.fingerprintOf(kp.publicKey));
});

test("both people derive the same key: what Alice seals, Bob opens, and back", async () => {
  const { alice, bob, keyFor } = await pairOfPeople();
  const sealed = await C.encrypt(await keyFor(alice, bob), "meet at the market at 5", 7, "alice-id");
  assert.equal(await C.decrypt(await keyFor(bob, alice), sealed, 7, "alice-id"), "meet at the market at 5");

  const reply = await C.encrypt(await keyFor(bob, alice), "ok 👍", 7, "bob-id");
  assert.equal(await C.decrypt(await keyFor(alice, bob), reply, 7, "bob-id"), "ok 👍");
});

test("the ciphertext is marked v2, hides the plaintext, and is different every time", async () => {
  const { alice, bob, keyFor } = await pairOfPeople();
  const key = await keyFor(alice, bob);
  const a = await C.encrypt(key, "secret words", 7, "alice-id");
  const b = await C.encrypt(key, "secret words", 7, "alice-id");
  assert.ok(C.isV2(a) && a.startsWith("e2."));
  assert.notEqual(a, b);                                    // fresh random IV
  assert.ok(!Buffer.from(a.slice(3), "base64").toString("latin1").includes("secret words"));
});

test("someone else's key pair cannot read the message", async () => {
  const { alice, bob, keyFor } = await pairOfPeople();
  const eve = await C.generateKeyPair();
  const sealed = await C.encrypt(await keyFor(alice, bob), "private", 7, "alice-id");
  assert.equal(await C.decrypt(await keyFor(eve, alice), sealed, 7, "alice-id"), undefined);
  assert.equal(await C.decrypt(await keyFor(eve, bob), sealed, 7, "alice-id"), undefined);
});

test("a message cannot be moved to another conversation or re-attributed to another sender", async () => {
  const { alice, bob, keyFor } = await pairOfPeople();
  const sealed = await C.encrypt(await keyFor(alice, bob, 7), "hello", 7, "alice-id");
  const bobKey = await keyFor(bob, alice, 7);
  assert.equal(await C.decrypt(bobKey, sealed, 7, "alice-id"), "hello");
  assert.equal(await C.decrypt(bobKey, sealed, 8, "alice-id"), undefined);        // other conversation
  assert.equal(await C.decrypt(bobKey, sealed, 7, "bob-id"), undefined);          // other sender
  assert.equal(await C.decrypt(await keyFor(bob, alice, 8), sealed, 8, "alice-id"), undefined);   // key is per conversation
});

test("any change to the ciphertext is detected", async () => {
  const { alice, bob, keyFor } = await pairOfPeople();
  const sealed = await C.encrypt(await keyFor(alice, bob), "hello", 7, "alice-id");
  const bytes = Buffer.from(sealed.slice(3), "base64");
  bytes[bytes.length - 1] ^= 1;
  assert.equal(await C.decrypt(await keyFor(bob, alice), "e2." + bytes.toString("base64"), 7, "alice-id"), undefined);
  assert.equal(await C.decrypt(await keyFor(bob, alice), "e2.not-base64!!", 7, "alice-id"), undefined);
});

test("old passphrase messages are not mistaken for v2 and are not decryptable as v2", async () => {
  const legacy = Buffer.from("iv-and-ciphertext-bytes").toString("base64");
  assert.equal(C.isV2(legacy), false);
  assert.equal(C.isV2(undefined), false);
  const { alice, bob, keyFor } = await pairOfPeople();
  assert.equal(await C.decrypt(await keyFor(alice, bob), legacy, 7, "alice-id"), undefined);
});

test("safety numbers match on both sides and change when a key changes", async () => {
  const { alice, bob } = await pairOfPeople();
  const n1 = await C.safetyNumber(alice.fingerprint, bob.fingerprint);
  assert.equal(n1, await C.safetyNumber(bob.fingerprint, alice.fingerprint));
  assert.match(n1, /^([0-9a-f]{5} ){7}[0-9a-f]{5}$/);
  const bob2 = await C.generateKeyPair();
  assert.notEqual(n1, await C.safetyNumber(alice.fingerprint, bob2.fingerprint));
  assert.match(C.formatFingerprint(alice.fingerprint), /^([0-9a-f]{4} ){7}[0-9a-f]{4}$/);
});

test("invalid public keys are rejected instead of being trusted", async () => {
  const kp = await C.generateKeyPair();
  const raw = Buffer.from(kp.publicKey, "base64");
  const offCurve = Buffer.from(raw); offCurve[64] ^= 1;
  const compressed = Buffer.concat([Buffer.from([2]), raw.subarray(1, 33)]);
  for (const bad of [offCurve.toString("base64"), compressed.toString("base64"), "AAAA", "%%%not base64%%%", ""]) {
    await assert.rejects(C.importPublicKey(bad), undefined, `accepted ${bad.slice(0, 12)}`);
  }
  await assert.rejects(C.fingerprintOf("AAAA"));
});

test("the key store reports itself unavailable where there is no IndexedDB (Node)", () => {
  assert.equal(C.KeyStore.available(), false);
  assert.equal(C.supported, true);
});

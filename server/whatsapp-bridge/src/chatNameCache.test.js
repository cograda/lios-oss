/**
 * Tests for the chat-name cache. Run with the Node.js built-in test runner
 * (no test framework was present in this package, and Node 22+ ships one —
 * see package.json's `test` script):
 *
 *   npm test
 */

import test from "node:test";
import assert from "node:assert/strict";
import {
  setChatName,
  getChatName,
  clearChatNameCache,
  chatNameCacheSize,
  isGroupJid,
} from "./chatNameCache.js";

test.beforeEach(() => {
  clearChatNameCache();
});

test("returns null for an unknown jid", () => {
  assert.equal(getChatName(1, "unknown@s.whatsapp.net"), null);
});

test("caches and returns a name for (userId, jid)", () => {
  setChatName(1, "abc@s.whatsapp.net", "Alice");
  assert.equal(getChatName(1, "abc@s.whatsapp.net"), "Alice");
});

test("a null/empty name is a no-op and does not clobber a cached name", () => {
  setChatName(1, "abc@s.whatsapp.net", "Alice");
  setChatName(1, "abc@s.whatsapp.net", null);
  setChatName(1, "abc@s.whatsapp.net", undefined);
  setChatName(1, "abc@s.whatsapp.net", "");
  assert.equal(getChatName(1, "abc@s.whatsapp.net"), "Alice");
});

test("a later real name overwrites an earlier one for the same key", () => {
  setChatName(1, "abc@s.whatsapp.net", "Alice");
  setChatName(1, "abc@s.whatsapp.net", "Alice Smith");
  assert.equal(getChatName(1, "abc@s.whatsapp.net"), "Alice Smith");
});

// This is the test that matters most: the same @lid must never leak a name
// from one user's account to another's. A WhatsApp @lid is account-scoped,
// not globally unique, so two different users can have the same jid resolve
// to two different real conversations.
test("scoping: the same jid under two different user_ids caches independently", () => {
  const jid = "123456789@lid";

  setChatName(1, jid, "Alex's Mother");
  setChatName(2, jid, "Sam's Book Club");

  assert.equal(getChatName(1, jid), "Alex's Mother");
  assert.equal(getChatName(2, jid), "Sam's Book Club");

  // Prove it's not just "last write wins" by re-asserting after both writes,
  // in reverse order.
  assert.equal(getChatName(2, jid), "Sam's Book Club");
  assert.equal(getChatName(1, jid), "Alex's Mother");
});

test("a lookup for the wrong user_id does not fall back to another user's entry", () => {
  setChatName(1, "shared-lid@lid", "User 1's name for this jid");
  assert.equal(getChatName(999, "shared-lid@lid"), null);
});

test("clearChatNameCache empties the cache", () => {
  setChatName(1, "abc@s.whatsapp.net", "Alice");
  assert.equal(chatNameCacheSize(), 1);
  clearChatNameCache();
  assert.equal(chatNameCacheSize(), 0);
  assert.equal(getChatName(1, "abc@s.whatsapp.net"), null);
});

test("getChatName with a falsy jid returns null rather than throwing", () => {
  assert.equal(getChatName(1, null), null);
  assert.equal(getChatName(1, undefined), null);
  assert.equal(getChatName(1, ""), null);
});

test("a group jid is never cached — contact 'names' for groups are participants, not subjects", () => {
  // Real case: 120363145771673138@g.us is the school parents group, and
  // whatsapp_contacts records its name as "Sam" — a participant. Caching
  // that would stamp a confident wrong name onto every message in the group.
  setChatName(1, "120363145771673138@g.us", "Sam");
  assert.equal(getChatName(1, "120363145771673138@g.us"), null);

  // The legacy <creator>-<timestamp>@g.us form must be rejected too.
  setChatName(1, "353879854393-1348642405@g.us", "Sean");
  assert.equal(getChatName(1, "353879854393-1348642405@g.us"), null);

  // A group being rejected must not stop 1:1 chats from caching normally.
  setChatName(1, "110462414917696@lid", "Peter");
  assert.equal(getChatName(1, "110462414917696@lid"), "Peter");
});

test("isGroupJid identifies both group id formats and nothing else", () => {
  assert.equal(isGroupJid("120363145771673138@g.us"), true);
  assert.equal(isGroupJid("353879854393-1348642405@g.us"), true);
  assert.equal(isGroupJid("110462414917696@lid"), false);
  assert.equal(isGroupJid("abc@s.whatsapp.net"), false);
  assert.equal(isGroupJid(null), false);
  assert.equal(isGroupJid(undefined), false);
});

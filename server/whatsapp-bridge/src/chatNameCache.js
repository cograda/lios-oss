/**
 * In-memory cache of chat/contact display names, keyed by (user_id, jid).
 *
 * Baileys delivers real names via `contacts.update` and the history-sync
 * contacts batch (`messaging-history.set`), but neither of those events
 * carries the message itself — so the name has to be cached somewhere and
 * looked up when a message for that jid comes through.
 *
 * MUST be keyed by (user_id, jid), never jid alone: a WhatsApp `@lid` is
 * account-scoped, not globally unique. The same LID can name a different
 * conversation under a different account. comar is multi-user (Alex and
 * Sam each run their own bridge process/session), so a cache keyed on
 * bare jid would risk attributing one user's chat name to another user's
 * messages wherever LIDs collide. Each bridge process only ever writes and
 * reads its own USER_ID, but the cache enforces the scoping itself rather
 * than relying on that as an accident of deployment.
 */

const cache = new Map();

function makeKey(userId, jid) {
  return `${userId}:${jid}`;
}

/** A group jid. Baileys uses the `@g.us` suffix for both group id formats. */
export function isGroupJid(jid) {
  return typeof jid === "string" && jid.endsWith("@g.us");
}

/**
 * Record a display name for (userId, jid). A null/empty/undefined name is a
 * no-op — we never want a "we don't know yet" event to clobber a real name
 * that was already cached.
 *
 * GROUPS ARE REJECTED, deliberately. The `contacts.update` and history-sync
 * contacts events do deliver entries for `@g.us` jids, but the name on them
 * is a *participant's* name, not the group subject — the school parents
 * group `120363145771673138@g.us` arrives as "Sam", who merely posts in
 * it. Caching that would stamp a confident wrong name onto every message in
 * the group, which is worse than the null it replaces: null is visibly
 * missing, "Sam" is not, and it is the group threads that most need
 * naming. Group subjects come from Baileys group metadata (`groups.update`
 * / `groupMetadata`), which this bridge does not consume yet.
 *
 * Enforced here rather than at the two call sites, for the same reason
 * `makeKey` owns the user scoping: a rule both callers must remember is a
 * rule one of them eventually won't.
 */
export function setChatName(userId, jid, name) {
  if (!jid || !name) return;
  if (isGroupJid(jid)) return;
  cache.set(makeKey(userId, jid), name);
}

/** Look up the cached display name for (userId, jid), or null if unknown. */
export function getChatName(userId, jid) {
  if (!jid) return null;
  return cache.get(makeKey(userId, jid)) ?? null;
}

/** Test-only: reset all cached state. */
export function clearChatNameCache() {
  cache.clear();
}

/** Test-only: number of cached entries. */
export function chatNameCacheSize() {
  return cache.size;
}

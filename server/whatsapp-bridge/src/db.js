/**
 * Postgres connection and message/contact upsert logic.
 *
 * Schema is owned by the Python server (SQLAlchemy + Alembic).
 *
 * `user_id` is written EXPLICITLY (from the USER_ID env var), not left to the
 * column default. One Baileys session is one phone number is one user, so a
 * second bridge container must not inherit the DEFAULT 1 and silently file
 * its messages under the first user. Both `whatsapp_messages` and
 * `whatsapp_contacts` are keyed (user_id, ...) — the ON CONFLICT targets
 * must match those composite constraints.
 */

import pg from "pg";

const { Pool } = pg;

let pool;

/**
 * Which comar user this bridge belongs to. Defaults to 1 so an existing
 * single-bridge deployment keeps working unchanged, but a bad value is fatal
 * rather than silently coerced: filing one person's messages under another is
 * not something to discover later from the data.
 */
export const USER_ID = (() => {
  const raw = process.env.USER_ID;
  if (raw === undefined || raw === "") return 1;
  const parsed = Number(raw);
  if (!Number.isInteger(parsed) || parsed < 1) {
    throw new Error(`USER_ID must be a positive integer, got ${JSON.stringify(raw)}`);
  }
  return parsed;
})();

export function initDb() {
  const connectionString = process.env.DATABASE_URL;
  if (!connectionString) {
    throw new Error("DATABASE_URL is required");
  }

  pool = new Pool({
    connectionString,
    max: 5,
    idleTimeoutMillis: 30000,
  });

  pool.on("error", (err) => {
    console.error("Postgres pool error:", err.message);
  });

  return pool;
}

export async function ensureTables() {
  // Schema is managed by Alembic on the Python side. Verify the tables and
  // the (user_id, message_id) unique constraint exist so the bridge fails
  // loudly at startup instead of silently dropping every insert.
  const client = await pool.connect();
  try {
    for (const [table, constraint] of [
      ["whatsapp_messages", "uq_wa_user_msg"],
      ["whatsapp_contacts", "uq_wa_user_contact"],
    ]) {
      const { rows } = await client.query(
        `SELECT conname FROM pg_constraint
          WHERE conrelid = $1::regclass AND contype = 'u' AND conname = $2`,
        [table, constraint],
      );
      if (rows.length === 0) {
        throw new Error(
          `${table} is missing the ${constraint} unique constraint. ` +
          "Run Alembic migrations on the Python server before starting the bridge.",
        );
      }
    }
    console.log(`Schema check passed (user_id=${USER_ID})`);
  } finally {
    client.release();
  }
}

export async function upsertMessage(msg) {
  const client = await pool.connect();
  try {
    await client.query(
      `INSERT INTO whatsapp_messages
        (user_id, message_id, chat_id, chat_name, sender_id, sender_name,
         is_group, timestamp, message_type, body, media_caption,
         is_from_me, reply_to_id, raw_json)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
       ON CONFLICT (user_id, message_id) DO NOTHING`,
      [
        USER_ID,
        msg.messageId,
        msg.chatId,
        msg.chatName,
        msg.senderId,
        msg.senderName,
        msg.isGroup,
        msg.timestamp,
        msg.messageType,
        msg.body,
        msg.mediaCaption,
        msg.isFromMe,
        msg.replyToId,
        msg.rawJson,
      ]
    );
  } finally {
    client.release();
  }
}

export async function upsertContact(contact) {
  const client = await pool.connect();
  try {
    await client.query(
      `INSERT INTO whatsapp_contacts (user_id, jid, name, notify_name, is_group, last_message_at, updated_at)
       VALUES ($1, $2, $3, $4, $5, $6, NOW())
       ON CONFLICT (user_id, jid) DO UPDATE SET
         name = COALESCE(EXCLUDED.name, whatsapp_contacts.name),
         notify_name = COALESCE(EXCLUDED.notify_name, whatsapp_contacts.notify_name),
         last_message_at = GREATEST(EXCLUDED.last_message_at, whatsapp_contacts.last_message_at),
         updated_at = NOW()`,
      [
        USER_ID,
        contact.jid,
        contact.name,
        contact.notifyName,
        contact.isGroup,
        contact.lastMessageAt,
      ]
    );
  } finally {
    client.release();
  }
}

export async function upsertMessageBatch(messages) {
  if (!messages.length) return 0;
  const client = await pool.connect();
  try {
    let inserted = 0;
    // Process in chunks of 100 for Postgres parameter limits
    for (let i = 0; i < messages.length; i += 100) {
      const batch = messages.slice(i, i + 100);
      const values = [];
      const params = [];
      let paramIdx = 1;

      for (const msg of batch) {
        values.push(
          `($${paramIdx++}, $${paramIdx++}, $${paramIdx++}, $${paramIdx++}, $${paramIdx++}, $${paramIdx++}, $${paramIdx++}, $${paramIdx++}, $${paramIdx++}, $${paramIdx++}, $${paramIdx++}, $${paramIdx++}, $${paramIdx++}, $${paramIdx++})`
        );
        params.push(
          USER_ID,
          msg.messageId, msg.chatId, msg.chatName, msg.senderId,
          msg.senderName, msg.isGroup, msg.timestamp, msg.messageType,
          msg.body, msg.mediaCaption, msg.isFromMe, msg.replyToId,
          msg.rawJson
        );
      }

      const result = await client.query(
        `INSERT INTO whatsapp_messages
          (user_id, message_id, chat_id, chat_name, sender_id, sender_name,
           is_group, timestamp, message_type, body, media_caption,
           is_from_me, reply_to_id, raw_json)
         VALUES ${values.join(", ")}
         ON CONFLICT (user_id, message_id) DO NOTHING`,
        params
      );
      inserted += result.rowCount;
    }
    return inserted;
  } finally {
    client.release();
  }
}

export async function upsertContactBatch(contacts) {
  if (!contacts.length) return;
  const client = await pool.connect();
  try {
    for (const contact of contacts) {
      await client.query(
        `INSERT INTO whatsapp_contacts (user_id, jid, name, notify_name, is_group, last_message_at, updated_at)
         VALUES ($1, $2, $3, $4, $5, $6, NOW())
         ON CONFLICT (user_id, jid) DO UPDATE SET
           name = COALESCE(EXCLUDED.name, whatsapp_contacts.name),
           notify_name = COALESCE(EXCLUDED.notify_name, whatsapp_contacts.notify_name),
           last_message_at = GREATEST(EXCLUDED.last_message_at, whatsapp_contacts.last_message_at),
           updated_at = NOW()`,
        [USER_ID, contact.jid, contact.name, contact.notifyName, contact.isGroup, contact.lastMessageAt]
      );
    }
  } finally {
    client.release();
  }
}

export async function getMessageCount() {
  const client = await pool.connect();
  try {
    const result = await client.query(
      "SELECT COUNT(*) as count FROM whatsapp_messages"
    );
    return parseInt(result.rows[0].count, 10);
  } finally {
    client.release();
  }
}

export function getPool() {
  return pool;
}

export async function getRawMessage(messageId) {
  const client = await pool.connect();
  try {
    const result = await client.query(
      "SELECT message_id, raw_json FROM whatsapp_messages WHERE message_id = $1 LIMIT 1",
      [messageId],
    );
    return result.rows[0] || null;
  } finally {
    client.release();
  }
}

/**
 * WhatsApp Bridge — Baileys multi-device connection → Postgres.
 *
 * Single-purpose service: maintain a WhatsApp Web connection and write
 * incoming messages to the whatsapp_messages table. The Python server
 * handles embedding, search, and MCP tools.
 *
 * First run: displays QR code in logs. Scan with WhatsApp to pair.
 * Subsequent runs: reconnects using persisted auth state.
 *
 * Health endpoint: GET http://localhost:3100/health
 */

import {
  makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  downloadMediaMessage,
} from "@whiskeysockets/baileys";
import { createServer } from "http";
import pino from "pino";
import qrcode from "qrcode-terminal";

import { initDb, ensureTables, upsertMessage, upsertContact, upsertMessageBatch, upsertContactBatch, getMessageCount, getRawMessage } from "./db.js";

const logger = pino({ level: process.env.LOG_LEVEL || "info" });

const AUTH_DIR = process.env.AUTH_DIR || "/app/auth_state";
const HEALTH_PORT = parseInt(process.env.HEALTH_PORT || "3100", 10);
const SYNC_FULL_HISTORY = process.env.SYNC_FULL_HISTORY === "true";

// Connection state
let sock = null;
let connectionState = "disconnected";
let messageCount = 0;
let historySyncProgress = null; // null = not syncing, 0-100 = in progress

// ---------------------------------------------------------------------------
// Message extraction
// ---------------------------------------------------------------------------

function extractMessage(msg) {
  const key = msg.key;
  const chatId = key.remoteJid;
  const isGroup = chatId?.endsWith("@g.us") || false;
  const isFromMe = key.fromMe || false;

  // Determine sender
  let senderId = isFromMe ? "me" : (key.participant || chatId);
  let senderName = msg.pushName || null;

  // Message content — check various message types
  const m = msg.message;
  if (!m) return null; // Protocol message, no content

  let messageType = "text";
  let body = null;
  let mediaCaption = null;

  if (m.conversation) {
    body = m.conversation;
  } else if (m.extendedTextMessage?.text) {
    body = m.extendedTextMessage.text;
  } else if (m.imageMessage) {
    messageType = "image";
    mediaCaption = m.imageMessage.caption || null;
  } else if (m.videoMessage) {
    messageType = "video";
    mediaCaption = m.videoMessage.caption || null;
  } else if (m.audioMessage) {
    messageType = "audio";
  } else if (m.documentMessage) {
    messageType = "document";
    mediaCaption = m.documentMessage.fileName || null;
  } else if (m.stickerMessage) {
    messageType = "sticker";
  } else if (m.reactionMessage) {
    messageType = "reaction";
    body = m.reactionMessage.text || null;
  } else if (m.protocolMessage || m.senderKeyDistributionMessage) {
    return null; // Internal protocol, skip
  } else {
    // Unknown message type — store what we can
    messageType = "other";
    body = null;
  }

  // Reply context
  const replyToId =
    m.extendedTextMessage?.contextInfo?.stanzaId ||
    m.imageMessage?.contextInfo?.stanzaId ||
    m.videoMessage?.contextInfo?.stanzaId ||
    null;

  // Timestamp
  const timestamp = msg.messageTimestamp
    ? new Date(
        typeof msg.messageTimestamp === "number"
          ? msg.messageTimestamp * 1000
          : Number(msg.messageTimestamp) * 1000
      )
    : new Date();

  return {
    messageId: key.id,
    chatId,
    chatName: null, // Filled from contact info
    senderId,
    senderName,
    isGroup,
    timestamp,
    messageType,
    body,
    mediaCaption,
    isFromMe,
    replyToId,
    // Cap at 256KB — media messages need full key material for later download;
    // the old 10KB cap truncated newer image payloads into invalid JSON,
    // making their media permanently unrecoverable.
    rawJson: JSON.stringify(msg, null, 0).slice(0, 262144),
  };
}

// ---------------------------------------------------------------------------
// Baileys connection
// ---------------------------------------------------------------------------

async function connectWhatsApp() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();

  sock = makeWASocket({
    version,
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, logger),
    },
    logger: logger.child({ module: "baileys" }),
    printQRInTerminal: false,
    generateHighQualityLinkPreview: false,
    syncFullHistory: SYNC_FULL_HISTORY,
    // READ-ONLY: do not leak any activity back to WhatsApp
    markOnlineOnConnect: false,   // Don't show "online" status
    shouldIgnoreJid: () => false, // Process all JIDs (but never respond)
  });

  // READ-ONLY: suppress read receipts and presence updates
  // Baileys sends read receipts by default — explicitly disable
  sock.sendReadReceipt = async () => {};
  sock.sendPresenceUpdate = async () => {};
  sock.presenceSubscribe = async () => {};
  sock.sendMessage = async () => {
    logger.warn("sendMessage blocked — bridge is read-only");
  };

  // Save credentials on update
  sock.ev.on("creds.update", saveCreds);

  // Connection state
  sock.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      logger.info("QR code generated — scan with WhatsApp to pair:");
      qrcode.generate(qr, { small: true });
    }

    if (connection === "close") {
      connectionState = "disconnected";
      const statusCode =
        lastDisconnect?.error?.output?.statusCode;
      const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

      logger.warn(
        { statusCode, shouldReconnect },
        "Connection closed"
      );

      if (shouldReconnect) {
        // Reconnect after a brief delay
        setTimeout(connectWhatsApp, 5000);
      } else {
        logger.error(
          "Logged out — delete auth_state volume and restart to re-pair"
        );
        connectionState = "logged_out";
      }
    } else if (connection === "open") {
      connectionState = "connected";
      messageCount = await getMessageCount();
      logger.info({ messageCount }, "Connected to WhatsApp");
    }
  });

  // Handle incoming messages
  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    for (const msg of messages) {
      try {
        const extracted = extractMessage(msg);
        if (!extracted) continue; // Protocol message, skip

        await upsertMessage(extracted);
        messageCount++;

        // Update contact
        await upsertContact({
          jid: extracted.chatId,
          name: extracted.chatName,
          notifyName: extracted.isFromMe ? null : extracted.senderName,
          isGroup: extracted.isGroup,
          lastMessageAt: extracted.timestamp,
        });

        if (extracted.body) {
          logger.info(
            {
              from: extracted.senderName || extracted.senderId,
              chat: extracted.chatId,
              type: extracted.messageType,
              preview: extracted.body?.slice(0, 80),
            },
            "Message saved"
          );
        } else {
          logger.debug(
            {
              chat: extracted.chatId,
              type: extracted.messageType,
            },
            "Non-text message saved"
          );
        }
      } catch (err) {
        logger.error({ err, messageId: msg.key?.id }, "Failed to process message");
      }
    }
  });

  // Handle contact updates
  sock.ev.on("contacts.update", async (updates) => {
    for (const contact of updates) {
      if (!contact.id) continue;
      try {
        await upsertContact({
          jid: contact.id,
          name: contact.name || contact.verifiedName || null,
          notifyName: contact.notify || null,
          isGroup: contact.id.endsWith("@g.us"),
          lastMessageAt: null,
        });
      } catch (err) {
        logger.error({ err, jid: contact.id }, "Failed to update contact");
      }
    }
  });

  // Handle history sync (backfill)
  sock.ev.on("messaging-history.set", async ({ chats, contacts, messages, progress, isLatest }) => {
    historySyncProgress = progress ?? 0;

    logger.info(
      { messageCount: messages?.length || 0, contactCount: contacts?.length || 0, chatCount: chats?.length || 0, progress, isLatest },
      "History sync batch received"
    );

    // Process messages
    if (messages?.length) {
      const extracted = [];
      for (const msg of messages) {
        try {
          const data = extractMessage(msg);
          if (data) extracted.push(data);
        } catch (err) {
          logger.debug({ err, messageId: msg.key?.id }, "Failed to extract history message");
        }
      }

      if (extracted.length) {
        try {
          const inserted = await upsertMessageBatch(extracted);
          messageCount += inserted;
          logger.info(
            { extracted: extracted.length, inserted, totalMessages: messageCount },
            "History messages saved"
          );
        } catch (err) {
          logger.error({ err }, "Failed to save history message batch");
        }
      }
    }

    // Process contacts from history
    if (contacts?.length) {
      const contactData = contacts
        .filter(c => c.id)
        .map(c => ({
          jid: c.id,
          name: c.name || c.verifiedName || null,
          notifyName: c.notify || null,
          isGroup: c.id.endsWith("@g.us"),
          lastMessageAt: null,
        }));

      try {
        await upsertContactBatch(contactData);
        logger.info({ count: contactData.length }, "History contacts saved");
      } catch (err) {
        logger.error({ err }, "Failed to save history contacts");
      }
    }

    if (isLatest) {
      historySyncProgress = 100;
      const finalCount = await getMessageCount();
      logger.info({ totalMessages: finalCount }, "History sync complete");
    }
  });
}

// ---------------------------------------------------------------------------
// Health endpoint
// ---------------------------------------------------------------------------

// Reconstruct the Baileys msg object from our stored raw_json and stream the
// decrypted media bytes. read-only stays intact — downloadMediaMessage
// only fetches + decrypts, it doesn't send anything back to WhatsApp.
async function handleDownload(messageId, res) {
  if (connectionState !== "connected") {
    res.writeHead(503, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "bridge not connected" }));
    return;
  }
  const row = await getRawMessage(messageId);
  if (!row || !row.raw_json) {
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "message not found" }));
    return;
  }
  let msg;
  try {
    msg = JSON.parse(row.raw_json);
  } catch (e) {
    res.writeHead(422, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "raw_json malformed", detail: e.message }));
    return;
  }
  const inner = msg?.message || {};
  const media =
    inner.documentMessage ||
    inner.imageMessage ||
    inner.videoMessage ||
    inner.audioMessage ||
    inner.stickerMessage;
  if (!media) {
    res.writeHead(422, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "message has no downloadable media" }));
    return;
  }
  // WhatsApp CDN URLs expire (weeks-to-months). On 403/404 we ask WA to
  // re-upload so Baileys gets a fresh signed URL, then retry once.
  async function tryDownload(m) {
    return downloadMediaMessage(
      m,
      "buffer",
      {},
      { logger, reuploadRequest: sock.updateMediaMessage },
    );
  }

  let buffer;
  try {
    buffer = await tryDownload(msg);
  } catch (err) {
    const status = err?.response?.status || err?.output?.statusCode;
    const expired = status === 403 || status === 410 || status === 404;
    if (!expired || typeof sock.updateMediaMessage !== "function") {
      logger.error({ err: { message: err.message, status }, messageId }, "download failed");
      res.writeHead(502, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "download failed", detail: err.message, status }));
      return;
    }
    logger.info({ messageId, status }, "CDN URL expired — requesting re-upload");
    try {
      const refreshed = await sock.updateMediaMessage(msg);
      buffer = await tryDownload(refreshed || msg);
    } catch (retryErr) {
      logger.error(
        { err: { message: retryErr.message }, messageId },
        "re-upload + retry failed",
      );
      res.writeHead(502, { "Content-Type": "application/json" });
      res.end(JSON.stringify({
        error: "download failed after re-upload",
        detail: retryErr.message,
      }));
      return;
    }
  }

  res.writeHead(200, {
    "Content-Type": media.mimetype || "application/octet-stream",
    "Content-Length": buffer.length,
    "Content-Disposition": `attachment; filename="${(media.fileName || messageId).replace(/"/g, "")}"`,
  });
  res.end(buffer);
}

function startHealthServer() {
  const server = createServer(async (req, res) => {
    if (req.url === "/health" && req.method === "GET") {
      const status = connectionState === "connected" ? 200 : 503;
      res.writeHead(status, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          status: connectionState,
          messages: messageCount,
          uptime: Math.floor(process.uptime()),
          historySync: SYNC_FULL_HISTORY
            ? { enabled: true, progress: historySyncProgress }
            : { enabled: false },
        })
      );
      return;
    }
    const dlMatch = req.method === "GET" && req.url?.match(/^\/download\/(.+)$/);
    if (dlMatch) {
      await handleDownload(decodeURIComponent(dlMatch[1]), res);
      return;
    }
    {
      res.writeHead(404);
      res.end();
    }
  });

  server.listen(HEALTH_PORT, () => {
    logger.info({ port: HEALTH_PORT }, "Health endpoint listening");
  });
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  logger.info("WhatsApp Bridge starting");

  initDb();
  await ensureTables();
  startHealthServer();
  await connectWhatsApp();
}

main().catch((err) => {
  logger.fatal({ err }, "Bridge startup failed");
  process.exit(1);
});

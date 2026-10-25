import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import {
  closeSync,
  existsSync,
  fsyncSync,
  linkSync,
  mkdirSync,
  openSync,
  readFileSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, isAbsolute, join } from "node:path";

export interface TelegramCredentials {
  apiId: number;
  apiHash: string;
}

/** Values entered on the one-time setup form must be actual Telegram app credentials. */
export function validateTelegramCredentials(apiId: unknown, apiHash: unknown): TelegramCredentials {
  const id = typeof apiId === "number" ? String(apiId) : apiId;
  if (typeof id !== "string" || !/^[1-9]\d{0,9}$/.test(id.trim())) {
    throw new Error("API ID должен быть положительным целым числом.");
  }
  const numeric = Number(id.trim());
  if (!Number.isSafeInteger(numeric) || numeric > 2147483647) {
    throw new Error("API ID выходит за допустимый диапазон.");
  }
  if (typeof apiHash !== "string" || !/^[a-fA-F0-9]{32}$/.test(apiHash.trim())) {
    throw new Error("API Hash должен содержать 32 шестнадцатеричных символа.");
  }
  return { apiId: numeric, apiHash: apiHash.trim().toLowerCase() };
}

export function telegramCredentialsPath(): string {
  const path = process.env.TG_CREDENTIALS_FILE?.trim() || join(homedir(), ".local", "state", "tg", "telegram-api.json");
  if (!isAbsolute(path)) throw new Error("TG_CREDENTIALS_FILE must be an absolute path outside the source tree.");
  return path;
}

/** Environment remains supported for existing self-hosters and test fixtures. */
export function readTelegramCredentials(): TelegramCredentials | null {
  const envId = Number(process.env.TELEGRAM_API_ID);
  const envHash = process.env.TELEGRAM_API_HASH?.trim();
  if (Number.isSafeInteger(envId) && envId > 0 && envHash) return { apiId: envId, apiHash: envHash };
  try {
    const saved = JSON.parse(readFileSync(telegramCredentialsPath(), "utf8"));
    return validateTelegramCredentials(saved.apiId, saved.apiHash);
  } catch {
    // A missing/unreadable/corrupt file is never replaced with fake credentials.
    return null;
  }
}

export function telegramCredentialsConfigured(): boolean {
  return readTelegramCredentials() !== null;
}

/** Constant-time comparison after hashing avoids leaking token lengths or prefixes. */
export function validSetupToken(value: unknown): boolean {
  const expected = process.env.TG_SETUP_TOKEN ?? "";
  if (expected.length < 32 || typeof value !== "string" || value.length > 512) return false;
  return timingSafeEqual(createHash("sha256").update(expected).digest(), createHash("sha256").update(value).digest());
}

/** Publish a completed 0600 file atomically and never overwrite existing credentials. */
export function saveTelegramCredentials(credentials: TelegramCredentials): void {
  const safe = validateTelegramCredentials(credentials.apiId, credentials.apiHash);
  if (telegramCredentialsConfigured()) throw new Error("Доступ Telegram уже настроен. Повторная настройка отключена.");
  const path = telegramCredentialsPath();
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  if (existsSync(path)) throw new Error("Файл настройки уже существует. Обратитесь к владельцу сервера.");
  const temporary = `${path}.${randomBytes(12).toString("hex")}.tmp`;
  let fd: number | undefined;
  try {
    fd = openSync(temporary, "wx", 0o600);
    writeFileSync(fd, JSON.stringify(safe), { encoding: "utf8" });
    fsyncSync(fd);
    closeSync(fd);
    fd = undefined;
    // A hard link is an atomic create-if-absent; competing setup requests cannot overwrite.
    linkSync(temporary, path);
  } finally {
    if (fd !== undefined) closeSync(fd);
    if (existsSync(temporary)) unlinkSync(temporary);
  }
}

export const TELEGRAM_SETUP_MESSAGE =
  "Telegram ещё не настроен. Владелец сервера должен открыть /setup и указать API ID и API Hash, затем повторить подключение.";

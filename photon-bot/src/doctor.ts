import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { IMessageSDK } from "@photon-ai/imessage-kit";
import Database from "better-sqlite3";

type CheckStatus = "ok" | "warn" | "error";

type CheckResult = {
  name: string;
  status: CheckStatus;
  detail: string;
};

const MESSAGES_APP_PATH = "/System/Applications/Messages.app";
const CHAT_DB_PATH = path.join(os.homedir(), "Library", "Messages", "chat.db");

async function main(): Promise<void> {
  const checks: CheckResult[] = [];

  checks.push(checkMessagesAppExists());
  checks.push(checkChatDbExists());
  checks.push(checkChatDbReadableByFs());
  checks.push(checkChatDbReadableBySqlite());
  checks.push(await checkSdkListChats());

  printReport(checks);

  const hasError = checks.some((check) => check.status === "error");
  process.exit(hasError ? 1 : 0);
}

function checkMessagesAppExists(): CheckResult {
  return fs.existsSync(MESSAGES_APP_PATH)
    ? {
        name: "Messages.app",
        status: "ok",
        detail: `Found at ${MESSAGES_APP_PATH}`,
      }
    : {
        name: "Messages.app",
        status: "error",
        detail: `Missing at ${MESSAGES_APP_PATH}`,
      };
}

function checkChatDbExists(): CheckResult {
  return fs.existsSync(CHAT_DB_PATH)
    ? {
        name: "chat.db exists",
        status: "ok",
        detail: `Found at ${CHAT_DB_PATH}`,
      }
    : {
        name: "chat.db exists",
        status: "error",
        detail: `Missing at ${CHAT_DB_PATH}`,
      };
}

function checkChatDbReadableByFs(): CheckResult {
  try {
    fs.accessSync(CHAT_DB_PATH, fs.constants.R_OK);
    return {
      name: "chat.db fs access",
      status: "ok",
      detail: "Node can access the Messages database path.",
    };
  } catch (error) {
    return {
      name: "chat.db fs access",
      status: "error",
      detail: classifyPermissionError(error, "Node cannot read chat.db."),
    };
  }
}

function checkChatDbReadableBySqlite(): CheckResult {
  let db: Database | null = null;

  try {
    db = new Database(CHAT_DB_PATH, { readonly: true, fileMustExist: true });
    const row = db.prepare("select count(*) as count from chat").get() as
      | { count?: number }
      | undefined;
    const count = typeof row?.count === "number" ? row.count : "unknown";
    return {
      name: "chat.db sqlite access",
      status: "ok",
      detail: `SQLite opened successfully. chat rows: ${count}`,
    };
  } catch (error) {
    return {
      name: "chat.db sqlite access",
      status: "error",
      detail: classifyPermissionError(error, "SQLite could not open chat.db."),
    };
  } finally {
    db?.close();
  }
}

async function checkSdkListChats(): Promise<CheckResult> {
  const sdk = new IMessageSDK({
    databasePath: CHAT_DB_PATH,
  });

  try {
    const chats = await sdk.listChats({ limit: 1, type: "all" });
    return {
      name: "IMessageSDK listChats",
      status: "ok",
      detail: `SDK connected successfully. Sample query returned ${chats.length} chat(s).`,
    };
  } catch (error) {
    return {
      name: "IMessageSDK listChats",
      status: "error",
      detail: classifyPermissionError(error, "SDK could not access Messages."),
    };
  } finally {
    try {
      await sdk.close();
    } catch {
      // Ignore cleanup errors after a failed init.
    }
  }
}

function classifyPermissionError(error: unknown, fallback: string): string {
  const message = error instanceof Error ? error.message : String(error);

  if (message.includes("unable to open database file")) {
    return `${fallback} This usually means Full Disk Access is missing for the app running this command.`;
  }
  if (message.includes("Operation not permitted")) {
    return `${fallback} macOS denied access. Full Disk Access is likely missing.`;
  }
  if (message.includes("Messages app is not running")) {
    return `${fallback} Open Messages.app first, then rerun this doctor command.`;
  }

  return `${fallback} ${message}`;
}

function printReport(checks: CheckResult[]): void {
  console.log("MemoLens iMessage Doctor");
  console.log(`Messages app path: ${MESSAGES_APP_PATH}`);
  console.log(`Messages db path: ${CHAT_DB_PATH}`);
  console.log("");

  for (const check of checks) {
    console.log(`[${check.status.toUpperCase()}] ${check.name}`);
    console.log(check.detail);
    console.log("");
  }

  const hasError = checks.some((check) => check.status === "error");
  if (!hasError) {
    console.log("Next step: run `npm run dev` in photon-bot.");
    return;
  }

  console.log("Likely fix:");
  console.log("1. Open System Settings -> Privacy & Security -> Full Disk Access.");
  console.log(
    "2. Add the exact app that runs `npm run dev`, for example Codex, Terminal, iTerm, VS Code, or Cursor.",
  );
  console.log("3. Fully quit and reopen that app.");
  console.log("4. Open Messages.app and make sure iMessage is logged in.");
  console.log("5. Rerun `npm run doctor:imessage`.");
}

void main().catch((error) => {
  console.error("Doctor failed:", error);
  process.exit(1);
});

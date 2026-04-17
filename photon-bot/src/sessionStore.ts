import type { SessionState } from "./types.js";

export class SessionStore {
  private readonly sessions = new Map<string, SessionState>();
  private readonly ttlMs: number;

  constructor(ttlMinutes: number) {
    this.ttlMs = ttlMinutes * 60_000;
  }

  get(key: string): SessionState | undefined {
    const session = this.sessions.get(key);
    if (!session) {
      return undefined;
    }

    if (this.isExpired(session)) {
      this.sessions.delete(key);
      return undefined;
    }

    return session;
  }

  set(key: string, state: SessionState): void {
    this.sessions.set(key, state);
  }

  clear(key: string): void {
    this.sessions.delete(key);
  }

  sweep(): void {
    for (const [key, state] of this.sessions.entries()) {
      if (this.isExpired(state)) {
        this.sessions.delete(key);
      }
    }
  }

  private isExpired(state: SessionState): boolean {
    const updatedAt = Date.parse(state.updatedAt);
    if (Number.isNaN(updatedAt)) {
      return true;
    }
    return Date.now() - updatedAt > this.ttlMs;
  }
}

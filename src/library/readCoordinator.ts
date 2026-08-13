export interface InboxReadTicket {
  epoch: number;
  cursor: string | null;
  controller: AbortController;
}

function cursorKey(cursor: string | null): string {
  return cursor === null ? "first" : `cursor:${cursor}`;
}

export class InboxReadCoordinator {
  private epoch = 0;
  private readonly active = new Map<string, AbortController>();

  replace(): number {
    this.epoch += 1;
    for (const controller of this.active.values()) {
      controller.abort();
    }
    this.active.clear();
    return this.epoch;
  }

  currentEpoch(): number {
    return this.epoch;
  }

  begin(epoch: number, cursor: string | null): InboxReadTicket | null {
    if (epoch !== this.epoch) {
      return null;
    }
    const key = cursorKey(cursor);
    if (this.active.has(key)) {
      return null;
    }
    const controller = new AbortController();
    this.active.set(key, controller);
    return { epoch, cursor, controller };
  }

  isCurrent(ticket: InboxReadTicket): boolean {
    return ticket.epoch === this.epoch
      && !ticket.controller.signal.aborted
      && this.active.get(cursorKey(ticket.cursor)) === ticket.controller;
  }

  settle(ticket: InboxReadTicket): void {
    const key = cursorKey(ticket.cursor);
    if (this.active.get(key) === ticket.controller) {
      this.active.delete(key);
    }
  }
}

export type IncomingMessage = {
  chatId: string;
  userId: string;
  senderName?: string;
  text: string;
  receivedAt: string;
};

export type BotReply = {
  text: string;
  imagePaths: string[];
};

export type SessionState = {
  lastQueryText: string;
  lastRelativePaths: string[];
  lastResultOffset: number;
  updatedAt: string;
};

export type RetrievalImage = {
  relative_path: string;
  place_name?: string | null;
  taken_at?: string | null;
  description?: string | null;
};

export type RetrievalResponse = {
  status: string;
  message?: string | null;
  title?: string | null;
  caption?: string | null;
  notes: string[];
  data: RetrievalImage[];
};

export type QueryPhotosInput = {
  text: string;
  topK: number;
};

export type ResolvedImageBatch = {
  imagePaths: string[];
  missingRelativePaths: string[];
  consumedCount: number;
};

export type MessageHandler = (message: IncomingMessage) => Promise<void>;

export type MessagePlatformAdapter = {
  startWatching(handlers: {
    onMessage: MessageHandler;
    onError: (error: Error) => void;
  }): Promise<void>;
  sendReply(chatId: string, reply: BotReply): Promise<void>;
  close(): Promise<void>;
};

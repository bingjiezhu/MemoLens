declare module "better-sqlite3" {
  export type Options = {
    readonly?: boolean;
    fileMustExist?: boolean;
  };

  export type Statement<T = unknown> = {
    get(...params: unknown[]): T;
  };

  export default class Database {
    constructor(filename: string, options?: Options);
    prepare<T = unknown>(sql: string): Statement<T>;
    close(): void;
  }

  export namespace Database {
    export type Database = import("better-sqlite3").default;
  }
}

export const DEFAULT_LOCAL_API_BASE = "http://127.0.0.1:5519";

export class LocalApiBaseError extends Error {
  readonly code = "local_api_base_denied";

  constructor() {
    super(
      "MemoLens requires VITE_BACKEND_BASE_URL to be a literal loopback HTTP(S) origin.",
    );
    this.name = "LocalApiBaseError";
  }
}

function isCanonicalIpv4Loopback(hostname: string): boolean {
  const octets = hostname.split(".");
  if (octets.length !== 4) return false;

  const values: number[] = [];
  for (const octet of octets) {
    if (!/^(?:0|[1-9][0-9]{0,2})$/.test(octet)) return false;
    const value = Number(octet);
    if (!Number.isInteger(value) || value > 255) return false;
    values.push(value);
  }
  return values[0] === 127;
}

function parseIpv6Groups(hostname: string): number[] | null {
  if (hostname.includes(".") || hostname.includes("%")) return null;

  const compression = hostname.indexOf("::");
  if (compression !== -1 && compression !== hostname.lastIndexOf("::")) return null;

  const parseSide = (value: string): number[] | null => {
    if (value === "") return [];
    const groups = value.split(":");
    if (groups.some((group) => !/^[0-9a-f]{1,4}$/i.test(group))) return null;
    return groups.map((group) => Number.parseInt(group, 16));
  };

  if (compression === -1) {
    const groups = parseSide(hostname);
    return groups?.length === 8 ? groups : null;
  }

  const left = parseSide(hostname.slice(0, compression));
  const right = parseSide(hostname.slice(compression + 2));
  if (!left || !right || left.length + right.length >= 8) return null;
  return [...left, ...Array(8 - left.length - right.length).fill(0), ...right];
}

function isLiteralIpv6Loopback(hostname: string): boolean {
  const groups = parseIpv6Groups(hostname);
  return Boolean(
    groups
      && groups.slice(0, 7).every((group) => group === 0)
      && groups[7] === 1,
  );
}

function validPort(port: string | undefined): boolean {
  if (port === undefined) return true;
  if (!/^(?:[1-9]|[1-9][0-9]{1,4})$/.test(port)) return false;
  const numericPort = Number(port);
  return numericPort >= 1 && numericPort <= 65_535;
}

export function resolveLocalApiBase(
  configuredBase: string | null | undefined,
  fallbackBase = DEFAULT_LOCAL_API_BASE,
): string {
  const candidate = configuredBase ?? fallbackBase;
  if (candidate === "" || candidate !== candidate.trim() || candidate.includes("\\")) {
    throw new LocalApiBaseError();
  }

  const originMatch = /^(https?):\/\/([^/?#]+)\/?$/i.exec(candidate);
  if (!originMatch) throw new LocalApiBaseError();

  const authority = originMatch[2];
  if (authority.includes("@")) throw new LocalApiBaseError();

  let literalLoopback = false;
  let rawPort: string | undefined;
  if (authority.startsWith("[")) {
    const match = /^\[([0-9a-f:]+)\](?::([0-9]+))?$/i.exec(authority);
    if (!match) throw new LocalApiBaseError();
    literalLoopback = isLiteralIpv6Loopback(match[1]);
    rawPort = match[2];
  } else {
    const match = /^([^:]+)(?::([0-9]+))?$/.exec(authority);
    if (!match) throw new LocalApiBaseError();
    literalLoopback = isCanonicalIpv4Loopback(match[1]);
    rawPort = match[2];
  }
  if (!literalLoopback || !validPort(rawPort)) throw new LocalApiBaseError();

  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    throw new LocalApiBaseError();
  }
  if (
    !["http:", "https:"].includes(parsed.protocol)
    || parsed.username !== ""
    || parsed.password !== ""
    || parsed.pathname !== "/"
    || parsed.search !== ""
    || parsed.hash !== ""
    || parsed.origin === "null"
  ) {
    throw new LocalApiBaseError();
  }
  return parsed.origin;
}

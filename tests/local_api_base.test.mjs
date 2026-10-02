import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_LOCAL_API_BASE,
  LocalApiBaseError,
  resolveLocalApiBase,
} from "../src/localApiBase.ts";

test("renderer API base defaults to the fixed local backend", () => {
  assert.equal(resolveLocalApiBase(undefined), DEFAULT_LOCAL_API_BASE);
  assert.equal(resolveLocalApiBase(null), DEFAULT_LOCAL_API_BASE);
});

test("renderer API base admits canonical literal loopback origins", () => {
  assert.equal(resolveLocalApiBase("http://127.0.0.1:5519/"), "http://127.0.0.1:5519");
  assert.equal(resolveLocalApiBase("https://127.255.2.3"), "https://127.255.2.3");
  assert.equal(resolveLocalApiBase("http://[::1]:5519"), "http://[::1]:5519");
  assert.equal(
    resolveLocalApiBase("http://[0:0:0:0:0:0:0:1]:5519"),
    "http://[::1]:5519",
  );
});

test("renderer API base rejects DNS names and browser numeric-host aliases", () => {
  for (const target of [
    "http://localhost:5519",
    "http://localtest.me:5519",
    "https://provider.example.invalid",
    "http://2130706433:5519",
    "http://127.1:5519",
    "http://0x7f000001:5519",
    "http://0177.0.0.1:5519",
    "http://127.000.0.1:5519",
    "http://127.0.0.1.example.invalid:5519",
  ]) {
    assert.throws(() => resolveLocalApiBase(target), LocalApiBaseError, target);
  }
});

test("renderer API base rejects confused authorities and non-origin URLs", () => {
  for (const target of [
    "http://user@127.0.0.1:5519",
    "http://127.0.0.1:5519@provider.example.invalid",
    "http://127.0.0.1\\@provider.example.invalid",
    "file:///tmp/memolens.sock",
    "http://192.168.1.2:5519",
    "http://[::2]:5519",
    "http://127.0.0.1:5519/v1",
    "http://127.0.0.1:5519?target=remote",
    "http://127.0.0.1:5519#remote",
    " http://127.0.0.1:5519",
    "http://127.0.0.1:00080",
    "http://127.0.0.1:65536",
    "",
  ]) {
    assert.throws(() => resolveLocalApiBase(target), LocalApiBaseError, target);
  }
});

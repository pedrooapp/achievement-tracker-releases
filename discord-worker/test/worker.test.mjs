// Testes do Worker sem Cloudflare: assina requisições como o Discord e chama o fetch().
// Rodar:  node --test releases-repo/discord-worker/test/worker.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import worker, { toTextCommand } from "../src/index.js";

const { publicKey, privateKey } = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
const rawPub = new Uint8Array(await crypto.subtle.exportKey("raw", publicKey));
const hex = (b) => [...b].map((x) => x.toString(16).padStart(2, "0")).join("");

const FEED = { games: { 219150: { revision: 1, status: "servers_shutdown" } }, tags: { grindy: { label_en: "Very grindy", breaks_100: false } } };
globalThis.fetch = async () => new Response(JSON.stringify(FEED));

const env = {
  DISCORD_PUBLIC_KEY: hex(rawPub),
  ADMIN_USER_IDS: "admin1",
  ADMIN_ROLE_ID: "role-admins",
  ADMIN_CHANNEL_ID: "admin-chan",
  FEED_URL: "https://example.invalid/feed.json",
};

async function call(payload, { badSignature = false } = {}) {
  const body = JSON.stringify(payload);
  const timestamp = String(Math.floor(Date.now() / 1000));
  const sig = new Uint8Array(await crypto.subtle.sign({ name: "Ed25519" }, privateKey, new TextEncoder().encode(timestamp + body)));
  if (badSignature) sig[0] ^= 1;
  const req = new Request("https://worker.test/", {
    method: "POST",
    body,
    headers: { "X-Signature-Ed25519": hex(sig), "X-Signature-Timestamp": timestamp },
  });
  const res = await worker.fetch(req, env);
  return { status: res.status, data: res.status === 200 ? await res.json() : null };
}

const cmd = (name, options, user = "admin1", roles = [], channel = "admin-chan") => ({ type: 2, channel_id: channel, member: { user: { id: user }, roles }, data: { name, options } });
const opt = (name, value) => ({ name, value, type: 3 });

test("PING com assinatura válida → PONG; assinatura errada → 401", async () => {
  assert.deepEqual((await call({ type: 1 })).data, { type: 1 });
  assert.equal((await call({ type: 1 }, { badSignature: true })).status, 401);
});

test("/game vira o comando de texto, visível no canal", async () => {
  const { data } = await call(cmd("game", [opt("appid", "219150"), opt("status", "servers_shutdown"), opt("shutdown", "2024-06-30"), opt("scope", "online mode"), opt("en", "Servers off."), opt("pt", "Desligados.")]));
  assert.equal(data.type, 4);
  assert.equal(data.data.flags, 0);
  assert.match(data.data.content, /```text\n!game 219150 servers_shutdown shutdown=2024-06-30 scope="online mode"\nen: Servers off\.\npt: Desligados\.\n```/);
});

test("tags criadas no feed valem como tipo; tipo desconhecido é recusado (efêmero)", async () => {
  const ok = await call(cmd("ach", [opt("appid", "219150"), opt("api_name", "GRIND_1"), opt("type", "grindy"), opt("en", "Long grind")]));
  assert.match(ok.data.data.content, /!ach 219150 GRIND_1 grindy\nen: Long grind/);
  const bad = await call(cmd("ach", [opt("appid", "219150"), opt("api_name", "X"), opt("type", "nope"), opt("en", "x")]));
  assert.equal(bad.data.data.flags, 64);
  assert.match(bad.data.data.content, /Unknown type/);
});

test("/tag add, /tag list e validação da chave", async () => {
  const add = await call(cmd("tag", [{ type: 1, name: "add", options: [opt("key", "speedrun"), { name: "breaks100", type: 5, value: true }, opt("en", "Speedrun required")] }]));
  assert.match(add.data.data.content, /!tag add speedrun breaks100=yes\nen: Speedrun required/);
  const list = await call(cmd("tag", [{ type: 1, name: "list", options: [] }]));
  assert.match(list.data.data.content, /grindy/);
  const builtin = await call(cmd("tag", [{ type: 1, name: "add", options: [opt("key", "dlc_required"), opt("en", "x")] }]));
  assert.match(builtin.data.data.content, /built-in/);
});

test("não-admin é recusado; admin por cargo é aceito; /help é livre", async () => {
  assert.match((await call(cmd("remove", [opt("appid", "1")], "stranger"))).data.data.content, /Only admins/);
  assert.match((await call(cmd("remove", [opt("appid", "1")], "mod", ["role-admins"]))).data.data.content, /!remove 1/);
  assert.match((await call(cmd("help", [], "stranger"))).data.data.content, /Built-in types/);
});

test("comando que muda o feed fora do canal de admins é recusado", async () => {
  const res = await call(cmd("remove", [opt("appid", "1")], "admin1", [], "other-chan"));
  assert.equal(res.data.data.flags, 64);
  assert.match(res.data.data.content, /<#admin-chan>/);
});

test("/show lê o feed; autocomplete lista os tipos", async () => {
  assert.match((await call(cmd("show", [opt("appid", "219150")]))).data.data.content, /servers_shutdown/);
  const ac = await call({ type: 4, member: { user: { id: "admin1" } }, data: { name: "ach", options: [{ name: "type", value: "gri", focused: true, type: 3 }] } });
  assert.deepEqual(ac.data.data.choices, [{ name: "grindy — Very grindy", value: "grindy" }]);
});

test("texto com ``` não quebra o bloco", () => {
  const out = toTextCommand("game", null, { appid: "5", status: "mp_required", en: "bad ``` text" }, FEED);
  assert.equal(out, "!game 5 mp_required\nen: bad ''' text");
});

async function signed(payload) {
  const body = JSON.stringify(payload);
  const timestamp = String(Math.floor(Date.now() / 1000));
  const sig = new Uint8Array(await crypto.subtle.sign({ name: "Ed25519" }, privateKey, new TextEncoder().encode(timestamp + body)));
  return new Request("https://worker.test/", { method: "POST", body, headers: { "X-Signature-Ed25519": hex(sig), "X-Signature-Timestamp": timestamp } });
}

test("com GITHUB_DISPATCH_TOKEN, comando que altera o feed dispara o sync", async () => {
  const calls = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return String(url).includes("api.github.com") ? new Response(null, { status: 204 }) : new Response(JSON.stringify(FEED));
  };
  try {
    const waits = [];
    const ctx = { waitUntil: (p) => waits.push(p) };
    const e = { ...env, GITHUB_DISPATCH_TOKEN: "t", GITHUB_REPO: "o/r" };
    const res = await (await worker.fetch(await signed(cmd("remove", [opt("appid", "219150")])), e, ctx)).json();
    assert.match(res.data.content, /about 1 minute/);
    assert.equal(waits.length, 1);
    await Promise.all(waits);
    const dispatch = calls.find((c) => c.url.includes("/dispatches"));
    assert.ok(dispatch, "chamou workflow_dispatch");
    assert.equal(dispatch.init.headers.Authorization, "Bearer t");
  } finally {
    globalThis.fetch = realFetch;
  }
});

test("sem token, não dispara e avisa ~10 min", async () => {
  const waits = [];
  const ctx = { waitUntil: (p) => waits.push(p) };
  const res = await (await worker.fetch(await signed(cmd("remove", [opt("appid", "219150")])), env, ctx)).json();
  assert.match(res.data.content, /~10 min/);
  assert.equal(waits.length, 0);
});

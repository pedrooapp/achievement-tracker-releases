// Testes do Worker sem Cloudflare: assina requisições como o Discord e chama o fetch().
// Rodar:  node --test releases-repo/discord-worker/test/worker.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import worker, { isAdmin, toTextCommand } from "../src/index.js";

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

test("/ach vira o comando de texto, visível no canal", async () => {
  const { data } = await call(cmd("ach", [opt("appid", "219150"), opt("api_name", "BOSS"), opt("obtainability", "unobtainable"), opt("add_tags", "MP_required, needs-coop"), opt("en", "Event over."), opt("pt", "Evento acabou.")]));
  assert.equal(data.type, 4);
  assert.equal(data.data.flags, 0);
  assert.match(data.data.content, /```text\n!ach 219150 apiname=BOSS obtainability=unobtainable add=mp_required,needs_coop\nen: Event over\.\npt: Evento acabou\.\n```/);
});

test("/ach recusa obtenção desconhecida, categoria usada como tag e comando vazio (efêmero)", async () => {
  for (const [options, needle] of [
    [[opt("obtainability", "gone")], /Unknown obtainability/],
    [[opt("add_tags", "unobtainable")], /not a tag/],
    [[opt("add_tags", "bad!tag")], /lowercase/],
    [[], /Nothing to change/],
  ]) {
    const res = await call(cmd("ach", [opt("appid", "219150"), opt("api_name", "X"), ...options]));
    assert.equal(res.data.data.flags, 64);
    assert.match(res.data.data.content, needle);
  }
});

test("/shutdown com data, now e remove", async () => {
  const dated = await call(cmd("shutdown", [opt("appid", "219150"), opt("date", "2027-01-31"), opt("scope", "online mode"), opt("achievements", " RANKED ; Co op Win;RANKED")]));
  assert.match(dated.data.data.content, /!shutdown 219150 2027-01-31 scope="online mode" achievements=RANKED;Co op Win/);
  assert.match((await call(cmd("shutdown", [opt("appid", "5"), opt("date", "now"), opt("achievements", "ALL")]))).data.data.content, /!shutdown 5 now achievements=all/);
  assert.match((await call(cmd("shutdown", [opt("appid", "5"), opt("date", "now"), opt("achievements", "none")]))).data.data.content, /!shutdown 5 now achievements=none/);
  assert.match((await call(cmd("removeshutdown", [opt("appid", "5")]))).data.data.content, /```text\n!removeshutdown 5\n```/);
  assert.match((await call(cmd("shutdown", [opt("appid", "5"), opt("date", "NOW")]))).data.data.content, /!shutdown 5 now/);
  assert.match((await call(cmd("shutdown", [opt("appid", "5"), opt("date", "remove")]))).data.data.content, /removeshutdown/);
  assert.match((await call(cmd("shutdown", [opt("appid", "5"), opt("date", "31/01/2027")]))).data.data.content, /YYYY-MM-DD/);
});

test("/tag add, /tag list e validação da chave", async () => {
  const add = await call(cmd("tag", [{ type: 1, name: "add", options: [opt("key", "speedrun"), opt("en", "Speedrun required")] }]));
  assert.match(add.data.data.content, /!tag add speedrun\nen: Speedrun required/);
  const list = await call(cmd("tag", [{ type: 1, name: "list", options: [] }]));
  assert.match(list.data.data.content, /grindy/);
  assert.match(list.data.data.content, /date_sensitive/);
  const builtin = await call(cmd("tag", [{ type: 1, name: "add", options: [opt("key", "dlc_required"), opt("en", "x")] }]));
  assert.match(builtin.data.data.content, /built-in/);
});

test("não-admin é recusado; admin por cargo é aceito; /help é livre", async () => {
  assert.match((await call(cmd("remove", [opt("appid", "1")], "stranger"))).data.data.content, /Only admins/);
  assert.match((await call(cmd("remove", [opt("appid", "1")], "mod", ["role-admins"]))).data.data.content, /!remove 1/);
  assert.match((await call(cmd("help", [], "stranger"))).data.data.content, /Built-in tags/);
});

test("comando que muda o feed fora do canal de admins é recusado", async () => {
  const res = await call(cmd("remove", [opt("appid", "1")], "admin1", [], "other-chan"));
  assert.equal(res.data.data.flags, 64);
  assert.match(res.data.data.content, /<#admin-chan>/);
});

test("/show lê o feed; autocomplete completa o último item da lista de tags", async () => {
  assert.match((await call(cmd("show", [opt("appid", "219150")]))).data.data.content, /servers_shutdown/);
  const ac = await call({ type: 4, member: { user: { id: "admin1" } }, data: { name: "ach", options: [{ name: "add_tags", value: "mp_required, gri", focused: true, type: 3 }] } });
  assert.deepEqual(ac.data.data.choices, [{ name: "mp_required,grindy — Very grindy", value: "mp_required,grindy" }]);
});

test("texto com ``` não quebra o bloco", () => {
  const out = toTextCommand("ach", null, { appid: "5", api_name: "X", en: "bad ``` text" }, FEED);
  assert.equal(out, "!ach 5 apiname=X\nen: bad ''' text");
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

test("alvos: lista com ; (sem repetir), all, e remove com lista", async () => {
  const many = await call(cmd("ach", [opt("appid", "5"), opt("api_name", " A ; B;A "), opt("obtainability", "possibly_obtainable")]));
  assert.match(many.data.data.content, /!ach 5 apiname=A;B obtainability=possibly_obtainable\n/);
  const fixed = await call(cmd("ach", [opt("appid", "5"), opt("api_name", "Pedro Approbato;B"), opt("obtainability", "none")]));
  assert.match(fixed.data.data.content, /!ach 5 apiname=Pedro Approbato;B obtainability=none\n/);
  const dated = await call(cmd("ach", [opt("appid", "5"), opt("api_name", "Xmas Event"), opt("date", "2026-12-25")]));
  assert.match(dated.data.data.content, /!ach 5 apiname=Xmas Event date=2026-12-25\n/);
  const badDate = await call(cmd("ach", [opt("appid", "5"), opt("api_name", "X"), opt("date", "25/12")]));
  assert.match(badDate.data.data.content, /YYYY-MM-DD/);
  const all = await call(cmd("ach", [opt("appid", "5"), opt("api_name", "all"), opt("add_tags", "mp_required")]));
  assert.match(all.data.data.content, /!ach 5 apiname=all add=mp_required\n/);
  const empty = await call(cmd("ach", [opt("appid", "5"), opt("api_name", " ; "), opt("obtainability", "unobtainable")]));
  assert.match(empty.data.data.content, /at least one API name/);
  // Espaço dentro do nome é aceito; só ; separa.
  const spaced = await call(cmd("ach", [opt("appid", "5"), opt("api_name", " Pedro Approbato ; Outra Conquista "), opt("obtainability", "unobtainable")]));
  assert.match(spaced.data.data.content, /!ach 5 apiname=Pedro Approbato;Outra Conquista obtainability=unobtainable/);
  const badChar = await call(cmd("ach", [opt("appid", "5"), opt("api_name", "A=B"), opt("obtainability", "unobtainable")]));
  assert.match(badChar.data.data.content, /can't contain/);
  assert.match((await call(cmd("remove", [opt("appid", "5"), opt("api_name", "X;Y")]))).data.data.content, /!remove 5 apiname=X;Y/);
  assert.match((await call(cmd("ach", [opt("appid", "5"), opt("api_name", "X"), opt("add_tags", "shutdown")]))).data.data.content, /not a tag/);
});

test("/game saiu: use /ach api_name:all", () => {
  assert.throws(() => toTextCommand("game", null, { appid: "5" }, FEED), /Unknown command/);
});

test("produção: só o cargo libera (sem lista de IDs)", async () => {
  const ROLE = "1574927607231815760";
  const prod = { ...env, ADMIN_USER_IDS: undefined, ADMIN_ROLE_ID: ROLE };
  const ask = async (user, roles, name = "remove", options = [opt("appid", "1")]) =>
    (await (await worker.fetch(await signed(cmd(name, options, user, roles)), prod, { waitUntil() {} })).json()).data.content;
  // Com o cargo: aceito.
  assert.match(await ask("anyone", [ROLE]), /!remove 1/);
  // Sem o cargo: recusado, mesmo quem estava na lista antiga de IDs e mesmo com outro cargo.
  for (const [user, roles] of [["admin1", []], ["330805976192253952", []], ["mod", ["role-admins"]], ["x", []]]) {
    assert.match(await ask(user, roles), /Only admins/, user);
    assert.match(await ask(user, roles, "show", [opt("appid", "219150")]), /Only admins/, user);
  }
  // /help continua livre (só texto de ajuda).
  assert.match(await ask("x", [], "help", []), /Built-in tags/);
});

test("cargo no secret com espaço/quebra de linha (gravado pelo PowerShell) ainda vale", () => {
  const ROLE = "1574927607231815760";
  const member = (roles) => ({ member: { user: { id: "u" }, roles } });
  for (const secret of [ROLE, `${ROLE}\r\n`, ` ${ROLE} `, `111, ${ROLE}`]) {
    assert.equal(isAdmin(member([ROLE]), { ADMIN_ROLE_ID: secret }), true, JSON.stringify(secret));
    assert.equal(isAdmin(member(["999"]), { ADMIN_ROLE_ID: secret }), false, JSON.stringify(secret));
  }
  assert.equal(isAdmin(member([ROLE]), {}), false, "sem cargo configurado ninguém passa");
});

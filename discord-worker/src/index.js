// Cloudflare Worker: responde NA HORA aos comandos de barra do bot de avisos (/ach, /shutdown, /game…).
//
// Só responde: não grava nada e não guarda nenhum token. A resposta (visível no canal) traz o
// comando equivalente num bloco ```text; o sync do GitHub Actions (scripts/discord_warnings_sync.py)
// lê esse bloco, aplica no unobtainable_warnings.json e reage com ✅. O app lê o feed de hora em hora.
//
// Variáveis (wrangler.toml, nenhuma secreta): DISCORD_PUBLIC_KEY, DISCORD_APPLICATION_ID,
// ADMIN_USER_IDS (vírgulas), ADMIN_ROLE_ID (opcional), ADMIN_CHANNEL_ID, FEED_URL, GITHUB_REPO.
// Secret opcional GITHUB_DISPATCH_TOKEN (fine-grained, só "Actions: write" neste repositório):
// depois de cada comando que altera o feed, o Worker dispara o sync na hora em vez de esperar o
// agendamento do GitHub (que pode atrasar horas).

// Modelo do feed (version 2): obtenção por conquista (notifica quem não tem a conquista),
// shutdown do jogo com data (notifica quem não tem 100%) e tags de conquista (nunca notificam).
const OBTAINABILITY = ["unobtainable", "possibly_obtainable", "broken_but_obtainable"];
const BUILTIN_TAGS = ["mp_required", "dlc_required", "unavailable", "developer_required", "date_sensitive"];
// `shutdown` não é tag de classificação: tem bloco próprio (/shutdown).
const NOT_TAGS = [...OBTAINABILITY, "unobtainable_100", "partial_broken", "servers_shutdown", "shutdown", "shut_down"];
const TAG_KEY = /^[a-z][a-z0-9_]{1,31}$/;
const EPHEMERAL = 64;

// Tipos de interação/resposta do Discord.
const PING = 1, COMMAND = 2, AUTOCOMPLETE = 4;
const PONG = 1, MESSAGE = 4, AUTOCOMPLETE_RESULT = 8;

// ---------------------------------------------------------------- assinatura (Ed25519)

function hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
  return out;
}

let keyCache = null;
async function publicKey(hex) {
  if (keyCache?.hex === hex) return keyCache.key;
  let key;
  try {
    key = await crypto.subtle.importKey("raw", hexToBytes(hex), { name: "Ed25519" }, false, ["verify"]);
  } catch {
    // Runtimes antigos do Workers usam o nome NODE-ED25519.
    key = await crypto.subtle.importKey("raw", hexToBytes(hex), { name: "NODE-ED25519", namedCurve: "NODE-ED25519" }, false, ["verify"]);
  }
  keyCache = { hex, key };
  return key;
}

export async function verify(request, body, publicKeyHex) {
  const signature = request.headers.get("X-Signature-Ed25519");
  const timestamp = request.headers.get("X-Signature-Timestamp");
  if (!signature || !timestamp || !/^[0-9a-f]+$/i.test(signature)) return false;
  const key = await publicKey(publicKeyHex);
  const algorithm = key.algorithm.name === "Ed25519" ? { name: "Ed25519" } : { name: "NODE-ED25519" };
  return crypto.subtle.verify(algorithm, key, hexToBytes(signature), new TextEncoder().encode(timestamp + body));
}

// ---------------------------------------------------------------- feed (só leitura, para validar e /show)

let feedCache = { at: 0, data: null };
async function loadFeed(url) {
  if (feedCache.data && Date.now() - feedCache.at < 60_000) return feedCache.data;
  try {
    const res = await fetch(url, { cf: { cacheTtl: 60 } });
    if (res.ok) feedCache = { at: Date.now(), data: await res.json() };
  } catch {
    /* sem feed: valida só o formato */
  }
  return feedCache.data ?? { games: {}, tags: {} };
}

/** Tags oficiais + as que os admins já usaram (feed.tags). */
const allTags = (feed) => [...BUILTIN_TAGS, ...Object.keys(feed.tags ?? {}).filter((k) => !BUILTIN_TAGS.includes(k)).sort()];

// ---------------------------------------------------------------- respostas

const json = (data) => new Response(JSON.stringify(data), { headers: { "content-type": "application/json" } });
const reply = (content, ephemeral = true) =>
  json({ type: MESSAGE, data: { content: content.slice(0, 1990), flags: ephemeral ? EPHEMERAL : 0, allowed_mentions: { parse: [] } } });

class UserError extends Error {}

/** Opções do comando → { nome: valor }, incluindo subcomando (/tag add …). */
function optionsOf(data) {
  let opts = data.options ?? [];
  let sub = null;
  if (opts.length === 1 && opts[0].type === 1) {
    sub = opts[0].name;
    opts = opts[0].options ?? [];
  }
  return { sub, values: Object.fromEntries(opts.map((o) => [o.name, o.value])) };
}

/** Texto seguro para dentro do bloco: uma linha, sem ``` (que fecharia o bloco). */
const clean = (v) => String(v ?? "").replace(/```/g, "'''").replace(/\s+/g, " ").trim();
const quoteOpt = (k, v) => (/\s/.test(v) ? `${k}="${v.replace(/"/g, "'")}"` : `${k}=${v}`);

function appIdOf(v) {
  const s = String(v ?? "").trim();
  if (!/^\d+$/.test(s) || Number(s) <= 0) throw new UserError(`\`${s}\` is not a Steam AppID (numbers only).`);
  return String(Number(s));
}

function obtainabilityOf(v) {
  const o = clean(v).toLowerCase();
  if (o === "none" || OBTAINABILITY.includes(o)) return o;
  throw new UserError(`Unknown obtainability \`${o}\`. Use: ${OBTAINABILITY.join(", ")} or none.`);
}

/** "a, B-c" → "a,b_c". Chave nova é aceita (o sync cria o nome dela). */
function tagListOf(v, field) {
  const keys = clean(v)
    .split(",")
    .map((t) => t.trim().toLowerCase().replace(/[\s-]+/g, "_"))
    .filter(Boolean);
  for (const k of keys) {
    if (NOT_TAGS.includes(k)) throw new UserError(`\`${k}\` is not a tag. Use \`obtainability\` or /shutdown instead.`);
    if (!TAG_KEY.test(k)) throw new UserError(`\`${field}\`: tag \`${k}\` must use lowercase letters, numbers and _ (2–32 chars), starting with a letter.`);
  }
  return [...new Set(keys)].join(",");
}

function textLines(values) {
  const lines = [];
  if (clean(values.en)) lines.push(`en: ${clean(values.en)}`);
  if (clean(values.pt)) lines.push(`pt: ${clean(values.pt)}`);
  return lines;
}

/** " ACH_1 ; ACH_2 ; ACH_1 " → ["ACH_1", "ACH_2"] (também aceita vírgula). */
export function splitTargets(v) {
  const out = [];
  for (const part of String(v ?? "").split(/[;,]/)) {
    const t = part.trim();
    if (t && !out.includes(t)) out.push(t);
  }
  return out;
}

/** Alvo de conquistas → texto do comando: "all", "none" (se permitido) ou "ACH_1;ACH_2". */
function targetsOf(v, field, { allowNone = false } = {}) {
  const raw = clean(v);
  const low = raw.toLowerCase();
  if (low === "all") return "all";
  if (allowNone && (low === "none" || low === "")) return "none";
  const names = splitTargets(raw);
  if (!names.length) throw new UserError(`\`${field}\`: give at least one API name (several with \`;\`, e.g. \`ACH_1;ACH_2\`) or \`all\`.`);
  const bad = names.find((n) => /\s/.test(n));
  if (bad) throw new UserError(`\`${field}\`: \`${bad}\` has a space. Separate API names with \`;\`.`);
  return names.join(";");
}

/** Comando de barra → comando de texto equivalente (o que o sync aplica). */
export function toTextCommand(name, sub, values, feed) {
  switch (name) {
    case "ach": {
      const head = [`!ach ${appIdOf(values.appid)} ${targetsOf(values.api_name, "api_name")}`];
      if (values.obtainability !== undefined) head.push(obtainabilityOf(values.obtainability));
      for (const [opt, field] of [["add", "add_tags"], ["del", "remove_tags"]]) {
        if (clean(values[field])) head.push(`${opt}=${tagListOf(values[field], field)}`);
      }
      if (head.length === 1 && !textLines(values).length) {
        throw new UserError("Nothing to change: set `obtainability`, tags or a note. (Shutdowns use /shutdown.)");
      }
      return [head.join(" "), ...textLines(values)].join("\n");
    }
    case "shutdown": {
      const when = clean(values.date).toLowerCase() || "now";
      if (when === "remove") throw new UserError("To remove a shutdown use /removeshutdown.");
      if (when !== "now" && !/^\d{4}-\d{2}-\d{2}$/.test(when)) {
        throw new UserError("`date` must be YYYY-MM-DD or `now` (already shut down).");
      }
      const head = [`!shutdown ${appIdOf(values.appid)} ${when}`];
      if (clean(values.scope)) head.push(quoteOpt("scope", clean(values.scope)));
      if (values.achievements !== undefined) {
        head.push(`achievements=${targetsOf(values.achievements, "achievements", { allowNone: true })}`);
      }
      return [head.join(" "), ...textLines(values)].join("\n");
    }
    case "removeshutdown":
      return `!removeshutdown ${appIdOf(values.appid)}`;
    case "remove": {
      const app = appIdOf(values.appid);
      return clean(values.api_name) ? `!remove ${app} ${targetsOf(values.api_name, "api_name")}` : `!remove ${app}`;
    }
    case "tag": {
      const key = clean(values.key).toLowerCase();
      if (BUILTIN_TAGS.includes(key)) throw new UserError(`\`${key}\` is a built-in tag: its name is already translated in the app.`);
      if (NOT_TAGS.includes(key)) throw new UserError(`\`${key}\` is not a tag.`);
      if (!TAG_KEY.test(key)) throw new UserError("Tag keys use lowercase letters, numbers and `_` (2–32 chars), starting with a letter. Ex.: `grindy`.");
      if (sub === "remove") return `!tag remove ${key}`;
      return [`!tag add ${key}`, ...textLines(values)].join("\n");
    }
    default:
      throw new UserError(`Unknown command /${name}.`);
  }
}

const HELP = [
  "**Achievement Tracker — warnings**",
  "Achievement targets: one API name, several with `;` (`ACH_1;ACH_2`) or `all` (every achievement of the game).",
  "`/ach` mark achievements: `obtainability` (unobtainable · possibly_obtainable · broken_but_obtainable · none), `add_tags` / `remove_tags`, note",
  "`/shutdown` servers shutting down on a date (`date: YYYY-MM-DD`) or already off (`now`); `achievements`: none · all · `ACH_1;ACH_2` (they break on the date) · `/removeshutdown` takes it off",
  "`/remove` a game or some achievements · `/show` what's published · `/tag add|remove|list` names for your own tags",
  `Built-in tags: ${BUILTIN_TAGS.join(", ")} (any new key works too; \`shutdown\` is not a tag)`,
  "Who gets notified: obtainability → players who don't have that achievement · shutdown → players below 100% · tags → nobody. The app writes the game summary from what's marked.",
  "Changes are published in about 1 minute (✅ on the message, or ❌ with the reason). In the app: Settings → Sync now, or it updates on its own within the hour.",
].join("\n");

/** "id1, id2\r\n" → ["id1", "id2"]: o secret pode vir com espaço/quebra de linha do terminal. */
export const idList = (v) => String(v ?? "").split(/[\s,;]+/).filter(Boolean);

export function isAdmin(interaction, env) {
  const user = interaction.member?.user?.id ?? interaction.user?.id;
  if (user && idList(env.ADMIN_USER_IDS).includes(user)) return true;
  const roles = interaction.member?.roles ?? [];
  return idList(env.ADMIN_ROLE_ID).some((r) => roles.includes(r));
}

/** Dispara o workflow de sync (workflow_dispatch). Espera um pouco para a resposta já estar no canal. */
export async function triggerSync(env, delayMs = 3000) {
  if (!env.GITHUB_DISPATCH_TOKEN || !env.GITHUB_REPO) return false;
  if (delayMs) await new Promise((r) => setTimeout(r, delayMs));
  const res = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/discord-warnings-sync.yml/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "achievement-warnings-bot",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  if (!res.ok) console.log(`sync dispatch failed: HTTP ${res.status}`);
  return res.ok;
}

async function handleCommand(interaction, env, ctx) {
  const { name } = interaction.data;
  const { sub, values } = optionsOf(interaction.data);
  if (name === "help") return reply(HELP);
  if (!isAdmin(interaction, env)) return reply("⛔ Only admins can use this command.");

  const feed = await loadFeed(env.FEED_URL);
  if (name === "show") {
    const appId = appIdOf(values.appid);
    const game = feed.games?.[appId];
    if (!game) return reply(`No warning published for \`${appId}\` (changes from the last ~10 min may not be published yet).`);
    const text = JSON.stringify(game, null, 2);
    return reply("```json\n" + (text.length > 1900 ? text.slice(0, 1880) + "\n…" : text) + "\n```");
  }
  if (name === "tag" && sub === "list") {
    const tags = feed.tags ?? {};
    const lines = [...BUILTIN_TAGS.map((k) => `\`${k}\` (built-in)`)];
    for (const k of Object.keys(tags).sort()) {
      if (BUILTIN_TAGS.includes(k)) continue;
      const t = tags[k];
      lines.push(`\`${k}\` — ${t.label_en || k} / ${t.label_pt || "—"}`);
    }
    return reply("**Tags** (never notify)\n" + lines.join("\n"));
  }

  // O sync só lê o canal de admins: confirmação em outro canal nunca seria aplicada.
  if (env.ADMIN_CHANNEL_ID && interaction.channel_id !== env.ADMIN_CHANNEL_ID) {
    return reply(`⚠️ Use this command in <#${env.ADMIN_CHANNEL_ID}> so it can be published.`);
  }
  const command = toTextCommand(name, sub, values, feed);
  const user = interaction.member?.user?.id ?? interaction.user?.id;
  // Visível no canal (não efêmera): é esta mensagem que o sync aplica.
  const instant = Boolean(env.GITHUB_DISPATCH_TOKEN && env.GITHUB_REPO);
  if (instant) ctx?.waitUntil(triggerSync(env));
  const when = instant ? "published in about 1 minute" : "published on the next sync (~10 min)";
  return reply(`✅ **Queued** by <@${user}> — ${when}.\n\`\`\`text\n${command}\n\`\`\``, false);
}

async function handleAutocomplete(interaction, env) {
  const focused = (interaction.data.options ?? []).flatMap((o) => (o.type === 1 ? o.options ?? [] : [o])).find((o) => o.focused);
  // Listas separadas por vírgula: completa só o último item.
  const typed = String(focused?.value ?? "");
  const parts = typed.split(",");
  const last = parts.pop().trim().toLowerCase();
  const prefix = parts.map((p) => p.trim()).filter(Boolean);
  const feed = await loadFeed(env.FEED_URL);
  const choices = allTags(feed)
    .filter((t) => t.includes(last) && !prefix.includes(t))
    .slice(0, 25)
    .map((t) => {
      const value = [...prefix, t].join(",");
      const label = feed.tags?.[t]?.label_en ? `${value} — ${feed.tags[t].label_en}` : value;
      return { name: label.slice(0, 100), value: value.slice(0, 100) };
    });
  return json({ type: AUTOCOMPLETE_RESULT, data: { choices } });
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") return new Response("Achievement Tracker warnings bot", { status: 200 });
    const body = await request.text();
    if (!(await verify(request, body, env.DISCORD_PUBLIC_KEY))) return new Response("invalid request signature", { status: 401 });

    const interaction = JSON.parse(body);
    try {
      if (interaction.type === PING) return json({ type: PONG });
      if (interaction.type === COMMAND) return await handleCommand(interaction, env, ctx);
      if (interaction.type === AUTOCOMPLETE) return await handleAutocomplete(interaction, env);
      return reply("Unsupported interaction.");
    } catch (e) {
      if (e instanceof UserError) return reply(`❌ ${e.message}`);
      return reply("❌ Something went wrong. Try again in a moment.");
    }
  },
};

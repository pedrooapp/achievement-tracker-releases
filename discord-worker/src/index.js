// Cloudflare Worker: responde NA HORA aos comandos de barra do bot de avisos (/game, /ach, /tag…).
//
// Só responde: não grava nada e não guarda nenhum token. A resposta (visível no canal) traz o
// comando equivalente num bloco ```text; o sync do GitHub Actions (scripts/discord_warnings_sync.py)
// lê esse bloco, aplica no unobtainable_warnings.json e reage com ✅. O app lê o feed de hora em hora.
//
// Variáveis (wrangler.toml, nenhuma secreta): DISCORD_PUBLIC_KEY, DISCORD_APPLICATION_ID,
// ADMIN_USER_IDS (vírgulas), ADMIN_ROLE_ID (opcional), ADMIN_CHANNEL_ID, FEED_URL.

const BUILTIN = ["servers_shutdown", "unobtainable_100", "partial_broken", "mp_required", "dlc_required"];
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

const allTypes = (feed) => [...BUILTIN, ...Object.keys(feed.tags ?? {}).sort()];

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

function typeOf(v, feed) {
  const t = clean(v).toLowerCase();
  if (!allTypes(feed).includes(t)) {
    throw new UserError(
      `Unknown type \`${t}\`. Use: ${allTypes(feed).join(", ")}.\nIf you just created it with /tag add, wait for the next sync (~10 min).`,
    );
  }
  return t;
}

function textLines(values) {
  const lines = [];
  if (clean(values.en)) lines.push(`en: ${clean(values.en)}`);
  if (clean(values.pt)) lines.push(`pt: ${clean(values.pt)}`);
  return lines;
}

/** Comando de barra → comando de texto equivalente (o que o sync aplica). */
export function toTextCommand(name, sub, values, feed) {
  switch (name) {
    case "game": {
      const head = [`!game ${appIdOf(values.appid)} ${typeOf(values.status, feed)}`];
      if (values.shutdown) {
        if (!/^\d{4}-\d{2}-\d{2}$/.test(clean(values.shutdown))) throw new UserError("`shutdown` must be a date in YYYY-MM-DD format.");
        head.push(`shutdown=${clean(values.shutdown)}`);
      }
      if (clean(values.scope)) head.push(quoteOpt("scope", clean(values.scope)));
      return [head.join(" "), ...textLines(values)].join("\n");
    }
    case "ach": {
      const api = clean(values.api_name);
      if (!/^\S+$/.test(api)) throw new UserError("`api_name` can't have spaces.");
      const head = [`!ach ${appIdOf(values.appid)} ${api} ${typeOf(values.type, feed)}`];
      if (clean(values.tags)) head.push(`tags=${clean(values.tags).replace(/\s*,\s*/g, ",").replace(/\s/g, "_")}`);
      return [head.join(" "), ...textLines(values)].join("\n");
    }
    case "remove": {
      const api = clean(values.api_name);
      return `!remove ${appIdOf(values.appid)}${api ? ` ${api}` : ""}`;
    }
    case "tag": {
      const key = clean(values.key).toLowerCase();
      if (BUILTIN.includes(key)) throw new UserError(`\`${key}\` is a built-in type and can't be changed.`);
      if (!TAG_KEY.test(key)) throw new UserError("Tag keys use lowercase letters, numbers and `_` (2–32 chars), starting with a letter. Ex.: `grindy`.");
      if (sub === "remove") return `!tag remove ${key}`;
      const head = [`!tag add ${key}`];
      if (values.breaks100 !== undefined) head.push(`breaks100=${values.breaks100 ? "yes" : "no"}`);
      return [head.join(" "), ...textLines(values)].join("\n");
    }
    default:
      throw new UserError(`Unknown command /${name}.`);
  }
}

const HELP = [
  "**Achievement Tracker — warnings**",
  "`/game` flag a whole game · `/ach` flag one achievement · `/remove` remove a game or achievement",
  "`/show` see what's published · `/tag add|remove|list` manage your own types (tags)",
  `Built-in types: ${BUILTIN.join(", ")}`,
  "Changes are confirmed here right away and published on the next sync (~10 min); the app picks them up within the hour.",
].join("\n");

function isAdmin(interaction, env) {
  const user = interaction.member?.user?.id ?? interaction.user?.id;
  const admins = String(env.ADMIN_USER_IDS ?? "").split(",").map((s) => s.trim()).filter(Boolean);
  if (user && admins.includes(user)) return true;
  return Boolean(env.ADMIN_ROLE_ID && interaction.member?.roles?.includes(env.ADMIN_ROLE_ID));
}

async function handleCommand(interaction, env) {
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
    const lines = [...BUILTIN.map((k) => `\`${k}\` (built-in)`)];
    for (const k of Object.keys(tags).sort()) {
      const t = tags[k];
      lines.push(`\`${k}\` — ${t.label_en || k} / ${t.label_pt || "—"}${t.breaks_100 ? " · breaks 100%" : ""}`);
    }
    return reply("**Types**\n" + lines.join("\n"));
  }

  // O sync só lê o canal de admins: confirmação em outro canal nunca seria aplicada.
  if (env.ADMIN_CHANNEL_ID && interaction.channel_id !== env.ADMIN_CHANNEL_ID) {
    return reply(`⚠️ Use this command in <#${env.ADMIN_CHANNEL_ID}> so it can be published.`);
  }
  const command = toTextCommand(name, sub, values, feed);
  const user = interaction.member?.user?.id ?? interaction.user?.id;
  // Visível no canal (não efêmera): é esta mensagem que o sync aplica.
  return reply(`✅ **Queued** by <@${user}> — published on the next sync (~10 min).\n\`\`\`text\n${command}\n\`\`\``, false);
}

async function handleAutocomplete(interaction, env) {
  const { values } = optionsOf(interaction.data);
  const focused = (interaction.data.options ?? []).flatMap((o) => (o.type === 1 ? o.options ?? [] : [o])).find((o) => o.focused);
  const typed = String(focused?.value ?? values.type ?? values.status ?? "").toLowerCase();
  const feed = await loadFeed(env.FEED_URL);
  const choices = allTypes(feed)
    .filter((t) => t.includes(typed))
    .slice(0, 25)
    .map((t) => ({ name: feed.tags?.[t]?.label_en ? `${t} — ${feed.tags[t].label_en}`.slice(0, 100) : t, value: t }));
  return json({ type: AUTOCOMPLETE_RESULT, data: { choices } });
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("Achievement Tracker warnings bot", { status: 200 });
    const body = await request.text();
    if (!(await verify(request, body, env.DISCORD_PUBLIC_KEY))) return new Response("invalid request signature", { status: 401 });

    const interaction = JSON.parse(body);
    try {
      if (interaction.type === PING) return json({ type: PONG });
      if (interaction.type === COMMAND) return await handleCommand(interaction, env);
      if (interaction.type === AUTOCOMPLETE) return await handleAutocomplete(interaction, env);
      return reply("Unsupported interaction.");
    } catch (e) {
      if (e instanceof UserError) return reply(`❌ ${e.message}`);
      return reply("❌ Something went wrong. Try again in a moment.");
    }
  },
};

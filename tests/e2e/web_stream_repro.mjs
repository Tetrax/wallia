// Reproduction navigateur RÉELLE (run209-web, point 4) : quand les callbacks
// `meta` + `sources` + `done` du flux SSE arrivent dans LE MÊME chunk réseau,
// React peut regrouper les setState et le finaliseur lisait une ref encore
// vide — le statut web était perdu après la fin du flux. Ce script monte le
// VRAI App (aucune copie de logique), avec un stub fetch local (AUCUN réseau,
// AUCUN serveur) qui livre tout le flux en UN SEUL chunk, puis vérifie que la
// note « Web constructeur public — version non vérifiée » est bien présente
// dans le message final après la fin du stream.
//
//     cd tests/e2e && node web_stream_repro.mjs
//
// SANS nouvelle dépendance : le bundle est construit avec l'esbuild déjà
// présent dans frontend/node_modules et rendu par le navigateur Playwright
// DÉJÀ installé sur la machine (chromium / chrome-headless-shell), piloté en
// `--dump-dom`. Le répertoire et le bundle produits sont CONSERVÉS sous TMPDIR
// (preuve relisible). Si aucun navigateur n'est trouvé, le script le dit et
// échoue explicitement — jamais un faux PASS.
//
// Les sauts de ligne SSE et HTML sont construits par CODE
// (String.fromCharCode(10)) et jamais par séquence d'échappement : le fichier
// ne contient aucun antislash, l'exactitude ne dépend d'aucune couche
// d'échappement.
//
// NON EXÉCUTÉ à l'écriture du lot : le principal lance ce script après revue.
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND = resolve(HERE, "../../frontend");
const RESULT_ID = "wallia-stream-repro-result";

// Point d'entrée du bundle : App RÉEL + driver de scénario (stub fetch local,
// pilotage de l'UI, vérifications). Aucune copie de la logique applicative.
const ENTRY = `
import { createElement } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";

const RESULT_ID = "wallia-stream-repro-result";
const checks = [];
const errors = [];

function record(name, pass, detail) {
  checks.push({ name: name, pass: Boolean(pass), detail: detail || "" });
}

function setResult(payload) {
  const json = JSON.stringify(payload);
  const bytes = new TextEncoder().encode(json);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
  const el = document.getElementById(RESULT_ID);
  if (el) el.textContent = "__WALLIA_REPRO__" + btoa(binary) + "__END__";
}

window.addEventListener("error", function (event) {
  errors.push("error: " + String(event.message));
});
window.addEventListener("unhandledrejection", function (event) {
  errors.push("rejection: " + String(event.reason));
});

const USER = {
  id: "user-1",
  email: "repro@wallia.local",
  is_admin: true,
  created_at: null,
  password_changed_at: null,
};

const WEB_SOURCE = {
  source_type: "web",
  chunk_id: null,
  document_id: null,
  title: "WALLIX Bastion — page publique",
  product: null,
  versions: [],
  demo: false,
  scope: "web",
  language: null,
  page_start: null,
  page_end: null,
  section: null,
  kind: "web",
  text: "Extrait public du constructeur (non qualifié).",
  score: null,
  score_vector: null,
  score_text: null,
  url: "https://www.wallix.com/fr/produits/wallix-bastion",
  domain: "www.wallix.com",
  version_state: "non_verifiee",
};

const CONVERSATION = {
  id: "conv-1",
  title: "Cas repro",
  case_state: {},
  created_at: null,
  updated_at: null,
};

const STATUS = {
  app: { version: "repro", git_sha: null, env: "repro", demo_mode: false },
  user: USER,
  embedding: { backend: "fake", model: "repro/embedding", revision: "0", dim: 384 },
  provider: {
    endpoint: "http://local.invalid/v1",
    model: "fake-model",
    key_configured: true,
    vision_enabled: false,
    last_test: null,
  },
  web: { available: false, reason: null },
  vision: { available: false, reason: null },
  retrieval: { top_k: 8, reranker: { backend: "fake", model: "fake-model", revision: "0" } },
  corpus: { documents_total: 0, documents_by_status: {}, chunks_serving: 0 },
  jobs: { by_status: {}, worker: { alive: true } },
};

let chatServed = 0;

function jsonResponse(payload) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function sseAllInOneChunk() {
  chatServed += 1;
  const NL = String.fromCharCode(10);
  const meta = { message_id: "msg-1", conversation_id: "conv-1", model: "fake-model", demo: false };
  const status = { state: "generation", label: "Rédaction de la réponse" };
  const sources = {
    status: "empty_corpus",
    sources: [WEB_SOURCE],
    diagnostics: {},
    web: { status: "ok", reason: null, query: "site:wallix.com voyant" },
  };
  const delta = { text: "Réponse locale de contrôle selon [1]." };
  const done = { message_id: "msg-1", status: "complete", model: "fake-model", demo: false, sources_count: 1 };
  // meta + status + sources + delta + done dans UN SEUL chunk réseau : les
  // callbacks sont regroupés et peuvent précéder tout render React.
  const frames =
    "event: meta" + NL + "data: " + JSON.stringify(meta) + NL + NL +
    "event: status" + NL + "data: " + JSON.stringify(status) + NL + NL +
    "event: sources" + NL + "data: " + JSON.stringify(sources) + NL + NL +
    "event: delta" + NL + "data: " + JSON.stringify(delta) + NL + NL +
    "event: done" + NL + "data: " + JSON.stringify(done) + NL + NL;
  const stream = new ReadableStream({
    start: function (controller) {
      controller.enqueue(new TextEncoder().encode(frames));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

window.fetch = async function (input, init) {
  const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  const parsed = new URL(url, "http://wallia.local");
  const path = parsed.pathname;
  const method = String((init && init.method) || "GET").toUpperCase();
  if (path === "/api/auth/me") return jsonResponse({ user: USER, csrf_token: "csrf-repro" });
  if (path === "/api/status") return jsonResponse(STATUS);
  if (path === "/api/conversations" && method === "GET") {
    return jsonResponse({ conversations: [CONVERSATION] });
  }
  if (path === "/api/conversations" && method === "POST") return jsonResponse(CONVERSATION);
  if (path === "/api/conversations/conv-1" && method === "GET") {
    return jsonResponse(
      Object.assign({}, CONVERSATION, {
        messages: [
          {
            id: "msg-user-1",
            conversation_id: "conv-1",
            seq: 1,
            role: "user",
            content: "Le voyant du Bastion et le journal de rotation",
            status: "complete",
            error: null,
            model: null,
            demo: false,
            sources: null,
            created_at: null,
            updated_at: null,
          },
          {
            id: "msg-1",
            conversation_id: "conv-1",
            seq: 2,
            role: "assistant",
            content: "Réponse locale de contrôle selon [1].",
            status: "complete",
            error: null,
            model: "fake-model",
            demo: false,
            sources: [WEB_SOURCE],
            created_at: null,
            updated_at: null,
          },
        ],
        attachments: [],
      }),
    );
  }
  if (path === "/api/conversations/conv-1/chat" && method === "POST") return sseAllInOneChunk();
  return jsonResponse({});
};

async function waitFor(label, predicate, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    let value = null;
    try {
      value = predicate();
    } catch (error) {
      value = null;
    }
    if (value) return value;
    if (Date.now() > deadline) throw new Error("attente épuisée : " + label);
    await new Promise(function (resolvePromise) {
      setTimeout(resolvePromise, 20);
    });
  }
}

async function runScenario() {
  const root = createRoot(document.getElementById("root"));
  root.render(createElement(App));
  await waitFor("application montée", function () {
    return document.querySelector(".app-shell");
  }, 8000);
  record("application montée (.app-shell)", true);

  const createButton = await waitFor("bouton créer une conversation", function () {
    return document.querySelector(".chat-empty .btn-primary");
  }, 8000);
  createButton.click();

  const textarea = await waitFor("composer actif", function () {
    const el = document.querySelector(".composer textarea");
    return el && !el.disabled ? el : null;
  }, 8000);
  const setValue = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
  setValue.call(textarea, "Le voyant du Bastion et le journal de rotation");
  textarea.dispatchEvent(new Event("input", { bubbles: true }));

  const sendButton = await waitFor("bouton envoyer actif", function () {
    const el = document.querySelector('button[title="Envoyer"]');
    return el && !el.disabled ? el : null;
  }, 8000);
  sendButton.click();

  // Fin du flux : la bulle de streaming disparaît, le message final (rechargé
  // depuis le stub) est affiché.
  const finalBubble = await waitFor("fin du flux et message final", function () {
    if (document.querySelector(".message-streaming")) return null;
    return document.querySelector(".message.message-assistant:not(.message-streaming)");
  }, 8000);

  record("message assistant final affiché après la fin du flux", Boolean(finalBubble));
  record("flux SSE servi exactement une fois", chatServed === 1, "appels chat : " + chatServed);

  const note = finalBubble ? finalBubble.querySelector(".web-note") : null;
  const noteText = note ? String(note.textContent || "") : "";
  record(
    "statut web conservé après la fin du flux (meta+sources+done regroupés en un chunk)",
    noteText.indexOf("Web constructeur public — version non vérifiée") >= 0,
    note ? "" : "note web absente du message final",
  );
  record(
    "requête publique affichée avec le statut conservé",
    noteText.indexOf("site:wallix.com voyant") >= 0,
  );
  record("aucune erreur JS pendant le scénario", errors.length === 0, errors.join(" | "));
}

runScenario()
  .then(function () {
    setResult({ state: "finished", checks: checks, chatCalls: chatServed });
  })
  .catch(function (error) {
    record("scénario terminé sans exception", false, String((error && error.stack) || error));
    setResult({ state: "error", checks: checks, error: String((error && error.stack) || error) });
  });
`;

function findChromium() {
  const roots = [];
  if (process.env.PLAYWRIGHT_BROWSERS_PATH) roots.push(process.env.PLAYWRIGHT_BROWSERS_PATH);
  roots.push(join(homedir(), ".cache", "ms-playwright"));
  roots.push("/home/hermes/.cache/ms-playwright");
  roots.push("/home/tetrax/.cache/ms-playwright");
  roots.push("/root/.cache/ms-playwright");
  const found = [];
  for (const root of roots) {
    if (!root) continue;
    let names = [];
    try {
      names = readdirSync(root);
    } catch (error) {
      continue;
    }
    for (const name of names) {
      const version = Number(String(name).split("-").pop());
      const versioned = Number.isFinite(version) ? version : 0;
      if (String(name).indexOf("chromium_headless_shell-") === 0) {
        for (const rel of ["chrome-headless-shell-linux64/chrome-headless-shell", "chrome-linux/headless_shell"]) {
          const candidate = join(root, name, rel);
          if (existsSync(candidate)) found.push({ path: candidate, version: versioned, kind: "headless_shell" });
        }
      } else if (String(name).indexOf("chromium-") === 0) {
        for (const rel of ["chrome-linux64/chrome", "chrome-linux/chrome"]) {
          const candidate = join(root, name, rel);
          if (existsSync(candidate)) found.push({ path: candidate, version: versioned, kind: "chrome" });
        }
      }
    }
  }
  // headless_shell d'abord (support natif de --dump-dom), puis chrome ; à
  // l'intérieur d'un type, la version la plus récente d'abord.
  found.sort((a, b) => (a.kind === b.kind ? b.version - a.version : a.kind === "headless_shell" ? -1 : 1));
  return found;
}

function runBrowser(entry, pageUrl) {
  const flags = [
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--hide-scrollbars",
    "--dump-dom",
    "--virtual-time-budget=20000",
    "--window-size=1440,900",
  ];
  if (entry.kind === "chrome") flags.unshift("--headless=new");
  return execFileSync(entry.path, flags.concat([pageUrl]), {
    encoding: "utf8",
    timeout: 90000,
    maxBuffer: 64 * 1024 * 1024,
    stdio: ["ignore", "pipe", "pipe"],
  });
}

async function main() {
  const esbuild = (await import("../../frontend/node_modules/esbuild/lib/main.js")).default;
  const workDir = mkdtempSync(join(tmpdir(), "wallia-web-stream-repro-"));
  const bundlePath = join(workDir, "bundle.js");
  const pagePath = join(workDir, "page.html");
  esbuild.buildSync({
    stdin: {
      contents: ENTRY,
      resolveDir: join(FRONTEND, "src"),
      sourcefile: "wallia-web-stream-entry.tsx",
      loader: "tsx",
    },
    bundle: true,
    format: "iife",
    platform: "browser",
    jsx: "automatic",
    logLevel: "silent",
    outfile: bundlePath,
  });
  const NL = String.fromCharCode(10);
  const pageHtml = [
    "<!doctype html>",
    '<html lang="fr">',
    '<head><meta charset="utf-8"><title>Wallia — repro flux web</title></head>',
    "<body>",
    '<div id="root"></div>',
    '<pre id="' + RESULT_ID + '">{"state":"running"}</pre>',
    '<script src="./bundle.js"></script>',
    "</body>",
    "</html>",
    "",
  ].join(NL);
  writeFileSync(pagePath, pageHtml, "utf8");

  const browsers = findChromium();
  if (browsers.length === 0) {
    console.error(
      "[ERR] navigateur Playwright introuvable (chromium/chrome-headless-shell) — repro navigateur NON exécutée.",
    );
    console.log("[wallia] répertoire de repro conservé : " + workDir);
    process.exitCode = 1;
    return;
  }

  let dom = null;
  let used = null;
  let lastError = null;
  for (const entry of browsers) {
    try {
      dom = runBrowser(entry, pathToFileURL(pagePath).href);
      used = entry;
      break;
    } catch (error) {
      lastError = error;
    }
  }
  if (dom === null) {
    console.error(
      "[ERR] aucun navigateur n'a produit de rendu : " +
        String((lastError && (lastError.stderr || lastError.message)) || lastError),
    );
    console.log("[wallia] répertoire de repro conservé : " + workDir);
    process.exitCode = 1;
    return;
  }

  const match = String(dom).match(/__WALLIA_REPRO__([A-Za-z0-9+/=]+)__END__/);
  if (!match) {
    console.error(
      "[ERR] marqueur de résultat absent du DOM — scénario non exécuté ou dump trop précoce (page : " + pagePath + ").",
    );
    console.log("[wallia] binaire utilisé : " + used.path);
    console.log("[wallia] répertoire de repro conservé : " + workDir);
    process.exitCode = 1;
    return;
  }

  const result = JSON.parse(Buffer.from(match[1], "base64").toString("utf8"));
  let failures = 0;
  for (const check of result.checks || []) {
    if (check.pass) {
      console.log("[OK]  " + check.name);
    } else {
      failures += 1;
      console.log("[ERR] " + check.name + (check.detail ? " : " + check.detail : ""));
    }
  }
  if (result.state !== "finished") {
    failures += 1;
    console.log(
      "[ERR] scénario non terminé (state=" + result.state + ")" + (result.error ? " : " + result.error : ""),
    );
  }
  console.log("[wallia] binaire utilisé : " + used.path + " (" + used.kind + ")");
  console.log("[wallia] répertoire de repro conservé : " + workDir);
  if (failures > 0) {
    console.log(failures + " cas en échec");
    process.exitCode = 1;
  } else {
    console.log("[wallia] repro flux web : tous les cas passent.");
  }
}

await main().catch((error) => {
  console.error("[ERR] exécution impossible : " + String((error && error.stack) || error));
  process.exitCode = 1;
});

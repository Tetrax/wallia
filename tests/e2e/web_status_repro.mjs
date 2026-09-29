// Contrôle local (run209-web, points 3 et 6) : le frontend affiche honnêtement
// le statut du repli web (ok / no_results / unavailable, rien d'inventé) et
// n'expose jamais de lien web non conforme aux gardes syntaxiques (parité
// complète avec le backend : autorité brute, labels DNS, host borné).
//
//     cd tests/e2e && node web_status_repro.mjs
//
// SANS nouvelle dépendance et SANS installation : le bundle est construit à la
// volée avec l'esbuild DÉJÀ présent dans frontend/node_modules, puis rendu en
// SSR avec react-dom/server (déjà présent). Aucun réseau, aucun navigateur.
// Le bundle ESM embarque react-dom/server en CJS : une passerelle
// createRequire est ajoutée en banner esbuild (solution standard) pour que
// les require internes (« stream », « util ») fonctionnent. Le répertoire et
// le bundle produits sont CONSERVÉS sous TMPDIR (preuve relisible).
//
// NON EXÉCUTÉ à l'écriture du lot : le principal lance ce script après revue.
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND = resolve(HERE, "../../frontend");

// Point d'entrée du bundle : on ne teste QUE des fonctions/composants réels du
// frontend (aucune copie de la logique).
const ENTRY = `
export { isAllowedWebUrl } from "./api";
export { ChatView } from "./components/Chat";
export { SourcesPanel } from "./components/Panels";
export { createElement } from "react";
export { renderToStaticMarkup } from "react-dom/server";
`;

const WEB_URL = "https://www.wallix.com/fr/produits/wallix-bastion";

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
  url: WEB_URL,
  domain: "www.wallix.com",
  version_state: "non_verifiee",
};

const ASSISTANT_MESSAGE = {
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
  created_at: "2026-09-29T10:00:00+00:00",
  updated_at: "2026-09-29T10:00:00+00:00",
};

let failures = 0;
function check(name, condition, detail = "") {
  if (condition) {
    console.log(`[OK]  ${name}`);
  } else {
    failures += 1;
    console.log(`[ERR] ${name}${detail ? `: ${detail}` : ""}`);
  }
}

async function main() {
  const esbuild = (await import("../../frontend/node_modules/esbuild/lib/main.js")).default;
  const workDir = mkdtempSync(join(tmpdir(), "wallia-web-repro-"));
  const outfile = join(workDir, "bundle.mjs");
  try {
    esbuild.buildSync({
      stdin: {
        contents: ENTRY,
        resolveDir: join(FRONTEND, "src"),
        sourcefile: "wallia-web-repro-entry.tsx",
        loader: "tsx",
      },
      bundle: true,
      format: "esm",
      platform: "node",
      jsx: "automatic",
      logLevel: "silent",
      outfile,
      // Passerelle standard : react-dom/server est embarqué en CJS et ses
      // require internes (« stream », « util ») échouent en ESM sans un
      // `require` réel — createRequire le fournit, sans nouvelle dépendance.
      banner: {
        js: 'import { createRequire as __walliaCreateRequire } from "node:module"; const require = __walliaCreateRequire(import.meta.url);',
      },
    });
    const mod = await import(pathToFileURL(outfile).href);
    const { createElement: h, renderToStaticMarkup, ChatView, SourcesPanel, isAllowedWebUrl } = mod;

    // --- gardes syntaxiques des liens web (mêmes règles que le serveur) ------
    for (const url of [
      "https://wallix.com/fr/",
      "https://www.wallix.com/fr/produits/",
      "https://wallix.com:443/fr/",
      "https://WALLIX.com/fr/",
      "https://fr.support.wallix.com/faq?lang=fr",
    ]) {
      check(`url autorisée : ${url}`, isAllowedWebUrl(url) === true);
    }
    for (const url of [
      "http://wallix.com/",
      "https://user:secret@wallix.com/",
      "https://@wallix.com/",  // userinfo vide : refusé (autorité brute)
      "https://wallix.com:8443/",
      "https://wallix.com/fr/#ancre",
      "https://***@evil.invalid/",
      "https://wallix.com\\@evil.invalid/",  // backslash déceptif
      "https://evilwallix.com/",
      "https://wallix.com.evil.invalid/",
      "https://wallix.com./fr/",
      "https://wallix..com/",  // label vide
      "https://wallix.cóm/",
      "https://wallix。com/",  // point idéographique : jamais normalisé
      "https://wallix%2Ecom/",  // percent-encoding du host
      "https://wall ix.com/",  // espace brut dans l'autorité
      "https://wallix_com.fr/",
      "https://-wallix.com/",  // tiret en début de label
      "https://wallix-.com/",  // tiret en fin de label
      `https://${"a".repeat(64)}.wallix.com/`,  // label > 63
      `https://${`${"a".repeat(60)}.`.repeat(4)}wallix.com/`,  // host > 253
      "javascript:alert(1)",
      "data:text/html;base64,PHNjcmlwdD4=",
    ]) {
      check(`url refusée : ${url}`, isAllowedWebUrl(url) === false);
    }

    const conversation = {
      id: "conv-1",
      title: "Cas web",
      case_state: {},
      created_at: null,
      updated_at: null,
    };
    const baseProps = {
      conversation,
      messages: [ASSISTANT_MESSAGE],
      attachments: [],
      streaming: null,
      messageWeb: {},
      providerConfigured: true,
      onSend: () => {},
      onStop: () => {},
      onRetry: () => {},
      onOpenSources: () => {},
      onOpenCase: () => {},
      onUpload: async () => null,
      onDeleteAttachment: () => {},
      onCreateConversation: () => {},
      onOpenSidebar: () => {},
    };
    const renderChat = (overrides) =>
      renderToStaticMarkup(h(ChatView, { ...baseProps, ...overrides }));

    // --- aucun statut inventé quand la métadonnée web est absente ------------
    const plain = renderChat({});
    check("provenance web visible sans métadonnée (chip)", plain.includes("web public — version non vérifiée"));
    check("aucun statut opérationnel inventé sans métadonnée", !plain.includes("Recherche web"));
    check("aucune note 'constructeur' inventée", !plain.includes("Web constructeur public"));

    // --- statut ok ------------------------------------------------------------
    const ok = renderChat({
      messageWeb: { "msg-1": { status: "ok", reason: null, query: "site:wallix.com voyant" } },
    });
    check("statut ok affiché", ok.includes("Web constructeur public — version non vérifiée"));
    check("statut ok : requête publique visible", ok.includes("requête publique : site:wallix.com voyant"));
    check("statut ok : résultats non qualifiés", ok.includes("Résultats non qualifiés"));

    // --- statut no_results ----------------------------------------------------
    const noResults = renderChat({
      messageWeb: { "msg-1": { status: "no_results", reason: null, query: "site:wallix.com rotation" } },
    });
    check("statut no_results explicite", noResults.includes("Recherche web publique : aucun résultat"));
    check("statut no_results : requête publique visible", noResults.includes("site:wallix.com rotation"));

    // --- statut unavailable ---------------------------------------------------
    const unavailable = renderChat({
      messageWeb: {
        "msg-1": { status: "unavailable", reason: "service HTTP 500", query: "site:wallix.com voyant" },
      },
    });
    check(
      "statut unavailable explicite",
      unavailable.includes("Recherche web complémentaire indisponible"),
    );
    check("statut unavailable : raison technique sûre", unavailable.includes("service HTTP 500"));
    check(
      "statut unavailable : jamais présenté actif",
      !unavailable.includes("Web constructeur public — version non vérifiée"),
    );

    // --- statut not_needed / disabled : rien au message (état connecteur) -----
    for (const status of ["not_needed", "disabled"]) {
      const quiet = renderChat({
        messageWeb: { "msg-1": { status, reason: null, query: null } },
      });
      check(`statut ${status} silencieux au message`, !quiet.includes("Recherche web"));
    }

    // --- flux en cours : statut observé affiché sans spinner fictionnel -------
    const streaming = renderChat({
      messages: [],
      streaming: {
        messageId: "msg-2",
        content: "amorce",
        sources: [WEB_SOURCE],
        sourcesStatus: "empty_corpus",
        web: { status: "ok", reason: null, query: "site:wallix.com voyant" },
        statusLabel: "Rédaction de la réponse",
        demo: false,
        error: null,
      },
    });
    check(
      "flux : statut web affiché pendant le streaming",
      streaming.includes("Web constructeur public — version non vérifiée"),
    );

    // --- panneau des sources : liens et provenance ----------------------------
    const renderPanel = (sources) =>
      renderToStaticMarkup(
        h(SourcesPanel, { open: true, sources, status: null, highlighted: null, onClose: () => {} }),
      );

    const panelValid = renderPanel([WEB_SOURCE]);
    check("panneau : provenance constructeur affichée", panelValid.includes("Web constructeur public"));
    check("panneau : version non vérifiée affichée", panelValid.includes("version non vérifiée"));
    check(
      "panneau : lien public ouvrable avec noopener+noreferrer",
      panelValid.includes(`href="${WEB_URL}"`) &&
        panelValid.includes('target="_blank"') &&
        panelValid.includes('rel="noopener noreferrer"'),
    );

    const deceptive = { ...WEB_SOURCE, url: "http://evil.invalid/redirection", domain: "evil.invalid" };
    const panelDeceptive = renderPanel([deceptive]);
    check(
      "panneau : URL non conforme jamais rendue ouvrable",
      !panelDeceptive.includes('href="http://evil.invalid/redirection"') &&
        panelDeceptive.includes("URL publique non conforme"),
    );

    const userinfo = { ...WEB_SOURCE, url: "https://user:pw@www.wallix.com/fr/", domain: "www.wallix.com" };
    const panelUserinfo = renderPanel([userinfo]);
    check(
      "panneau : userinfo jamais rendu ouvrable",
      !panelUserinfo.includes('href="https://user:pw@www.wallix.com/fr/"'),
    );

    // Garde anti-régression du helper lui-même : une chaîne vide/absente ne
    // passe jamais, et un caractère de contrôle est refusé.
    check("url absente refusée", isAllowedWebUrl(null) === false && isAllowedWebUrl("") === false);
    check(
      "caractère de contrôle refusé",
      isAllowedWebUrl(`https://wallix.com/fr/${String.fromCharCode(1)}`) === false,
    );
  } finally {
    // Répertoire et bundle CONSERVÉS sous TMPDIR : preuve relisible du rendu
    // (aucun cleanup récursif).
    console.log(`[wallia] répertoire de repro conservé : ${workDir}`);
  }
}

await main().catch((error) => {
  console.error(`[ERR] exécution impossible : ${error?.stack ?? error}`);
  process.exitCode = 1;
});

if (failures > 0) {
  console.error(`${failures} cas en échec`);
  process.exitCode = 1;
}

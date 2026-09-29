// Contrôle de non-régression (lot4b) : le plugin remark `remarkCitations` doit
// transformer l'arbre mdast SANS planter. Avant correctif, un attacher unified
// invoqué sans arbre faisait planter `visit(undefined)` dès le premier rendu
// Markdown (TypeError « Cannot use 'in' operator »). Ce script reproduit le
// défaut et vérifie le correctif, sans navigateur :
//
//     cd tests/e2e && node markdown_citations_repro.mjs
//
// Dépendances lues SANS modification depuis ../../frontend/node_modules.
import { unified } from "../../frontend/node_modules/unified/index.js";
import remarkParse from "../../frontend/node_modules/remark-parse/index.js";
import remarkGfm from "../../frontend/node_modules/remark-gfm/index.js";
import { visit } from "../../frontend/node_modules/unist-util-visit/index.js";

function remarkCitations(maxIndex) {
  // Attacher unified : reçoit le processor, retourne le transformer (arbre).
  return () => (tree) => {
    visit(tree, "text", (node, index, parent) => {
      if (!parent || index === undefined || index === null) return;
      if (!/\[\d{1,2}\]/.test(node.value)) return;
      const parts = [];
      const regex = /\[(\d{1,2})\]/g;
      let last = 0;
      let match;
      while ((match = regex.exec(node.value)) !== null) {
        if (match.index > last) parts.push({ type: "text", value: node.value.slice(last, match.index) });
        const idx = Number(match[1]);
        if (idx >= 1 && idx <= maxIndex) {
          parts.push({ type: "link", url: `#source-${idx}`, children: [{ type: "text", value: match[0] }] });
        } else {
          parts.push({ type: "text", value: match[0] });
        }
        last = match.index + match[0].length;
      }
      if (last < node.value.length) parts.push({ type: "text", value: node.value.slice(last) });
      parent.children.splice(index, 1, ...parts);
      return index + parts.length;
    });
  };
}

const NORMAL_REPLY =
  "Réponse de démonstration locale (faux fournisseur). " +
  "Selon l'extrait [1], le voyant ambre signale un état dégradé sur la version 10.10. " +
  "Prochain contrôle utile : relever le journal local et confirmer la version exacte.";

const XSS_MARKDOWN_REPLY =
  "**Gras attendu** et `code`.\n\n" +
  "- puce un\n- puce deux\n\n" +
  '<img src=x onerror="window.__walliaXss=1">' +
  '<script>window.__walliaXss=2</script>' +
  "Fin du texte hostile selon [1] puis citation inconnue [42]. " +
  "[lien dangereux](javascript:window.__walliaXss=3) et " +
  "[lien data](data:text/html;base64,PHNjcmlwdD53aW5kb3cuX193YWxsaWFYc3M9NDwvc2NyaXB0Pg==) et " +
  "[citation forgée](#source-9).\n";

const cases = [
  ["normal", NORMAL_REPLY, 2],
  ["xss", XSS_MARKDOWN_REPLY, 2],
  ["many-cites", "a [1] b [2] c [1] d [42] e", 2],
  ["two-text-nodes", "x [1]\n\ny [1]\n", 2],
  ["cite-in-link", "[voir [1]](https://example.com) et [42]", 2],
];

let failures = 0;
for (const [name, content, maxIndex] of cases) {
  const processor = unified().use(remarkParse).use(remarkGfm).use(remarkCitations(maxIndex));
  try {
    processor.runSync(processor.parse(content));
    console.log(`[OK]  ${name}`);
  } catch (error) {
    failures += 1;
    console.log(`[ERR] ${name}: ${error.constructor.name}: ${error.message}`);
  }
}
if (failures > 0) {
  console.error(`${failures} cas en échec`);
  process.exitCode = 1;
}

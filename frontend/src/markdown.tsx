import { createContext, useContext, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { visit } from "unist-util-visit";

/**
 * Plugin remark : transforme les marqueurs [n] en liens cliquables `#source-n`
 * uniquement pour les indices de sources réellement fournis par le serveur.
 * Les indices inconnus restent du texte (aucune citation fabriquée à l'affichage).
 *
 * ATTENTION (défaut corrigé lot4b) : unified appelle le plugin comme
 * « attacher » au moment du `freeze` SANS argument ; c'est la fonction
 * RETOURNÉE par l'attacher qui reçoit l'arbre mdast. La version précédente
 * passait directement la fonction-attendue-arbre, donc `visit(undefined...)`
 * plantait (TypeError « Cannot use 'in' operator ») dès le premier rendu
 * Markdown — rendu invisible sans exécution navigateur réelle.
 */
function remarkCitations(maxIndex: number) {
  return () => (tree: unknown) => {
    visit(tree as never, "text", (node: { value: string }, index: number | undefined, parent: { children: unknown[] } | undefined) => {
      if (!parent || index === undefined || index === null) return;
      if (!/\[\d{1,2}\]/.test(node.value)) return;
      const parts: unknown[] = [];
      const regex = /\[(\d{1,2})\]/g;
      let last = 0;
      let match: RegExpExecArray | null;
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

function CodeBlock({ children }: { children?: React.ReactNode }) {
  const ref = useRef<HTMLPreElement>(null);
  const [copied, setCopied] = useState(false);
  return (
    <div className="codeblock">
      <button
        type="button"
        className="codeblock-copy"
        onClick={async () => {
          const text = ref.current?.innerText ?? "";
          try {
            await navigator.clipboard.writeText(text);
            setCopied(true);
            setTimeout(() => setCopied(false), 1400);
          } catch {
            /* presse-papiers indisponible */
          }
        }}
      >
        {copied ? "Copié" : "Copier"}
      </button>
      <pre ref={ref}>{children}</pre>
    </div>
  );
}

/* eslint-disable @typescript-eslint/no-explicit-any -- composants react-markdown aux props internes non typées publiquement */
export const CitationContext = createContext<((index: number) => void) | null>(null);
const SourcesCountContext = createContext(0);

function MarkdownAnchor({ href, children }: any) {
  const handler = useContext(CitationContext);
  const sourcesCount = useContext(SourcesCountContext);
  if (typeof href === "string" && href.startsWith("#source-")) {
    const index = Number(href.slice("#source-".length));
    // Un lien `#source-N` forgé (écrit par le modèle ou par un contenu tiers)
    // n'est jamais accepté : seuls les indices réellement fournis par le
    // serveur pour CE message sont cliquables.
    const valid = Number.isInteger(index) && index >= 1 && index <= sourcesCount;
    if (!valid) {
      return (
        <span className="cite cite-invalid" title="Citation invalide : aucune source correspondante dans ce message">
          {children}
        </span>
      );
    }
    return (
      <button
        type="button"
        className="cite"
        data-citation={index}
        onClick={() => handler?.(index)}
        title="Voir la source"
      >
        {children}
      </button>
    );
  }
  return (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  );
}

const MarkdownComponents = {
  pre: ({ children }: any) => <CodeBlock>{children}</CodeBlock>,
  a: MarkdownAnchor,
};

export function Markdown({
  content,
  sourcesCount,
  onCitation,
}: {
  content: string;
  sourcesCount: number;
  onCitation?: (index: number) => void;
}) {
  const plugin = useMemo(() => remarkCitations(sourcesCount), [sourcesCount]);
  return (
    <CitationContext.Provider value={onCitation ?? null}>
      <SourcesCountContext.Provider value={sourcesCount}>
        <div className="markdown">
          <ReactMarkdown remarkPlugins={[remarkGfm, plugin]} components={MarkdownComponents as any}>
            {content}
          </ReactMarkdown>
        </div>
      </SourcesCountContext.Provider>
    </CitationContext.Provider>
  );
}

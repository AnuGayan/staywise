import type { ReactNode } from "react";

/**
 * Minimal, safe markdown for assistant replies: paragraphs, line breaks,
 * bullet/numbered lists, headings, **bold**, *italic* and `code`.
 * Produces React nodes only (no HTML injection). Keys are source offsets,
 * which are stable and unique for an immutable string.
 */

type Block =
  | { kind: "p"; offset: number; lines: Line[] }
  | { kind: "ul" | "ol"; offset: number; items: Line[] }
  | { kind: "h"; offset: number; line: Line };

interface Line {
  offset: number;
  text: string;
}

const BULLET = /^\s*[-*•]\s+(.*)$/;
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;
const HEADING = /^\s*#{1,6}\s+(.*)$/;
const INLINE = /(\*\*[^*]+\*\*|__[^_]+__|`[^`]+`|\*[^*\s][^*]*\*)/g;

function toLines(text: string): Line[] {
  const lines: Line[] = [];
  let offset = 0;
  for (const raw of text.replace(/\r\n?/g, "\n").split("\n")) {
    lines.push({ offset, text: raw });
    offset += raw.length + 1;
  }
  return lines;
}

function parseBlocks(text: string): Block[] {
  const blocks: Block[] = [];
  let current: Block | null = null;

  // Read through a function so TS does not over-narrow `current` across flush().
  const peek = (): Block | null => current;
  const flush = (): void => {
    if (current) blocks.push(current);
    current = null;
  };

  for (const line of toLines(text)) {
    const bullet = BULLET.exec(line.text);
    const numbered = bullet ? null : NUMBERED.exec(line.text);
    const heading = HEADING.exec(line.text);
    const listKind = bullet ? "ul" : numbered ? "ol" : null;
    const listText = (bullet ?? numbered)?.[1] ?? "";

    if (line.text.trim().length === 0) {
      flush();
    } else if (heading) {
      flush();
      blocks.push({
        kind: "h",
        offset: line.offset,
        line: { offset: line.offset, text: heading[1] ?? "" },
      });
    } else if (listKind) {
      const open = peek();
      if (open?.kind === listKind) {
        open.items.push({ offset: line.offset, text: listText });
      } else {
        flush();
        current = {
          kind: listKind,
          offset: line.offset,
          items: [{ offset: line.offset, text: listText }],
        };
      }
    } else {
      const open = peek();
      if (open?.kind === "p") {
        open.lines.push(line);
      } else {
        flush();
        current = { kind: "p", offset: line.offset, lines: [line] };
      }
    }
  }
  flush();
  return blocks;
}

function renderInline(line: Line): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  for (const match of line.text.matchAll(INLINE)) {
    const token = match[0];
    const start = match.index;
    if (start > last) nodes.push(line.text.slice(last, start));
    const key = `i${line.offset + start}`;
    if (token.startsWith("**") || token.startsWith("__")) {
      nodes.push(<strong key={key}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith("`")) {
      nodes.push(<code key={key}>{token.slice(1, -1)}</code>);
    } else {
      nodes.push(<em key={key}>{token.slice(1, -1)}</em>);
    }
    last = start + token.length;
  }
  if (last < line.text.length) nodes.push(line.text.slice(last));
  return nodes;
}

function renderBlock(block: Block): ReactNode {
  const key = `b${block.offset}`;
  switch (block.kind) {
    case "h":
      return (
        <p key={key} className="md-heading">
          <strong>{renderInline(block.line)}</strong>
        </p>
      );
    case "ul":
    case "ol": {
      const items = block.items.map((item) => (
        <li key={`l${item.offset}`}>{renderInline(item)}</li>
      ));
      return block.kind === "ul" ? <ul key={key}>{items}</ul> : <ol key={key}>{items}</ol>;
    }
    case "p":
      return (
        <p key={key}>
          {block.lines.flatMap((line, i) =>
            i === 0 ? renderInline(line) : [<br key={`br${line.offset}`} />, ...renderInline(line)],
          )}
        </p>
      );
  }
}

export function Markdown({ text }: { text: string }): ReactNode {
  return <div className="md">{parseBlocks(text).map(renderBlock)}</div>;
}

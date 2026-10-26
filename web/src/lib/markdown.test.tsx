import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Markdown } from "./markdown";

describe("Markdown", () => {
  it("renders bold, italic, code, lists and line breaks without injecting HTML", () => {
    const text = [
      "Here are **2 rooms** for *you*:",
      "line two with `code`",
      "",
      "- Lighthouse Deluxe",
      "- Ocean Suite",
      "",
      "1. first",
      "2. second",
      "<img src=x onerror=alert(1)>",
    ].join("\n");
    const { container } = render(<Markdown text={text} />);
    expect(container.querySelector("strong")?.textContent).toBe("2 rooms");
    expect(container.querySelector("em")?.textContent).toBe("you");
    expect(container.querySelector("code")?.textContent).toBe("code");
    expect(container.querySelectorAll("p br")).toHaveLength(1);
    expect(Array.from(container.querySelectorAll("ul li")).map((li) => li.textContent)).toEqual([
      "Lighthouse Deluxe",
      "Ocean Suite",
    ]);
    expect(container.querySelectorAll("ol li")).toHaveLength(2);
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("<img src=x onerror=alert(1)>");
  });

  it("renders headings as emphasised paragraphs", () => {
    const { container } = render(<Markdown text={"## Summary\nAll set."} />);
    expect(container.querySelector(".md-heading strong")?.textContent).toBe("Summary");
  });
});

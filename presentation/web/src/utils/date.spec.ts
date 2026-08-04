import { describe, expect, it } from "vitest";
import { toLocalDateStr } from "./date";

describe("toLocalDateStr", () => {
  it("uses local timezone, not UTC", () => {
    // 北京 2026-08-04 00:30 = UTC 2026-08-03 16:30
    // toISOString().slice(0,10) 会得到 "2026-08-03"（UTC 昨日），
    // 本地时区实现必须得到 "2026-08-04"（北京当日）。
    const d = new Date("2026-08-03T16:30:00Z");
    expect(toLocalDateStr(d)).toBe("2026-08-04");
  });
});

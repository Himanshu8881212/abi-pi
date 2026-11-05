import { expect, test } from "vitest";
import { chartOption } from "./charts";

const vd = (dims: string[], rows: unknown[][]) => ({
  type: "bar", columns: [...dims, "m.amount"], measures: { "m.amount": "Amount" }, format: {}, rows,
}) as any;

test("a bar with a second dimension stacks its members, as the report does", () => {
  const o = chartOption(vd(["o.week", "o.region"], [[1, "North", 5], [1, "South", 3], [2, "North", 4]]))!;
  expect(o.series.map((s: any) => [s.name, s.stack])).toEqual([["North", "members"], ["South", "members"]]);
});

test("measures side by side and a month-year axis don't stack", () => {
  expect(chartOption(vd(["o.region"], [["North", 5], ["South", 3]]))!.series.every((s: any) => !s.stack)).toBe(true);
  const per = chartOption(vd(["o.month", "o.year"], [[1, 2025, 5], [2, 2025, 3]]))!;
  expect(per.series.every((s: any) => !s.stack)).toBe(true);
});

import test from "node:test";
import assert from "node:assert/strict";
import { periodFromFields, fieldsForPeriod, searchHorizon } from "../src/orderflow/static/time.mjs";

test("Выбранное предложение сохраняет микросекунды при отправке формы", () => {
  const exact = { start_at: "2030-01-02T11:00:00.000003Z", end_at: "2030-01-02T12:00:00.000004Z" };
  const display = fieldsForPeriod(exact);
  assert.equal(display.start, "2030-01-02T11:00:00.000");
  assert.deepEqual(periodFromFields(display.start, display.end, { ...exact, display }), exact);
});

test("Ручное изменение даты отменяет точный интервал предложения", () => {
  const exact = { start_at: "2030-01-02T11:00:00.000003Z", end_at: "2030-01-02T12:00:00.000004Z" };
  const display = fieldsForPeriod(exact);
  assert.deepEqual(periodFromFields("2030-01-03T11:00", display.end, { ...exact, display }), {
    start_at: "2030-01-03T11:00:00.000Z",
    end_at: "2030-01-02T12:00:00.000Z",
  });
});

test("Горизонт сохраняет дробную часть и не выходит за год 9999", () => {
  assert.equal(searchHorizon("2030-01-02T11:00:00.000003Z"), "2030-02-01T11:00:00.000003Z");
  assert.equal(searchHorizon("9999-12-30T11:00:00.000003Z"), "9999-12-31T23:59:59.999999Z");
});

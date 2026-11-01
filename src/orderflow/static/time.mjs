export function fieldsForPeriod(period) {
  return {
    start: new Date(period.start_at).toISOString().slice(0, 23),
    end: new Date(period.end_at).toISOString().slice(0, 23),
  };
}

export function periodFromFields(start, end, exact = null) {
  // Date хранит только миллисекунды. Исходные ISO сохраняем до ручного изменения полей.
  if (exact && start === exact.display.start && end === exact.display.end) {
    return { start_at: exact.start_at, end_at: exact.end_at };
  }
  return {
    start_at: new Date(start + "Z").toISOString(),
    end_at: new Date(end + "Z").toISOString(),
  };
}

export function searchHorizon(start) {
  const horizon = new Date(start);
  horizon.setUTCDate(horizon.getUTCDate() + 30);
  if (horizon.getUTCFullYear() > 9999) return "9999-12-31T23:59:59.999999Z";
  const fraction = start.match(/\.\d+Z$/)?.[0] || "Z";
  return horizon.toISOString().replace(/\.\d{3}Z$/, fraction);
}

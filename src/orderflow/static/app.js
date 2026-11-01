import { fieldsForPeriod, periodFromFields, searchHorizon } from "./time.mjs";
const $ = (selector, parent = document) => parent.querySelector(selector);
const $$ = (selector, parent = document) => [...parent.querySelectorAll(selector)];
const state = {
  key: "",
  role: "viewer",
  products: [],
  bookings: [],
  status: "",
  view: "bookings",
  offset: 0,
  audit: [],
  attempt: null,
  listRequest: 0,
  alternativesRequest: 0,
  exactPeriod: null,
};
const labels = {
  hold: "Резерв",
  confirmed: "Подтверждено",
  checked_out: "Выдано",
  returned: "Возвращено",
  cancelled: "Отменено",
  expired: "Истёк резерв",
  overdue: "Просрочен возврат",
};
const roles = { admin: "Администратор", operator: "Оператор", viewer: "Наблюдатель" };
const actionLabels = {
  "booking.create": "Создан временный резерв",
  "booking.confirm": "Бронирование подтверждено",
  "booking.check_out": "Оборудование выдано",
  "booking.return": "Оборудование возвращено",
  "booking.cancel": "Бронирование отменено",
  "booking.expire": "Резерв истёк",
  "booking.reschedule": "Бронирование перенесено",
  "product.create": "Добавлено оборудование",
  "product.update": "Обновлено оборудование",
};
const money = (value) =>
  new Intl.NumberFormat("ru-RU", {
    style: "currency",
    currency: "RUB",
    maximumFractionDigits: value % 100 ? 2 : 0,
  }).format(value / 100);
const date = (value) =>
  new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
  }).format(new Date(value));
const shortDate = (value) =>
  new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
  }).format(new Date(value));
const localInput = (value) => new Date(value).toISOString().slice(0, 16);
const toUTC = (value) => new Date(value + "Z").toISOString();
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function clearError(id) {
  $(id).hidden = true;
  $(id).replaceChildren();
}
function showError(id, error) {
  $(id).textContent = error.message || "Операция не выполнена. Попробуйте ещё раз.";
  $(id).hidden = false;
}
function toast(message) {
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => {
    $("#toast").hidden = true;
  }, 4500);
}
async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(`/api${path}`, {
      ...options,
      headers: { "Content-Type": "application/json", "X-API-Key": state.key, ...options.headers },
    });
  } catch {
    throw new Error("Нет связи с сервером. Проверьте соединение и повторите запрос.");
  }
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error?.message || `Ошибка сервера (${response.status})`);
    error.code = data.error?.code;
    error.details = data.error?.details;
    throw error;
  }
  return data;
}
async function busy(button, task) {
  button.disabled = true;
  try {
    await task();
  } finally {
    button.disabled = false;
  }
}
function badge(booking) {
  const status = booking.overdue ? "overdue" : booking.status;
  return el("span", `status ${status}`, labels[status] || status);
}
function openDialog(id) {
  if (!$(id).open) $(id).showModal();
}
$$("[data-close]").forEach((button) =>
  button.addEventListener("click", () => document.getElementById(button.dataset.close).close()),
);
$("#identity").addEventListener("click", () => openDialog("#login-dialog"));
$("#mobile-identity").addEventListener("click", () => openDialog("#login-dialog"));
$("#today").textContent = new Intl.DateTimeFormat("ru-RU", {
  day: "numeric",
  month: "long",
  timeZone: "UTC",
}).format(new Date());

async function login(key) {
  state.key = key;
  try {
    const principal = await api("/me");
    state.role = principal.role;
    $("#identity-name").textContent = principal.name;
    $("#identity-role").textContent = roles[principal.role];
    $("#mobile-identity").textContent = `${roles[principal.role]} ↗`;
    $("#new-booking").hidden = state.role === "viewer";
    $("#expire").hidden = state.role !== "admin";
    $("#login-dialog").close();
    $("#api-key").value = "";
    state.attempt = null;
    await refresh();
  } catch (error) {
    state.key = "";
    showError("#login-error", error);
    openDialog("#login-dialog");
  }
}
$("#login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  clearError("#login-error");
  busy($("button[type=submit], .wide", event.target), () => login($("#api-key").value));
});
$$("[data-demo]").forEach((button) =>
  button.addEventListener("click", () => login(`demo-${button.dataset.demo}`)),
);

function metric(label, value, note, symbol, className = "") {
  const card = el("div", `metric ${className}`);
  card.append(
    el("span", "metric-label", label),
    el("span", "metric-number", value),
    el("span", "metric-note", note),
    el("span", "metric-symbol", symbol),
  );
  return card;
}
async function refresh() {
  clearError("#global-error");
  try {
    const [products, bookings] = await Promise.all([api("/products"), api("/bookings?limit=200")]);
    state.products = products;
    state.bookings = bookings;
    const active = bookings.filter((row) =>
      ["hold", "confirmed", "checked_out"].includes(row.status),
    );
    const overdue = bookings.filter((row) => row.overdue);
    $("#nav-count").textContent = active.length;
    $("#metrics").replaceChildren(
      metric("Активные бронирования", active.length, "в последних 200 записях", "↗", "accent"),
      metric(
        "Ожидают подтверждения",
        bookings.filter((row) => row.status === "hold").length,
        "резервы с ограниченным сроком",
        "◷",
      ),
      metric(
        "Оборудование на выезде",
        bookings.filter((row) => row.status === "checked_out").length,
        "бронирований выдано",
        "⇢",
      ),
      metric(
        "Просроченные возвраты",
        overdue.length,
        overdue.length ? "нужны действия оператора" : "все возвраты по плану",
        "!",
        overdue.length ? "alert" : "",
      ),
    );
    $("#overdue-banner").hidden = overdue.length === 0;
    $("#overdue-banner").textContent =
      `⚑ ${overdue.length} ${overdue.length === 1 ? "просроченный возврат" : "просроченных возврата"} в последних 200 записях. Плановая доступность может отличаться от физического остатка. Проверьте оборудование перед выдачей.`;
    renderCatalog();
    await loadView();
  } catch (error) {
    showError("#global-error", error);
  }
}
$("#refresh").addEventListener("click", (event) => busy(event.currentTarget, refresh));
const titles = {
  bookings: [
    "Бронирования",
    "Планы меняются.",
    "Оборудование учтено.",
    "Брони, выдачи и возвраты — в одном рабочем ритме.",
  ],
  availability: [
    "Доступность",
    "Всё на своём месте.",
    "И в своё время.",
    "Посмотрите, где свободен ресурс, а где нужен другой интервал.",
  ],
  inventory: [
    "Оборудование",
    "Каждая единица",
    "работает на событие.",
    "Единый каталог, остатки и ставки проката.",
  ],
  audit: [
    "Журнал действий",
    "У каждого изменения",
    "есть своя история.",
    "Кто, когда и что изменил — без потерянного контекста.",
  ],
};
$$("[data-view]").forEach((button) =>
  button.addEventListener("click", async () => {
    state.view = button.dataset.view;
    $$("[data-view]").forEach((item) => item.classList.toggle("active", item === button));
    $$(".view").forEach((view) => {
      view.hidden = view.id !== `${state.view}-view`;
    });
    const [name, first, second, description] = titles[state.view];
    $("#breadcrumb").textContent = name;
    $("#page-title").replaceChildren(
      document.createTextNode(first),
      el("br"),
      el("span", "", second),
    );
    $("#page-description").textContent = description;
    try {
      await loadView();
    } catch (error) {
      showError("#global-error", error);
    }
  }),
);
async function loadView() {
  if (!state.key) return;
  if (state.view === "bookings") await loadBookings();
  if (state.view === "availability") await loadAvailability();
  if (state.view === "audit") await loadAudit();
}
async function loadBookings() {
  // Медленный ответ старого поиска не должен подменять результат нового фильтра.
  const token = ++state.listRequest;
  const params = new URLSearchParams({ limit: "100", offset: String(state.offset) });
  if (state.status) params.set("status", state.status);
  if ($("#search").value) params.set("search", $("#search").value);
  const rows = await api(`/bookings?${params}`);
  if (token !== state.listRequest) return;
  $("#board-count").textContent = rows.length;
  $("#booking-rows").replaceChildren();
  $("#booking-empty").hidden = rows.length !== 0;
  $("#prev-page").disabled = state.offset === 0;
  $("#next-page").disabled = rows.length < 100;
  $("#list-note").textContent = rows.length
    ? `Записи ${state.offset + 1}–${state.offset + rows.length} · Время UTC`
    : "Нет записей по текущему фильтру";
  for (const booking of rows) {
    const row = el("tr");
    const titleCell = el("td");
    const button = el("button", "booking-title", booking.label);
    button.addEventListener("click", () => inspect(booking.id));
    titleCell.append(button, el("span", "reference", booking.reference));
    const period = el("td", "", date(booking.start_at));
    period.append(el("span", "subline", `→ ${date(booking.end_at)}`));
    const equipment = el(
      "td",
      "",
      `${booking.items.reduce((sum, item) => sum + item.quantity, 0)} ед. / ${booking.items.length} поз.`,
    );
    equipment.append(el("span", "subline", booking.items[0]?.name.split(" · ")[0] || "—"));
    const status = el("td");
    status.append(badge(booking));
    const action = el("td");
    const arrow = el("button", "row-arrow", "↗");
    arrow.setAttribute("aria-label", `Открыть ${booking.reference}`);
    arrow.addEventListener("click", () => inspect(booking.id));
    action.append(arrow);
    row.append(
      titleCell,
      period,
      equipment,
      status,
      el("td", "numeric", money(booking.total_cents)),
      action,
    );
    $("#booking-rows").append(row);
  }
}
$$("[data-status]").forEach((button) =>
  button.addEventListener("click", () => {
    state.status = button.dataset.status;
    state.offset = 0;
    $$("[data-status]").forEach((item) => item.classList.toggle("active", item === button));
    loadBookings().catch((error) => showError("#global-error", error));
  }),
);
$("#search").addEventListener("input", () => {
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(() => {
    state.offset = 0;
    loadBookings().catch((error) => showError("#global-error", error));
  }, 250);
});
$("#prev-page").addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - 100);
  loadBookings().catch((error) => showError("#global-error", error));
});
$("#next-page").addEventListener("click", () => {
  state.offset += 100;
  loadBookings().catch((error) => showError("#global-error", error));
});

function defaultPeriod() {
  const start = new Date(Date.now() + 60 * 60000);
  start.setUTCMinutes(0, 0, 0);
  const end = new Date(start.getTime() + 8 * 3600000);
  return [localInput(start), localInput(end)];
}
const initialPeriod = defaultPeriod();
$("#availability-start").value = initialPeriod[0];
$("#availability-end").value = initialPeriod[1];
$("#availability-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy($("button", event.target), async () => {
    try {
      await loadAvailability();
    } catch (error) {
      showError("#global-error", error);
    }
  });
});
async function loadAvailability() {
  const start = toUTC($("#availability-start").value),
    end = toUTC($("#availability-end").value);
  const rows = await api(`/availability?${new URLSearchParams({ start, end })}`);
  clearError("#global-error");
  $("#timeline").replaceChildren();
  for (const row of rows) {
    const card = el("div", "timeline-row");
    const name = el("div", "timeline-name", row.name);
    name.append(el("small", "", `${row.sku} · парк: ${row.capacity} ед.`));
    const chart = el("div", "timeline-chart");
    const segments = el("div", "segments");
    for (const segment of row.segments) {
      const node = el(
        "button",
        `segment ${segment.reserved >= row.capacity && row.capacity > 0 ? "full" : segment.reserved ? "busy" : ""}`,
        String(segment.reserved),
      );
      const length = new Date(segment.end_at) - new Date(segment.start_at);
      node.style.flexGrow = String(length);
      node.title = `${shortDate(segment.start_at)} → ${shortDate(segment.end_at)} UTC: занято ${segment.reserved} из ${row.capacity}`;
      node.setAttribute("aria-label", node.title);
      node.addEventListener("click", () => {
        toast(
          `${node.title}${segment.booking_ids.length ? ". Брони: " + segment.booking_ids.map((id) => state.bookings.find((booking) => booking.id === id)?.reference || id.slice(0, 8)).join(", ") : ""}`,
        );
      });
      segments.append(node);
    }
    const axis = el("div", "timeline-labels");
    axis.append(el("span", "", shortDate(start)), el("span", "", `${shortDate(end)} UTC`));
    chart.append(segments, axis);
    const stock = el("div", "timeline-stock");
    stock.append(
      el("strong", "", `${row.available} свободно по плану`),
      el(
        "small",
        row.overdue_quantity ? "warning-text" : "",
        `На складе: ${row.physical_available}${row.overdue_quantity ? ` · просрочено: ${row.overdue_quantity}` : ""}`,
      ),
    );
    card.append(name, chart, stock);
    $("#timeline").append(card);
  }
}

function renderCatalog() {
  $("#catalog").replaceChildren();
  for (const product of state.products) {
    const card = el("article", "product-card");
    card.append(
      el(
        "div",
        "product-symbol",
        { Звук: "◉", Свет: "✳", Видео: "▣", Питание: "ϟ" }[product.category] || "◇",
      ),
      el("h3", "", product.name),
      el("p", "", `${product.sku} · ${product.category}`),
    );
    const numbers = el("div", "product-numbers");
    const capacity = el("strong", "", `${product.capacity} ед.`);
    capacity.append(el("small", "", "Всего в парке"));
    const price = el("strong", "", money(product.daily_rate_cents));
    price.append(el("small", "", "За сутки / единицу"));
    numbers.append(capacity, price);
    card.append(numbers);
    if (state.role === "admin") {
      const form = el("form", "inventory-edit");
      const input = el("input");
      input.type = "number";
      input.min = "0";
      input.max = "1000000";
      input.value = product.capacity;
      input.required = true;
      input.setAttribute("aria-label", `Новый остаток: ${product.name}`);
      const button = el("button", "button secondary", "Сохранить остаток");
      form.append(input, button);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        busy(button, async () => {
          try {
            await api(`/products/${product.id}`, {
              method: "PATCH",
              body: JSON.stringify({ capacity: Number(input.value) }),
            });
            toast("Остаток оборудования обновлён");
            await refresh();
          } catch (error) {
            showError("#global-error", error);
          }
        });
      });
      card.append(form);
    }
    $("#catalog").append(card);
  }
}

$("#new-booking").addEventListener("click", () => {
  const form = $("#composer");
  form.reset();
  state.exactPeriod = null;
  state.alternativesRequest++;
  $("#exact-period").hidden = true;
  $("#alternative-tools").hidden = true;
  $("#alternative-results").replaceChildren();
  clearError("#composer-error");
  $("#composer-conflicts").replaceChildren();
  state.attempt = null;
  const period = defaultPeriod();
  form.elements.start.value = period[0];
  form.elements.end.value = period[1];
  $("#composer-items").replaceChildren();
  for (const product of state.products) {
    const row = el("div", "composer-item");
    const label = el("label");
    label.htmlFor = `quantity-${product.id}`;
    label.append(
      el("strong", "", product.name),
      el("small", "", `${money(product.daily_rate_cents)} / сутки · ${product.capacity} в парке`),
    );
    const available = el("span", "composer-available", "Не проверено");
    available.dataset.product = product.id;
    const quantity = el("input");
    quantity.type = "number";
    quantity.min = "0";
    quantity.max = "1000000";
    quantity.value = "0";
    quantity.dataset.product = product.id;
    quantity.id = `quantity-${product.id}`;
    quantity.setAttribute("aria-label", `Количество: ${product.name}`);
    quantity.addEventListener("input", updateQuote);
    row.append(label, available, quantity);
    $("#composer-items").append(row);
  }
  updateQuote();
  openDialog("#composer-dialog");
});
$("#composer").addEventListener("input", (event) => {
  state.alternativesRequest++;
  $("#alternative-results").replaceChildren();
  if (["start", "end"].includes(event.target.name)) {
    state.exactPeriod = null;
    $("#exact-period").hidden = true;
  }
  updateQuote();
});
function composerPayload() {
  const form = $("#composer");
  return {
    label: form.elements.label.value.trim(),
    ...periodFromFields(form.elements.start.value, form.elements.end.value, state.exactPeriod),
    items: $$("input[data-product]", form)
      .filter((input) => Number(input.value) > 0)
      .map((input) => ({
        product_id: Number(input.dataset.product),
        quantity: Number(input.value),
      })),
    ttl_minutes: 15,
  };
}
function updateQuote() {
  try {
    const payload = composerPayload();
    const days = Math.ceil(
      (state.exactPeriod?.duration_microseconds ??
        (new Date(payload.end_at) - new Date(payload.start_at)) * 1000) / 86400000000,
    );
    const total =
      payload.items.reduce(
        (sum, item) =>
          sum +
          item.quantity *
            state.products.find((product) => product.id === item.product_id).daily_rate_cents,
        0,
      ) * Math.max(0, days);
    $("#quote-total").textContent = Number.isSafeInteger(total)
      ? money(total)
      : "Превышен предел суммы";
    $("#quote-days").textContent =
      days > 0
        ? `${days} × начатые сутки · Позиций: ${payload.items.length}`
        : "Окончание должно быть позже начала";
  } catch {
    $("#quote-total").textContent = "—";
  }
}
$("#composer-check").addEventListener("click", (event) =>
  busy(event.currentTarget, async () => {
    clearError("#composer-error");
    try {
      const payload = composerPayload();
      const rows = await api(
        `/availability?${new URLSearchParams({ start: payload.start_at, end: payload.end_at })}`,
      );
      for (const row of rows) {
        $(`.composer-available[data-product="${row.id}"]`).textContent =
          `Свободно: ${row.available}`;
      }
    } catch (error) {
      showError("#composer-error", error);
    }
  }),
);
$("#composer").addEventListener("submit", (event) => {
  event.preventDefault();
  busy($("#reserve"), async () => {
    clearError("#composer-error");
    $("#composer-conflicts").replaceChildren();
    try {
      const payload = composerPayload();
      if (!payload.items.length) throw new Error("Выберите хотя бы одну единицу оборудования.");
      const body = JSON.stringify(payload);
      if (!state.attempt || state.attempt.body !== body)
        state.attempt = { body, key: crypto.randomUUID() };
      const booking = await api("/bookings", {
        method: "POST",
        headers: { "Idempotency-Key": state.attempt.key },
        body,
      });
      state.attempt = null;
      $("#composer-dialog").close();
      toast(`Резерв ${booking.reference} создан на 15 минут`);
      await refresh();
      await inspect(booking.id);
    } catch (error) {
      showError("#composer-error", error);
      $("#alternative-tools").hidden = error.code !== "capacity_conflict";
      for (const conflict of error.details?.conflicts || []) {
        const card = el(
          "div",
          "conflict",
          `${conflict.name || "Оборудование"}: запрошено ${conflict.requested}, занято ${conflict.reserved}, всего ${conflict.capacity}. ${conflict.start_at ? `${date(conflict.start_at)} → ${date(conflict.end_at)} UTC` : ""}`,
        );
        for (const id of conflict.booking_ids || []) {
          const button = el(
            "button",
            "text-button",
            `Открыть ${state.bookings.find((booking) => booking.id === id)?.reference || id.slice(0, 8)} ↗`,
          );
          button.type = "button";
          button.addEventListener("click", () => inspect(id));
          card.append(button);
        }
        $("#composer-conflicts").append(card);
      }
    }
  });
});

$("#find-alternatives").addEventListener("click", (event) =>
  busy(event.currentTarget, async () => {
    const token = ++state.alternativesRequest;
    $("#alternative-results").replaceChildren(
      el("p", "muted", "Ищем свободные участки без округления времени…"),
    );
    try {
      const payload = composerPayload();
      if (!payload.items.length) throw new Error("Выберите хотя бы одну позицию.");
      const result = await api("/alternatives", {
        method: "POST",
        body: JSON.stringify({
          start_at: payload.start_at,
          end_at: payload.end_at,
          items: payload.items,
          search_until: searchHorizon(payload.start_at),
          limit: 3,
        }),
      });
      // Отбрасываем ответ, если оператор успел изменить даты, количества или закрыть форму.
      if (token !== state.alternativesRequest || !$("#composer-dialog").open) return;
      const box = $("#alternative-results");
      box.replaceChildren();
      if (!result.alternatives.length) {
        const message =
          result.reason === "insufficient_fleet"
            ? result.shortages
                .map(
                  (item) =>
                    `${item.name}: требуется ${item.requested}, всего в парке ${item.capacity}.`,
                )
                .join(" ")
            : "В пределах 30 суток нет непрерывного свободного периода нужной длительности. Измените состав или длительность.";
        box.append(el("p", "notice warning", message));
        return;
      }
      box.append(
        el(
          "p",
          "fineprint",
          "Один вариант на каждый свободный участок. Предложения не удерживают оборудование; при создании всё проверяется заново.",
        ),
      );
      if (result.items.some((item) => item.overdue_quantity))
        box.append(
          el(
            "p",
            "notice warning",
            "Есть просроченные возвраты. Эти варианты подходят по плану; физическая возможность выдачи зависит от возврата оборудования.",
          ),
        );
      for (const option of result.alternatives) {
        const card = el("article", "alternative-card");
        card.append(
          el("strong", "", `${date(option.start_at)} → ${date(option.end_at)} UTC`),
          el("p", "exact-time", `${option.start_at} → ${option.end_at}`),
          el(
            "p",
            "muted",
            `Самое позднее начало на этом участке: ${option.latest_start} (включительно).`,
          ),
          el(
            "p",
            "fineprint",
            option.items
              .map(
                (item) =>
                  `${item.name}: минимум ${item.minimum_available} свободно, нужно ${item.requested}`,
              )
              .join(" · "),
          ),
        );
        const footer = el("div", "alternative-footer");
        const button = el("button", "button secondary", "Выбрать период");
        button.type = "button";
        button.addEventListener("click", () => {
          const form = $("#composer");
          const display = fieldsForPeriod(option);
          form.elements.start.value = display.start;
          form.elements.end.value = display.end;
          // Браузер может нормализовать текст datetime-local; сравниваем с фактическими значениями.
          state.exactPeriod = {
            ...option,
            duration_microseconds: result.duration_microseconds,
            display: { start: form.elements.start.value, end: form.elements.end.value },
          };
          $("#exact-period").textContent =
            `Выбраны точные границы UTC: ${option.start_at} → ${option.end_at}. Они сохраняются до ручного изменения дат.`;
          $("#exact-period").hidden = false;
          clearError("#composer-error");
          $("#composer-conflicts").replaceChildren();
          $("#alternative-tools").hidden = true;
          box.replaceChildren();
          state.attempt = null;
          updateQuote();
          toast("Период выбран. Проверьте данные и создайте резерв.");
        });
        footer.append(el("strong", "", money(option.total_cents)), button);
        card.append(footer);
        box.append(card);
      }
    } catch (error) {
      if (token === state.alternativesRequest) {
        $("#alternative-results").replaceChildren();
        showError("#composer-error", error);
      }
    }
  }),
);

async function inspect(id) {
  openDialog("#inspector-dialog");
  clearError("#inspector-error");
  $("#inspector").replaceChildren(el("p", "muted", "Загружаем бронирование…"));
  try {
    const [booking, audit] = await Promise.all([
      api(`/bookings/${id}`),
      api(`/audit?booking_id=${encodeURIComponent(id)}&limit=20`),
    ]);
    const box = $("#inspector");
    box.replaceChildren();
    const title = el("h2", "", booking.label);
    title.id = "inspector-title";
    box.append(title);
    const meta = el("div", "inspector-meta");
    meta.append(el("span", "reference", booking.reference), badge(booking));
    box.append(meta);
    const period = el(
      "div",
      "inspector-period",
      `${date(booking.start_at)} → ${date(booking.end_at)} UTC`,
    );
    period.append(
      el(
        "span",
        "subline",
        booking.status === "hold"
          ? `Подтвердите до ${date(booking.expires_at)} UTC`
          : `Создал: ${booking.created_by_name}`,
      ),
    );
    box.append(period);
    if (booking.overdue)
      box.append(
        el(
          "div",
          "notice warning",
          "Возврат просрочен. Это оборудование по-прежнему считается выданным и может блокировать следующую выдачу.",
        ),
      );
    for (const item of booking.items) {
      const row = el("div", "inspector-line");
      row.append(
        el("span", "", `${item.name} × ${item.quantity}`),
        el("span", "", `${money(item.rate_cents)} / сутки`),
      );
      box.append(row);
    }
    const total = el("div", "inspector-total");
    total.append(
      el("span", "", "Стоимость бронирования"),
      el("strong", "", money(booking.total_cents)),
    );
    box.append(total);
    if (state.role !== "viewer") {
      const actions = el("div", "inspector-actions");
      const options =
        booking.status === "hold"
          ? [
              ["confirm", "Подтвердить бронь"],
              ["cancel", "Отменить"],
            ]
          : booking.status === "confirmed"
            ? [
                ["check_out", "Выдать оборудование"],
                ["cancel", "Отменить"],
              ]
            : booking.status === "checked_out"
              ? [["return", "Принять возврат"]]
              : [];
      for (const [action, label] of options) {
        const button = el(
          "button",
          `button ${action === "cancel" ? "danger-button" : "primary"}`,
          label,
        );
        if (
          action === "check_out" &&
          !(Date.now() >= Date.parse(booking.start_at) && Date.now() < Date.parse(booking.end_at))
        ) {
          button.disabled = true;
          button.title = "Выдача доступна только в период бронирования";
        }
        button.addEventListener("click", () =>
          busy(button, async () => {
            clearError("#inspector-error");
            try {
              await api(`/bookings/${id}/${action}`, { method: "POST" });
              toast("Состояние бронирования обновлено");
              await refresh();
              await inspect(id);
            } catch (error) {
              showError("#inspector-error", error);
              for (const conflict of error.details?.conflicts || [])
                $("#inspector-error").append(
                  el(
                    "div",
                    "",
                    `Позиция ${conflict.product_id}: выдано ${conflict.checked_out}, требуется ${conflict.requested}, парк ${conflict.capacity}.`,
                  ),
                );
            }
          }),
        );
        actions.append(button);
      }
      box.append(actions);
      if (["hold", "confirmed"].includes(booking.status)) {
        const details = el("details");
        details.append(el("summary", "", "Перенести бронирование"));
        const form = el("form");
        const grid = el("div", "form-grid");
        for (const [key, label] of [
          ["start_at", "Новое начало · UTC"],
          ["end_at", "Новое окончание · UTC"],
        ]) {
          const field = el("label", "", label);
          const input = el("input");
          input.type = "datetime-local";
          input.name = key;
          input.value = localInput(booking[key]);
          input.required = true;
          field.append(input);
          grid.append(field);
        }
        const button = el("button", "button secondary wide", "Проверить и перенести");
        form.append(
          grid,
          el(
            "p",
            "fineprint",
            "Ставки сохранятся; стоимость изменится с длительностью. Срок резерва не продлевается.",
          ),
          button,
        );
        details.append(form);
        box.append(details);
        form.addEventListener("submit", (event) => {
          event.preventDefault();
          busy(button, async () => {
            try {
              await api(`/bookings/${id}/schedule`, {
                method: "PATCH",
                body: JSON.stringify({
                  start_at: toUTC(form.elements.start_at.value),
                  end_at: toUTC(form.elements.end_at.value),
                }),
              });
              toast("Бронирование перенесено");
              await refresh();
              await inspect(id);
            } catch (error) {
              showError("#inspector-error", error);
            }
          });
        });
      }
    }
    const history = el("details");
    history.append(el("summary", "", `История бронирования · ${audit.length} последних событий`));
    for (const entry of audit) history.append(auditCard(entry));
    box.append(history);
  } catch (error) {
    showError("#inspector-error", error);
  }
}
function auditCard(entry) {
  const card = el("div", "audit-event");
  card.append(el("span", "audit-icon", "↺"));
  const body = el("div");
  body.append(
    el("strong", "", actionLabels[entry.action] || entry.action),
    el("p", "", `${entry.actor_name} · #${entry.id}`),
  );
  if (entry.booking_id) {
    const button = el(
      "button",
      "text-button",
      `${state.bookings.find((booking) => booking.id === entry.booking_id)?.reference || entry.booking_id.slice(0, 8)} ↗`,
    );
    button.addEventListener("click", () => inspect(entry.booking_id));
    body.append(button);
  }
  card.append(body, el("time", "", `${date(entry.created_at)} UTC`));
  return card;
}
async function loadAudit(more = false) {
  const before = more && state.audit.length ? `&before_id=${state.audit.at(-1).id}` : "";
  const rows = await api(`/audit?limit=30${before}`);
  state.audit = more ? [...state.audit, ...rows] : rows;
  $("#audit-list").replaceChildren(...state.audit.map(auditCard));
  $("#more-audit").hidden = rows.length < 30;
  if (!state.audit.length)
    $("#audit-list").append(
      el("p", "empty", "История пока пуста. Здесь появятся успешные операции."),
    );
}
$("#more-audit").addEventListener("click", (event) =>
  busy(event.currentTarget, () =>
    loadAudit(true).catch((error) => showError("#global-error", error)),
  ),
);
$("#expire").addEventListener("click", (event) =>
  busy(event.currentTarget, async () => {
    try {
      const result = await api("/admin/expire", { method: "POST" });
      toast(`Завершено резервов: ${result.expired}`);
      await refresh();
    } catch (error) {
      showError("#global-error", error);
    }
  }),
);
try {
  const config = await api("/config");
  $("#demo-badge").hidden = !config.demo;
  $("#demo-logins").hidden = !config.demo;
  if (config.demo) await login("demo-operator");
  else openDialog("#login-dialog");
} catch (error) {
  showError("#global-error", error);
  openDialog("#login-dialog");
}

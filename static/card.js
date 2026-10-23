// Карточка заявки для фотографа + панель пересчёта с персональной скидкой.
// Цена всегда считается на сервере (/api/manager/quote), здесь только отображение.

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const rub = (n) => (n < 0 ? "−" : "") + Math.abs(n).toLocaleString("ru-RU") + " ₽";

let OPTIONS = null;
async function calcOptions() {
  if (!OPTIONS) OPTIONS = await (await fetch("/api/calc/options")).json();
  return OPTIONS;
}

const LABELS_FALLBACK = {
  theme: "Тематика", lead_type: "Тип заявки", shoot_date: "Дата съёмки", date_status: "Дата по календарю",
  location: "Место", duration_hours: "Длительность", people: "Кто в кадре", budget: "Бюджет",
  price_estimate: "Ориентир стоимости", key_data: "Известные детали", missing_data: "Чего не хватает",
  next_step: "Следующий шаг", reply_draft: "Черновик ответа",
};

function value(v) {
  if (Array.isArray(v)) return v.length ? `<ul>${v.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "—";
  if (typeof v === "boolean") return v ? "да" : "нет";
  return esc(v);
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export function breakdownHtml(q) {
  if (!q || q.total == null) return `<p class="muted">Выберите тематику — посчитаю стоимость.</p>`;
  const rows = q.lines.map((l) => `<div class="bl"><span>${esc(l.label)}</span><span>${rub(l.amount)}</span></div>`).join("");
  const disc = q.discounts.map((l) => `<div class="bl disc"><span>${esc(l.label)}</span><span>${rub(l.amount)}</span></div>`).join("");
  return `${rows}${disc}<div class="bl total"><span>Итого${q.is_from ? " (от)" : ""}</span><span>${rub(q.total)}</span></div>
    <p class="muted small">В ретуши ${q.retouch} кадров</p>`;
}

export async function renderCard(root, card, crm, labels = LABELS_FALLBACK) {
  const order = ["theme", "lead_type", "shoot_date", "date_status", "location", "duration_hours", "people", "budget", "price_estimate", "key_data", "missing_data", "next_step"];
  const rows = order.map((k) => `<dt>${esc(labels[k])}</dt><dd>${value(card[k])}</dd>`).join("");
  const badge = card.needs_human
    ? `<span class="badge human">Нужен фотограф лично</span>`
    : `<span class="badge ok">Можно ответить по черновику</span>`;
  const reason = card.needs_human ? `<p class="reason">${esc(card.human_reason)}</p>` : "";
  let crmHtml = "";
  if (crm?.status === "ok") {
    crmHtml = `Лид создан в Битрикс24: <a href="${esc(crm.lead_url)}" target="_blank" rel="noopener">№${esc(crm.lead_id)}</a>`;
    if (crm.warnings?.length) crmHtml += `<br><span class="warn">${crm.warnings.map(esc).join("<br>")}</span>`;
  } else if (crm) {
    crmHtml = `<span class="warn">${esc(crm.message)}</span>`;
  }
  const canRecalc = card.lead_type !== "нецелевой" && card.lead_type !== "жалоба";

  root.innerHTML = `
    <div class="head"><span class="badge">${esc(card.lead_type)}</span><span class="badge">${esc(card.source || "текст")}</span>${badge}</div>
    <p class="summary">${esc(card.summary)}</p>
    ${reason}
    <dl>${rows}</dl>
    ${canRecalc ? `<details class="recalc">
      <summary>Пересчитать и задать скидку</summary>
      <div class="recalc-body"></div>
    </details>` : ""}
    <h3>${esc(labels.reply_draft)} <button class="copy" type="button">Скопировать</button></h3>
    <div class="draft"></div>
    <div class="crm">${crmHtml}</div>`;

  const draftEl = root.querySelector(".draft");
  draftEl.textContent = card.reply_draft;
  root.querySelector(".copy").addEventListener("click", () => navigator.clipboard?.writeText(draftEl.textContent));
  if (canRecalc) await mountRecalc(root.querySelector(".recalc-body"), card, crm, draftEl);
}

async function mountRecalc(box, card, crm, draftEl) {
  const opt = await calcOptions();
  const p = card.params || {};
  const themeOpts = opt.themes.map((t) => `<option value="${esc(t.title)}" ${t.key === p.theme ? "selected" : ""}>${esc(t.title)}</option>`).join("");
  box.innerHTML = `
    <div class="rgrid">
      <label>Тематика <select name="theme"><option value="">— выберите —</option>${themeOpts}</select></label>
      <label>Часы <input name="hours" type="number" min="0.5" max="${opt.max_hours}" step="0.5" value="${p.hours ?? ""}" placeholder="базовый"></label>
      <label>Дата <input name="date" type="date" value="${esc(p.date || "")}"></label>
      <label>Доп. кадры <input name="extra_frames" type="number" min="0" max="500" step="5" value="${p.extra_frames || 0}"></label>
    </div>
    <div class="checks">
      <label><input type="checkbox" name="out_of_town" ${p.out_of_town ? "checked" : ""}> выезд за город</label>
      <label><input type="checkbox" name="urgent" ${p.urgent ? "checked" : ""}> срочная обработка</label>
      <label class="second"><input type="checkbox" name="second_photographer" ${p.second_photographer ? "checked" : ""}> второй фотограф</label>
    </div>
    <div class="rgrid personal">
      <label>Персональная скидка, % <input name="personal_discount" type="number" min="0" max="${opt.discounts.max_personal_percent}" step="1" value="0"></label>
      <label>Причина <input name="personal_reason" maxlength="120" placeholder="постоянный клиент, съёмка для портфолио…"></label>
    </div>
    <div class="breakdown"></div>
    <div class="ractions">
      ${crm?.lead_id ? `<button type="button" class="save">Сохранить в Битрикс24</button>` : ""}
      <span class="rdate muted small"></span>
      <span class="rsave small"></span>
    </div>`;

  const f = (n) => box.querySelector(`[name="${n}"]`);
  const secondWrap = box.querySelector(".second");
  const status = box.querySelector(".rdate");
  const saveStatus = box.querySelector(".rsave");
  const body = card.reply_body || card.reply_draft;
  let last = null;
  let savedKey = null;

  const params = () => ({
    theme: f("theme").value || null,
    hours: f("hours").value ? Number(f("hours").value) : null,
    date: f("date").value || null,
    out_of_town: f("out_of_town").checked,
    urgent: f("urgent").checked,
    extra_frames: Number(f("extra_frames").value || 0),
    second_photographer: f("second_photographer").checked,
    personal_discount: Number(f("personal_discount").value || 0),
    personal_reason: f("personal_reason").value,
  });

  const recalc = debounce(async () => {
    if (JSON.stringify(params()) !== savedKey) saveStatus.textContent = "";
    const t = opt.themes.find((x) => x.title === f("theme").value);
    secondWrap.hidden = !(t && t.full_day);
    const r = await fetch("/api/manager/quote", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(params()) });
    const q = await r.json();
    if (!r.ok) { status.textContent = q.error || "Ошибка расчёта"; return; }
    last = q;
    status.textContent = q.date_status && q.date_status !== "не указана" ? `Дата: ${q.date_status}` : "";
    box.querySelector(".breakdown").innerHTML = breakdownHtml(q);
    draftEl.textContent = q.reply_block ? `${body}\n\n${q.reply_block}` : body;
  }, 250);

  box.addEventListener("input", recalc);
  box.addEventListener("change", recalc);
  box.querySelector(".save")?.addEventListener("click", async () => {
    saveStatus.className = "rsave small muted";
    saveStatus.textContent = "Сохраняю…";
    const r = await fetch("/api/manager/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ lead_id: crm.lead_id, params: params() }) });
    const j = await r.json();
    saveStatus.className = `rsave small ${r.ok ? "okmsg" : "warn"}`;
    if (r.ok) savedKey = JSON.stringify(params());
    saveStatus.textContent = r.ok ? `✓ Сохранено: сумма лида ${rub(j.total)}, расчёт в ленте лида` : j.error;
  });
  recalc();
}

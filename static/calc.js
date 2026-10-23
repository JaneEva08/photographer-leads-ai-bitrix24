import { breakdownHtml, renderCard } from "./card.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const rub = (n) => Math.abs(n).toLocaleString("ru-RU") + " ₽";
const fmtDate = (iso) => (iso ? iso.split("-").reverse().join(".") : "не выбрана");
const hoursTxt = (h) => `${String(h).replace(".", ",")} ч`;

let OPT;
const state = { theme: null, hours: null, date: "", out_of_town: false, urgent: false, second_photographer: false, extra_frames: 0 };
let lastQuote = null;
let reqSeq = 0;

const theme = () => OPT.themes.find((t) => t.key === state.theme);

function params() {
  const t = theme();
  return {
    theme: t ? t.title : null,
    hours: state.hours,
    date: state.date || null,
    out_of_town: state.out_of_town,
    urgent: state.urgent,
    extra_frames: state.extra_frames,
    second_photographer: !!(t && t.full_day && state.second_photographer),
  };
}

async function init() {
  OPT = await (await fetch("/api/calc/options")).json();
  if (OPT.demo_mode) $("nav").hidden = false;
  const ex = OPT.extras, d = OPT.discounts;
  $("pOut").textContent = `+${rub(ex.out_of_town)}`;
  $("pUrg").textContent = `+${ex.urgent_percent}%`;
  $("pSec").textContent = `+${rub(ex.second_photographer_hour)}/ч`;
  $("pFr").textContent = `${rub(ex.extra_retouch_frame)} за кадр`;
  $("discHint").textContent = `Скидки считаются сами: фотосессия в будний день −${d.weekday_percent}%, свадьба за ${Math.round(d.early_wedding_days / 30)}+ месяцев −${d.early_wedding_percent}%.`;
  $("date").min = OPT.today;

  $("themes").innerHTML = OPT.themes.map((t) => `
    <button type="button" class="theme" role="radio" aria-checked="false" data-key="${t.key}">
      <strong>${esc(t.title)}</strong>
      <span>от ${rub(t.base_price)} · ${hoursTxt(t.base_hours)} · ${t.retouch} кадров</span>
    </button>`).join("");
  $("themes").addEventListener("click", (e) => {
    const b = e.target.closest(".theme");
    if (!b) return;
    state.theme = b.dataset.key;
    state.hours = theme().base_hours;
    document.querySelectorAll(".theme").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
    update();
  });

  document.querySelectorAll(".stepper").forEach((s) => s.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const d = Number(b.dataset.d);
    if (s.dataset.field === "hours") {
      const t = theme();
      if (!t) return;
      state.hours = Math.min(OPT.max_hours, Math.max(t.base_hours, (state.hours || t.base_hours) + d));
    } else {
      state.extra_frames = Math.min(500, Math.max(0, state.extra_frames + d));
    }
    update();
  }));
  $("date").addEventListener("change", (e) => { state.date = e.target.value; update(); });
  for (const [id, key] of [["out_of_town", "out_of_town"], ["urgent", "urgent"], ["second", "second_photographer"]]) {
    $(id).addEventListener("change", (e) => { state[key] = e.target.checked; update(); });
  }
  $("datestatus").addEventListener("click", (e) => {
    const b = e.target.closest("[data-date]");
    if (!b) return;
    state.date = b.dataset.date;
    $("date").value = state.date;
    update();
  });

  $("toStep2").addEventListener("click", () => go(2));
  $("back").addEventListener("click", () => go(1));
  $("again").addEventListener("click", () => location.reload());
  $("contact").addEventListener("submit", send);
  // на телефоне плашка с итогом не нужна, когда сам расчёт уже на экране
  new IntersectionObserver(([e]) => $("mobilebar").classList.toggle("inview", e.isIntersecting))
    .observe($("summary"));
  update();
}

async function update() {
  const t = theme();
  $("hoursOut").textContent = t ? hoursTxt(state.hours) : "—";
  $("hoursHint").textContent = t ? `В пакете ${hoursTxt(t.base_hours)}, каждый следующий час — ${rub(t.extra_hour)}` : "Сначала выберите, что снимаем";
  $("framesOut").textContent = state.extra_frames;
  $("secondWrap").hidden = !(t && t.full_day);

  if (!t) {
    $("breakdown").innerHTML = breakdownHtml(null);
    $("toStep2").disabled = true;
    return;
  }
  const seq = ++reqSeq;
  const r = await fetch("/api/calc/quote", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(params()) });
  const q = await r.json();
  if (seq !== reqSeq) return; // пришёл ответ на устаревший расчёт
  if (!r.ok) { $("breakdown").innerHTML = `<p class="error">${esc(q.error)}</p>`; return; }
  lastQuote = q;
  $("breakdown").innerHTML = breakdownHtml(q);
  $("mobileTotal").textContent = (q.is_from ? "от " : "") + rub(q.total);
  $("mobilebar").hidden = false;
  $("toStep2").disabled = false;
  renderDateStatus(q);
}

function renderDateStatus(q) {
  const el = $("datestatus");
  if (!state.date) { el.className = "datestatus"; el.textContent = "Дату можно не выбирать — тогда фотограф предложит варианты."; return; }
  if (q.date_status === "свободна") {
    el.className = "datestatus free";
    el.textContent = `${fmtDate(state.date)} свободна`;
  } else if (q.date_status === "занята") {
    el.className = "datestatus busy";
    const alt = q.nearest_free.map((d) => `<button type="button" class="chip" data-date="${d}">${fmtDate(d)}</button>`).join("");
    el.innerHTML = `${fmtDate(state.date)} уже занята.${alt ? ` Свободные даты рядом: ${alt}` : ""}`;
  } else {
    el.className = "datestatus";
    el.textContent = "Эта дата уже прошла.";
  }
}

function recapHtml() {
  const t = theme(), p = params();
  const opts = [p.out_of_town && "выезд за город", p.urgent && "срочная обработка", p.second_photographer && "второй фотограф",
    p.extra_frames && `+${p.extra_frames} кадров в ретушь`].filter(Boolean);
  const status = lastQuote?.date_status === "занята" ? " — занята, фотограф предложит другую" : lastQuote?.date_status === "свободна" ? " — свободна" : "";
  return `
    <dt>Съёмка</dt><dd>${esc(t.title)}</dd>
    <dt>Дата</dt><dd>${fmtDate(state.date)}${status}</dd>
    <dt>Длительность</dt><dd>${hoursTxt(state.hours)}</dd>
    <dt>Дополнительно</dt><dd>${opts.length ? esc(opts.join(", ")) : "—"}</dd>`;
}

function go(step) {
  for (const n of [1, 2, 3]) $(`step${n}`).hidden = n !== step;
  $("mobilebar").classList.toggle("off", step !== 1);
  document.querySelectorAll("#steps li").forEach((li, i) => li.classList.toggle("on", i < step));
  if (step === 2) {
    $("recap").innerHTML = recapHtml();
    $("breakdown2").innerHTML = breakdownHtml(lastQuote);
  }
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function fail(msg) {
  $("error").textContent = msg;
  $("error").hidden = false;
}

async function send(e) {
  e.preventDefault();
  const f = Object.fromEntries(new FormData($("contact")));
  if (!f.phone?.trim() && !f.email?.trim()) return fail("Укажите телефон или email, чтобы фотограф мог ответить.");
  if (!f.consent) return fail("Отметьте согласие на обработку персональных данных.");
  $("error").hidden = true;
  $("send").disabled = true;
  $("send").textContent = "Отправляю…";
  try {
    const body = { name: f.name || "", phone: f.phone || "", email: f.email || "", comment: f.comment || "",
      consent: !!f.consent, portfolio_consent: !!f.portfolio_consent, params: params() };
    const r = await fetch("/api/calc/request", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({ error: "Сервер вернул непонятный ответ." }));
    if (!r.ok) throw new Error(j.error || `Ошибка ${r.status}`);
    $("doneMsg").textContent = j.message;
    $("doneRecap").textContent = `${theme().title}, ${fmtDate(state.date)}, предварительно ${rub(j.quote.total)}.`;
    go(3);
    if (j.card) {
      $("demo").hidden = false;
      if (j.masked_text) { $("masked").textContent = j.masked_text; $("maskedBox").hidden = false; }
      await renderCard($("card"), j.card, j.crm);
    }
  } catch (err) {
    fail(err.message === "Failed to fetch" ? "Не удалось отправить — проверьте интернет и попробуйте ещё раз." : err.message);
  } finally {
    $("send").disabled = false;
    $("send").textContent = "Отправить заявку";
  }
}

init();

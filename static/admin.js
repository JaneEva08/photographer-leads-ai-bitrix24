import { renderCard } from "./card.js";

const $ = (id) => document.getElementById(id);
const form = $("form");
let LABELS = {};

async function init() {
  const cfg = await (await fetch("/api/config")).json();
  LABELS = cfg.labels;
  const box = $("samples");
  cfg.samples.forEach((s) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = s.label;
    b.addEventListener("click", () => {
      for (const k of ["name", "phone", "email", "text"]) form.elements[k].value = s[k] || "";
      $("error").hidden = true;
    });
    box.appendChild(b);
  });
}

function show(state) {
  $("empty").hidden = state !== "empty";
  $("loading").hidden = state !== "loading";
  $("card").hidden = state !== "card";
}

function fail(msg) {
  $("error").textContent = msg;
  $("error").hidden = false;
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(form));
  if (!data.text.trim()) return fail("Заполните текст заявки.");
  if (!data.phone.trim() && !data.email.trim()) return fail("Укажите хотя бы один контакт: телефон или email.");
  $("error").hidden = true;
  $("go").disabled = true;
  show("loading");
  try {
    const r = await fetch("/api/process", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
    const body = await r.json().catch(() => ({ error: "Сервер вернул непонятный ответ." }));
    if (!r.ok) throw new Error(body.error || `Ошибка ${r.status}`);
    await renderCard($("card"), body.card, body.crm, LABELS);
    show("card");
    if (body.masked_text !== undefined) {
      $("masked").textContent = body.masked_text;
      $("maskedBox").hidden = false;
    }
  } catch (err) {
    show("empty");
    fail(err.message === "Failed to fetch" ? "Сервер не отвечает. Он запущен?" : err.message);
  } finally {
    $("go").disabled = false;
  }
});

init();

"use strict";
const $ = (s) => document.querySelector(s);
const els = {
  statusDot: $("#statusDot"), statusText: $("#statusText"),
  connectBtn: $("#connectBtn"), themeBtn: $("#themeBtn"),
  cumIn: $("#cumIn"), cumOut: $("#cumOut"), cumRub: $("#cumRub"), usageReset: $("#usageReset"),
  sesIn: $("#sesIn"), sesOut: $("#sesOut"), sesRub: $("#sesRub"),
  dropzone: $("#dropzone"), fileInput: $("#fileInput"), dzText: $("#dzText"), mapSummary: $("#mapSummary"),
  posBody: $("#posBody"),
  runBtn: $("#runBtn"), reportBtn: $("#reportBtn"),
  resetBtn: $("#resetBtn"), stopBtn: $("#stopBtn"),
  collapseAllBtn: $("#collapseAllBtn"), tableHint: $("#tableHint"),
  queueEmpty: $("#queueEmpty"), qRunning: $("#qRunning"), qQueued: $("#qQueued"), qDone: $("#qDone"),
  modeBtn: $("#modeBtn"), modeModal: $("#modeModal"), modeClose: $("#modeClose"),
  modeSave: $("#modeSave"), modeReset: $("#modeReset"),
  messages: $("#messages"), chatInput: $("#chatInput"), chatSend: $("#chatSend"),
  chatList: $("#chatList"), chatNew: $("#chatNew"), chatDelete: $("#chatDelete"),
  chatStop: $("#chatStop"),
  logConsole: $("#logConsole"), logLevel: $("#logLevel"), logSearch: $("#logSearch"),
  logAutoscroll: $("#logAutoscroll"), logClear: $("#logClear"), traceTools: $("#traceTools"),
  modelConsole: $("#modelConsole"), modelTools: $("#modelTools"), modelSearch: $("#modelSearch"),
  modelAutoscroll: $("#modelAutoscroll"), modelClear: $("#modelClear"),
  selfTestBtn: $("#selfTestBtn"), testModal: $("#testModal"), testClose: $("#testClose"),
  testRun: $("#testRun"), testError: $("#testError"), testHint: $("#testHint"),
  checkSearch: $("#checkSearch"), checkSearchDetail: $("#checkSearchDetail"),
  checkModel: $("#checkModel"), checkModelDetail: $("#checkModelDetail"),
  checkSpeed: $("#checkSpeed"), checkSpeedDetail: $("#checkSpeedDetail"),
  speedBox: $("#speedBox"), speedGrid: $("#speedGrid"), speedWarn: $("#speedWarn"),
  statusStrip: $("#statusStrip"), statusMsg: $("#statusMsg"),
  modelState: $("#modelState"), modelStateMsg: $("#modelStateMsg"),
  parseState: $("#parseState"), parseStateMsg: $("#parseStateMsg"),
  affix: $("#affix"),
  cTotal: $("#cTotal"), cFound: $("#cFound"), cAnalog: $("#cAnalog"), cNone: $("#cNone"),
  cElapsed: $("#cElapsed"),
  modal: $("#modal"), modalClose: $("#modalClose"), form: $("#modelForm"),
  formError: $("#formError"), submitBtn: $("#submitBtn"), toasts: $("#toasts"),
  modelSelect: $("#modelSelect"), modelBaseUrl: $("#modelBaseUrl"), modelToken: $("#modelToken"),
  modelMeta: $("#modelMeta"), tokenState: $("#tokenState"),
  serperKey: $("#serperKey"), serperState: $("#serperState"),
};

let connected = false, authMethod = "key", chatWs = null, streaming = false;
let modelReady = false;                               // прямой API готов (выбрана модель + есть токен)
let posRows = {}, posCandidates = {}, srcFound = 0;   // строки таблицы, кандидаты по строке
let fetchByUrl = {};                                  // результат загрузки страницы по URL (M3a)
let manualByUrl = {};                                 // состояние ручного режима по URL
let pricesByUrl = {};                                 // извлечённые цены по URL (M4): [{value,currency,match,...}]
let siteByUrl = {};                                   // статус анализа сайта по URL (site_update): {status,chars,found,match,price,comment,note}
let verdictByRow = {};                                // свод по товару (M4): {primary,min,max,match,confidence}
let inspectByUrl = {};                              // триаж страницы по URL (§8): {kind,price_present,reason}
let expandedRows = new Set();                         // раскрытые товары (для сворачивания панели)
let taskById = {};                                    // задачи оркестратора по id (§9/M8): {kind,row,state,...}
let extractProgress = { done: 0, total: 0 };          // прогресс извлечения «товар k/N»
let traceStream = {};                                 // записи стрима модели по ключу (см. streamKey)
const STAGES = ["query", "fetch", "extract", "match"];
const LEVELS = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
let logs = [], autoscroll = true;
// Вкладка «Модель»: записи живут в массиве, а не только в DOM — иначе фильтр и «очистить»
// стирали бы ответы модели безвозвратно, а они дороже логов (за них заплачено токенами).
let modelLog = [], modelAutoscroll = true;
const MODEL_LOG_MAX = 1000;
// Потолок УЗЛОВ в консолях «Ход» и «Модель». Массивы обрезались и раньше, а DOM — нет: за
// прогон на 999 позиций в двух консолях накапливались десятки тысяч элементов. На скорость
// поиска это не влияет (он идёт на сервере), но каждая вставка сопровождается прокруткой
// вниз — принудительный reflow, и его цена растёт вместе с длиной консоли. Отсюда «интерфейс
// подтормаживает к концу большого прогона».
const LOG_DOM_MAX = 1000;

// ---- Тема ------------------------------------------------------------------
(function () {
  const saved = localStorage.getItem("theme");
  const dark = saved ? saved === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  setTheme(dark);
})();
function setTheme(dark) {
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  els.themeBtn.textContent = dark ? "☀" : "☾";
  localStorage.setItem("theme", dark ? "dark" : "light");
}
els.themeBtn.onclick = () => setTheme(document.documentElement.getAttribute("data-theme") !== "dark");

// ---- Тосты -----------------------------------------------------------------
function toast(msg, kind = "") {
  const t = document.createElement("div");
  t.className = "toast " + kind; t.textContent = msg;
  els.toasts.appendChild(t);
  setTimeout(() => t.remove(), 5000);
}

// ---- Запросы к серверу -----------------------------------------------------
// Единая точка: тело читаем как ТЕКСТ и разбираем сами. Иначе при ответе не-JSON (500 от
// uvicorn, прокси, обрыв) res.json() кидает невнятную ошибку браузера («The string did not
// match the expected pattern» в Safari) вместо настоящей причины.
async function apiJson(url, opts) {
  let res;
  try { res = await fetch(url, opts); }
  catch (e) { throw new Error("Сервер недоступен: " + (e.message || e)); }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (_) {}
  if (data === null) {
    throw new Error(res.ok ? "Пустой/неразборчивый ответ сервера (HTTP " + res.status + ")"
                           : "HTTP " + res.status + ": " + text.slice(0, 200));
  }
  if (!res.ok || data.ok === false) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}
function postJson(url, body) {
  return apiJson(url, { method: "POST", headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(body || {}) });
}

// ---- Статус ----------------------------------------------------------------
function setStatus(state, text) { els.statusDot.className = "dot " + state; els.statusText.textContent = text; }
setStatus("local", "Локально");

// Всегда-видимая строка статуса (питается из логов и действий; правило log-everything).
const LEVEL_CLASS = { DEBUG: "info", INFO: "info", WARNING: "warn", ERROR: "err", CRITICAL: "err" };
function setStatusMsg(text, level) {
  els.statusMsg.textContent = text;
  els.statusStrip.className = "statusstrip " + (level === "busy" ? "busy" : (LEVEL_CLASS[level] || "info"));
}

// ---- Загрузка Excel --------------------------------------------------------
// dropzone — это <label>, обёрнутый вокруг <input type=file>: клик по нему открывает
// диалог НАТИВНО. Отдельный .click() здесь НЕ ставим — иначе диалог триггерится дважды
// и выбор файла может не сработать (баг «работает перетаскивание, не работает выбор»).
els.fileInput.onchange = () => {
  const f = els.fileInput.files[0];
  if (f) uploadFile(f);
  els.fileInput.value = "";            // сброс: тот же файл можно выбрать повторно
};
["dragover", "dragenter"].forEach((e) => els.dropzone.addEventListener(e, (ev) => {
  ev.preventDefault(); els.dropzone.classList.add("drag");
}));
["dragleave", "drop"].forEach((e) => els.dropzone.addEventListener(e, (ev) => {
  ev.preventDefault(); els.dropzone.classList.remove("drag");
}));
els.dropzone.addEventListener("drop", (ev) => {
  const f = ev.dataTransfer.files[0]; if (f) uploadFile(f);
});

async function uploadFile(file) {
  els.dzText.textContent = "Загрузка: " + file.name + "…";
  setStatusMsg("Загрузка файла: " + file.name + "…", "busy");
  const fd = new FormData(); fd.append("file", file);
  try {
    const data = await apiJson("/api/upload", { method: "POST", body: fd });
    renderUpload(data);
    els.dzText.textContent = file.name + " · загрузить другой";
    setStatusMsg("Разобрано: " + data.counts.total + " товаров (" + file.name + ")", "INFO");
    toast("Файл разобран: " + data.counts.total + " товаров", "ok");
  } catch (err) {
    els.dzText.textContent = "Перетащите Excel/CSV или нажмите для выбора";
    setStatusMsg("Ошибка загрузки: " + (err.message || err), "ERROR");
    toast(String(err.message || err), "err");
  }
}

function renderUpload(d) {
  try { _renderUpload(d); }
  catch (e) {                                    // правило «логировать видимо»: не молча
    console.error("renderUpload:", e);
    toast("Ошибка отрисовки таблицы: " + (e && e.message || e), "err");
    setStatusMsg("Ошибка отрисовки: " + (e && e.message || e), "ERROR");
  }
}

function _renderUpload(d) {
  // Плашка молчит, пока файл разобран нормально: «Колонки: name / Уверенность: средняя /
  // Заголовок: строка 0 / Лист: Sheet1» ничего не решали и занимали место у таблицы.
  // Остаётся единственное, что требует действия: из файла не вышло ни одного товара — сервер
  // присылает готовую причину (пустой файл, только заголовок, колонка не распознана).
  if (d.reason) {
    els.mapSummary.innerHTML = `<span class="chip warn">⚠ товары не распознаны: ${esc(d.reason)}</span>`;
    els.mapSummary.hidden = false;
  } else {
    els.mapSummary.innerHTML = ""; els.mapSummary.hidden = true;
  }

  // Таблица: каждая строка с наименованием — товар (ничего не исключаем)
  els.posBody.innerHTML = "";
  posRows = {}; posCandidates = {}; srcFound = 0;
  if (!d.positions.length) {
    els.posBody.innerHTML = `<tr class="empty-row"><td colspan="7">Товары не распознаны${d.reason ? " — " + esc(d.reason) : "."}</td></tr>`;
  }
  d.positions.forEach((p, i) => makePosRow(p, i + 1));
  els.cTotal.textContent = d.counts.total;
  els.cFound.textContent = els.cAnalog.textContent = els.cNone.textContent = 0;
  stopElapsed(); elapsedFrom = null; renderElapsed(); parsedRows = 0; resetParseWait();   // новый файл — новая задача
  els.reportBtn.disabled = true;                              // сводов ещё нет — отчёт недоступен
  fetchByUrl = {}; manualByUrl = {}; pricesByUrl = {}; siteByUrl = {}; verdictByRow = {}; inspectByUrl = {};
  resetQueue(); expandedRows.clear(); updateExpandedMode();
  els.runBtn.disabled = false;                                // «Найти» активна после загрузки файла
  els.resetBtn.disabled = false;
}

// Одна строка товара в таблице позиций (используется при загрузке и восстановлении сессии).
// Нумерация в колонке «#» — порядковая (1..N), а не номер строки Excel: сам номер строки живёт
// в data-row (на нём вся событийная логика) и показывается подсказкой.
function makePosRow(p, idx) {
  const tr = document.createElement("tr");
  tr.dataset.row = p.row; tr.dataset.name = p.name || "";
  tr.dataset.idx = idx;                       // порядковый номер — им подписаны ответы модели
  tr.innerHTML =
    `<td class="c-row" title="строка в файле: ${p.row}">${idx}</td>` +
    `<td class="c-name">${esc(p.name) || "—"}</td>` +
    `<td class="c-stage">${stagesHtml("pending")}<span class="rstate">ожидает</span></td>` +
    `<td class="c-price">—</td>` +
    `<td class="c-found">—</td>` +
    `<td class="c-range">—</td>` +
    `<td class="c-time">—</td>`;
  tr.onclick = () => toggleExpand(p.row);
  els.posBody.appendChild(tr);
  posRows[p.row] = tr;
  return tr;
}

function stagesHtml(state) {
  return `<span class="stages">${STAGES.map((st) => `<span class="s ${state === "pending" ? "" : state}" data-stage="${st}"></span>`).join("")}</span>`;
}
function esc(s) { return String(s == null ? "" : s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }

// ---- Итоговый Excel-отчёт (M7) ----------------------------------------------
// Два листа: «Итоги» по позициям и «Детали» по каждому сайту выдачи. Сервер строит отчёт из
// СВОЕЙ сессии (полные своды, статусы сайтов, цены по страницам) и НЕ зовёт модель: комментарии
// написаны ещё во время прогона, поэтому файл отдаётся сразу, а не через минуты генерации.
els.reportBtn.onclick = async () => {
  if (!Object.keys(verdictByRow).length) { toast("Нет сводов: сначала выполните «Найти».", ""); return; }
  els.reportBtn.disabled = true;
  setStatusMsg("Собираю отчёт…", "busy");
  try {
    const res = await fetch("/api/report", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: [] }),
    });
    if (!res.ok) {                                   // тело может быть и не JSON — показываем как есть
      const body = await res.text();
      let msg = "HTTP " + res.status + ": " + body.slice(0, 200);
      try { msg = JSON.parse(body).error || msg; } catch (_) {}
      throw new Error(msg);
    }
    const blob = await res.blob();
    const cd = res.headers.get("Content-Disposition") || "";
    const m = /filename\*=UTF-8''([^;]+)/.exec(cd);
    const fname = m ? decodeURIComponent(m[1]) : "отчёт_цены.xlsx";
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = fname; document.body.appendChild(a); a.click();
    a.remove(); URL.revokeObjectURL(url);
    setStatusMsg(`Отчёт скачан: ${fname}`, "INFO");
    toast("Отчёт готов: " + fname, "ok");
  } catch (e) {
    setStatusMsg("Ошибка отчёта: " + (e.message || e), "ERROR"); toast(String(e.message || e), "err");
  } finally {
    els.reportBtn.disabled = false;
  }
};

// ---- WebSocket событий (логи) ---------------------------------------------
function openEvents() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/events`);
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "log") {
      logs.push(m); if (logs.length > 2000) logs = logs.slice(-1500);
      renderLog(m);
      // Всегда-видимый статус: последнее событие из логов (кроме DEBUG-шума).
      if (m.level !== "DEBUG") setStatusMsg(m.name.replace("search_agent.", "") + ": " + m.msg, m.level);
    } else if (m.type === "position") {
      setStage(m.row, m.stage, m.state);
      if (m.stage === "extract" && m.state === "active" && m.domain) {
        const site = (m.idx && m.count) ? ` (${m.idx}/${m.count})` : "";
        const it = extractProgress.total ? `товар ${Math.min(extractProgress.done + 1, extractProgress.total)}/${extractProgress.total} · ` : "";
        setStatusMsg(`Извлечение: ${it}сайт ${m.domain}${site}`, "busy");
      }
    } else if (m.type === "candidates") {
      setCandidates(m.row, m.list, m.queries);
    } else if (m.type === "fetched") {
      onFetched(m);
    } else if (m.type === "inspect") {
      onInspect(m);
    } else if (m.type === "task") {
      onTask(m);
    } else if (m.type === "llm_state") {
      onLlmState(m);
    } else if (m.type === "refill") {
      // Добор: переспрашиваем модель по страницам, где цену пришлось взять из разметки.
      if (m.state === "start") {
        setStatusMsg(`Добор непроверенных: ${m.pages} страниц по ${m.rows} позициям `
                     + `(страницы из кэша, платим только за модель)`, "busy");
      } else if (m.state === "progress") {
        setStatusMsg(`Добор непроверенных: ${m.done} из ${m.pages} · уточнено ${m.fixed}`, "busy");
      } else {
        setStatusMsg(`Добор непроверенных завершён: уточнено ${m.fixed} из ${m.pages} страниц`,
                     m.fixed < m.pages ? "WARNING" : "INFO");
      }
    } else if (m.type === "queries_planned") {
      setStatusMsg(`Запросы от модели: ${m.by_model} из ${m.total} позиций` +
                   (m.by_model < m.total ? ` (остальные — по правилам)` : ""), "INFO");
    } else if (m.type === "find_link") {
      if (m.found) setStatusMsg(`Карточка с листинга: найдено ${m.found} · ${m.top || ""}`, "busy");
    } else if (m.type === "escalate") {
      setStatusMsg(`Эскалация ${m.url}: ${m.via || ""} → ${m.kind || "?"}`, m.kind && m.kind !== "blocked" && m.kind !== "empty" ? "busy" : "WARNING");
    } else if (m.type === "agent_start") {
      setStatusMsg(`Агент r${m.row}: «${m.name || ""}» — начинаю`, "INFO");
    } else if (m.type === "agent_step") {
      if (m.action) setStatusMsg(`Агент r${m.row} · шаг ${m.step} → ${m.action}${m.thought ? " · " + m.thought : ""}`, "busy");
    } else if (m.type === "agent_observation") {
      // наблюдение уходит в лог-консоль через обычный лог; здесь коротко в статус
      if (m.observation) setStatusMsg(`Агент r${m.row}: ${String(m.observation).slice(0, 90)}`, "INFO");
    } else if (m.type === "agent_final") {
      setStatusMsg(`Агент r${m.row}: завершил (${m.steps} шагов, цен ${m.prices})`, "INFO");
    } else if (m.type === "judge") {
      if (m.rejected) setStatusMsg(`Код-судья r${m.row}: отклонил «${m.found || ""}» — ${m.reason || ""}`, "WARNING");
    } else if (m.type === "fetch_done") {
      const hint = m.blocked ? " · повторный клик «Загрузить страницы» догрузит незагруженные" : "";
      setStatusMsg(`Загрузка завершена: успешно ${m.loaded}, заблокировано/ошибка ${m.blocked} из ${m.total}${hint}`, "INFO");
      toast(`Загружено ${m.loaded}/${m.total}` + (m.blocked ? `, не удалось ${m.blocked}` : ""), m.blocked ? "" : "ok");
    } else if (m.type === "history_saved") {
      setStatusMsg(`История сохранена: прогон #${m.run_id} · офферов ${m.offers}, dead-letter ${m.dead_letter}`, "INFO");
      toast(`История: прогон #${m.run_id} сохранён (${m.offers} офферов)`, "ok");
    } else if (m.type === "run_started") {
      // Новый прогон — новое состояние разбора и модели. Без этого запись о пакете прошлого,
      // прерванного прогона живёт вечно и тикает часами («пакет 2 из 13 — 4:31:23»).
      resetParseWait(); resetModelState(); parsedRows = 0;
      startElapsed(m.started_at);
    } else if (m.type === "items_parsed") {
      onItemsParsed(m);
    } else if (m.type === "parse_wait") {
      onParseWait(m);
    } else if (m.type === "parse_done") {
      onParseDone(m);
    } else if (m.type === "position_time") {
      setPositionTime(m.row, m.active_s);
    } else if (m.type === "run_done") {
      stopElapsed();
      resetParseWait();                            // прогон кончился — ожидающих пакетов быть не может
      hideRunning(); els.runBtn.disabled = false; setStatusMsg("Поиск завершён", "INFO");
    } else if (m.type === "price") {
      onPrice(m);
    } else if (m.type === "verdict") {
      onVerdict(m);
    } else if (m.type === "checkpoint") {
      // Промежуточное сохранение на диск: пользователь должен видеть, что работа не пропадёт.
      setStatusMsg(`Сохранено на диск (${m.reason}): готово ${m.done} из ${m.total}, `
                   + `осталось ${m.left} · ${m.dir}`, "INFO");
    } else if (m.type === "rationale") {
      // Комментарий-обоснование по позиции — тоже ответ модели: в отчёт он попадёт, но увидеть
      // его по ходу дела можно только здесь. В таблице места нет, поэтому — вкладка «Модель».
      if (!m.degraded) onModelAnswer({ ...m, stage: "rationale" });
    } else if (m.type === "progress") {
      setProgress(m);
    } else if (m.type === "stage_progress") {
      onStageProgress(m);
    } else if (m.type === "model_stream") {
      onModelStream(m);
    } else if (m.type === "model_answer") {
      onModelAnswer(m);
    } else if (m.type === "usage") {
      renderUsage(m.cumulative);
    } else if (m.type === "stopped") {
      stopElapsed();
      resetParseWait();                            // «Стоп» посреди пакета не должен оставлять фантом
      setStatusMsg("Остановлено пользователем", "WARNING"); toast("Поиск остановлен", ""); hideRunning();
      // Незавершённые позиции: честно «остановлено», а не «не найдено» — их не доискали.
      for (const r of Object.keys(posRows)) if (!verdictByRow[r]) setRowState(r, "остановлено", "warn");
    } else if (m.type === "manual") {
      onManualEvent(m);
    } else if (m.type === "site_update") {
      onSiteUpdate(m);
    } else if (m.type === "fetch_load") {
      onFetchLoad(m);
    } else if (m.type === "stage_load") {
      onStageLoad(m);
    } else if (m.type === "session_reset") {
      clearSessionUI();
    } else if (m.type === "selftest") {
      onSelfTest(m);
    }
  };
  ws.onclose = () => setTimeout(openEvents, 1500);
}
function levelPass(l) { return (LEVELS[l] || 0) >= (LEVELS[els.logLevel.value] || 0); }
function searchPass(m) { const q = els.logSearch.value.trim().toLowerCase();
  return !q || (m.msg + " " + m.name).toLowerCase().includes(q); }
// Срезать «хвост сверху» до LOG_DOM_MAX узлов. Первым элементом остаётся отметка об обрезке:
// молча исчезающие строки читаются как потеря данных, а это не потеря — это окно.
// onDrop нужен вкладке «Модель»: у её записей есть ссылка на узел, и она обязана погаснуть,
// иначе поток продолжит писать в элемент, которого уже нет в документе.
function trimConsole(el, max, onDrop) {
  let notice = el.firstElementChild;
  if (!notice || !notice.classList.contains("log-trunc")) notice = null;
  let extra = el.childElementCount - (notice ? 1 : 0) - max;
  if (extra <= 0) return;
  let dropped = 0;
  while (extra-- > 0) {
    const node = notice ? notice.nextElementSibling : el.firstElementChild;
    if (!node) break;
    if (onDrop) onDrop(node);
    node.remove();
    dropped += 1;
  }
  if (!dropped) return;
  if (!notice) {
    notice = document.createElement("div");
    notice.className = "log-trunc";
    notice.dataset.n = "0";
    el.prepend(notice);
  }
  const n = (Number(notice.dataset.n) || 0) + dropped;
  notice.dataset.n = String(n);
  notice.textContent = `… ранние строки скрыты (${n}) · показаны последние ${max}`;
}

function renderLog(m) {
  if (!levelPass(m.level) || !searchPass(m)) return;
  const line = document.createElement("div");
  line.className = "log-line log-" + m.level;
  line.innerHTML = `<span class="t">${m.ts}</span> <span class="lv">${m.level[0]}</span> ` +
    (m.run_id ? `<span class="rid">${m.run_id}</span> ` : "") +
    `<span class="nm">${esc(m.name.replace("search_agent.", ""))}</span>: ${esc(m.msg)}`;
  els.logConsole.appendChild(line);
  trimConsole(els.logConsole, LOG_DOM_MAX);
  if (autoscroll) els.logConsole.scrollTop = els.logConsole.scrollHeight;
}
function rerenderLogs() { els.logConsole.innerHTML = ""; logs.forEach(renderLog); }
// Пересборка консоли — до 2000 узлов; без задержки она повторялась на КАЖДЫЙ символ в поле
// фильтра и подвешивала ввод.
function debounce(fn, ms) {
  let t = null;
  return () => { clearTimeout(t); t = setTimeout(fn, ms); };
}
els.logLevel.onchange = rerenderLogs;
els.logSearch.oninput = debounce(rerenderLogs, 200);
els.logClear.onclick = () => { els.logConsole.innerHTML = ""; };
els.logAutoscroll.onclick = () => {
  autoscroll = !autoscroll;
  els.logAutoscroll.style.opacity = autoscroll ? "1" : ".5";
};

// ---- Вкладки ---------------------------------------------------------------
document.querySelectorAll(".tab").forEach((tab) => tab.onclick = () => {
  document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
  tab.classList.add("active");
  const which = tab.dataset.tab;
  $("#panel-dialog").hidden = which !== "dialog";
  $("#panel-queue").hidden = which !== "queue";
  $("#panel-trace").hidden = which !== "trace";
  $("#panel-model").hidden = which !== "model";
  els.traceTools.hidden = which !== "trace";
  els.modelTools.hidden = which !== "model";
});

// Свернуть все раскрытые товары (возвращает панель справа).
els.collapseAllBtn.onclick = () => {
  Array.from(expandedRows).forEach((r) => {
    const tr = posRows[r], nx = tr && tr.nextElementSibling;
    if (nx && nx.classList.contains("cand-row")) nx.remove();
    if (tr) tr.classList.remove("expanded");
  });
  expandedRows.clear(); updateExpandedMode();
};

// Режим «раскрыт товар»: скрыть панель справа, отдать таблице всю ширину (проблема №7).
function updateExpandedMode() {
  const any = expandedRows.size > 0;
  document.querySelector(".workspace").classList.toggle("expanded-mode", any);
  els.collapseAllBtn.hidden = !any;
  els.tableHint.textContent = any ? `раскрыто товаров: ${expandedRows.size}` : "";
}

// ---- Токен-счётчик + стоимость (₽) -----------------------------------------
// Сессия = «Всего сейчас» минус базовый снимок при загрузке страницы (сервер шлёт только
// накопительное). Так сессия честно считается без отдельного счётчика на бэкенде.
let usageBaseline = null;
async function loadUsage() {
  try {
    const c = await apiJson("/api/usage");
    usageBaseline = c;                       // базовый снимок для расчёта «Сессия»
    renderUsage(c);
  } catch (e) { setStatusMsg("Счётчик токенов недоступен: " + (e.message || e), "WARNING"); }
}
function renderUsage(c) {
  if (!c) return;
  els.cumIn.textContent = fmt(c.prompt_tokens); els.cumOut.textContent = fmt(c.completion_tokens);
  els.cumRub.textContent = rub(c.cost_rub);
  const b = usageBaseline || { prompt_tokens: 0, completion_tokens: 0, cost_rub: 0 };
  els.sesIn.textContent = fmt((c.prompt_tokens || 0) - (b.prompt_tokens || 0));
  els.sesOut.textContent = fmt((c.completion_tokens || 0) - (b.completion_tokens || 0));
  els.sesRub.textContent = rub((c.cost_rub || 0) - (b.cost_rub || 0));
}
// ---- время: общее по задаче и активное по позиции ---------------------------
// Общее время тикает на клиенте от абсолютной отметки старта, присланной сервером: так оно
// переживает перезагрузку страницы и переподключение сокета, а не начинает счёт заново.
let elapsedFrom = null, elapsedTimer = null;

function hms(sec) {
  if (sec === null || sec === undefined) return "—";
  const t = Math.max(0, Math.round(Number(sec) || 0));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s2 = t % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s2)}` : `${m}:${pad(s2)}`;
}

function renderElapsed() {
  if (!els.cElapsed) return;
  els.cElapsed.textContent = elapsedFrom ? hms(Date.now() / 1000 - elapsedFrom) : "0:00";
}

function startElapsed(startedAt) {
  if (!startedAt) return;
  elapsedFrom = startedAt;
  renderElapsed();
  if (elapsedTimer) clearInterval(elapsedTimer);
  elapsedTimer = setInterval(renderElapsed, 1000);
}

function stopElapsed() {
  if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; }
  renderElapsed();                       // финальное значение остаётся на экране
}

// ---- Состояние модели: лимиты API, пауза, восстановление --------------------
// Своя полоса, а не общая строка статуса: пауза модели обязана быть видна ОДНОВРЕМЕННО с
// ходом работы. Здесь же живой счётчик — «сколько уже ждём», иначе пауза и зависание
// выглядят одинаково.
let modelPausedAt = null, modelPauseTimer = null, modelPauseReason = "";

function renderModelState() {
  if (modelPausedAt === null) return;
  const waited = hms(Date.now() / 1000 - modelPausedAt);
  els.modelStateMsg.textContent =
    `Модель недоступна (${modelPauseReason}) — работа на паузе ${waited}, проверяю доступность. `
    + `Найденное не теряется: продолжим с того же места`;
}

function showModelState(text, ok) {
  els.modelState.hidden = false;
  els.modelState.className = "modelstate" + (ok ? " ok" : "");
  els.modelStateMsg.textContent = text;
}

function onLlmState(m) {
  if (m.event === "paused") {
    modelPauseReason = m.reason || "лимит API";
    modelPausedAt = Date.now() / 1000;
    els.modelState.hidden = false;
    els.modelState.className = "modelstate";
    renderModelState();
    if (!modelPauseTimer) modelPauseTimer = setInterval(renderModelState, 1000);
  } else if (m.event === "resumed") {
    modelPausedAt = null;
    if (modelPauseTimer) { clearInterval(modelPauseTimer); modelPauseTimer = null; }
    showModelState(`Модель снова доступна (пауза ${hms(m.waited_s || 0)}) — продолжаю`, true);
    setTimeout(() => { if (modelPausedAt === null) els.modelState.hidden = true; }, 8000);
  } else if (m.event === "backoff" && modelPausedAt === null) {
    // Одиночный отказ: показываем, но полосу не «залипаем» — она уйдёт со следующим успехом.
    showModelState(`Лимит API (${m.reason || "перегрузка"}) — жду ${m.delay} с и повторяю `
                   + `(попытка ${m.attempt})`, false);
  } else if (m.event === "still_paused") {
    modelPauseReason = m.reason || modelPauseReason;
  } else if (m.event === "limits_down") {
    setStatusMsg(`Снижаю нагрузку на модель: ${m.limits.rps}/с, ${m.limits.rpm}/мин, `
                 + `${m.limits.tpm} ток/мин (${m.reason || ""})`, "WARNING");
  } else if (m.event === "limits_up") {
    setStatusMsg(`Модель отвечает стабильно — поднимаю темп: ${m.limits.rps}/с, `
                 + `${m.limits.rpm}/мин`, "INFO");
  }
}

function resetModelState() {
  modelPausedAt = null; modelPauseReason = "";
  if (modelPauseTimer) { clearInterval(modelPauseTimer); modelPauseTimer = null; }
  if (els.modelState) els.modelState.hidden = true;
}

// Разбор наименований моделью идёт ДО поиска и занимает минуты: без этой строки таблица молчит
// с «ожидает», и работа выглядит зависшей. Здесь видно главное — думает модель или не отвечает:
// какой пакет из скольких, сколько секунд ждём и за сколько пришёл прошлый.
//
// Пишем в СВОЮ полосу, а не в общую строку статуса. Раньше тикающий раз в секунду таймер
// затирал весь ход работы, и прерванный прогон оставлял на экране «ждём ответ модели по
// пакету 2 из 13 — 4:31:23» на четыре с половиной часа.
let parsedRows = 0, waitingBatches = new Map(), parseTimer = null, lastParseTook = null;
let parseBatches = 0;

// Ответа по пакету не может не быть дольше этого: свой предел ожидания в query_llm — 450 с.
// Запись, пережившая его, — потерянное событие, а не работающая модель; молча снимаем.
const PARSE_WAIT_TTL = 520;

function parseStatus() {
  const now = Date.now() / 1000;
  for (const [batch, v] of [...waitingBatches]) {          // страховка от потерянного события
    if (now - v.since > PARSE_WAIT_TTL) {
      waitingBatches.delete(batch);
      setStatusMsg(`Пакет ${batch} разбора: ответ так и не пришёл — позиции пойдут по правилам`,
                   "WARNING");
    }
  }
  const parts = [];
  if (waitingBatches.size) {
    const longest = Math.max(...[...waitingBatches.values()].map((v) => now - v.since));
    const nums = [...waitingBatches.keys()].sort((a, b) => a - b).join(", ");
    const of = parseBatches ? ` из ${parseBatches}` : "";
    parts.push(`ждём ответ модели по пакету ${nums}${of} — ${hms(longest)}`);
  }
  if (parsedRows) parts.push(`разобрано ${parsedRows} позиций`);
  if (lastParseTook !== null) parts.push(`прошлый пакет за ${hms(lastParseTook)}`);
  if (!waitingBatches.size && parseTimer) { clearInterval(parseTimer); parseTimer = null; }
  if (!parts.length) { els.parseState.hidden = true; return; }
  els.parseState.hidden = false;
  els.parseStateMsg.textContent =
    "Разбор наименований моделью (до начала поиска): " + parts.join(" · ");
}

function onParseWait(m) {
  parseBatches = m.batches || parseBatches;
  if (m.state === "sent") {
    waitingBatches.set(m.batch, { since: Date.now() / 1000, rows: m.rows });
    if (!parseTimer) parseTimer = setInterval(parseStatus, 1000);   // счёт секунд ожидания
  } else {
    // Любой НЕ-«sent» — терминальный: done, timeout, error, cancelled. Сервер обязан прислать
    // ровно один такой на каждый sent (query_llm.py), иначе запись висела бы вечно.
    waitingBatches.delete(m.batch);
    if (m.state === "done") lastParseTook = m.took_s;
    if (m.state === "timeout") {
      setStatusMsg(`Пакет ${m.batch} из ${m.batches} не ответил вовремя (${m.rows} позиций) — дроблю на части`, "WARNING");
    } else if (m.state === "error") {
      setStatusMsg(`Пакет ${m.batch} из ${m.batches} не разобран: ${m.error || "сбой"} — `
                   + `эти позиции пойдут по правилам`, "WARNING");
    } else if (m.state === "cancelled") {
      setStatusMsg(`Разбор прерван на пакете ${m.batch} из ${m.batches}`, "WARNING");
    }
    if (!waitingBatches.size && parseTimer) { clearInterval(parseTimer); parseTimer = null; }
  }
  parseStatus();
}

function resetParseWait() {
  waitingBatches.clear(); lastParseTook = null; parseBatches = 0;
  if (parseTimer) { clearInterval(parseTimer); parseTimer = null; }
  if (els.parseState) els.parseState.hidden = true;
  // Ответы модели относятся к прежним позициям: с новым файлом они только путали бы.
  modelLog = []; traceStream = {};
  if (els.modelConsole) els.modelConsole.innerHTML = "";
}

function onItemsParsed(m) {
  parsedRows += (m.rows || []).length;
  parseStatus();
}

function onParseDone(m) {
  // Итог разбора виден пользователю, а не только в логе: сколько позиций пойдёт с разбором
  // модели, а сколько — по правилам.
  const rules = Math.max(0, (m.total || 0) - (m.parsed || 0));
  setStatusMsg(`Разбор наименований завершён: моделью ${m.parsed} из ${m.total}`
               + (rules ? `, остальные ${rules} — по правилам` : ""), "INFO");
  parseStatus();
}

// Активное время позиции приходит готовым числом, когда позиция закрыта: досчитывать его на
// клиенте нечем — из времени вычтено стояние в очередях, а это знает только движок.
function setPositionTime(row, activeS) {
  const tr = posRows[row];
  if (!tr) return;
  const cell = tr.querySelector(".c-time");
  if (cell) cell.textContent = hms(activeS);
}

function fmt(n) { return (n || 0).toLocaleString("ru-RU"); }
function rub(n) { return (n || 0).toLocaleString("ru-RU", { maximumFractionDigits: 2 }) + " ₽"; }
els.usageReset.onclick = async () => {
  if (!confirm("Обнулить накопительный счётчик токенов и стоимость?")) return;
  try {
    const c = await postJson("/api/usage/reset");
    usageBaseline = c; renderUsage(c);
  } catch (e) { toast(String(e.message || e), "err"); }
};

// ---- Модалка выбора модели -------------------------------------------------
let modelCatalog = [];
// Проверка «есть ли токены» делается ОДИН раз, при загрузке страницы: loadModels() зовётся и
// при каждом открытии модалки, и там открывать её повторно незачем.
let askTokensOnce = true;
els.connectBtn.onclick = () => { els.modal.hidden = false; loadModels(); };
els.modalClose.onclick = () => { els.modal.hidden = true; };
els.modal.onclick = (e) => { if (e.target === els.modal) els.modal.hidden = true; };

function modelLabel(m) {
  const price = (m.price_in + m.price_out).toLocaleString("ru-RU", { maximumFractionDigits: 0 });
  const tags = (m.tags && m.tags.length) ? " [" + m.tags.join(",") + "]" : "";
  return `${m.id} · ${price} ₽/млн${tags}`;
}
function renderModelMeta(m) {
  if (!m) { els.modelMeta.textContent = ""; return; }
  const ctx = m.context_length ? (m.context_length / 1000).toLocaleString("ru-RU") + "k" : "?";
  els.modelMeta.textContent = `вход ${m.price_in} ₽/млн · выход ${m.price_out} ₽/млн · контекст ${ctx}`
    + ((m.tags && m.tags.length) ? " · " + m.tags.join(", ") : "");
}
async function loadModels() {
  els.formError.hidden = true;
  try {
    const d = await apiJson("/api/models");
    modelCatalog = d.models || [];
    els.modelSelect.innerHTML = "";
    modelCatalog.forEach((m) => {
      const opt = document.createElement("option");
      opt.value = m.id; opt.textContent = modelLabel(m);
      els.modelSelect.appendChild(opt);
    });
    const cur = d.current || (modelCatalog[0] && modelCatalog[0].id);
    if (cur) els.modelSelect.value = cur;
    onModelChange();
    if (d.base_url) els.modelBaseUrl.value = d.base_url;
    els.tokenState.textContent = d.token_present ? "токен модели сохранён" : "токен модели не задан";
    setSerperState(d.serper_key_present);
    modelReady = !!(d.current && d.token_present);
    syncChatEnabled();                                // чат доступен, когда модель готова
    if (d.current) setStatus(d.token_present ? "online" : "local",
      (d.token_present ? "Готово · " : "Нет токена · ") + d.current);
    // Первый запуск: скрипты запуска токенов больше не спрашивают, поэтому спросить обязан
    // интерфейс — иначе человек увидит рабочий с виду экран, нажмёт «Найти» и получит невнятный
    // отказ. Модалка ничего не блокирует: её можно закрыть и работать без ключей.
    if (askTokensOnce && (!d.token_present || !d.serper_key_present)) {
      els.modal.hidden = false;
      setStatusMsg("Не заданы токены — впишите их в окне «Модель и поиск»", "WARNING");
    }
    askTokensOnce = false;
  } catch (err) {
    els.formError.hidden = false; els.formError.textContent = String(err.message || err);
  }
}
// Ключ поиска задан или нет. Без него поиск вообще не стартует, поэтому «не задан» —
// это предупреждение, а не нейтральная подпись.
function setSerperState(present) {
  if (!els.serperState) return;
  els.serperState.textContent = present ? "ключ поиска сохранён" : "⚠ ключ поиска не задан";
  els.serperState.classList.toggle("warn", !present);
}

function onModelChange() {
  const m = modelCatalog.find((x) => x.id === els.modelSelect.value);
  renderModelMeta(m);
  if (m && m.base_url) els.modelBaseUrl.value = m.base_url;   // автозаполнение URL
}
els.modelSelect.onchange = onModelChange;

els.form.onsubmit = async (e) => {
  e.preventDefault();
  els.formError.hidden = true;
  const serperKey = els.serperKey ? els.serperKey.value.trim() : "";
  const payload = {
    model: els.modelSelect.value,
    base_url: els.modelBaseUrl.value.trim(),
    token: els.modelToken.value.trim(),      // пусто → сервер оставит сохранённый
    serper_key: serperKey,                   // пусто → сервер оставит сохранённый
  };
  els.submitBtn.disabled = true; els.submitBtn.textContent = "Сохранение…";
  try {
    const data = await postJson("/api/model/select", payload);
    els.modelToken.value = "";               // не держим токены в полях
    if (els.serperKey) els.serperKey.value = "";
    els.tokenState.textContent = data.token_present ? "токен модели сохранён" : "токен модели не задан";
    setSerperState(data.serper_key_present);
    modelReady = !!(data.model && data.token_present);
    syncChatEnabled();
    setStatus(data.token_present ? "online" : "local",
      (data.token_present ? "Готово · " : "Нет токена · ") + data.model);
    // Замена ключа поиска — самостоятельное событие: пользователь пришёл сюда именно ради него,
    // и «Модель сохранена» на такой вопрос не отвечает.
    toast(serperKey ? "Ключ поиска сохранён и уже действует" : "Модель сохранена: " + data.model, "ok");
    setTimeout(() => { els.modal.hidden = true; }, 600);
  } catch (err) {
    els.formError.hidden = false; els.formError.textContent = String(err.message || err);
  } finally {
    els.submitBtn.disabled = false; els.submitBtn.textContent = "Сохранить";
  }
};

// ---- Проверка системы ------------------------------------------------------
// «Готово» в шапке означает лишь «токен записан». Когда прогон идёт медленно, нужен ответ на
// другой вопрос: отвечает ли поиск, отвечает ли модель и с какой скоростью она генерирует.
// Это единственное место, где ответ получается реальным запросом, а не по настройкам.
els.selfTestBtn.onclick = () => { els.testModal.hidden = false; runSelfTest(); };
els.testClose.onclick = () => { els.testModal.hidden = true; };
els.testRun.onclick = () => runSelfTest();
els.testModal.onclick = (e) => { if (e.target === els.testModal) els.testModal.hidden = true; };

function setCheck(el, detailEl, state, text) {
  el.className = "check " + state;                     // wait | ok | fail
  el.querySelector(".check-dot").textContent = state === "ok" ? "✅" : state === "fail" ? "❌" : "⏳";
  detailEl.textContent = text;
}

async function runSelfTest() {
  els.testError.hidden = true;
  els.testRun.disabled = true; els.testRun.textContent = "Проверяю…";
  setCheck(els.checkSearch, els.checkSearchDetail, "wait", "запрос в поисковую систему…");
  setCheck(els.checkModel, els.checkModelDetail, "wait", "ждём ответа модели…");
  setCheck(els.checkSpeed, els.checkSpeedDetail, "wait", "замер генерации…");
  els.speedBox.hidden = true;
  try {
    const d = await postJson("/api/selftest", {});
    renderSelfTest(d);
    toast(d.passed ? "Проверка пройдена" : "Проверка нашла проблемы", d.passed ? "ok" : "err");
  } catch (err) {
    els.testError.hidden = false; els.testError.textContent = String(err.message || err);
    ["checkSearch", "checkModel", "checkSpeed"].forEach((k) => {
      if (els[k].className.includes("wait"))
        setCheck(els[k], els[k + "Detail"], "fail", "проверка не выполнена");
    });
  } finally {
    els.testRun.disabled = false; els.testRun.textContent = "Проверить ещё раз";
  }
}

function renderSelfTest(d) {
  const s = d.search || {}, m = d.model || {};
  setCheck(els.checkSearch, els.checkSearchDetail, s.ok ? "ok" : "fail",
    s.ok ? `${s.backend}: ${s.results} результатов за ${s.took_s} с` +
           (s.sample && s.sample.length ? ` · ${s.sample.join(", ")}` : "") +
           (s.delay_s ? ` (включая паузу ${s.delay_s[0]}–${s.delay_s[1]} с по настройке)` : "")
      : `${s.backend || "поиск"}: ${s.error || "нет ответа"}`);
  setCheck(els.checkModel, els.checkModelDetail, m.ok ? "ok" : "fail",
    m.ok ? `${m.model}: короткий вопрос — ответ за ${m.ping_s} с${m.answer ? ` («${m.answer}»)` : ""}`
      : `${m.model || "модель не выбрана"}: ${m.error || "нет ответа"}`);
  setCheck(els.checkSpeed, els.checkSpeedDetail, m.ok ? "ok" : "fail",
    m.ok ? `${m.tok_s || "?"} токенов/с${m.tok_s_approx ? " (оценка)" : ""} · ` +
           `${m.completion_tokens || "?"} токенов за ${m.gen_s} с · первый символ через ${m.ttft_s} с` +
           (m.reasoning_tokens ? ` (из них ≈${m.reasoning_tokens} токенов — скрытые рассуждения модели: `
             + `именно они дают паузу перед ответом)` : "")
      : "не измеряли — модель не ответила");
  renderSpeedSettings(d.speed || {});
}

const SPEED_LABELS = [
  ["stage_positions", "Позиций одновременно на этапе", (v, sp) => v +
    (Object.keys(sp.stage_positions_by_kind || {}).length
      ? " (" + Object.entries(sp.stage_positions_by_kind).map(([k, n]) => `${k}: ${n}`).join(", ") + ")" : "")],
  ["search_backend", "Поисковая система", (v, sp) => `${v}, ${sp.search_concurrency} запрос(ов) одновременно` +
    (sp.search_delay_range && sp.search_delay_range.length
      ? `, пауза ${sp.search_delay_range[0]}–${sp.search_delay_range[1]} с` : "")],
  ["fetch_backend", "Загрузка сайтов", (v, sp) => `${v}, ${sp.fetch_concurrency} вкладок(и), ` +
    `пауза на домен ${(sp.fetch_delay_range || []).join("–")} с, таймаут ${sp.fetch_timeout} с`],
  ["model_levels", "Вызовов модели одновременно", (v, sp) => `${v.join(" → ")} (снижается при таймауте), ` +
    `таймаут вызова ${sp.model_timeout} с, транспорт ${sp.llm_timeout} с`],
  ["parse_batch", "Разбор наименований", (v) => `пакет ${v} позиций, последовательно, до начала поиска`],
  ["queries_per_item", "Запросов на позицию", (v) => String(v)],
];

function renderSpeedSettings(sp) {
  els.speedGrid.innerHTML = "";
  SPEED_LABELS.forEach(([key, label, fmt]) => {
    if (sp[key] === undefined) return;
    const k = document.createElement("div"); k.className = "speed-k"; k.textContent = label;
    const v = document.createElement("div"); v.className = "speed-v"; v.textContent = fmt(sp[key], sp);
    els.speedGrid.appendChild(k); els.speedGrid.appendChild(v);
  });
  const warns = sp.warnings || [];
  els.speedWarn.hidden = warns.length === 0;
  els.speedWarn.innerHTML = warns.map((w) => `⚠ ${esc(w)}`).join("<br />");
  els.speedBox.hidden = false;
}

// Ход проверки приходит событиями — окно не должно молчать, пока идут запросы.
function onSelfTest(m) {
  if (m.step === "search" && m.state === "start")
    setCheck(els.checkSearch, els.checkSearchDetail, "wait", "запрос в поисковую систему…");
  else if (m.step === "search" && m.state !== "start")
    setCheck(els.checkSearch, els.checkSearchDetail, m.state === "ok" ? "ok" : "fail",
      m.state === "ok" ? `${m.backend}: ${m.results} результатов за ${m.took_s} с`
        : `${m.backend || "поиск"}: ${m.error || "нет ответа"}`);
  else if (m.step === "model_ping" && m.state === "ok")
    setCheck(els.checkModel, els.checkModelDetail, "ok",
      `ответ за ${m.ping_s} с${m.answer ? ` («${m.answer}»)` : ""} · меряю скорость…`);
}

// ---- Диалог: чат-исследователь ---------------------------------------------
// Агент сам ищет, читает страницы и ПРОВЕРЯЕТ источники данных; каждое утверждение в ответе
// подкреплено ссылкой [Sn] на реально прочитанный источник. Шаги показываем чипами по ходу
// дела — «ничего не происходит» недопустимо.
let chatId = null;                                    // текущий диалог
let chatSources = {};                                 // sid → {url, domain, title}
let curBubble = null, stepsBox = null;

function openChat() {
  if (chatWs) { try { chatWs.close(); } catch (_) {} }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  chatWs = new WebSocket(`${proto}://${location.host}/ws/chat`);
  chatWs.onmessage = onChatMsg;
  chatWs.onclose = () => { if (streaming) finishChat(); setTimeout(openChat, 2000); };
}

function onChatMsg(ev) {
  const m = JSON.parse(ev.data);
  if (m.type === "start") {
    chatId = m.chat_id; streaming = true;
    els.chatSend.disabled = true; els.chatStop.hidden = false;
    stepsBox = document.createElement("div");
    stepsBox.className = "steps";
    els.messages.appendChild(stepsBox); scrollMsgs();
  } else if (m.type === "chat_step") {
    addStep(stepIcon(m.tool), `${m.tool}: ${stepArgs(m.args)}`, m.thought);
  } else if (m.type === "chat_source") {
    chatSources[m.sid] = m;
    addStep("🔗", `[${m.sid}] ${m.domain || m.url}`);
  } else if (m.type === "chat_dossier") {
    addStep("🧪", `${m.domain}: ${m.verdict}${periodOf(m)}`);
  } else if (m.type === "chat_observation") {
    setStatusMsg(`Диалог · ${m.tool}: ${(m.text || "").split("\n")[0].slice(0, 120)}`, "busy");
  } else if (m.type === "chat_answer") {
    // сам текст придёт в done — здесь только отметка, что итог сформирован
    setStatusMsg("Диалог: ответ готов", "INFO");
  } else if (m.type === "question") {
    addQuestion(m.text, m.options || []);
  } else if (m.type === "done") {
    (m.sources || []).forEach((s) => { chatSources[s.sid] = s; });
    if (m.answer) addAnswer(m.answer, m.dossiers || []);
    if (m.cumulative) renderUsage(m.cumulative);
    finishChat();
  } else if (m.type === "error") {
    addMsg("assistant", "⚠ " + m.error).classList.add("error");
    finishChat();
  }
}

const STEP_ICONS = {
  web_search: "🔍", find_sources: "🔍", read_page: "📄", probe_source: "🧪",
  extract_prices: "💰", inspect_page: "🔬", find_product_link: "🧭", escalate_fetch: "🛡",
};
function stepIcon(tool) { return STEP_ICONS[tool] || "▸"; }
function stepArgs(args) {
  const a = args || {};
  return String(a.query || a.url || a.need || a.name || "").slice(0, 90);
}
function periodOf(d) {
  const p = d.period || {};
  return p.seen ? ` · ${p.seen}` : "";
}
function addStep(icon, text, thought) {
  if (!stepsBox) return;
  const d = document.createElement("div");
  d.className = "step";
  d.textContent = `${icon} ${text}`;
  if (thought) d.title = thought;
  stepsBox.appendChild(d); scrollMsgs();
}

function addAnswer(text, dossiers) {
  const d = addMsg("assistant", "");
  d.innerHTML = renderAnswer(text);
  if ((dossiers || []).length) d.appendChild(renderPromote(dossiers));
  scrollMsgs();
  return d;
}

// Ссылки [Sn] → кликабельные; markdown не рендерим (кроме таблиц-«как есть») — только escape.
function renderAnswer(text) {
  const esc = escapeHtml(text);
  return esc.replace(/\[S(\d+)\]/g, (full, n) => {
    const s = chatSources["S" + n];
    if (!s) return `<span class="cite bad" title="источник не найден">${full}</span>`;
    return `<a class="cite" href="${escapeHtml(s.url)}" target="_blank" rel="noopener" ` +
           `title="${escapeHtml(s.title || s.url)}">[S${n}]</a>`;
  }).replace(/\n/g, "<br />");
}

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

// Блок «в профиль поиска»: проверенные источники поднимаем в рейтинге ТАБЛИЧНОГО поиска.
function renderPromote(dossiers) {
  const good = dossiers.filter((d) => d.verdict === "годится" || d.verdict === "частично");
  const box = document.createElement("div");
  box.className = "promote";
  if (!good.length) return box;
  box.innerHTML = `<div class="promote-head">Добавить проверенные источники в профиль поиска ` +
                  `(поднимутся в рейтинге при поиске по таблице):</div>`;
  const list = document.createElement("div"); list.className = "promote-list";
  good.forEach((d) => {
    const id = "pr_" + Math.random().toString(36).slice(2, 8);
    const lab = document.createElement("label");
    lab.innerHTML = `<input type="checkbox" id="${id}" value="${escapeHtml(d.domain || "")}" checked /> ` +
                    `${escapeHtml(d.domain || d.url)} <span class="muted">· ${escapeHtml(d.verdict)}</span>`;
    list.appendChild(lab);
  });
  box.appendChild(list);
  const btn = document.createElement("button");
  btn.className = "btn small"; btn.textContent = "В профиль поиска";
  btn.onclick = async () => {
    const domains = [...list.querySelectorAll("input:checked")].map((i) => i.value).filter(Boolean);
    if (!domains.length) return;
    btn.disabled = true;
    try {
      const r = await fetch("/api/sources/promote", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ domains }),
      });
      const data = await r.json();
      if (!data.ok) throw new Error(data.error || "не сохранено");
      toast(`В профиль поиска добавлено: ${(data.added || []).join(", ") || "—"}`, "ok");
      loadProfile();
    } catch (err) {
      toast("Не удалось сохранить: " + (err.message || err), "err");
      btn.disabled = false;
    }
  };
  box.appendChild(btn);
  return box;
}

function addQuestion(text, options) {
  const d = addMsg("assistant", "");
  d.classList.add("question");
  d.innerHTML = `<div class="q-text">❓ ${escapeHtml(text)}</div>`;
  if (options.length) {
    const row = document.createElement("div"); row.className = "q-options";
    options.forEach((o) => {
      const b = document.createElement("button");
      b.className = "btn small ghost"; b.textContent = o;
      b.onclick = () => { els.chatInput.value = o; sendChat(); };
      row.appendChild(b);
    });
    d.appendChild(row);
  }
  scrollMsgs();
}

function finishChat() {
  curBubble = null; stepsBox = null; streaming = false;
  els.chatStop.hidden = true;
  syncChatEnabled();
  loadChats();
}

// Ввод в чате разблокирован, когда модель готова (выбрана + есть токен).
function syncChatEnabled() {
  const ready = modelReady && !streaming;
  els.chatInput.disabled = !ready;
  els.chatSend.disabled = !ready;
  els.chatInput.placeholder = modelReady
    ? "Спросите про цены, сравнение по обзорам или где взять данные…"
    : "Сначала выберите модель (кнопка «Модель» вверху)";
}

function addMsg(role, text) {
  const hint = els.messages.querySelector(".hint"); if (hint) hint.remove();
  const d = document.createElement("div"); d.className = "msg " + role; d.textContent = text;
  els.messages.appendChild(d); scrollMsgs(); return d;
}
function scrollMsgs() { els.messages.scrollTop = els.messages.scrollHeight; }

function sendChat() {
  const t = els.chatInput.value.trim();
  if (!t || streaming || !chatWs || chatWs.readyState !== WebSocket.OPEN) return;
  if (!modelReady) { toast("Сначала выберите модель и укажите токен (кнопка «Модель»)", "err"); return; }
  addMsg("user", t);
  chatWs.send(JSON.stringify({ content: t, chat_id: chatId }));
  els.chatInput.value = "";
}
els.chatSend.onclick = sendChat;
els.chatInput.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); } });

els.chatStop.onclick = async () => {
  els.chatStop.disabled = true;
  try { await fetch(`/api/chats/${chatId || "current"}/stop`, { method: "POST" }); }
  catch (_) { /* сокет всё равно вернёт итог */ }
  els.chatStop.disabled = false;
};

els.chatNew.onclick = async () => {
  const r = await fetch("/api/chats", { method: "POST" });
  const data = await r.json();
  chatId = data.chat_id; chatSources = {};
  els.messages.innerHTML = '<div class="hint">Новый диалог. Спросите что угодно.</div>';
  loadChats();
};

els.chatDelete.onclick = async () => {
  if (!chatId || !confirm("Удалить этот диалог вместе с историей?")) return;
  await fetch(`/api/chats/${chatId}`, { method: "DELETE" });
  chatId = null; chatSources = {};
  els.messages.innerHTML = '<div class="hint">Диалог удалён. Начните новый.</div>';
  loadChats();
};

els.chatList.onchange = () => { if (els.chatList.value) openChatById(els.chatList.value); };

async function loadChats() {
  try {
    const data = await (await fetch("/api/chats")).json();
    const list = data.chats || [];
    if (!chatId) chatId = data.current || null;
    els.chatList.innerHTML = list.length
      ? list.map((c) => `<option value="${c.id}"${c.id === chatId ? " selected" : ""}>` +
                        `${escapeHtml(c.title || "без названия")} (${c.messages})</option>`).join("")
      : '<option value="">— диалогов пока нет —</option>';
  } catch (_) { /* список истории не критичен */ }
}

async function openChatById(id) {
  try {
    const data = await (await fetch(`/api/chats/${id}`)).json();
    chatId = data.chat_id; chatSources = {};
    (data.sources || []).forEach((s) => { chatSources[s.sid] = s; });
    els.messages.innerHTML = "";
    (data.messages || []).forEach((m) => {
      if (m.role === "user") addMsg("user", m.content);
      else if (m.kind === "question") addQuestion(m.content, (m.meta || {}).options || []);
      else addAnswer(m.content, data.dossiers || []);
    });
    if (!(data.messages || []).length) {
      els.messages.innerHTML = '<div class="hint">Диалог пуст.</div>';
    }
    if (data.pending_question) setStatusMsg("Агент ждёт ответа: " + data.pending_question, "INFO");
  } catch (err) {
    toast("Не удалось открыть диалог: " + (err.message || err), "err");
  }
}

// ---- Режим открытия сайтов (анти-блок уровни) ------------------------------
// Пресеты зеркалят search_agent/config.py FETCH_PRESETS. Незаданные в пресете поля
// сохраняют дефолт (см. DEFAULT_MODE). UI — источник истины: на прогон шлём явные поля.
const DEFAULT_MODE = {
  _v: 4,                                              // версия схемы настроек (см. loadMode)
  preset: "basic", backend: "browser", real_chrome: true, anti_detect: false, headed: false,
  persistent_profile: false, warmup: true, human_input: false, wait_networkidle: true,
  http_first: true,
  delay_min: 8, delay_max: 25, timeout: 30, top_n_fetch: 0, concurrency: 2,
  retry_on_block: 1, cooldown_min: 20, cooldown_max: 40,
  _muted: {},                                         // погашенные зависимые: чем были до гашения
};
const FETCH_PRESETS = {
  fast:   { backend: "http",    real_chrome: true, anti_detect: false, headed: false, persistent_profile: false, warmup: false, human_input: false, wait_networkidle: true, http_first: true,  delay_min: 2,  delay_max: 6 },
  basic:  { backend: "browser", real_chrome: true, anti_detect: false, headed: false, persistent_profile: false, warmup: true,  human_input: false, wait_networkidle: true, http_first: true,  delay_min: 8,  delay_max: 25 },
  strong: { backend: "browser", real_chrome: true, anti_detect: true,  headed: false, persistent_profile: true,  warmup: true,  human_input: true,  wait_networkidle: true, http_first: true,  delay_min: 8,  delay_max: 25 },
  max:    { backend: "browser", real_chrome: true, anti_detect: true,  headed: true,  persistent_profile: true,  warmup: true,  human_input: true,  wait_networkidle: true, http_first: false, delay_min: 12, delay_max: 30 },
};
const PRESET_LABEL = { fast: "Быстрый", basic: "Базовый", strong: "Усиленный", max: "Максимальный", custom: "Свой" };
const PRESET_DESC = {
  fast: "httpx без браузера, короткие паузы. Лёгкие сайты, когда альтернатив много.",
  basic: "Chrome headless + прогрев + ожидание JS + stealth. Прежний режим (~57%).",
  strong: "+ анти-детект (patchright), постоянный профиль, человеческий ввод, повтор. Без окна.",
  max: "+ графический режим (реальное окно) = эксперимент (~79%, берёт Ozon/Леруа). Длинные паузы.",
  custom: "Ручная настройка приёмов и параметров ниже.",
};
const MODE_KEY = "webui_fetchmode";
const FLAGS = ["real_chrome", "anti_detect", "headed", "persistent_profile", "warmup", "human_input", "wait_networkidle", "http_first"];
const NUMS = ["delay_min", "delay_max", "timeout", "top_n_fetch", "concurrency", "retry_on_block", "cooldown_min", "cooldown_max"];
// Зеркало search_agent/config.py FETCH_DEPS: приём → что обязано быть включено, иначе он не
// работает вовсе. Псевдо-флаг "browser" = backend === "browser". Связь односторонняя: снятие
// предпосылки гасит зависимые; снятие зависимого предпосылку не трогает.
const FETCH_DEPS = {
  real_chrome: ["browser"], anti_detect: ["browser"], headed: ["browser"],
  persistent_profile: ["browser"], warmup: ["browser"], human_input: ["browser"],
  wait_networkidle: ["browser"], http_first: ["browser"],
};
let fetchMode = loadMode();

function loadMode() {
  let saved; try { saved = JSON.parse(localStorage.getItem(MODE_KEY) || "null"); } catch (_) {}
  // Смысл поля «Кандидатов на товар» изменился: было «сколько из отобранных грузить» (в главном
  // пути не работало), стало «сколько кандидатов выдачи вообще рассматривать, 0 — все». Старое
  // сохранённое число молча урезало бы выдачу — сбрасываем его один раз.
  if (saved && ![2, 3, 4].includes(saved._v)) { delete saved.top_n_fetch; saved._v = 4; }
  // v3: появились связанные группы. Старый blob мог хранить несогласованный набор (например
  // httpx + анти-детект) — приводим к согласованному при загрузке, а не молча отправляем на сервер.
  if (saved && saved._v !== 3 && saved._v !== 4) { saved._v = 4; saved._muted = {}; }
  // v4: появился «Сначала без браузера». В старом blob поля нет, и дефолт (true) противоречил бы
  // пресету «Максимальный», где лёгкий путь выключен намеренно. Поэтому у сохранённого пресета
  // берём его значение, а «Свой» получает общий дефолт.
  if (saved && saved._v === 3) {
    saved._v = 4;
    const p = FETCH_PRESETS[saved.preset];
    saved.http_first = p ? p.http_first : DEFAULT_MODE.http_first;
  }
  const m = Object.assign({}, DEFAULT_MODE, saved || {});
  m._muted = Object.assign({}, m._muted);
  return m;
}

// Включена ли предпосылка. "browser" — не галочка состояния, а выбранный бэкенд.
function depOn(name) {
  return name === "browser" ? fetchMode.backend === "browser" : !!fetchMode[name];
}

/** Синхронизировать зависимые приёмы: без предпосылки они гаснут и блокируются.
 *  Прежний выбор помним в `_muted` и возвращаем, когда предпосылку включают обратно —
 *  иначе переключение туда-обратно каждый раз стирало бы настройку. */
function applyDeps() {
  for (const [flag, needs] of Object.entries(FETCH_DEPS)) {
    const ok = needs.every(depOn);
    const why = "заблокировано: нужен " + (needs.includes("browser") ? "браузер" : needs.join(", "));
    if (!ok) {
      if (fetchMode[flag]) { fetchMode._muted[flag] = true; fetchMode[flag] = false; }
    } else if (flag in fetchMode._muted) {
      fetchMode[flag] = fetchMode._muted[flag];
      delete fetchMode._muted[flag];
    }
    const el = document.querySelector(`[data-flag="${flag}"]`);
    if (!el) continue;
    el.checked = !!fetchMode[flag];
    el.disabled = !ok;
    const lbl = el.closest("label");
    if (lbl) { lbl.classList.toggle("dep-off", !ok); lbl.title = ok ? "" : why; }
  }
  document.querySelectorAll("[data-group-requires]").forEach((g) => {
    g.classList.toggle("dep-off", !depOn(g.dataset.groupRequires));
  });
  // Числовые связи. «Параллельно доменов» при человеческом вводе всегда 1 (один «человек» —
  // один курсор): раньше интерфейс показывал 8, а движок молча брал 1.
  const cLbl = document.querySelector('[data-num-disabled-by="human_input"]');
  const cEl = document.querySelector('[data-num="concurrency"]');
  if (cEl && cLbl) {
    const forced = !!fetchMode.human_input;
    cEl.disabled = forced;
    if (forced) cEl.value = 1;
    cLbl.classList.toggle("dep-off", forced);
    cLbl.querySelector(".dep-note").textContent = forced ? "— 1 из-за человеческого ввода" : "";
  }
  // Cooldown нужен только если есть повторы при мягком блоке.
  const rLbl = document.querySelector('[data-num-requires="retry_on_block"]');
  if (rLbl) {
    const off = !(Number(fetchMode.retry_on_block) > 0);
    rLbl.classList.toggle("dep-off", off);
    rLbl.querySelectorAll("input").forEach((i) => { i.disabled = off; });
    rLbl.querySelector(".dep-note").textContent = off ? "— повторов нет, пауза не применяется" : "";
  }
}
function saveMode() { try { localStorage.setItem(MODE_KEY, JSON.stringify(fetchMode)); } catch (_) {} }

function applyPreset(name) {
  fetchMode.preset = name;
  if (name !== "custom" && FETCH_PRESETS[name]) {
    fetchMode._muted = {};              // пресет задаёт приёмы явно — прежние «погашенные» забываем
    Object.assign(fetchMode, FETCH_PRESETS[name]);
  }
  syncModeUI();
}
function syncModeUI() {                                   // state → контролы + кнопка
  document.querySelectorAll("#modeSeg .seg-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.preset === fetchMode.preset));
  $("#modeDesc").textContent = PRESET_DESC[fetchMode.preset] || "";
  const bk = document.querySelector('[data-flag="backend"]'); if (bk) bk.checked = fetchMode.backend === "browser";
  FLAGS.forEach((f) => { const el = document.querySelector(`[data-flag="${f}"]`); if (el) el.checked = !!fetchMode[f]; });
  NUMS.forEach((n) => { const el = document.querySelector(`[data-num="${n}"]`); if (el) el.value = fetchMode[n]; });
  applyDeps();                                           // связанные приёмы: гасим и блокируем
  const lbl = PRESET_LABEL[fetchMode.preset] || fetchMode.preset;
  els.modeBtn.title = "Режим открытия: " + lbl + " — " + (PRESET_DESC[fetchMode.preset] || "");
  els.modeBtn.classList.toggle("mode-hot", fetchMode.preset === "max" || fetchMode.preset === "strong");
}
function onManualChange() { fetchMode.preset = "custom"; saveMode(); syncModeUI(); }

document.querySelectorAll("#modeSeg .seg-btn").forEach((b) => b.onclick = () => { applyPreset(b.dataset.preset); saveMode(); });
document.querySelector('[data-flag="backend"]').addEventListener("change", (e) => { fetchMode.backend = e.target.checked ? "browser" : "http"; onManualChange(); });
FLAGS.forEach((f) => document.querySelector(`[data-flag="${f}"]`)
  .addEventListener("change", (e) => { fetchMode[f] = e.target.checked; onManualChange(); }));
NUMS.forEach((n) => document.querySelector(`[data-num="${n}"]`)
  .addEventListener("change", (e) => { const v = parseFloat(e.target.value); if (!isNaN(v)) fetchMode[n] = v; onManualChange(); }));

els.modeBtn.onclick = () => { syncModeUI(); syncCleanUI(); els.modeModal.hidden = false; };
els.modeClose.onclick = () => { els.modeModal.hidden = true; };
// ---- Профиль поиска (хранится на сервере: переживает перезапуск и смену браузера) ----
// Поля главного экрана (аффикс, тумблер разбора) — это разовая правка «на сейчас»; профиль же
// подставляется при каждом запуске, поэтому храним его не в localStorage, а в базе пользователя.
const csv2list = (s) => String(s || "").split(",").map((x) => x.trim()).filter(Boolean);

function fillProfile(p) {
  $("#profAffix").value = p.affix || "";
  $("#profLlm").checked = !!p.use_llm_queries;
  $("#profSecondPage").checked = p.second_page_if_empty !== false;
  $("#profPreferred").value = (p.preferred_domains || []).join(", ");
  $("#profBlocked").value = (p.blocked_domains || []).join(", ");
  $("#profStrict").checked = !!p.strict_analog;
  $("#profEnough").value = String(p.enough_confirmations ?? 0);
  if (p.affix && !els.affix.value.trim()) els.affix.value = p.affix;
}

async function loadProfile() {
  try {
    const d = await (await fetch("/api/profile")).json();
    if (d && d.profile) {
      fillProfile(d.profile);
      els.affix.value = d.profile.affix || els.affix.value;
      const t = $("#llmToggle"); if (t) t.checked = !!d.profile.use_llm_queries;
    }
  } catch (e) { logLine("Профиль поиска не загружен: " + (e.message || e), "WARNING"); }
}

async function saveProfile() {
  const err = $("#profError");
  err.hidden = true;
  try {
    const d = await postJson("/api/profile", {
      affix: $("#profAffix").value.trim(),
      use_llm_queries: $("#profLlm").checked,
      second_page_if_empty: $("#profSecondPage").checked,
      preferred_domains: csv2list($("#profPreferred").value),
      blocked_domains: csv2list($("#profBlocked").value),
      strict_analog: $("#profStrict").checked,
      enough_confirmations: Number($("#profEnough").value) || 0,
    });
    fillProfile(d.profile);
    els.affix.value = d.profile.affix || els.affix.value;
    const t = $("#llmToggle"); if (t) t.checked = !!d.profile.use_llm_queries;
    toast("Профиль поиска сохранён", "ok");
  } catch (e) {
    err.textContent = String(e.message || e); err.hidden = false;
  }
}

els.modeSave.onclick = () => { saveMode(); saveClean(); saveProfile(); els.modeModal.hidden = true; };
els.modeModal.onclick = (e) => { if (e.target === els.modeModal) els.modeModal.hidden = true; };
els.modeReset.onclick = () => {
  const cleanActive = document.querySelector('.mtab[data-mtab="clean"]').classList.contains("active");
  if (cleanActive) { cleanMode = Object.assign({}, DEFAULT_CLEAN, { _muted: {} }); applyCleanPreset("balanced"); saveClean(); }
  // `_muted` — вложенный объект: копируем отдельно, иначе сброс мутировал бы сам DEFAULT_MODE
  else { fetchMode = Object.assign({}, DEFAULT_MODE, { _muted: {} }); applyPreset("basic"); saveMode(); }
};

function currentFetchOpts() {
  const m = fetchMode;
  return {
    preset: m.preset, backend: m.backend, real_chrome: m.real_chrome, anti_detect: m.anti_detect,
    headed: m.headed, persistent_profile: m.persistent_profile, warmup: m.warmup,
    human_input: m.human_input, wait_networkidle: m.wait_networkidle, http_first: m.http_first,
    timeout: m.timeout, top_n_fetch: m.top_n_fetch, concurrency: m.concurrency,
    retry_on_block: m.retry_on_block, delay_range: [m.delay_min, m.delay_max],
    cooldown_range: [m.cooldown_min, m.cooldown_max],
  };
}

// ---- Сценарий очистки контента (зеркалит search_agent/config.py CLEAN_PRESETS) -------------
const DEFAULT_CLEAN = {
  preset: "balanced", strip_link_urls: true, drop_structural: true, drop_structural_force: false,
  remove_cookie_banner: true, link_density_prune: false,
  _muted: {},                                         // погашенные зависимые фильтры (см. CLEAN_DEPS)
};
const CLEAN_PRESETS = {
  safe:     { strip_link_urls: true, drop_structural: false, drop_structural_force: false, remove_cookie_banner: true,  link_density_prune: false },
  balanced: { strip_link_urls: true, drop_structural: true,  drop_structural_force: false, remove_cookie_banner: true,  link_density_prune: false },
  max:      { strip_link_urls: true, drop_structural: true,  drop_structural_force: true,  remove_cookie_banner: true,  link_density_prune: true  },
};
const CLEAN_LABEL = { safe: "Безопасно", balanced: "Сбалансированно", max: "Максимум", custom: "Свой" };
const CLEAN_DESC = {
  safe: "Только убрать адреса ссылок (текст остаётся). Объём ×0.5, цена гарантированно не теряется.",
  balanced: "+ удалить меню/шапку/подвал без цены и cookie-баннеры. Объём ×3, цена сохраняется (рекомендуется).",
  max: "+ удалять блоки даже с ценой и «меню» по плотности ссылок. Максимум экономии токенов, малый риск.",
  custom: "Ручной набор фильтров ниже.",
};
const CLEAN_KEY = "webui_cleanmode";
const CFLAGS = ["strip_link_urls", "drop_structural", "drop_structural_force", "remove_cookie_banner", "link_density_prune"];
// Зеркало search_agent/config.py CLEAN_DEPS: «…даже если внутри есть цена» — уточнение к
// «удалять меню/шапку/подвал», само по себе оно ничего не делает.
const CLEAN_DEPS = { drop_structural_force: ["drop_structural"] };
let cleanMode = loadClean();

function loadClean() {
  let saved; try { saved = JSON.parse(localStorage.getItem(CLEAN_KEY) || "null"); } catch (_) {}
  const m = Object.assign({}, DEFAULT_CLEAN, saved || {});
  m._muted = Object.assign({}, m._muted);
  return m;
}
function saveClean() { try { localStorage.setItem(CLEAN_KEY, JSON.stringify(cleanMode)); } catch (_) {} }

function applyCleanPreset(name) {
  cleanMode.preset = name;
  if (name !== "custom" && CLEAN_PRESETS[name]) {
    cleanMode._muted = {};
    Object.assign(cleanMode, CLEAN_PRESETS[name]);
  }
  syncCleanUI();
}
function syncCleanUI() {
  document.querySelectorAll("#cleanSeg .seg-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.cleanPreset === cleanMode.preset));
  $("#cleanDesc").textContent = CLEAN_DESC[cleanMode.preset] || "";
  CFLAGS.forEach((f) => { const el = document.querySelector(`[data-cflag="${f}"]`); if (el) el.checked = !!cleanMode[f]; });
  applyCleanDeps();
}

/** То же, что applyDeps, для фильтров очистки: зависимый фильтр без предпосылки гаснет. */
function applyCleanDeps() {
  for (const [flag, needs] of Object.entries(CLEAN_DEPS)) {
    const ok = needs.every((n) => !!cleanMode[n]);
    if (!ok) {
      if (cleanMode[flag]) { cleanMode._muted[flag] = true; cleanMode[flag] = false; }
    } else if (flag in cleanMode._muted) {
      cleanMode[flag] = cleanMode._muted[flag];
      delete cleanMode._muted[flag];
    }
    const el = document.querySelector(`[data-cflag="${flag}"]`);
    if (!el) continue;
    el.checked = !!cleanMode[flag];
    el.disabled = !ok;
    const lbl = el.closest("label");
    if (lbl) {
      lbl.classList.toggle("dep-off", !ok);
      lbl.title = ok ? "" : "заблокировано: нужно «Удалять меню/шапку/подвал»";
    }
  }
}
function onCleanManualChange() { cleanMode.preset = "custom"; saveClean(); syncCleanUI(); }

document.querySelectorAll("#cleanSeg .seg-btn").forEach((b) => b.onclick = () => { applyCleanPreset(b.dataset.cleanPreset); saveClean(); });
CFLAGS.forEach((f) => document.querySelector(`[data-cflag="${f}"]`)
  .addEventListener("change", (e) => { cleanMode[f] = e.target.checked; onCleanManualChange(); }));

function currentCleanOpts() {
  const m = cleanMode;
  return {
    preset: m.preset, strip_link_urls: m.strip_link_urls, drop_structural: m.drop_structural,
    drop_structural_force: m.drop_structural_force, remove_cookie_banner: m.remove_cookie_banner,
    link_density_prune: m.link_density_prune,
  };
}

// ---- Вкладки модалки (Режим открытия / Сценарий очистки) ----------------------------------
document.querySelectorAll(".mtab").forEach((tab) => tab.onclick = () => {
  document.querySelectorAll(".mtab").forEach((t) => t.classList.toggle("active", t === tab));
  const which = tab.dataset.mtab;
  $("#mpanel-open").hidden = which !== "open";
  $("#mpanel-clean").hidden = which !== "clean";
  $("#mpanel-profile").hidden = which !== "profile";
});

// ---- Переочистить кэш выбранным сценарием (без повторного захода на сайты) -----------------
$("#cleanReclean").onclick = async () => {
  const btn = $("#cleanReclean"), st = $("#cleanRecleanStatus");
  btn.disabled = true; st.textContent = "переочистка…";
  try {
    const d = await postJson("/api/reclean", { clean_opts: currentCleanOpts() });
    st.textContent = `перечищено ${d.recleaned}, пропущено ${d.skipped}`;
    toast(`Кэш переочищен сценарием «${CLEAN_LABEL[cleanMode.preset] || cleanMode.preset}»: ${d.recleaned} стр.`, "ok");
  } catch (e) {
    st.textContent = "ошибка: " + (e.message || e); toast(String(e.message || e), "err");
  } finally {
    btn.disabled = false;
  }
};

// ---- Единая кнопка «Найти» (агент ведёт весь пайплайн, резюмирует прерванное) -------
els.runBtn.onclick = async () => {
  if (!Object.keys(posRows).length) { toast("Сначала загрузите файл со списком товаров.", ""); return; }
  const affix = els.affix.value.trim();            // фраза целиком (без разбиения по запятым)
  els.runBtn.disabled = true; showRunning();
  extractProgress = { done: 0, total: Object.keys(posRows).length };
  const modeLbl = PRESET_LABEL[fetchMode.preset] || fetchMode.preset;
  setStatusMsg("Найти: агент ведёт поиск (источники → сайты → цены → свод) · режим: " + modeLbl + "…", "busy");
  try {
    const useLlmQueries = !!($("#llmToggle") || {}).checked && (modelReady || connected);
    const d = await postJson("/api/find",
      { affix, use_llm_queries: useLlmQueries, opts: currentFetchOpts(), clean_opts: currentCleanOpts() });
    toast("Поиск запущен: " + d.count + " товаров · транспорт: " + (d.transport || "?"), "ok");
  } catch (e) {
    setStatusMsg("Ошибка: " + (e.message || e), "ERROR"); toast(String(e.message || e), "err");
    els.runBtn.disabled = false; hideRunning();
  }
};

// ---- Сбросить прогресс (с предупреждением) ----------------------------------
els.resetBtn.onclick = async () => {
  if (!confirm("Сбросить весь прогресс поиска?\n\nБудут стёрты найденные источники, загрузки, извлечённые цены и своды по всем товарам. Действие необратимо.")) return;
  els.resetBtn.disabled = true;
  try {
    await postJson("/api/session/reset");
    clearSessionUI();
    toast("Прогресс сброшен", "ok");
  } catch (e) {
    toast(String(e.message || e), "err");
  } finally {
    els.resetBtn.disabled = false;
  }
};

// Очистить всю рабочую область (после сброса прогресса).
function clearSessionUI() {
  stopElapsed(); elapsedFrom = null; renderElapsed(); parsedRows = 0; resetParseWait();
  els.posBody.innerHTML = `<tr class="empty-row"><td colspan="7">Загрузите файл — здесь появятся товары.</td></tr>`;
  posRows = {}; posCandidates = {}; srcFound = 0;
  fetchByUrl = {}; manualByUrl = {}; pricesByUrl = {}; siteByUrl = {}; verdictByRow = {}; inspectByUrl = {};
  resetQueue(); expandedRows.clear(); updateExpandedMode();
  els.mapSummary.hidden = true;
  els.cTotal.textContent = els.cFound.textContent = els.cAnalog.textContent = els.cNone.textContent = 0;
  els.runBtn.disabled = true; els.reportBtn.disabled = true; els.resetBtn.disabled = true;
  els.dzText.textContent = "Перетащите Excel/CSV или нажмите для выбора";
  hideRunning(); setStatusMsg("Прогресс сброшен · загрузите файл", "INFO");
}

// Событие видимой асинхронности: сколько вкладок сейчас открыто из скольких доступно.
function onFetchLoad(m) {
  if (m.active > 0) setStatusMsg(`Загрузка сайтов: открыто вкладок ${m.active}/${m.cap}`, "busy");
}

// Загрузка этапов: сколько ПОЗИЦИЙ внутри каждого этапа и сколько ждут слот (agent/gates.py).
// Без этого при лимите 5 позиций из 500 остальная таблица выглядела бы как «ничего не делается».
const STAGE_LABEL = { discover: "поиск", fetch: "загрузка", inspect: "триаж",
                      find_link: "карточка", escalate: "эскалация", extract: "извлечение" };
function onStageLoad(m) {
  const el = $("#stageLoad"); if (!el) return;
  const parts = [];
  for (const s of m.stages || []) {
    if (!s.rows && !s.waiting) continue;                     // пустой этап не показываем
    const wait = s.waiting ? ` <span class="sl-wait">ждут ${s.waiting}</span>` : "";
    parts.push(`<span class="sl-item">${STAGE_LABEL[s.name] || s.name} ` +
               `<b>${s.rows}/${s.cap}</b>${wait}</span>`);
  }
  if (m.tabs && (m.tabs.slots || m.tabs.waiting)) {
    parts.push(`<span class="sl-item">вкладки <b>${m.tabs.slots}/${m.tabs.cap}</b></span>`);
  }
  el.innerHTML = parts.length ? "Позиций в работе: " + parts.join(" · ") : "";
}

function setStage(row, stage, state) {
  const tr = posRows[row]; if (!tr) return;
  const dot = tr.querySelector(`.stages .s[data-stage="${stage}"]`); if (!dot) return;
  dot.className = "s " + (["active", "done", "error", "skip", "wait"].includes(state) ? state : "");
  tr.classList.toggle("active-row", state === "active");
  if (state === "active") setRowState(row, STAGE_STATE[stage] || "");
}

// Подпись состояния позиции рядом с кружками: строка никогда не выглядит «ничего не происходит».
// Кружки показывают КАКАЯ стадия, подпись — человеческим языком, что сейчас с товаром.
const STAGE_STATE = { query: "поиск источников…", fetch: "загрузка сайтов…",
                      extract: "извлечение цены…", match: "свожу итог…" };
function setRowState(row, text, cls) {
  const tr = posRows[row]; if (!tr) return;
  const el = tr.querySelector(".rstate"); if (!el) return;
  el.textContent = text;
  el.className = "rstate" + (cls ? " " + cls : "");
}

function setCandidates(row, list, queries) {
  posCandidates[row] = { list: list || [], queries: queries || [] };
  const tr = posRows[row]; if (!tr) return;
  const n = (list || []).length;
  const cell = tr.querySelector(".c-price");
  if (!verdictByRow[row]) {                                    // до свода — показываем число источников
    cell.innerHTML = n ? `${n} ист. <span class="caret">▾</span>` : "нет";
    cell.classList.toggle("has", n > 0);
  }
  if (n > 0) { srcFound++; }
  const nx = tr.nextElementSibling;                            // если строка развёрнута — обновить
  if (nx && nx.classList.contains("cand-row") && nx.dataset.row === String(row)) {
    nx.querySelector("td").innerHTML = renderExpansion(row);
  }
}

// Полосы прогресса нет намеренно: этапы идут вперемешку по позициям, и одна шкала показывала
// то откат назад, то «100%» в середине прогона. Честный ответ даёт счётчик внизу и вкладка «Ход».
function setProgress(m) {
  if (m.done >= m.total) setStatusMsg("Готово: " + m.total, "INFO");
}

// Прогресс по товарам (извлечение/конвейер): «готово k/N»; конец → скрыть «Стоп».
function onStageProgress(m) {
  extractProgress = { done: m.item_done || 0, total: m.item_total || 0 };
  setStatusMsg(`Обработано товаров: ${m.item_done}/${m.item_total}`, "INFO");
  if (m.item_total && m.item_done >= m.item_total) hideRunning();
}

// ---- Вкладка «Модель»: что отвечает модель, с привязкой к позиции -----------
// Логи консоли (что делает программа) и ответы модели (что она сказала) — разные вопросы, и
// в одном потоке они забивали друг друга. Здесь только модель, и каждая запись подписана
// номером позиции и её наименованием: «#12 · Аккумулятор Delta DTM 1212 · shop.ru».

// Подпись позиции: порядковый номер в таблице (как в колонке «#») и наименование из файла.
function posLabel(row) {
  const tr = posRows[row];
  if (!tr) return { idx: row == null ? "" : "стр. " + row, name: "" };
  return { idx: "#" + (tr.dataset.idx || row), name: tr.dataset.name || "" };
}

// Заголовок записи. Для разбора наименований позиция не одна — подписываем диапазоном номеров.
function modelTitle(m) {
  const where = m.stage === "parse" ? `разбор наименований · пакет ${m.batch}/${m.batches}`
    : m.stage === "rationale" ? "комментарий к цене"
      : m.domain === "chat" ? "диалог" : (m.domain || "модель");
  if (m.stage === "parse") {
    const nums = (m.row_nums || []).map((r) => posLabel(r).idx);
    const span = nums.length > 1 ? `${nums[0]}–${nums[nums.length - 1]}` : (nums[0] || "");
    return `${span} · ${where}`;
  }
  const { idx, name } = posLabel(m.row);
  return [idx, name, where].filter(Boolean).join(" · ");
}

// Ключ склейки дельт: у извлечения это позиция+сайт, у разбора — свой sid (позиций много).
function streamKey(m) { return m.sid || (m.row + "|" + m.domain); }

function modelEntryPass(e) {
  const q = els.modelSearch.value.trim().toLowerCase();
  return !q || (e.title + " " + e.text).toLowerCase().includes(q);
}

// Одна запись вкладки: заголовок + тело (поток или готовый ответ). Возвращает узел тела,
// чтобы поток мог дописываться в него по мере генерации.
function renderModelEntry(e) {
  if (!modelEntryPass(e)) return null;
  const box = document.createElement("div");
  box.className = "model-entry" + (e.kind === "answer" ? " is-answer" : "");
  const head = document.createElement("div");
  head.className = "model-entry-head";
  head.textContent = (e.kind === "answer" ? "◆ " : "▸ ") + e.title;
  const body = document.createElement("div");
  body.className = "model-entry-body";
  body.textContent = e.text;
  box.appendChild(head); box.appendChild(body);
  const hint = els.modelConsole.querySelector(".hint");
  if (hint) hint.remove();
  els.modelConsole.appendChild(box);
  e.node = body;
  box._entry = e;                    // обратная ссылка: при обрезке запись должна забыть свой узел
  trimConsole(els.modelConsole, LOG_DOM_MAX, (node) => {
    if (node._entry) node._entry.node = null;
  });
  if (modelAutoscroll) els.modelConsole.scrollTop = els.modelConsole.scrollHeight;
  return e.node;
}

function pushModelEntry(e) {
  modelLog.push(e);
  if (modelLog.length > MODEL_LOG_MAX) modelLog = modelLog.slice(-Math.floor(MODEL_LOG_MAX * 0.75));
  renderModelEntry(e);
  return e;
}

function rerenderModelLog() {
  els.modelConsole.innerHTML = "";
  modelLog.forEach((e) => { e.node = null; renderModelEntry(e); });
}

// «Живое мышление» модели: дельты дописываются в тело своей записи.
function onModelStream(m) {
  const key = streamKey(m);
  if (m.start || !traceStream[key]) {
    traceStream[key] = pushModelEntry({ kind: "stream", key, title: modelTitle(m), text: "" });
    if (m.start) return;
  }
  if (m.delta) {
    const e = traceStream[key];
    e.text += m.delta;
    if (e.node) {
      e.node.textContent = e.text;
      if (modelAutoscroll) els.modelConsole.scrollTop = els.modelConsole.scrollHeight;
    }
  }
}

// Готовый ответ модели по позиции: разбор наименования, итог по сайту, комментарий к цене.
function onModelAnswer(m) {
  if (!m.text) return;
  pushModelEntry({ kind: "answer", title: modelTitle(m), text: m.text });
  if (m.stage !== "parse") setStatusMsg(m.text, "INFO");   // разборов сотни — статус не забиваем
}

els.modelSearch.oninput = debounce(rerenderModelLog, 200);
els.modelClear.onclick = () => { modelLog = []; traceStream = {}; els.modelConsole.innerHTML = ""; };
els.modelAutoscroll.onclick = () => {
  modelAutoscroll = !modelAutoscroll;
  els.modelAutoscroll.style.opacity = modelAutoscroll ? "1" : ".5";
};

// Показ/скрытие кнопки «Стоп» на время прогона.
function showRunning() { els.stopBtn.hidden = false; }
function hideRunning() { els.stopBtn.hidden = true; }

function onFetched(m) {
  fetchByUrl[m.url] = { status: m.status, blocked: m.blocked, chars: m.chars };
  // если строка развёрнута — обновить карточки
  const tr = posRows[m.row]; if (!tr) return;
  const nx = tr.nextElementSibling;
  if (nx && nx.classList.contains("cand-row") && nx.dataset.row === String(m.row)) {
    nx.querySelector("td").innerHTML = renderExpansion(m.row);
  }
}

els.stopBtn.onclick = async () => {
  try {
    const d = await postJson("/api/stop");
    setStatusMsg("Останавливаю…", "WARNING");
    if (!d.stopped || !d.stopped.length) toast("Активных поисков нет", "");
  } catch (e) { toast(String(e.message || e), "err"); }
  hideRunning(); els.runBtn.disabled = false;
};

// ---- Статус анализа сайта (событие site_update) — питает таблицу выдачи ------
function onSiteUpdate(m) {
  siteByUrl[m.url] = {
    status: m.status, chars: m.chars, found: m.found, match: m.match,
    price: m.price, comment: m.comment, note: m.note, domain: m.domain,
  };
  const tr = posRows[m.row]; if (!tr) return;
  const nx = tr.nextElementSibling;
  if (nx && nx.classList.contains("cand-row") && nx.dataset.row === String(m.row)) {
    nx.querySelector("td").innerHTML = renderExpansion(m.row);
  }
}

function onPrice(m) {
  (pricesByUrl[m.url] = pricesByUrl[m.url] || []).push({
    value: m.value, currency: m.currency, match: m.match, in_stock: m.in_stock,
    confidence: m.confidence, degraded: m.degraded, note: m.note,
  });
  // ДО свода в ячейке товара НЕ показываем цену числом (это цена лишь одного сайта и может быть
  // выбросом/галлюцинацией). Показываем нейтральный «анализ · найдено N». Итог — только по verdict.
  const tr = posRows[m.row];
  if (tr && !verdictByRow[m.row]) {
    const n = countPricesForRow(m.row);
    const cell = tr.querySelector(".c-price");
    cell.innerHTML = `<span class="analyzing">анализ · найдено ${n}</span> <span class="caret">▾</span>`;
    cell.classList.add("has");
  }
  // если строка развёрнута — обновить карточки (покажет цены под каждым источником)
  const trr = posRows[m.row]; if (!trr) return;
  const nx = trr.nextElementSibling;
  if (nx && nx.classList.contains("cand-row") && nx.dataset.row === String(m.row)) {
    nx.querySelector("td").innerHTML = renderExpansion(m.row);
  }
}

function countPricesForRow(row) {
  const list = posCandidates[row] ? posCandidates[row].list : [];
  let n = 0;
  for (const c of list) n += (pricesByUrl[c.url] || []).length;
  return n;
}

function fmtPrice(v, cur) {
  const sym = { RUB: "₽", USD: "$", EUR: "€", CNY: "¥" }[cur] || (" " + cur);
  return Number(v).toLocaleString("ru-RU") + sym;
}

// Триаж страницы (§8): показываем в всегда-видимом статусе, что реально получили по сайту
// (карточка/листинг/блок/пусто), чтобы «не найдено» не выглядело как «ничего не произошло».
const inspectLabels = { blocked: "блок", empty: "пусто", listing: "листинг", card: "карточка", other: "?" };
function onInspect(m) {
  inspectByUrl[m.url] = m;
  if (m.kind !== "card") {
    const label = inspectLabels[m.kind] || m.kind;
    setStatusMsg(`Триаж ${m.domain || m.url}: ${label} — ${m.reason}`, m.kind === "blocked" ? "WARNING" : "busy");
  }
}

// Очередь оркестратора (§9/M8): «сейчас / в очереди / готово» из потока task-событий.
const taskLabels = { discover: "поиск источников", fetch: "загрузка", inspect: "триаж",
  find_link: "поиск карточки", escalate: "эскалация", extract: "извлечение (модель)",
  adjudicate: "свод" };
// Какой кружок стадии подсветить, когда задача ждёт слот этапа (state=blocked).
const KIND_STAGE = { discover: "query", fetch: "fetch", inspect: "fetch", find_link: "fetch",
  escalate: "fetch", extract: "extract", adjudicate: "match" };
const QUEUE_LIMIT = 40;                              // сколько задач показываем в колонке
function onTask(m) {
  taskById[m.id] = m;
  // Ждёт слот этапа — так и пишем в строке товара: «ничего не происходит» недопустимо.
  if (m.state === "blocked" && m.row != null && posRows[m.row]) {
    const tr = posRows[m.row];
    const dot = tr.querySelector(`.stages .s[data-stage="${KIND_STAGE[m.kind] || "fetch"}"]`);
    if (dot && !dot.classList.contains("done")) dot.className = "s wait";
    if (!verdictByRow[m.row]) setRowState(m.row, "в очереди: " + (taskLabels[m.kind] || m.kind));
  }
  scheduleQueueRender();
}
// Перерисовка склеивается по кадру: событий `task` тысячи, а перестройка трёх колонок —
// это innerHTML на сотни узлов. Без склейки интерфейс замирал бы на больших списках.
let queueRaf = 0;
function scheduleQueueRender() {
  if (queueRaf) return;
  queueRaf = requestAnimationFrame(() => { queueRaf = 0; renderQueue(); });
}
function renderQueue() {
  const all = Object.values(taskById);
  if (!all.length) { els.queueEmpty.hidden = false; return; }
  els.queueEmpty.hidden = true;
  const bucket = { running: [], queued: [], done: [] };
  for (const t of all) {
    if (t.state === "running") bucket.running.push(t);
    // blocked — задача ЖДЁТ слот этапа, а не выполнена: место ей в «в очереди», иначе
    // она выглядела бы завершённой.
    else if (t.state === "queued" || t.state === "blocked") bucket.queued.push(t);
    else bucket.done.push(t);                        // done|failed|cancelled
  }
  const byRow = (a, b) => (a.row ?? 1e9) - (b.row ?? 1e9);
  bucket.running.sort(byRow);
  bucket.queued.sort(byRow);                         // ранние позиции — выше: видно порядок работы
  const row = (t) => {
    const lbl = taskLabels[t.kind] || t.kind;
    const rw = t.row != null ? ` · стр.${t.row}` : "";
    const model = t.uses_model ? ' <span class="qm">модель</span>' : "";
    const state = t.state === "blocked" ? "ждёт этап" : t.state;
    return `<div class="qitem st-${t.state}"><span class="qk">${lbl}${rw}</span>${model}<span class="qs">${state}</span></div>`;
  };
  const render = (list, tail) => {
    const shown = tail ? list.slice(-QUEUE_LIMIT).reverse() : list.slice(0, QUEUE_LIMIT);
    const rest = list.length - shown.length;
    return (shown.map(row).join("") || '<div class="qnone">—</div>') +
           (rest > 0 ? `<div class="qnone">и ещё ${rest}</div>` : "");
  };
  els.qRunning.innerHTML = render(bucket.running);
  els.qQueued.innerHTML = render(bucket.queued);
  els.qDone.innerHTML = render(bucket.done, true);   // «готово»: последние сверху
}
function resetQueue() { taskById = {}; els.qRunning.innerHTML = els.qQueued.innerHTML = els.qDone.innerHTML = ""; els.queueEmpty.hidden = false; }

// Свод по товару (M4): итоговая цена + диапазон в колонке «Цена» + счётчики.
function onVerdict(m) {
  verdictByRow[m.row] = m;
  renderVerdictCell(m.row, m);
  recountVerdicts();
  els.reportBtn.disabled = false;                   // есть хотя бы один свод — отчёт доступен
}

function renderVerdictCell(row, m) {
  const tr = posRows[row]; if (!tr) return;
  const cell = tr.querySelector(".c-price");
  const rcell = tr.querySelector(".c-range");
  const fcell = tr.querySelector(".c-found");
  const onreq = m.on_request || null;
  // Для КАКОГО товара названа цена: название со страницы дословно (PriceCandidate.found).
  // Без него итог не проверить глазами — цена есть, а чего именно, непонятно.
  const setFound = (name) => {
    if (!fcell) return;
    const s = String(name || "").trim();
    fcell.textContent = s || "—";
    fcell.title = s || "";
    fcell.classList.toggle("has", !!s);
  };
  // Терминальное состояние позиции: свод пришёл — работа по товару закончена, так и пишем.
  const resolved = m.primary != null || !!onreq;
  setRowState(row, resolved ? "готово" : "не найдено", resolved ? "ok" : "none");
  if (m.primary == null && onreq) {
    // Товар найден, но продавец не публикует цифру — это не «не найдено».
    const mt = onreq.match === "аналог" ? ' <span class="mtag analog">аналог</span>' : "";
    cell.innerHTML = `<span class="mtag onreq" title="${esc(onreq.found || "")}">цена по запросу</span>${mt}` +
                     ` <span class="caret">▾</span>`;
    cell.classList.add("has");
    setFound(onreq.found);                 // товар найден, цифры цены нет — но НАЙДЕН именно этот
    if (rcell) rcell.textContent = "—";
  } else if (m.primary == null) {
    const why = m.not_found_reason ? ` <span class="mtag-why" title="причина">${esc(m.not_found_reason)}</span>` : "";
    cell.innerHTML = `<span class="mtag none">не найдено</span>${why} <span class="caret">▾</span>`;
    cell.classList.remove("has");
    setFound("");
    if (rcell) rcell.textContent = "—";
  } else {
    // Цена, которую модель не сверяла с товаром (взята из разметки страницы, потому что модель
    // была недоступна), обязана выглядеть иначе: раньше она показывалась как обычное «точное».
    const unver = m.verified === false
      ? ' <span class="mtag none" title="цена из структурной разметки страницы: модель была'
        + ' недоступна и соответствие товару не проверяла">не проверено моделью</span>' : "";
    const mt = m.match === "аналог" ? ' <span class="mtag analog">аналог</span>' : "";
    const conf = m.confidence != null ? ` <span class="pconf" title="достоверность">${Math.round(m.confidence * 100)}%</span>` : "";
    cell.innerHTML = `<b>${fmtPrice(m.primary, m.currency)}</b>${unver}${mt} <span class="caret">▾</span>`;
    cell.classList.add("has");
    setFound(m.found);
    if (rcell) {
      rcell.innerHTML = (m.price_min != null && m.price_max != null && m.price_min !== m.price_max)
        ? `<span class="prange">${fmtPrice(m.price_min, m.currency)}–${fmtPrice(m.price_max, m.currency)}</span>${conf}`
        : `<span class="prange">—</span>${conf}`;
    }
  }
}

// Полный дамп Verdict (из кэша сессии) → плоский вид для ячейки/счётчиков.
function flatVerdict(v, nfr, onreq) {
  if (!v) return { primary: null, not_found_reason: nfr, on_request: onreq || null };
  const p = v.primary;
  if (p && typeof p === "object") {
    // `found` копируем обязательно: без него после перезагрузки страницы колонка «Найдено»
    // у уже посчитанных позиций осталась бы пустой.
    return { primary: p.value, match: (p.match && p.match.value) || p.match, currency: v.currency,
             price_min: v.price_min, price_max: v.price_max, confidence: v.confidence,
             domain: p.source_domain, found: p.found, url: p.url,
             verified: p.verified !== false,       // пометка «не проверено» переживает перезагрузку
             not_found_reason: nfr, on_request: onreq || null };
  }
  return { primary: p, match: v.match, currency: v.currency, price_min: v.price_min,
           price_max: v.price_max, confidence: v.confidence, domain: v.domain,
           verified: v.verified !== false, found: v.found,
           not_found_reason: nfr, on_request: onreq || null };
}

function recountVerdicts() {
  let found = 0, analog = 0, none = 0;
  for (const r in verdictByRow) {
    const v = verdictByRow[r];
    const onreq = v.on_request || null;
    if (v.primary == null && !onreq) none++;                       // реально не нашли товар
    else if ((v.match || (onreq && onreq.match)) === "аналог") analog++;
    else found++;                                                  // цена или «цена по запросу»
  }
  els.cFound.textContent = found;
  els.cAnalog.textContent = analog;
  els.cNone.textContent = none;
}

// Ручной режим: открыть окно / забрать сейчас / закрыть (делегирование; не тогглит строку)
els.posBody.addEventListener("click", (e) => {
  const op = e.target.closest(".manual-open"), gr = e.target.closest(".manual-grab"), cl = e.target.closest(".manual-close");
  if (op) { e.stopPropagation(); manualOpen(op.dataset.url); }
  else if (gr) { e.stopPropagation(); manualGrab(gr.dataset.url); }
  else if (cl) { e.stopPropagation(); manualClose(cl.dataset.url); }
});

// «показать контент» в карточке (делегирование; не конфликтует с тоглом строки)
els.posBody.addEventListener("click", async (e) => {
  const a = e.target.closest(".showc"); if (!a) return;
  e.stopPropagation();
  const box = a.closest("tr").querySelector(".serp-content");
  if (box.dataset.loaded) { box.hidden = !box.hidden; return; }
  try {
    const r = await apiJson("/api/content?url=" + encodeURIComponent(a.dataset.url));
    const meta = r.structured ? `[структура: JSON-LD ${r.structured.jsonld}, microdata ${r.structured.microdata}, meta ${r.structured.meta}]\n\n` : "";
    box.textContent = meta + (r.markdown || "(пусто)");
    box.dataset.loaded = "1";
  } catch (e) { box.textContent = "нет контента: " + (e.message || e); }
  box.hidden = false;
});

// ---- Разворот строки: таблица выдачи по сайту (проблема №7) -----------------
function toggleExpand(row) {
  const tr = posRows[row]; if (!tr) return;
  const nx = tr.nextElementSibling;
  if (nx && nx.classList.contains("cand-row") && nx.dataset.row === String(row)) {
    nx.remove(); tr.classList.remove("expanded");             // свернуть
    expandedRows.delete(String(row)); updateExpandedMode(); return;
  }
  const exp = document.createElement("tr");
  exp.className = "cand-row"; exp.dataset.row = row;
  exp.innerHTML = `<td colspan="7">${renderExpansion(row)}</td>`;
  tr.after(exp); tr.classList.add("expanded");
  expandedRows.add(String(row)); updateExpandedMode();
}

const SITE_STATUS_LABEL = {
  discovered: "ожидает", fetching: "загрузка…", fetched: "загружено", extracting: "извлечение…",
  done: "готово", on_request: "цена по запросу", empty: "цена не найдена",
  blocked: "блокировка", error: "ошибка", file: "файл", skipped: "не понадобился",
};
const SITE_STATUS_CLASS = {
  done: "ok", on_request: "ok", empty: "warn", blocked: "blk", error: "blk",
  fetching: "busy", extracting: "busy", skipped: "warn",
};

// Слить статус сайта из site_update + fetch/manual/цен (обратная совместимость и восстановление).
function siteView(c) {
  const s = siteByUrl[c.url] || {};
  const f = fetchByUrl[c.url] || {};
  const man = manualByUrl[c.url] || {};
  const prices = pricesByUrl[c.url] || [];
  let { status, chars, price, match, found, comment, note, on_request: onreq } = s;
  if (chars == null) chars = (man.chars != null ? man.chars : f.chars);
  if (price == null && prices.length) {                       // вывести представительную цену из потока price
    const bp = prices.slice().sort((a, b) =>
      ((a.match === "точное" ? 0 : 1) - (b.match === "точное" ? 0 : 1)) || (a.value - b.value))[0];
    price = Math.round(bp.value); match = match || bp.match; found = found || bp.found; comment = comment || bp.note;
  }
  if (!status) {
    if (c.is_file) status = "file";
    else if (prices.length) status = "done";
    else if (man.chars) status = "fetched";
    else if (f.blocked) status = "blocked";
    else if (f.chars) status = "fetched";
    else status = "discovered";
  }
  if (!note && f.blocked) note = String(f.blocked);
  return { status, chars, price, match, found, comment, note, onreq: !!onreq };
}

function renderExpansion(row) {
  const data = posCandidates[row];
  if (data === undefined) return `<div class="serp-empty">Ещё не искали — нажмите «Найти».</div>`;
  if (data === null) return `<div class="serp-empty">Идёт поиск…</div>`;
  const q = (data.queries && data.queries.length)
    ? `<div class="serp-queries">Запросы: ${data.queries.map((x) => `<span class="qchip">${esc(x)}</span>`).join(" ")}</div>` : "";
  if (!data.list.length) return q + `<div class="serp-empty">Источники не найдены.</div>`;
  const terms = queryTerms(data.queries);
  const head = `<thead><tr>
    <th class="s-src">Источник</th><th class="s-act" title="Открыть в нашем окне"></th>
    <th class="s-status">Статус анализа</th>
    <th class="s-chars">Симв.</th><th class="s-found">Найдено на сайте</th>
    <th class="s-type">Тип</th><th class="s-price">Цена</th>
    <th class="s-comment">Комментарий</th>
  </tr></thead>`;
  const rows = data.list.map((c) => serpRow(c, terms)).join("");
  return q + `<div class="serp-table-wrap"><table class="serp-table">${head}<tbody>${rows}</tbody></table></div>`;
}

function serpRow(c, terms) {
  const fav = "https://www.google.com/s2/favicons?domain=" + encodeURIComponent(c.domain) + "&sz=32";
  const tl = tierLabel(c.tier);
  const v = siteView(c);
  const man = manualByUrl[c.url];
  const contentUrl = (man && man.chars && man.current) ? man.current : c.url;
  const label = SITE_STATUS_LABEL[v.status] || v.status || "—";
  const scls = SITE_STATUS_CLASS[v.status] || "";
  const note = v.note ? `<div class="s-note">${esc(v.note)}</div>` : "";
  const typ = v.match ? `<span class="mtag ${v.match === "аналог" ? "analog" : "exact"}">${esc(v.match)}</span>` : "—";
  const price = (v.price != null) ? `<b>${Number(v.price).toLocaleString("ru-RU")} ₽</b>`
                                  : (v.onreq ? `<span class="mtag onreq">по запросу</span>` : "—");
  // Заголовок = ссылка. Для не-файлов открывает НАШЕ окно (капча/захват), не текущий браузер.
  const title = c.is_file
    ? `<a class="serp-title" href="${esc(c.url)}" target="_blank" rel="noopener">${hl(c.title || c.url, terms)}</a>`
    : `<a class="serp-title manual-open" data-url="${esc(c.url)}" title="Открыть в нашем окне (решить капчу, забрать страницу)">${hl(c.title || c.url, terms)}</a>`;
  const showc = (v.chars) ? `<a class="showc" data-url="${esc(contentUrl)}" title="Показать извлечённый текст">📄 текст</a>` : "";
  // Ручное окно — отдельной колонкой справа от источника: только иконки, одна под другой.
  const manCtl = c.is_file ? "" : `<div class="serp-manual" data-url="${esc(c.url)}">${manualCtlHtml(c.url)}</div>`;
  return `<tr class="site-row" data-url="${esc(c.url)}">
    <td class="s-src">
      <div class="s-src-head"><img class="fav" src="${fav}" alt="" onerror="this.style.visibility='hidden'"/>
        <span class="s-dom">${esc(c.domain)}</span>${tl ? ` · <span class="tl">${tl}</span>` : ""} · ранг ${c.score}${c.is_file ? ' · <span class="file">файл</span>' : ""}</div>
      ${title}
      <div class="serp-snip">${hl(c.snippet || "", terms)}</div>
      <div class="s-tools">${showc}</div>
      <pre class="serp-content" hidden></pre>
    </td>
    <td class="s-act">${manCtl}</td>
    <td class="s-status"><span class="sbadge ${scls}">${esc(label)}</span>${note}</td>
    <td class="s-chars">${v.chars != null ? Number(v.chars).toLocaleString("ru-RU") : "—"}</td>
    <td class="s-found">${v.found ? esc(v.found) : "—"}</td>
    <td class="s-type">${typ}</td>
    <td class="s-price">${price}</td>
    <td class="s-comment">${v.comment ? esc(v.comment) : "—"}</td>
  </tr>`;
}

// ---- Ручной режим (открыть окно → пройти капчу → забрать страницу) ----------
// Состояние manualByUrl: { state: idle|busy|open|done, chars, current, note }

// Только иконки, без подписей: смысл — в подсказке (title), состояние — в колонке «Статус анализа».
function manualCtlHtml(url) {
  const m = manualByUrl[url] || {};
  const s = m.state;
  const blocked = fetchByUrl[url] && fetchByUrl[url].blocked;
  const openTitle = "Открыть в нашем окне: пройдите проверку/капчу — страница заберётся сама."
                  + " Окно закроете сами." + (m.note ? "\n" + m.note : "");
  const open = `<a class="mico manual-open${blocked ? " primary" : ""}" data-url="${esc(url)}" title="${esc(openTitle)}">⧉</a>`;
  const grabNow = `<a class="mico manual-grab" data-url="${esc(url)}" title="Забрать содержимое прямо сейчас (если проверка уже пройдена)">⤓</a>`;
  const closeWin = `<a class="mico manual-close" data-url="${esc(url)}" title="Закрыть окно">✕</a>`;
  if (s === "busy") return `<span class="mico wait" title="${esc(m.note || "…")}">⏳</span>`;
  if (s === "watching" || s === "blocked" || s === "surfed" || s === "timeout") {
    // Окно открыто: авто-забор идёт сам, иконки — подстраховка (забрать сейчас / переоткрыть / закрыть).
    const spin = (s === "watching" || s === "blocked")
      ? `<span class="mico wait" title="${esc(m.note || "ждём прохождения проверки")}">⏳</span>` : "";
    return spin + grabNow + open + closeWin;
  }
  if (s === "grabbed") return open + closeWin;      // забрано; окно осталось открытым — закрывает человек
  return open;                                      // idle / undefined / closed
}

function setManual(url, patch) {
  manualByUrl[url] = Object.assign({}, manualByUrl[url], patch);
  document.querySelectorAll(`.serp-manual[data-url="${cssEsc(url)}"]`).forEach((box) => {
    box.innerHTML = manualCtlHtml(url);
  });
}
function cssEsc(s) { return String(s).replace(/["\\]/g, "\\$&"); }

async function manualOpen(url) {
  setManual(url, { state: "busy", note: "открываю окно…" });
  try {
    await postJson("/api/manual/open", { url });
    setManual(url, { state: "watching", note: "окно открыто — пройдите проверку, страница заберётся сама" });
    setStatusMsg("Ручной режим: окно открыто — пройдите проверку, страница заберётся сама (окно закройте сами)", "busy");
  } catch (e) {
    setManual(url, { state: "idle", note: "не открылось: " + (e.message || e) });
    toast(String(e.message || e), "err");
  }
}

async function manualGrab(url) {                    // override «забрать сейчас»
  setManual(url, { state: "busy", note: "забираю страницу…" });
  try {
    const r = await postJson("/api/manual/grab", { url });
    if (r.blocked) { setManual(url, { state: "blocked", note: "ещё капча (" + r.blocked + ") — пройдите и повторите" }); return; }
    setManual(url, { state: "grabbed", chars: r.chars, current: r.current, note: "" });
    toast("Забрано: " + r.chars + " симв.", "ok");
    rerenderCard(url);
  } catch (e) {
    setManual(url, { state: "watching", note: "ошибка: " + (e.message || e) });
    toast(String(e.message || e), "err");
  }
}

async function manualClose(url) {
  setManual(url, { state: "busy", note: "закрываю окно…" });
  try { await postJson("/api/manual/close", { url }); }
  catch (e) { toast(String(e.message || e), "err"); }
  manualByUrl[url] = {}; rerenderCard(url);
}

// События ручного режима от наблюдателя (WS): watching/surfed/blocked/grabbed/timeout/closed
function onManualEvent(m) {
  const url = m.url;
  if (m.state === "closed") { manualByUrl[url] = {}; rerenderCard(url); return; }
  const patch = { state: m.state, note: m.note || "" };
  if (m.chars != null) patch.chars = m.chars;
  if (m.current) patch.current = m.current;
  setManual(url, patch);
  if (m.note) setStatusMsg("Ручной режим: " + m.note,
    m.state === "grabbed" ? "INFO" : (m.state === "timeout" ? "WARNING" : "busy"));
  if (m.state === "grabbed") { toast("Забрано автоматически: " + m.chars + " симв.", "ok"); rerenderCard(url); }
}

function rerenderCard(url) {                        // перерисовать строку(и), где есть эта карточка
  for (const row of Object.keys(posCandidates)) {
    const data = posCandidates[row];
    if (!data || !data.list || !data.list.some((c) => c.url === url)) continue;
    const tr = posRows[row], nx = tr && tr.nextElementSibling;
    if (nx && nx.classList.contains("cand-row") && nx.dataset.row === String(row)) {
      nx.querySelector("td").innerHTML = renderExpansion(row);
    }
  }
}

function tierLabel(t) { return ({ 1: "офиц.", 2: "дистриб./вендор", 3: "маркетплейс", 4: "прочее" })[t] || ""; }

function queryTerms(queries) {
  const set = new Set();
  (queries || []).forEach((q) => String(q).toLowerCase().split(/\s+/).forEach((t) => { if (t.length > 2) set.add(t); }));
  return [...set];
}
function hl(text, terms) {          // экранируем, затем подсвечиваем слова запроса
  let out = esc(text);
  (terms || []).forEach((t) => {
    const re = new RegExp("(" + t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "gi");
    out = out.replace(re, "<mark>$1</mark>");
  });
  return out;
}

// ---- Восстановление прошлой сессии при открытии (проблема №2) ---------------
function buildPositions(positions) {
  els.posBody.innerHTML = "";
  posRows = {}; posCandidates = {}; srcFound = 0;
  positions.forEach((p, i) => makePosRow(p, i + 1));
  els.cTotal.textContent = positions.length;
}

async function restoreSession() {
  let d;
  try { d = await apiJson("/api/session"); }
  catch (e) { setStatusMsg("Прошлая сессия не восстановлена: " + (e.message || e), "WARNING"); return; }
  if (!d || !d.ok || !d.items || !d.items.length) return;
  buildPositions(d.items.map((it) => ({ row: it.row, name: it.name })));
  for (const it of d.items) {
    const list = (it.candidates || []).map((c) => ({
      url: c.url, domain: c.domain, title: c.title, snippet: c.snippet, engine: c.engine,
      tier: c.tier, weight: c.weight, score: c.score, is_file: c.is_file }));
    posCandidates[it.row] = { list, queries: it.queries || [] };
    for (const c of (it.candidates || [])) {
      siteByUrl[c.url] = { status: c.status, chars: c.chars, found: c.found, match: c.match,
                           price: c.price, comment: c.comment, note: c.note, domain: c.domain };
      if (c.blocked) fetchByUrl[c.url] = { blocked: c.blocked, chars: c.chars };
    }
    restoreStages(it.row, it);                    // восстановить цветные кружки стадий
    if (it.active_s !== undefined && it.active_s !== null) setPositionTime(it.row, it.active_s);
    if (it.verdict) {
      verdictByRow[it.row] = flatVerdict(it.verdict, it.not_found_reason, it.on_request);
      renderVerdictCell(it.row, verdictByRow[it.row]);   // проставит «готово»/«не найдено»
    } else if (list.length) {
      const cell = posRows[it.row].querySelector(".c-price");
      cell.innerHTML = `${list.length} ист. <span class="caret">▾</span>`; cell.classList.add("has");
      setRowState(it.row, "не завершено", "warn");   // прошлый прогон не довёл товар до свода
    } else {
      setRowState(it.row, "ожидает");               // стадии восстановлены, но работа не начиналась
    }
  }
  recountVerdicts();
  // Прогон продолжается в фоне (страницу могли просто перезагрузить) — таймер идёт дальше
  // от исходной отметки, а не с нуля.
  if (d.running && d.started_at) startElapsed(d.started_at);
  els.runBtn.disabled = false; els.resetBtn.disabled = false;
  els.reportBtn.disabled = Object.keys(verdictByRow).length === 0;
  els.dzText.textContent = (d.filename || "прошлая сессия") + " · восстановлено · загрузите другой";
  setStatusMsg("Восстановлена прошлая сессия: " + d.items.length + " товаров (продолжайте кнопкой «Найти»)", "INFO");
}

// Восстановить цветные кружки стадий (query/fetch/extract/match) из статусов кандидатов.
function restoreStages(row, it) {
  const cands = (it.candidates || []).filter((c) => !c.is_file);
  const term = (s) => ["done", "empty", "blocked", "error"].includes(s);
  const st = cands.map((c) => c.status || "discovered");
  if ((it.candidates || []).length || (it.queries || []).length) setStage(row, "query", "done");
  if (st.some((s) => s !== "discovered")) setStage(row, "fetch", st.every(term) ? "done" : "active");
  if (st.some((s) => ["done", "empty"].includes(s))) setStage(row, "extract", st.every(term) ? "done" : "active");
  if (it.verdict) {
    setStage(row, "fetch", "done"); setStage(row, "extract", "done");
    setStage(row, "match", (verdictByRow[row] && verdictByRow[row].primary != null) ? "done" : "error");
  }
}

// ---- Инициализация ---------------------------------------------------------
(function init() {
  loadModels(); loadUsage(); openEvents(); loadProfile();
  openChat(); loadChats();                   // чат-исследователь (M9): диалог доступен сразу
  syncModeUI();                              // подпись кнопки «Режим» из сохранённого выбора
  syncCleanUI();                             // состояние вкладки «Сценарий очистки»
  els.logAutoscroll.style.opacity = "1";
  restoreSession();                          // подтянуть прошлую сессию (кэш между запусками)
})();

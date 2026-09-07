// =============================================================================
// i18n.js — Apply the baked language strings to the shell.
//
// The generator bakes the chosen language's string map (config.i18n) into
// data.js. This module reads it and rewrites the static index.html chrome at
// load time:
//   - [data-i18n="key"]      → element.textContent     = T(key)
//   - [data-i18n-ph="key"]   → element.placeholder     = T(key)
// English text in index.html stays as the in-markup fallback, so a fork with no
// translation renders correctly with zero baked config.
// =============================================================================

import { config } from "./state.js";

const LANG_STORAGE_KEY = "dashboard_lang";
const ALL = (config && config.i18n_all) || null;

/** Languages the dashboard can switch between (from the baked i18n_all). */
export function availableLanguages() {
  return ALL ? Object.keys(ALL) : [(config && config.language) || "en"];
}

/**
 * Pure resolver for the active UI language. Order:
 *   1. an explicit user toggle saved in localStorage (only if we bundle it),
 *   2. else the server-baked default `config.language` — which the generator
 *      sets from the profile's product language (## OUTPUT_LANGUAGE),
 *   3. else "en".
 * Kept side-effect-free so it unit-tests without DOM/localStorage.
 */
export function pickLanguage(saved, configLang, all) {
  if (saved && all && all[saved]) return saved;
  return configLang || "en";
}

/** The active UI language: user's saved toggle, else the profile-baked default. */
export function getLanguage() {
  var saved = null;
  try {
    saved = localStorage.getItem(LANG_STORAGE_KEY);
  } catch (e) {
    saved = null;
  }
  return pickLanguage(saved, config && config.language, ALL);
}

/** Intl locale for the active dashboard language (date/number formatting). */
export function dateLocale() {
  return getLanguage() === "ru" ? "ru-RU" : "en-US";
}

/** Persist a language choice and reload so every view re-renders in it. */
export function setLanguage(lang) {
  if (lang === getLanguage()) return;
  try {
    localStorage.setItem(LANG_STORAGE_KEY, lang);
  } catch (e) {
    /* ignore — the reload below still applies it for this load */
  }
  location.reload();
}

const STRINGS = (ALL && ALL[getLanguage()]) || (config && config.i18n) || {};

// Strings that ship with the browser code, not with the baked snapshot: the
// Screen view (bulk screening inbox) lives on the dashboard line, whose
// snapshot is written by a different branch. A baked string still wins.
const LOCAL = {
  en: {
    screen_tab: "Screen",
    screen_title: "Make one decision about several roles.",
    screen_list_to_screen: "To screen",
    screen_list_kept: "Kept",
    screen_list_aside: "Put aside",
    screen_group_language: "A language requirement",
    screen_group_onsite: "Onsite or location constraint",
    screen_group_seniority: "Seniority stated",
    screen_group_unclear: "Eligibility unclear",
    screen_group_all: "All remaining roles",
    screen_group_hint: "Groups filter the To screen list only.",
    screen_required: "Required",
    screen_preferred: "Preferred",
    screen_unknown: "Unknown",
    screen_evidence: "Read posting evidence",
    screen_no_quote: "no quote",
    screen_profile_notes: "Compared with your profile",
    screen_empty: "No roles left in this list.",
    screen_processing: "Not prepared yet: {unprepared} · Failed: {failed}",
    screen_selected: "{n} selected",
    screen_select_all: "Select all",
    screen_clear: "Clear selection",
    screen_keep: "Keep",
    screen_put_aside: "Put aside",
    screen_undo: "Undo",
    screen_saved: "{n} of {m} saved",
    screen_undone: "{n} of {m} restored",
    screen_loading: "Loading statuses…",
    screen_open: "Open",
  },
};
Object.assign(LOCAL.en, {
  screen_take: "Can I take it?",
  screen_do: "Can I do it?",
  screen_enjoy: "Would I enjoy it?",
  screen_activity: "What would I spend my week doing?",
  screen_all_values: "Any",
  screen_building: "Building and launching",
  screen_running: "Running and improving",
  screen_selling: "Selling and relationships",
  screen_specialist: "Specialist analysis and production",
  screen_unclassified: "Work details not prepared",
  screen_work_unprepared: "Work details not prepared",
  screen_coordination: "Coordinate technical work",
  screen_practical: "Practical tools and automation",
  screen_technical_specialist: "Specialist technical expertise",
  screen_unknown: "Unknown / not stated",
  screen_direct_impact: "Direct social impact",
  screen_enabling_impact: "Enabling social impact",
  screen_commercial: "Commercial outcomes",
  screen_purpose: "Purpose of the work",
  screen_technical: "Technical depth",
  screen_seniority_filter: "Seniority",
  screen_age: "First seen",
  screen_last7: "Within 7 days",
  screen_last14: "Within 14 days",
  screen_last30: "Within 30 days",
  screen_older30: "More than 30 days ago",
  screen_deadline_filter: "Application deadline",
  screen_no_passed_deadline: "No passed deadline",
  screen_expired: "Deadline passed",
  screen_first_seen_days: "First seen {n}d ago",
  screen_deadline_passed: "Deadline passed: {date}",
  screen_work_mode: "Work arrangement",
  screen_remote: "Remote",
  screen_hybrid: "Hybrid",
  screen_onsite: "Onsite",
  screen_requirement_kind: "Requirement type",
  screen_strength_filter: "Requirement strength",
  screen_requirement_text: "Requirement name contains",
  screen_requirement_placeholder: "Language, location, skill…",
  screen_language: "Language",
  screen_location: "Location",
  screen_authorisation: "Work authorisation",
  screen_domain: "Domain knowledge",
  screen_skill: "Skill",
  screen_experience: "Experience",
  screen_education: "Education",
  screen_other: "Other",
  screen_required: "Required",
  screen_preferred: "Preferred",
  screen_finding: "Compared with my profile",
  screen_match: "Evidence in profile",
  screen_possible_conflict: "Possible conflict",
  screen_search: "Title or company",
  screen_filter_hint:
    "Filters combine. Requirement filters refer to the same requirement; text searches its name, not the quote. Unknown is not a rejection reason.",
  screen_clear_filters: "Clear filters",
  screen_matches: "{n} matching · {m} in this list",
  screen_work_availability:
    "{n} roles in this list have no work details yet. Requirement filters still work.",
  screen_page: "Page {n} of {m}",
  screen_previous: "Previous",
  screen_next: "Next batch",
  screen_select_page: "Select this page",
  screen_flow_title: "Find a batch with a shared reason to decide.",
});
LOCAL.ru = {
  screen_age: "Когда впервые найдена",
  screen_last7: "За последние 7 дней",
  screen_last14: "За последние 14 дней",
  screen_last30: "За последние 30 дней",
  screen_older30: "Больше 30 дней назад",
  screen_deadline_filter: "Срок подачи",
  screen_no_passed_deadline: "Нет истёкшего срока",
  screen_expired: "Срок подачи истёк",
  screen_first_seen_days: "Найдена {n} дн. назад",
  screen_deadline_passed: "Срок подачи истёк: {date}",
  screen_take: "Могу ли я принять эту работу?",
  screen_do: "Справлюсь ли я с работой?",
  screen_enjoy: "Хочу ли я этим заниматься?",
  screen_activity: "Чем я буду заниматься в течение недели?",
  screen_all_values: "Любой вариант",
  screen_building: "Создавать и запускать",
  screen_running: "Управлять и улучшать",
  screen_selling: "Продавать и развивать отношения",
  screen_specialist: "Выполнять специализированную работу",
  screen_unclassified: "Характер работы ещё не разобран",
  screen_work_unprepared: "Характер работы ещё не разобран",
  screen_coordination: "Координация технической работы",
  screen_practical: "Инструменты и автоматизация",
  screen_technical_specialist: "Глубокая техническая экспертиза",
  screen_unknown: "Неизвестно / не указано",
  screen_direct_impact: "Прямой общественно полезный результат",
  screen_enabling_impact: "Обеспечивать общественно полезную работу",
  screen_commercial: "Коммерческий результат",
  screen_purpose: "Ради какого результата работа",
  screen_technical: "Техническая глубина",
  screen_seniority_filter: "Уровень роли",
  screen_work_mode: "Формат работы",
  screen_remote: "Удалённо",
  screen_hybrid: "Гибрид",
  screen_onsite: "В офисе",
  screen_requirement_kind: "Тип требования",
  screen_strength_filter: "Насколько обязательно",
  screen_requirement_text: "Слова в названии требования",
  screen_requirement_placeholder: "Язык, место, навык…",
  screen_language: "Язык",
  screen_location: "Место работы",
  screen_authorisation: "Право на работу",
  screen_skill: "Навык",
  screen_experience: "Опыт",
  screen_education: "Образование",
  screen_domain: "Профессиональная область",
  screen_other: "Другое",
  screen_required: "Обязательно",
  screen_preferred: "Желательно",
  screen_finding: "Сравнение с моим профилем",
  screen_supported: "Есть подтверждение в профиле",
  screen_match: "Есть подтверждение в профиле",
  screen_possible_conflict: "Возможное несоответствие",
  screen_search: "Название или компания",
  screen_filter_hint:
    "Фильтры сочетаются для одного требования. Поиск — по названию требования. Неизвестное — не причина для отказа.",
  screen_clear_filters: "Сбросить фильтры",
  screen_matches: "Под фильтры: {n} · Всего в списке: {m}",
  screen_work_availability:
    "У {n} вакансий ещё не разобран характер работы. Фильтры по требованиям уже работают.",
  screen_page: "Страница {n} из {m}",
  screen_previous: "Назад",
  screen_next: "Следующая группа",
  screen_select_page: "Выбрать эту страницу",
  screen_flow_title: "Разберите вакансии по характеру работы.",
  screen_batches: "Группы для отбора",
  screen_junior: "Младший специалист",
  screen_mid: "Специалист",
  screen_senior: "Старший специалист",
  screen_head: "Руководитель",
  screen_director: "Директор",
  screen_executive: "Высшее руководство",
  screen_work_evidence: "Почему так описана работа",
  screen_more_requirements: "Ещё требований: {n}",
};
Object.assign(LOCAL.ru, {
  screen_check_requirement: "Проверить требование",
  screen_check_conflict: "Проверить возможное несоответствие",
  screen_check_fit: "Соответствие профилю стоит проверить",
  screen_check_location: "Проверить место работы и остальные требования",
  screen_past_notes: "История причин",
  screen_note_reviewed: "Разобрано с ИИ",
  screen_note_pending: "Ожидает разбора с ИИ",
  screen_no_notes: "Причин пока нет.",
  screen_notes_failed: "Не удалось загрузить причины. Попробуйте ещё раз.",
  screen_review_title: "Разберём несколько вакансий вместе",
  screen_batch_product: "Продукт",
  screen_batch_operations: "Операции и управление",
  screen_batch_research: "Исследования",
  screen_batch_programmes: "Программы и проекты",
  screen_batch_partnerships: "Фандрайзинг и партнёрства",
  screen_batch_other: "Другие роли",
  screen_batch_reason_product: "Управление и развитие продуктов",
  screen_batch_reason_operations:
    "Операционная работа и координация организации",
  screen_batch_reason_research: "Исследования, анализ и оценка",
  screen_batch_reason_programmes: "Реализация программ и проектов",
  screen_batch_reason_partnerships: "Фандрайзинг и внешние партнёрства",
  screen_batch_reason_other: "Характер работы стоит уточнить",
  screen_roles: "вакансий",
  screen_recover_hint: "Решение можно изменить.",
  screen_batch_showing: "Показаны {start}–{end} из {total} в этой группе",
  screen_review_later: "Позже · следующие",
  screen_reason_label: "Почему? Необязательно — сохраним вместе с решением.",
  screen_feedback_failed:
    "Решения сохранены, но причина — нет. Повторите сохранение причины.",
  screen_feedback_saved:
    "Причина сохранена · ожидает разбора с ИИ. Предпочтения не изменены.",
  screen_retry: "Повторить",
  screen_preparation: "Подготовка вакансий",
  screen_save_failed:
    "Не удалось завершить сохранение. Проверьте список перед повтором.",
});
const LOCAL_STRINGS = { ...LOCAL.en, ...(LOCAL[getLanguage()] || {}) };

function has(table, key) {
  return Object.prototype.hasOwnProperty.call(table, key);
}

/** Translate a stable key. Returns the baked string, else the fallback. */
export function T(key, fallback) {
  if (has(STRINGS, key)) return STRINGS[key];
  if (has(LOCAL_STRINGS, key)) return LOCAL_STRINGS[key];
  return fallback !== undefined ? fallback : key;
}

/** Apply all [data-i18n*] translations to the document. */
export function applyI18n() {
  // Document language attribute (affects screen readers, hyphenation).
  if (STRINGS.html_lang) document.documentElement.lang = STRINGS.html_lang;
  if (STRINGS.page_title) document.title = STRINGS.page_title;

  // Text content.
  document.querySelectorAll("[data-i18n]").forEach(function (el) {
    var key = el.getAttribute("data-i18n");
    var text = T(key, null);
    if (text !== null) el.textContent = text;
  });

  // Placeholders (search inputs).
  document.querySelectorAll("[data-i18n-ph]").forEach(function (el) {
    var key = el.getAttribute("data-i18n-ph");
    if (Object.prototype.hasOwnProperty.call(STRINGS, key)) {
      el.setAttribute("placeholder", STRINGS[key]);
    }
  });
}

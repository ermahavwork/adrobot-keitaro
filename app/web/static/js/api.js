// Клиент к API AdRobot. Одна функция `api()`: таймаут, единый формат ошибок,
// токен доступа (если включён на сервере) и имя пользователя для журнала операций.

// Каталог, из которого открыт интерфейс: "/" при обычном запуске, "/adrobot/" за обратным
// прокси в подкаталоге. Все пути API строим от него, поэтому приложению всё равно, где оно стоит.
const BASE = new URL(".", document.baseURI).pathname;
export const url = (path) => BASE + String(path).replace(/^\//, "");

const TOKEN_KEY = "adrobot.token";
const USER_KEY = "adrobot.user";

export const session = {
  readOnly: false, // токен «только чтение»: выставляет app.js по ответу /api/auth/me
  get token() { return sessionStorage.getItem(TOKEN_KEY) || ""; },
  set token(value) { value ? sessionStorage.setItem(TOKEN_KEY, value) : sessionStorage.removeItem(TOKEN_KEY); },
  get user() { return localStorage.getItem(USER_KEY) || ""; },
  set user(value) { value ? localStorage.setItem(USER_KEY, value) : localStorage.removeItem(USER_KEY); },
};

export class ApiError extends Error {
  constructor(message, { code = "error", status = 0, details = null } = {}) {
    super(message);
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

/**
 * api("POST", "/api/streams/5/push", {force: true})
 * Возвращает разобранный JSON. Любой неуспех — исключение ApiError с текстом для человека.
 */
export async function api(method, path, body, { headers = {}, timeoutMs = 60000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const requestHeaders = { Accept: "application/json", ...headers };
  if (body !== undefined) requestHeaders["Content-Type"] = "application/json";
  if (session.token) requestHeaders.Authorization = `Bearer ${session.token}`;
  // Режем ДО кодирования: иначе percent-последовательность кириллицы обрывается посередине.
  if (session.user) requestHeaders["X-AdRobot-User"] = encodeURIComponent(session.user.slice(0, 64));

  let response;
  try {
    response = await fetch(url(path), {
      method, headers: requestHeaders, signal: controller.signal, cache: "no-store",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    throw new ApiError(
      error.name === "AbortError"
        ? "Сервер AdRobot не ответил вовремя. Проверьте, что он запущен, и повторите."
        : "Нет связи с сервером AdRobot. Проверьте, что он запущен.",
      { code: "network" });
  } finally {
    clearTimeout(timer);
  }

  let payload = null;
  try { payload = await response.json(); } catch { /* пустое тело или не JSON */ }

  if (!response.ok) {
    const envelope = payload && payload.error ? payload.error : {};
    if (response.status === 401) window.dispatchEvent(new CustomEvent("adrobot:auth-required"));
    throw new ApiError(envelope.message || `Ошибка ${response.status}`, {
      code: envelope.code || "http_error", status: response.status, details: envelope.details ?? null });
  }
  return payload;
}

export const newIdempotencyKey = () =>
  (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`);

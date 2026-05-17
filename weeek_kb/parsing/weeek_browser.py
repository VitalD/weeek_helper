"""Playwright: вход в WEEEK и сбор комментариев с карточки задачи (DOM)."""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any

try:
    from playwright.sync_api import Page, sync_playwright
except ImportError:
    Page = None  # type: ignore[misc, assignment]
    sync_playwright = None  # type: ignore[assignment]

DOM_COMMENT_EXTRACTOR = """
() => {
  const items = Array.from(document.querySelectorAll('div.comment[id^="task-comment-"]'));
  function dateFromOlderSiblings(el) {
    let n = el.previousElementSibling;
    let hops = 0;
    while (n && hops < 400) {
      if (n.classList && n.classList.contains('date')) {
        return (n.textContent || '').replace(/\\s+/g, ' ').trim();
      }
      n = n.previousElementSibling;
      hops++;
    }
    const parent = el.parentElement;
    if (!parent) return null;
    const kids = Array.from(parent.children);
    const idx = kids.indexOf(el);
    for (let i = idx - 1; i >= 0; i--) {
      const x = kids[i];
      if (x.classList && x.classList.contains('date')) {
        return (x.textContent || '').replace(/\\s+/g, ' ').trim();
      }
    }
    return null;
  }
  function authorFromComment(el) {
    const sels = [
      '.comment__author',
      '.comment__author-name',
      '.comment__user-name',
      '.comment-user-name',
      '.comment__header .name',
      '.comment__header [class*="name"]',
      '[class*="CommentAuthor"]',
      '[class*="userName"]',
      'a[href*="/ws/"][href*="/user/"]',
      'a[href*="/profile"]',
    ];
    for (let i = 0; i < sels.length; i++) {
      const n = el.querySelector(sels[i]);
      if (n && n.textContent) {
        const t = (n.textContent || '').replace(/\\s+/g, ' ').trim();
        if (t) return t;
      }
    }
    const hdr = el.querySelector('.comment__header, .comment-header, header');
    if (hdr) {
      const t = (hdr.innerText || '').split('\\n').map(function (s) { return s.trim(); }).filter(Boolean)[0];
      if (t) return t;
    }
    return null;
  }
  return items.map((el) => {
    const editor = el.querySelector('.editor__html') || el.querySelector('.comment__editor');
    const timeEl = el.querySelector('.comment__time');
    const dateHeading = dateFromOlderSiblings(el);
    const author = authorFromComment(el);
    const html = editor ? editor.innerHTML : null;
    const text = editor
      ? (editor.innerText || '').replace(/\\s+/g, ' ').trim()
      : (el.innerText || '').trim();
    const m = (el.id || '').match(/^task-comment-(\\d+)$/);
    return {
      commentDomId: el.id || null,
      commentId: m ? m[1] : null,
      author: author,
      dateHeading: dateHeading,
      time: timeEl ? (timeEl.textContent || '').trim() : null,
      html: html,
      text: text,
      _source: 'dom'
    };
  });
}
"""

COMMENT_SELECTOR = "div.comment[id^='task-comment-']"


def _is_transient_navigation_error(exc: BaseException) -> bool:
    if type(exc).__name__ == "TargetClosedError":
        return False
    msg = str(exc).lower()
    needles = (
        "err_network_changed",
        "err_internet_disconnected",
        "err_connection_reset",
        "err_connection_refused",
        "err_connection_aborted",
        "err_address_unreachable",
        "err_name_not_resolved",
        "navigation timeout",
        "timeout 120000ms exceeded",
        "net::err_",
    )
    return any(n in msg for n in needles)


def goto_with_retries(
    page: Page,
    url: str,
    *,
    wait_until: str = "load",
    timeout_ms: int = 120_000,
    max_attempts: int = 6,
) -> None:
    last: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            return
        except Exception as e:
            last = e
            if not _is_transient_navigation_error(e) or attempt >= max_attempts:
                raise
            delay_ms = min(45_000, 2_000 * (2 ** (attempt - 1)))
            print(
                f"  [retry] goto {attempt}/{max_attempts}: {e!s} — ждём {delay_ms} мс",
                file=sys.stderr,
                flush=True,
            )
            try:
                page.wait_for_timeout(delay_ms)
            except Exception:
                pass
    assert last is not None
    raise last


def _click_first(page: Page, selectors: tuple[str, ...], *, timeout: int = 6000) -> bool:
    for sel in selectors:
        try:
            page.click(sel, timeout=timeout)
            return True
        except Exception:
            continue
    return False


# Кнопка входа в форме WEEEK (после email и после пароля)
_LOGIN_BUTTON_SELECTORS = (
    "button.button:not([disabled])",
    "button.button",
    'button[type="submit"]:not([disabled])',
    'button[type="submit"]',
    'button:has-text("Войти")',
    'button:has-text("Login")',
    'button:has-text("Далее")',
    'button:has-text("Continue")',
    'button:has-text("Продолжить")',
    "form button",
)


def _click_login_button(page: Page) -> bool:
    """Нажать кнопку отправки формы WEEEK (в т.ч. button.button)."""
    for sel in _LOGIN_BUTTON_SELECTORS:
        try:
            loc = page.locator(sel)
            count = loc.count()
            if count == 0:
                continue
            for i in range(min(count, 5)):
                btn = loc.nth(i)
                try:
                    if not btn.is_visible(timeout=3000):
                        continue
                except Exception:
                    continue
                try:
                    if btn.is_disabled(timeout=500):
                        continue
                except Exception:
                    pass
                btn.scroll_into_view_if_needed(timeout=3000)
                btn.click(timeout=10_000)
                return True
        except Exception:
            continue
    return False


def _fill_email_step(page: Page, email: str) -> None:
    email_sel = 'input[type="email"], input[name="email"], input[autocomplete="username"]'
    page.wait_for_selector(email_sel, timeout=30_000)
    page.fill(email_sel, email)


def _submit_email_step(page: Page) -> None:
    page.wait_for_timeout(400)
    if _click_login_button(page):
        return
    page.keyboard.press("Enter")


def _fill_password_step(page: Page, password: str) -> None:
    page.wait_for_selector('input[type="password"]', state="visible", timeout=30_000)
    page.fill('input[type="password"]', password)
    page.wait_for_timeout(400)


def _submit_password_step(page: Page) -> None:
    try:
        page.wait_for_selector(
            "button.button:not([disabled])",
            state="visible",
            timeout=15_000,
        )
    except Exception:
        pass
    if _click_login_button(page):
        return
    if _click_first(page, ("button.button",), timeout=5000):
        return
    page.keyboard.press("Enter")


def login_with_email_password(page: Page, email: str, password: str, ws_id: int) -> None:
    """Двухшаговый вход: email → пароль (поле пароля после логина)."""
    login_urls = (
        "https://app.weeek.net/signin",
        "https://app.weeek.net/login",
        "https://app.weeek.net/auth/signin",
        "https://app.weeek.net/",
    )
    for u in login_urls:
        try:
            page.goto(u, wait_until="domcontentloaded")
        except Exception:
            continue
        try:
            _fill_email_step(page, email)
            break
        except Exception:
            for sel in (
                'a:has-text("Войти")',
                'button:has-text("Войти")',
                'a:has-text("Login")',
            ):
                try:
                    page.click(sel, timeout=1500)
                    _fill_email_step(page, email)
                    break
                except Exception:
                    continue
            else:
                continue
            break
    else:
        raise RuntimeError("Не найдено поле email на странице входа WEEEK")

    _submit_email_step(page)
    _fill_password_step(page, password)
    _submit_password_step(page)

    print("Вход отправлен, ожидаем интерфейс…", flush=True)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=25_000)
    except Exception:
        pass
    page.wait_for_timeout(1200)

    u = page.url or ""
    if f"/ws/{ws_id}" not in u:
        print(f"Открываю воркспейс /ws/{ws_id}/ …", flush=True)
        page.goto(
            f"https://app.weeek.net/ws/{ws_id}/",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
    _wait_workspace_ready(page, ws_id)
    print("Воркспейс готов.", flush=True)


def _wait_workspace_ready(page: Page, ws_id: int, *, max_seconds: int = 20) -> None:
    """Короткое ожидание URL воркспейса (без networkidle)."""
    for i in range(max_seconds * 5):
        u = page.url or ""
        on_ws = f"/ws/{ws_id}" in u
        not_login = "signin" not in u.lower() and "/welcome" not in u.lower() and "/login" not in u.lower()
        if on_ws and not_login:
            try:
                pl = page.locator('input[type="password"]')
                if pl.count() > 0 and pl.first.is_visible(timeout=300):
                    pass
                else:
                    return
            except Exception:
                return
        if i and i % 10 == 0:
            print(f"  … ждём воркспейс ({i // 5} с)", flush=True)
        page.wait_for_timeout(200)
    raise RuntimeError(f"Не удалось открыть воркспейс /ws/{ws_id}/ после входа (URL: {page.url})")


def _session_valid(page: Page, ws_id: int) -> bool:
    u = page.url or ""
    if "/welcome" in u or "signin" in u or "/login" in u.lower():
        return False
    try:
        pl = page.locator('input[type="password"]')
        if pl.count() > 0 and pl.first.is_visible(timeout=2000):
            return False
    except Exception:
        pass
    return f"/ws/{ws_id}" in u or "app.weeek.net" in u


def start_browser(
    *,
    ws_id: int,
    headless: bool,
    cookie_header: str,
    email: str,
    password: str,
    manual_login: bool,
    session_file: Path | None,
):
    if sync_playwright is None:
        print(
            "Установите: pip install playwright && playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(2)

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=headless)
    session_path = (
        session_file if session_file and session_file.is_file() and not manual_login else None
    )

    if cookie_header.strip():
        context = browser.new_context(
            locale="ru-RU",
            extra_http_headers={"Cookie": cookie_header.strip()},
        )
    elif session_path:
        context = browser.new_context(locale="ru-RU", storage_state=str(session_path))
    else:
        context = browser.new_context(locale="ru-RU")

    page = context.new_page()

    if session_path:
        print(f"Загрузка сессии из {session_path}…", flush=True)
        page.goto(
            f"https://app.weeek.net/ws/{ws_id}/",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        page.wait_for_timeout(800)
        if not _session_valid(page, ws_id):
            browser.close()
            pw.stop()
            raise RuntimeError(
                "Сессия устарела. Запустите с --manual-login или войдите заново (email/password)."
            )
        print(f"Сессия загружена из {session_path}", flush=True)
        return pw, browser, context, page

    if manual_login:
        print("Откройте Chromium и войдите в WEEEK вручную.", flush=True)
        try:
            page.goto(f"https://app.weeek.net/ws/{ws_id}/", wait_until="domcontentloaded")
        except Exception:
            pass
        for _ in range(900):
            if _session_valid(page, ws_id):
                print("Вход подтверждён.", flush=True)
                return pw, browser, context, page
            page.wait_for_timeout(1000)
        browser.close()
        pw.stop()
        raise RuntimeError("Не дождались ручного входа.")
    elif cookie_header.strip():
        page.goto(f"https://app.weeek.net/ws/{ws_id}/", wait_until="domcontentloaded")
        if not _session_valid(page, ws_id):
            browser.close()
            pw.stop()
            raise RuntimeError("Cookie недействителен для воркспейса.")
        return pw, browser, context, page

    if not email or not password:
        browser.close()
        pw.stop()
        raise RuntimeError("Нужны WEEEK_LOGIN/WEEEK_PASSWORD, cookie или --manual-login")

    print("Вход в WEEEK по email/password…", flush=True)
    login_with_email_password(page, email, password, ws_id)
    return pw, browser, context, page


def harvest_task_comments(page: Page, url: str, wait_ms: int) -> list[dict[str, Any]]:
    """Сбор комментариев — порядок действий как в RNP weeek_enrich_comments.py."""
    print(f"  открываю карточку…", flush=True)
    goto_with_retries(page, url, wait_until="domcontentloaded", timeout_ms=60_000)
    page.wait_for_timeout(500)

    try:
        page.wait_for_selector(
            "div.comment[id^='task-comment-']",
            state="attached",
            timeout=min(max(wait_ms * 5, 4000), 35000),
        )
    except Exception:
        pass

    page.wait_for_timeout(wait_ms)

    try:
        last = page.locator("div.comment[id^='task-comment-']").last
        if last.count() == 0:
            last = page.locator("div.comment[id^='task-comment-']").last
        if last.count() > 0:
            last.scroll_into_view_if_needed(timeout=3000)
            page.wait_for_timeout(250)
    except Exception:
        pass

    try:
        page.evaluate("() => { window.scrollTo(0, document.body.scrollHeight); }")
        page.keyboard.press("End")
        page.wait_for_timeout(350)
        page.evaluate("() => { window.scrollTo(0, document.body.scrollHeight); }")
    except Exception:
        pass

    try:
        tab = page.get_by_role("tab", name=re.compile(r"коммент", re.I))
        if tab.count():
            tab.first.click(timeout=3000)
            page.wait_for_timeout(400)
    except Exception:
        pass

    final_url = page.url or ""
    if "/welcome" in final_url:
        raise RuntimeError(
            "Открылась /welcome — сессия недействительна. Перелогиньтесь (--manual-login)."
        )

    dom_list: list[dict[str, Any]] = []
    try:
        raw = page.evaluate(DOM_COMMENT_EXTRACTOR)
        if isinstance(raw, list):
            dom_list = [x for x in raw if isinstance(x, dict)]
    except Exception:
        dom_list = []

    if not dom_list:
        try:
            dbg = page.evaluate(
                """() => ({
  href: location.href,
  hasLoginForm: !!document.querySelector('form.Nm'),
  commentNodes: document.querySelectorAll('div.comment').length,
  taskCommentIds: document.querySelectorAll('[id^="task-comment-"]').length,
})"""
            )
            print(f"  [debug] нет комментариев в DOM: {dbg}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"  [debug] оценка страницы: {e}", file=sys.stderr, flush=True)

    return dom_list


def save_session(context: Any, session_file: Path) -> None:
    session_file.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(session_file))

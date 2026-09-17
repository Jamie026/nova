"""Control del navegador con Playwright sobre CDP.

Por qué CDP y no `playwright.chromium.launch()`: lanzar el Chromium de Playwright
da un navegador estéril, sin sesiones iniciadas ni extensiones, y abre una ventana
distinta de la que el usuario ya está mirando. Aquí se lanza el Chrome real del
sistema con --remote-debugging-port y se conecta por CDP, así el usuario ve
exactamente lo que el agente hace y las sesiones persisten.

Se usa un perfil dedicado (%APPDATA%/Nova/chrome-profile) porque Chrome rechaza
abrir el puerto de depuración sobre un perfil ya en uso. El usuario inicia sesión
una vez en ese perfil y queda guardado.

La interacción es por rol, texto y selectores CSS — nunca por coordenadas X/Y,
que se rompen con cualquier cambio de resolución o de layout.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from urllib.parse import quote_plus

from ..paths import chrome_profile_dir

log = logging.getLogger("nova.browser")

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


@dataclass(slots=True)
class TabInfo:
    index: int
    title: str
    url: str
    active: bool


class BrowserController:
    def __init__(self, channel: str = "chrome", cdp_port: int = 9222,
                 attach_timeout_s: int = 20,
                 search_template: str = "https://www.google.com/search?q={query}"):
        self._channel = channel
        self._port = cdp_port
        self._timeout = attach_timeout_s
        self._search = search_template
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ ciclo de vida
    def _executable(self) -> str | None:
        candidates = EDGE_PATHS if self._channel == "msedge" else CHROME_PATHS
        for path in candidates:
            if os.path.exists(path):
                return path
        return shutil.which("chrome") or shutil.which("msedge")

    async def _launch_browser_process(self) -> None:
        executable = self._executable()
        if not executable:
            raise RuntimeError(f"No encuentro {self._channel} instalado en el sistema")
        subprocess.Popen(
            [executable, f"--remote-debugging-port={self._port}",
             f"--user-data-dir={chrome_profile_dir()}", "--no-first-run",
             "--no-default-browser-check", "--restore-last-session"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        log.info("navegador lanzado con puerto de depuración %d", self._port)

    def _alive(self) -> bool:
        """¿Sigue viva la conexión? `context.pages` NO lo dice.

        Es una propiedad síncrona que devuelve la lista cacheada sin tocar el
        navegador, así que con el navegador ya cerrado seguía «funcionando» y
        ensure() daba por buena una conexión muerta. El siguiente new_page()
        reventaba con «Target page, context or browser has been closed», una y
        otra vez, en cada orden. is_connected() sí pregunta de verdad.
        """
        try:
            return bool(self._browser and self._browser.is_connected()
                        and self._context is not None
                        and self._context in self._browser.contexts)
        except Exception:
            return False

    async def ensure(self) -> None:
        async with self._lock:
            if self._context is not None:
                if self._alive():
                    return
                log.info("el navegador se cerró; vuelvo a conectar")
                await self._teardown()

            from playwright.async_api import async_playwright

            if self._playwright is None:
                self._playwright = await async_playwright().start()

            endpoint = f"http://127.0.0.1:{self._port}"
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(endpoint, timeout=3000)
            except Exception:
                await self._launch_browser_process()
                deadline = asyncio.get_event_loop().time() + self._timeout
                last_error: Exception | None = None
                while asyncio.get_event_loop().time() < deadline:
                    await asyncio.sleep(0.5)
                    try:
                        self._browser = await self._playwright.chromium.connect_over_cdp(endpoint, timeout=3000)
                        break
                    except Exception as exc:
                        last_error = exc
                else:
                    raise RuntimeError(f"No pude conectar al navegador por CDP: {last_error}")

            self._context = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
            pages = self._context.pages
            self._page = pages[-1] if pages else await self._context.new_page()

    async def _teardown(self) -> None:
        for closer in (self._browser,):
            try:
                if closer:
                    await closer.close()
            except Exception:
                pass
        self._browser = self._context = self._page = None

    async def close(self) -> None:
        await self._teardown()
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    # ----------------------------------------------------------------- pestañas
    def _pages(self) -> list:
        return [p for p in (self._context.pages if self._context else []) if not p.is_closed()]

    async def list_tabs(self) -> list[TabInfo]:
        await self.ensure()
        tabs: list[TabInfo] = []
        for index, page in enumerate(self._pages()):
            try:
                title = await page.title()
            except Exception:
                title = ""
            tabs.append(TabInfo(index=index, title=title, url=page.url, active=(page is self._page)))
        return tabs

    async def new_tab(self, url: str | None = None) -> TabInfo:
        await self.ensure()
        page = await self._context.new_page()
        self._page = page
        if url:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.bring_to_front()
        return TabInfo(len(self._pages()) - 1, await page.title(), page.url, True)

    async def close_tab(self, index: int | None = None) -> bool:
        await self.ensure()
        pages = self._pages()
        page = self._page if index is None else (pages[index] if 0 <= index < len(pages) else None)
        if page is None:
            return False
        # Chrome se cierra entero al cerrar su última pestaña, y con él la
        # conexión. Abrir una en blanco antes evita que «cierra esa pestaña»
        # deje al agente sin navegador para la orden siguiente.
        if len(pages) <= 1:
            await self._context.new_page()
        await page.close()
        remaining = self._pages()
        self._page = remaining[-1] if remaining else None
        if self._page:
            await self._page.bring_to_front()
        return True

    async def switch_tab(self, index: int | None = None, match: str | None = None) -> TabInfo | None:
        await self.ensure()
        pages = self._pages()
        target = None
        if index is not None and 0 <= index < len(pages):
            target = pages[index]
        elif match:
            needle = match.lower()
            for page in pages:
                title = (await page.title()).lower()
                if needle in title or needle in page.url.lower():
                    target = page
                    break
        if target is None:
            return None
        self._page = target
        await target.bring_to_front()
        return TabInfo(pages.index(target), await target.title(), target.url, True)

    # -------------------------------------------------------------- navegación
    async def goto(self, url: str) -> TabInfo:
        await self.ensure()
        if not url.startswith(("http://", "https://", "file://")):
            url = "https://" + url
        if self._page is None:
            return await self.new_tab(url)
        await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return TabInfo(0, await self._page.title(), self._page.url, True)

    async def search(self, query: str, new_tab: bool = False) -> TabInfo:
        url = self._search.format(query=quote_plus(query))
        return await (self.new_tab(url) if new_tab else self.goto(url))

    async def go_back(self) -> bool:
        await self.ensure()
        if self._page is None:
            return False
        await self._page.go_back(wait_until="domcontentloaded", timeout=20000)
        return True

    async def scroll(self, direction: str = "down", amount: int = 600) -> bool:
        await self.ensure()
        if self._page is None:
            return False
        delta = amount if direction == "down" else -amount
        await self._page.mouse.wheel(0, delta)
        return True

    # ------------------------------------------------------------- interacción
    async def click(self, text: str) -> str:
        """Busca por rol accesible primero y sólo después por texto visible."""
        await self.ensure()
        page = self._page
        if page is None:
            raise RuntimeError("No hay pestaña activa")
        attempts = [
            page.get_by_role("link", name=text, exact=False),
            page.get_by_role("button", name=text, exact=False),
            page.get_by_label(text, exact=False),
            page.get_by_text(text, exact=False),
        ]
        for locator in attempts:
            try:
                element = locator.first
                await element.wait_for(state="visible", timeout=2500)
                await element.click(timeout=5000)
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                return f"Hice clic en «{text}»"
            except Exception:
                continue
        raise RuntimeError(f"No encontré ningún elemento clicable que diga «{text}»")

    async def click_result(self, position: int) -> str:
        """«Abre el segundo» sobre una página de resultados de búsqueda."""
        await self.ensure()
        page = self._page
        if page is None:
            raise RuntimeError("No hay pestaña activa")
        links = page.locator("#search a:has(h3), #rso a:has(h3), a:has(h3)")
        count = await links.count()
        if count == 0:
            links = page.get_by_role("link")
            count = await links.count()
        if position < 1 or position > count:
            raise RuntimeError(f"Sólo veo {count} resultados en esta página")
        href = await links.nth(position - 1).get_attribute("href")
        if not href:
            await links.nth(position - 1).click()
            return f"Abrí el resultado {position}"
        tab = await self.new_tab(href)
        return f"Abrí el resultado {position}: {tab.title}"

    async def type_text(self, text: str, into: str | None = None, submit: bool = False) -> str:
        await self.ensure()
        page = self._page
        if page is None:
            raise RuntimeError("No hay pestaña activa")
        if into:
            locator = page.get_by_label(into, exact=False).or_(page.get_by_placeholder(into, exact=False)).first
        else:
            locator = page.locator("input:visible, textarea:visible").first
        await locator.wait_for(state="visible", timeout=4000)
        await locator.fill(text)
        if submit:
            await locator.press("Enter")
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        return f"Escribí «{text}»"

    async def read_page(self, max_chars: int = 4000) -> str:
        await self.ensure()
        page = self._page
        if page is None:
            raise RuntimeError("No hay pestaña activa")
        text = await page.evaluate(
            """() => {
                const root = document.querySelector('main, article, #search, #rso') || document.body;
                return root.innerText.replace(/\\n{3,}/g, '\\n\\n').trim();
            }"""
        )
        return text[:max_chars]

    async def find_in_page(self, needle: str, context_chars: int = 320) -> str:
        content = await self.read_page(max_chars=20000)
        lowered = content.lower()
        position = lowered.find(needle.lower())
        if position < 0:
            return f"No encontré «{needle}» en esta página."
        start = max(0, position - context_chars // 2)
        return content[start:start + context_chars]

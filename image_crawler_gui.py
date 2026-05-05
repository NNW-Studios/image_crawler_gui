import json
import mimetypes
import queue
import re
import threading
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "Image Crawler Downloader"
CONFIG_PATH = Path.home() / ".image_crawler_downloader_config.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
DEFAULT_TYPES = "jpg,jpeg,png,webp,gif,svg"
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
DOWNLOAD_CHUNK_SIZE = 64 * 1024
IMAGE_EXTENSIONS = {
    "jpg",
    "jpeg",
    "png",
    "webp",
    "gif",
    "svg",
    "bmp",
    "ico",
    "avif",
    "jfif",
}
IMAGE_MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/avif": ".avif",
}


class ImageAssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.page_links: list[str] = []
        self.asset_links: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_dict = dict(attrs)
        if tag == "a":
            href = attrs_dict.get("href", "").strip()
            if href:
                self.page_links.append(href)
                if self._looks_like_image_link(href):
                    self.asset_links.append(href)

        if tag in {"img", "source"}:
            self._collect_asset_attrs(attrs_dict)

        for key in ("data-src", "data-original", "data-lazy-src", "data-image", "poster"):
            value = attrs_dict.get(key, "").strip()
            if value:
                self.asset_links.append(value)

        style = attrs_dict.get("style", "")
        if style:
            self.asset_links.extend(self._extract_style_urls(style))

    def _collect_asset_attrs(self, attrs_dict: dict[str, str]) -> None:
        for key in ("src", "data-src", "data-original", "data-lazy-src", "poster"):
            value = attrs_dict.get(key, "").strip()
            if value:
                self.asset_links.append(value)

        for key in ("srcset", "data-srcset"):
            value = attrs_dict.get(key, "").strip()
            if value:
                self.asset_links.extend(self._split_srcset(value))

    def _split_srcset(self, value: str) -> list[str]:
        results: list[str] = []
        for part in value.split(","):
            item = part.strip()
            if not item:
                continue
            results.append(item.split()[0].strip())
        return results

    def _extract_style_urls(self, style: str) -> list[str]:
        return [match[1].strip() for match in re.findall(r"url\((['\"]?)(.*?)\1\)", style, flags=re.IGNORECASE)]

    def _looks_like_image_link(self, value: str) -> bool:
        parsed = urllib.parse.urlparse(value)
        path = parsed.path.lower()
        return any(path.endswith(f".{ext}") for ext in IMAGE_EXTENSIONS)


class ImageCrawlerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("980x820")
        self.root.minsize(920, 740)

        self.start_url = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.file_types = tk.StringVar(value=DEFAULT_TYPES)
        self.max_pages = tk.StringVar(value="30")
        self.max_depth = tk.StringVar(value="1")
        self.same_domain_only = tk.BooleanVar(value=True)
        self.crawl_subpages = tk.BooleanVar(value=True)
        self.download_external_images = tk.BooleanVar(value=True)
        self.status_text = tk.StringVar(value="Bereit. Website eingeben, Ordner waehlen und Download starten.")

        self.is_running = False
        self.log_queue: queue.Queue[str] = queue.Queue()

        self._build_ui()
        self._load_config()
        self._poll_log_queue()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        container = ttk.Frame(self.root, padding=18)
        container.grid(row=0, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)
        container.rowconfigure(3, weight=1)
        container.rowconfigure(5, weight=1)

        ttk.Label(container, text=APP_TITLE, font=("Segoe UI", 18, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(
            container,
            text="Website-URL und Dateitypen eingeben, dann werden gefundene Bilder automatisch heruntergeladen.",
        ).grid(row=1, column=0, sticky="w", pady=(4, 16))

        settings = ttk.LabelFrame(container, text="Crawler-Setup", padding=14)
        settings.grid(row=2, column=0, sticky="ew")
        settings.columnconfigure(1, weight=1)

        ttk.Label(settings, text="Website-URL").grid(row=0, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.start_url).grid(row=0, column=1, sticky="ew", padx=(10, 10))

        ttk.Label(settings, text="Zielordner").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(settings, textvariable=self.output_dir).grid(row=1, column=1, sticky="ew", padx=(10, 10), pady=(10, 0))
        ttk.Button(settings, text="Ordner waehlen", command=self._choose_output_dir).grid(row=1, column=2, sticky="e", pady=(10, 0))

        ttk.Label(settings, text="Dateitypen").grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(settings, textvariable=self.file_types).grid(row=2, column=1, sticky="ew", padx=(10, 10), pady=(10, 0))

        ttk.Label(settings, text="Max. Seiten").grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.max_pages_entry = ttk.Entry(settings, textvariable=self.max_pages, width=10)
        self.max_pages_entry.grid(row=3, column=1, sticky="w", padx=(10, 10), pady=(10, 0))

        ttk.Label(settings, text="Max. Tiefe").grid(row=4, column=0, sticky="w", pady=(10, 0))
        self.max_depth_entry = ttk.Entry(settings, textvariable=self.max_depth, width=10)
        self.max_depth_entry.grid(row=4, column=1, sticky="w", padx=(10, 10), pady=(10, 0))

        ttk.Checkbutton(
            settings,
            text="Unterseiten mit crawlen",
            variable=self.crawl_subpages,
            command=self._toggle_crawl_options,
        ).grid(row=5, column=1, sticky="w", pady=(10, 0))

        self.same_domain_check = ttk.Checkbutton(
            settings,
            text="Nur auf derselben Domain bleiben",
            variable=self.same_domain_only,
        )
        self.same_domain_check.grid(row=6, column=1, sticky="w", pady=(10, 0))

        ttk.Checkbutton(
            settings,
            text="Auch externe Bild-CDNs erlauben",
            variable=self.download_external_images,
        ).grid(row=7, column=1, sticky="w", pady=(10, 0))

        help_frame = ttk.LabelFrame(container, text="Hinweise", padding=14)
        help_frame.grid(row=3, column=0, sticky="nsew", pady=(14, 0))
        help_frame.columnconfigure(0, weight=1)
        help_frame.rowconfigure(0, weight=1)

        self.info_text = tk.Text(help_frame, wrap="word", height=7, state="normal", font=("Consolas", 10))
        self.info_text.grid(row=0, column=0, sticky="nsew")
        help_scroll = ttk.Scrollbar(help_frame, orient="vertical", command=self.info_text.yview)
        help_scroll.grid(row=0, column=1, sticky="ns")
        self.info_text.configure(yscrollcommand=help_scroll.set)
        self.info_text.insert(
            "1.0",
            "Beispiele fuer Dateitypen: jpg,png,webp\n"
            "Die App sucht in img/src, srcset, lazy-load-Attributen, CSS-Backgrounds und direkten Bild-Links.\n"
            "Unterseiten werden optional mit durchsucht. Du kannst damit einzelne Seiten oder kleine Website-Bereiche absammeln.",
        )
        self.info_text.configure(state="disabled")

        actions = ttk.Frame(container)
        actions.grid(row=4, column=0, sticky="ew", pady=(14, 14))
        actions.columnconfigure(2, weight=1)

        self.start_button = ttk.Button(actions, text="Bilder crawlen und herunterladen", command=self._start)
        self.start_button.grid(row=0, column=0, sticky="w")
        ttk.Button(actions, text="Felder leeren", command=self._clear_fields).grid(row=0, column=1, sticky="w", padx=(10, 0))
        ttk.Label(actions, textvariable=self.status_text).grid(row=0, column=2, sticky="e")

        log_frame = ttk.LabelFrame(container, text="Log", padding=14)
        log_frame.grid(row=5, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, wrap="word", height=18, state="disabled", font=("Consolas", 10))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        self._toggle_crawl_options()

    def _choose_output_dir(self) -> None:
        current = self.output_dir.get().strip()
        initial_dir = current if current else str(Path.home())
        path = filedialog.askdirectory(title="Zielordner waehlen", initialdir=initial_dir)
        if path:
            self.output_dir.set(path)
            self._save_config()

    def _clear_fields(self) -> None:
        self.start_url.set("")
        self.output_dir.set("")
        self.file_types.set(DEFAULT_TYPES)
        self.max_pages.set("30")
        self.max_depth.set("1")
        self.same_domain_only.set(True)
        self.crawl_subpages.set(True)
        self.download_external_images.set(True)
        self._toggle_crawl_options()
        self._save_config()

    def _toggle_crawl_options(self) -> None:
        state = "normal" if self.crawl_subpages.get() else "disabled"
        self.max_pages_entry.configure(state=state)
        self.max_depth_entry.configure(state=state)
        self.same_domain_check.configure(state=state)
        self._save_config()

    def _log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_status(self, text: str) -> None:
        self.status_text.set(text)

    def _poll_log_queue(self) -> None:
        try:
            while True:
                self._log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(150, self._poll_log_queue)

    def _load_config(self) -> None:
        if not CONFIG_PATH.exists():
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        self.start_url.set(data.get("start_url", ""))
        self.output_dir.set(data.get("output_dir", ""))
        self.file_types.set(data.get("file_types", DEFAULT_TYPES))
        self.max_pages.set(data.get("max_pages", "30"))
        self.max_depth.set(data.get("max_depth", "1"))
        self.same_domain_only.set(bool(data.get("same_domain_only", True)))
        self.crawl_subpages.set(bool(data.get("crawl_subpages", True)))
        self.download_external_images.set(bool(data.get("download_external_images", True)))

    def _save_config(self) -> None:
        data = {
            "start_url": self.start_url.get().strip(),
            "output_dir": self.output_dir.get().strip(),
            "file_types": self.file_types.get().strip(),
            "max_pages": self.max_pages.get().strip(),
            "max_depth": self.max_depth.get().strip(),
            "same_domain_only": self.same_domain_only.get(),
            "crawl_subpages": self.crawl_subpages.get(),
            "download_external_images": self.download_external_images.get(),
        }
        try:
            CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _start(self) -> None:
        if self.is_running:
            return

        url = self.start_url.get().strip()
        output_dir = self.output_dir.get().strip()

        if not url:
            messagebox.showerror(APP_TITLE, "Bitte eine Website-URL eingeben.")
            return
        if not output_dir:
            messagebox.showerror(APP_TITLE, "Bitte einen Zielordner waehlen.")
            return

        normalized_url = self._normalize_page_url(url, url)
        if not normalized_url:
            messagebox.showerror(APP_TITLE, "Bitte eine gueltige http- oder https-URL eingeben.")
            return

        try:
            max_pages = max(1, int(self.max_pages.get().strip() or "1"))
            max_depth = max(0, int(self.max_depth.get().strip() or "0"))
        except ValueError:
            messagebox.showerror(APP_TITLE, "Max. Seiten und Max. Tiefe muessen ganze Zahlen sein.")
            return

        try:
            output_path = Path(output_dir)
            output_path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Zielordner konnte nicht erstellt werden:\n{exc}")
            return

        allowed_types = self._parse_allowed_types(self.file_types.get())
        if not allowed_types:
            messagebox.showerror(APP_TITLE, "Bitte mindestens einen Dateityp wie jpg,png,webp eingeben.")
            return

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._set_status("Crawler laeuft...")
        self.is_running = True
        self.start_button.configure(state="disabled")
        self._save_config()

        worker = threading.Thread(
            target=self._run_crawler,
            args=(normalized_url, output_path, allowed_types, max_pages, max_depth),
            daemon=True,
        )
        worker.start()

    def _finish(self) -> None:
        self.is_running = False
        self.start_button.configure(state="normal")

    def _run_crawler(
        self,
        start_url: str,
        output_dir: Path,
        allowed_types: set[str],
        max_pages: int,
        max_depth: int,
    ) -> None:
        visited_pages: set[str] = set()
        queued_pages: list[tuple[str, int]] = [(start_url, 0)]
        downloaded_urls: set[str] = set()
        root_domain = urllib.parse.urlparse(start_url).netloc.lower()
        page_hits = 0
        image_hits = 0

        try:
            while queued_pages and page_hits < max_pages:
                current_url, depth = queued_pages.pop(0)
                if current_url in visited_pages:
                    continue
                visited_pages.add(current_url)
                page_hits += 1
                self.log_queue.put(f"Scanne Seite {page_hits}/{max_pages}: {current_url}")

                try:
                    html = self._fetch_html(current_url)
                    parser = ImageAssetParser()
                    parser.feed(html)

                    for asset in parser.asset_links:
                        normalized_asset = self._normalize_asset_url(asset, current_url)
                        if not normalized_asset or normalized_asset in downloaded_urls:
                            continue
                        if not self.download_external_images.get():
                            asset_domain = urllib.parse.urlparse(normalized_asset).netloc.lower()
                            if asset_domain and asset_domain != root_domain:
                                continue
                        if not self._asset_matches_types(normalized_asset, allowed_types):
                            continue
                        try:
                            saved_path = self._download_asset(normalized_asset, output_dir, allowed_types)
                            if saved_path:
                                downloaded_urls.add(normalized_asset)
                                image_hits += 1
                                self.log_queue.put(f"Bild gespeichert: {saved_path.name}")
                        except Exception as exc:
                            self.log_queue.put(f"Fehler beim Download {normalized_asset}: {exc}")

                    if self.crawl_subpages.get() and depth < max_depth:
                        for link in parser.page_links:
                            normalized_link = self._normalize_page_url(link, current_url)
                            if not normalized_link or normalized_link in visited_pages:
                                continue
                            parsed = urllib.parse.urlparse(normalized_link)
                            if self.same_domain_only.get() and parsed.netloc.lower() != root_domain:
                                continue
                            if self._looks_like_binary_page(normalized_link):
                                continue
                            queued_pages.append((normalized_link, depth + 1))
                except Exception as exc:
                    self.log_queue.put(f"Fehler bei Seite {current_url}: {exc}")

            self.root.after(
                0,
                lambda: self._set_status(
                    f"Fertig. {image_hits} Bilder gespeichert, {len(visited_pages)} Seiten besucht."
                ),
            )
        finally:
            self.root.after(0, self._finish)

    def _fetch_html(self, url: str) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=20) as response:
            content_type = response.headers.get("Content-Type", "")
            if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                raise RuntimeError(f"Kein HTML-Dokument: {content_type}")
            raw = response.read()
        return self._decode_html(raw, content_type)

    def _decode_html(self, raw: bytes, content_type: str) -> str:
        charset = None
        match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type, flags=re.IGNORECASE)
        if match:
            charset = match.group(1).strip("\"' ")
        for candidate in [charset, "utf-8", "cp1252", "latin-1"]:
            if not candidate:
                continue
            try:
                return raw.decode(candidate, errors="replace")
            except LookupError:
                continue
        return raw.decode("utf-8", errors="replace")

    def _normalize_page_url(self, url: str, base_url: str) -> str | None:
        return self._normalize_url(url, base_url, keep_query=False)

    def _normalize_asset_url(self, url: str, base_url: str) -> str | None:
        return self._normalize_url(url, base_url, keep_query=True)

    def _normalize_url(self, url: str, base_url: str, keep_query: bool) -> str | None:
        if not url:
            return None
        raw = url.strip()
        if raw.startswith("data:") or raw.startswith("javascript:") or raw.startswith("mailto:"):
            return None
        joined = urllib.parse.urljoin(base_url, raw)
        parsed = urllib.parse.urlparse(joined)
        if parsed.scheme not in {"http", "https"}:
            return None
        cleaned = parsed._replace(fragment="")
        if not keep_query:
            cleaned = cleaned._replace(query="")
        return cleaned.geturl()

    def _parse_allowed_types(self, raw: str) -> set[str]:
        result: set[str] = set()
        for part in raw.split(","):
            item = part.strip().lower().lstrip(".")
            if item:
                result.add(item)
        return result

    def _asset_matches_types(self, asset_url: str, allowed_types: set[str]) -> bool:
        parsed = urllib.parse.urlparse(asset_url)
        suffix = Path(parsed.path).suffix.lower().lstrip(".")
        if suffix:
            return suffix in allowed_types
        return True

    def _looks_like_binary_page(self, url: str) -> bool:
        suffix = Path(urllib.parse.urlparse(url).path).suffix.lower().lstrip(".")
        if not suffix:
            return False
        return suffix in {
            "jpg",
            "jpeg",
            "png",
            "webp",
            "gif",
            "svg",
            "pdf",
            "zip",
            "mp4",
            "mp3",
            "webm",
            "ico",
        }

    def _download_asset(self, asset_url: str, output_dir: Path, allowed_types: set[str]) -> Path | None:
        request = urllib.request.Request(asset_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            ext = self._guess_extension(asset_url, content_type)
            if not ext:
                return None
            if ext.lstrip(".").lower() not in allowed_types:
                return None
            content_length = response.headers.get("Content-Length", "").strip()
            if content_length:
                try:
                    if int(content_length) > MAX_DOWNLOAD_BYTES:
                        raise RuntimeError(
                            f"Datei zu gross ({content_length} Bytes). Limit: {MAX_DOWNLOAD_BYTES} Bytes."
                        )
                except ValueError:
                    pass

        filename = self._build_filename(asset_url, ext)
        target = self._unique_path(output_dir / filename)
        bytes_written = 0
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                with target.open("wb") as handle:
                    while True:
                        chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                        if not chunk:
                            break
                        bytes_written += len(chunk)
                        if bytes_written > MAX_DOWNLOAD_BYTES:
                            raise RuntimeError(
                                f"Download abgebrochen: Datei groesser als {MAX_DOWNLOAD_BYTES} Bytes."
                            )
                        handle.write(chunk)
        except Exception:
            if target.exists():
                target.unlink()
            raise
        return target

    def _guess_extension(self, asset_url: str, content_type: str) -> str | None:
        parsed = urllib.parse.urlparse(asset_url)
        suffix = Path(parsed.path).suffix.lower()
        if suffix:
            return suffix
        if content_type in IMAGE_MIME_TO_EXT:
            return IMAGE_MIME_TO_EXT[content_type]
        guessed = mimetypes.guess_extension(content_type or "")
        if guessed:
            return guessed
        return None

    def _build_filename(self, asset_url: str, ext: str) -> str:
        parsed = urllib.parse.urlparse(asset_url)
        stem = Path(parsed.path).stem.strip()
        if not stem:
            stem = "image"
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "image"

        query_part = ""
        if parsed.query:
            query_part = "_" + re.sub(r"[^A-Za-z0-9]+", "_", parsed.query)[:40].strip("_")

        return f"{safe_stem}{query_part}{ext}"

    def _unique_path(self, target: Path) -> Path:
        if not target.exists():
            return target
        stem = target.stem
        suffix = target.suffix
        counter = 2
        while True:
            candidate = target.with_name(f"{stem}_{counter}{suffix}")
            if not candidate.exists():
                return candidate
            counter += 1


def main() -> None:
    root = tk.Tk()
    root.option_add("*Font", ("Segoe UI", 10))
    ImageCrawlerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

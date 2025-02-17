import os
from dataclasses import dataclass
from pathlib import PurePath
from typing import List, Set, Union
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..config import Config
from ..logging import ProgressBar, log
from ..output_dir import FileSink
from ..utils import soupify
from .crawler import CrawlError
from .http_crawler import HttpCrawler, HttpCrawlerSection


class KitIpdCrawlerSection(HttpCrawlerSection):
    def target(self) -> str:
        target = self.s.get("target")
        if not target:
            self.missing_value("target")

        if not target.startswith("https://"):
            self.invalid_value("target", target, "Should be a URL")

        return target


@dataclass
class KitIpdFile:
    name: str
    url: str


@dataclass
class KitIpdFolder:
    name: str
    files: List[KitIpdFile]


class KitIpdCrawler(HttpCrawler):

    def __init__(
            self,
            name: str,
            section: KitIpdCrawlerSection,
            config: Config,
    ):
        super().__init__(name, section, config)
        self._url = section.target()

    async def _run(self) -> None:
        maybe_cl = await self.crawl(PurePath("."))
        if not maybe_cl:
            return

        folders: List[KitIpdFolder] = []

        async with maybe_cl:
            folder_tags = await self._fetch_folder_tags()
            folders = [self._extract_folder(tag) for tag in folder_tags]

        tasks = [self._crawl_folder(folder) for folder in folders]

        await self.gather(tasks)

    async def _crawl_folder(self, folder: KitIpdFolder) -> None:
        path = PurePath(folder.name)
        if not await self.crawl(path):
            return

        tasks = [self._download_file(path, file) for file in folder.files]

        await self.gather(tasks)

    async def _download_file(self, parent: PurePath, file: KitIpdFile) -> None:
        element_path = parent / file.name
        maybe_dl = await self.download(element_path)
        if not maybe_dl:
            return

        async with maybe_dl as (bar, sink):
            await self._stream_from_url(file.url, sink, bar)

    async def _fetch_folder_tags(self) -> Set[Tag]:
        page = await self.get_page()
        elements: List[Tag] = self._find_file_links(page)
        folder_tags: Set[Tag] = set()

        for element in elements:
            enclosing_data: Tag = element.findParent(name="td")
            label: Tag = enclosing_data.findPreviousSibling(name="td")
            folder_tags.add(label)

        return folder_tags

    def _extract_folder(self, folder_tag: Tag) -> KitIpdFolder:
        name = folder_tag.getText().strip()
        files: List[KitIpdFile] = []

        container: Tag = folder_tag.findNextSibling(name="td")
        for link in self._find_file_links(container):
            files.append(self._extract_file(link))

        log.explain_topic(f"Found folder {name!r}")
        for file in files:
            log.explain(f"Found file {file.name!r}")

        return KitIpdFolder(name, files)

    def _extract_file(self, link: Tag) -> KitIpdFile:
        name = link.getText().strip()
        url = self._abs_url_from_link(link)
        _, extension = os.path.splitext(url)
        return KitIpdFile(name + extension, url)

    def _find_file_links(self, tag: Union[Tag, BeautifulSoup]) -> List[Tag]:
        return tag.findAll(name="a", attrs={"href": lambda x: x and "intern" in x})

    def _abs_url_from_link(self, link_tag: Tag) -> str:
        return urljoin(self._url, link_tag.get("href"))

    async def _stream_from_url(self, url: str, sink: FileSink, bar: ProgressBar) -> None:
        async with self.session.get(url, allow_redirects=False) as resp:
            if resp.status == 403:
                raise CrawlError("Received a 403. Are you within the KIT network/VPN?")
            if resp.content_length:
                bar.set_total(resp.content_length)

            async for data in resp.content.iter_chunked(1024):
                sink.file.write(data)
                bar.advance(len(data))

            sink.done()

    async def get_page(self) -> BeautifulSoup:
        async with self.session.get(self._url) as request:
            return soupify(await request.read())
